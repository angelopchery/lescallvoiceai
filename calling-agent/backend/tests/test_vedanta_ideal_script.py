"""Ravi test — Pramukh Vedanta on Pramukh's ideal call script (2026-10-07).

Frozen fixture: requirement-first stages, purpose branch, no Vedanta price
figure in the prompt, LesCall opening beats kept.
"""
from pathlib import Path

import pytest

from app.agent_definition.schema import AgentDefinition
from app.agent_definition.spoken_surfaces import resolve_spoken_surfaces
from app.voice.conversation import ConversationRouter

FIX = Path(__file__).parent / "fixtures" / "pramukh_vedanta_test_outbound_2026_10_07.yaml"


@pytest.fixture()
def agent():
    return AgentDefinition.from_yaml(FIX)


def test_flow_is_requirement_first(agent):
    assert [s.id for s in agent.call_flow.stages] == [
        "WARM_WELCOME", "INTRO_AND_TIME_CHECK", "REQUIREMENT_FIRST",
        "UNDERSTAND_REQUIREMENT", "FIT_AND_PITCH", "INVITE_SITE_VISIT",
        "CONFIRM_AND_CLOSE"]
    req = [s.name for s in agent.slots if s.required]
    assert req[:7] == ["caller_name", "purpose", "configuration_required",
                       "budget_range", "purchase_timeline", "decision_factor",
                       "investment_horizon"]


def test_opening_beats_are_lescalls(agent):
    hi = resolve_spoken_surfaces(agent, "outbound")["openings"]["hi"]
    assert hi["line"] == "हेलो?" and "{contact_name}" in hi["second_line"]
    assert "New Chala" in agent.outbound.good_time_check_line["hi"]


@pytest.mark.parametrize("reply,retired", [
    ("ख़ुद रहने के लिए", ["investment_horizon"]), ("investment के लिए", [])])
def test_purpose_branch(agent, reply, retired):
    r = ConversationRouter(agent_def=agent, default_language="hi")
    r.state.stage = "REQUIREMENT_FIRST"
    r.state_mgr.observe_user(reply, asked="purpose")
    assert r._maybe_branch("information")
    assert r.state.stage == "UNDERSTAND_REQUIREMENT"
    assert sorted(r.state_mgr.retired) == retired


def test_no_vedanta_price_figure_reaches_the_prompt(agent):
    r = ConversationRouter(agent_def=agent, default_language="hi")
    r.state.stage = "FIT_AND_PITCH"
    p = r._build_system_prompt()
    line = p[p.find("▸ Pramukh Vedanta"):].split("\n")[0]
    assert "1.75" not in line and "cost sheet" in line
    # RUNBOOK §72: facts are the owner's sheet, not the script's older set.
    assert "Ultra 4 BHK" in line and "ready possession" in line
    assert "6 acres" in p and "Survey No." not in p and "5.9" not in p
    assert "swimming pool" not in line.lower() and "clubhouse" not in line.lower()
