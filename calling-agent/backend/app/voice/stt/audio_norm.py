"""
Dynamic, per-call speech normalization — the pre-STT front end.

Why this exists
---------------
`normalize_pcm_audio` (base.py) applies ONE fixed RMS boost with no memory and
no idea what is speech vs noise. On real phone calls that means quiet-but-noisy
lines get their *noise* amplified right along with the voice, which is a major
cause of empty / garbled Sarvam transcripts.

`DynamicNormalizer` adapts to the actual caller and line, per call:

  1. DC-offset removal            — kills the electrical bias telephony adds.
  2. Speech-vs-noise separation   — per-frame RMS, then percentiles: the loud
                                     frames estimate the speech level, the quiet
                                     frames estimate the noise floor.
  3. Adaptive AGC                 — gain is chosen from the SPEECH level (not the
                                     whole-segment RMS), so silence/noise never
                                     drags the gain down. Smoothed across the call
                                     with an EMA so it doesn't pump turn-to-turn.
  4. Soft noise gate              — frames near the noise floor are attenuated (a
                                     smooth envelope, not a hard chop) so the gaps
                                     between words are quiet and the model isn't
                                     fed a wall of hiss.
  5. Soft limiter                 — peaks are tanh-compressed near the rails
                                     instead of hard-clipped, avoiding the crunchy
                                     distortion that confuses STT.

Speed
-----
Everything is vectorized NumPy — no Python per-sample loops, no scipy. A 1–2 s
16 kHz segment (16k–32k samples) is processed in well under a millisecond, so it
adds no meaningful latency to the STT path. `process()` never raises: on any
unexpected error it returns the original bytes so a call can never break here.
"""
from __future__ import annotations

import logging
import time

import numpy as np

logger = logging.getLogger(__name__)

_INT16_MAX = 32767.0


class DynamicNormalizer:
    """Stateful (per-call) adaptive normalizer. One instance per STT/call."""

    def __init__(
        self,
        target_rms: float = 3500.0,
        max_gain: float = 12.0,
        frame_ms: float = 20.0,
        noise_gate: bool = True,
        gate_floor_mult: float = 1.8,   # frames below noise_floor*this are gated
        gate_reduce: float = 0.35,      # gain applied to gated (noise) frames
        ema: float = 0.4,               # smoothing of level estimates across turns
        soft_limit: bool = True,
    ) -> None:
        self.target_rms = float(target_rms)
        self.max_gain = float(max_gain)
        self.frame_ms = float(frame_ms)
        self.noise_gate = bool(noise_gate)
        self.gate_floor_mult = float(gate_floor_mult)
        self.gate_reduce = float(gate_reduce)
        self.ema = float(ema)
        self.soft_limit = bool(soft_limit)

        # Per-call adaptive state (None until the first speechy segment).
        self._speech_level: float | None = None
        self._noise_floor: float | None = None
        self._segments = 0

    # ------------------------------------------------------------------
    def _update_ema(self, attr: str, value: float) -> float:
        prev = getattr(self, attr)
        new = value if prev is None else (self.ema * value + (1.0 - self.ema) * prev)
        setattr(self, attr, new)
        return new

    # ------------------------------------------------------------------
    def process(self, pcm_bytes: bytes, sample_rate: int = 16_000) -> bytes:
        try:
            return self._process(pcm_bytes, sample_rate)
        except Exception as exc:  # never break the STT path
            logger.debug("[DYN-NORM] fell back to raw audio: %s", exc)
            return pcm_bytes

    def _process(self, pcm_bytes: bytes, sample_rate: int) -> bytes:
        if len(pcm_bytes) < 640:  # <20ms — too short to analyse
            return pcm_bytes

        t0 = time.monotonic()
        x = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)

        # 1) DC-offset removal
        x -= x.mean()

        # 2) Per-frame RMS → speech level (loud frames) + noise floor (quiet frames)
        flen = max(1, int(sample_rate * self.frame_ms / 1000.0))
        n_frames = x.size // flen
        if n_frames >= 3:
            frames = x[: n_frames * flen].reshape(n_frames, flen)
            frame_rms = np.sqrt(np.mean(frames * frames, axis=1)) + 1e-6
            noise_est = float(np.percentile(frame_rms, 10.0))
            speech_est = float(np.percentile(frame_rms, 90.0))
        else:
            frame_rms = None
            noise_est = speech_est = float(np.sqrt(np.mean(x * x)) + 1e-6)

        # Pure noise / silence — don't amplify it.
        if speech_est < 12.0:
            return np.clip(x, -32768.0, _INT16_MAX).astype(np.int16).tobytes()

        speech_level = self._update_ema("_speech_level", speech_est)
        noise_floor = self._update_ema("_noise_floor", noise_est)
        self._segments += 1

        # 3) Adaptive AGC from the SPEECH level (not whole-segment RMS)
        gain = float(np.clip(self.target_rms / speech_level, 1.0, self.max_gain))

        # 4) Soft noise gate — smooth per-sample envelope, no clicks
        if self.noise_gate and frame_rms is not None:
            gate = np.where(frame_rms < noise_floor * self.gate_floor_mult,
                            self.gate_reduce, 1.0).astype(np.float32)
            # smooth across frames (moving avg of 3) so gain changes gradually
            if gate.size >= 3:
                k = np.array([0.25, 0.5, 0.25], dtype=np.float32)
                gate = np.convolve(gate, k, mode="same")
            env = np.repeat(gate, flen)
            # pad the untiled tail (samples beyond n_frames*flen) with the last value
            if env.size < x.size:
                env = np.concatenate([env, np.full(x.size - env.size, gate[-1], np.float32)])
            x = x * (gain * env)
        else:
            x = x * gain

        # 5) Soft limiter — tanh knee near the rails instead of hard clipping
        if self.soft_limit:
            thr = 0.95 * _INT16_MAX
            over = np.abs(x) > thr
            if over.any():
                sign = np.sign(x[over])
                mag = np.abs(x[over])
                x[over] = sign * (thr + (_INT16_MAX - thr) * np.tanh((mag - thr) / (_INT16_MAX - thr)))

        out = np.clip(x, -32768.0, _INT16_MAX).astype(np.int16)

        if self._segments <= 3 or self._segments % 20 == 0:
            logger.info(
                "[DYN-NORM] seg#%d speech=%.0f noise=%.0f gain=%.2fx gate=%s took=%.2fms",
                self._segments, speech_level, noise_floor, gain,
                "on" if self.noise_gate else "off", (time.monotonic() - t0) * 1000.0,
            )
        return out.tobytes()
