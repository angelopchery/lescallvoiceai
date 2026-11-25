"""Follow-up auto-dialer guardrails.

This is the only code path that rings a real customer with no human in the
loop, so the brakes get tested harder than the feature. A previous auto-dial
scheduler was disabled precisely because it had none of these and would dial
any past-due row at any hour, forever.

Pure functions only — no DB, no clock, no phone.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from shared.intelligence import autodial, config  # noqa: E402

IST = autodial.IST


def at(day: int, hour: int, minute: int = 0) -> dt.datetime:
    """2026-07-20 is a Monday, so `day` maps 0=Mon … 6=Sun."""
    return dt.datetime(2026, 7, 20 + day, hour, minute, tzinfo=IST)


def row(**over):
    base = {
        "id": 1, "owner": "t@x.com", "attempts": 0, "last_attempt_at": None,
        "scheduled_at": at(0, 9),           # due well before any test `now`
        "contact_handle": "db42", "dial_phone": "+919999999999",
        "contact_name": "Asha", "dnd": False,
        # AI-picked time (the conservative default these tests assume):
        # grace + business window apply. auto_scheduled=0 = the CUSTOMER
        # stated the time — covered by its own tests below.
        "auto_scheduled": 1,
        # Every test below this line is about some OTHER brake, so the base row
        # is one a human already approved. The approval gate itself is pinned
        # in its own section at the bottom — and note that omitting this key
        # entirely still refuses the call, which is the behaviour that matters.
        "approval_state": "approved",
        "approval_expires_at": None,
    }
    base.update(over)
    return base


# ── calling window ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("hour,ok", [
    (5, False), (9, False),        # too early — 06:00 calls are complaints
    (10, True), (14, True), (18, True),
    (19, False), (23, False),      # window end is exclusive
])
def test_window_hours(hour, ok):
    assert autodial.within_window(at(0, hour)) is ok


def test_sunday_is_never_called():
    # Saturday works, Sunday does not — the default weekday set is Mon-Sat.
    assert autodial.within_window(at(5, 12)) is True     # Saturday
    assert autodial.within_window(at(6, 12)) is False    # Sunday


def test_next_window_open_rolls_to_a_valid_slot():
    nxt = autodial.next_window_open(at(6, 12))  # Sunday noon
    assert autodial.within_window(nxt)
    assert nxt.weekday() == 0  # Monday


def test_outside_window_is_transient_not_permanent():
    """An overnight backlog must still be callable in the morning: a row that
    fell due at 01:00 is skipped at 03:00, not abandoned."""
    r = row(scheduled_at=at(0, 1))
    assert autodial.blocked_reason(r, now=at(0, 3), placed_today=0) == "outside_window"
    assert "outside_window" not in autodial.PERMANENT
    # …and the same row dials once the window opens.
    assert autodial.blocked_reason(r, now=at(0, 11), placed_today=0) is None


# ── DND ─────────────────────────────────────────────────────────────────────

def test_dnd_beats_everything():
    """A customer who asked us to stop is never dialled, whatever else is true."""
    r = row(dnd=True, scheduled_at=at(0, 9))
    assert autodial.blocked_reason(r, now=at(0, 12), placed_today=0) == "dnd"
    assert "dnd" in autodial.PERMANENT


def test_dnd_reported_even_when_also_outside_window():
    r = row(dnd=True)
    assert autodial.blocked_reason(r, now=at(0, 3), placed_today=0) == "dnd"


# ── retry cap + gap ─────────────────────────────────────────────────────────

def test_retry_cap_stops_permanently():
    r = row(attempts=config.AUTODIAL_MAX_ATTEMPTS)
    assert autodial.blocked_reason(r, now=at(0, 12), placed_today=0) == "retry_cap"
    assert "retry_cap" in autodial.PERMANENT


def test_one_attempt_below_the_cap_still_dials():
    r = row(attempts=config.AUTODIAL_MAX_ATTEMPTS - 1)
    assert autodial.blocked_reason(r, now=at(0, 12), placed_today=0) is None


def test_retry_gap_blocks_a_rapid_second_attempt():
    r = row(attempts=1, last_attempt_at=at(0, 11, 55))
    assert autodial.blocked_reason(r, now=at(0, 12), placed_today=0) == "retry_gap"


def test_retry_gap_clears_after_the_configured_wait():
    last = at(0, 12) - dt.timedelta(minutes=config.AUTODIAL_RETRY_GAP_MIN + 1)
    r = row(attempts=1, last_attempt_at=last)
    assert autodial.blocked_reason(r, now=at(0, 12), placed_today=0) is None


# ── volume + timing ─────────────────────────────────────────────────────────

def test_daily_cap_is_a_hard_ceiling():
    r = row()
    assert autodial.blocked_reason(
        r, now=at(0, 12), placed_today=config.AUTODIAL_MAX_PER_DAY) == "daily_cap"
    assert autodial.blocked_reason(
        r, now=at(0, 12), placed_today=config.AUTODIAL_MAX_PER_DAY - 1) is None


def test_grace_period_lets_a_human_intervene():
    """An AI-picked time scheduled seconds ago is not dialled instantly."""
    sched = at(0, 12)
    r = row(scheduled_at=sched)
    assert autodial.blocked_reason(r, now=sched, placed_today=0) == "not_due"
    after = sched + dt.timedelta(minutes=config.AUTODIAL_GRACE_MIN + 1)
    assert autodial.blocked_reason(r, now=after, placed_today=0) is None


# ── customer-stated times (auto_scheduled=0) ────────────────────────────────

def test_customer_stated_time_skips_grace():
    """'Call me in 10 minutes' must fire AT its time — grace would make a
    10-minute promise ~15 minutes late."""
    sched = at(0, 12)
    r = row(scheduled_at=sched, auto_scheduled=0)
    assert autodial.blocked_reason(r, now=sched, placed_today=0) is None
    # …but a not-yet-due row still waits.
    early = sched - dt.timedelta(minutes=1)
    assert autodial.blocked_reason(r, now=early, placed_today=0) == "not_due"


def test_customer_stated_time_gets_courtesy_window():
    """The customer's own ask ('call me at 8 tonight' / Sunday) is honoured
    beyond the business window — but never at unsociable hours."""
    r = row(scheduled_at=at(0, 9), auto_scheduled=0)
    # 20:00 Monday: outside business window, inside courtesy window.
    assert autodial.blocked_reason(r, now=at(0, 20), placed_today=0) is None
    # Sunday noon: weekday gate does not apply to a customer-stated time.
    r_sun = row(scheduled_at=at(6, 11), auto_scheduled=0)
    assert autodial.blocked_reason(r_sun, now=at(6, 12), placed_today=0) is None
    # 22:00: beyond the courtesy window — still refused.
    assert autodial.blocked_reason(r, now=at(0, 22), placed_today=0) == "outside_window"
    # …and an AI-picked time keeps the conservative business window.
    r_ai = row(scheduled_at=at(0, 9), auto_scheduled=1)
    assert autodial.blocked_reason(r_ai, now=at(0, 20), placed_today=0) == "outside_window"


def test_stale_followups_are_abandoned_not_called():
    old = at(0, 12) - dt.timedelta(hours=config.AUTODIAL_MAX_AGE_H + 1)
    r = row(scheduled_at=old)
    assert autodial.blocked_reason(r, now=at(0, 12), placed_today=0) == "too_old"
    assert "too_old" in autodial.PERMANENT


def test_naive_scheduled_at_is_treated_as_local_not_utc():
    """A naive timestamp read back from PG must not shift by 5h30m and dial
    five and a half hours early."""
    r = row(scheduled_at=dt.datetime(2026, 7, 20, 11, 0))  # naive
    assert autodial.blocked_reason(r, now=at(0, 10), placed_today=0) == "not_due"


# ── timestamp coercion ──────────────────────────────────────────────────────
#
# This database layer returns TIMESTAMPTZ columns as STRINGS. The first version
# of blocked_reason() type-checked with isinstance(..., datetime), so on every
# real row both the not-due and too-old gates were skipped — a dry run showed
# it would have dialled follow-ups scheduled three weeks ahead, immediately.

@pytest.mark.parametrize("raw,expect_utc_hour", [
    ("2026-07-20 07:30:00+00", 7),        # the exact shape PG hands back
    ("2026-07-20T07:30:00+00:00", 7),
    ("2026-07-20T07:30:00Z", 7),
    ("2026-07-20 13:00:00+05:30", 7, ),   # same instant, IST offset
])
def test_as_dt_parses_db_timestamp_strings(raw, expect_utc_hour):
    got = autodial.as_dt(raw)
    assert got is not None and got.tzinfo is not None
    assert got.astimezone(dt.timezone.utc).hour == expect_utc_hour


def test_as_dt_treats_naive_strings_as_ist():
    got = autodial.as_dt("2026-07-20 11:00:00")
    assert got == dt.datetime(2026, 7, 20, 11, 0, tzinfo=IST)


@pytest.mark.parametrize("bad", [None, "", "not a date", 12345, []])
def test_as_dt_rejects_junk(bad):
    assert autodial.as_dt(bad) is None


def test_future_row_as_STRING_is_not_due():
    """The regression itself: a string-typed future timestamp must block."""
    future = (at(0, 12) + dt.timedelta(days=21)).isoformat()
    r = row(scheduled_at=future)
    assert autodial.blocked_reason(r, now=at(0, 12), placed_today=0) == "not_due"


def test_stale_row_as_STRING_is_too_old():
    old = (at(0, 12) - dt.timedelta(hours=config.AUTODIAL_MAX_AGE_H + 5)).isoformat()
    r = row(scheduled_at=old)
    assert autodial.blocked_reason(r, now=at(0, 12), placed_today=0) == "too_old"


def test_retry_gap_honours_string_last_attempt():
    r = row(attempts=1, last_attempt_at=at(0, 11, 55).isoformat())
    assert autodial.blocked_reason(r, now=at(0, 12), placed_today=0) == "retry_gap"


def test_unscheduled_row_is_never_dialled():
    """No scheduled_at means nothing decided WHEN — refuse, don't call now."""
    assert autodial.blocked_reason(row(scheduled_at=None),
                                   now=at(0, 12), placed_today=0) == "not_due"


