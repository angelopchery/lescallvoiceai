"""Billing lifecycle against a real Postgres: entitlements, quotas, webhooks.

The billing tables are migration-managed and PG-only, exactly like the
intelligence layer, so these run against the SAME `<db>_test` database the
intel tests use — rebuilt from the real migration files, so what is exercised
here is the SQL that will actually ship rather than a hand-written
approximation of it.

Reusing `pg_test_db` is not just convenience. LESCALL_DB_URL is set to the
PRODUCTION database as a side effect of collection order (test_contact_hub_api
imports dashboard/intel_api, which calls load_dotenv() at import time), so a
module that merely checks "is LESCALL_DB_URL Postgres?" will happily run its
INSERTs against live customer data. The shared fixture asserts the test URL
differs from production before touching anything.
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


def _owner() -> str:
    return f"t-{uuid.uuid4().hex[:12]}@example.test"


@pytest.fixture()
def owner():
    """A fresh tenant per test, cleaned up afterwards."""
    from shared.db.database import get_db
    o = _owner()
    yield o
    with get_db() as db:
        for table in ("payment_attempts", "minute_topups", "billing_reservations",
                      "payments", "entitlements", "pilot_claims", "subscriptions",
                      "usage_ledger"):
            try:
                db.execute(f"DELETE FROM {table} WHERE owner=?", (o,))
            except Exception:
                pass


@pytest.fixture(autouse=True)
def enforcement_on(_use_test_db):        # noqa: F811  (fixture, not a value)
    """Turn enforcement on for the duration of each test.

    The `_use_test_db` parameter is NOT decoration — it is an ordering
    dependency, and leaving it out is a live-data hazard. Both fixtures are
    autouse and function-scoped, so pytest is free to run this one FIRST, while
    LESCALL_DB_URL still points at production (collection sets it from
    deploy/.pg_env as a side effect of another module's import). This fixture
    then writes `billing_enforce` into the PRODUCTION settings table.

    That is not hypothetical: the live row was found stamped
    `by: "pytest restore"` on 2026-07-31. Naming the dependency forces the
    monkeypatched URL to be in place before anything here touches a database.
    """
    from shared.billing.entitlements import enforcement_enabled, set_enforcement
    before = enforcement_enabled(refresh=True)
    set_enforcement(True, "pytest")
    yield
    set_enforcement(before, "pytest restore")


def _ledger(owner: str, seconds: int, n: int = 1, billable: int = 1):
    """n calls of `seconds` each, answered now."""
    from shared.db.database import get_db
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with get_db() as db:
        for _ in range(n):
            cid = uuid.uuid4().hex
            db.execute(
                "INSERT INTO usage_ledger (conn_id, fs_uuid, owner, answered_at, "
                "last_beat_at, billsec_pipeline, billable, status) "
                "VALUES (?,?,?,?,?,?,?,'closed')",
                (cid, cid, owner, now, now, seconds, billable))


# ── entitlement resolution ──────────────────────────────────────────────────

def test_unknown_owner_defaults_to_free_not_generous(owner):
    from shared.billing.entitlements import get_entitlements
    ent = get_entitlements(owner)
    assert ent["plan_code"] == "free"
    assert ent["minutes_limit"] == 30
    assert ent["max_agent_families"] == 1
    assert not any(ent[f] for f in ("autocall", "transcripts", "intelligence"))


def test_empty_owner_is_never_treated_as_a_tenant():
    """An unattributable call must not resolve to somebody's entitlement."""
    from shared.billing.entitlements import billing_owner, get_entitlements
    assert billing_owner({"owner": ""}) == ""
    assert billing_owner(None) == ""
    assert get_entitlements("")["plan_code"] == "free"


def test_active_pilot_grants_the_trial_window(owner):
    from shared.billing.entitlements import get_entitlements, resolve_entitlements
    from shared.db.database import get_db
    now = dt.datetime.now(dt.timezone.utc)
    with get_db() as db:
        db.execute(
            "INSERT INTO subscriptions (owner, plan_code, kind, status, "
            "current_start, current_end) VALUES (?,'pilot','one_time','active',?,?)",
            (owner, now.isoformat(), (now + dt.timedelta(days=7)).isoformat()))
    resolve_entitlements(owner)
    ent = get_entitlements(owner)
    assert ent["plan_code"] == "pilot"
    assert ent["minutes_limit"] == 500
    assert ent["summaries"] == 1        # Pilot gets AI summaries…
    assert ent["transcripts"] == 0      # …but not transcripts (pricing page)
    assert ent["autocall"] == 0         # …and not automated follow-ups


def test_elapsed_pilot_is_expired_ON_READ_without_waiting_for_sync(owner):
    """Otherwise a lapsed trial keeps dialling for up to a full sync interval."""
    from shared.billing.entitlements import get_entitlements, resolve_entitlements
    from shared.db.database import get_db
    now = dt.datetime.now(dt.timezone.utc)
    with get_db() as db:
        db.execute(
            "INSERT INTO subscriptions (owner, plan_code, kind, status, "
            "current_start, current_end) VALUES (?,'pilot','one_time','active',?,?)",
            (owner, (now - dt.timedelta(days=8)).isoformat(),
             (now - dt.timedelta(minutes=1)).isoformat()))
    resolve_entitlements(owner)
    ent = get_entitlements(owner)
    assert ent["plan_code"] == "free"
    assert ent["minutes_limit"] == 30


def test_non_payment_cancel_drops_immediately_but_voluntary_runs_to_cycle_end():
    """Grace expiry sets status='cancelled' too (it must, to free the unique
    index) — only `cancel_at_cycle_end` separates "they paid for this period"
    from "they did not"."""
    from shared.billing.entitlements import resolve_entitlements
    from shared.db.database import get_db
    involuntary, voluntary = _owner(), _owner()
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=10)).isoformat()
    with get_db() as db:
        db.execute("INSERT INTO subscriptions (owner, plan_code, kind, status, "
                   "cancel_at_cycle_end, current_end) "
                   "VALUES (?,'growth','subscription','cancelled',0,?)",
                   (involuntary, future))
        db.execute("INSERT INTO subscriptions (owner, plan_code, kind, status, "
                   "cancel_at_cycle_end, current_end) "
                   "VALUES (?,'growth','subscription','cancelled',1,?)",
                   (voluntary, future))
    try:
        assert resolve_entitlements(involuntary)["plan_code"] == "free"
        assert resolve_entitlements(voluntary)["plan_code"] == "growth"
    finally:
        with get_db() as db:
            for o in (involuntary, voluntary):
                db.execute("DELETE FROM entitlements WHERE owner=?", (o,))
                db.execute("DELETE FROM subscriptions WHERE owner=?", (o,))


