"""Stage 6 — in-app notifications (bell + SSE).

Backfill-safe: only calls that ended recently produce notifications, so
re-processing history never floods the bell. Noise-controlled: routine
neutral calls stay silent; signal-bearing outcomes (hot lead, follow-up
scheduled, negative sentiment, compliance issue) notify.
"""

from __future__ import annotations

import json
from typing import Any

from shared.db.database import get_db

FRESH_WINDOW_S = 3600


def _is_fresh(call: dict[str, Any]) -> bool:
    with get_db() as db:
        row = db.execute(
            "SELECT 1 FROM calls WHERE id=? AND created_at > now() - make_interval(secs => ?)",
            (call["id"], FRESH_WINDOW_S),
        ).fetchone()
    return bool(row)


def _emit(owner: str, kind: str, severity: str, title: str, body: str,
          link: str, ref: str) -> None:
    with get_db() as db:
        exists = db.execute(
            "SELECT 1 FROM notifications WHERE owner=? AND kind=? AND ref=?",
            (owner, kind, ref),
        ).fetchone()
        if exists:  # idempotent re-run
            return
        db.execute(
            """INSERT INTO notifications (owner, kind, severity, title, body, link, ref)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (owner, kind, severity, title, body, link, ref),
        )


async def stage_notify(call: dict[str, Any]) -> None:
    if not _is_fresh(call):
        return
    call_id = call["id"]
    owner = call.get("owner") or ""
    call_key = call.get("call_key") or str(call_id)
    link = f"/intelligence/{call_key}"

    with get_db() as db:
        ci = db.execute(
            "SELECT summary, sentiment, lead_score, compliance_score, urgency"
            " FROM call_intelligence WHERE call_id=?", (call_id,)
        ).fetchone()
        fu = db.execute(
            "SELECT decision, scheduled_at, reason FROM followups"
            " WHERE call_id=? ORDER BY id DESC LIMIT 1", (call_id,)
        ).fetchone()
    if not ci:
        return

    phone = call.get("phone") or "unknown number"
    lead = ci["lead_score"] or 0
    if lead >= 70:
        _emit(owner, "lead.hot", "warn",
              f"Hot lead: {phone} (score {lead})",
              ci["summary"] or "", link, f"call:{call_id}:hot")
    if ci["sentiment"] == "negative":
        _emit(owner, "sentiment.negative", "warn",
              f"Negative call: {phone}",
              ci["summary"] or "", link, f"call:{call_id}:neg")
    if (ci["compliance_score"] or 100) < 70:
        _emit(owner, "compliance.issue", "critical",
              f"Compliance issue on call with {phone}",
              ci["summary"] or "", link, f"call:{call_id}:comp")
    if fu and fu["decision"] == "yes":
        when = (fu["scheduled_at"] or "")[:16]
        _emit(owner, "followup.created", "info",
              f"Follow-up scheduled for {phone}" + (f" — {when}" if when else ""),
              fu["reason"] or "", "/followups", f"call:{call_id}:fu")
    else:
        _emit(owner, "call.analyzed", "info",
              f"Call analyzed: {phone}",
              (ci["summary"] or "")[:200], link, f"call:{call_id}:done")


# ---------------------------------------------------------------------------
# Auto-dialer alerts
# ---------------------------------------------------------------------------

def notify_autodial_unroutable(owner: str, detail: str, pending: int) -> None:
    """Follow-ups are due but the account has nothing to call them FROM.

    This is the silent-failure case worth shouting about: the AI keeps
    scheduling follow-ups, the executor keeps finding them, and every one is
    refused because no agent is bound to an outbound number (or its trunk was
    never applied). Nothing in the UI would otherwise say so — the rows just
    age into 'missed', which looks like the customer didn't answer.

    Deduped per UTC day via `ref`, so a permanently mis-configured account gets
    one notification a day rather than one every two minutes.
    """
    import datetime as _dt

    day = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    _emit(owner, "autodial.unroutable", "critical",
          f"{pending} follow-up call{'s' if pending != 1 else ''} could not be placed",
          f"{detail} Activate an agent on one of your numbers' OUTBOUND slot "
          f"in Telephony, then these will dial automatically.",
          "/telephony", f"autodial:unroutable:{owner}:{day}")


def notify_autodial_blocked(owner: str, followup_id: int, who: str,
                            reason: str) -> None:
    """One follow-up hit a PERMANENT guardrail and will not be auto-dialled.

    Only fires for the terminal cases (retry cap, DND, too old) — transient
    skips like 'outside calling hours' are normal operation and stay silent.
    """
    _emit(owner, "autodial.blocked", "warn",
          f"Auto-calling stopped for {who}",
          reason, "/followups", f"autodial:blocked:{followup_id}")
