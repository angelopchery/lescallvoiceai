"""Follow-up auto-dialer — guardrails and candidate selection.

The follow-up engine decides a call is needed and schedules a time; until now
nothing placed it, and `sweep_overdue` flipped every due row to 'missed' an
hour later (0 of 155 follow-ups had ever completed). This module decides WHICH
rows may be dialled and WHY a row may not be; the actual origination lives in
dashboard/server.py, which owns the caller-ID/trunk identity, the duplicate
guard and the FreeSWITCH channel.

Everything that makes a decision here is a pure function over (row, now,
counters) so the guardrails can be tested without a database, a clock, or a
phone. `due_candidates` and `record_attempt` are the only DB-touching parts.

The guardrail set, and why each exists:

    window       a customer called at 06:00 is a complaint, not a follow-up
    weekday      Sunday calling generates complaints faster than anything else
    retry cap    without it, a restart loop redials the same person forever
    retry gap    three attempts inside a minute reads as harassment
    daily cap    bounds the blast radius of a bad batch to something survivable
    grace        a human gets a moment to intervene on a just-scheduled row
    max age      a follow-up nobody chased for a week is cold; calling is worse
                 than not calling
    DND          a customer who asked us to stop is never dialled again
    kill switch  config.AUTODIAL_ENABLED, re-read every tick

A row that trips a PERMANENT guardrail (retry cap, DND) is stamped
autodial_blocked_at and never reconsidered. A TRANSIENT one (window, caps)
leaves it eligible for the next sweep.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from shared.db.database import get_db
from shared.intelligence import config

log = logging.getLogger("intel.autodial")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# Guardrails whose verdict can never change for this row, so the executor stops
# reconsidering it. Everything else is retried on the next sweep.
# 'superseded': a call to this contact happened AFTER the follow-up was
# created — the conversation the follow-up wanted already took place; dialling
# again reads as "we forgot we just spoke".
PERMANENT = {"retry_cap", "dnd", "too_old", "no_contact", "superseded"}


def now_local() -> dt.datetime:
    return dt.datetime.now(IST)


def as_dt(value: Any) -> dt.datetime | None:
    """Coerce a DB timestamp to an aware datetime, or None.

    Load-bearing: this database layer hands TIMESTAMPTZ columns back as
    STRINGS ('2026-07-14 07:30:00+00'), not datetimes. The first version of
    blocked_reason() type-checked with isinstance(..., datetime) and therefore
    skipped BOTH the not-due and too-old gates on every real row — a dry run
    showed it would have dialled follow-ups scheduled three weeks into the
    future, immediately. Never assume these columns are datetimes.

    A naive value is interpreted as IST, matching followups.timezone.
    """
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=IST)
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    # '+00' / '-0530' → '+00:00' / '-05:30' for fromisoformat on older parsers.
    for pat, repl in ((r"([+-]\d{2})$", r"\1:00"), (r"([+-]\d{2})(\d{2})$", r"\1:\2")):
        import re as _re
        text = _re.sub(pat, repl, text)
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)


# ---------------------------------------------------------------------------
# Pure guardrail predicates
# ---------------------------------------------------------------------------

def within_window(when: dt.datetime, *, customer_requested: bool = False) -> bool:
    """True if `when` (local) is inside the permitted calling window.

    customer_requested=True is the row whose time the CUSTOMER stated
    ("call me at 8 tonight", auto_scheduled=0): honouring their own ask
    outranks the business window, so a wider courtesy window applies
    (AUTODIAL_CB_HOUR_START..END, any weekday). AI-picked times keep the
    conservative default window.
    """
    if customer_requested:
        return (config.AUTODIAL_CB_HOUR_START <= when.hour
                < config.AUTODIAL_CB_HOUR_END)
    if when.weekday() not in config.autodial_weekdays():
        return False
    return config.AUTODIAL_HOUR_START <= when.hour < config.AUTODIAL_HOUR_END


def next_window_open(when: dt.datetime,
                     customer_requested: bool = False) -> dt.datetime:
    """The next moment the window opens at or after `when` — used to explain a
    skip ("will call after 10:00 Mon") rather than just refusing."""
    probe = when
    for _ in range(14 * 24):  # bounded: a fortnight of hourly probes
        if within_window(probe, customer_requested=customer_requested):
            return probe
        nxt = (probe + dt.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        if nxt.hour < probe.hour:  # rolled past midnight
            nxt = nxt.replace(hour=0)
        probe = nxt
    return when


def blocked_reason(row: dict[str, Any], *, now: dt.datetime,
                   placed_today: int) -> str | None:
    """None when the row may be dialled right now, else a machine-readable code.

    Order matters: the most permanent/serious reason is reported first, so a
    DND contact never gets explained away as merely "outside window".
    """
    if row.get("dnd"):
        return "dnd"
    # Human approval, re-checked outside the SQL. _DUE_SQL already filters
    # unapproved rows, so this is deliberately redundant: it means an edit to
    # that one query cannot, on its own, put the platform back to cold-calling
    # people. Both would have to be wrong at once. Absent/unknown value reads
    # as NOT approved — the fail-safe direction is always "don't call".
    if str(row.get("approval_state") or "pending") != "approved":
        return "not_approved"
    approval_expiry = as_dt(row.get("approval_expires_at"))
    if approval_expiry is not None and now >= approval_expiry:
        return "approval_expired"
    # A dialable number is the requirement, not a live contacts row: users
    # delete/recreate contacts routinely, and treating a dangling contact_id
    # as terminal permanently blocked 38 live follow-ups in the executor's
    # first 30 minutes — every one of which had a usable phone via the calls
    # join (dial_phone = COALESCE(ct.phone, c.phone)).
    if not row.get("dial_phone"):
        return "no_contact"
    max_attempts = int(row.get("max_attempts") or 0) or config.AUTODIAL_MAX_ATTEMPTS
    if int(row.get("attempts") or 0) >= max_attempts:
        return "retry_cap"

    # The conversation already happened: a call to this contact placed AFTER
    # the follow-up was created supersedes it (the analysis of that call will
    # have produced its own follow-up decision).
    last_call = as_dt(row.get("last_contact_call_at"))
    created = as_dt(row.get("created_at"))
    if last_call is not None and created is not None and last_call > created:
        return "superseded"

    scheduled = as_dt(row.get("scheduled_at"))
    if scheduled is None:
        # No time on the row means nothing decided WHEN to call. Refuse rather
        # than treat "unscheduled" as "call right now".
        return "not_due"
    # auto_scheduled=0 means the CUSTOMER stated this time ("call me in 10
    # minutes", "tomorrow at 4"). Their explicit ask skips the human-review
    # grace — a 10-minute callback that waits grace+poll arrives ~15 minutes
    # late and reads as a broken promise. AI-guessed times keep the grace.
    customer_requested = not int(row.get("auto_scheduled") or 0)
    due_at = scheduled if customer_requested else (
        scheduled + dt.timedelta(minutes=config.AUTODIAL_GRACE_MIN))
    if now < due_at:
        return "not_due"
    if now - scheduled > dt.timedelta(hours=config.AUTODIAL_MAX_AGE_H):
        return "too_old"

    # Explicit backoff wins over the implicit gap when set (written by
    # record_attempt as attempts×gap linear backoff).
    next_try = as_dt(row.get("next_attempt_at"))
    if next_try is not None:
        if now < next_try:
            return "retry_gap"
    else:
        last = as_dt(row.get("last_attempt_at"))
        if last is not None and now - last < dt.timedelta(minutes=config.AUTODIAL_RETRY_GAP_MIN):
            return "retry_gap"

    if placed_today >= config.AUTODIAL_MAX_PER_DAY:
        return "daily_cap"
    if not within_window(now, customer_requested=customer_requested):
        return "outside_window"
    return None


def explain(reason: str, now: dt.datetime | None = None) -> str:
    """Human sentence for a guardrail code — shown on the follow-up row."""
    now = now or now_local()
    return {
        "dnd": "Customer asked not to be contacted — never auto-dialled.",
        "not_approved": ("Waiting for your approval — review it under "
                         "“Review & approve calls” before it can be dialled."),
        "approval_expired": ("The approval for this call has expired — "
                             "approve it again to let the platform dial."),
        "no_contact": "No phone number on the follow-up or its call.",
        "superseded": ("A newer call to this customer already happened — "
                       "this follow-up is settled."),
        "retry_cap": f"Reached the {config.AUTODIAL_MAX_ATTEMPTS}-attempt limit — needs a human.",
        "not_due": "Not due yet.",
        "too_old": f"More than {config.AUTODIAL_MAX_AGE_H}h past its time — too cold to auto-call.",
        "retry_gap": f"Waiting {config.AUTODIAL_RETRY_GAP_MIN} min between attempts.",
        "daily_cap": f"Daily auto-call limit ({config.AUTODIAL_MAX_PER_DAY}) reached.",
        "outside_window": ("Outside calling hours — will dial after "
                           f"{next_window_open(now):%a %H:%M}."),
        "no_identity": "No agent is activated on an outbound number for this account.",
    }.get(reason, reason)


# ---------------------------------------------------------------------------
# Candidate selection (DB)
# ---------------------------------------------------------------------------

_DUE_SQL = """
    SELECT f.id, f.owner, f.call_id, f.contact_id, f.scheduled_at, f.priority,
           f.reason, f.summary, f.attempts, f.last_attempt_at, f.status,
           f.mode, f.max_attempts, f.next_attempt_at, f.created_at,
           f.auto_scheduled, f.followup_type,
           f.approval_state, f.approved_at, f.approved_by, f.approval_expires_at,
           COALESCE(ct.legacy_id, 'db' || ct.id::text) AS contact_handle,
           COALESCE(ct.phone, c.phone)                 AS dial_phone,
           ct.name                                     AS contact_name,
           (ct.disposition = 'rejected')               AS dnd,
           cm.description                              AS commitment_text,
           c.started_at                                AS source_call_at,
           ci.customer_intent                          AS source_call_intent,
           (SELECT max(c3.started_at) FROM calls c3
             WHERE c3.contact_id = f.contact_id AND c3.owner = f.owner
               AND f.contact_id IS NOT NULL)           AS last_contact_call_at
      FROM followups f
      LEFT JOIN calls c    ON c.id  = f.call_id
      LEFT JOIN contacts ct ON ct.id = f.contact_id
      LEFT JOIN commitments cm ON cm.id = f.commitment_id
      LEFT JOIN call_intelligence ci ON ci.call_id = f.call_id
     WHERE f.deleted_at IS NULL
       AND f.autodial_blocked_at IS NULL
       AND f.decision = 'yes'
       AND f.mode = 'auto'
       -- THE gate. A human approved THIS row, by name, before we may ring
       -- anyone. Never widen this predicate; see 0011 for why it exists.
       AND f.approval_state = 'approved'
       AND (f.approval_expires_at IS NULL OR f.approval_expires_at > now())
       AND f.status IN ('pending', 'scheduled', 'missed')
       AND f.scheduled_at IS NOT NULL
     ORDER BY f.priority = 'urgent' DESC, f.scheduled_at
     LIMIT ?"""


def due_candidates(limit: int = 200) -> list[dict[str, Any]]:
    """Rows the executor should consider this sweep, most urgent first.

    Three independent conditions must ALL hold, and they mean different things:

    `decision='yes'`   the AI judged a follow-up worthwhile. Advisory only.
    `mode='auto'`      someone is willing for the platform to dial this KIND
                       of row. A preference; a config default can set it.
    `approval_state`   a named human authorised THIS row. A fact about a
                       person's action. Nothing but the approve endpoint can
                       write it, so no default, flag, or prompt regression can
                       manufacture consent.

    The third one is the only real safeguard, because the first two have both
    failed in production: 0007's executor dialled every yes-row (12 unwanted
    calls, 2026-07-23), and after 0008 made mode opt-in, commit b693a8c
    flipped the per-agent default back on and the dialer called a customer who
    had promised nothing (2026-07-29, follow-up 216). Consent is not a
    default. Do not reduce these three to fewer.
    """
    with get_db() as db:
        return [dict(r) for r in db.execute(_DUE_SQL, (limit,)).fetchall()]


def placed_today(owner: str) -> int:
    """Auto-calls already placed for this tenant in the current local day."""
    with get_db() as db:
        row = db.execute(
            """SELECT count(*) AS n FROM followups
                WHERE owner = ? AND last_attempt_at IS NOT NULL
                  AND (last_attempt_at AT TIME ZONE 'Asia/Kolkata')::date
                      = (now() AT TIME ZONE 'Asia/Kolkata')::date
                  AND last_attempt_error IS NULL""",
            (owner,),
        ).fetchone()
    return int(row["n"] or 0)


def record_attempt(fid: int, *, ok: bool, detail: str = "",
                   permanent: bool = False) -> None:
    """Stamp the outcome of one auto-dial attempt.

    `attempts` only increments when a call was actually PLACED — a row skipped
    for being outside the window must not burn a retry, or an overnight backlog
    would exhaust its budget before business hours.
    """
    with get_db() as db:
        row = db.execute(
            "SELECT audit FROM followups WHERE id=?", (fid,)).fetchone()
        audit = []
        if row:
            try:
                audit = json.loads(row["audit"] or "[]")
            except Exception:
                audit = []
        audit.append({
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "by": "autodial",
            "action": "call_placed" if ok else "call_skipped",
            "detail": detail[:300],
        })
        if ok:
            # Linear backoff: attempt N schedules attempt N+1 no sooner than
            # N × retry-gap from now. Written explicitly (next_attempt_at) so
            # the UI can answer "why isn't it calling yet" without modelling
            # the executor's implicit-gap rule.
            db.execute(
                """UPDATE followups
                      SET attempts = attempts + 1, last_attempt_at = now(),
                          next_attempt_at = now() + make_interval(
                              mins => ? * (attempts + 1)),
                          last_attempt_error = NULL, status = 'scheduled',
                          audit = ?, updated_at = now()
                    WHERE id = ?""",
                (config.AUTODIAL_RETRY_GAP_MIN, json.dumps(audit[-50:]), fid),
            )
        else:
            db.execute(
                """UPDATE followups
                      SET last_attempt_error = ?, audit = ?, updated_at = now(),
                          autodial_blocked_at = CASE WHEN ? THEN now()
                                                     ELSE autodial_blocked_at END
                    WHERE id = ?""",
                (detail[:300], json.dumps(audit[-50:]), permanent, fid),
            )


def record_result(fid: int, outcome: str) -> None:
    """Stamp the FreeSWITCH originate result on the follow-up.

    answered|no_answer|busy|declined|failed. Non-answered rows stay
    retry-eligible (record_attempt already set the backoff); 'answered' is
    provisional — the analysis-time completion hook in stage_followup is what
    marks the row completed, because an originate +OK also fires for
    voicemail pickups.
    """
    with get_db() as db:
        db.execute(
            "UPDATE followups SET execution_outcome=?, updated_at=now() "
            "WHERE id=?",
            (outcome[:32], fid),
        )
