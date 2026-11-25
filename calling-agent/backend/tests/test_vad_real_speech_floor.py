"""VAD segmentation audit (2026-08-04) — calls 5aaee2cd / 02819104.

Two live outbound calls showed the same four defects, all in the scaffolding
around FSMN rather than in the model (its worker reported hops_dropped=0,
p99 5.1ms, degraded=False throughout):

  1. MIN_SPEECH_MS was measured on the PADDED buffer. The 400ms pre-speech
     ring and the 200ms hysteresis hold are both added to _speech_duration_ms,
     so one 20ms frame of model-confirmed speech scored ~620ms and cleared the
     300ms floor. The gate was arithmetically incapable of rejecting anything:
     9 of 23 STT submissions in 5aaee2cd carried no caller speech (RMS 8-89 vs
     737-3655 for real speech), and an 80ms blip padded to 980ms came back as
     "₹1 lakh." in a call about the caller's budget.

  2. The echo-cooldown early exit counted bare raw_speech — the FSMN verdict —
     even though vad_stack.py's own contract is that the model classifies our
     TTS echo as speech and names energy as the only echo discriminator. It
     broke out of a 5.4s echo window on 4 frames and shipped 700ms of digital
     silence (RMS 8) to Sarvam.

  3. EndpointController was called with turn_text="" on every decision, so
     both semantic profiles (hold / fast_complete) were unreachable and
     _DIGIT_TAIL — written so budget dictation isn't chopped between bursts —
     had never executed on a live call.

  4. The merge fast-path sampled _prev_speech at a single instant. FSMN
     confirms resumed speech 100-300ms late, so 'area.' was fast-pathed to the
     LLM 196ms before the model reported the caller was still talking; the
     merge landed 1.3s after dispatch and the caller got two answers to one
     sentence.
"""

from __future__ import annotations

import asyncio
import time
import types

import pytest


FRAME = b"\x00\x00" * 160  # 20ms @ 16kHz


class _FakeVad:
    """Scripted FSMN stand-in: is_speech() replays a caller-supplied pattern
    and last_energy reports whatever that frame was worth."""

    def __init__(self, pattern, energy=1500.0, noise=5.0):
        self._pattern = list(pattern)
        self._i = 0
        self.calibrated = True
        self.threshold = 260.0
        self.barge_in_threshold = 520.0
        self._energy = energy
        self._noise = noise
        self.last_energy = 0.0

    def is_speech(self, frame):
        v = self._pattern[self._i] if self._i < len(self._pattern) else False
        self._i += 1
        self.last_energy = self._energy if v else self._noise
        return bool(v)


class _FakeTurn:
    def __init__(self):
        self.flushed = 0

    def mark_speech(self):
        pass

    def update_text(self, _t):
        pass

    def should_trigger(self):
        return False

    def flush(self):
        self.flushed += 1


def _harness(pattern, energy=1500.0, mute_until=0.0, endpointer=True,
             noise=5.0):
    """Bind the REAL _process_vad_frame onto a minimal namespace."""
    import app.voice.pipeline as pl
    from app.voice.endpointer import EndpointController

    ns = types.SimpleNamespace(
        _mic_enabled=True,
        vad=_FakeVad(pattern, energy=energy, noise=noise),
        _vad_frames_seen=0,
        _vad_speech_frames=0,
        _user_speaking=False,
        _tts_playing=False,
        _vad_mute_until=mute_until,
        _echo_gate_speech_frames=0,
        _echo_preserve_logged=False,
        _greeting_echo_window=False,
        _greeting_done=True,
        _continuation_likely_until=0.0,
        _speech_onset_frames=0,
        _silence_frames=0,
        _last_voice_activity=0.0,
        _pre_speech_buffer=[],
        _prev_speech=False,
        _cur_speech_start_ts=0.0,
        _audio_buffer=bytearray(),
        _speech_duration_ms=0.0,
        _raw_speech_ms=0.0,
        _speech_total_ms=0.0,
        _energy_window=__import__("collections").deque(
            maxlen=pl.SNR_AMBIENT_WINDOW_FRAMES),
        _frames_since_segment=0,
        _shadow_frames=[],
        _shadow_speech_ms=0.0,
        _shadow_started_ts=0.0,
        _shadow_until=0.0,
        _shadow_seeded=False,
        _segment_has_shadow=False,
        _session=None,
        _turn=_FakeTurn(),
        _speech_onset_min_frames=pl.SPEECH_ONSET_MIN_FRAMES,
        _speech_hold_min_frames=pl.SPEECH_HOLD_MIN_FRAMES,
        _endpointer=EndpointController() if endpointer else None,
        _call_start_ts=time.monotonic() - 60.0,   # past the connect window
        _last_fragment_text="",
        _last_fragment_ts=0.0,
        _segments_created=0,
        _segments_sent_to_stt=0,
        _segments_dropped_no_speech=0,
        _segments_dropped_snr=0,
        _snr_drop_streak=0,
        _connect_onset_fired=False,
        _last_heard_activity=0.0,
        _stt_queue=asyncio.Queue(maxsize=16),
        _speech_frame_streak=0,
        conversation=None,
    )
    for name in ("_process_vad_frame", "_min_speech_ms", "_min_real_speech_ms",
                 "_endpoint_hint_text", "_claim_shadow", "_drop_shadow",
                 "_shadow_capture", "_reset_vad_state", "_segment_snr",
                 "_reset_segment_energy", "_segment_speech_energy",
                 "_ambient_energy", "_segment_is_loud",
                 "_maybe_connect_onset"):
        setattr(ns, name, types.MethodType(getattr(pl.VoicePipeline, name), ns))
    return ns


