"""
Two-stage model VAD (deploy/PLAN-vad-stack.md, VAD_ENGINE=fsmn).

Stage 1 — WebRTC VAD pre-filter: removes obvious silence at ~zero CPU so
the model never chews on dead air. It can ONLY gate FSMN invocation
during confirmed idle — it never ends a turn, never drops a frame from
the pipeline's buffers, never touches barge-in. While a speech segment
is open (or within the hangover) FSMN runs on every hop regardless of
what WebRTC thinks, so stage 1 cannot lose soft speech when anything is
at stake.

Stage 2 — FunASR FSMN-VAD (onnx, vendored at data/models/fsmn_vad,
Apache-2.0): the primary VAD. Detects speech start/end as model events,
segments audio for ASR, and replaces the amplitude threshold entirely —
a quiet caller is a model problem, not a loudness problem. Inference
runs on a dedicated worker thread in 100ms hops (measured on this VM:
p50 1.6ms / p95 2.4ms / max 8ms per hop with intra_op=1, full model —
the quantized one is SLOWER here, dequant overhead beats the gains), so
the 20ms frame path never blocks on the model.

Frame-level energy is still computed (2 lines/frame) because it remains
the ONLY echo discriminator for barge-in — a model VAD happily
classifies our own TTS echo as speech. Energy makes no speech decision
on this engine.

Drop-in for VoiceActivityDetector: same is_speech()/threshold/
last_energy/barge_in_threshold surface, calibrated=True from frame one
(no 500ms calibration deafness).
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)

FRAME_MS = 20
HOP_MS = 100
FRAMES_PER_HOP = HOP_MS // FRAME_MS

# FSMN's internal end-of-segment silence. The endpointer adds only a small
# residual on top (see endpointer.py), so total endpoint latency stays in
# the same band as the legacy 400ms hard flush.
FSMN_MAX_END_SIL_MS = int(os.environ.get("FSMN_MAX_END_SIL_MS", "280"))

# Stage-1 gate: FSMN is skipped only after this much continuous
# webrtc-quiet with no open segment. Generous on purpose — the gate is a
# CPU saver for confirmed idle, not a detector.
PREGATE_IDLE_MS = int(os.environ.get("VAD_PREGATE_IDLE_MS", "600"))

_DEFAULT_MODEL_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..",
    "data", "models", "fsmn_vad")

# ── Shared warm model (roadmap #3) ───────────────────────────────────────
# Fsmn_vad_online keeps ALL per-stream state in the caller's param_dict
# (in_cache / frontend / vad_scorer — verified in funasr_onnx.vad_bin);
# the object itself is just the ORT session + config. Loading it ONCE and
# sharing across per-call workers removes the per-call construction lag
# (ONNX session init; first call after boot also paid the funasr import).
# _INFER_LOCK serializes the ~1.6ms forward passes — deliberate on the
# 2-core box (owner doctrine: batch, don't parallelize).
_MODEL_LOCK = threading.Lock()
_INFER_LOCK = threading.Lock()
_SHARED_MODEL = None
_SHARED_MODEL_DIR = None


def preload_model(model_dir: str | None = None):
    """Load (once) and return the shared FSMN model. Called from boot
    warmup so the first call of the process pays nothing."""
    global _SHARED_MODEL, _SHARED_MODEL_DIR
    mdir = os.path.abspath(model_dir or os.environ.get(
        "FSMN_VAD_MODEL_DIR", _DEFAULT_MODEL_DIR))
    with _MODEL_LOCK:
        if _SHARED_MODEL is None or _SHARED_MODEL_DIR != mdir:
            from funasr_onnx import Fsmn_vad_online
            _SHARED_MODEL = Fsmn_vad_online(
                mdir, quantize=False, intra_op_num_threads=1,
                max_end_sil=FSMN_MAX_END_SIL_MS)
            _SHARED_MODEL_DIR = mdir
            logger.info("[VAD-STACK] shared FSMN model loaded (%s)", mdir)
    return _SHARED_MODEL


class FsmnVadStack:
    """WebRTC pre-filter → FSMN-VAD primary, VoiceActivityDetector-compatible."""

    def __init__(
        self,
        sample_rate: int = 16000,
        model_dir: str | None = None,
        minimum_threshold: float = 260.0,
        minimum_rms_floor: float = 60.0,
        barge_in_multiplier: float = 2.0,
        barge_in_minimum: float = 500.0,
        event_sink=None,
    ) -> None:
        self.sample_rate = sample_rate
        self.calibrated = True  # model VAD needs no calibration window
        self._last_energy: float = 0.0
        # Energy floor kept ONLY for the barge-in echo guards that read
        # threshold/barge_in_threshold — never for speech decisions here.
        self._minimum_threshold = minimum_threshold
        self._minimum_rms_floor = minimum_rms_floor
        self._barge_in_multiplier = barge_in_multiplier
        self._barge_in_minimum = barge_in_minimum

        self._model_dir = os.path.abspath(model_dir or os.environ.get(
            "FSMN_VAD_MODEL_DIR", _DEFAULT_MODEL_DIR))

        # Stage-1 pre-filter. Failure to import → gate permanently open
        # (FSMN full-rate); the stack still works, just without the saver.
        self._webrtc = None
        try:
            import webrtcvad
            self._webrtc = webrtcvad.Vad(1)  # permissive by doctrine
        except Exception as exc:
            logger.warning("[VAD-STACK] webrtcvad unavailable (%r) — "
                           "pre-filter disabled, FSMN full-rate", exc)
        self._last_webrtc_speech = time.monotonic()

        # Shared model state (written by worker, read by frame path).
        self._speech_open = False
        self._open_since_ms: float | None = None
        self._events: list[tuple[str, float]] = []  # (kind, stream_ms)
        self._events_lock = threading.Lock()

        # Worker plumbing.
        self._hop_buf = bytearray()
        self._hop_ring: list[bytes] = []  # last hops for cache-reset replay
        self._gate_open = True
        self._stream_ms = 0.0  # audio-time position fed to FSMN
        # Depth 64 = 6.4s of audio: covers the cold funasr_onnx import
        # (~1-3s, librosa) on the worker's first spin-up without dropping
        # the earliest hops of the call.
        self._q: queue.Queue = queue.Queue(maxsize=64)
        self._q_drop_logged = False
        # Concurrency instrumentation (roadmap #3): dropped hops corrupt
        # endpointing SILENTLY — count everything, and above the depth
        # threshold flip this call to visible energy-VAD degradation
        # instead of invisible model starvation.
        self.hops_in = 0
        self.hops_dropped = 0
        self.hop_lat_ms: list[float] = []   # rolling, capped 512
        self.deadline_misses = 0            # hop latency > HOP_MS
        self.degraded = False
        self._degrade_depth = int(os.environ.get("VAD_DEGRADE_DEPTH", "32"))
        self._worker = threading.Thread(
            target=self._worker_loop, name="fsmn-vad", daemon=True)
        self._worker.start()
        # Audio-event classifier rides the same worker (per-hop DSP).
        self._classifier = event_sink

        logger.info(
            "[VAD-STACK] FSMN engine active model=%s max_end_sil=%dms "
            "pregate_idle=%dms webrtc=%s",
            self._model_dir, FSMN_MAX_END_SIL_MS, PREGATE_IDLE_MS,
            "on" if self._webrtc else "off")

    # ── VoiceActivityDetector-compatible surface ─────────────────────

    @property
    def threshold(self) -> float:
        return self._minimum_threshold

    @property
    def last_energy(self) -> float:
        return self._last_energy

    @property
    def barge_in_threshold(self) -> float:
        return max(self._minimum_threshold * self._barge_in_multiplier,
                   self._barge_in_minimum)

    def is_speech(self, frame: bytes) -> bool:
        audio = np.frombuffer(frame, dtype=np.int16)
        self._last_energy = float(np.abs(audio).mean())

        now = time.monotonic()
        webrtc_speech = True
        if self._webrtc is not None:
            try:
                webrtc_speech = self._webrtc.is_speech(frame, self.sample_rate)
            except Exception:
                webrtc_speech = True
        if webrtc_speech:
            self._last_webrtc_speech = now

        # Stage-1 gate: skip the model only in confirmed idle — never
        # while a segment is open (business decisions belong to FSMN).
        idle_ms = (now - self._last_webrtc_speech) * 1000.0
        want_open = self._speech_open or idle_ms < PREGATE_IDLE_MS
        if want_open and not self._gate_open:
            # Re-opening: model cache restarts, replay recent context so
            # the onset isn't scored cold.
            self._gate_open = True
            self._submit(("reset", b"", 0.0))
            for hop in self._hop_ring:
                self._submit(("hop", hop, time.monotonic()))
        elif not want_open and self._gate_open:
            self._gate_open = False

        self._hop_buf.extend(frame)
        hop_bytes = int(self.sample_rate * 2 * HOP_MS / 1000)
        if len(self._hop_buf) >= hop_bytes:
            hop = bytes(self._hop_buf[:hop_bytes])
            del self._hop_buf[:hop_bytes]
            self._hop_ring.append(hop)
            if len(self._hop_ring) > 3:
                self._hop_ring.pop(0)
            if self._gate_open:
                self.hops_in += 1
                self._submit(("hop", hop, time.monotonic()))

        if self.degraded:
            # Visible fallback: energy decision, model verdict ignored.
            return self._last_energy > self._minimum_threshold
        return self._speech_open

    # ── extras consumed by endpointer / classifier / diagnostics ─────

    def stats(self) -> dict:
        """Concurrency health snapshot (callsim harvest + live diagnostics)."""
        lats = sorted(self.hop_lat_ms)
        return {
            "hops_in": self.hops_in,
            "hops_dropped": self.hops_dropped,
            "queue_depth": self._q.qsize(),
            "hop_lat_p50_ms": round(lats[len(lats) // 2], 1) if lats else None,
            "hop_lat_p99_ms": round(lats[int(len(lats) * 0.99)], 1)
            if lats else None,
            "deadline_misses": self.deadline_misses,
            "degraded": self.degraded,
        }

    def pop_events(self) -> list[tuple[str, float]]:
        with self._events_lock:
            out, self._events = self._events, []
        return out

    @property
    def speech_open(self) -> bool:
        return self._speech_open

    def close(self) -> None:
        try:
            self._q.put_nowait(("stop", b""))
        except queue.Full:
            pass

    # ── worker ───────────────────────────────────────────────────────

    def _submit(self, item) -> None:
        depth = self._q.qsize()
        if depth >= self._degrade_depth and not self.degraded:
            # Degrade, don't drop: energy-based speech decisions are worse
            # but VISIBLE; silent hop loss corrupts endpointing untraceably.
            self.degraded = True
            logger.warning("[VAD-STACK] DEGRADED to energy VAD "
                           "(queue depth %d ≥ %d — CPU starved)",
                           depth, self._degrade_depth)
        try:
            self._q.put_nowait(item)
            self._q_drop_logged = False
        except queue.Full:
            self.hops_dropped += 1
            if not self._q_drop_logged:
                self._q_drop_logged = True
                logger.warning("[VAD-STACK] worker queue full — dropping "
                               "hops (CPU starved?)")

    def _worker_loop(self) -> None:
        try:
            model = preload_model(self._model_dir)
        except Exception:
            logger.exception("[VAD-STACK] FSMN model load failed — "
                             "stack is dead, pipeline should use energy")
            self._speech_open = False
            return
        param: dict = {"in_cache": []}
        logger.info("[VAD-STACK] FSMN worker ready")
        while True:
            item = self._q.get()
            kind, payload = item[0], item[1]
            enq_t = item[2] if len(item) > 2 else 0.0
            if kind == "stop":
                return
            if kind == "reset":
                param = {"in_cache": []}
                continue
            if enq_t > 0.0:
                lat = (time.monotonic() - enq_t) * 1000.0
                self.hop_lat_ms.append(lat)
                if len(self.hop_lat_ms) > 512:
                    del self.hop_lat_ms[:256]
                if lat > HOP_MS:
                    self.deadline_misses += 1
                # Recovery: queue drained back to healthy → un-degrade.
                if self.degraded and self._q.qsize() < self._degrade_depth // 4:
                    self.degraded = False
                    logger.info("[VAD-STACK] recovered from degraded mode "
                                "(queue drained)")
            # /32768 is LOAD-BEARING (2026-08-04). funasr's WavFrontendOnline
            # multiplies its input by (1 << 15) before handing it to kaldi
            # fbank (utils/frontend.py), and its own loader is
            # librosa.load() — i.e. the model is trained and calibrated on
            # waveforms in [-1, 1]. Feeding raw int16 made every frame 32768x
            # hot, shifting every log-mel bin by a constant ~2*ln(32768) and
            # putting the model permanently outside its calibration. It still
            # half-worked, which is why this survived since the stack went
            # live on 2026-07-30: measured on call 5aaee2cd it flagged 25
            # speech segments of which 7 (28%) were silence (audio RMS < 100);
            # at the correct scale, 20 segments of which 2 (10%).
            # The classifier below keeps the int16 scale — its DTMF/tone
            # Goertzel bank and AEC_RMS_FLOOR=60 are absolute int16 thresholds,
            # so handing it the normalized copy would read every hop as
            # SILENCE. Two scales, two consumers, deliberately.
            pcm16 = np.frombuffer(payload, dtype=np.int16).astype(np.float32)
            audio = pcm16 / 32768.0
            self._stream_ms += 1000.0 * len(audio) / self.sample_rate
            try:
                with _INFER_LOCK:
                    segs = model(audio, param_dict=param, is_final=False)
            except Exception as exc:
                logger.warning("[VAD-STACK] inference failed: %r — "
                               "resetting cache", exc)
                param = {"in_cache": []}
                continue
            if segs and segs[0]:
                for beg, end in segs[0]:
                    if beg != -1 and end == -1 and not self._speech_open:
                        self._speech_open = True
                        self._open_since_ms = float(beg)
                        self._push_event("speech_start", float(beg))
                    elif end != -1 and self._speech_open:
                        self._speech_open = False
                        self._push_event("speech_end", float(end))
                    elif beg != -1 and end != -1:
                        # closed segment delivered atomically
                        self._push_event("speech_start", float(beg))
                        self._push_event("speech_end", float(end))
                        self._speech_open = False
            if self.hops_in and self.hops_in % 300 == 0:
                logger.info("[VAD-STACK] health %s", self.stats())
            if self._classifier is not None:
                try:
                    self._classifier.update(
                        pcm16, self._speech_open, self._stream_ms)
                except Exception:
                    logger.exception("[VAD-STACK] classifier failed — "
                                     "disabling")
                    self._classifier = None

    def _push_event(self, kind: str, at_ms: float) -> None:
        with self._events_lock:
            self._events.append((kind, at_ms))
            if len(self._events) > 64:
                del self._events[:32]
        logger.info("[VAD-STACK] %s @%.0fms", kind, at_ms)