# ── contact linkage ─────────────────────────────────────────────────────────

def test_no_dial_phone_is_not_dialable():
    # 0008 change: the requirement is a dialable NUMBER, not a live contacts
    # row — dial_phone falls back to calls.phone, so only its absence blocks.
    r = row(contact_handle=None, dial_phone=None)
    assert autodial.blocked_reason(r, now=at(0, 12), placed_today=0) == "no_contact"


# ── happy path + messaging ──────────────────────────────────────────────────

def test_a_clean_due_row_inside_the_window_dials():
    assert autodial.blocked_reason(row(), now=at(0, 12), placed_today=0) is None


@pytest.mark.parametrize("reason", [
    "dnd", "no_contact", "retry_cap", "too_old", "retry_gap",
    "daily_cap", "outside_window", "no_identity",
])
def test_every_reason_has_a_human_explanation(reason):
    text = autodial.explain(reason, at(0, 3))
    assert text and text != reason and len(text) > 12


def test_only_decision_yes_is_selected():
    """Guards the SQL: an 'unsure' decision is exactly the case a human must
    review, and must never trigger an automatic call."""
    assert "f.decision = 'yes'" in autodial._DUE_SQL
    assert "autodial_blocked_at IS NULL" in autodial._DUE_SQL


def test_batch_paces_the_backlog_rather_than_bursting():
    """The batch is per-tenant per-sweep. At the default poll this must work
    out to a gradual drain, not a burst: a 36-row backlog should take the
    better part of an hour, not a couple of minutes."""
    per_hour = (3600 / config.AUTODIAL_POLL_S) * config.AUTODIAL_BATCH
    assert per_hour <= 60, f"{per_hour}/hour is a burst, not a drain"
    assert config.AUTODIAL_BATCH >= 1


