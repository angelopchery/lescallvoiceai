"""Stage 1 — LLM intelligence extraction.

One structured LLM call per completed call turns the speaker-labeled transcript
plus the deterministic call facts (outcome, collected_data flags, agent config)
into the full intelligence record: summary, scores, sentiment, topics,
commitments, action items, and the follow-up judgment.

Design notes:
    * prompt is date-anchored (IST) — the legacy analyzer hallucinated
      follow-up dates years in the past because it had no "today"
    * every commitment must carry a supporting quote that fuzzy-matches the
      transcript, or it is dropped (hallucination guard)
    * all dates are validated >= call date; scores clamped; enums coerced
    * prompt/schema is versioned via SCHEMA_VERSION stamped on the row
    * the LLM invocation is isolated in `run_llm` so tests mock it
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from shared.intelligence import config

SCHEMA_VERSION = 1

_ENUMS = {
    "urgency": ("low", "medium", "high", "critical"),
    "sentiment": ("positive", "neutral", "negative"),
    "priority": ("low", "medium", "high", "urgent"),
    "made_by": ("agent", "customer"),
    "commitment_kind": ("callback", "send_document", "whatsapp", "meeting",
                        "quote", "payment", "visit", "other"),
    "followup_required": ("yes", "no", "unsure"),
    "followup_type": ("callback", "send_info", "meeting", "visit", "payment", "other"),
    "promised_by": ("customer_requested", "agent_promised", "internal",
                    "external_dependency"),
}

_LIST_FIELDS = (
    "topics", "pain_points", "objections", "buying_signals",
    "competitor_mentions", "product_interest", "budget_discussion",
    "timeline_discussion", "decision_makers", "compliance_issues",
    "escalations", "risk_indicators", "tags",
)

_SYSTEM_PROMPT = """You are the conversation-intelligence engine of an AI calling platform. \
You analyze completed phone-call transcripts between a company's AI voice agent and a customer. \
Calls may be in English, Hindi, Gujarati, or code-mixed (Hinglish). Analyze in whatever language \
the call used, but ALWAYS write your output in English (translate quotes where needed, keeping \
the original phrase in parentheses when it matters).

Return ONE JSON object with EXACTLY these fields:

{
 "summary": "3-5 sentence business summary of the call",
 "outcome_summary": "1 sentence: how the call ended and what was achieved",
 "customer_intent": "short phrase: what the customer wanted",
 "conversation_quality": 0.0-1.0,
 "conversion_probability": 0.0-1.0,
 "satisfaction": 0.0-1.0,
 "urgency": "low|medium|high|critical",
 "confidence": 0.0-1.0  (your confidence in this whole analysis; low for short/garbled calls),
 "lead_score": 0-100, "opportunity_score": 0-100, "risk_score": 0-100, "compliance_score": 0-100,
 "sentiment": "positive|neutral|negative",
 "sentiment_journey": [{"phase": "opening|middle|closing", "sentiment": "positive|neutral|negative", "note": "..."}],
 "topics": ["..."], "pain_points": ["..."], "objections": ["..."], "buying_signals": ["..."],
 "competitor_mentions": ["..."], "product_interest": ["..."], "budget_discussion": ["..."],
 "timeline_discussion": ["..."], "decision_makers": ["..."], "compliance_issues": ["..."],
 "escalations": ["..."], "risk_indicators": ["..."],
 "agent_performance": {"score": 0-100, "strengths": ["..."], "improvements": ["..."]},
 "key_moments": [{"label": "short label", "detail": "what happened"}],
 "recommendations": {"next_actions": ["..."], "emails": ["..."], "documents": ["..."], "crm_updates": ["..."]},
 "entities": {"names": ["..."], "orgs": ["..."], "amounts": ["..."], "dates": ["..."], "places": ["..."]},
 "tags": ["3-8 short lowercase tags"],
 "commitments": [{"made_by": "agent|customer", "kind": "callback|send_document|whatsapp|meeting|quote|payment|visit|other",
                  "description": "...", "quote": "verbatim transcript phrase supporting this",
                  "deadline_text": "raw phrase like 'next week' or null", "deadline_date": "YYYY-MM-DD or null",
                  "deadline_time": "HH:MM or null", "is_vague": true/false, "confidence": 0.0-1.0}],
 "action_items": [{"description": "...", "priority": "low|medium|high|urgent", "due_date": "YYYY-MM-DD or null"}],
 "follow_up": {"required": "yes|no|unsure", "confidence": 0.0-1.0,
               "reason": "why (or why not)", "who_promised": "customer_requested|agent_promised|internal|external_dependency or null",
               "expected_outcome": "...", "type": "callback|send_info|meeting|visit|payment|other or null",
               "priority": "low|medium|high|urgent",
               "suggested_date": "YYYY-MM-DD or null", "suggested_time": "HH:MM or null",
               "suggested_offset_minutes": integer or null}
}

