"""
RUNBOOK §63 — machine speech on the caller's side, and the mid-call hello.

Call 919426600929_20260928_094925 (Craft Idea / Nisha, jai_mistry-2-outbound
rev 6): an iPhone Call Screening answered, the engine treated it as the
customer, said "You're welcome!" to its "thanks, please stay on the line",
and when the real person picked up and said "hello" twice it asked the next
two questions of its script. The replay below drives the engine with the
exact transcript of that call against a frozen snapshot of the agent.

The classifier half is checked against lines taken verbatim from the
recordings corpus — including the human lines that share words with a
machine ("no, not available right now, call me 10 minutes after").
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice import machine_line as ml
from app.voice.conversation import ConversationRouter

FIXTURE = (Path(__file__).resolve().parent / "fixtures"
           / "craftidea_nisha_outbound_2026_09_28.yaml")

OPENING = ("नमस्ते सर, मैं निशा बोल रही हूँ Craft Idea से। एक छोटा सा सवाल था — "
           "ज़्यादातर नए customers आपको आजकल कैसे मिलते हैं?")


@pytest.fixture(autouse=True)
def _no_network_llm(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


def drive(router: ConversationRouter, text: str) -> str:
    async def _one():
        out = []
        async for res in router.process_stream(text):
            out.append(res.text)
        return " ".join(t for t in out if t)
    return asyncio.run(_one())


def nisha() -> ConversationRouter:
    r = ConversationRouter(agent_def=AgentDefinition.from_yaml(FIXTURE),
                           default_language="hi")
    r.set_call_context(direction="outbound", contact_name="Rajiv C Shah",
                       contact_phone="919426600929", contact_gender="male")
    r.memory.add_assistant(OPENING, language="hi")
    r._store_last_response(OPENING)
    r._opening_ts = time.monotonic()
    return r


# ── 1. the classifier, on corpus lines ─────────────────────────────────────

@pytest.mark.parametrize("line,kind", [
    ("hi, if you record your name and reason for calling, i'll", ml.SCREENER),
    ("i'll see if this person is available.", ml.SCREENER),
    ("record your name, reason for calling. i'll see if this person is available.", ml.SCREENER),
    ("Hi, the person you're calling is using a screening service from Google "
     "and will get a copy of this conversation. Go ahead and say your name "
     "and why you're calling.", ml.SCREENER),
    ("please stay on the line.", ml.HOLD),
    ("the person you are speaking with has put your call on hold. please stay on the line.", ml.HOLD),
    ("उसने आपकी कॉल को होल्ड पर रखा है। कृपया लाइन पर बने रहें।", ml.HOLD),
    ("your call has been forwarded to voicemail. the person you're trying to "
     "reach is not available. at the tone, please record your message.", ml.VOICEMAIL),
    ("forward it to voicemail.", ml.VOICEMAIL),
    ("the person you're trying to reach is not available.", ml.VOICEMAIL),
    ("trying to reach is not available.", ml.VOICEMAIL),
    ("please leave your message after the beep.", ml.VOICEMAIL),
    ("when you have finished recording, you may haang up.", ml.VOICEMAIL),
    ("the number you are calling is currently switched off.", ml.UNREACHABLE),
    ("आप जिस नंबर पर कॉल कर रहे हैं वह अभी स्विच ऑफ है", ml.UNREACHABLE),
])
def test_machine_lines_are_recognised(line, kind):
    assert ml.classify(line) == kind


@pytest.mark.parametrize("line", [
    "no, not available right now. call me 10 minutes after, okay?",
    "i'm interested in 3 bhks, they're not available.",
    "my phone was switched off yesterday",
    "hello", "thanks", "please", "okay okay",
    "calling? from where you are calling?",
    "i am not interested in this.",
    "हाँ बोलिए, कौन बोल रहा है?",
    "just leave a message on whatsapp",
])
def test_people_are_not_machines(line):
    assert ml.classify(line) is None


def test_split_keeps_the_persons_words():
    kind, human = ml.split("Please stay on the line. Hello?")
    assert kind == ml.HOLD and human == "Hello?"


# ── 2. the call, replayed ───────────────────────────────────────────────────

def test_call_919426600929_replayed():
    r = nisha()

    # The screener prompt gets identity + purpose — once — in English, and
    # nothing about it is kept as an answer, language evidence or history.
    said = drive(r, "hi, if you record your name and reason for calling, "
                    "i'll i'll see if this person is available.")
    assert "Nisha" in said and "Craft Idea" in said and "Rajiv" in said
    assert r.state.language == "hi"
    assert r.memory.qa_tracker._answered == {}
    assert r.state_mgr.state.get("customer_channel") is None
    assert r.awaiting_machine_handoff()
    assert not any("record your name" in (t.get("content") or "")
                   for t in r.memory.get_history_for_llm())

    # The screener's "thanks, please stay on the line", split by STT.
    assert drive(r, "thanks please") == ""
    assert drive(r, "please stay on the line.") == ""

    # The person picks up. They have heard nothing — open the call again,
    # in the call's language, with the owner's own second line.
    said = drive(r, "hello")
    assert "निशा" in said and "customers" in said
    assert not r.awaiting_machine_handoff() and r._human_spoke

    # A second hello is a presence check: say we're here, ask the SAME
    # question again — never the next one.
    said = drive(r, "hello")
    assert "आवाज़ आ रही है" in said
    assert "customers" in said and "business" in said
    assert "किस चीज़" not in said  # not the business_type question

    # "okay okay" answers nothing open-ended.
    drive(r, "okay okay")
    assert "customer_channel" not in r.memory.qa_tracker._answered


def test_screener_silence_is_not_dead_air():
    r = nisha()
    drive(r, "record your name and reason for calling.")
    assert r.awaiting_machine_handoff()
    r._machine_wait_since -= 3600
    assert not r.awaiting_machine_handoff()


def test_voicemail_closes_in_silence_by_default():
    r = nisha()
    said = drive(r, "your call has been forwarded to voicemail. the person "
                    "you're trying to reach is not available.")
    assert said == ""
    assert r.hangup_pending and r._hangup_hard
    assert r.state_mgr.extra_flags["machine_answered"] == ml.VOICEMAIL
    # the mailbox keeps talking; nothing cancels the close
    assert drive(r, "please record your message.") == ""
    assert r.hangup_pending


def test_voicemail_message_waits_for_the_tone():
    r = nisha()
    r._def.call_flow.connect["voicemail"] = {
        "action": "leave_message",
        "message": {"hi": "नमस्ते, Craft Idea से निशा। मैं बाद में फिर कॉल करूँगी।"}}
    assert drive(r, "forwarded to voicemail. the person you're trying to "
                    "reach is not available.") == ""
    assert not r.hangup_pending
    said = drive(r, "at the tone, please record your message.")
    assert "Craft Idea" in said and r.hangup_pending and r._hangup_hard


def test_voicemail_wording_mid_call_is_a_person():
    r = nisha()
    drive(r, "word of mouth se hi aate hain")
    assert r._human_spoke
    drive(r, "just leave a message after the beep on my other number")
    assert not r.hangup_pending


def test_hold_mid_call_is_silence_then_resume():
    r = nisha()
    drive(r, "referral se aate hain")
    assert drive(r, "the person you are speaking with has put your call on "
                    "hold. please stay on the line.") == ""
    assert not r.hangup_pending


# ── 3. platform rules the call exposed ─────────────────────────────────────

def test_gratitude_shortcut_needs_gratitude_only():
    r = nisha()
    assert r._is_gratitude_only("thank you so much ji")
    assert not r._is_gratitude_only("thanks please")
    assert not r._is_gratitude_only("thank you, 2BHK works")


def test_question_turns_and_bare_acks_answer_nothing():
    r = nisha()
    assert r._is_question_only("calling? from where you are calling?")
    assert not r._is_question_only("word of mouth, mostly. why?")
    assert r._is_bare_ack("okay okay")
    assert not r._is_bare_ack("okay, mostly referrals")


def test_last_question_keeps_abbreviations_whole():
    r = nisha()
    r.memory.add_assistant("Right, so how do most new customers usually find "
                           "you Mr. Rajiv — is it mostly word of mouth?",
                           language="en")
    q = r._last_question_sentence()
    assert q.startswith("Right, so how do") and q.endswith("word of mouth?")


def test_arabic_script_is_never_spoken():
    r = nisha()
    out = r._finalize_spoken("એટલે word of mouth થી જ زیادہ customers આવે છે?")
    assert "زیادہ" not in out and "customers" in out
