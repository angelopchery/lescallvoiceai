"""WhatsApp sharing tracker (RUNBOOK §72): which project is under discussion,
which ones the caller agreed to receive, and which number to send to.

Built from call 1052 (2026-10-10, Ravi test / Pramukh Vedanta). Offline: no
LLM, no live agent YAML — the frozen Swarit/Akshay fixture with the flag
toggled in the test.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice import share_tracker as st
from app.voice.conversation import ConversationRouter

FIX = Path(__file__).parent / "fixtures" / "pramukh_swarit_akshay_outbound_2026_10_07.yaml"

SWARIT, SILVER, GREEN, VIVAN, BLUE, ARISTO, VEDANTA = (
    "pramukh-swarit", "pramukh-silver-stone", "pramukh-green-county",
    "pramukh-vivan", "pramukh-blue-stone", "pramukh-aristo", "pramukh-vedanta")


@pytest.fixture()
def agent():
    a = AgentDefinition.from_yaml(FIX)
    a.follow_up.whatsapp_promise["track_projects"] = True
    return a


@pytest.fixture()
def kb(agent):
    return agent.knowledge_base


@pytest.fixture()
def plain():
    return AgentDefinition.from_yaml(FIX)


# ── opt-in ──────────────────────────────────────────────────────────────────
def test_disabled_without_flag(plain):
    assert not st.enabled(plain)
    r = ConversationRouter(agent_def=plain, default_language="hi")
    assert r._share is None
    assert "━━ WHATSAPP" not in r._build_system_prompt()


def test_enabled_with_flag(agent):
    assert st.enabled(agent)
    r = ConversationRouter(agent_def=agent, default_language="hi")
    assert r._share is not None
    assert "━━ WHATSAPP — what you share, and where ━━" in r._build_system_prompt()


# ── names, numbers ──────────────────────────────────────────────────────────
def test_named_in_order_last_mention_wins(kb):
    assert st.named_in_order(kb, "Silver Stone नहीं, Blue Stone बोल रहा हूँ") == [SILVER, BLUE]
    assert st.named_in_order(kb, "मुझे ग्रीन काउंटी का भेज दो")[-1] == GREEN
    assert st.named_in_order(kb, "green country की location") == [GREEN]
    assert st.named_in_order(kb, "आपने विमान का क्या बोला") == []


@pytest.mark.parametrize("text,digits", [
    ("9876543210", "9876543210"),
    ("mera number 98765 43210 hai", "9876543210"),
    ("+91 98765-43210", "9876543210"),
    ("९८७६५४३२१०", "9876543210"),
    ("नौ आठ सात छह पांच चार तीन दो एक शून्य", "9876543210"),
    ("nine eight seven six five four three two one zero", "9876543210"),
    ("double nine 8 7 6 5 4 3 2 1", "9987654321"),
    ("1234567890", ""),                      # not an Indian mobile
    ("3 BHK 2320 sq ft 95 lakh", ""),
])
def test_phone_in(text, digits):
    assert st.phone_in(text) == digits


# ── the agent's line ────────────────────────────────────────────────────────
def test_agent_offer_names_the_topic(kb):
    s = st.ShareState()
    st.observe_agent(kb, s, "Pramukh Green County में 3 BHK का कारपेट एरिया लगभग "
                     "1,242 वर्ग फुट है सर। क्या यह नंबर WhatsApp पर है जिस पर मैं "
                     "आपको इसका लोकेशन, फ्लोर प्लान और cost sheet भेज सकूँ?")
    assert s.topic_id == GREEN and s.offered_id == GREEN
    assert s.number_question_open and s.number_asked == 1


def test_agent_offer_names_the_project_in_the_sentence(kb):
    s = st.ShareState()
    s.topic_id = VIVAN
    st.observe_agent(kb, s, "Pramukh Vivan में gymnasium और temple जैसी amenities "
                     "हैं सर। क्या मैं आपको Pramukh Silver Stone का लोकेशन WhatsApp "
                     "पर भेज दूँ सर?")
    assert s.topic_id == VIVAN            # two names → topic untouched
    assert s.offered_id == SILVER
    assert not s.number_question_open     # no "number" in the offer


def test_agent_statement_is_not_an_offer(kb):
    s = st.ShareState()
    st.observe_agent(kb, s, "बिल्कुल सर, मैं Pramukh Silver Stone का लोकेशन इसी "
                     "नंबर पर WhatsApp पर भेज देता हूँ। ठीक है, तो फिर मैं कॉल कट कर दूँ?")
    assert s.topic_id == SILVER and s.offered_id is None


def test_agent_line_seen_twice_counts_once(kb):
    s = st.ShareState()
    line = "क्या यह नंबर WhatsApp पर है सर?"
    st.observe_agent(kb, s, line)
    st.observe_agent(kb, s, line)
    assert s.number_asked == 1


def test_ask_for_digits_only_after_different(kb):
    s = st.ShareState()
    ask = "ठीक है सर, आप मुझे वो WhatsApp नंबर बता दीजिए?"
    st.observe_agent(kb, s, ask)
    assert not s.alt_question_open and s.alt_number_asked == 0
    s.number = st.NUMBER_DIFFERENT
    st.observe_agent(kb, s, ask + " ")
    assert s.alt_question_open and s.alt_number_asked == 1


# ── the caller's line ───────────────────────────────────────────────────────
def test_yes_to_offer_is_agreed(kb):
    s = st.ShareState(topic_id=GREEN, offered_id=GREEN)
    out = st.observe_caller(kb, s, "हाँ, मुझे वो भेज देना।")
    assert s.agreed == [GREEN] and out["agreed"] == [GREEN]
    assert s.offered_id is None


def test_yes_opening_a_question_is_agreed(kb):
    s = st.ShareState(topic_id=GREEN, offered_id=GREEN, number_question_open=True)
    st.observe_caller(kb, s, "हां जी हां जी और यहां पे नियर बाय चीजें बता सकते हैं?")
    assert s.agreed == [GREEN] and s.number == st.NUMBER_SAME


def test_plain_question_is_not_consent(kb):
    s = st.ShareState(topic_id=GREEN, offered_id=GREEN)
    st.observe_caller(kb, s, "Green County, मुझे उसकी लोकेशन जाननी है।")
    assert s.agreed == [] and s.topic_id == GREEN


def test_named_project_before_send_verb(kb):
    s = st.ShareState(topic_id=SILVER)
    st.observe_caller(kb, s, "मुझे Green County का भेज दो।")
    assert s.agreed == [GREEN] and s.topic_id == GREEN


def test_name_after_send_verb_is_a_new_question(kb):
    s = st.ShareState(topic_id=BLUE, offered_id=BLUE)
    st.observe_caller(kb, s, "हाँ हाँ, मुझे व्हाट्सएप पर भेज देना। और फाइनली मुझे "
                      "बस एक Aristo की लोकेशन बता दो?")
    assert s.agreed == [BLUE]
    assert s.topic_id == ARISTO


def test_send_without_name_goes_to_the_topic_we_were_on(kb):
    # call 1052 replay: "हाँ वो भी भेज देना। और Aristo की लोकेशन बता दो" —
    # the first sentence is about Blue Stone, the second a new question.
    s = st.ShareState(topic_id=BLUE)
    st.observe_caller(kb, s, "हाँ वो भी भेज देना। और Aristo की लोकेशन बता दो।")
    assert s.agreed == [BLUE] and s.topic_id == ARISTO


def test_tell_me_request_is_not_a_number_answer(kb):
    s = st.ShareState(number_question_open=True)
    st.observe_caller(kb, s, "Vivan के बारे में भी बता दो।")
    assert s.number == st.NUMBER_UNKNOWN and s.topic_id == VIVAN


def test_correction_with_negation_and_send(kb):
    s = st.ShareState(topic_id=SILVER, offered_id=BLUE)
    st.observe_caller(kb, s, "Silver Stone नहीं, Blue Stone बोल रहा हूँ। हाँ, वो "
                      "भेज देना मेरे को। और कितनी बिल्डिंग है इसमें?")
    assert s.agreed == [BLUE] and s.topic_id == BLUE


def test_decline_is_remembered(kb):
    s = st.ShareState(topic_id=SWARIT, offered_id=SWARIT)
    out = st.observe_caller(kb, s, "नहीं, रहने दो।")
    assert s.agreed == [] and s.declined == {SWARIT} and out["declined"] == SWARIT
    lines = "\n".join(st.band(kb, s, SWARIT))
    assert "declined Pramukh Swarit" in lines


@pytest.mark.parametrize("text", [
    "नहीं, व्हाट्सएप पे मेरा अलग नंबर है एक्चुअली।",
    "नहीं नहीं, मेरा नंबर अलग है भाई। व्हाट्सएप का नंबर अलग है",
    "मेरा व्हाट्सएप नंबर अलग है, यह वाला नहीं है।",
    "whatsapp mera doosre number pe hai",
    "is number pe whatsapp nahi hai",
    "my WhatsApp is on a different number",
])
def test_different_number_phrases(kb, text):
    s = st.ShareState(number=st.NUMBER_SAME)
    out = st.observe_caller(kb, s, text)
    assert s.number == st.NUMBER_DIFFERENT and out["number"] == st.NUMBER_DIFFERENT


def test_another_project_is_not_another_number(kb):
    s = st.ShareState(topic_id=SILVER, offered_id=SILVER)
    st.observe_caller(kb, s, "हाँ, और दूसरे project का भी भेज दो")
    assert s.number == st.NUMBER_UNKNOWN


def test_no_to_number_question(kb):
    s = st.ShareState(number_question_open=True)
    st.observe_caller(kb, s, "नहीं")
    assert s.number == st.NUMBER_DIFFERENT


def test_digits_captured_only_after_different(kb):
    s = st.ShareState()
    st.observe_caller(kb, s, "callback 98765 43210 pe kar dena")
    assert s.alt_number == "" and s.number == st.NUMBER_UNKNOWN
    s.number = st.NUMBER_DIFFERENT
    out = st.observe_caller(kb, s, "नौ आठ सात छह पांच चार तीन दो एक शून्य")
    assert s.number == st.NUMBER_CAPTURED and s.alt_number == "9876543210"
    assert out["alt_number"] == "9876543210"


# ── the band ────────────────────────────────────────────────────────────────
def test_band_topic_vs_pitch(kb):
    s = st.ShareState(topic_id=GREEN, agreed=[SILVER, GREEN, VEDANTA])
    lines = "\n".join(st.band(kb, s, SILVER))
    assert "Project under discussion RIGHT NOW: Pramukh Green County" in lines
    assert "Pramukh Silver Stone is still the project you pitch" in lines
    assert "YES to receiving on WhatsApp: Pramukh Silver Stone, Pramukh Green County, Pramukh Vedanta" in lines
    assert "whether they mean Pramukh Green County or Pramukh Silver Stone" in lines


def test_band_topic_is_pitch(kb):
    s = st.ShareState()
    lines = "\n".join(st.band(kb, s, SWARIT))
    assert "Project under discussion right now: Pramukh Swarit — the project you pitch" in lines
    assert "not agreed to receive anything on WhatsApp yet" in lines
    assert "Site visit:" not in lines


@pytest.mark.parametrize("state,needle", [
    (dict(), "not confirmed yet"),
    (dict(number_asked=1), "already asked whether this number is on WhatsApp"),
    (dict(number_asked=2), "Do NOT ask again"),
    (dict(number=st.NUMBER_SAME, number_asked=3), "THIS number — confirmed"),
    (dict(number=st.NUMBER_DIFFERENT), "Ask them, once, to tell you the WhatsApp number"),
    (dict(number=st.NUMBER_DIFFERENT, alt_number_asked=2), "could not catch it"),
    (dict(number=st.NUMBER_CAPTURED, alt_number="9876543210"), "ending in 3210"),
])
def test_band_number_lines(kb, state, needle):
    s = st.ShareState(**state)
    lines = "\n".join(st.band(kb, s, SWARIT))
    assert needle in lines
    if state.get("alt_number"):
        assert "9876543210" not in lines          # never the whole number


# ── through the router ──────────────────────────────────────────────────────
def test_router_records_flags_and_slot(agent):
    r = ConversationRouter(agent_def=agent, default_language="hi")
    r._share_observe_agent("Pramukh Green County की location Chharwada है सर। क्या "
                           "यह नंबर WhatsApp पर है जिस पर मैं इसका लोकेशन भेज सकूँ?")
    r.state_mgr.observe_user("हाँ जी, मुझे वो भेज देना।", asked=None)
    r._share_observe_caller("हाँ जी, मुझे वो भेज देना।")
    flags = r.state_mgr.extra_flags
    assert flags["whatsapp_share_projects"] == ["Pramukh Green County"]
    assert flags["whatsapp_promised"] is True
    assert flags["whatsapp_number_status"] == "same"
    assert r.state_mgr.state.get("whatsapp_same_number") == "Yes"
    band = r._build_system_prompt()
    assert "YES to receiving on WhatsApp: Pramukh Green County" in band
    assert "THIS number — confirmed" in band

    r._share_observe_caller("नहीं, व्हाट्सएप पे मेरा अलग नंबर है")
    assert flags["whatsapp_number_status"] == "different"
    assert r.state_mgr.state.get("whatsapp_same_number") == "No"
    r._share_observe_caller("98765 43210")
    assert flags["whatsapp_alt_number"] == "9876543210"
    assert "ending in 3210" in r._build_system_prompt()


def test_consent_reads_the_alternate_number():
    from shared.whatsapp import consent
    call = {"collected_data": {"flags": {"whatsapp_promised": True,
                                         "whatsapp_alt_number": "9876543210"},
                               "state": {}}, "transcript": ""}
    c = consent.detect(call, [])
    assert c and c.granted and c.given_phone == "+919876543210"


# ── call 1052 replayed ──────────────────────────────────────────────────────
CALL_1052 = [
    ("agent", "Pramukh Silver Stone में 3 और 4 BHK के spacious homes मिल जाते हैं सर। क्या आप इस weekend site visit करना चाहेंगे सर?"),
    ("caller", "हां, थोड़ा एमेनिटीज़ के बारे में बता सकते हो?"),
    ("agent", "Pramukh Silver Stone का कुल लैंड एरिया लगभग दो एकड़ है सर। क्या यह नंबर WhatsApp पर है जिस पर मैं आपको इसका लोकेशन, फ्लोर प्लान और cost sheet भेज सकूँ, और आप इसे देखने कब आ रहे हैं?"),
    ("caller", "हाँ, वो तो ठीक है, लेकिन Green County का भी बोल सकते हो, मेरे को थोड़ा बता सकते हो।"),
    ("agent", "Pramukh Green County में आपको 27,000 वर्ग फुट का विशाल क्लबहाउस मिल जाता है सर। क्या यह नंबर WhatsApp पर है जिस पर मैं आपको इसका लोकेशन, फ्लोर प्लान और cost sheet भेज सकूँ?"),
    ("caller", "हां जी हां जी और यहां पे नियर बाय चीजें बता सकते हैं और उसकी एड्रेस भी बता दो मेरे को"),
    ("agent", "Pramukh Green County Near Gunjan area, Chharwada, Vapi में स्थित है सर। क्या यह नंबर WhatsApp पर है जिस पर मैं आपको इसका लोकेशन भेज सकूँ?"),
    ("caller", "हाँ, मुझे वो भेज देना। इसके अलावा थोड़ा यहां के एमेनिटीज़ के बारे में बता सकते हो?"),
    ("agent", "Pramukh Green County में party lawn और indoor pool जैसी amenities हैं सर। क्या मैं आपको इसका लोकेशन WhatsApp पर भेज दूँ सर?"),
    ("caller", "इसके अलावा आपके पास Vivan भी है तो Vivan के बारे में भी बता दो।"),
    ("agent", "Pramukh Vivan में 1.5 BHK और 2 BHK अपार्टमेंट्स मिलते हैं सर। क्या मैं आपको इसका लोकेशन, फ्लोर प्लान और cost sheet WhatsApp पर भेज दूँ सर?"),
    ("caller", "हाँ, मेरे को भेज देना और आपने विमान का क्या बोला? सोल्ड आउट है।"),
    ("agent", "जी सर, Pramukh Vivan के 2 BHK सोल्ड आउट हो चुके हैं।"),
    ("caller", "बस शायद Blue Stone थोड़ा सा बचा हुआ है। Blue Stone के बारे में थोड़ा बता दो।"),
    ("agent", "Pramukh Blue Stone में 2 BHK और 3 BHK अपार्टमेंट्स मिल जाते हैं सर। क्या मैं आपको इसका लोकेशन इसी नंबर पर WhatsApp पर भेज दूँ सर?"),
    ("caller", "Silver Stone नहीं, Blue Stone बोल रहा हूँ। हाँ, वो भेज देना मेरे को। और कितनी बिल्डिंग है इसमें?"),
    ("agent", "Pramukh Blue Stone में कुल 5 बिल्डिंग्स हैं सर।"),
    ("caller", "हाँ हाँ, मुझे व्हाट्सएप पर भेज देना। और फाइनली मुझे बस एक Aristo की लोकेशन बता दो"),
    ("agent", "Pramukh Aristo नियर डभेल चेक पोस्ट, चाला, वापी में स्थित है सर। क्या यह नंबर WhatsApp पर है जिस पर मैं आपको इसका लोकेशन भेज सकूँ?"),
    ("caller", "हाँ, मुझे भेज देना और कार्पेट एरिया के बारे में बता दो।"),
    ("agent", "सर, Pramukh Aristo में 3 BHK का कारपेट एरिया लगभग 1,392 वर्ग फुट है।"),
    ("caller", "और जो आपका Swarit है, उसकी क्या एड्रेस है?"),
    ("agent", "Pramukh Swarit का एड्रेस New Chala, Vapi में, Pramukh Vivan के पीछे है सर। क्या यह नंबर WhatsApp पर है जिस पर मैं आपको इसका लोकेशन भेज सकूँ?"),
    ("caller", "मुझे Green County का भेज दो।"),
    ("agent", "बिल्कुल सर, Pramukh Green County का लोकेशन मैं आपको भेज देता हूँ। क्या यह नंबर WhatsApp पर है सर?"),
    ("caller", "हां जी हां जी। नहीं, व्हाट्सएप पे मेरा अलग नंबर है एक्चुअली।"),
    ("agent", "कोई बात नहीं सर, आप मुझे वो WhatsApp नंबर बता दीजिए?"),
    ("caller", "मेरा व्हाट्सएप नंबर अलग है, यह वाला नहीं है। नौ आठ सात छह पांच चार तीन दो एक शून्य"),
]


def test_call_1052_replay(kb):
    s = st.ShareState()
    for who, line in CALL_1052:
        (st.observe_agent if who == "agent" else st.observe_caller)(kb, s, line)
    assert s.agreed == [SILVER, GREEN, VIVAN, BLUE, ARISTO]
    assert s.topic_id == GREEN
    assert s.number == st.NUMBER_CAPTURED and s.alt_number == "9876543210"
    assert s.number_asked >= 5
    lines = "\n".join(st.band(kb, s, SILVER))
    assert "Pramukh Green County (the caller brought it up last)" in lines
    assert "ending in 3210" in lines
