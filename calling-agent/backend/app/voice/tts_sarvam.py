"""
Sarvam AI Bulbul streaming TTS via HTTP stream endpoint.

Uses the /text-to-speech/stream endpoint for low-latency chunked audio
delivery. Plays audio through the system speaker using sounddevice with
true streaming playback — chunks are written to the output as they arrive,
minimizing time-to-first-audio.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import queue as _queue
import re
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import sounddevice as sd

from .tts_stream import StreamingTTS, TTSChunk
from .key_rotator import current_key
from ..audio_transport.base import AudioSink
from ..audio_transport.local_speaker_sink import LocalSpeakerSink

logger = logging.getLogger(__name__)

SARVAM_TTS_STREAM_URL = "https://api.sarvam.ai/text-to-speech/stream"
# Anything outside this set gets HTTP 400 from Sarvam on every utterance —
# the call connects but is silent. Guarded here as the last line of defense;
# agent configs are also validated at load time (agent_definition/schema.py).
VALID_MODELS = ("bulbul:v2", "bulbul:v3-beta", "bulbul:v3")
DEFAULT_MODEL = "bulbul:v3"
DEFAULT_SPEAKER = "shubh"
DEFAULT_SAMPLE_RATE = 16_000
# STREAM_TIMEOUT is httpx's read timeout: max wait for the FIRST byte and
# max gap BETWEEN chunks — not total stream duration. Healthy first chunk
# is ~350ms; cut 10.0 → 3.5 (2026-08-03) because a hung stream-open made
# callers sit through 10s+ of dead air after answering (the retry then
# succeeded in ~0.5s). A 3.5s inter-chunk gap still never fires on a
# healthy stream, which delivers continuously once started.
# The FIRST attempt gets an even tighter 2.0s cap: during the 08-03 incident
# hangs were per-request (~1-in-3) and a fresh connection almost always
# answered fast, so failing over sooner directly shrinks greeting dead air
# (2.0 + retry ≈ 2.7s worst case). Later attempts keep the roomier cap to
# avoid burning the whole retry budget against a genuinely slow API.
STREAM_TIMEOUT = 3.5
STREAM_TIMEOUT_FIRST_ATTEMPT = 2.0
CONNECT_TIMEOUT = 5.0
CHUNK_READ_SIZE = 4096

# Shared per-process HTTP client: TTS instances are created per call, but the
# warmup flag is per-process — without sharing, every call after the first
# pays a cold TLS handshake on its first utterance. SARVAM_HTTP_SHARED=false
# restores the old per-instance clients.
SARVAM_HTTP_SHARED = os.environ.get("SARVAM_HTTP_SHARED", "true").lower() == "true"
SARVAM_KEEPALIVE_EXPIRY_S = float(os.environ.get("SARVAM_KEEPALIVE_EXPIRY_S", "300"))

_shared_client: httpx.AsyncClient | None = None


def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(STREAM_TIMEOUT, connect=CONNECT_TIMEOUT),
        limits=httpx.Limits(
            max_keepalive_connections=5,
            keepalive_expiry=SARVAM_KEEPALIVE_EXPIRY_S if SARVAM_HTTP_SHARED else 60,
        ),
    )


# Canned-audio disk cache (greeting, fillers, response-bank lines). A cache
# hit replays stored PCM through the exact same sink path as live synthesis —
# zero Sarvam credits, ~0ms synthesis latency. TTS_CANNED_CACHE=false disables.
TTS_CANNED_CACHE = os.environ.get("TTS_CANNED_CACHE", "true").lower() == "true"
TTS_CACHE_DIR = Path(
    os.environ.get("TTS_CACHE_DIR")
    or Path(__file__).resolve().parents[4] / "data" / "tts_cache"
)
TTS_CACHE_MAX_ENTRIES = int(os.environ.get("TTS_CACHE_MAX_ENTRIES", "500"))

_LANG_CODE_MAP = {
    "en": "en-IN",
    "hi": "hi-IN",
    "gu": "gu-IN",
    # Full platform roster (voice_catalog.PLATFORM_LANGUAGES). A language
    # missing here silently falls back to en-IN, which pushes Indic text
    # through the English frontend — mispronounced, halting delivery.
    "mr": "mr-IN",
    "ta": "ta-IN",
    "te": "te-IN",
    "kn": "kn-IN",
    "ml": "ml-IN",
    "bn": "bn-IN",
    "pa": "pa-IN",
    "od": "od-IN",
}

# Adaptive speech rate. Two documented Bulbul behaviours skew perceived speed
# away from the configured pace (docs.sarvam.ai TTS best-practices):
#   • long, lightly-punctuated sentences are delivered rushed — on top of the
#     fleet-wide 1.15 base pace they read as "too fast";
#   • Hindi delivery runs audibly slower than English at the same pace value,
#     so a base pace tuned on English sounds sluggish in hi.
# Corrections are per-request multipliers on the agent's configured pace
# (agent.yaml tts_voices.<lang>.pace stays the base knob), clamped to
# bulbul:v3's supported range. TTS_ADAPTIVE_PACE=false restores raw base pace.
_PACE_MIN, _PACE_MAX = 0.5, 2.0
TTS_ADAPTIVE_PACE = os.environ.get("TTS_ADAPTIVE_PACE", "true").lower() == "true"
_LANG_PACE_FACTORS = {
    lang: float(os.environ.get(f"TTS_PACE_FACTOR_{lang.upper()}", default))
    for lang, default in (("hi", "1.08"), ("gu", "1.0"), ("en", "1.0"))
}
# A sentence (text between .!?। boundaries) at/above this length gets damped —
# the threshold is on sentence length, not chunk length, because a chunk
# packing several short well-punctuated sentences paces fine.
_LONG_SENT_CHARS = int(os.environ.get("TTS_PACE_LONG_SENT_CHARS", "140"))
_LONG_SENT_FACTOR = float(os.environ.get("TTS_PACE_LONG_SENT_FACTOR", "0.93"))
# Folded into the canned-audio cache key: retuning any knob above must miss
# the cache, or old-pace PCM would replay for greetings/fillers forever.
_PACE_CFG_TAG = "|".join(
    [str(int(TTS_ADAPTIVE_PACE))]
    + [f"{k}:{v}" for k, v in sorted(_LANG_PACE_FACTORS.items())]
    + [str(_LONG_SENT_CHARS), str(_LONG_SENT_FACTOR)]
)


def _effective_pace(base: float, language: str, text: str) -> float:
    """Per-request pace: base × language factor × long-sentence damping."""
    pace = base if base else 1.0
    if TTS_ADAPTIVE_PACE:
        pace *= _LANG_PACE_FACTORS.get(language, 1.0)
        longest = max(
            (len(s) for s in _SENTENCE_BOUNDARY_RE.split(text)), default=0
        )
        if longest >= _LONG_SENT_CHARS:
            pace *= _LONG_SENT_FACTOR
    return round(min(max(pace, _PACE_MIN), _PACE_MAX), 3)

# Incremental-playback chunking. Long utterances are synthesized in parts and
# flushed to the sink part-by-part, so telephony playback of part 1 starts
# while part 2 is still being synthesized. Without this, TelephonySink holds
# the entire utterance until the full Sarvam stream completes — a 14s reply
# adds ~8s of dead air before the caller hears anything.
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?।])\s+")
_CLAUSE_BOUNDARY_RE = re.compile(r"(?<=[,;:])\s+")
_FIRST_CHUNK_MAX_CHARS = 90    # small first part → fastest speech onset
_CHUNK_MAX_CHARS = 180
# Matches the +0.1s inter-file pacing gap in conversation_call_bridge.py's
# _playback_loop; used when reporting total utterance duration for echo-mute.
_INTER_WAV_PACING_S = 0.1

# Early first-flush: the caller used to hear nothing until part 1's ENTIRE
# Sarvam stream completed — measured median 806ms / max 1882ms of extra dead
# air per turn (2026-07-23 latency audit), because a 90-char Indic part can
# synthesize to ~5s of audio. Instead, once ≥ _EARLY_FLUSH_MIN_S of PCM is
# buffered for the first part, flush it as its own WAV so playback starts
# while the rest still synthesizes. The bridge paces files with a ~100ms gap,
# so the cut must land in an inter-word pause, never mid-word: after the
# minimum is buffered we keep scanning the newest chunk's tail RMS and flush
# at the first near-silence; _EARLY_FLUSH_MAX_S caps the wait when someone
# speaks without pausing (a mid-word seam is still better than +1s dead air).
TTS_EARLY_FLUSH = os.environ.get("TTS_EARLY_FLUSH", "true").lower() == "true"
_EARLY_FLUSH_MIN_S = float(os.environ.get("TTS_EARLY_FLUSH_MIN_S", "0.9"))
_EARLY_FLUSH_MAX_S = float(os.environ.get("TTS_EARLY_FLUSH_MAX_S", "1.6"))
# Hard flush ceiling (2026-08-03 fleet audit, defect #2): no single WAV may
# exceed this much audio. Unpunctuated LLM sentences produced 8–16s
# monoliths — while one plays, the reply to the caller's NEXT turn queues
# behind it (up to +10s wire latency), and barge-in has that much more to
# kill. Applies to live synthesis (mid-part) and cached replay alike.
_MAX_FLUSH_S = float(os.environ.get("TTS_MAX_FLUSH_S", "4.0"))
# Cached openers additionally cut their FIRST piece short so the caller
# hears the agent fast (greeting dead-air, fleet defect #4).
_CACHE_FIRST_FLUSH_S = float(os.environ.get("TTS_CACHE_FIRST_FLUSH_S", "2.0"))
_EARLY_FLUSH_TAIL_MS = 40          # window scanned for the pause
_EARLY_FLUSH_SILENCE_RMS = 300     # ≲1% full-scale ≈ inter-word gap

# ── Did we get back what we asked for? ──────────────────────────────────────
# Nothing downstream checks the AUDIO against the TEXT, and two failure modes
# live in that gap:
#
#   • Bulbul occasionally appends a hallucinated clause to a short line. Live
#     2026-09-16, "Am I speaking to Piyush Gupta?" came back as "…Piyush
#     Gupta OR" and "Am I speaking to Yogesh Kumar?" as "…Yogesh Kumar 40,
#     the man is Kumar?". Both were then CACHED (below) and replayed to those
#     contacts on every subsequent call — which is why the owner heard it
#     "always".
#   • A stream that ignores speech_sample_rate. `_check_stream_rate` was
#     written for exactly that, but it can only read a RIFF header and the
#     stream is headerless linear16 — so it has never fired. 16 kHz PCM
#     labelled 8 kHz is a 2.0x duration inflation and lands here instead.
#
# The invariant both violate is the same: audio duration must be plausible
# for the text that produced it. `_expected_speech_seconds` is a floor model
# (per-sentence lead-in/tail + per-character rate, Indic characters counted
# double because one codepoint carries more speech). Calibrated 2026-09-16
# over 88 live Bulbul syntheses spanning en/hi/gu, speakers shubh + simran,
# paces 1.0–1.3 and 5–129 characters: the WORST legitimate ratio measured
# 0.95. The two confirmed hallucinations measured 1.27 and 1.44.
#
# A false positive costs one cache miss (live synthesis — what an uncached
# line does anyway). A false negative is a corrupted line played to a real
# contact forever. The threshold sits accordingly.
_DURATION_SENTENCE_S = 0.55        # lead-in + trailing pad, per sentence
_DURATION_PER_CHAR_S = 0.052       # steady-state speech rate
TTS_DURATION_MAX_RATIO = float(
    os.environ.get("TTS_DURATION_MAX_RATIO", "1.2"))


def _expected_speech_seconds(text: str) -> float:
    """Plausibility model for how long `text` should take to speak."""
    t = (text or "").strip()
    if not t:
        return 0.0
    sentences = max(1, len([s for s in _SENTENCE_BOUNDARY_RE.split(t)
                            if s.strip()]))
    weighted = sum(2 if ord(c) > 0x0900 else 1 for c in t)
    return _DURATION_SENTENCE_S * sentences + _DURATION_PER_CHAR_S * weighted


def _duration_ratio(text: str, pcm_bytes: int, sample_rate: int) -> float:
    """Actual audio duration ÷ the plausibility model. >1 means too long."""
    expected = _expected_speech_seconds(text)
    if expected <= 0.0 or sample_rate <= 0:
        return 0.0
    return (pcm_bytes / (2.0 * sample_rate)) / expected


def _duration_implausible(text: str, pcm_bytes: int,
                          sample_rate: int) -> float | None:
    """The ratio when the audio is too long for the text, else None."""
    if TTS_DURATION_MAX_RATIO <= 0.0:
        return None
    ratio = _duration_ratio(text, pcm_bytes, sample_rate)
    return ratio if ratio > TTS_DURATION_MAX_RATIO else None


def _tail_is_pause(pcm: bytes, sample_rate: int) -> bool:
    """True when the last _EARLY_FLUSH_TAIL_MS of `pcm` is near-silent."""
    tail_bytes = int(sample_rate * 2 * _EARLY_FLUSH_TAIL_MS / 1000)
    tail = pcm[-tail_bytes:]
    if len(tail) < tail_bytes:
        return False
    try:
        import audioop
        return audioop.rms(tail, 2) < _EARLY_FLUSH_SILENCE_RMS
    except Exception:
        return True  # can't judge — don't hold audio hostage


def _split_for_streaming(text: str) -> list[str]:
    """Split TTS text into parts synthesized and flushed incrementally.

    Splits at sentence enders first; LLM replies are often comma-run-ons with
    no sentence-ending punctuation until the very end, so any "sentence"
    longer than the chunk cap is further split at clause punctuation. Pieces
    are then greedily packed: a small first part for fast onset, larger
    follow-ups so pacing gaps stay rare.
    """
    text = text.strip()
    if not text:
        return []

    pieces: list[str] = []
    for sentence in _SENTENCE_BOUNDARY_RE.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > _CHUNK_MAX_CHARS:
            pieces.extend(
                p.strip() for p in _CLAUSE_BOUNDARY_RE.split(sentence) if p.strip()
            )
        else:
            pieces.append(sentence)

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        limit = _FIRST_CHUNK_MAX_CHARS if not chunks else _CHUNK_MAX_CHARS
        if current and len(current) + 1 + len(piece) > limit:
            chunks.append(current)
            current = piece
        else:
            current = f"{current} {piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


class SarvamTTS(StreamingTTS):
    """
    Sarvam AI Bulbul TTS — streams audio via HTTP and plays through an AudioSink.
    """

    # Counter for empty/near-empty text attempts (for metrics)
    _empty_text_count = 0

    # Class-level flag: warmup runs at most once per process regardless of
    # how many calls / pipeline instances are created.
    _warmup_done_flag: bool = False

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        speaker: str = DEFAULT_SPEAKER,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        pace: float = 1.0,
        output_device: int | str | None = None,
        audio_sink: AudioSink | None = None,
    ) -> None:
        super().__init__(audio_sink=audio_sink or LocalSpeakerSink(
            sample_rate=sample_rate,
            output_device=output_device
        ))
        self._api_key = api_key or os.environ.get("SARVAM_API_KEY", "")
        if model not in VALID_MODELS:
            logger.warning("[TTS:Sarvam] invalid model %r — using %s "
                           "(valid: %s)", model, DEFAULT_MODEL,
                           ", ".join(VALID_MODELS))
            model = DEFAULT_MODEL
        self._model = model
        self._speaker = speaker
        self._sample_rate = sample_rate
        self._pace = pace
        self._client: httpx.AsyncClient | None = None
        self._language = "en"
        self._output_device = output_device
        # Canned-audio cache capture (see speak_cached)
        self._capture_buf: bytearray | None = None
        self._last_speak_clean = False
        # Characters actually POSTed to Sarvam by the last speak() — the
        # billable quantity. Differs from len(text) when barge-in cuts the
        # part loop short. Survives _reset() (which clears only _cancelled /
        # _playing) so the credit-tracking subclass can read it afterwards.
        self._last_speak_billed_chars = 0
        # Set when a mid-stream stall truncated the utterance — such audio
        # must never be cached (it would replay truncated forever).
        self._stream_truncated = False
        # Audio-duration ÷ plausibility model for the last speak(). >1.0
        # means Bulbul returned more speech than the text asked for.
        self._last_speak_duration_ratio = 0.0
        # ── Was the agent actually AUDIBLE on this call? ─────────────────
        # A speak() that returns without emitting a single audio chunk is a
        # mute turn: the caller hears nothing and the transcript still logs
        # the line as spoken. Counted so call teardown can tell "the callee
        # said nothing" apart from "we said nothing" — the two are recorded
        # identically otherwise, and the customer gets blamed for our
        # outage (RUNBOOK §34).
        self.spoke_utterances = 0
        self.mute_utterances = 0
        self.credit_blocked = False

        if not self._api_key:
            logger.warning("[TTS:Sarvam] SARVAM_API_KEY not set — calls will fail")
        else:
            masked = self._api_key[:6] + "…" + self._api_key[-4:]
            logger.info("[TTS:Sarvam] API key loaded: %s", masked)

    def set_language(self, lang: str) -> None:
        self._language = lang

    def set_voice(self, model: str | None = None, speaker: str | None = None,
                  pace: float | None = None) -> None:
        """v2: per-language voice on mid-call language switch. Invalid models
        are ignored (the current voice keeps playing rather than 400-ing)."""
        if model:
            if model in VALID_MODELS:
                self._model = model
            else:
                logger.warning("[TTS:Sarvam] set_voice: invalid model %r ignored", model)
        if speaker:
            self._speaker = speaker
        if pace:
            try:
                p = float(pace)
                if 0.5 <= p <= 2.0:
                    self._pace = p
            except (TypeError, ValueError):
                pass

    def _get_client(self) -> httpx.AsyncClient:
        if SARVAM_HTTP_SHARED:
            global _shared_client
            if _shared_client is None or _shared_client.is_closed:
                _shared_client = _make_client()
            return _shared_client
        if self._client is None:
            # keepalive_expiry=60: idle connections older than 60s are never
            # reused, so a call after a long pause always gets a fresh TLS
            # handshake instead of dying on a silently-dropped server socket.
            self._client = _make_client()
        return self._client

    def _reset_client(self) -> None:
        """Drop the active client so the next request opens a fresh one."""
        global _shared_client
        if SARVAM_HTTP_SHARED:
            _shared_client = None
        else:
            self._client = None

    async def warmup(self) -> None:
        """Pre-open a TLS connection to the Sarvam host so the first real
        speak() call doesn't pay DNS+TCP+TLS cost (~300ms cold start).

        Mirrors SarvamSTT.warmup(): a cheap HEAD lands the connection in the
        httpx per-host pool; errors are silently ignored. Runs at most once
        per process (class flag).
        """
        if SarvamTTS._warmup_done_flag:
            logger.debug("[TTS:Sarvam] Warmup skipped — already done this process")
            return
        try:
            client = self._get_client()
            await asyncio.wait_for(client.head(SARVAM_TTS_STREAM_URL), timeout=3.0)
            SarvamTTS._warmup_done_flag = True
            logger.info("[TTS:Sarvam] Connection warmed (first time this process)")
        except Exception as exc:
            logger.debug("[TTS:Sarvam] Warmup skipped: %s", exc)

    async def speak(self, text: str) -> None:
        self._reset()
        self._playing = True
        # Zero until a part is actually submitted — every early return below
        # leaves Sarvam untouched, and so must leave the bill untouched.
        self._last_speak_billed_chars = 0

        if not self._api_key:
            logger.error("[TTS:Sarvam] No API key — MUTE TURN, the caller "
                         "hears silence")
            # No key is silence just as surely as no credits is. Count it, or
            # teardown reads the resulting dead-air call as the CALLEE saying
            # nothing (see TelephonySession._agent_was_mute).
            self.mute_utterances += 1
            self._playing = False
            return

        # Check for empty or near-empty text
        if not text or len(text.strip()) < 2:
            logger.warning(
                "[TTS:Sarvam] Attempting to speak empty or near-empty text: %r", text
            )
            self.__class__._empty_text_count += 1
            if self.__class__._empty_text_count % 10 == 0:
                logger.warning(
                    "[TTS:Sarvam] Empty text count: %d", self.__class__._empty_text_count
                )
            self._playing = False
            self._reset()
            return

        chunk_count = 0

        # Ensure sink is started
        await self._sink.start()

        parts = _split_for_streaming(text)
        if len(parts) > 1:
            logger.info(
                "[TTS:Sarvam] Utterance split into %d parts for incremental playback",
                len(parts),
            )
        total_duration = 0.0
        flushed_parts = 0
        audio_bytes = 0
        speak_t0 = time.monotonic()

        # Early-flush state (first part of the utterance only).
        sink_flushable = hasattr(self._sink, 'flush')
        early_min_bytes = int(self._sample_rate * 2 * _EARLY_FLUSH_MIN_S)
        early_max_bytes = int(self._sample_rate * 2 * _EARLY_FLUSH_MAX_S)

        try:
            for part in parts:
                if self._cancelled:
                    break
                part_bytes = 0
                part_tail = b""
                early_flushed = False
                unflushed_bytes = 0
                ceiling_bytes = int(self._sample_rate * 2 * _MAX_FLUSH_S)
                # This part is about to be POSTed — bill it. Parts after a
                # barge-in never reach here, so they are never charged.
                self._last_speak_billed_chars += len(part)
                async for chunk in self._stream_audio(part):
                    if self._cancelled:
                        logger.info("[TTS:Sarvam] Cancelled mid-stream")
                        break

                    await self._sink.write_frame(chunk.audio)
                    unflushed_bytes += len(chunk.audio)
                    audio_bytes += len(chunk.audio)
                    if self._capture_buf is not None:
                        self._capture_buf += chunk.audio
                    chunk_count += 1
                    if chunk_count == 1:
                        logger.info(
                            "[TTS:Sarvam] First chunk sent to sink (%.0fms)",
                            (time.monotonic() - speak_t0) * 1000,
                        )

                    # Early first-flush: get audio to the caller before the
                    # part's stream completes. First part only, at most once,
                    # cut at an inter-word pause (or the hard cap).
                    if (TTS_EARLY_FLUSH and sink_flushable
                            and flushed_parts == 0 and not early_flushed
                            and self._capture_buf is None):  # never while caching
                        part_bytes += len(chunk.audio)
                        part_tail = (part_tail + chunk.audio)[-4096:]
                        if part_bytes >= early_min_bytes and (
                            part_bytes >= early_max_bytes
                            or _tail_is_pause(part_tail, self._sample_rate)
                        ):
                            await self._sink.flush()
                            total_duration += getattr(
                                self._sink, 'last_flush_duration', 0.0)
                            flushed_parts += 1
                            early_flushed = True
                            unflushed_bytes = 0
                            logger.info(
                                "[TTS:Sarvam] Early flush %.2fs after %.0fms",
                                part_bytes / (self._sample_rate * 2),
                                (time.monotonic() - speak_t0) * 1000,
                            )

                    # Flush ceiling: an unpunctuated part can synthesize to
                    # many seconds of audio in one piece — cut it so playback
                    # stays interleavable (prefer a pause, force at +25%).
                    if (sink_flushable and self._capture_buf is None
                            and unflushed_bytes >= ceiling_bytes):
                        tail = chunk.audio[-4096:]
                        if (_tail_is_pause(tail, self._sample_rate)
                                or unflushed_bytes >= ceiling_bytes * 1.25):
                            await self._sink.flush()
                            total_duration += getattr(
                                self._sink, 'last_flush_duration', 0.0)
                            flushed_parts += 1
                            logger.info(
                                "[TTS:Sarvam] Ceiling flush %.2fs "
                                "(part still streaming)",
                                unflushed_bytes / (self._sample_rate * 2))
                            unflushed_bytes = 0
                if self._cancelled:
                    break
                # Incremental flush: hand this part's audio to the transport
                # now so playback starts before later parts are synthesized.
                if hasattr(self._sink, 'flush'):
                    await self._sink.flush()
                    total_duration += getattr(self._sink, 'last_flush_duration', 0.0)
                    flushed_parts += 1

        except Exception:
            logger.exception("[TTS:Sarvam] speak() failed")
        finally:
            # We don't stop the sink here because it might be shared or need to finish playing
            self._playing = False
            if hasattr(self._sink, 'flush') and not self._cancelled:
                await self._sink.flush()
                # The pipeline reads last_flush_duration after speak() returns
                # to size its echo-mute window for the WHOLE utterance. With
                # incremental flushes it only holds the last part, so restore
                # the utterance total (plus the bridge's pacing gap per extra
                # WAV file).
                if flushed_parts > 1 and hasattr(self._sink, 'last_flush_duration'):
                    self._sink.last_flush_duration = (
                        total_duration + _INTER_WAV_PACING_S * (flushed_parts - 1)
                    )
            if not self._cancelled:
                logger.info(
                    "[TTS:Sarvam] Playback complete (%d chunks, %d parts)",
                    chunk_count, flushed_parts or 1,
                )
                if chunk_count > 0:
                    self.spoke_utterances += 1
                else:
                    self.mute_utterances += 1
                    logger.error(
                        "[TTS:Sarvam] MUTE TURN — utterance produced no audio; "
                        "the caller heard silence (mute=%d spoke=%d)",
                        self.mute_utterances, self.spoke_utterances,
                    )
            # Did the audio match the text? A hallucinated clause or a stream
            # at the wrong sample rate both land as "far too long for these
            # words". Nothing can be un-spoken here — the chunks are already
            # with the caller — but it is the one place the anomaly is
            # visible, and speak_cached reads the same verdict to refuse the
            # cache write that would replay it forever.
            self._last_speak_duration_ratio = _duration_ratio(
                text, audio_bytes, self._sample_rate)
            if not self._cancelled and chunk_count > 0:
                _bad = _duration_implausible(text, audio_bytes,
                                             self._sample_rate)
                if _bad is not None:
                    logger.warning(
                        "[TTS:Sarvam] Audio implausible for text — %.2fs for "
                        "%d chars (%.2fx the expected ceiling). Bulbul may "
                        "have appended speech the caller heard, or the "
                        "stream ignored speech_sample_rate=%d. Text: %r",
                        audio_bytes / (2.0 * self._sample_rate), len(text),
                        _bad, self._sample_rate, text[:60])
            # _reset() clears _cancelled — preserve completion state for
            # speak_cached's cache-write decision. A truncated stream is
            # never clean: caching it would replay the cut-off audio on
            # every future hit.
            self._last_speak_clean = (not self._cancelled
                                      and chunk_count > 0
                                      and not self._stream_truncated)
            self._reset()

    # ------------------------------------------------------------------
    # Canned-audio cache
    # ------------------------------------------------------------------

    def _cache_path(self, text: str) -> Path | None:
        if not TTS_CANNED_CACHE:
            return None
        lang_code = _LANG_CODE_MAP.get(self._language, "en-IN")
        key = (
            f"{self._model}|{self._speaker}|{self._pace}|{lang_code}|"
            f"{self._sample_rate}|{_PACE_CFG_TAG}|{text}"
        )
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return TTS_CACHE_DIR / f"{digest}.pcm"

    def _write_cache(self, path: Path, pcm: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{id(self)}.tmp")
            tmp.write_bytes(pcm)
            os.replace(tmp, path)
            logger.info(
                "[TTS:Sarvam] Cached utterance (%.1fs, %d bytes)",
                len(pcm) / (self._sample_rate * 2), len(pcm),
            )
            self._prune_cache(path.parent)
        except Exception:
            logger.warning("[TTS:Sarvam] Cache write failed", exc_info=True)

    @staticmethod
    def _prune_cache(cache_dir: Path) -> None:
        try:
            entries = sorted(
                cache_dir.glob("*.pcm"), key=lambda p: p.stat().st_mtime
            )
            excess = len(entries) - TTS_CACHE_MAX_ENTRIES
            for stale in entries[: max(excess, 0)]:
                stale.unlink(missing_ok=True)
        except Exception:
            logger.debug("[TTS:Sarvam] Cache prune failed", exc_info=True)

    async def speak_cached(self, text: str) -> None:
        """speak() with a disk cache for canned lines (greeting, fillers,
        response-bank phrases).

        Hit: replay stored PCM through the normal sink path — flush,
        playback_busy_until, and echo-mute sizing behave exactly as live
        synthesis, and zero Sarvam credits are consumed (no API call, so
        the Tracking subclass correctly records nothing). Miss: synthesize
        live via self.speak() (polymorphic — credit tracking unchanged) and
        store the PCM on clean completion. Any cache error falls through to
        live synthesis.
        """
        path = self._cache_path(text)
        if path is not None:
            pcm = b""
            try:
                pcm = path.read_bytes()
            except FileNotFoundError:
                pass
            except Exception:
                logger.warning(
                    "[TTS:Sarvam] Cache read failed for %r — synthesizing live",
                    text[:40], exc_info=True,
                )
            # Sanity ceiling: an entry that is far too long for its text is a
            # frozen Bulbul hallucination (or a wrong-rate stream). Evict it
            # and synthesize live — the entry was written before this gate
            # existed, or by an older build. Without this, the 2026-09-16
            # "Am I speaking to Piyush Gupta OR" line would replay to that
            # contact until the LRU happened to drop it.
            _stale = _duration_implausible(text, len(pcm), self._sample_rate)
            if pcm and _stale is not None:
                logger.warning(
                    "[TTS:Sarvam] Evicting implausible cache entry "
                    "(%.2fs, %.2fx expected) for %r — synthesizing live",
                    len(pcm) / (self._sample_rate * 2), _stale, text[:50])
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                pcm = b""
            # Sanity floor: anything under 150ms of audio is a corrupt entry.
            if len(pcm) >= int(self._sample_rate * 2 * 0.15):
                self._reset()
                self._playing = True
                try:
                    await self._sink.start()
                    logger.info(
                        "[TTS:Sarvam] Cache HIT (%.1fs, %s, %s) for %r",
                        len(pcm) / (self._sample_rate * 2),
                        _LANG_CODE_MAP.get(self._language, "en-IN"),
                        self._speaker, text[:50],
                    )
                    try:
                        os.utime(path)  # LRU touch
                    except OSError:
                        pass
                    # Chunked replay (fleet defects #2/#4): a long cached
                    # opener used to land as ONE WAV — first audio waited on
                    # the whole file and barge-in had the whole file to kill.
                    # First piece is short so the caller hears the agent
                    # fast; the rest flushes in ceiling-sized pieces.
                    if hasattr(self._sink, "flush"):
                        first_b = int(self._sample_rate * 2
                                      * _CACHE_FIRST_FLUSH_S)
                        max_b = int(self._sample_rate * 2 * _MAX_FLUSH_S)
                        min_tail_b = int(self._sample_rate * 2 * 0.25)
                        off = 0
                        piece = min(first_b, len(pcm))
                        while off < len(pcm) and not self._cancelled:
                            # Never leave a sub-250ms orphan piece — the
                            # sink's WAV validator rejects <100ms files.
                            if len(pcm) - off - piece < min_tail_b:
                                piece = len(pcm) - off
                            await self._sink.write_frame(pcm[off:off + piece])
                            await self._sink.flush()
                            off += piece
                            piece = max_b
                    else:
                        await self._sink.write_frame(pcm)
                    self._playing = False
                    self._reset()
                    return
                except Exception:
                    logger.exception(
                        "[TTS:Sarvam] Cache replay failed — synthesizing live"
                    )
                    self._playing = False
                    self._reset()
        # Miss (or cache disabled/error) — synthesize live, capturing chunks.
        self._capture_buf = bytearray() if path is not None else None
        self._last_speak_clean = False
        try:
            await self.speak(text)
        finally:
            buf, self._capture_buf = self._capture_buf, None
            if path is not None and buf and self._last_speak_clean:
                # One bad synthesis must not become permanent. speak() already
                # warned; refusing the write is what keeps the next caller
                # from hearing it — a cache miss costs one live synthesis,
                # a cached hallucination costs every future call.
                _bad = _duration_implausible(text, len(buf), self._sample_rate)
                if _bad is not None:
                    logger.warning(
                        "[TTS:Sarvam] NOT caching implausible utterance "
                        "(%.2fx expected): %r", _bad, text[:50])
                else:
                    self._write_cache(path, bytes(buf))

    def _check_stream_rate(self, first_chunk: bytes) -> None:
        """Detect a sample-rate the sink will mislabel.

        Every sink downstream stamps WAV headers with the *requested* rate
        without verifying what Sarvam actually sent — if Sarvam ever falls
        back to its 24 kHz default while we label the PCM 8 kHz, FreeSWITCH
        plays it at 1/3 speed, octaves down, with zero errors anywhere. The
        stream is normally headerless linear16, but if a RIFF header shows
        up we can read the real rate; detection only, audio is untouched.
        """
        if len(first_chunk) < 28 or first_chunk[:4] != b"RIFF" \
                or first_chunk[8:12] != b"WAVE":
            return
        actual = int.from_bytes(first_chunk[24:28], "little")
        if actual and actual != self._sample_rate:
            logger.warning(
                "[TTS:Sarvam] Stream declares %d Hz but sink assumes %d Hz — "
                "playback will run at %.2fx speed",
                actual, self._sample_rate, self._sample_rate / actual,
            )

    async def _stream_audio(self, text: str):
        lang_code = _LANG_CODE_MAP.get(self._language, "en-IN")
        pace = _effective_pace(self._pace, self._language, text)

        payload = {
            "text": text,
            "target_language_code": lang_code,
            "speaker": self._speaker,
            "model": self._model,
            "pace": pace,
            "speech_sample_rate": self._sample_rate,
            "output_audio_codec": "linear16",
        }

        # Budget covers both connection retries AND hot-swapping through the
        # backup key pool when a key returns 402 (out of credits).
        max_retries = 8
        # Once any audio from this utterance has reached the sink, a retry
        # would re-stream the utterance FROM THE START — the caller hears the
        # sentence twice (the double-speak class of bug). Truncating the tail
        # is the lesser evil, so mid-stream failures after first audio return
        # instead of retrying.
        yielded_any = False
        self._stream_truncated = False
        for attempt in range(max_retries):
            if self._cancelled:
                return

            # Always use the live key so a rotation (ours or the watchdog's)
            # is picked up on the very next attempt without a restart.
            _provider = getattr(self, "key_provider", None)
            self._api_key = _provider() if _provider is not None \
                else current_key()
            client = self._get_client()
            try:
                async with client.stream(
                    "POST",
                    SARVAM_TTS_STREAM_URL,
                    headers={
                        "api-subscription-key": self._api_key,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=httpx.Timeout(
                        STREAM_TIMEOUT_FIRST_ATTEMPT if attempt == 0
                        else STREAM_TIMEOUT,
                        connect=CONNECT_TIMEOUT,
                    ),
                ) as response:
                    if response.status_code == 402:
                        body = await response.aread()
                        logger.error(
                            "[SARVAM:CREDITS] TTS HTTP 402 — %s — rotating key from pool.",
                            body[:200],
                        )
                        from .key_rotator import retire as _retire_key
                        # Rotate first, then stop leasing the dead key — the
                        # same order as STT (see key_rotator.retire).
                        new_key = _retire_key(self._api_key)
                        if new_key:
                            self._api_key = new_key
                            self._reset_client()  # force fresh TLS with new key
                            logger.info(
                                "[TTS:Sarvam] Key rotated — retrying speak() with new key"
                            )
                            continue
                        logger.error(
                            "[TTS:Sarvam] No backup key available — cannot speak"
                        )
                        self.credit_blocked = True
                        return
                    if response.status_code != 200:
                        body = await response.aread()
                        logger.error(
                            "[TTS:Sarvam] HTTP %d — %s",
                            response.status_code, body[:500],
                        )
                        return

                    logger.info("[TTS:Sarvam] Streaming started for %r (%s, %s, pace=%.2f)",
                                text[:50], lang_code, self._speaker, pace)

                    first_chunk = True
                    async for raw_chunk in response.aiter_bytes(CHUNK_READ_SIZE):
                        if self._cancelled:
                            return
                        if first_chunk:
                            first_chunk = False
                            self._check_stream_rate(raw_chunk)
                        yielded_any = True
                        yield TTSChunk(
                            audio=raw_chunk,
                            text_segment=text,
                            is_last=False,
                        )

                    logger.info("[TTS:Sarvam] Stream complete")
                    return

            except httpx.TimeoutException:
                logger.warning("[TTS:Sarvam] Stream timeout (attempt %d/%d) for %r",
                               attempt + 1, max_retries, text[:50])
                # Force new client on next attempt — timeout may mean the
                # connection is in a bad state (partially sent request).
                self._reset_client()
                if yielded_any:
                    logger.error("[TTS:Sarvam] Mid-stream stall after audio was "
                                 "delivered — dropping remainder of %r to avoid "
                                 "double-speak", text[:50])
                    self._stream_truncated = True
                    return
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as exc:
                # Stale or dropped connection — always reset so the retry
                # opens a fresh TLS socket.  ConnectError was previously
                # swallowed by the generic handler below and caused no retry,
                # leading to silent failure (no audio / elongated first word
                # from a corrupted partial stream).
                logger.warning("[TTS:Sarvam] Stale/dropped connection (attempt %d/%d): %s — resetting client",
                               attempt + 1, max_retries, exc)
                self._reset_client()
                if yielded_any:
                    logger.error("[TTS:Sarvam] Connection dropped after audio was "
                                 "delivered — dropping remainder of %r to avoid "
                                 "double-speak", text[:50])
                    self._stream_truncated = True
                    return
            except Exception:
                logger.exception("[TTS:Sarvam] Stream error (attempt %d/%d)",
                                 attempt + 1, max_retries)
                break

            if attempt < max_retries - 1:
                await asyncio.sleep(0.2)

    async def stop(self) -> None:
        await super().stop()
        await self._sink.stop()

    async def close(self) -> None:
        global _shared_client
        if SARVAM_HTTP_SHARED:
            client, _shared_client = _shared_client, None
        else:
            client, self._client = self._client, None
        if client:
            await client.aclose()
            logger.info("[TTS:Sarvam] Client closed")
