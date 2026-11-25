"""
AgentDefinition — the complete configuration contract for a voice agent.

AgentSpec v2: every behaviour that was once platform-hardcoded Pramukh
doctrine has exactly one data home here. IRON RULES (see
deploy/PLAN-copilot-agent-builder.md §3):

1. Every new field/section is optional with a default that reproduces the
   pre-v2 behaviour byte-for-byte (defaults ledger in the plan's §12).
2. All parsing is tolerant `.get()` reads — an unknown YAML key can never
   TypeError an agent load (that failure used to answer the call as the
   fallback agent).

Nothing in this module has any dependency on the voice pipeline or
conversation engine — it is pure data.
"""

from __future__ import annotations

import logging

from dataclasses import dataclass, field
from typing import Any

from .voice_catalog import (  # single source of truth — re-exported here
    DEFAULT_TTS_MODEL,
    DEFAULT_TTS_SPEAKER,
    PLATFORM_LANGUAGES,
    VALID_TTS_MODELS,
    speaker_gender,
    speaker_models,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tolerant field readers — the iron-rule helpers
# ---------------------------------------------------------------------------

def _as_str(d: dict, key: str, default: str = "") -> str:
    v = d.get(key, default)
    if v is None:
        return default
    return v if isinstance(v, str) else str(v)


def _as_int(d: dict, key: str, default: int) -> int:
    try:
        return int(d.get(key, default))
    except (TypeError, ValueError):
        return default


def _as_float(d: dict, key: str, default: float) -> float:
    try:
        return float(d.get(key, default))
    except (TypeError, ValueError):
        return default


def _as_bool(d: dict, key: str, default: bool) -> bool:
    v = d.get(key, default)
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return default


def _as_list(d: dict, key: str) -> list:
    v = d.get(key)
    return list(v) if isinstance(v, (list, tuple)) else []


def _as_dict(d: dict, key: str) -> dict:
    v = d.get(key)
    return dict(v) if isinstance(v, dict) else {}


def _as_variants(d: dict, key: str) -> dict[str, list[str]]:
    """{lang: [non-empty str, …]} — a bare string becomes a one-item list."""
    out: dict[str, list[str]] = {}
    for lang, lines in _as_dict(d, key).items():
        if isinstance(lines, str):
            lines = [lines]
        if isinstance(lines, (list, tuple)):
            clean = [str(x).strip() for x in lines if str(x or "").strip()]
            if clean:
                out[str(lang)] = clean
    return out


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

@dataclass
class AgentIdentity:
    """Who the agent is and what company it represents."""
    agent_name: str           # spoken name, e.g. "Vishal Patel"
    company_name: str         # e.g. "Pramukh Group"
    company_tagline: str      # short brand descriptor for the system prompt
    role_description: str     # e.g. "inbound property sales consultant"
    contact: str              # phone / WhatsApp shown to callers, e.g. "+91 87583 70000"
    pronouns: str = "she"     # legacy field — prefer personality.pronouns; kept for backward compat
    # Full replacement for the hardcoded "warm, genuine, experienced
    # {role_description}" identity framing. '' = today's exact f-string.
    role_line: str = ""


# ---------------------------------------------------------------------------
# Personality
# ---------------------------------------------------------------------------

@dataclass
class PersonalityConfig:
    """Controls the agent's speaking style, energy, and overall vibe."""
    pronouns: str = "she"              # "she" or "he" — enforced in all languages
    energy: str = "medium"             # "low" | "medium" | "high"
    tone: str = "conversational"       # "professional" | "conversational" | "friendly" | "formal"
    style: str = "natural"             # "concise" | "natural" | "detailed"
    lingo: str = ""                    # vocabulary/jargon preferences
    custom_instructions: str = ""      # extra vibe or persona notes
    traits: list[str] = field(default_factory=list)   # e.g. ["patient", "witty"]
    energy_mirroring: bool = True      # mirror the caller's energy (today's behaviour)
    # deflect | admit | admit_if_pressed — 'deflect' = today's hardcoded
    # "You are NOT reading a script" line, no disclosure behaviour change.
    ai_disclosure: str = "deflect"
    verbosity_max_sentences: int = 3   # "N sentences max per response" prompt rule
    # Per-call variety. ONE of these is picked at random when a call starts
    # and rendered as ━━ THIS CALL ━━ for the whole call — a conversational
    # angle ("lead with curiosity about their trade", "be brisk, they are
    # busy"). Without it every call is the same prompt at the same
    # temperature, and an owner test-calling their own agent hears the same
    # script each time (reported 2026-09-18). [] = no band, prompt unchanged.
    style_variants: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Brand (NEW — AgentSpec v2)
# ---------------------------------------------------------------------------

@dataclass
class BrandConfig:
    """Structured brand identity. All-empty = the exact legacy BRAND lines
    rendered from identity/knowledge_base via the effective_* resolvers."""
    spoken_company_name: str = ""      # fallback identity.company_name
    positioning_tagline: str = ""      # fallback identity.company_tagline
    credibility_markers: list[str] = field(default_factory=list)
    trust_proofs: list[str] = field(default_factory=list)   # top 2-3, objection ammo
    regions_served: list[str] = field(default_factory=list)  # fallback kb.locations
    contact_phone: str = ""            # fallback identity.contact
    whatsapp_number: str = ""          # fallback contact_phone


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------

@dataclass
class TTSVoiceConfig:
    """TTS model + speaker for one language."""
    model: str = DEFAULT_TTS_MODEL   # must be one of VALID_TTS_MODELS
    speaker: str = DEFAULT_TTS_SPEAKER
    pace: float = 1.0  # speaking rate; >1.0 = faster (snappier phone delivery)
    gender: str = ""   # informational only (warn-only validation)

    def __post_init__(self) -> None:
        model = (self.model or "").strip()
        if model not in VALID_TTS_MODELS:
            # A speaker name sometimes lands in the model field (e.g.
            # model: shubh). Salvage it as the speaker if that slot is empty,
            # then fall back to the platform default model.
            if model and not (self.speaker or "").strip():
                self.speaker = model.lower()
            if model:
                logging.getLogger(__name__).warning(
                    "[TTSVoiceConfig] invalid tts model %r — using %s",
                    self.model, DEFAULT_TTS_MODEL)
            self.model = DEFAULT_TTS_MODEL
        if not (self.speaker or "").strip():
            self.speaker = DEFAULT_TTS_SPEAKER
        # Speaker↔model compatibility (dead-air prevention, 2026-07-24): the
        # bulbul:v3 roster is verified against the live API — a legacy v2-only
        # speaker (abhilash, anushka, …) riding a v3 model 400s every
        # utterance and the call goes silent. Repair to the same-gender v3
        # default. UNKNOWN speakers pass through untouched (warn-only
        # doctrine), and non-v3 models are left alone (v2 roster unverified).
        _spk = self.speaker.strip().lower()
        _known_models = speaker_models(_spk)
        if (_known_models is not None
                and self.model in ("bulbul:v3", "bulbul:v3-beta")
                and self.model not in _known_models):
            _fb = "simran" if speaker_gender(_spk) == "female" else "shubh"
            logging.getLogger(__name__).warning(
                "[TTSVoiceConfig] speaker %r is not available on %s — using %s",
                _spk, self.model, _fb)
            self.speaker = _fb
        try:
            self.pace = float(self.pace)
        except (TypeError, ValueError):
            self.pace = 1.0
        if not (0.5 <= self.pace <= 2.0):
            # Reset silently and a user typing pace: 2.5 gets SLOWER speech
            # than the 1.15 fleet default with no explanation.
            logging.getLogger(__name__).warning(
                "[TTSVoiceConfig] pace %s outside 0.5–2.0 — using 1.0",
                self.pace)
            self.pace = 1.0
        if not isinstance(self.gender, str):
            self.gender = ""


@dataclass
class LanguageConfig:
    """Which languages the agent speaks and how."""
    supported: list[str]                    # BCP-47 short codes, e.g. ["hi", "en", "gu"]
    default: str                            # used before caller's language is detected
    tts_voices: dict[str, TTSVoiceConfig]   # lang_code -> TTS config
    # Per-language style guidance injected into the system prompt.
    # Describes register, vocabulary preferences, code-switching norms.
    register_notes: dict[str, str]          # lang_code -> guidance paragraph
    # When set (e.g. "hi-IN"), STT is LOCKED to this language for every turn
    # instead of Sarvam auto-detecting per segment. Use for single-language
    # agents where auto-detect misfires (e.g. Hindi mis-tagged as Punjabi/
    # Tamil/Malayalam). Empty string = auto-detect (default behaviour).
    stt_forced_language: str = ""
    # RUNBOOK §81: with STT locked, a short answer to an ENUM question that
    # matches none of its values is re-transcribed once WITHOUT the lock,
    # and the re-read is used only if it names a value. English option
    # words under hi-IN ("Amenities" → "हमें देखिए", call 1092). Off by default.
    stt_enum_recheck: bool = False
    # RUNBOOK §82: every figure written in DIGITS is spoken in English
    # ("fifty to fifty five lakh", "two point one five crore") — Bulbul read
    # "₹50-55 लाख" digit by digit in Hindi and voiced the hyphen. Only the
    # text handed to TTS changes (voice.spoken_numbers); the transcript and
    # the rest of the line are untouched. Off by default.
    tts_numbers_english: bool = False
    # When True, the LLM is told to reply in natural Hinglish (Hindi freely
    # code-mixed with everyday English words) instead of being forced into a
    # single pure language. Makes phone conversations sound human. Default
    # False preserves the strict single-language behaviour for other agents.
    allow_code_mixing: bool = False
    # When True (platform default), the TTS target_language_code is derived
    # from the script/markers of the text actually being spoken, so the
    # accent always matches the words. False restores the legacy behaviour
    # (accent follows conversation state — English replies could go out
    # hi-IN). Per-agent opt-out for TTS_LANG_FROM_TEXT.
    tts_lang_from_text: bool = True
    # When True (platform default), mid-call language switches require word
    # EVIDENCE inside the utterance (density-scored votes; see conversation
    # _evidence_switch_decision) instead of a single word/label. False
    # restores the legacy single-signal switching. Per-agent opt-out for
    # LANG_EVIDENCE_SWITCH.
    evidence_switch: bool = True
    # Mid-call language switching, ON by default and STRICTLY BOUNDED BY
    # `supported`: if the caller speaks one of the agent's languages, the
    # agent follows them into it; it can never be dragged into a language
    # this agent was not built for. Set False for an agent that must answer
    # in `default` no matter what the caller speaks.
    auto_switch: bool = True
    # Language of the opening greeting ('' = languages.default — today).
    greeting_language: str = ""
    # Hint that the greeting should be bilingual (informational for now).
    bilingual_greeting: bool = False
    # How the language of closing-flavoured lines (closing_responses,
    # decline_farewell, wrong_number_close) is chosen.
    # ''         = today: state.language at the moment of the final utterance
    #              (the single most misdetection-prone turn — 'bye bye thank
    #              you' flips a Hindi call to English one turn before close).
    # 'dominant' = majority language of the caller's turns this call.
    closing_language_policy: str = ""


# ---------------------------------------------------------------------------
# Calendar / Appointment Slots
# ---------------------------------------------------------------------------

@dataclass
class AppointmentSlot:
    """A single bookable time window."""
    day: str    # e.g. "Tomorrow", "This Saturday"
    time: str   # e.g. "10:00 AM"


@dataclass
class CalendarConfig:
    """Appointment availability configuration."""
    type: str                           # "mock" | "webhook" (Phase 2+)
    mock_slots: list[AppointmentSlot]   # used when type == "mock"
    # Localised slot-list strings for injection into the system prompt.
    # Keyed by the same short lang codes used in LanguageConfig.supported.
    slots_text: dict[str, str]
    # Canonical slot-day label -> caller phrases, consulted BEFORE the
    # hardcoded Today/Tomorrow map. {} = today's exact behaviour.
    day_synonyms: dict[str, list[str]] = field(default_factory=dict)
    timezone: str = "Asia/Kolkata"
    booking_confirmation_line: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Call Flow / Stage Machine
# ---------------------------------------------------------------------------

@dataclass
class StageDefinition:
    """One stage in the conversation flow."""
    id: str     # matches the STAGE_* constants in conversation.py
    order: int  # position in the linear sequence (0-indexed)
    # LLM stage goal — what the agent must accomplish before advancing.
    # Written in English; the LLM responds in the detected caller language.
    goal: str
    # After this many turns in this stage, auto-advance to the next stage.
    # Override 99 to block automatic advancement (use booking events only).
    turns_until_advance: int = 8
    # v2 — all defaults reproduce the legacy linear turn-count machine:
    # greeting|confirm_person|discovery|pitch|recommend|scheduling|
    # confirmation|closing|custom. '' = derived from the legacy id.
    role: str = ""
    label: str = ""                     # human display label
    collect: list[str] = field(default_factory=list)   # slot names captured here
    # {all_collect_filled: bool, slots: [...], min_turns: int, intent: str,
    #  appointment_captured: bool, max_turns: int, goto: STAGE_ID}
    #  — {} = turn-count-only advancement.
    # `goto` names where this stage's advancement LANDS. Without it,
    # advancement is list-adjacency, so a stage that exists to catch ONE
    # early-exit path escapes into whatever happens to sit next in the YAML.
    # Two stages that both converge on the same closing stage cannot both be
    # adjacent to it; `goto` is how the second one says so (RUNBOOK §37).
    advance_when: dict = field(default_factory=dict)
    # [{when_intent: ..., when_slot: {name, equals?}, goto: STAGE_ID,
    #   max_takes: 1, actions: [...]}] — [] = no branching. `equals` omitted
    # on when_slot means "this slot has ANY answer" — the gate for a
    # free-text follow-up (a reason, a challenge) that has no fixed
    # vocabulary to match a value against (RUNBOOK §35).
    branches: list[dict] = field(default_factory=list)
    # RUNBOOK §75 — a SCRIPTED stage the engine holds to its lines, after
    # the model's reply (the model is told the lines too; this is the floor):
    #   ask_verbatim: true → when the directive asked slot X and the reply
    #     did not ask it (no question about X in it), X's authored question
    #     is spoken after the reply, word for word;
    #   after_answer: {lang: line} → spoken after every reply of the stage
    #     unless the reply already asks it, asks a permission-to-close
    #     question, or carries one of the `unless` lines;
    #   unless: [str] — lines (any language) whose presence suppresses it.
    #   {} = nothing (every pre-existing agent).
    script: dict = field(default_factory=dict)


@dataclass
class SlotDefinition:
    """One piece of information the agent must collect during the call.

    Consumed by app/voice/state_manager.py: slots drive entity extraction,
    missing-field detection, and dynamic question generation. Optional per
    agent — agents without a `slots:` section get a sensible default set
    derived from their qa_field_signals.
    """
    name: str                                   # canonical key, e.g. "location"
    priority: int = 100                         # lower = asked earlier
    required: bool = True                       # counts toward missing-slot list
    label: str = ""                             # display label ("Configuration")
    question: dict[str, str] = field(default_factory=dict)  # lang -> question
    # v2 — '' / empty defaults keep the legacy name-keyed extractors:
    # ''|text|number|money|enum|boolean|phone|date|time|location
    type: str = ""
    enum_values: list[str] = field(default_factory=list)
    enum_synonyms: dict[str, list[str]] = field(default_factory=dict)
    extract_patterns: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)   # never ASKED before these fill
    merge_policy: str = "free"                  # free|correction_gated|confirm
    # RUNBOOK §75: written ONLY as the answer to its own question (or on an
    # explicit correction) — for a vocabulary of everyday domain words that
    # callers also say in questions ("location बताओ", "amenities क्या हैं").
    ask_gated: bool = False
    confirm_readback: str = ""                  # ''|verbatim|digit_groups_2
    max_asks: int = 0                           # 0 = unlimited (legacy)
    reask_question: dict[str, str] = field(default_factory=dict)
    summary_known: str = ""                     # '{value}' template for the lead summary
    summary_unknown: str = ""


