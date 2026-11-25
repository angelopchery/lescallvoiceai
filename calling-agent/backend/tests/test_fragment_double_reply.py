"""Regression gate: ONE reply per caller utterance, however it fragments.

Live 2026-09-09 05:43 (Jai Mistry → outbound). The caller answered in three
pieces with pauses between them. The agent spoke two full pitches back to
back, each one a complete answer to a different fragment:

    05:43:21.495  [VAD] Dropping implausible speech span (5.70s vs 0.86s)
    05:43:22.131  fragment 2 → its own turn
    05:43:24.941  reply A generated  ("राइट Sir, … schedule करना चाहेंगे?")
    05:43:24.942  fragment 3 → its own turn
    05:43:26.807  reply B generated  ("बिलकुल Sir, … schedule करना चाहेंगे?")
    05:43:27.980  reply A starts playing
    05:43:27.982  reply B starts playing

The continuation-merge layer exists precisely to prevent this, and every one
of its gates was open. The cause was upstream: the VAD plausibility check
zeroed BOTH speech timestamps whenever wall-clock disagreed with counted
speech by more than a second — which is the definition of a caller who pauses
mid-utterance, i.e. exactly the caller the merge layer is for. With the span
gone, `_turn_txn_speech_end` was never advanced either, so fragment 3 measured
its gap against speech that had ended ~10s earlier, cleared every window, and
was committed as a brand-new turn.

Three independent repairs, one per test group below.

No network, no model: every assertion is deterministic platform behaviour.
"""
from __future__ import annotations

import asyncio
import time
import types

import pytest

from test_vad_real_speech_floor import FRAME, _drive, _harness


# ---------------------------------------------------------------------------
# 1. An implausible ONSET must not cost us the END anchor
# ---------------------------------------------------------------------------

class _Clock:
    """A monotonic clock that advances only when the frame loop says so.

    The shared VAD harness drives every frame in a tight loop, so wall-clock
    barely moves while the VAD believes seconds of audio went by — under that,
    a segment's measured span is meaningless. These tests are ABOUT the span,
    so they run the frames on a clock that ticks one frame per frame.
    """

    def __init__(self):
        self.t = 10_000.0

    def monotonic(self):
        return self.t

    def time(self):
        return 1_700_000_000.0 + self.t


async def _drive_in_time(ns, clock, frames):
    for _ in range(frames):
        await ns._process_vad_frame(FRAME)
        clock.t += 0.02


@pytest.fixture
def clock(monkeypatch):
    import app.voice.pipeline as pl
    c = _Clock()
    monkeypatch.setattr(pl, "time", c)
    return c


def _segment(ns, clock, frames):
    asyncio.run(_drive_in_time(ns, clock, frames))
    assert not ns._stt_queue.empty(), "segment never reached STT"
    return ns._stt_queue.get_nowait()


def _stale_onset_segment(clock, stale_by_s: float = 6.0):
    """A segment whose onset anchor is far older than the audio it labels —
    the live shape (audio preserved across a barge-in, or claimed from the
    playback shadow ring, carries the anchor forward; the buffer does not)."""
    ns = _harness([True] * 25 + [False] * 40)
    ns._call_start_ts = clock.t - 60.0
    # Non-empty buffer ⇒ the onset branch keeps the anchor it already has.
    ns._audio_buffer = bytearray(FRAME * 5)
    ns._speech_duration_ms = 100.0
    ns._raw_speech_ms = 100.0
    ns._cur_speech_start_ts = clock.t - stale_by_s
    return _segment(ns, clock, 65)


def test_implausible_span_keeps_the_end_anchor(clock):
    """The end anchor is derived from the flush moment minus counted silence.
    Nothing the onset does can make it wrong, and zeroing it disarmed every
    continuation gate downstream."""
    seg = _stale_onset_segment(clock)
    assert seg.speech_end_ts > 0.0, (
        "end anchor dropped — the continuation gates all require it")


def test_implausible_onset_is_reconstructed_not_zeroed(clock):
    """end − buffered speech is the tightest honest lower bound on the onset,
    and it is exact when the caller did not pause. Zero is not a lower bound,
    it is a sentinel that reads as 'no continuation possible'."""
    seg = _stale_onset_segment(clock)
    assert seg.speech_start_ts > 0.0
    assert seg.speech_start_ts < seg.speech_end_ts
    expected = seg.speech_end_ts - seg.duration_ms / 1000.0
    assert abs(seg.speech_start_ts - expected) < 0.001
    # And it is a genuine repair, not the stale anchor passed through.
    assert seg.speech_start_ts > clock.t - 6.0 + 1.0


def test_a_reconstructed_span_is_flagged_as_such(clock):
    assert _stale_onset_segment(clock).span_estimated is True


def test_an_ordinary_segment_keeps_its_measured_span(clock):
    """The common path must be untouched: a caller who does not pause gets a
    real measured onset, a sane span, and no estimation flag."""
    ns = _harness([True] * 25 + [False] * 40)
    ns._call_start_ts = clock.t - 60.0
    seg = _segment(ns, clock, 65)
    assert seg.span_estimated is False
    assert 0.0 < seg.speech_start_ts < seg.speech_end_ts