Rules:
- Empty list [] when nothing applies; never invent content.
- commitments: ONLY promises actually made in the call (either side). The quote must be a real
  phrase from the transcript. is_vague=true when day or time is missing or fuzzy ("later",
  "next week", "kal dekhte hain").
- follow_up.required="no" needs a reason too (e.g. "resolved, customer declined, wrong number").
  Use "unsure" with a reason when signals conflict or the call is too short/garbled to judge.
- All dates are in the future relative to the call date given below; never output past dates.
- When the customer asked to be called back after a RELATIVE delay ("call me in 10 minutes",
  "after half an hour", "das minute baad"), set follow_up.suggested_offset_minutes to that
  delay in minutes (counted from the end of the call) and leave suggested_date/time null.
  Never convert a relative delay into a next-day date.
- compliance_score starts at 100 and drops for issues (misleading claims, pressure tactics,
  data-handling problems, abusive handling). risk_score is the risk of LOSING this customer/deal.
- Score honestly: a wrong number is lead_score 0; a hot buyer asking for pricing is 80+."""


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def _ist_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30)))


def middle_out(text: str, limit: int) -> str:
    """Budget the transcript keeping the head and tail (openings carry intent,
    closings carry commitments)."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    return (text[:head] + "\n[... middle of the call truncated for length ...]\n"
            + text[-tail:])


def agent_context(agent_yaml: dict[str, Any] | None) -> str:
    """Compact agent-definition context (identity, mission, outcome taxonomy,
    scoring criteria) read straight from agents/<slug>/agent.yaml."""
    if not agent_yaml:
        return ""
    parts: list[str] = []
    ident = agent_yaml.get("identity") or {}
    if ident.get("name") or ident.get("role"):
        parts.append(f"Agent identity: {ident.get('name', '')} — {ident.get('role', '')}".strip())
    mission = (agent_yaml.get("call_flow") or {}).get("mission") or agent_yaml.get("mission")
    if mission:
        parts.append(f"Call mission: {mission}")
    post = agent_yaml.get("post_call") or {}
    if post.get("success_definition"):
        parts.append(f"Success definition: {post['success_definition']}")
    outcomes = post.get("outcomes") or []
    if outcomes:
        parts.append("Outcome taxonomy: " + "; ".join(
            f"{o.get('id')}={o.get('label')}" for o in outcomes if isinstance(o, dict)))
    scoring = post.get("scoring") or {}
    if scoring.get("criteria_prompt"):
        parts.append(f"Scoring guidance from the business: {scoring['criteria_prompt']}")
    return "\n".join(parts)


