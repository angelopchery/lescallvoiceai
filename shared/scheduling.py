"""Scheduled calls and scheduled campaigns — when a dial is allowed to fire.

The outbound surface could only act NOW. This module is the "later": a row
saying who to call (or which campaign to run) and when, plus the rules that
decide whether a due row may actually be dialled at the moment it comes up.

Split of responsibility, matching `shared/intelligence/autodial.py`
-------------------------------------------------------------------
Everything that makes a DECISION here is a pure function over (row, now) so
the guardrails can be tested without a database, a clock or a phone.
`claim_due`, `create`, `cancel`, `finish` and `upcoming` are the only
DB-touching parts, and none of them dials: origination stays in
dashboard/server.py, which owns caller-ID identity, the trunk, the billing
gate and the FreeSWITCH channel.

The guardrails, and why each exists
-----------------------------------
    not yet due    the obvious one, and the reason `scheduled_at` is an
                   absolute instant rather than a wall-clock string
    calling hours  RE-CHECKED AT FIRE TIME, not only when the row was created.
                   A schedule accepted for 18:55 that the executor reaches at
                   19:05 — a slow campaign ahead of it, a deploy, a paused VM —
                   must not ring anyone. The window is the same one campaigns
                   and follow-ups obey; a scheduler that could dial outside it
                   would be the only way to get a 6am call out of this system.
    staleness      a schedule the executor missed by more than STALE_AFTER is
                   MISSED, never fired late. This is the guardrail that costs
                   something to get wrong: lescall-api restarts on every
                   deploy, so "fire everything that is due" after an outage
                   means a burst of calls at whatever hour the box came back,
                   to people who were promised a specific time. Late is worse
                   than never here — the customer already knows we said 4pm.
    terminal once  done/failed/missed/cancelled are ends. A failed schedule is
                   not retried: the failures are "no minutes", "no trunk",
                   "outside hours" — a blind retry fixes none of them and
                   re-dials a real person while nobody is watching.

Times are stored and compared as absolute instants. The UI collects a
wall-clock time and the operator thinks in IST, so the boundary converts once,
at the edge; everything below `create()` is tz-aware UTC.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from shared.db.database import get_db
from shared.intelligence import autodial, config as intel_config

log = logging.getLogger("shared.scheduling")

IST = autodial.IST

KINDS = ("call", "campaign")
PENDING = "pending"
RUNNING = "running"
TERMINAL = ("done", "failed", "missed", "cancelled")

# How late the executor may still fire a schedule. Past this it is MISSED.
# 30 minutes covers a deploy, a restart and a long campaign holding the loop;
# it does not cover "the box was down all night", which is exactly the case
# that must never turn into a 3am burst of calls.
STALE_AFTER = dt.timedelta(minutes=30)

# How far ahead a schedule may be set. Not a technical limit — a year-out
# reminder is not a call anyone will still want placed, and the row would sit
# in the table outliving the contact, the agent and the phone number.
MAX_HORIZON = dt.timedelta(days=90)

# A schedule must be far enough out that the operator can still cancel it.
MIN_LEAD = dt.timedelta(minutes=1)


class ScheduleError(Exception):
    """Rejected at the boundary — the message is shown to the operator."""


# ---------------------------------------------------------------------------
# Pure decisions
# ---------------------------------------------------------------------------

def to_utc(value: Any) -> dt.datetime | None:
    """Coerce anything the DB or an API body can hand us to aware UTC.

    The database layer returns TIMESTAMPTZ as STRINGS ('2026-09-09 07:30:00+00')
    — see `autodial.as_dt`, where assuming otherwise silently skipped two
    guardrails on every real row. A naive value is read as IST, because the
    only naive times in this system are wall-clock times an operator typed.
    """
    when = autodial.as_dt(value)
    return when.astimezone(dt.timezone.utc) if when else None


def fire_decision(row: dict[str, Any], *, now: dt.datetime) -> str:
    """One of 'fire' | 'wait' | 'missed' | 'outside_hours'.

    `now` is aware. Pure: no DB, no clock of its own.
    """
    if (row.get("status") or "") != PENDING:
        return "wait"
    when = to_utc(row.get("scheduled_at"))
    if when is None:
        return "missed"
    now_utc = now.astimezone(dt.timezone.utc)
    if now_utc < when:
        return "wait"
    if now_utc - when > STALE_AFTER:
        return "missed"
    if not autodial.within_window(now_utc.astimezone(IST)):
        return "outside_hours"
    return "fire"


def validate_time(when: dt.datetime, *, now: dt.datetime) -> dt.datetime:
    """The time a schedule may be created for, or raise ScheduleError.

    Refusing at CREATE time is the point: an operator who picks 21:30 learns
    now, while they are looking at the form and can pick 10:00 instead. The
    alternative is a row that sits looking scheduled and quietly becomes
    'outside_hours' at the moment it was supposed to ring.
    """
    when_utc = to_utc(when)
    if when_utc is None:
        raise ScheduleError("Pick a date and time for this call.")
    now_utc = now.astimezone(dt.timezone.utc)
    if when_utc - now_utc < MIN_LEAD:
        raise ScheduleError(
            "That time has already passed. Pick a time at least a minute from now.")
    if when_utc - now_utc > MAX_HORIZON:
        raise ScheduleError(
            f"Schedules can be set up to {MAX_HORIZON.days} days ahead.")
    local = when_utc.astimezone(IST)
    if not autodial.within_window(local):
        opens = autodial.next_window_open(local)
        raise ScheduleError(
            f"{local.strftime('%a %d %b at %H:%M')} IST is outside calling hours "
            f"({intel_config.AUTODIAL_HOUR_START:02d}:00–"
            f"{intel_config.AUTODIAL_HOUR_END:02d}:00 IST, Mon–Sat). "
            f"The next slot is {opens.strftime('%a %d %b at %H:%M')} IST.")
    return when_utc


def explain(status: str, row: dict[str, Any] | None = None) -> str:
    """Operator-facing sentence for a row's status. Says what happened and, for
    the ones that did not run, what to do about it."""
    row = row or {}
    if status == PENDING:
        when = to_utc(row.get("scheduled_at"))
        return ("Scheduled for " + when.astimezone(IST).strftime("%a %d %b, %H:%M")
                + " IST") if when else "Scheduled"
    return {
        RUNNING: "Dialling now",
        "done": "Placed",
        "cancelled": "Cancelled before it ran",
        "missed": ("Missed — the scheduler reached it more than "
                   f"{int(STALE_AFTER.total_seconds() // 60)} minutes late, so it "
                   "was not dialled. Reschedule it if you still want the call."),
        "failed": (row.get("last_error")
                   or "Could not be placed. Nothing was dialled."),
    }.get(status, status)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _row(r) -> dict[str, Any]:
    d = dict(r)
    if d.get("audience"):
        try:
            d["audience"] = json.loads(d["audience"])
        except Exception:
            d["audience"] = {}
    if d.get("result"):
        try:
            d["result"] = json.loads(d["result"])
        except Exception:
            pass
    return d


def create(owner: str, *, kind: str, when: dt.datetime, label: str = "",
           contact_handle: str = "", phone: str = "",
           audience: dict | None = None, created_by: str = "",
           now: dt.datetime | None = None) -> dict[str, Any]:
    """Record one intent. Validates before writing — a stored schedule is one
    the executor is expected to be able to run."""
    if kind not in KINDS:
        raise ScheduleError(f"Unknown schedule kind {kind!r}.")
    if kind == "call" and not contact_handle:
        raise ScheduleError("Pick the contact to call.")
    when_utc = validate_time(when, now=now or dt.datetime.now(dt.timezone.utc))
    with get_db() as db:
        db.execute(
            "INSERT INTO scheduled_calls (owner, kind, label, contact_handle, "
            " phone, audience, scheduled_at, status, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (owner, kind, (label or "")[:200], contact_handle or None,
             phone or None,
             json.dumps(audience or {}, ensure_ascii=False) if kind == "campaign" else None,
             when_utc, PENDING, created_by or owner))
        row = db.execute(
            "SELECT * FROM scheduled_calls WHERE owner=? ORDER BY id DESC LIMIT 1",
            (owner,)).fetchone()
    return _row(row)


def upcoming(owner: str, *, limit: int = 100,
             include_done: bool = True) -> list[dict[str, Any]]:
    """This tenant's schedule. Pending first (soonest at the top), then what
    already happened (most recent first) — the order the page reads in."""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM scheduled_calls WHERE owner=? "
            + ("" if include_done else f"AND status IN ('{PENDING}','{RUNNING}') ")
            + "ORDER BY CASE WHEN status IN (?,?) THEN 0 ELSE 1 END, "
              "CASE WHEN status IN (?,?) THEN scheduled_at END ASC, "
              "scheduled_at DESC LIMIT ?",
            (owner, PENDING, RUNNING, PENDING, RUNNING, int(limit))).fetchall()
    return [_row(r) for r in rows]


def get(owner: str, sid: int) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM scheduled_calls WHERE id=? AND owner=?",
                         (int(sid), owner)).fetchone()
    return _row(row) if row else None


def cancel(owner: str, sid: int) -> bool:
    """Cancel a schedule that has not fired. Owner-scoped in the WHERE clause,
    and conditional on still being pending — cancelling a row the executor has
    already claimed would report a call as cancelled while it rings."""
    with get_db() as db:
        cur = db.execute(
            "UPDATE scheduled_calls SET status='cancelled', "
            "updated_at=now() WHERE id=? AND owner=? AND status=?",
            (int(sid), owner, PENDING))
        return bool(getattr(cur, "rowcount", 0))


def reschedule(owner: str, sid: int, when: dt.datetime,
               now: dt.datetime | None = None) -> dict[str, Any] | None:
    """Move a pending schedule. Same validation as create — a moved schedule is
    not exempt from calling hours."""
    when_utc = validate_time(when, now=now or dt.datetime.now(dt.timezone.utc))
    with get_db() as db:
        cur = db.execute(
            "UPDATE scheduled_calls SET scheduled_at=?, updated_at=now() "
            "WHERE id=? AND owner=? AND status=?",
            (when_utc, int(sid), owner, PENDING))
        if not getattr(cur, "rowcount", 0):
            return None
    return get(owner, sid)


def claim_due(now: dt.datetime, *, limit: int = 20) -> list[dict[str, Any]]:
    """Claim every row that should act on this tick, atomically.

    Returns rows already moved OUT of 'pending', so no second executor can pick
    them up. Rows that are due but stale or outside calling hours are settled
    here (to 'missed') and NOT returned — the caller only ever receives rows it
    should dial.
    """
    now_utc = now.astimezone(dt.timezone.utc)
    claimed: list[dict[str, Any]] = []
    with get_db() as db:
        due = db.execute(
            "SELECT * FROM scheduled_calls WHERE status=? AND scheduled_at<=? "
            "ORDER BY scheduled_at LIMIT ?",
            (PENDING, now_utc, int(limit))).fetchall()
        for r in due:
            row = _row(r)
            verdict = fire_decision(row, now=now_utc)
            if verdict == "wait":
                continue
            if verdict == "outside_hours":
                # Not settled: the window reopens and this row is still wanted,
                # unless it goes stale first — which the next tick decides.
                continue
            target = RUNNING if verdict == "fire" else "missed"
            cur = db.execute(
                "UPDATE scheduled_calls SET status=?, updated_at=now()"
                + (", fired_at=now()" if verdict == "fire" else
                   ", last_error=?")
                + " WHERE id=? AND status=?",
                ((target, row["id"], PENDING) if verdict == "fire"
                 else (target, explain("missed", row), row["id"], PENDING)))
            if not getattr(cur, "rowcount", 0):
                continue          # someone else claimed it
            if verdict == "fire":
                row["status"] = RUNNING
                claimed.append(row)
            else:
                log.info("schedule %s (%s) missed — due %s, now %s",
                         row["id"], row["owner"], row.get("scheduled_at"), now_utc)
    return claimed


def finish(sid: int, *, status: str, result: dict | None = None,
           error: str = "") -> None:
    """Settle a claimed row. Terminal — nothing re-opens it."""
    if status not in TERMINAL:
        raise ValueError(f"{status!r} is not a terminal schedule status")
    with get_db() as db:
        db.execute(
            "UPDATE scheduled_calls SET status=?, result=?, last_error=?, "
            "updated_at=now() WHERE id=?",
            (status, json.dumps(result or {}, ensure_ascii=False),
             (error or "")[:2000] or None, int(sid)))


def release_running(older_than_min: int = 30) -> int:
    """Fail rows left 'running' by a process that died mid-dial.

    A claim with no finisher would otherwise pin the row forever. They are
    failed rather than returned to pending, for the same reason a failed
    schedule is not retried: nobody knows whether the originate went out, and
    guessing wrong dials a real person twice.
    """
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=older_than_min)
    with get_db() as db:
        cur = db.execute(
            "UPDATE scheduled_calls SET status='failed', updated_at=now(), "
            "last_error='Interrupted while dialling — the scheduler restarted "
            "mid-call. Check Calls for what went out before rescheduling.' "
            "WHERE status=? AND updated_at < ?", (RUNNING, cutoff))
        return int(getattr(cur, "rowcount", 0) or 0)
