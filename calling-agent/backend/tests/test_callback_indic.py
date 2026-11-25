"""Callback requests in the caller's own script (RUNBOOK §77).

Proves: the Devanagari / Gujarati REQUEST shapes are callbacks and the
reporting / describing / refusing shapes are not; a Hindi caller's
"5 मिनट में कॉल बैक" is confirmed in Devanagari and closes the call; a vague
"बाद में कॉल कर सकते हो?" gets ONE Devanagari clarification (the agent's own
authored busy line for a busy brush-off, when it has one) and the turn after
it carries the ━━ CALLBACK REQUESTED ━━ band until the time is settled; a
day answer confirms in Devanagari; English behaviour is unchanged.

Offline: stub LLM, no live agent YAML. The corpus lists are frozen copies of
the 2026-10-12 sweep over recordings/ (11,621 caller lines): before the fix
the detector had 0 Devanagari hits; of the 27 Devanagari hits after, 25 are
requests to be called or deferrals to a later call, one is the caller saying
they will call ("बाद में कॉल करता हूँ") and one narrates such a request
("मैंने एक को बोला मुझे कॉल बैक करो") — both tolerated, both end as a
polite deferral. Nothing Latin was lost.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice.conversation import ConversationRouter


@pytest.fixture(autouse=True)
def _offline(stub_llm):
    """Turns that fall through to the LLM run against the offline stub."""


LATIN = re.compile(r"[A-Za-z]")
BUSY_HI = "बिल्कुल — कब कॉल करूँ तो ठीक रहेगा? मैं तभी कॉल करूँगा।"


def make_router(lang: str = "hi", busy_line: bool = True,
                clarify: bool = True) -> ConversationRouter:
    ob = {"identity_confirm_line": {"hi": "नमस्ते! क्या मेरी बात {contact_name} जी से हो रही है?",
                                    "en": "Am I speaking with {contact_name}?"}}
    if busy_line:
        ob["busy_response"] = {"hi": BUSY_HI,
                               "en": "Of course — when would be a better time?"}
    d = {
        "agent_id": "t_cb_indic", "version": "1.0.0",
        "identity": {"agent_name": "Dhaval", "company_name": "TestCo",
                     "pronouns": "he"},
        "languages": {"supported": ["hi", "en"], "default": lang,
                      "tts_voices": {}},
        "call_flow": {
            "stages": [
                {"id": "S1", "order": 0, "goal": "one", "turns_until_advance": 8},
                {"id": "S2", "order": 1, "goal": "two", "turns_until_advance": 99},
            ],
            "callback_policy": {"enabled": True, "capture_exact_time": True},
        },
        "follow_up": {"clarify_commitments": {"enabled": clarify}},
        "calendar": {"mock_slots": []},
        "response_bank": {"opening_greeting": "hi"},
        "call_direction": "outbound",
        "outbound": ob,
    }
    agent = AgentDefinition.from_dict(d)
    r = ConversationRouter(agent_def=agent, default_language=lang)
    r.call_direction = "outbound"
    r.state.language = lang
    r._available_calendar_slots = lambda: []
    return r


def drive(router: ConversationRouter, text: str) -> str:
    async def _one():
        out = []
        async for res in router.process_stream(text):
            out.append(res.text)
        return " ".join(t for t in out if t)
    return asyncio.run(_one())


# ── the detector ────────────────────────────────────────────────────────────
# The three live lines (calls 1061, 1073) first; the rest are the 2026-10-12
# corpus hits, verbatim. Every one is a request to be called or a deferral
# to a later call.
POSITIVE = [
    "तो आप मुझे पांच मिनट में कॉल बैक कर सकते हैं।",
    "क्या आप मुझे बाद में कॉल कर सकते हो?",
    "मैं आपको बाद में कॉल करके बताऊँगी, आप मुझे एक बार कल ग्यारह बजे तक कॉल कर दीजिए।",
    "हम्म, आप मुझे परसों कॉल कर दीजिए।",
    "आप मुझे मंडे को कॉल कर सकते हो।",
    "क्या आपको आप मुझे 10 मिनट बाद कॉल कर सकते हो?",
    "मुझे आप आराम से 10 मिनट बाद कॉल करना।",
    "थोड़ी देर बाद कॉल कीजिए, यह कन्फर्म करने के बाद भी आप पूछ रहे हैं",
    "हाँ, मैं अभी काम में हूँ। मुझे कल सुबह ग्यारह बजे कॉल कीजिए, ठीक है?",
    "नहीं नहीं नहीं भाई 20 मिनट बाद 20 मिनट बाद ही कॉल करना ठीक है चलो बाय",
    "हाँ हाँ बाद में तो आप कॉल कर सकते हो मुझे",
    "रिमाइंडर कॉल कर देना बारह बजे के आसपास।",
    "आप मुझे एक घंटे बाद कॉल करना, मैं उनको बता दूंगा आपको कॉल करने के लिए।",
    "कॉल करना आप मुझे, उसमें डिसाइड कर लेंगे हम।",
    "क्या कल आप मुझे कॉल बैक कर सकती हैं अभी मैं बिजी हूँ",
    "हाँ, कल दोपहर को तीन बजे कॉल कर दीजिए।",
    "मेरे पास। मैं कल फ्री हूँ, कल कॉल कर सकते हो मेरे को?",
    "और बाद में बात करते हैं, थैंक यू।",
    "कल सुबह बात करते हैं",
    "મને કાલે કૉલ કરજો",
    "પછી કૉલ કરશો?",
]
# Reporting a call, describing calls, refusing one, a past call, an answer
# to a question — none of these asks to be called.
NEGATIVE = [
    "नहीं नहीं मुझे मत करना कॉल, मैं आपको कॉल कर लूँगा खुद ही।",
    "आप मुझे मत कॉल करो आप नहीं कर सकते मेरी मदद थैंक यू",
    "कॉल मत करना मुझे",
    "अभी तो हमारी इंटरनल टीम ही सेल्स कॉल करती है।",
    "क्यों कॉल किया आपने?",
    "मुझे आपका कॉल आया था",
    "कॉल आया था आपका",
    "मैं अभी तो vapi में ही हूँ।",
    "और हम मतलब खुद से ही कॉल करके सब कुछ कर सकते हैं।",
    "फिर बाद में जो वापस कॉल करेगा।",
    "नहीं, थोड़ी देर बाद कॉल की थी।",
    "इन्वेस्टमेंट है।",
    "कल बात ये है कि मुझे 3 bhk चाहिए",
    "कल शाम को बात हुई थी आपसे",
    "मुझे कल कॉल करना है ऑफिस में",
]


@pytest.mark.parametrize("text", POSITIVE)
def test_indic_request_is_a_callback(text):
    assert make_router()._detect_callback_request(text) is True


@pytest.mark.parametrize("text", NEGATIVE)
def test_indic_non_request_is_not_a_callback(text):
    assert make_router()._detect_callback_request(text) is False


def test_english_detection_unchanged():
    r = make_router(lang="en")
    assert r._detect_callback_request("can you call me back in 5 minutes") is True
    assert r._detect_callback_request("i got a call from this number 20 minutes back") is False


# ── the spoken lines ────────────────────────────────────────────────────────
def test_precise_hindi_request_is_confirmed_in_devanagari_and_closes():
    r = make_router()
    reply = drive(r, "तो आप मुझे पांच मिनट में कॉल बैक कर सकते हैं।")
    assert "5 मिनट में" in reply
    assert not LATIN.search(reply), reply
    assert r.state_mgr.extra_flags["callback_requested"] is True
    assert r.state_mgr.extra_flags["callback_offset_min"] == 5
    assert r.call_outcome == "callback_requested"
    assert r.hangup_pending is True and r.state.is_closing is True


def test_vague_hindi_request_gets_one_devanagari_question_then_confirms_the_day():
    r = make_router()
    q = drive(r, "क्या आप मुझे बाद में कॉल कर सकते हो?")
    assert "?" in q and "कॉल" in q and not LATIN.search(q), q
    assert r._clarify_asked is True and r.state.is_closing is False
    assert r.state_mgr.extra_flags["callback_requested"] is True
    confirm = drive(r, "कल सुबह")
    assert "कल" in confirm and "सुबह" in confirm and not LATIN.search(confirm), confirm
    assert r.hangup_pending is True


def test_busy_brush_off_uses_the_agents_authored_busy_line():
    r = make_router(busy_line=True)
    assert drive(r, "अभी busy हूँ, बाद में कॉल कीजिए") == BUSY_HI
    r2 = make_router(busy_line=False)
    reply = drive(r2, "अभी busy हूँ, बाद में कॉल कीजिए")
    assert "कितने मिनट" in reply and not LATIN.search(reply), reply


def test_callback_policy_fallback_is_hindi_when_clarify_is_off():
    # The v2 policy (clarifier off) handles deferrals classified `objection`;
    # its built-in confirmation was English on every Hindi call.
    r = make_router(clarify=False)
    r.state.stage = "S2"      # the objection branch never runs in GREETING
    reply = drive(r, "क्या आप मुझे बाद में कॉल कर सकते हो?")
    assert "कॉल करेंगे" in reply and not LATIN.search(reply), reply
    assert r.state_mgr.extra_flags["callback_requested"] is True
    assert r.hangup_pending is True


def test_english_spoken_lines_unchanged():
    r = make_router(lang="en")
    reply = drive(r, "can you call me back in 5 minutes")
    assert reply == ("Perfect — I'll call you back in about 5 minutes. "
                     "Thank you, and have a great day!")


# ── the band ────────────────────────────────────────────────────────────────
def test_band_holds_the_model_to_the_callback_until_the_time_is_settled():
    r = make_router()
    assert r._callback_pending_band() == []
    drive(r, "क्या आप मुझे बाद में कॉल कर सकते हो?")        # the one question
    assert "━━ CALLBACK REQUESTED ━━" in r._build_system_prompt()
    drive(r, "हाँ ठीक है")                                      # unparseable answer → model
    band = "\n".join(r._callback_pending_band())
    assert "do NOT ask a qualification question" in band
    assert "━━ CALLBACK REQUESTED ━━" in r._build_system_prompt()
    # The caller repeats the request (the agent "missed" it): no second
    # question — confirmed in Devanagari and closed.
    confirm = drive(r, "कल दोपहर को कॉल कर दीजिए")
    assert "कल दोपहर" in confirm and not LATIN.search(confirm), confirm
    assert r.state.is_closing is True
    assert r._callback_pending_band() == []


def test_spoken_day_time_localises_only_hi_and_gu():
    f = ConversationRouter._spoken_day_time
    assert f("Tomorrow", "evening", "hi") == ("कल", "शाम")
    assert f("Monday", "11 baje", "hi") == ("सोमवार", "11 बजे")
    assert f("Tomorrow", "evening", "gu") == ("કાલે", "સાંજે")
    assert f("Tomorrow", "3 pm", "en") == ("Tomorrow", "3 pm")
    assert ConversationRouter._offset_phrase(10, "hi") == "10 मिनट में"
    assert ConversationRouter._offset_phrase(60, "hi") == "1 घंटे में"
    assert ConversationRouter._offset_phrase(10, "en") == "in about 10 minutes"