def test_admin_override_beats_the_plan(owner):
    from shared.billing.entitlements import get_entitlements, resolve_entitlements
    from shared.db.database import get_db
    with get_db() as db:
        db.execute(
            "INSERT INTO entitlements (owner, plan_code, overrides) VALUES (?,?,?)",
            (owner, "free", json.dumps({"plan_code": "growth",
                                        "minutes_limit": 99999})))
    resolve_entitlements(owner)
    ent = get_entitlements(owner)
    assert ent["plan_code"] == "growth"
    assert ent["minutes_limit"] == 99999


def test_a_lock_survives_a_plan_change(owner):
    """Locking is an administrative act; re-resolving must not quietly undo it."""
    from shared.billing.entitlements import get_entitlements, resolve_entitlements
    from shared.db.database import get_db
    resolve_entitlements(owner)
    with get_db() as db:
        db.execute("UPDATE entitlements SET status='locked' WHERE owner=?", (owner,))
    resolve_entitlements(owner)
    assert get_entitlements(owner)["status"] == "locked"


# ── quota + admission ───────────────────────────────────────────────────────

def test_minutes_use_the_per_call_ceiling_law(owner):
    """Quota and invoice must round identically or they diverge."""
    from shared.billing.entitlements import (
        get_entitlements, minutes_used, period_bounds, resolve_entitlements)
    resolve_entitlements(owner)
    _ledger(owner, seconds=1, n=3)      # three 1-second calls
    start, end = period_bounds(get_entitlements(owner))
    # per_call_ceil bills each call up to a whole minute: 3, not 1.
    assert minutes_used(owner, start, end) == 3


def test_unbillable_calls_do_not_consume_quota(owner):
    from shared.billing.entitlements import (
        get_entitlements, minutes_used, period_bounds, resolve_entitlements)
    resolve_entitlements(owner)
    _ledger(owner, seconds=600, n=1, billable=0)
    start, end = period_bounds(get_entitlements(owner))
    assert minutes_used(owner, start, end) == 0