def test_daily_cap_still_bounds_a_multi_sweep_drain():
    """Pacing must not be the only ceiling — over a 9-hour window the sweeps
    alone would exceed the daily cap, so the cap has to bind."""
    window_hours = config.AUTODIAL_HOUR_END - config.AUTODIAL_HOUR_START
    reachable = (3600 / config.AUTODIAL_POLL_S) * config.AUTODIAL_BATCH * window_hours
    assert reachable > config.AUTODIAL_MAX_PER_DAY


def test_kill_switch_defaults_on_but_is_overridable():
    assert isinstance(config.AUTODIAL_ENABLED, bool)
    assert config.AUTODIAL_HOUR_START < config.AUTODIAL_HOUR_END
    assert config.AUTODIAL_MAX_ATTEMPTS >= 1
    assert 6 not in config.autodial_weekdays()  # Sunday off by default


# ── 0008: execution modes — auto-dialling is opt-in ─────────────────────────
# The 0007 executor dialled EVERY decision='yes' row; mode='auto' (default
# 'manual') restores the product intent. The filter lives in SQL, so pin the
# SQL itself; the guardrail deltas are pure and tested directly.

def test_due_sql_selects_only_opted_in_rows():
    assert "f.mode = 'auto'" in autodial._DUE_SQL
    assert "f.decision = 'yes'" in autodial._DUE_SQL


