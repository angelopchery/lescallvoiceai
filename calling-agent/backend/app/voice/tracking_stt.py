"""
Tracking wrappers for Sarvam STT and Whisper STT providers.

These are thin subclasses that call super().transcribe() unchanged and
record usage to SarvamCreditTracker after a successful response.

Key contract:
  - If super() raises, the exception propagates — no usage is recorded.
  - Recording fires only after a successful HTTP 200 (or Whisper success).
  - call_id_provider is a callable so the tracker always sees the
    *current* call_id at the moment of the API call, not the stale
    value captured at construction time.
"""

from __future__ import annotations

import logging
from typing import Callable

from .stt.sarvam import SarvamSTT
from .stt.openai_whisper import OpenAIWhisperSTT
from .stt.base import STTResult
from .credit_tracker import SarvamCreditTracker

logger = logging.getLogger(__name__)

CallIdProvider = Callable[[], str | None]


class TrackingSarvamSTT(SarvamSTT):
    """SarvamSTT with credit tracking. Behaviour is identical to parent."""

    def __init__(self, call_id_provider: CallIdProvider, **kwargs) -> None:
        super().__init__(**kwargs)
        self._call_id_provider = call_id_provider
        self._tracker = SarvamCreditTracker.get()

    async def transcribe(
        self,
        audio_bytes: bytes,
        sample_rate: int = 16_000,
        language: str = "en",
    ) -> STTResult:
        # super() raises on HTTP errors / rate limits — record only on success
        result = await super().transcribe(audio_bytes, sample_rate, language)
        audio_seconds = len(audio_bytes) / (sample_rate * 2)
        await self._tracker.record_stt(
            self._call_id_provider(), audio_seconds, provider="sarvam"
        )
        return result


class TrackingWhisperSTT(OpenAIWhisperSTT):
    """OpenAIWhisperSTT with credit tracking. Behaviour is identical to parent."""

    def __init__(self, call_id_provider: CallIdProvider, **kwargs) -> None:
        super().__init__(**kwargs)
        self._call_id_provider = call_id_provider
        self._tracker = SarvamCreditTracker.get()

    async def transcribe(
        self,
        audio_bytes: bytes,
        sample_rate: int = 16_000,
        language: str = "en",
    ) -> STTResult:
        result = await super().transcribe(audio_bytes, sample_rate, language)
        audio_seconds = len(audio_bytes) / (sample_rate * 2)
        await self._tracker.record_stt(
            self._call_id_provider(), audio_seconds, provider="whisper"
        )
        return result
