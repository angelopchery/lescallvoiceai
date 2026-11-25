"""
OpenAI Whisper Speech-to-Text provider (fallback).

Uses the OpenAI audio transcription API.
Requires OPENAI_API_KEY environment variable.
"""

from __future__ import annotations

import os
import logging

from openai import AsyncOpenAI

from .base import STTProvider, STTResult, normalize_pcm_audio, pcm_to_wav

logger = logging.getLogger(__name__)

# ISO-639-1 codes whisper-1 accepts. Anything else is a hard HTTP 400, not a
# degraded transcript — notably `gu` (Gujarati), one of this platform's three
# supported languages. Source: OpenAI's published whisper-1 language list.
WHISPER_LANGUAGES = frozenset({
    "af", "ar", "hy", "az", "be", "bs", "bg", "ca", "zh", "hr", "cs", "da",
    "nl", "en", "et", "fi", "fr", "gl", "de", "el", "he", "hi", "hu", "is",
    "id", "it", "ja", "kn", "kk", "ko", "lv", "lt", "mk", "ms", "mr", "mi",
    "ne", "no", "fa", "pl", "pt", "ro", "ru", "sr", "sk", "sl", "es", "sw",
    "sv", "tl", "ta", "th", "tr", "uk", "ur", "vi", "cy",
})

# Whisper is the LAST-RESORT provider: by the time it runs the caller has
# already waited through Sarvam's read timeout. Observed healthy latency is
# ~1.5s; 8s bounds the worst-case turn without giving up on slow successes.
# (Was 15.0 — tightened 2026-08-03 during the Sarvam-hang incident.)
WHISPER_TIMEOUT = 8.0


class OpenAIWhisperSTT(STTProvider):
    """
    OpenAI Whisper STT provider.

    Converts PCM audio to WAV, sends via the OpenAI SDK's async
    transcription endpoint, and returns the transcript.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "whisper-1",
        timeout: float = WHISPER_TIMEOUT,
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._model = model
        self._timeout = timeout
        self._client: AsyncOpenAI | None = None

        if not self._api_key:
            logger.warning("[STT:Whisper] OPENAI_API_KEY not set — calls will fail")

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._api_key,
                timeout=self._timeout,
            )
        return self._client

    async def transcribe(
        self,
        audio_bytes: bytes,
        sample_rate: int = 16_000,
        language: str = "en",
    ) -> STTResult:
        # Same signal the primary hears. Sarvam normalizes before upload
        # (sarvam.py); Whisper never did, so the LAST-RESORT provider was
        # always given the QUIETER version of audio the primary had already
        # struggled with — on telephony callers that is exactly backwards.
        audio_bytes = normalize_pcm_audio(audio_bytes)
        wav_data = pcm_to_wav(audio_bytes, sample_rate=sample_rate)
        duration_ms = len(audio_bytes) // (sample_rate * 2) * 1000
        logger.info("[STT:Whisper] Sending %d bytes (%.0fms) for transcription", len(wav_data), duration_ms)

        client = self._get_client()
        # language code for Whisper is ISO-639-1 (e.g. "en", "hi").
        # "unknown" means: omit the parameter so Whisper auto-detects.
        kwargs: dict = {
            "model": self._model,
            "file": ("audio.wav", wav_data, "audio/wav"),
        }
        _hint = (language or "").split("-")[0].lower()
        if _hint and _hint != "unknown" and _hint in WHISPER_LANGUAGES:
            kwargs["language"] = _hint
        elif _hint and _hint != "unknown":
            # whisper-1 hard-400s on an unsupported code ("Language 'gu' is
            # not supported"), and the router retries once — so BOTH attempts
            # died and the segment was lost outright. 14 such 400s = 7
            # segments of caller speech thrown away (07-30, 07-31, 08-03).
            # Auto-detect is worse than a correct hint but infinitely better
            # than nothing.
            logger.info("[STT:Whisper] %r is not a whisper-1 language — "
                        "auto-detecting instead of failing the request",
                        _hint)

        result = await client.audio.transcriptions.create(**kwargs)

        transcript = result.text.strip()
        logger.info("[STT:Whisper] Transcript: %r", transcript)
        # Whisper's API reports no confidence — None, never a fabricated 1.0.
        return STTResult(text=transcript)
