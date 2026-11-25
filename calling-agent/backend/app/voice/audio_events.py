"""
Audio event classifier (deploy/PLAN-vad-stack.md).

Pure-DSP, per-100ms-hop classification on the FSMN worker thread —
no ML model fits the 2-core budget, and the tonal classes are textbook
signal processing anyway:

    HUMAN_SPEECH   FSMN speech + strong syllabic (3-8Hz) envelope
    MUSIC          sustained energy, weak syllabic modulation
                   (call_session maps it to CALLER_TUNE before the first
                    exchange on outbound, HOLD_MUSIC mid-call)
    RINGBACK       ~400-450Hz tone, long off-cadence (India: 0.4s bursts)
    BUSY           ~400-450Hz tone, symmetric on/off cadence
    DTMF           one row + one column of the 8-frequency grid
    BEEP           short single mid-band tone burst (voicemail beep)
    FAX            1100Hz CNG chirps / 2100Hz CED carrier
    SILENCE        below the energy floor
    NOISE          energy without any of the above signatures

IVR and VOICEMAIL are NOT acoustic classes — they are session-level
interpretations of HUMAN_SPEECH patterns (monologue length, beep after
monologue) and live in call_session.py.

Shadow by doctrine: the classifier informs logs, EventHub and the
session state machine; it gates nothing on live inbound calls.
"""

from __future__ import annotations

import logging
import math
import os
import time

import numpy as np

logger = logging.getLogger(__name__)

_DTMF_ROWS = (697.0, 770.0, 852.0, 941.0)
_DTMF_COLS = (1209.0, 1336.0, 1477.0, 1633.0)
_RMS_FLOOR = float(os.environ.get("AEC_RMS_FLOOR", "60"))

# Label must persist this many hops before we switch (except the
# transient classes, which are inherently short).
_HYSTERESIS_HOPS = 3
_TRANSIENT = {"DTMF", "BEEP"}

# A CONFIRMED mailbox beep (RUNBOOK §83): BEEP_MIN_HOPS..BEEP_MAX_HOPS
# consecutive 100 ms hops whose peak frequency stays within
# BEEP_MAX_SPREAD_HZ, after which the tone STOPS. Carrier and handset record
# cues measure 0.3-1.0 s at one frequency; a whistle glides, hold music
# moves, and a dial tone never ends (it overruns the 1.2 s ceiling and is
# discarded). The raw per-hop BEEP label above stays as it was — it is a
# shadow signal; only a confirmed beep is reported through on_beep.
BEEP_MIN_HOPS = int(os.environ.get("AEC_BEEP_MIN_HOPS", "3"))
BEEP_MAX_HOPS = int(os.environ.get("AEC_BEEP_MAX_HOPS", "12"))
BEEP_MAX_SPREAD_HZ = float(os.environ.get("AEC_BEEP_MAX_SPREAD_HZ", "40"))


def _goertzel(x: np.ndarray, sr: int, freq: float) -> float:
    """Single-bin power. Cheap enough to run a ~13-freq bank per hop."""
    n = len(x)
    k = int(0.5 + n * freq / sr)
    w = 2.0 * math.pi * k / n
    coeff = 2.0 * math.cos(w)
    s0 = s1 = s2 = 0.0
    for v in x:
        s0 = v + coeff * s1 - s2
        s2, s1 = s1, s0
    return s1 * s1 + s2 * s2 - coeff * s1 * s2


