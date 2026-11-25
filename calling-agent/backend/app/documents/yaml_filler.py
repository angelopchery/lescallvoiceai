"""
Use the LLM to extract structured YAML fields from document text.
Returns a diff preview; the caller decides whether to apply it.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_AGENTS_DIR = Path(__file__).resolve().parents[3] / "agents"

_PROMPT_HEADER = """\
You are a structured data extractor for AI voice agent configuration files.

Read the document below and extract information to fill a voice agent's YAML configuration.
The information is often UNLABELED — scripted lines are quoted inside prose, rules appear as
narrative ("he never quotes prices"), and the call flow may be described as a story. Read the
whole document carefully and pull out everything that matches the fields, however it is phrased.
Return ONLY a JSON object (starting with {{ and ending with }}) with ONLY the fields you can confidently infer from the document.
NEVER return a plain string, array, or any value other than a JSON object.
Do NOT guess or hallucinate. If a field cannot be determined, omit it entirely.
If the document explicitly says to ignore something, DO NOT extract it.
If no fields can be extracted at all, return an empty object: {{}}

Extract these fields (use these exact key names):
"""

# Focused extraction groups: one LLM pass per group. A single 25-field prompt
# made the extractor skip half the fields on unlabeled real-world documents
# (Kaapd live test 2026-07-15); three themed passes recover the recall.
_GROUP_FIELDS: dict[str, str] = {
    "business": """\
  agent_name            — The spoken name the AI agent should use, if the document names one (string)
  company_name          — Full company name (string)
  company_tagline       — One-sentence brand descriptor, max 100 chars (string)
  role_description      — What role the AI agent plays, e.g. "Sales Agent and Fabric Expert" (string)
  contact               — Phone/WhatsApp number callers can be given (string)
  call_direction        — "inbound" if the agent answers calls (support/reception/booking), "outbound" if it places calls (sales outreach/follow-ups). Only if the document makes this clear. (string)
  portfolio_prompt_text — Detailed multi-paragraph description of what the business makes/sells/offers, in the document's own facts — everything the agent must know to talk shop. Include history, founders, address, contact channels and process facts (how samples/dispatch/orders work) unless the document says to ignore them (string)
  known_projects        — Dict of lowercase product/service keys → [canonical_name, "Standard"], e.g. {{"cotton fabric": ["Cotton Fabric", "Standard"]}} (object)
  locations             — List of cities/regions served (array of strings; NOT cities the document says to ignore)
  catalog               — Structured list of EVERY distinct product/service/family the document says the business makes or sells. ALWAYS fill this whenever portfolio_prompt_text is filled — a single product family is still one catalog item. Derive pitch_points from the document's own claims (in-house processes, launch cadence, certifications). NEVER invent items the document does not name. [{{"id": "slug", "name": "...", "pitch_points": [...], "aliases": [caller phrasings for it], "price_range": "...", "availability": "..."}}] (array of objects; omit sub-keys the document doesn't support)
  match_rules           — Need→product mappings if the document implies them: [{{"when": {{"budget": "<=40L"}}, "recommend": ["id"], "reason": "..."}}] (array)
  pricing_policy        — {{"mode": "state_freely"}} ONLY if the document clearly wants prices quoted openly; {{"mode": "defer"}} if pricing must be deferred to the team (object)
  brand                 — {{"credibility_markers": [...], "trust_proofs": [top 2-3 proof points the caller should hear], "regions_served": [...], "whatsapp_number": "..."}} (object; only sub-keys the document supports)
