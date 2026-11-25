"""
P8 validation (plan §11): the §8 seven-step outbound flow expressed as a REAL
agent built through the new Copilot pipeline (update_draft → hardened create
gate → draft-consuming create), then executed by the live engine: stage gates,
branches, branch actions, slots, callback policy.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import yaml

from conftest import REPO_ROOT


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def cop(tmp_path, monkeypatch):
    monkeypatch.setenv("LESCALL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    dash = str(REPO_ROOT / "dashboard")
    if dash not in sys.path:
        sys.path.insert(0, dash)
    import agent_builder_api as builder
    import agent_copilot_api as cop
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    monkeypatch.setattr(builder, "_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(cop, "_AGENTS_DIR", agents_dir)
    return cop, agents_dir


def _seven_step_draft() -> dict:
    """The §8 reference spec, as interview answers (draft form)."""
    fixture = Path(__file__).resolve().parent / "fixtures" / "seven_step_agent.yaml"
    data = yaml.safe_load(fixture.read_text(encoding="utf-8"))
    for k in ("agent_id", "version"):
        data.pop(k, None)
    return data


def test_seven_step_agent_via_copilot(cop):
    cop_mod, agents_dir = cop
    session: dict = {"messages": [], "transcript": [], "meta": {}}
    guard = {"user_turns": 5, "created_this_turn": False,
             "session": session, "user_message": "use defaults for the rest and build it", "sid": "s7"}

    # Interview: the owner's answers land in the draft (multiple turns
    # compressed into one dump — the engine doesn't care how they arrived).
    res, _s, ok, _m = _run(cop_mod._execute_tool(
        "update_draft", {"changes": _seven_step_draft()}, guard))
    assert ok

    # Close any remaining compulsory layers explicitly (the §8 example spec
    # doesn't answer every interview topic).
    progress = cop_mod.interview_progress(session["interview"]["draft"],
                                          session["interview"]["skipped"])
    for open_id in list(progress["open_layers"]):
        guard.pop("skips_this_turn", None)  # each skip on its own turn
        _run(cop_mod._execute_tool("skip_layer", {
            "layer_id": open_id, "user_words": "defaults are fine, build it"}, guard))

    res2, _s, ok2, _m2 = _run(cop_mod._execute_tool("create_agent", {
        "name": "seven_step", "definition": {}}, guard))
    assert ok2 and res2["status"] == "created"

    # ── The created agent runs the §8 flow on the LIVE engine ──────────────
    from app.agent_definition.schema import AgentDefinition
    from app.voice.conversation import ConversationRouter

    agent = AgentDefinition.from_yaml(agents_dir / res2["name"] / "agent.yaml")
    assert agent.call_direction == "outbound"
    assert [s.id for s in agent.call_flow.stages][:2] == ["OPENING", "DISCOVER_LOCATION"]

    r = ConversationRouter(agent_def=agent, default_language="hi")
    r._available_calendar_slots = lambda: list(r._def.calendar.mock_slots or [])
    r.set_call_context(direction="outbound", contact_name="Ramesh Bhai",
                       contact_phone="9876543210")
    r.state.stage = "OPENING"

    # Steps 1-2: identity + good-time → affirmation gate
    r.state.discovery_turns = 1
    r.state.last_intent = "affirmation"
    r._maybe_advance_stage()
    assert r.state.stage == "DISCOVER_LOCATION"

    # Steps 3-5: the three discovery gates
    r.state.last_intent = "general"
    for utterance, slot, value, next_stage in [
        ("Vapi side dekh rahe hain", "location", "Vapi", "DISCOVER_CONFIG"),
        ("2 bhk chahiye", "bhk", "2BHK", "DISCOVER_BUDGET"),
        ("30 lakh tak", "budget", "30 Lakhs", "RECOMMEND"),
    ]:
        r.state_mgr.observe_user(utterance)
        assert r.state_mgr.state[slot] == value
        r._maybe_advance_stage()
        assert r.state.stage == next_stage

    # Step 6: deterministic recommendation candidates in the prompt
    prompt = r._build_system_prompt()
    assert "Recommend: Sahaj" in prompt

    # Step 7b: rejection → WhatsApp follow-up branch with stored promise
    assert r._maybe_branch("negation")
    assert r.state.stage == "HANDLE_REJECTION"
    assert r._maybe_branch("affirmation")
    assert r.state.stage == "NEXT_STEP"
    assert r.state_mgr.extra_flags.get("whatsapp_promised") is True
    assert "phone" in r.state_mgr.detect_missing_slots()["missing"]

    # Phone capture + confirm + read-back
    r.state_mgr.observe_user("98765 43210 pe bhej dena")
    assert "phone" in r.state_mgr.detect_missing_slots()["pending"]
    r.state_mgr.set_externally("phone", r.state_mgr.state["phone"], source="system")
    assert "98 76 54 32 10" in r._build_system_prompt()

    # advance_when slots:[phone] → CLOSING
    r._maybe_advance_stage()
    assert r.state.stage == "CLOSING"
