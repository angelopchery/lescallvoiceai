"""Per-direction agent binding: one number, two agents.

Covers the two halves of the change:
  * creating an agent yields a twin PAIR (<base>-inbound / <base>-outbound)
  * a call resolves the binding for ITS OWN direction, so an outbound leg can
    never silently answer with the inbound receptionist (or, worse, fall
    through to the legacy global agent — the original dead-air bug).
"""
import asyncio
import shutil
import sys

import pytest
import yaml

from conftest import AGENTS_DIR, REPO_ROOT


@pytest.fixture()
def builder(tmp_path, monkeypatch):
    monkeypatch.setenv("LESCALL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    dash = str(REPO_ROOT / "dashboard")
    if dash not in sys.path:
        sys.path.insert(0, dash)
    import agent_builder_api as mod
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True)
    monkeypatch.setattr(mod, "_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(mod, "_ctx_owner", lambda: "tenant@example.com")
    monkeypatch.setattr(mod, "_ctx_is_admin", lambda: False)
    monkeypatch.setattr(mod, "_claim_slug", lambda *a, **k: None)
    return mod, agents_dir


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _base_definition() -> dict:
    with open(AGENTS_DIR / "pramukh" / "agent.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_create_yields_an_inbound_and_an_outbound_agent(builder):
    mod, agents_dir = builder
    body = {**_base_definition(), "name": "acme", "call_direction": "outbound"}
    res = asyncio.run(mod.create_agent(_Req(body)))

    assert res["family"] == "acme"
    assert res["twins"] == {"inbound": "acme-inbound", "outbound": "acme-outbound"}
    # the requested direction is the one the editor opens on
    assert res["name"] == "acme-outbound"

    for direction, slug in res["twins"].items():
        with open(agents_dir / slug / "agent.yaml", encoding="utf-8") as fh:
            assert yaml.safe_load(fh)["call_direction"] == direction


def test_a_half_created_pair_is_never_left_behind(builder, monkeypatch):
    """A failure on the second twin must remove the first: a lone survivor
    could be bound to a number and run wrong-direction prompts."""
    mod, agents_dir = builder
    real = mod.write_agent_dir
    calls = {"n": 0}

    def flaky(name, spec, owner):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full")
        return real(name, spec, owner)

    monkeypatch.setattr(mod, "write_agent_dir", flaky)
    with pytest.raises(RuntimeError):
        asyncio.run(mod.create_agent(_Req({**_base_definition(), "name": "acme"})))
    assert not list(agents_dir.iterdir())


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("LESCALL_DB_URL", f"sqlite:///{tmp_path}/routing.db")
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import conversation_call_bridge as mod
    # Importing the bridge starts the registry hot-reload watcher (a daemon
    # thread meant to outlive a long-running service). Left running it aborts
    # the interpreter during pytest's shutdown — which fails the deploy gate
    # even with every test green. Stop it when this fixture tears down.
    from backend.app.agent_definition.registry import agent_registry
    agents_dir = tmp_path / "agents"
    for slug in ("acme-inbound", "acme-outbound"):
        (agents_dir / slug).mkdir(parents=True)
        shutil.copy(AGENTS_DIR / "pramukh" / "agent.yaml",
                    agents_dir / slug / "agent.yaml")
    monkeypatch.setattr(mod, "AGENTS_DIR", agents_dir)
    yield mod
    agent_registry.stop_watcher()


def _bind(number: str, inbound: str | None, outbound: str | None) -> None:
    from shared.db.database import get_db
    from shared.phone import significant_digits
    with get_db() as db:
        db.execute(
            "INSERT INTO phone_numbers (number, sig_digits, inbound_agent_slug, "
            "outbound_agent_slug, owner) VALUES (?,?,?,?,?)",
            (number, significant_digits(number), inbound, outbound, "t@example.com"))


DID = "+919999900001"


def test_each_direction_resolves_its_own_binding(bridge):
    _bind(DID, "acme-inbound", "acme-outbound")
    assert bridge.resolve_agent_slug(did=DID, direction="inbound") == "acme-inbound"
    assert bridge.resolve_agent_slug(did=DID, direction="outbound") == "acme-outbound"


def test_unbound_outbound_slot_falls_back_to_the_inbound_agent(bridge):
    """A tenant who never touched the new field keeps dialing as before —
    never dropping through to the legacy global agent."""
    _bind(DID, "acme-inbound", None)
    assert bridge.resolve_agent_slug(did=DID, direction="outbound") == "acme-inbound"


def test_inbound_never_borrows_the_outbound_agent(bridge):
    """The fallback is one-way: an inbound call to a number with no inbound
    agent must not be answered by the outbound sales script."""
    _bind(DID, None, "acme-outbound")
    assert bridge.resolve_agent_slug(did=DID, direction="inbound") != "acme-outbound"
