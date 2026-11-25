"""A registered DID must never be answered by another tenant's agent.

The leak this pins shut: `resolve_agent_slug` used to fall through from "this
DID has no agent bound" to `_resolve_active_agent_slug()`, which picked
whichever agent carried the global `is_active` flag — a flag EVERY tenant's
Activate button writes. With four tenants having activated agents, a customer
calling tenant A's number would have been greeted by tenant B's agent, using
B's company name, and the minutes would have billed to B.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (str(_REPO), str(_REPO / "calling-agent" / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture()
def bridge():
    """Imported INSIDE a fixture, not at module scope.

    Importing conversation_call_bridge at collection time pulls in the audio
    stack (VAD/onnxruntime), whose threads segfault the interpreter at
    shutdown — the file passed its assertions and then exited 139, which a
    deploy gate reads as a failure. The existing bridge test
    (test_direction_binding.py) imports the same way for the same reason.
    """
    import conversation_call_bridge as mod
    return mod


@pytest.fixture()
def fake_did(bridge, monkeypatch):
    """A registered DID whose agent bindings are empty."""
    class _Row(dict):
        def __getitem__(self, k):
            return dict.get(self, k)

    class _DB:
        def execute(self, sql, params=()):
            self._sql = sql
            return self

        def fetchone(self):
            if "FROM phone_numbers" in self._sql and "owner" in self._sql:
                return _Row(owner="tenant-a@example.test")
            if "FROM phone_numbers" in self._sql:
                return _Row(inbound_agent_slug=None, outbound_agent_slug=None)
            if "FROM agents WHERE is_active" in self._sql:
                return _Row(slug="other-tenants-agent")
            return None

    class _Ctx:
        def __enter__(self): return _DB()
        def __exit__(self, *a): return False

    monkeypatch.setattr("shared.db.database.get_db", lambda *a, **k: _Ctx())
    # Every slug "exists on disk" so the test cannot pass by accident.
    monkeypatch.setattr(bridge, "_agent_yaml_exists", lambda s: bool(s))


def test_registered_did_with_no_binding_returns_the_unrouted_sentinel(bridge, fake_did):
    assert bridge.resolve_agent_slug(did="912264231897",
                                     direction="inbound") == bridge.UNROUTED_AGENT


def test_it_does_not_borrow_the_globally_active_agent(bridge, fake_did):
    """The specific regression: never `other-tenants-agent`."""
    got = bridge.resolve_agent_slug(did="912264231897", direction="outbound")
    assert got != "other-tenants-agent"
    assert got == bridge.UNROUTED_AGENT


def test_the_unrouted_call_is_still_attributed_to_the_dids_owner(bridge, fake_did):
    """Quota and ledger must land on the number's tenant even with no agent."""
    assert bridge._did_owner("912264231897") == "tenant-a@example.test"


def test_the_legacy_global_fallback_is_scoped_to_the_founding_tenant(bridge, monkeypatch):
    """`is_active` is one global flag every tenant writes, so the last-resort
    lookup must filter by owner or it hands calls to whoever activated last."""
    seen = {}

    class _DB:
        def execute(self, sql, params=()):
            seen["sql"], seen["params"] = sql, params
            return self

        def fetchone(self):
            return None

    class _Ctx:
        def __enter__(self): return _DB()
        def __exit__(self, *a): return False

    monkeypatch.setattr("shared.db.database.get_db", lambda *a, **k: _Ctx())
    monkeypatch.setattr(bridge, "_agent_yaml_exists", lambda s: False)
    bridge._resolve_active_agent_slug()
    assert "owner=" in seen["sql"].replace(" ", "")
    assert seen["params"] and "@" in str(seen["params"][0])
