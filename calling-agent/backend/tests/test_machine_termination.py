"""RUNBOOK §83 — a machine on an outbound line ends the call, now, and a
person never does by mistake.

Three failures this covers, all from recordings/:

* call 917265832771 (2026-10-02, 690 s): a hold announcement repeating every
  ~45 s in Gujarati, Hindi and English kept re-arming the 60 s machine wait,
  and every garbled fragment of it ("i'm better off", "the person you are
  speaking with has put") reached the model as the caller's turn and got a
  fresh pitch — 8.5 minutes of it;
* every voicemail close drained the opening + onset intro before hanging up
  (3 s minimum, more with a long opening) — TTS billed into a mailbox;
* a mailbox whose greeting STT never rendered (an unsupported script, music
  then a tone) was never recognised at all.

The precision side is the point: the classifier is checked against corpus
lines a PERSON said that share words with a machine, the beep may only end an
outbound call before anyone has spoken, and a person coming back from a hold
is let through.
"""
from __future__ import annotations

import asyncio
import math
import sys
import time
import types

import numpy as np
import pytest

from conftest import REPO_ROOT

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agent_definition.schema import AgentDefinition  # noqa: E402
from app.voice import machine_line as ml  # noqa: E402
from app.voice.audio_events import AudioEventClassifier  # noqa: E402
from app.voice.call_session import CallSession  # noqa: E402
from app.voice.conversation import ConversationRouter  # noqa: E402