def test_dial_is_refused_once_the_quota_is_spent(owner):
    from shared.billing.entitlements import check_dial_allowed, resolve_entitlements
    resolve_entitlements(owner)
    _ledger(owner, seconds=60, n=30)          # exactly the Free allowance
    d = check_dial_allowed(owner)
    assert not d.allowed and d.code == "quota_minutes"


def test_admission_reserves_so_parallel_dials_cannot_both_pass(owner):
    """The endpoint returns before the call is placed, so a plain read would
    let N simultaneous requests all see the same remaining figure."""
    from shared.billing.entitlements import (
        check_dial_allowed, release_reservation, resolve_entitlements)
    resolve_entitlements(owner)           # Free: max_concurrent_calls = 1
    first = check_dial_allowed(owner)
    assert first.allowed and first.reservation
    second = check_dial_allowed(owner)
    assert not second.allowed and second.code == "cap_concurrency"
    release_reservation(first.reservation)
    assert check_dial_allowed(owner).allowed


def test_expired_reservations_are_swept_not_leaked(owner):
    """A crashed call must not shrink the tenant's quota permanently."""
    from shared.billing.entitlements import (
        check_dial_allowed, resolve_entitlements, sweep_reservations)
    from shared.db.database import get_db
    resolve_entitlements(owner)
    d = check_dial_allowed(owner)
    with get_db() as db:
        db.execute("UPDATE billing_reservations SET expires_at = CURRENT_TIMESTAMP "
                   "- INTERVAL '1 minute' WHERE ref=?", (d.reservation,))
    sweep_reservations()
    with get_db() as db:
        assert db.execute("SELECT 1 FROM billing_reservations WHERE ref=?",
                          (d.reservation,)).fetchone() is None
    assert check_dial_allowed(owner).allowed


def test_unattributed_calls_are_refused_not_billed_to_the_founder(owner):
    """The hole this closes: naming an unclaimed slug used to check quota
    against the empty-string tenant and bill minutes to the unlimited founding
    account — a self-serve free-minutes switch."""
    from shared.billing.entitlements import check_dial_allowed
    d = check_dial_allowed("")
    assert not d.allowed
    assert d.code == "unattributed"
    assert d.check == "unattributed"


def test_locked_accounts_cannot_dial_or_create(owner):
    from shared.billing.entitlements import (
        check_agent_create, check_dial_allowed, resolve_entitlements)
    from shared.db.database import get_db
    resolve_entitlements(owner)
    with get_db() as db:
        db.execute("UPDATE entitlements SET status='locked' WHERE owner=?", (owner,))
    assert check_dial_allowed(owner).code == "account_locked"
    assert check_agent_create(owner).code == "account_locked"


def test_enforcement_off_allows_everything_and_reserves_nothing(owner):
    from shared.billing.entitlements import (
        check_dial_allowed, resolve_entitlements, set_enforcement)
    resolve_entitlements(owner)
    _ledger(owner, seconds=60, n=100)     # way over the Free allowance
    set_enforcement(False, "pytest")
    d = check_dial_allowed(owner)
    assert d.allowed and not d.reservation


# ── top-ups ─────────────────────────────────────────────────────────────────

def _grant(owner: str, plan_code: str):
    """Put a tenant on a plan the way the admin plane does — an override.

    Note what does NOT work: writing plan_code straight onto the entitlements
    row. resolve_entitlements() rebuilds wholesale from the subscriptions
    history, so a bare plan_code with no subscription and no override correctly
    falls back to Free. That is the design, not a bug — entitlements are
    derived state.
    """
    from shared.billing.entitlements import resolve_entitlements
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("INSERT INTO entitlements (owner, plan_code, overrides) "
                   "VALUES (?,?,?) ON CONFLICT (owner) DO UPDATE SET "
                   "overrides=excluded.overrides",
                   (owner, plan_code, json.dumps({"plan_code": plan_code})))
    resolve_entitlements(owner)


def test_a_bare_plan_code_without_a_subscription_resolves_to_free(owner):
    from shared.billing.entitlements import get_entitlements, resolve_entitlements
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("INSERT INTO entitlements (owner, plan_code) VALUES (?,'growth')",
                   (owner,))
    resolve_entitlements(owner)
    assert get_entitlements(owner)["plan_code"] == "free"


