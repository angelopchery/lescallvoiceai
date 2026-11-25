"""
create_agent draft-authority + dead-draft regression suite (incident
2026-07-15, pramukh_agent).

The fallback LLM passed a self-authored `definition` to create_agent —
fabricated projects/brand/phone numbers, call_direction=inbound, 3 generic
slots — and the old merge order (definition merged LAST) let it clobber every
confirmed interview answer. Then, post-create, the model kept "recording" the
user's corrections ("make it outbound", "voice shubh") via update_draft into
the dead draft, reporting success while the live YAML never changed.

Covers: interview-answered sections beat the definition argument; definition
still fills never-touched sections; update_draft is refused after create with
a redirect to update_agent.
"""
from __future__ import annotations

import asyncio
import sys

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


def _guard(session):
    return {"user_turns": 4, "created_this_turn": False,
            "session": session, "user_message": "use defaults for the rest and build it", "sid": "s1"}


# A complete-enough interview draft (mirrors the pramukh session's real data).
_DRAFT = {
    "identity": {"agent_name": "Vishal Patel", "company_name": "Pramukh Group",
                 "role_description": "outbound real-estate agent calling interested buyers"},
    "call_direction": "outbound",
    "call_flow": {
        "call_goal": "Confirm identity, collect location/size/budget, recommend "
                     "a Pramukh project and schedule a site visit.",
        "stages": [
            {"id": "CONFIRM_IDENTITY", "goal": "Greet the caller by name and confirm "
             "you are speaking with the right person before continuing the call."},
            {"id": "DISCOVERY", "goal": "Ask for location, size and budget preferences "
             "one at a time and store each answer before moving on."},
            {"id": "CLOSING", "goal": "Thank the caller warmly, confirm the WhatsApp "
             "number and wish them a great day before ending the call."}],
        "scheduling": {"style": "flexible"}},
    "personality": {"tone": "friendly", "energy": "medium"},
    "languages": {"supported": ["hi", "en", "gu"], "default": "hi",
                  "tts_voices": {"hi": {"speaker": "shubh"},
                                 "en": {"speaker": "shubh"},
                                 "gu": {"speaker": "shubh"}}},
    "brand": {"credibility_markers": ["30+ years", "Zero stalled projects"]},
    "knowledge_base": {"portfolio_prompt_text":
                       "Pramukh Group builds premium residential projects across "
                       "Surat, Vapi and Silvassa. Flagship: Pramukh Revanta (Vesu, "
                       "3BHK, 1.11Cr onwards) and Pramukh Aura (Vapi, ready to move)."},
    "slots": [{"name": "location_preferences", "required": True,
               "question": {"hi": "स्थान?", "en": "Location?", "gu": "સ્થળ?"}},
              {"name": "budget_preferences", "required": True,
               "question": {"hi": "बजट?", "en": "Budget?", "gu": "બજેટ?"}}],
    "response_bank": {
        "opening_greeting": "Good {current_time}, am I speaking with {contact_name}?",
        "objection_responses": {"en": ["No worries, we can find better options."]},
        "closing_responses": {"hi": "धन्यवाद!", "en": "Thank you!", "gu": "આભાર!"}},
    "guardrails": {"never_rules": ["Never disparage other builders."]},
    "calendar": {"mock_slots": [{"day": "Tomorrow", "time": "10:00 AM"}]},
    "post_call": {"outcomes": [{"id": "site_visit", "label": "Site visit", "kind": "success"}]},
}

# The kind of definition the incident model fabricated.
_FABRICATED_DEFINITION = {
    "call_direction": "inbound",
    "brand": {"credibility_markers": ["Established in 2010", "Trusted by thousands"],
              "contact_phone": "+91-12345-67890"},
    "knowledge_base": {"locations": ["Mumbai", "Pune"],
                       "portfolio_prompt_text": "I can assist with various properties."},
    "slots": [{"name": "generic_slot", "required": True,
               "question": {"en": "How can I help?"}}],
    "languages": {"tts_voices": {"gu": {"speaker": "manisha"}}},
    # a section the interview never touched — legitimate gap-filler
    "follow_up": {"callback": {"enabled": True, "default_delay_hours": 12}},
}