class AudioEventClassifier:
    def __init__(self, sample_rate: int = 16000, on_label=None,
                 on_beep=None) -> None:
        self.sr = sample_rate
        self._on_label = on_label          # callback(label, prev, at_ms)
        self._on_beep = on_beep            # callback(duration_ms, freq_hz, at_ms)
        self._beep_run: list[float] = []   # peak Hz of consecutive BEEP hops
        self._beep_overrun = False         # the tone outlived the beep ceiling
        self._last_peak_hz = 0.0
        self.beeps: list[tuple[float, float, float]] = []  # (at_ms, dur_ms, hz)
        self._rms_hist: list[float] = []   # last 2s of hop RMS
        self._tone_state = {"on_ms": 0.0, "off_ms": 0.0, "cycles": []}
        self._candidate = "SILENCE"
        self._candidate_hops = 0
        self.current_label = "SILENCE"
        self._label_since_ms = 0.0
        self._speech_run_ms = 0.0          # continuous FSMN-speech length

    # ------------------------------------------------------------------
    def update(self, hop: np.ndarray, fsmn_speech: bool, at_ms: float) -> str:
        # Decimate for the Goertzel bank (all target tones < 2.2kHz, and
        # a 400-sample loop is 4x cheaper than 1600).
        x = hop[::4].astype(np.float64)
        sr4 = self.sr // 4
        rms = float(np.sqrt(np.mean(np.square(hop)))) if len(hop) else 0.0
        self._rms_hist.append(rms)
        if len(self._rms_hist) > 20:
            self._rms_hist.pop(0)
        self._speech_run_ms = self._speech_run_ms + 100.0 if fsmn_speech else 0.0

        label = self._classify(x, sr4, hop, rms, fsmn_speech)
        self._track_beep(label, at_ms)
        self._apply_hysteresis(label, at_ms)
        return self.current_label

    # ------------------------------------------------------------------
    def _classify(self, x, sr4, hop, rms, fsmn_speech) -> str:
        if rms < _RMS_FLOOR:
            self._track_tone(False)
            return "SILENCE"

        total = float(np.sum(np.square(x))) + 1e-9
        row_p = [_goertzel(x, sr4, f) for f in _DTMF_ROWS]
        col_p = [_goertzel(x, sr4, f) for f in _DTMF_COLS]
        tone400 = max(_goertzel(x, sr4, f) for f in (400.0, 425.0, 450.0))
        fax_cng = _goertzel(x, sr4, 1100.0)
        # CED 2100Hz is beyond the /4-decimated Nyquist (2kHz) — detect it
        # on a /2-decimated copy (8kHz sampling, Nyquist 4kHz).
        x2 = hop[::2].astype(np.float64)
        ced_norm = (float(np.sum(np.square(x2))) + 1e-9) * len(x2) / 2.0
        fax_ced_ratio = _goertzel(x2, self.sr // 2, 2100.0) / ced_norm

        norm = total * len(x) / 2.0  # goertzel power scale vs sum-of-squares
        r, c = max(row_p), max(col_p)
        if r / norm > 0.15 and c / norm > 0.15:
            self._track_tone(False)
            return "DTMF"

        # Spectral shape (full-rate FFT, trivial at 10Hz cadence).
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        psum = float(np.sum(spec)) + 1e-9
        peak_i = int(np.argmax(spec))
        peak_ratio = float(spec[peak_i]) / psum
        peak_hz = peak_i * sr4 / len(x)

        if peak_ratio > 0.35:  # narrowband tone
            if 370.0 <= peak_hz <= 480.0 and tone400 / norm > 0.2:
                return self._ring_or_busy()
            if 1020.0 <= peak_hz <= 1180.0 and fax_cng / norm > 0.2:
                self._track_tone(False)
                return "FAX"
            if fax_ced_ratio > 0.25:
                self._track_tone(False)
                return "FAX"
            if 650.0 <= peak_hz <= 1400.0:
                self._track_tone(True)
                self._last_peak_hz = peak_hz
                # short isolated burst = beep; sustained = treat as tone/noise
                if self._tone_state["on_ms"] <= 1200.0:
                    return "BEEP"
                self._beep_overrun = True
                return "NOISE"
        self._track_tone(False)

        # Speech vs music: syllabic envelope modulation over the last 2s.
        mod = self._syllabic_modulation()
        if fsmn_speech:
            if mod >= 0.22 or self._speech_run_ms < 800.0:
                return "HUMAN_SPEECH"
            return "MUSIC"
        if mod < 0.18 and len(self._rms_hist) >= 15 and \
                min(self._rms_hist[-15:]) > _RMS_FLOOR:
            return "MUSIC"
        return "NOISE"

    # ------------------------------------------------------------------
    def _track_beep(self, label: str, at_ms: float) -> None:
        """Confirm a mailbox beep from the run of raw BEEP hops: a run ends
        when the label changes; it counts only if it is beep-length, held one
        frequency, and ended because the tone stopped (not because it ran
        past the 1.2 s ceiling)."""
        if label == "BEEP":
            self._beep_run.append(self._last_peak_hz)
            return
        if not self._beep_run:
            self._beep_overrun = False
            return
        run, self._beep_run = self._beep_run, []
        overrun, self._beep_overrun = self._beep_overrun, False
        n = len(run)
        if overrun or n < BEEP_MIN_HOPS or n > BEEP_MAX_HOPS:
            return
        if max(run) - min(run) > BEEP_MAX_SPREAD_HZ:
            return
        dur_ms = n * 100.0
        hz = sum(run) / n
        self.beeps.append((at_ms, dur_ms, hz))
        logger.info("[AUDIO-EVENT] beep confirmed %.0fms @%.0fHz", dur_ms, hz)
        if self._on_beep is not None:
            try:
                self._on_beep(dur_ms, hz, at_ms)
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _syllabic_modulation(self) -> float:
        """Depth of the 3-8Hz envelope modulation. Speech syllable cadence
        makes hop-RMS swing hard at this timescale; music/tones don't."""
        if len(self._rms_hist) < 8:
            return 1.0  # too early to judge — bias toward speech (soft-safe)
        h = np.array(self._rms_hist, dtype=np.float64)
        m = float(np.mean(h)) + 1e-9
        return float(np.mean(np.abs(np.diff(h)))) / m

    def _track_tone(self, tonal: bool) -> None:
        st = self._tone_state
        if tonal:
            if st["off_ms"] > 0:
                st["cycles"].append((st["on_ms"], st["off_ms"]))
                st["cycles"] = st["cycles"][-4:]
                st["on_ms"] = 0.0
            st["on_ms"] += 100.0
            st["off_ms"] = 0.0
        else:
            if st["on_ms"] > 0:
                st["off_ms"] += 100.0
                if st["off_ms"] > 3000.0:
                    st["on_ms"] = 0.0
                    st["off_ms"] = 0.0
                    st["cycles"].clear()

    def _ring_or_busy(self) -> str:
        self._track_tone(True)
        cycles = self._tone_state["cycles"]
        if len(cycles) >= 1:
            on_ms, off_ms = cycles[-1]
            # India: busy ≈ symmetric 750/750; ringback bursts then a long
            # (~2s) gap. Off-time is the discriminator.
            return "RINGBACK" if off_ms > 1200.0 else "BUSY"
        return "RINGBACK"  # first burst — provisional

    # ------------------------------------------------------------------
    def _apply_hysteresis(self, label: str, at_ms: float) -> None:
        if label == self._candidate:
            self._candidate_hops += 1
        else:
            self._candidate = label
            self._candidate_hops = 1
        need = 1 if label in _TRANSIENT else _HYSTERESIS_HOPS
        if self._candidate_hops >= need and label != self.current_label:
            prev = self.current_label
            self.current_label = label
            self._label_since_ms = at_ms
            logger.info("[AUDIO-EVENT] %s → %s @%.0fms", prev, label, at_ms)
            if self._on_label is not None:
                try:
                    self._on_label(label, prev, at_ms)
                except Exception:
                    pass
