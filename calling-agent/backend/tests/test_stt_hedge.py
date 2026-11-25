"""Hedged STT fallback — 2026-08-03 Sarvam-hang incident hardening.

During the incident ~1-in-3 Sarvam requests hung server-side (confirmed by
live probe: 503-after-11.6s / >15s stalls on an otherwise-healthy key) while
the rest answered in 150-450ms. The sequential router turned every hang into
read-timeout + fallback latency of dead air; turns queued behind the hang
arrived 16-19s late and the agent answered things the caller said long ago.

These tests pin the hedge semantics:
  * fast primary → hedge never starts, fallback provider untouched;
  * hung primary + valid hedge transcript → hedge wins the race fast, the
    hanging primary attempt is cancelled;
  * hedge finishing EMPTY does not preempt — the primary is waited out;
  * primary error after the hedge started → the in-flight hedge is REUSED
    (exactly one fallback call, not a second sequential one);
  * primary clean-empty (no speech) still discards any hedge text — Sarvam's
    clean-empty stays the anti-hallucination authority;
  * rate-limited primary reuses the in-flight hedge;
  * hedge_delay=0 disables hedging (sequential behavior preserved).

Run with the calling-agent venv:
    cd calling-agent/backend && ../venv/bin/python -m pytest tests/test_stt_hedge.py -q
"""

import asyncio
import time

import httpx

from app.voice.stt.base import STTProvider, STTResult
from app.voice.stt.router import STTRouter
from app.voice.stt.sarvam import SarvamRateLimitError

# Strings already proven to survive validate_transcript in the F16 tests.
HINDI_GREETING = "नमस्ते जी"
HINDI_ACK = "चलो अच्छा ठीक है"

AUDIO = b"\x00\x01" * 16_000  # 1 s of fake PCM16 @ 16 kHz


def run(coro):
    return asyncio.run(coro)


class SlowFakeSTT(STTProvider):
    """Scripted provider with per-call latency.

    Each transcribe() sleeps `delay` seconds, then pops the next queued
    result (STTResult or Exception). Records call languages and whether a
    call was cancelled mid-sleep.
    """

    def __init__(self, results=None, delay=0.0):
        self._results = list(results or [])
        self._delay = delay
        self.calls: list[str] = []
        self.cancelled = False

    async def transcribe(self, audio_bytes, sample_rate=16_000, language="en"):
        self.calls.append(language)
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if not self._results:
            return STTResult(text="")
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_router(primary, fallback, hedge_delay):
    return STTRouter(primary=primary, fallback=fallback,
                     hedge_delay=hedge_delay)


# ---------------------------------------------------------------------------
# Fast primary: the hedge never starts
# ---------------------------------------------------------------------------

def test_fast_primary_never_hedges():
    primary = SlowFakeSTT([STTResult(text=HINDI_GREETING)], delay=0.0)
    fallback = SlowFakeSTT([STTResult(text=HINDI_ACK)], delay=0.0)
    router = make_router(primary, fallback, hedge_delay=0.25)

    result = run(router.transcribe(AUDIO, language="hi-IN"))

    assert HINDI_GREETING in result.text
    assert result.source != "fallback"
    assert fallback.calls == []


# ---------------------------------------------------------------------------
# Hung primary: valid hedge transcript wins the race fast
# ---------------------------------------------------------------------------

def test_hung_primary_hedge_wins_and_cancels_primary():
    primary = SlowFakeSTT([STTResult(text=HINDI_GREETING)], delay=5.0)
    fallback = SlowFakeSTT([STTResult(text=HINDI_ACK)], delay=0.05)
    router = make_router(primary, fallback, hedge_delay=0.1)

    t0 = time.monotonic()
    result = run(router.transcribe(AUDIO, language="hi-IN"))
    elapsed = time.monotonic() - t0

    assert HINDI_ACK in result.text
    assert result.source == "fallback"
    assert elapsed < 1.5, f"hedge should win in ~0.15s, took {elapsed:.2f}s"
    assert primary.cancelled, "hanging primary attempt must be cancelled"


# ---------------------------------------------------------------------------
# Hedge finishes empty: primary is waited out and still wins
# ---------------------------------------------------------------------------

def test_empty_hedge_does_not_preempt_primary():
    primary = SlowFakeSTT([STTResult(text=HINDI_GREETING)], delay=0.3)
    fallback = SlowFakeSTT([STTResult(text="")], delay=0.0)
    router = make_router(primary, fallback, hedge_delay=0.05)

    result = run(router.transcribe(AUDIO, language="hi-IN"))

    assert HINDI_GREETING in result.text
    assert result.source != "fallback"
    assert len(fallback.calls) == 1  # hedge ran, its empty result was ignored


# ---------------------------------------------------------------------------
# Primary times out after the hedge started: hedge is reused, not re-called
# ---------------------------------------------------------------------------

def test_primary_timeout_reuses_inflight_hedge():
    primary = SlowFakeSTT([httpx.ReadTimeout("hang")], delay=0.2)
    fallback = SlowFakeSTT([STTResult(text=HINDI_ACK)], delay=0.3)
    router = make_router(primary, fallback, hedge_delay=0.05)

    result = run(router.transcribe(AUDIO, language="hi-IN"))

    assert HINDI_ACK in result.text
    assert result.source == "fallback"
    assert len(fallback.calls) == 1, "in-flight hedge must be reused"


# ---------------------------------------------------------------------------
# Clean-empty primary discards hedge text (anti-hallucination gate)
# ---------------------------------------------------------------------------

def test_clean_empty_primary_discards_hedge_text():
    primary = SlowFakeSTT([STTResult(text="")], delay=0.2)
    fallback = SlowFakeSTT([STTResult(text=HINDI_ACK)], delay=0.0)
    router = make_router(primary, fallback, hedge_delay=0.05)

    result = run(router.transcribe(AUDIO, language="hi-IN"))

    assert result.text == ""


# ---------------------------------------------------------------------------
# Rate-limited primary reuses the in-flight hedge
# ---------------------------------------------------------------------------

def test_rate_limited_primary_reuses_hedge():
    primary = SlowFakeSTT([SarvamRateLimitError(retry_after="30")], delay=0.2)
    fallback = SlowFakeSTT([STTResult(text=HINDI_ACK)], delay=0.05)
    router = make_router(primary, fallback, hedge_delay=0.05)

    result = run(router.transcribe(AUDIO, language="hi-IN"))

    assert HINDI_ACK in result.text
    assert result.source == "fallback"
    assert len(fallback.calls) == 1


# ---------------------------------------------------------------------------
# hedge_delay=0 disables hedging entirely (sequential router preserved)
# ---------------------------------------------------------------------------

def test_hedge_disabled_is_sequential():
    primary = SlowFakeSTT([httpx.ReadTimeout("hang")], delay=0.2)
    fallback = SlowFakeSTT([STTResult(text=HINDI_ACK)], delay=0.0)
    router = make_router(primary, fallback, hedge_delay=0.0)

    result = run(router.transcribe(AUDIO, language="hi-IN"))

    assert HINDI_ACK in result.text
    assert result.source == "fallback"
    assert len(fallback.calls) == 1
