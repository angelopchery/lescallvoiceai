"""Stage 3 — the follow-up decision engine.

Deterministic-first, LLM-second, explainable always:

    1. A clarified in-call commitment with a concrete deadline wins outright
       (decision=yes, confidence 0.98, source in_call).
    2. Deterministic live-engine flags (callback_requested with a parseable
       time, whatsapp_promised) force YES.
    3. Otherwise the extraction stage's follow_up judgment decides
       (yes / no / unsure with the model's confidence + reason).

Every decision — including NO — is stored as a followups row so the dashboard
can always answer "why (not)?". Vague YES decisions get a concrete
scheduled_at from deterministic rules, flagged auto_scheduled so the UI shows
the time was AI-picked.

Idempotent: re-runs replace only rows with source='pipeline' for the call;
in-call and manual follow-ups are never touched.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from shared.db.database import get_db

log = logging.getLogger("intel.followup")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

_PRIORITY_DEFAULT = "medium"
_DEFAULT_HOUR = 11  # business-hours default when only a date is known


def _now() -> dt.datetime:
    return dt.datetime.now(IST)


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")
_TIME_WORDS = {"morning": (10, 0), "afternoon": (14, 0), "evening": (18, 0)}


def resolve_deadline(day: str, time_s: str, anchor: dt.datetime) -> str | None:
    """Deterministic 'Tomorrow' + 'evening' → concrete IST timestamp, anchored
    at the call time. Returns ISO string or None when unresolvable."""
    import re as _re

    day_l = (day or "").strip().lower()
    when = None
    if day_l == "today":
        when = anchor
    elif day_l == "tomorrow":
        when = anchor + dt.timedelta(days=1)
    elif day_l.startswith("day after"):
        when = anchor + dt.timedelta(days=2)
    elif day_l in _WEEKDAYS:
        delta = (_WEEKDAYS.index(day_l) - anchor.weekday()) % 7 or 7
        when = anchor + dt.timedelta(days=delta)
    if when is None:
        return None
    hour, minute = 11, 0
    t = (time_s or "").strip().lower()
    if t in _TIME_WORDS:
        hour, minute = _TIME_WORDS[t]
    else:
        m = _re.match(r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?|baje)?", t)
        if m and m.group(1):
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            suffix = (m.group(3) or "").replace(".", "")
            if suffix == "pm" and hour < 12:
                hour += 12
            elif suffix in ("baje", "") and hour < 9:
                hour += 12  # "5 baje" on a business call ≈ 17:00
    return when.replace(hour=min(hour, 23), minute=minute, second=0,
                        microsecond=0).isoformat()


def _parse_anchor(call: dict[str, Any]) -> dt.datetime:
    raw = (call.get("started_at") or "").replace("T", " ").replace("Z", "")
    try:
        return dt.datetime.fromisoformat(raw[:19]).replace(tzinfo=IST)
    except ValueError:
        return _now()


def ingest_in_call_commitments(call: dict[str, Any]) -> None:
    """The live engine's typed commitments (collected_data flags.commitments)
    become commitments rows with source='in_call', deadlines resolved
    deterministically. Idempotent: replaces only in_call rows for the call."""
    try:
        flags = (json.loads(call.get("collected_data") or "{}") or {}).get("flags", {}) or {}
    except Exception:
        return
    raw = flags.get("commitments") or []
    if not isinstance(raw, list):
        return
    anchor = _parse_anchor(call)
    # Relative offsets ("call me in 10 minutes") count from when the caller
    # said it — the end of the call, not its start.
    call_end = anchor + dt.timedelta(seconds=int(call.get("duration_s") or 0))
    with get_db() as db:
        db.execute(
            "DELETE FROM commitments WHERE call_id=? AND source='in_call'",
            (call["id"],),
        )
        for c in raw[:10]:
            if not isinstance(c, dict):
                continue
            day, tm = str(c.get("day") or ""), str(c.get("time") or "")
            offset_min = 0
            try:
                offset_min = max(0, min(int(c.get("offset_min") or 0), 7 * 24 * 60))
            except (TypeError, ValueError):
                offset_min = 0
            if offset_min:
                deadline = (call_end + dt.timedelta(minutes=offset_min)).isoformat()
            else:
                deadline = resolve_deadline(day, tm, anchor) if day else None
            db.execute(
                """INSERT INTO commitments
                       (call_id, owner, contact_id, made_by, kind, description,
                        quote, deadline_text, deadline_at, is_vague, clarified,
                        source, confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_call', 0.98)""",
                (call["id"], call.get("owner") or "", call.get("contact_id"),
                 c.get("made_by") or "customer", c.get("kind") or "callback",
                 str(c.get("description") or "callback requested")[:500],
                 str(c.get("quote") or "")[:500] or None,
                 (f"in {offset_min} minutes" if offset_min
                  else " ".join(x for x in (day, tm) if x) or None),
                 deadline, 1 if c.get("vague") else 0,
                 1 if c.get("clarified") else 0),
            )