def _harness_energies(energies, truth, lag):
    """Same harness, driven by a VAD whose labels lag its own energy."""
    ns = _harness([])
    ns.vad = _LaggingVad(energies, truth, lag)
    return ns


async def _drive(ns, frames):
    for _ in range(frames):
        await ns._process_vad_frame(FRAME)


# ── Defect 1: the padded floor could never reject ────────────────────────────

def test_padded_duration_alone_would_have_passed_the_old_floor():
    """The live '₹1 lakh.' segment, reproduced: an 80ms speech span (4 frames,
    audit span 12.89-12.97s) padded by the ring and the hold into a 660ms
    buffer. The old gate read the padded number and shipped it; the new floor
    reads the 80ms and drops it."""
    # 20 silence frames fill the pre-speech ring, then the 80ms blip.
    ns = _harness([False] * 20 + [True] * 4 + [False] * 40)
    asyncio.run(_drive(ns, 64))

    assert ns._segments_sent_to_stt == 0, "80ms blip must not reach paid STT"
    assert ns._segments_dropped_no_speech == 1
    assert ns._stt_queue.empty()
    # Why the bug was invisible: the number the OLD gate compared really was
    # far above MIN_SPEECH_MS, entirely from padding.
    assert ns._speech_duration_ms == 0.0   # cleared by the drop
    assert ns._raw_speech_ms == 0.0


def test_real_speech_clears_the_floor():
    """A genuine short answer ("yes", ~400ms) must still reach STT."""
    ns = _harness([False] * 20 + [True] * 20 + [False] * 40)
    asyncio.run(_drive(ns, 80))

    assert ns._segments_sent_to_stt == 1
    assert ns._segments_dropped_no_speech == 0
    seg = ns._stt_queue.get_nowait()
    # Padding is still IN the audio (onset recovery) — it just no longer counts
    # toward the floor.
    assert seg.duration_ms > 400.0


def test_hold_and_ring_frames_do_not_count_as_real_speech():
    """The hysteresis hold and pre-speech ring inflate duration_ms but must
    contribute nothing to _raw_speech_ms."""
    ns = _harness([False] * 20 + [True] * 3 + [False] * 40)
    asyncio.run(_drive(ns, 63))

    # 3 speech frames = 60ms real, well under MIN_REAL_SPEECH_MS.
    assert ns._segments_dropped_no_speech == 1
    assert ns._segments_sent_to_stt == 0


@pytest.mark.parametrize("speech_frames,expect_sent", [
    (2, 0),    # 40ms  — click/breath
    (5, 0),    # 100ms — the "₹1 lakh." class of hallucination
    (9, 0),    # 180ms — still under the floor
    (10, 1),   # 200ms — floor exactly met
    (25, 1),   # 500ms — ordinary short reply
])
def test_real_speech_floor_boundary(speech_frames, expect_sent):
    ns = _harness([False] * 20 + [True] * speech_frames + [False] * 40)
    asyncio.run(_drive(ns, 20 + speech_frames + 40))
    assert ns._segments_sent_to_stt == expect_sent


# ── Defect 1b: SNR guard — model speech in a segment carrying no signal ─────

def test_flat_line_is_rejected_even_when_it_clears_the_ms_floor():
    """The 760ms / RMS-15 replay segment: 200ms of model-confirmed speech, but
    the "speech" sits at the same level as the line noise. It cleared the ms
    floor and cost a paid STT call that returned ""."""
    ns = _harness([False] * 20 + [True] * 20 + [False] * 40,
                  energy=15.0, noise=14.0)     # ratio ≈ 1.07
    asyncio.run(_drive(ns, 80))

    assert ns._segments_sent_to_stt == 0
    assert ns._segments_dropped_no_speech == 1