def _build(cop_mod, session):
    guard = _guard(session)
    res, _s, ok, _m = _run(cop_mod._execute_tool(
        "update_draft", {"changes": _DRAFT}, guard))
    assert ok
    progress = cop_mod.interview_progress(session["interview"]["draft"],
                                          session["interview"]["skipped"])
    for open_id in list(progress["open_layers"]):
        guard.pop("skips_this_turn", None)  # each skip on its own turn
        _run(cop_mod._execute_tool("skip_layer", {
            "layer_id": open_id, "user_words": "defaults are fine"}, guard))
    return guard


def test_interview_answers_beat_fabricated_definition(cop):
    cop_mod, agents_dir = cop
    session = {"messages": [], "transcript": [], "meta": {}}
    guard = _build(cop_mod, session)
    res, _s, ok, _m = _run(cop_mod._execute_tool("create_agent", {
        "name": "draft_wins", "definition": _FABRICATED_DEFINITION}, guard))
    assert ok and res["status"] == "created"
    assert any("ignored in favor of the draft" in n for n in res["notes"])
    # create yields a twin pair; res["name"] is the primary (interviewed) slug
    with open(agents_dir / res["name"] / "agent.yaml", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    # every interview-confirmed section survived; the fabrication did not
    assert doc["call_direction"] == "outbound"
    assert doc["brand"]["credibility_markers"] == ["30+ years", "Zero stalled projects"]
    assert not doc["brand"].get("contact_phone")
    assert "Mumbai" not in (doc["knowledge_base"].get("locations") or [])
    assert doc["knowledge_base"]["portfolio_prompt_text"].startswith("Pramukh Group builds")
    assert [s["name"] for s in doc["slots"]] == ["location_preferences", "budget_preferences"]
    assert doc["languages"]["tts_voices"]["gu"]["speaker"] == "shubh"
    # the never-interviewed section is a legitimate gap-filler and DID land
    assert doc["follow_up"]["callback"]["default_delay_hours"] == 12


def test_update_draft_refused_after_create(cop):
    cop_mod, _agents_dir = cop
    session = {"messages": [], "transcript": [], "meta": {}}
    guard = _build(cop_mod, session)
    res, _s, ok, _m = _run(cop_mod._execute_tool("create_agent", {
        "name": "dead_draft", "definition": {}}, guard))
    assert ok
    # the incident: "It is supposed to handle outbound calls" post-create
    with pytest.raises(Exception) as ei:
        _run(cop_mod._execute_tool("update_draft", {
            "changes": {"call_direction": "outbound"}}, guard))
    msg = str(ei.value)
    assert "ALREADY CREATED" in msg
    assert "update_agent" in msg and "dead_draft" in msg


# ─── honesty.forbidden_fabrications type safety (call-crash 2026-07-15) ──────

def test_forbidden_fabrications_bool_is_coerced(cop):
    """A bool here reached a live YAML and crashed the engine's prompt build
    on EVERY turn (agent stuck on 'could you repeat that'). The sanitizer must
    never let a non-list through."""
    cop_mod, _agents_dir = cop
    for bad, expect in ((True, None), ("no invented prices", ["no invented prices"]),
                        (["a", "", "b"], ["a", "b"]), (42, None)):
        d = {"guardrails": {"honesty": {"forbidden_fabrications": bad,
                                        "unknown_answer_policy": "admit"}}}
        cop_mod._normalize_definition(d, [])
        got = d["guardrails"]["honesty"].get("forbidden_fabrications")
        assert got == expect, (bad, got)


def test_fabrication_rule_never_crashes_on_bad_types():
    from backend.app.agent_definition.schema import AgentDefinition
    from backend.app.voice import prompt_blocks as pb
    for bad in (True, False, 42, {"x": 1}, "single string", None):
        agent = AgentDefinition.from_dict({
            "identity": {"agent_name": "T"},
            "guardrails": {"honesty": {"forbidden_fabrications": bad}}})
        rule = pb._fabrication_rule(agent)
        assert rule.startswith("✗ NEVER make up")
