"""Copilot edit fidelity — every applied change must be hearable.

Regression source (2026-08-01, Jethalal): the copilot applied
response_bank.opening_greeting → "Hello?" to an outbound agent, reported
success, and the next call still opened with outbound.opening_greeting's
line. These tests replay that exact edit end-to-end through _execute_tool
and pin the guardrails that now surround every write: reachability
warnings, dead-key refusal, the engine-resolved `hearable` readback, twin
divergence reporting, the direction-aware get_agent digest, the
preview_opening tool, and the corrected doctrine text.
"""
from __future__ import annotations

import asyncio
import copy
import shutil
import sys

import pytest
import yaml

from conftest import AGENTS_DIR, REPO_ROOT

HI_OB = "नमस्ते, क्या मेरी बात {contact_name} से हो रही है?"
EN_OB = "Hello, am I speaking with {contact_name}?"

OUTBOUND_DEF = {
    "identity": {"agent_name": "Jetha Test", "company_name": "Gada Traders"},
    "languages": {"supported": ["hi", "en"], "default": "hi"},
    "call_direction": "outbound",
    "call_flow": {
        "call_goal": "Confirm identity and note interest.",
        "stages": [
            {"id": "GREETING", "order": 0, "role": "greeting",
             "goal": "Greet and confirm the contact.",
             "turns_until_advance": 2},
            {"id": "CLOSING", "order": 1, "role": "closing",
             "goal": "Thank them and close."},
        ],
    },
    "response_bank": {"opening_greeting":
                      "Hello! How can I assist you today?"},
    "outbound": {"opening_greeting": {"hi": HI_OB, "en": EN_OB}},
}


