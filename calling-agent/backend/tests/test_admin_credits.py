"""Admin-granted minute credits — the pool must DRAIN, not refill.

The one thing worth testing hard here is the reason `minutes_consumed` exists
at all. `get_entitlements()` recomputes `minutes_limit = included + topup` for
every billing period, so the naive implementation of "1000 minutes valid for 3
months" hands out 1000 minutes three times. Every other assertion in this file
is scaffolding around that one.

Runs against the same `<db>_test` Postgres the rest of the billing suite uses —
rebuilt from the real migration files, so 0025 is exercised as it will ship.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import uuid
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401,E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


@pytest.fixture()
def owner():
    from shared.db.database import get_db
    o = f"t-{uuid.uuid4().hex[:12]}@example.test"
    with get_db() as db:
        db.execute("INSERT INTO users (email, name) VALUES (?,?) "
                   "ON CONFLICT (email) DO NOTHING", (o, "credit test"))
    yield o
    with get_db() as db:
        for table in ("minute_topups", "usage_ledger", "entitlements", "users"):
            col = "email" if table == "users" else "owner"
            try:
                db.execute(f"DELETE FROM {table} WHERE {col}=?", (o,))
            except Exception:
                pass


def _plan(owner: str, plan_code: str):
    """Put the tenant on a plan the way the admin plane does — an override."""
    from shared.billing.entitlements import resolve_entitlements
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("INSERT INTO entitlements (owner, plan_code, overrides) "
                   "VALUES (?,?,?) ON CONFLICT (owner) DO UPDATE SET "
                   "overrides=excluded.overrides",
                   (owner, plan_code, json.dumps({"plan_code": plan_code})))
    resolve_entitlements(owner)


def _ledger(owner: str, minutes: int, when: dt.datetime):
    """One closed, billable call of `minutes` answered at `when`."""
    from shared.db.database import get_db
    cid = uuid.uuid4().hex
    with get_db() as db:
        db.execute(
            "INSERT INTO usage_ledger (conn_id, fs_uuid, owner, answered_at, "
            "last_beat_at, billsec_pipeline, billable, status) "
            "VALUES (?,?,?,?,?,?,1,'closed')",
            (cid, cid, owner, when.isoformat(), when.isoformat(), minutes * 60))


def _month_start(months_ago: int) -> dt.datetime:
    """Midnight IST on the 1st, `months_ago` calendar months back, in UTC."""
    now = dt.datetime.now(IST).replace(day=1, hour=0, minute=0, second=0,
                                       microsecond=0)
    for _ in range(months_ago):
        now = (now - dt.timedelta(days=1)).replace(day=1)
    return now.astimezone(dt.timezone.utc)


def _backdate(grant_id: int, months: int):
    """Pretend the grant was made `months` calendar months ago.

    Settlement only ever looks at CLOSED months, so a grant created during the
    test has nothing to settle — every multi-month assertion needs this.
    """
    from shared.billing.credits import add_months
    from shared.db.database import get_db
    start = _month_start(months) + dt.timedelta(days=2)
    with get_db() as db:
        row = db.execute("SELECT period_start, period_end FROM minute_topups "
                         "WHERE id=?", (grant_id,)).fetchone()
        span = (dt.datetime.fromisoformat(str(row["period_end"])).year * 12
                + dt.datetime.fromisoformat(str(row["period_end"])).month) - \
               (dt.datetime.fromisoformat(str(row["period_start"])).year * 12
                + dt.datetime.fromisoformat(str(row["period_start"])).month)
        db.execute("UPDATE minute_topups SET period_start=?, period_end=?, "
                   "settled_through=? WHERE id=?",
                   (start, add_months(start, span), start, grant_id))


# ── granting ────────────────────────────────────────────────────────────────

def test_a_grant_raises_the_allowance_immediately(owner):
    from shared.billing.credits import grant_minutes
    from shared.billing.entitlements import get_entitlements
    _plan(owner, "basic")
    assert get_entitlements(owner)["minutes_limit"] == 3000

    grant_minutes(owner, 500, 1, actor="pytest", note="goodwill")
    ent = get_entitlements(owner)
    assert ent["minutes_limit"] == 3500
    assert ent["minutes_topup"] == 500


def test_validity_is_calendar_months_with_the_day_clamped():
    """"3 months" must land on the same date, not 90 days later."""
    from shared.billing.credits import add_months
    jan31 = dt.datetime(2026, 1, 31, tzinfo=dt.timezone.utc)
    assert add_months(jan31, 1).date() == dt.date(2026, 2, 28)
    assert add_months(jan31, 12).date() == dt.date(2027, 1, 31)
    assert add_months(dt.datetime(2026, 12, 15, tzinfo=dt.timezone.utc), 2).date() \
        == dt.date(2027, 2, 15)


def test_grant_is_refused_for_an_account_that_does_not_exist():
    from shared.billing.credits import CreditError, grant_minutes
    with pytest.raises(CreditError, match="no account"):
        grant_minutes("nobody-at-all@example.test", 100, 1)


@pytest.mark.parametrize("minutes,months", [
    (0, 1), (-5, 1), (10_000_000, 1), (100, 0), (100, 61), ("x", 1), (100, "x"),
])
def test_absurd_grants_are_refused(owner, minutes, months):
    from shared.billing.credits import CreditError, grant_minutes
    with pytest.raises(CreditError):
        grant_minutes(owner, minutes, months)


def test_an_expired_grant_stops_counting(owner):
    from shared.billing.credits import grant_minutes
    from shared.billing.entitlements import get_entitlements
    from shared.db.database import get_db
    _plan(owner, "basic")
    g = grant_minutes(owner, 500, 1, actor="pytest")
    with get_db() as db:
        db.execute("UPDATE minute_topups SET period_end=? WHERE id=?",
                   ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)),
                    g["id"]))
    assert get_entitlements(owner)["minutes_limit"] == 3000


# ── the pool must drain ─────────────────────────────────────────────────────

def test_a_multi_month_grant_is_a_POOL_not_a_monthly_allowance(owner):
    """The defect this feature exists to avoid.

    1000 minutes for 3 months on a 30-minute plan. The tenant burns the lot in
    a month that then closes. Without settlement the next period recomputes
    `included + 1000` and hands the reward out all over again — 3000 minutes
    from a 1000-minute grant.
    """
    from shared.billing.credits import grant_minutes, settle_owner
    from shared.billing.entitlements import get_entitlements
    _plan(owner, "free")                                  # 30 included
    g = grant_minutes(owner, 1000, 3, actor="pytest")
    _backdate(g["id"], 2)

    # Last month: 30 on the plan + the whole 1000 reward.
    _ledger(owner, 1030, _month_start(1) + dt.timedelta(days=3))

    settle_owner(owner)
    ent = get_entitlements(owner)
    assert ent["minutes_topup"] == 0, "the pool refilled at the period rollover"
    assert ent["minutes_limit"] == 30


def test_settlement_only_charges_what_exceeded_the_plan(owner):
    """A tenant inside their own allowance spends none of the reward."""
    from shared.billing.credits import grant_minutes, list_grants, settle_owner
    _plan(owner, "free")                                  # 30 included
    g = grant_minutes(owner, 1000, 3, actor="pytest")
    _backdate(g["id"], 2)
    _ledger(owner, 25, _month_start(1) + dt.timedelta(days=3))   # under the plan

    settle_owner(owner)
    after = [x for x in list_grants(owner) if x["id"] == g["id"]][0]
    assert after["minutes_consumed"] == 0
    assert after["minutes_remaining"] == 1000


def test_settlement_charges_only_the_overspend(owner):
    from shared.billing.credits import grant_minutes, list_grants, settle_owner
    from shared.billing.entitlements import get_entitlements
    _plan(owner, "free")                                  # 30 included
    g = grant_minutes(owner, 1000, 3, actor="pytest")
    _backdate(g["id"], 2)
    _ledger(owner, 130, _month_start(1) + dt.timedelta(days=3))  # 100 over

    settle_owner(owner)
    after = [x for x in list_grants(owner) if x["id"] == g["id"]][0]
    assert after["minutes_consumed"] == 100
    assert after["minutes_remaining"] == 900
    assert after["status"] == "active"

    # The half-drained case is the one that pins the entitlement read: a grant
    # that is spent outright drops out of the sum anyway because settlement
    # marks it `spent`, so only a PARTIAL pool proves the limit is built from
    # what is LEFT rather than from what was granted.
    ent = get_entitlements(owner)
    assert ent["minutes_topup"] == 900, "the allowance re-granted the spent minutes"
    assert ent["minutes_limit"] == 930


def test_settlement_is_idempotent(owner):
    """The worker runs it every pass; a second pass must charge nothing more."""
    from shared.billing.credits import grant_minutes, list_grants, settle_owner
    _plan(owner, "free")
    g = grant_minutes(owner, 1000, 3, actor="pytest")
    _backdate(g["id"], 2)
    _ledger(owner, 130, _month_start(1) + dt.timedelta(days=3))

    first = settle_owner(owner)
    second = settle_owner(owner)
    third = settle_owner(owner)
    assert first["minutes_charged"] == 100
    assert second["minutes_charged"] == 0
    assert third["minutes_charged"] == 0
    assert [x for x in list_grants(owner) if x["id"] == g["id"]][0]["minutes_consumed"] == 100


def test_an_exhausted_grant_is_marked_spent(owner):
    from shared.billing.credits import grant_minutes, list_grants, settle_owner
    from shared.billing.entitlements import get_entitlements
    _plan(owner, "free")
    g = grant_minutes(owner, 100, 6, actor="pytest")
    _backdate(g["id"], 2)
    _ledger(owner, 500, _month_start(1) + dt.timedelta(days=3))   # blows through it

    settle_owner(owner)
    after = [x for x in list_grants(owner) if x["id"] == g["id"]][0]
    assert after["status"] == "spent"
    assert after["minutes_remaining"] == 0
    assert get_entitlements(owner)["minutes_topup"] == 0


def test_the_live_period_is_never_settled(owner):
    """Settling the open month would double-count: this month's overspend is
    already bounded by `included + remaining`."""
    from shared.billing.credits import grant_minutes, list_grants, settle_owner
    _plan(owner, "free")
    g = grant_minutes(owner, 1000, 3, actor="pytest")
    _backdate(g["id"], 2)
    _ledger(owner, 500, dt.datetime.now(dt.timezone.utc))          # THIS month

    settle_owner(owner)
    assert [x for x in list_grants(owner) if x["id"] == g["id"]][0]["minutes_consumed"] == 0


def test_two_grants_drain_oldest_first(owner):
    from shared.billing.credits import grant_minutes, list_grants, settle_owner
    _plan(owner, "free")
    old = grant_minutes(owner, 100, 6, actor="pytest", note="first")
    new = grant_minutes(owner, 900, 6, actor="pytest", note="second")
    _backdate(old["id"], 2)
    _backdate(new["id"], 2)
    _ledger(owner, 180, _month_start(1) + dt.timedelta(days=3))    # 150 over

    settle_owner(owner)
    grants = {x["id"]: x for x in list_grants(owner)}
    assert grants[old["id"]]["minutes_consumed"] == 100            # drained first
    assert grants[new["id"]]["minutes_consumed"] == 50


def test_an_unlimited_plan_settles_nothing(owner):
    """Enterprise has no ceiling, so no call can ever exceed the plan."""
    from shared.billing.credits import grant_minutes, list_grants, settle_owner
    _plan(owner, "enterprise")
    g = grant_minutes(owner, 500, 6, actor="pytest")
    _backdate(g["id"], 2)
    _ledger(owner, 9000, _month_start(1) + dt.timedelta(days=3))

    settle_owner(owner)
    assert [x for x in list_grants(owner) if x["id"] == g["id"]][0]["minutes_consumed"] == 0


# ── revoking ────────────────────────────────────────────────────────────────

def test_revoke_removes_the_unused_minutes_and_keeps_the_record(owner):
    from shared.billing.credits import grant_minutes, list_grants, revoke_grant
    from shared.billing.entitlements import get_entitlements
    _plan(owner, "basic")
    g = grant_minutes(owner, 500, 3, actor="pytest", note="goodwill")
    assert get_entitlements(owner)["minutes_limit"] == 3500

    revoke_grant(g["id"], actor="pytest", reason="granted to the wrong account")
    assert get_entitlements(owner)["minutes_limit"] == 3000

    history = list_grants(owner)
    assert len(history) == 1 and history[0]["status"] == "revoked"
    assert "granted to the wrong account" in (history[0]["note"] or "")


def test_a_revoked_grant_cannot_be_revoked_twice(owner):
    from shared.billing.credits import CreditError, grant_minutes, revoke_grant
    _plan(owner, "basic")
    g = grant_minutes(owner, 500, 1, actor="pytest")
    revoke_grant(g["id"], actor="pytest", reason="first")
    with pytest.raises(CreditError, match="already revoked"):
        revoke_grant(g["id"], actor="pytest", reason="second")


def test_revoke_refuses_an_id_that_is_not_a_credit(owner):
    """A purchased top-up is not the admin plane's to withdraw — refunding it
    is a gateway operation with an invoice attached."""
    from shared.billing.credits import CreditError, revoke_grant
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute(
            "INSERT INTO minute_topups (owner, minutes, amount_paise, status, source) "
            "VALUES (?,500,400000,'active','razorpay') RETURNING id", (owner,)).fetchone()
    with pytest.raises(CreditError, match="no such credit grant"):
        revoke_grant(int(row["id"]))


# ── the worker wiring ───────────────────────────────────────────────────────

def test_the_sync_pass_settles_every_owner_holding_a_grant(owner):
    """If this pass is not wired in, a multi-month grant silently refills."""
    from shared.billing.credits import grant_minutes, list_grants
    from shared.billing.sync import pass_credit_settlement
    _plan(owner, "free")
    g = grant_minutes(owner, 1000, 3, actor="pytest")
    _backdate(g["id"], 2)
    _ledger(owner, 130, _month_start(1) + dt.timedelta(days=3))

    stats = pass_credit_settlement()
    assert stats["credit_minutes_charged"] >= 100
    assert [x for x in list_grants(owner) if x["id"] == g["id"]][0]["minutes_consumed"] == 100


def test_purchased_topups_are_untouched_by_settlement(owner):
    """A paid top-up expires with its period and has no pool to drain — the
    settlement pass must not find it, let alone charge it."""
    from shared.billing.credits import settle_all
    from shared.billing.entitlements import get_entitlements
    from shared.db.database import get_db
    _plan(owner, "basic")
    end = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=10))
    with get_db() as db:
        db.execute("INSERT INTO minute_topups (owner, minutes, amount_paise, status, "
                   "source, period_end) VALUES (?,500,400000,'active','razorpay',?)",
                   (owner, end))
    settle_all()
    with get_db() as db:
        row = db.execute("SELECT minutes_consumed, status FROM minute_topups "
                         "WHERE owner=?", (owner,)).fetchone()
    assert row["minutes_consumed"] == 0 and row["status"] == "active"
    assert get_entitlements(owner)["minutes_limit"] == 3500
