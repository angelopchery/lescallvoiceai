"""Internal rows -> the documented public JSON shapes.

This module is the API's contract with every CRM that integrates. Two rules
hold it together:

  * **Nothing is passed through by accident.** Every field is named explicitly,
    so adding an internal column can never leak it, and renaming one breaks a
    test here rather than a customer's import at 3am.
  * **Public identifiers are stable strings.** `call_id` is the call_key
    ("<digits>_<YYYYMMDD>_<HHMMSS>"), never the integer primary key — a CRM
    stores that value forever and it must survive any future re-keying.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

# The intelligence fields that hold JSON lists / objects in the DB. Parsed on
# the way out so a client gets arrays, not strings containing arrays.
_JSON_LIST_FIELDS = (
    "sentiment_journey", "topics", "pain_points", "objections", "buying_signals",
    "competitor_mentions", "product_interest", "budget_discussion",
    "timeline_discussion", "decision_makers", "compliance_issues", "escalations",
    "risk_indicators", "tags", "key_moments",
)
_JSON_OBJ_FIELDS = ("agent_performance", "recommendations", "entities")


def ts(value) -> str | None:
    """Every timestamp leaves this API as RFC 3339 UTC.

    Postgres hands this codebase timestamps as STRINGS (database._TextLoader),
    in the "2026-08-12 14:32:10.123+00:00" shape — which several mainstream CRM
    date parsers reject for the missing `T`. Normalizing here means no client
    ever has to discover that.
    """
    if not value:
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        raw = str(value).strip()
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw          # unparseable: hand back what we hold, verbatim
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _j(raw, default="[]"):
    try:
        return json.loads(raw or default)
    except Exception:
        return json.loads(default)


def _f(val):
    return None if val is None else float(val)


def _i(val):
    return None if val is None else int(val)


# ---------------------------------------------------------------------------
# contacts
# ---------------------------------------------------------------------------

def contact(row: dict) -> dict:
    """One contact. `id` is the integer PK as a string — the phone book's
    stable handle for API callers (the dashboard's own `legacy_id` handles are
    a UI artifact and deliberately not exposed)."""
    details = _j(row.get("details"), "{}")
    if not isinstance(details, dict):
        details = {"value": details}
    extra = _j(row.get("extra"), "{}")
    return {
        "id": str(row.get("id")),
        "phone": row.get("phone") or "",
        "name": row.get("name") or "",
        "notes": row.get("notes") or "",
        "gender": row.get("gender") or "",
        "group_id": row.get("group_id"),
        "group_name": row.get("group_name") or None,
        "fields": details,
        "call_status": row.get("call_status") or "not_called",
        "disposition": row.get("disposition") or "not_classified",
        "call_count": _i(row.get("call_count")) or 0,
        "last_called_at": ts(row.get("last_called_at")),
        "call_notes": row.get("call_notes") or "",
        "primary_agent": row.get("primary_agent_slug") or None,
        "created_at": ts(row.get("created_at")),
        "updated_at": ts(row.get("updated_at")),
        "call_history": extra.get("call_history", []),
    }


# ---------------------------------------------------------------------------
# calls
# ---------------------------------------------------------------------------

def call_summary(row: dict, base_url: str = "") -> dict:
    """The list shape: enough for a CRM to decide whether it wants the detail,
    without the transcript or the deep intelligence lists."""
    call_id = row.get("call_key") or row.get("conn_id") or ""
    out = {
        "call_id": call_id,
        "direction": row.get("direction") or "inbound",
        "agent": row.get("agent_slug") or None,
        "phone": row.get("phone") or "",
        "contact_id": (str(row["contact_id"]) if row.get("contact_id") else None),
        "contact_name": row.get("contact_name") or "",
        "language": row.get("language") or None,
        "started_at": ts(row.get("started_at")),
        "ended_at": ts(row.get("ended_at")),
        "duration_seconds": _f(row.get("duration_s")),
        # NULL answer_state means the detector never ran; readers must treat it
        # as 'answered' (see shared/db/database.py MIGRATIONS), so we say so
        # rather than emitting a null a CRM would have to know that rule about.
        "answer_state": row.get("answer_state") or "answered",
        "outcome": row.get("outcome") or None,
        "outcome_kind": row.get("outcome_kind") or None,
        "summary": row.get("summary") or (row.get("ci_summary") or ""),
        "has_recording": bool(row.get("recording_path") or row.get("gcs_object")),
        "has_transcript": bool(row.get("transcript")),
        "analyzed": bool(row.get("ci_processed_at")),
    }
    if row.get("ci_processed_at") or row.get("lead_score") is not None:
        out["scores"] = {
            "lead_score": _i(row.get("lead_score")),
            "opportunity_score": _i(row.get("opportunity_score")),
            "risk_score": _i(row.get("risk_score")),
            "sentiment": row.get("sentiment") or None,
            "conversion_probability": _f(row.get("conversion_probability")),
        }
    if base_url and call_id:
        out["links"] = {
            "self": f"{base_url}/v1/calls/{call_id}",
            "transcript": f"{base_url}/v1/calls/{call_id}/transcript",
            "intelligence": f"{base_url}/v1/calls/{call_id}/intelligence",
            "recording": f"{base_url}/v1/calls/{call_id}/recording",
        }
    return out


def intelligence(ci: dict | None) -> dict | None:
    """The full per-call intelligence record, JSON columns parsed.

    `stages` is the pipeline's internal checkpoint map — omitted, because it
    describes how WE processed the call, not anything about the call.
    """
    if not ci:
        return None
    out = {
        "processed_at": ts(ci.get("processed_at")),
        "model": ci.get("model"),
        "schema_version": _i(ci.get("schema_version")),
        "summary": ci.get("summary") or "",
        "outcome_summary": ci.get("outcome_summary") or "",
        "customer_intent": ci.get("customer_intent") or "",
        "sentiment": ci.get("sentiment") or None,
        "urgency": ci.get("urgency") or None,
        "confidence": _f(ci.get("confidence")),
        "low_confidence": bool(ci.get("low_confidence")),
        "scores": {
            "lead_score": _i(ci.get("lead_score")),
            "opportunity_score": _i(ci.get("opportunity_score")),
            "risk_score": _i(ci.get("risk_score")),
            "compliance_score": _i(ci.get("compliance_score")),
            "conversation_quality": _f(ci.get("conversation_quality")),
            "conversion_probability": _f(ci.get("conversion_probability")),
            "satisfaction": _f(ci.get("satisfaction")),
        },
    }
    for field in _JSON_LIST_FIELDS:
        out[field] = _j(ci.get(field), "[]")
    for field in _JSON_OBJ_FIELDS:
        out[field] = _j(ci.get(field), "{}")
    return out


def commitment(row: dict) -> dict:
    return {
        "id": str(row.get("id")),
        "made_by": row.get("made_by"),
        "kind": row.get("kind"),
        "description": row.get("description") or "",
        "quote": row.get("quote") or "",
        "deadline_text": row.get("deadline_text") or "",
        "deadline_at": ts(row.get("deadline_at")),
        "is_vague": bool(row.get("is_vague")),
        "status": row.get("status") or "open",
        "confidence": _f(row.get("confidence")),
    }


def action_item(row: dict) -> dict:
    return {
        "id": str(row.get("id")),
        "description": row.get("description") or "",
        "status": row.get("status") or "open",
        "priority": row.get("priority") or "medium",
        "assigned_to": row.get("assigned_to") or None,
        "due_at": ts(row.get("due_at")),
        "source": row.get("source") or "ai",
    }


def followup(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "id": str(row.get("id")),
        "decision": row.get("decision"),
        "type": row.get("followup_type"),
        "reason": row.get("reason") or "",
        "summary": row.get("summary") or "",
        "scheduled_at": ts(row.get("scheduled_at")),
        "timezone": row.get("timezone") or "Asia/Kolkata",
        "priority": row.get("priority") or "medium",
        "status": row.get("status") or "pending",
        "auto_scheduled": bool(row.get("auto_scheduled")),
    }


def transcript_turns(transcript: str) -> list[dict]:
    """Speaker-tagged turns. Parsing is the intelligence layer's
    `parse_turns` — imported lazily so this module stays dependency-free for
    tests that only exercise shapes."""
    from shared.intelligence.embed import parse_turns
    return [{"at": ts, "speaker": sp, "text": tx}
            for ts, sp, tx in parse_turns(transcript or "")]


def recording(row: dict, base_url: str = "") -> dict:
    """Where the audio is and how to fetch it.

    Two ways on purpose: `download_url` is the existing tokenized public link
    (no credentials — paste it into a CRM field and it just works), while
    `stream_url` needs the API key and is what a server-to-server importer
    should use when it would rather not persist an unauthenticated URL.
    """
    call_key = row.get("call_key") or ""
    available = bool(row.get("recording_path") or row.get("gcs_object"))
    out: dict[str, Any] = {"available": available, "mime_type": "audio/wav"}
    if not available:
        return out
    if row.get("recording_url"):
        out["download_url"] = row["recording_url"]
    if base_url and call_key:
        out["stream_url"] = f"{base_url}/v1/calls/{call_key}/recording"
    out["stored_in_cloud"] = bool(row.get("gcs_object"))
    return out


def call_detail(row: dict, *, ci: dict | None, commitments: list[dict],
                items: list[dict], fu: dict | None, base_url: str = "",
                include_transcript: bool = True) -> dict:
    """The full export record — the payload a CRM imports per call."""
    out = {
        "call": call_summary(row, base_url),
        "intelligence": intelligence(ci),
        "recording": recording(row, base_url),
        "commitments": [commitment(c) for c in commitments],
        "action_items": [action_item(a) for a in items],
        "followup": followup(fu),
        "collected_data": _j(row.get("collected_data"), "{}"),
    }
    if include_transcript:
        out["transcript"] = {
            "text": row.get("transcript") or "",
            "turns": transcript_turns(row.get("transcript") or ""),
        }
    return out