def test_due_sql_carries_outcome_loop_fields():
    for col in ("f.max_attempts", "f.next_attempt_at", "f.created_at",
                "last_contact_call_at", "commitment_text"):
        assert col in autodial._DUE_SQL


def test_superseded_when_a_newer_call_already_happened():
    r = row(created_at=at(0, 8), last_contact_call_at=at(0, 12))
    reason = autodial.blocked_reason(r, now=at(0, 14), placed_today=0)
    assert reason == "superseded"
    assert "superseded" in autodial.PERMANENT


def test_not_superseded_by_the_originating_call():
    # The call that CREATED the follow-up precedes it — must not supersede.
    r = row(created_at=at(0, 8), last_attempt_at=None,
            last_contact_call_at=at(0, 7, 30))
    assert autodial.blocked_reason(r, now=at(0, 14), placed_today=0) is None


def test_per_row_max_attempts_overrides_config():
    r = row(attempts=1, max_attempts=1)
    assert autodial.blocked_reason(r, now=at(0, 14), placed_today=0) == "retry_cap"
    # NULL max_attempts falls back to the platform default
    r2 = row(attempts=1, max_attempts=None)
    assert autodial.blocked_reason(r2, now=at(0, 14), placed_today=0) is None


def test_explicit_next_attempt_at_wins_over_implicit_gap():
    # last attempt long ago (implicit gap satisfied) but explicit backoff
    # still in the future → blocked
    r = row(attempts=1, last_attempt_at=at(0, 10),
            next_attempt_at=at(0, 15))
    assert autodial.blocked_reason(r, now=at(0, 14), placed_today=0) == "retry_gap"
    # explicit backoff elapsed → clear
    assert autodial.blocked_reason(r, now=at(0, 15, 30), placed_today=0) is None


def test_deleted_contact_no_longer_blocks_when_phone_resolves():
    """The 38-followups incident: users delete/recreate contacts routinely;
    a dangling contact_id with a dialable phone from the calls join must not
    be a permanent block."""
    r = row(contact_handle=None, dial_phone="+919876543210")
    assert autodial.blocked_reason(r, now=at(0, 14), placed_today=0) is None


