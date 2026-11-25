"""Outcome intelligence for dials that never connected.

Connected calls get calls.outcome at teardown and a timeline event from the
pipeline. Failed outbound attempts (busy / no answer / rejected / invalid
number / network failure) leave only a call_links row and a FreeSWITCH CDR
line — this pass joins the two and lands a classified `call.missed` event
(with a retry recommendation) on the contact's timeline.

Read-alongside by design: it opens the same CDR CSV the billing referee
reads but NEVER touches usage_ledger or any billing semantics. Runs inside
the intel worker's sweeper loop on its own cadence.

Idempotency: events ref call_links rows, deduped by uq_contact_events_ref.
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
from typing import Any

from shared.db.database import get_db
from shared.intelligence import timeline
from shared.usage_metering import CDR_PATH, _CDR_COLS, _IDX, parse_ts

log = logging.getLogger("intel.dial_outcomes")

# FreeSWITCH hangup cause → outcome taxonomy.
CAUSE_MAP = {
    "USER_BUSY": "busy",
    "NO_ANSWER": "no_answer",
    "ORIGINATOR_CANCEL": "no_answer",
    "ALLOTTED_TIMEOUT": "no_answer",
    "NO_USER_RESPONSE": "no_answer",
    "CALL_REJECTED": "rejected",
    "UNALLOCATED_NUMBER": "invalid_number",
    "INVALID_NUMBER_FORMAT": "invalid_number",
    "NO_ROUTE_DESTINATION": "invalid_number",
    "NORMAL_TEMPORARY_FAILURE": "network_failure",
    "NETWORK_OUT_OF_ORDER": "network_failure",
    "RECOVERY_ON_TIMER_EXPIRE": "network_failure",
    "USER_NOT_REGISTERED": "network_failure",
}

# Outcome → retry recommendation (rendered in the timeline + hub).
RETRY_HINTS = {
    "busy": {"retry_after_min": 30, "note": "line was busy — retry shortly"},
    "no_answer": {"retry_after_min": 240,
                  "note": "no answer — retry in the customer's preferred call window"},
    "rejected": {"retry_after_min": 1440, "note": "call rejected — retry next day"},
    "invalid_number": {"retry_after_min": None,
                       "note": "number invalid — verify the contact's phone"},
    "network_failure": {"retry_after_min": 60, "note": "network failure — retry in an hour"},
    "other_failed": {"retry_after_min": 240, "note": "unclassified failure"},
}

# How closely a CDR leg must follow the dial to count as the same attempt.
_MATCH_BEFORE_S = 60
_MATCH_AFTER_S = 300


def classify_cause(cause: str) -> str:
    return CAUSE_MAP.get((cause or "").strip().upper(), "other_failed")


def _load_unanswered(path: str | None = None) -> list[dict[str, Any]]:
    """CDR rows with no answer_stamp: {dest, start, cause, uuid}."""
    out: list[dict[str, Any]] = []
    try:
        with open(path or CDR_PATH, newline="", encoding="utf-8",
                  errors="replace") as f:
            for cells in csv.reader(f):
                if len(cells) < len(_CDR_COLS):
                    continue
                if cells[_IDX["answer_stamp"]].strip():
                    continue  # connected — the calls/billing path owns it
                out.append({
                    "dest": cells[_IDX["destination_number"]].strip(),
                    "start": cells[_IDX["start_stamp"]].strip(),
                    "cause": cells[_IDX["hangup_cause"]].strip(),
                    "uuid": cells[_IDX["uuid"]].strip(),
                })
    except FileNotFoundError:
        log.warning("CDR file not found: %s", path or CDR_PATH)
    except PermissionError:
        log.error("CDR file not readable: %s", path or CDR_PATH)
    return out


def match_dials(dials: list[dict[str, Any]],
                cdr_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pure join: each dial matched to the first unanswered CDR leg for the
    same significant digits inside the time window."""
    from shared.phone import significant_digits

    by_sig: dict[str, list[dict]] = {}
    for row in cdr_rows:
        sig = significant_digits(row["dest"])
        if sig:
            by_sig.setdefault(sig, []).append(row)

    matches = []
    for dial in dials:
        sig = dial.get("sig_digits") or significant_digits(dial.get("phone") or "")
        dialed = parse_ts(dial.get("dialed_at"))
        if not sig or dialed is None:
            continue
        for row in by_sig.get(sig, ()):
            start = parse_ts(row["start"])
            if start is None:
                continue
            delta = (start - dialed).total_seconds()
            if -_MATCH_BEFORE_S <= delta <= _MATCH_AFTER_S:
                matches.append({**dial, "cdr": row,
                                "outcome": classify_cause(row["cause"])})
                break
    return matches


def classify_recent(hours: int = 24, cdr_path: str | None = None) -> int:
    """One pass: emit call.missed events for recent failed dials. Returns the
    number of events written."""
    from shared.phone import significant_digits

    cutoff = (dt.datetime.utcnow() - dt.timedelta(hours=hours)
              ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_db() as db:
        dials = [dict(r) for r in db.execute(
            """SELECT id, owner, phone, sig_digits, contact_id, name, dialed_at
                 FROM call_links WHERE dialed_at >= ?""", (cutoff,)
        ).fetchall()]
    if not dials:
        return 0
    cdr_rows = _load_unanswered(cdr_path)
    if not cdr_rows:
        return 0

    written = 0
    for m in match_dials(dials, cdr_rows):
        outcome = m["outcome"]
        sig = m.get("sig_digits") or significant_digits(m.get("phone") or "")
        with get_db() as db:
            contact_id = timeline.resolve_contact(db, m.get("owner") or "",
                                                  m.get("contact_id"), sig)
        if contact_id is None:
            continue
        start = parse_ts(m["cdr"]["start"])
        occurred = (start.strftime("%Y-%m-%dT%H:%M:%SZ") if start
                    else m.get("dialed_at"))
        if timeline.emit(contact_id, m.get("owner") or "", "call.missed",
                         ref_table="call_links", ref_id=m["id"],
                         occurred_at=occurred,
                         payload={
                             "outcome": outcome,
                             "hangup_cause": m["cdr"]["cause"],
                             "phone": m.get("phone"),
                             "retry": RETRY_HINTS.get(outcome,
                                                      RETRY_HINTS["other_failed"]),
                         }):
            written += 1
    if written:
        log.info("dial_outcomes: %d failed dial(s) classified", written)
    return written