def _auto_schedule(fu: dict[str, Any], commitments: list[dict[str, Any]],
                   call_end: dt.datetime | None = None) -> tuple[str | None, bool]:
    """Concrete scheduled_at (ISO) + whether it was auto-picked."""
    # Relative request ("call me in 10 minutes") beats a calendar guess —
    # the LLM extraction reports it as suggested_offset_minutes.
    try:
        offset_min = max(0, min(int(fu.get("suggested_offset_minutes") or 0),
                                7 * 24 * 60))
    except (TypeError, ValueError):
        offset_min = 0
    if offset_min:
        base = call_end or _now()
        return (base + dt.timedelta(minutes=offset_min)).isoformat(), False
    date_s, time_s = fu.get("suggested_date"), fu.get("suggested_time")
    if date_s:
        return f"{date_s} {time_s or f'{_DEFAULT_HOUR:02d}:00'}:00+05:30", not bool(time_s)
    # earliest concrete commitment deadline
    deadlines = sorted(c["deadline_at"] for c in commitments if c.get("deadline_at"))
    if deadlines:
        return deadlines[0], False
    # nothing concrete anywhere: business default +2 days
    when = (_now() + dt.timedelta(days=2)).replace(
        hour=_DEFAULT_HOUR, minute=0, second=0, microsecond=0)
    return when.isoformat(), True


