"""Language-switch re-delivery (platform rule, every agent).

A switch request that carries a real ask ("हिंदी में बताओ लेस कॉल के बारे में"),
or that names the language the call is ALREADY in ("say that in Hindi"
mid-Hindi-call), must reach the LLM so the content gets delivered in the
target language. The canned "sure, let's speak X" acknowledgment is reserved
for a bare switch request that actually changes the language.

Regression source: live call 2026-07-28 — the agent answered
"जी बिल्कुल, हम हिंदी में बात करते हैं।" three turns straight while the caller
kept asking for the SAME content in Hindi.
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice.conversation import ConversationRouter, detect_intent

HI_ACK = "जी बिल्कुल, हम हिंदी में बात करते हैं।"
EN_ACK = "Sure, let's continue in English."
GU_ACK = "જી બિલકુલ, ચાલો ગુજરાતીમાં વાત કરીએ."

# The three caller turns from the looping live call.
TRANSCRIPT_TURNS = [
    "हिंदी में बताओ लेस कॉल के बारे में हिंदी में बताओ",
    "एक मुझे ना ये सेम चीज़ हिंदी में बताओ।",
    "tell me the same thing about in hindi.",
]


@pytest.fixture(autouse=True)
def _stub(stub_llm):
    """Offline fixed-reply LLM for every test in this module."""


def make_router(**over) -> ConversationRouter:
    d = {
        "agent_id": "t_langswitch",
        "version": "1.0.0",
        "identity": {"agent_name": "Maya", "company_name": "LesCall"},
        "languages": {"supported": ["en", "hi", "gu"], "default": "en",
                      "tts_voices": {}},
        "call_flow": {
            "stages": [
                {"id": "S1", "order": 0, "goal": "one",
                 "turns_until_advance": 8},
            ],
        },
        "calendar": {"mock_slots": []},
        "response_bank": {
            "opening_greeting": "Hello! This is Maya from LesCall.",
            "lang_switch_responses": {
                "hi": HI_ACK, "en": EN_ACK, "gu": GU_ACK,
            },
        },
    }
    d.update(over)
    return ConversationRouter(agent_def=AgentDefinition.from_dict(d),
                              default_language="en")


def drive(router: ConversationRouter, text: str) -> str:
    async def _one():
        out = []
        async for res in router.process_stream(text):
            out.append(res.text)
        return " ".join(t for t in out if t)

    return asyncio.run(_one())


# ---------------------------------------------------------------------------
# intent classification — every transcript turn is a language_switch
# ---------------------------------------------------------------------------

def test_transcript_turns_classify_as_language_switch():
    for turn in TRANSCRIPT_TURNS:
        assert detect_intent(turn, "S1") == "language_switch", turn


# ---------------------------------------------------------------------------
# content detector
# ---------------------------------------------------------------------------

def test_content_bearing_switches_detected():
    r = make_router()
    for turn in TRANSCRIPT_TURNS:
        assert r._lang_switch_has_content(turn) is True, turn
    assert r._lang_switch_has_content(
        "tell me about pricing in hindi") is True


def test_bare_switches_not_content_bearing():
    r = make_router()
    for turn in [
        "hindi me baat karo",
        "can you speak english please",
        "switch to gujarati",
        "हिंदी में बोलो",
        "मुझे हिंदी नहीं आती",
        "i don't know english",
    ]:
        assert r._lang_switch_has_content(turn) is False, turn


# ---------------------------------------------------------------------------
# routing behaviour
# ---------------------------------------------------------------------------

def test_bare_switch_yields_canned_ack(stub_llm):
    r = make_router()
    r.state.language = "en"
    resp = drive(r, "please speak in hindi")
    assert resp == HI_ACK
    assert r.state.language == "hi"
    # The ack must never become the repeat/replay target.
    assert r.last_agent_response != HI_ACK


def test_content_bearing_switch_goes_to_llm(stub_llm):
    r = make_router()
    r.state.language = "en"
    resp = drive(r, "हिंदी में बताओ लेस कॉल के बारे में")
    assert resp == stub_llm          # LLM answered, not the ack
    assert HI_ACK not in resp
    assert r.state.language == "hi"  # switch itself still applied


def test_same_language_switch_goes_to_llm(stub_llm):
    r = make_router()
    r.state.language = "hi"
    resp = drive(r, "हिंदी में बोलो")  # bare, but already in Hindi
    assert resp == stub_llm
    assert HI_ACK not in resp


def test_transcript_never_repeats_ack(stub_llm):
    """The full looping transcript: the ack must not play even once —
    the call was already in Hindi on every turn."""
    r = make_router()
    r.state.language = "hi"
    r.last_agent_response = "LesCall builds voice AI agents for your business."
    for turn in TRANSCRIPT_TURNS:
        resp = drive(r, turn)
        assert HI_ACK not in resp, turn
        assert resp == stub_llm, turn


def test_directive_armed_and_consumed(stub_llm):
    r = make_router()
    r.state.language = "hi"
    r._arm_lang_redeliver_directive("hi")
    assert "Hindi" in r._turn_lang_directive
    drive(r, "हिंदी में बताओ लेस कॉल के बारे में")
    # consumed by _call_llm_stream (and reset at turn start)
    assert r._turn_lang_directive is None


# ---------------------------------------------------------------------------
# repeat detection — "same thing" family
# ---------------------------------------------------------------------------

def test_same_thing_counts_as_repeat_request():
    r = make_router()
    assert r._is_repeat_request("tell me the same thing in hindi") is True
    assert r._is_repeat_request("ये सेम चीज़ हिंदी में बताओ") is True
    assert r._is_repeat_request("वही बात हिंदी में बोलो") is True
