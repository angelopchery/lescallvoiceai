"""Call-repair platform rules (every agent, every call).

1. Early-call "hello?" — a bare hello (any supported script) inside the
   opening window means the caller didn't hear the agent: replay the last
   utterance verbatim instead of treating it as a conversational turn.
   Capped at 2 replays.
2. Dead-air reprompt — no caller voice activity well past their turn means
   STT probably missed the reply: say "I couldn't hear you" and re-ask the
   question. Pure decision logic lives in VoicePipeline._silence_reprompt_due;
   the phrase composition in ConversationRouter.generate_silence_reprompt.
"""

from __future__ import annotations

import asyncio
import time
import types

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice.conversation import ConversationRouter


def make_router(**over) -> ConversationRouter:
    d = {
        "agent_id": "t_repair",
        "version": "1.0.0",
        "identity": {"agent_name": "Asha", "company_name": "TestCo"},
        "languages": {"supported": ["en", "hi"], "default": "en", "tts_voices": {}},
        "call_flow": {
            "stages": [
                {"id": "S1", "order": 0, "goal": "one", "turns_until_advance": 8},
            ],
        },
        "calendar": {"mock_slots": []},
        "response_bank": {"opening_greeting": "Namaste! This is Asha from TestCo. How can I help you today?"},
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
# early-call "hello?" replay
# ---------------------------------------------------------------------------

def test_early_hello_replays_greeting():
    r = make_router()
    greeting = asyncio.run(r.generate_opening()).text
    resp = drive(r, "Hello?")
    assert resp == greeting
    assert r._hello_replays == 1


def test_repeated_hello_replays_again_then_caps():
    r = make_router()
    greeting = asyncio.run(r.generate_opening()).text
    assert drive(r, "hello hello") == greeting
    assert drive(r, "हेलो हेलो") == greeting
    assert r._hello_replays == 2
    # Cap spent — the guard that feeds the replay branch is now closed.
    assert r._is_early_hello("hello") is True   # still inside the window…
    assert r._hello_replays >= 2                # …but the replay path won't take it


def test_hello_with_filler_still_counts_but_content_does_not():
    r = make_router()
    asyncio.run(r.generate_opening())
    assert r._is_early_hello("are hello haan") is True
    assert r._is_early_hello("hello I want a 2BHK flat") is False
    assert r._is_early_hello("haan") is False          # affirmation, not a hello
    assert r._is_early_hello("good morning") is False


def test_hello_outside_window_is_not_repair():
    r = make_router()
    asyncio.run(r.generate_opening())
    r._opening_ts = time.monotonic() - 60.0
    assert r._is_early_hello("hello") is False


def test_hello_before_any_agent_speech_is_not_repair():
    r = make_router()  # generate_opening never ran
    assert r._is_early_hello("hello") is False


# ---------------------------------------------------------------------------
# dead-air reprompt composition
# ---------------------------------------------------------------------------

def test_silence_reprompt_before_first_turn_replays_plain():
    r = make_router()
    greeting = asyncio.run(r.generate_opening()).text
    assert r.generate_silence_reprompt() == greeting  # no "couldn't hear YOU"


def test_silence_reprompt_repeats_question_with_prefix():
    r = make_router()
    r.last_agent_response = "What time works for you tomorrow?"
    r.state.turn_count = 2
    out = r.generate_silence_reprompt()
    assert out.startswith("Sorry, I couldn't hear you.")
    assert "What time works for you tomorrow?" in out
    # last_agent_response is NOT compounded — a second reprompt is identical
    assert r.last_agent_response == "What time works for you tomorrow?"
    assert r.generate_silence_reprompt() == out


def test_silence_reprompt_language_aware():
    r = make_router()
    r.last_agent_response = "कल कौन सा समय ठीक रहेगा?"
    r.state.turn_count = 3
    r.state.language = "hi"
    out = r.generate_silence_reprompt()
    assert out.startswith("माफ़ कीजिए")
    assert "कल कौन सा समय" in out


def test_silence_reprompt_empty_without_last_response():
    r = make_router()
    assert r.generate_silence_reprompt() == ""


# ---------------------------------------------------------------------------
# dead-air watchdog decision (pure, on a stand-in state object)
# ---------------------------------------------------------------------------

def _pipeline_state(**over):
    from app.voice.pipeline import VoicePipeline

    ns = types.SimpleNamespace(
        _greeting_done=True, _tts_playing=False, _reprompt_count=0,
        conversation=types.SimpleNamespace(call_outcome=""),
        _current_generation_task=None,
        _stt_inflight=0, _user_speaking=False, _prev_speech=False,
        _stt_queue=asyncio.Queue(), _transcript_queue=asyncio.Queue(),
        _tts_queue=asyncio.Queue(),
        sink=types.SimpleNamespace(playback_busy_until=0.0),
        _tts_end_time=0.0, _last_voice_activity=0.0, _last_reprompt_ts=0.0,
        _last_heard_activity=0.0,
        _silence_hangup_fired=False, _maxdur_fired=False, _call_start_ts=0.0,
    )
    # The decision methods share helpers on the class — bind them so the
    # stand-in behaves like a real pipeline object.
    for helper in ("_pipeline_quiet", "_silence_anchor", "_termination_cfg"):
        setattr(ns, helper, types.MethodType(getattr(VoicePipeline, helper), ns))
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def _due(ns, now) -> bool:
    from app.voice.pipeline import VoicePipeline

    return VoicePipeline._silence_reprompt_due(ns, now)


def test_watchdog_fires_after_quiet_period():
    now = 1000.0
    ns = _pipeline_state(_tts_end_time=now - 7.0)
    assert _due(ns, now) is True


def test_watchdog_respects_activity_and_playback():
    now = 1000.0
    assert _due(_pipeline_state(_tts_end_time=now - 7.0,
                                _last_heard_activity=now - 2.0), now) is False
    assert _due(_pipeline_state(_tts_end_time=now - 7.0,
                                _tts_playing=True), now) is False
    ns = _pipeline_state(_tts_end_time=now - 7.0)
    ns.sink.playback_busy_until = now + 3.0
    assert _due(ns, now) is False
    assert _due(_pipeline_state(_tts_end_time=now - 3.0), now) is False


def test_discarded_audio_does_not_count_as_activity():
    """2026-08-17: the anchor read _last_voice_activity, bumped by every raw
    VAD frame — including frames whose segment was then thrown away by the
    SNR guard. On ffbdc517 that made the pipeline believe the caller was
    talking for 14 straight seconds while discarding every word, so dead-air
    repair never came due. Audio we dropped is the failure this watchdog
    exists to repair, not evidence against it."""
    now = 1000.0
    ns = _pipeline_state(_tts_end_time=now - 7.0,
                         _last_voice_activity=now - 0.1,   # VAD saw "speech"
                         _last_heard_activity=0.0)          # nothing reached STT
    assert _due(ns, now) is True


def test_watchdog_never_fires_before_agent_spoke_or_after_cap_or_closing():
    now = 1000.0
    assert _due(_pipeline_state(), now) is False  # anchor 0 — agent never spoke
    assert _due(_pipeline_state(_tts_end_time=now - 7.0,
                                _reprompt_count=2), now) is False
    ns = _pipeline_state(_tts_end_time=now - 7.0)
    ns.conversation.call_outcome = "completed"
    assert _due(ns, now) is False
    assert _due(_pipeline_state(_tts_end_time=now - 7.0,
                                _greeting_done=False), now) is False


def test_watchdog_waits_between_reprompts():
    now = 1000.0
    ns = _pipeline_state(_tts_end_time=now - 20.0, _reprompt_count=1,
                         _last_reprompt_ts=now - 3.0)
    assert _due(ns, now) is False       # last reprompt too recent
    ns._last_reprompt_ts = now - 7.0
    assert _due(ns, now) is True


def test_watchdog_skips_when_turn_in_flight():
    now = 1000.0
    assert _due(_pipeline_state(_tts_end_time=now - 7.0, _stt_inflight=1),
                now) is False
    assert _due(_pipeline_state(_tts_end_time=now - 7.0, _prev_speech=True),
                now) is False
    task = types.SimpleNamespace(done=lambda: False)
    assert _due(_pipeline_state(_tts_end_time=now - 7.0,
                                _current_generation_task=task), now) is False


# ---------------------------------------------------------------------------
# connect intro on speech ONSET (2026-08-17, call ffbdc517)
#
# The transcript-driven fast path is correct but arrives too late to help when
# the transcript itself never comes: five hellos in a row were discarded
# before STT and the caller heard 14s of dead air while the answer sat ready
# (it went out in 1ms the moment a transcript finally landed). Inside the
# connect window the second beat does not depend on WHAT the caller said, so
# the pipeline fires it on VAD onset and the conversation absorbs the
# transcript that follows.
# ---------------------------------------------------------------------------

def _bare_hello_agent(**over):
    """An agent whose opening is itself a bare 'Hello' — the only shape where
    an intro (rather than a replay) is the right repair."""
    over.setdefault("response_bank", {"opening_greeting": "Hello"})
    return make_router(**over)


def test_onset_line_is_available_before_any_transcript():
    r = _bare_hello_agent()
    asyncio.run(r.generate_opening())
    line = r.connect_onset_line()
    assert line, "the intro must be speakable without waiting for STT"
    assert not r._is_bare_hello_text(line)


def test_onset_line_is_a_pure_query():
    """It runs on the VAD frame path — calling it must change nothing."""
    r = _bare_hello_agent()
    asyncio.run(r.generate_opening())
    before = (r._intro_delivered, r.last_agent_response, r.state.turn_count)
    for _ in range(3):
        assert r.connect_onset_line()
    assert (r._intro_delivered, r.last_agent_response,
            r.state.turn_count) == before


def test_onset_line_withheld_when_a_real_opening_was_spoken():
    """A greeting with content may simply have been missed — replay, not
    intro, is that call's repair. Unchanged from the transcript path."""
    r = make_router()      # opening is a full sentence
    asyncio.run(r.generate_opening())
    assert r.connect_onset_line() == ""


def test_onset_line_withheld_after_the_conversation_starts():
    r = _bare_hello_agent()
    asyncio.run(r.generate_opening())
    drive(r, "I am looking for a three bhk in Bopal")
    assert r.connect_onset_line() == ""


def test_onset_line_honours_the_per_agent_opt_out():
    r = _bare_hello_agent(call_flow={
        "stages": [{"id": "S1", "order": 0, "goal": "one",
                    "turns_until_advance": 8}],
        "connect": {"hello_fastpath": False},
    })
    asyncio.run(r.generate_opening())
    assert r.connect_onset_line() == ""


def test_the_triggering_hello_is_absorbed_not_answered_twice():
    """THE regression this pairing exists to prevent: the onset intro and the
    transcript of the very hello that triggered it are ONE exchange."""
    r = _bare_hello_agent()
    asyncio.run(r.generate_opening())
    intro = r.connect_onset_line()
    r.note_connect_intro_spoken(intro)

    assert drive(r, "hello") == "", "one caller hello, one agent line"
    assert drive(r, "hello?") == "", "and the repeats behind it"
    assert r.last_agent_response == intro


def test_absorb_only_swallows_bare_hellos(stub_llm):
    """Anything with content is a real turn and must be answered."""
    r = _bare_hello_agent()
    asyncio.run(r.generate_opening())
    r.note_connect_intro_spoken(r.connect_onset_line())
    assert drive(r, "haan yes this is Rohan speaking")


def test_absorb_expires():
    r = _bare_hello_agent()
    asyncio.run(r.generate_opening())
    r.note_connect_intro_spoken(r.connect_onset_line())
    r._connect_onset_absorb_until = time.monotonic() - 0.01
    assert drive(r, "hello?"), "a much later hello is a real repair signal"


def test_intro_fires_once_across_both_paths():
    """Onset delivery must close the transcript fast path, or a caller whose
    hello DOES transcribe hears the identity line twice."""
    r = _bare_hello_agent()
    asyncio.run(r.generate_opening())
    r.note_connect_intro_spoken(r.connect_onset_line())
    r._connect_onset_absorb_until = 0.0        # past the absorb window
    assert r._intro_delivered is True
    assert r.connect_onset_line() == ""


@pytest.fixture
def onset_on(monkeypatch):
    """The trigger is telephony-only by default (CONNECT_ONSET_TRIGGER reads
    TELEPHONY_MODE at import); tests run in CLI mode, so switch it on."""
    import app.voice.pipeline as pl
    monkeypatch.setattr(pl, "CONNECT_ONSET_TRIGGER", True)


def _onset_pipeline(router, **over):
    """Minimal stand-in carrying just what _maybe_connect_onset touches."""
    from app.voice.pipeline import VoicePipeline

    ns = types.SimpleNamespace(
        _connect_onset_fired=False, _raw_speech_ms=0.0,
        _call_start_ts=time.monotonic(), _tts_playing=False,
        conversation=router, _tts_queue=asyncio.Queue(), _hub=None,
    )
    for helper in ("_maybe_connect_onset", "_queue_watchdog_line",
                   "_emit_agent_line"):
        setattr(ns, helper, types.MethodType(getattr(VoicePipeline, helper), ns))
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def test_pipeline_fires_the_intro_on_onset_without_a_transcript(onset_on):
    import app.voice.pipeline as pl

    r = _bare_hello_agent()
    asyncio.run(r.generate_opening())
    ns = _onset_pipeline(r)

    # A frame or two of speech is not a turn — nothing yet.
    ns._raw_speech_ms = 40.0
    ns._maybe_connect_onset()
    assert ns._tts_queue.empty()

    # ...but sustained onset speaks, with no transcript anywhere in sight.
    ns._raw_speech_ms = pl.CONNECT_ONSET_MS
    ns._maybe_connect_onset()
    assert ns._tts_queue.qsize() == 1
    spoken = ns._tts_queue.get_nowait()
    assert spoken.text == r.last_agent_response
    assert spoken.protected is True, "a caller mid-hello must not cut it off"
    assert r._intro_delivered is True


def test_onset_intro_fires_at_most_once(onset_on):
    import app.voice.pipeline as pl

    r = _bare_hello_agent()
    asyncio.run(r.generate_opening())
    ns = _onset_pipeline(r, _raw_speech_ms=pl.CONNECT_ONSET_MS)
    for _ in range(5):
        ns._maybe_connect_onset()
    assert ns._tts_queue.qsize() == 1


def test_onset_intro_does_not_fire_over_agent_audio(onset_on):
    import app.voice.pipeline as pl

    r = _bare_hello_agent()
    asyncio.run(r.generate_opening())
    ns = _onset_pipeline(r, _raw_speech_ms=pl.CONNECT_ONSET_MS,
                         _tts_playing=True)
    ns._maybe_connect_onset()
    assert ns._tts_queue.empty()


def test_onset_intro_latches_off_past_the_connect_window(onset_on):
    import app.voice.pipeline as pl

    r = _bare_hello_agent()
    asyncio.run(r.generate_opening())
    ns = _onset_pipeline(
        r, _raw_speech_ms=pl.CONNECT_ONSET_MS,
        _call_start_ts=time.monotonic() - pl.CONNECT_WINDOW_S - 1.0)
    ns._maybe_connect_onset()
    assert ns._tts_queue.empty()
    assert ns._connect_onset_fired is True, "stop re-checking on every frame"