# ---------------------------------------------------------------------------
# 2. One bad span must not poison the turns that follow it
# ---------------------------------------------------------------------------

def _anchor(**kw) -> float:
    from app.voice.pipeline import Transcript, VoicePipeline
    return VoicePipeline._turn_end_anchor(Transcript(
        text="x", duration_ms=100.0, timestamp=time.monotonic(), **kw))


def test_anchor_uses_the_measured_speech_end_when_there_is_one():
    end = time.monotonic() - 0.4
    assert _anchor(speech_end_ts=end) == end


def test_anchor_never_leaves_a_stale_value_standing():
    """The live failure: fragment 2 carried no end anchor, so the previous
    turn's value survived and fragment 3 measured a ~10s gap against it —
    past even the 6s long window. Now is wrong by the STT round trip; the
    stale anchor was wrong by three turns."""
    before = time.monotonic()
    got = _anchor(speech_end_ts=0.0)
    assert before <= got <= time.monotonic()


# ---------------------------------------------------------------------------
# 3. A reply the caller cannot have heard is always mergeable
# ---------------------------------------------------------------------------

def _pipeline(**over):
    """Minimal stand-in carrying the real _reply_still_inaudible."""
    from app.voice.pipeline import VoicePipeline

    ns = types.SimpleNamespace(
        _current_generation_task=None,
        _tts_queue=asyncio.Queue(),
        _tts_playing=False,
        sink=types.SimpleNamespace(playback_busy_until=0.0),
    )
    for k, v in over.items():
        setattr(ns, k, v)
    ns._reply_still_inaudible = types.MethodType(
        VoicePipeline._reply_still_inaudible, ns)
    return ns


class _Task:
    def __init__(self, done): self._done = done
    def done(self): return self._done


def test_a_reply_still_generating_is_inaudible():
    assert _pipeline(_current_generation_task=_Task(False)
                     )._reply_still_inaudible() is True


def test_a_reply_queued_for_tts_is_inaudible():
    q = asyncio.Queue()
    q.put_nowait(object())
    assert _pipeline(_tts_queue=q)._reply_still_inaudible() is True


def test_a_reply_playing_with_nothing_on_the_clock_is_inaudible():
    """The live case at 05:43:24.942: the TTS player had dequeued the reply
    and begun synthesising, but the sink did not write its first audio until
    05:43:26.022 — 1.1s later. 'Playing' is not 'heard'."""
    assert _pipeline(_tts_playing=True)._reply_still_inaudible() is True


def test_a_reply_on_the_playback_clock_is_audible():
    ns = _pipeline(_tts_playing=True)
    ns.sink.playback_busy_until = time.monotonic() + 2.0
    assert ns._reply_still_inaudible() is False


def test_an_idle_pipeline_has_nothing_to_cut():
    """No pending audio anywhere ⇒ the previous reply was fully delivered and
    the next transcript is a genuine new turn, not a continuation."""
    ns = _pipeline(_current_generation_task=_Task(True))
    assert ns._reply_still_inaudible() is False


def test_a_sink_without_a_playback_clock_counts_playing_as_heard():
    """LocalSpeakerSink (CLI) exposes no playback clock. Absent evidence, a
    playing reply must count as heard — the merge is the destructive option."""
    ns = _pipeline(_tts_playing=True, sink=types.SimpleNamespace())
    assert ns._reply_still_inaudible() is False


# ---------------------------------------------------------------------------
# The gate the three repairs feed, mirrored from the production expression
# ---------------------------------------------------------------------------

def _merges(gap_ms, *, inaudible, undelivered=False, start_ts=1.0,
            txn_end=1.0, hello=False, chain=0):
    from app.voice import pipeline as P
    gap_ok = (
        start_ts > 0.0 and txn_end > 0.0 and 0.0 <= gap_ms
        and (gap_ms <= P.CONTINUATION_WINDOW_MS
             or (undelivered and gap_ms <= P.CONTINUATION_LONG_WINDOW_MS))
    )
    return (not hello and (gap_ok or inaudible)
            and chain < P.CONTINUATION_MAX_MERGES)


def test_the_live_gap_would_still_be_rejected_on_timing_alone():
    """Fragment 3 measured 9.8s against the poisoned anchor — past the 6s
    long window. Timing alone could not have saved this call."""
    assert _merges(9800.0, inaudible=False, undelivered=True) is False


def test_but_an_unheard_reply_merges_regardless_of_the_gap():
    assert _merges(9800.0, inaudible=True, undelivered=True) is True


def test_a_delivered_reply_past_the_window_is_still_a_new_turn():
    """The rule must not swallow the caller's ANSWER to a question they
    actually heard — that is a genuine new turn and gets its own reply."""
    assert _merges(9800.0, inaudible=False) is False


def test_a_bare_hello_never_merges():
    assert _merges(500.0, inaudible=True, hello=True) is False


def test_the_chain_cap_still_bounds_an_unheard_reply():
    from app.voice import pipeline as P
    assert _merges(500.0, inaudible=True,
                   chain=P.CONTINUATION_MAX_MERGES) is False


def test_the_ordinary_short_pause_still_merges_on_timing():
    assert _merges(1240.0, inaudible=False) is True