def test_quiet_caller_above_their_own_floor_is_heard():
    """The doctrine that must not regress: loudness is not the test. A caller
    at energy 60 on a 4-energy line is 15× their ambient — quieter in absolute
    terms than the rejected segment above, and must still be heard."""
    ns = _harness([False] * 20 + [True] * 20 + [False] * 40,
                  energy=60.0, noise=4.0)
    asyncio.run(_drive(ns, 80))

    assert ns._segments_sent_to_stt == 1, "quiet ≠ absent"


def test_snr_fails_open_without_enough_ambient():
    """Too few silence frames to estimate the floor → benefit of the doubt."""
    ns = _harness([True] * 30 + [False] * 12, energy=20.0, noise=19.0)
    asyncio.run(_drive(ns, 42))

    assert ns._segment_snr() is None or ns._segments_sent_to_stt == 1


def test_substantial_speech_is_never_judged_on_energy():
    """THE regression this guard nearly caused: the callsim replay's real
    "d v h k" utterance carried 800ms of model-confirmed speech but measured
    1.8× ambient, while other real speech in the same call measured 2.0-8.4×.
    Past SNR_ENFORCE_BELOW_MS the model's verdict is final."""
    import app.voice.pipeline as pl

    frames = int(pl.SNR_ENFORCE_BELOW_MS / 20) + 10
    ns = _harness([False] * 20 + [True] * frames + [False] * 40,
                  energy=18.0, noise=10.0)     # ratio 1.8 — below the floor
    asyncio.run(_drive(ns, 20 + frames + 40))

    assert ns._segments_sent_to_stt == 1, "long real speech must never be cut"
    assert ns._segments_dropped_no_speech == 0


def test_noisy_line_with_real_speech_still_passes():
    """Background noise raises the floor; genuine speech still clears it."""
    ns = _harness([False] * 20 + [True] * 25 + [False] * 40,
                  energy=900.0, noise=300.0)   # ratio 3.0
    asyncio.run(_drive(ns, 85))

    assert ns._segments_sent_to_stt == 1


# ── Defect 1c: the SNR guard must not be inverted by VAD label lag ──────────
# (2026-08-17, call ffbdc517 — five consecutive "hello"s discarded, 14s of
#  dead air, and two other calls that day where the caller was never heard.)


class _LaggingVad:
    """FSMN as it actually behaves: last_energy is THIS frame, but is_speech()
    reports the worker's state, which opens ~250ms after the audio and (via
    FSMN_MAX_END_SIL_MS) closes late too. Replaying real recordings through
    the live stack measured the label window 200-280ms behind the burst."""

    def __init__(self, energies, truth, lag_frames):
        self._energies = list(energies)
        self._truth = list(truth)
        self._lag = lag_frames
        self._i = 0
        self.calibrated = True
        self.threshold = 260.0
        self.barge_in_threshold = 520.0
        self.last_energy = 0.0

    def is_speech(self, frame):
        i = self._i
        self._i += 1
        self.last_energy = (self._energies[i] if i < len(self._energies)
                            else self._energies[-1])
        j = i - self._lag
        return bool(self._truth[j]) if 0 <= j < len(self._truth) else False


# Frames of continuous silence the endpointer waits out before flushing.
_FLUSH_SIL = 10


def _dtx_hello(speech_frames=17, lag=13, loud=3000.0, comfort=8.0, lead=30):
    """A bare "hello" on a silence-suppressed line: an abrupt burst with no
    ramp, sitting in DTX comfort fill of a single constant value.

    `lead` is the gap since the previous flush — deliberately short, because
    the old estimator reset its buckets at every flush and that concentration
    is half the defect."""
    truth = [False] * lead + [True] * speech_frames + [False] * 100
    energies = [comfort] * lead + [loud] * speech_frames + [comfort] * 100
    return energies, truth, lag


def _old_style_snr(energies, truth, lag, lead=30, speech_frames=17):
    """What the pre-fix estimator computed on the same frames: mean over the
    frames the MODEL labelled speech, over mean of the rest, both reset at the
    previous flush. Kept in the suite because it IS the defect — if this ever
    stops coming out below the floor, the test below has stopped exercising
    the bug it was written for."""
    upto = lead + lag + speech_frames + _FLUSH_SIL
    lab = [bool(0 <= i - lag < len(truth) and truth[i - lag])
           for i in range(upto)]
    sp = [e for e, v in zip(energies, lab) if v]
    no = [e for e, v in zip(energies, lab) if not v]
    return (sum(sp) / len(sp)) / (sum(no) / len(no))


def test_the_defect_is_real_the_old_estimator_inverts():
    """Guard on the guard. With the label window shifted 260ms late, the
    caller's burst lands in the NOISE bucket and the DTX comfort fill after it
    lands in the SPEECH bucket — so the old ratio comes out BELOW the reject
    floor on audio that is 375x its own line noise."""
    import app.voice.pipeline as pl

    energies, truth, lag = _dtx_hello()
    assert _old_style_snr(energies, truth, lag) < pl.MIN_SPEECH_SNR
    # ...while the honest measurement is nowhere near the floor.
    assert max(energies) / min(energies) > 100


