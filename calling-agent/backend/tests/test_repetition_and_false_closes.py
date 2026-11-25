"""Masters-Naukri call review (2026-09-16) — five platform defects.

Every case below is taken from a real shubhangi call on 2026-09-15/16 and is
fixed in the ENGINE, so it applies to every existing agent and every agent
Copilot builds next — none of it is per-agent YAML.

1. TTS cache poisoning — Bulbul appended speech to a short line ("Am I
   speaking to Piyush Gupta" + "or"; "Am I speaking to Yogesh Kumar" + "40,
   the man is Kumar"), the canned-audio cache stored it, and those contacts
   heard it on EVERY call. Audio must be plausible for its text before it is
   cached, and a stored entry that is not gets evicted on read.
2. Farewell false positive — "Signify, Philips Lighting" transcribed as "bye
   philips lightning" triggered "shall I end the call here?" mid-answer.
3. Callback false positive — "I got a call from this number 20 minutes
   before, what's the process?" was read as a callback request; the agent
   said "I'll call you back in about 20 minutes" and hung up on the caller.
4. Transcript pollution — replies cut by a continuation merge before a frame
   reached the wire were still written to the call transcript, so one
   question read back as six.
5. Asking ahead of the flow, then asking again — the next-question directive
   used global slot priority, so the agent asked a LATER stage's question
   early and that stage's script then asked it word for word.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from conftest import AGENTS_DIR
from app.agent_definition.schema import AgentDefinition
from app.voice import prompt_blocks as pb
from app.voice.conversation import (
    ConversationRouter,
    _farewell_is_terminal,
    detect_intent,
)
from app.voice.state_manager import ConversationStateManager
from app.voice import tts_sarvam as ts


@pytest.fixture(autouse=True)
def _no_network_llm(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


# ───────────────────────── 1. TTS cache poisoning ──────────────────────────
#
# Durations below are MEASURED, not invented: 88 live Bulbul syntheses on
# 2026-09-16 across en/hi/gu, speakers shubh + simran, paces 1.0-1.3.

@pytest.mark.parametrize("text,seconds", [
    ("Am I speaking to Piyush Gupta?", 1.79),      # slowest clean sample
    ("What is your current location?", 1.62),
    ("Alright then, shall I end the call here?", 1.96),
    ("Okay.", 0.68),                               # short lines pay a fixed pad
    ("ठीक है, तो फिर मैं कॉल कट कर दूँ?", 2.30),
    ("સારું, તો પછી હું કૉલ કટ કરી દઉં?", 3.35),   # worst legitimate ratio
    ("Hello! This is Vishal Patel from Pramukh Group. "
     "How may I help you today?", 3.93),
])
def test_real_synthesis_durations_are_never_flagged(text, seconds):
    pcm = int(seconds * 2 * 8000)
    assert ts._duration_implausible(text, pcm, 8000) is None


@pytest.mark.parametrize("text,seconds,heard", [
    # Cached live and replayed to that contact on every call.
    ("Am I speaking to Piyush Gupta?", 2.686, "Am I speaking to Piyush Gupta or"),
    ("Am I speaking to Yogesh Kumar?", 3.03,
     "Am I speaking to Yogesh Kumar 40, the man is Kumar?"),
])
def test_bulbul_hallucinations_are_flagged(text, seconds, heard):
    pcm = int(seconds * 2 * 8000)
    ratio = ts._duration_implausible(text, pcm, 8000)
    assert ratio is not None, f"would have been cached and replayed as {heard!r}"
    assert ratio > 1.0


def test_sixteen_khz_stream_labelled_eight_khz_is_caught():
    """`_check_stream_rate` can only read a RIFF header and the stream is
    headerless, so it has never fired. Double-length audio lands here."""
    text = "What is your current location?"
    clean = int(1.3 * 2 * 8000)
    assert ts._duration_implausible(text, clean, 8000) is None
    assert ts._duration_implausible(text, clean * 2, 8000) is not None


class _FakeSink:
    """Enough of AudioSink for speak_cached's replay path."""

    def __init__(self):
        self.written = bytearray()

    async def start(self):
        pass

    async def write_frame(self, pcm):
        self.written += pcm

    async def flush(self):
        pass