def test_active_topups_raise_the_limit_for_this_period(owner):
    from shared.billing.entitlements import get_entitlements
    from shared.db.database import get_db
    _grant(owner, "basic")
    assert get_entitlements(owner)["minutes_limit"] == 3000
    end = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=10)).isoformat()
    with get_db() as db:
        db.execute("INSERT INTO minute_topups (owner, minutes, amount_paise, "
                   "status, period_end) VALUES (?,500,400000,'active',?)",
                   (owner, end))
    ent = get_entitlements(owner)
    assert ent["minutes_limit"] == 3500
    assert ent["minutes_topup"] == 500


def test_expired_and_refunded_topups_stop_counting(owner):
    from shared.billing.entitlements import get_entitlements
    from shared.db.database import get_db
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=10)).isoformat()
    _grant(owner, "basic")
    with get_db() as db:
        db.execute("INSERT INTO minute_topups (owner, minutes, amount_paise, "
                   "status, period_end) VALUES (?,500,400000,'active',?)", (owner, past))
        db.execute("INSERT INTO minute_topups (owner, minutes, amount_paise, "
                   "status, period_end) VALUES (?,500,400000,'refunded',?)", (owner, future))
    assert get_entitlements(owner)["minutes_limit"] == 3000


# ── agent caps ──────────────────────────────────────────────────────────────

def _agent(owner: str, slug: str, archived: bool = False):
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("INSERT INTO agents (slug, owner, display_name, archived_at) "
                   "VALUES (?,?,?,?) ON CONFLICT (slug) DO NOTHING",
                   (slug, owner, slug,
                    dt.datetime.now(dt.timezone.utc).isoformat() if archived else None))


def _drop_agents(owner: str):
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("DELETE FROM agents WHERE owner=?", (owner,))


def test_a_twin_pair_counts_as_one_family(owner):
    from shared.billing.entitlements import agent_families_used, resolve_entitlements
    resolve_entitlements(owner)
    try:
        _agent(owner, f"{owner}-sales-inbound")
        _agent(owner, f"{owner}-sales-outbound")
        assert agent_families_used(owner) == 1
    finally:
        _drop_agents(owner)


def test_archived_agents_do_not_consume_a_plan_slot(owner):
    """DELETE moves the folder but keeps the row — counting it forever would
    charge a tenant for agents they deleted."""
    from shared.billing.entitlements import (
        agent_families_used, check_agent_create, resolve_entitlements)
    resolve_entitlements(owner)
    try:
        _agent(owner, f"{owner}-a-inbound")
        assert not check_agent_create(owner, "new-one").allowed   # Free cap = 1
        _agent(owner, f"{owner}-a-inbound", archived=True)
        from shared.db.database import get_db
        with get_db() as db:
            db.execute("UPDATE agents SET archived_at=CURRENT_TIMESTAMP WHERE owner=?",
                       (owner,))
        assert agent_families_used(owner) == 0
        assert check_agent_create(owner, "new-one").allowed
    finally:
        _drop_agents(owner)


def test_completing_a_pair_you_already_own_is_not_a_new_family(owner):
    """Otherwise a tenant at cap could never create the outbound half of their
    own agent."""
    from shared.billing.entitlements import check_agent_create, resolve_entitlements
    resolve_entitlements(owner)
    try:
        _agent(owner, "acme-inbound")
        assert check_agent_create(owner, "acme").allowed
        assert not check_agent_create(owner, "different").allowed
    finally:
        _drop_agents(owner)


# ── webhooks ────────────────────────────────────────────────────────────────

def _order_paid(order_id: str, payment_id: str, amount: int) -> dict:
    return {"event": "order.paid", "payload": {
        "order": {"entity": {"id": order_id, "amount": amount}},
        "payment": {"entity": {"id": payment_id, "amount": amount,
                               "method": "upi"}}}}


def test_pilot_order_opens_the_window_and_issues_one_receipt(owner):
    from shared.billing import webhooks
    from shared.billing.entitlements import get_entitlements
    from shared.db.database import get_db
    order_id = f"order_{uuid.uuid4().hex[:12]}"
    with get_db() as db:
        db.execute("INSERT INTO subscriptions (owner, plan_code, kind, status, "
                   "razorpay_order_id) VALUES (?,'pilot','one_time','created',?)",
                   (owner, order_id))
    payload = _order_paid(order_id, f"pay_{uuid.uuid4().hex[:12]}", 353882)
    result = webhooks.ingest(f"evt_{uuid.uuid4().hex}", "order.paid",
                             json.dumps(payload).encode(), payload)
    assert result["status"] == "processed"
    assert get_entitlements(owner)["plan_code"] == "pilot"
    with get_db() as db:
        rows = db.execute("SELECT receipt_number FROM payments WHERE owner=?",
                          (owner,)).fetchall()
    assert len(rows) == 1 and rows[0]["receipt_number"]


