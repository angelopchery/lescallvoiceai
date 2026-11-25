"""Scheduled calls and scheduled campaigns — the guardrails, not the plumbing.

The decisions are pure functions over (row, now), so these run without a clock,
a database or a phone. The storage tests use the lescall_test DB.

The one that matters most is staleness. lescall-api restarts on every deploy;
"fire everything that is due" after an outage is a burst of calls at whatever
hour the box came back, to people who were promised a specific time.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401  (fixtures)

from shared import scheduling
from shared.scheduling import IST, STALE_AFTER, ScheduleError

OWNER = "tech@dhronai.com"


def _at(y, m, d, hh, mm=0) -> dt.datetime:
    """A wall-clock IST instant — how an operator states a time."""
    return dt.datetime(y, m, d, hh, mm, tzinfo=IST)


def _row(when, status="pending", **kw):
    return {"id": 1, "owner": OWNER, "kind": "call", "status": status,
            "scheduled_at": when, **kw}


# ── fire_decision ───────────────────────────────────────────────────────────

def test_a_schedule_in_the_future_waits():
    now = _at(2026, 9, 9, 11, 0)          # Wed, inside hours
    assert scheduling.fire_decision(_row(_at(2026, 9, 9, 16, 0)), now=now) == "wait"


def test_a_schedule_that_is_due_fires():
    now = _at(2026, 9, 9, 16, 0)
    assert scheduling.fire_decision(_row(_at(2026, 9, 9, 16, 0)), now=now) == "fire"


def test_a_schedule_the_scheduler_reached_late_is_missed_not_fired():
    """The deploy case. A row due at 16:00 that the loop only reaches at 17:30
    must NOT ring — the customer was told 4pm and it is now half past five."""
    due = _at(2026, 9, 9, 16, 0)
    late = due + STALE_AFTER + dt.timedelta(minutes=1)
    assert scheduling.fire_decision(_row(due), now=late) == "missed"


def test_a_schedule_slightly_late_still_fires():
    """A restart, or a long campaign holding the loop, is normal. Inside the
    grace window the call is still the one that was promised."""
    due = _at(2026, 9, 9, 16, 0)
    assert scheduling.fire_decision(
        _row(due), now=due + STALE_AFTER - dt.timedelta(minutes=1)) == "fire"


def test_calling_hours_are_rechecked_at_fire_time():
    """Accepted for 18:55, reached at 19:05 — the window closed in between.
    Without this check the scheduler would be the only way to get an
    out-of-hours call out of this system."""
    due = _at(2026, 9, 9, 18, 55)
    assert scheduling.fire_decision(_row(due), now=_at(2026, 9, 9, 19, 5)) == "outside_hours"


def test_a_row_that_is_not_pending_is_never_fired():
    now = _at(2026, 9, 9, 16, 0)
    for status in ("running", "done", "failed", "cancelled", "missed"):
        assert scheduling.fire_decision(
            _row(_at(2026, 9, 9, 16, 0), status=status), now=now) == "wait", status


def test_a_row_with_no_time_is_missed_not_fired():
    assert scheduling.fire_decision(_row(None), now=_at(2026, 9, 9, 16, 0)) == "missed"


def test_a_timestamp_string_from_the_database_is_read_correctly():
    """The DB layer hands TIMESTAMPTZ back as STRINGS. Assuming datetimes is
    what silently skipped two autodial guardrails on every real row."""
    now = _at(2026, 9, 9, 16, 0)
    assert scheduling.fire_decision(
        _row("2026-09-09 10:30:00+00"), now=now) == "fire"      # 16:00 IST
    assert scheduling.fire_decision(
        _row("2026-09-09 12:30:00+00"), now=now) == "wait"      # 18:00 IST


# ── validate_time ───────────────────────────────────────────────────────────

def test_a_time_in_the_past_is_refused():
    now = _at(2026, 9, 9, 16, 0)
    with pytest.raises(ScheduleError, match="already passed"):
        scheduling.validate_time(_at(2026, 9, 9, 15, 0), now=now)


def test_a_time_outside_calling_hours_is_refused_at_creation():
    """Refused while the operator is looking at the form, not silently skipped
    at the moment it was supposed to ring."""
    now = _at(2026, 9, 9, 16, 0)
    with pytest.raises(ScheduleError, match="outside calling hours"):
        scheduling.validate_time(_at(2026, 9, 9, 21, 30), now=now)


def test_the_refusal_names_the_next_slot_that_would_work():
    now = _at(2026, 9, 9, 16, 0)
    with pytest.raises(ScheduleError) as exc:
        scheduling.validate_time(_at(2026, 9, 9, 21, 30), now=now)
    assert "next slot" in str(exc.value)


def test_a_sunday_is_refused():
    now = _at(2026, 9, 9, 16, 0)          # Wed
    sunday = _at(2026, 9, 13, 11, 0)
    assert sunday.weekday() == 6
    with pytest.raises(ScheduleError, match="outside calling hours"):
        scheduling.validate_time(sunday, now=now)


def test_a_time_beyond_the_horizon_is_refused():
    now = _at(2026, 9, 9, 16, 0)
    with pytest.raises(ScheduleError, match="days ahead"):
        scheduling.validate_time(now + dt.timedelta(days=200), now=now)


def test_a_good_time_comes_back_as_utc():
    now = _at(2026, 9, 9, 11, 0)
    out = scheduling.validate_time(_at(2026, 9, 9, 16, 0), now=now)
    assert out.tzinfo is dt.timezone.utc
    assert out == dt.datetime(2026, 9, 9, 10, 30, tzinfo=dt.timezone.utc)


# ── storage ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def clean_schedules():
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("DELETE FROM scheduled_calls WHERE owner=?", (OWNER,))
    yield
    with get_db() as db:
        db.execute("DELETE FROM scheduled_calls WHERE owner=?", (OWNER,))


def _soon() -> dt.datetime:
    """A valid slot: 11:00 IST on the next weekday from now."""
    from shared.intelligence import autodial
    probe = autodial.now_local().replace(hour=11, minute=0, second=0, microsecond=0)
    while probe <= autodial.now_local() or not autodial.within_window(probe):
        probe += dt.timedelta(days=1)
    return probe


def test_a_created_schedule_is_pending_and_readable(clean_schedules):
    row = scheduling.create(OWNER, kind="call", when=_soon(),
                            contact_handle="h1", phone="+919876543210")
    assert row["status"] == "pending"
    assert [r["id"] for r in scheduling.upcoming(OWNER)] == [row["id"]]


def test_a_call_schedule_needs_a_contact(clean_schedules):
    with pytest.raises(ScheduleError, match="Pick the contact"):
        scheduling.create(OWNER, kind="call", when=_soon())


def test_cancelling_a_pending_schedule_works_once(clean_schedules):
    row = scheduling.create(OWNER, kind="call", when=_soon(), contact_handle="h1")
    assert scheduling.cancel(OWNER, row["id"]) is True
    assert scheduling.cancel(OWNER, row["id"]) is False
    assert scheduling.get(OWNER, row["id"])["status"] == "cancelled"


def test_another_tenant_cannot_cancel_my_schedule(clean_schedules):
    row = scheduling.create(OWNER, kind="call", when=_soon(), contact_handle="h1")
    assert scheduling.cancel("someone@else.com", row["id"]) is False
    assert scheduling.get(OWNER, row["id"])["status"] == "pending"


def test_another_tenant_cannot_read_my_schedule(clean_schedules):
    row = scheduling.create(OWNER, kind="call", when=_soon(), contact_handle="h1")
    assert scheduling.get("someone@else.com", row["id"]) is None


def test_claiming_a_due_row_returns_it_exactly_once(clean_schedules):
    """The row is claimed with a conditional UPDATE, so a second executor gets
    nothing. Without it, two API workers would ring every scheduled contact
    twice."""
    row = scheduling.create(OWNER, kind="call", when=_soon(), contact_handle="h1")
    fire_at = scheduling.to_utc(_soon()) + dt.timedelta(seconds=1)
    first = [r["id"] for r in scheduling.claim_due(fire_at)]
    second = [r["id"] for r in scheduling.claim_due(fire_at)]
    assert row["id"] in first
    assert row["id"] not in second
    assert scheduling.get(OWNER, row["id"])["status"] == "running"


def test_claiming_settles_a_stale_row_as_missed_without_returning_it(clean_schedules):
    row = scheduling.create(OWNER, kind="call", when=_soon(), contact_handle="h1")
    much_later = scheduling.to_utc(_soon()) + STALE_AFTER + dt.timedelta(minutes=5)
    assert [r["id"] for r in scheduling.claim_due(much_later)] == []
    assert scheduling.get(OWNER, row["id"])["status"] == "missed"


def test_claiming_leaves_an_out_of_hours_row_pending(clean_schedules):
    """The window reopens; the row is still wanted until it goes stale."""
    row = scheduling.create(OWNER, kind="call", when=_soon(), contact_handle="h1")
    due = scheduling.to_utc(_soon())
    # 20:10 IST on the due date — past the window, inside the grace period is
    # impossible here, so assert only that it is not claimed and not settled.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scheduling.autodial, "within_window", lambda *a, **k: False)
        assert scheduling.claim_due(due) == []
    assert scheduling.get(OWNER, row["id"])["status"] == "pending"


def test_finishing_settles_the_row_terminally(clean_schedules):
    row = scheduling.create(OWNER, kind="call", when=_soon(), contact_handle="h1")
    scheduling.claim_due(scheduling.to_utc(_soon()) + dt.timedelta(seconds=1))
    scheduling.finish(row["id"], status="done", result={"phone": "+919876543210"})
    got = scheduling.get(OWNER, row["id"])
    assert got["status"] == "done"
    assert got["result"]["phone"] == "+919876543210"


def test_a_non_terminal_finish_is_a_programming_error(clean_schedules):
    with pytest.raises(ValueError):
        scheduling.finish(1, status="pending")


def test_a_row_stranded_mid_dial_is_failed_not_retried(clean_schedules):
    """Nobody knows whether the originate went out. Guessing wrong dials a real
    person twice, so it fails with an explanation instead of returning to the
    queue."""
    from shared.db.database import get_db
    row = scheduling.create(OWNER, kind="call", when=_soon(), contact_handle="h1")
    with get_db() as db:
        db.execute("UPDATE scheduled_calls SET status='running', "
                   "updated_at=now() - interval '2 hours' WHERE id=?", (row["id"],))
    assert scheduling.release_running() >= 1
    got = scheduling.get(OWNER, row["id"])
    assert got["status"] == "failed"
    assert "Interrupted" in (got["last_error"] or "")


def test_rescheduling_revalidates_the_new_time(clean_schedules):
    row = scheduling.create(OWNER, kind="call", when=_soon(), contact_handle="h1")
    with pytest.raises(ScheduleError):
        scheduling.reschedule(OWNER, row["id"], _soon().replace(hour=23))
    assert scheduling.get(OWNER, row["id"])["status"] == "pending"


def _valid_slot_after(when: dt.datetime) -> dt.datetime:
    """The next slot inside calling hours after `when`. `when + 1 day` is not
    one on a Saturday: it lands on Sunday, reschedule() refuses the TIME
    before it ever looks at the row's status, and the test below failed every
    Friday-to-Saturday for a reason that has nothing to do with what it
    checks."""
    from shared.intelligence import autodial
    probe = when + dt.timedelta(days=1)
    while not autodial.within_window(probe):
        probe += dt.timedelta(days=1)
    return probe


def test_a_settled_schedule_cannot_be_moved(clean_schedules):
    row = scheduling.create(OWNER, kind="call", when=_soon(), contact_handle="h1")
    scheduling.cancel(OWNER, row["id"])
    assert scheduling.reschedule(OWNER, row["id"],
                                 _valid_slot_after(_soon())) is None