def _cache_tts(sink=None) -> ts.SarvamTTS:
    tts = ts.SarvamTTS.__new__(ts.SarvamTTS)
    tts._model, tts._speaker, tts._pace = "bulbul:v3", "simran", 1.15
    tts._language, tts._sample_rate = "en", 8000
    tts._sink = sink
    tts._cancelled = False
    tts._playing = False
    tts._capture_buf = None
    tts._last_speak_clean = False
    tts._stream_truncated = False
    return tts


def test_a_healthy_cache_entry_still_replays(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(ts, "TTS_CACHE_DIR", Path(tmp))
        sink = _FakeSink()
        tts = _cache_tts(sink)
        monkeypatch.setattr(tts, "_reset", lambda: None, raising=False)
        text = "Am I speaking to Piyush Gupta?"
        path = tts._cache_path(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        good = b"\x00" * int(1.5 * 2 * 8000)
        path.write_bytes(good)
        asyncio.run(tts.speak_cached(text))
        assert path.exists()                    # kept
        assert len(sink.written) == len(good)   # and played


def test_cache_read_evicts_a_poisoned_entry(monkeypatch):
    """The entry written before this gate existed must not keep replaying.

    Drives the real speak_cached() path: on a miss it falls through to live
    synthesis, which here raises — proving the poisoned PCM was NOT replayed.
    """
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(ts, "TTS_CACHE_DIR", Path(tmp))
        sink = _FakeSink()
        tts = _cache_tts(sink)
        monkeypatch.setattr(tts, "_reset", lambda: None, raising=False)
        text = "Am I speaking to Piyush Gupta?"
        path = tts._cache_path(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * int(2.686 * 2 * 8000))   # the live bad one

        synthesized = []

        async def _fake_speak(t):
            synthesized.append(t)

        monkeypatch.setattr(tts, "speak", _fake_speak, raising=False)
        asyncio.run(tts.speak_cached(text))

        assert not path.exists()            # evicted
        assert synthesized == [text]        # re-synthesized live
        assert sink.written == b""          # the caller never heard the bad audio


def test_cache_key_is_per_contact_so_one_bad_line_hits_one_person():
    tts = ts.SarvamTTS.__new__(ts.SarvamTTS)
    tts._model, tts._speaker, tts._pace = "bulbul:v3", "simran", 1.15
    tts._language, tts._sample_rate = "en", 8000
    a = tts._cache_path("Am I speaking to Piyush Gupta?")
    b = tts._cache_path("Am I speaking to Yogesh Kumar?")
    assert a != b


# ─────────────────────────── 2. Farewell position ──────────────────────────

@pytest.mark.parametrize("text", [
    "bye",
    "okay bye",
    "good bye",
    "bye bye",
    "thank you. bye bye.",
    "yeah, bye. thank you.",
    "have a good one, bye.",
    "that's it for now, bye.",
    "buh-bye.",
    "oh, bye",
    "okay then, bye. i'll talk to you later. thank you for the time. bye bye.",
])
def test_real_goodbyes_still_close_the_call(text):
    """Every genuine goodbye in the 762-call corpus."""
    assert _farewell_is_terminal(text) is True


@pytest.mark.parametrize("text", [
    "bye philips lightning",          # "Signify, Philips Lighting" (call 850)
    "bye at the time of interview",   # "by the time of interview" (call 704)
])
def test_mis_transcribed_bye_is_not_a_goodbye(text):
    assert _farewell_is_terminal(text) is False
    assert detect_intent(text) != "farewell"


def make_router(**over) -> ConversationRouter:
    d = {
        "agent_id": "t_rep",
        "version": "1.0.0",
        "identity": {"agent_name": "Shubhangi", "company_name": "Dhron AI"},
        "languages": {"supported": ["en"], "default": "en", "tts_voices": {}},
        "call_flow": {"stages": [
            {"id": "S1", "order": 0, "goal": "collect details",
             "turns_until_advance": 8},
            {"id": "S2", "order": 1, "goal": "close",
             "turns_until_advance": 99},
        ]},
        "response_bank": {"opening_greeting": "hi"},
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


def test_employer_name_heard_as_bye_does_not_ask_to_end_the_call():
    """Live call 850: the agent had just asked which organization they work
    for. The caller answered. The platform offered to hang up."""
    r = make_router()
    r.state.turn_count = 4
    r._store_last_response("Which organization are you currently working with?")
    out = drive(r, "bye philips lightning")
    assert "end the call" not in out.lower()
    assert r.hangup_pending is False
    assert r._close_confirm_pending is False


def test_a_real_goodbye_still_reaches_the_close_confirm():
    r = make_router()
    r.state.turn_count = 4
    out = drive(r, "okay thank you, bye")
    assert "end the call" in out.lower()


# ────────────────────────── 3. Callback detection ──────────────────────────

@pytest.mark.parametrize("text", [
    "yes ma'am, so i just got a call from your number. sorry, phone to new.",
    "i got a call from this number, 15, 20 minutes before, regarding some "
    "engineering opening and i just wanted to know what's the process?",
    "yeah, i got a call from this number 5 minutes back and there's nobody "
    "speaking. i mean, what's this about?",
    "i have a missed call from this number",
    "someone called me from this number",
])
def test_reporting_a_received_call_is_not_a_callback_request(text):
    r = make_router()
    assert r._detect_callback_request(text) is False


@pytest.mark.parametrize("text", [
    "call me back tomorrow",
    "can you call me after 2 hours",
    "i am busy right now",
    "call me in 20 minutes",
    "I am busy right now, call me next week",
    "baad mein call karna",
    "mujhe 10 minute baad call karo",
    # Both shapes at once: the explicit ask wins over the report.
    "i missed your call, please call me back at 5 pm",
])
def test_genuine_callback_requests_still_fire(text):
    r = make_router()
    assert r._detect_callback_request(text) is True


def test_maam_no_longer_reads_as_a_clock_time():
    """`\\bam\\b` matched "ma'am" — the whole reason the caller's FIRST
    sentence was answered with "which day and what time suits you?"."""
    r = make_router()
    assert not r._CALLBACK_TIME_RE.search("yes ma'am")
    assert not r._CALLBACK_TIME_RE.search("i am an engineer")
    assert r._CALLBACK_TIME_RE.search("call me at 5 pm")
    assert r._CALLBACK_TIME_RE.search("call me at 10am")


def test_inbound_caller_asking_what_the_call_was_about_is_not_hung_up_on():
    """Live calls 856 and 863, verbatim."""
    r = make_router(follow_up={"clarify_commitments": {"enabled": True}})
    r.state.turn_count = 2
    out = drive(r, "i got a call from this number, 15, 20 minutes before, "
                   "regarding some engineering opening and i just wanted to "
                   "know what's the process?")
    assert "call you back" not in out.lower()
    assert r.state.is_closing is False
    assert r.hangup_pending is False


# ─────────────────────── 4. Unheard replies are retracted ──────────────────

def test_audit_drops_an_agent_line_the_caller_never_heard():
    import call_audit

    with tempfile.TemporaryDirectory() as tmp:
        sess = call_audit.CallAuditSession.__new__(call_audit.CallAuditSession)
        sess._start_time = 0.0
        sess._txt_path = os.path.join(tmp, "transcript.txt")
        sess._txt_file = open(sess._txt_path, "a", encoding="utf-8")
        sess._transcript_count = 0
        sess._last_entry_offset = -1
        sess._last_entry_speaker = ""
        sess._last_entry_text = ""

        sess.add_event("Caller", "my name is piyush and i have completed my")
        sess.add_event("Agent", "What is your current location?")
        assert sess.drop_last_event("Agent", "What is your current location?")
        sess._txt_file.close()

        body = Path(sess._txt_path).read_text(encoding="utf-8")
        assert "current location" not in body
        assert "my name is piyush" in body
        assert sess._transcript_count == 1


def test_retraction_only_ever_removes_the_entry_it_is_handed():
    import call_audit

    with tempfile.TemporaryDirectory() as tmp:
        sess = call_audit.CallAuditSession.__new__(call_audit.CallAuditSession)
        sess._start_time = 0.0
        sess._txt_path = os.path.join(tmp, "transcript.txt")
        sess._txt_file = open(sess._txt_path, "a", encoding="utf-8")
        sess._transcript_count = 0
        sess._last_entry_offset = -1
        sess._last_entry_speaker = ""
        sess._last_entry_text = ""

        sess.add_event("Agent", "What is your current location?")
        sess.add_event("Caller", "bangalore")
        # The agent line is no longer last — a retraction must not touch it.
        assert not sess.drop_last_event("Agent", "What is your current location?")
        # Nor may a mismatched text remove whatever happens to be last.
        assert not sess.drop_last_event("Caller", "mumbai")
        sess._txt_file.close()

        body = Path(sess._txt_path).read_text(encoding="utf-8")
        assert "current location" in body and "bangalore" in body


# ────────────────── 5. Asking ahead of the flow, then again ────────────────

def _screening_router() -> ConversationRouter:
    """The shubhangi shape: the stage that owns the last two questions has
    them in `collect`, and its goal scripts them word for word."""
    d = {
        "agent_id": "t_screen",
        "version": "1.0.0",
        "identity": {"agent_name": "Shubhangi", "company_name": "Dhron AI"},
        "languages": {"supported": ["en"], "default": "en", "tts_voices": {}},
        "call_flow": {"stages": [
            {"id": "RELOCATION_CHECK", "order": 0, "role": "discovery",
             "goal": "Ask if they can relocate to Vapi.",
             "collect": ["relocation_willingness"],
             "advance_when": {"all_collect_filled": True, "max_turns": 4}},
            {"id": "ROLE_AND_TIMELINE", "order": 1, "role": "discovery",
             "goal": "Say: 'could you let me know of the roles that you are "
                     "interested in?' Then ask: 'also could you let us know "
                     "how fast you could join us?'",
             "collect": ["role_of_interest", "joining_timeline"],
             "advance_when": {"all_collect_filled": True, "max_turns": 6}},
        ]},
        "slots": [
            {"name": "relocation_willingness", "priority": 7, "required": True,
             "label": "Relocation Willingness", "type": "boolean",
             "question": {"en": "Will you be okay with relocating to Vapi?"}},
            {"name": "role_of_interest", "priority": 8, "required": True,
             "label": "Role of Interest", "type": "text",
             "question": {"en": "Could you let me know of the roles that you "
                                "are interested in?"}},
            {"name": "joining_timeline", "priority": 9, "required": True,
             "label": "Joining Timeline", "type": "text",
             "question": {"en": "Could you let us know how fast you could "
                                "join us?"}},
        ],
        "response_bank": {"opening_greeting": "hi"},
    }
    return ConversationRouter(agent_def=AgentDefinition.from_dict(d),
                              default_language="en")


def test_a_stage_never_asks_a_later_stages_question():
    r = _screening_router()
    r.state.stage = "RELOCATION_CHECK"
    prompt = r._build_system_prompt()
    assert "Ask ONLY ONE question next, about: Relocation Willingness" in prompt
    assert "Could you let us know how fast you could join us?" not in prompt
    assert "Do NOT ask them yet" in prompt


def test_the_owning_stage_does_ask_its_own_questions():
    r = _screening_router()
    r.state.stage = "ROLE_AND_TIMELINE"
    r.state_mgr.set_externally("relocation_willingness", "yes")
    prompt = r._build_system_prompt()
    assert "Ask ONLY ONE question next, about: Role of Interest" in prompt


def test_legacy_stages_without_collect_keep_global_priority():
    """Byte-identity for every pre-existing agent: an empty collect list
    changes nothing about which slot is asked next."""
    r = _screening_router()
    for stage in r._def.call_flow.stages:
        stage.collect = []
    r.state.stage = "RELOCATION_CHECK"
    prompt = r._build_system_prompt()
    assert "Ask ONLY ONE question next, about: Relocation Willingness" in prompt
    assert "Do NOT ask them yet" not in prompt


def test_stage_missing_narrows_then_falls_back_to_empty():
    assert ConversationStateManager._stage_missing(["a", "b"], None) == ["a", "b"]
    assert ConversationStateManager._stage_missing(["a", "b"], []) == ["a", "b"]
    assert ConversationStateManager._stage_missing(["a", "b"], ["b"]) == ["b"]
    # Stage declares slots and they are all answered → nothing to march on.
    assert ConversationStateManager._stage_missing(["a"], ["c"]) == []


def test_the_prompt_says_what_it_asked_last_turn():
    r = _screening_router()
    r.state.stage = "ROLE_AND_TIMELINE"
    r._store_last_response("Understood. Could you let us know how fast you "
                           "could join us?")
    prompt = r._build_system_prompt()
    assert "━━ YOU ALREADY ASKED THIS ━━" in prompt
    assert "Could you let us know how fast you could join us?" in prompt
    # After CURRENT GOAL, so a script quoting the same question cannot win.
    assert prompt.index("━━ CURRENT GOAL ━━") < prompt.index(
        "━━ YOU ALREADY ASKED THIS ━━")


def test_no_last_question_band_when_the_agent_asked_nothing():
    r = _screening_router()
    r._store_last_response("Understood, thank you for sharing that.")
    assert "━━ YOU ALREADY ASKED THIS ━━" not in r._build_system_prompt()
    assert pb.last_question_block("") == []
    assert pb.last_question_block("ok?") == []


def test_previous_turn_question_takes_the_last_sentence_only():
    r = _screening_router()
    r._store_last_response("Right, so 3 LPA. What is your expected CTC?")
    assert r._previous_turn_question() == "What is your expected CTC?"


def test_qa_signals_come_from_the_slot_questions_not_the_label():
    """Builder agents get qa_field_signals filled with the slot LABEL, which
    the agent never says — the anti-repetition tracker matched nothing."""
    r = _screening_router()
    signals = r.memory.qa_tracker._active_signals
    assert any("how fast you could join us" in s
               for s in signals["joining_timeline"])
    r.memory.qa_tracker.observe_agent_turn(
        "Understood. Could you let us know how fast you could join us?", 3)
    r.memory.qa_tracker.observe_user_turn("immediate, immediately", 4)
    assert r.memory.qa_tracker.is_answered("joining_timeline")
    assert "joining_timeline" in r.memory.qa_tracker.to_prompt_block()


# ───────────────── 6. Never repair a garbled answer into a fact ────────────

def test_every_agent_is_told_not_to_invent_a_value():
    """Live call 849: "immediate joiner" arrived as "may merit join" and the
    agent answered "so, joining in May — got it."; "20 to 15 lpa" came back
    as "20 to 25 LPA". Both were then carried as confirmed facts."""
    r = make_router()
    prompt = r._build_system_prompt()
    assert "NEVER repair a garbled answer into a specific fact" in prompt
    assert "use the caller's OWN words" in prompt


def test_the_rule_ships_with_the_real_estate_pack_too():
    r = make_router(guardrails={"use_builtin_rules": True,
                                "builtin_rules_pack": "real_estate"})
    assert "NEVER repair a garbled answer" in r._build_system_prompt()


# ──────────── 7. Don't re-ask an identity the caller just gave ─────────────

def _outbound_router() -> ConversationRouter:
    d = {
        "agent_id": "t_ident",
        "version": "1.0.0",
        "identity": {"agent_name": "Shubhangi", "company_name": "Dhron AI"},
        "languages": {"supported": ["en"], "default": "en", "tts_voices": {}},
        "call_flow": {"stages": [
            {"id": "S1", "order": 0, "goal": "confirm", "role": "confirm_person",
             "turns_until_advance": 3}],
            "connect": {"intro_line": {"en": "Am I speaking to {contact_name}?"}}},
        "outbound": {"identity_confirm_line":
                     {"en": "Am I speaking to {contact_name}?"}},
        "call_direction": "outbound",
        "response_bank": {"opening_greeting": "Hello"},
    }
    r = ConversationRouter(agent_def=AgentDefinition.from_dict(d),
                           default_language="en")
    r.call_direction = "outbound"
    r.outbound_contact_name = "Piyush Gupta"
    r.memory.add_assistant("Am I speaking to Piyush Gupta?", language="en")
    r._store_last_response("Am I speaking to Piyush Gupta?")
    return r


@pytest.mark.parametrize("text", [
    "hello, this is piyush gupta. who is this?",   # live calls 874 and 875
    "yes, speaking. who is this?",
    "piyush gupta here, who am i speaking to?",
])
def test_identity_is_not_re_asked_when_the_caller_just_gave_it(text):
    r = _outbound_router()
    answer = r._identity_answer(text)
    assert "Dhron AI" in answer
    assert "Am I speaking to" not in answer


@pytest.mark.parametrize("text", [
    "who is this?",
    "which company is calling?",
    "kaun bol raha hai?",
])
def test_identity_question_is_still_handed_back_when_unanswered(text):
    r = _outbound_router()
    assert "Am I speaking to Piyush Gupta?" in r._identity_answer(text)


def test_identity_suppression_only_applies_to_the_identity_beat():
    """A different pending question must still come back with the answer."""
    r = _outbound_router()
    r.memory.add_assistant("What is your current location?", language="en")
    r._store_last_response("What is your current location?")
    assert "current location" in r._identity_answer(
        "this is piyush gupta. who is this?")


# ───────── 8. A misread "no" must not skip the call (call 883) ────────────
#
# Live 2026-09-16. The caller was asked "is now a good time?" and answered
# "no, no, i think we can talk about it right now" — agreement. The word-level
# classifier scored it `negation`, the CONFIRM_AVAILABILITY branch fired, and
# the call jumped five stages straight to WRAPUP_AND_CLOSE. The agent then sat
# in wrap-up asking "anything you wanted to ask me before we wrap up?" four
# times while the candidate was still describing his tech stack, and hung up
# 100 seconds in having collected nothing.

from app.voice.conversation import _is_ack_only


@pytest.mark.parametrize("text", [
    "no, no, i think we can talk about it right now. we can can talk something",
    "no no, go ahead",
    "no, no, i'm fine with relocating",
    "no problem, please continue",
    "no worries, we can start",
    "no, that's fine, carry on",
])
def test_a_leading_no_that_means_go_ahead_is_agreement(text):
    assert detect_intent(text) == "affirmation"


@pytest.mark.parametrize("text", [
    "no", "no thanks", "no, i can't relocate", "nope",
])
def test_a_real_refusal_is_still_a_refusal(text):
    assert detect_intent(text) == "negation"


@pytest.mark.parametrize("text", [
    "no, call me later", "i'm not interested", "not right now", "i'm busy right now",
])
def test_a_genuine_deferral_is_still_an_objection(text):
    """Objection is checked above everything, so the go-ahead phrases cannot
    swallow a real decline."""
    assert detect_intent(text) == "objection"


def test_the_availability_step_has_no_branch_to_the_close():
    """A branch on a bare intent at step 1 is a five-stage trapdoor: one
    misread word and the entire screening is skipped. The platform's callback
    flow already handles a genuine "call me later" — with the time recorded."""
    for slug in ("shubhangi-inbound", "shubhangi-outbound"):
        d = AgentDefinition.from_yaml(AGENTS_DIR / slug / "agent.yaml")
        stage = next(s for s in d.call_flow.stages
                     if s.id == "CONFIRM_AVAILABILITY")
        assert not stage.branches, (
            f"{slug}: CONFIRM_AVAILABILITY must not branch — "
            f"got {stage.branches}")


def test_the_call_883_turn_now_reaches_the_introduction():
    for slug in ("shubhangi-inbound", "shubhangi-outbound"):
        d = AgentDefinition.from_yaml(AGENTS_DIR / slug / "agent.yaml")
        r = ConversationRouter(agent_def=d, default_language="en")
        r.call_direction = "outbound"
        r.outbound_contact_name = "Piyush Gupta"
        for turn in ("yes this is piyush", "okay",
                     "no, no, i think we can talk about it right now"):
            drive(r, turn)
        assert r.state.stage == "CANDIDATE_INTRO", (
            f"{slug}: reached {r.state.stage}, not the introduction")
        assert r.state.is_closing is False


@pytest.mark.parametrize("text", [
    ", yeah yeah, we can, we can.", "yes", "okay sure", "hmm yeah", "alright then",
])
def test_a_bare_acknowledgement_carries_no_slot_values(text):
    """It filled relocation_willingness='Yes' from the caller agreeing to
    TALK — a commitment about moving city that nobody had asked for, which
    then suppressed the real relocation question."""
    assert _is_ack_only(text) is True


@pytest.mark.parametrize("text", [
    "gurgaon", "4 lpa", "python and java", "i can join in 30 days",
    "no, no, i think we can talk about it right now",
])
def test_a_turn_with_real_content_is_still_extracted(text):
    assert _is_ack_only(text) is False
