"""
Objections re-ask / stage-wipe loop regression suite (incident 2026-07-15).

Session u864190591d65e126ae5f: the user's objection answers were recorded
under response_bank.objection_responses (a real engine field), but the
Objections layer's done() only accepted response_bank.objection_playbook —
so the interview re-asked the question forever. Each re-ask, the model
followed the layer's 'call_flow.stages[].branches' spec-path hint and sent
call_flow.stages = [one pushback stage], wiping the user's 8-stage flow
(lists replace whole) — three byte-identical destructive calls in a row.

Covers: done() accepts objection_responses; the stages shrink guard refuses
unknown-id shrinks with a repairable error; legitimate stage rewrites still
pass; the spec-path hint no longer advertises stages[].branches.
"""
from __future__ import annotations

import asyncio
import copy
import sys

import pytest

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
    return cop


def _guard(session):
    return {"user_turns": 3, "created_this_turn": False,
            "session": session, "user_message": "interview answer"}


def _fresh_session():
    return {"messages": [], "transcript": [], "meta": {}}


def _stage(sid, goal_suffix=""):
    return {"id": sid, "goal": ("Substantive multi-sentence goal for this "
                                f"stage covering {sid}. It says what to do "
                                "and when to advance." + goal_suffix)}


_EIGHT_STAGES = [_stage(s) for s in (
    "confirm_identity", "ask_location_preferences", "ask_property_size_preferences",
    "ask_budget_preferences", "recommend_properties", "suggest_site_visit",
    "schedule_site_visit", "closing")]

# The literal destructive payload the model sent at turns 104/108/116.
_INCIDENT_PAYLOAD = {"changes": {"call_flow": {"stages": [{
    "id": "pushback_handling",
    "goal": "Handle objections and pushbacks from the caller. If they say "
            "it's too expensive, reassure them that better options can be found.",
    "role": "objections_branching"}]}}}


def _seed_flow(cop, session):
    res, _s, ok, _m = _run(cop._execute_tool(
        "update_draft",
        {"changes": {"call_flow": {"stages": copy.deepcopy(_EIGHT_STAGES)}}},
        _guard(session)))
    assert ok and "call_flow.stages" in res["recorded_paths"]


# ─── done(): recorded objection answers must satisfy the layer ───────────────

def test_objection_responses_mark_layer_done(cop):
    from copilot_interview import _done_objections
    draft = {"languages": {"supported": ["en-IN", "hi-IN"]},
             "response_bank": {"objection_responses": {
                 "en-IN": ["No worries, we can look for better options."]}}}
    ok, missing = _done_objections(draft)
    assert ok and not missing


def test_playbook_path_still_works(cop):
    from copilot_interview import _done_objections
    draft = {"languages": {"supported": ["en-IN"]},
             "response_bank": {"objection_playbook": [
                 {"id": "too_expensive", "patterns": ["too expensive"],
                  "answer": {"en-IN": "We can find options in your budget."}}]}}
    ok, missing = _done_objections(draft)
    assert ok and not missing
    # and an empty response_bank is still not done
    ok2, missing2 = _done_objections({"languages": {"supported": ["en-IN"]}})
    assert not ok2 and missing2


def test_spec_paths_no_longer_advertise_stage_branches(cop):
    from copilot_interview import INTERVIEW_LAYERS
    layer = next(l for l in INTERVIEW_LAYERS if l.id == "objections_branching")
    assert "response_bank.objection_responses" in layer.spec_paths
    assert not any("stages" in p for p in layer.spec_paths)


# ─── the shrink guard: incident payload refused, flow intact ─────────────────

def test_incident_stage_wipe_refused_and_flow_intact(cop):
    session = _fresh_session()
    _seed_flow(cop, session)
    with pytest.raises(Exception) as ei:
        _run(cop._execute_tool("update_draft",
                               copy.deepcopy(_INCIDENT_PAYLOAD), _guard(session)))
    msg = str(ei.value)
    assert "REFUSED" in msg
    assert "objection_playbook" in msg and "objection_responses" in msg
    assert "do NOT touch" in msg
    stages = session["interview"]["draft"]["call_flow"]["stages"]
    assert [s["id"].lower() for s in stages] == [s["id"] for s in _EIGHT_STAGES]


def test_after_refusal_objections_record_and_layer_closes(cop):
    session = _fresh_session()
    _seed_flow(cop, session)
    _run(cop._execute_tool("update_draft", {"changes": {
        "languages": {"supported": ["en-IN"], "default": "en-IN"}}},
        _guard(session)))
    with pytest.raises(Exception):
        _run(cop._execute_tool("update_draft",
                               copy.deepcopy(_INCIDENT_PAYLOAD), _guard(session)))
    # the corrected retry (what the error instructs) succeeds and closes the layer
    res, _s, ok, _m = _run(cop._execute_tool("update_draft", {"changes": {
        "response_bank": {"objection_responses": {
            "en-IN": ["No worries, we can definitely look for better options."]}}}},
        _guard(session)))
    assert ok
    from copilot_interview import interview_progress
    progress = interview_progress(session["interview"]["draft"],
                                  session["interview"]["skipped"])
    layer = next(l for l in progress["layers"] if l["id"] == "objections_branching")
    assert layer["done"]
    # and the flow is still the user's
    assert len(session["interview"]["draft"]["call_flow"]["stages"]) == 8


# ─── legitimate stage edits keep working ─────────────────────────────────────

def test_grow_and_same_size_rewrites_still_allowed(cop):
    session = _fresh_session()
    res, _s, ok, _m = _run(cop._execute_tool(
        "update_draft",
        {"changes": {"call_flow": {"stages": [_stage("only_stage")]}}},
        _guard(session)))
    assert ok
    _seed_flow(cop, session)  # 1 → 8: grow allowed
    fresh = [_stage(f"rewritten_{i}") for i in range(8)]
    res, _s, ok, _m = _run(cop._execute_tool(
        "update_draft", {"changes": {"call_flow": {"stages": fresh}}},
        _guard(session)))
    assert ok  # same-size full rewrite allowed
    assert [s["id"].lower() for s in session["interview"]["draft"]["call_flow"]["stages"]] \
        == [s["id"] for s in fresh]


def test_trim_to_subset_of_existing_ids_allowed(cop):
    session = _fresh_session()
    _seed_flow(cop, session)
    keep = copy.deepcopy(_EIGHT_STAGES[:3])
    res, _s, ok, _m = _run(cop._execute_tool(
        "update_draft", {"changes": {"call_flow": {"stages": keep}}},
        _guard(session)))
    assert ok  # intentional trim to known stages passes
    assert len(session["interview"]["draft"]["call_flow"]["stages"]) == 3