def build_user_prompt(call: dict[str, Any], agent_ctx: str = "",
                      memory_digest: str = "") -> str:
    now = _ist_now()
    call_date = (call.get("started_at") or "")[:10] or now.strftime("%Y-%m-%d")
    flags: dict[str, Any] = {}
    try:
        flags = (json.loads(call.get("collected_data") or "{}") or {}).get("flags", {}) or {}
    except Exception:
        pass

    lines = [
        f"Today's date/time (IST): {now.strftime('%Y-%m-%d %H:%M')} ({now.strftime('%A')})",
        f"Call date: {call_date}",
        f"Direction: {call.get('direction') or 'unknown'}",
        f"Duration: {int(call.get('duration_s') or 0)}s",
        f"Language: {call.get('language') or 'unknown'}",
    ]
    if call.get("outcome"):
        lines.append(f"Deterministic outcome (computed by the platform): {call['outcome']}")
    if flags:
        lines.append(f"Deterministic flags from the live engine: {json.dumps(flags, ensure_ascii=False)}")
    if agent_ctx:
        lines.append("\n" + agent_ctx)
    if memory_digest:
        lines.append(f"\nKnown customer history: {memory_digest}")
    transcript = middle_out(call.get("transcript") or "",
                            config.EXTRACT_MAX_TRANSCRIPT_CHARS)
    lines.append("\nTRANSCRIPT (Agent = the AI voice agent, Caller = the customer):\n"
                 + transcript)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM invocation (isolated for tests)
# ---------------------------------------------------------------------------

async def run_llm(user_prompt: str) -> str:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY,
                         timeout=config.LLM_TIMEOUT_S)
    try:
        kwargs: dict = {}
        if config.LLM_REASONING_EFFORT:
            kwargs["reasoning_effort"] = config.LLM_REASONING_EFFORT
        resp = await client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[{"role": "system", "content": _SYSTEM_PROMPT},
                      {"role": "user", "content": user_prompt}],
            response_format={"type": "json_object"},
            max_tokens=config.LLM_MAX_OUTPUT_TOKENS,
            temperature=0.2,
            **kwargs,
        )
        return resp.choices[0].message.content or ""
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Parsing + validation
# ---------------------------------------------------------------------------

def _clamp(val: Any, lo: float, hi: float) -> float | None:
    try:
        return max(lo, min(hi, float(val)))
    except (TypeError, ValueError):
        return None


def _enum(val: Any, allowed: tuple[str, ...], default: str | None = None) -> str | None:
    if isinstance(val, str) and val.strip().lower() in allowed:
        return val.strip().lower()
    return default


def _str_list(val: Any, cap: int = 25) -> list[str]:
    if not isinstance(val, list):
        return []
    out = []
    for item in val[:cap]:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict) and item.get("text"):
            out.append(str(item["text"]).strip())
    return out


_WS_RE = re.compile(r"[\W_]+", re.UNICODE)


def _normalize(text: str) -> str:
    return _WS_RE.sub("", text).lower()


_SPEAKER_LINE_RE = re.compile(r"^\s*(agent|caller|customer|user)\s*:\s*(.*)$", re.I)


def speech_runs(transcript: str) -> list[str]:
    """Each RUN of consecutive lines by ONE speaker, normalized, as one string.

    The audit transcript writes one line per VAD segment, not per sentence,
    with a timestamp line between them:

        [01:12.004] [spoke 70.1-71.0s]
        Caller: i expected

        [01:13.871]
        Caller: 4 lakh per annum

    A model quoting that answer writes "i expected 4 lakh per annum" — every
    word the caller's, in order, uninterrupted — and it is not a substring of
    the transcript, because "Caller" and the timestamp digits sit in the
    middle of it. 28.7% of caller lines in the live corpus are continuations
    like this (measured 2026-09-21), so the guard was failing genuine quotes
    on exactly the callers who pause mid-sentence (RUNBOOK §46).

    A run never crosses a change of speaker, so nothing here lets a quote
    stitch an agent's question onto a caller's "yes".
    """
    runs: list[str] = []
    who: str | None = None
    buf: list[str] = []
    for line in (transcript or "").splitlines():
        m = _SPEAKER_LINE_RE.match(line)
        if not m:
            continue                      # timestamps, "[spoke …]", blanks
        speaker = m.group(1).lower()
        if speaker != who and buf:
            runs.append(_normalize(" ".join(buf)))
            buf = []
        who = speaker
        buf.append(m.group(2))
    if buf:
        runs.append(_normalize(" ".join(buf)))
    return runs