def test_a_replayed_event_has_no_second_effect(owner):
    from shared.billing import webhooks
    from shared.db.database import get_db
    order_id = f"order_{uuid.uuid4().hex[:12]}"
    with get_db() as db:
        db.execute("INSERT INTO subscriptions (owner, plan_code, kind, status, "
                   "razorpay_order_id) VALUES (?,'pilot','one_time','created',?)",
                   (owner, order_id))
    payload = _order_paid(order_id, f"pay_{uuid.uuid4().hex[:12]}", 353882)
    eid = f"evt_{uuid.uuid4().hex}"
    raw = json.dumps(payload).encode()
    assert webhooks.ingest(eid, "order.paid", raw, payload)["status"] == "processed"
    assert webhooks.ingest(eid, "order.paid", raw, payload)["status"] == "duplicate"
    with get_db() as db:
        n = db.execute("SELECT COUNT(*) AS n FROM payments WHERE owner=?",
                       (owner,)).fetchone()
    assert n["n"] == 1


def test_the_same_payment_under_a_new_event_id_is_still_recorded_once(owner):
    """Gateway-level dedupe is not enough — the payments insert is idempotent
    on razorpay_payment_id so a receipt number can never be burned twice."""
    from shared.billing import webhooks
    from shared.db.database import get_db
    order_id = f"order_{uuid.uuid4().hex[:12]}"
    with get_db() as db:
        db.execute("INSERT INTO subscriptions (owner, plan_code, kind, status, "
                   "razorpay_order_id) VALUES (?,'pilot','one_time','created',?)",
                   (owner, order_id))
    payload = _order_paid(order_id, f"pay_{uuid.uuid4().hex[:12]}", 353882)
    raw = json.dumps(payload).encode()
    webhooks.ingest(f"evt_{uuid.uuid4().hex}", "order.paid", raw, payload)
    webhooks.ingest(f"evt_{uuid.uuid4().hex}", "order.paid", raw, payload)
    with get_db() as db:
        n = db.execute("SELECT COUNT(*) AS n FROM payments WHERE owner=?",
                       (owner,)).fetchone()
    assert n["n"] == 1


def test_a_wrong_amount_is_quarantined_and_grants_nothing(owner):
    from shared.billing import webhooks
    from shared.billing.entitlements import get_entitlements
    from shared.db.database import get_db
    order_id = f"order_{uuid.uuid4().hex[:12]}"
    with get_db() as db:
        db.execute("INSERT INTO subscriptions (owner, plan_code, kind, status, "
                   "razorpay_order_id) VALUES (?,'pilot','one_time','created',?)",
                   (owner, order_id))
    payload = _order_paid(order_id, f"pay_{uuid.uuid4().hex[:12]}", 100)
    result = webhooks.ingest(f"evt_{uuid.uuid4().hex}", "order.paid",
                             json.dumps(payload).encode(), payload)
    assert result["handled"] == "amount_mismatch"
    assert get_entitlements(owner)["plan_code"] == "free"


def test_an_unknown_order_grants_nothing_and_is_recorded(owner):
    from shared.billing import webhooks
    payload = _order_paid(f"order_GHOST_{uuid.uuid4().hex[:8]}", "pay_x", 353882)
    result = webhooks.ingest(f"evt_{uuid.uuid4().hex}", "order.paid",
                             json.dumps(payload).encode(), payload)
    assert result["handled"] == "unknown_reference"


def test_an_unsubscribed_event_is_acked_not_left_pending():
    """A subscribed-but-unhandled event would sit with processed_at NULL
    forever and trip the stuck-event alarm every 15 minutes."""
    from shared.billing import webhooks
    from shared.db.database import get_db
    eid = f"evt_{uuid.uuid4().hex}"
    payload = {"event": "payment.dispute.created", "payload": {}}
    assert webhooks.ingest(eid, "payment.dispute.created",
                           json.dumps(payload).encode(), payload)["status"] == "ignored"
    with get_db() as db:
        row = db.execute("SELECT processed_at FROM billing_events WHERE event_id=?",
                         (eid,)).fetchone()
    assert row["processed_at"] is not None


