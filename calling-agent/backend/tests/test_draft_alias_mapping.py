"""
Draft alias-mapping regression suite (PLAN-copilot-draft-mapping-fix).

Incident: the interview LLM called update_draft({"call_objective": "..."}) —
the interview LAYER id, not the spec path call_flow.call_goal. Everything was
dropped, update_draft hard-errored with no corrective mapping, and the LLM
told the user their answer "is not a real setting".

Covers: F1 alias rewrite, F2 self-correcting failure contract, and the
no-shadowing / no-op-on-real-definitions invariants.

Drives the copilot tool layer directly (no LLM, no HTTP).
"""
from __future__ import annotations

import asyncio
import copy
import sys

import pytest
import yaml

from conftest import AGENTS_DIR, REPO_ROOT


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


# ─── F1: the literal incident now succeeds ──────────────────────────────────

def test_incident_call_objective_lands_in_call_goal(cop):
    session = _fresh_session()
    goal = ("A successful call asks the interested caller for their preferred "
            "location, property size configuration and budget to recommend the "
            "perfect project for them.")
    res, _s, ok, _m = _run(cop._execute_tool(
        "update_draft", {"changes": {"call_objective": goal}}, _guard(session)))
    assert ok and res["status"] == "draft_updated"
    assert "call_flow.call_goal" in res["recorded_paths"]
    assert session["interview"]["draft"]["call_flow"]["call_goal"] == goal
    assert any("aliased 'call_objective'" in n for n in res["notes"])
    # provenance recorded for the canonical path, not the alias
    assert session["interview"]["provenance"]["call_flow.call_goal"] == "confirmed"


def test_persona_scalar_aliases(cop):
    session = _fresh_session()
    res, _s, ok, _m = _run(cop._execute_tool(
        "update_draft",
        {"changes": {"tone": "warm", "energy": "high", "agent_name": "Aarav"}},
        _guard(session)))
    assert ok
    draft = session["interview"]["draft"]
    assert draft["personality"]["tone"] == "warm"
    assert draft["personality"]["energy"] == "high"
    assert draft["identity"]["agent_name"] == "Aarav"


def test_layer_id_dict_alias_merges_into_section(cop):
    session = _fresh_session()
    res, _s, ok, _m = _run(cop._execute_tool(
        "update_draft",
        {"changes": {"brand_trust": {"trust_proofs": ["10 years in business"]}}},
        _guard(session)))
    assert ok
    assert session["interview"]["draft"]["brand"]["trust_proofs"] == ["10 years in business"]


def test_explicit_real_path_beats_alias(cop):
    session = _fresh_session()
    res, _s, ok, _m = _run(cop._execute_tool(
        "update_draft",
        {"changes": {"call_objective": "alias goal",
                     "call_flow": {"call_goal": "explicit goal"}}},
        _guard(session)))
    assert ok
    assert session["interview"]["draft"]["call_flow"]["call_goal"] == "explicit goal"
    assert any("already provided" in n for n in res["notes"])


# ─── F2: failure is machine-repairable, partial success carries hints ───────

def test_all_unknown_error_names_layer_paths_and_forbids_blaming_user(cop):
    session = _fresh_session()
    # settle earlier layers so the CURRENT topic is call_objective
    _run(cop._execute_tool("update_draft", {"changes": {
        "identity": {"company_name": "SkyJets",
                     "role_description": "sells private jets to buyers"}}},
        _guard(session)))
    with pytest.raises(Exception) as ei:
        _run(cop._execute_tool(
            "update_draft", {"changes": {"zzq_nonsense_field": "whatever"}},
            _guard(session)))
    msg = str(ei.value)
    assert "records under" in msg
    assert "call_flow.call_goal" in msg          # the CURRENT layer's spec paths
    assert "do NOT ask them to rephrase" in msg
    # the failed call must not corrupt the draft
    assert "zzq_nonsense_field" not in session["interview"]["draft"]


def test_partial_success_keeps_valid_and_hints_dropped(cop):
    session = _fresh_session()
    res, _s, ok, _m = _run(cop._execute_tool(
        "update_draft",
        {"changes": {"identity": {"company_name": "SkyJets"},
                     "call_objektive": "misspelled beyond aliasing"}},
        _guard(session)))
    assert ok  # valid part recorded
    assert session["interview"]["draft"]["identity"]["company_name"] == "SkyJets"
    dropped = [n for n in res["notes"] if "call_objektive" in n]
    assert dropped and "Did you mean" in dropped[0]
    assert "call_flow.call_goal" in dropped[0]


# ─── invariants: aliases can never touch a well-formed definition ────────────

def test_alias_keys_never_shadow_real_keys(cop):
    real = set(cop._TOP_LEVEL_KEYS)
    assert not (set(cop._TOP_LEVEL_ALIASES) & real)
    assert not (set(cop._LAYER_ID_SECTION_ALIASES) & real)
    assert "data_capture" not in real
    # every alias destination is a real path
    canonical = set(cop._canonical_paths())
    for path, _types in cop._TOP_LEVEL_ALIASES.values():
        assert ".".join(path) in canonical, path
    for section in cop._LAYER_ID_SECTION_ALIASES.values():
        assert section in canonical, section


def test_alias_rewrite_is_noop_on_pramukh_yaml(cop):
    with open(AGENTS_DIR / "pramukh" / "agent.yaml", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    before = copy.deepcopy(doc)
    notes: list[str] = []
    cop._alias_rewrite(doc, notes)
    assert doc == before
    assert notes == []


def test_interview_state_prompt_names_spec_paths(cop):
    from copilot_interview import interview_progress, render_interview_state
    progress = interview_progress(
        {"identity": {"company_name": "SkyJets",
                      "role_description": "sells private jets to buyers"}}, [])
    assert progress["next_layer_id"] == "call_objective"
    block = render_interview_state(progress)
    assert "Record answers under: call_flow.call_goal, call_direction" in block


# ─── dotted flat keys (incident 2026-07-15: catalog lost to "kb.x" keys) ─────

def test_dotted_flat_keys_expand_and_record(cop):
    session = _fresh_session()
    res, _s, ok, _m = _run(cop._execute_tool(
        "update_draft",
        {"changes": {
            "knowledge_base.known_projects": {
                "Pramukh Revanta": ["Vesu, Surat", "3 BHK", "1.11 Cr onwards"]},
            "call_flow.call_goal": "Recommend the right Pramukh project and "
                                   "book a site visit for interested callers.",
            "response_bank.opening_greeting": "Good {current_time}, am I "
                                              "speaking with {contact_name}?"}},
        _guard(session)))
    assert ok
    draft = session["interview"]["draft"]
    assert "Pramukh Revanta" in draft["knowledge_base"]["known_projects"]
    assert draft["call_flow"]["call_goal"].startswith("Recommend")
    assert draft["response_bank"]["opening_greeting"].startswith("Good")
    assert any("expanded dotted key" in n for n in res["notes"])


def test_dotted_key_merges_into_existing_section(cop):
    session = _fresh_session()
    _run(cop._execute_tool("update_draft", {"changes": {
        "identity": {"company_name": "SkyJets"}}}, _guard(session)))
    res, _s, ok, _m = _run(cop._execute_tool(
        "update_draft",
        {"changes": {"identity.role_description": "sells private jets"}},
        _guard(session)))
    assert ok
    ident = session["interview"]["draft"]["identity"]
    assert ident["company_name"] == "SkyJets"          # untouched
    assert ident["role_description"] == "sells private jets"