def decide(call: dict[str, Any], judgment: dict[str, Any],
           commitments: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure decision function (unit-testable): returns the followups row dict."""
    judgment = judgment or {}
    flags: dict[str, Any] = {}
    try:
        flags = (json.loads(call.get("collected_data") or "{}") or {}).get("flags", {}) or {}
    except Exception:
        pass

    # DND outranks EVERYTHING — including a clarified commitment. A caller who
    # said "stop calling me" and also happened to utter a time never gets a
    # follow-up, whatever the extraction LLM concluded.
    if flags.get("dnd_requested"):
        return {
            "decision": "no", "confidence": 1.0,
            "explanation": "Customer asked not to be contacted again (DND).",
            "reason": (str(flags.get("dnd_quote") or ""))[:200] or
                      "Do-not-disturb request during the call",
            "followup_type": None, "promised_by": None,
            "scheduled_at": None, "auto_scheduled": 0,
            "priority": _PRIORITY_DEFAULT, "status": "archived",
            "source": "pipeline", "commitment_id": None,
        }

    clarified = [c for c in commitments
                 if c.get("source") == "in_call" and c.get("clarified")
                 and c.get("deadline_at")]
    open_kinds = {c.get("kind") for c in commitments}

    if clarified:
        best = sorted(clarified, key=lambda c: c["deadline_at"])[0]
        return {
            "decision": "yes", "confidence": 0.98,
            "explanation": "Commitment was clarified during the call itself.",
            "reason": best.get("description"),
            "followup_type": "callback" if best.get("kind") == "callback" else "other",
            "promised_by": ("customer_requested" if best.get("made_by") == "customer"
                            else "agent_promised"),
            "scheduled_at": best["deadline_at"], "auto_scheduled": 0,
            "priority": judgment.get("priority") or _PRIORITY_DEFAULT,
            "status": "scheduled", "source": "in_call",
            "commitment_id": best.get("id"),
        }

    call_end = _parse_anchor(call) + dt.timedelta(
        seconds=int(call.get("duration_s") or 0))

    if flags.get("callback_requested"):
        sched, auto = _auto_schedule(judgment, commitments, call_end)
        return {
            "decision": "yes", "confidence": 0.95,
            "explanation": "The live engine recorded an explicit callback request.",
            "reason": judgment.get("reason") or "Customer asked for a callback"
                      + (f" ({flags.get('callback_time')})" if flags.get("callback_time") else ""),
            "followup_type": "callback", "promised_by": "customer_requested",
            "scheduled_at": sched, "auto_scheduled": 1 if auto else 0,
            "priority": judgment.get("priority") or "high",
            "status": "scheduled", "source": "pipeline",
        }

    required = (judgment.get("required") or "unsure").lower()
    confidence = judgment.get("confidence")
    if required == "yes":
        sched, auto = _auto_schedule(judgment, commitments, call_end)
        return {
            "decision": "yes", "confidence": confidence,
            "explanation": judgment.get("reason") or "AI analysis found a pending obligation.",
            "reason": judgment.get("reason"),
            "followup_type": judgment.get("type")
                or ("whatsapp" in open_kinds and "send_info") or "callback",
            "promised_by": judgment.get("who_promised"),
            "scheduled_at": sched, "auto_scheduled": 1 if auto else 0,
            "priority": judgment.get("priority") or _PRIORITY_DEFAULT,
            "status": "scheduled", "source": "pipeline",
        }
    if required == "no":
        return {
            "decision": "no", "confidence": confidence,
            "explanation": judgment.get("reason") or "No pending obligation detected.",
            "reason": judgment.get("reason"),
            "followup_type": None, "promised_by": None,
            "scheduled_at": None, "auto_scheduled": 0,
            "priority": "low", "status": "archived", "source": "pipeline",
        }
    return {
        "decision": "unsure", "confidence": confidence,
        "explanation": judgment.get("reason")
                       or "Signals were too weak or conflicting to auto-decide.",
        "reason": judgment.get("reason"),
        "followup_type": judgment.get("type"), "promised_by": judgment.get("who_promised"),
        "scheduled_at": None, "auto_scheduled": 0,
        "priority": judgment.get("priority") or _PRIORITY_DEFAULT,
        "status": "pending", "source": "pipeline",
    }


def _autocall_entitled(owner: str) -> bool:
    """E8 — does this tenant's plan include automated follow-up calls?

    Fails OPEN: a billing outage must not silently downgrade every follow-up
    in the platform to manual, which would be an invisible loss of a paid
    feature. E3 re-checks at dial time, where refusing is cheap and visible.
    """
    if not owner:
        return False
    try:
        from shared.billing.entitlements import autocall_allowed
        return autocall_allowed(owner)
    except Exception as exc:
        log.warning("[followup] autocall entitlement check unavailable: %r", exc)
        return True


def _agent_auto_execute(agent_slug: str | None) -> bool:
    """Whether this agent's follow-ups are auto-dialled.

    Platform universal rule (2026-07-27): ON by default for every agent —
    a missing agent dir, missing file, or missing key all mean True, so the
    rule covers agents created later and calls whose slug has no YAML on
    disk. An agent opts OUT with follow_up.auto_execute: {enabled: false}.
    Mirrors the schema-side default in
    calling-agent/backend/app/agent_definition/schema.py (change both
    together). Every autodial guardrail (window, caps, DND, kill switch)
    still applies to each individual call.
    """
    if not agent_slug:
        return True
    try:
        from shared.intelligence.pipeline import _agent_yaml

        ay = _agent_yaml(agent_slug) or {}
        flag = ((ay.get("follow_up") or {}).get("auto_execute") or {}).get("enabled")
        return True if flag is None else bool(flag)
    except Exception:
        return True


def _complete_executed_followup(call: dict[str, Any]) -> None:
    """Outcome loop: when the analyzed call IS a follow-up's auto-dial,
    complete that follow-up.

    Join key is call_links.followup_id — stamped by the executor at dial
    time — matched by owner+digits with the dial preceding the call by at
    most 30 minutes. Completion additionally requires a non-agent transcript
    turn so a dead pickup can't complete anything. (A voicemail greeting DOES
    transcribe as a caller turn — that residual false-complete is accepted
    because the voicemail call's own analysis creates a successor follow-up.)
    """
    started = call.get("started_at")
    phone = call.get("phone") or ""
    owner = call.get("owner") or ""
    if not (started and phone and owner):
        return
    transcript = call.get("transcript") or ""
    caller_spoke = any(
        line.strip() and not line.strip().lower().startswith(("agent", "["))
        for line in transcript.splitlines()
    )
    if not caller_spoke:
        return
    try:
        from shared.phone import significant_digits

        sig = significant_digits(phone)
        with get_db() as db:
            link = db.execute(
                """SELECT followup_id FROM call_links
                    WHERE owner = ? AND sig_digits = ?
                      AND followup_id IS NOT NULL
                      AND dialed_at::timestamptz
                          BETWEEN ?::timestamptz - interval '30 minutes'
                              AND ?::timestamptz + interval '5 minutes'
                    ORDER BY dialed_at DESC LIMIT 1""",
                (owner, sig, started, started),
            ).fetchone()
            if not link:
                return
            fid = link["followup_id"]
            updated = db.execute(
                """UPDATE followups
                       SET status = 'completed', executed_call_id = ?,
                           execution_outcome = 'answered',
                           completed_at = now(), updated_at = now()
                     WHERE id = ? AND owner = ? AND deleted_at IS NULL
                       AND mode = 'auto'
                       AND status IN ('pending', 'scheduled', 'missed')
                 RETURNING id, contact_id""",
                (call["id"], fid, owner),
            ).fetchone()
        if updated:
            log.info("followup %s completed by auto-dialled call %s",
                     fid, call["id"])
            try:
                from shared.intelligence import timeline

                if updated["contact_id"]:
                    timeline.emit(updated["contact_id"], owner,
                                  "followup.completed",
                                  ref_table="followups", ref_id=fid,
                                  payload={"call_id": call["id"],
                                           "by": "autodial"})
            except Exception:
                pass
    except Exception:
        log.warning("followup completion hook failed for call %s",
                    call.get("id"), exc_info=True)


def _apply_dnd(call: dict[str, Any]) -> None:
    """Enforce an in-call 'stop calling me': disposition → rejected (the
    autodial guardrail that is never overridden), every open follow-up for
    the contact cancelled + permanently blocked, timeline + notification so
    a human sees it happened. Idempotent."""
    try:
        flags = (json.loads(call.get("collected_data") or "{}") or {}).get(
            "flags", {}) or {}
    except Exception:
        return
    if not flags.get("dnd_requested"):
        return
    owner = call.get("owner") or ""
    contact_id = call.get("contact_id")
    quote = str(flags.get("dnd_quote") or "")[:200]
    with get_db() as db:
        if contact_id:
            db.execute(
                "UPDATE contacts SET disposition='rejected', "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?", (contact_id,))
            cancelled = [dict(r) for r in db.execute(
                """UPDATE followups
                       SET status='cancelled', autodial_blocked_at=now(),
                           last_attempt_error='Customer asked not to be contacted (DND).',
                           updated_at=now()
                     WHERE contact_id=? AND owner=? AND deleted_at IS NULL
                       AND status IN ('pending','scheduled','missed')
                 RETURNING id""",
                (contact_id, owner),
            ).fetchall()]
        else:
            cancelled = []
    log.info("DND enforced for call %s (contact=%s): %d open follow-up(s) "
             "cancelled", call.get("id"), contact_id, len(cancelled))
    try:
        from shared.intelligence import notify, timeline

        if contact_id:
            timeline.emit(contact_id, owner, "contact.dnd",
                          ref_table="calls", ref_id=call.get("id"),
                          payload={"quote": quote,
                                   "cancelled_followups": len(cancelled)})
        notify._emit(
            owner, "contact.dnd", "warning",
            "Customer asked to stop calls",
            (f"{call.get('phone') or 'A customer'} asked not to be "
             f"contacted again — marked DND, {len(cancelled)} open "
             f"follow-up(s) cancelled. Quote: “{quote}”"),
            f"/intelligence/{call.get('call_key') or call.get('id')}",
            f"dnd:{call.get('id')}")
    except Exception:
        log.warning("DND notify/timeline failed for call %s", call.get("id"),
                    exc_info=True)


async def stage_followup(call: dict[str, Any]) -> None:
    call_id = call["id"]
    _complete_executed_followup(call)
    _apply_dnd(call)
    ingest_in_call_commitments(call)
    with get_db() as db:
        ci = db.execute(
            "SELECT stages, summary FROM call_intelligence WHERE call_id=?", (call_id,)
        ).fetchone()
        commitments = [dict(r) for r in db.execute(
            "SELECT * FROM commitments WHERE call_id=?", (call_id,)
        ).fetchall()]
    judgment = {}
    if ci:
        try:
            judgment = json.loads(ci["stages"] or "{}").get("_follow_up_judgment", {}) or {}
        except Exception:
            judgment = {}

    row = decide(call, judgment, commitments)
    with get_db() as db:
        # replace only pipeline/in_call rows for this call (idempotent re-run);
        # manual follow-ups are user data and never touched
        db.execute(
            "DELETE FROM followups WHERE call_id=? AND source IN ('pipeline','in_call')",
            (call_id,),
        )
        # mode: 'auto' only when the agent's YAML opts in AND the decision is
        # an actionable yes — a human toggle on the row can override later.
        # E8: and only when the tenant's PLAN includes automated follow-up
        # calls. Staging a row as 'auto' on a plan without the capability would
        # queue work the dialer must then refuse one row at a time; stamping it
        # 'manual' up front is the honest state. E3 remains the safety net for
        # rows staged before a downgrade.
        mode = ("auto" if row["decision"] == "yes"
                and _agent_auto_execute(call.get("agent_slug"))
                and _autocall_entitled(call.get("owner") or "") else "manual")
        db.execute(
            """INSERT INTO followups
                   (owner, call_id, contact_id, commitment_id, decision, confidence,
                    explanation, reason, summary, followup_type, promised_by,
                    scheduled_at, auto_scheduled, priority, status, source, mode,
                    audit)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (call.get("owner") or "", call_id, call.get("contact_id"),
             row.get("commitment_id"), row["decision"], row.get("confidence"),
             row.get("explanation"), row.get("reason"),
             (ci["summary"] if ci else None),
             row.get("followup_type"), row.get("promised_by"),
             row.get("scheduled_at"), row.get("auto_scheduled", 0),
             row.get("priority") or _PRIORITY_DEFAULT, row["status"], row["source"],
             mode,
             json.dumps([{"at": _now().isoformat(), "by": "pipeline",
                          "action": "decision", "detail": row["decision"]}])),
        )


def sweep_overdue() -> int:
    """Mark scheduled follow-ups whose time passed as missed, then fire
    followup.overdue automation events for each."""
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            # mode='auto' rows still inside their retry budget belong to the
            # executor — flipping them 'missed' an hour past schedule made
            # the UI disagree with reality (row shown missed, then dialled
            # anyway, then flipped back). They age into 'missed' only once
            # permanently blocked.
            """UPDATE followups
                   SET status='missed', updated_at=now()
                 WHERE status IN ('pending', 'scheduled')
                   AND scheduled_at IS NOT NULL
                   AND scheduled_at < now() - interval '1 hour'
                   AND deleted_at IS NULL
                   AND NOT (mode = 'auto' AND autodial_blocked_at IS NULL)
             RETURNING id, owner, call_id, priority, reason""",
        ).fetchall()]
    if rows:
        try:
            from shared.intelligence.automation import fire_overdue_events

            fire_overdue_events(rows)
        except Exception:  # events are best-effort; the status change is not
            pass
    return len(rows)
