"""What gets STORED from a caller's answer (Aristo test calls, 2026-10-11/12).

Three defects from the real transcripts:

  * a bare "इन्वेस्टमेंट।" — the answer to "ख़ुद रहने के लिए या investment?" —
    was never stored (calls 1059, 1068): Sarvam ends Hindi with a danda and
    the danda counted as a letter, so no vocabulary word had a right edge;
  * budget_range='No' was written from "मैंने बोला कि मुझे नहीं देखना"
    (call 1059) — a refusal to visit became a budget;
  * a WhatsApp offer is held until the agent's stage reaches the roles it
    names (follow_up.whatsapp_promise.offer_from_roles).

Offline: no LLM calls, no live agent YAML.
"""
from __future__ import annotations

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice import state_manager as sm
from app.voice.conversation import ConversationRouter


@pytest.fixture(autouse=True)
def _offline(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


def _slot(name, typ, enum=None, syn=None, prio=1):
    return {"name": name, "priority": prio, "required": True, "label": name,
            "question": {"en": f"{name}?"}, "type": typ,
            "enum_values": enum or [], "enum_synonyms": syn or {}}


def _agent(extra=None):
    d = {
        "agent_id": "t_capture", "version": "1.0.0",
        "identity": {"agent_name": "Asha", "company_name": "TestCo"},
        "languages": {"supported": ["hi"], "default": "hi", "tts_voices": {}},
        "call_flow": {"stages": [
            {"id": "S1", "order": 0, "goal": "g", "role": "discovery",
             "turns_until_advance": 5},
            {"id": "S2", "order": 1, "goal": "g", "role": "recommend",
             "turns_until_advance": 5}]},
        "response_bank": {"opening_greeting": "hi"},
        "slots": [
            _slot("purpose", "enum", ["Self-use", "Investment"],
                  {"Self-use": ["खुद रहने के लिए", "रहने के लिए"],
                   "Investment": ["investment", "इन्वेस्टमेंट", "निवेश"]}),
            _slot("budget_range", "money", prio=2),
        ],
    }
    d.update(extra or {})
    return AgentDefinition.from_dict(d)


def _mgr():
    m = sm.build_state_manager(_agent())
    m.turn_index = 3
    return m


@pytest.mark.parametrize("text,expected", [
    ("इन्वेस्टमेंट।", "Investment"),
    ("निवेश।", "Investment"),
    ("खुद रहने के लिए।", "Self-use"),
    ("इन्वेस्टमेंट॥", "Investment"),
    ("इन्वेस्टमेंट", "Investment"),
])
def test_a_bare_answer_ending_in_a_danda_is_stored(text, expected):
    assert _mgr().extract_entities(text) == {"purpose": expected}


def test_a_danda_is_not_a_letter():
    assert sm._word_boundary_ok("इन्वेस्टमेंट।", 0, len("इन्वेस्टमेंट"))
    assert not sm._word_boundary_ok("इन्वेस्टमेंटर", 0, len("इन्वेस्टमेंट"))


@pytest.mark.parametrize("value,utterance", [
    ("No", "मैंने बोला कि मुझे नहीं देखना"),
    ("flexible", "देखते हैं"),
    ("90 Lakhs", "मैंने बोला कि मुझे नहीं देखना"),   # value fine, caller never said it
])
def test_a_budget_must_be_an_amount_the_caller_said(value, utterance):
    m = _mgr()
    m.merge_state({"budget_range": value}, source="llm", utterance=utterance)
    assert m.state["budget_range"] is None


@pytest.mark.parametrize("value,utterance", [
    ("90 Lakhs", "बजट नब्बे लाख तक का है"),
    ("1 Crore", "एक करोड़ तक"),
    ("1.5 Cr", "डेढ़ करोड़"),
])
def test_a_spoken_amount_is_stored(value, utterance):
    m = _mgr()
    m.merge_state({"budget_range": value}, source="llm", utterance=utterance)
    assert m.state["budget_range"] == value


def test_whatsapp_offer_is_held_until_the_named_stage_role():
    d = _agent({
        "knowledge_base": {"catalog": [
            {"id": "p1", "name": "Pramukh One", "aliases": ["One"]}]},
        "follow_up": {"whatsapp_promise": {
            "enabled": True, "track_projects": True,
            "offer_from_roles": ["recommend"]}},
    })
    r = ConversationRouter(agent_def=d, default_language="hi")
    r.state.stage = "S1"
    held = "\n".join(r._share_band_lines())
    assert "HOLD" in held
    r.state.stage = "S2"
    assert "HOLD" not in "\n".join(r._share_band_lines())


def test_no_option_means_no_hold():
    d = _agent({
        "knowledge_base": {"catalog": [
            {"id": "p1", "name": "Pramukh One", "aliases": ["One"]}]},
        "follow_up": {"whatsapp_promise": {"enabled": True,
                                           "track_projects": True}},
    })
    r = ConversationRouter(agent_def=d, default_language="hi")
    r.state.stage = "S1"
    assert "HOLD" not in "\n".join(r._share_band_lines())