@dataclass
class CallFlowConfig:
    """Ordered list of stages and global flow parameters."""
    stages: list[StageDefinition]
    # Number of objections / deferrals before ending the call graciously.
    decline_threshold: int = 2
    # v2 — one-sentence mission rendered as ━━ CALL MISSION ━━ ('' = no block)
    call_goal: str = ""
    # {required_slots: [...], require_appointment: bool, gate_closing: bool}
    completed_when: dict = field(default_factory=dict)
    # {offer_count: 2, style: exact_n|flexible, allow_caller_proposed: bool}
    scheduling: dict = field(default_factory=dict)
    # auto|on|off — may the agent propose an appointment before it has named
    # a specific catalogue item to this caller? 'auto' (default) says no
    # whenever the flow declares a recommendation stage (role: recommend, or
    # the legacy PROJECT_MATCH id) AND the catalogue is non-empty; 'on'
    # enforces it for any agent with a catalogue; 'off' disables the gate.
    # See ConversationRouter._recommendation_gate_open.
    recommend_before_schedule: str = "auto"
    # {enabled: false, capture_slot: callback_time, responses: {lang: str},
    #  counts_as_decline: true}
    callback_policy: dict = field(default_factory=dict)
    # Call-termination policy (all keys optional; {} = platform defaults):
    #   llm_end_call: bool (default True) — the LLM may end the call by
    #     appending [END_CALL] after its goodbye line; the engine strips the
    #     token and arms the platform hangup.
    #   confirm_before_end: bool (default True) — before acting on a heard
    #     goodbye (caller farewell or the LLM's own end-call signal) the
    #     agent asks one confirmation question and only hangs up if the
    #     caller says yes. Guards against a misheard "bye". Does not apply
    #     to DND / wrong-number / silence hangup / max_call_minutes.
    #   end_conditions: [str] — plain-English business rules rendered into
    #     the system prompt as ━━ END-CALL RULES ━━ ("once the visit is
    #     booked and confirmed, wrap up", "if the caller is abusive, end").
    #   farewell_patterns: [str] — extra caller phrases (any language /
    #     script) that count as a goodbye and close deterministically.
    #   silence_hangup: {enabled: bool, grace_s: float} — after the dead-air
    #     reprompt cap is spent, say goodbye and end instead of holding the
    #     line open forever.
    #   max_call_minutes: number — hard duration cap (0 = no cap); the agent
    #     delivers one wrap-up line and the platform ends the call.
    termination: dict = field(default_factory=dict)
    # Connect-window behaviour (all keys optional; {} = platform defaults —
    # every protection ON). Counters the repeated-"hello" loop at call start:
    #   hello_fastpath: bool (default True) — a bare "hello?" early in the
    #     call gets an instant full identity intro instead of an LLM turn.
    #   delivery_guard: bool (default True) — during the connect window a
    #     short burst of caller speech cannot cancel an agent reply the
    #     caller has not heard yet; the kill is deferred until STT proves
    #     the interruption was more than a "hello?".
    #   intro_line: {lang: str} (or a bare string) — THE SECOND BEAT. The
    #     exact sentence the caller hears the moment they first respond,
    #     after a short opening like "Hello". {contact_name},
    #     {current_time} and {honorific} ARE substituted here, exactly as in
    #     the opening greeting ({honorific} resolving to nothing when the
    #     contact carries no gender).
    #     Empty (the default) = the platform composes a line from
    #     identity.agent_name + identity.company_name. A value that is
    #     itself a bare hello is ignored (it would re-open the parrot loop).
    connect: dict = field(default_factory=dict)
    # Turn-taking overrides (all keys optional; {} = platform defaults, every
    # other agent byte-identical). RUNBOOK §78 — a scripted agent that must
    # never answer half a sentence and never talk over the caller:
    #   pause_hold_ms: number — EVERY caller pause is held at least this long
    #     before the turn is concluded, so a sentence split by a long or
    #     unnatural pause is answered ONCE (the platform holds only narrative
    #     / splitter callers). Costs that much latency on every reply; the
    #     hold ends early the moment the caller resumes.
    #   max_merged_fragments: int — pieces one utterance may be rebuilt from
    #     inside that hold (platform default 4).
    #   backchannel_guard: bool — a bare acknowledgement ("हाँ", "जी", "हम्म",
    #     "okay") spoken while the agent's audio still has ≥1 s to play is a
    #     listener's back-channel: dropped, never a turn, never a stage
    #     advance.
    #   sustained_barge_ms: number — a barge-in the delivery guard deferred
    #     (reply not yet heard) is carried out anyway once the caller's
    #     speech has gone on this long (a "hello?" is under 400 ms).
    #   tail_barge_in: bool — caller speech that is NOT a back-channel,
    #     arriving while the agent's audio is still playing after synthesis
    #     finished, stops that audio instead of queueing the reply behind it.
    turn_taking: dict = field(default_factory=dict)

    # Convenience accessors -------------------------------------------------

    @property
    def stage_goals(self) -> dict[str, str]:
        return {s.id: s.goal for s in self.stages}

    @property
    def stage_order(self) -> dict[str, int]:
        return {s.id: s.order for s in self.stages}