@pytest.fixture(autouse=True)
def _no_network_llm(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


def make_router(direction: str = "outbound", **over) -> ConversationRouter:
    d = {
        "agent_id": "t_machine",
        "version": "1.0.0",
        "identity": {"agent_name": "Ravi", "company_name": "Pramukh Group"},
        "languages": {"supported": ["en", "hi"], "default": "hi",
                      "tts_voices": {}},
        "call_direction": direction,
        "call_flow": {
            "stages": [
                {"id": "S1", "order": 0, "goal": "one", "turns_until_advance": 8},
                {"id": "S2", "order": 1, "goal": "two", "turns_until_advance": 99},
            ],
        },
        "response_bank": {"opening_greeting": "नमस्ते"},
    }
    d.update(over)
    r = ConversationRouter(agent_def=AgentDefinition.from_dict(d),
                           default_language="hi")
    r.set_call_context(direction=direction, contact_name="Jainam",
                       contact_phone="917265832771")
    r.memory.add_assistant("नमस्ते! क्या मेरी बात Jainam जी से हो रही है?",
                           language="hi")
    r._store_last_response("नमस्ते! क्या मेरी बात Jainam जी से हो रही है?")
    r._opening_ts = time.monotonic()
    return r


def drive(router: ConversationRouter, text: str) -> str:
    async def _one():
        out = []
        async for res in router.process_stream(text):
            out.append(res.text)
        return " ".join(t for t in out if t)
    return asyncio.run(_one())


# ── 1. classifier: new wording from the corpus, and the people near it ─────

@pytest.mark.parametrize("line,kind", [
    # the hold announcement in pieces (call 917265832771)
    ("the person you are speaking with has put", ml.HOLD),
    ("the person you are speaking with has your call on hold", ml.HOLD),
    ("કૃપા કરીને લાઈન પર રહો.", ml.HOLD),
    ("એ મેં તમારા કોલને હોલ્ડ પર રાખ્યો છે.", ml.HOLD),
    ("તમે જે વ્યક્તિ સાથે વાત કરી રહ્યા છો એ", ml.HOLD),
    ("આપ જે વ્યક્તિ સે વાત કર રહે હૈ ઉસ", ml.HOLD),
    ("ધ પર્સન યુ આર સ્પીકિંગ વિથ હેસ પુટ યોર કોલ ઓન હોલ્ડ પ્લીઝ.", ml.HOLD),
    ("आप जिस व्यक्ति से बात कर रहे हैं उसने", ml.HOLD),
    ("एमे તમારા कॉल ने होल्ड पर रखयो छे।", ml.HOLD),
    ("તમે જે વ્યક્તિને કોલ કર્યો છે, તેને તમારો કોલ હોલ્ડ પર રાખ્યો છે. કૃપા.", ml.HOLD),
    # voicemail: STT variants and a Devanagari rendering of the English line
    ("add the tone, please.", ml.VOICEMAIL),
    ("द पर्सन यू आर ट्राइंग टू रीच इज नॉट अवेलेबल एट द टाइम।", ml.VOICEMAIL),
    ("व्हेन यू हैव फिनिश्ड रिकॉर्डिंग, यू मे हैंग अप।", ml.VOICEMAIL),
    ("press 1 for more options.", ml.VOICEMAIL),
    # a personal mailbox greeting, first person
    ("hi, you've reached rahul. i can't take your call right now, please "
     "leave your name and number and i'll get back to you.", ml.VOICEMAIL),
    ("we are unable to take your call at the moment.", ml.VOICEMAIL),
    # a network announcement with the Gujarati word for subscriber
    ("તમે જે ગ્રાહકને કૉલ કર્યો છે તે હાલમાં ઉપલબ્ધ નથી", ml.UNREACHABLE),
    ("the number you are calling is not answering", ml.UNREACHABLE),
])
def test_new_machine_lines_are_recognised(line, kind):
    assert ml.classify(line) == kind


@pytest.mark.parametrize("line", [
    # corpus lines a PERSON said that share words with a machine
    "actually, you have reached the wrong number. it's govind singh tanwar.",
    "no, not available right now. call me 10 minutes after, okay?",
    "अ हां, अभी अभी मेरी आवाज़ आ रही है।",
    # third person: a family member who answered, not a mailbox
    "he can't come to the phone right now, call back later",
    "she is not able to take your call, she is driving",
    # a person asking us to wait is not the hold announcement
    "एक मिनट होल्ड करो",
    "hold on one second please",
    # ordinary sales-call sentences
    "i'll get back to you tomorrow after checking with my wife",
    "please leave your number with me, i will call",
    "यस बताइए, मैं सुन रहा हूँ",
    "haan boliye",
    "i'm better off",
])
def test_people_near_machine_wording_stay_people(line):
    assert ml.classify(line) is None


def test_hold_residue_is_the_announcement_in_pieces():
    assert ml.is_hold_residue("the person you are speaking with has put")
    assert ml.is_hold_residue("પર રાખ્યો છે.")
    assert ml.is_residue(ml.HOLD, "please stay on the")
    assert not ml.is_hold_residue("hello")
    assert not ml.is_hold_residue("हाँ बोलिए")
    assert not ml.is_residue(ml.VOICEMAIL, "the person you are speaking with")
    # split() hands the person's words back past hold residue too
    kind, human = ml.split("the person you are speaking with has put your "
                           "call on hold. please stay on the line. hello?")
    assert kind == ml.HOLD and human == "hello?"


# ── 2. the gate: a hold loop mid-call is silence, then a close ─────────────

def _enter_conversation(r: ConversationRouter) -> None:
    assert drive(r, "हाँ") != ""
    assert r._human_spoke


def test_hold_fragments_mid_call_get_no_reply(monkeypatch):
    """Call 917265832771 replayed: the announcement, its pieces in three
    languages and the STT garbage between them all stay silent."""
    r = make_router()
    _enter_conversation(r)
    turns = [
        "તમે જે વ્યક્તિ સાથે વાત કરી રહ્યા છો।",
        "एमे તમારા कॉल ने होल्ड पर रखयो छे।",
        "कृपया करणे लाईन.",
        "i'm better off",                         # STT for "stay on the line"
        "आप जिस व्यक्ति से बात कर रहे हैं उसने",
        "हॉल को होल्ड पर रखा है।",
        "कृपया लाइन पर बने रहें।",
        "the person you are speaking with has put",
        "put your call on hold, please.",
        "please stay on the line.",
        "ही स्टाइल बनाईन.",                        # garbage, 3 tokens
        "come back, but",
        "પર રાખ્યો છે.",
    ]
    for t in turns:
        assert drive(r, t) == "", t
    assert r._machine_wait_kind == ml.HOLD
    assert r.awaiting_machine_handoff()
    assert not r.hangup_pending


def test_person_returning_from_hold_is_answered():
    r = make_router()
    _enter_conversation(r)
    assert drive(r, "the person you are speaking with has put your call on "
                    "hold. please stay on the line.") == ""
    assert drive(r, "i'm better off") == ""        # the announcement's tail
    assert drive(r, "हाँ बोलिए") != ""              # the person is back
    assert r._machine_wait_kind is None
    assert r._machine_wait_started == 0.0


@pytest.mark.parametrize("text,pickup", [
    ("हाँ बोलिए", True), ("hello", True), ("yes tell me", True),
    ("sorry, go ahead", True), ("आवाज़ आ रही है?", True),
    ("tell me about the project in detail please", True),
    ("i'm better off", False), ("come back, but", False),
    ("ही स्टाइल बनाईन", False), ("પર રાખ્યો છે.", False), ("", False),
])
def test_hold_pickup_rule(text, pickup):
    r = make_router()
    assert r._looks_like_hold_pickup(text) is pickup


def test_repeated_hold_announcement_does_not_extend_the_episode(monkeypatch):
    """The ceiling is on the EPISODE: a repeat 45 s later re-arms the gap
    timer but never the start, and past the ceiling the next repeat closes
    the call in silence, immediately."""
    import app.voice.conversation as cv
    monkeypatch.setattr(cv, "MACHINE_HOLD_MAX_S", 90.0)
    r = make_router()
    _enter_conversation(r)
    assert drive(r, "please stay on the line.") == ""
    started = r._machine_wait_started
    assert started > 0.0
    # 45 s later: the announcement repeats
    r._machine_wait_since -= 45.0
    r._machine_wait_started -= 45.0
    assert drive(r, "the person you are speaking with has put your call on "
                    "hold. please stay on the line.") == ""
    assert r._machine_wait_started == pytest.approx(started - 45.0, abs=0.5)
    assert r.machine_wait_expired() is None
    assert not r.hangup_pending
    # 95 s into the episode: the next repeat ends it
    r._machine_wait_started -= 50.0
    assert drive(r, "कृपया लाइन पर बने रहें।") == ""
    assert r.hangup_pending and r._hangup_hard and r.hangup_immediate
    assert r.call_outcome == "hold_timeout"
    assert r.state_mgr.extra_flags.get("machine_answered") == ml.HOLD
    # and nothing the "caller" says now gets a reply
    assert drive(r, "the person you are speaking with has put") == ""


def test_watchdog_sees_an_expired_hold_once():
    r = make_router()
    _enter_conversation(r)
    assert drive(r, "please stay on the line.") == ""
    now = time.monotonic()
    assert r.machine_wait_expired(now) is None
    # the announcement stopped 61 s ago (hold music, nobody back)
    r._machine_wait_since = now - 61.0
    assert r.machine_wait_expired(now) == ml.HOLD
    r.arm_machine_timeout_close(ml.HOLD)
    assert r.hangup_immediate and r.call_outcome == "hold_timeout"
    assert r.machine_wait_expired(now) is None
    assert not r.awaiting_machine_handoff(now)


def test_hold_ceiling_is_per_agent():
    r = make_router(call_flow={
        "connect": {"hold_max_s": 30},
        "stages": [{"id": "S1", "order": 0, "goal": "one",
                    "turns_until_advance": 8}],
    })
    _enter_conversation(r)
    assert drive(r, "please stay on the line.") == ""
    assert r._machine_episode_max_s() == 30.0
    now = time.monotonic()
    r._machine_wait_started = now - 31.0
    r._machine_wait_since = now - 1.0
    assert r.machine_wait_expired(now) == ml.HOLD


def test_screener_nobody_answers_expires_too():
    r = make_router()
    out = drive(r, "hi, if you record your name and reason for calling, "
                   "i'll see if this person is available.")
    assert "Pramukh" in out
    now = time.monotonic()
    assert r.machine_wait_expired(now) is None
    r._machine_wait_since = now - 61.0
    assert r.machine_wait_expired(now) == ml.SCREENER
    r.arm_machine_timeout_close(ml.SCREENER)
    assert r.call_outcome == "screener_timeout" and r.hangup_immediate


# ── 3. voicemail closes are immediate; an authored message is not ─────────

def test_voicemail_close_is_immediate_and_silent():
    r = make_router()
    assert drive(r, "your call has been forwarded to voicemail. the person "
                    "you're trying to reach is not available.") == ""
    assert r.hangup_pending and r._hangup_hard and r.hangup_immediate
    assert r.call_outcome == ml.VOICEMAIL
    assert r.state_mgr.extra_flags.get("machine_answered") == ml.VOICEMAIL


def test_voicemail_with_authored_message_drains_first():
    r = make_router(call_flow={
        "connect": {"voicemail": {"action": "leave_message",
                                  "message": {"hi": "नमस्ते, रवि Pramukh से। "
                                                    "हम बाद में कॉल करेंगे।"}}},
        "stages": [{"id": "S1", "order": 0, "goal": "one",
                    "turns_until_advance": 8}],
    })
    out = drive(r, "the person you're trying to reach is not available. "
                   "at the tone, please record your message.")
    assert "Pramukh" in out
    assert r.hangup_pending and r._hangup_hard
    assert r.hangup_immediate is False


def test_a_person_talking_about_voicemail_mid_call_is_not_closed():
    r = make_router()
    _enter_conversation(r)
    assert drive(r, "mera voicemail check karo, maine message chhoda tha") != ""
    assert not r.hangup_pending


# ── 4. the beep may end the call only before anyone has spoken ────────────

def test_beep_close_allowed_only_outbound_before_a_person():
    r = make_router()
    assert r.machine_audio_close_allowed()
    # a machine line keeps it allowed
    assert drive(r, "please stay on the line.") == ""
    assert r.machine_audio_close_allowed()
    # a person's words end it for the rest of the call
    assert drive(r, "हाँ बोलिए") != ""
    assert not r.machine_audio_close_allowed()
    assert not make_router(direction="inbound").machine_audio_close_allowed()


def test_beep_close_records_the_evidence():
    r = make_router()
    r.arm_machine_audio_close("beep")
    assert r.hangup_pending and r.hangup_immediate
    assert r.call_outcome == ml.VOICEMAIL
    assert r.state_mgr.extra_flags.get("machine_evidence") == "beep"
    assert not r.machine_audio_close_allowed()


# ── 5. the audio layer confirms a beep, and only a beep ───────────────────

SR = 16000
HOP = SR // 10  # 100 ms


def _hops(seconds: float, freq: float | None, amp: float = 6000.0,
          glide_to: float | None = None):
    n = int(round(seconds * 10))
    out = []
    for i in range(n):
        t = (np.arange(HOP) + i * HOP) / SR
        if freq is None:
            out.append(np.zeros(HOP))
        else:
            f = freq if glide_to is None else freq + (glide_to - freq) * i / max(n - 1, 1)
            out.append(amp * np.sin(2 * math.pi * f * t))
    return out


def _run(hops, fsmn=False):
    got = []
    c = AudioEventClassifier(sample_rate=SR,
                             on_beep=lambda d, hz, at: got.append((d, hz, at)))
    at = 0.0
    for h in hops:
        c.update(h.astype(np.float64), fsmn, at)
        at += 100.0
    return c, got


def test_record_cue_tone_is_a_confirmed_beep():
    c, got = _run(_hops(0.5, None) + _hops(0.5, 1000.0) + _hops(0.5, None))
    assert len(got) == 1
    dur, hz, _ = got[0]
    assert dur == pytest.approx(500.0)
    assert hz == pytest.approx(1000.0, abs=15.0)
    assert c.beeps and c.beeps[0][1] == pytest.approx(500.0)


def test_a_single_hop_blip_is_not_a_beep():
    _, got = _run(_hops(0.5, None) + _hops(0.1, 1000.0) + _hops(0.5, None))
    assert got == []


def test_a_glide_is_not_a_beep():
    """A whistle moves; a record cue does not."""
    _, got = _run(_hops(0.3, None) + _hops(0.5, 800.0, glide_to=1300.0)
                  + _hops(0.5, None))
    assert got == []


def test_a_sustained_tone_is_not_a_beep():
    """A dial / hold tone outruns the ceiling and is discarded whole."""
    _, got = _run(_hops(0.3, None) + _hops(3.0, 1000.0) + _hops(0.5, None))
    assert got == []


def test_speech_like_audio_is_not_a_beep():
    rng = np.random.default_rng(7)
    hops = []
    for i in range(30):
        t = (np.arange(HOP) + i * HOP) / SR
        env = 0.2 + 0.8 * (np.sin(2 * math.pi * 4 * t) > 0)
        # harmonic voice: 180 Hz fundamental with partials, no single bin wins
        voice = sum(np.sin(2 * math.pi * 180 * k * t) / k for k in range(1, 9))
        hops.append(2500 * env * voice + rng.normal(0, 300, HOP))
    _, got = _run(hops, fsmn=True)
    assert got == []


def test_session_records_the_beep_from_the_worker_thread():
    s = CallSession(direction="outbound")
    s.on_beep(500.0, 1000.0, 12000.0)
    assert s.beep_count == 1
    assert s.last_beep["freq_hz"] == 1000.0
    assert s.last_beep["at_mono"] <= time.monotonic()
    # the shadow interpretation still needs the monologue first
    assert s.state != "VOICEMAIL_SUSPECT"
    s.on_speech_run(9000.0)
    s.on_beep(500.0, 1000.0, 22000.0)
    assert s.state == "VOICEMAIL_SUSPECT"


# ── 6. pipeline: decision + abort, bound onto a stand-in like test_dead_media

class _Sink:
    def __init__(self):
        self.interrupted = 0
        self.wav_queue = asyncio.Queue()
        self.playback_busy_until = 0.0

    async def interrupt_playback(self):
        self.interrupted += 1


class _TTS:
    def __init__(self):
        self.stopped = 0

    async def stop(self):
        self.stopped += 1


def _pipeline(conv, **over):
    import app.voice.pipeline as pl

    ns = types.SimpleNamespace(
        conversation=conv, _session=types.SimpleNamespace(last_beep=None),
        _beep_consumed_mono=0.0, _speech_total_ms=0.0,
        _machine_abort_done=False, _current_generation_task=None,
        _tts_queue=asyncio.Queue(), _stt_queue=asyncio.Queue(),
        _transcript_queue=asyncio.Queue(), _stt_ready_queue=asyncio.Queue(),
        _stt_inflight=0, _delivery_epoch=0, _tts_playing=True,
        sink=_Sink(), tts=_TTS(),
    )
    ns.__dict__.update(over)
    for name in ("_machine_beep_close_due", "_abort_agent_speech",
                 "_machine_close_tick", "_flush_pipeline_queues"):
        setattr(ns, name, types.MethodType(getattr(pl.VoicePipeline, name), ns))
    return ns


def _beep(ago: float = 0.0) -> dict:
    return {"at_mono": time.monotonic() - ago, "duration_ms": 500.0,
            "freq_hz": 1000.0, "at_ms": 9000.0}


def test_beep_closes_an_outbound_call_nobody_answered(monkeypatch):
    import app.voice.pipeline as pl
    monkeypatch.setattr(pl, "MACHINE_BEEP_CLOSE_ENABLED", True)
    r = make_router()
    p = _pipeline(r, _speech_total_ms=4000.0)
    p._session.last_beep = _beep()
    p._tts_queue.put_nowait("opening still queued")
    assert asyncio.run(p._machine_close_tick(time.monotonic())) is True
    assert r.hangup_pending and r.hangup_immediate
    assert r.call_outcome == ml.VOICEMAIL
    assert p.sink.interrupted == 1 and p.tts.stopped == 1
    assert p._tts_playing is False and p._tts_queue.empty()
    # a second tick finds the call already ending and does nothing more
    assert asyncio.run(p._machine_close_tick(time.monotonic())) is True
    assert p.sink.interrupted == 1


def test_beep_after_a_person_spoke_is_ignored(monkeypatch):
    import app.voice.pipeline as pl
    monkeypatch.setattr(pl, "MACHINE_BEEP_CLOSE_ENABLED", True)
    r = make_router()
    _enter_conversation(r)
    p = _pipeline(r, _speech_total_ms=4000.0)
    p._session.last_beep = _beep()
    assert asyncio.run(p._machine_close_tick(time.monotonic())) is False
    assert not r.hangup_pending and p.sink.interrupted == 0


def test_beep_with_no_greeting_before_it_is_ignored(monkeypatch):
    import app.voice.pipeline as pl
    monkeypatch.setattr(pl, "MACHINE_BEEP_CLOSE_ENABLED", True)
    r = make_router()
    p = _pipeline(r, _speech_total_ms=400.0)
    p._session.last_beep = _beep()
    assert asyncio.run(p._machine_close_tick(time.monotonic())) is False
    assert not r.hangup_pending
    # the same beep is never re-judged
    p._speech_total_ms = 4000.0
    assert p._machine_beep_close_due(time.monotonic()) is False


def test_beep_on_an_inbound_call_is_ignored(monkeypatch):
    import app.voice.pipeline as pl
    monkeypatch.setattr(pl, "MACHINE_BEEP_CLOSE_ENABLED", True)
    r = make_router(direction="inbound")
    p = _pipeline(r, _speech_total_ms=4000.0)
    p._session.last_beep = _beep()
    assert asyncio.run(p._machine_close_tick(time.monotonic())) is False


def test_beep_close_is_off_outside_telephony(monkeypatch):
    import app.voice.pipeline as pl
    monkeypatch.setattr(pl, "MACHINE_BEEP_CLOSE_ENABLED", False)
    r = make_router()
    p = _pipeline(r, _speech_total_ms=4000.0)
    p._session.last_beep = _beep()
    assert p._machine_beep_close_due(time.monotonic()) is False


def test_expired_hold_closes_from_the_watchdog():
    r = make_router()
    _enter_conversation(r)
    assert drive(r, "please stay on the line.") == ""
    r._machine_wait_since -= 61.0
    p = _pipeline(r)
    assert asyncio.run(p._machine_close_tick(time.monotonic())) is True
    assert r.call_outcome == "hold_timeout" and r.hangup_immediate
    assert p.sink.interrupted == 1 and p.tts.stopped == 1


def test_gate_close_aborts_speech_once():
    r = make_router()
    assert drive(r, "forwarded to voicemail.") == ""
    assert r.hangup_immediate
    p = _pipeline(r)
    asyncio.run(p._abort_agent_speech("machine on the line"))
    asyncio.run(p._abort_agent_speech("machine on the line"))
    assert p.sink.interrupted == 1 and p.tts.stopped == 1


def test_nothing_happens_on_a_live_call():
    r = make_router()
    _enter_conversation(r)
    p = _pipeline(r, _speech_total_ms=9000.0)
    assert asyncio.run(p._machine_close_tick(time.monotonic())) is False
    assert p.sink.interrupted == 0 and not r.hangup_pending


# ── 7. the bridge hangs up at once on an immediate close ──────────────────

def _bridge_session(conv):
    import conversation_call_bridge as cb

    calls = []

    async def _break():
        calls.append("break")
        return True

    async def _hangup():
        calls.append("hangup")

    ns = types.SimpleNamespace(
        conn_id="t", _hangup_sent=False, _hangup_armed_ts=None,
        pipeline=types.SimpleNamespace(conversation=conv,
                                       _pipeline_quiet=lambda now: True),
        sink=_Sink(), _send_break_request=_break, _send_hangup_request=_hangup,
    )
    ns._maybe_agent_hangup = types.MethodType(
        cb.TelephonySession._maybe_agent_hangup, ns)
    return ns, calls


def test_bridge_hangs_up_immediately_on_a_machine_close():
    r = make_router()
    assert drive(r, "forwarded to voicemail.") == ""
    s, calls = _bridge_session(r)
    asyncio.run(s._maybe_agent_hangup())
    assert calls == ["break", "hangup"]
    assert s._hangup_sent is True


def test_bridge_still_drains_a_consented_close():
    """The farewell path is untouched: the first poll only arms the drain."""
    r = make_router()
    r.hangup_pending = True
    s, calls = _bridge_session(r)
    asyncio.run(s._maybe_agent_hangup())
    assert calls == [] and s._hangup_armed_ts is not None


def test_bridge_speaks_an_authored_voicemail_message_before_hanging_up():
    r = make_router(call_flow={
        "connect": {"voicemail": {"action": "leave_message",
                                  "message": {"hi": "नमस्ते, हम बाद में कॉल करेंगे।"}}},
        "stages": [{"id": "S1", "order": 0, "goal": "one",
                    "turns_until_advance": 8}],
    })
    assert drive(r, "at the tone, please record your message.") != ""
    s, calls = _bridge_session(r)
    asyncio.run(s._maybe_agent_hangup())
    assert calls == []          # armed, draining — not cut
