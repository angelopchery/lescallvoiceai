"""Dead-media detection (2026-08-04, call 02819104).

From second 64 to the end of that call every sample on the caller channel was
exactly -1 — 61 of its 125 seconds. That value is not reachable from a µ-law
decode at all (the near-zero reachable values are 0, ±8, ±16, ±24) and the line
floor before the cut was ±8, so the carrier leg went flat upstream of us. VAD
correctly reported no speech; the problem was that nothing distinguished a dead
channel from a quiet caller, so the agent spent the next 53 seconds re-asking
twice and delivering a farewell into a dead socket, billing TTS throughout.

Zero variance is the discriminator, not level.
"""

from __future__ import annotations

import os
import time
import types
import wave

import numpy as np
import pytest


REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
DEAD_CALL = os.path.join(
    REPO, "recordings", "918076562864_20260804_062339", "caller_audio.wav")


def _ns(**over):
    import app.voice.pipeline as pl

    ns = types.SimpleNamespace(
        _dead_media_since=0.0,
        _dead_media_fired=False,
        conversation=None,
    )
    ns.__dict__.update(over)
    for name in ("_track_media_liveness", "_dead_media_due"):
        setattr(ns, name, types.MethodType(getattr(pl.VoicePipeline, name), ns))
    return ns


def _feed(ns, samples: np.ndarray, frame=160):
    b = samples.astype(np.int16).tobytes()
    for i in range(0, len(b) - frame * 2 + 1, frame * 2):
        ns._track_media_liveness(b[i:i + frame * 2])


# ── the discriminator ───────────────────────────────────────────────────────

def test_mulaw_silence_is_not_dead_media():
    """The floor of a LIVE quiet line: µ-law silence dithering over 0/±8."""
    ns = _ns()
    rng = np.random.default_rng(0)
    _feed(ns, rng.choice([-8, 0, 8], size=16000))
    assert ns._dead_media_since == 0.0, "a quiet caller must never look dead"


def test_constant_sample_is_dead_media():
    ns = _ns()
    _feed(ns, np.full(16000, -1))
    assert ns._dead_media_since > 0.0


def test_digital_zero_is_also_dead_media():
    """Some carriers flatline to 0 rather than -1."""
    ns = _ns()
    _feed(ns, np.zeros(16000))
    assert ns._dead_media_since > 0.0


def test_real_speech_is_not_dead_media():
    ns = _ns()
    t = np.arange(16000) / 16000.0
    _feed(ns, 6000 * np.sin(2 * np.pi * 220 * t))
    assert ns._dead_media_since == 0.0


def test_recovery_rearms():
    ns = _ns()
    _feed(ns, np.full(16000, -1))
    assert ns._dead_media_since > 0.0
    ns._dead_media_fired = True
    rng = np.random.default_rng(1)
    _feed(ns, rng.choice([-8, 0, 8], size=3200))
    assert ns._dead_media_since == 0.0
    assert ns._dead_media_fired is False, "media came back — re-arm the latch"


# ── firing rule ─────────────────────────────────────────────────────────────

@pytest.fixture
def telephony(monkeypatch):
    """DEAD_MEDIA_ENABLED defaults to TELEPHONY_MODE; force it on."""
    import app.voice.pipeline as pl
    monkeypatch.setattr(pl, "DEAD_MEDIA_ENABLED", True)


def test_does_not_fire_before_the_window(telephony):
    import app.voice.pipeline as pl

    conv = types.SimpleNamespace(
        hangup_pending=False, arm_dead_media_close=lambda: None)
    ns = _ns(conversation=conv)
    _feed(ns, np.full(16000, -1))
    assert ns._dead_media_due(time.monotonic()) is False
    # ...and does once the channel has been flat past the window.
    ns._dead_media_since = time.monotonic() - pl.DEAD_MEDIA_S - 0.1
    assert ns._dead_media_due(time.monotonic()) is True


def test_does_not_double_fire_or_race_an_existing_close(telephony):
    import app.voice.pipeline as pl

    conv = types.SimpleNamespace(
        hangup_pending=False, arm_dead_media_close=lambda: None)
    ns = _ns(conversation=conv,
             _dead_media_since=time.monotonic() - pl.DEAD_MEDIA_S - 1.0)
    assert ns._dead_media_due(time.monotonic()) is True
    ns._dead_media_fired = True
    assert ns._dead_media_due(time.monotonic()) is False
    ns._dead_media_fired = False
    conv.hangup_pending = True          # a close is already draining
    assert ns._dead_media_due(time.monotonic()) is False


def test_close_is_hard_silent_and_outcome_tagged():
    """No farewell is synthesized, and caller speech cannot cancel it."""
    from app.voice.conversation import ConversationRouter

    conv = types.SimpleNamespace(
        state=types.SimpleNamespace(is_closing=False, stage=None),
        state_mgr=types.SimpleNamespace(extra_flags={}),
        call_outcome="",
        hangup_pending=False,
        _hangup_hard=False,
        commit_pending_turn=lambda: None,
    )
    ConversationRouter.arm_dead_media_close(conv)

    assert conv.hangup_pending is True
    assert conv._hangup_hard is True, "caller speech must not cancel it"
    assert conv.call_outcome == "dead_media"
    assert conv.state_mgr.extra_flags["dead_media_close"] is True


# ── the actual incident audio ───────────────────────────────────────────────

@pytest.mark.skipif(not os.path.isfile(DEAD_CALL), reason="recording pruned")
def test_detects_the_live_incident_and_not_its_live_half():
    """Replay call 02819104's own caller channel: silent for the first 63s of
    REAL line (must not trip), constant -1 after (must trip)."""
    import app.voice.pipeline as pl

    w = wave.open(DEAD_CALL)
    sr = w.getframerate()
    pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    w.close()

    live = _ns()
    _feed(live, pcm[:60 * sr])
    assert live._dead_media_since == 0.0, "the live half must never look dead"

    dead = _ns()
    _feed(dead, pcm[70 * sr:75 * sr])
    assert dead._dead_media_since > 0.0

    # And the flat run is far longer than the firing window, so the close
    # would have fired ~48s before the call actually ended.
    flat = 0
    for i in range(0, len(pcm) - 160, 160):
        blk = pcm[i:i + 160]
        flat = flat + 1 if blk.max() == blk.min() else 0
    assert flat * 0.02 > pl.DEAD_MEDIA_S