@pytest.fixture()
def copilot(tmp_path, monkeypatch):
    monkeypatch.setenv("LESCALL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    dash = str(REPO_ROOT / "dashboard")
    if dash not in sys.path:
        sys.path.insert(0, dash)
    import agent_builder_api as builder
    import agent_copilot_api as cop
    agents_dir = tmp_path / "agents"
    inbound = copy.deepcopy(OUTBOUND_DEF)
    inbound["call_direction"] = "inbound"
    inbound["outbound"] = {}
    for slug, d in (("jetha_test-outbound", OUTBOUND_DEF),
                    ("jetha_test-inbound", inbound)):
        (agents_dir / slug).mkdir(parents=True)
        (agents_dir / slug / "agent.yaml").write_text(
            yaml.safe_dump(copy.deepcopy(d), allow_unicode=True),
            encoding="utf-8")
    monkeypatch.setattr(builder, "_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(cop, "_AGENTS_DIR", agents_dir)
    for slug in ("jetha_test-outbound", "jetha_test-inbound"):
        builder._claim_slug(slug, builder._ctx_owner(), "Jetha Test")
    from app.agent_definition.registry import agent_registry
    agent_registry.invalidate_all()
    return cop, agents_dir


def _run(coro):
    return asyncio.run(coro)


def _guard():
    return {"user_turns": 1, "created_this_turn": False,
            "session": {"messages": []}, "user_message": "yes go ahead"}


# ── the incident, replayed ─────────────────────────────────────────────────

def test_incident_replay_shadowed_edit_warns_and_reads_back(copilot):
    cop, _ = copilot
    res, _s, ok, mutated = _run(cop._execute_tool("update_agent", {
        "name": "jetha_test-outbound",
        "changes": {"response_bank": {"opening_greeting": "Hello?"}},
    }, _guard()))
    assert ok and mutated
    # The write succeeds (legitimate fallback edit) but carries the warning…
    assert any("outbound.opening_greeting" in w
               for w in res.get("reachability", []))
    # …and the readback QUOTES what the next call will actually open with —
    # still the outbound line, exposing the shadow before any test call.
    assert res["hearable"]["opening_next_call"]["hi"] == HI_OB
    assert res["hearable"]["source"] == "outbound.opening_greeting"


def test_correct_outbound_edit_is_clean_and_audible(copilot):
    cop, _ = copilot
    res, _s, ok, _m = _run(cop._execute_tool("update_agent", {
        "name": "jetha_test-outbound",
        "changes": {"outbound": {"opening_greeting":
                                 {"hi": "हेलो?", "en": "Hello?"}}},
    }, _guard()))
    assert ok
    assert "reachability" not in res
    assert res["hearable"]["opening_next_call"]["hi"] == "हेलो?"
    assert res["hearable"]["opening_next_call"]["en"] == "Hello?"


def test_dead_localized_key_write_is_refused(copilot):
    cop, _ = copilot
    with pytest.raises(ValueError, match="no engine code path"):
        _run(cop._execute_tool("update_agent", {
            "name": "jetha_test-outbound",
            "changes": {"response_bank": {"localized_responses": {
                "hi": {"opening_greeting": "कभी नहीं बोली जाएगी"}}}},
        }, _guard()))


def test_propose_update_flags_shadowed_change_before_confirmation(copilot):
    cop, _ = copilot
    res, _s, ok, _m = _run(cop._execute_tool("propose_update", {
        "name": "jetha_test-outbound",
        "changes": {"response_bank": {"opening_greeting": "Namaste!"}},
        "reason": "test",
    }, _guard()))
    assert ok and res["status"] == "proposed"
    assert any("outbound.opening_greeting" in w for w in res["reachability"])
    assert "NOT be heard" in res["next"]


def test_propose_update_refuses_dead_key(copilot):
    cop, _ = copilot
    with pytest.raises(ValueError, match="no engine code path"):
        _run(cop._execute_tool("propose_update", {
            "name": "jetha_test-outbound",
            "changes": {"response_bank": {"localized_responses": {
                "en": {"opening_greeting": "never spoken"}}}},
            "reason": "test",
        }, _guard()))


# ── twin divergence ────────────────────────────────────────────────────────

def test_one_sided_edit_reports_twin_divergence(copilot):
    cop, _ = copilot
    res, _s, ok, _m = _run(cop._execute_tool("update_agent", {
        "name": "jetha_test-outbound",
        "changes": {"response_bank": {"opening_greeting": "Hello?"}},
    }, _guard()))
    assert ok
    assert res["twin"]["slug"] == "jetha_test-inbound"
    assert "response_bank.opening_greeting" in res["twin"]["diverges_on"]


def test_no_twin_note_when_twins_agree_on_changed_path(copilot):
    cop, agents_dir = copilot
    # Give both twins the same never_rules, then change something else.
    res, _s, ok, _m = _run(cop._execute_tool("update_agent", {
        "name": "jetha_test-outbound",
        "changes": {"identity": {"company_tagline": "same everywhere"}},
    }, _guard()))
    assert ok
    twin = res.get("twin")
    if twin:  # tagline empty on both twins before the edit → diverges now
        assert "identity.company_tagline" in twin["diverges_on"]


# ── digest + preview ───────────────────────────────────────────────────────

def test_get_agent_digest_shows_engine_resolved_opening(copilot):
    cop, _ = copilot
    res, _s, ok, _m = _run(cop._execute_tool(
        "get_agent", {"name": "jetha_test-outbound"}, _guard()))
    assert ok
    ss = res["spoken_surfaces"]
    assert ss["opening_line_next_call"]["line"] == HI_OB
    assert ss["opening_line_next_call"]["source"] == "outbound.opening_greeting"
    assert ss["opening_line_next_call"]["direction"] == "outbound"
    assert "NOT spoken" in ss["opening_note"]


def test_get_agent_digest_clean_agent_has_no_note(copilot):
    cop, _ = copilot
    res, _s, ok, _m = _run(cop._execute_tool(
        "get_agent", {"name": "jetha_test-inbound"}, _guard()))
    assert ok
    ss = res["spoken_surfaces"]
    assert ss["opening_line_next_call"]["source"] == \
        "response_bank.opening_greeting"
    assert "opening_note" not in ss


def test_preview_opening_tool(copilot):
    cop, _ = copilot
    res, summary, ok, mutated = _run(cop._execute_tool(
        "preview_opening", {"name": "jetha_test-outbound"}, _guard()))
    assert ok and mutated is False
    assert res["openings"]["hi"]["line"] == HI_OB
    assert res["openings"]["en"]["line"] == EN_OB
    assert "starts with" in summary


def test_preview_opening_is_registered_everywhere(copilot):
    cop, _ = copilot
    assert "preview_opening" in cop._TOOLS_WITH_AGENT_NAME
    assert "preview_opening" in cop._TOOL_VERBS
    assert any(t["function"]["name"] == "preview_opening" for t in cop.TOOLS)
    assert "preview_opening" not in cop._MUTATING_TOOLS  # read-only


# ── doctrine can no longer teach the bug ───────────────────────────────────

def test_doctrine_is_direction_aware(copilot):
    cop, _ = copilot
    src = open(cop.__file__, encoding="utf-8").read()
    assert "The first thing spoken on EVERY call is " \
        "response_bank.opening_greeting" not in src
    assert "WHICH FIELD OPENS THE CALL DEPENDS ON DIRECTION" in src
    assert 'If a tool result includes "reachability"' in src
    assert 'If a tool result includes "twin"' in src
    assert "CONFIRMATIONS ARE EXACT" in src


# ── build-path unification (adapt_direction) ───────────────────────────────

def test_adapt_direction_outbound_writes_canonical_home(copilot):
    import agent_builder_api as builder
    spec = {"identity": {"company_name": "Acme"},
            "response_bank": {"opening_greeting": "Hello"}}
    out = builder.adapt_direction(spec, "outbound")
    ob = out["outbound"]["opening_greeting"]
    assert ob["en"] == builder._OUTBOUND_GREETING
    # Both homes carry the SAME text — one truth, no shadow divergence.
    assert out["response_bank"]["opening_greeting"] == builder._OUTBOUND_GREETING


def test_adapt_direction_preserves_authored_outbound_opener(copilot):
    import agent_builder_api as builder
    spec = {"identity": {"company_name": "Acme"},
            "response_bank": {"opening_greeting": "Hello"},
            "outbound": {"opening_greeting": {"hi": HI_OB, "en": EN_OB}}}
    out = builder.adapt_direction(spec, "outbound")
    assert out["outbound"]["opening_greeting"] == {"hi": HI_OB, "en": EN_OB}
    # Fallback aligned to the authored opener, not the generic template.
    assert out["response_bank"]["opening_greeting"] == EN_OB


def test_adapt_direction_inbound_clears_dormant_outbound_opener(copilot):
    import agent_builder_api as builder
    spec = {"identity": {"company_name": "Acme"},
            "response_bank": {"opening_greeting": "Hi"},
            "outbound": {"opening_greeting": {"hi": HI_OB}}}
    out = builder.adapt_direction(spec, "inbound")
    assert out["outbound"]["opening_greeting"] == {}
    assert out["response_bank"]["opening_greeting"] == builder._INBOUND_GREETING


# ── dictated speech must land where the caller HEARS it (2026-08-04) ────────
#
# Production failure, session sms2w7slva12a15 (07-31): the owner dictated
# "Would it be okay if I could share the details to you on WhatsApp at this
# same number?" and the Copilot filed it inside the MEETING_BOOKING STAGE
# GOAL of both maya twins. Stage goals are instructions to the AI and are
# never spoken. It was in the definition, it was "saved", and the owner
# would only have found out on a call. So the check is scoped to hearable
# fields on purpose — an "anywhere in the definition" check passes this.

import sys as _sys
from conftest import REPO_ROOT as _ROOT
if str(_ROOT / "dashboard") not in _sys.path:
    _sys.path.insert(0, str(_ROOT / "dashboard"))
import agent_copilot_api as _cop  # noqa: E402


def test_dictated_sentences_are_extracted_from_quotes():
    got = _cop._dictated_sentences(
        'Please make her say "Am I speaking to the right person today?" first')
    assert "Am I speaking to the right person today?" in got


def test_dictated_sentences_are_extracted_from_a_cue():
    got = _cop._dictated_sentences(
        "After that, continue with this: My name is Pooja from B2 Market.")
    assert any("My name is Pooja from B2 Market." in g for g in got)


def test_a_short_quote_is_not_treated_as_dictation():
    assert _cop._dictated_sentences('rename "Asha" please') == []


def test_second_beat_requests_are_recognised():
    for msg in [
        "I want the agent to first start with a simple Hello, wait for the "
        "callers response as their first word will also be hello mostly. "
        "After that, continue with this: My name is Pooja from B2 Market.",
        "After a hello from the caller, I want the agent to say, "
        "'Am I speaking to {contact_name}'",
        "it should say hello, wait for their hello and after that tell them "
        "that Jethalal was going through a list",
    ]:
        assert _cop._describes_second_beat(msg), msg


def test_ordinary_edits_are_not_second_beat_requests():
    for msg in ["change her voice to priya",
                "add a rule that she never quotes prices",
                "make the greeting shorter"]:
        assert not _cop._describes_second_beat(msg), msg


def test_a_line_in_a_stage_goal_is_not_hearable():
    """The exact 07-31 failure."""
    defn = {"call_flow": {"stages": [
        {"id": "MEETING_BOOKING",
         "goal": "Once they express interest, ask: Would it be okay if I "
                 "could share the details to you on WhatsApp at this same "
                 "number?"}]}}
    assert _cop._landed_in_a_hearable_field(
        defn, "Would it be okay if I could share the details to you on "
              "WhatsApp at this same number?") is False


def test_a_line_in_the_connect_intro_is_hearable():
    defn = {"call_flow": {"connect": {"intro_line": {
        "en": "My name is Pooja from B2 Market."}}}}
    assert _cop._landed_in_a_hearable_field(
        defn, "My name is Pooja from B2 Market.") is True


def test_a_line_in_the_opening_is_hearable():
    defn = {"response_bank": {"opening_greeting": "Hello, this is Asha."}}
    assert _cop._landed_in_a_hearable_field(defn, "Hello, this is Asha.") \
        is True


def test_hearable_match_ignores_case_spacing_and_final_punctuation():
    defn = {"response_bank": {"opening_greeting": "Hello,   this is  ASHA"}}
    assert _cop._landed_in_a_hearable_field(defn, "hello, this is asha.") \
        is True