# ---------------------------------------------------------------------------
# Response Bank — all multilingual canned responses
# ---------------------------------------------------------------------------

@dataclass
class ResponseBank:
    """
    Every canned response the agent can emit without calling the LLM.

    Keys for per-language dicts are the same short codes used in
    LanguageConfig.supported ("hi", "en", "gu", …).
    """
    # The opening utterance the agent delivers when the call connects.
    # Usually bilingual so the caller can reply in their preferred language.
    opening_greeting: str = ""

    # Played when the agent is waiting >FILLER_TIMEOUT_S for the LLM.
    filler_responses: dict[str, list[str]] = field(default_factory=dict)

    # "Who is calling?" — fast deterministic answer
    identity_responses: dict[str, str] = field(default_factory=dict)

    # First objection / deferral ("call me later", "not interested")
    objection_responses: dict[str, list[str]] = field(default_factory=dict)

    # Second objection — warm end of call
    decline_farewell: dict[str, str] = field(default_factory=dict)

    # Language-switch confirmation ("Sure, let's speak in Hindi")
    lang_switch_responses: dict[str, str] = field(default_factory=dict)

    # Spoken when the caller asks for a language this agent was NOT built
    # for ("do you speak Tamil?"). Empty = the agent simply answers in the
    # language it is already speaking.
    lang_unsupported_responses: dict[str, str] = field(default_factory=dict)

    # "I couldn't hear you / please repeat"
    lang_fallbacks: dict[str, str] = field(default_factory=dict)

    # LLM timed out mid-stream — spoken while retrying silently
    timeout_fallbacks: dict[str, str] = field(default_factory=dict)

    # Spoken when the model answered a turn with a tool call and no words,
    # and the text re-ask did not land either. Last line of defence against
    # dead air on a turn where an action WAS taken. Empty = platform default.
    tool_ack_fallbacks: dict[str, str] = field(default_factory=dict)

    # Absolute last resort — single universal string, no LLM
    response_fallback: str = ""

    # Warm close when all mandatory fields are collected and confirmed
    graceful_close: dict[str, str] = field(default_factory=dict)

    # Intent-keyed responses for common shortcut intents (greeting, farewell,
    # gratitude, closing). Values can be str or list[str] (rotated).
    localized_responses: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Stage-specific proactive nudges when the LLM hasn't naturally advanced.
    # Keyed by stage ID then by language.
    proactive_prompts: dict[str, dict[str, str]] = field(default_factory=dict)

    # Optional: alternate decline/end-of-call responses (override decline_farewell)
    decline_responses: dict[str, list[str]] = field(default_factory=dict)  # lang -> [resp, ...]

    # Optional: template for presenting appointment slots ("Available: {slots}")
    slots_prompt: dict[str, str] = field(default_factory=dict)  # lang -> template

    # v2 — D3: objection playbook feeds BOTH the deterministic shortcut and
    # the prompt block. [{id, patterns: [...], answer: {lang: str},
    # prompt_guidance: str}]. [] = today's 4 hardcoded prompt lines.
    objection_playbook: list[dict] = field(default_factory=list)

    # v2 — D9: real farewell-path closing line. {} = today's farewell chain.
    closing_responses: dict[str, str] = field(default_factory=dict)

    # v2 — deterministic canned phrases: [{patterns: [...], responses: {lang}}]
    canned_phrases: list[dict] = field(default_factory=list)

    # v2 — spoken when personality.ai_disclosure is admit/admit_if_pressed
    ai_disclosure_responses: dict[str, str] = field(default_factory=dict)

    # v2 — off = today's behaviour (Pramukh has localized greeting entries;
    # a default-on shortcut would change its live hi/gu "hello" turns).
    enable_greeting_shortcut: bool = False

    # ALTERNATIVE opening lines, {lang: [line, …]}. At call start the engine
    # picks at random among opening_greeting and these, so the first thing a
    # contact hears is not identical on every call. A separate field on
    # purpose: opening_greeting stays the single string the Builder, the
    # Copilot normalizer ("was a list — kept the first line only") and every
    # preview already understand. {} = exactly one opening, as before.
    opening_variants: dict[str, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Knowledge Base
# ---------------------------------------------------------------------------

@dataclass
class STTCorrections:
    """
    Domain-specific text substitutions applied to raw STT output.

    Each dict maps a lowercase source substring to its corrected form.
    All corrections are applied case-insensitively before intent detection.
    Agents that need no correction may omit any or all fields.
    """
    city_corrections: dict[str, str] = field(default_factory=dict)
    phonetic_corrections: dict[str, str] = field(default_factory=dict)
    real_estate_corrections: dict[str, str] = field(default_factory=dict)
    project_name_corrections: dict[str, str] = field(default_factory=dict)
    # v2 — false disables the unconditional built-in Pramukh tables in
    # post_processor.py. true = today's behaviour for every existing agent.
    use_builtin_tables: bool = True


@dataclass
class KnowledgeBase:
    """
    Domain knowledge used for project/product matching and STT correction.

    known_projects:
        Maps lowercase substring keys to (canonical_name, location) tuples.
        Keys are sorted longest-first at lookup time so multi-word names
        (e.g. "aranya ii") match before shorter substrings (e.g. "aranya").

    locations:
        Canonical location names the agent serves. Used for STT correction
        (fuzzy-matched against caller speech) and for building the system
        prompt geography section.

    portfolio_prompt_text:
        The full plain-English paragraph injected into the system prompt's
        PROJECT PORTFOLIO section.

    location_aliases:
        All script/language variants of location names (Latin, Devanagari,
        Gujarati, etc.) used by detect_intent() for location_selection intent.

    objection_patterns / time_patterns:
        Substring lists for deterministic intent classification. If empty,
        the corresponding intent type is never triggered by pattern matching.

    qa_field_signals:
        Maps qualification field names (e.g. "name", "budget") to lists of
        trigger phrases. Used by QATracker to prevent re-asking answered fields.

    stt_corrections:
        Domain-specific STT post-processing substitution tables.

    catalog (v2 — D16):
        Structured product list: [{id, name, location, configurations,
        price_range, availability, positioning_tag, pitch_points, aliases,
        facts}]. [] = portfolio_prompt_text is rendered verbatim (today).

    match_rules (v2):
        [{when: {budget: '<=40L', location: Vapi}, recommend: [ids],
          reason: '...'}] — deterministic recommendation candidates.

    pricing_policy / fact_policy / product_lock (v2):
        {} = today's exact behaviour (pricing on request; no fact lines;
        set-once product lock).

    primary_project (v3 — 2026-09-24, RUNBOOK §55):
        {id: <catalog id>, mode: exclusive|lead, reason: '...'} — the ONE
        catalogue item the agent pitches proactively. `exclusive` (the
        default once an id is set): the recommendation band carries only
        this item; every other catalogue entry is INFORMATION, answered
        exactly as written when the caller asks and never volunteered.
        `lead`: this item is always the first candidate and the match rules
        may follow it. An id the catalogue does not carry is ignored — a
        primary that cannot be rendered must not silently demote the rest.
        {} = no primary (today: match rules alone decide).
    """
    known_projects: dict[str, tuple[str, str]]  # key -> (canonical_name, location)
    locations: list[str]                         # e.g. ["Surat", "Vapi", "Silvassa"]
    portfolio_prompt_text: str                   # injected verbatim into system prompt
    # Label for the knowledge section injected into the LLM system prompt.
    # Override per-agent: "DOCTOR DIRECTORY", "PRODUCT CATALOGUE", etc.
    portfolio_section_label: str = "KNOWLEDGE BASE"
    location_aliases: list[str] = field(default_factory=list)
    objection_patterns: list[str] = field(default_factory=list)
    # How custom objection_patterns combine with the platform's universal
    # deferral set at intent-detection time.
    # 'replace' = today: any custom pattern list REPLACES the ~28 universal
    #             phrases (load-bearing for both Pramukh agents).
    # 'extend'  = custom patterns are ADDED to the universal set — authoring
    #             5 domain phrases no longer disables 'not interested' etc.
    objection_patterns_mode: str = "replace"
    time_patterns: list[str] = field(default_factory=list)
    qa_field_signals: dict[str, list[str]] = field(default_factory=dict)
    stt_corrections: STTCorrections = field(default_factory=STTCorrections)
    catalog: list[dict] = field(default_factory=list)
    match_rules: list[dict] = field(default_factory=list)
    pricing_policy: dict = field(default_factory=dict)
    fact_policy: dict = field(default_factory=dict)
    product_lock: dict = field(default_factory=dict)
    # {id, mode: exclusive|lead, reason} — see the class docstring.
    primary_project: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Outbound scripts (NEW — AgentSpec v2, D6)
# ---------------------------------------------------------------------------

@dataclass
class OutboundConfig:
    """Owns ALL outbound-call scripting (steps 1-2 of an outbound flow plus
    wrong-number/wrong-person/busy handling). All-empty = today's exact
    direction-block prose."""
    identity_confirm_line: dict[str, str] = field(default_factory=dict)
    good_time_check_line: dict[str, str] = field(default_factory=dict)
    pitch: dict[str, str] = field(default_factory=dict)
    wrong_person_response: dict[str, str] = field(default_factory=dict)
    busy_response: dict[str, str] = field(default_factory=dict)
    wrong_number_close: dict[str, str] = field(default_factory=dict)
    wrong_number_patterns: list[str] = field(default_factory=list)
    # RUNBOOK §73: asked ONCE after a "no" / "wrong number" to the identity
    # question, before the call is closed with wrong_number_close. {} = the
    # platform's built-in question in the call's language.
    wrong_number_reconfirm_line: dict[str, str] = field(default_factory=dict)
    opening_greeting: dict[str, str] = field(default_factory=dict)  # outbound variant
    direction_brief: str = ""
    # Alternatives to outbound.opening_greeting — see
    # ResponseBank.opening_variants. {lang: [line, …]}; {} = one opening.
    opening_variants: dict[str, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Guardrails (NEW — AgentSpec v2, D4)
# ---------------------------------------------------------------------------

@dataclass
class GuardrailsConfig:
    """NEVER/ALWAYS rules + honesty/compliance/escalation policy.
    Defaults = today's FIRM RULES band byte-for-byte."""
    use_builtin_rules: bool = True
    # Which builtin FIRM RULES pack renders when use_builtin_rules is true.
    # 'real_estate' = the original band (site-visit/property/portfolio lines).
    # 'generic'     = industry-neutral variant: keeps every domain-neutral
    #                 hygiene rule, swaps the site-visit/property/portfolio
    #                 lines for appointment/catalogue equivalents.
    #
    # DEFAULT IS 'generic' since 2026-08-04. It used to be 'real_estate', so
    # any agent that never set the field inherited property rules: an audit
    # that day found 12 of 15 such agents were NOT real estate — an education
    # counsellor, a healthcare clinic, a fabric mill and a shoe shop were all
    # being told to push the caller toward a site visit. (This is the same
    # failure the 'generic' pack was introduced for: "a fabric agent carrying
    # 'push toward a site visit' is how the Kaapd call went off-script".)
    # The three genuine real-estate agents now pin the pack explicitly, so
    # flipping the default changed nothing for them.
    #
    # honesty may additionally carry knowledge_grounding: 'strict' → renders
    # the KNOWLEDGE GROUNDING band (deflect-not-invent) after the knowledge
    # section. Absent = today (no band).
    builtin_rules_pack: str = "generic"
    never_rules: list[str] = field(default_factory=list)   # '✗ NEVER <rule>'
    always_rules: list[str] = field(default_factory=list)  # '✓ <rule>'
    # {unknown_answer_policy: '', forbidden_fabrications: [prices, dates]}
    honesty: dict = field(default_factory=dict)
    # {disclaimers: [], consent_line: '', dnd_phrases: [], restricted_topics: []}
    compliance: dict = field(default_factory=dict)
    # {enabled: false, action: offer_callback, triggers: [], handoff_message: {}}
    escalation: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Follow-up (NEW — AgentSpec v2, D7)
# ---------------------------------------------------------------------------

@dataclass
class FollowUpConfig:
    """Post-call promises the agent may make (WhatsApp media, callbacks)."""
    # {enabled: false, assets: [], promise_line: {lang: str}}
    whatsapp_promise: dict = field(default_factory=dict)
    # {enabled: false, default_delay_hours: 24}
    callback: dict = field(default_factory=dict)
    # In-call vague-commitment clarification (PLAN-intelligence-layer §2.8):
    # when the caller makes a vague promise ("call me next week"), ask exactly
    # ONE polite clarifying question so the follow-up can be scheduled
    # precisely. Platform universal rule (2026-07-27): ON by default for all
    # agents; from_dict injects enabled=True unless the YAML says
    # {enabled: false}.
    clarify_commitments: dict = field(default_factory=dict)
    # Follow-up execution default for THIS agent's calls. When enabled,
    # yes-decision follow-ups are created with mode='auto' and the platform's
    # auto-dialer places the call at the scheduled time (subject to every
    # autodial guardrail + the INTEL_AUTODIAL_ENABLED kill switch). Platform
    # universal rule (2026-07-27): ON by default for all agents; opt out with
    # {enabled: false}.
    auto_execute: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Post-call (NEW — AgentSpec v2, D15)
# ---------------------------------------------------------------------------

@dataclass
class PostCallConfig:
    """Success definition, outcome taxonomy, scoring, lead profile."""
    # {mandatory_slots: [], success_outcome: '', gate_closing: false}
    success_definition: dict = field(default_factory=dict)
    # [{id, label, kind: success|progress|failure, description}]
    outcomes: list[dict] = field(default_factory=list)
    # {enabled: false, auto_run: false, criteria_prompt: '', score_labels: []}
    scoring: dict = field(default_factory=dict)
    # {summary_template: {}, include_slots: []}
    lead_profile: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# LLM Configuration
# ---------------------------------------------------------------------------

@dataclass
class LLMConfig:
    """
    All LLM-related tunables that were previously scattered as module-level
    constants in conversation.py.
    """
    model: str                      # e.g. "gpt-4o-mini"
    max_tokens: int                 # generation cap per response
    timeout_s: float                # per-turn LLM call timeout
    stream_timeout_s: float         # hard deadline for streaming response
    filler_timeout_s: float         # how long to wait before playing a filler
    intent_classify_timeout_s: float
    max_response_length: int        # character cap before truncation
    # Filler frequency governors (0/0/False = today's exact behaviour:
    # a filler on EVERY turn whose first LLM chunk exceeds filler_timeout_s).
    # min_gap_turns: suppress a filler within N turns of the previous one.
    # max_per_call: hard cap per call. ack_extended: also recognise
    # Devanagari/Gujarati-script affirmations as simple acks (so approvals
    # get the short approval filler, not the 'let me check' one).
    filler_min_gap_turns: int = 0
    filler_max_per_call: int = 0
    filler_ack_extended: bool = False
    # Native LLM tool calling (deploy/PLAN-llm-tool-calling.md):
    #   "off"    — no tools anywhere.
    #   "shadow" — spoken path unchanged; the background extractor uses the
    #              tool roster and action proposals are logged only (P1).
    #   "on"     — tools on the main streaming call, executed through the
    #              deterministic gates (P2/P3).
    # Env LLM_TOOLS_MODE overrides this for every agent (kill switch).
    enable_tools: str = "shadow"
    # Additional domain-specific NEVER/ALWAYS rules appended to the
    # platform's built-in rules in the system prompt. (D4: from_dict aliases
    # these into guardrails when guardrails carries no rules of its own —
    # see the aliasing note in from_dict; activation is a P7 flagged change.)
    extra_never_rules: list[str] = field(default_factory=list)
    extra_always_rules: list[str] = field(default_factory=list)
    # Whether record_customer_details is offered on the MAIN (spoken) LLM
    # call in tools "on" mode. True = today. With it on the roster the model
    # answers most turns with the tool call and NO words, and the engine has
    # to ask again for the sentence: measured over 518 live turns 2026-09-15/16
    # that second request ran on 77% of turns at a median 1,223 ms — of a
    # median 3,030 ms the caller waits in silence. False takes it off the
    # spoken path for this agent: action tools (book / callback / do-not-call
    # / end) stay, and details are captured by the background extractor,
    # which runs concurrently and costs the caller nothing.
    record_details_on_main_call: bool = True


# ---------------------------------------------------------------------------
# Top-level AgentDefinition
# ---------------------------------------------------------------------------

@dataclass
class AgentDefinition:
    """
    Complete, self-contained description of one voice agent.

    The platform runtime (ConversationRouter, VoicePipeline) reads this
    object at startup and operates entirely from it — no domain knowledge
    is embedded in the runtime code itself.
    """
    agent_id: str       # unique slug, e.g. "pramukh-v1"
    version: str        # semver string, e.g. "1.0.0"

    identity: AgentIdentity
    languages: LanguageConfig
    call_flow: CallFlowConfig
    knowledge_base: KnowledgeBase
    response_bank: ResponseBank
    calendar: CalendarConfig
    llm_config: LLMConfig
    personality: PersonalityConfig = field(default_factory=PersonalityConfig)
    # Optional conversation-state slot definitions (see SlotDefinition).
    slots: list[SlotDefinition] = field(default_factory=list)
    # "inbound"  — the agent answers calls: welcome the caller, listen first,
    #              resolve issues/requests/queries, book appointments and visits.
    # "outbound" — the agent places calls: confirm who answered, state the
    #              purpose, pitch products/services, and lead the conversation.
    call_direction: str = "inbound"
    # v2 sections — all-default instances reproduce pre-v2 behaviour exactly.
    brand: BrandConfig = field(default_factory=BrandConfig)
    outbound: OutboundConfig = field(default_factory=OutboundConfig)
    guardrails: GuardrailsConfig = field(default_factory=GuardrailsConfig)
    follow_up: FollowUpConfig = field(default_factory=FollowUpConfig)
    post_call: PostCallConfig = field(default_factory=PostCallConfig)
    # Persistent customer memory (Customer Intelligence Hub): pre-call context
    # injection for returning customers. {use_customer_context: true} —
    # default ON; the INTEL_PRECALL_CONTEXT env var is the platform
    # kill-switch. Pattern mirrors follow_up.clarify_commitments.
    memory: dict = field(default_factory=dict)
    # Server-managed audit fields — stamped by agent_definition/store.py,
    # stripped from LLM input by the Copilot normalizer, never read by the
    # runtime.
    revision: int = 0
    updated_at: str = ""
    updated_by: str = ""

    # ------------------------------------------------------------------
    # Brand resolvers — runtime reads ONLY these, guaranteeing old YAMLs
    # (empty brand) behave identically to pre-v2.
    # ------------------------------------------------------------------

    def effective_spoken_name(self) -> str:
        return self.brand.spoken_company_name or self.identity.company_name

    def effective_tagline(self) -> str:
        return self.brand.positioning_tagline or self.identity.company_tagline

    def effective_regions(self) -> list[str]:
        return self.brand.regions_served or list(self.knowledge_base.locations or [])

    def effective_contact_phone(self) -> str:
        return self.brand.contact_phone or self.identity.contact

    def effective_whatsapp_number(self) -> str:
        return (self.brand.whatsapp_number or self.brand.contact_phone
                or self.identity.contact)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, d: dict) -> "AgentDefinition":
        """Reconstruct an AgentDefinition from a plain nested dict (e.g. from YAML).

        Tolerant by contract: unknown keys are ignored, missing optional keys
        get defaults, wrong-typed values are coerced or dropped — a YAML that
        was ever valid keeps loading forever.
        """
        id_d = _as_dict(d, "identity") or dict(d.get("identity") or {})
        identity = AgentIdentity(
            agent_name=_as_str(id_d, "agent_name"),
            company_name=_as_str(id_d, "company_name"),
            company_tagline=_as_str(id_d, "company_tagline"),
            role_description=_as_str(id_d, "role_description"),
            contact=_as_str(id_d, "contact"),
            pronouns=_as_str(id_d, "pronouns", "she") or "she",
            role_line=_as_str(id_d, "role_line"),
        )

        lang_d = _as_dict(d, "languages")
        raw_supported = [str(x).strip() for x in _as_list(lang_d, "supported") if str(x).strip()]
        supported = []
        for code in raw_supported:
            if code in PLATFORM_LANGUAGES:
                supported.append(code)
            else:
                logger.warning(
                    "[schema] agent %r lists unsupported language %r — dropped",
                    d.get("agent_id"), code)
        if not supported:
            supported = raw_supported or ["en"]
        default_lang = _as_str(lang_d, "default") or supported[0]
        if default_lang not in supported:
            logger.warning(
                "[schema] agent %r default language %r not in supported %s — "
                "clamped to %r", d.get("agent_id"), default_lang, supported,
                supported[0])
            default_lang = supported[0]
        tts_voices = {}
        for k, v in _as_dict(lang_d, "tts_voices").items():
            if not isinstance(v, dict):
                v = {"speaker": str(v)} if isinstance(v, str) else {}
            tts_voices[k] = TTSVoiceConfig(
                model=_as_str(v, "model", DEFAULT_TTS_MODEL),
                speaker=_as_str(v, "speaker", DEFAULT_TTS_SPEAKER),
                pace=_as_float(v, "pace", 1.0),
                gender=_as_str(v, "gender"),
            )
        languages = LanguageConfig(
            supported=supported,
            default=default_lang,
            tts_voices=tts_voices,
            register_notes=_as_dict(lang_d, "register_notes"),
            stt_forced_language=_as_str(lang_d, "stt_forced_language"),
            stt_enum_recheck=_as_bool(lang_d, "stt_enum_recheck", False),
            tts_numbers_english=_as_bool(lang_d, "tts_numbers_english", False),
            allow_code_mixing=_as_bool(lang_d, "allow_code_mixing", False),
            tts_lang_from_text=_as_bool(lang_d, "tts_lang_from_text", True),
            evidence_switch=_as_bool(lang_d, "evidence_switch", True),
            auto_switch=_as_bool(lang_d, "auto_switch", True),
            greeting_language=_as_str(lang_d, "greeting_language"),
            bilingual_greeting=_as_bool(lang_d, "bilingual_greeting", False),
            closing_language_policy=_as_str(lang_d, "closing_language_policy"),
        )

        cal_d = _as_dict(d, "calendar")
        mock_slots = [
            AppointmentSlot(day=_as_str(s, "day"), time=_as_str(s, "time"))
            for s in _as_list(cal_d, "mock_slots") if isinstance(s, dict)
        ]
        day_synonyms = {
            str(k): [str(x) for x in v] if isinstance(v, (list, tuple)) else [str(v)]
            for k, v in _as_dict(cal_d, "day_synonyms").items()
        }
        calendar = CalendarConfig(
            type=_as_str(cal_d, "type", "mock") or "mock",
            mock_slots=mock_slots,
            slots_text=_as_dict(cal_d, "slots_text"),
            day_synonyms=day_synonyms,
            timezone=_as_str(cal_d, "timezone", "Asia/Kolkata") or "Asia/Kolkata",
            booking_confirmation_line=_as_dict(cal_d, "booking_confirmation_line"),
        )

        cf_d = _as_dict(d, "call_flow")
        stages = []
        for s in _as_list(cf_d, "stages"):
            if not isinstance(s, dict):
                continue
            stages.append(StageDefinition(
                id=_as_str(s, "id"),
                order=_as_int(s, "order", len(stages)),
                goal=_as_str(s, "goal"),
                turns_until_advance=_as_int(s, "turns_until_advance", 8),
                role=_as_str(s, "role"),
                label=_as_str(s, "label"),
                collect=[str(x) for x in _as_list(s, "collect")],
                advance_when=_as_dict(s, "advance_when"),
                branches=[b for b in _as_list(s, "branches") if isinstance(b, dict)],
                script=_as_dict(s, "script"),
            ))
        call_flow = CallFlowConfig(
            stages=stages,
            decline_threshold=_as_int(cf_d, "decline_threshold", 2),
            call_goal=_as_str(cf_d, "call_goal"),
            completed_when=_as_dict(cf_d, "completed_when"),
            scheduling=_as_dict(cf_d, "scheduling"),
            callback_policy=_as_dict(cf_d, "callback_policy"),
            termination=_as_dict(cf_d, "termination"),
            connect=_as_dict(cf_d, "connect"),
            turn_taking=_as_dict(cf_d, "turn_taking"),
            recommend_before_schedule=_as_str(
                cf_d, "recommend_before_schedule", "auto") or "auto",
        )

        kb_d = _as_dict(d, "knowledge_base")
        # YAML loads tuple values as lists — convert back to tuples.
        known_projects = {}
        for k, v in _as_dict(kb_d, "known_projects").items():
            if isinstance(v, (list, tuple)):
                known_projects[k] = tuple(v)
        stt_d = _as_dict(kb_d, "stt_corrections")
        stt_corrections = STTCorrections(
            city_corrections=_as_dict(stt_d, "city_corrections"),
            phonetic_corrections=_as_dict(stt_d, "phonetic_corrections"),
            real_estate_corrections=_as_dict(stt_d, "real_estate_corrections"),
            project_name_corrections=_as_dict(stt_d, "project_name_corrections"),
            use_builtin_tables=_as_bool(stt_d, "use_builtin_tables", True),
        )
        knowledge_base = KnowledgeBase(
            known_projects=known_projects,
            locations=[str(x) for x in _as_list(kb_d, "locations")],
            portfolio_prompt_text=_as_str(kb_d, "portfolio_prompt_text"),
            portfolio_section_label=_as_str(kb_d, "portfolio_section_label",
                                            "KNOWLEDGE BASE") or "KNOWLEDGE BASE",
            location_aliases=[str(x) for x in _as_list(kb_d, "location_aliases")],
            objection_patterns=[str(x) for x in _as_list(kb_d, "objection_patterns")],
            objection_patterns_mode=(
                _as_str(kb_d, "objection_patterns_mode", "replace") or "replace"),
            time_patterns=[str(x) for x in _as_list(kb_d, "time_patterns")],
            qa_field_signals=_as_dict(kb_d, "qa_field_signals"),
            stt_corrections=stt_corrections,
            catalog=[c for c in _as_list(kb_d, "catalog") if isinstance(c, dict)],
            match_rules=[m for m in _as_list(kb_d, "match_rules") if isinstance(m, dict)],
            pricing_policy=_as_dict(kb_d, "pricing_policy"),
            fact_policy=_as_dict(kb_d, "fact_policy"),
            product_lock=_as_dict(kb_d, "product_lock"),
            primary_project=_as_dict(kb_d, "primary_project"),
        )

        rb_d = _as_dict(d, "response_bank")
        response_bank = ResponseBank(
            opening_greeting=_as_str(rb_d, "opening_greeting"),
            filler_responses=_as_dict(rb_d, "filler_responses"),
            identity_responses=_as_dict(rb_d, "identity_responses"),
            objection_responses=_as_dict(rb_d, "objection_responses"),
            decline_farewell=_as_dict(rb_d, "decline_farewell"),
            lang_switch_responses=_as_dict(rb_d, "lang_switch_responses"),
            lang_unsupported_responses=_as_dict(
                rb_d, "lang_unsupported_responses"),
            lang_fallbacks=_as_dict(rb_d, "lang_fallbacks"),
            timeout_fallbacks=_as_dict(rb_d, "timeout_fallbacks"),
            tool_ack_fallbacks=_as_dict(rb_d, "tool_ack_fallbacks"),
            response_fallback=_as_str(rb_d, "response_fallback"),
            graceful_close=_as_dict(rb_d, "graceful_close"),
            localized_responses=_as_dict(rb_d, "localized_responses"),
            proactive_prompts=_as_dict(rb_d, "proactive_prompts"),
            decline_responses=_as_dict(rb_d, "decline_responses"),
            slots_prompt=_as_dict(rb_d, "slots_prompt"),
            objection_playbook=[o for o in _as_list(rb_d, "objection_playbook")
                                if isinstance(o, dict)],
            closing_responses=_as_dict(rb_d, "closing_responses"),
            canned_phrases=[c for c in _as_list(rb_d, "canned_phrases")
                            if isinstance(c, dict)],
            ai_disclosure_responses=_as_dict(rb_d, "ai_disclosure_responses"),
            enable_greeting_shortcut=_as_bool(rb_d, "enable_greeting_shortcut", False),
            opening_variants=_as_variants(rb_d, "opening_variants"),
        )

        llm_d = _as_dict(d, "llm_config")
        llm_config = LLMConfig(
            model=_as_str(llm_d, "model", "gpt-4o-mini") or "gpt-4o-mini",
            max_tokens=_as_int(llm_d, "max_tokens", 120),
            timeout_s=_as_float(llm_d, "timeout_s",
                                _as_float(llm_d, "timeout_seconds", 8.0)),
            stream_timeout_s=_as_float(llm_d, "stream_timeout_s", 10.0),
            filler_timeout_s=_as_float(llm_d, "filler_timeout_s", 1.5),
            intent_classify_timeout_s=_as_float(llm_d, "intent_classify_timeout_s", 1.0),
            max_response_length=_as_int(llm_d, "max_response_length", 400),
            filler_min_gap_turns=_as_int(llm_d, "filler_min_gap_turns", 0),
            filler_max_per_call=_as_int(llm_d, "filler_max_per_call", 0),
            filler_ack_extended=_as_bool(llm_d, "filler_ack_extended", False),
            extra_never_rules=[str(x) for x in _as_list(llm_d, "extra_never_rules")],
            extra_always_rules=[str(x) for x in _as_list(llm_d, "extra_always_rules")],
            enable_tools=(lambda m: m if m in ("off", "shadow", "on") else "shadow")(
                (_as_str(llm_d, "enable_tools", "shadow") or "shadow").lower()),
            record_details_on_main_call=_as_bool(
                llm_d, "record_details_on_main_call", True),
        )

        _MERGE_POLICIES = ("free", "correction_gated", "confirm")
        _READBACKS = ("", "verbatim", "digit_groups_2", "spelled")
        _SLOT_TYPES = ("", "text", "number", "money", "enum", "boolean",
                       "phone", "email", "date", "time", "location")
        slots = []
        for s in (d.get("slots") or []):
            if not isinstance(s, dict) or not s.get("name"):
                continue
            merge_policy = _as_str(s, "merge_policy", "free")
            if merge_policy not in _MERGE_POLICIES:
                merge_policy = "free"
            readback = _as_str(s, "confirm_readback")
            if readback not in _READBACKS:
                readback = ""
            slot_type = _as_str(s, "type")
            if slot_type not in _SLOT_TYPES:
                slot_type = ""
            enum_synonyms = {
                str(k): [str(x) for x in v] if isinstance(v, (list, tuple)) else [str(v)]
                for k, v in _as_dict(s, "enum_synonyms").items()
            }
            slots.append(SlotDefinition(
                name=str(s["name"]),
                priority=_as_int(s, "priority", 100),
                required=_as_bool(s, "required", True),
                label=_as_str(s, "label"),
                question=_as_dict(s, "question"),
                type=slot_type,
                enum_values=[str(x) for x in _as_list(s, "enum_values")],
                enum_synonyms=enum_synonyms,
                extract_patterns=[str(x) for x in _as_list(s, "extract_patterns")],
                depends_on=[str(x) for x in _as_list(s, "depends_on")],
                merge_policy=merge_policy,
                ask_gated=_as_bool(s, "ask_gated", False),
                confirm_readback=readback,
                max_asks=_as_int(s, "max_asks", 0),
                reask_question=_as_dict(s, "reask_question"),
                summary_known=_as_str(s, "summary_known"),
                summary_unknown=_as_str(s, "summary_unknown"),
            ))

        pers_d = _as_dict(d, "personality")
        _ai_disc = _as_str(pers_d, "ai_disclosure", "deflect") or "deflect"
        if _ai_disc not in ("deflect", "admit", "admit_if_pressed"):
            _ai_disc = "deflect"
        personality = PersonalityConfig(
            pronouns=_as_str(pers_d, "pronouns",
                             _as_str(id_d, "pronouns", "she") or "she") or "she",
            energy=_as_str(pers_d, "energy", "medium") or "medium",
            tone=_as_str(pers_d, "tone", "conversational") or "conversational",
            style=_as_str(pers_d, "style", "natural") or "natural",
            lingo=_as_str(pers_d, "lingo"),
            custom_instructions=_as_str(pers_d, "custom_instructions"),
            traits=[str(x) for x in _as_list(pers_d, "traits")],
            energy_mirroring=_as_bool(pers_d, "energy_mirroring", True),
            ai_disclosure=_ai_disc,
            verbosity_max_sentences=_as_int(pers_d, "verbosity_max_sentences", 3),
            style_variants=[str(x).strip() for x in _as_list(pers_d, "style_variants")
                            if str(x or "").strip()],
        )

        brand_d = _as_dict(d, "brand")
        brand = BrandConfig(
            spoken_company_name=_as_str(brand_d, "spoken_company_name"),
            positioning_tagline=_as_str(brand_d, "positioning_tagline"),
            credibility_markers=[str(x) for x in _as_list(brand_d, "credibility_markers")],
            trust_proofs=[str(x) for x in _as_list(brand_d, "trust_proofs")],
            regions_served=[str(x) for x in _as_list(brand_d, "regions_served")],
            contact_phone=_as_str(brand_d, "contact_phone"),
            whatsapp_number=_as_str(brand_d, "whatsapp_number"),
        )

        ob_d = _as_dict(d, "outbound")
        outbound = OutboundConfig(
            identity_confirm_line=_as_dict(ob_d, "identity_confirm_line"),
            good_time_check_line=_as_dict(ob_d, "good_time_check_line"),
            pitch=_as_dict(ob_d, "pitch"),
            wrong_person_response=_as_dict(ob_d, "wrong_person_response"),
            busy_response=_as_dict(ob_d, "busy_response"),
            wrong_number_close=_as_dict(ob_d, "wrong_number_close"),
            wrong_number_patterns=[str(x) for x in _as_list(ob_d, "wrong_number_patterns")],
            wrong_number_reconfirm_line=_as_dict(ob_d, "wrong_number_reconfirm_line"),
            opening_greeting=_as_dict(ob_d, "opening_greeting"),
            direction_brief=_as_str(ob_d, "direction_brief"),
            opening_variants=_as_variants(ob_d, "opening_variants"),
        )

        gr_d = _as_dict(d, "guardrails")
        guardrails = GuardrailsConfig(
            use_builtin_rules=_as_bool(gr_d, "use_builtin_rules", True),
            builtin_rules_pack=(
                _as_str(gr_d, "builtin_rules_pack", "generic")
                or "generic"),
            never_rules=[str(x) for x in _as_list(gr_d, "never_rules")],
            always_rules=[str(x) for x in _as_list(gr_d, "always_rules")],
            honesty=_as_dict(gr_d, "honesty"),
            compliance=_as_dict(gr_d, "compliance"),
            escalation=_as_dict(gr_d, "escalation"),
        )
        # D4 aliasing — llm_config.extra_*_rules into guardrails when
        # guardrails carries no rules of its own — ships in P7 behind a
        # per-agent audit (agents like angelobot carry extra rules that have
        # NEVER run live; auto-activating them here would change live calls).
        # Until then guardrails.never/always_rules render only when written
        # explicitly.

        fu_d = _as_dict(d, "follow_up")
        # Platform universal rule (2026-07-27): callback capture + follow-up
        # auto-execution are ON for every agent, existing and future. A YAML
        # that says nothing gets enabled=True; only an explicit
        # {enabled: false} opts an agent out. (The post-call side —
        # shared/intelligence/followup.py:_agent_auto_execute — applies the
        # same default when reading the raw YAML; change both together.)
        _clarify = _as_dict(fu_d, "clarify_commitments")
        _clarify.setdefault("enabled", True)
        _auto_exec = _as_dict(fu_d, "auto_execute")
        _auto_exec.setdefault("enabled", True)
        follow_up = FollowUpConfig(
            whatsapp_promise=_as_dict(fu_d, "whatsapp_promise"),
            callback=_as_dict(fu_d, "callback"),
            clarify_commitments=_clarify,
            auto_execute=_auto_exec,
        )

        # The agent's own gender is the VOICE's (RUNBOOK §42). The pronoun
        # fields were set independently of the voice and drifted: seven
        # shubh-voiced agents carried "she" (several by the schema default
        # alone), and a caller heard Vishal Patel say "बात कर रही हूँ". Every
        # load now derives them from the voice this agent actually speaks with
        # — the default language's speaker, else the platform default — so the
        # dashboard, the Copilot and the runtime all read the same answer, and
        # every save persists it. A speaker the catalog cannot classify leaves
        # the stored value alone.
        _dv = tts_voices.get(default_lang)
        _vg = (speaker_gender(_dv.speaker) or (_dv.gender or "").strip().lower()
               if _dv is not None else speaker_gender(DEFAULT_TTS_SPEAKER))
        if _vg in ("male", "female"):
            identity.pronouns = personality.pronouns = (
                "he" if _vg == "male" else "she")

        pc_d = _as_dict(d, "post_call")
        post_call = PostCallConfig(
            success_definition=_as_dict(pc_d, "success_definition"),
            outcomes=[o for o in _as_list(pc_d, "outcomes") if isinstance(o, dict)],
            scoring=_as_dict(pc_d, "scoring"),
            lead_profile=_as_dict(pc_d, "lead_profile"),
        )

        return cls(
            agent_id=_as_str(d, "agent_id"),
            version=_as_str(d, "version", "1.0.0") or "1.0.0",
            identity=identity,
            languages=languages,
            call_flow=call_flow,
            knowledge_base=knowledge_base,
            response_bank=response_bank,
            calendar=calendar,
            llm_config=llm_config,
            personality=personality,
            slots=slots,
            call_direction=(_as_str(d, "call_direction", "inbound") or "inbound").strip().lower(),
            brand=brand,
            outbound=outbound,
            guardrails=guardrails,
            follow_up=follow_up,
            post_call=post_call,
            memory=_as_dict(d, "memory"),
            revision=_as_int(d, "revision", 0),
            updated_at=_as_str(d, "updated_at"),
            updated_by=_as_str(d, "updated_by"),
        )

    @classmethod
    def from_yaml(cls, path: "str | Path") -> "AgentDefinition":
        """Load an AgentDefinition from a YAML file."""
        import yaml
        from pathlib import Path as _Path
        with open(_Path(path), encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls.from_dict(data)

    def to_yaml(self, path: "str | Path") -> None:
        """Persist this AgentDefinition to a YAML file (atomically)."""
        import dataclasses
        import yaml
        from pathlib import Path as _Path

        def _coerce(obj: Any) -> Any:
            """Convert dataclasses and tuples to YAML-serialisable primitives."""
            if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
                return {k: _coerce(v) for k, v in dataclasses.asdict(obj).items()}
            if isinstance(obj, tuple):
                return list(obj)
            if isinstance(obj, list):
                return [_coerce(i) for i in obj]
            if isinstance(obj, dict):
                return {k: _coerce(v) for k, v in obj.items()}
            return obj

        data = _coerce(self)
        out = _Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: dump to a temp file in the same directory, then
        # os.replace() over the target. A call arriving mid-write can now only
        # ever see the complete old file or the complete new file — a torn
        # half-written YAML used to parse-fail and drop the caller onto the
        # fallback agent. watchfiles treats the replace as a change, so the
        # registry hot-reload keeps working.
        import os as _os
        import tempfile as _tempfile
        fd, tmp_path = _tempfile.mkstemp(
            prefix=out.name + ".", suffix=".tmp", dir=str(out.parent))
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
                f.flush()
                _os.fsync(f.fileno())
            _os.replace(tmp_path, out)
        except BaseException:
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass
            raise
