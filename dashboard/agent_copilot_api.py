"""
Agent Copilot API — conversational agent builder mounted into the dashboard.

Exposes a ChatGPT-style copilot that creates / edits / manages voice agents by
interviewing the user, then writing through the SAME plumbing the Agent
Builder uses (AgentDefinition schema + agents/<slug>/agent.yaml + registry).

Endpoints
---------
POST /api/copilot/chat                  {session_id, message} -> {reply, actions, agents_changed}
GET  /api/copilot/sessions/{session_id} -> {transcript: [...]}  (for page reload)

Design notes
------------
* Server-side agentic loop (max ~8 tool iterations per user turn) against the
  OpenAI-compatible endpoint configured in calling-agent/.env
  (LLM_BASE_URL / LLM_API_KEY / LLM_MODEL — Groq in production).
* Tools reuse agent_builder_api's handler coroutines where they are plain
  async functions (list/get/activate/get_routing). Handlers that need a
  fastapi Request (create/update/validate/set_routing) are replicated in a
  few lines on top of AgentDefinition / agent_registry — same code paths,
  same files on disk.
* create fills unspecified sections with sensible defaults modeled on
  agents/dhronai_ea/agent.yaml, so the LLM only has to draft the
  business-specific content (greeting, stage goals, knowledge, objections).
* update NEVER replaces from scratch: it loads the current full definition,
  DEEP-MERGES the partial changes onto it, re-validates via
  AgentDefinition.from_dict, then writes — calendar / slots /
  qa_field_signals / stt_corrections etc. always survive an edit.
* Sessions: in-memory dict + JSON persistence under dashboard/copilot_sessions/
  (lazily reloaded, so a dashboard restart keeps conversations).
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request

logger = logging.getLogger("agent_copilot_api")

# agent_builder_api already puts calling-agent on sys.path and loads .env.
import agent_builder_api as builder                                 # noqa: E402
from agent_builder_api import _AGENTS_DIR, _read_active             # noqa: E402
from backend.app.agent_definition.schema import (                   # noqa: E402
    AgentDefinition, AgentIdentity, PersonalityConfig, LanguageConfig,
    TTSVoiceConfig, CallFlowConfig, StageDefinition, SlotDefinition,
    KnowledgeBase, ResponseBank, CalendarConfig, LLMConfig,
    BrandConfig, OutboundConfig, GuardrailsConfig, FollowUpConfig,
    PostCallConfig,
)
from backend.app.agent_definition.registry import agent_registry    # noqa: E402
from backend.app.agent_definition.voice_catalog import (            # noqa: E402
    DEFAULT_TTS_MODEL, RECOMMENDED_SPEAKERS, SPEAKER_CATALOG,
    recommended_speaker, speaker_gender,
)
from backend.app.agent_definition.boot_check import engine_boot_check  # noqa: E402
from backend.app.agent_definition import spoken_surfaces as _surfaces  # noqa: E402
from backend.app.agent_definition import controls as _controls  # noqa: E402
from backend.app.agent_definition import store as agent_store       # noqa: E402
from backend.app.agent_definition.diff import (                     # noqa: E402
    apply_ops as _apply_ops, compute_diff as _compute_diff,
    render_human as _render_diff,
)
import agent_builder_api as builder                                 # noqa: E402
from copilot_interview import (                                     # noqa: E402
    INTERVIEW_LAYERS, interview_progress, render_interview_state,
)

_UPLOADS_DIR = Path(__file__).parent / "copilot_uploads"


def _interview_state(session: dict) -> dict:
    """Lazy interview state on a session (old session files unaffected)."""
    return session.setdefault(
        "interview", {"draft": {}, "skipped": [], "provenance": {}, "phase": "idle"})


async def _copilot_tenancy_dep(request: Request):
    """Resolve owner, propagate to the builder's ContextVar (Copilot creates
    agents through builder handlers, which must claim the slug for THIS tenant),
    and enforce per-owner session access. Async on purpose — see builder."""
    import auth
    owner = auth.current_owner(request)
    request.state.owner = owner
    builder._REQ_OWNER.set(owner)
    sid = request.path_params.get("session_id")
    if sid and not _session_owner_ok(sid, owner):
        raise HTTPException(status_code=404, detail="session not found")


router = APIRouter(dependencies=[Depends(_copilot_tenancy_dep)])

_SESSIONS_DIR = Path(__file__).parent / "copilot_sessions"
_MINE_DUMP_MIN_CHARS = 1500  # a paste this big during a build is mined server-side
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")


def _gate_owner(request):
    owner = getattr(request.state, "owner", None)
    if owner:
        return owner
    try:
        import auth
        return auth.current_owner(request)
    except Exception:
        return None


def _billing_llm_gate(request, surface: str, request_tokens: int = 0) -> None:
    """E9/E10: charge one LLM turn against the tenant's daily budget.

    Raises BillingRefusal (402) when the budget is spent. The refusal is an
    HTTPException subclass on purpose — the copilot's outer handler turns bare
    exceptions into a chat reply string, which would have shown the user a
    stack-trace-ish message instead of an upgrade prompt.

    `request_tokens` is the size of what the user just sent, so the per-message
    ceiling can refuse an enormous paste BEFORE it is forwarded to the model.
    Checking it afterwards would be theatre: the shared model quota is already
    spent by then, and that quota is the same one live phone calls draw on.
    """
    try:
        from shared.billing.entitlements import (
            check_copilot_budget, record_copilot_turn)
    except Exception:
        return
    owner = _gate_owner(request)
    if not owner:
        return
    check_copilot_budget(owner, request_tokens).raise_for_http()
    record_copilot_turn(owner, surface)


def _remaining_tokens(owner) -> int | None:
    """Today's unspent token budget, or None when nothing caps this tenant.

    Read once per turn. Returns None on any failure — a budget that cannot be
    read must not become a wall in the middle of someone's work.
    """
    if not owner:
        return None
    try:
        from shared.billing.entitlements import (
            copilot_tokens_used, effective_limits)
        cap = effective_limits(owner)["copilot_tokens_per_day"]
        if cap is None:
            return None
        return max(0, int(cap) - copilot_tokens_used(owner))
    except Exception as exc:
        logger.debug("[copilot] token budget lookup failed for %s: %r", owner, exc)
        return None


def _charge_tokens(owner, usage) -> int:
    """Bill one LLM response's usage to today's token budget; return the total.

    Called for EVERY provider round, not once per user turn — a single turn is
    a tool loop of up to _MAX_TOOL_ROUNDS requests, and charging only the last
    one would under-count the expensive turns by the exact factor that makes
    them worth capping.

    `usage` is whatever the OpenAI-compatible endpoint returned. It is read
    defensively: Groq, Gemini's compat layer and the OpenAI fallback all shape
    it slightly differently, and a missing field must cost a count, never a
    reply.
    """
    if not owner or usage is None:
        return 0
    try:
        from shared.billing.entitlements import record_copilot_tokens
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        total = int(getattr(usage, "total_tokens", 0) or 0)
        record_copilot_tokens(owner, prompt_tokens=prompt,
                              completion_tokens=completion, total_tokens=total)
        return max(total, prompt + completion)
    except Exception as exc:
        logger.debug("[copilot] token charge failed for %s: %r", owner, exc)
        return 0
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{1,60}$")
# The only languages the voice engine can actually speak.
_PLATFORM_LANGUAGES = frozenset({"hi", "en", "gu"})
_MAX_TOOL_ITERATIONS = 8
# Read-only rounds no longer spend the budget. The system prompt ORDERS the
# model to verify its own work (find_text before and after a wording sweep,
# preview_opening after a greeting edit) and then charged it for obeying: a
# single "change both directions" request costs get_agent ×2 + update ×2
# before any verification, so the mandated checks were what got cut. Writes
# still cap at _MAX_TOOL_ITERATIONS; _MAX_TOOL_ROUNDS is the anti-runaway
# ceiling for read loops.
_READ_ONLY_TOOLS = frozenset({
    "get_agent", "list_agents", "preview_opening", "find_text",
    "validate_agent", "get_routing", "list_revisions", "list_controls",
})
_MAX_TOOL_ROUNDS = 20
# Same read, same arguments, three times in one turn = a loop, not research.
_MAX_IDENTICAL_READS = 2
_CONTEXT_MAX_MESSAGES = 48  # trim very long sessions before sending to the LLM


# ─────────────────────────────────────────────────────────────────────────────
# LLM client (OpenAI-compatible — Groq in production)
# ─────────────────────────────────────────────────────────────────────────────
_client = None


def _env(name: str, default: str = "") -> str:
    """COPILOT_LLM_* overrides win over the voice pipeline's LLM_* config,
    so the copilot can run on a different model/key without touching the
    live calling pipeline (Groq rate limits are per-model)."""
    return (os.environ.get(f"COPILOT_{name}") or os.environ.get(name) or default).strip()


def _get_client():
    global _client
    if _client is None:
        from openai import AsyncOpenAI
        base_url = _env("LLM_BASE_URL")
        api_key = _env("LLM_API_KEY")
        if not base_url or not api_key:
            raise HTTPException(
                status_code=503,
                detail="Copilot LLM not configured — set LLM_BASE_URL / LLM_API_KEY in calling-agent/.env",
            )
        # Generous retries: Groq free tier rate-limits aggressively (429 with
        # Retry-After up to ~60s) and the SDK honors Retry-After between tries.
        _client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=120.0, max_retries=5)
    return _client


def _model_name() -> str:
    return _env("LLM_MODEL", "llama-3.3-70b-versatile")


# ─────────────────────────────────────────────────────────────────────────────
# Default agent definition (modeled on agents/dhronai_ea/agent.yaml)
# ─────────────────────────────────────────────────────────────────────────────
_LANG_STRINGS: dict[str, dict[str, Any]] = {
    "en": {
        "fillers": ["One moment please...", "Sure, one second..."],
        "lang_fallback": "Sorry, could you repeat that?",
        "timeout_fallback": "Let me get back to you on that.",
        "decline_farewell": "No problem at all — thank you for your time, have a great day!",
        "graceful_close": "Thank you so much for your time. Have a wonderful day!",
        "lang_switch": "Sure, let's continue in English.",
        "slots_text": "Tomorrow at 11:00 AM, Tomorrow at 4:00 PM, or This Saturday at 12:00 PM",
        "register_note": (
            "Use natural, warm, professional English — courteous and concise, "
            "like a polished front-desk professional. Never corporate or robotic."
        ),
    },
    "hi": {
        "fillers": ["एक मिनट...", "हाँ, एक सेकंड..."],
        "lang_fallback": "माफ़ कीजिए, क्या आप दोबारा बोल सकते हैं?",
        "timeout_fallback": "मैं इस पर वापस आती हूँ।",
        "decline_farewell": "कोई बात नहीं, आपके समय के लिए धन्यवाद। आपका दिन शुभ रहे!",
        "graceful_close": "आपके समय के लिए बहुत धन्यवाद! आपका दिन शुभ हो।",
        "lang_switch": "जी बिल्कुल, हम हिंदी में बात करते हैं।",
        "slots_text": "कल सुबह 11 बजे, कल शाम 4 बजे, या इस शनिवार दोपहर 12 बजे",
        "register_note": (
            "Use natural, modern conversational Hindi — polite and warm. Code-switch to "
            "everyday English words Indians use ('appointment', 'time', 'service'). "
            "Avoid formal/Sanskritized vocabulary. Address the caller respectfully (aap)."
        ),
    },
    "gu": {
        "fillers": ["એક મિનિટ...", "હા, એક સેકંડ..."],
        "lang_fallback": "માફ કરશો, ફરી બોલશો?",
        "timeout_fallback": "હું આના પર પાછી આવું છું.",
        "decline_farewell": "કંઈ વાંધો નહીં, તમારા સમય માટે આભાર. તમારો દિવસ શુભ રહે!",
        "graceful_close": "તમારા સમય માટે ખૂબ ખૂબ આભાર! તમારો દિવસ શુભ રહે.",
        "lang_switch": "ચોક્કસ, આપણે ગુજરાતીમાં વાત કરીએ.",
        "slots_text": "કાલે સવારે 11 વાગ્યે, કાલે સાંજે 4 વાગ્યે, અથવા આ શનિવારે બપોરે 12 વાગ્યે",
        "register_note": (
            "Use natural conversational Gujarati — warm and polite. Keep technical/"
            "business terms in English. Address the caller respectfully (tame)."
        ),
    },
}

_DEFAULT_STAGES = [
    {
        "id": "GREETING",
        "order": 0,
        "goal": (
            "Warmly greet the caller in their language, introduce yourself and the company "
            "in one short sentence, and ask how you can help — ONE question only."
        ),
        "turns_until_advance": 1,
    },
    {
        "id": "DISCOVERY",
        "order": 1,
        "goal": (
            "Understand what the caller needs. Ask ONE short question at a time. "
            "Acknowledge each answer before the next question. Once their need is clear, "
            "move toward proposing a concrete next step."
        ),
        "turns_until_advance": 6,
    },
    {
        "id": "INVITE_VISIT",
        "order": 2,
        "goal": (
            "The caller is interested. Propose a concrete next step (appointment / visit / "
            "callback) and offer EXACTLY TWO specific options from the available slots. "
            "When they pick one, confirm the day and time."
        ),
        "turns_until_advance": 99,
    },
    {
        "id": "CONFIRMATION",
        "order": 3,
        "goal": (
            "Confirm the booking details clearly: day, time, and the caller's name. "
            "Ask for the best phone number or WhatsApp to send the confirmation."
        ),
        "turns_until_advance": 99,
    },
    {
        "id": "CLOSING",
        "order": 4,
        "goal": (
            "Warm, genuine close using the caller's name. Thank them and let them know "
            "they can call back any time."
        ),
        "turns_until_advance": 99,
    },
]

_DEFAULT_OUTBOUND_STAGES = [
    {
        "id": "OPENING",
        "order": 0,
        "goal": (
            "You placed this call. Confirm you're speaking with the right person, introduce "
            "yourself and who you represent in ONE short sentence, and ask if it's a good time "
            "to talk. Exactly ONE question — then wait."
        ),
        # 2, not 1: stage advancement runs before the turn's prompt is built,
        # so a threshold of 1 would skip this stage's goal entirely.
        "turns_until_advance": 2,
    },
    {
        "id": "PITCH",
        "order": 1,
        "goal": (
            "Deliver the core message of the call. State plainly why you're calling, then make "
            "your case using the SPECIFIC talking points in the KNOWLEDGE section — lead with the "
            "single strongest point, then support it with the others. Keep each turn to 2-3 "
            "sentences, and check they're following before piling on more."
        ),
        "turns_until_advance": 6,
    },
    {
        "id": "HANDLE_QUESTIONS",
        "order": 2,
        "goal": (
            "Answer questions and push-back directly and confidently using the facts you have. "
            "Acknowledge the concern first, then counter it with one concrete, specific point. "
            "Stay warm and never defensive; if you don't know something, say you'll follow up."
        ),
        # Finite: with 99 here the ASK stage — the whole point of an outbound
        # call — was unreachable (turn-count is the only advancement path).
        "turns_until_advance": 3,
    },
    {
        "id": "ASK",
        "order": 3,
        "goal": (
            "Make the specific ask this call exists for — clearly and directly. State exactly the "
            "outcome, decision, or next step you want from them, and give them the chance to agree."
        ),
        "turns_until_advance": 4,
    },
    {
        "id": "CLOSING",
        "order": 4,
        "goal": (
            "Thank them genuinely by name, restate any agreed next step in one sentence, and close "
            "warmly. Leave a positive final impression."
        ),
        "turns_until_advance": 99,
    },
]

_DEFAULT_OBJECTION_PATTERNS = [
    "not interested", "busy right now", "call later", "don't need", "no time",
    "don't want", "stop calling", "talk later", "later please", "maybe later",
    "some other time", "another time", "not right now", "call me later",
    "call back later", "baad mein baat", "phir baat", "abhi nahi", "abhi busy",
]

_DEFAULT_TIME_PATTERNS = [
    "morning", "afternoon", "evening", "tomorrow", "today", "this week", "next week",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "10 am", "11 am", "12 pm", "2 pm", "3 pm", "4 pm",
    "subah", "dopahar", "shaam", "kal", "parso", "aaj",
]


def _default_definition(name: str, provided: dict) -> dict:
    """Full, schema-valid default definition; the LLM's fields are merged on top."""
    langs = provided.get("languages")
    if not isinstance(langs, dict):  # belt-and-braces; _normalize_definition fixes this upstream
        langs = {"supported": langs} if isinstance(langs, list) else {}
    supported = [l for l in (langs.get("supported") or ["en"]) if isinstance(l, str)] or ["en"]
    default_lang = langs.get("default") or supported[0]
    if default_lang not in supported:
        default_lang = supported[0]

    def s(lang: str, key: str):
        return (_LANG_STRINGS.get(lang) or _LANG_STRINGS["en"])[key]

    # ONE speaker across all languages (mid-call speaker switches change the
    # person), picked for the DEFAULT language per Sarvam's CER tables —
    # the old hardcoded simran spoke English below its recommended tier.
    # The voice's gender defaults to the first person the owner WROTE ("करण
    # बोल रहा हूँ" → male); female only when the script commits to neither.
    # Any voice the user actually chose is merged on top of this default.
    _gender = _authored_self_gender(_spoken_texts_of(provided)) or "female"
    _speaker = recommended_speaker(default_lang, _gender)
    tts_voices = {l: {"model": "bulbul:v3", "speaker": _speaker, "pace": 1.15} for l in supported}
    _pron = "he" if _gender == "male" else "she"
    register_notes = {l: s(l, "register_note") for l in supported}

    # Outbound agents lead the call and need a pitch-shaped flow, not the
    # inbound welcome→discovery→book skeleton. Pick the skeleton that matches
    # what the platform will actually do on the call.
    direction = str(provided.get("call_direction") or "inbound").strip().lower()
    base_stages = _DEFAULT_OUTBOUND_STAGES if direction == "outbound" else _DEFAULT_STAGES

    pretty = name.replace("_", " ").replace("-", " ").title()
    return {
        "agent_id": f"{name}-v1",
        "version": "1.0.0",
        "identity": {
            "agent_name": pretty,
            "company_name": pretty,
            "company_tagline": pretty,
            "role_description": "inbound customer assistant",
            "contact": "",
            "pronouns": _pron,
        },
        "personality": {
            "pronouns": _pron,
            "energy": "medium",
            "tone": "conversational",
            "style": "natural",
            "lingo": "",
            "custom_instructions": "Keep every reply to 1-2 short spoken sentences. Be warm and never pushy.",
        },
        "languages": {
            "supported": supported,
            "default": default_lang,
            "stt_forced_language": "",
            "allow_code_mixing": default_lang == "hi",
            "tts_voices": tts_voices,
            "register_notes": register_notes,
            # F15: the farewell speaks the call's dominant language, not
            # whatever a misdetected final "bye bye thank you" flipped to.
            "closing_language_policy": "dominant",
        },
        "call_flow": {
            "stages": copy.deepcopy(base_stages),
            "decline_threshold": 2,
        },
        "knowledge_base": {
            "known_projects": {},
            "locations": [],
            "portfolio_prompt_text": "No specific products configured. Use general knowledge to assist the caller.",
            "portfolio_section_label": "KNOWLEDGE BASE",
            "location_aliases": [],
            "objection_patterns": list(_DEFAULT_OBJECTION_PATTERNS),
            # F15: custom patterns EXTEND the universal deferral set for new
            # agents — authoring 5 domain phrases no longer disables 'not
            # interested' / 'stop calling' / 'busy'.
            "objection_patterns_mode": "extend",
            "time_patterns": list(_DEFAULT_TIME_PATTERNS),
            "qa_field_signals": {
                "name": ["your name", "naam", "aapka naam", "may i know your name", "who am i speaking"],
            },
            "stt_corrections": {
                "city_corrections": {},
                "phonetic_corrections": {},
                "real_estate_corrections": {},
                "project_name_corrections": {},
            },
        },
        "response_bank": {
            "opening_greeting": f"Hello! I'm {pretty}. How can I help you today?",
            "filler_responses": {l: list(s(l, "fillers")) for l in supported},
            "identity_responses": {},
            "objection_responses": {},
            "decline_farewell": {l: s(l, "decline_farewell") for l in supported},
            "lang_switch_responses": {l: s(l, "lang_switch") for l in supported},
            "lang_fallbacks": {l: s(l, "lang_fallback") for l in supported},
            "timeout_fallbacks": {l: s(l, "timeout_fallback") for l in supported},
            "response_fallback": s(default_lang, "lang_fallback"),
            "graceful_close": {l: s(l, "graceful_close") for l in supported},
            "localized_responses": {},
        },
        # F13: the calendar starts EMPTY — a phantom 3-slot default used to
        # ship a live-availability band into flows that never schedule
        # anything. The interview's scheduling layer captures real slots (or
        # an explicit "we don't book"), and mock_slots stay [] otherwise.
        "calendar": {
            "type": "mock",
            "mock_slots": [],
            "slots_text": {},
        },
        "llm_config": {
            # F9: no "model" key — the runtime model is env-governed
            # (LLM_MODEL wins over any per-agent value; conversation.py:588).
            # Writing gpt-4o-mini here was a false affordance users could
            # never actually exercise.
            "max_tokens": 160,
            "timeout_s": 5.0,
            "stream_timeout_s": 7.0,
            # F15: 0.6s sat below the live model's typical first-chunk
            # latency (630-1178ms measured), guaranteeing a filler on every
            # turn; the caps keep fillers occasional instead of a verbal tic.
            "filler_timeout_s": 1.2,
            "filler_min_gap_turns": 2,
            "filler_max_per_call": 3,
            "filler_ack_extended": True,
            "intent_classify_timeout_s": 1.0,
            "max_response_length": 400,
            "extra_never_rules": [],
            "extra_always_rules": ["Always keep replies to 1-2 short spoken sentences."],
        },
        # AgentSpec v2 sections — all-empty defaults (plan §12 ledger):
        # empty = exactly today's engine behaviour.
        "brand": {},
        "outbound": {},
        # F2/F14: new agents get the industry-neutral rules pack and strict
        # knowledge grounding (deflect-not-invent). The Copilot LLM sets
        # builtin_rules_pack back to 'real_estate' for real-estate
        # businesses; user-provided honesty values win on merge.
        "guardrails": {
            "use_builtin_rules": True,
            "builtin_rules_pack": "generic",
            "honesty": {
                "knowledge_grounding": "strict",
                "unknown_answer_policy": (
                    "say you'll check with the team and share exact details "
                    "after the call"),
            },
        },
        "follow_up": {},
        "post_call": {},
    }


def _deep_merge(base: dict, changes: dict) -> dict:
    """Recursively merge `changes` onto `base` in place. Lists/scalars replace."""
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _coerce_obj(value: Any) -> dict:
    """The LLM sometimes passes JSON objects as strings — accept both."""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


# Sections of the definition and the dataclass whose fields define what keys
# are real. Anything else the LLM invents (e.g. response_bank.follow_up) is
# dropped WITH A NOTE so the model can put the content somewhere real.
_SECTION_DATACLASSES: dict[str, type] = {
    "identity": AgentIdentity,
    "personality": PersonalityConfig,
    "languages": LanguageConfig,
    "call_flow": CallFlowConfig,
    "knowledge_base": KnowledgeBase,
    "response_bank": ResponseBank,
    "calendar": CalendarConfig,
    "llm_config": LLMConfig,
    # AgentSpec v2 sections
    "brand": BrandConfig,
    "outbound": OutboundConfig,
    "guardrails": GuardrailsConfig,
    "follow_up": FollowUpConfig,
    "post_call": PostCallConfig,
}
# revision/updated_at/updated_by are server-managed audit fields: they must
# survive the whole-document normalize on update (the current definition
# carries them), but LLM-provided input is stripped of them in create/update.
_SERVER_MANAGED_KEYS = {"revision", "updated_at", "updated_by"}
_TOP_LEVEL_KEYS = {"agent_id", "version", "call_direction", "slots",
                   *_SERVER_MANAGED_KEYS, *_SECTION_DATACLASSES}

# ResponseBank fields that must be per-language dicts. The LLM often sends a
# bare string — wrap it under the default language instead of crashing.
_RB_LANG_STR_FIELDS = (
    "identity_responses", "decline_farewell", "lang_switch_responses",
    "lang_fallbacks", "timeout_fallbacks", "graceful_close", "slots_prompt",
)
_RB_LANG_LIST_FIELDS = ("filler_responses", "objection_responses", "decline_responses")


def _fields_of(cls: type) -> set[str]:
    import dataclasses as _dc
    return {f.name for f in _dc.fields(cls)}


def _dict_typed_fields(cls: type) -> dict[str, bool]:
    """{field: is_plain_dict} for every dict-typed field of a section
    dataclass. A plain `dict` field is STRUCTURAL settings (scheduling,
    honesty, callback…); a `dict[str, …]` one is a per-language spoken
    surface, which _lang_dict later accepts as a bare string too."""
    import dataclasses as _dc
    out: dict[str, bool] = {}
    for f in _dc.fields(cls):
        t = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", str(f.type))
        t = str(t).strip()
        if t == "dict" or t.startswith("dict[") or t.startswith("Dict["):
            out[f.name] = t == "dict"
    return out


# What a bare word in call_flow.scheduling means. Gemini has sent both the
# JSON text of the object and a style/flag word where the object belongs.
_SCHEDULING_WORDS: dict[str, dict] = {
    "flexible": {"style": "flexible"}, "exact_n": {"style": "exact_n"},
    "false": {"enabled": False}, "off": {"enabled": False},
    "none": {"enabled": False}, "no": {"enabled": False},
    "disabled": {"enabled": False},
    "true": {"enabled": True}, "on": {"enabled": True},
    "yes": {"enabled": True}, "enabled": {"enabled": True},
}


def _decode_stringified_objects(d: dict, notes: list[str]) -> None:
    """Nested objects that arrived as JSON TEXT (RUNBOOK §60).

    `call_flow.scheduling: '{ "enabled": false }'` passed every guard: the
    top-level sections are shape-checked, from_dict reads {} for a non-dict
    nested field, and the quality lints then called .get() on the string —
    AFTER the write. create_agent wrote both twins and reported "DID NOT
    HAPPEN"; update_draft recorded the draft and said the same, eight turns
    in a row, and the poisoned draft failed every later turn too. Decode
    what parses to an object; map the scheduling words; drop any other
    non-dict from a STRUCTURAL field with a note. Per-language spoken
    fields are left for _lang_dict, which accepts a bare string on purpose."""
    for section, cls in _SECTION_DATACLASSES.items():
        val = d.get(section)
        if not isinstance(val, dict):
            continue
        for fname, plain in _dict_typed_fields(cls).items():
            if fname not in val:
                continue
            v = val[fname]
            if isinstance(v, dict):
                continue
            if isinstance(v, str):
                text = v.strip()
                if text.startswith("{"):
                    try:
                        parsed = json.loads(text)
                    except ValueError:
                        parsed = None
                    if isinstance(parsed, dict):
                        val[fname] = parsed
                        notes.append(f"{section}.{fname} arrived as JSON text — decoded")
                        continue
                if section == "call_flow" and fname == "scheduling":
                    mapped = _SCHEDULING_WORDS.get(text.lower())
                    if mapped is not None:
                        val[fname] = dict(mapped)
                        notes.append(f"call_flow.scheduling {v!r} read as {mapped}")
                        continue
            if plain:
                val.pop(fname)
                notes.append(
                    f"'{section}.{fname}' had an unusable shape (expected settings, "
                    "not text or a list) — ignored.")


def _default_lang_of(d: dict) -> str:
    langs = d.get("languages")
    if isinstance(langs, dict):
        dl = langs.get("default")
        if isinstance(dl, str) and dl:
            return dl
        sup = langs.get("supported")
        if isinstance(sup, list) and sup:
            return str(sup[0])
    return "en"


# ── F1: alias rewrite — the vocabulary the LLM sees (interview layer ids,
# natural field names) is not the vocabulary the spec accepts. Rename known
# misfires to their canonical paths BEFORE the drop-unknown pass, so a good
# answer recorded under a wrong-but-obvious key lands instead of erroring
# (incident: update_draft({"call_objective": ...}) → everything dropped).
# INVARIANT: no alias key may be a real top-level key (tested), so this can
# never touch a well-formed definition — including full-document normalizes.
#
# alias key → (path tuple, accepted value types)
_TOP_LEVEL_ALIASES: dict[str, tuple[tuple[str, ...], tuple[type, ...]]] = {
    # call objective / mission (the incident class — layer id 'call_objective')
    "call_objective": (("call_flow", "call_goal"), (str,)),
    "objective": (("call_flow", "call_goal"), (str,)),
    "call_goal": (("call_flow", "call_goal"), (str,)),
    "goal": (("call_flow", "call_goal"), (str,)),
    "mission": (("call_flow", "call_goal"), (str,)),
    "direction": (("call_direction",), (str,)),
    # persona scalars
    "tone": (("personality", "tone"), (str,)),
    "energy": (("personality", "energy"), (str,)),
    "style": (("personality", "style"), (str,)),
    "traits": (("personality", "traits"), (list,)),
    "ai_disclosure": (("personality", "ai_disclosure"), (str,)),
    # identity scalars
    "agent_name": (("identity", "agent_name"), (str,)),
    "company": (("identity", "company_name"), (str,)),
    "company_name": (("identity", "company_name"), (str,)),
    "business_name": (("identity", "company_name"), (str,)),
    "role_description": (("identity", "role_description"), (str,)),
    # greeting / closing
    "greeting": (("response_bank", "opening_greeting"), (str,)),
    "opening_line": (("response_bank", "opening_greeting"), (str,)),
    "opening_greeting": (("response_bank", "opening_greeting"), (str,)),
    "closing_responses": (("response_bank", "closing_responses"), (dict,)),
    # knowledge
    "knowledge": (("knowledge_base", "portfolio_prompt_text"), (str,)),
    "knowledge_text": (("knowledge_base", "portfolio_prompt_text"), (str,)),
    "catalog_text": (("knowledge_base", "portfolio_prompt_text"), (str,)),
    "portfolio_prompt_text": (("knowledge_base", "portfolio_prompt_text"), (str,)),
    "catalog": (("knowledge_base", "catalog"), (list,)),
    # call flow / objections / guardrails / brand / calendar lists
    "stages": (("call_flow", "stages"), (list,)),
    "objections": (("response_bank", "objection_playbook"), (list,)),
    "objection_playbook": (("response_bank", "objection_playbook"), (list,)),
    "objection_responses": (("response_bank", "objection_responses"), (list, dict)),
    "never_rules": (("guardrails", "never_rules"), (list,)),
    "always_rules": (("guardrails", "always_rules"), (list,)),
    "trust_proofs": (("brand", "trust_proofs"), (list,)),
    "mock_slots": (("calendar", "mock_slots"), (list,)),
}

# Interview layer ids the LLM may echo back as section names → the section
# their dict content belongs in. ('languages' and real sections need no entry;
# 'call_objective' dicts are unpacked key-by-key below.)
_LAYER_ID_SECTION_ALIASES: dict[str, str] = {
    "business_identity": "identity",
    "persona_voice": "personality",
    "brand_trust": "brand",
    "guardrails_compliance": "guardrails",
    "scheduling_followup": "calendar",
    "knowledge_catalog": "knowledge_base",
    "greeting_close": "response_bank",
    "objections_branching": "response_bank",
}


def _alias_set(d: dict, path: tuple[str, ...], value: Any,
               notes: list[str], alias_key: str) -> None:
    """Place an aliased value at its canonical path. An explicit real value
    already there wins — the alias is dropped with a note, never a clobber."""
    node = d
    for p in path[:-1]:
        nxt = node.get(p)
        if nxt is None:
            nxt = node[p] = {}
        elif not isinstance(nxt, dict):
            notes.append(f"'{alias_key}' ignored — '{p}' has an unusable shape")
            return
        node = nxt
    leaf = path[-1]
    dest = ".".join(path)
    if node.get(leaf) not in (None, "", [], {}):
        notes.append(f"'{alias_key}' ignored — '{dest}' was already provided explicitly")
        return
    node[leaf] = value
    notes.append(f"aliased '{alias_key}' → {dest}")


def _alias_rewrite(d: dict, notes: list[str]) -> None:
    """F1: rename known-wrong top-level keys to canonical spec paths, in place.
    Conservative by design — only unambiguous key+shape pairs move; anything
    else is left for the drop-unknown pass (which notes it with suggestions)."""
    # Dotted spec paths sent as FLAT keys ({"call_flow.stages": [...]}) are
    # expanded into the nested form before anything else — the incident
    # interview (2026-07-15) lost a whole project catalog because
    # "knowledge_base.known_projects" was treated as one unknown key and the
    # closest-match hint suggested the same dotted string straight back.
    for key in [k for k in list(d.keys())
                if isinstance(k, str) and "." in k
                and k.split(".")[0] in _TOP_LEVEL_KEYS]:
        parts = key.split(".")
        val = d.pop(key)
        nested = val
        for p in reversed(parts[1:]):
            nested = {p: nested}
        target = d.setdefault(parts[0], {})
        if isinstance(target, dict):
            _deep_merge(target, nested)
            notes.append(f"expanded dotted key '{key}' into its nested form")
        else:
            d[key] = val  # top-level slot holds a non-dict — leave for the shape pass
    for key in [k for k in d if k in _TOP_LEVEL_ALIASES]:
        path, types = _TOP_LEVEL_ALIASES[key]
        val = d[key]
        if isinstance(val, types) and (val or isinstance(val, (int, float))):
            d.pop(key)
            _alias_set(d, path, val, notes, key)
    # 'data_capture' (layer id) as a slot list
    dc = d.get("data_capture")
    if isinstance(dc, list) and "slots" not in d:
        d["slots"] = d.pop("data_capture")
        notes.append("aliased 'data_capture' → slots")
    # layer-id keys carrying a dict of that layer's settings → their section
    for key in [k for k in d if k in _LAYER_ID_SECTION_ALIASES]:
        val = d[key]
        if isinstance(val, dict) and val:
            section = _LAYER_ID_SECTION_ALIASES[key]
            d.pop(key)
            target = d.setdefault(section, {})
            if isinstance(target, dict):
                for sk, sv in val.items():
                    target.setdefault(sk, sv)
                notes.append(f"aliased '{key}' → {section}")
            else:
                d[key] = val  # section unusable — put it back for the shape pass
    # 'call_objective'/'objective' as a dict: unpack the two known subkeys
    for key in ("call_objective", "objective"):
        val = d.get(key)
        if isinstance(val, dict):
            d.pop(key)
            g = val.get("call_goal") or val.get("goal") or val.get("objective")
            if isinstance(g, str) and g.strip():
                _alias_set(d, ("call_flow", "call_goal"), g, notes, f"{key}.goal")
            cd = val.get("call_direction") or val.get("direction")
            if isinstance(cd, str) and cd.strip():
                _alias_set(d, ("call_direction",), cd, notes, f"{key}.direction")


def _canonical_paths() -> list[str]:
    """Every real setting path ('call_flow.call_goal', 'slots', …) — the
    close-match vocabulary for drop notes."""
    paths = sorted(_TOP_LEVEL_KEYS - _SERVER_MANAGED_KEYS)
    for section, cls in _SECTION_DATACLASSES.items():
        paths.extend(f"{section}.{f}" for f in sorted(_fields_of(cls)))
    return paths


def _closest_paths(key: str, n: int = 2) -> list[str]:
    import difflib
    paths = _canonical_paths()
    # Match on leaf names AND known alias names, so both 'call_goal' and a
    # near-miss like 'call_objektive' resolve to 'call_flow.call_goal'.
    vocab = {p.rsplit(".", 1)[-1]: p for p in paths}
    vocab.update({alias: ".".join(path)
                  for alias, (path, _t) in _TOP_LEVEL_ALIASES.items()})
    vocab.update({lid: section for lid, section in _LAYER_ID_SECTION_ALIASES.items()})
    hits: list[str] = []
    for m in difflib.get_close_matches(key, list(vocab), n=n, cutoff=0.6):
        if vocab[m] not in hits:
            hits.append(vocab[m])
    hits += [p for p in difflib.get_close_matches(key, paths, n=n, cutoff=0.6)
             if p not in hits]
    return hits[:n]


# The FOUR template placeholders the runtime understands, in ANY authored
# spoken line (conversation.py _substitute_tokens, called from _lang_response):
#   {contact_name} — the outbound contact's name; with none on file an
#                    identity-question clause is dropped (RUNBOOK §42)
#   {current_time} — morning/afternoon/evening in IST at call time
#   {honorific}    — "Sir"/"Ma'am" from contacts.gender, standing alone
#   {title}        — "Mr."/"Mrs." from contacts.gender, before the name
# The last two resolve to NOTHING when that contact has no gender (the token
# and the seam it leaves are removed).
# LLMs constantly invent other syntaxes — ${contact_name}, {{contact_name}},
# [contact name], {time_of_day} — which would be SPOKEN LITERALLY on a real
# call. Canonicalize them all.
_PLACEHOLDER_RE = re.compile(
    r"(?:\$\{|\{\{|\{|\[|<)\s*"
    r"(?:contact[ _\-]?name|customer[ _\-]?name|person[ _\-]?name|callee[ _\-]?name|contact|name)"
    r"\s*(?:\}\}|\}|\]|>)"
    r"|\$contact[ _\-]?name\b",
    re.IGNORECASE,
)

_TIME_PLACEHOLDER_RE = re.compile(
    r"(?:\$\{|\{\{|\{|\[|<)\s*"
    r"(?:current[ _\-]?time|time[ _\-]?of[ _\-]?day|greeting[ _\-]?time|day[ _\-]?part)"
    r"\s*(?:\}\}|\}|\]|>)"
    r"|\$current[ _\-]?time\b",
    re.IGNORECASE,
)


# Mirrors conversation.ConversationRouter._HONORIFIC_PH_RE — the two must agree
# about what the engine will substitute, or the copilot writes a token that the
# runtime strips (silently losing the author's intent) or lints a token that
# actually works.
_HONORIFIC_PLACEHOLDER_RE = re.compile(
    r"(?:\$\{|\{\{|\{|\[|<)\s*"
    r"(?:honorific|sir[ _\-/]?(?:or[ _\-/]?)?ma['\u2019 ]?am|"
    r"gender[ _\-]?(?:honorific|salutation)|salutation)"
    r"\s*(?:\}\}|\}|\]|>)"
    r"|\$honorific\b",
    re.IGNORECASE,
)

# Mirrors ConversationRouter._TITLE_PH_RE — the name-prefix half of gendered
# address ("Mr. Rajesh"), distinct from the standalone {honorific} ("Yes Sir").
_TITLE_PLACEHOLDER_RE = re.compile(
    r"(?:\$\{|\{\{|\{|\[|<)\s*"
    r"(?:title|mr\.?[ _\-/]*(?:or[ _\-/]+)?mr?s\.?|"
    r"name[ _\-]?(?:title|prefix)|gender[ _\-]?title|prefix)"
    r"\s*(?:\}\}|\}|\]|>)"
    r"|\$title\b",
    re.IGNORECASE,
)


def _canonicalize_placeholders(text: str) -> str:
    text = _PLACEHOLDER_RE.sub("{contact_name}", text)
    text = _TIME_PLACEHOLDER_RE.sub("{current_time}", text)
    text = _HONORIFIC_PLACEHOLDER_RE.sub("{honorific}", text)
    return _TITLE_PLACEHOLDER_RE.sub("{title}", text)


def _any_placeholder(text: str) -> bool:
    return bool(_PLACEHOLDER_RE.search(text) or _TIME_PLACEHOLDER_RE.search(text)
                or _HONORIFIC_PLACEHOLDER_RE.search(text)
                or _TITLE_PLACEHOLDER_RE.search(text))


# The exact spellings conversation._substitute_tokens resolves in every
# authored line, plus {trust_proofs}, which _match_objection_playbook fills.
_RESOLVED_TOKENS = ("{contact_name}", "{current_time}", "{honorific}",
                    "{title}", "{trust_proofs}")
# Exact form only: "${contact_name}" and "{{contact_name}}" contain the
# canonical spelling but are NOT it, and the engine resolves neither.
_RESOLVED_TOKEN_RE = re.compile(
    r"(?<![${])(?:" + "|".join(re.escape(t) for t in _RESOLVED_TOKENS) + r")(?!\})")
_BRACE_TOKEN_RE = re.compile(r"\$?\{\{?\s*[A-Za-z_][A-Za-z0-9_ \-]*\s*\}?\}")


def _unresolvable_placeholder(text: str) -> bool:
    """A placeholder the engine will NOT resolve — a non-canonical spelling
    of a known one (${contact_name}, [name]) or a token it has never heard of
    ({company}). The canonical four are fine anywhere a line is spoken."""
    rest = _RESOLVED_TOKEN_RE.sub("", str(text or ""))
    return bool(_any_placeholder(rest) or _BRACE_TOKEN_RE.search(rest))


# ── what the user dictated, and which beat they meant ───────────────────────
# Two structural guards against the failure the owner hit five times across
# three agents: a sentence they gave word-for-word was saved into a field the
# caller never hears (a stage goal), or into the wrong beat (the opening line
# instead of the line after the caller responds). Both looked like success.

# "after they say hello", "once the caller responds", "then say", "second line",
# "wait for the caller's response". Three shapes, any one is enough.
_SECOND_BEAT_RE = re.compile(
    # (a) a time word, then someone speaking
    r"(?:\bafter\b|\bonce\b|\bwhen\b|\bthen\b)[^.?!]{0,60}?"
    r"\b(?:say|says|said|saying|speak|speaks|spoke|respond|responds|responded|"
    r"response|reply|replies|replied|answer|answers|answered|"
    r"pick(?:s|ed)?\s*up|hello|hi)\b"
    # (b) the user naming the beat outright
    r"|\bsecond\s+(?:line|sentence|beat|thing)\b"
    # (c) "wait for the caller / their response / them to answer"
    r"|\bwait(?:s|ing)?\s+for\s+(?:the\s+|a\s+|their\s+|his\s+|her\s+)?"
    r"(?:caller|callers|caller's|customer|contact|client|person|them|they|"
    r"response|reply|answer|hello)\b",
    re.IGNORECASE)

# A quoted span, or a line following an explicit dictation cue.
_QUOTED_RE = re.compile(r"[\"“”'‘’]([^\"“”\n]{8,300}?)[\"“”'‘’]")
_DICTATION_CUE_RE = re.compile(
    r"\b(?:say|says|should say|must say|start(?:s|ing)? with|"
    r"continue with|greet(?:s|ing)? with|line is|exactly this|"
    r"word for word|verbatim|like this)\b\s*[:\-—]?\s*(.+)$",
    re.IGNORECASE | re.MULTILINE)


def _describes_second_beat(message: str) -> bool:
    """True when the request is about what the agent says AFTER the caller
    first responds — the beat that lives in call_flow.connect.intro_line."""
    return bool(_SECOND_BEAT_RE.search(str(message or "")))


def _dictated_sentences(message: str, min_words: int = 4) -> list[str]:
    """Sentences the user gave word-for-word for the agent to SAY.

    Quote marks and cue words only — no NLP, so it behaves identically in
    English, Hindi and Gujarati. Deliberately conservative: a miss costs
    nothing, a false positive would nag on every turn.
    """
    text = str(message or "")
    quoted = [m.group(1).strip() for m in _QUOTED_RE.finditer(text)]
    quoted = [q for q in quoted if len(q.split()) >= min_words]
    out: list[str] = list(quoted)
    for m in _DICTATION_CUE_RE.finditer(text):
        cand = m.group(1).strip().strip('"“”\'‘’ ')
        # Quotes are the precise boundary — when the user gave both a cue AND
        # quotes, the quoted span is the sentence, not the rest of the line.
        if any(q in cand for q in quoted):
            continue
        if len(cand.split()) >= min_words and cand not in out:
            out.append(cand)
    # De-dup nested cue matches, keeping the longest form of each.
    uniq: list[str] = []
    for s in sorted(out, key=len, reverse=True):
        if not any(s in u for u in uniq):
            uniq.append(s)
    return uniq[:4]


_HEARABLE_FIELD_HINT = (
    "the first thing the agent says (the opening line), or the line right "
    "after the caller responds")


def _landed_in_a_hearable_field(defn: dict, sentence: str) -> bool:
    """Is this sentence somewhere the CALLER actually hears it?

    Scoped deliberately: "anywhere in the definition" would have passed the
    real 2026-07-31 failure, where a dictated WhatsApp line was filed into a
    stage goal — present in the YAML, never spoken.
    """
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", str(s or "")).strip().casefold().rstrip(".!?।")

    target = _norm(sentence)
    if not target:
        return True
    rb = defn.get("response_bank") or {}
    ob = defn.get("outbound") or {}
    cf = defn.get("call_flow") or {}
    conn = cf.get("connect") if isinstance(cf, dict) else {}
    candidates: list[Any] = [
        rb.get("opening_greeting"), ob.get("opening_greeting"),
        (conn or {}).get("intro_line"),
        rb.get("objection_responses"), rb.get("objection_playbook"),
        rb.get("closing_responses"), rb.get("graceful_close"),
        rb.get("decline_farewell"), rb.get("identity_responses"),
        rb.get("intent_responses"), rb.get("proactive_prompts"),
        rb.get("lang_fallbacks"), rb.get("tool_ack_fallbacks"),
        (cf.get("callback_policy") or {}).get("responses")
        if isinstance(cf, dict) else None,
    ]
    for s in (d.get("question") for d in (defn.get("slots") or [])
              if isinstance(d, dict)):
        candidates.append(s)

    def _walk(node) -> bool:
        if isinstance(node, str):
            return target in _norm(node)
        if isinstance(node, dict):
            return any(_walk(v) for v in node.values())
        if isinstance(node, (list, tuple)):
            return any(_walk(v) for v in node)
        return False

    return any(_walk(c) for c in candidates)


def _prune_nones(node: Any) -> Any:
    """Recursively remove None values. A None from the LLM must NEVER survive
    to the deep-merge, where it would clobber a good platform default and
    ship an agent that crashes mid-call (e.g. opening_greeting: null)."""
    if isinstance(node, dict):
        return {k: _prune_nones(v) for k, v in node.items() if v is not None}
    if isinstance(node, list):
        return [_prune_nones(x) for x in node if x is not None]
    return node


def _normalize_definition(d: dict, notes: list[str] | None = None) -> dict:
    """Deterministically repair every known LLM shape mistake, in place.

    This is the load-bearing wall between a sloppy tool call and a broken
    (or worse: silently wrong) agent. Every fix observed in production is
    covered; anything unknown is dropped with an explanatory note rather
    than crashing create/update. Idempotent — safe to run twice.
    """
    if notes is None:
        notes = []

    # ── Nones out first — they'd otherwise clobber good defaults on merge ───
    pruned = _prune_nones(d)
    d.clear()
    d.update(pruned)

    # ── F1: known-wrong key names → canonical paths (before anything drops) ─
    _alias_rewrite(d, notes)

    # ── F14: nested objects sent as JSON text / a bare word (RUNBOOK §60) ───
    _decode_stringified_objects(d, notes)

    # ── sections in impossible container shapes ─────────────────────────────
    for section in ("identity", "personality"):
        val = d.get(section)
        if isinstance(val, list):
            first_dict = next((x for x in val if isinstance(x, dict)), None)
            if first_dict is not None:
                d[section] = first_dict
            elif val and isinstance(val[0], str):
                d[section] = (
                    {"agent_name": val[0]} if section == "identity"
                    else {"custom_instructions": " ".join(str(x) for x in val)}
                )
            else:
                d.pop(section)
                notes.append(f"{section} had an unusable shape — ignored")
    if isinstance(d.get("slots"), dict):
        d["slots"] = [d["slots"]]

    # ── languages: list/str → {"supported": [...], "default": first} ────────
    # The engine has detectors, scripts and TTS voices for exactly these.

    langs = d.get("languages")
    if isinstance(langs, (list, tuple)):
        d["languages"] = langs = {"supported": [str(x).strip().lower() for x in langs if str(x).strip()]}
        notes.append("languages was a plain list — converted to {supported: [...]}")
    elif isinstance(langs, str):
        d["languages"] = langs = {"supported": [langs.strip().lower()]}
        notes.append("languages was a plain string — converted to {supported: [...]}")
    if isinstance(langs, dict):
        sup = langs.get("supported")
        if isinstance(sup, str):
            langs["supported"] = [sup.strip().lower()]
        elif isinstance(sup, list):
            langs["supported"] = [str(x).strip().lower() for x in sup if str(x).strip()]
        elif "supported" in langs:
            langs.pop("supported")
            notes.append("languages.supported had an unusable shape — platform default kept")
        # An EMPTY supported list must not survive: on merge it would erase
        # the platform default and ship a zero-language agent as success.
        if "supported" in langs and not langs["supported"]:
            langs.pop("supported")
            notes.append("languages.supported was empty — platform default kept")
        dl = langs.get("default")
        if isinstance(dl, list):
            langs["default"] = str(dl[0]) if dl else ""
        elif dl is not None and not isinstance(dl, str):
            langs.pop("default", None)
        if not langs.get("default") and langs.get("supported"):
            langs["default"] = langs["supported"][0]
        # Only hi/en/gu exist on this platform. A language the engine has no
        # detector, no scripts and no voice for must never reach `supported`
        # — it would look configured and silently do nothing.
        if isinstance(langs.get("supported"), list):
            _kept = [x for x in langs["supported"] if x[:2] in _PLATFORM_LANGUAGES]
            _dropped = [x for x in langs["supported"] if x[:2] not in _PLATFORM_LANGUAGES]
            if _dropped:
                notes.append(
                    "this platform speaks only Hindi, English and Gujarati — "
                    + ", ".join(sorted(set(_dropped)))
                    + " was dropped. TELL THE USER it is not supported rather "
                      "than leaving them to find out on a call.")
            if _kept:
                langs["supported"] = _kept
                if langs.get("default", "")[:2] not in _PLATFORM_LANGUAGES:
                    langs["default"] = _kept[0]
            else:
                langs.pop("supported", None)
                langs.pop("default", None)
        if "auto_switch" in langs:
            langs["auto_switch"] = bool(langs["auto_switch"])

    # ── stray top-level tts_voices/voice/speaker → languages.tts_voices ─────
    stray_tts = d.pop("tts_voices", None)
    stray_voice = d.pop("voice", None)
    stray_voice = stray_voice if isinstance(stray_voice, str) else d.pop("speaker", None)
    if stray_tts is not None or stray_voice is not None:
        if not isinstance(d.get("languages"), dict):
            d["languages"] = {}
        lang_d = d["languages"]
        if isinstance(stray_tts, dict):
            existing = lang_d.get("tts_voices")
            lang_d["tts_voices"] = {**stray_tts, **existing} if isinstance(existing, dict) else stray_tts
            notes.append("tts_voices moved inside languages (its real home)")
        if isinstance(stray_voice, str) and stray_voice.strip():
            tv = lang_d.setdefault("tts_voices", {})
            for code in lang_d.get("supported") or ["en"]:
                tv.setdefault(code, {"model": "bulbul:v3", "speaker": stray_voice.strip().lower(), "pace": 1.15})
            notes.append(f"voice '{stray_voice}' applied as the TTS speaker for every language")
    # tts voice given as a bare speaker string per language
    tv = (d.get("languages") or {}).get("tts_voices") if isinstance(d.get("languages"), dict) else None
    if isinstance(tv, dict):
        voice_fields = _fields_of(TTSVoiceConfig)
        for code, v in list(tv.items()):
            if isinstance(v, str):
                tv[code] = {"model": "bulbul:v3", "speaker": v.strip().lower(), "pace": 1.15}
            elif isinstance(v, dict):
                v.setdefault("model", "bulbul:v3")
                v.setdefault("speaker", "simran")
                if v.get("model") not in ("bulbul:v2", "bulbul:v3-beta", "bulbul:v3"):
                    # speaker names sometimes land in model (e.g. model: shubh);
                    # Sarvam 400s the whole call, so coerce to a valid model
                    if isinstance(v.get("model"), str) and v["model"].strip() and v.get("speaker") in (None, "", "simran"):
                        v["speaker"] = v["model"].strip().lower()
                    v["model"] = "bulbul:v3"
                    notes.append(f"tts model for '{code}' was invalid — reset to bulbul:v3")
                for k in [k for k in v if k not in voice_fields]:
                    v.pop(k)
            else:  # numeric / junk — keep the platform default voice instead
                tv.pop(code)
    elif tv is not None and isinstance(d.get("languages"), dict):
        d["languages"].pop("tts_voices", None)
        notes.append("tts_voices had an unusable shape — platform default voices kept")

    # ── call_direction: list/"both"/junk → "inbound" | "outbound" ───────────
    if "call_direction" in d:
        cd = d.get("call_direction")
        if isinstance(cd, (list, tuple, set)):
            text = " ".join(str(x).strip().lower() for x in cd)
        else:
            text = str(cd).strip().lower()
        # Word-level matching — substring tricks invert on phrases like
        # "inbound and outbound" (contains both) or "outgoing" (contains "in").
        has_in = bool(re.search(r"in\s*bound|incoming|receiv|answer", text)) or text in ("in", "both", "all")
        has_out = bool(re.search(r"out\s*bound|outgoing|place|dial", text)) or text == "out"
        if (has_in and has_out) or "both" in text:
            d["call_direction"] = "inbound"
            notes.append(
                "call type 'both' stored as 'inbound' — the agent automatically switches to the "
                "outbound approach whenever the platform places the call, so inbound covers both"
            )
        elif has_out:
            d["call_direction"] = "outbound"
        elif has_in:
            d["call_direction"] = "inbound"
        else:
            d.pop("call_direction", None)
            notes.append(f"call_direction {cd!r} not understood — left unchanged")

    # ── call_flow: bare list → {"stages": [...]}; stage row repairs ─────────
    cf = d.get("call_flow")
    if isinstance(cf, list):
        d["call_flow"] = cf = {"stages": cf}
    if isinstance(cf, dict) and isinstance(cf.get("stages"), list):
        # Both skeletons: LLM-authored outbound stages (OPENING/PITCH/...)
        # that omit turns_until_advance must inherit the outbound pacing,
        # not the generic fallback. Only shared id is CLOSING (99 in both).
        turns_default = {s["id"]: s["turns_until_advance"]
                         for s in (_DEFAULT_STAGES + _DEFAULT_OUTBOUND_STAGES)}
        stage_fields = _fields_of(StageDefinition)
        fixed_stages = []
        for i, stage in enumerate(cf["stages"]):
            if isinstance(stage, str):
                stage = {"id": stage, "goal": stage}
            if not isinstance(stage, dict):
                continue
            if "id" in stage:
                stage["id"] = str(stage["id"]).upper().replace(" ", "_")
            stage["order"] = i
            stage.setdefault("turns_until_advance", turns_default.get(stage.get("id", ""), 8))
            for k in [k for k in stage if k not in stage_fields]:
                stage.pop(k)
            fixed_stages.append(stage)
        if fixed_stages:
            cf["stages"] = fixed_stages
        else:
            # An empty stages list would erase the default 5-stage flow on
            # merge and ship an agent with nothing to run. Keep the default.
            cf.pop("stages")
            notes.append("call_flow.stages was empty/unusable — default call flow kept")

    # ── identity / personality given as plain strings ───────────────────────
    if isinstance(d.get("identity"), str):
        d["identity"] = {"agent_name": d["identity"]}
    if isinstance(d.get("personality"), str):
        d["personality"] = {"custom_instructions": d["personality"]}

    # ── knowledge_base coercions ─────────────────────────────────────────────
    kb = d.get("knowledge_base")
    if isinstance(kb, str):
        d["knowledge_base"] = kb = {"portfolio_prompt_text": kb}
    elif isinstance(kb, list):
        # A bare list of facts → fold into the portfolio paragraph so the
        # content SURVIVES instead of crashing from_dict on kb["known_projects"]
        # (list indices error) or leaving a list where a dict is expected.
        facts = [str(x).strip() for x in kb if str(x).strip()]
        d["knowledge_base"] = kb = {"portfolio_prompt_text": " ".join(facts)} if facts else {}
        notes.append("knowledge_base was a list — folded into the knowledge paragraph")
    if isinstance(kb, dict):
        loc = kb.get("locations")
        if isinstance(loc, str):
            kb["locations"] = [s.strip() for s in loc.split(",") if s.strip()]
        elif isinstance(loc, list):
            kb["locations"] = [str(x).strip() for x in loc if str(x).strip()]
        elif "locations" in kb:
            kb.pop("locations")
        if not isinstance(kb.get("portfolio_prompt_text", ""), str):
            kb["portfolio_prompt_text"] = str(kb["portfolio_prompt_text"])
        kp = kb.get("known_projects")
        if isinstance(kp, list):  # ["Product A", {"name": ...}] → keyed dict
            fixed_kp: dict[str, list] = {}
            for item in kp:
                if isinstance(item, str) and item.strip():
                    fixed_kp[item.strip().lower()] = [item.strip(), "Standard"]
                elif isinstance(item, dict) and item.get("name"):
                    nm = str(item["name"]).strip()
                    fixed_kp[nm.lower()] = [nm, str(item.get("price") or item.get("location") or "Standard")]
            kb["known_projects"] = kp = fixed_kp
            notes.append("known_projects was a list — converted to the keyed form")
        if isinstance(kp, dict):
            for k, v in list(kp.items()):
                if isinstance(v, str):
                    kp[k] = [v, "Standard"]
                elif isinstance(v, list) and len(v) == 1:
                    kp[k] = [v[0], "Standard"]
                elif not isinstance(v, (list, tuple)):
                    kp[k] = [str(v), "Standard"]
        elif kp is not None:
            kb.pop("known_projects", None)
        # stt_corrections / qa_field_signals are real KB fields (so the
        # invented-key filter keeps them) but from_dict calls .get() on
        # stt_corrections — a list there is the exact "'list' object has no
        # attribute 'get'" crash. Enforce dict-shape here.
        sc = kb.get("stt_corrections")
        if sc is not None and not isinstance(sc, dict):
            kb.pop("stt_corrections", None)
            notes.append("stt_corrections had an unusable shape — platform default kept")
        elif isinstance(sc, dict):
            for sub in list(sc):
                if not isinstance(sc[sub], dict):
                    sc.pop(sub)
        qs = kb.get("qa_field_signals")
        if qs is not None and not isinstance(qs, dict):
            kb.pop("qa_field_signals", None)
            notes.append("qa_field_signals had an unusable shape — ignored")

    # ── calendar coercions ───────────────────────────────────────────────────
    cal = d.get("calendar")
    if isinstance(cal, list):  # bare slot list → proper calendar block
        d["calendar"] = cal = {"type": "mock", "mock_slots": cal}
    if isinstance(cal, dict):
        cal.setdefault("type", "mock")
        ms = cal.get("mock_slots")
        if isinstance(ms, list):
            fixed_slots = []
            for row in ms:
                if isinstance(row, dict) and row.get("day") and row.get("time"):
                    fixed_slots.append({"day": str(row["day"]).strip(), "time": str(row["time"]).strip()})
                elif isinstance(row, str) and " at " in row:
                    day, _, tme = row.partition(" at ")
                    if day.strip() and tme.strip():
                        fixed_slots.append({"day": day.strip(), "time": tme.strip()})
            # F13 sanity (runs only when the payload itself carried a
            # calendar — an untouched stored agent is never rewritten):
            # exact-duplicate (day,time) pairs dropped, keep-first order.
            _seen: set[tuple[str, str]] = set()
            _deduped = []
            for slot in fixed_slots:
                key = (slot["day"].lower(), slot["time"].lower())
                if key in _seen:
                    continue
                _seen.add(key)
                _deduped.append(slot)
            if len(_deduped) < len(fixed_slots):
                notes.append(f"calendar: {len(fixed_slots) - len(_deduped)} "
                             "duplicate slot(s) removed")
            fixed_slots = _deduped
            # A degenerate calendar where >=4 slots share one identical time
            # string is LLM anchoring, not real availability (Kaapd: six
            # 10:00 AM slots) — keep the first two, flag the rest.
            _by_time: dict[str, int] = {}
            for slot in fixed_slots:
                _by_time[slot["time"].lower()] = _by_time.get(slot["time"].lower(), 0) + 1
            for tkey, cnt in _by_time.items():
                if cnt >= 4:
                    kept, trimmed = [], 0
                    for slot in fixed_slots:
                        if slot["time"].lower() == tkey and sum(
                                1 for s in kept if s["time"].lower() == tkey) >= 2:
                            trimmed += 1
                            continue
                        kept.append(slot)
                    fixed_slots = kept
                    notes.append(
                        f"calendar had {cnt} slots all at the same time "
                        f"({tkey}) — kept two; confirm the real availability")
            if len(fixed_slots) > 12:
                notes.append(f"calendar capped at 12 slots "
                             f"({len(fixed_slots) - 12} dropped)")
                fixed_slots = fixed_slots[:12]
            cal["mock_slots"] = fixed_slots
        elif "mock_slots" in cal:
            cal.pop("mock_slots")
            notes.append("calendar.mock_slots had an unusable shape — existing slots kept")
        st = cal.get("slots_text")
        if st is not None and not isinstance(st, dict):
            cal.pop("slots_text")
    elif cal is not None:
        d.pop("calendar", None)
        notes.append("calendar had an unusable shape — ignored")

    # ── response_bank repairs ────────────────────────────────────────────────
    rb = d.get("response_bank")
    if isinstance(rb, str):
        d["response_bank"] = rb = {"opening_greeting": rb}
    if isinstance(rb, dict):
        default_lang = _default_lang_of(d)
        greeting = rb.get("opening_greeting")
        if isinstance(greeting, dict):
            picked = greeting.get(default_lang) or next(iter(greeting.values()), "")
            if str(picked).strip():
                rb["opening_greeting"] = str(picked)
            else:
                rb.pop("opening_greeting")
                notes.append("opening_greeting was empty — existing/default greeting kept")
        elif isinstance(greeting, (list, tuple)):
            # A bilingual-greeting-as-list must not become a spoken Python repr.
            picked = next((x.strip() for x in greeting if isinstance(x, str) and x.strip()), "")
            if picked:
                rb["opening_greeting"] = picked
                notes.append("opening_greeting was a list — kept the first line only")
            else:
                rb.pop("opening_greeting")
                notes.append("opening_greeting was empty — existing/default greeting kept")
        elif greeting is not None and not isinstance(greeting, str):
            rb["opening_greeting"] = str(greeting)
        # Canonicalize invented placeholder syntaxes — ${contact_name} etc.
        # would be spoken literally on a real call.
        g = rb.get("opening_greeting")
        if isinstance(g, str) and _any_placeholder(g):
            fixed = _canonicalize_placeholders(g)
            if fixed != g:
                rb["opening_greeting"] = fixed
                notes.append(
                    "placeholder in the greeting rewritten to {contact_name}/{current_time} — "
                    "the only templates the call engine understands (single curly braces)"
                )
        if "response_fallback" in rb and not isinstance(rb["response_fallback"], str):
            rb["response_fallback"] = str(rb["response_fallback"])
        for key in _RB_LANG_STR_FIELDS:
            val = rb.get(key)
            if isinstance(val, str):
                rb[key] = {default_lang: val}
            elif isinstance(val, dict):
                for lang, v in list(val.items()):
                    if not isinstance(v, str):
                        val[lang] = str(v)
            elif val is not None:
                rb.pop(key)
        for key in _RB_LANG_LIST_FIELDS:
            val = rb.get(key)
            if isinstance(val, str):
                rb[key] = {default_lang: [val]}
            elif isinstance(val, list):
                rb[key] = {default_lang: [str(x) for x in val]}
            elif isinstance(val, dict):
                for lang, v in list(val.items()):
                    if isinstance(v, str):
                        val[lang] = [v]
                    elif isinstance(v, list):
                        val[lang] = [str(x) for x in v]
                    else:
                        val[lang] = [str(v)]
            elif val is not None:
                rb.pop(key)

    # ── identity / personality: every field is a plain string ───────────────
    # (except the v2 typed personality fields, validated separately below)
    _PERS_TYPED = {"traits", "energy_mirroring", "verbosity_max_sentences"}
    for section in ("identity", "personality"):
        val = d.get(section)
        if isinstance(val, dict):
            for k, v in list(val.items()):
                if section == "personality" and k in _PERS_TYPED:
                    continue
                if isinstance(v, (dict, list)):
                    val.pop(k)
                    notes.append(f"{section}.{k} had an unusable shape — ignored")
                elif not isinstance(v, str):
                    val[k] = str(v)
    pers = d.get("personality")
    if isinstance(pers, dict):
        if "ai_disclosure" in pers and str(pers.get("ai_disclosure")).strip().lower() \
                not in ("deflect", "admit", "admit_if_pressed"):
            notes.append(f"personality.ai_disclosure {pers['ai_disclosure']!r} invalid — reset to 'deflect'")
            pers["ai_disclosure"] = "deflect"
        elif isinstance(pers.get("ai_disclosure"), str):
            pers["ai_disclosure"] = pers["ai_disclosure"].strip().lower()
        if "traits" in pers:
            t = pers["traits"]
            pers["traits"] = ([str(x) for x in t] if isinstance(t, list)
                              else [str(t)] if isinstance(t, str) and t.strip() else [])
        if "energy_mirroring" in pers and not isinstance(pers["energy_mirroring"], bool):
            v = str(pers["energy_mirroring"]).strip().lower()
            pers["energy_mirroring"] = v in ("1", "true", "yes", "on")
        if "verbosity_max_sentences" in pers:
            try:
                pers["verbosity_max_sentences"] = max(1, min(8, int(pers["verbosity_max_sentences"])))
            except (TypeError, ValueError):
                pers.pop("verbosity_max_sentences")

    # ── slots rows — shape filter + v2 value validation ─────────────────────
    if isinstance(d.get("slots"), list):
        slot_fields = _fields_of(SlotDefinition)
        fixed_slots = []
        for s in d["slots"]:
            if not (isinstance(s, dict) and s.get("name")):
                continue
            s = {k: v for k, v in s.items() if k in slot_fields}
            s["name"] = str(s["name"]).strip().lower().replace(" ", "_")
            # enum-ish fields: coerce invalid values to their defaults —
            # a hallucinated value must never abort a save that would
            # otherwise succeed.
            if "type" in s and str(s.get("type")).strip().lower() not in (
                    "", "text", "number", "money", "enum", "boolean",
                    "phone", "email", "date", "time", "location"):
                notes.append(f"slot '{s['name']}' type {s['type']!r} invalid — cleared")
                s["type"] = ""
            elif isinstance(s.get("type"), str):
                s["type"] = s["type"].strip().lower()
            if "ask_gated" in s:
                s["ask_gated"] = bool(s["ask_gated"])
            if "merge_policy" in s and str(s.get("merge_policy")).strip().lower() not in (
                    "free", "correction_gated", "confirm"):
                notes.append(f"slot '{s['name']}' merge_policy {s['merge_policy']!r} invalid — reset to 'free'")
                s["merge_policy"] = "free"
            elif isinstance(s.get("merge_policy"), str):
                s["merge_policy"] = s["merge_policy"].strip().lower()
            if "confirm_readback" in s and str(s.get("confirm_readback")).strip().lower() not in (
                    "", "verbatim", "digit_groups_2", "spelled"):
                notes.append(f"slot '{s['name']}' confirm_readback {s['confirm_readback']!r} invalid — cleared")
                s["confirm_readback"] = ""
            elif isinstance(s.get("confirm_readback"), str):
                s["confirm_readback"] = s["confirm_readback"].strip().lower()
            for int_key, default in (("priority", 100), ("max_asks", 0)):
                if int_key in s:
                    try:
                        s[int_key] = int(s[int_key])
                    except (TypeError, ValueError):
                        s[int_key] = default
            # extract_patterns: cap count and per-pattern length; drop
            # patterns that don't compile (they'd be rejected at load).
            if "extract_patterns" in s:
                pats = s["extract_patterns"] if isinstance(s["extract_patterns"], list) else []
                good = []
                for p in pats[:8]:
                    p = str(p)[:200]
                    try:
                        re.compile(p)
                        good.append(p)
                    except re.error:
                        notes.append(f"slot '{s['name']}' extract pattern {p!r} does not compile — dropped")
                s["extract_patterns"] = good
            if "depends_on" in s and not isinstance(s["depends_on"], list):
                s["depends_on"] = [str(s["depends_on"])] if isinstance(s["depends_on"], str) else []
            fixed_slots.append(s)
        # depends_on: names must exist in the slot set; cycles are dropped.
        names = {s["name"] for s in fixed_slots}
        for s in fixed_slots:
            deps = [str(x).strip().lower().replace(" ", "_") for x in (s.get("depends_on") or [])]
            valid = [x for x in deps if x in names and x != s["name"]]
            if len(valid) != len(deps):
                notes.append(f"slot '{s['name']}' depends_on entries not in the slot set were dropped")
            if valid:
                s["depends_on"] = valid
            elif "depends_on" in s:
                s["depends_on"] = []
        # cycle detection (DFS); on a cycle, clear depends_on for its members
        graph = {s["name"]: set(s.get("depends_on") or []) for s in fixed_slots}
        def _cyclic(node, stack):
            if node in stack:
                return True
            stack = stack | {node}
            return any(_cyclic(n, stack) for n in graph.get(node, ()))
        cyclic_nodes = {n for n in graph if _cyclic(n, frozenset())}
        if cyclic_nodes:
            notes.append(f"slot dependency cycle among {sorted(cyclic_nodes)} — dependencies cleared")
            for s in fixed_slots:
                if s["name"] in cyclic_nodes:
                    s["depends_on"] = []
        d["slots"] = fixed_slots

    # ── v2 sections: shape coercions ─────────────────────────────────────────
    default_lang_v2 = _default_lang_of(d)

    def _lang_dict(val):
        """str → {default_lang: str}; dict of str kept; anything else None.

        A {lang: {situation: line}} nest (how users naturally dictate
        closings) is flattened to one line per language — the thank-you /
        closing-most line — instead of silently becoming {} and losing the
        content behind a success status (incident 2026-07-15: greeting_close
        re-asked forever because closing_responses normalized to {}).
        Never returns an empty dict: empty means None so callers drop + note.
        """
        if isinstance(val, str):
            return {default_lang_v2: val} if val.strip() else None
        if isinstance(val, dict):
            out = {}
            for k, v in val.items():
                if isinstance(v, (str, int, float)) and str(v).strip():
                    out[str(k)] = str(v)
                elif isinstance(v, dict):
                    strs = {str(kk): str(vv).strip() for kk, vv in v.items()
                            if isinstance(vv, (str, int, float)) and str(vv).strip()}
                    if strs:
                        pick = next((strs[kk] for kk in
                                     ("thank_you", "closing", "closing_line",
                                      "close", "goodbye", "final")
                                     if kk in strs), None)
                        out[str(k)] = pick or list(strs.values())[-1]
            return out or None
        return None

    br = d.get("brand")
    if isinstance(br, str):
        d["brand"] = br = {"spoken_company_name": br}
    if isinstance(br, dict):
        for k in ("credibility_markers", "trust_proofs", "regions_served"):
            if k in br:
                v = br[k]
                br[k] = ([str(x) for x in v] if isinstance(v, list)
                         else [str(v)] if isinstance(v, str) and v.strip() else [])
        for k in ("spoken_company_name", "positioning_tagline", "contact_phone", "whatsapp_number"):
            if k in br and not isinstance(br[k], str):
                br[k] = str(br[k])
    elif br is not None:
        d.pop("brand", None)
        notes.append("brand had an unusable shape — ignored")

    ob = d.get("outbound")
    if isinstance(ob, dict):
        for k in ("identity_confirm_line", "good_time_check_line", "pitch",
                  "wrong_person_response", "busy_response", "wrong_number_close",
                  "wrong_number_reconfirm_line", "opening_greeting"):
            if k in ob:
                fixed = _lang_dict(ob[k])
                if fixed is None:
                    ob.pop(k)
                    notes.append(f"outbound.{k} had an unusable shape — ignored")
                else:
                    ob[k] = fixed
        if "wrong_number_patterns" in ob:
            v = ob["wrong_number_patterns"]
            ob["wrong_number_patterns"] = ([str(x) for x in v] if isinstance(v, list)
                                           else [str(v)] if isinstance(v, str) else [])
        if "direction_brief" in ob and not isinstance(ob["direction_brief"], str):
            ob["direction_brief"] = str(ob["direction_brief"])
    elif ob is not None:
        d.pop("outbound", None)
        notes.append("outbound had an unusable shape — ignored")

    gr = d.get("guardrails")
    if isinstance(gr, dict):
        for k in ("never_rules", "always_rules"):
            if k in gr:
                v = gr[k]
                gr[k] = ([str(x) for x in v] if isinstance(v, list)
                         else [str(v)] if isinstance(v, str) and v.strip() else [])
        if "use_builtin_rules" in gr and not isinstance(gr["use_builtin_rules"], bool):
            gr["use_builtin_rules"] = str(gr["use_builtin_rules"]).strip().lower() in ("1", "true", "yes", "on")
        for k in ("honesty", "compliance", "escalation"):
            if k in gr and not isinstance(gr[k], dict):
                gr.pop(k)
                notes.append(f"guardrails.{k} had an unusable shape — ignored")
        hon = gr.get("honesty")
        if isinstance(hon, dict):
            # forbidden_fabrications is a LIST of things never to invent — the
            # engine iterates it in the prompt build, so a bare bool crashed
            # every turn of a live call (incident 2026-07-15: agent stuck on
            # "could you repeat that"). bool True = "use the default" → drop.
            if "forbidden_fabrications" in hon:
                ff = hon["forbidden_fabrications"]
                if isinstance(ff, bool) or ff is None:
                    hon.pop("forbidden_fabrications")
                elif isinstance(ff, str):
                    hon["forbidden_fabrications"] = [ff] if ff.strip() else []
                elif isinstance(ff, list):
                    hon["forbidden_fabrications"] = [str(x) for x in ff if str(x).strip()]
                else:
                    hon.pop("forbidden_fabrications")
            if "unknown_answer_policy" in hon and not isinstance(hon["unknown_answer_policy"], str):
                hon["unknown_answer_policy"] = str(hon["unknown_answer_policy"])
        comp = gr.get("compliance")
        if isinstance(comp, dict):
            for k in ("disclaimers", "dnd_phrases", "restricted_topics"):
                if k in comp and not isinstance(comp[k], list):
                    comp[k] = [str(comp[k])] if isinstance(comp[k], str) and comp[k].strip() else []
        esc = gr.get("escalation")
        if isinstance(esc, dict):
            hm = esc.get("handoff_message")
            if hm is not None:
                fixed = _lang_dict(hm)
                if fixed is None:
                    esc.pop("handoff_message")
                else:
                    esc["handoff_message"] = fixed
    elif gr is not None:
        d.pop("guardrails", None)
        notes.append("guardrails had an unusable shape — ignored")

    fu = d.get("follow_up")
    if isinstance(fu, dict):
        wp = fu.get("whatsapp_promise")
        if wp is not None and not isinstance(wp, dict):
            fu.pop("whatsapp_promise")
            notes.append("follow_up.whatsapp_promise had an unusable shape — ignored")
        elif isinstance(wp, dict):
            pl = wp.get("promise_line")
            if pl is not None:
                fixed = _lang_dict(pl)
                if fixed is None:
                    wp.pop("promise_line")
                else:
                    wp["promise_line"] = fixed
            if "assets" in wp and not isinstance(wp["assets"], list):
                wp["assets"] = [str(wp["assets"])] if isinstance(wp["assets"], str) else []
        cb = fu.get("callback")
        if cb is not None and not isinstance(cb, dict):
            fu.pop("callback")
    elif fu is not None:
        d.pop("follow_up", None)
        notes.append("follow_up had an unusable shape — ignored")

    pc = d.get("post_call")
    if isinstance(pc, dict):
        for k in ("success_definition", "scoring", "lead_profile"):
            if k in pc and not isinstance(pc[k], dict):
                pc.pop(k)
                notes.append(f"post_call.{k} had an unusable shape — ignored")
        if "outcomes" in pc:
            oc = pc["outcomes"]
            pc["outcomes"] = [o for o in oc if isinstance(o, dict)] if isinstance(oc, list) else []
    elif pc is not None:
        d.pop("post_call", None)
        notes.append("post_call had an unusable shape — ignored")

    # ── stage v2 fields: role/advance_when/branches validation ──────────────
    cf2 = d.get("call_flow")
    if isinstance(cf2, dict) and isinstance(cf2.get("stages"), list):
        stage_ids = {str(s.get("id") or "") for s in cf2["stages"] if isinstance(s, dict)}
        _ROLES = ("", "greeting", "confirm_person", "discovery", "pitch",
                  "recommend", "scheduling", "confirmation", "closing", "custom")
        for s in cf2["stages"]:
            if not isinstance(s, dict):
                continue
            if "role" in s:
                role = str(s.get("role") or "").strip().lower()
                if role not in _ROLES:
                    notes.append(f"stage {s.get('id')} role {s['role']!r} invalid — cleared")
                    role = ""
                s["role"] = role
            if "collect" in s and not isinstance(s["collect"], list):
                s["collect"] = [str(s["collect"])] if isinstance(s["collect"], str) else []
            if "advance_when" in s and not isinstance(s["advance_when"], dict):
                s.pop("advance_when")
                notes.append(f"stage {s.get('id')} advance_when had an unusable shape — ignored")
            if "branches" in s:
                brs = s["branches"] if isinstance(s["branches"], list) else []
                good_brs = []
                for b in brs:
                    if not isinstance(b, dict):
                        continue
                    goto = str(b.get("goto") or "").upper().replace(" ", "_")
                    if goto and goto not in stage_ids:
                        notes.append(f"branch goto {b.get('goto')!r} names a stage that "
                                     "does not exist — branch dropped")
                        continue
                    b["goto"] = goto
                    if "max_takes" in b:
                        try:
                            b["max_takes"] = max(1, int(b["max_takes"]))
                        except (TypeError, ValueError):
                            b["max_takes"] = 1
                    if "actions" in b:
                        acts = b["actions"] if isinstance(b["actions"], list) else [b["actions"]]
                        b["actions"] = [str(a) for a in acts if str(a) in (
                            "capture_phone", "promise_whatsapp_media",
                            "book_visit", "schedule_callback")]
                    good_brs.append(b)
                s["branches"] = good_brs

    # ── call_flow.termination shape validation ──────────────────────────────
    if isinstance(cf2, dict) and "termination" in cf2:
        term = cf2.get("termination")
        if not isinstance(term, dict):
            cf2.pop("termination", None)
            notes.append("call_flow.termination had an unusable shape — ignored")
        else:
            for lk in ("end_conditions", "farewell_patterns"):
                if lk in term:
                    v = term[lk]
                    if isinstance(v, str):
                        term[lk] = [v] if v.strip() else []
                    elif isinstance(v, list):
                        term[lk] = [str(x).strip() for x in v if str(x).strip()]
                    else:
                        term.pop(lk)
                        notes.append(f"call_flow.termination.{lk} had an "
                                     "unusable shape — ignored")
            # A single short token ("ok", "no", "बस") as a farewell pattern
            # would cut live calls mid-conversation on a stray affirmation.
            fps = term.get("farewell_patterns") or []
            risky = [p for p in fps if len(str(p).strip()) < 4]
            if risky:
                term["farewell_patterns"] = [p for p in fps if p not in risky]
                notes.append(
                    "call_flow.termination.farewell_patterns dropped entries "
                    "too short to be safe (they would cut live calls): "
                    + ", ".join(repr(p) for p in risky))
            for bk in ("llm_end_call", "confirm_before_end"):
                if bk in term:
                    term[bk] = bool(term[bk])
            if "pre_close_question" in term:
                # RUNBOOK §75: {lang: str}, like the outbound lines.
                fixed = _lang_dict(term["pre_close_question"])
                if fixed is None:
                    term.pop("pre_close_question")
                    notes.append("call_flow.termination.pre_close_question "
                                 "had an unusable shape — ignored")
                else:
                    term["pre_close_question"] = fixed
            if "max_call_minutes" in term:
                try:
                    term["max_call_minutes"] = max(
                        0.0, float(term["max_call_minutes"]))
                except (TypeError, ValueError):
                    term.pop("max_call_minutes")
                    notes.append("call_flow.termination.max_call_minutes was "
                                 "not a number — ignored")
            if "silence_hangup" in term and not isinstance(
                    term["silence_hangup"], dict):
                term.pop("silence_hangup")
                notes.append("call_flow.termination.silence_hangup had an "
                             "unusable shape — ignored")

    # ── call_flow.connect shape validation ──────────────────────────────────
    if isinstance(cf2, dict) and "connect" in cf2:
        conn = cf2.get("connect")
        if not isinstance(conn, dict):
            cf2.pop("connect", None)
            notes.append("call_flow.connect had an unusable shape — ignored")
        else:
            for bk in ("hello_fastpath", "delivery_guard"):
                if bk in conn:
                    conn[bk] = bool(conn[bk])
            # intro_line is a SPOKEN surface — the second beat. Same
            # per-language shape as every other spoken field, so a bare
            # string is accepted and normalized rather than silently dropped.
            if "intro_line" in conn:
                fixed = _lang_dict(conn["intro_line"])
                if fixed:
                    conn["intro_line"] = fixed
                else:
                    conn.pop("intro_line", None)
                    notes.append("call_flow.connect.intro_line had an "
                                 "unusable shape — ignored")
    # RUNBOOK §78: per-agent turn-taking — numbers and booleans only.
    if isinstance(cf2, dict) and "turn_taking" in cf2:
        tt = cf2.get("turn_taking")
        if not isinstance(tt, dict):
            cf2.pop("turn_taking", None)
            notes.append("call_flow.turn_taking had an unusable shape — ignored")
        else:
            for nk in ("pause_hold_ms", "sustained_barge_ms"):
                if nk in tt:
                    try:
                        tt[nk] = max(0.0, float(tt[nk]))
                    except (TypeError, ValueError):
                        tt.pop(nk)
                        notes.append(f"call_flow.turn_taking.{nk} was not a "
                                     "number — ignored")
            if "max_merged_fragments" in tt:
                try:
                    tt["max_merged_fragments"] = max(
                        1, int(tt["max_merged_fragments"]))
                except (TypeError, ValueError):
                    tt.pop("max_merged_fragments")
                    notes.append("call_flow.turn_taking.max_merged_fragments "
                                 "was not a number — ignored")
            for bk in ("backchannel_guard", "tail_barge_in"):
                if bk in tt:
                    tt[bk] = bool(tt[bk])

    # ── knowledge_base v2 fields ─────────────────────────────────────────────
    kb2 = d.get("knowledge_base")
    if isinstance(kb2, dict):
        if "catalog" in kb2:
            cat = kb2["catalog"]
            kb2["catalog"] = ([c for c in cat if isinstance(c, dict) and c.get("name")]
                              if isinstance(cat, list) else [])
        if "match_rules" in kb2:
            mr = kb2["match_rules"]
            kb2["match_rules"] = ([m for m in mr if isinstance(m, dict)]
                                  if isinstance(mr, list) else [])
        for k in ("pricing_policy", "fact_policy", "product_lock"):
            if k in kb2 and not isinstance(kb2[k], dict):
                kb2.pop(k)
                notes.append(f"knowledge_base.{k} had an unusable shape — ignored")

    # ── response_bank v2 fields ──────────────────────────────────────────────
    rb2 = d.get("response_bank")
    if isinstance(rb2, dict):
        if "objection_playbook" in rb2:
            pb = rb2["objection_playbook"]
            good_pb = []
            for o in (pb if isinstance(pb, list) else []):
                if not isinstance(o, dict):
                    continue
                if "patterns" in o and not isinstance(o["patterns"], list):
                    o["patterns"] = [str(o["patterns"])] if isinstance(o["patterns"], str) else []
                ans = o.get("answer")
                if ans is not None:
                    fixed = _lang_dict(ans)
                    if fixed is None:
                        o.pop("answer")
                    else:
                        o["answer"] = fixed
                good_pb.append(o)
            rb2["objection_playbook"] = good_pb
        if "closing_responses" in rb2:
            raw_cr = rb2["closing_responses"]
            fixed = _lang_dict(raw_cr)
            if fixed is None:
                rb2.pop("closing_responses")
                notes.append(
                    "response_bank.closing_responses had an unusable shape — "
                    "NOT saved. Send {lang: ONE closing line string} per "
                    "language; retry update_draft with that shape now.")
            else:
                if isinstance(raw_cr, dict) and any(
                        isinstance(v, dict) for v in raw_cr.values()):
                    notes.append(
                        "closing_responses were nested per-situation — kept the "
                        "final thank-you line per language as THE closing line "
                        "(the engine speaks one closing per language); the "
                        "situational lines belong in the stage goals")
                rb2["closing_responses"] = fixed
        if "ai_disclosure_responses" in rb2:
            fixed = _lang_dict(rb2["ai_disclosure_responses"])
            if fixed is None:
                rb2.pop("ai_disclosure_responses")
                notes.append("response_bank.ai_disclosure_responses had an "
                             "unusable shape — NOT saved ({lang: str} expected)")
            else:
                rb2["ai_disclosure_responses"] = fixed
        if "canned_phrases" in rb2:
            cp = rb2["canned_phrases"]
            good_cp = []
            for c in (cp if isinstance(cp, list) else []):
                if not isinstance(c, dict):
                    continue
                if "patterns" in c and not isinstance(c["patterns"], list):
                    c["patterns"] = [str(c["patterns"])] if isinstance(c["patterns"], str) else []
                resp = c.get("responses")
                if resp is not None:
                    fixed = _lang_dict(resp)
                    if fixed is None:
                        c.pop("responses")
                    else:
                        c["responses"] = fixed
                good_cp.append(c)
            rb2["canned_phrases"] = good_cp
        if "enable_greeting_shortcut" in rb2 and not isinstance(rb2["enable_greeting_shortcut"], bool):
            rb2["enable_greeting_shortcut"] = str(rb2["enable_greeting_shortcut"]).strip().lower() in (
                "1", "true", "yes", "on")

    # ── drop invented keys (with notes so the LLM relocates the content) ────
    for section, cls in _SECTION_DATACLASSES.items():
        val = d.get(section)
        if isinstance(val, dict):
            allowed = _fields_of(cls)
            for k in [k for k in val if k not in allowed]:
                val.pop(k)
                notes.append(
                    f"'{section}.{k}' is not a real setting — its content was NOT saved. "
                    "Put it in a stage goal, the greeting, or the knowledge text instead."
                )
        elif val is not None:
            # Last-line guard: a section that reached here in a non-dict shape
            # (e.g. llm_config as a list) would crash from_dict on `.get()`
            # — the '\'list\' object has no attribute \'get\'' failure. Drop it
            # so the platform default is used instead of bricking create/update.
            d.pop(section)
            notes.append(
                f"'{section}' had an unusable shape (expected settings, not a list/value) "
                "— platform default kept. Put that content in a stage goal or the knowledge text."
            )
    for k in [k for k in d if k not in _TOP_LEVEL_KEYS]:
        d.pop(k)
        close = _closest_paths(k)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        notes.append(f"top-level '{k}' is not a real setting — dropped.{hint}")

    # On the CHANGE subtree, not the merged agent: a scoped write that only
    # moves the voice must not also rewrite stage goals it never touched —
    # write_agent's scope proof would (rightly) roll the whole write back.
    _tokenize_spoken_address(d, notes)
    return d


def _copilot_actor() -> str:
    return f"{builder._ctx_owner()}:copilot"


def _invariant_violations(merged: dict) -> list[str]:
    """§5.4: the whole-document passes that used to silently MUTATE untouched
    fields become validation-time CHECKS that fail the proposal instead —
    otherwise the scoped-diff proof would read the mutations as collateral."""
    problems: list[str] = []
    langs = merged.get("languages") or {}
    sup = langs.get("supported") if isinstance(langs, dict) else None
    if not (isinstance(sup, list) and any(isinstance(x, str) and x.strip() for x in sup)):
        problems.append("languages.supported would become empty")
    elif isinstance(langs, dict) and langs.get("default") not in sup:
        problems.append("languages.default would not be in supported")
    cf = merged.get("call_flow") or {}
    if not (isinstance(cf, dict) and cf.get("stages")):
        problems.append("call_flow.stages would become empty")
    rb = merged.get("response_bank") or {}
    greeting = rb.get("opening_greeting") if isinstance(rb, dict) else None
    if not (isinstance(greeting, str) and greeting.strip()):
        problems.append("opening_greeting would become empty")
    # F9: max_tokens is the spoken-brevity budget — an LLM-authored
    # "unlimited"/true/4096 must fail the proposal, not ship (checks, never
    # mutations, per §5.4).
    lc = merged.get("llm_config") or {}
    if isinstance(lc, dict) and "max_tokens" in lc:
        try:
            _mt = int(lc["max_tokens"])
        except (TypeError, ValueError):
            _mt = -1
        if not (80 <= _mt <= 512):
            problems.append(
                f"llm_config.max_tokens would become {lc['max_tokens']!r} "
                "(must be an integer between 80 and 512)")
    problems += _platform_invariants(merged)
    return problems


# Object-list fields: every entry MUST be a mapping. A write of bare scalars
# is accepted by the merge, then silently emptied by the normalizer.
_OBJECT_LIST_FIELDS: tuple[tuple[str, ...], ...] = (
    ("response_bank", "objection_playbook"),
    ("response_bank", "canned_phrases"),
    ("call_flow", "stages"),
    ("slots",),
    ("calendar", "mock_slots"),
)
# Only these are substituted before speech (conversation.py). Anything else
# in braces is read out to the caller verbatim.
_LIVE_TOKENS = {"contact_name", "customer_name", "current_time", "honorific",
                "title"}
_TOKEN_RE = re.compile(r"\{\{([^{}]{1,60})\}\}|\{([A-Za-z_][A-Za-z0-9_ .\-]{0,58})\}")
# Surfaces whose strings are spoken to the caller more or less verbatim.
# stage goals are INSTRUCTIONS to the model, not speech, so they are excluded;
# response_bank.slots_prompt is a documented {slots} template.
_SPOKEN_PREFIXES = ("response_bank.", "outbound.")
_SPOKEN_EXCLUDE = ("response_bank.slots_prompt",)
_HUMAN_CLAIM_RE = re.compile(
    r"(?:you\s+are|you're|i\s+am|i'm)\s+(?:a\s+)?(?:real|actual|human)\s*"
    r"(?:person|human|being)?\b"
    r"|never\s+(?:reveal|admit|say|disclose)[^.]{0,40}\b(?:ai|bot|robot|automated)\b"
    r"|deny\s+(?:being|that\s+you\s+are)\s+(?:an?\s+)?(?:ai|bot)"
    r"|\bnot\s+an?\s+(?:ai|bot|robot)\b",
    re.I)


def _walk_strings(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj


def _platform_invariants(merged: dict) -> list[str]:
    """Invariants the PLATFORM enforces for every agent, on every write.

    These exist because each one was a real live-call defect that no human
    was going to catch by reading YAML (2026-08-04 audit):
      * bare scalars written into an object-list were accepted, the field was
        silently emptied, and the tool still answered "updated" — that is how
        both hardik twins lost their five objection answers;
      * a stage collecting a slot that does not exist used to advance
        vacuously, deleting a whole qualification stage from live calls;
      * an invented placeholder is SPOKEN with its braces
        ("...the parent or guardian of {{student_name}}?");
      * telling an agent it is a real person makes it say so when challenged,
        which is a compliance problem, not a tone choice.
    """
    out: list[str] = []

    for path in _OBJECT_LIST_FIELDS:
        node = merged
        for key in path:
            node = (node or {}).get(key) if isinstance(node, dict) else None
        if isinstance(node, list):
            bad = [type(x).__name__ for x in node if not isinstance(x, dict)]
            if bad:
                out.append(
                    f"{'.'.join(path)} must be a list of objects — got "
                    f"{', '.join(sorted(set(bad)))}. Send the full entries, "
                    "not bare values (this silently empties the field).")

    slot_names = {s.get("name") for s in (merged.get("slots") or [])
                  if isinstance(s, dict)}
    for st in ((merged.get("call_flow") or {}).get("stages") or []):
        if not isinstance(st, dict):
            continue
        for c in (st.get("collect") or []):
            if c not in slot_names:
                out.append(
                    f"stage {st.get('id')!r} collects {c!r}, which is not a "
                    "defined slot — the stage would never complete. Add the "
                    "slot or remove it from that stage's collect list.")

    for path, text in _walk_strings(merged):
        spoken = (path.startswith(_SPOKEN_PREFIXES)
                  and not path.startswith(_SPOKEN_EXCLUDE))
        if spoken:
            for m in _TOKEN_RE.finditer(text):
                name = (m.group(1) or m.group(2) or "").strip()
                if name not in _LIVE_TOKENS:
                    out.append(
                        f"{path} contains {m.group(0)!r}, which the engine does "
                        "not substitute — it would be read aloud to the caller. "
                        "Use {contact_name}, {customer_name}, {current_time}, "
                        "{honorific} or {title}.")
                    break
        if _HUMAN_CLAIM_RE.search(text) and path.split("[")[0] in (
                "personality.custom_instructions",
                "guardrails.never_rules", "guardrails.always_rules"):
            out.append(
                f"{path} tells the agent to claim it is human or to hide that "
                "it is an AI. The platform will not do that — use "
                "personality.ai_disclosure (deflect / admit / admit_if_pressed).")

    return out[:12]


def _paths_of_changes(changes: dict, prefix: str = "") -> set[str]:
    """Dotted path prefixes a partial-changes dict intends to touch (leaf
    subtrees; lists are replaced whole so the list path itself is the scope)."""
    out: set[str] = set()
    for k, v in (changes or {}).items():
        p = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            out |= _paths_of_changes(v, p)
        else:
            out.add(p)
    return out


def _read_full_definition(slug: str) -> tuple[dict, str]:
    """(canonical full definition dict, rev sha). Canonical = through
    from_dict→asdict so proposals diff against the same form the store's
    scoping proof uses."""
    raw, rev = agent_store.read_agent(slug, _AGENTS_DIR)
    canon = dataclasses.asdict(AgentDefinition.from_dict(raw))
    return canon, rev


# ── edit fidelity (2026-08-01 Jethalal incident) ────────────────────────────
# The copilot edited response_bank.opening_greeting on an outbound agent and
# reported success, but outbound calls speak outbound.opening_greeting — the
# edit was applied, unreachable, and inaudible. These helpers make every
# write answer "will the caller actually hear this?": dead-key writes are
# refused, shadowed writes carry a warning the doctrine forces the LLM to
# act on, and greeting edits return the engine-resolved next-call opening.


def _block_dead_paths(paths, notes: list | None = None) -> None:
    """Refuse a write whose changed paths include keys NO engine path reads —
    it would succeed, diff cleanly, and change nothing on any call.

    Two families. Dead LOCALIZED keys are always refused (proven dead, one
    shape, zero false positives). Schema-unknown keys — a setting the
    platform simply does not have — run through
    COPILOT_UNKNOWN_PATH_GUARD: 'warn' (default) appends to `notes` so the
    model must tell the user, 'strict' refuses the write, 'off' disables.
    Warn-first because a too-tight allowlist would edit-lock working agents,
    which is a worse failure than the one it prevents.
    """
    reasons = [r for r in (_surfaces.dead_path_reason(p) for p in paths) if r]
    if reasons:
        raise ValueError("Refused — this write would change nothing on any "
                         "call: " + " ".join(dict.fromkeys(reasons)))
    mode = os.environ.get("COPILOT_UNKNOWN_PATH_GUARD", "warn").lower()
    if mode == "off":
        return
    try:
        unknown = _controls.unknown_paths(paths)
    except Exception:  # noqa: BLE001 — the guard must never break a write
        logger.warning("[copilot] control-surface check failed", exc_info=True)
        return
    if not unknown:
        return
    near = _controls.nearest_controls(unknown[0][0], k=3)
    msg = ("NOT A REAL SETTING: " + "; ".join(r for _, r in unknown[:3])
           + ". Nothing on any call would change."
           + (" The closest real controls are: " + ", ".join(near) + "."
              if near else "")
           + " If the user asked for something this platform cannot do, TELL "
             "THEM plainly and name the closest thing you CAN do — never "
             "write the request into a nearby field.")
    if mode == "strict":
        raise ValueError("Refused — " + msg)
    logger.warning("[copilot] unknown control paths (warn mode): %s",
                   [p for p, _ in unknown[:5]])
    if notes is not None:
        notes.append(msg)


async def _fidelity_extras(merged: dict, changed_paths) -> dict:
    """`reachability` warnings + engine-resolved `hearable` readback for a
    result whose write touched opening-adjacent paths. Advisory: a resolver
    crash must never fail the user's edit, so this fails open (empty)."""
    extras: dict = {}
    try:
        findings = _surfaces.reachability_findings(merged, changed_paths)
        if findings["warnings"]:
            extras["reachability"] = findings["warnings"]
        if _surfaces.touches_opening(changed_paths):
            sr = await asyncio.to_thread(
                _surfaces.resolve_spoken_surfaces, merged)
            _dl = sr["default_language"]
            extras["hearable"] = {
                "direction": sr["direction"],
                "opening_next_call": {
                    lang: o["line"] for lang, o in sr["openings"].items()},
                "source": (sr["openings"].get(_dl) or {}).get("source", ""),
                # The second beat: what the caller hears the moment they
                # first respond. Invisible before 2026-08-04, which is how a
                # dictated second line could be "saved" five times over three
                # agents and never once spoken.
                "second_line_next_call": {
                    lang: o.get("second_line", "")
                    for lang, o in sr["openings"].items()},
                "second_source": (sr["openings"].get(_dl) or {}).get(
                    "second_source", ""),
                "note": (f"quote BOTH {_dl} lines to the user: "
                         f"opening_next_call is what the next "
                         f"{sr['direction']} call opens with, and "
                         "second_line_next_call is what the agent says the "
                         "moment the caller first responds"),
            }
            if sr.get("shadowed"):
                extras["hearable"]["shadowed"] = sr["shadowed"]
    except Exception as exc:  # noqa: BLE001 — advisory only
        logger.warning("[copilot] fidelity readback failed: %s", exc)
    return extras


# Paths that MAKE the two directions different — mirroring them would erase
# the distinction the twin pair exists for.
_DIRECTION_SPECIFIC_PREFIXES = ("outbound.", "response_bank.opening_greeting")


async def _mirror_to_twin(name: str, changes: dict, expected) -> dict:
    """Apply the same partial change to `name`'s opposite-direction twin.

    Best-effort by contract: the primary write has already committed, so
    every failure here is REPORTED, never raised. Runs the full pipeline on
    the twin's own current definition — never a copy of the primary's merged
    dict, which would import the primary's direction-specific fields.
    """
    out: dict = {"applied": False}
    try:
        twin = builder.counterpart_of(name)
        if not twin or not (_AGENTS_DIR / twin / "agent.yaml").exists():
            return {"applied": False,
                    "reason": "this agent has no opposite-direction version"}
        out["slug"] = twin
        if not _visible_slug(twin):
            return {"applied": False, "slug": twin,
                    "reason": "the other direction belongs to a different "
                              "account"}
        blocked = sorted({p for p in (str(x) for x in expected or ())
                          if p.startswith(_DIRECTION_SPECIFIC_PREFIXES)
                          or p == "call_direction"})
        if blocked:
            return {"applied": False, "slug": twin,
                    "reason": ("these settings are what make the two "
                               "directions different, so they were changed "
                               "on this agent only: "
                               + ", ".join(blocked[:5])
                               + ". Edit the other direction separately if "
                                 "you want it to match.")}
        t_current, t_rev = _read_full_definition(twin)
        t_merged = _deep_merge(copy.deepcopy(t_current), copy.deepcopy(changes))
        violations = _invariant_violations(t_merged)
        if violations:
            return {"applied": False, "slug": twin,
                    "reason": "; ".join(violations)}
        t_result = agent_store.write_agent(
            twin, t_merged, actor=_copilot_actor(), base_rev=t_rev,
            reason="copilot update (both directions): "
                   + ", ".join(sorted(changes.keys()))[:150],
            owner=builder._ctx_owner(), is_admin=builder._ctx_is_admin(),
            expected_paths=expected, agents_dir=_AGENTS_DIR)
        logger.info("[copilot] mirrored update to twin '%s' rev=%s",
                    twin, t_result["revision"])
        out = {"applied": True, "slug": twin,
               "revision": t_result["revision"],
               "display": _agent_display_name(twin)}
    except Exception as exc:  # noqa: BLE001 — the primary write already landed
        logger.warning("[copilot] twin mirror failed for '%s': %s", name, exc)
        out = {"applied": False, "slug": out.get("slug", ""),
               "reason": f"the other direction could not be updated: {exc}"}
    return out


def _twin_note(name: str, changed_paths) -> dict | None:
    """When `name` has a twin that still differs on the just-changed paths,
    say so — an edit meant for 'the agent' otherwise lands on one direction
    while the user tests the other. Advisory; never fails the edit."""
    try:
        other = builder.counterpart_of(name)
        if not other or not (_AGENTS_DIR / other / "agent.yaml").exists():
            return None
        mine, _ = _read_full_definition(name)
        theirs, _ = _read_full_definition(other)
        twin_diff = {d["path"] for d in _compute_diff(theirs, mine)}
        diverges = sorted({
            p for p in (str(x) for x in changed_paths or ())
            if any(dp == p or dp.startswith(p + ".") or dp.startswith(p + "[")
                   for dp in twin_diff)})
        if not diverges:
            return None
        return {"slug": other, "diverges_on": diverges[:8],
                "note": (f"this edit changed '{name}' only — its twin "
                         f"'{other}' still differs on these fields; tell the "
                         "user which direction you changed and offer to "
                         "match the twin if the change is direction-neutral")}
    except Exception:  # noqa: BLE001 — advisory only
        return None


def _visible_slug(folder: str) -> bool:
    """Is `folder` an agent the current tenant may see? Multi-tenant guard so
    name-resolution NEVER enumerates or matches another tenant's agents (the
    error messages here are shown to the user by the copilot)."""
    try:
        allowed = builder._visible_slugs(builder._ctx_owner())  # None = admin
    except Exception:
        allowed = None
    return allowed is None or folder in allowed


_DIRECTION_SUFFIXES = ("-outbound", "-inbound")


def _direction_of(folder: str) -> str:
    """'lead_qualifier-outbound' → 'outbound'; '' when the folder carries no
    direction suffix."""
    for suf in _DIRECTION_SUFFIXES:
        if folder.endswith(suf):
            return suf[1:]
    return ""


def _direction_pair(matches: list[tuple[str, str]]) -> bool:
    """True when every match is the SAME agent, differing only by call
    direction — the one ambiguity that can be settled without asking."""
    bases = set()
    for folder, _ in matches:
        d = _direction_of(folder)
        bases.add(folder[: -(len(d) + 1)] if d else folder)
    return len(bases) == 1 and all(_direction_of(f) for f, _ in matches)


def _pick_by_direction(matches: list[tuple[str, str]], dir_hint: str) -> str:
    if not dir_hint or not _direction_pair(matches):
        return ""
    for folder, _ in matches:
        if _direction_of(folder) == dir_hint:
            return folder
    return ""


def _resolve_agent_slug(name: str) -> str:
    """Users say 'Ananya', not 'dhronai_ea' — resolve display names to slugs.

    Order: exact folder → lowercased/underscored folder → the agent whose
    spoken name (identity.agent_name) matches, as a whole, a word, or a
    prefix. Ambiguity and no-match raise with a human-friendly list of the
    agents that DO exist, so the copilot can ask instead of guessing.

    Only the CALLER's own agents are ever considered or named — a tenant can
    never discover another tenant's agents through this resolver.
    """
    raw = str(name).strip()
    if not raw:
        raise ValueError("No agent name given")
    # Escape hatch for ambiguity errors: every error path below prints options
    # as "Display (folder)" — accept that exact form back, so any option we
    # show the user is itself a valid, unambiguous input.
    m = re.fullmatch(r".{0,80}\(([A-Za-z0-9_\-]{1,80})\)\s*", raw)
    if m and _visible_slug(m.group(1)) and (_AGENTS_DIR / m.group(1) / "agent.yaml").exists():
        return m.group(1)
    # Folder lookups only for plain names — anything with path separators or
    # dots must not escape the agents dir (or reach into _archived/).
    folder_match = ""
    if re.fullmatch(r"[A-Za-z0-9 _\-]{1,80}", raw):
        if _visible_slug(raw) and (_AGENTS_DIR / raw / "agent.yaml").exists():
            folder_match = raw
        else:
            slug = raw.lower().replace(" ", "_").replace("-", "_")
            for candidate in (slug, slug.replace("_", "-")):
                if _visible_slug(candidate) and (_AGENTS_DIR / candidate / "agent.yaml").exists():
                    folder_match = candidate
                    break
    if folder_match:
        # Guard against the split-brain trap: if a DIFFERENT agent's spoken
        # name is exactly what the user said, the folder shortcut would
        # silently edit the wrong agent (seen in production: two agents both
        # displaying "LesCall Agent"; edits went to the inactive duplicate
        # while calls ran the other). Ambiguity must be surfaced, not guessed.
        collisions = []
        for ypath in sorted(_AGENTS_DIR.glob("*/agent.yaml")):
            folder = ypath.parent.name
            if folder == folder_match or not _visible_slug(folder):
                continue
            try:
                display = (agent_registry.get(folder, _AGENTS_DIR).identity.agent_name or "").strip()
            except Exception:
                continue
            if display.lower() == raw.lower():
                collisions.append(f"{display} ({folder})")
        if collisions:
            try:
                fm_display = (agent_registry.get(folder_match, _AGENTS_DIR).identity.agent_name
                              or folder_match).strip()
            except Exception:
                fm_display = folder_match
            raise ValueError(
                f"'{raw}' is ambiguous — more than one agent answers to it: "
                f"{fm_display} ({folder_match}), {', '.join(collisions)}. Ask the user which "
                "one they mean, then pass that choice back EXACTLY as written including the "
                "part in parentheses."
            )
        return folder_match

    wanted = raw.lower()
    # A direction the user tacked onto the name — "Prakhar (outbound)",
    # "outbound Prakhar". Held aside rather than matched, so it can settle an
    # inbound/outbound pair below instead of derailing the name lookup (before
    # this, "Prakhar (outbound)" matched NOTHING and reported that no such
    # agent exists). Only a standalone word or a parenthetical counts — never a
    # direction embedded in a slug like "outbound_lead_qualifier".
    _DIR_HINT_RE = re.compile(
        r"\((inbound|outbound)\)\s*$|^(inbound|outbound)\s+|\s+(inbound|outbound)\s*$")
    dir_hint = ""
    m_dir = _DIR_HINT_RE.search(wanted)
    if m_dir:
        dir_hint = next(g for g in m_dir.groups() if g)
        wanted = _DIR_HINT_RE.sub(" ", wanted).strip()

    matches: list[tuple[str, str]] = []  # (folder, display)
    display_list: list[str] = []
    for ypath in sorted(_AGENTS_DIR.glob("*/agent.yaml")):
        folder = ypath.parent.name
        if not _visible_slug(folder):
            continue
        try:
            agent = agent_registry.get(folder, _AGENTS_DIR)
            display = (agent.identity.agent_name or "").strip()
        except Exception:
            continue
        display_list.append(f"{display or folder} ({folder})")
        d = display.lower()
        if d and (d == wanted or wanted in d.split() or d.startswith(wanted)):
            matches.append((folder, display))
    if len(matches) == 1:
        return matches[0][0]
    if len(matches) > 1:
        # The commonest "ambiguity" is one agent that exists in both call
        # directions. That is not a real question for the user when the
        # direction is already in what they said, or in the surrounding
        # request — resolve it instead of spending a turn asking.
        picked = _pick_by_direction(matches, dir_hint)
        if picked:
            return picked
        opts = ", ".join(f"{disp} ({f})" for f, disp in matches)
        extra = ""
        if _direction_pair(matches):
            extra = (" These are the same agent in both call directions — if the "
                     "user's request makes the direction clear, retry with "
                     f"'{matches[0][1]} (inbound)' or '{matches[0][1]} (outbound)' "
                     "instead of asking.")
        raise ValueError(f"More than one agent matches '{raw}': {opts}. Ask the "
                         f"user which one they mean.{extra}")
    raise ValueError(f"No agent called '{raw}'. The agents that exist are: {', '.join(display_list) or 'none yet'}.")


# Tools whose `name` argument refers to an existing agent (resolved before
# execution so the LLM can pass whatever the user said).
_TOOLS_WITH_AGENT_NAME = {
    "get_agent", "update_agent", "activate_agent",
    "get_routing", "set_routing", "replace_mentions", "find_text",
    "preview_opening",
    "propose_update", "update_stage", "add_stage", "remove_stage",
    "reorder_stages", "update_slot", "add_slot", "remove_slot",
    "list_revisions", "rollback_agent",
}

# Tools that change something real. Used by the truthfulness backstop: if all
# of these failed in a turn, the reply MUST admit it.
_MUTATING_TOOLS = {"create_agent", "update_agent", "activate_agent", "set_routing",
                   "replace_mentions", "apply_update", "update_stage", "add_stage",
                   "remove_stage", "reorder_stages", "update_slot", "add_slot",
                   "remove_slot", "rollback_agent"}

# Plain-English verbs for action chips ("Couldn't create the agent: ...").
_TOOL_VERBS = {
    "list_agents": "check the agents",
    "get_agent": "look at the agent",
    "validate_agent": "check the draft",
    "create_agent": "create the agent",
    "update_agent": "update the agent",
    "replace_mentions": "apply the replacement",
    "find_text": "search the agent",
    "list_controls": "check what the platform can change",
    "preview_opening": "preview the opening line",
    "activate_agent": "activate the agent",
    "get_routing": "check call routing",
    "set_routing": "update call routing",
    "propose_update": "prepare the change",
    "apply_update": "apply the change",
    "discard_update": "discard the change",
    "update_stage": "edit the call stage",
    "add_stage": "add the call stage",
    "remove_stage": "remove the call stage",
    "reorder_stages": "reorder the call stages",
    "update_slot": "edit the question",
    "add_slot": "add the question",
    "remove_slot": "remove the question",
    "list_revisions": "check the edit history",
    "rollback_agent": "undo the change",
}


def _failure_payload(tool: str, error: str) -> dict:
    """Tool-result body for a failed call — written so a sloppy LLM cannot
    misread it as success, and knows to either retry (fixed) or own up."""
    return {
        "status": "FAILED",
        "error": error,
        "reality": (
            "This operation DID NOT HAPPEN — nothing was created, changed, or activated. "
            "If you can fix the input, retry NOW in this same turn. Otherwise tell the "
            "user plainly that it failed and what you'll do about it. Do NOT claim success."
        ),
    }


# Friendly names for changed sections in action summaries — the user should
# never see raw config keys like "response_bank".
_SECTION_LABELS = {
    "identity": "who the agent is",
    "personality": "personality",
    "languages": "languages & voice",
    "call_flow": "call flow",
    "knowledge_base": "what the agent knows",
    "response_bank": "responses & greeting",
    "calendar": "calendar",
    "llm_config": "rules",
    "call_direction": "call type",
    "slots": "questions asked",
    "brand": "brand & trust",
    "outbound": "outbound scripts",
    "guardrails": "rules & compliance",
    "follow_up": "follow-ups",
    "post_call": "call outcomes",
}


def _friendly_sections(keys) -> str:
    return ", ".join(dict.fromkeys(_SECTION_LABELS.get(k, k.replace("_", " ")) for k in keys))


# What each tool DID, in plain past tense, for the one-line digest shown
# under a copilot reply. {a} = the agent's display name. Related edits share
# one phrase so five stage tweaks read as one line, not five chips.
_DIGEST_DONE = {
    "create_agent": "created {a}",
    "enrich_agent": "filled in {a}'s missing details",
    "update_agent": "updated {a}",
    "replace_mentions": "updated wording in {a}",
    "activate_agent": "put {a} live",
    "set_routing": "changed which agent handles your calls",
    "apply_update": "applied the change to {a}",
    "discard_update": "set the proposed change aside",
    "rollback_agent": "restored an earlier version of {a}",
    "update_stage": "adjusted {a}'s call flow",
    "add_stage": "adjusted {a}'s call flow",
    "remove_stage": "adjusted {a}'s call flow",
    "reorder_stages": "adjusted {a}'s call flow",
    "update_slot": "changed the questions {a} asks",
    "add_slot": "changed the questions {a} asks",
    "remove_slot": "changed the questions {a} asks",
    "ingest_document": "read your uploaded document",
    "propose_update": "prepared a change to {a} — waiting for your go-ahead",
}
# Interview note-taking — collapses to one "noted" line no matter how often
# the model calls it (mine_dump is the pre-v2 name still in old transcripts).
_DIGEST_NOTES = {"update_draft", "skip_layer", "mine_dump"}


def _digest_direction_note(slugs) -> str:
    """'both call directions' when a turn wrote to an inbound/outbound pair.

    The digest names agents by DISPLAY name, and twins share one — so
    without this, two real writes collapse into a single phrase and the user
    is never told the other direction moved too.
    """
    fams: dict[str, set[str]] = {}
    for s in slugs:
        s = str(s or "").strip().lower()
        for suffix, direction in (("-inbound", "inbound"),
                                  ("-outbound", "outbound")):
            if s.endswith(suffix):
                fams.setdefault(s[: -len(suffix)], set()).add(direction)
                break
    if any(len(d) > 1 for d in fams.values()):
        return " Both the inbound and outbound versions were updated."
    return ""


def _actions_digest(actions: list) -> str:
    """One short plain-English sentence describing what this turn actually
    did — what non-technical users see instead of per-tool chips. No tool
    names, config keys, or raw error text can reach this string; those stay
    in `actions` for the quick-action buttons and debugging."""
    phrases: list[str] = []
    notes = reads = 0
    read_agent = ""
    failed_change = False
    changed_slugs: list[str] = []
    for a in actions or []:
        tool = str(a.get("tool") or "")
        summary = str(a.get("summary") or "")
        slug = str(a.get("agent") or "").strip()
        if a.get("ok") and tool in _MUTATING_TOOLS and slug:
            changed_slugs.append(slug)
        if not a.get("ok"):
            # An apply_update held for the user's confirmation is the designed
            # flow, not a failure — the diff card explains it.
            if tool in _MUTATING_TOOLS and "confirm" not in summary.lower():
                failed_change = True
            continue
        tpl = _DIGEST_DONE.get(tool)
        if tpl:
            p = tpl.replace("{a}", _digest_agent_name(slug))
            if p not in phrases:
                phrases.append(p)
        elif tool in _DIGEST_NOTES:
            notes += 1
        else:
            reads += 1
            read_agent = read_agent or slug
    if phrases:
        if len(phrases) == 1:
            body = phrases[0]
        else:
            body = ", ".join(phrases[:-1]) + f" and {phrases[-1]}"
        line = body[0].upper() + body[1:] + ("" if body.endswith(("!", "?")) else ".")
        line += _digest_direction_note(changed_slugs)
        if failed_change:
            line += " One other change didn't go through."
        return line
    if failed_change:
        return "Tried to make a change but it didn't go through — nothing was changed."
    if notes:
        return "Noted what you've told me so far — nothing has been changed yet."
    if reads:
        who = _digest_agent_name(read_agent) if read_agent else "your setup"
        return f"Checked {who} — nothing was changed."
    return ""


def _remaining_hint(session: dict, actions: list) -> str:
    """What is demonstrably still open when the step budget runs out.

    Derived only from state the server already holds — a pending diff, an
    unedited twin, the last failed tool. Never from the model's prose, and
    never a guess: an empty string is the honest answer when the server
    cannot tell.
    """
    clauses: list[str] = []
    try:
        pend = session.get("pending_changes") or {}
        for slug in list(pend)[:1]:
            clauses.append(f"a change to {_digest_agent_name(slug)} "
                           "waiting for your go-ahead")
        edited = {str(a.get("agent") or "").strip().lower()
                  for a in actions or []
                  if a.get("ok") and a.get("tool") in _MUTATING_TOOLS}
        edited.discard("")
        for slug in sorted(edited):
            twin = builder.counterpart_of(slug)
            if twin and twin not in edited and \
                    (_AGENTS_DIR / twin / "agent.yaml").exists():
                other = "outbound" if twin.endswith("-outbound") else "inbound"
                clauses.append(f"matching the {other} version")
                break
        if len(clauses) < 2:
            for a in reversed(actions or []):
                if not a.get("ok") and a.get("tool") in _TOOL_VERBS:
                    clauses.append(
                        f"the {_TOOL_VERBS[a['tool']].replace('the ', '')} "
                        "that didn't go through")
                    break
    except Exception:  # noqa: BLE001 — advisory only
        return ""
    return " and ".join(clauses[:2])


def _agent_links_for(actions: list) -> list[str]:
    """Agent slugs from this turn's actions that exist on disk RIGHT NOW —
    the only ones the UI may offer Try/Open/Activate buttons for. A draft
    still being interviewed is not tryable: the pre-consent create_agent
    refusal carries the requested slug, and rendering buttons for it gave
    the user dead controls (incident 2026-08-01)."""
    out: list[str] = []
    for a in actions or []:
        slug = str(a.get("agent") or "").strip()
        if (slug and slug not in out and _visible_slug(slug)
                and (_AGENTS_DIR / slug / "agent.yaml").exists()):
            out.append(slug)
    return out


def _digest_agent_name(slug: str) -> str:
    """Spoken name for the digest line; a slug whose agent no longer exists
    (deleted since the chat) is prettified instead of shown raw."""
    if not slug:
        return "the agent"
    disp = _agent_display_name(slug)
    if disp == slug:
        return slug.replace("_", " ").replace("-", " ").title()
    return disp


def _replace_in_tree(node: Any, pattern: "re.Pattern", replacement: str) -> tuple[Any, int]:
    """Recursively replace text in every string VALUE of a nested structure.
    Keys are left untouched (stage ids, language codes, lookup keys must not
    silently change). Returns (new_node, replacement_count)."""
    if isinstance(node, str):
        # lambda replacement = literal text; backslashes in the replacement
        # would otherwise be interpreted as regex group references
        new, n = pattern.subn(lambda _m: replacement, node)
        return new, n
    if isinstance(node, (list, tuple)):
        total = 0
        out = []
        for item in node:
            new, n = _replace_in_tree(item, pattern, replacement)
            out.append(new)
            total += n
        return out, total  # tuples come back as lists; from_dict re-tuples known_projects
    if isinstance(node, dict):
        total = 0
        out = {}
        for k, v in node.items():
            new, n = _replace_in_tree(v, pattern, replacement)
            out[k] = new
            total += n
        return out, total
    return node, 0


def _find_in_tree(node: Any, pattern: "re.Pattern",
                  path: str = "") -> list[tuple[str, str]]:
    """(dotted-path, ±40-char snippet) for every string VALUE matching
    pattern. List items are keyed by their id/name when present (stages,
    slots, catalog entries) so paths read like call_flow.stages[GREETING].goal.
    Same traversal shape as _replace_in_tree; read-only."""
    hits: list[tuple[str, str]] = []
    if isinstance(node, str):
        m = pattern.search(node)
        if m:
            s, e = max(0, m.start() - 40), min(len(node), m.end() + 40)
            hits.append((path, ("…" if s else "") + node[s:e]
                         + ("…" if e < len(node) else "")))
    elif isinstance(node, dict):
        for k, v in node.items():
            hits += _find_in_tree(v, pattern, f"{path}.{k}" if path else str(k))
    elif isinstance(node, (list, tuple)):
        for i, item in enumerate(node):
            key = (item.get("id") or item.get("name")) \
                if isinstance(item, dict) else None
            hits += _find_in_tree(item, pattern, f"{path}[{key or i}]")
    return hits


# Bulbul v3 roster (verified against the live API 2026-07-24) plus the legacy
# v2-only names so gender detection still works on old configs. Mirror of
# calling-agent/backend/app/agent_definition/voice_catalog.py — keep in sync.
_MALE_VOICES = {
    "shubh", "aditya", "rahul", "rohan", "amit", "dev", "ratan", "varun",
    "manan", "sumit", "kabir", "aayan", "ashutosh", "advait", "anand",
    "tarun", "sunny", "mani", "gokul", "vijay", "mohit", "rehan", "soham",
    "abhilash", "karun",  # v2-only legacy
}
_FEMALE_VOICES = {
    "ritu", "priya", "neha", "pooja", "simran", "kavya", "ishita", "shreya",
    "roopa", "tanya", "shruti", "suhani", "kavitha", "rupali",
    "anushka", "manisha", "vidya",  # v2-only legacy
}

# Voice previews: samples generated by deploy/generate_voice_samples.py and
# served by server.py at /api/voices/sample/<speaker>. When a copilot reply
# names one of the sampled voices in a voice context, the response carries a
# voice_samples list and the chat renders play buttons.
_VOICE_SAMPLES_FILE = (Path(__file__).resolve().parents[1]
                       / "data" / "voice_samples" / "index.json")
# Speaker names are common Indian first names (Priya, Amit…) — only attach
# previews when the reply is actually talking about voices.
_VOICE_CONTEXT_RE = re.compile(
    r"\bvoices?\b|\bspeakers?\b|आवाज़|आवाज|અવાજ", re.I)


def _voice_samples_for(reply: str) -> list[dict]:
    if not reply or not _VOICE_CONTEXT_RE.search(reply):
        return []
    try:
        index = json.loads(_VOICE_SAMPLES_FILE.read_text())
    except Exception:
        return []
    low = reply.lower()
    return [
        {"speaker": v["speaker"], "gender": v.get("gender", ""),
         "url": f"/api/voices/sample/{v['speaker']}"}
        for v in index
        if isinstance(v, dict) and v.get("speaker")
        and re.search(rf"\b{re.escape(v['speaker'])}\b", low)
    ]
# Platform default lines that carry grammatical gender: feminine -> masculine.
_GENDERED_DEFAULTS: dict[str, dict[str, tuple[str, str]]] = {
    "timeout_fallbacks": {
        "hi": ("मैं इस पर वापस आती हूँ।", "मैं इस पर वापस आता हूँ।"),
        "gu": ("હું આના પર પાછી આવું છું.", "હું આના પર પાછો આવું છું."),
    },
}


def _enforce_invariants(merged: dict, notes: list[str]) -> None:
    """Final gate before validate/write: whatever the merge produced, these
    properties must hold or the agent would die on its first call."""
    langs = merged.setdefault("languages", {})
    sup = langs.get("supported")
    clean_sup = [x for x in sup if isinstance(x, str) and x.strip()] if isinstance(sup, list) else []
    if not clean_sup:
        clean_sup = ["en"]
        notes.append("no usable languages — defaulted to English")
    langs["supported"] = clean_sup
    if langs.get("default") not in clean_sup:
        langs["default"] = clean_sup[0]
    tv = langs.get("tts_voices")
    if not isinstance(tv, dict):
        tv = langs["tts_voices"] = {}
    # A missing per-language entry inherits the agent's existing speaker
    # (one voice across languages); a fully-unvoiced agent gets the
    # recommended speaker for its default language.
    _existing_spk = next(
        (v.get("speaker") for v in
         [tv.get(langs["default"])] + list(tv.values())
         if isinstance(v, dict) and v.get("speaker")), None)
    # No voice chosen: default to the gender of the first person the owner
    # WROTE ("main bol raha hoon" → a male voice), female only when the script
    # commits to neither. The flat female default gave the Kaapd brief's
    # "करण बोल रहा हूँ" a priya voice — a masculine script read by a woman,
    # the exact jai_mistry-2 defect, created by the platform (RUNBOOK §42).
    _authored = "" if _existing_spk else _authored_self_gender(
        _spoken_texts_of(merged))
    _fill_spk = _existing_spk or recommended_speaker(
        langs["default"], _authored or "female")
    if not _existing_spk and _authored and any(
            not isinstance(tv.get(c), dict) for c in clean_sup):
        notes.append(
            f"no voice was chosen, so a {_authored} voice ({_fill_spk}) was "
            f"picked — the script is written in the {_authored} first person. "
            "Tell the user which voice was picked and that they can change it "
            "(the agent's own he/she follows whichever voice they choose)")
    for code in clean_sup:
        if not isinstance(tv.get(code), dict):
            tv[code] = {"model": "bulbul:v3", "speaker": _fill_spk, "pace": 1.15}
    rn = langs.get("register_notes")
    if not isinstance(rn, dict):
        rn = langs["register_notes"] = {}
    for code in clean_sup:
        rn.setdefault(code, (_LANG_STRINGS.get(code) or _LANG_STRINGS["en"])["register_note"])

    # Gender consistency: the VOICE is a deliberate user choice, the "she"
    # pronoun and the feminine Hindi/Gujarati fallbacks are only platform
    # defaults — a default must never contradict the user's input (Kaapd
    # audit 2026-07-15: Karan spoke with shubh but carried pronouns 'she'
    # and 'मैं इस पर वापस आती हूँ।'). Only unambiguous cases are corrected,
    # and only where the value still IS the default.
    _speakers = {str((tv.get(c) or {}).get("speaker") or "").strip().lower()
                 for c in clean_sup}
    _male, _female = _speakers & _MALE_VOICES, _speakers & _FEMALE_VOICES
    if bool(_male) != bool(_female):
        _pron = "he" if _male else "she"
        _opposite = "she" if _male else "he"
        _fixed = []
        for _section in ("identity", "personality"):
            _node = merged.get(_section)
            if isinstance(_node, dict) and _node.get("pronouns") == _opposite:
                _node["pronouns"] = _pron
                _fixed.append(f"{_section}.pronouns")
        if _pron == "he":
            _rb = merged.get("response_bank")
            if isinstance(_rb, dict):
                for _field, _table in _GENDERED_DEFAULTS.items():
                    _val = _rb.get(_field)
                    if isinstance(_val, dict):
                        for _code, _pair in _table.items():
                            if _val.get(_code) == _pair[0]:
                                _val[_code] = _pair[1]
                                _fixed.append(f"response_bank.{_field}.{_code}")
        if _fixed:
            notes.append(f"matched the default wording to the {_pron} voice: "
                         + ", ".join(_fixed))

    cf = merged.setdefault("call_flow", {})
    if not isinstance(cf, dict):
        cf = merged["call_flow"] = {}
    if not cf.get("stages"):
        cf["stages"] = copy.deepcopy(_DEFAULT_STAGES)
        notes.append("call flow was empty — default 5-stage flow restored")

    rb = merged.setdefault("response_bank", {})
    if not isinstance(rb, dict):
        rb = merged["response_bank"] = {}
    greeting = rb.get("opening_greeting")
    if not isinstance(greeting, str) or not greeting.strip():
        agent_name = (merged.get("identity") or {}).get("agent_name") or "your assistant"
        rb["opening_greeting"] = f"Hello! I'm {agent_name}. How can I help you today?"
        notes.append("opening greeting was missing — a simple default was set (tell the user)")


# ─── gendered address is DATA, never a literal word (RUNBOOK §42) ────────────
# A spoken line that says "Sir" says it to every contact — the women on the
# list and the ones with no gender on file included (jai_mistry-2 greeted
# "SHAHANAZ Sir" and "SHREE Sir", both marked female). On every Copilot write
# a literal address in a spoken field becomes {honorific} / {title}, so the
# script the business reads says what the call will do. The engine also holds
# any literal that slips through to the contact's gender at speak time; this
# keeps the words on the page honest.
from backend.app.voice.address import (  # noqa: E402
    authored_self_gender as _authored_self_gender,
    protected_names as _address_protected_names,
    tokenize_address as _tokenize_address,
    tokenize_spoken_fields as _tokenize_spoken_fields,
)

# The user asked for gendered address in the SCRIPT — not merely typed "sir"
# at the Copilot ("ok sir, do it" is courtesy to us, not a request). An
# honorific word counts only alongside an ask about what the agent says.
_HONORIFIC_ASK_WORD_RE = re.compile(
    r"(?<![A-Za-z])(?:sir|ma['’]?am|maam|madam|mam|mr\.?|mrs\.?|ms\.?|"
    r"honorifics?|salutations?)(?![A-Za-z])|सर(?![ऀ-ॣ])|मैडम|मैम|"
    r"श्रीमती|श्रीमान|સર(?![઀-૥])|મેડમ", re.IGNORECASE)
_HONORIFIC_ASK_CONTEXT_RE = re.compile(
    r"(?<![A-Za-z])(?:say|says|saying|said|speak|address|addressed|call(?:ing)?\s+"
    r"(?:them|him|her|the\s+(?:caller|customer|client|contact))|greet|greeting|"
    r"opening|script|line|sentence|add|use|include|put|write|instead|replace|"
    r"agent|caller|customers?|contacts?|clients?|leads?|bol\w*|kah\w*|likh\w*|"
    r"laga\w*|daal\w*|dal\w*|jod\w*)(?![A-Za-z])|/|\bor\b", re.IGNORECASE)
_GENDER_DATA_MENTION_RE = re.compile(r"\bgender\b", re.IGNORECASE)
_GENDER_REMINDER_MARK = "Sir/Ma'am and Mr./Mrs. are dynamic"


def _asks_for_honorific(message: str) -> bool:
    m = message or ""
    return bool(_HONORIFIC_ASK_WORD_RE.search(m)
                and _HONORIFIC_ASK_CONTEXT_RE.search(m))


def _gender_data_reminder(owner) -> str:
    """The standing reminder, with this account's real coverage in it — a
    number is what makes "most contact lists have no gender" actionable."""
    have = total = None
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE LOWER(TRIM("
                "COALESCE(gender,''))) IN ('male','female')) AS g "
                "FROM contacts WHERE owner=?", (owner or "",)).fetchone()
        if row:
            total, have = int(row["n"] or 0), int(row["g"] or 0)
    except Exception:  # noqa: BLE001 — the reminder must never fail a turn
        logger.warning("[copilot] gender coverage lookup failed", exc_info=True)
    if total is None:
        coverage = ""
    elif total == 0:
        coverage = " You have no contacts yet — include a Gender column when you upload them."
    else:
        coverage = (f" Right now {have} of your {total} contact"
                    f"{'s' if total != 1 else ''} "
                    f"{'have' if have != 1 else 'has'} a gender on file.")
    return (f"ℹ️ {_GENDER_REMINDER_MARK} — the agent picks them from each "
            "contact's Gender, so they only work for contacts that have one."
            + coverage + " Add a Gender column (male/female or M/F) to your "
            "contact upload, send `gender` through the API, or set it in "
            "Contacts → Add/Edit. A contact without a gender is addressed by "
            "name only — the agent never guesses, and never uses a fixed Sir.")


def _all_strings(node) -> list[str]:
    out: list[str] = []
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            out += _all_strings(v)
    elif isinstance(node, list):
        for v in node:
            out += _all_strings(v)
    return out


def _spoken_texts_of(d: dict) -> list[str]:
    """Owner-written SPOKEN text only — never caller-side patterns, which are
    in the caller's own first person ("main gaadi chala raha hoon")."""
    out: list[str] = []
    rb = d.get("response_bank")
    if isinstance(rb, dict):
        for k, v in rb.items():
            if k == "objection_playbook" and isinstance(v, list):
                out += [s for it in v if isinstance(it, dict)
                        for s in _all_strings(it.get("answer"))]
            elif k not in ("objection_patterns",):
                out += _all_strings(v)
    ob = d.get("outbound")
    if isinstance(ob, dict):
        out += [s for k, v in ob.items() if k != "wrong_number_patterns"
                for s in _all_strings(v)]
    cf = d.get("call_flow")
    if isinstance(cf, dict):
        out += [str(st.get("goal") or "") for st in (cf.get("stages") or [])
                if isinstance(st, dict)]
        if isinstance(cf.get("connect"), dict):
            out += _all_strings(cf["connect"].get("intro_line"))
    return out


def _tokenize_spoken_address(d: dict, notes: list[str]) -> list[str]:
    """Literal Sir/Ma'am/Mr./Mrs. in the SPOKEN fields of `d` become
    {honorific}/{title} (address.tokenize_spoken_fields owns which fields
    those are). Returns the changed paths and tells the model via a note."""
    changed = _tokenize_spoken_fields(d)
    if changed:
        notes.append(
            f"a literal Sir/Ma'am/Mr./Mrs. in {len(changed)} spoken line(s) was "
            "rewritten as {honorific}/{title} — each contact is now addressed "
            "by the Gender on their contact record, and a contact with no "
            "gender on file is addressed by name only (tell the user, and "
            "remind them the Gender column is what makes it work)")
    return changed


# ─── create-time enrichment (F10) ────────────────────────────────────────────
# Kaapd incident (call cf6b0e23): the Copilot emitted six stages with
# advance_when {} / collect [] / role '' and 8-turn thresholds — every
# advancement gate in the engine was dead, the call never left stage 0, and
# the user's discovery questions (written correctly into the PITCH goal)
# never reached a single prompt. These rules derive the machine-readable
# wiring FROM what the user already provided. Every rule fills empty fields
# only, is idempotent, and emits a note.
#
# PLACEMENT IS LOAD-BEARING: called ONLY from the create_agent handler.
# _normalize_definition re-runs over EXISTING agents' change subtrees on
# every copilot edit — enrichment there would silently rewire the live
# pramukh_agent (which exactly matches the all-empty precondition).

from backend.app.voice.stage_roles import LEGACY_STAGE_ROLES  # noqa: E402

_ENRICH_ROLE_TURNS = {"greeting": 2, "confirm_person": 2, "discovery": 6,
                      "pitch": 4, "recommend": 4, "scheduling": 6,
                      "confirmation": 3}

# Names whose <1ms regex capture is keyed on the UNTYPED legacy dispatch —
# stamping a type on them silently downgrades capture to LLM-only.
_LEGACY_SLOT_NAMES = {"name", "location", "bhk", "configuration",
                      "property_type", "budget", "timeline", "loan_required",
                      "purpose", "occupation"}

_ROLE_HINTS = [
    ("confirm_person", re.compile(
        r"right person|correct person|speaking (with|to)|owner|"
        r"confirm.{0,24}(person|identity)|am i talking", re.I)),
    ("greeting", re.compile(
        r"introduce|good time|good moment|is (it|this) a good", re.I)),
    # 'delivery particulars' is data capture, NOT appointment scheduling —
    # a scheduling role wrongly revives the calendar band for the flow.
    ("scheduling", re.compile(
        r"schedul|book (a |an )?(visit|slot|appointment|time)|appointment|"
        r"time slot|call.?back", re.I)),
    ("recommend", re.compile(
        r"recommend|offer|match|suggest|sample", re.I)),
    ("closing", re.compile(
        r"thank(s| them| you).{0,40}(end|close)|politely end|"
        r"goodbye|wrap(s|ping)? up", re.I)),
]


def _slot_identity_tokens(slot: dict) -> set[str]:
    """Words that identify a slot in prose: its name, label and question
    wording (>=4-char words; 3 for name parts)."""
    toks: set[str] = set()
    name = str(slot.get("name") or "").replace("_", " ").lower()
    toks.update(w for w in name.split() if len(w) >= 3)
    toks.update(w for w in re.findall(
        r"\w+", str(slot.get("label") or "").lower()) if len(w) >= 4)
    q = slot.get("question") or {}
    if isinstance(q, dict):
        for v in q.values():
            toks.update(w for w in re.findall(r"\w+", str(v).lower())
                        if len(w) >= 4)
    return toks


def _enrich_new_agent(d: dict, notes: list[str]) -> None:
    """CREATE-ONLY: derive stage wiring (collect / advance_when / role /
    pacing), qa signals, defer_line and slot types from the user's own
    content. Fills-empty-only; a second application is a no-op."""
    # 0. OPENING — only a MISSING opening is filled, and only with a short
    # "Hello!": the platform default is greet-then-WAIT — the engine speaks
    # the full identity intro (composed from identity fields) the moment the
    # caller responds. A provided opening is NEVER touched, whatever its
    # length: rewriting a user's greeting is the incident this comment
    # commemorates (2026-07-30).
    rb = d.get("response_bank")
    if not isinstance(rb, dict):
        rb = {}
        d["response_bank"] = rb
    if not str(rb.get("opening_greeting") or "").strip():
        rb["opening_greeting"] = "Hello!"
        notes.append(
            "opening_greeting was empty — set to a short 'Hello!' (the "
            "agent greets, waits, and introduces itself the moment the "
            "caller responds)")

    cf = d.get("call_flow") or {}
    stages = cf.get("stages") if isinstance(cf, dict) else None
    if not isinstance(stages, list) or not stages:
        return
    stages = [s for s in stages if isinstance(s, dict)]
    slots = [s for s in (d.get("slots") or []) if isinstance(s, dict)]
    turns_default = {s["id"]: s["turns_until_advance"]
                     for s in (_DEFAULT_STAGES + _DEFAULT_OUTBOUND_STAGES)}
    last_idx = len(stages) - 1

    # 1. COLLECT — assign each required slot to the stage whose goal mentions
    # it. Conservative: only when NO stage is already wired (a partially
    # wired flow is a deliberate design the platform must not second-guess).
    if slots and not any(s.get("collect") for s in stages):
        assignments: dict[int, list[str]] = {}
        match_counts: dict[int, int] = {}
        unmatched: list[str] = []
        for slot in slots:
            if not slot.get("required", False):
                continue
            sname = str(slot.get("name") or "")
            if not sname:
                continue
            toks = _slot_identity_tokens(slot)
            best_i, best_score = -1, 0
            for i, st in enumerate(stages):
                goal_words = set(re.findall(
                    r"\w+", str(st.get("goal") or "").lower()))
                score = len(toks & goal_words)
                if score > best_score:
                    best_i, best_score = i, score
            if best_i >= 0:
                assignments.setdefault(best_i, []).append(sname)
                match_counts[best_i] = match_counts.get(best_i, 0) + best_score
            else:
                unmatched.append(sname)
        if unmatched:
            # Fall back to the stage that already matched the most slots,
            # else the first mid-flow stage.
            if match_counts:
                tgt = max(match_counts, key=lambda i: match_counts[i])
            else:
                tgt = next((i for i in range(len(stages))
                            if 0 < i < last_idx), 0)
            assignments.setdefault(tgt, []).extend(unmatched)
        for i, names in assignments.items():
            stages[i]["collect"] = names
            notes.append(
                f"wired slots {', '.join(names)} into stage "
                f"{stages[i].get('id', i)} (its goal asks for them)")

    # 2. ROLE — only for custom ids (legacy ids already derive correctly)
    # with no explicit role. Position first, then goal-text hints, then
    # collect ⇒ discovery.
    for i, st in enumerate(stages):
        sid = str(st.get("id") or "")
        if st.get("role") or sid.upper() in LEGACY_STAGE_ROLES:
            continue
        text = f"{sid.replace('_', ' ')} {st.get('goal') or ''}"
        role = ""
        if i == last_idx:
            role = "closing"
        else:
            for cand, pat in _ROLE_HINTS:
                if pat.search(text):
                    role = cand
                    break
            if not role and st.get("collect"):
                role = "discovery"
        if role:
            st["role"] = role
            notes.append(f"stage {sid or i}: derived role '{role}'")

    # 3. ADVANCE_WHEN — a stage that collects slots exits when they're
    # filled (with a time escape so a shy caller can't strand it); an
    # outbound confirm-person stage exits on the caller's yes.
    direction = str(d.get("call_direction") or "").lower()
    for st in stages[:-1] if len(stages) > 1 else []:
        if st.get("advance_when"):
            continue
        if st.get("collect"):
            st["advance_when"] = {"all_collect_filled": True, "max_turns": 6}
            notes.append(
                f"stage {st.get('id')}: advances when its slots are filled "
                "(6-turn escape)")
        elif (st.get("role") == "confirm_person" and direction == "outbound"):
            st["advance_when"] = {"intent": "affirmation",
                                  "min_turns": 1, "max_turns": 3}
            notes.append(
                f"stage {st.get('id')}: advances on the caller's yes "
                "(3-turn escape)")

    # 4. PACING — only values still equal to the normalizer's injected
    # default for that id are rewritable (an explicit different number is
    # user/LLM pacing and stands). Never touches the last stage.
    for i, st in enumerate(stages):
        if i == last_idx:
            continue
        sid = str(st.get("id") or "")
        injected = turns_default.get(sid, 8)
        cur = st.get("turns_until_advance")
        if cur != injected:
            continue
        role = st.get("role") or LEGACY_STAGE_ROLES.get(sid.upper(), "")
        new = _ENRICH_ROLE_TURNS.get(role, 4 if injected == 8 else injected)
        if role == "discovery":
            new = max(4, len(st.get("collect") or []) + 2)
        if new != cur:
            st["turns_until_advance"] = new
            notes.append(
                f"stage {sid or i}: pacing {cur}→{new} turns "
                "(conversational pace for its role)")

    # 5. QA FIELD SIGNALS — no-re-ask protection for the user's own slots.
    kb = d.setdefault("knowledge_base", {})
    if isinstance(kb, dict) and slots:
        signals = kb.setdefault("qa_field_signals", {})
        if isinstance(signals, dict):
            for slot in slots:
                sname = str(slot.get("name") or "")
                if not sname or sname in signals:
                    continue
                sig = [sname.replace("_", " ")]
                label = str(slot.get("label") or "").strip()
                if label and label.lower() not in [s.lower() for s in sig]:
                    sig.append(label)
                # The name and the label are what the FIELD is called, not
                # what the agent SAYS. QATracker matches these against the
                # agent's spoken line, so a signal list of "joining timeline"
                # / "Joining Timeline" never matched "Could you let us know
                # how fast you could join us?" — the no-re-ask protection was
                # inert on every builder agent until 2026-09-16, and a live
                # caller was asked the same question twice nine seconds apart
                # (call 851). The slot's own question IS the spoken form.
                _q = slot.get("question")
                if isinstance(_q, dict):
                    for phrase in _q.values():
                        phrase = str(phrase or "").strip().rstrip("?.!")
                        if len(phrase) >= 12 and phrase.lower() not in [
                                s.lower() for s in sig]:
                            sig.append(phrase)
                signals[sname] = sig

    # 6. PRICING DEFER LINE — the user's scripted rates answer becomes the
    # deflection line the FIRM RULES render (their words, not the generic
    # 'the team will share exact pricing').
    pricing = kb.get("pricing_policy") if isinstance(kb, dict) else None
    if (isinstance(pricing, dict) and pricing.get("mode") == "defer"
            and not pricing.get("defer_line")):
        playbook = ((d.get("response_bank") or {}).get("objection_playbook")
                    or [])
        _rates = re.compile(r"rate|price|pricing|expensive|cost|mehng", re.I)
        for o in playbook:
            if not isinstance(o, dict):
                continue
            hay = " ".join([str(o.get("id") or "")]
                           + [str(p) for p in (o.get("patterns") or [])])
            answer = o.get("answer") or {}
            if _rates.search(hay) and isinstance(answer, dict) and answer:
                default_lang = ((d.get("languages") or {}).get("default")
                                or "en")
                line = str(answer.get(default_lang) or answer.get("en")
                           or next(iter(answer.values()), "")).strip()
                if line:
                    pricing["defer_line"] = {default_lang: line}
                    notes.append(
                        "pricing defer line set from your scripted rates "
                        "answer")
                break

    # 7. SLOT TYPES — explicit contract; number-shaped slots gain the <1ms
    # regex capture. Legacy-named slots stay untyped (typing them kills
    # their name-keyed extractor).
    _qty = re.compile(r"quantity|qty|count|volume|number_of|how_many", re.I)
    _emailish = re.compile(r"e[-_]?mail", re.I)
    _phoneish = re.compile(r"phone|mobile|contact[-_]?number|whatsapp", re.I)
    for slot in slots:
        sname = str(slot.get("name") or "")
        if not sname or sname.lower() in _LEGACY_SLOT_NAMES:
            continue
        if not str(slot.get("type") or ""):
            if _emailish.search(sname):
                slot["type"] = "email"
            elif _phoneish.search(sname):
                slot["type"] = "phone"
            else:
                slot["type"] = "number" if _qty.search(sname) else "text"
        # B5 (2026-07-30): contact slots ship fully provisioned — the engine
        # captures/validates/reads back emails deterministically (spelled
        # read-back, pending-until-confirmed, capped asks → unresolved flag).
        # The engine also defaults these at runtime; writing them into the
        # YAML makes the contract visible to the user.
        if str(slot.get("type")) == "email":
            if str(slot.get("merge_policy") or "free") == "free":
                slot["merge_policy"] = "confirm"
            if not str(slot.get("confirm_readback") or ""):
                slot["confirm_readback"] = "spelled"
            if int(slot.get("max_asks") or 0) <= 0:
                slot["max_asks"] = 2
            notes.append(f"slot '{sname}': deterministic email capture "
                         "provisioned (confirm + spelled read-back + 2-ask cap)")
        elif (str(slot.get("type")) == "phone"
                and not str(slot.get("confirm_readback") or "")):
            slot["confirm_readback"] = "digit_groups_2"


def _changed_stage_ids(before: dict, after: dict) -> set[str]:
    """Stage ids whose definition this write changed (added or edited).

    Feeds the stage-scoped self-transition lint so an update result reports
    problems in the text it JUST wrote, without nagging about stages the turn
    never touched."""
    def _by_id(d: dict) -> dict:
        st = ((d or {}).get("call_flow") or {}).get("stages") or []
        return {str(s.get("id") or ""): s for s in st if isinstance(s, dict)}
    old, new = _by_id(before), _by_id(after)
    return {sid for sid, sdef in new.items()
            if sid and old.get(sid) != sdef}


def _safe_quality(merged: dict, **kw) -> list[str]:
    """`_quality_warnings` is ADVISORY, and it runs AFTER the write in
    create_agent / update_agent / update_draft. A crash inside it turned a
    write that had landed into a "FAILED — DID NOT HAPPEN" tool result
    (RUNBOOK §60: the Viraj pair was created, reported as not created,
    "created" again, and the wrong twin edited on the strength of that lie).
    A lint failure is a note in the result, never the write's outcome."""
    try:
        return _quality_warnings(merged, **kw)
    except Exception as exc:  # noqa: BLE001 — advisory by contract
        logger.exception("[copilot] quality lints crashed (the write itself "
                         "is unaffected): %s", exc)
        return [f"quality check unavailable this turn ({type(exc).__name__}: "
                f"{exc}) — the change itself was saved"]


def _quality_warnings(merged: dict,
                      include_wiring_lints: bool = True,
                      user_message: str = "",
                      touched_stages: "set[str] | None" = None) -> list[str]:
    """Deterministic depth check on the FINAL definition. The runtime only
    speaks as richly as these fields are written — a one-line stage goal or an
    empty knowledge paragraph makes a shallow call no matter how good the chat
    was. Returned in the tool result so the copilot enriches thin content in
    the SAME turn instead of reporting a hollow agent as done.

    include_wiring_lints: the F12 stage-wiring lints are emitted for
    CREATE/VALIDATE only — on update results they would nag every edit of a
    pre-enricher agent (e.g. the live pramukh_agent) and the copilot's
    same-turn self-repair would push unrequested pacing edits onto a
    production agent.

    user_message: this turn's request, used only by the second-beat lint —
    the same words decide whether a dictated line belongs in the opening or
    in call_flow.connect.intro_line, and the definition alone cannot tell.

    touched_stages: stage ids THIS write changed. Linting what a turn just
    wrote is never a nag, so the self-transition lint runs on those stages
    even when include_wiring_lints is off. Without this the §35 lint —
    written specifically to catch "advance to WRAP_UP" in a stage goal — was
    silent for the entire path agents are actually edited through: on
    2026-09-08 a Copilot goal rewrite reintroduced that exact phrasing into
    jai_mistry-outbound's AGENCY_CHALLENGES and the tool result came back
    "quality": [] (RUNBOOK §37)."""
    w: list[str] = []
    direction = str(merged.get("call_direction") or "inbound").strip().lower()

    cf = merged.get("call_flow") or {}
    stages = cf.get("stages") if isinstance(cf, dict) else None
    stages = stages if isinstance(stages, list) else []
    thin = [
        str(s.get("id") or f"#{i}")
        for i, s in enumerate(stages)
        if isinstance(s, dict) and len(str(s.get("goal") or "").strip()) < 45
    ]
    if thin:
        w.append(
            "These call stages have goals too short to steer a real call: "
            f"{', '.join(thin)}. Rewrite each as 2-4 full sentences of concrete instructions — "
            "what to say, which specific talking points to use, and the ONE thing to accomplish "
            "before moving on."
        )
    if len(stages) < 2:
        w.append(
            "The call has only one stage. A real call needs a flow "
            + ("(opening → pitch → handle questions → the ask → close)."
               if direction == "outbound" else
               "(greeting → discovery → propose next step → confirm → close).")
            + " Give it distinct stages so the agent knows how to progress."
        )

    # Stage goals are INSTRUCTIONS to the AI — a verbatim script line (or a
    # template placeholder) in a goal never reaches the caller's ear the way
    # the author intended. Both patterns showed up in production.
    scripty = [
        str(s.get("id") or f"#{i}")
        for i, s in enumerate(stages)
        if isinstance(s, dict) and (
            _any_placeholder(str(s.get("goal") or ""))
            or re.match(r"\s*(?:hello|hi|hey|namaste|namaskar|नमस्ते|नमस्कार)\b[ ,!]",
                        str(s.get("goal") or ""), re.IGNORECASE)
        )
    ]
    if scripty:
        w.append(
            f"These stage goals look like word-for-word script lines: {', '.join(scripty)}. "
            "Stage goals are INSTRUCTIONS to the AI, not spoken text — placeholders like "
            "{contact_name} do NOT work there. Put the exact opening line in "
            "response_bank.opening_greeting (where {contact_name} IS substituted) and rewrite "
            "the stage goal as instructions for what to accomplish. "
            "IF the sentence is what the agent says AFTER the caller first speaks or says "
            "hello, it belongs in call_flow.connect.intro_line instead — "
            "response_bank.opening_greeting is the FIRST line, spoken before the caller has "
            "said anything, so putting a second-beat line there overwrites their opening."
        )

    # The second-beat misfile: the user described what the agent says AFTER
    # the caller responds, and the turn wrote the FIRST line instead. This is
    # exactly how a dictated "say Hello, wait, then say <sentence>" request
    # ends up replacing the opening the user was happy with.
    if user_message and _describes_second_beat(user_message):
        _cf_conn = cf.get("connect") if isinstance(cf, dict) else None
        if not (isinstance(_cf_conn, dict) and _cf_conn.get("intro_line")):
            w.append(
                "The user described the SECOND line — what the agent says after the caller "
                "first responds. That line lives in call_flow.connect.intro_line; "
                "response_bank.opening_greeting / outbound.opening_greeting are spoken BEFORE "
                "the caller has said anything. Put their exact sentence in "
                "call_flow.connect.intro_line, or tell them which of the two lines you changed."
            )

    kb = merged.get("knowledge_base") or {}
    ppt = str((kb.get("portfolio_prompt_text") if isinstance(kb, dict) else "") or "").strip()
    if not ppt or ppt.startswith("No specific products") or len(ppt) < 80:
        w.append(
            "The knowledge paragraph is empty or generic. Fill it with the SPECIFIC facts, "
            "talking points, names, numbers and details this agent must know — this text is "
            "read on every single turn of the call, so it is where the substance lives."
        )

    rb = merged.get("response_bank") or {}
    if direction == "outbound" and isinstance(rb, dict) and not rb.get("objection_responses"):
        w.append(
            "This outbound agent has no objection responses. Add 1-2 per language for the "
            "pushback this specific call will get (e.g. 'not now', 'why should I', a flat no)."
        )
    greeting = str(rb.get("opening_greeting") or "") if isinstance(rb, dict) else ""

    # ── the generic second line ─────────────────────────────────────────────
    # A short opening ("Hello") is the platform default and is correct — but
    # the line spoken the instant the caller answers is then COMPOSED from
    # identity, so every agent built this way says the same sentence:
    # "Hello! This is <Agent> from <Company>. Can you hear me clearly?"
    # (reported live 2026-08-04: "all agents now have a common opening
    # line"). The business's own words belong in call_flow.connect.intro_line.
    _ob_g = (merged.get("outbound") or {}).get("opening_greeting") or {}
    _first_lines = [greeting] + [str(v) for v in (
        _ob_g.values() if isinstance(_ob_g, dict) else [])]
    _all_short = all(len(str(x).strip()) < 20 for x in _first_lines if str(x).strip())
    _conn = cf.get("connect") if isinstance(cf, dict) else None
    _has_intro = isinstance(_conn, dict) and bool(_conn.get("intro_line"))
    # An outbound agent's identity line is spoken as the second beat too, so
    # having one means the caller is NOT getting the generic sentence.
    if direction == "outbound" and (merged.get("outbound") or {}).get(
            "identity_confirm_line"):
        _has_intro = True
    if _first_lines and _all_short and not _has_intro:
        w.append(
            "This agent opens with a short greeting and has no second line set, so the "
            "platform will speak a GENERIC line the moment the caller answers — "
            "\"Hello! This is <agent> from <company>. Can you hear me clearly?\" — the "
            "same sentence every other agent says. Ask the user for the exact words they "
            "want there and put them in call_flow.connect.intro_line (per language; "
            "{contact_name} IS substituted). If they are happy with the generic line, say "
            "so once and move on."
        )

    # ── language switching ──────────────────────────────────────────────────
    _langs = merged.get("languages") or {}
    _supported = [str(x) for x in (_langs.get("supported") or []) if str(x).strip()]
    if len(_supported) > 1 and _langs.get("auto_switch") is False:
        w.append(
            f"This agent is built for {', '.join(_supported)} but automatic language "
            "switching is OFF, so it will answer every caller in "
            f"'{_langs.get('default') or _supported[0]}' even when they speak one of its "
            "other languages. Set languages.auto_switch true unless the business "
            "explicitly wants a single-language agent."
        )
    if len(_supported) > 1:
        _voices = _langs.get("tts_voices") or {}
        _missing_voice = [lg for lg in _supported
                          if isinstance(_voices, dict) and not _voices.get(lg)]
        if _missing_voice:
            w.append(
                f"No voice is configured for {', '.join(_missing_voice)}, but the agent is "
                "built for those languages and will switch into them when the caller does. "
                "Add languages.tts_voices for each, or drop the language from supported."
            )

    _name_m = re.search(r"\b(?:Mr|Mrs|Ms|Miss|Dr|Shri|Smt)\.?\s+([A-Zऀ-ॿ][\w''-]*)", greeting)
    _agent_name = str(((merged.get("identity") or {}).get("agent_name")) or "").lower()
    if (
        direction == "outbound"
        and "{contact_name}" not in greeting
        and _name_m
        # the agent introducing ITSELF ("this is Dr. Mehta from City Clinic")
        # is fine — only a hardcoded name that isn't the agent's own is stale
        and _name_m.group(1).lower() not in _agent_name
    ):
        w.append(
            f"The opening greeting hardcodes the name '{_name_m.group(0)}', but this outbound "
            "agent may call different contacts. If that is the person being called, replace the "
            "name with {contact_name} — at call time it becomes the dialed contact's name from "
            "Outbound Calls. If it is the agent's own name, ignore this."
        )

    # A placeholder the RUNTIME DOES NOT KNOW is spoken literally ("dollar
    # contact name"). The four it does know work in every authored spoken
    # line, not just the greeting (RUNBOOK §41 — see the header above
    # _PLACEHOLDER_RE). This lint predates §41 and kept flagging ALL of them
    # outside opening_greeting, telling the Copilot to "rewrite them without
    # placeholders" — so every Copilot edit quietly undid the §42 migration:
    # on 2026-09-18 a rewrite of jai_mistry-outbound stripped {honorific} out
    # of objection_responses, closing_responses and graceful_close, and those
    # contacts who DO carry a gender lost their Sir/Ma'am on every closing
    # line. Only what the engine cannot resolve is flagged now.
    if isinstance(rb, dict):
        def _walk(node):
            if isinstance(node, str):
                yield node
            elif isinstance(node, dict):
                for v in node.values():
                    yield from _walk(v)
            elif isinstance(node, (list, tuple)):
                for v in node:
                    yield from _walk(v)
        bad_fields = sorted({
            key for key, val in rb.items()
            if key != "opening_greeting"
            and any(_unresolvable_placeholder(s) for s in _walk(val))
        })
        if bad_fields:
            w.append(
                "These response fields contain placeholder text the runtime does not understand, "
                f"so it would be SPOKEN LITERALLY on a call: {', '.join(bad_fields)}. "
                "Exactly four placeholders work, in any spoken line: {contact_name}, "
                "{current_time}, {honorific} and {title} (the last two resolve to nothing for a "
                "contact with no gender on file). Write them in that exact form, or remove them."
            )

    # ── F12: stage-wiring lints ──────────────────────────────────────────────
    # Suppressed for pure-legacy flows (every id in LEGACY_STAGE_ROLES):
    # legacy pacing is intentional, and warnings surface on UPDATE results
    # too — an LLM 'fixing' production Pramukh pacing from a nag is a real
    # failure mode.
    _all_legacy = bool(stages) and all(
        str(s.get("id") or "").upper() in LEGACY_STAGE_ROLES
        for s in stages if isinstance(s, dict))
    if stages and not _all_legacy and include_wiring_lints:
        slot_names = {str(s.get("name") or "") for s in
                      (merged.get("slots") or []) if isinstance(s, dict)
                      and s.get("required")}
        slot_names.discard("")
        collected = {str(n) for s in stages if isinstance(s, dict)
                     for n in (s.get("collect") or [])}
        orphaned = sorted(slot_names - collected)
        if orphaned:
            w.append(
                "Required slots collected by NO stage: "
                f"{', '.join(orphaned)}. The agent is never forced to ask "
                "for them — add them to the collect list of the stage whose "
                "goal asks those questions."
            )
        _exempt_roles = ("scheduling", "confirmation", "closing")
        slow = [
            str(s.get("id") or f"#{i}") for i, s in enumerate(stages[:-1])
            if isinstance(s, dict)
            and str(s.get("id") or "").upper() not in LEGACY_STAGE_ROLES
            and (s.get("role") or "") not in _exempt_roles
            and 8 <= int(s.get("turns_until_advance") or 0) < 99
            and not (s.get("advance_when") or {})
        ]
        if slow:
            w.append(
                "These stages need 8+ caller turns to advance and have no "
                f"advance_when condition: {', '.join(slow)}. At conversational "
                "pace one such stage consumes an entire call — set "
                "advance_when (all_collect_filled / intent + max_turns) or "
                "turns_until_advance 2-4."
            )
        unstoppable = [
            str(s.get("id") or f"#{i}") for i, s in enumerate(stages)
            if isinstance(s, dict)
            and any(k in (s.get("advance_when") or {})
                    for k in ("intent", "slots", "all_collect_filled"))
            and not (s.get("advance_when") or {}).get("max_turns")
        ]
        if unstoppable:
            w.append(
                "These stages' advance_when has no max_turns escape: "
                f"{', '.join(unstoppable)}. A condition that never fires "
                "strands the call in that stage forever — add max_turns."
            )


    # A goal that tells the model to transition itself ("advance to closing",
    # "move to the next stage", "wrap up", "terminate the call") is asking for
    # something it cannot see: the destination stage's actual script is never
    # in THIS turn's prompt. On 2026-09-08 exactly this phrasing, paired with
    # an all_collect_filled gate an early-exit answer could never satisfy,
    # made a live agent invent an unscripted "should we end the call?" line
    # instead of using its own closing script (RUNBOOK §35). Flagged whenever
    # the stage ALSO lacks a `branches` entry — a stage that already branches
    # has a real, immediate way to reach that destination, so the same wording
    # there is describing what the ENGINE does, not asking the model to do it.
    #
    # NOT gated by include_wiring_lints: on updates it runs over the stages
    # this write TOUCHED, which is never a nag about someone else's pre-
    # existing wiring. Gating it wholesale is why the §35 lint was silent when
    # a Copilot rewrite put "and advance to WRAP_UP" straight back into the
    # very agent it had been written for (RUNBOOK §37).
    _lint_stage_scope = (
        stages if include_wiring_lints
        else [s for s in stages if isinstance(s, dict)
              and str(s.get("id") or "") in (touched_stages or set())])
    if _lint_stage_scope and not _all_legacy:
        _transition_talk = re.compile(
            r"\b(advance|move|proceed|transition)\b[^.\n]{0,40}\b"
            r"(closing|close|next stage|wrap.?up)\b"
            r"|\bwrap.?up\b[^.\n]{0,30}\bcall\b"
            # An INSTRUCTION to hang up — "then terminate the call
            # immediately". Deliberately not a bare "…end the call…", which
            # also matches a goal DESCRIBING platform behaviour ("the
            # platform ends the call itself; never ask to cut it yourself"):
            # flagging that would push authors to delete the very sentence
            # that keeps the model from hanging up.
            r"|(?:^|[.\n]\s*)(?:then\s+)?(?:terminate|end|hang ?up|cut)\s+"
            r"the\s+call\b",
            re.IGNORECASE | re.MULTILINE)
        self_transitioning = [
            str(s.get("id") or f"#{i}") for i, s in enumerate(_lint_stage_scope)
            if isinstance(s, dict)
            and _transition_talk.search(str(s.get("goal") or ""))
            and not (s.get("branches") or [])
        ]
        if self_transitioning:
            w.append(
                "These stage goals tell the model to transition itself: "
                f"{', '.join(self_transitioning)}. It cannot — it only sees "
                "THIS stage's goal, never the destination's script, so "
                "'advance to closing' gets improvised instead of spoken, and "
                "'terminate the call' is not the model's to do (the platform "
                "ends calls, and asks the caller's permission first). "
                "For an early-exit answer (a 'no', a reason with no fixed "
                "vocabulary), branch to a dedicated stage instead: "
                "branches: [{when_intent: negation, goto: STAGE_ID}] for a "
                "yes/no split, or {when_slot: {name: SLOT}, goto: STAGE_ID} "
                "(no `equals`) once a free-text follow-up has any answer — "
                "then say the actual line at the TOP of that stage's goal."
            )

    # Playbook language coverage: the deterministic matcher is a literal
    # substring test, so patterns/answers missing the DEFAULT language can
    # never fire on real calls in that language.
    _default_lang = str(((merged.get("languages") or {}).get("default"))
                        or "en")
    _playbook = ((merged.get("response_bank") or {}).get("objection_playbook")
                 or [])
    _uncovered = [
        str(o.get("id") or "?") for o in _playbook
        if isinstance(o, dict)
        and isinstance(o.get("answer"), dict) and o.get("answer")
        and not str(o["answer"].get(_default_lang) or "").strip()
    ]
    if _uncovered and _default_lang != "en":
        w.append(
            f"Objection answers missing the agent's default language "
            f"({_default_lang}): {', '.join(_uncovered)}. Add "
            f"{_default_lang} versions so the scripted answers can be "
            "spoken on real calls."
        )

    # Playbook SCRIPT coverage: patterns are a literal substring test with no
    # transliteration. An owner dictating a Hindi/Gujarati agent's triggers
    # almost always types them romanized ("kya karte ho aap"), but Sarvam STT
    # transcribes fluent speech in Devanagari/Gujarati script — a pattern
    # list with zero native-script characters can never match a real caller,
    # regardless of wording (RUNBOOK §36: this is exactly how a Jai Mistry
    # caller's real question got an improvised answer instead of the owner's
    # scripted one). Checked whenever hi/gu is supported, not just default,
    # since a bilingual caller may ask in either.
    _native_script_langs = {"hi", "gu"} & set(
        (merged.get("languages") or {}).get("supported") or [])
    if _native_script_langs and _playbook:
        _romanized_only = [
            str(o.get("id") or "?") for o in _playbook
            if isinstance(o, dict)
            and (o.get("patterns") or [])
            and not any(re.search(r"[ऀ-ॿ઀-૿]", str(p))
                        for p in o["patterns"])
        ]
        if _romanized_only:
            w.append(
                "These objection_playbook entries have patterns in Latin "
                f"script only: {', '.join(_romanized_only)}. This agent "
                f"speaks {'/'.join(sorted(_native_script_langs))}, and real "
                "speech in that language transcribes in native script "
                "(Devanagari/Gujarati) — add that alongside the romanized "
                "form for each pattern, or a real caller's question can "
                "never trigger the scripted answer."
            )

    # Same defect, in the field that decides whether a DECLINE is heard as
    # one. knowledge_base.objection_patterns drives detect_intent's objection
    # classification, which drives decline_count and call_flow.decline_
    # threshold. Romanized-only there means a Hindi/Gujarati caller can say
    # "no" as often as they like and the agent never registers a decline
    # (RUNBOOK §37 — the platform's own defaults now cover both scripts, so
    # this fires only on an agent that REPLACED them with a Latin-only list).
    if _native_script_langs:
        _kb = merged.get("knowledge_base") or {}
        _kb_pats = [str(x) for x in (_kb.get("objection_patterns") or [])]
        # 'extend' (the recommended mode) merges the platform defaults at
        # runtime, and those now carry Devanagari + Gujarati — such an agent
        # is covered and must not be nagged. Only a list that REPLACES the
        # defaults with Latin-only text is actually blind.
        _replaces = str(_kb.get("objection_patterns_mode")
                        or "replace").strip().lower() != "extend"
        if (_kb_pats and _replaces
                and not any(re.search(r"[ऀ-ॿ઀-૿]", x) for x in _kb_pats)):
            w.append(
                "knowledge_base.objection_patterns is Latin-script only, but "
                f"this agent speaks {'/'.join(sorted(_native_script_langs))}. "
                "These patterns decide whether the platform HEARS a decline "
                "at all — a caller declining in their own language would "
                "never count toward decline_threshold, so the agent keeps "
                "pitching someone who already said no. Add the native-script "
                "forms alongside the romanized ones."
            )

    # ── Cross-surface contradiction lints (2026-07-28) ──────────────────
    # A live call spoke a banned "twenty-minute demo … Tuesday at 10 AM"
    # AFTER the stage goals were edited: call_goal (rendered every turn as
    # CALL MISSION) and the scheduling FIRM RULES kept ordering the old
    # behaviour. These catch the three definition shapes that let that
    # happen. Deliberately not gated by include_wiring_lints — they flag
    # real contradictions, not enrichment gaps.
    _sched = cf.get("scheduling") if isinstance(cf, dict) else None
    if not isinstance(_sched, dict):  # JSON text / a word / a list — §60
        _sched = {}
    _exact_n = (str(_sched.get("style") or "exact_n") != "flexible"
                and _sched.get("enabled") is not False)
    _cal = merged.get("calendar")
    if not isinstance(_cal, dict):
        _cal = {}
    _has_slots = (bool(_cal.get("mock_slots"))
                  and str(_cal.get("type") or "mock") != "none")
    _goal_surfaces = [("call_flow.call_goal",
                       str(cf.get("call_goal") or "")
                       if isinstance(cf, dict) else "")]
    _goal_surfaces += [
        (f"stage {s.get('id') or f'#{i}'}", str(s.get("goal") or ""))
        for i, s in enumerate(stages) if isinstance(s, dict)]

    # (i) goals say the CALLER picks the time, but scheduling still resolves
    # to exact_n — the always-injected platform rules contradict the goals.
    _caller_picks_re = re.compile(
        r"do(?:n'?t| not)?\s+(?:suggest|offer|propose|give)[^.]{0,60}"
        r"(?:time|date|slot)"
        r"|let them (?:tell|say|pick|choose)"
        r"|ask (?:them )?when they(?:'d| would)?\s*(?:be )?free"
        r"|when they (?:are|'re|would be) free",
        re.IGNORECASE)
    _conflicting = [n for n, g in _goal_surfaces
                    if g and _caller_picks_re.search(g)]
    if _exact_n and _conflicting:
        w.append(
            "These goals tell the agent to let the CALLER propose the time "
            f"({', '.join(_conflicting)}), but call_flow.scheduling still "
            "resolves to 'exact_n' (agent offers N specific times). Set "
            "call_flow.scheduling to {\"style\": \"flexible\"} so the "
            "always-injected platform rules match the goals."
        )

    # (ii) unreachable beats: the turn that satisfies advance_when is
    # answered under the NEXT stage's goal, so a multi-beat goal on an
    # intent-advanced stage with min_turns<=1 only ever speaks its first beat.
    _unreachable = []
    for i, s in enumerate(stages):
        if not isinstance(s, dict):
            continue
        aw = s.get("advance_when") or {}
        if not aw.get("intent"):
            continue
        try:
            _min = int(aw.get("min_turns") or 0)
        except (TypeError, ValueError):
            _min = 0
        if _min > 1:
            continue
        _beats = [x for x in re.split(r"[.!?]", str(s.get("goal") or ""))
                  if len(x.strip()) > 15]
        if len(_beats) >= 2:
            _unreachable.append(str(s.get("id") or f"#{i}"))
    if _unreachable:
        w.append(
            "These stages advance the moment the caller's trigger intent "
            f"lands, so only the FIRST beat of their goal is ever spoken: "
            f"{', '.join(_unreachable)}. The reply to the triggering turn is "
            "generated under the NEXT stage's goal. Keep only the trigger "
            "beat here (e.g. the identity question) and move everything "
            "meant for AFTER the caller's answer to the TOP of the next "
            "stage's goal — or set advance_when.min_turns: 2."
        )

    # (iii) meeting-booking language with no calendar while exact_n: the
    # agent has no times to offer — 'flexible' (caller proposes) is the
    # correct declaration, or add real slots.
    if _exact_n and not _has_slots:
        _booking_re = re.compile(
            r"\b(demo(?:nstration)?s?|meeting|appointment|schedul\w*|"
            r"book(?:ing)?|visit|call ?back)\b", re.IGNORECASE)
        _bookish = [n for n, g in _goal_surfaces if g and _booking_re.search(g)]
        if _bookish:
            w.append(
                f"This agent books meetings ({', '.join(_bookish)}) but "
                "calendar.mock_slots is EMPTY while call_flow.scheduling "
                "resolves to 'exact_n' — it has no times to offer. Set "
                "call_flow.scheduling to {\"style\": \"flexible\"} (the "
                "caller proposes the time) or add real calendar.mock_slots."
            )

    # NOTE deliberately NO lint on short/bare openings: "Hello" then wait is
    # the intended platform style — the engine's connect fast path speaks the
    # identity intro when the caller responds. (A bare-opening lint shipped
    # 2026-07-30 and was reverted the same day: it nagged users into
    # monologue openings.)

    # Termination sanity: rules that can never fire, or caps that would cut
    # every call short, must be flagged before they reach a live agent.
    term = cf.get("termination") if isinstance(cf, dict) else None
    term = term if isinstance(term, dict) else {}
    if (term.get("end_conditions")
            and term.get("llm_end_call") is False):
        w.append(
            "call_flow.termination.end_conditions is set but llm_end_call is "
            "false — the agent is given END-CALL RULES it has no way to act "
            "on, so no call will ever end from them. Remove the conditions "
            "or set llm_end_call true."
        )
    try:
        _cap = float(term.get("max_call_minutes") or 0)
    except (TypeError, ValueError):
        _cap = 0.0
    if 0 < _cap < 2:
        w.append(
            f"call_flow.termination.max_call_minutes is {_cap:g} — under two "
            "minutes barely covers a greeting and one question; every call "
            "would be cut mid-conversation. Confirm the user really wants a "
            "cap this low, or raise it."
        )
    if term.get("confirm_before_end") is False:
        w.append(
            "call_flow.termination.confirm_before_end is false — the agent "
            "will hang up on the first thing it hears as a goodbye, with no "
            "check. Speech recognition mishears this often ('okay, by the "
            "way...' → 'okay bye'), and a live call was cut that way. Only "
            "keep this off if the user explicitly asked for instant hangups."
        )
    return w


def _parse_tool_args(raw: Any) -> dict:
    """Tolerant parse of a tool call's `arguments` field.

    Groq/llama emits empty strings, "null", or double-encoded JSON for
    no-parameter tools — treat all of those as {} instead of erroring.
    """
    if raw is None or isinstance(raw, dict):
        return raw or {}
    text = str(raw).strip()
    if not text or text in ("null", '""', "''", "{}"):
        return {}
    parsed = json.loads(text)
    if isinstance(parsed, str):  # double-encoded
        parsed = json.loads(parsed) if parsed.strip() else {}
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError("arguments must be a JSON object")
    return parsed


# ─────────────────────────────────────────────────────────────────────────────
# Tools — same underlying logic as agent_builder_api endpoints
# ─────────────────────────────────────────────────────────────────────────────
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_agents",
            "description": "List every voice agent on the platform and which one is active (answering inbound calls).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_agent",
            "description": "Read the full current definition of one agent (identity, languages, stages, greeting, knowledge, etc.). Always call this before editing an agent you haven't read this conversation.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description":
                    "agent slug or spoken name, e.g. 'pramukh'. When one agent "
                    "exists in both call directions, add the direction rather "
                    "than asking: 'Prakhar (outbound)'."}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_agent",
            "description": "Dry-run schema validation of a (possibly partial) agent definition merged onto platform defaults. No files are written.",
            "parameters": {
                "type": "object",
                "properties": {"definition": {"type": "object", "description": "agent definition JSON to validate"}},
                "required": ["definition"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_agent",
            "description": (
                "Create a new voice agent. Provide ONLY the fields you want to customize — "
                "every unspecified section is filled with sensible platform defaults "
                "(calendar slots, filler lines, timeouts, objection patterns, stage skeleton). "
                "Fails if the slug already exists."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "slug: lowercase letters, digits, _ or -, e.g. 'salon_receptionist'"},
                    "definition": {"type": "object", "description": "partial agent definition JSON (identity, languages, call_flow, knowledge_base, response_bank, personality, llm_config)"},
                },
                "required": ["name", "definition"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_agent",
            "description": (
                "Edit an existing agent. Pass the MINIMAL partial changes — they are deep-merged "
                "onto the current full definition, so everything else is preserved. "
                "Nested objects merge; lists (e.g. call_flow.stages) are replaced whole."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "agent slug"},
                    "changes": {"type": "object", "description": "partial definition, e.g. {\"response_bank\": {\"opening_greeting\": \"...\"}}"},
                    "both_directions": {
                        "type": "boolean",
                        "description": (
                            "true = apply the SAME change to this agent AND its opposite-direction "
                            "twin in one step. Use it for direction-neutral changes (wording, rules, "
                            "knowledge, voice, pacing) — it is the correct way to honour 'change it "
                            "in both the inbound and outbound scripts'. NEVER for the greeting, any "
                            "outbound.* field, or call_direction: those are what make the two "
                            "directions different, and they are refused on the twin."
                        ),
                    },
                },
                "required": ["name", "changes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_mentions",
            "description": (
                "Find-and-replace a piece of text EVERYWHERE in an agent — greeting, stage goals, "
                "knowledge, canned responses, rules, everything at once (case-insensitive). "
                "ALWAYS use this instead of update_agent when the user renames or swaps something that "
                "appears in multiple places: a person (boss/owner), company name, product, city, phone "
                "number, price, or the agent's own name. This guarantees no stale mention survives. "
                "Also the right tool for removing BANNED phrasing the user wants the agent to stop "
                "saying: replace the banned phrase with the corrected wording everywhere at once, "
                "then verify with find_text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "agent slug or spoken name"},
                    "find": {"type": "string", "description": "exact text to find, e.g. 'Mr. Ben'"},
                    "replace": {"type": "string", "description": "replacement text, e.g. 'Mr. Umang'"},
                    "dry_run": {"type": "boolean", "description": "true = only count/preview matches, write nothing"},
                },
                "required": ["name", "find", "replace"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_text",
            "description": (
                "Read-only search: report every place a text appears in an agent (case-insensitive) "
                "with the exact field path and a snippet. Use it (a) BEFORE a removal/ban edit to map "
                "every surface that speaks the phrase — stage goals, call_flow.call_goal, objection "
                "answers, knowledge, rules — and (b) AFTER the edits to VERIFY zero mentions remain. "
                "Never claim a phrase is gone without a 0-hit find_text result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "agent slug or spoken name"},
                    "find": {"type": "string", "description": "exact text to search for, e.g. 'twenty-minute'"},
                },
                "required": ["name", "find"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_controls",
            "description": (
                "Read-only: which settings this platform ACTUALLY has for a topic. Use it BEFORE "
                "inventing a field name, and whenever a user asks for behaviour you are not sure "
                "the platform supports. An empty result means the platform has no control for "
                "that — say so plainly and offer the nearest thing it CAN do; never write the "
                "request into a field that merely sounds related."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "what the user asked for, e.g. 'greeting', "
                                       "'hang up', 'second line', 'callback'",
                    },
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "preview_opening",
            "description": (
                "Read-only: resolve EXACTLY what the agent speaks at the start of its next call — "
                "BOTH BEATS: the opening line, AND the line the caller hears the instant they first "
                "respond (openings[lang].second_line, from call_flow.connect.intro_line or composed "
                "from identity) — per language, with the config field each comes from, by running "
                "the real call engine. Use it whenever the user asks what the agent says first, and "
                "to VERIFY any greeting edit actually changed what callers hear (an edit to a "
                "shadowed field looks applied but is silent). {contact_name} in the result is "
                "spoken as the dialed contact's name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "agent slug or spoken name"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_draft",
            "description": (
                "Record interview answers into the new-agent DRAFT (server-side, survives any "
                "context length). Call this IMMEDIATELY whenever the user gives information for "
                "a new agent — mine info dumps completely, recording their wording VERBATIM. "
                "Pass the same partial-definition shape as create_agent. Returns which topics "
                "are still open and the next question to ask. Set source='proposal' when "
                "recording defaults YOU proposed and the user accepted. The server ranks every "
                "value: content the user actually said outranks copy you wrote, so a write that "
                "would overwrite their wording is dropped and reported in notes. Write your own "
                "copy only for topics the user has not spoken on."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "changes": {"type": "object", "description": "partial agent definition to merge into the draft"},
                    "source": {"type": "string", "enum": ["user", "proposal"]},
                },
                "required": ["changes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skip_layer",
            "description": (
                "Skip ONE interview topic because the USER explicitly declined it ('use defaults', "
                "'skip that'). Quote their exact words — this is audited. The only way to unblock "
                "create_agent without answering a compulsory topic."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "layer_id": {"type": "string", "description": "topic id from the INTERVIEW STATE / create refusal"},
                    "user_words": {"type": "string", "description": "the user's exact words declining this topic"},
                },
                "required": ["layer_id", "user_words"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ingest_document",
            "description": (
                "Read a document the user attached to this chat (doc_id from the attachment note) "
                "and pull its facts (products, prices, brand, greeting…) into the new-agent draft."
            ),
            "parameters": {
                "type": "object",
                "properties": {"doc_id": {"type": "string"}},
                "required": ["doc_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_update",
            "description": (
                "Prepare an edit WITHOUT applying it: builds the candidate, validates it, and "
                "returns a human-readable diff for the user to confirm. Use for any non-trivial "
                "edit. Provide either `changes` (partial definition, deep-merged) or `ops` "
                "(surgical path operations)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "agent slug or spoken name"},
                    "changes": {"type": "object", "description": "partial definition to merge"},
                    "ops": {"type": "array", "items": {"type": "object"},
                            "description": "surgical ops: [{op: set|append|remove_at|move, path, value?, to?}]"},
                    "reason": {"type": "string", "description": "one-line reason for the change"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "enrich_agent",
            "description": ("Repair an EXISTING agent built before the "
                            "platform's stage-wiring enrichment: derives "
                            "collect/advance_when/role/pacing from its own "
                            "goals and slots, plus the modern defaults "
                            "(neutral rules pack, strict knowledge grounding, "
                            "filler caps) — fills empty fields ONLY, never "
                            "overwrites anything authored. ALWAYS run as a "
                            "dry-run first and show the user the diff; pass "
                            "confirm=true only after they approve."),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "agent slug"},
                    "confirm": {"type": "boolean",
                                "description": "true ONLY after the user has "
                                               "seen and approved the dry-run "
                                               "diff"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_update",
            "description": "Apply a previously proposed change AFTER the user confirms. Commits with concurrency protection (409 if the agent changed since the proposal).",
            "parameters": {
                "type": "object",
                "properties": {"change_id": {"type": "string"}},
                "required": ["change_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "discard_update",
            "description": "Discard a previously proposed change the user rejected.",
            "parameters": {
                "type": "object",
                "properties": {"change_id": {"type": "string"}},
                "required": ["change_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_stage",
            "description": "Surgically edit ONE call-flow stage by id (goal, turns_until_advance, role, collect, advance_when, branches, label) — never resend the whole stage list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "stage_id": {"type": "string"},
                    "fields": {"type": "object", "description": "only the stage fields to change"},
                },
                "required": ["name", "stage_id", "fields"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_stage",
            "description": "Insert one new call-flow stage (after `after_id`, or at the end). Orders are renumbered automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "stage": {"type": "object", "description": "the new stage {id, goal, ...}"},
                    "after_id": {"type": "string", "description": "existing stage id to insert after"},
                },
                "required": ["name", "stage"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_stage",
            "description": "Remove one call-flow stage by id. Orders are renumbered automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "stage_id": {"type": "string"},
                },
                "required": ["name", "stage_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reorder_stages",
            "description": "Reorder the call-flow stages. Pass the COMPLETE list of existing stage ids in the new order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "ordered_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "ordered_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_slot",
            "description": "Surgically edit ONE data-capture slot by name (question wording, type, priority, depends_on, merge_policy, confirm_readback, max_asks, ...).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "slot_name": {"type": "string"},
                    "fields": {"type": "object"},
                },
                "required": ["name", "slot_name", "fields"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_slot",
            "description": "Add one data-capture slot (a piece of information the agent must collect).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "slot": {"type": "object", "description": "the new slot {name, question: {lang: str}, ...}"},
                },
                "required": ["name", "slot"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_slot",
            "description": "Remove one data-capture slot by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "slot_name": {"type": "string"},
                },
                "required": ["name", "slot_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_revisions",
            "description": "List an agent's saved edit history (who changed it, when, why) — every change is reversible.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rollback_agent",
            "description": "Restore an agent to an earlier revision from list_revisions. Only after the user explicitly asks to undo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "rev_id": {"type": "integer"},
                },
                "required": ["name", "rev_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "activate_agent",
            "description": "Make this agent answer all inbound phone calls (writes agents/active_agent.json). Only call after the user explicitly confirms.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "agent slug"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_routing",
            "description": "Read an agent's call-routing rules (DID, caller-ID prefixes, time windows, fallback agent).",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "agent slug"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_routing",
            "description": "Write an agent's routing.yaml. Body keys: did, caller_id_prefixes, time_windows, fallback_agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "agent slug"},
                    "routing": {"type": "object", "description": "routing config JSON"},
                },
                "required": ["name", "routing"],
            },
        },
    },
]


_EDIT_TOOLS = {"get_agent", "update_agent", "update_stage", "add_stage",
               "remove_stage", "reorder_stages", "update_slot", "add_slot",
               "remove_slot", "replace_mentions", "activate_agent",
               "rollback_agent", "get_revisions"}


def _should_mine_dump(session: dict, message: str, attachments: list) -> bool:
    """A big paste is auto-mined unless this session is clearly editing an
    existing agent: the agent is already built from this interview, or any
    edit tool has been used. Attachments take the ingest_document path."""
    if len(message) < _MINE_DUMP_MIN_CHARS or attachments:
        return False
    iv = _interview_state(session)
    if iv.get("phase") == "built":
        return False
    for m in session.get("messages") or []:
        for tc in m.get("tool_calls") or []:
            if ((tc.get("function") or {}).get("name")) in _EDIT_TOOLS:
                return False
    return True


def _extracted_fields_to_changes(fields: dict) -> dict:
    """Map the document-extractor's flat field names to draft paths (the same
    shape update_draft expects). Full interview coverage — a document or
    pasted dump that scripts objections, the call flow, closings, persona,
    languages, or slots must close those topics, not get re-asked."""
    ch: dict = {}
    _id_map = ("agent_name", "company_name", "company_tagline",
               "role_description", "contact")
    for k in _id_map:
        if fields.get(k):
            ch.setdefault("identity", {})[k] = fields[k]
    kb_map = ("portfolio_prompt_text", "known_projects", "locations",
              "objection_patterns", "catalog", "match_rules", "pricing_policy")
    for k in kb_map:
        if fields.get(k):
            ch.setdefault("knowledge_base", {})[k] = fields[k]
    rb_map = ("opening_greeting", "objection_playbook", "objection_responses",
              "closing_responses")
    for k in rb_map:
        if fields.get(k):
            ch.setdefault("response_bank", {})[k] = fields[k]
    if fields.get("call_direction"):
        ch["call_direction"] = fields["call_direction"]
    if fields.get("call_goal"):
        ch.setdefault("call_flow", {})["call_goal"] = fields["call_goal"]
    if isinstance(fields.get("stages"), list) and fields["stages"]:
        ch.setdefault("call_flow", {})["stages"] = fields["stages"]
    for k in ("personality", "languages"):
        if isinstance(fields.get(k), dict) and fields[k]:
            ch[k] = fields[k]
    if isinstance(fields.get("slots"), list) and fields["slots"]:
        ch["slots"] = fields["slots"]
    for k in ("extra_always_rules", "extra_never_rules"):
        if fields.get(k):
            ch.setdefault("llm_config", {})[k] = fields[k]
    for k in ("brand", "guardrails"):
        if isinstance(fields.get(k), dict):
            ch[k] = fields[k]
    if fields.get("appointment_slots"):
        ch.setdefault("calendar", {})["mock_slots"] = fields["appointment_slots"]
    # F11: logistics-group routes — extracted policies used to have no home
    # and were silently dropped (Kaapd: WhatsApp catalogue promise, Tuesday
    # callback doctrine, Mon-Sat dispatch window, scripted English opener).
    if isinstance(fields.get("scheduling"), dict) and fields["scheduling"]:
        ch.setdefault("call_flow", {})["scheduling"] = fields["scheduling"]
    if isinstance(fields.get("callback_policy"), dict) and fields["callback_policy"]:
        cb = dict(fields["callback_policy"])
        # An extracted-but-disabled policy is dead at conversation.py's
        # enabled gate — extraction means the document wants it.
        cb.setdefault("enabled", True)
        ch.setdefault("call_flow", {})["callback_policy"] = cb
    if isinstance(fields.get("whatsapp_promise"), dict) and fields["whatsapp_promise"]:
        wp = dict(fields["whatsapp_promise"])
        wp.setdefault("enabled", True)
        ch.setdefault("follow_up", {})["whatsapp_promise"] = wp
    if isinstance(fields.get("opening_greeting_by_language"), dict) \
            and fields["opening_greeting_by_language"]:
        ch.setdefault("outbound", {})["opening_greeting"] = \
            fields["opening_greeting_by_language"]
    return ch


# ─────────────────────────────────────────────────────────────────────────────
# Input-priority regime: the USER's words outrank the model's authorship.
#
# Every draft leaf carries a provenance rank. A write may only land if it
# ranks >= what already sits at that path, so the model can fill gaps with
# its own copy (its "free power") but can NEVER overwrite something the user
# actually said. The rank is SERVER-VERIFIED, not taken on trust: the model
# declaring source='user' does not make it so — spoken content must trace
# back to the user's own text (typed or in a document they supplied).
# ─────────────────────────────────────────────────────────────────────────────

_PROV_RANK = {"authored": 0, "proposal": 1, "derived": 2, "confirmed": 3}
_VERIFY_MIN_CHARS = 25      # below this a string is structural (enum/flag/name)
_TRACE_NGRAM = 3            # phrase length: what invention cannot fake
_TRACE_MIN_OVERLAP = 0.35   # share of a value's phrases that must be the user's


def _norm_words(text: str) -> list[str]:
    return re.findall(r"\w+", str(text).lower(), flags=re.UNICODE)


def _ngrams(words: list[str], n: int = _TRACE_NGRAM) -> set[str]:
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def _user_corpus(session: dict) -> set[str]:
    """The USER's own phrases this session — their typed messages plus the
    text of any document they supplied. Phrases (not loose words): against a
    long brief nearly every common word appears somewhere, so word-overlap
    would wave through invented copy built from ordinary vocabulary (the
    'I understand you are busy…' lines of the Kaapd incident). A sentence the
    user never wrote does not share their PHRASING."""
    words: set[str] = set()
    phrases: set[str] = set()
    for m in session.get("messages") or []:
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            w = _norm_words(m["content"])
            words.update(w)
            phrases.update(_ngrams(w))
    iv = session.get("interview") or {}
    for text in iv.get("input_texts") or []:
        w = _norm_words(text)
        words.update(w)
        phrases.update(_ngrams(w))
    # One set: phrases plus \x00-tagged single words for the short-value path.
    return phrases | {f"\x00{w}" for w in words}


def _traces_to_user(value: str, corpus: set[str]) -> bool:
    """Does this spoken content come from the user's own words? Phrase-level
    evidence: the model legitimately reflows, trims and reorders what the
    user wrote (many phrases survive), but it cannot conjure the user's
    phrasing out of nothing."""
    words = _norm_words(value)
    if not words:
        return True
    if len(words) < _TRACE_NGRAM:
        return all(f"\x00{w}" in corpus for w in words)
    grams = _ngrams(words)
    hits = sum(1 for g in grams if g in corpus)
    return (hits / len(grams)) >= _TRACE_MIN_OVERLAP


def _long_strings(value) -> list[str]:
    """Every sentence-length string anywhere inside a value — including the
    goals buried in call_flow.stages[] and the answers inside
    objection_playbook[].answer, which is where invented copy hides."""
    out: list[str] = []
    if isinstance(value, str):
        if len(value.strip()) >= _VERIFY_MIN_CHARS:
            out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(_long_strings(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            out.extend(_long_strings(v))
    return out


def _verified_rank(value, corpus: set[str], claimed: str) -> str:
    """Rank one leaf. Short scalars are structure (enums, flags, names, langs)
    and keep the claimed rank — the risk is invented CONTENT, the sentences a
    real caller would hear. Those must trace to the user or they are the
    model's own authorship, whatever it claims. Half tracing is enough: the
    model legitimately translates and trims what the user gave."""
    longs = _long_strings(value)
    if not longs:
        return claimed
    traced = sum(1 for s in longs if _traces_to_user(s, corpus))
    return claimed if traced >= max(1, len(longs) // 2) else "authored"


def _rank_changes(changes: dict, corpus: set[str], claimed: str) -> dict[str, str]:
    """path -> verified rank name, for every leaf of a changes tree.

    Fails OPEN: with no user text to check against there is no evidence of
    invention, so nothing is downgraded. Verification may only demote content
    it can positively see the user never wrote."""
    out: dict[str, str] = {}
    if not corpus:
        return {p: claimed for p in _paths_of_changes(changes)}

    def _walk(node: dict, prefix: str) -> None:
        for k, v in (node or {}).items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict) and v:
                _walk(v, p)
            else:
                out[p] = _verified_rank(v, corpus, claimed)
    _walk(changes, "")
    return out


def _get_path(tree: dict, path: str):
    node = tree
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _pop_path(tree: dict, path: str) -> None:
    parts = path.split(".")
    node = tree
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return
    if isinstance(node, dict):
        node.pop(parts[-1], None)


def _set_path(tree: dict, path: str, value) -> None:
    parts = path.split(".")
    node = tree
    for part in parts[:-1]:
        nxt = node.get(part) if isinstance(node, dict) else None
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _rebase_change(chg: dict) -> tuple[dict, str]:
    """Re-seat a pending proposal on top of the CURRENT definition.

    A proposal freezes a full candidate snapshot plus the base_rev it was built
    from. Anything that writes the agent in between — including the copilot's
    OWN direct-edit tools (update_stage et al) — makes that base_rev stale, and
    every later apply fails the optimistic-concurrency check with 409. Nothing
    cleared or refreshed the pending change either, so the diff card stayed on
    screen and re-failed identically forever (seen in production: seven
    consecutive 409s on a single card, with no way out but abandoning the edit).

    Replaying only the paths the proposal claims (`expected_paths`) is what
    makes this safe: an unrelated concurrent edit is preserved instead of being
    clobbered by the frozen full-definition snapshot.

    Returns (rebased_candidate, fresh_rev). Raises ValueError naming the real
    conflict when the newer write touched one of the very paths this proposal
    edits — the only case a human genuinely has to re-decide.
    """
    slug = chg["slug"]
    current, fresh_rev = _read_full_definition(slug)
    base = chg.get("base")
    candidate = chg.get("candidate") or {}
    paths = [p for p in (chg.get("expected_paths") or []) if p]
    if not isinstance(base, dict) or not paths:
        raise ValueError(
            "This proposal was built before the agent changed and can't be "
            "replayed safely — re-propose it against the current definition.")

    conflicts = sorted(p for p in paths if _get_path(base, p) != _get_path(current, p))
    if conflicts:
        raise ValueError(
            "The agent changed after this was proposed, on the same setting(s) "
            "it edits: " + ", ".join(conflicts[:5]) +
            ". Applying it would silently revert that newer edit — re-propose "
            "against the current definition.")

    rebased = copy.deepcopy(current)
    for p in paths:
        val = _get_path(candidate, p)
        if val is None and _get_path(base, p) is not None:
            _pop_path(rebased, p)          # the proposal removed this field
        else:
            _set_path(rebased, p, copy.deepcopy(val))

    violations = _invariant_violations(rebased)
    if violations:
        raise ValueError("Refused — replaying this change would break the "
                         "agent: " + "; ".join(violations))
    AgentDefinition.from_dict(rebased)     # dry validation, no write
    return rebased, fresh_rev


def _strip_outranked(changes: dict, draft: dict, provenance: dict,
                     incoming: dict[str, str], notes: list[str],
                     note_prefix: str = "kept your own input over") -> list[str]:
    """THE enforcement point: drop every incoming leaf that would overwrite a
    higher-ranked value already in the draft. Returns the dropped paths."""
    dropped: list[str] = []
    for path, in_rank in sorted(incoming.items()):
        existing = _get_path(draft, path)
        if existing in (None, "", [], {}):
            continue                      # a gap — anyone may fill it
        held = (provenance or {}).get(path)
        if held is None:
            continue                      # unprovenanced (defaults) — replaceable
        if _PROV_RANK.get(in_rank, 0) < _PROV_RANK.get(held, 0):
            _pop_path(changes, path)
            dropped.append(path)
    if dropped:
        notes.append(f"{note_prefix} new copy for: " + ", ".join(dropped[:8])
                     + (f" (+{len(dropped) - 8} more)" if len(dropped) > 8 else ""))
    return dropped


def _strip_confirmed_paths(changes: dict, provenance: dict, notes: list[str]) -> None:
    """Derived (document/dump) data must never overwrite what the user
    confirmed in the interview — drop those leaves in place."""
    dropped = []
    for path in [p for p, v in (provenance or {}).items() if v == "confirmed"]:
        parts = path.split(".")
        node = changes
        for p in parts[:-1]:
            node = node.get(p) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict) and parts[-1] in node:
            node.pop(parts[-1])
            dropped.append(path)
    if dropped:
        notes.append("kept your confirmed answers over the document for: "
                     + ", ".join(sorted(dropped)[:8]))


def _merge_extracted_into_draft(session: dict, fields: dict) -> tuple[dict, list[str], dict, list[str]]:
    """Shared by ingest_document and the pasted-dump auto-miner: map extracted
    fields to draft paths, apply protection rules, merge, record provenance.
    Returns (draft_changes, notes, progress, layers_closed)."""
    notes: list[str] = []
    iv = _interview_state(session)
    before = interview_progress(iv["draft"], iv["skipped"])
    done_before = {l["id"] for l in before["layers"] if l["done"] or l["skipped"]}
    draft_changes = _extracted_fields_to_changes(fields)
    # F11 backstop (LLM-proof): a document that yielded known_projects but no
    # catalog still produces catalog items — the ONE knowledge field the
    # prompt renderer actually shows the LLM (known_projects is never
    # rendered; Kaapd shipped catalog:[] and the model improvised sarees).
    # DRAFT-ONLY: never applied to existing agents; only when catalog is
    # empty in both the extraction and the draft.
    _kb_ch = draft_changes.get("knowledge_base") or {}
    _draft_kb = (iv["draft"].get("knowledge_base")
                 if isinstance(iv["draft"].get("knowledge_base"), dict)
                 else {}) or {}
    if (isinstance(_kb_ch.get("known_projects"), dict)
            and _kb_ch["known_projects"]
            and not _kb_ch.get("catalog")
            and not _draft_kb.get("catalog")):
        _synth = []
        for key, val in _kb_ch["known_projects"].items():
            canonical = (str(val[0]) if isinstance(val, (list, tuple)) and val
                         else str(key).title())
            loc = (str(val[1]) if isinstance(val, (list, tuple))
                   and len(val) > 1 and str(val[1]) != "Standard" else "")
            item = {"id": re.sub(r"[^a-z0-9]+", "-", str(key).lower()).strip("-"),
                    "name": canonical, "aliases": [str(key)]}
            if loc:
                item["location"] = loc
            _synth.append(item)
        if _synth:
            draft_changes.setdefault("knowledge_base", {})["catalog"] = _synth
            notes.append(
                f"catalog synthesized from the document's product families "
                f"({len(_synth)} item(s)) — names only, nothing invented")
    _strip_confirmed_paths(draft_changes, iv.get("provenance") or {}, notes)
    # A document never replaces a call flow the interview already built.
    cur_stages = ((iv["draft"].get("call_flow") or {}).get("stages")
                  if isinstance(iv["draft"].get("call_flow"), dict) else None) or []
    new_cf = draft_changes.get("call_flow")
    if isinstance(new_cf, dict) and "stages" in new_cf and len(cur_stages) >= 2:
        new_cf.pop("stages")
        notes.append("kept the existing call flow — the extracted stages were "
                     "not applied (edit stages explicitly to change the flow)")
    _normalize_definition(draft_changes, notes)
    def _drop_empty(node: dict) -> None:
        # extractors emit hollow shells like {"en": [], "hi": []} — prune
        # empty lists AND dicts so they never land in the draft
        for k in list(node.keys()):
            v = node[k]
            if isinstance(v, dict):
                _drop_empty(v)
            if isinstance(v, (dict, list)) and not v:
                node.pop(k)
    _drop_empty(draft_changes)
    # Input priority: document content is the USER's input (rank 'derived'),
    # so it beats anything the model authored on its own, but never beats
    # what the user typed directly ('confirmed', stripped above).
    ranks = {p: "derived" for p in _paths_of_changes(draft_changes)}
    _strip_outranked(draft_changes, iv["draft"], iv.get("provenance") or {},
                     ranks, notes,
                     note_prefix="kept your typed answers over the document's")
    _drop_empty(draft_changes)
    _deep_merge(iv["draft"], draft_changes)
    for p in sorted(_paths_of_changes(draft_changes)):
        prev = iv["provenance"].get(p)
        if _PROV_RANK.get("derived", 2) >= _PROV_RANK.get(prev, -1):
            iv["provenance"][p] = "derived"
    iv["phase"] = "interviewing"
    progress = interview_progress(iv["draft"], iv["skipped"])
    layers_closed = [l["title"] for l in progress["layers"]
                     if (l["done"] or l["skipped"]) and l["id"] not in done_before]
    return draft_changes, notes, progress, layers_closed


async def _execute_tool(tool: str, args: dict, guard: dict | None = None) -> tuple[Any, str, bool, bool]:
    """Run one tool. Returns (result, summary, ok, mutated_agents).

    `guard` carries per-turn safety state: {"user_turns": int, "created_this_turn": bool}.
    Two rules are enforced SERVER-SIDE (the LLM cannot bypass them):
      * create_agent is refused on the session's very first user message —
        the copilot must interview before building.
      * activate_agent is refused in the same turn as a successful
        create_agent — activation needs the user's explicit confirmation.
    """
    guard = guard or {}
    owner = builder._ctx_owner()

    # Tenant boundary for every tool that names an EXISTING agent: the LLM
    # cannot be trusted to respect ownership, so enforce it here regardless of
    # what slug it passes. create_agent is exempted (it claims a new slug).
    _named = str(args.get("name") or "").strip()
    if _named and tool != "create_agent":
        builder._assert_slug_access(_named, owner)

    if tool == "list_agents":
        res = await builder.list_agents()
        names = [a.get("name") for a in res.get("agents", [])]
        return res, f"Checked your {len(names)} agents (live: {res.get('active') or 'none'})", True, False

    if tool == "get_agent":
        name = str(args["name"]).strip()
        res = await builder.get_agent(name)
        # Compact spoken-surfaces digest FIRST in the payload: historical
        # tool results are truncated to 900 chars in _context_window, and a
        # full definition dump buries call_goal ~6KB deep — which is how a
        # stale "twenty-minute demo" mission survived a ban edit unseen.
        cf = res.get("call_flow") or {}
        # Engine-resolved opening: what the NEXT call actually speaks first,
        # and from which field. The raw response_bank value alone misled a
        # real edit on 2026-08-01 — on outbound agents it can be shadowed by
        # outbound.opening_greeting. Advisory: never fails the read.
        _open_resolved: dict = {}
        _open_note = ""
        try:
            _sr = await asyncio.to_thread(
                _surfaces.resolve_spoken_surfaces, res)
            _dl = _sr["default_language"]
            _open_resolved = {
                "direction": _sr["direction"],
                "line": (_sr["openings"].get(_dl) or {}).get("line", "")[:160],
                "source": (_sr["openings"].get(_dl) or {}).get("source", ""),
                "second_line": (_sr["openings"].get(_dl) or {}).get(
                    "second_line", "")[:160],
                "second_source": (_sr["openings"].get(_dl) or {}).get(
                    "second_source", ""),
            }
            _open_note = "; ".join(
                f"{s['path']}: {s['note']}" for s in _sr["shadowed"])[:300]
        except Exception as exc:  # noqa: BLE001 — advisory only
            logger.warning("[copilot] get_agent opening resolve failed: %s", exc)
        res = {"spoken_surfaces": {
            "call_goal": str(cf.get("call_goal") or "")[:200],
            "opening_greeting": str((res.get("response_bank") or {})
                                    .get("opening_greeting") or "")[:120],
            **({"opening_line_next_call": _open_resolved}
               if _open_resolved else {}),
            **({"opening_note": _open_note} if _open_note else {}),
            # Per-language speaking speed (0.5–2.0, 1.0 natural) — surfaced so
            # a "talks too fast/slow" turn can quote the current value without
            # digging through the truncated full dump.
            "pace": {lg: (v or {}).get("pace")
                     for lg, v in ((res.get("languages") or {})
                                   .get("tts_voices") or {}).items()},
            "stages": [{"id": s.get("id"),
                        "goal_head": str(s.get("goal") or "")[:60]}
                       for s in (cf.get("stages") or [])
                       if isinstance(s, dict)],
            "scheduling": cf.get("scheduling") or {"style": "exact_n (default)"},
            "mock_slots_count": len(((res.get("calendar") or {})
                                     .get("mock_slots")) or []),
            "never_rules_count": len(((res.get("guardrails") or {})
                                      .get("never_rules")) or []),
        }, **res}
        return res, f"Looked at {_agent_display_name(name)}", True, False

    if tool == "validate_agent":
        notes: list[str] = []
        definition = _coerce_obj(args.get("definition") or {})
        definition.pop("name", None)
        _normalize_definition(definition, notes)
        merged = _default_definition("preview_agent", definition)
        _normalize_definition(_deep_merge(merged, definition))
        _enforce_invariants(merged, notes)
        agent = AgentDefinition.from_dict(merged)
        res = {
            "valid": True,
            "agent_id": agent.agent_id,
            "stages": [s.id for s in agent.call_flow.stages],
            "languages": agent.languages.supported,
            "notes": notes,
        }
        return res, "Checked the draft — it's valid", True, False

    if tool == "create_agent":
        if guard.get("user_turns", 99) < 2:
            raise ValueError(
                "Refused: this is the user's first message — interview them first. "
                "Ask ONE short clarifying question (persona, voice, greeting, or what "
                "the agent must know), and only create after they've answered and agreed."
            )
        # Server-verified create consent (2026-07-23 audit): a brochure paste
        # triggered an unsolicited create_agent under a model-chosen slug.
        # The LATEST user message must actually instruct a build. Long
        # messages (pastes) additionally need the verb near an agent-ish
        # noun, so "we build smiles since 1998" in a document can't count.
        _umsg = str(guard.get("user_message") or "")
        _verb = re.compile(
            r"\b(create|build|make|ship|launch|deploy|generate|set\s*it\s*up|"
            r"go\s*ahead)\b|बना|तैयार\s*कर|બનાવ", re.I)
        _near = re.compile(
            r"(create|build|make|ship|launch|deploy|generate)\W{0,80}"
            r"(agent|bot|receptionist|assistant|it|this|one|एजेंट)", re.I | re.S)
        if not (_verb.search(_umsg) and (len(_umsg) < 1200 or _near.search(_umsg))):
            raise ValueError(
                "Held until the user says yes — say 'build it' to create this "
                "agent. (Copilot: summarize the plan in 3-5 short lines and "
                "ask 'shall I build it?' — create only after they agree.)"
            )
        # v2 hardened create gate (plan §6.2): every compulsory interview
        # layer must be complete or EXPLICITLY skipped. Dump-and-done is
        # mechanically impossible; "just build it" = per-layer skip_layer.
        _session = guard.get("session") or {}
        _iv = _interview_state(_session)
        _progress = interview_progress(_iv.get("draft") or {}, _iv.get("skipped") or [])
        if not _progress["complete"]:
            raise ValueError(
                "Refused: the interview is not finished — these layers are open: "
                + ", ".join(_progress["open_layers"])
                + f". Ask the next question now: {_progress['next_question']!r}. "
                "If the user explicitly said to skip a topic, call skip_layer with "
                "their words; only then can the agent be created."
            )
        name = str(args["name"]).strip().lower()
        notes: list[str] = []
        definition = _coerce_obj(args.get("definition") or {})
        definition.pop("name", None)
        for k in _SERVER_MANAGED_KEYS:  # audit fields are server-stamped only
            definition.pop(k, None)
        _normalize_definition(definition, notes)
        if not _SLUG_RE.match(name):
            raise ValueError("slug must be lowercase letters/digits with _ or - (2-61 chars)")
        # One create yields a PAIR: <base>-inbound and <base>-outbound. The two
        # directions need different prompts, and binding a number now asks for
        # an agent per direction — so both must exist from the start.
        _family = builder.agent_family(name)
        _twins = {d: builder.twin_slug(_family, d) for d in builder.DIRECTIONS}
        for _slug in _twins.values():
            if (_AGENTS_DIR / _slug).exists():
                # Name the twin whose direction matches what is being built:
                # "viraj-inbound already exists — use update_agent" sent the
                # model to the INBOUND twin with an outbound script (§60).
                _want = str((_iv.get("draft") or {}).get("call_direction")
                            or definition.get("call_direction") or "inbound").lower()
                if _want not in builder.DIRECTIONS:
                    _want = "inbound"
                raise ValueError(
                    f"Agent family '{_family}' already exists "
                    f"({_twins['inbound']} + {_twins['outbound']}) — nothing to "
                    f"create. For {_want} calls edit '{_twins[_want]}' with "
                    "update_agent; each twin's call type is fixed by its name, "
                    "so never send call_direction to switch one.")
            _existing_owner = builder._slug_owner(_slug)
            if _existing_owner and _existing_owner != owner and not builder._ctx_is_admin():
                raise ValueError(f"Agent '{_slug}' already exists")
        # v2: create CONSUMES the draft — everything captured during the
        # interview lands in the YAML even if the final tool call is thin and
        # the chat history was trimmed (fixes the 48-message context loss).
        _draft = copy.deepcopy(_iv.get("draft") or {})
        _draft.pop("_ingested_docs", None)
        # The interview draft is AUTHORITATIVE over the model's definition
        # argument. Incident 2026-07-15 (pramukh_agent): the fallback model
        # passed a self-authored definition (fabricated projects, brand facts,
        # phone numbers, call_direction=inbound) and the old merge order let
        # it clobber every confirmed interview answer. Sections the interview
        # touched are stripped from the definition entirely; elsewhere the
        # definition only fills gaps, with the draft merged last.
        _prov_sections = {p.split(".")[0] for p in (_iv.get("provenance") or {})}
        _overridden = sorted(k for k in list(definition.keys()) if k in _prov_sections)
        for k in _overridden:
            definition.pop(k)
        if _overridden:
            notes.append(
                "definition sections already answered in the interview were "
                "ignored in favor of the draft: " + ", ".join(_overridden))
        merged = _default_definition(name, _deep_merge(copy.deepcopy(definition), copy.deepcopy(_draft)))
        _normalize_definition(_deep_merge(merged, definition))
        _normalize_definition(_deep_merge(merged, _draft))
        _enforce_invariants(merged, notes)
        _enrich_new_agent(merged, notes)
        # F9: create COERCES an invalid max_tokens (the update path REJECTS —
        # checks not mutations there; here nothing exists yet to protect).
        _lc = merged.get("llm_config")
        if isinstance(_lc, dict) and "max_tokens" in _lc:
            try:
                _mt = int(_lc["max_tokens"])
            except (TypeError, ValueError):
                _mt = -1
            if not (80 <= _mt <= 512):
                notes.append(f"llm_config.max_tokens {_lc['max_tokens']!r} "
                             "was invalid — set to 160")
                _lc["max_tokens"] = 160
            else:
                _lc["max_tokens"] = _mt
        agent = AgentDefinition.from_dict(merged)          # validate before any write
        # F7 (Incident B): render every prompt surface the way a live call
        # would — a definition that would crash or misrender live is refused
        # HERE, with the failing field family named, instead of degrading
        # three real calls to "Sorry, could you repeat that?".
        _bc_mode = os.environ.get("ENGINE_BOOT_CHECK", "strict").lower()
        if _bc_mode != "off":
            _bc = await asyncio.to_thread(engine_boot_check, agent)
            if _bc["errors"]:
                raise ValueError(
                    "Engine check failed — this agent would crash on a live "
                    "call: " + "; ".join(_bc["errors"][:5])
                    + ". Fix the offending fields and retry create_agent.")
            notes.extend(f"engine check: {w}" for w in _bc["warnings"])
        # The interviewed direction is the one the user gets dropped into; the
        # counterpart is the same definition with call_direction flipped, ready
        # to be tailored. The boot check above already renders both directions.
        _primary_dir = str(merged.get("call_direction") or "inbound")
        if _primary_dir not in builder.DIRECTIONS:
            _primary_dir = "inbound"
        import shutil as _sh
        _created: list[str] = []
        _agents = {}
        try:
            for _d, _slug in _twins.items():
                _spec = {**merged, "call_direction": _d}
                _adef = agent if _d == _primary_dir else AgentDefinition.from_dict(_spec)
                _dir = _AGENTS_DIR / _slug
                _dir.mkdir(parents=True, exist_ok=True)
                if _d != _primary_dir:
                    _adef.call_direction = _d
                _adef.to_yaml(_dir / "agent.yaml")
                agent_registry.put(_slug, _adef)
                _agents[_d] = _adef
                _created.append(_slug)
                builder._claim_slug(_slug, owner, _adef.identity.agent_name)
        except Exception as exc:
            if not builder._ctx_is_admin():
                # Half a pair is worse than none — a number could bind to the
                # survivor and run wrong-direction prompts.
                for _slug in _created:
                    _sh.rmtree(_AGENTS_DIR / _slug, ignore_errors=True)
                    agent_registry.invalidate(_slug)
                raise ValueError(f"could not create the agent pair: {exc}")
        name = _twins[_primary_dir]
        agent = _agents.get(_primary_dir, agent)
        logger.info("[copilot] created agent pair '%s' (%s + %s) owner=%s (%d input repairs)",
                    _family, _twins["inbound"], _twins["outbound"], owner, len(notes))
        _iv["phase"] = "built"
        _iv["built_slug"] = name
        res = {
            "status": "created", "name": name, "agent_id": agent.agent_id,
            "family": _family, "twins": _twins,
            "notes": notes,
            "quality": _safe_quality(
                merged, user_message=str(guard.get("user_message") or "")),
            "provenance_confirmed": sum(1 for v in (_iv.get("provenance") or {}).values()
                                        if v == "confirmed"),
        }
        # What the caller will actually hear first — resolved by the engine,
        # so the build summary can quote the real opening, not a guess.
        try:
            _sr = await asyncio.to_thread(
                _surfaces.resolve_spoken_surfaces, agent)
            res["hearable"] = {
                "direction": _sr["direction"],
                "opening_next_call": {
                    lang: o["line"] for lang, o in _sr["openings"].items()},
            }
        except Exception as exc:  # noqa: BLE001 — advisory only
            logger.warning("[copilot] create hearable readback failed: %s", exc)
        return res, f"Created {agent.identity.agent_name or name}", True, True

    if tool == "enrich_agent":
        name = str(args["name"]).strip()
        confirm = bool(args.get("confirm"))
        if name in ("pramukh", "pramukh_agent"):
            raise ValueError(
                "Refused: this is a golden-pinned production agent — its "
                "pacing and rules are intentional. Use normal update_agent "
                "edits the user asks for, never bulk enrichment.")
        current, base_rev = _read_full_definition(name)
        candidate = copy.deepcopy(current)
        notes: list[str] = []
        _enrich_new_agent(candidate, notes)
        # Real-estate-looking agents keep the real-estate pack — the pack
        # switch is only for the industry-mismatch class (fabric agent
        # carrying site-visit rules).
        _kb = candidate.get("knowledge_base") or {}
        _kb_text = " ".join([
            str(_kb.get("portfolio_prompt_text") or ""),
            str(_kb.get("portfolio_section_label") or ""),
            " ".join((_kb.get("qa_field_signals") or {}).keys()
                     if isinstance(_kb.get("qa_field_signals"), dict) else []),
        ]).lower()
        _looks_re = any(m in _kb_text for m in
                        ("bhk", "real estate", "property", "site visit",
                         "project portfolio"))
        # NOTE: _read_full_definition returns the CANONICAL form — schema
        # defaults are materialized as explicit keys — so every check below
        # compares against the old-default VALUE, never key absence.
        gr = candidate.setdefault("guardrails", {})
        if isinstance(gr, dict):
            if (str(gr.get("builtin_rules_pack") or "real_estate")
                    == "real_estate" and not _looks_re):
                gr["builtin_rules_pack"] = "generic"
                notes.append("industry-neutral conversation rules (this "
                             "agent was carrying the built-in real-estate "
                             "rule pack)")
            honesty = gr.setdefault("honesty", {})
            if isinstance(honesty, dict) and \
                    not str(honesty.get("knowledge_grounding") or ""):
                honesty["knowledge_grounding"] = "strict"
                notes.append("strict knowledge grounding — the agent will "
                             "deflect instead of inventing products/prices")
        lc = candidate.setdefault("llm_config", {})
        if isinstance(lc, dict):
            if lc.get("filler_timeout_s") == 0.6:  # the old skeleton literal
                lc["filler_timeout_s"] = 1.2
                notes.append("filler timeout 0.6s→1.2s (0.6 sat below the "
                             "live model's first-word latency — a filler "
                             "played on every turn)")
            if not lc.get("filler_min_gap_turns"):
                lc["filler_min_gap_turns"] = 2
            if not lc.get("filler_max_per_call"):
                lc["filler_max_per_call"] = 3
            if not lc.get("filler_ack_extended"):
                lc["filler_ack_extended"] = True
        langs = candidate.setdefault("languages", {})
        if isinstance(langs, dict) and \
                not str(langs.get("closing_language_policy") or ""):
            langs["closing_language_policy"] = "dominant"
            notes.append("closing line follows the call's dominant language")
        if isinstance(_kb, dict) and _kb.get("objection_patterns") \
                and str(_kb.get("objection_patterns_mode")
                        or "replace") == "replace":
            _kb["objection_patterns_mode"] = "extend"
            notes.append("custom objection phrases now EXTEND the universal "
                         "set instead of replacing it")
        # Empty catalog + populated known_projects: synthesize the catalog
        # items the prompt renderer actually shows (names only — nothing
        # invented; same backstop the document-ingest path applies).
        if isinstance(_kb, dict) and not _kb.get("catalog") \
                and isinstance(_kb.get("known_projects"), dict) \
                and _kb["known_projects"]:
            _synth = []
            for key, val in _kb["known_projects"].items():
                canonical = (str(val[0]) if isinstance(val, (list, tuple))
                             and val else str(key).title())
                loc = (str(val[1]) if isinstance(val, (list, tuple))
                       and len(val) > 1 and str(val[1]) != "Standard" else "")
                item = {"id": re.sub(r"[^a-z0-9]+", "-",
                                     str(key).lower()).strip("-"),
                        "name": canonical, "aliases": [str(key)]}
                if loc:
                    item["location"] = loc
                _synth.append(item)
            _kb["catalog"] = _synth
            notes.append(f"catalog built from its product families "
                         f"({len(_synth)} item(s)) — names only, nothing "
                         "invented")
        diff = [d for d in _compute_diff(current, candidate)
                if d["path"] not in ("revision", "updated_at", "updated_by")]
        if not diff:
            return ({"status": "no_changes", "name": name},
                    f"{_agent_display_name(name)} already has full wiring — "
                    "nothing to repair", True, False)
        # The repaired candidate must itself pass the engine boot-check.
        _bc = await asyncio.to_thread(
            engine_boot_check, AgentDefinition.from_dict(candidate))
        if _bc["errors"]:
            raise ValueError("Enrichment produced a definition that fails "
                             "the engine check (not applied): "
                             + "; ".join(_bc["errors"][:3]))
        if not confirm:
            return ({"status": "dry_run", "name": name,
                     "diff": _render_diff(diff), "notes": notes,
                     "changes": len(diff)},
                    f"Repair preview for {_agent_display_name(name)} — "
                    f"{len(diff)} change(s), nothing applied yet", True, False)
        result = agent_store.write_agent(
            name, candidate, actor=_copilot_actor(), base_rev=base_rev,
            reason="enrich_agent: stage wiring + modern defaults repair",
            owner=builder._ctx_owner(), is_admin=builder._ctx_is_admin(),
            agents_dir=_AGENTS_DIR)
        logger.info("[copilot] enriched agent '%s' rev=%s (%d changes)",
                    name, result["revision"], len(diff))
        return ({"status": "enriched", "name": name,
                 "diff": _render_diff(diff), "notes": notes,
                 "revision": result["revision"], "effective": "next_call"},
                f"Repaired {_agent_display_name(name)} — "
                f"{len(diff)} change(s), effective next call", True, True)

    if tool == "update_agent":
        name = str(args["name"]).strip()
        notes: list[str] = []
        changes = _coerce_obj(args.get("changes") or {})
        changes.pop("name", None)
        for k in _SERVER_MANAGED_KEYS:  # audit fields are server-stamped only
            changes.pop(k, None)
        _normalize_definition(changes, notes)
        # Sections that filtering emptied out carry no change — strip them so
        # a "nothing real left" update fails loudly instead of reporting
        # success while writing nothing.
        for k in [k for k, v in changes.items() if isinstance(v, dict) and not v]:
            changes.pop(k)
        if not changes:
            raise ValueError(
                "None of those changes map to real settings — nothing was saved. "
                + " ".join(notes)
            )
        _block_dead_paths(_paths_of_changes(changes), notes)
        yaml_path = _AGENTS_DIR / name / "agent.yaml"
        if not yaml_path.exists():
            raise ValueError(f"Agent '{name}' not found")
        # GET current full definition → deep-merge partial changes → check →
        # write through the store (§5.1). §5.4: normalization ran on the
        # CHANGES subtree only; whole-doc invariants are CHECKS that fail the
        # update (silent whole-doc mutation would read as collateral to the
        # store's scoped-diff proof).
        current, base_rev = _read_full_definition(name)
        merged = _deep_merge(copy.deepcopy(current), changes)
        violations = _invariant_violations(merged)
        if violations:
            raise ValueError("Refused — this change would break the agent: "
                             + "; ".join(violations))
        # F7: engine boot-check on the merged result. Default 'warn' for
        # UPDATE (one burn-in cycle — a legacy agent with a pre-existing
        # latent crash must not be edit-locked); flip to strict via env.
        _bc_mode = os.environ.get(
            "ENGINE_BOOT_CHECK_UPDATE",
            os.environ.get("ENGINE_BOOT_CHECK", "warn")).lower()
        if _bc_mode != "off":
            try:
                _bc = await asyncio.to_thread(
                    engine_boot_check, AgentDefinition.from_dict(merged))
            except Exception as exc:  # from_dict itself failing IS the signal
                _bc = {"errors": [f"definition no longer parses: {exc}"],
                       "warnings": []}
            if _bc["errors"]:
                _msg = ("Engine check failed — this update would crash live "
                        "calls: " + "; ".join(_bc["errors"][:5]))
                if _bc_mode == "strict":
                    raise ValueError(_msg)
                logger.error("[copilot] %s (warn mode — write allowed)", _msg)
                notes.append("ENGINE CHECK (advisory): " + _msg)
        expected = _paths_of_changes(changes)
        # No-op guard. `write_agent(expected_paths=…)` proves nothing changed
        # OUTSIDE the scope; it has never proved anything changed INSIDE it.
        # So a write naming a setting the schema drops diffs to nothing, bumps
        # the revision, and comes back "updated" — the user is told it landed
        # and finds out on a call that it did not. `current` is already
        # canonical (_read_full_definition round-trips through
        # from_dict→asdict), so this comparison is exact and safe to enforce.
        try:
            _canon = dataclasses.asdict(AgentDefinition.from_dict(merged))
            _eff = {d["path"] for d in _compute_diff(current, _canon)} - {
                "revision", "updated_at", "updated_by"}
        except Exception:  # noqa: BLE001 — never let the guard break a write
            _eff = {"__unknown__"}
        if not _eff:
            raise ValueError(
                "Refused — this is already the agent's current state, or it "
                "names settings this platform does not have. Nothing was "
                "saved and nothing on a call would sound different. Tell the "
                "user plainly what the platform can and cannot do instead of "
                "writing their request into a nearby field."
                + (" " + " ".join(notes) if notes else ""))
        result = agent_store.write_agent(
            name, merged, actor=_copilot_actor(), base_rev=base_rev,
            reason="copilot update: " + ", ".join(sorted(changes.keys()))[:180],
            owner=builder._ctx_owner(), is_admin=builder._ctx_is_admin(),
            expected_paths=expected, agents_dir=_AGENTS_DIR)
        logger.info("[copilot] updated agent '%s' (%s) rev=%s",
                    name, ", ".join(changes.keys()), result["revision"])
        res = {
            "status": "updated", "name": name, "changed_sections": sorted(changes.keys()),
            "notes": notes,
            "quality": _safe_quality(
                merged, include_wiring_lints=False,
                user_message=str(guard.get("user_message") or ""),
                touched_stages=_changed_stage_ids(current, merged)),
            "revision": result["revision"], "effective": "next_call",
        }
        res.update(await _fidelity_extras(merged, expected))
        _summary = (f"Updated {_agent_display_name(name)} — "
                    f"{_friendly_sections(sorted(changes.keys()))}")
        # One request, both directions. A twin edit used to cost four of the
        # eight steps (get ×2, update ×2) before any verification could run,
        # which is how "change it in both scripts" turns became the ones that
        # hit the cap. The mirror is BEST-EFFORT: the primary write is
        # already committed, so a twin failure is reported, never raised —
        # raising here would turn a half-landed edit into a reported failure.
        if args.get("both_directions"):
            res["twin_write"] = await _mirror_to_twin(name, changes, expected)
            if res["twin_write"].get("applied"):
                _summary += " (and its opposite-direction version)"
        else:
            _twin = _twin_note(name, expected)
            if _twin:
                res["twin"] = _twin
        return res, _summary, True, True

    if tool == "replace_mentions":
        name = str(args["name"]).strip()
        find = str(args.get("find") or "")
        replacement = str(args.get("replace") or "")
        dry_run = bool(args.get("dry_run", False))
        if not find.strip():
            raise ValueError("'find' text is required")
        yaml_path = _AGENTS_DIR / name / "agent.yaml"
        if not yaml_path.exists():
            raise ValueError(f"Agent '{name}' not found")
        current, base_rev = _read_full_definition(name)
        pattern = re.compile(re.escape(find), re.IGNORECASE)
        # agent_id/version are internal identifiers — a persona rename must
        # never mutate them (they key the registry and document lookups).
        # revision/updated_* are server-managed.
        work = copy.deepcopy(current)
        preserved = {k: work.pop(k)
                     for k in ("agent_id", "version", "revision",
                               "updated_at", "updated_by") if k in work}
        replaced_tree, count = _replace_in_tree(work, pattern, replacement)
        replaced_tree.update(preserved)
        if count == 0:
            res = {"status": "no_matches", "name": name, "find": find, "replaced": 0}
            return res, f"'{find}' does not appear anywhere in {_agent_display_name(name)}", True, False
        match_paths = [
            d["path"] for d in _compute_diff(current, replaced_tree)
            if d["path"] not in ("revision", "updated_at", "updated_by")
        ]
        if dry_run:
            res = {"status": "dry_run", "name": name, "find": find,
                   "replace": replacement, "would_replace": count,
                   "paths": match_paths[:40]}
            return res, (f"'{find}' appears in {count} places in "
                         f"{_agent_display_name(name)} — not applied yet"), True, False
        AgentDefinition.from_dict(replaced_tree)   # validate before any write
        result = agent_store.write_agent(
            name, replaced_tree, actor=_copilot_actor(), base_rev=base_rev,
            reason=f"replace '{find[:60]}' -> '{replacement[:60]}'",
            owner=builder._ctx_owner(), is_admin=builder._ctx_is_admin(),
            agents_dir=_AGENTS_DIR)
        logger.info("[copilot] replace_mentions '%s': %r -> %r (%d)", name, find, replacement, count)
        res = {"status": "replaced", "name": name, "find": find,
               "replace": replacement, "replaced": count,
               "revision": result["revision"], "effective": "next_call"}
        res.update(await _fidelity_extras(replaced_tree, match_paths))
        return res, f"Replaced '{find}' with '{replacement}' in {count} places in {_agent_display_name(name)}", True, True

    if tool == "find_text":
        name = str(args["name"]).strip()
        find = str(args.get("find") or "")
        if not find.strip():
            raise ValueError("'find' text is required")
        current, _base = _read_full_definition(name)
        for k in ("agent_id", "version", "revision", "updated_at", "updated_by"):
            current.pop(k, None)
        hits = _find_in_tree(current, re.compile(re.escape(find), re.IGNORECASE))
        res = {"status": "ok", "name": name, "find": find,
               "matches": len(hits),
               "hits": [{"path": p, "snippet": s} for p, s in hits[:40]]}
        return res, (f"'{find}' appears in {len(hits)} place(s) in {_agent_display_name(name)}"
                     if hits else
                     f"'{find}' does not appear anywhere in {_agent_display_name(name)}"), True, False

    if tool == "list_controls":
        topic = str(args.get("topic") or "").strip()
        if not topic:
            raise ValueError("'topic' is required")
        hits = _controls.nearest_controls(topic, k=8)
        res = {"status": "ok", "topic": topic, "controls": hits,
               "note": ("these are the real settings closest to that topic. "
                        "If none of them does what the user asked, the "
                        "platform cannot do it — say so plainly and offer "
                        "the nearest thing it can; do NOT write the request "
                        "into a field that merely sounds related.")}
        return res, (f"Checked what the platform can control for “{topic[:40]}”"
                     if hits else
                     f"The platform has no setting for “{topic[:40]}”"), True, False

    if tool == "preview_opening":
        name = str(args["name"]).strip()
        current, _base = _read_full_definition(name)
        sr = await asyncio.to_thread(_surfaces.resolve_spoken_surfaces, current)
        res = {"status": "ok", "name": name, **sr}
        _o = sr["openings"].get(sr["default_language"]) or {}
        _line = _o.get("line", "")
        _second = _o.get("second_line", "")
        return res, (f"Previewed {_agent_display_name(name)}'s opening — the next "
                     f"{sr['direction']} call starts with: “{_line[:90]}”"
                     + (f", then answers the caller with: “{_second[:90]}”"
                        if _second else "")), True, False

    if tool == "activate_agent":
        if guard.get("created_this_turn"):
            raise ValueError(
                "Refused: never activate in the same turn as creating an agent. "
                "Tell the user the agent was created and ASK whether to activate it "
                "(activation answers real inbound calls); activate only after their explicit yes."
            )
        if not builder._ctx_is_admin():
            raise ValueError(
                "Activation is platform-admin only. To route calls to this agent, "
                "bind your phone number to it in the Telephony panel.")
        name = str(args["name"]).strip()
        res = await builder.activate_agent(name)
        return res, f"{_agent_display_name(name)} is now answering calls", True, True

    if tool == "get_routing":
        name = str(args["name"]).strip()
        res = await builder.get_routing(name)
        return res, f"Checked call routing for {_agent_display_name(name)}", True, False

    if tool == "set_routing":
        name = str(args["name"]).strip()
        routing = _coerce_obj(args.get("routing") or {})
        if not (_AGENTS_DIR / name).exists():
            raise ValueError(f"Agent '{name}' not found")
        routing_path = _AGENTS_DIR / name / "routing.yaml"
        with open(routing_path, "w", encoding="utf-8") as f:
            yaml.dump(routing, f, allow_unicode=True, default_flow_style=False)
        logger.info("[copilot] updated routing for agent '%s'", name)
        return {"status": "updated"}, f"Updated call routing for {_agent_display_name(name)}", True, True

    # ── v2 interview engine (plan §6.2) ──────────────────────────────────────

    if tool == "update_draft":
        session = guard.get("session")
        if session is None:
            raise ValueError("no session for the draft")
        iv = _interview_state(session)
        if iv.get("phase") == "built":
            # Post-create, the draft is dead: writes here changed NOTHING on
            # the live agent while reporting "Noted" (incident 2026-07-15:
            # the user's 'make it outbound' and 'voice shubh' requests were
            # swallowed by the dead draft after pramukh_agent was created).
            _slug = str(iv.get("built_slug") or "").strip()
            _target = f"'{_slug}'" if _slug else "the created agent"
            raise ValueError(
                f"The agent {_target} was ALREADY CREATED from this interview — "
                "the draft is no longer live, and editing it changes NOTHING on "
                "the real agent. Apply the user's change NOW with "
                f"update_agent(name={_target}, changes={{...}}) — or "
                "update_stage/add_stage/remove_stage/reorder_stages/update_slot "
                "for stage and slot edits. Do not tell the user it was updated "
                "until one of THOSE tools succeeds. To build a different agent, "
                "start a new chat.")
        changes = _coerce_obj(args.get("changes") or {})
        # The model's claim is only a HINT — _rank_changes verifies spoken
        # content against the user's own words before it earns 'confirmed'.
        claimed = "proposal" if str(args.get("source") or "") == "proposal" else "confirmed"
        notes: list[str] = []
        _normalize_definition(changes, notes)

        # Prune emptied branches RECURSIVELY: when normalization strips all
        # content out of a nested field (e.g. closing_responses sent in an
        # unusable shape), the write must not report that path as recorded —
        # an empty leaf left in place produced a lying success and an
        # interview topic that never completed (incident 2026-07-15).
        def _drop_empty_dicts(node: dict) -> None:
            for k in list(node.keys()):
                v = node[k]
                if isinstance(v, dict):
                    _drop_empty_dicts(v)
                    if not v:
                        node.pop(k)
        _drop_empty_dicts(changes)
        # Destructive-replace guard: lists merge by whole replacement, so a
        # model that misfiles an objections answer under call_flow.stages
        # would silently wipe a built flow (incident 2026-07-15: an 8-stage
        # flow replaced by one 'pushback_handling' stage, three turns in a
        # row). A shrink to stages the draft has never seen is refused with a
        # repairable error; growing, same-size rewrites, and trims to a
        # subset of the existing stage ids still pass.
        new_stages = (changes.get("call_flow") or {}).get("stages") \
            if isinstance(changes.get("call_flow"), dict) else None
        if isinstance(new_stages, list):
            cur_cf = iv["draft"].get("call_flow")
            old_stages = (cur_cf.get("stages") if isinstance(cur_cf, dict) else None) or []
            old_ids = [str(s.get("id") or "") for s in old_stages if isinstance(s, dict)]
            new_ids = sorted({str(s.get("id") or "")
                              for s in new_stages if isinstance(s, dict)})
            if (len(old_ids) >= 2 and len(new_stages) < len(old_ids)
                    and not set(new_ids).issubset(set(old_ids))):
                raise ValueError(
                    "REFUSED — call_flow.stages is replaced WHOLE, and this update "
                    f"would delete the existing {len(old_ids)}-stage flow "
                    f"({', '.join(old_ids)}) leaving only: {', '.join(new_ids)}. "
                    "Nothing was changed. If you are recording objections or "
                    "pushback answers, they belong in "
                    "response_bank.objection_playbook (list of {id, patterns: "
                    "[caller phrasings], answer: {lang: str}}) or "
                    "response_bank.objection_responses ({lang: [str]}) — retry "
                    "update_draft NOW with those paths and do NOT touch "
                    "call_flow. To genuinely restructure the flow, resend the "
                    "FULL stages list including every existing stage you keep.")
        if not changes:
            # F2: a machine-repairable failure. The LLM must fix its field
            # paths and retry — the USER's answer was never the problem.
            progress = interview_progress(iv["draft"], iv["skipped"])
            layer_line = ""
            if progress["next_layer_id"]:
                layer = next(l for l in INTERVIEW_LAYERS
                             if l.id == progress["next_layer_id"])
                layer_line = (f" The CURRENT topic ({layer.title}) records under: "
                              + ", ".join(layer.spec_paths) + ".")
            raise ValueError(
                "Nothing was recorded — the field names didn't match real settings. "
                + " ".join(notes) + layer_line
                + " Retry update_draft NOW with the corrected paths. The user's "
                "answer was fine — do NOT ask them to rephrase and do NOT tell "
                "them a setting doesn't exist.")
        # ── input priority ──────────────────────────────────────────────
        # Rank every leaf against the user's actual words, then refuse any
        # leaf that would overwrite something better-sourced. The model may
        # fill gaps freely; it may not rewrite the user (incident
        # 2026-07-15: invented objection_responses on a topic the brief
        # had already scripted).
        ranks = _rank_changes(changes, _user_corpus(session), claimed)
        _strip_outranked(changes, iv["draft"], iv.get("provenance") or {},
                         ranks, notes,
                         note_prefix="kept the wording you gave over new")
        _drop_empty_dicts(changes)
        if not changes:
            raise ValueError(
                "Nothing was recorded: every field in that call would have "
                "overwritten wording the USER already gave, with copy they "
                "did not. Their input stands. Do NOT retry the same content — "
                "either move on to the next open topic, or, if the user just "
                "asked for a change in their latest message, quote THEIR words "
                "in the new value.")
        authored = sorted(p for p, r in ranks.items()
                          if r == "authored" and p in _paths_of_changes(changes))
        if authored:
            notes.append(
                "recorded as YOUR draft copy (the user has not said this): "
                + ", ".join(authored[:8])
                + ". If the user gave wording for it, re-record theirs verbatim.")
        _deep_merge(iv["draft"], changes)
        iv["phase"] = "interviewing"
        for p in sorted(_paths_of_changes(changes)):
            # the VERIFIED rank, never the model's claim
            iv["provenance"][p] = ranks.get(p, claimed)
        progress = interview_progress(iv["draft"], iv["skipped"])
        # Quality moves PRE-create: warn on the merged preview now, so thin
        # content is challenged during the interview, not after the write.
        preview = _default_definition("draft_preview", copy.deepcopy(iv["draft"]))
        _deep_merge(preview, copy.deepcopy(iv["draft"]))
        preview.pop("_ingested_docs", None)
        # Preview through the same create-time enrichment so the lints match
        # what create_agent will actually produce (throwaway dict — the draft
        # itself is never mutated here).
        try:
            _enrich_new_agent(preview, [])
        except Exception:  # enrichment must never break a draft update
            logger.exception("[copilot] preview enrichment failed")
        quality = _safe_quality(preview)
        res = {
            "status": "draft_updated",
            "recorded_paths": sorted(_paths_of_changes(changes)),
            "notes": notes,
            "quality": quality,
            "progress": {
                "complete": progress["complete"],
                "open_layers": progress["open_layers"],
                "layers": [
                    {"id": l["id"], "title": l["title"], "done": l["done"],
                     "skipped": l["skipped"]} for l in progress["layers"]],
            },
            "next_layer_id": progress["next_layer_id"],
            "next_question": progress["next_question"],
        }
        done_n = sum(1 for l in progress["layers"] if l["done"] or l["skipped"])
        return (res, f"Noted — {done_n}/{len(progress['layers'])} topics covered",
                True, False)

    if tool == "skip_layer":
        session = guard.get("session")
        if session is None:
            raise ValueError("no session")
        iv = _interview_state(session)
        layer_id = str(args.get("layer_id") or "").strip()
        user_words = str(args.get("user_words") or "").strip()
        known = {l.id for l in INTERVIEW_LAYERS}
        if layer_id not in known:
            raise ValueError(f"No interview layer '{layer_id}' — layers: {', '.join(sorted(known))}")
        if not user_words:
            raise ValueError(
                "skip_layer needs the user's own words declining this topic "
                "(quote them) — it is auditable, not a shortcut for you.")
        # Consent must be real (2026-07-23 audit: two "don't ask me anything"
        # messages mass-skipped 9 layers and shipped a fully fabricated
        # agent, and one skip cited BROCHURE text as the user's words).
        # (a) the quoted words must come from an actual USER message, or the
        # latest user message must itself express skip intent; (b) at most 3
        # skips per turn, so "skip everything" still takes deliberate,
        # repeated instruction rather than one impatient sentence.
        _latest = str(guard.get("user_message") or "").lower()
        _skip_intent = re.compile(
            r"skip|don'?t ask|no more question|just (build|create|make)|"
            r"your (call|choice)|defaults?|whatever you think|"
            r"मत पूछ|जो ठीक लगे|પૂછ", re.I)
        _user_texts = " \n".join(
            str(m.get("content") or "").lower()
            for m in (session.get("messages") or []) if m.get("role") == "user")
        _quoted_ok = user_words[:80].lower() in _user_texts
        if not (_quoted_ok or _skip_intent.search(_latest)):
            raise ValueError(
                "Refused: those words don't appear in anything the user "
                "typed. A layer is skipped only when the USER declines it — "
                "ask the layer's question instead.")
        _skips = int(guard.get("skips_this_turn") or 0) + 1
        guard["skips_this_turn"] = _skips
        if _skips > 3:
            raise ValueError(
                "Refused: 3 layers already skipped this turn. Ask the next "
                "open question — if the user really wants everything "
                "skipped, they can say so again.")
        if layer_id not in iv["skipped"]:
            iv["skipped"].append(layer_id)
        iv["provenance"][f"skip:{layer_id}"] = user_words[:200]
        iv["phase"] = "interviewing"
        progress = interview_progress(iv["draft"], iv["skipped"])
        res = {"status": "skipped", "layer_id": layer_id,
               "open_layers": progress["open_layers"],
               "next_question": progress["next_question"]}
        return res, f"Skipped the {layer_id.replace('_', ' ')} questions (user's call)", True, False

    if tool == "ingest_document":
        session = guard.get("session")
        sid = str(guard.get("sid") or "")
        if session is None or not sid:
            raise ValueError("no session for document ingestion")
        doc_id = str(args.get("doc_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-.]{1,120}", doc_id):
            raise ValueError("invalid doc_id")
        # Extracted text persists in the session, so re-ingesting (e.g. when a
        # chat is continued days later) is instant and never re-runs OCR.
        _doc_store = session.setdefault("documents", {})
        cached = _doc_store.get(doc_id) or {}
        extracted = str(cached.get("text") or "")
        if not extracted:
            doc_path = _find_uploaded_doc(sid, doc_id, guard.get("owner"))
            if doc_path is None:
                raise ValueError(f"No uploaded document '{doc_id}' on this conversation")
            from backend.app.documents.extractor import extract_text
            content = doc_path.read_bytes()
            # extract may hit Gemini OCR (network, up to ~2 min) — keep the
            # event loop free.
            extracted = await asyncio.to_thread(extract_text, content, doc_id)
            if not extracted.strip():
                raise ValueError("No text could be extracted from this document")
            _doc_store[doc_id] = {"text": extracted[:400_000],
                                  "chars": len(extracted),
                                  "ingested_at": _now_iso()}
            _save_session(sid)
        from backend.app.documents.yaml_filler import extract_fields_from_text
        # The document's text IS user input — register it in the corpus so
        # content traced to it ranks above the model's own authorship.
        _interview_state(session).setdefault("input_texts", []).append(extracted)
        # extraction runs up to 12 sync OpenAI calls (~30-60s) — keep the
        # event loop free, same as extract_text above.
        result = await asyncio.to_thread(extract_fields_from_text, extracted)
        if result.get("status") != "ok":
            raise ValueError(result.get("error") or "extraction failed")
        fields = result.get("fields") or {}
        draft_changes, notes, progress, layers_closed = \
            _merge_extracted_into_draft(session, fields)
        iv = _interview_state(session)
        docs = iv["draft"].setdefault("_ingested_docs", [])
        if doc_id not in docs:
            docs.append(doc_id)
        res = {
            "status": "ingested", "doc_id": doc_id,
            "extracted_fields": sorted(k for k in fields if fields.get(k)),
            "recorded_paths": sorted(_paths_of_changes(draft_changes)),
            "topics_closed": layers_closed,
            "missing": result.get("missing") or [],
            "notes": notes,
            "open_layers": progress["open_layers"],
            "next_question": progress["next_question"],
        }
        closed_txt = (" — closed: " + ", ".join(layers_closed)) if layers_closed else ""
        return (res, f"Read the document and pulled {len(fields)} field(s) "
                     f"into the draft{closed_txt}", True, False)

    # ── v2 surgical edit engine (plan §5.3/§5.5) ─────────────────────────────

    def _write_scoped(name: str, merged: dict, base_rev: str, reason: str,
                      expected: set[str] | None) -> dict:
        return agent_store.write_agent(
            name, merged, actor=_copilot_actor(), base_rev=base_rev,
            reason=reason, owner=builder._ctx_owner(),
            is_admin=builder._ctx_is_admin(), expected_paths=expected,
            agents_dir=_AGENTS_DIR)

    if tool == "propose_update":
        name = str(args["name"]).strip()
        reason = str(args.get("reason") or "")
        notes: list[str] = []
        current, base_rev = _read_full_definition(name)
        ops = args.get("ops")
        if ops:
            if not isinstance(ops, list):
                raise ValueError("'ops' must be a list of operations")
            candidate = _apply_ops(current, ops)
            expected = sorted({str(o.get("path") or "").split("[")[0].rstrip(".")
                               for o in ops if o.get("path")})
        else:
            changes = _coerce_obj(args.get("changes") or {})
            changes.pop("name", None)
            for k in _SERVER_MANAGED_KEYS:
                changes.pop(k, None)
            _normalize_definition(changes, notes)
            for k in [k for k, v in changes.items() if isinstance(v, dict) and not v]:
                changes.pop(k)
            if not changes:
                raise ValueError("None of those changes map to real settings. " + " ".join(notes))
            candidate = _deep_merge(copy.deepcopy(current), changes)
            expected = sorted(_paths_of_changes(changes))
        violations = _invariant_violations(candidate)
        if violations:
            raise ValueError("Refused — this change would break the agent: "
                             + "; ".join(violations))
        AgentDefinition.from_dict(candidate)  # dry validation, no write
        diff = [d for d in _compute_diff(current, candidate)
                if d["path"] not in ("revision", "updated_at", "updated_by")]
        # Dead/shadowed writes are cheapest to stop at proposal time — the
        # user has not confirmed anything yet. Check the intent paths AND the
        # diff paths: a new nested subtree diffs at its ROOT (diff.py), so
        # only `expected` still carries the dead leaf.
        _all_paths = list(expected) + [d["path"] for d in diff]
        _block_dead_paths(_all_paths)
        _reach = _surfaces.reachability_findings(candidate, _all_paths)["warnings"]
        session = guard.get("session") or {}
        pending = session.setdefault("pending_changes", {})
        # Monotonic per-session counter, NOT len(pending): discarding a change
        # shrinks the dict, so len+1 re-issues an id that a still-visible diff
        # card may already point at — clicking its Apply would then commit a
        # different change than the one on screen. abs(hash(name)) is no help
        # either: str hashing is salted per process, so the same agent yields a
        # different suffix after any restart.
        seq = int(session.get("change_seq") or 0) + 1
        session["change_seq"] = seq
        change_id = f"chg_{seq}_{re.sub(r'[^a-z0-9]+', '', name.lower())[:12] or 'agent'}"
        pending[change_id] = {
            "slug": name, "base_rev": base_rev, "candidate": candidate,
            # Base snapshot: lets a stale proposal be replayed onto a newer
            # definition instead of dying on an unrecoverable 409 (_rebase_change).
            "base": copy.deepcopy(current),
            "expected_paths": list(expected), "reason": reason,
            "diff_human": _render_diff(diff),
            "proposed_at_turn": guard.get("user_turns", 0),
        }
        res = {"status": "proposed", "change_id": change_id, "name": name,
               "diff": _render_diff(diff), "paths": expected,
               "notes": notes,
               "next": "Show the diff to the user; call apply_update after they confirm."}
        if _reach:
            res["reachability"] = _reach
            res["next"] = ("This change will NOT be heard as-is — read "
                           "'reachability' and fix the proposal (or tell the "
                           "user plainly) BEFORE asking them to confirm.")
        return res, f"Proposed a change to {_agent_display_name(name)} (awaiting confirmation)", True, False

    if tool == "apply_update":
        change_id = str(args.get("change_id") or "").strip()
        session = guard.get("session") or {}
        pending = session.get("pending_changes") or {}
        chg = pending.get(change_id)
        if not chg:
            raise ValueError(f"No pending change '{change_id}' — propose it first")
        # Server-side confirmation gate: proposed in a PRIOR turn, or the
        # triggering user message carries an explicit confirmation.
        msg = str(guard.get("user_message") or "").lower()
        confirmed_words = bool(re.search(
            r"\b(yes|yeah|apply|confirm|go ahead|do it|approve|haan|ok|okay|sure|ha)\b", msg))
        if guard.get("user_turns", 0) <= chg.get("proposed_at_turn", 0) and not confirmed_words:
            raise ValueError(
                "Refused: apply_update needs the user's confirmation — show the "
                "diff and apply only after they say yes.")
        name = chg["slug"]
        builder._assert_slug_access(name, owner)
        written = chg["candidate"]
        try:
            result = _write_scoped(name, chg["candidate"], chg["base_rev"],
                                   chg.get("reason") or "copilot proposed change",
                                   set(chg.get("expected_paths") or []) or None)
        except agent_store.ConflictError:
            # The agent moved under us (often the copilot's own direct edit in
            # the same turn). Replay this proposal's paths onto the current
            # definition rather than failing — and drop the dead change so a
            # genuine conflict can't be retried forever.
            try:
                rebased, fresh_rev = _rebase_change(chg)
            except ValueError:
                pending.pop(change_id, None)
                raise
            result = _write_scoped(name, rebased, fresh_rev,
                                   chg.get("reason") or "copilot proposed change",
                                   set(chg.get("expected_paths") or []) or None)
            written = rebased
        pending.pop(change_id, None)
        res = {"status": "updated", "name": name, "change_id": change_id,
               "revision": result["revision"], "effective": "next_call"}
        _chg_paths = list(chg.get("expected_paths") or [])
        res.update(await _fidelity_extras(written, _chg_paths))
        _twin = _twin_note(name, _chg_paths)
        if _twin:
            res["twin"] = _twin
        return res, (f"Applied the change to {_agent_display_name(name)} — "
                     f"revision {result['revision']}, applies from the next call"), True, True

    if tool == "discard_update":
        change_id = str(args.get("change_id") or "").strip()
        session = guard.get("session") or {}
        pending = session.get("pending_changes") or {}
        # Idempotent: "make sure that proposal is gone" is satisfied by it
        # already being gone. Erroring here made the copilot report a failure
        # for a no-op and then burn turns trying to recover from it.
        existed = pending.pop(change_id, None) is not None
        return ({"status": "discarded", "change_id": change_id,
                 "already_gone": not existed},
                "Discarded the proposed change" if existed
                else "That proposal was already gone — nothing to discard",
                True, False)

    if tool in ("update_stage", "add_stage", "remove_stage", "reorder_stages"):
        name = str(args["name"]).strip()
        current, base_rev = _read_full_definition(name)
        stages = list((current.get("call_flow") or {}).get("stages") or [])
        ids = [str(s.get("id") or "") for s in stages]
        if tool == "update_stage":
            sid = str(args.get("stage_id") or "").strip()
            if sid not in ids:
                raise ValueError(f"No stage '{sid}' — stages are: {', '.join(ids)}")
            fields = _coerce_obj(args.get("fields") or {})
            fields.pop("id", None)
            stage_keys = _fields_of(StageDefinition)
            fields = {k: v for k, v in fields.items() if k in stage_keys}
            if not fields:
                raise ValueError("No real stage fields to update")
            if isinstance(fields.get("goal"), str):   # RUNBOOK §42
                fields["goal"] = _tokenize_address(
                    fields["goal"], _address_protected_names(_all_strings(current)))
            stages[ids.index(sid)] = {**stages[ids.index(sid)], **fields}
            reason = f"update stage {sid}: {', '.join(fields)}"
        elif tool == "add_stage":
            stage = _coerce_obj(args.get("stage") or {})
            sid = str(stage.get("id") or "").strip().upper().replace(" ", "_")
            if not sid:
                raise ValueError("stage.id is required")
            if sid in ids:
                raise ValueError(f"Stage '{sid}' already exists")
            stage["id"] = sid
            if isinstance(stage.get("goal"), str):    # RUNBOOK §42
                stage["goal"] = _tokenize_address(
                    stage["goal"], _address_protected_names(_all_strings(current)))
            after_id = str(args.get("after_id") or "").strip()
            idx = ids.index(after_id) + 1 if after_id in ids else len(stages)
            stages.insert(idx, stage)
            reason = f"add stage {sid}"
        elif tool == "remove_stage":
            sid = str(args.get("stage_id") or "").strip()
            if sid not in ids:
                raise ValueError(f"No stage '{sid}' — stages are: {', '.join(ids)}")
            if len(stages) <= 1:
                raise ValueError("Cannot remove the last stage")
            stages.pop(ids.index(sid))
            reason = f"remove stage {sid}"
        else:  # reorder_stages
            ordered = [str(x) for x in (args.get("ordered_ids") or [])]
            if sorted(ordered) != sorted(ids):
                raise ValueError(
                    f"ordered_ids must contain exactly the existing stages: {', '.join(ids)}")
            by_id = {str(s.get("id")): s for s in stages}
            stages = [by_id[i] for i in ordered]
            reason = "reorder stages: " + " → ".join(ordered)
        for i, s in enumerate(stages):  # renumber order ints
            s["order"] = i
        candidate = copy.deepcopy(current)
        candidate.setdefault("call_flow", {})["stages"] = stages
        violations = _invariant_violations(candidate)
        if violations:
            raise ValueError("Refused: " + "; ".join(violations))
        diff = [d for d in _compute_diff(current, candidate)
                if d["path"] not in ("revision", "updated_at", "updated_by")]
        result = _write_scoped(name, candidate, base_rev, reason,
                               {"call_flow.stages"})
        res = {"status": "updated", "name": name, "diff": _render_diff(diff),
               "stages": [str(s.get("id")) for s in stages],
               "revision": result["revision"], "effective": "next_call",
               # Stage edits only touch call_flow.stages — re-surface the
               # mission (rendered EVERY turn as ━━ CALL MISSION ━━) so a
               # ban/removal edit can't silently leave it stale, and run the
               # quality lints so cross-surface contradictions show up in
               # the same turn (update_stage historically returned neither).
               "quality": _safe_quality(
                   candidate, include_wiring_lints=False,
                   touched_stages=_changed_stage_ids(current, candidate)),
               "call_goal": str((candidate.get("call_flow") or {})
                               .get("call_goal") or "")[:300],
               "call_goal_note": (
                   "call_goal renders EVERY turn as the CALL MISSION — if "
                   "this edit removed or banned phrasing, verify call_goal "
                   "no longer carries it (fix via update_agent)."),
               }
        return res, f"{reason} in {_agent_display_name(name)}", True, True

    if tool in ("update_slot", "add_slot", "remove_slot"):
        name = str(args["name"]).strip()
        current, base_rev = _read_full_definition(name)
        slots = list(current.get("slots") or [])
        names_l = [str(s.get("name") or "") for s in slots]
        if tool == "update_slot":
            sn = str(args.get("slot_name") or "").strip().lower()
            if sn not in names_l:
                raise ValueError(f"No slot '{sn}' — slots are: {', '.join(names_l) or 'none'}")
            fields = _coerce_obj(args.get("fields") or {})
            fields.pop("name", None)
            slot_keys = _fields_of(SlotDefinition)
            fields = {k: v for k, v in fields.items() if k in slot_keys}
            if not fields:
                raise ValueError("No real slot fields to update")
            slots[names_l.index(sn)] = {**slots[names_l.index(sn)], **fields}
            reason = f"update slot {sn}: {', '.join(fields)}"
        elif tool == "add_slot":
            slot = _coerce_obj(args.get("slot") or {})
            sn = str(slot.get("name") or "").strip().lower().replace(" ", "_")
            if not sn:
                raise ValueError("slot.name is required")
            if sn in names_l:
                raise ValueError(f"Slot '{sn}' already exists")
            slot["name"] = sn
            slots.append(slot)
            reason = f"add slot {sn}"
        else:
            sn = str(args.get("slot_name") or "").strip().lower()
            if sn not in names_l:
                raise ValueError(f"No slot '{sn}' — slots are: {', '.join(names_l) or 'none'}")
            slots.pop(names_l.index(sn))
            reason = f"remove slot {sn}"
        candidate = copy.deepcopy(current)
        candidate["slots"] = slots
        # Run slot-level value validation from the sanitizer on the subtree.
        notes: list[str] = []
        _normalize_definition({"slots": candidate["slots"]}, notes)
        AgentDefinition.from_dict(candidate)
        diff = [d for d in _compute_diff(current, candidate)
                if d["path"] not in ("revision", "updated_at", "updated_by")]
        result = _write_scoped(name, candidate, base_rev, reason, {"slots"})
        res = {"status": "updated", "name": name, "diff": _render_diff(diff),
               "slots": [str(s.get("name")) for s in slots], "notes": notes,
               "revision": result["revision"], "effective": "next_call",
               "quality": _safe_quality(candidate,
                                            include_wiring_lints=False)}
        return res, f"{reason} in {_agent_display_name(name)}", True, True

    if tool == "list_revisions":
        name = str(args["name"]).strip()
        revs = agent_store.list_revisions(name)
        res = {"revisions": [
            {"rev_id": r["id"], "actor": r["actor"], "reason": r["reason"],
             "created_at": str(r["created_at"])} for r in revs]}
        return res, f"Checked {_agent_display_name(name)}'s edit history", True, False

    if tool == "rollback_agent":
        name = str(args["name"]).strip()
        rev_id = int(args.get("rev_id") or 0)
        result = agent_store.rollback_agent(
            name, rev_id, actor=f"{owner}:rollback", owner=owner,
            is_admin=builder._ctx_is_admin(), agents_dir=_AGENTS_DIR)
        res = {"status": "rolled_back", "name": name, "rev_id": rev_id,
               "revision": result["revision"], "effective": "next_call"}
        return res, (f"Rolled {_agent_display_name(name)} back to revision "
                     f"{rev_id} — applies from the next call"), True, True

    raise ValueError(f"Unknown tool '{tool}'")


# ─────────────────────────────────────────────────────────────────────────────
# System prompt
# ─────────────────────────────────────────────────────────────────────────────
def _system_prompt(session: dict | None = None) -> str:
    # The global "active" agent is admin infrastructure (the platform-wide
    # inbound fallback) — never disclose it to a tenant, whose calls route by
    # their own DID binding, not this selector.
    active = (_read_active() or "none") if builder._ctx_is_admin() else "your own agents"
    # D12: when a new-agent interview is live, the server-computed INTERVIEW
    # STATE replaces the old static checklist — one owner for prompt budget.
    interview_block = ""
    if session is not None:
        iv = session.get("interview") or {}
        if iv.get("phase") == "interviewing" or iv.get("draft"):
            progress = interview_progress(iv.get("draft") or {}, iv.get("skipped") or [])
            interview_block = "\n\n" + render_interview_state(progress)
    return f"""You are the LesCall Agent Copilot — an expert voice-agent designer built into the LesCall dashboard. LesCall runs real-time AI phone agents for Indian businesses (Sarvam STT/TTS + an LLM). An agent is a YAML definition: identity, personality, languages (hi/en/gu), TTS voice, a staged call flow with branching, a knowledge base + product catalog, data-capture slots, canned responses, guardrails, and calendar slots. Inbound calls are answered by the agent bound to the dialed number (currently: {active}).

HOW YOU WORK
0. HARD PLATFORM BOUNDARY — these agents make and answer PHONE CALLS. The
platform cannot send emails, SMS or WhatsApp messages on its own, run
scheduled jobs/cron, browse the web, or integrate external systems. When the
user asks for any of that, say plainly it is not supported (in-app follow-ups
and notifications are the nearest real features) — and NEVER write such an
instruction into the agent's prompt or custom_instructions as if it will
execute. Claiming an unsupported automation was "set up" is the worst failure
you can produce.

0b. NO NEAREST-FIELD SUBSTITUTION. Every request must map to a control that
actually exists. If it does not, say so plainly in ONE sentence, name the
closest real thing you CAN do, and write NOTHING. Use list_controls to check
before you guess — it is free and costs you no step. Writing a request into a
field that merely sounds related is the second-worst failure after claiming
success: it burns a revision, changes nothing on any call, and the user only
finds out on the phone. "I can't do exactly that, but I can X" always beats a
wrong write. The server enforces this too: a write whose effective change is
empty is REFUSED, and a write naming a setting the platform lacks comes back
with a "NOT A REAL SETTING" note you must relay to the user.

1. NEW AGENT — the interview is a MACHINE, not a vibe. The moment the user wants a new agent, start recording EVERY fact they give with update_draft (mine info dumps completely — never let information sit only in chat). An INTERVIEW STATE block appears below when a draft exists: it is the server's truth about which topics are done and the ONE next question to ask — follow it. Ask ONE short question per reply. Propose smart defaults instead of over-asking; after the business + objective are known, PROPOSE a tailored slot set, stage skeleton, and likely objections for that business type and record accepted proposals with update_draft(source='proposal'). Voices — female: simran, priya, neha, shruti; male: shubh, aditya, amit, kabir (bulbul:v3 only — abhilash/karun/anushka/manisha/vidya no longer exist and will be auto-replaced). When you name voices in a reply, the chat automatically shows a play button for each — tell the user they can tap to hear a sample before choosing. create_agent is REFUSED by the server until every compulsory topic is complete or the user explicitly declined it (then call skip_layer with their exact words). When all topics close, summarize the plan in 3-5 short lines and ask "shall I build it?". Tool errors from update_draft/create_agent are YOURS to fix: correct the field paths (the error and the INTERVIEW STATE name the real ones) and retry silently — NEVER tell the user their answer "isn't a real setting" and NEVER ask them to rephrase because of a field-mapping problem.
2. THE USER'S INPUT OUTRANKS YOU — ALWAYS. Anything they typed or put in a document is the source of truth: record it VERBATIM (their exact lines, their exact objection answers, their exact rules) and never paraphrase, "improve", translate away, or replace it with copy of your own. The server enforces this: a write that would overwrite better-sourced wording is dropped, and content you invented is flagged back to you as YOUR copy. Your own authorship is for GAPS ONLY — a topic the user has said nothing about. There, draft DEEP: stage goals of 2-4 full sentences of concrete instructions (never a 5-word label), a knowledge paragraph carrying real substance (it is injected on EVERY turn), real objection responses, natural phone-appropriate copy in the agent's languages. But when the user HAS spoken on a topic, your job is to transcribe them, not to write. NEVER invent facts (products, prices, phone numbers, brand claims, locations, policies) — a fabricated fact gets SPOKEN to real callers. If a tool result says content was recorded as YOUR draft copy and the user did give wording for it, re-record theirs verbatim in the same turn. For an outbound pitch, the mission and talking points MUST live in the stage goals and the knowledge paragraph — that is the only way they reach the live call.
3. Get the copy into the DRAFT with update_draft BEFORE creating — create_agent builds the agent FROM THE INTERVIEW DRAFT, and the draft always wins: any definition you pass to create_agent is ignored for every topic the interview answered, and only fills topics the user explicitly skipped. NEVER invent facts (projects, prices, phone numbers, brand claims, locations) the user did not give you — a fabricated fact gets SPOKEN to real callers. Slug = lowercase with underscores.
4. After create_agent succeeds the draft is DEAD — update_draft is refused. Every further change ("make it outbound", "change the voice", greeting edits) goes through update_agent (or the stage/slot tools) on the created agent, and you must confirm the change with THOSE tool results, never from a draft write.
5. RIGHT AFTER create_agent (and after any content edit): read the tool result. If it lists "quality" warnings, the agent is too shallow to run well — fix EVERY warning with update_agent in this SAME turn (richer stage goals, a fuller knowledge paragraph, real objection responses) before you say a word to the user. Only once the result comes back with no quality warnings do you tell the user it's built. Then confirm in one sentence and ASK whether to activate it (activation makes it answer REAL inbound phone calls). CRITICAL: call activate_agent ONLY after the user's explicit yes in their most recent message — never in the same turn as creation, never assumed. If they decline, that's fine — leave the current active agent alone and offer further edits.
6. EDITS — this must be BULLETPROOF:
   a. Users name agents by their spoken name ("Ananya") — pass that name as-is to tools; resolution to the right agent is automatic. If a tool says no such agent exists, tell the user plainly which agents DO exist and ask which they meant — never guess a different agent and never silently switch to the active one.
   a2. ONE agent, TWO directions: when a name matches the same agent in both inbound and outbound, do NOT spend a turn asking if the answer is already obvious. Re-call the tool with the direction appended — get_agent("Prakhar (outbound)"). The user talking about dialling out, contacts, or leads means outbound; talking about callers reaching them means inbound. Ask only when the request genuinely fits both, and ask ONCE — never repeat the same failing call.
   b. Call get_agent first if you haven't read that agent THIS conversation, and re-read it before any edit you're not 100% sure about.
   c. RENAMES/SWAPS: when the user changes something that can appear in more than one place — a person (boss, owner, CEO), the company, a product, a city, a price, a phone number, the agent's own name, or a phrase the user wants the agent to STOP SAYING — you MUST use replace_mentions (find/replace across the whole agent), NOT update_agent. Example: "assistant for Mr. Ben → Mr. Umang" = replace_mentions(find="Mr. Ben", replace="Mr. Umang"). Map the mentions first and verify afterwards with find_text. A partial edit that leaves old mentions in the greeting, responses, or call_flow.call_goal is a FAILURE.
   d. After replace_mentions, if the replaced count seems low for how prominent the thing is, get_agent and check for other spellings/variants of the old text (e.g. "Ben", "Mr Ben", "बेन") and replace those too.
   e. Other edits: update_agent with the MINIMAL partial change (server-side merge keeps everything else). NEVER resend a whole list to edit one item: for one call stage use update_stage/add_stage/remove_stage/reorder_stages; for one collected-info question use update_slot/add_slot/remove_slot — these edit exactly that item on the server.
   e2. For bigger or riskier edits, prefer the two-step flow: propose_update (returns a diff — show it to the user in plain words) → apply_update after they confirm, or discard_update if they decline. Every change is versioned: list_revisions shows history and rollback_agent undoes a change when the user asks.
   e3. PICK ONE LANE PER AGENT PER TURN. Once you have proposed a change and are waiting on the user, do NOT also edit that same agent directly (update_agent/update_stage/update_slot/...) — the direct write moves the agent underneath your own pending proposal and the user's Apply button then fails. If you decide the direct edit is the right move, discard the proposal first. Never leave a proposal on screen that you have already superseded.
   e4. If an apply comes back saying the agent changed since the proposal, do NOT retry the same apply — it will fail identically every time. get_agent to see the current state, then either propose_update again from that fresh state, or tell the user plainly what changed underneath. Repeating a failing apply is never progress.
   f. After every successful edit, tell the user in one plain sentence WHAT is different now and that it applies from the next call (quote the revision number when the tool returns one). If nothing was changed in a turn, never claim it was.
   g. If the user says a change didn't take effect on a real call: do NOT speculate about caching or internal mechanisms. First list_agents to confirm the agent you've been editing IS the live one — if a different agent is live, say so plainly and ask whether to activate the right one. Then get_agent, hunt for the old wording, and fix every remaining mention. Special case: if the OLD OPENING LINE still played at connect, the greeting lives ONLY in response_bank.opening_greeting — check and fix THAT field; editing stage goals never changes what plays at connect.
   e5. EDIT A FIELD, DO NOT REGENERATE IT. When the user dictates new wording for something that already exists — a stage goal, the knowledge paragraph, an objection answer — you are changing the PART they named, not rewriting the field from their message. Free-text fields accumulate constraints that nobody will dictate again because they were never the user's words: "say ONLY that line and wait", "do not manufacture a problem", "never invent specific numbers or ROI", an acknowledgment rule, a script-fidelity guard. Read the current text first, keep every such sentence verbatim, and splice the user's new wording into it. On 2026-09-08 three separate Copilot edits to jai_mistry-outbound each dropped a constraint the user had not mentioned — including the rule that stopped the agent inventing lead-count numbers — and the next live call went off-script (RUNBOOK §37). If you genuinely believe an existing constraint should go, say so and ask; never delete one silently.
   e6. NEVER write "then terminate the call", "end the call immediately", "advance to WRAP_UP" or any other transition/hangup instruction into a stage goal. The model cannot see the destination stage's script, and it does not end calls — the PLATFORM ends them, and it asks the caller's permission first ("alright then, shall I end the call here?"). A goal that tells the model to hang up produces an improvised line instead of the scripted close. Use branches for transitions and call_flow.termination.end_conditions for when the call should end.
   h. If the user says the agent SAID something it was told not to: find_text the offending phrase — the surface that still carries it (most often call_flow.call_goal, an objection answer, or the knowledge paragraph) is the bug, NOT the stage goal you already edited. If the offence is the agent proposing meeting times, also check call_flow.scheduling: the exact_n default FORCES time offers regardless of goals — set {{"style": "flexible"}}.
6. QUESTIONS ("which agent is live?"): use list_agents / get_agent / get_routing. Do NOT call get_agent unless the user asks about that agent or you are about to edit it — during a new-agent interview no tool calls are needed until you create.

DEFINITION FIELDS you may set (all optional — deep-merged onto defaults). SHAPES ARE STRICT — get them right the first time:
- identity: agent_name, company_name, company_tagline, role_description, contact, role_line (full replacement identity sentence)
- personality: energy (low|medium|high), tone, style, custom_instructions, traits [str], energy_mirroring (bool), ai_disclosure ("deflect"|"admit"|"admit_if_pressed"), verbosity_max_sentences (int)
- languages MUST be an OBJECT, never a list: {{"supported": ["hi","en"], "default": "hi", "auto_switch": true, "allow_code_mixing": true, "stt_forced_language": "", "tts_voices": {{"hi": {{"model":"bulbul:v3","speaker":"simran","pace":1.15}}, "en": {{...}}}}}}. LANGUAGE SWITCHING IS ON BY DEFAULT and is BOUNDED BY `supported`: the agent follows the caller into any language listed there, and can never be dragged into one that is not. So "supported" is the real control — adding a language means the agent will actually speak it (give it a tts_voice too), and removing one means it never will. Set auto_switch false ONLY when the business explicitly wants one fixed language regardless of what the caller speaks; say plainly what that costs. If the user asks for a language the platform does not support (only hi/en/gu exist), tell them — do not add it to the list. response_bank.lang_unsupported_responses is what the agent says when a caller asks for a language it was not built for. tts_voices lives INSIDE languages — never at the top level. pace = speaking speed 0.5–2.0 (1.0 natural, 1.15 platform default) and is PER LANGUAGE. SPEED COMPLAINTS ('talks too fast', 'too slow' — overall or in one language): do NOT silently change anything. FIRST tell the user the current setting and the scale, read from the agent (get_agent's spoken_surfaces.pace has it), e.g. 'LesCall currently speaks at pace 1.15 on a 0.5–2.0 scale, where 1.0 is natural' — THEN ask what they'd prefer (slower or faster, or an exact number) and apply their choice to that language's pace in ±0.1 steps via update_agent. Never change the voice to fix speed.
- call_direction: the STRING "inbound" or "outbound" — never a list. If the user wants BOTH, use "inbound" — the platform automatically applies the outbound approach whenever it places the call.
- An EXISTING agent's call type is fixed by its name: `x-inbound` answers calls, `x-outbound` places them, and both exist from creation. Never send call_direction to update_agent to "switch" one — edit the other version instead.
- call_flow: {{"call_goal": "one-sentence mission", "stages": [{{id, order, goal, turns_until_advance, role, collect: [slot names], advance_when: {{all_collect_filled|slots|min_turns|intent|appointment_captured, max_turns: N (time escape — the stage also advances at turn N even if the condition never fires)}}, branches: [{{when_intent: affirmation|negation|question|no_more_questions (no_more_questions = the caller's "no, nothing else" to THIS stage's "any other question?" moves the flow to goto instead of closing the call), goto: STAGE_ID, max_takes, actions: [capture_phone|promise_whatsapp_media|book_visit|schedule_callback]}} or {{when_slot: {{name: SLOT}}, goto: STAGE_ID, max_takes}} — omitting when_slot.equals means "this slot has ANY answer", the gate for a free-text follow-up with no fixed vocabulary], script: {{ask_verbatim: true (optional — the engine speaks the directive's slot question word for word after a reply that did not ask it; a paraphrase is never repeated), after_answer: {{lang: line}} (optional — spoken after every reply of this stage unless the reply already asks it or asks whether there is anything else), unless: [lines whose presence in the reply suppresses after_answer]}} — for a business that hands you an exact script; omit otherwise}}], "scheduling": {{style: exact_n|flexible — flexible means the CALLER proposes the time; SET flexible whenever the user says not to suggest times or to ask when the caller is free; exact_n (the default) makes the platform force N specific time offers every turn, offer_count, enabled: false when the business doesn't book}}, "callback_policy": {{enabled, capture_slot, responses: {{lang: str}}, counts_as_decline}}, "completed_when": {{required_slots, require_appointment}}, "termination": {{"end_conditions": ["plain-English rules for when the agent should wrap up and END the call — e.g. 'once the site visit is booked and confirmed, thank them and end', 'if the caller becomes abusive, apologise briefly and end immediately' — injected into every turn as END-CALL RULES the agent acts on"], "farewell_patterns": ["extra caller phrases in ANY language/script that mean goodbye and hang up deterministically, e.g. 'chalo rakhta hu' — NEVER single common words like 'ok'/'no'/'haan', and never a word that is a homophone of an ordinary one, they would cut live calls. The built-in goodbye words (bye, alvida, aavjo…) are already handled, and only count when they END the caller's sentence: 'okay thank you, bye' closes the call, 'bye philips lighting' (speech recognition mangling an employer name) does not"], "llm_end_call": true (default — lets the agent itself end the call after its goodbye; set false ONLY if the business wants hangups on caller-goodbye alone), "confirm_before_end": true (default, platform-wide — before hanging up on a heard goodbye the agent asks a short "alright then, shall I end the call here?" in the call's language, and after the caller confirms speaks a brief thank-you and hangs up; set false ONLY if the business explicitly wants instant hangups and accepts that a misheard "bye" cuts a live call), "pre_close_question": {{lang: str}} (optional — the business's OWN "anything else before I go?" line; when set it is asked before every goodbye, including after a confirmed visit read-back, and the caller's "no / nothing else" to it ends the call on response_bank.closing_responses),"silence_hangup": {{"enabled": true, "grace_s": 10}} (after both couldn't-hear reprompts go unanswered, say goodbye and end instead of holding the line open), "max_call_minutes": N (hard call-length ceiling — one polite wrap-up line, then the platform ends the call even mid-conversation; 0/omitted = no cap)}}, "connect": {{"hello_fastpath": true (default — an early bare "hello?" from the caller gets an instant identity intro instead of a slow LLM turn), "delivery_guard": true (default — during the first seconds a short "hello?" cannot cancel an agent reply the caller has not heard yet); set either false ONLY if the business explicitly wants the protection off}}, "turn_taking": {{"pause_hold_ms": N (hold EVERY caller pause this long before answering, so a sentence split by a long pause is answered once — costs that latency on every reply), "max_merged_fragments": N, "backchannel_guard": true (a bare "haan / ji / hmm / okay" while the agent is still speaking is a listener's acknowledgement, never a turn), "sustained_barge_ms": N (a barge-in held back by the delivery guard is carried out once the caller has kept speaking this long), "tail_barge_in": true (real caller speech while the agent's audio is still playing stops that audio)}} — set ONLY when the business reports the agent answering half-sentences or talking over callers; omit otherwise}}. Stage roles: greeting|confirm_person|discovery|pitch|recommend|scheduling|confirmation|closing. Write goals in ENGLISH as instructions (the agent replies in the caller's language). STAGE WIRING IS WHAT MAKES THE FLOW ACTUALLY RUN: every stage SHOULD carry role; a stage whose goal asks for slots lists them in collect and uses advance_when {{all_collect_filled: true, max_turns: 6}}; a yes/no confirmation stage gets turns_until_advance 2-3 or advance_when {{intent: affirmation, max_turns: 3}}; NEVER leave a mid-flow stage needing 8+ caller turns — one such stage consumes an entire call and the rest of your flow never plays (this is exactly how a live agent ignored its whole script). If you omit the wiring at create time the platform derives it from the goals and slots — but wiring you write explicitly always wins.
- slots (TOP-LEVEL list — the qualification schema, the heart of data capture): [{{name, priority (lower asked first), required, label, question: {{hi/en/gu: exact wording}}, type: text|number|money|enum|boolean|phone|email|date|time|location, enum_values, enum_synonyms, depends_on: [slot names never asked before], merge_policy: free|correction_gated|confirm, ask_gated: false (default; true = the slot is written ONLY as the answer to its own question or on an explicit correction — for a closed vocabulary of everyday domain words callers also say in questions, e.g. "what matters most: location/amenities/…"), confirm_readback: ""|verbatim|digit_groups_2 (phone in 2-digit groups)|spelled (email letter-by-letter), max_asks (2 recommended), reask_question: {{lang}}}}]. EVERY new agent gets slots for what it must collect, with questions in EVERY supported language. EMAIL/PHONE CAPTURE IS ENGINE-OWNED (platform-wide): type an email slot "email" — the ENGINE then buffers spelled letters, assembles + validates the address, reads it back letter-by-letter, and confirms or flags it unresolved for follow-up. Write stage goals so the agent asks for the email ONCE and moves on; NEVER script the agent to ask callers to "speak slowly" (slow spelling fragments worse) and never promise letter-by-letter read-back in the goal — the engine does it.
- knowledge_base: portfolio_prompt_text (plain-English paragraph — injected every turn), portfolio_section_label, locations, objection_patterns, known_projects {{"lowercase key": ["Canonical Name", "location or price"]}}, catalog [{{id, name, location, configurations, price_range, availability, positioning_tag, pitch_points, aliases, facts}}], match_rules [{{when: {{slot: value or "<=40L"}}, recommend: [ids], reason}}], primary_project {{id: <catalog id>, mode: exclusive|lead, reason}} (the ONE catalogue item pitched proactively — exclusive: every other item is answered when asked but never volunteered; lead: it is always the first candidate), pricing_policy {{mode: on_request|state_freely|defer, defer_line: {{lang}}}}, stt_corrections {{use_builtin_tables: false for non-real-estate agents}}
- brand: spoken_company_name, positioning_tagline, credibility_markers [str], trust_proofs [top 2-3 — objection ammo], regions_served, contact_phone, whatsapp_number
- response_bank: opening_greeting (ONE plain string in the default language — the INBOUND opening; see THE OPENING LINE below for which field outbound calls speak), identity_responses {{lang: str}}, objection_responses {{lang: [str]}}, objection_playbook [{{id, patterns: [caller phrasings], answer: {{lang: str — may use {{trust_proofs}}}}, prompt_guidance}}], closing_responses {{lang: str}}, canned_phrases [{{patterns, responses: {{lang}}}}], graceful_close {{lang: str}}
- OBJECTION_PLAYBOOK PATTERNS MUST COVER THE SCRIPT THE CALLER ACTUALLY SPEAKS IN, NOT JUST THE SCRIPT THE OWNER TYPED: an owner describing a Hindi/Gujarati agent almost always dictates patterns in romanized Hinglish ("kya karte ho aap") — but Sarvam STT transcribes fluent Hindi/Gujarati speech in DEVANAGARI/GUJARATI SCRIPT ("क्या करते हो आप"), and pattern matching is a literal substring test with no transliteration. A pattern authored in only one script can never fire for a caller speaking the other, no matter how well-worded it is — this is exactly how a Jai Mistry caller's real "tell me about your agency" got a fully improvised pitch instead of the owner's exact verbatim answer despite it being scripted for precisely that question (RUNBOOK §36). For every objection_playbook pattern and every knowledge_base.objection_patterns/time_patterns entry on an agent whose languages.supported includes hi or gu: add the native-script form alongside the romanized one, not instead of it (a caller may code-switch either way). Do this automatically — don't wait for the owner to notice a script is missing.
- OBJECTION_PLAYBOOK ENTRIES ARE MATCHED WHETHER OR NOT THE TURN CLASSIFIES AS AN OBJECTION: a neutral question ("what do you do", "are you an AI", "how did you get my number") is not a decline, and the platform checks these patterns independent of that classification — author them freely for ANY question with a fixed answer the owner wants spoken verbatim, not just pushback.
- outbound (for outbound agents): identity_confirm_line/good_time_check_line/pitch/wrong_person_response/busy_response/wrong_number_close/wrong_number_reconfirm_line (all {{lang: str}}), wrong_number_patterns [str], opening_greeting {{lang}} — when set, OUTBOUND calls open with THIS (per language), beating response_bank.opening_greeting. A "no" / "wrong number" to the identity question is ALWAYS re-confirmed once with wrong_number_reconfirm_line (built-in default when empty) before wrong_number_close ends the call; the engine does this, never a script
- guardrails: use_builtin_rules (bool, default true), builtin_rules_pack ("generic" | "real_estate"), never_rules [str], always_rules [str], honesty {{unknown_answer_policy, forbidden_fabrications}}, compliance {{disclaimers, consent_line, dnd_phrases (["stop calling","remove my number"] recommended for new agents), restricted_topics}}, escalation {{enabled, triggers, handoff_message: {{lang}}}}
  - builtin_rules_pack: SET IT EXPLICITLY ON EVERY NEW AGENT. "real_estate" ONLY when the business genuinely sells or leases property — it injects site-visit/property/portfolio rules. For every other business (clinic, coaching class, fabric mill, electronics, SaaS, finance...) use "generic". An agent on the wrong pack is told to push callers toward a "site visit" it does not offer.
  - The caller's OWN OPENING HOURS belong in call_flow.scheduling.days + .window (e.g. days: ["Monday",...,"Friday"], window: "09:00-17:00"). Without them the agent will happily agree to a Sunday appointment. Ask for opening hours whenever the agent books anything.
  - NEVER tell an agent to claim it is human, and never write a never_rule/always_rule/custom_instruction that says it is a "real person" or must "not reveal" that it is an AI. personality.ai_disclosure is the ONLY control (deflect = do not volunteer it; admit / admit_if_pressed = say so). No mode may deny being an AI — if the user asks for one that does, tell them plainly the platform will not claim to be human, and offer "deflect" instead.
  - Placeholders: the ONLY tokens the engine substitutes into spoken text are {{contact_name}}, {{customer_name}}, {{current_time}}, {{honorific}}, {{title}} — single braces. Anything else you invent, or the same name in double braces (e.g. {{{{student_name}}}}), is SPOKEN ALOUD to the caller, braces and all — never write one.
- follow_up: whatsapp_promise {{enabled, assets, promise_line: {{lang}}}}, callback {{enabled, default_delay_hours}}
- calendar: {{"type": "mock", "mock_slots": [{{"day": "Tomorrow", "time": "11:00 AM"}}, {{"day": "Friday", "time": "4:30 PM"}}], day_synonyms, booking_confirmation_line: {{lang}}}}. Times must be REALISTIC and VARIED — never the same time repeated across days. Omit mock_slots entirely (or set type: "none") when the business doesn't book time slots — a phantom calendar steers the agent toward booking behaviour its flow doesn't have.
- post_call: outcomes [{{id, label, kind: success|progress|failure}}], scoring {{enabled, criteria_prompt, score_labels}}
- guardrails.builtin_rules_pack: "generic" (default for new agents — industry-neutral conversation rules) or "real_estate" (set this for real-estate businesses — site-visit/property-focused rules). guardrails.honesty.knowledge_grounding: "strict" (default for new agents) makes the agent deflect instead of inventing when asked about products/prices not in its knowledge — keep it unless the user explicitly wants free improvisation.

WHAT THE LIVE CALL ACTUALLY READS (the injection model — edits that ignore this LOOK successful but change nothing on the phone; a real user's ban was spoken verbatim on the very next call because of it):
- call_flow.call_goal renders on EVERY turn as "━━ CALL MISSION ━━ … Every turn must move the call toward this mission." A stale mission overrides stage-goal edits — any change to the agent's objective, offer, or wording (e.g. dropping "twenty-minute demo") MUST update call_goal too (update_agent; update_stage cannot touch it).
- Only the CURRENT stage's goal is injected per turn. A rule written into one stage's goal is INVISIBLE for the rest of the call. Durable behaviour rules ("never say X", "never suggest times") belong in guardrails.never_rules / always_rules — those are injected on every turn.
- call_flow.scheduling defaults to {{"style": "exact_n", "offer_count": 2}}, which makes the platform FIRM RULES order the agent to "always give exactly TWO specific time options". When the user wants the CALLER to propose the time ("ask them when they're free", "don't suggest times"), you MUST set call_flow.scheduling to {{"style": "flexible"}} — otherwise the platform rules fight the goals. An agent with empty calendar.mock_slots has no times to offer at all; exact_n there is always wrong.
- STAGE-BEAT PLACEMENT: the caller turn that satisfies a stage's advance_when is answered under the NEXT stage's goal. Script meant for "right after the caller confirms" goes at the TOP of the NEXT stage's goal — or raise advance_when.min_turns to 2 so the confirming stage keeps the turn. A confirm_person stage's goal should contain ONLY the confirm beat; anything written after it in that goal is never spoken.
- EARLY-EXIT SCRIPTS NEED THEIR OWN STAGE + BRANCH, never prose inside a shared one: a script shaped "ask X; if NO, ask why then close; if YES, ask Y then Z" is NOT one stage with advance_when.all_collect_filled over both X's and Y's slots — on the NO path Y's slot never fills, so the ONLY exit is max_turns, and the goal text's own "then close" instruction fires turns before that, with NO closing script in view (the model improvises something else entirely — a real 2026-09-08 incident, RUNBOOK §35). Split it: X's own stage branches on when_intent: negation|affirmation to two DIFFERENT stages (the NO path's "ask why" stage, the YES path's "ask Y" stage); a "why"/free-text follow-up stage advances with branches: [{{when_slot: {{name: SLOT}}, goto: NEXT}}] (no `equals` — any answer converges) so the model is NEVER asked to "wrap up"/"advance to closing" without the destination stage's actual script in front of it. If a shared closing stage is the convergence point for several such paths, its goal opens by naming each path's acknowledgment line (readable from CALL STATE) BEFORE the closing script — never a bare "advance to closing"/"wrap up" left for the model to invent.
- A QUOTED LINE find_text CANNOT FIND WAS IMPROVISED, NOT CONFIGURED. When a user pastes something the agent SAID on a call and asks you to change it, find_text it first. Zero hits does not mean "search harder with a shorter substring" and it never means the edit is done — it means no field contains that sentence and the MODEL composed it, usually from a stage goal or the knowledge paragraph nearby. Say that to the user in one plain sentence ("that exact line isn't in your script — the agent improvised it from the WRAP_UP goal"), then fix the cause: put the wording they want into a real field (an objection_playbook answer, the stage goal's verbatim line) so it is scripted rather than composed, and add a guardrails.never_rules entry only as a backstop. What you must NOT do is add a never_rule about a phrase you never located and then report it as "removed" — that tells the user a line was deleted from their configuration when nothing of the sort happened (this exact exchange happened on 2026-09-08, RUNBOOK §37). Report what you actually changed.
- BANNED-PHRASE SWEEP (any "stop saying X" / "remove X" / "never do Y"): get_agent FRESH, then find_text the phrase (and obvious variants) to map every surface — call_goal, every stage goal, objection_playbook answers, portfolio_prompt_text, catalog pitch_points, outbound.* lines, brand.trust_proofs, existing rules. Fix EVERY hit, add a guardrails.never_rules entry for the ban, then find_text AGAIN and only claim success on a 0-hit result — report remaining hits honestly.

THE OPENING LINE & PLACEHOLDERS (get this right — it burned a real user TWICE):
- WHICH FIELD OPENS THE CALL DEPENDS ON DIRECTION. Inbound calls open with response_bank.opening_greeting, word-for-word. Outbound calls open with outbound.opening_greeting for the call's language whenever it is non-empty, and fall back to response_bank.opening_greeting only when it is not — so on an outbound agent, editing response_bank.opening_greeting alone usually changes NOTHING the caller hears (a real user's "Hello?" edit was applied, reported as done, and never spoken, 2026-08-01). When the user talks about the greeting / the first line / "as soon as the call connects": inbound agent → edit response_bank.opening_greeting; outbound agent → edit outbound.opening_greeting in EVERY supported language (and keep response_bank.opening_greeting in step so the fallback matches). Scheduled follow-up calls always open with a platform-composed callback line — no greeting field changes those. Stage goals are INSTRUCTIONS to the AI (always written in English), never a script — editing a stage goal will NOT change what plays at connect.
- VERIFY GREETING EDITS: after any greeting change the tool result carries "hearable" with TWO lines — "opening_next_call" (what the call opens with) and "second_line_next_call" (what the agent says the instant the caller first responds). QUOTE BOTH to the user (e.g. "the next outbound call now starts with: '…', then when they answer she says: '…'"). Use the preview_opening tool whenever the user asks what the agent says at the start of a call, or to double-check a greeting edit took. get_agent's spoken_surfaces.opening_line_next_call shows the same engine-resolved truth, second line included.
- Exactly FOUR placeholders exist, all single curly braces, and they work in ANY authored spoken line — opening_greeting, outbound.* lines, call_flow.connect.intro_line, closing_responses, canned_phrases, objection_playbook answers, graceful_close — AND inside the verbatim script lines a stage goal quotes (the platform resolves them for each call before the model reads the goal):
  * {{contact_name}} — on outbound calls, replaced with the dialed contact's name from Outbound Calls. A contact with NO name on file: a clause that only asks who they are is dropped ("Hello, am I speaking with {{contact_name}}?" is spoken as "Hello.", and if nothing is left the platform opens with the agent's own intro), and anywhere else the token simply disappears ("Thank you {{contact_name}}!" → "Thank you!"). It is NEVER replaced with "sir/ma'am". When the user wants the agent to greet whoever it calls by name, write e.g. "Hello, am I speaking with {{contact_name}}?"
  * {{current_time}} — replaced at call time with morning/afternoon/evening (India time). E.g. "Good {{current_time}}, am I speaking with {{contact_name}}?" becomes "Good morning, am I speaking with Rajesh?". If the greeting is written in Hindi or Gujarati script, the word comes out in that language automatically.
  * {{honorific}} — "Sir" or "Ma'am" from the contact's Gender. Stands ALONE: "Yes {{honorific}}", "Thank you {{honorific}}".
  * {{title}} — "Mr." or "Mrs." from the same Gender. Goes BEFORE THE NAME and nowhere else: "{{title}} {{contact_name}}". Never write "{{title}}" on its own — "Thank you Mr." is not a sentence.
  * BOTH GENDERED TOKENS ARE OPTIONAL DATA, and this is the thing to get right. Gender is an optional column in the contact upload (a "Gender" or "Sex" header; male/female/M/F), MOST CONTACT LISTS DO NOT HAVE IT, and a contact without one is simply never addressed by title — the token and the gap it leaves are removed. So "Good {{current_time}} {{honorific}}, am I speaking with {{title}} {{contact_name}}?" is spoken as "Good morning Sir, am I speaking with Mr. Rajesh?" for a contact marked male, and "Good morning, am I speaking with Rajesh?" for a contact with no gender. In Hindi/Gujarati they come out as सर/मैडम, श्री/श्रीमती (સર/મેડમ, શ્રી/શ્રીમતી) automatically. SAFE TO ADD BY DEFAULT for that reason — but tell the user they only speak for contacts whose sheet had a gender column, and NEVER write a line whose grammar collapses without them (e.g. "Am I speaking to {{honorific}}?" reads as "Am I speaking to?"). They are a courtesy, never a substitute for the name.
  * The agent ALSO applies gendered address to its OWN improvised sentences for the whole call whenever the gender is known — the placeholders are only for the lines the user writes out by hand. Do not add guardrails rules telling it to say "Sir"; that is automatic.
  * GENDERED ADDRESS IS ALWAYS DYNAMIC — PLATFORM-WIDE, NO EXCEPTIONS. NEVER write a literal Sir / Ma'am / Madam / Mr. / Mrs. (or सर / मैडम / श्री / श्रीमती) into a spoken line: write {{honorific}} / {{title}}. The platform converts any literal you leave into the placeholder on save, and at call time holds EVERY Sir/Ma'am the agent says — scripted, canned or improvised — to that contact's Gender: the right word when it is known, and NO gendered word at all when it is not (the agent uses the name, or 'ji'). A fixed "Sir" for everyone does not exist on this platform; do not offer one.
  * THE GENDER-DATA REMINDER — REQUIRED. Whenever the user explicitly asks for Sir/Ma'am, Madam, Mr./Mrs., सर/मैडम or any honorific in the script, do it with {{honorific}}/{{title}} AND tell them plainly, in your reply: this is dynamic — it only works for contacts that have a Gender in their data; add a Gender column (male/female or M/F) to the contact upload, set gender in the API, or pick it in Add Contact; contacts WITHOUT a gender are addressed by name only — never guessed, never a fixed Sir. The server checks your reply for this and appends it if you leave it out, so say it yourself, once, in your own words.
- THE AGENT'S OWN GENDER FOLLOWS ITS VOICE (platform-wide, every agent). Pick a male voice (shubh, ratan, aditya, …) and the agent speaks as a man — pronouns he, Hindi "kar raha hoon", "samajh gaya", Gujarati "રહ્યો છું"; a female voice (simran, priya, ishita, …) and it speaks as a woman — "kar rahi hoon", "samajh gayi", "રહી છું". This is applied in the prompt AND to every scripted line at speak time, automatically; there is NO pronouns setting to change (personality.pronouns / identity.pronouns are derived from the voice and anything you write there is overwritten). So: to change the agent's gender, change the voice — and never rewrite scripts to switch verb gender, the platform does it. When the user switches the voice to the other gender, say in one sentence that the agent's own grammar now follows automatically; if the agent's NAME is plainly the other gender (e.g. "Jai Mistry" with a female voice), point it out and offer to rename it — never rename silently.
- NEVER invent any other placeholder or syntax — ${{...}}, {{{{...}}}}, [name], <contact>, {{date}} do not exist and would be SPOKEN ALOUD literally. Placeholders in the knowledge text are NOT substituted (that text is shared by every call; a gendered token there is simply removed) — put per-caller wording in a spoken field or a stage goal's verbatim line instead.
CALL-REPAIR BEHAVIOUR THE PLATFORM APPLIES TO EVERY AGENT (know this before you "fix" a greeting — these are NOT settings and you cannot edit them):
- Bare "hello?" from the caller within ~25s of the opening line: the agent replays its last utterance (up to twice), on the assumption the caller didn't hear it. This deliberately does NOT fire when the opening line is itself just "Hello?" — a hello answering a hello means contact is established, so the agent moves straight on to its script.
- ~6s of caller silence: the agent says it couldn't hear and re-asks.
- ENDING CALLS (platform-wide): before wrapping up, every agent asks the caller's permission ("anything else I can help you with?") and only says goodbye after the caller confirms they're done — then the PLATFORM hangs the call up automatically after the goodbye. Because speech recognition mishears goodbyes ("okay, by the way..." → "okay bye"), the platform ALSO asks one short confirmation — "alright then, shall I end the call here?" (in the call's language) — before acting on any heard goodbye, and only treats a goodbye as one at all when it ENDS the caller's sentence (a "bye" with an answer after it is a mis-transcription, not a hang-up); after a clear yes it speaks a brief thank-you ("thank you for talking with me — have a wonderful day") and hangs up. This is on for every agent by default (call_flow.termination.confirm_before_end) and needs no configuration; per-agent copy overrides live in response_bank.localized_responses under 'close_confirm' and 'close_confirmed'. The platform also ends the call on the caller's own goodbye, after the couldn't-hear reprompts go unanswered (silence hangup), and — by default — whenever the agent itself judges the conversation is over per its END-CALL RULES (see call_flow.termination).
- CALLBACK REQUESTS & BUSY CALLERS (platform-wide): when a caller says "call me later / in 10 minutes / tomorrow" or brushes off with "sorry, not free right now", every agent stops pitching, pins down ONE specific time, confirms it with a goodbye, and the platform HANGS UP the call by itself right after that confirmation — then automatically places a real follow-up call at the agreed time, opening with a polite reference to the earlier conversation. This is on for all agents by default; a business can opt an agent out of the automatic calling (follow_up.auto_execute enabled false) but the time pin-down stays.
- CALL-ENDING REQUIREMENTS GO IN call_flow.termination, NOT IN PROMPT TEXT: when a user asks for anything about cutting/ending/hanging up calls — "end the call once the booking is done", "hang up if they abuse the agent", "don't let calls run past 10 minutes", "cut the call when they say chalo rakhta hu" — map it into call_flow.termination (end_conditions for judgment-based rules, farewell_patterns for exact caller phrases, max_call_minutes for duration caps, silence_hangup for no-response behaviour). That section is real engine wiring: the platform physically ends the call from it. A guardrails rule or stage-goal sentence saying "end the call" does NOTHING by itself — never implement a termination request as prompt text alone, and never claim call-cutting behaviour you did not write into termination. Do NOT add rules like "never reschedule / never ask for a better time", which fight the platform's callback pin-down. After setting it, tell the user plainly which of their conditions now end the call.
- OPENING STYLE — SHORT GREETING, THEN WAIT (platform-wide): a short opening like "Hello" is the intended default. The agent greets, WAITS for the caller, and the moment the caller responds the agent speaks a SECOND LINE — instantly, with no LLM turn and cached audio. That second line IS SETTABLE WORD-FOR-WORD: call_flow.connect.intro_line, a per-language map ({{"en": "...", "hi": "..."}}) where {{contact_name}} IS substituted. Leave it empty and the platform composes one from identity.agent_name + identity.company_name. On OUTBOUND agents, outbound.identity_confirm_line is ALSO spoken as that second line when no intro_line is set — so a business that already wrote an identity line hears its own words, not the generic one. WHEN THE USER DICTATES WHAT THE AGENT SAYS AFTER THE CALLER FIRST SPEAKS OR SAYS HELLO ("say Hello, wait for them, THEN say ..."), that sentence goes in call_flow.connect.intro_line — NOT in response_bank.opening_greeting (that is the FIRST line, spoken before the caller has said anything) and NEVER in a stage goal (stage goals are instructions to the AI and are never spoken). This is engine wiring, so a short opening does NOT stall or loop the call. NEVER rewrite a user's opening — not short-to-long, not long-to-short — unless they ask for different words. A business that wants the full intro spoken up front simply writes the full line into the opening field its direction reads (response_bank.opening_greeting for inbound, outbound.opening_greeting for outbound); both styles are fully supported. For agents with a short opening, make sure identity.agent_name and identity.company_name are filled in — the connect intro is composed from them. Connect-window protections (call_flow.connect: hello_fastpath, delivery_guard) default ON for every agent — configure them only when the business explicitly asks.
So if a user reports "it keeps repeating hello" or "it won't start its script", explain what actually happens rather than guessing — and do NOT rewrite their opening line to dodge a platform rule unless they want different words. Changing a greeting they liked, to work around behaviour you misdiagnosed, is a failure.
If a tool result includes "notes", the platform auto-corrected or DROPPED some of your input — read them; if content was dropped, put it where the note says (usually a stage goal) with update_agent in the SAME turn.
If a tool result includes "reachability", the write landed on a field this agent's calls DO NOT SPEAK — the change is invisible on the phone. Fix it in the SAME turn (edit the field the warning names) or tell the user plainly that nothing will sound different; NEVER report a change as done while a reachability warning stands unaddressed.
If a tool result includes "twin", you edited only ONE direction of a twin pair and the other still differs on those fields — tell the user which direction you changed and offer to match the twin when the change is direction-neutral (wording, rules, knowledge). Never mirror silently.
WHEN THE USER SAYS "both" (both scripts, both directions, inbound and outbound): pass both_directions: true on update_agent and it lands on BOTH twins in ONE step — do not spend two get_agent + two update_agent rounds doing it by hand. The result carries "twin_write": if applied is false, read its "reason" and tell the user plainly which direction did NOT change. Greetings, outbound.* fields and call type are direction-SPECIFIC and are refused on the twin by design — edit those per direction.
CONFIRMATIONS ARE EXACT: when you ask the user to confirm a proposed change, list exactly what THAT pending change contains — never bundle already-applied items into the same "shall I apply?" question (say "the pace and language changes are already applied; this applies only the greeting"). If a proposal was lost and you re-propose it, show the new diff again before applying.
If a tool result includes non-empty "quality" warnings, the agent is too shallow — you MUST resolve every one with update_agent in the SAME turn before telling the user it's ready. Do not report a hollow agent as done.
WHEN THE USER SAYS A CHANGE "isn't in the agent" or "wasn't implemented": do not just re-word the same field. get_agent, and translate their intent into the fields the CALL actually reads — call_flow.call_goal (the every-turn CALL MISSION), call_flow.scheduling (time-offer behaviour), call_flow.stages goals AND knowledge_base.portfolio_prompt_text (both are spoken), guardrails rules. The pitch is not "implemented" if it only sits in one thin stage goal, and a ban is not implemented while call_goal or a platform scheduling default still orders the old behaviour. Enrich those, then confirm what specifically changed.

TRUTHFULNESS (ABSOLUTE — violations are the worst possible failure):
- SUMMARIES ARE READ, NEVER INVENTED: when you describe the agent's plan/objective/direction to the user ("here is a summary", "shall I build it?"), every claim must come from the CURRENT draft or tool results — its call_direction, call_flow.call_goal, stages and languages — never from generic boilerplate. Telling a user their OUTBOUND sample-kit sales agent will "handle customer inquiries" and "answer inbound calls" (a real incident) makes them doubt everything that was actually captured correctly.
- Only claim something was created/updated/activated if a tool result in THIS turn confirms it (status created/updated/activated/replaced). A tool result with status FAILED means NOTHING happened.
- After a FAILED result: fix the input and retry in the same turn if you can; otherwise tell the user plainly it didn't work. NEVER re-send an earlier success message after a failure.
- Never say "it's set up" / "it's activated" / "it will now..." unless you just did it, successfully, with a tool.
- After creating an agent it appears in the dashboard's Agents tab immediately — if the user says they don't see it, list the agents to check whether it truly exists before saying anything else.

STYLE — PLAIN LANGUAGE ONLY (the user is a business person, not a developer):
- Replies are SHORT: 1-3 sentences, at most ONE question, plain conversational text. No markdown headers, no YAML/JSON at the user, no bullet walls (a brief 3-5 line summary before creating is fine).
- NEVER mention tools or tool names (update_agent, get_agent, replace_mentions...), YAML, JSON, "definition", "partial changes", "deep-merge", "knowledge base fields", slugs, caching, or ANY internal machinery. Say "I'll update her greeting", "Done — Ananya now introduces herself as Mr. Umang's assistant", never "I'll use the update_agent tool to apply these changes".
- Never tell the user to run a tool or "check the agent definition" — YOU do the checking and report the result.
- Describe agents by what callers experience: the greeting, how she speaks, what she knows, when she books meetings.
- When the user says they're done, close warmly in one sentence — do not claim any additional update happened.
- NEVER write out an imagined transcript of a future call ("The conversation will proceed as follows: ..."). It reads as fact, goes stale instantly, and hides what actually changed. State in 1-2 sentences WHAT changed; quote at most the exact opening greeting.
- Never invent agents or facts — check with tools first, silently.{interview_block}"""


# ─────────────────────────────────────────────────────────────────────────────
# Sessions
# ─────────────────────────────────────────────────────────────────────────────
_SESSIONS: dict[str, dict] = {}  # sid -> {"messages": [...], "transcript": [...], "meta": {...}}

# One turn at a time per session: two concurrent messages used to interleave
# their tool/assistant messages in the shared history and answer each other's
# questions (both replies to "what languages?" + "what greeting?" answered the
# greeting). The second message queues behind the first instead.
_SESSION_LOCKS: dict[str, "asyncio.Lock"] = {}


def _session_lock(sid: str) -> "asyncio.Lock":
    import asyncio as _asyncio

    lock = _SESSION_LOCKS.get(sid)
    if lock is None:
        lock = _SESSION_LOCKS.setdefault(sid, _asyncio.Lock())
    if len(_SESSION_LOCKS) > 2000:  # unbounded-growth guard
        for k in [k for k, v in list(_SESSION_LOCKS.items())
                  if not v.locked()][:1000]:
            _SESSION_LOCKS.pop(k, None)
    return lock

# Tools that tie a conversation to a specific agent. Mutating tools always
# re-point the session's agent; read tools only set it when none is known yet
# (so "also show me pramukh" mid-edit doesn't rename the chat).
_AGENT_MUTATING_TOOLS = {"create_agent", "update_agent", "activate_agent", "set_routing",
                         "replace_mentions", "apply_update", "update_stage", "add_stage",
                         "remove_stage", "reorder_stages", "update_slot", "add_slot",
                         "remove_slot", "rollback_agent"}
_AGENT_READING_TOOLS = {"get_agent", "get_routing"}


def _session_path(sid: str) -> Path:
    return _SESSIONS_DIR / f"{sid}.json"


def _find_uploaded_doc(sid: str, doc_id: str, owner: str | None) -> Path | None:
    """Locate an uploaded document for this conversation.

    Primary: this session's own upload folder. Fallback: another folder of the
    SAME owner — covers a doc attached moments before the user hit "New chat",
    or a session id that got remapped between upload and ingest. Never serves
    a document from a folder whose session belongs to someone else.
    """
    p = _UPLOADS_DIR / sid / doc_id
    if p.exists():
        return p
    if not owner:
        return None
    try:
        candidates = sorted(_UPLOADS_DIR.glob(f"*/{doc_id}"),
                            key=lambda c: c.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for cand in candidates:
        cand_sid = cand.parent.name
        if cand_sid != sid and _session_owner(cand_sid) == owner:
            logger.info("[copilot] doc %s served from sibling session %s -> %s",
                        doc_id, cand_sid, sid)
            return cand
    return None


def _session_owner(sid: str) -> str | None:
    """Owner recorded on a session's meta (None for legacy/never-saved)."""
    try:
        if sid in _SESSIONS:
            return _SESSIONS[sid].get("meta", {}).get("owner")
        path = _session_path(sid)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8")) or {}
            return data.get("meta", {}).get("owner")
    except Exception:
        pass
    return None


def _session_exists(sid: str) -> bool:
    return sid in _SESSIONS or _session_path(sid).exists()


def _tenant_sid(owner: str | None, sid: str) -> str:
    """A stable per-tenant session id derived from a stale/foreign one, so a
    client holding an inaccessible session id gets its OWN continuous chat
    instead of an error — and never reaches the original session."""
    import hashlib
    h = hashlib.sha1(f"{owner or ''}\x00{sid}".encode("utf-8")).hexdigest()[:20]
    return f"u{h}"


def _session_owner_ok(sid: str, owner: str) -> bool:
    """Who may open a session.

    * admin: any session.
    * a session that does NOT exist yet: claimable — this request creates it.
    * an existing session: ONLY its recorded owner.

    A legacy session with no owner (owner=None) is treated as the platform
    admin's — NOT visible to tenants. (The old code let owner=None be seen by
    everyone, which leaked every pre-tenancy chat to all tenants.)
    """
    import auth
    if auth.is_admin(owner):
        return True
    if not _session_exists(sid):
        return True
    return _session_owner(sid) == owner


def _load_session(sid: str) -> dict:
    if sid in _SESSIONS:
        return _SESSIONS[sid]
    session = {"messages": [], "transcript": [], "meta": {}}
    path = _session_path(sid)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
            session["messages"] = data.get("messages", [])
            session["transcript"] = data.get("transcript", [])
            session["meta"] = data.get("meta", {})
            if data.get("pending_changes"):
                session["pending_changes"] = data["pending_changes"]
            # Must survive the reload: restarting the counter would re-issue
            # change ids that diff cards from before the reload still point at.
            if data.get("change_seq"):
                session["change_seq"] = data["change_seq"]
            if data.get("interview"):
                session["interview"] = data["interview"]
            if data.get("documents"):
                session["documents"] = data["documents"]
            if data.get("owner"):
                session["owner"] = data["owner"]
        except Exception as exc:  # pragma: no cover - corrupt file: start fresh
            logger.warning("[copilot] could not load session %s: %r", sid, exc)
    _SESSIONS[sid] = session
    return session


def _now_iso() -> str:
    import datetime
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _agent_display_name(slug: str) -> str:
    """Pretty title for a chat tied to an agent: its spoken name from the YAML."""
    try:
        agent = agent_registry.get(slug, _AGENTS_DIR)
        return agent.identity.agent_name or slug
    except Exception:
        return slug


def _session_summary(sid: str, session: dict, mtime: float | None = None) -> dict:
    import datetime

    meta = session.get("meta") or {}
    transcript = session.get("transcript") or []
    agent = meta.get("agent") or ""
    title = meta.get("title") or ""
    if not title:
        if agent:
            title = _agent_display_name(agent)
        else:
            first_user = next((m.get("content", "") for m in transcript if m.get("role") == "user"), "")
            title = (first_user[:48] + "…") if len(first_user) > 48 else (first_user or "New chat")
    updated = meta.get("updated_at")
    if not updated and mtime:
        updated = datetime.datetime.utcfromtimestamp(mtime).isoformat(timespec="seconds") + "Z"
    return {
        "session_id": sid,
        "title": title,
        "agent": agent or None,
        "agent_name": _agent_display_name(agent) if agent else None,
        "created_at": meta.get("created_at"),
        "updated_at": updated,
        "message_count": sum(1 for m in transcript if m.get("role") == "user"),
    }


def _save_session(sid: str) -> None:
    try:
        _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        _session_path(sid).write_text(
            json.dumps(_SESSIONS[sid], ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception as exc:  # pragma: no cover - persistence is best effort
        logger.warning("[copilot] could not persist session %s: %r", sid, exc)


def _context_window(messages: list[dict]) -> list[dict]:
    """Trim long sessions; cut at a user-message boundary so assistant
    tool_calls are never separated from their tool results. Old bulky tool
    results (full agent definitions, ~6KB each) are compacted to keep the
    request inside Groq's tokens-per-minute budget."""
    if len(messages) > _CONTEXT_MAX_MESSAGES:
        tail = messages[-_CONTEXT_MAX_MESSAGES:]
        for i, m in enumerate(tail):
            if m.get("role") == "user":
                tail = tail[i:]
                break
        else:
            tail = list(tail)
        # No user boundary found (or boundary search left orphans): drop
        # leading tool results / tool-calling assistant turns whose parents
        # were cut off — the API rejects tool messages without their parent.
        while tail and (
            tail[0].get("role") == "tool"
            or (tail[0].get("role") == "assistant" and tail[0].get("tool_calls"))
        ):
            tail.pop(0)
    else:
        tail = list(messages)

    # Index of the current turn's user message (last user msg): tool results
    # from THIS turn stay full; older bulky tool dumps get compacted.
    last_user = max((i for i, m in enumerate(tail) if m.get("role") == "user"), default=0)
    out: list[dict] = []
    for i, m in enumerate(tail):
        content = m.get("content") or ""
        if m.get("role") == "tool" and i < last_user and len(content) > 900:
            m = {**m, "content": content[:900] + " …[truncated — call the tool again if you need the full data]"}
        out.append(m)
    return out


_TOOLS_CHARS: int | None = None


def _tools_chars() -> int:
    global _TOOLS_CHARS
    if _TOOLS_CHARS is None:
        _TOOLS_CHARS = len(json.dumps(TOOLS))
    return _TOOLS_CHARS


def _approx_tokens(messages: list[dict]) -> int:
    """Conservative request-size estimate (chars/3.5 over-counts vs Groq's
    tokenizer, which is the safe direction). Tool schemas ride on every
    request, so they are part of the floor."""
    chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
    return int((chars + _tools_chars()) / 3.5)


def _int_env(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _fit_messages(messages: list[dict], budget_tokens: int) -> list[dict]:
    """Shrink a request to fit a model's per-minute token ceiling: compact old
    tool results, then drop the oldest turns. The system prompt and the
    CURRENT turn (last user message onward) are never dropped. Safe for the
    interview: its ground truth is the server-side draft, not chat history."""
    if _approx_tokens(messages) <= budget_tokens or len(messages) < 2:
        return messages
    system, tail = messages[0], [dict(m) for m in messages[1:]]
    last_user = max((i for i, m in enumerate(tail) if m.get("role") == "user"), default=0)
    for i in range(last_user):
        c = tail[i].get("content") or ""
        if tail[i].get("role") == "tool" and len(c) > 300:
            tail[i] = {**tail[i], "content": c[:300] + " …[truncated]"}
    while _approx_tokens([system] + tail) > budget_tokens:
        last_user = max((i for i, m in enumerate(tail) if m.get("role") == "user"), default=0)
        if last_user == 0:
            break  # only the current turn left
        tail.pop(0)
        # never leave orphaned tool results / tool-calling assistants in front
        while tail and (
            tail[0].get("role") == "tool"
            or (tail[0].get("role") == "assistant" and tail[0].get("tool_calls"))
        ):
            tail.pop(0)
    return [system] + tail


def _fallback_model_name() -> str:
    """Second Groq model used when the primary exhausts its daily token quota.
    Groq rate limits are PER MODEL, so a sibling model has its own fresh
    tokens-per-day bucket. Empty string disables the fallback."""
    # llama-3.3-70b shares its daily budget with live calls, but it is the only
    # account model whose per-minute limit fits a full copilot request; it is
    # only touched when the primary model's quota is already exhausted.
    return _env("LLM_FALLBACK_MODEL", "llama-3.3-70b-versatile")


_oai_client = None


def _openai_fallback() -> tuple[Any, str] | None:
    """Last-resort cross-provider fallback: OpenAI with the voice pipeline's
    existing OPENAI_API_KEY (already used for Whisper). Paid per-token, no
    daily quota — used only when every Groq bucket is dry."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = _env("LLM_FALLBACK2_MODEL", "gpt-4o-mini")
    if not key or not model:
        return None
    global _oai_client
    if _oai_client is None:
        from openai import AsyncOpenAI
        _oai_client = AsyncOpenAI(api_key=key, timeout=120.0, max_retries=3)
    return _oai_client, model


def _adapt_tool_calls_for_provider(messages: list[dict], cli) -> list[dict]:
    """Gemini 3 (OpenAI-compat) requires every assistant functionCall in
    history to carry extra_content.google.thought_signature and 400s
    without it; other providers may reject the unknown field. Adapt a
    COPY of the history per provider — the session store is not touched.
    Legacy history entries (recorded before signatures were preserved)
    get the migration placeholder Google documents for this case."""
    is_gemini = "generativelanguage" in str(getattr(cli, "base_url", "") or "")
    out: list[dict] = []
    for m in messages:
        tcs = m.get("tool_calls") if isinstance(m, dict) else None
        if not tcs:
            out.append(m)
            continue
        m2 = dict(m)
        new_tcs = []
        for tc in tcs:
            tc2 = copy.deepcopy(tc)
            if is_gemini:
                extra = tc2.setdefault("extra_content", {})
                if isinstance(extra, dict):
                    goog = extra.setdefault("google", {})
                    if isinstance(goog, dict) and not goog.get("thought_signature"):
                        goog["thought_signature"] = "context_engineering_is_the_way_to_go"
            else:
                tc2.pop("extra_content", None)
            new_tcs.append(tc2)
        m2["tool_calls"] = new_tcs
        out.append(m2)
    return out


async def _chat_completion(client, model: str, messages: list[dict],
                           trace: dict | None = None, owner=None,
                           spend: dict | None = None):
    """One LLM call with retries for Groq's `tool_use_failed` (the model emitted
    a malformed/truncated tool call — a known llama flakiness). On failure we
    re-ask with a corrective nudge appended; the nudge is never persisted.
    On a 413/429 (request over the model's TPM ceiling / quota exhausted) the
    call walks a fallback chain: sibling Groq model (separate per-model
    quota), then OpenAI gpt-4o-mini. Every Groq attempt is FITTED to that
    model's tokens-per-minute ceiling first (Groq counts input + max_tokens
    per request, per model, per org) — a request that cannot fit is never
    sent, so a 413 on one model can't kill the whole turn anymore.

    `trace` (optional dict) records WHICH model actually answered and every
    fallback that fired. Before 2026-08-04 a silent downgrade left no trace
    at all: on 2026-07-15 the chain dropped to gpt-4o-mini and it authored a
    whole agent definition — fabricated projects, brand facts and phone
    numbers — and the only way to establish that afterwards was forensics on
    the tool-call id alphabet. Never again."""
    from openai import APIStatusError, BadRequestError

    async def _attempt(cli, mdl: str, msgs: list[dict], max_toks: int):
        msgs = _adapt_tool_calls_for_provider(msgs, cli)
        # Gemini models think by default and thinking tokens count against
        # max_tokens — cap it via COPILOT_LLM_REASONING_EFFORT / LLM_REASONING_EFFORT.
        # Gemini-only: OpenAI fallback models (gpt-4o-mini) reject the param.
        _extra: dict = {}
        _reff = _env("LLM_REASONING_EFFORT")
        if _reff and mdl.startswith("gemini"):
            _extra["reasoning_effort"] = _reff
        nudge: list[dict] = []
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                completion = await cli.chat.completions.create(
                    model=mdl,
                    messages=msgs + nudge,
                    tools=TOOLS,
                    tool_choice="auto",
                    # temperature 0 on retries: a malformed tool call is a
                    # sampling flake — determinism beats creativity here.
                    temperature=0.3 if attempt == 0 else 0.0,
                    max_tokens=max_toks,
                    **_extra,
                )
                # Charged HERE, inside the attempt loop, rather than at the
                # call site: a nudged retry and a walk down the fallback chain
                # are additional real requests against the shared model quota,
                # and a budget that only counts the successful one would let a
                # tenant provoking malformed tool calls spend three times what
                # it records. Done before the return so no path skips it.
                _charged = _charge_tokens(owner, getattr(completion, "usage", None))
                if spend is not None:
                    spend["tokens"] = spend.get("tokens", 0) + _charged
                return completion.choices[0].message
            except BadRequestError as exc:
                if "tool_use_failed" not in str(exc):
                    raise
                last_exc = exc
                nudge = [{
                    "role": "system",
                    "content": (
                        "Your previous tool call was REJECTED because its JSON was malformed or "
                        "truncated. Call the tool again with strictly valid, COMPLETE JSON. "
                        "Keep the definition compact — rely on platform defaults for anything "
                        "you don't need to customize."
                    ),
                }]
        raise last_exc  # type: ignore[misc]

    # (client, model, max_tokens, input budget; 0 budget = send as-is).
    # Primary budget is HALF the TPM so a multi-iteration tool loop (2+ LLM
    # calls per user turn, same minute, same model) still fits the ceiling.
    primary_tpm = _int_env("LLM_TPM", 30000)
    primary_max = _int_env("LLM_MAX_TOKENS", 3000)
    fb_tpm = _int_env("LLM_FALLBACK_TPM", 12000)
    fb_max = _int_env("LLM_FALLBACK_MAX_TOKENS", 2000)
    chain: list[tuple[Any, str, int, int]] = [
        (client, model, primary_max, max(8000, primary_tpm // 2 - primary_max - 500))]
    fallback = _fallback_model_name()
    if fallback and fallback != model:
        chain.append((client, fallback, fb_max, max(7000, fb_tpm - fb_max - 500)))
    oai = _openai_fallback()
    if oai:
        chain.append((*oai, 4096, 0))

    last_chain_exc: Exception | None = None
    for i, (cli, mdl, max_toks, budget) in enumerate(chain):
        msgs = _fit_messages(messages, budget) if budget else messages
        try:
            _msg = await _attempt(cli, mdl, msgs, max_toks)
            if trace is not None:
                trace["model"] = mdl
                trace["reasoning_effort"] = _env("LLM_REASONING_EFFORT") or ""
                if i:
                    trace.setdefault("fallbacks", []).append(
                        {"from": chain[i - 1][1], "to": mdl})
            return _msg
        except APIStatusError as exc:
            status = getattr(exc, "status_code", None)
            # Walk the chain on: token-limit failures (413/429) AND a model
            # that can't produce valid tool JSON after 3 nudged tries (400
            # tool_use_failed — a per-model weakness; the next model usually
            # emits it fine). Anything else is a real bug — raise it.
            if status not in (413, 429) and not (
                    status == 400 and "tool_use_failed" in str(exc)):
                raise
            last_chain_exc = exc
            if i + 1 < len(chain):
                logger.warning("[copilot] %s failed (%s%s) — retrying on fallback %s",
                               mdl, status,
                               " tool_use_failed" if status == 400 else "",
                               chain[i + 1][1])
    raise last_chain_exc  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/api/copilot/chat")
async def copilot_chat(request: Request) -> dict:
    """One conversational turn. Runs the agentic tool loop server-side."""
    body = await request.json()
    sid = str(body.get("session_id") or "").strip()
    message = str(body.get("message") or "").strip()
    if not _SESSION_ID_RE.match(sid):
        raise HTTPException(status_code=400, detail="invalid session_id")
    if not message:
        raise HTTPException(status_code=400, detail="'message' is required")
    # E9 — the copilot's LLM budget is SHARED with live calls (COPILOT_LLM_TPM
    # and the provider's daily ceiling), so an unmetered chat lets one tenant
    # degrade every other tenant's phone calls at zero cost to themselves.
    # A daily turn AND token budget per plan; refusal is a 402 the SPA renders
    # inline. The size estimate is the raw message only — _approx_tokens()
    # would fold in the tool schemas that ride every request and report a
    # one-word "hi" as thousands of tokens, refusing it on a per-message cap.
    _billing_llm_gate(request, "copilot_chat",
                      request_tokens=int(len(message) / 3.5))
    # v2: attached documents ride along as doc ids; the LLM ingests them via
    # ingest_document (the note is machine-added, not user text).
    attachments = [str(a) for a in (body.get("attachments") or [])
                   if re.fullmatch(r"[A-Za-z0-9_\-.]{1,120}", str(a))]
    if attachments:
        message += "\n\n[The user attached document(s): " + \
            ", ".join(f"doc_id={a}" for a in attachments) + \
            " — call ingest_document on each to read them into the draft.]"

    # session_id arrives in the BODY, so the router dependency (which only sees
    # path params) can't gate it — enforce ownership here. Blocks continuing or
    # reading another tenant's chat.
    #
    # But a client may hold a STALE session id (a pre-tenancy chat, or one left
    # by another account on a shared browser). Instead of erroring, transparently
    # remap it to THIS tenant's own stable session derived from that id — so even
    # an old cached frontend keeps working, with full continuity, and never
    # touches the foreign/legacy chat. The response carries the real (possibly
    # remapped) session id for the client to adopt.
    owner = getattr(request.state, "owner", None)
    if not _session_owner_ok(sid, owner):
        sid = _tenant_sid(owner, sid)

    async with _session_lock(sid):
        return await _copilot_chat_turn(request, sid, owner, message,
                                        attachments)


async def _copilot_chat_turn(request: Request, sid: str, owner,
                             message: str, attachments: list) -> dict:
    client = _get_client()
    model = _model_name()
    session = _load_session(sid)
    # Stamp the tenant on the session so it's scoped from its first turn.
    session.setdefault("meta", {}).setdefault("owner", owner)
    # Snapshot for exact rollback — popping single messages on error can
    # orphan an assistant tool_calls message and brick the session history.
    mark_messages = len(session["messages"])
    mark_transcript = len(session["transcript"])
    session["messages"].append({"role": "user", "content": message})
    session["transcript"].append({"role": "user", "content": message})

    actions: list[dict] = []
    agents_changed = False
    reply = ""
    guard = {
        "user_turns": sum(1 for m in session["messages"] if m.get("role") == "user"),
        "created_this_turn": False,
        # v2 edit engine: pending-change storage + confirmation gating.
        "session": session,
        "user_message": message,
        "sid": sid,
        "owner": owner,
    }

    # Deterministic dump mining: a large paste during an agent build is run
    # through the SAME extractor as an attached document BEFORE the chat model
    # sees the turn, so absorbing it never depends on the model's diligence.
    # Fires whenever the session is not editing an existing agent — the first
    # live test (Kaapd brief, 2026-07-15) pasted the dump as the SECOND
    # message, before anything was recorded, and a draft-or-first-turn guard
    # wrongly skipped it. Extraction failure degrades silently to the
    # prompt-driven mining that exists today.
    if _should_mine_dump(session, message, attachments):
        try:
            import asyncio as _asyncio
            from backend.app.documents.yaml_filler import extract_fields_from_text
            _res = await _asyncio.get_running_loop().run_in_executor(
                None, extract_fields_from_text, message)
            if _res.get("status") == "ok" and _res.get("fields"):
                _ch, _notes, _prog, _closed = _merge_extracted_into_draft(
                    session, _res["fields"])
                if _ch:
                    _closed_txt = (" Topics now closed: " + ", ".join(_closed) + ".") if _closed else ""
                    session["messages"].append({
                        "role": "system",
                        "content": "[The platform already extracted the user's pasted "
                                   "content into the draft — recorded: "
                                   + ", ".join(sorted(_ch.keys())) + "."
                                   + _closed_txt
                                   + ((" Notes: " + " | ".join(_notes)) if _notes else "")
                                   + " Do NOT re-ask about these topics and do NOT "
                                     "re-record the same content; acknowledge what was "
                                     "absorbed and ask only about what is still open.]"})
                    actions.append({"tool": "mine_dump", "ok": True, "agent": "",
                                    "summary": "Absorbed the pasted content into the draft"
                                               + ((" — closed: " + ", ".join(_closed)) if _closed else "")})
        except Exception:
            logger.warning("[copilot] dump auto-mining failed (falling back to "
                           "prompt-driven mining)", exc_info=True)

    _rounds = 0
    _charged = 0
    _read_signatures: dict[str, int] = {}
    # Which model actually served this turn, and every silent downgrade.
    _llm_trace: dict = {}
    # The daily budget is checked ONCE, before the turn. That is the right
    # place for it — but a turn is a tool loop of up to _MAX_TOOL_ROUNDS full
    # requests, so a tenant one token short of the ceiling could still spend
    # twenty rounds' worth in a single send. This bounds that overshoot to one
    # round WITHOUT a database read per iteration: the remaining budget is
    # fetched once here and drawn down locally from the usage each round
    # already reports.
    _budget_left = _remaining_tokens(owner)
    _spend: dict = {"tokens": 0}
    _budget_stop = ""
    try:
        while _charged < _MAX_TOOL_ITERATIONS and _rounds < _MAX_TOOL_ROUNDS:
            if _budget_left is not None and _spend["tokens"] >= _budget_left:
                # Stop the loop, keep the turn. Whatever the tools already did
                # is saved; raising here would discard completed work and the
                # user would be charged for it anyway.
                _budget_stop = (
                    "I have used up today's AI-assistant allowance part-way "
                    "through this request, so I stopped here. Anything I "
                    "already saved is saved. The allowance resets at midnight "
                    "IST — or upgrade from Billing for a higher one.")
                logger.info("[copilot] token budget exhausted mid-turn "
                            "(owner=%s spent=%s left=%s)",
                            owner, _spend["tokens"], _budget_left)
                break
            _rounds += 1
            msg = await _chat_completion(
                client,
                model,
                [{"role": "system", "content": _system_prompt(session)}]
                + _context_window(session["messages"]),
                trace=_llm_trace,
                owner=owner,
                spend=_spend,
            )

            if not msg.tool_calls:
                reply = (msg.content or "").strip()
                break

            def _tc_extra(tc) -> dict:
                # Gemini (OpenAI-compat) rides its thought_signature in
                # extra_content on the tool call — it must round-trip
                # verbatim or the next request 400s. Other providers set
                # nothing here and the dict stays clean.
                extra = getattr(tc, "extra_content", None)
                if extra is None and getattr(tc, "model_extra", None):
                    extra = tc.model_extra.get("extra_content")
                if hasattr(extra, "model_dump"):
                    extra = extra.model_dump()
                return {"extra_content": extra} if isinstance(extra, dict) else {}

            session["messages"].append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        **_tc_extra(tc),
                    }
                    for tc in msg.tool_calls
                ],
            })

            for tc in msg.tool_calls:
                tool_name = tc.function.name
                try:
                    args = _parse_tool_args(tc.function.arguments)
                    # Users say "Ananya", the filesystem says "dhronai_ea" —
                    # resolve BEFORE execution so every tool (and the session
                    # title below) works from the real slug.
                    if tool_name in _TOOLS_WITH_AGENT_NAME and args.get("name"):
                        args["name"] = _resolve_agent_slug(args["name"])
                    if tool_name in _READ_ONLY_TOOLS:
                        # Reads are free, so they need their own brake: the
                        # same read with the same arguments returns the same
                        # answer, and repeating it is a loop, not research.
                        _sig = tool_name + "|" + json.dumps(
                            args, sort_keys=True, default=str)
                        _read_signatures[_sig] = _read_signatures.get(_sig, 0) + 1
                        if _read_signatures[_sig] > _MAX_IDENTICAL_READS:
                            raise ValueError(
                                "You already ran this exact check twice this "
                                "message and got the same answer both times. "
                                "Act on what it told you, or tell the user "
                                "what you found — do not run it again.")
                    result, summary, ok, mutated = await _execute_tool(tool_name, args, guard)
                    if tool_name == "create_agent" and ok:
                        guard["created_this_turn"] = True
                    # Tie this chat to the agent it's working on (names the
                    # conversation in the sidebar, ChatGPT-style).
                    if ok and args.get("name"):
                        meta = session.setdefault("meta", {})
                        # Prefer the slug the tool actually wrote (create
                        # lower-cases/strips its input) over the raw argument.
                        slug = (result.get("name") if isinstance(result, dict) else None) \
                            or str(args["name"]).strip()
                        if tool_name in _AGENT_MUTATING_TOOLS or (
                            tool_name in _AGENT_READING_TOOLS and not meta.get("agent")
                        ):
                            meta["agent"] = slug
                            meta["title"] = _agent_display_name(slug)
                except HTTPException as exc:
                    result, summary, ok, mutated = (
                        _failure_payload(tool_name, str(exc.detail)),
                        f"Couldn't {_TOOL_VERBS.get(tool_name, tool_name)}: {exc.detail}", False, False,
                    )
                except Exception as exc:
                    # A ValueError here is the handler's own refusal (expected,
                    # worded for the model). Anything else is OUR crash and it
                    # used to vanish: the only record of the §60 'str'.get()
                    # failure was the session file.
                    if isinstance(exc, ValueError):
                        logger.warning("[copilot] tool %s refused: %s", tool_name, exc)
                    else:
                        logger.exception("[copilot] tool %s crashed: %s", tool_name, exc)
                    result, summary, ok, mutated = (
                        _failure_payload(tool_name, str(exc)),
                        f"Couldn't {_TOOL_VERBS.get(tool_name, tool_name)}: {exc}", False, False,
                    )
                agents_changed = agents_changed or mutated
                # Record the slug the tool actually wrote when it differs from
                # what was asked — create_agent may suffix the call direction
                # ("jethalal" → "jethalal-outbound"), and the Try/Open/Activate
                # buttons built from this field must point at the real agent.
                _acted = ((result.get("name") if ok and isinstance(result, dict) else None)
                          or args.get("name") or "")
                actions.append({
                    "tool": tool_name, "summary": summary, "ok": ok,
                    "agent": str(_acted).strip().lower(),
                })
                session["messages"].append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tool_name,
                    "content": json.dumps(result, ensure_ascii=False, default=str)[:20000],
                })

            # A round that only looked things up costs nothing.
            if any(tc.function.name not in _READ_ONLY_TOOLS
                   for tc in msg.tool_calls):
                _charged += 1
        else:
            # Step budget spent. Say what landed and what is still open, in
            # the SAME plain English as every other reply: this branch used
            # to join the raw internal `summary` strings, which is how a user
            # ended up reading "update stage MEETING_BOOKING: collect, goal
            # in LesCall; …" — the exact jargon _actions_digest exists to
            # keep out of the chat.
            logger.info("[copilot] step budget spent: %d rounds, %d charged "
                        "(sid=%s)", _rounds, _charged, sid)
            _digest = _actions_digest([a for a in actions if a.get("ok")])
            _pending = _remaining_hint(session, actions)
            _landed = any(a["ok"] and a["tool"] in _MUTATING_TOOLS
                          for a in actions)
            if _landed and _digest:
                reply = (_digest + " I ran out of steps for this message"
                         + (f" — still to do: {_pending}." if _pending else ".")
                         + " Send me the next bit and I'll carry on.")
            else:
                reply = ("I ran out of steps before I could change anything"
                         + (f" — I was still working on: {_pending}."
                            if _pending else ".")
                         + " Could you break that into smaller requests?")
    except Exception as exc:
        ok_saves_so_far = [a for a in actions if a["ok"] and a["tool"] in _MUTATING_TOOLS]
        if ok_saves_so_far:
            # Real changes already landed before the LLM died — report them
            # truthfully instead of erasing the record.
            logger.exception("[copilot] LLM failed mid-turn after successful changes")
            done = "; ".join(a["summary"] for a in ok_saves_so_far)
            reply = (
                f"I applied this change: {done}. Then I hit a connection problem before I could finish "
                "replying — the change IS saved. Please continue when ready."
            )
        else:
            # Nothing changed — roll history back to exactly before this turn
            # so no dangling tool-call messages can brick the session.
            del session["messages"][mark_messages:]
            del session["transcript"][mark_transcript:]
            if isinstance(exc, HTTPException):
                raise
            logger.exception("[copilot] LLM turn failed")
            from openai import RateLimitError
            if isinstance(exc, RateLimitError):
                # The frontend only shows the HTTP status text, so a quota
                # outage must come back as a normal reply, not an error code.
                m = re.search(r"try again in ([0-9]+m)?([0-9.]+s)?", str(exc))
                wait = f" in about {(m.group(1) or '')}{('' if m.group(1) else m.group(2) or '')}".rstrip() if m and (m.group(1) or m.group(2)) else " later"
                reply = (
                    "The AI service's rate limit is exhausted on both the primary and "
                    f"fallback models, so I can't process this message right now. Please try again{wait} "
                    "— your agents and this conversation are safe, nothing was lost. "
                    "(Operators can raise COPILOT_LLM_TPM, set a dedicated COPILOT_LLM_API_KEY, "
                    "or point COPILOT_LLM_MODEL at a different provider.)"
                )
                # Keep the user's message visible in the saved transcript (the
                # LLM history stays rolled back so a retry re-sends it cleanly).
                session["transcript"].append({"role": "user", "content": message})
                session["transcript"].append({"role": "assistant", "content": reply, "actions": []})
                _save_session(sid)
                return {
                    "reply": reply,
                    "actions": [],
                    "agents_changed": False,
                    "session": _session_summary(sid, session),
                }
            # Continuity: persist the turn as an error marker so a page reload
            # (or a container restart) still shows this chat where it left
            # off. The LLM message history stays rolled back — a retry
            # re-sends the message cleanly.
            session["transcript"].append({"role": "user", "content": message})
            session["transcript"].append({
                "role": "assistant", "actions": [], "isError": True,
                "content": ("⚠️ A temporary backend error interrupted this "
                            "message — nothing was changed. Please send it again."),
            })
            meta = session.setdefault("meta", {})
            meta.setdefault("created_at", _now_iso())
            meta["updated_at"] = _now_iso()
            _save_session(sid)
            raise HTTPException(status_code=502, detail=f"Copilot LLM error: {exc}")

    if _budget_stop:
        # Prepended, not substituted: when the loop did work before it ran out,
        # the model's own account of that work is the useful half of the reply
        # and the budget notice is the reason it stops there.
        reply = f"{reply}\n\n{_budget_stop}".strip() if reply else _budget_stop
    if not reply:
        reply = "Done." if actions else "Sorry, I didn't produce a reply — please try again."

    # ── Truthfulness backstop (deterministic — the LLM cannot bypass it) ────
    # If every attempted save/create/activate FAILED this turn, say so in a
    # status line regardless of what the model wrote. Redundant next to an
    # honest reply; load-bearing next to a dishonest one.
    # An apply_update refused pending the user's confirmation is the DESIGNED
    # flow after a successful propose_update, not a failure — warning "Nothing
    # was saved" under a diff card that says "want me to apply this?" told the
    # user their proposal was lost when it was sitting right there.
    def _is_confirmation_refusal(a: dict) -> bool:
        return (a["tool"] == "apply_update"
                and "confirm" in (a.get("summary") or "").lower())

    failed_saves = [a for a in actions if not a["ok"]
                    and a["tool"] in _MUTATING_TOOLS
                    and not _is_confirmation_refusal(a)]
    ok_saves = [a for a in actions if a["ok"] and a["tool"] in _MUTATING_TOOLS]
    proposed_ok = any(a["ok"] and a["tool"] == "propose_update" for a in actions)
    if failed_saves and not ok_saves and not proposed_ok:
        reply += "\n\n⚠️ Nothing was saved in this step — the change did not go through."
        logger.warning("[copilot] truthfulness backstop fired (sid=%s)", sid)

    # ── Verbatim backstop (deterministic) ──────────────────────────────────
    # When the user dictates a sentence for the agent to SAY and the turn
    # wrote something, that sentence must end up in a field the CALLER
    # HEARS. Scoped to hearable fields on purpose: the real 2026-07-31
    # failure put a dictated WhatsApp line inside a stage goal, where an
    # "anywhere in the definition" check would have passed it silently and
    # the user would only have found out on the phone.
    if ok_saves:
        try:
            _dictated = _dictated_sentences(message)
            if _dictated:
                _missing: list[str] = []
                _defs = {}
                for _slug in {str(a.get("agent") or "").strip().lower()
                              for a in ok_saves if a.get("agent")}:
                    try:
                        _defs[_slug] = _read_full_definition(_slug)[0]
                    except Exception:  # noqa: BLE001 — advisory only
                        continue
                for _sent in _dictated:
                    if _defs and not any(
                            _landed_in_a_hearable_field(d, _sent)
                            for d in _defs.values()):
                        _missing.append(_sent)
                if _missing:
                    _first = _missing[0]
                    reply += (
                        "\n\n⚠️ I did not save your exact words — “"
                        + (_first[:70] + ("…" if len(_first) > 70 else ""))
                        + "” — anywhere the agent actually speaks them. Tell "
                        "me which line you meant — " + _HEARABLE_FIELD_HINT
                        + " — and I'll put it in word-for-word.")
                    logger.warning(
                        "[copilot] verbatim backstop fired (sid=%s): %r",
                        sid, _first[:80])
        except Exception:  # noqa: BLE001 — advisory only
            logger.warning("[copilot] verbatim backstop failed", exc_info=True)

    # ── Gender-data reminder (RUNBOOK §42) ──────────────────────────────────
    # Sir/Ma'am and Mr./Mrs. are resolved per contact from contacts.gender and
    # are silent without it — so a user who asks for them has to hear, every
    # time, that the data is what makes them work. The system prompt asks the
    # model to say so; this guarantees it, with the account's real coverage.
    try:
        if (_asks_for_honorific(message)
                and not _GENDER_DATA_MENTION_RE.search(reply or "")):
            reply += "\n\n" + _gender_data_reminder(owner)
            logger.info("[copilot] gender-data reminder appended (sid=%s)", sid)
    except Exception:  # noqa: BLE001 — advisory only
        logger.warning("[copilot] gender-data reminder failed", exc_info=True)

    # ── Hide self-corrected retries from the user-facing action list ────────
    # The LLM often gets a save rejected (e.g. invented setting name), reads
    # the error, and immediately retries correctly. A "Couldn't update …"
    # chip next to successful "Updated …" chips reads as data loss when the
    # change actually landed. Drop a failed save ONLY when a later successful
    # save on the SAME agent exists in this turn; genuine unrecovered
    # failures stay visible (and the backstop above still fires on them).
    visible_actions = [
        a for i, a in enumerate(actions)
        if a["ok"] or a["tool"] not in _MUTATING_TOOLS or not any(
            b["ok"] and b["tool"] in _MUTATING_TOOLS and b.get("agent") == a.get("agent")
            for b in actions[i + 1:]
        )
    ]

    voice_samples = _voice_samples_for(reply)
    actions_summary = _actions_digest(visible_actions)
    agent_links = _agent_links_for(visible_actions)
    session["messages"].append({"role": "assistant", "content": reply})
    _t_entry: dict = {"role": "assistant", "content": reply, "actions": visible_actions,
                      "actions_summary": actions_summary, "agent_links": agent_links}
    # Model provenance on every turn (2026-08-04). Every model attribution in
    # the 08-04 forensics — the 07-15 gpt-4o-mini incident included — had to
    # be inferred from tool-call id alphabets because nothing recorded this.
    if _llm_trace:
        _t_entry["llm"] = {"model": _llm_trace.get("model", ""),
                           "reasoning_effort": _llm_trace.get(
                               "reasoning_effort", ""),
                           "rounds": _rounds}
        if _llm_trace.get("fallbacks"):
            _t_entry["llm"]["fallbacks"] = _llm_trace["fallbacks"]
            # The user is entitled to know a weaker model answered them.
            _fb_to = _llm_trace["fallbacks"][-1]["to"]
            reply += ("\n\n(Note: the main AI model was unavailable for part "
                      f"of this message, so a backup model ({_fb_to}) "
                      "answered. Please double-check anything important — I "
                      "can redo it if something looks off.)")
            _t_entry["content"] = reply
            logger.warning("[copilot] turn served by fallback model %s "
                           "(sid=%s)", _fb_to, sid)
    if voice_samples:
        _t_entry["voice_samples"] = voice_samples  # reloaded chats keep players
    session["transcript"].append(_t_entry)
    meta = session.setdefault("meta", {})
    meta.setdefault("created_at", _now_iso())
    meta["updated_at"] = _now_iso()
    _save_session(sid)
    # v2 additive API contract (old bundles ignore these):
    # - interview: server-computed progress for the progress rail + preview
    # - pending_changes: proposed diffs for Apply/Discard cards
    out: dict = {
        "reply": reply,
        "actions": visible_actions,
        "actions_summary": actions_summary,
        "agent_links": agent_links,
        "agents_changed": agents_changed,
        "session": _session_summary(sid, session),
    }
    if voice_samples:
        out["voice_samples"] = voice_samples
    out.update(_rail_payload(session))
    return out


def _rail_payload(session: dict) -> dict:
    """interview + pending_changes for the progress rail, preview and diff
    cards — shared by the chat turn and the page-reload GET (the rail used
    to vanish on reload because only the POST carried it)."""
    out: dict = {}
    iv = session.get("interview") or {}
    if iv.get("phase") in ("interviewing", "built") or iv.get("draft"):
        progress = interview_progress(iv.get("draft") or {}, iv.get("skipped") or [])
        out["interview"] = {
            "phase": iv.get("phase") or "idle",
            "complete": progress["complete"],
            "layers": [{"id": l["id"], "title": l["title"], "done": l["done"],
                        "skipped": l["skipped"]} for l in progress["layers"]],
            "next_layer_id": progress["next_layer_id"],
            "draft": iv.get("draft") or {},
        }
    pend = session.get("pending_changes") or {}
    if pend:
        out["pending_changes"] = [
            {"change_id": cid, "slug": c.get("slug"),
             "diff": c.get("diff_human"), "reason": c.get("reason")}
            for cid, c in pend.items()]
    return out


@router.post("/api/copilot/sessions/{session_id}/changes/{change_id}/apply")
async def copilot_apply_change(session_id: str, change_id: str, request: Request) -> dict:
    """Diff-card Apply button: the click IS the user's confirmation (§7.3)."""
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="invalid session_id")
    session = _load_session(session_id)
    pending = session.get("pending_changes") or {}
    chg = pending.get(change_id)
    if not chg:
        raise HTTPException(status_code=404, detail="no such pending change")
    slug = chg["slug"]
    builder._assert_slug_access(slug, builder._ctx_owner())
    def _commit(definition: dict, rev: str | None) -> dict:
        return agent_store.write_agent(
            slug, definition, actor=_copilot_actor(), base_rev=rev,
            reason=chg.get("reason") or "diff-card apply",
            owner=builder._ctx_owner(), is_admin=builder._ctx_is_admin(),
            expected_paths=set(chg.get("expected_paths") or []) or None,
            agents_dir=_AGENTS_DIR)

    try:
        try:
            result = _commit(chg["candidate"], chg.get("base_rev"))
        except agent_store.ConflictError:
            # Don't dead-end the card. Replay the proposal's own paths onto the
            # current definition; only a change to those very paths is a real
            # conflict, and then the card is dropped so the user isn't left
            # clicking an Apply that can never succeed.
            try:
                rebased, fresh_rev = _rebase_change(chg)
            except ValueError as exc:
                pending.pop(change_id, None)
                _save_session(session_id)
                raise HTTPException(status_code=409, detail=str(exc))
            result = _commit(rebased, fresh_rev)
    except agent_store.ScopeError as exc:
        # The store rolled the write back — surface it as a reviewable 422,
        # matching the builder's own /ops endpoint, instead of the raw 500
        # the diff card used to get. The card is kept so the user can retry
        # after the underlying cause (usually a concurrent edit) settles.
        raise HTTPException(status_code=422, detail=str(exc))
    pending.pop(change_id, None)
    session["transcript"].append({
        "role": "assistant",
        "content": f"Applied the proposed change — revision {result['revision']}, "
                   "applies from the next call.",
        "actions": [{"tool": "apply_update", "ok": True,
                     "summary": f"Applied change to {_agent_display_name(slug)}",
                     "agent": slug}]})
    _save_session(session_id)
    return {"status": "updated", "name": slug, "revision": result["revision"],
            "effective": "next_call"}


@router.post("/api/copilot/sessions/{session_id}/changes/{change_id}/discard")
async def copilot_discard_change(session_id: str, change_id: str) -> dict:
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="invalid session_id")
    session = _load_session(session_id)
    pending = session.get("pending_changes") or {}
    # Idempotent — a Discard click on a card that is already gone (double
    # click, stale tab) is a success, not a 404 the user has to interpret.
    pending.pop(change_id, None)
    _save_session(session_id)
    return {"status": "discarded", "change_id": change_id}


@router.post("/api/copilot/upload")
async def copilot_upload(request: Request) -> dict:
    """Attach a document to a Copilot conversation (plan §6.4). Multipart:
    session_id + file. Returns {doc_id} for the chat body's `attachments`."""
    form = await request.form()
    sid = str(form.get("session_id") or "").strip()
    upload = form.get("file")
    if not _SESSION_ID_RE.match(sid):
        raise HTTPException(status_code=400, detail="invalid session_id")
    owner = getattr(request.state, "owner", None)
    if not _session_owner_ok(sid, owner):
        sid = _tenant_sid(owner, sid)
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=400, detail="'file' is required")
    content = await upload.read()
    if not content:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    if len(content) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file too large (15 MB max)")
    import time as _time
    safe_name = re.sub(r"[^A-Za-z0-9_.\-]", "_",
                       getattr(upload, "filename", "") or "document.txt")[-80:]
    doc_id = f"{int(_time.time())}_{safe_name}"
    dest = _UPLOADS_DIR / sid
    dest.mkdir(parents=True, exist_ok=True)
    (dest / doc_id).write_bytes(content)
    # Claim the (possibly remapped) session for this owner NOW, so the chat
    # turn that follows resolves to the SAME sid and ingest_document finds the
    # file. Without this the sid stays unowned and the chat remaps it again.
    if owner:
        _load_session(sid).setdefault("meta", {}).setdefault("owner", owner)
        _save_session(sid)
    logger.info("[copilot] upload %s/%s (%d bytes)", sid, doc_id, len(content))
    return {"status": "uploaded", "doc_id": doc_id, "session_id": sid,
            "note": "pass this doc_id in the chat body's `attachments` list"}


# ===========================================================================
# Voice prompting for the composer — speak instead of type.
#
# The correction pipeline lives in copilot_voice.py; what belongs here is the
# part that only the API knows: WHOSE vocabulary to repair against. Generic
# spellcheck is worthless on "make hardik-2-inbound greet in Gujarati" — the
# words that get mangled are the caller's own agent slugs, agent names and
# company names, and those are exactly what this module can enumerate.
# ===========================================================================

_VOICE_LEX_CACHE: dict[str, tuple[float, Any]] = {}
_VOICE_LEX_TTL_S = 120.0
_VOICE_RATE: dict[str, list[float]] = {}
_VOICE_RATE_MAX = 60            # transcriptions per owner …
_VOICE_RATE_WINDOW_S = 600.0    # … per 10 minutes


def _voice_lexicon(owner: str | None):
    """The caller's own nouns, plus this platform's jargon.

    Rebuilt at most every two minutes: an agent renamed mid-conversation
    should be sayable almost immediately, but not at the cost of re-reading
    every YAML on every held mic button.
    """
    import copilot_voice
    key = owner or "-"
    now = time.monotonic()
    hit = _VOICE_LEX_CACHE.get(key)
    if hit and now - hit[0] < _VOICE_LEX_TTL_S:
        return hit[1]

    phrases: list[str] = []
    try:
        visible = builder._visible_slugs(owner or builder._ctx_owner())
    except Exception:
        visible = set()
    for path in sorted(_AGENTS_DIR.glob("*/agent.yaml")):
        slug = path.parent.name
        if visible is not None and slug not in visible:
            continue
        # The slug itself is the highest-value entry: normalized it is
        # "hardik2inbound", which is what "hardik 2 inbound" collapses to, so
        # the spoken form repairs straight back to the canonical slug.
        phrases.append(slug)
        try:
            agent = agent_registry.get(slug, _AGENTS_DIR)
        except Exception:
            continue
        phrases.append(agent.identity.agent_name)
        phrases.append(agent.identity.company_name)
        try:
            phrases.extend(list((agent.knowledge_base.known_projects or {}).keys()))
            phrases.extend(list(agent.knowledge_base.locations or []))
            phrases.extend(v.speaker for v in
                           (agent.languages.tts_voices or {}).values())
        except Exception:
            pass
    lex = copilot_voice.Lexicon(phrases + list(copilot_voice._PRODUCT_LEXICON))
    _VOICE_LEX_CACHE[key] = (now, lex)
    logger.info("[voice] lexicon for %s: %d phrases", key, len(lex.by_key))
    return lex


def _voice_rate_ok(owner: str | None) -> bool:
    key = owner or "-"
    now = time.monotonic()
    hits = [t for t in _VOICE_RATE.get(key, []) if now - t < _VOICE_RATE_WINDOW_S]
    if len(hits) >= _VOICE_RATE_MAX:
        _VOICE_RATE[key] = hits
        return False
    hits.append(now)
    _VOICE_RATE[key] = hits
    return True


@router.post("/api/copilot/transcribe")
async def copilot_transcribe(request: Request) -> dict:
    """Composer mic → prompt text. Multipart: `audio` (16 kHz mono WAV from
    the browser; anything ffmpeg reads also works).

    Returns the corrected text plus what was corrected and where the time
    went, so a slow mic can be diagnosed without guessing which stage."""
    import copilot_voice
    owner = getattr(request.state, "owner", None)
    if not _voice_rate_ok(owner):
        raise HTTPException(status_code=429,
                            detail="too many voice prompts — give it a minute")
    form = await request.form()
    upload = form.get("audio")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=400, detail="'audio' is required")
    raw = await upload.read()
    if not raw:
        raise HTTPException(status_code=400, detail="the recording is empty")
    if len(raw) > copilot_voice.MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="the recording is too long")
    # Auto-detect: this composer is used in English and in Hinglish, and
    # forcing en-IN turns the Hindi words people mix in into nonsense.
    language = str(form.get("language") or "unknown").strip() or "unknown"
    want_polish = str(form.get("polish") or "").lower() in ("1", "true", "yes") \
        or copilot_voice.POLISH_ENABLED
    try:
        out = await copilot_voice.transcribe(
            raw, _voice_lexicon(owner),
            language=language,
            want_polish=want_polish,
            get_client=_get_client,
            polish_model=(_env("LLM_VOICE_MODEL")
                          or _fallback_model_name() or _model_name()),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[voice] transcription failed")
        raise HTTPException(status_code=502,
                            detail=f"could not transcribe that: {exc}")
    logger.info("[voice] %.1fs audio → %r (%s) %s", out["seconds"],
                out["text"][:80], out["language"] or "?", out["ms"])
    return out


# ===========================================================================
# F17 "Try your agent" — a LIVE SIMULATED CALL, not a chat with the agent.
#
# A phone call holds ONE ConversationRouter for its whole life, and the agent
# speaks the first word. The original endpoint did neither: it rebuilt the
# router on every request and replayed the entire history through it, which
# was wrong four ways —
#
#   1. it iterated `router.process()`, which is a coroutine returning one
#      EngineResult, not an async generator. EVERY turn 502'd with
#      "'async for' requires an object with __aiter__ method, got coroutine".
#      (`process_stream` is the generator; the pipeline drives that one.)
#   2. the agent's opening was generated and thrown away, so the simulation
#      began with the CALLER talking — the one thing that never happens on a
#      call, and the reason the two-beat opening (greeting, then
#      call_flow.connect.intro_line once the caller answers) could not be
#      tested here at all.
#   3. replay re-ran the LLM for every prior turn — turn N cost N generations
#      — and then ALSO injected the UI's copy of each prior reply into
#      memory, so every past agent line appeared twice in the LLM's context.
#   4. every agent, inbound included, was handed contact_name="Test Caller",
#      so inbound flows skipped asking the caller's name.
#
# Now: one router per simulated call, held server-side for the life of the
# session, driven exactly as pipeline.py drives it. The only deliberate
# divergence from a real call is persistence — a simulated booking must not
# consume a real calendar slot — and the parts of the pipeline that only
# exist because there is audio (VAD, barge-in, dead-air repair, silence and
# max-duration timers) simply have no analogue in typed text.
# ===========================================================================

_TRY_SIM_TTL_S = 1800.0        # a simulated call goes stale after 30 idle min
_TRY_SIM_MAX = 60              # process-wide ceiling on live simulations
_TRY_SIM_MAX_TURNS = 200       # a runaway client cannot pin a router forever
_TRY_SIMS: dict[str, dict] = {}
_TRY_SIM_ID_RE = re.compile(r"^sim_[a-f0-9]{24}$")


def _try_sim_gc() -> None:
    """Drop idle simulations, then the oldest ones over the ceiling.

    Committing the pending turn on the way out mirrors pipeline.stop(): an
    abandoned simulation settles exactly like an abandoned call.
    """
    now = time.monotonic()
    for sid, sim in list(_TRY_SIMS.items()):
        if now - sim["last_used"] > _TRY_SIM_TTL_S:
            _try_sim_close(_TRY_SIMS.pop(sid, None))
    while len(_TRY_SIMS) > _TRY_SIM_MAX:
        oldest = min(_TRY_SIMS, key=lambda k: _TRY_SIMS[k]["last_used"])
        _try_sim_close(_TRY_SIMS.pop(oldest, None))


def _try_sim_close(sim: dict | None) -> None:
    if not sim:
        return
    try:
        sim["router"].commit_pending_turn()
    except Exception:
        logger.exception("[copilot] sim commit_pending_turn failed")


def _try_assert_agent(name: str, request: Request) -> None:
    """Same tenancy + existence gate the old endpoint applied."""
    owner = getattr(request.state, "owner", None)
    _slug_owner = builder._slug_owner(name)
    if _slug_owner and owner and _slug_owner != owner \
            and not builder._ctx_is_admin():
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    if not (_AGENTS_DIR / name / "agent.yaml").exists():
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")


def _new_sim_router(name: str, sim_id: str, direction: str,
                    contact_name: str):
    """Build the router a real call would build for this agent.

    Mirrors conversation_call_bridge._build_agent_stack + set_call_context:
    the same registry load, the same "seed the language from the agent's own
    default" rule, and the same per-call context. Returns the router plus the
    list that captures calendar writes instead of performing them.
    """
    from backend.app.voice.conversation import ConversationRouter
    agent = agent_registry.get(name, _AGENTS_DIR)
    router_obj = ConversationRouter(
        agent_def=agent,
        default_language=(agent.languages.default or "hi"),
    )
    direction = (direction or agent.call_direction or "inbound").lower()
    if direction == "outbound":
        # Only an outbound dial knows who it is calling. Inbound callers are
        # strangers — handing one a contact name pre-fills customer_name and
        # the flow then never asks for it, which is precisely the bug an
        # inbound test is supposed to catch.
        router_obj.set_call_context(
            direction="outbound",
            contact_name=(contact_name or "Test Caller").strip(),
            contact_phone="9999999999",
            conn_id=sim_id)
    else:
        router_obj.set_call_context(direction="inbound", conn_id=sim_id)
    # Persistence divergence: a simulated booking must not eat a real slot in
    # the agent's calendar (later real callers would be told it is taken), so
    # the write is captured and handed back to the tester instead.
    bookings: list[str] = []
    router_obj._write_calendar_booking = bookings.append
    return router_obj, bookings


def _sim_state(sim: dict) -> dict:
    """The live engine readout — the reason to test in text rather than by
    phone. Same fields the pipeline stamps on its per-turn trace."""
    router_obj = sim["router"]
    state = getattr(router_obj, "state", None)
    flags = {k: v for k, v in
             dict(getattr(router_obj.state_mgr, "extra_flags", {}) or {}).items()
             if v}
    return {
        "sim_id": sim["id"],
        "stage": (str(getattr(state, "conversation_stage", "")
                      or getattr(state, "stage", "") or "")),
        "language": getattr(state, "language", "") or "",
        "slots": {k: v for k, v in dict(router_obj.state_mgr.state).items()
                  if v is not None},
        "flags": flags,
        "bookings": list(sim["bookings"]),
        "turn": sim["turns"],
        "ended": bool(sim["ended"]),
        "end_reason": sim["end_reason"],
        "outcome": str(getattr(router_obj, "call_outcome", "") or ""),
        "direction": router_obj.call_direction,
        "agent_revision": (agent_registry.meta(sim["slug"]) or {}).get("revision", 0),
        "model": router_obj._model,
    }


def _sim_reply(sim: dict, parts: list[dict]) -> dict:
    """Fold one engine turn into the wire shape.

    `reply` excludes fillers for the same reason the pipeline excludes them
    from the stored transcript: "One moment please…" is played on the call but
    is not what the agent SAID. They are returned separately so the tester can
    still see that a filler was spoken.
    """
    out = _sim_state(sim)
    out["reply"] = " ".join(p["text"] for p in parts
                            if p["intent"] != "filler" and p["text"]).strip()
    out["parts"] = parts
    out["fillers"] = [p["text"] for p in parts if p["intent"] == "filler"]
    return out


async def _sim_run_turn(sim: dict, text: str) -> list[dict]:
    """One caller utterance through the real engine, exactly as
    pipeline._conversation_layer drives it."""
    router_obj = sim["router"]
    parts: list[dict] = []
    try:
        async for res in router_obj.process_stream(text):
            if getattr(res, "text", ""):
                parts.append({"text": res.text, "intent": res.intent,
                              "is_shortcut": bool(res.is_shortcut)})
    except Exception as exc:
        logger.exception("[copilot] sim turn failed for %s", sim["slug"])
        raise HTTPException(status_code=502,
                            detail=f"engine turn failed: {exc}")
    sim["turns"] += 1
    sim["last_used"] = time.monotonic()
    # The agent decided to hang up. The pipeline drains TTS and then breaks
    # the channel; here the call is simply over — no further turns are taken,
    # so the pending transaction is committed as pipeline.stop() would.
    if getattr(router_obj, "hangup_pending", False):
        sim["ended"] = True
        sim["end_reason"] = (str(getattr(router_obj, "call_outcome", "") or "")
                             or "agent ended the call")
        _try_sim_close(sim)
    elif sim["turns"] >= _TRY_SIM_MAX_TURNS:
        sim["ended"] = True
        sim["end_reason"] = "simulation turn limit reached"
        _try_sim_close(sim)
    return parts


@router.post("/api/copilot/try/{name}/start")
async def copilot_try_start(name: str, request: Request) -> dict:
    """Answer the simulated call: build the router and return the agent's
    OPENING line — the agent always speaks first, on every real call and now
    here too. No LLM runs (the opening is configured text), so this does not
    spend the tenant's turn budget."""
    _try_assert_agent(name, request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    _try_sim_gc()
    sim_id = "sim_" + secrets.token_hex(12)
    try:
        router_obj, bookings = _new_sim_router(
            name, sim_id,
            direction=str(body.get("direction") or ""),
            contact_name=str(body.get("contact_name") or ""))
        opening = await router_obj.generate_opening()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[copilot] sim start failed for %s", name)
        raise HTTPException(status_code=502,
                            detail=f"engine start failed: {exc}")
    sim = {
        "id": sim_id, "slug": name, "router": router_obj,
        "bookings": bookings, "owner": getattr(request.state, "owner", None),
        "turns": 0, "ended": False, "end_reason": "",
        "last_used": time.monotonic(),
    }
    _TRY_SIMS[sim_id] = sim
    logger.info("[copilot] sim %s started for %s (direction=%s)",
                sim_id, name, router_obj.call_direction)
    return _sim_reply(sim, [{"text": opening.text, "intent": "greeting",
                             "is_shortcut": True}] if opening.text else [])


@router.post("/api/copilot/try/{name}")
async def copilot_try_agent(name: str, request: Request) -> dict:
    """One caller turn against a built agent on the LIVE env model — exactly
    the engine a phone call gets. The Kaapd failures (fabricated products,
    ignored flow, real-estate objections) were all discoverable in sixty
    seconds of text chat; the first execution of a built agent must never
    again be a production phone call.

    Body: {"sim_id": "...", "message": "..."} — one turn against the live
    simulation opened by POST .../try/{name}/start.

    Legacy body {"messages": [...]} is still accepted for browser tabs holding
    a pre-simulation bundle: it opens a throwaway simulation and replays the
    history through it. Replay costs one LLM generation per prior turn, which
    is exactly why the session form exists.

    Returns the reply plus the live stage/slot state so the tester can SEE the
    flow advancing."""
    _try_assert_agent(name, request)
    body = await request.json()
    body = body if isinstance(body, dict) else {}
    # E10 — try-agent drives a REAL ConversationRouter, the most expensive
    # LLM surface here per call. It reports no usage back (the router owns its
    # own client), so it spends the turn budget and is bounded by whatever the
    # chat surface has already charged against the token budget.
    _billing_llm_gate(request, "copilot_try",
                      request_tokens=int(len(str(body.get("message") or "")) / 3.5))

    sim_id = str(body.get("sim_id") or "").strip()
    if sim_id:
        if not _TRY_SIM_ID_RE.match(sim_id):
            raise HTTPException(status_code=400, detail="invalid sim_id")
        sim = _TRY_SIMS.get(sim_id)
        if sim is None or sim["slug"] != name:
            # 410, not 404: the agent is fine, this particular simulated call
            # is gone (idle timeout or a dashboard restart). The UI reads the
            # status and offers Restart rather than showing a dead error.
            raise HTTPException(
                status_code=410,
                detail="this simulated call has expired — restart to begin a "
                       "new one")
        owner = getattr(request.state, "owner", None)
        if sim["owner"] and owner and sim["owner"] != owner \
                and not builder._ctx_is_admin():
            raise HTTPException(status_code=404,
                                detail="this simulated call has expired — "
                                       "restart to begin a new one")
        if sim["ended"]:
            raise HTTPException(
                status_code=409,
                detail=f"the call has ended ({sim['end_reason']}) — restart "
                       "to begin a new one")
        text = str(body.get("message") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="'message' is required")
        parts = await _sim_run_turn(sim, text[:2000])
        return _sim_reply(sim, parts)

    # ---- legacy stateless replay (pre-simulation frontend bundles) --------
    msgs = [m for m in (body.get("messages") or [])
            if isinstance(m, dict) and str(m.get("text") or "").strip()][-30:]
    if not msgs or msgs[-1].get("role") != "user":
        raise HTTPException(status_code=400,
                            detail="messages must end with a user turn")
    _try_sim_gc()
    replay_id = "sim_" + secrets.token_hex(12)
    try:
        router_obj, bookings = _new_sim_router(
            name, replay_id,
            direction=str(body.get("direction") or ""),
            contact_name=str(body.get("contact_name") or ""))
        await router_obj.generate_opening()
    except Exception as exc:
        logger.exception("[copilot] sim replay start failed for %s", name)
        raise HTTPException(status_code=502,
                            detail=f"engine start failed: {exc}")
    sim = {"id": replay_id, "slug": name, "router": router_obj,
           "bookings": bookings, "owner": getattr(request.state, "owner", None),
           "turns": 0, "ended": False, "end_reason": "",
           "last_used": time.monotonic()}
    # Only the CALLER's turns are replayed. The router generates (and
    # remembers) its own replies — re-injecting the UI's copy on top is what
    # used to double every past agent line in the LLM's context.
    for m in msgs[:-1]:
        if m.get("role") == "user" and not sim["ended"]:
            await _sim_run_turn(sim, str(m["text"])[:2000])
    parts = await _sim_run_turn(sim, str(msgs[-1]["text"])[:2000])
    _try_sim_close(sim)
    return _sim_reply(sim, parts)


@router.get("/api/copilot/sessions")
async def copilot_sessions(request: Request) -> dict:
    """The CALLER's saved conversations, newest first (admins see all).
    Empty sessions (no user message yet) are skipped."""
    import auth
    owner = getattr(request.state, "owner", None) or auth.DEFAULT_OWNER
    show_all = auth.is_admin(owner)

    def _mine(session: dict) -> bool:
        # Admins see all; tenants see ONLY sessions they own. Legacy/unowned
        # (owner=None) sessions belong to the admin — never shown to tenants.
        return show_all or session.get("meta", {}).get("owner") == owner

    summaries: list[dict] = []
    seen: set[str] = set()
    try:
        files = sorted(_SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        files = []
    for path in files:
        sid = path.stem
        if not _SESSION_ID_RE.match(sid):
            continue
        try:
            session = _load_session(sid)
            if not session.get("transcript") or not _mine(session):
                continue
            summaries.append(_session_summary(sid, session, mtime=path.stat().st_mtime))
            seen.add(sid)
        except Exception as exc:  # pragma: no cover - skip corrupt files
            logger.warning("[copilot] skipping session %s: %r", sid, exc)
    # In-memory sessions not yet flushed to disk (first turn in progress)
    for sid, session in _SESSIONS.items():
        if sid not in seen and session.get("transcript") and _mine(session):
            summaries.append(_session_summary(sid, session))
    summaries.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
    return {"sessions": summaries}


@router.delete("/api/copilot/sessions/{session_id}")
async def copilot_delete_session(session_id: str) -> dict:
    """Delete a conversation (sidebar 'remove chat')."""
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="invalid session_id")
    _SESSIONS.pop(session_id, None)
    path = _session_path(session_id)
    if path.exists():
        try:
            path.unlink()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"could not delete session: {exc}")
    return {"status": "deleted", "session_id": session_id}


@router.get("/api/copilot/sessions/{session_id}")
async def copilot_session(session_id: str, request: Request) -> dict:
    """Return the UI transcript for a session (page-reload restore)."""
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="invalid session_id")
    session = _load_session(session_id)
    # Claim the session for the requester (the tenancy guard already ensured
    # it's theirs or brand-new). Without this, a GET-created session sits in
    # _SESSIONS with no owner, and the NEXT write (upload/chat) treats it as
    # foreign and remaps to a hashed sid — which is how an uploaded document
    # ended up in one session folder while ingest_document looked in another.
    owner = getattr(request.state, "owner", None)
    if owner and not session.setdefault("meta", {}).get("owner"):
        session["meta"]["owner"] = owner
        # Persist the claim only for sessions with real content — an empty
        # GET-created session stays memory-only (no junk files), and the
        # first chat turn saves it with the owner already stamped.
        if session.get("transcript"):
            _save_session(session_id)
    # Older transcripts predate the plain-English digest and the existence-
    # checked agent links — compute both on the way out so reloaded chats
    # read the same as live ones. agent_links is ALWAYS recomputed: existence
    # is checked at load time, so buttons for a since-deleted agent vanish
    # instead of 404ing. (Not persisted; the stored file stays untouched.)
    def _restore(m: dict) -> dict:
        if m.get("role") != "assistant" or not m.get("actions"):
            return m
        out = {**m, "agent_links": _agent_links_for(m["actions"])}
        if "actions_summary" not in out:
            out["actions_summary"] = _actions_digest(m["actions"])
        return out

    transcript = [_restore(m) for m in session["transcript"]]
    return {
        "session_id": session_id,
        "transcript": transcript,
        "meta": session.get("meta", {}),
        "session": _session_summary(session_id, session),
        **_rail_payload(session),
    }


# ── Quick controls: voice + speaking speed by click (RUNBOOK §67) ──────────
#
# The two settings owners change most, and the two the chat handled worst:
# a speaker name has to be typed exactly, a pace has to be guessed as a
# number, and every tweak cost a full LLM turn against the quota live calls
# share. The panel writes the SAME fields the model would
# (languages.tts_voices.<lang>.{speaker,pace}) through the same store path,
# so a revision is recorded and the agent's he/she re-derives from the voice
# on load (schema.from_dict). Before the agent exists the draft is edited
# instead. Either way the session's model context gets a note, so the chat
# never "corrects" a choice it did not make.

PACE_MIN, PACE_MAX, PACE_STEP = 0.5, 2.0, 0.05
_PACE_DEFAULT = 1.15            # the Copilot's default for a new agent
_V3_MODEL = "bulbul:v3"
_LANG_NAMES = {"en": "English", "hi": "Hindi", "gu": "Gujarati",
               "mr": "Marathi", "ta": "Tamil", "te": "Telugu",
               "kn": "Kannada", "ml": "Malayalam", "bn": "Bengali",
               "pa": "Punjabi", "od": "Odia"}


def _voice_roster() -> list[dict]:
    """Every speaker a new voice can be (bulbul:v3), with a sample when one
    was generated and the languages Sarvam recommends it for."""
    try:
        sampled = {str(v.get("speaker")) for v in
                   json.loads(_VOICE_SAMPLES_FILE.read_text())
                   if isinstance(v, dict)}
    except Exception:
        sampled = set()
    rec: dict[str, list[str]] = {}
    for (lang, _g), spk in RECOMMENDED_SPEAKERS.items():
        rec.setdefault(spk, []).append(lang)
    out = []
    for spk, info in SPEAKER_CATALOG.items():
        if _V3_MODEL not in info.get("models", ()):
            continue                      # v2-only legacy — cannot be chosen
        out.append({
            "speaker": spk, "gender": info.get("gender", ""),
            "sample_url": (f"/api/voices/sample/{spk}" if spk in sampled
                           else None),
            "recommended_for": sorted(rec.get(spk, [])),
        })
    out.sort(key=lambda v: (v["gender"] != "female", not v["sample_url"],
                            v["speaker"]))
    return out


def _qc_target(session: dict) -> dict:
    """What the panel edits: the chat's agent once it exists on disk, else
    the interview draft, else nothing yet."""
    meta = session.get("meta") or {}
    iv = session.get("interview") or {}
    slug = str(meta.get("agent") or iv.get("built_slug") or "").strip()
    if slug and (_AGENTS_DIR / slug / "agent.yaml").exists() \
            and _visible_slug(slug):
        twin = builder.counterpart_of(slug)
        if not (twin and (_AGENTS_DIR / twin / "agent.yaml").exists()
                and _visible_slug(twin)):
            twin = None
        return {"kind": "agent", "slug": slug, "twin": twin,
                "display_name": _agent_display_name(slug)}
    if iv.get("phase") == "interviewing" or iv.get("draft"):
        return {"kind": "draft", "slug": None, "twin": None,
                "display_name": str(((iv.get("draft") or {}).get("identity")
                                     or {}).get("agent_name") or "")}
    return {"kind": "none", "slug": None, "twin": None, "display_name": ""}


def _qc_languages(defn: dict) -> tuple[list[str], str]:
    langs = defn.get("languages") if isinstance(defn.get("languages"), dict) \
        else {}
    sup = [c for c in (langs.get("supported") or []) if isinstance(c, str) and c]
    tv = langs.get("tts_voices") if isinstance(langs.get("tts_voices"), dict) \
        else {}
    if not sup:
        sup = [c for c in tv.keys() if isinstance(c, str)]
    default = langs.get("default") if langs.get("default") in sup else (
        sup[0] if sup else "")
    return sup, default


def _qc_state(defn: dict) -> dict:
    """Voice / pace / pronouns as the panel shows them."""
    sup, default = _qc_languages(defn)
    tv = ((defn.get("languages") or {}).get("tts_voices") or {}) \
        if isinstance(defn.get("languages"), dict) else {}
    voices = {c: tv.get(c) for c in sup if isinstance(tv.get(c), dict)}
    speakers = [str(v.get("speaker") or "").lower() for v in voices.values()
                if v.get("speaker")]
    main = str((voices.get(default) or {}).get("speaker") or
               (speakers[0] if speakers else "")).lower()
    gender = speaker_gender(main)
    pace = {}
    for c in sup:
        try:
            pace[c] = round(float((voices.get(c) or {}).get("pace")
                                  or _PACE_DEFAULT), 2)
        except (TypeError, ValueError):
            pace[c] = _PACE_DEFAULT
    return {
        "languages": [{"code": c, "name": _LANG_NAMES.get(c, c)} for c in sup],
        "default_language": default,
        "speaker": main or None,
        "mixed_speakers": len(set(speakers)) > 1,
        "gender": gender,
        "pronouns": {"male": "he/him", "female": "she/her"}.get(gender, ""),
        "model": str((voices.get(default) or {}).get("model")
                     or DEFAULT_TTS_MODEL),
        "pace": pace,
    }


def _qc_changes(defn: dict, speaker: str | None,
                pace: dict[str, float]) -> tuple[dict, list[str]]:
    """The partial definition to merge, and the paths it touches."""
    sup, _ = _qc_languages(defn)
    tv = ((defn.get("languages") or {}).get("tts_voices") or {}) \
        if isinstance(defn.get("languages"), dict) else {}
    out: dict = {}
    for c in sup or list(pace.keys()):
        cur = tv.get(c) if isinstance(tv.get(c), dict) else {}
        entry: dict = {}
        if speaker:
            entry["speaker"] = speaker
            model = str(cur.get("model") or DEFAULT_TTS_MODEL)
            # A v3 speaker on a v2 model 400s every utterance: move it up.
            if model not in SPEAKER_CATALOG[speaker]["models"]:
                entry["model"] = _V3_MODEL
            elif not cur.get("model"):
                entry["model"] = model
            if not cur.get("pace") and c not in pace:
                entry["pace"] = _PACE_DEFAULT
        if c in pace:
            entry["pace"] = pace[c]
        if entry:
            out[c] = entry
    changes = {"languages": {"tts_voices": out}} if out else {}
    paths = [f"languages.tts_voices.{c}.{k}" for c, e in out.items() for k in e]
    return changes, paths


def _qc_parse(body: dict, langs: list[str]) -> tuple[str | None, dict]:
    speaker = body.get("speaker")
    if speaker is not None:
        speaker = str(speaker).strip().lower()
        info = SPEAKER_CATALOG.get(speaker)
        if not info or _V3_MODEL not in info.get("models", ()):
            raise HTTPException(status_code=400,
                                detail=f"unknown voice '{speaker}'")
    raw = body.get("pace")
    pace: dict[str, float] = {}
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        raw = {c: raw for c in (langs or ["en"])}
    if isinstance(raw, dict):
        for c, v in raw.items():
            try:
                f = float(v)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"bad pace for {c}")
            if not (PACE_MIN <= f <= PACE_MAX):
                raise HTTPException(
                    status_code=400,
                    detail=f"speed must be between {PACE_MIN} and {PACE_MAX}")
            pace[str(c)] = round(round(f / PACE_STEP) * PACE_STEP, 2)
    elif raw is not None:
        raise HTTPException(status_code=400, detail="bad pace")
    if not speaker and not pace:
        raise HTTPException(status_code=400, detail="nothing to change")
    return speaker, pace


def _qc_note(session: dict, speaker: str | None, pace: dict,
             where: str) -> None:
    """Tell the chat's model what the user set by hand."""
    bits = []
    if speaker:
        g = speaker_gender(speaker)
        bits.append(f"voice = {speaker} ({g}; the agent's pronouns now follow "
                    f"it: {'he' if g == 'male' else 'she'})")
    if pace:
        bits.append("speaking speed = " + ", ".join(
            f"{_LANG_NAMES.get(c, c)} {v:.2f}" for c, v in pace.items()))
    session.setdefault("messages", []).append({
        "role": "user",
        "content": ("[Quick controls — the user set this directly and it is "
                    f"ALREADY SAVED on {where}: " + "; ".join(bits)
                    + ". Do not change or re-ask it unless they bring it up. "
                      "No reply needed for this note.]"),
    })


@router.get("/api/copilot/sessions/{session_id}/quick-controls")
async def copilot_quick_controls(session_id: str, request: Request) -> dict:
    """Voice roster + the current voice/speed of whatever this chat edits."""
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="invalid session_id")
    session = _load_session(session_id)
    target = _qc_target(session)
    state: dict = {}
    if target["kind"] == "agent":
        defn, _rev = await asyncio.to_thread(_read_full_definition,
                                             target["slug"])
        state = _qc_state(defn)
    elif target["kind"] == "draft":
        state = _qc_state((session.get("interview") or {}).get("draft") or {})
    return {
        "target": target,
        "state": state,
        "roster": _voice_roster(),
        "pace_range": {"min": PACE_MIN, "max": PACE_MAX, "step": PACE_STEP,
                       "default": _PACE_DEFAULT},
    }


@router.post("/api/copilot/sessions/{session_id}/quick-controls")
async def copilot_quick_controls_apply(session_id: str,
                                       request: Request) -> dict:
    """Body: {speaker?: str, pace?: number | {lang: number},
    both_directions?: bool (default true)}. Saves at once — a live agent's
    next call uses it."""
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="invalid session_id")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected an object")
    async with _session_lock(session_id):
        session = _load_session(session_id)
        target = _qc_target(session)
        if target["kind"] == "none":
            raise HTTPException(
                status_code=409,
                detail="Start describing the agent first — there is nothing "
                       "to set a voice on yet.")
        result: dict = {"target": target}
        if target["kind"] == "agent":
            slug = target["slug"]
            current, base_rev = await asyncio.to_thread(
                _read_full_definition, slug)
            speaker, pace = _qc_parse(body, _qc_languages(current)[0])
            changes, paths = _qc_changes(current, speaker, pace)
            if not changes:
                raise HTTPException(status_code=400, detail="nothing to change")
            merged = _deep_merge(copy.deepcopy(current), changes)
            violations = _invariant_violations(merged)
            if violations:
                raise HTTPException(status_code=422,
                                    detail="; ".join(violations))
            try:
                wr = await asyncio.to_thread(
                    agent_store.write_agent, slug, merged,
                    actor=f"{builder._ctx_owner()}:copilot-quick",
                    base_rev=base_rev,
                    reason="quick controls: " + ", ".join(
                        (["voice"] if speaker else [])
                        + (["speed"] if pace else [])),
                    owner=builder._ctx_owner(),
                    is_admin=builder._ctx_is_admin(),
                    expected_paths=paths, agents_dir=_AGENTS_DIR)
            except agent_store.ConflictError:
                raise HTTPException(
                    status_code=409,
                    detail="The agent changed while you were editing — "
                           "reload the panel and try again.")
            result["revision"] = wr["revision"]
            logger.info("[copilot] quick controls on '%s': %s rev=%s",
                        slug, ", ".join(paths), wr["revision"])
            if target["twin"] and body.get("both_directions", True):
                result["twin_write"] = await _mirror_to_twin(slug, changes,
                                                             paths)
            defn, _ = await asyncio.to_thread(_read_full_definition, slug)
            result["state"] = _qc_state(defn)
            where = f"agent '{slug}'" + (
                " and its other direction"
                if (result.get("twin_write") or {}).get("applied") else "")
        else:
            iv = _interview_state(session)
            draft = iv.setdefault("draft", {})
            speaker, pace = _qc_parse(body, _qc_languages(draft)[0])
            sup, _ = _qc_languages(draft)
            if not sup:
                # Nothing about languages yet: park the voice on the
                # platform default; the build fills every language from it.
                draft.setdefault("languages", {}).setdefault(
                    "tts_voices", {}).setdefault("en", {})
            changes, _paths = _qc_changes(draft, speaker, pace)
            iv["draft"] = _deep_merge(draft, changes)
            result["state"] = _qc_state(iv["draft"])
            where = "the draft (it will be used when the agent is created)"
        _qc_note(session, speaker, pace, where)
        session.setdefault("meta", {})["updated_at"] = _now_iso()
        _save_session(session_id)
        result.update(_rail_payload(session))
        return result
