"""The trial floor: the limits the enforcement switch cannot turn off.

The whole point of this file is the FIRST test. Every cap in
shared/billing/entitlements.py used to begin `if not enforcement_enabled():
return Decision.ok()`, and that switch has been off since 2026-08-01 — so a
free account could mint agents without limit and drive the copilot as hard as
it liked, and the copilot's model quota is shared with live phone calls. If a
future change reinstates that short-circuit on the agent or copilot gates,
`test_free_plan_cap_binds_with_enforcement_off` is what fails.

Run with the calling-agent venv:
    cd calling-agent/backend && ../venv/bin/python -m pytest tests/test_trial_floor.py
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Same shared fixture the billing and intelligence suites use. It asserts the
# test URL differs from production before touching anything — LESCALL_DB_URL
# points at the LIVE database as a side effect of collection order, so a module
# that only checks "is this Postgres?" would happily run its INSERTs against
# real customer data.
from tests.test_intel_queue import pg_test_db  # noqa: F401,E402

FREE = "trialfloor-free@example.com"
PAID = "trialfloor-paid@example.com"


@pytest.fixture(autouse=True)
def _isolated(pg_test_db, monkeypatch):  # noqa: F811  (fixture, not a value)
    monkeypatch.setenv("LESCALL_DB_URL", pg_test_db)
    from shared.billing import entitlements as ent
    from shared.db.database import get_db

    # Enforcement OFF is the state this whole file is about — assert it rather
    # than assume it, so a test that silently ran with enforcement ON (and
    # therefore proved nothing about the floor) cannot pass.
    ent.set_enforcement(False, actor="test")
    assert ent.enforcement_enabled(refresh=True) is False

    with get_db() as db:
        for owner in (FREE, PAID):
            db.execute("DELETE FROM agents WHERE owner=?", (owner,))
            db.execute("DELETE FROM entitlements WHERE owner=?", (owner,))
            db.execute("DELETE FROM copilot_token_usage WHERE owner=?", (owner,))
            db.execute("DELETE FROM audit_events WHERE owner=?", (owner,))
        db.execute(
            "INSERT INTO entitlements (owner, plan_code, max_agent_families, "
            "copilot_turns_per_day, copilot_tokens_per_day) "
            "VALUES (?, 'growth', 25, 500, 8000000)", (PAID,))
    yield
    with get_db() as db:
        for owner in (FREE, PAID):
            db.execute("DELETE FROM agents WHERE owner=?", (owner,))
            db.execute("DELETE FROM entitlements WHERE owner=?", (owner,))
            db.execute("DELETE FROM copilot_token_usage WHERE owner=?", (owner,))
            db.execute("DELETE FROM audit_events WHERE owner=?", (owner,))


def _own(slug: str, owner: str = FREE) -> None:
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("INSERT INTO agents (slug, owner) VALUES (?,?) "
                   "ON CONFLICT (slug) DO UPDATE SET owner=excluded.owner",
                   (slug, owner))


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------

def test_free_plan_cap_binds_with_enforcement_off():
    """THE regression this file exists for."""
    from shared.billing.entitlements import check_agent_create

    assert check_agent_create(FREE, "first-one").allowed      # zero used
    _own("first-one-inbound")
    _own("first-one-outbound")

    decision = check_agent_create(FREE, "second-one")
    assert not decision.allowed
    assert decision.code == "cap_agents"
    assert decision.detail["agents_limit"] == 1
    assert decision.detail["trial"] is True


def test_a_twin_is_not_a_second_agent():
    """One create makes <base>-inbound AND <base>-outbound. The cap counts
    FAMILIES, so re-creating the other half of a pair already owned must not
    be refused — otherwise the free tier cannot finish making its one agent."""
    from shared.billing.entitlements import check_agent_create
    _own("first-one-inbound")
    assert check_agent_create(FREE, "first-one").allowed
    assert not check_agent_create(FREE, "other").allowed


def test_archiving_frees_the_slot():
    """The refusal tells the user to archive one. That has to actually work,
    or the message is a lie and the account is permanently stuck at one."""
    from shared.billing.entitlements import check_agent_create
    from shared.db.database import get_db
    _own("first-one-inbound")
    assert not check_agent_create(FREE, "second-one").allowed
    with get_db() as db:
        db.execute("UPDATE agents SET archived_at=now() WHERE owner=?", (FREE,))
    assert check_agent_create(FREE, "second-one").allowed


def test_paid_plans_are_untouched_while_enforcement_is_off():
    """The floor is anti-abuse for the free tier, not a back door that turns
    billing enforcement on for everyone."""
    from shared.billing.entitlements import check_agent_create
    for i in range(4):
        _own(f"paid-{i}-inbound", PAID)
    assert check_agent_create(PAID, "another").allowed


def test_an_account_with_no_entitlements_row_is_treated_as_free():
    """A signup from thirty seconds ago has no row at all. If that resolved to
    'no limits' the cap would be bypassable by being new."""
    from shared.billing.entitlements import effective_limits
    limits = effective_limits("never-seen-before@example.com")
    assert limits["trial"] is True
    assert limits["max_agent_families"] == 1


def test_the_floor_only_tightens():
    """An entitlement stricter than the floor wins; a looser one does not."""
    from shared.billing.entitlements import effective_limits
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("INSERT INTO entitlements (owner, plan_code, "
                   "max_agent_families, copilot_tokens_per_day) "
                   "VALUES (?, 'free', 9, 99000000)", (FREE,))
    limits = effective_limits(FREE)
    assert limits["max_agent_families"] == 1          # 9 is looser — floored
    assert limits["copilot_tokens_per_day"] == 150_000

    with get_db() as db:
        db.execute("UPDATE entitlements SET max_agent_families=0 WHERE owner=?",
                   (FREE,))
    assert effective_limits(FREE)["max_agent_families"] == 0   # stricter wins


# ---------------------------------------------------------------------------
# copilot tokens
# ---------------------------------------------------------------------------

def test_token_budget_binds_with_enforcement_off():
    from shared.billing.entitlements import (
        check_copilot_tokens, record_copilot_tokens)

    assert check_copilot_tokens(FREE, request_tokens=500).allowed
    record_copilot_tokens(FREE, prompt_tokens=149_000, completion_tokens=1_500)

    decision = check_copilot_tokens(FREE, request_tokens=500)
    assert not decision.allowed
    assert decision.code == "quota_copilot_tokens"
    assert decision.detail["copilot_tokens_limit"] == 150_000


def test_one_enormous_message_is_refused_before_it_is_sent():
    """The daily budget cannot stop a single huge paste — by the time the
    counter moves, the request has already cost the shared model quota."""
    from shared.billing.entitlements import check_copilot_tokens
    decision = check_copilot_tokens(FREE, request_tokens=400_000)
    assert not decision.allowed
    assert decision.code == "quota_copilot_tokens"
    assert decision.detail["copilot_request_limit"] == 25_000


def test_provider_total_is_trusted_over_the_visible_parts():
    """Gemini bills reasoning tokens at the output rate and folds them into
    `total_tokens` without showing them in `completion_tokens`. Summing the two
    visible numbers under-counts the expensive half of every request."""
    from shared.billing.entitlements import (
        copilot_tokens_used, record_copilot_tokens)
    record_copilot_tokens(FREE, prompt_tokens=1_000, completion_tokens=100,
                          total_tokens=5_000)
    assert copilot_tokens_used(FREE) == 5_000


def test_a_nonsense_total_cannot_undercount():
    """A provider reporting total < parts must not shrink the charge."""
    from shared.billing.entitlements import (
        copilot_tokens_used, record_copilot_tokens)
    record_copilot_tokens(FREE, prompt_tokens=4_000, completion_tokens=1_000,
                          total_tokens=7)
    assert copilot_tokens_used(FREE) == 5_000


def test_turn_cap_and_token_cap_are_both_checked():
    """They fail in opposite directions — a token budget alone permits 10,000
    one-word messages, a turn budget alone permits 20 enormous ones."""
    from shared.audit import record
    from shared.billing.entitlements import check_copilot_budget

    assert check_copilot_budget(FREE, request_tokens=100).allowed
    for _ in range(20):
        record(FREE, "copilot_turn", "chat")
    decision = check_copilot_budget(FREE, request_tokens=100)
    assert not decision.allowed
    assert decision.code == "quota_copilot"      # turns bite first, by design


def test_budget_is_per_ist_day():
    """Both caps reset at the same midnight. A yesterday row must not count."""
    from shared.billing.entitlements import copilot_tokens_used
    from shared.db.database import get_db
    yesterday = (dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30)))
                 - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    with get_db() as db:
        db.execute("INSERT INTO copilot_token_usage (owner, day, total_tokens) "
                   "VALUES (?,?,?)", (FREE, yesterday, 900_000))
    assert copilot_tokens_used(FREE) == 0


def test_paid_tenant_keeps_its_own_token_allowance():
    from shared.billing.entitlements import effective_limits
    limits = effective_limits(PAID)
    assert limits["trial"] is False
    assert limits["copilot_tokens_per_day"] == 8_000_000
    assert limits["copilot_max_request_tokens"] is None


def test_usage_summary_shows_the_enforced_numbers():
    """A meter that disagrees with the gate is how a limit looks like a bug."""
    from shared.billing.entitlements import record_copilot_tokens, usage_summary
    record_copilot_tokens(FREE, prompt_tokens=10_000, completion_tokens=2_000)
    summary = usage_summary(FREE)
    assert summary["trial"] is True
    assert summary["max_agent_families"] == 1
    assert summary["copilot_tokens_per_day"] == 150_000
    assert summary["copilot_tokens_used"] == 12_000
    assert summary["copilot_tokens_remaining"] == 138_000


def test_counters_never_raise_into_a_chat_turn():
    """A dropped count costs the platform tokens; a raised exception costs the
    user their reply after the model already ran."""
    from shared.billing import entitlements as ent

    def _boom(*a, **k):
        raise RuntimeError("postgres is having a day")

    import shared.db.database as database
    real = database.get_db
    database.get_db = _boom
    try:
        ent.record_copilot_tokens(FREE, prompt_tokens=1, completion_tokens=1)
        assert ent.copilot_tokens_used(FREE) == 0        # read degrades to 0
        assert ent.check_copilot_tokens(FREE, 10).allowed  # gate fails open
    finally:
        database.get_db = real
