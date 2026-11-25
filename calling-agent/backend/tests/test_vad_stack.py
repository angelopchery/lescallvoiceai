"""VAD stack (deploy/PLAN-vad-stack.md): FSMN adapter on real speech,
DSP audio-event classifier on synthetic signals, endpointer profiles,
session state machine. Offline — the vendored onnx model is in-repo."""

import math
import os
import time
import wave

import numpy as np
import pytest

from app.voice.audio_events import AudioEventClassifier
from app.voice.call_session import CallSession
from app.voice.endpointer import EndpointController

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
SAMPLE = os.path.join(REPO, "data", "voice_samples", "aditya.wav")
MODEL = os.path.join(REPO, "data", "models", "fsmn_vad")
SR = 16000


def _tone(freq, ms, amp=6000):
    t = np.arange(int(SR * ms / 1000.0)) / SR
    return (amp * np.sin(2 * math.pi * freq * t)).astype(np.float64)


def _hops(sig):
    hop = SR // 10
    return [sig[i:i + hop] for i in range(0, len(sig) - hop + 1, hop)]


# ── FSMN adapter on real committed speech ────────────────────────────

@pytest.mark.skipif(not os.path.isdir(MODEL), reason="model not vendored")
def test_fsmn_stack_detects_real_speech_and_is_cheap():
    from app.voice.vad_stack import FsmnVadStack
    stack = FsmnVadStack()
    w = wave.open(SAMPLE)
    assert w.getframerate() == SR
    pcm = w.readframes(w.getnframes())
    frame = SR * 2 // 50  # 20ms
    lat, saw_speech = [], False
    for i in range(0, len(pcm) - frame, frame):
        t0 = time.monotonic()
        if stack.is_speech(pcm[i:i + frame]):
            saw_speech = True
        lat.append(time.monotonic() - t0)
        time.sleep(0.002)
    deadline = time.monotonic() + 5.0
    while not saw_speech and time.monotonic() < deadline:
        time.sleep(0.05)
        saw_speech = stack.speech_open
    events = stack.pop_events()
    stack.close()
    assert saw_speech or any(k == "speech_start" for k, _ in events)
    # Frame path must be essentially free (model runs on the worker).
    assert np.percentile(np.array(lat) * 1000, 95) < 5.0
    # Compat surface the pipeline reads:
    assert stack.calibrated and stack.barge_in_threshold >= 500.0
    assert stack.threshold > 0 and stack._minimum_rms_floor > 0