def test_no_phone_anywhere_is_still_permanent():
    r = row(contact_handle=None, dial_phone=None)
    assert autodial.blocked_reason(r, now=at(0, 14), placed_today=0) == "no_contact"
    assert "no_contact" in autodial.PERMANENT


def test_explain_covers_superseded():
    text = autodial.explain("superseded")
    assert "newer call" in text


# ── human approval — the gate that outranks every other brake ───────────────
#
# 2026-07-29: the dialer called a customer two days after a 44-second call on
# which nobody promised anything. The AI had scored that call
# promised_by='agent_promised', confidence 0.9. Fifteen of the twenty rows
# armed that morning had no recorded commitment at all. The lesson these tests
# encode is that the model's verdict is not consent, and neither is a config
# default: only a stamped human approval is.

def test_a_perfect_row_is_still_refused_without_approval():
    """Due, in-window, in-budget, dialable, not DND — and still not called.

    This is the whole feature in one assertion: everything the old code looked
    at says "dial", and the answer is no.
    """
    r = row(approval_state="pending")
    assert autodial.blocked_reason(r, now=at(0, 14), placed_today=0) == "not_approved"


@pytest.mark.parametrize("state", ["pending", "declined", "", None, "APPROVED",
                                   "yes", "true", "auto"])
def test_only_the_exact_string_approved_unlocks_a_call(state):
    """Anything that is not exactly 'approved' means do not call.

    Includes the near-misses a future refactor might introduce ('APPROVED',
    'true'), because a fuzzy match here would be indistinguishable from having
    no gate at all.
    """
    r = row(approval_state=state)
    assert autodial.blocked_reason(r, now=at(0, 14), placed_today=0) == "not_approved"


def test_a_row_missing_the_approval_field_entirely_is_refused():
    """Fail-safe on absence. If a query forgets to SELECT approval_state, or a
    caller hands over a dict built before 0011, the row must read as
    unapproved rather than sail through on a missing key."""
    r = row()
    del r["approval_state"]
    assert autodial.blocked_reason(r, now=at(0, 14), placed_today=0) == "not_approved"


def test_approved_row_dials():
    r = row(approval_state="approved")
    assert autodial.blocked_reason(r, now=at(0, 14), placed_today=0) is None


def test_approval_expires():
    """Consent to call someone today is not consent to call them next week."""
    r = row(approval_state="approved", approval_expires_at=at(0, 13))
    assert autodial.blocked_reason(r, now=at(0, 12), placed_today=0) is None
    assert autodial.blocked_reason(
        r, now=at(0, 13), placed_today=0) == "approval_expired"
    assert autodial.blocked_reason(
        r, now=at(0, 14), placed_today=0) == "approval_expired"


def test_expired_approval_is_recoverable_not_permanent():
    """Re-approving must work: neither approval code may be PERMANENT, or the
    executor would stamp autodial_blocked_at and the row could never be
    rescued by the human it is waiting for."""
    assert "not_approved" not in autodial.PERMANENT
    assert "approval_expired" not in autodial.PERMANENT


def test_dnd_still_outranks_approval():
    """A customer's "stop calling me" beats an operator's click."""
    r = row(approval_state="approved", dnd=True)
    assert autodial.blocked_reason(r, now=at(0, 14), placed_today=0) == "dnd"


def test_approval_predicates_are_in_the_candidate_sql():
    """Defence in depth, asserted on the source.

    blocked_reason is the second line of defence; _DUE_SQL is the first. If a
    refactor drops the predicate from the query, the pure-function tests above
    would all still pass while production quietly went back to cold-calling.
    """
    sql = autodial._DUE_SQL
    assert "approval_state = 'approved'" in sql
    assert "approval_expires_at IS NULL OR f.approval_expires_at > now()" in sql


def test_explain_tells_the_user_what_to_do_about_it():
    assert "approval" in autodial.explain("not_approved").lower()
    assert "expired" in autodial.explain("approval_expired").lower()
