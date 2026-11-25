"""'Try your agent' must be a simulated CALL, not a chat box.

Four defects shipped together here and none had a test, which is why the
surface stayed broken in production:

1. The endpoint iterated `ConversationRouter.process()`. That is a coroutine
   returning one EngineResult — `process_stream()` is the async generator the
   pipeline drives. Every single turn 502'd with "'async for' requires an
   object with __aiter__ method, got coroutine".
2. The agent's opening was generated and discarded, so the simulation opened
   with the CALLER talking. On a real call the agent always speaks first, and
   the second beat (call_flow.connect.intro_line, or the composed identity
   line) only fires in answer to the caller's first word — neither could be
   observed here.
3. History was replayed through a freshly built router on every request AND
   the UI's copy of each past reply was injected into memory on top of the
   reply the replay had just generated — every past agent line appeared
   twice in the LLM's context.
4. Every agent, inbound included, was given contact_name="Test Caller", which
   prefills customer_name — so an inbound flow under test never asked for the
   caller's name, hiding exactly the bug the test call exists to find.
"""

from __future__ import annotations

import asyncio
import inspect
import shutil
import sys
from types import SimpleNamespace

import pytest

from conftest import AGENTS_DIR, REPO_ROOT


def _run(coro):
    return asyncio.run(coro)


class _Req:
    """The two things these endpoints touch on a Request."""

    def __init__(self, body: dict | None = None, owner=None):
        self._body = body if body is not None else {}
        self.state = SimpleNamespace(owner=owner)

    async def json(self):
        return self._body


