"""
Tracking wrapper for SarvamTTS.

Captures whether each speak() call was cancelled mid-stream (barge-in)
or completed naturally. Uses a per-instance flag set in stop() override
because the parent's _cancelled flag is cleared by _reset() before
speak() returns — so it can't be read after await super().speak().

Cost is recorded against the characters Sarvam actually RECEIVED, which
is not the same as the characters we asked for: speak() synthesizes an
utterance as a sequence of clause-sized HTTP requests and breaks out of
that loop on barge-in, so the tail clauses are never POSTed and never
billed. The parent reports the submitted total via
_last_speak_billed_chars; `chars` is still reported alongside it so the
report can show intended-vs-billed (barge-in waste).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .tts_sarvam import SarvamTTS
from .credit_tracker import SarvamCreditTracker

logger = logging.getLogger(__name__)

CallIdProvider = Callable[[], str | None]


class TrackingSarvamTTS(SarvamTTS):
    """SarvamTTS with credit tracking. Behaviour is identical to parent."""

    def __init__(self, call_id_provider: CallIdProvider, **kwargs) -> None:
        super().__init__(**kwargs)
        self._call_id_provider = call_id_provider
        self._tracker = SarvamCreditTracker.get()
        self._speak_was_cancelled = False

    async def stop(self) -> None:
        # Capture cancellation before parent resets internal state
        self._speak_was_cancelled = True
        await super().stop()

    async def speak(self, text: str) -> None:
        self._speak_was_cancelled = False  # reset for each new utterance
        call_id = self._call_id_provider()
        chars = len(text)
        await super().speak(text)
        # parent's _reset() has already cleared _cancelled — use our flag
        completed = not self._speak_was_cancelled
        self._speak_was_cancelled = False
        # _reset() leaves _last_speak_billed_chars intact (it clears only
        # _cancelled / _playing), so this is the count for THIS utterance.
        billed = getattr(self, "_last_speak_billed_chars", chars)
        # Schedule as a task so speak() returns without blocking on file I/O
        asyncio.ensure_future(
            self._tracker.record_tts(
                call_id, chars, completed=completed, billed_chars=billed
            )
        )