def test_lagged_labels_no_longer_deafen_the_call():
    """THE regression. A 340ms hello, 375x above its own line floor, labelled
    260ms late by the model. It must reach STT."""
    energies, truth, lag = _dtx_hello()
    ns = _harness_energies(energies, truth, lag)
    asyncio.run(_drive(ns, len(energies)))

    assert ns._segments_sent_to_stt == 1, "a loud hello must never be dropped"
    assert ns._segments_dropped_snr == 0


def test_ambient_floor_is_not_dragged_up_by_the_speech_it_measures():
    """The floor is a percentile of recent frame energy, so a burst — a small
    slice of any multi-second window — cannot raise it. The old per-segment
    mean could be dominated by a handful of loud frames."""
    energies, truth, lag = _dtx_hello()
    ns = _harness_energies(energies, truth, lag)
    asyncio.run(_drive(ns, len(energies)))
    assert ns._ambient_energy() == 8.0


def test_loud_backstop_survives_a_poisoned_floor():
    """Even if the floor estimate is wrong, absolute loudness is not: a
    segment above barge-in level is never judged on a ratio."""
    energies, truth, lag = _dtx_hello(loud=3000.0, comfort=3000.0)
    ns = _harness_energies(energies, truth, lag)
    asyncio.run(_drive(ns, len(energies)))
    # Floor == speech everywhere, so the ratio is 1.0 and the guard would
    # reject; the absolute backstop is the only thing that saves this call.
    assert ns._ambient_energy() == 3000.0
    assert ns._segments_sent_to_stt == 1
    assert ns._segments_dropped_snr == 0


def test_quiet_blip_on_a_real_line_is_still_rejected():
    """The guard's original job, unchanged: a short blip that never rises
    above the line's own noise still costs no paid STT call — lag or not."""
    energies, truth, lag = _dtx_hello(speech_frames=12, loud=15.0, comfort=14.0)
    ns = _harness_energies(energies, truth, lag)
    asyncio.run(_drive(ns, len(energies)))
    assert ns._segments_sent_to_stt == 0
    assert ns._segments_dropped_no_speech == 1


# ── Defect 2: echo gate must not exit on the model verdict alone ─────────────

def test_echo_gate_ignores_model_speech_at_echo_energy():
    """FSMN says speech, energy says echo → the cooldown holds."""
    ns = _harness([True] * 40, energy=8.0,          # the live RMS-8 segment
                  mute_until=time.monotonic() + 5.0)
    asyncio.run(_drive(ns, 40))

    assert ns._vad_mute_until > time.monotonic(), "gate must stay shut"
    assert ns._segments_sent_to_stt == 0


def test_echo_gate_still_exits_for_a_real_caller():
    """A caller genuinely talking over the tail keeps their barge-in."""
    ns = _harness([True] * 40, energy=1500.0,
                  mute_until=time.monotonic() + 5.0)
    asyncio.run(_drive(ns, 10))

    assert ns._vad_mute_until == 0.0, "sustained loud speech must open the gate"


# ── Defect 3: the endpointer must receive text it can reason over ───────────

def test_endpoint_hint_is_empty_without_a_recent_fragment():
    ns = _harness([False])
    assert ns._endpoint_hint_text() == ""


def test_endpoint_hint_carries_the_in_flight_fragment():
    ns = _harness([False])
    ns._last_fragment_text = "my budget is one"
    ns._last_fragment_ts = time.monotonic()
    assert ns._endpoint_hint_text() == "my budget is one"


def test_endpoint_hint_expires_with_the_continuation_window():
    import app.voice.pipeline as pl

    ns = _harness([False])
    ns._last_fragment_text = "my budget is one"
    ns._last_fragment_ts = (
        time.monotonic() - pl.CONTINUATION_WINDOW_MS / 1000.0 - 1.0)
    assert ns._endpoint_hint_text() == "", "stale turn must not hold the next"


def test_digit_tail_now_reaches_the_hold_profile():
    """The live failure: the agent asked for a budget and the caller's
    dictation was chopped between bursts. With the fragment text wired in,
    _DIGIT_TAIL finally selects `hold` instead of `default`."""
    from app.voice.endpointer import EndpointController

    ep = EndpointController()
    # 7 silence frames = the default residual, which used to flush here.
    assert ep.should_flush(7, 900.0, "my budget is 50", False) is False
    assert ep.last_profile == "hold"
    # And the same acoustics with no digit tail still flush on default.
    assert ep.should_flush(7, 900.0, "", False) is True
    assert ep.last_profile == "default"