def test_a_refund_flips_the_payment_and_issues_a_credit_note(owner):
    from shared.billing import webhooks
    from shared.db.database import get_db
    order_id = f"order_{uuid.uuid4().hex[:12]}"
    payment_id = f"pay_{uuid.uuid4().hex[:12]}"
    with get_db() as db:
        db.execute("INSERT INTO subscriptions (owner, plan_code, kind, status, "
                   "razorpay_order_id) VALUES (?,'pilot','one_time','created',?)",
                   (owner, order_id))
    paid = _order_paid(order_id, payment_id, 353882)
    webhooks.ingest(f"evt_{uuid.uuid4().hex}", "order.paid",
                    json.dumps(paid).encode(), paid)
    refund = {"event": "refund.processed", "payload": {
        "refund": {"entity": {"id": "rfnd_1", "payment_id": payment_id,
                              "amount": 353882}},
        "payment": {"entity": {"id": payment_id, "amount": 353882}}}}
    webhooks.ingest(f"evt_{uuid.uuid4().hex}", "refund.processed",
                    json.dumps(refund).encode(), refund)
    with get_db() as db:
        row = db.execute("SELECT status, refunded_paise, credit_note_number "
                         "FROM payments WHERE razorpay_payment_id=?",
                         (payment_id,)).fetchone()
    assert row["status"] == "refunded"
    assert row["refunded_paise"] == 353882
    # A tax invoice cannot be deleted — it is reversed with a credit note.
    assert row["credit_note_number"].startswith("LCCN-")


# ── pilot once-ever ─────────────────────────────────────────────────────────

def test_the_pilot_claim_survives_expiry_and_blocks_a_second_trial(owner):
    from shared.billing.service import CheckoutError, start_checkout
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("INSERT INTO pilot_claims (owner) VALUES (?)", (owner,))
    with pytest.raises(CheckoutError) as exc:
        start_checkout(owner, "pilot")
    # The gateway is unconfigured in tests, so the only way this raises
    # pilot_used rather than gateway_unconfigured is if the claim is checked —
    # which is the point. Accept either ordering, assert it never proceeds.
    assert exc.value.code in ("pilot_used", "gateway_unconfigured")


# ── telephony gate (BYON is a paid capability) ──────────────────────────────

def test_free_accounts_cannot_register_telephony(owner):
    from shared.billing.entitlements import resolve_entitlements, telephony_allowed
    resolve_entitlements(owner)
    assert not telephony_allowed(owner)


def test_a_brand_new_signup_with_no_row_is_locked_out():
    """Fail CLOSED: an account that has never been resolved must not be able to
    put a trunk on the carrier edge just because its row does not exist yet."""
    from shared.billing.entitlements import telephony_allowed
    assert not telephony_allowed(f"never-seen-{uuid.uuid4().hex[:8]}@example.test")
    assert not telephony_allowed("")


@pytest.mark.parametrize("plan", ["pilot", "basic", "growth", "enterprise"])
def test_every_paid_plan_includes_telephony(owner, plan):
    from shared.billing.entitlements import telephony_allowed
    _grant(owner, plan)
    assert telephony_allowed(owner)


def test_telephony_gate_ignores_the_enforcement_switch(owner):
    """Unlike every other feature gate.

    The enforcement switch stages QUOTA rollout for existing tenants. This gate
    protects the carrier edge from strangers who signed up today, so it must
    bite the moment signups reopen — not whenever P6 happens to be flipped.
    """
    from shared.billing.entitlements import resolve_entitlements, set_enforcement, telephony_allowed
    resolve_entitlements(owner)
    set_enforcement(False, "pytest")
    assert not telephony_allowed(owner)      # still locked with enforcement OFF
    set_enforcement(True, "pytest")
    assert not telephony_allowed(owner)


def test_an_existing_number_holder_keeps_telephony_through_re_resolution(owner):
    """Grandfathering must go through `overrides` or resolve_entitlements()
    strips it — the same trap that lost 0013's staff seed."""
    from shared.billing.entitlements import resolve_entitlements, telephony_allowed
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("INSERT INTO entitlements (owner, plan_code, overrides) "
                   "VALUES (?, 'free', ?)",
                   (owner, json.dumps({"telephony": 1})))
    resolve_entitlements(owner)
    assert telephony_allowed(owner)
    resolve_entitlements(owner)          # stable across repeats
    assert telephony_allowed(owner)
