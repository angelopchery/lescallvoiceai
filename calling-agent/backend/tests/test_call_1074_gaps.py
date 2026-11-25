"""Call 1074 (Aristo test rev 8, 2026-10-12) — six gaps, one test each.

  * the agent's line echoed "बजट" and "4 BHK" before asking the factor
    question; the QA tracker credited the first dict entry that matched
    anywhere (the legacy amenity_priorities slot shares "सबसे ज़रूरी") and
    the real answer was refused as "not asked";
  * "एक करोड़ बीस लाख" was stored as 20 lakhs;
  * ₹1.2 Cr against a ₹90 lakh home read "does not fit" (above the band);
  * "I'll send it on WhatsApp" was said five times;
  * "हाँ, सुबह आ सकती हूँ, संडे को" was read back as fixed with no hour;
  * the first price question got no policy line, the second no figure.
Offline: no LLM calls, no live agent YAML.
"""
from __future__ import annotations

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice import conversation as conv
from app.voice import pitch_switch as ps
from app.voice import share_tracker as st
from app.voice.conversation import ConversationRouter
from app.voice.memory import QATracker
from app.voice.state_manager import ConversationStateManager as CSM


@pytest.fixture(autouse=True)
def _offline(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


LINE_1074 = ("₹1.20 करोड़ के बजट में Pramukh Aristo के 4 BHK apartments आपके लिए "
             "बहुत ही सही बैठेंगे मैडम — property चुनते वक़्त आपके लिए सबसे ज़रूरी "
             "क्या है, location, size, amenities, construction quality, या "
             "investment value?")


def test_tracker_credits_the_question_at_the_end_of_the_line():
    qa = QATracker(field_signals={
        "configuration_required": ["bhk"],
        "budget_range": ["budget", "बजट"],
        "amenity_priorities": ["सबसे ज़रूरी", "matters most"],
        "decision_factor": ["सबसे ज़रूरी", "property चुनते वक़्त आपके लिए सबसे "
                            "ज़रूरी क्या है"],
    })
    qa.mark_extracted("configuration_required", "4 BHK")
    qa.mark_extracted("budget_range", "1.2 Crore")
    qa.observe_agent_turn(LINE_1074, 7)
    # Same position for both factor slots → the longer (own-question) signal.
    assert qa.last_asked_field == "decision_factor"
    assert qa.asks_field("decision_factor", LINE_1074)


@pytest.mark.parametrize("text,expected", [
    ("एक करोड़ बीस लाख।", "1.2 Crore"),
    ("1 crore 20 lakh", "1.2 Crore"),
    ("डेढ़ करोड़", "1.5 Crore"),
    ("पचास लाख", "50 Lakhs"),
    ("1.5 crore se 2 crore", "2 Crore"),
    ("50-60 lakh", "50-60 Lakhs"),
])
def test_compound_amounts_are_one_amount(text, expected):
    assert CSM._extract_budget(text) == expected


def test_a_budget_above_the_price_is_not_a_mismatch():
    kb = type("KB", (), {})()
    kb.catalog = [{"id": "p1", "name": "P One", "budget_lakhs": [90, 90],
                   "configurations": ["3 BHK", "4 BHK"]}]
    kb.primary_project = {"id": "p1", "mode": "exclusive", "budget_switch": True}
    state = ps.PitchSwitchState(base_id="p1", active_id="p1")
    assert not any("BUDGET FIT" in l for l in ps.band(kb, state, "1.2 Crore"))
    assert any("BUDGET FIT" in l for l in ps.band(kb, state, "40 Lakhs"))


def test_a_promise_said_twice_is_never_said_again():
    kb = type("KB", (), {})()
    kb.catalog = [{"id": "p1", "name": "Pramukh One", "aliases": ["One"]}]
    s = st.ShareState()
    st.observe_agent(kb, s, "मैं Pramukh One की details WhatsApp पर भेज देता हूँ।")
    assert not any("Never say it again" in l for l in st.band(kb, s, "p1"))
    st.observe_agent(kb, s, "बिल्कुल, मैं details इसी नंबर पर भेज देता हूँ।")
    assert s.promised_lines == 2
    assert any("Never say it again" in l for l in st.band(kb, s, "p1"))


def _agent(pricing=None):
    d = {
        "agent_id": "t_1074", "version": "1.0.0",
        "identity": {"agent_name": "Asha", "company_name": "TestCo"},
        "languages": {"supported": ["hi"], "default": "hi", "tts_voices": {}},
        "call_flow": {"stages": [
            {"id": "S1", "order": 0, "goal": "g", "role": "discovery",
             "turns_until_advance": 5},
            {"id": "S2", "order": 1, "goal": "g", "role": "scheduling",
             "turns_until_advance": 5}]},
        "response_bank": {"opening_greeting": "hi"},
        "knowledge_base": {"pricing_policy": pricing or {}},
    }
    return AgentDefinition.from_dict(d)


def test_a_day_without_an_hour_asks_the_hour():
    r = ConversationRouter(agent_def=_agent(), default_language="hi")
    r.state.stage = "S2"
    r.memory.add_user("हाँ, सुबह आ सकती हूँ, संडे को।")
    assert any("TIME NEEDED" in l for l in r._visit_time_needed_band())
    r.memory.add_user("संडे ग्यारह बजे।")
    assert r._visit_time_needed_band() == []
    r.state.stage = "S1"
    r.memory.add_user("संडे को")
    assert r._visit_time_needed_band() == []


def test_price_band_first_ask_policy_second_ask_approx():
    r = ConversationRouter(agent_def=_agent({
        "mode": "on_request", "second_ask_approx": True,
        "approx_line": "about ₹90 lakh (3 BHK)"}), default_language="hi")
    r._price_ask_pending = False
    for t in ("प्राइस कितना है?",):
        r._price_ask_pending = bool(conv._PRICE_ASK_RE.search(t))
        r._price_asks += 1
    first = "\n".join(r._price_band_lines())
    assert "FIRST price question" in first and "90 lakh" not in first
    r._price_asks += 1
    second = "\n".join(r._price_band_lines())
    assert "about ₹90 lakh" in second
    assert conv._PRICE_ASK_RE.search("अप्रोक्सीमेट तो बताओ")
    assert not conv._PRICE_ASK_RE.search("Daman कितना पास है?")


def test_price_band_is_off_without_the_option():
    r = ConversationRouter(agent_def=_agent(), default_language="hi")
    r._price_ask_pending = True
    r._price_asks = 2
    assert r._price_band_lines() == []


def test_a_yes_to_the_offer_is_acknowledged_not_restated():
    kb = type("KB", (), {})()
    kb.catalog = [{"id": "p1", "name": "Pramukh One", "aliases": ["One"]}]
    s = st.ShareState()
    st.observe_agent(kb, s, "क्या मैं Pramukh One की details WhatsApp पर भेज दूँ?")
    st.observe_caller(kb, s, "हाँ, भेज दो।")
    assert s.just_agreed and "p1" in s.agreed
    assert any("just said YES" in l for l in st.band(kb, s, "p1"))
    st.observe_agent(kb, s, "ठीक है।")
    st.observe_caller(kb, s, "और पेमेंट कैसे करनी है?")
    assert not s.just_agreed
    assert not any("just said YES" in l for l in st.band(kb, s, "p1"))


def test_a_yes_to_the_number_question_is_acknowledged_not_restated():
    kb = type("KB", (), {})()
    kb.catalog = [{"id": "p1", "name": "Pramukh One", "aliases": ["One"]}]
    s = st.ShareState()
    st.observe_agent(kb, s, "क्या यही नंबर WhatsApp पर है?")
    st.observe_caller(kb, s, "हाँ हाँ यही है।")
    assert s.number == st.NUMBER_SAME and s.just_agreed
    assert any("just said YES" in l for l in st.band(kb, s, "p1"))


def test_a_budget_above_the_price_never_suggests_another_project():
    """Call 1077: ₹1.2 Cr against a ₹90 lakh 3 BHK offered Silver Stone and
    Green County; a caller who can afford more is not out of range."""
    kb = type("KB", (), {})()
    kb.catalog = [
        {"id": "p1", "name": "P One", "budget_lakhs": [90, 90],
         "configurations": ["3 BHK", "4 BHK"]},
        {"id": "p2", "name": "P Two", "budget_lakhs": [110, 125],
         "configurations": ["3 BHK", "4 BHK"]},
    ]
    kb.primary_project = {"id": "p1", "mode": "exclusive", "budget_switch": True}
    state = ps.PitchSwitchState(base_id="p1", active_id="p1")
    cands, _, _ = ps.compute_candidates(kb, state, "1.2 Crore")
    assert cands == []
    assert not any("BUDGET FIT" in l for l in ps.band(kb, state, "1.2 Crore"))
    cands, _, _ = ps.compute_candidates(kb, state, "40 Lakhs")
    assert not any("BUDGET FIT" in l for l in ps.band(kb, state, "1.2 Crore"))


def _kb_two():
    kb = type("KB", (), {})()
    kb.catalog = [
        {"id": "p1", "name": "P One", "aliases": ["One"], "budget_lakhs": [90, 111],
         "configurations": ["3 BHK", "4 BHK"]},
        {"id": "p2", "name": "P Two", "aliases": ["Two"], "budget_lakhs": [40, 55],
         "configurations": ["2 BHK", "3 BHK"]},
    ]
    kb.primary_project = {"id": "p1", "mode": "exclusive", "budget_switch": True}
    return kb


def test_below_budget_asks_the_generic_question_before_any_name():
    kb = _kb_two()
    s = ps.PitchSwitchState(base_id="p1", active_id="p1")
    band = "\n".join(ps.band(kb, s, "40 Lakhs"))
    assert "other properties in that budget" in band and "P Two" not in band.split("▸ It is a suggestion")[0]
    ps.observe_agent(kb, s, "क्या आप इस budget में हमारी और properties देखना चाहेंगे?")
    assert s.generic_open
    assert "not answered that yet" in "\n".join(ps.band(kb, s, "40 Lakhs"))
    ps.observe_caller(kb, s, "हाँ, बताओ", "40 Lakhs")
    band = "\n".join(ps.band(kb, s, "40 Lakhs"))
    assert s.generic_yes and "P Two" in band and "said YES" in band
    ps.observe_agent(kb, s, "P Two में 2 और 3 BHK हैं, लगभग 40 से 55 लाख — इसके बारे में सुनना चाहेंगे?")
    assert s.offered == ["p2"]
    assert ps.observe_caller(kb, s, "हाँ", "40 Lakhs") == "p2"


def test_below_budget_no_to_the_generic_question_silences_it():
    kb = _kb_two()
    s = ps.PitchSwitchState(base_id="p1", active_id="p1")
    ps.band(kb, s, "40 Lakhs")
    ps.observe_agent(kb, s, "क्या आप इस budget में हमारी और properties देखना चाहेंगे?")
    ps.observe_caller(kb, s, "नहीं, Aristo ही बताओ", "40 Lakhs")
    assert s.quiet_for_budget == "40 Lakhs" and "p2" in s.declined
    assert not any("BUDGET FIT" in l for l in ps.band(kb, s, "40 Lakhs"))


def _mgr_factor():
    d = AgentDefinition.from_dict({
        "agent_id": "t_opts", "version": "1.0.0",
        "identity": {"agent_name": "Asha", "company_name": "TestCo"},
        "languages": {"supported": ["hi"], "default": "hi", "tts_voices": {}},
        "call_flow": {"stages": [{"id": "S1", "order": 0, "goal": "g",
                                  "role": "discovery", "turns_until_advance": 5}]},
        "response_bank": {"opening_greeting": "hi"},
        "slots": [
            {"name": "configuration_required", "priority": 1, "type": "enum",
             "enum_values": ["3 BHK", "4 BHK"],
             "enum_synonyms": {"3 BHK": ["3 bhk", "तीन भी"], "4 BHK": ["4 bhk", "चार भी"]}},
            {"name": "decision_factor", "priority": 2, "type": "enum", "ask_gated": True,
             "enum_values": ["Location", "Size", "Amenities", "Construction quality",
                             "Investment value"],
             "enum_synonyms": {"Location": ["location", "लोकेशन"], "Size": ["size", "साइज़"],
                               "Amenities": ["amenities", "एमनिटिज़", "एमेनिटीज़"],
                               "Construction quality": ["construction", "कंस्ट्रक्शन"],
                               "Investment value": ["investment value", "इन्वेस्टमेंट वैल्यू"]}},
        ]})
    from app.voice import state_manager as sm
    m = sm.build_state_manager(d)
    m.turn_index = 6
    return m


@pytest.mark.parametrize("line,slot", [
    ("यह घर चुनने में आपके लिए सबसे ज़्यादा क्या मायने रखता है — लोकेशन, साइज़, "
     "एमनिटिज़, या कंस्ट्रक्शन क्वालिटी?", "decision_factor"),
    ("समझ गया मैडम। आपके लिए कौन सी बात सबसे ज़्यादा मायने रखती है — लोकेशन, "
     "साइज़, या इन्वेस्टमेंट वैल्यू?", "decision_factor"),
    ("Arisha जी, 3 BHK या 4 BHK — कौन सा size देख रहे हैं?", "configuration_required"),
    ("Pramukh Aristo में 3 और 4 BHK हैं। आप कहाँ से बात कर रहे हैं?", None),
])
def test_a_question_listing_options_asked_that_slot(line, slot):
    assert _mgr_factor().options_asked(line) == slot


def test_reworded_factor_answer_is_stored():
    m = _mgr_factor()
    m.observe_user("amenities", asked=m.options_asked(
        "सबसे ज़्यादा क्या मायने रखता है — लोकेशन, साइज़, एमनिटिज़, या कंस्ट्रक्शन क्वालिटी?"))
    assert m.state["decision_factor"] == "Amenities"


def test_stt_teen_bhi_is_three_bhk():
    m = _mgr_factor()
    assert m.extract_entities("तीन भी है इसके लिए").get("configuration_required") == "3 BHK"
