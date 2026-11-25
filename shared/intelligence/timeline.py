"""Stage — unified contact timeline (contact_events emitter).

Every analyzed call lands on its contact's timeline automatically: the call
itself, the analysis verdict, each commitment, the follow-up decision, and
the memory merge. The sweeper backfills these events across historical calls
for free (new-stage retrofit), so the timeline is complete from day one.

Idempotency: uq_contact_events_ref (kind, ref_table, ref_id) — every emit
here carries a ref, so re-runs are no-ops via ON CONFLICT DO NOTHING.
Payloads are small JSON render summaries, never transcripts.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from shared.db.database import get_db

log = logging.getLogger("intel.timeline")

_CONFLICT = ("ON CONFLICT (kind, ref_table, ref_id)"
             " WHERE ref_table IS NOT NULL AND ref_id IS NOT NULL DO NOTHING")


def emit(contact_id: int, owner: str, kind: str, *,
         ref_table: str | None = None, ref_id: int | None = None,
         occurred_at: str | None = None, payload: dict | None = None) -> bool:
    """Append one event. Returns True if a row was written (False = dedupe
    hit or failure). Never raises — timeline writes are best-effort
    everywhere except the pipeline stage, which wants the exception."""
    try:
        return emit_strict(contact_id, owner, kind, ref_table=ref_table,
                           ref_id=ref_id, occurred_at=occurred_at,
                           payload=payload) > 0
    except Exception as exc:
        log.warning("timeline emit %s failed for contact %s: %r", kind, contact_id, exc)
        return False


def emit_strict(contact_id: int, owner: str, kind: str, *,
                ref_table: str | None = None, ref_id: int | None = None,
                occurred_at: str | None = None, payload: dict | None = None) -> int:
    """Append one event; returns rows written (0 = dedupe hit)."""
    body = json.dumps(payload, ensure_ascii=False) if payload else None
    with get_db() as db:
        if ref_table is not None and ref_id is not None:
            cur = db.execute(
                f"""INSERT INTO contact_events
                        (contact_id, owner, kind, ref_table, ref_id, occurred_at, payload)
                    VALUES (?, ?, ?, ?, ?, COALESCE(?::timestamptz, now()), ?)
                    {_CONFLICT}""",
                (contact_id, owner, kind, ref_table, ref_id, occurred_at, body),
            )
        else:
            cur = db.execute(
                """INSERT INTO contact_events
                       (contact_id, owner, kind, occurred_at, payload)
                   VALUES (?, ?, ?, COALESCE(?::timestamptz, now()), ?)""",
                (contact_id, owner, kind, occurred_at, body),
            )
        return max(cur.rowcount, 0)


def resolve_contact(db, owner: str, contact_ref: str | int | None,
                    sig: str) -> int | None:
    """Map a loose contact reference (numeric PK, legacy id, or nothing) plus
    significant digits to the contacts row PK. None = no contact to attach."""
    if contact_ref is not None and str(contact_ref).isdigit():
        row = db.execute("SELECT id FROM contacts WHERE id=? AND owner=?",
                         (int(contact_ref), owner)).fetchone()
        if row:
            return row["id"]
    if sig:
        row = db.execute("SELECT id FROM contacts WHERE owner=? AND sig_digits=?",
                         (owner, sig)).fetchone()
        if row:
            return row["id"]
    return None


def _trim(text: str | None, n: int) -> str | None:
    if not text:
        return None
    text = str(text).strip()
    return text[: n - 1] + "…" if len(text) > n else text


async def stage_timeline(call: dict[str, Any]) -> None:
    contact_id = call.get("contact_id")
    if not contact_id:
        return
    call_id = call["id"]
    owner = call.get("owner") or ""
    at = call.get("started_at") or call.get("created_at")

    with get_db() as db:
        ci = db.execute(
            """SELECT lead_score, opportunity_score, risk_score, sentiment,
                      customer_intent
                 FROM call_intelligence WHERE call_id=?""", (call_id,)
        ).fetchone()
        commitments = [dict(r) for r in db.execute(
            """SELECT id, made_by, kind, description, deadline_at
                 FROM commitments WHERE call_id=?""", (call_id,)
        ).fetchall()]
        followups = [dict(r) for r in db.execute(
            """SELECT id, decision, followup_type, scheduled_at, priority
                 FROM followups WHERE call_id=? AND decision='yes'
                  AND deleted_at IS NULL""", (call_id,)
        ).fetchall()]
        mem = db.execute(
            "SELECT id, version FROM customer_memory WHERE contact_id=?",
            (contact_id,)
        ).fetchone()

    direction = (call.get("direction") or "inbound").lower()
    emit_strict(contact_id, owner, f"call.{direction}",
                ref_table="calls", ref_id=call_id, occurred_at=at,
                payload={
                    "duration_s": call.get("duration_s"),
                    "outcome": call.get("outcome"),
                    "outcome_kind": call.get("outcome_kind"),
                    "language": call.get("language"),
                    "agent_slug": call.get("agent_slug"),
                    "call_key": call.get("call_key") or call.get("conn_id"),
                    "summary": _trim(call.get("summary"), 200),
                })
    if ci:
        emit_strict(contact_id, owner, "call.analyzed",
                    ref_table="calls", ref_id=call_id, occurred_at=at,
                    payload={
                        "lead_score": ci["lead_score"],
                        "opportunity_score": ci["opportunity_score"],
                        "risk_score": ci["risk_score"],
                        "sentiment": ci["sentiment"],
                        "customer_intent": _trim(ci["customer_intent"], 120),
                        "call_key": call.get("call_key") or call.get("conn_id"),
                    })
    for c in commitments:
        emit_strict(contact_id, owner, "commitment.made",
                    ref_table="commitments", ref_id=c["id"], occurred_at=at,
                    payload={
                        "made_by": c["made_by"], "kind": c["kind"],
                        "description": _trim(c["description"], 200),
                        "deadline_at": c["deadline_at"],
                    })
    for f in followups:
        emit_strict(contact_id, owner, "followup.created",
                    ref_table="followups", ref_id=f["id"], occurred_at=at,
                    payload={
                        "followup_type": f["followup_type"],
                        "scheduled_at": f["scheduled_at"],
                        "priority": f["priority"],
                    })
    if mem:
        emit_strict(contact_id, owner, "memory.updated",
                    ref_table="calls", ref_id=call_id, occurred_at=at,
                    payload={"version": mem["version"]})
