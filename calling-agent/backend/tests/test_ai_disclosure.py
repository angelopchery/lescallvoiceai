"""Every agent says it is an AI — only when explicitly asked (RUNBOOK §66)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice.ai_disclosure import admits_ai, asks_if_ai, default_disclosure
from app.voice.conversation import ConversationRouter
from test_call_termination import drive, make_router

RAVI_OUT = Path(__file__).parent / "fixtures" / "ravi_outbound_2026_09_24.yaml"


@pytest.fixture(autouse=True)
def _offline(stub_llm):
    """No network LLM — see conftest."""


# ── the detector ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Are you a robot?", "are you an AI", "are you a real person?",
    "is this a recording", "Am I talking to a bot?",
    "is this an ai that i'm talking to or is this a real person?",   # corpus
    "or are you a real person?",                                      # corpus
    "human or robot?", "क्या आप AI हो?", "आप रोबोट हो क्या", "आप एआई हो",
    "रोबोट हो क्या", "आप इंसान हो या मशीन?", "इंसान हो या रोबोट",
    "ये रिकॉर्डिंग है क्या", "क्या ये AI है", "मशीन से बात हो रही है क्या",
    "kya aap robot ho", "aap insaan ho ya machine", "ai ho kya",
    "તમે રોબોટ છો?", "તમે માણસ છો?", "આ રેકોર્ડિંગ છે?",
])
def test_explicit_questions(text):
    assert asks_if_ai(text)


@pytest.mark.parametrize("text", [
    "आपकी मशीन है क्या?", "हम CNC मशीन बेचते हैं", "are you available tomorrow",
    "is this the real estate company", "who is this?", "कौन बोल रहा है?",
    "I am a real estate agent", "Are you a person from Pramukh?",
    "human resources", "मुझे इंसान से बात करनी है", "are you both coming",
    "robotics company में काम करता हूँ", "Is this about the AI course?", "",
])
def test_not_questions_about_the_agent(text):
    assert not asks_if_ai(text)


def test_admits_and_denies():
    assert admits_ai("हाँ, मैं DhronAI का AI रिक्रूटमेंट असिस्टेंट हूँ।")
    assert admits_ai("Yes, I'm an AI voice assistant.")
    assert not admits_ai("No, I'm a real person.")
    assert not admits_ai("मैं रोबोट नहीं हूँ, असली इंसान हूँ")
    assert not admits_ai("I'm Ravi from Pramukh Group.")


def test_default_line_per_language():
    assert default_disclosure("en", "Ravi", "Pramukh Group") == (
        "Yes, I'm Ravi, an AI voice assistant calling on behalf of Pramukh Group.")
    assert "AI voice assistant" in default_disclosure("hi", "Ravi", "Pramukh Group")
    assert "AI voice assistant" in default_disclosure("gu", "Ravi", "Pramukh Group")
    assert "Ravi" not in default_disclosure("en", "", "Pramukh Group")
    # Unsupported language → English, never a crash.
    assert default_disclosure("ta", "Ravi", "X").startswith("Yes")


# ── the router ──────────────────────────────────────────────────────────────

def _ravi() -> ConversationRouter:
    r = ConversationRouter(agent_def=AgentDefinition.from_yaml(RAVI_OUT),
                           default_language="hi")
    r.state.language = "hi"
    r.outbound_contact_name = "Angelo"
    r._opening_ts = time.monotonic() - 60          # past the connect window
    return r


def test_ravi_admits_and_carries_on_with_the_question():
    r = _ravi()
    r.last_agent_response = "और आप कहाँ से बात कर रहे हैं — Vapi में ही हैं?"
    r.memory.add_assistant(r.last_agent_response, language="hi")
    resp = drive(r, "एक मिनट, क्या आप AI हो?")
    assert "AI voice assistant" in resp
    assert "Ravi" in resp or "रवि" in resp
    assert "कहाँ से बात कर रहे हैं" in resp          # the call carries on


def test_not_asked_no_disclosure():
    r = _ravi()
    r.last_agent_response = "और आप कहाँ से बात कर रहे हैं?"
    assert "AI voice assistant" not in drive(r, "मैं Vapi से बोल रहा हूँ")


def test_owner_playbook_answer_that_admits_is_kept_verbatim():
    owner = "Yes, I'm an AI recruitment assistant working with DhronAI."
    r = make_router(response_bank={
        "opening_greeting": "hi",
        "objection_playbook": [{"id": "ai_disclosure",
                                "patterns": ["are you a bot"],
                                "answer": {"en": owner}}]})
    r.last_agent_response = "What role are you applying for?"
    assert drive(r, "wait, are you a bot?") == owner


def test_owner_playbook_answer_that_denies_is_overridden():
    r = make_router(response_bank={
        "opening_greeting": "hi",
        "objection_playbook": [{"id": "human", "patterns": ["are you a robot"],
                                "answer": {"en": "No, I'm a real person!"}}]})
    r.last_agent_response = "Shall we continue?"
    resp = drive(r, "are you a robot?")
    assert "AI voice assistant" in resp and "real person" not in resp


def test_authored_disclosure_line_wins_over_the_platform_line():
    r = make_router(response_bank={
        "opening_greeting": "hi",
        "ai_disclosure_responses": {"en": "I'm Asha, TestCo's AI assistant."}})
    r.last_agent_response = "Which city are you in?"
    r.memory.add_assistant(r.last_agent_response, language="en")
    resp = drive(r, "is this a recording?")
    assert resp.startswith("I'm Asha, TestCo's AI assistant.")
    assert resp.endswith("Which city are you in?")


def test_identity_question_without_ai_is_unchanged():
    r = make_router()
    r.last_agent_response = "Which city are you in?"
    assert "AI voice assistant" not in drive(r, "who is this?")