""",
    "scripts": """\
  opening_greeting      — The exact line the agent speaks first on a call, if the document scripts one (quoted openers count) (string)
  closing_responses     — The exact closing/goodbye line per language if the document scripts how calls end: {{"en": "...", "hi": "...", "gu": "..."}} (object; ONE string per language — pick the final thank-you/goodbye line)
  call_goal             — The ONE thing a successful call accomplishes, 1-2 sentences, if the document states the call's purpose (string)
  stages                — The call flow as ordered stages, if the document walks through the call step by step (even as a story): [{{"id": "SNAKE_CASE_ID", "goal": "2-4 full sentences of concrete instructions for this step, including any exact lines the document scripts for it", "collect": [snake_case slot names this step asks for — only when the document ties questions to this step], "advance_when": {{"all_collect_filled": true}} when the document says the step ends once its questions are answered, "role": one of greeting|confirm_person|discovery|pitch|recommend|scheduling|confirmation|closing when obvious}}] (array of objects; omit collect/advance_when/role when the document doesn't say)
  objection_playbook    — Objections WITH their scripted answers, if the document gives responses to pushbacks: [{{"id": "slug", "patterns": ["caller phrasings — include every language the call uses, native script AND romanized (matching is a literal substring test)"], "answer": {{"en": "...", "hi": "...", "gu": "..."}} (only languages the document provides)}}] (array of objects)
  objection_responses   — Scripted pushback replies grouped by language when they are not tied to specific objections: {{"en": ["...", "..."]}} (object of lang → array of strings)
  objection_patterns    — Common objections callers raise, short lowercase phrases (array of strings)
""",
    "policy": """\
  personality           — {{"tone": "friendly|formal|...", "energy": "low|medium|high", "style": "...", "traits": [...], "ai_disclosure": "admit" if the document says to admit being an AI when asked / "deflect" if it says to avoid the topic (omit if unstated)}} if the document describes how the agent should come across, even loosely ("warm, quick, never pushy") (object)
  languages             — {{"supported": ["hi","en","gu" — only from that set], "default": "..."}} if the document states or shows which languages the agent speaks (scripted lines in a language count) (object)
  slots                 — Information the agent must collect from every caller, if the document lists it (even narratively — "ask what they stock, then the quantity"): [{{"name": "snake_case", "required": true, "question": {{"en": "question wording", ...per language given}}}}] (array of objects)
  guardrails            — {{"never_rules": [...], "always_rules": [...], "compliance": {{"disclaimers": [...]}}}} for any rules the document states, even narratively ("he never quotes prices") (object)
  extra_always_rules    — Business-specific rules the agent must always follow (array of strings)
  extra_never_rules     — Business-specific things the agent must never say or do (array of strings)
  appointment_slots     — Bookable visit/appointment time slots if the document lists availability, e.g. [{{"day": "Tomorrow", "time": "11:00 AM"}}, {{"day": "Friday", "time": "4:30 PM"}}] (array of objects with "day" and "time"; times must be realistic and non-identical; omit entirely if the business doesn't book time slots)
  scheduling            — {{"days": ["Monday", ...], "window": "10:00-18:00"}} if the document states working/dispatch days and hours rather than bookable slots (object)
  callback_policy       — {{"enabled": true, "capture_exact_time": true}} if the document says exact callback times get recorded/honored (object)
  whatsapp_promise      — {{"enabled": true, "assets": ["catalogue PDF", ...]}} if the document promises material over WhatsApp after the call (object)
  opening_greeting_by_language — {{"en": "exact scripted opener", "hi": "..."}} when the document scripts the opener in more than one language (object)
""",
}

_PROMPT_FOOTER = """
DOCUMENT:
{document_text}

Return ONLY the JSON object. No markdown, no explanation, no code fences."""


def _group_prompt(group: str) -> str:
    return _PROMPT_HEADER + _GROUP_FIELDS[group] + _PROMPT_FOOTER


# Whole-schema prompt kept for callers/tests that reference it directly.
_PROMPT = _PROMPT_HEADER + "".join(_GROUP_FIELDS.values()) + _PROMPT_FOOTER


# Fields the builder considers essential for a working agent. Anything the
# document didn't yield comes back in "missing" so the UI can explicitly ask
# the user for it instead of silently leaving gaps.
_REQUIRED_FIELDS: list[tuple[str, str, str]] = [
    ("agent_name",            "Agent name",          "The spoken name the agent introduces itself with"),
    ("company_name",          "Company name",        "The business the agent represents"),
    ("company_tagline",       "Company tagline",     "One-line brand descriptor used in the agent's prompt"),
    ("role_description",      "Agent role",          "e.g. 'inbound support consultant' or 'outbound sales agent'"),
    ("contact",               "Contact number",      "Phone/WhatsApp the agent may share with callers"),
    ("call_direction",        "Call type",           "Will this agent take inbound calls or place outbound calls?"),
    ("opening_greeting",      "Opening greeting",    "The first thing the agent says on a call"),
    ("portfolio_prompt_text", "Products / services", "What the agent knows and can talk about"),
    ("catalog",               "Product list",        "Each product or family the agent may claim, as named items — without it the agent improvises products"),
    ("objection_playbook",    "Pushback answers",    "The top objections and the best scripted answer to each"),
    ("locations",             "Locations served",    "Cities/regions the business covers"),
    ("appointment_slots",     "Calendar slots",      "Bookable time slots (recommended so the agent can schedule visits)"),
]


def compute_missing(fields: dict) -> list[dict]:
    """Which essential fields the extraction did NOT produce."""
    missing = []
    for key, label, hint in _REQUIRED_FIELDS:
        val = fields.get(key)
        if val is None or val == "" or val == [] or val == {}:
            missing.append({"key": key, "label": label, "hint": hint})
    return missing


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# 60k chars (~15k tokens) per pass, up to six passes. The old 12k × 4 was
# sized for gpt-4o-mini's patience and silently dropped everything past 48k
# chars — a 30-page brochure lost its price list and its closing script. The
# platform's Gemini models read a million tokens; the cap now exists only to
# bound the spend on a pathological upload, and the result says when it bit
# (`truncated`).
_CHUNK_CHARS = _env_int("DOC_EXTRACT_CHUNK_CHARS", 60_000)
_MAX_CHUNKS = _env_int("DOC_EXTRACT_MAX_CHUNKS", 6)
# Gemini's thinking tokens count against max_tokens on the OpenAI-compatible
# endpoint — a tight budget returns truncated JSON, not a shorter answer (the
# intelligence extractor learned this first). Generous budget, low effort.
_MAX_OUTPUT_TOKENS = _env_int("DOC_EXTRACT_MAX_OUTPUT_TOKENS", 16_000)

_DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"


def resolve_llm() -> dict:
    """Which endpoint, key and model the extractor uses — decided in one place
    so the API can report it and a test can pin it.

    Since 2026-09-25 (RUNBOOK §57) the extractor runs on the platform's own
    LLM endpoint (`LLM_BASE_URL` / `LLM_API_KEY`: Gemini through its
    OpenAI-compatible route) instead of a hardcoded `openai.OpenAI(OPENAI_API_KEY)`
    — that key ran out of credit on 2026-09-11 and every client document
    uploaded since came back "429 insufficient_quota". The model defaults to
    the Copilot's (`COPILOT_LLM_MODEL`): reading a client's brief is part of
    building their agent, so it shares the agent-building quota bucket and
    never the live-call one. `DOC_EXTRACT_MODEL` / `DOC_EXTRACT_BASE_URL` /
    `DOC_EXTRACT_API_KEY` override each part; `OPENAI_API_KEY` is the last
    resort, exactly as before.
    """
    def _env(name: str) -> str:
        return (os.environ.get(name) or "").strip()

    base_url = _env("DOC_EXTRACT_BASE_URL") or _env("LLM_BASE_URL")
    api_key = _env("DOC_EXTRACT_API_KEY") or (_env("LLM_API_KEY") if base_url else "")
    model = _env("DOC_EXTRACT_MODEL")
    if base_url and api_key:
        if not model:
            model = _env("COPILOT_LLM_MODEL") or _env("LLM_MODEL") or _DEFAULT_GEMINI_MODEL
        provider = "gemini" if "googleapis.com" in base_url else "compat"
    else:
        base_url, api_key = "", _env("OPENAI_API_KEY")
        model = model or "gpt-4o-mini"
        provider = "openai"
    effort = _env("DOC_EXTRACT_REASONING_EFFORT") or "low"
    return {"base_url": base_url, "api_key": api_key, "model": model,
            "provider": provider, "reasoning_effort": effort if provider != "openai" else ""}


def _friendly_error(exc: Exception, model: str) -> tuple[str, str]:
    """(code, sentence for the user) for the failures a document upload meets."""
    text = str(exc)
    low = text.lower()
    status = getattr(exc, "status_code", None)
    if status == 429 or "insufficient_quota" in low or "no credits" in low \
            or "credit balance" in low or "rate limit" in low or "resource_exhausted" in low:
        return "llm_quota", (f"The extraction model ({model}) is out of quota or rate-limited "
                             "right now — try again in a minute. If it keeps happening the "
                             "API key needs credit.")
    if status in (401, 403) or "authentication" in low or "api key" in low:
        return "llm_auth", f"The extraction model ({model}) rejected the API key — check LLM_API_KEY."
    if status == 404 or "not found" in low and "model" in low:
        return "llm_model", f"The extraction model '{model}' is not available on this endpoint."
    if "timeout" in low or "timed out" in low or "connection" in low:
        return "llm_unreachable", "Couldn't reach the extraction model — try again."
    return "llm_error", f"Extraction failed: {text[:300]}"


def _merge_chunk_fields(base: dict, extra: dict) -> dict:
    """Combine per-chunk extractions: lists extend (deduped), dicts update
    (existing keys win), scalars first-wins."""
    for k, v in extra.items():
        if k not in base or base[k] in (None, "", [], {}):
            base[k] = v
        elif isinstance(base[k], list) and isinstance(v, list):
            for item in v:
                if item not in base[k]:
                    base[k].append(item)
        elif isinstance(base[k], dict) and isinstance(v, dict):
            for sk, sv in v.items():
                base[k].setdefault(sk, sv)
    return base


def _parse_json_object(raw: str):
    """The JSON the model returned, tolerating fences and a sentence around
    the object — the cheapest possible repair before a paid retry."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:] if lines[-1].strip() == "```" else lines[1:-1]).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            return json.loads(raw[start:end + 1])
        raise


def _chat(client, model: str, prompt: str, *, reasoning_effort: str = "",
          json_mode: bool = True) -> str:
    """One completion. JSON mode and the reasoning knob are dropped, once, if
    the endpoint rejects them as parameters — a parameter rejection is a 400
    that names the field, and it is about the endpoint, not the document."""
    kwargs: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": _MAX_OUTPUT_TOKENS,
        "temperature": 0,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        if status in (400, 422) and (json_mode or reasoning_effort):
            logger.warning("[docs/yaml_filler] endpoint rejected a parameter (%s); "
                           "retrying plain", str(exc)[:120])
            kwargs.pop("response_format", None)
            kwargs.pop("reasoning_effort", None)
            resp = client.chat.completions.create(**kwargs)
        else:
            raise
    return (resp.choices[0].message.content or "").strip()


def _extract_one(client, model: str, text: str, prompt_template: str = _PROMPT,
                 reasoning_effort: str = "") -> dict:
    """One extraction call with a single deterministic retry on bad JSON."""
    for attempt in range(2):
        raw = _chat(client, model, prompt_template.format(document_text=text),
                    reasoning_effort=reasoning_effort)
        try:
            fields = _parse_json_object(raw)
        except json.JSONDecodeError as exc:
            logger.warning("[docs/yaml_filler] JSON parse error (attempt %d): %s | raw=%r",
                           attempt + 1, exc, raw[:200])
            if attempt == 0:
                continue
            raise
        if isinstance(fields, dict):
            return fields
        logger.warning("[docs/yaml_filler] LLM returned %s instead of object: %r",
                       type(fields).__name__, raw[:120])
        if attempt == 0:
            continue
        raise ValueError(f"LLM returned a {type(fields).__name__} instead of a JSON object")
    raise ValueError("extraction failed")  # unreachable


def extract_fields_from_text(document_text: str) -> dict:
    """
    Call LLM to extract YAML-fillable fields. Long texts are chunked
    (~12k chars, up to 4 chunks) and the per-chunk results merged, so a long
    brochure no longer loses its tail to truncation.
    Returns {"fields": {...}, "missing": [...], "status": "ok", "model": ...,
    "chunks": n, "truncated": bool}
    or {"error": "...", "code": "...", "status": "error"}.
    """
    llm = resolve_llm()
    model = llm["model"]
    if not llm["api_key"]:
        return {"status": "error", "code": "llm_unconfigured", "model": model,
                "error": ("No extraction model is configured — set LLM_BASE_URL and "
                          "LLM_API_KEY (or OPENAI_API_KEY).")}

    text = document_text.strip()
    limit = _CHUNK_CHARS * _MAX_CHUNKS
    chunks = [text[i:i + _CHUNK_CHARS]
              for i in range(0, min(len(text), limit), _CHUNK_CHARS)]
    truncated = len(text) > limit
    if truncated:
        logger.warning("[docs/yaml_filler] text truncated at %d chars (%d given)",
                       limit, len(text))

    try:
        from concurrent.futures import ThreadPoolExecutor
        import openai
        client = openai.OpenAI(api_key=llm["api_key"], base_url=llm["base_url"] or None,
                               timeout=120.0, max_retries=2)
        fields: dict = {}
        groups = list(_GROUP_FIELDS)
        for chunk in chunks:
            # three focused passes per chunk, in parallel — one giant prompt
            # made the model skip half the fields on unlabeled documents
            with ThreadPoolExecutor(max_workers=len(groups)) as pool:
                results = list(pool.map(
                    lambda g: _extract_one(client, model, chunk, _group_prompt(g),
                                           reasoning_effort=llm["reasoning_effort"]),
                    groups))
            for r in results:
                fields = _merge_chunk_fields(fields, r)
        logger.info("[docs/yaml_filler] extracted %d field(s) from %d chars via %s "
                    "(%d chunk(s)%s)", len(fields), len(text), model, len(chunks),
                    ", truncated" if truncated else "")
        return {"status": "ok", "fields": fields, "missing": compute_missing(fields),
                "model": model, "chunks": len(chunks), "truncated": truncated}
    except json.JSONDecodeError:
        return {"status": "error", "code": "llm_bad_json", "model": model,
                "error": "The extraction model returned invalid JSON — try again or upload a different document."}
    except Exception as exc:
        logger.error("[docs/yaml_filler] Extraction error (%s): %s", model, exc)
        code, message = _friendly_error(exc, model)
        return {"status": "error", "code": code, "model": model, "error": message}


def apply_fields(agent_folder: str, fields: dict) -> dict:
    """
    Merge extracted fields into an agent's YAML file.
    Returns {"status": "ok", "applied": [...]} or {"status": "error", "error": "..."}.
    """
    yaml_path = _AGENTS_DIR / agent_folder / "agent.yaml"
    if not yaml_path.exists():
        return {"status": "error", "error": f"Agent folder '{agent_folder}' not found"}

    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}

        applied: list[str] = []

        _set(data, ["identity", "agent_name"],      fields, "agent_name",         applied)
        _set(data, ["identity", "contact"],         fields, "contact",            applied)
        _set(data, ["identity", "company_name"],    fields, "company_name",       applied)
        _set(data, ["identity", "company_tagline"], fields, "company_tagline",     applied)
        _set(data, ["identity", "role_description"], fields, "role_description",   applied)

        _set(data, ["knowledge_base", "portfolio_prompt_text"], fields, "portfolio_prompt_text", applied)
        _set(data, ["knowledge_base", "known_projects"],        fields, "known_projects",        applied)
        _set(data, ["knowledge_base", "locations"],             fields, "locations",             applied)
        _set(data, ["knowledge_base", "objection_patterns"],    fields, "objection_patterns",    applied)
        _set(data, ["knowledge_base", "catalog"],               fields, "catalog",               applied)
        _set(data, ["knowledge_base", "match_rules"],           fields, "match_rules",           applied)
        _set(data, ["knowledge_base", "pricing_policy"],        fields, "pricing_policy",        applied)

        # v2 sections extracted whole (brochures fill brand/guardrails too).
        for section in ("brand", "guardrails"):
            if isinstance(fields.get(section), dict) and fields[section]:
                node = data.setdefault(section, {})
                if isinstance(node, dict):
                    node.update(fields[section])
                    applied.append(section)

        _set(data, ["response_bank", "opening_greeting"],       fields, "opening_greeting",      applied)

        _set(data, ["llm_config", "extra_always_rules"],        fields, "extra_always_rules",    applied)
        _set(data, ["llm_config", "extra_never_rules"],         fields, "extra_never_rules",     applied)

        direction = str(fields.get("call_direction", "")).strip().lower()
        if direction in ("inbound", "outbound"):
            data["call_direction"] = direction
            applied.append("call_direction")

        slots = fields.get("appointment_slots")
        if isinstance(slots, list):
            clean = [
                {"day": str(s.get("day", "")).strip(), "time": str(s.get("time", "")).strip()}
                for s in slots
                if isinstance(s, dict) and s.get("day") and s.get("time")
            ]
            if clean:
                cal = data.setdefault("calendar", {"type": "mock", "mock_slots": [], "slots_text": {}})
                cal["type"] = cal.get("type") or "mock"
                cal["mock_slots"] = clean
                slot_str = ", ".join(f"{s['day']} at {s['time']}" for s in clean)
                texts = cal.get("slots_text") or {}
                # Regenerate for every configured language; translations of a
                # stale slot list are worse than an English list of real slots.
                cal["slots_text"] = {k: slot_str for k in texts} if texts else {"en": slot_str}
                applied.append("appointment_slots")

        if not applied:
            return {"status": "ok", "applied": []}

        # Route through the store choke point (plan §5.1): revision snapshot,
        # audit stamps, validation, atomic write, registry invalidation.
        # No expected_paths scoping: apply_fields maps document fields to a
        # known-safe fixed path set that the diff proof would only re-derive.
        from ..agent_definition import store as agent_store
        agent_store.write_agent(
            agent_folder, data, actor="docfill",
            reason="document field extraction: " + ", ".join(applied)[:150],
            agents_dir=_AGENTS_DIR)

        logger.info("[docs/yaml_filler] Applied %d fields to agent '%s'", len(applied), agent_folder)
        return {"status": "ok", "applied": applied}

    except Exception as exc:
        logger.error("[docs/yaml_filler] Apply error: %s", exc)
        return {"status": "error", "error": str(exc)}


def _set(data: dict, path: list[str], fields: dict, key: str, applied: list[str]) -> None:
    """Set data[path[0]][path[1]]... = fields[key] if key present in fields."""
    if key not in fields:
        return
    node = data
    for step in path[:-1]:
        node = node.setdefault(step, {})
    node[path[-1]] = fields[key]
    applied.append(key)