@pytest.mark.skipif(not os.path.isdir(MODEL), reason="model not vendored")
def test_model_is_fed_normalised_audio_and_classifier_is_not():
    """THE 2026-08-04 root cause. funasr's WavFrontendOnline multiplies its
    input by (1 << 15) before kaldi fbank and its own loader is librosa.load,
    so the model is calibrated on [-1, 1]. The worker fed it raw int16 —
    32768x hot — from the day the stack went live (2026-07-30) until this was
    found, which is why FSMN flagged silence as speech on 28% of segments in
    call 5aaee2cd.

    The classifier riding the same worker must NOT be normalised: its DTMF
    Goertzel bank and AEC_RMS_FLOOR=60 are absolute int16 thresholds.
    """
    import app.voice.vad_stack as vs

    seen = {"model": [], "clf": []}

    class _CaptureModel:
        def __call__(self, audio, param_dict=None, is_final=False):
            seen["model"].append(float(np.max(np.abs(audio))) if len(audio) else 0.0)
            return None

    class _CaptureClf:
        def update(self, hop, speech_open, at_ms):
            seen["clf"].append(float(np.max(np.abs(hop))) if len(hop) else 0.0)

    real_preload = vs.preload_model
    vs.preload_model = lambda *a, **k: _CaptureModel()
    try:
        stack = vs.FsmnVadStack(event_sink=_CaptureClf())
        # Full-scale int16 speech: the harshest case for a scaling error.
        loud = (np.ones(SR // 5, dtype=np.int16) * 20000).tobytes()
        frame = SR * 2 // 50
        for i in range(0, len(loud) - frame + 1, frame):
            stack.is_speech(loud[i:i + frame])
        deadline = time.monotonic() + 5.0
        while not seen["model"] and time.monotonic() < deadline:
            time.sleep(0.02)
        stack.close()
    finally:
        vs.preload_model = real_preload

    assert seen["model"], "worker never invoked the model"
    assert max(seen["model"]) <= 1.0, (
        f"model got peak {max(seen['model'])} — must be normalised to [-1, 1]")
    assert seen["clf"], "classifier never invoked"
    assert max(seen["clf"]) > 1000.0, (
        f"classifier got peak {max(seen['clf'])} — must stay int16-scaled")


# ── audio-event classifier (synthetic signals) ───────────────────────

def _run(clf, sig, fsmn=False, at0=0.0):
    label = None
    for j, h in enumerate(_hops(sig)):
        label = clf.update(h, fsmn, at0 + 100.0 * j)
    return label


def test_classifier_dtmf():
    clf = AudioEventClassifier()
    sig = _tone(770, 600) + _tone(1336, 600)  # key "5"
    assert _run(clf, sig) == "DTMF"


def test_classifier_ringback_vs_busy_cadence():
    clf = AudioEventClassifier()
    # India ringback: 400Hz bursts with a long (~2s) off gap.
    sig = np.concatenate([_tone(400, 400), np.zeros(SR * 2),
                          _tone(400, 400), np.zeros(SR * 2),
                          _tone(400, 400)])
    assert _run(clf, sig) in ("RINGBACK", "SILENCE")  # tone hops classify
    assert "RINGBACK" == clf._ring_or_busy() or clf.current_label == "RINGBACK"

    clf2 = AudioEventClassifier()
    # Busy: symmetric 750/750.
    sig2 = np.concatenate(sum([[_tone(425, 700), np.zeros(int(SR * 0.7))]
                               for _ in range(4)], []))
    _run(clf2, sig2)
    # After ≥2 cycles the off-time (700ms < 1200ms) reads as BUSY.
    assert clf2._ring_or_busy() == "BUSY"


def test_classifier_speech_vs_music():
    clf = AudioEventClassifier()
    rng = np.random.default_rng(7)
    # Speech-like: noise with strong 4Hz syllabic amplitude modulation.
    t = np.arange(SR * 3) / SR
    speech = rng.normal(0, 2500, SR * 3) * (0.2 + 0.8 * (np.sin(2 * math.pi * 4 * t) > 0))
    assert _run(clf, speech, fsmn=True) == "HUMAN_SPEECH"

    clf2 = AudioEventClassifier()
    # Music-like: steady broadband chord, no syllabic envelope.
    chord = sum(_tone(f, 3000, 1500) for f in (262, 330, 392, 523))
    chord = chord + rng.normal(0, 400, len(chord))
    assert _run(clf2, chord, fsmn=False) == "MUSIC"


def test_classifier_silence():
    clf = AudioEventClassifier()
    assert _run(clf, np.zeros(SR)) == "SILENCE"


# ── endpointer profiles ──────────────────────────────────────────────

def test_endpointer_profiles():
    ep = EndpointController()
    # Complete short answer → fast profile (2 residual frames).
    assert not ep.should_flush(1, 900, "हाँ ठीक है।", True)
    assert ep.should_flush(2, 900, "हाँ ठीक है।", True)
    assert ep.last_profile == "fast_complete"
    # Dangling conjunction → hold (26 frames).
    assert not ep.should_flush(12, 2000, "मुझे दो बीएचके चाहिए और", True)
    assert ep.last_profile == "hold"
    assert ep.should_flush(26, 2000, "मुझे दो बीएचके चाहिए और", True)
    # Digit dictation tail → hold.
    ep.should_flush(1, 2000, "मेरा नंबर है 98 76", True)
    assert ep.last_profile == "hold"
    # No text → default (7 frames), never a hold.
    assert not ep.should_flush(6, 1200, "", True)
    assert ep.should_flush(7, 1200, "", True)
    assert ep.last_profile == "default"


# ── session state machine ────────────────────────────────────────────

def test_session_transitions_and_voicemail_interpretation():
    s = CallSession(direction="outbound")
    s.set("AI_SPEAKING")
    s.set("AI_SPEAKING")  # dedup
    assert [st for st, _ in s.history] == ["CALL_INIT", "AI_SPEAKING"]
    s.set("LIVE_CONVERSATION")
    # Outbound machine monologue → IVR_SUSPECT → beep → VOICEMAIL_SUSPECT.
    s.on_speech_run(9000.0)
    assert s.state == "IVR_SUSPECT"
    s.on_audio_label("BEEP", "HUMAN_SPEECH", 12000.0)
    assert s.state == "VOICEMAIL_SUSPECT"
    # A real exchange clears suspicion paths for the rest of the call.
    s2 = CallSession(direction="outbound")
    s2.mark_exchange()
    s2.on_speech_run(9000.0)
    assert s2.state == "CALL_INIT"  # no suspicion once exchanged
    # Terminal state is sticky.
    s.set("CALL_ENDED")
    s.set("LIVE_CONVERSATION")
    assert s.state == "CALL_ENDED"


def test_session_music_mapping_inbound_vs_outbound():
    si = CallSession(direction="inbound")
    si.on_audio_label("MUSIC", "SILENCE", 3000.0)
    assert si.state == "MUSIC"
    so = CallSession(direction="outbound")
    so.on_audio_label("MUSIC", "SILENCE", 3000.0)
    assert so.state == "MUSIC"