def quote_in_transcript(quote: str, transcript_norm: str,
                        runs: list[str] | None = None) -> bool:
    """Hallucination guard: the (normalized) quote must appear in the
    transcript. Short quotes (<8 normalized chars) are too weak to verify and
    are accepted.

    `runs` (from `speech_runs`) additionally accepts a quote that spans
    several consecutive segments of the same speaker. Optional, so a caller
    holding only normalized text keeps the strict whole-transcript check."""
    q = _normalize(quote or "")
    if len(q) < 8:
        return True
    if q in transcript_norm:
        return True
    return any(q in r for r in (runs or ()))


def _future_date(date_s: Any, time_s: Any, call_date: str) -> tuple[str | None, str | None]:
    """Validate 'YYYY-MM-DD' (+ optional 'HH:MM') is on/after the call date."""
    if not isinstance(date_s, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", date_s):
        return None, None
    if call_date and date_s < call_date:
        return None, None
    t = time_s if isinstance(time_s, str) and re.match(r"^\d{2}:\d{2}$", time_s or "") else None
    return date_s, t


def parse_and_validate(raw: str, call: dict[str, Any]) -> dict[str, Any]:
    """Parse the LLM output into a validated record. Raises ValueError on
    unusable output (caller retries via the queue)."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text, flags=re.I | re.M)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError("LLM output contains no JSON object")
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("LLM output is not a JSON object")

    call_date = (call.get("started_at") or "")[:10]
    transcript_norm = _normalize(call.get("transcript") or "")
    transcript_runs = speech_runs(call.get("transcript") or "")
    rec: dict[str, Any] = {"schema_version": SCHEMA_VERSION}

    for f in ("summary", "outcome_summary", "customer_intent"):
        v = data.get(f)
        rec[f] = v.strip() if isinstance(v, str) and v.strip() else None
    if not rec["summary"]:
        raise ValueError("LLM output missing summary")

    for f in ("conversation_quality", "conversion_probability", "satisfaction", "confidence"):
        rec[f] = _clamp(data.get(f), 0.0, 1.0)
    for f in ("lead_score", "opportunity_score", "risk_score", "compliance_score"):
        v = _clamp(data.get(f), 0, 100)
        rec[f] = int(v) if v is not None else None
    rec["urgency"] = _enum(data.get("urgency"), _ENUMS["urgency"], "low")
    rec["sentiment"] = _enum(data.get("sentiment"), _ENUMS["sentiment"], "neutral")
    rec["low_confidence"] = 1 if (rec["confidence"] or 0) < 0.5 else 0

    journey = data.get("sentiment_journey")
    rec["sentiment_journey"] = journey if isinstance(journey, list) else []
    for f in _LIST_FIELDS:
        rec[f] = _str_list(data.get(f))

    perf = data.get("agent_performance")
    if isinstance(perf, dict):
        score = _clamp(perf.get("score"), 0, 100)
        rec["agent_performance"] = {
            "score": int(score) if score is not None else None,
            "strengths": _str_list(perf.get("strengths")),
            "improvements": _str_list(perf.get("improvements")),
        }
    else:
        rec["agent_performance"] = {}

    moments = data.get("key_moments")
    rec["key_moments"] = [
        {"label": str(m.get("label", ""))[:120], "detail": str(m.get("detail", ""))[:500]}
        for m in (moments if isinstance(moments, list) else []) if isinstance(m, dict)
    ][:15]

    recs = data.get("recommendations")
    rec["recommendations"] = {
        k: _str_list((recs or {}).get(k)) for k in
        ("next_actions", "emails", "documents", "crm_updates")
    } if isinstance(recs, dict) else {}

    ents = data.get("entities")
    rec["entities"] = {
        k: _str_list((ents or {}).get(k)) for k in
        ("names", "orgs", "amounts", "dates", "places")
    } if isinstance(ents, dict) else {}

    commitments: list[dict[str, Any]] = []
    for c in (data.get("commitments") or [])[:15]:
        if not isinstance(c, dict) or not c.get("description"):
            continue
        quote = str(c.get("quote") or "")
        if quote and not quote_in_transcript(quote, transcript_norm, transcript_runs):
            continue  # hallucinated evidence — drop the commitment
        date_s, time_s = _future_date(c.get("deadline_date"), c.get("deadline_time"), call_date)
        commitments.append({
            "made_by": _enum(c.get("made_by"), _ENUMS["made_by"], "agent"),
            "kind": _enum(c.get("kind"), _ENUMS["commitment_kind"], "other"),
            "description": str(c["description"])[:500],
            "quote": quote[:500] or None,
            "deadline_text": (str(c.get("deadline_text"))[:120]
                              if c.get("deadline_text") else None),
            "deadline_date": date_s,
            "deadline_time": time_s,
            "is_vague": bool(c.get("is_vague")),
            "confidence": _clamp(c.get("confidence"), 0.0, 1.0),
        })
    rec["commitments"] = commitments

    items: list[dict[str, Any]] = []
    for a in (data.get("action_items") or [])[:15]:
        if not isinstance(a, dict) or not a.get("description"):
            continue
        due, _ = _future_date(a.get("due_date"), None, call_date)
        items.append({
            "description": str(a["description"])[:500],
            "priority": _enum(a.get("priority"), _ENUMS["priority"], "medium"),
            "due_date": due,
        })
    rec["action_items"] = items

    fu = data.get("follow_up") if isinstance(data.get("follow_up"), dict) else {}
    date_s, time_s = _future_date(fu.get("suggested_date"), fu.get("suggested_time"), call_date)
    try:
        offset_min = int(fu.get("suggested_offset_minutes") or 0)
    except (TypeError, ValueError):
        offset_min = 0
    rec["follow_up"] = {
        "required": _enum(fu.get("required"), _ENUMS["followup_required"], "unsure"),
        "confidence": _clamp(fu.get("confidence"), 0.0, 1.0),
        "reason": (str(fu.get("reason"))[:500] if fu.get("reason") else None),
        "who_promised": _enum(fu.get("who_promised"), _ENUMS["promised_by"]),
        "expected_outcome": (str(fu.get("expected_outcome"))[:300]
                             if fu.get("expected_outcome") else None),
        "type": _enum(fu.get("type"), _ENUMS["followup_type"]),
        "priority": _enum(fu.get("priority"), _ENUMS["priority"], "medium"),
        "suggested_date": date_s,
        "suggested_time": time_s,
        # Relative requests ("call me in 10 minutes"): minutes after call end,
        # clamped to a week; 0/absent = not a relative request.
        "suggested_offset_minutes": max(0, min(offset_min, 7 * 24 * 60)),
    }
    return rec


# ---------------------------------------------------------------------------
# Legacy compatibility blob (calls.analysis) — keeps ConvAnalytics, the
# per-call analysis endpoint, and the callback context builder working
# until they are migrated (removed in the final phase).
# ---------------------------------------------------------------------------

def legacy_blob(rec: dict[str, Any], model: str) -> dict[str, Any]:
    lead = rec.get("lead_score") or 0
    fu = rec.get("follow_up") or {}
    return {
        "summary": rec.get("summary"),
        "customer_intent": rec.get("customer_intent"),
        "key_points": [m["label"] for m in rec.get("key_moments", []) if m.get("label")][:8],
        "customer_questions": [],
        "suggested_next_action": next(iter(
            (rec.get("recommendations") or {}).get("next_actions", [])), None),
        "sentiment": rec.get("sentiment"),
        "lead_quality": "hot" if lead >= 70 else ("warm" if lead >= 40 else "cold"),
        "objections": rec.get("objections", []),
        "topics": rec.get("topics", []),
        "products_mentioned": rec.get("product_interest", []),
        "follow_up_needed": fu.get("required") == "yes",
        "follow_up_reason": fu.get("reason"),
        "follow_up_priority": fu.get("priority"),
        "recommended_follow_up_date": fu.get("suggested_date"),
        "_analyzed_at": _ist_now().isoformat(),
        "_model": model,
        "_pipeline": "intel_v1",
    }