@pytest.fixture()
def cop(tmp_path, monkeypatch):
    monkeypatch.setenv("LESCALL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    dash = str(REPO_ROOT / "dashboard")
    if dash not in sys.path:
        sys.path.insert(0, dash)
    import agent_builder_api as builder
    import agent_copilot_api as cop_mod

    agents_dir = tmp_path / "agents"
    (agents_dir / "pramukh").mkdir(parents=True)
    shutil.copy(AGENTS_DIR / "pramukh" / "agent.yaml",
                agents_dir / "pramukh" / "agent.yaml")
    # A second agent whose opening is the bare "Hello" the platform doctrine
    # asks for — that is the shape where beat two is a DIFFERENT line (the
    # connect intro) rather than a replay of a long opening.
    import yaml as _yaml
    defn = _yaml.safe_load((agents_dir / "pramukh" / "agent.yaml").read_text())
    defn["agent_id"] = "barehello-v1"
    defn["response_bank"]["opening_greeting"] = "Hello"
    (agents_dir / "barehello").mkdir(parents=True)
    (agents_dir / "barehello" / "agent.yaml").write_text(_yaml.safe_dump(defn))
    monkeypatch.setattr(builder, "_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(cop_mod, "_AGENTS_DIR", agents_dir)
    # Budget accounting is not what these tests are about, and the real gate
    # wants a billing DB.
    monkeypatch.setattr(cop_mod, "_billing_llm_gate", lambda *a, **k: None)
    from app.agent_definition.registry import agent_registry
    agent_registry.invalidate_all()
    cop_mod._TRY_SIMS.clear()
    yield cop_mod
    cop_mod._TRY_SIMS.clear()


# ---------------------------------------------------------------------------
# The crash itself
# ---------------------------------------------------------------------------

def test_process_is_a_coroutine_and_process_stream_is_the_generator():
    """The engine contract the endpoint has to honour. If this ever flips,
    the endpoint's `async for` target must flip with it."""
    from app.voice.conversation import ConversationRouter
    assert inspect.iscoroutinefunction(ConversationRouter.process)
    assert not inspect.isasyncgenfunction(ConversationRouter.process)
    assert inspect.isasyncgenfunction(ConversationRouter.process_stream)


def test_a_turn_completes_without_the_async_for_type_error(cop, stub_llm):
    start = _run(cop.copilot_try_start("pramukh", _Req()))
    out = _run(cop.copilot_try_agent(
        "pramukh", _Req({"sim_id": start["sim_id"],
                         "message": "I'm looking for a 3BHK in Ahmedabad"})))
    assert out["reply"].strip(), "the engine turn produced no reply"
    assert out["turn"] == 1


# ---------------------------------------------------------------------------
# The agent speaks first
# ---------------------------------------------------------------------------

def test_start_returns_the_agents_own_opening_line(cop):
    """Beat one: the greeting the caller hears before saying anything."""
    from app.agent_definition.registry import agent_registry
    agent = agent_registry.get("pramukh", cop._AGENTS_DIR)

    out = _run(cop.copilot_try_start("pramukh", _Req()))
    assert out["sim_id"].startswith("sim_")
    assert out["reply"].strip()
    assert out["turn"] == 0 and out["ended"] is False
    # It is the CONFIGURED opening, not an LLM invention.
    assert out["reply"] in (agent.response_bank.opening_greeting or ""), \
        "opening must come from response_bank.opening_greeting"


def test_second_beat_is_the_connect_intro_after_a_bare_hello(cop, stub_llm):
    """Beat two: when the opening is a bare "Hello" the caller's own hello is
    answered instantly with the identity intro (the connect fast path), not
    with an LLM turn. Only observable because beat one now really happens."""
    start = _run(cop.copilot_try_start("barehello", _Req()))
    assert start["reply"] == "Hello"
    out = _run(cop.copilot_try_agent(
        "barehello", _Req({"sim_id": start["sim_id"], "message": "hello"})))
    assert [p["intent"] for p in out["parts"]] == ["connect_intro"]
    assert out["reply"] and out["reply"] != start["reply"]


@pytest.mark.parametrize("slug", ["pramukh", "barehello"])
def test_simulation_opens_exactly_as_the_resolver_predicts(cop, stub_llm, slug):
    """The dashboard tells the owner what their call opens with by driving
    the engine through resolve_spoken_surfaces(). The simulation must speak
    those very two lines — if the two ever diverge, one of them is lying."""
    from app.agent_definition.registry import agent_registry
    from app.agent_definition.spoken_surfaces import resolve_spoken_surfaces
    agent = agent_registry.get(slug, cop._AGENTS_DIR)
    resolved = resolve_spoken_surfaces(agent)
    expect = resolved["openings"][resolved["default_language"]]

    start = _run(cop.copilot_try_start(slug, _Req()))
    second = _run(cop.copilot_try_agent(
        slug, _Req({"sim_id": start["sim_id"], "message": "hello"})))
    assert start["reply"] == expect["line"]
    assert second["reply"] == expect["second_line"]


# ---------------------------------------------------------------------------
# One router for the whole call
# ---------------------------------------------------------------------------

def test_the_simulation_keeps_one_router_across_turns(cop, stub_llm):
    start = _run(cop.copilot_try_start("pramukh", _Req()))
    sim_id = start["sim_id"]
    router_a = cop._TRY_SIMS[sim_id]["router"]
    _run(cop.copilot_try_agent(
        "pramukh", _Req({"sim_id": sim_id, "message": "hello"})))
    _run(cop.copilot_try_agent(
        "pramukh", _Req({"sim_id": sim_id, "message": "my name is Rajesh"})))
    assert cop._TRY_SIMS[sim_id]["router"] is router_a
    assert cop._TRY_SIMS[sim_id]["turns"] == 2
    # Carried forward rather than rebuilt: the memory still holds turn one.
    said = [t.text for t in router_a.memory.turns]
    assert "hello" in said and "my name is Rajesh" in said


def test_the_agent_remembers_saying_each_line_exactly_once(cop, stub_llm):
    """The old replay added every past reply twice — once as the router
    generated it, once as the UI's copy injected on top. The LLM then saw
    itself say everything twice.

    (Assistant lines CAN legitimately repeat — the early-hello replay says
    the opening again on purpose — so this counts entries, not duplicates.)
    """
    start = _run(cop.copilot_try_start("barehello", _Req()))
    sim_id = start["sim_id"]
    _run(cop.copilot_try_agent(
        "barehello", _Req({"sim_id": sim_id, "message": "hello"})))
    _run(cop.copilot_try_agent(
        "barehello", _Req({"sim_id": sim_id, "message": "do you have 2BHK?"})))
    router_obj = cop._TRY_SIMS[sim_id]["router"]
    texts = [t.text for t in router_obj.memory.turns if t.role == "assistant"]
    assert len(texts) == 3, texts          # opening + connect intro + reply


# ---------------------------------------------------------------------------
# Direction fidelity
# ---------------------------------------------------------------------------

def test_inbound_simulation_does_not_prefill_a_caller_name(cop):
    _run(cop.copilot_try_start("pramukh", _Req({"direction": "inbound"})))
    sim = next(iter(cop._TRY_SIMS.values()))
    assert sim["router"].call_direction == "inbound"
    assert not sim["router"].memory.call_state.customer_name, \
        "an inbound caller is a stranger — nothing may be prefilled"


def test_outbound_simulation_carries_the_dialed_contact(cop):
    _run(cop.copilot_try_start(
        "pramukh", _Req({"direction": "outbound", "contact_name": "Meera"})))
    sim = next(iter(cop._TRY_SIMS.values()))
    assert sim["router"].call_direction == "outbound"
    assert sim["router"].outbound_contact_name == "Meera"
    assert sim["router"].memory.call_state.customer_name == "Meera"


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

def test_unknown_sim_id_is_410_not_a_dead_error(cop):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _run(cop.copilot_try_agent(
            "pramukh", _Req({"sim_id": "sim_" + "0" * 24, "message": "hi"})))
    assert exc.value.status_code == 410


def test_ended_call_refuses_further_turns(cop, stub_llm):
    start = _run(cop.copilot_try_start("pramukh", _Req()))
    sim = cop._TRY_SIMS[start["sim_id"]]
    sim["ended"] = True
    sim["end_reason"] = "agent ended the call"
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _run(cop.copilot_try_agent(
            "pramukh", _Req({"sim_id": start["sim_id"], "message": "hi"})))
    assert exc.value.status_code == 409


def test_idle_simulations_are_evicted(cop):
    start = _run(cop.copilot_try_start("pramukh", _Req()))
    cop._TRY_SIMS[start["sim_id"]]["last_used"] -= cop._TRY_SIM_TTL_S + 1
    cop._try_sim_gc()
    assert start["sim_id"] not in cop._TRY_SIMS


# ---------------------------------------------------------------------------
# A simulation must not touch real state
# ---------------------------------------------------------------------------

def test_a_simulated_booking_never_consumes_a_real_slot(cop):
    _run(cop.copilot_try_start("pramukh", _Req()))
    sim = next(iter(cop._TRY_SIMS.values()))
    sim["router"]._record_calendar_booking("Monday at 5 PM")
    sim["router"].commit_pending_turn()
    assert sim["bookings"] == ["Monday at 5 PM"]
    assert not (cop._AGENTS_DIR / "pramukh" / "bookings.json").exists()


# ---------------------------------------------------------------------------
# Old browser tabs
# ---------------------------------------------------------------------------

def test_legacy_messages_body_still_answers(cop, stub_llm):
    out = _run(cop.copilot_try_agent("pramukh", _Req({
        "messages": [{"role": "user", "text": "hello"},
                     {"role": "assistant", "text": "Hi there!"},
                     {"role": "user", "text": "do you have 2BHK flats?"}],
    })))
    assert out["reply"].strip()
    assert out["stage"]
