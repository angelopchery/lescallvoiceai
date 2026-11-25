"""F16 — STT script-misdetection retry.

Verified incident (call cf6b0e23, 2026-07-15 13:26): two genuine Hindi
utterances were transcribed by Sarvam in Gurmukhi script (lang=pa-IN,
confidence=1.00) — 'ਨਮਸਤੇ ਜੀ' and 'ਚਲੋ ਅੱਛਾ ਅੱਛਾ ਠੀਕ ਹੈ'. validate_transcript
rejected them as unsupported_script and the router treated the rejection as
"clean empty — skipping fallback": no retry, no Whisper, speech silently
dropped.

These tests pin the fix:
  * unsupported-script rejection + language hint → ONE forced-language Sarvam
    retry; success returns the retried transcript.
  * retry also rejected → segment is FALLBACK-ELIGIBLE (Whisper runs).
  * every other rejection type (hallucination, repetition, quality, …) keeps
    today's behaviour exactly: clean-empty return, no retry, no fallback.
  * genuinely empty transcripts (no speech): no retry, no fallback.
  * no language hint (first utterance of a call): no retry, no fallback.
  * script_reject_count / script_recovered_count counters.

Run with the calling-agent venv:
    cd calling-agent/backend && ../venv/bin/python -m pytest tests/test_stt_script_retry.py -q
"""

import asyncio

from app.voice.stt.base import STTProvider, STTResult
from app.voice.stt.router import (
    SCRIPT_REJECT_REASON,
    STTRouter,
    validate_transcript,
    validate_transcript_ex,
)

# Exact transcripts from the live incident.
GURMUKHI_GREETING = "ਨਮਸਤੇ ਜੀ"
GURMUKHI_ACK = "ਚਲੋ ਅੱਛਾ ਅੱਛਾ ਠੀਕ ਹੈ"
HINDI_GREETING = "नमस्ते जी"
HINDI_ACK = "चलो अच्छा ठीक है"

AUDIO = b"\x00\x01" * 16_000  # 1 s of fake PCM16 @ 16 kHz


def run(coro):
    return asyncio.run(coro)


class FakeSTT(STTProvider):
    """Scripted STT provider: pops one queued result per transcribe() call.

    Records the `language` argument of every call so tests can assert how
    many attempts were made and which language was forced on each.
    """

    def __init__(self, results=None):
        self._results = list(results or [])
        self.calls: list[str] = []

    async def transcribe(self, audio_bytes, sample_rate=16_000, language="en"):
        self.calls.append(language)
        if not self._results:
            return STTResult(text="")
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_router(primary_results, fallback_results=None):
    primary = FakeSTT(primary_results)
    fallback = FakeSTT(fallback_results or [])
    router = STTRouter(primary=primary, fallback=fallback)
    return router, primary, fallback


# ---------------------------------------------------------------------------
# (a) Script rejection + language hint → one forced retry, success path
# ---------------------------------------------------------------------------

def test_gurmukhi_reject_with_hint_retries_once_and_recovers():
    router, primary, fallback = make_router([
        STTResult(text=GURMUKHI_GREETING, confidence=1.0, detected_language="pa-IN"),
        STTResult(text=HINDI_GREETING, confidence=1.0, detected_language="hi-IN"),
    ])

    result = run(router.transcribe(AUDIO, language="unknown", default_language="hi"))

    assert result.text == HINDI_GREETING
    assert result.detected_language == "hi-IN"
    # Exactly one auto-detect attempt + one forced-language retry, no Whisper.
    assert primary.calls == ["unknown", "hi"]
    assert fallback.calls == []
    assert router.script_reject_count == 1
    assert router.script_recovered_count == 1


def test_explicit_language_param_preferred_over_default_language():
    router, primary, fallback = make_router([
        STTResult(text=GURMUKHI_GREETING, confidence=1.0, detected_language="pa-IN"),
        STTResult(text=HINDI_GREETING, confidence=1.0, detected_language="hi-IN"),
    ])

    result = run(router.transcribe(AUDIO, language="hi-IN", default_language="gu"))

    assert result.text == HINDI_GREETING
    # The explicit language hint the router received wins over default_language.
    assert primary.calls == ["hi-IN", "hi-IN"]
    assert fallback.calls == []


# ---------------------------------------------------------------------------
# (b) Retry also rejected → fallback-eligible (Whisper runs), capped at 1 retry
# ---------------------------------------------------------------------------

def test_retry_also_rejected_routes_to_fallback():
    router, primary, fallback = make_router(
        [
            STTResult(text=GURMUKHI_ACK, confidence=1.0, detected_language="pa-IN"),
            STTResult(text=GURMUKHI_ACK, confidence=1.0, detected_language="pa-IN"),
        ],
        [STTResult(text=HINDI_ACK, confidence=1.0, detected_language="hi-IN")],
    )

    result = run(router.transcribe(AUDIO, language="unknown", default_language="hi"))

    assert result.text == HINDI_ACK
    # Cap: exactly ONE forced retry — never a second one for the same turn.
    assert primary.calls == ["unknown", "hi"]
    assert fallback.calls == ["hi"]
    assert router.script_reject_count == 1
    assert router.script_recovered_count == 0


def test_retry_clean_empty_still_routes_to_fallback():
    # Forced retry returns empty — the audio still demonstrably contained
    # speech (primary produced text), so Whisper must run.
    router, primary, fallback = make_router(
        [
            STTResult(text=GURMUKHI_GREETING, confidence=1.0, detected_language="pa-IN"),
            STTResult(text="", confidence=1.0),
        ],
        [STTResult(text=HINDI_GREETING, confidence=1.0, detected_language="hi-IN")],
    )

    result = run(router.transcribe(AUDIO, language="unknown", default_language="hi"))

    assert result.text == HINDI_GREETING
    assert primary.calls == ["unknown", "hi"]
    assert fallback.calls == ["hi"]
    assert router.script_reject_count == 1
    assert router.script_recovered_count == 0


# ---------------------------------------------------------------------------
# (c) Hallucination rejection → NO retry, NO fallback (today's behaviour)
# ---------------------------------------------------------------------------

def test_hallucination_reject_no_retry_no_fallback():
    router, primary, fallback = make_router(
        [STTResult(text="thank you for watching", confidence=1.0,
                   detected_language="en-IN")],
        [STTResult(text="must never be used", confidence=1.0)],
    )

    result = run(router.transcribe(AUDIO, language="unknown", default_language="hi"))

    assert result.text == ""
    assert primary.calls == ["unknown"]   # no retry
    assert fallback.calls == []           # no fallback
    assert router.script_reject_count == 0
    assert router.script_recovered_count == 0


def test_repetition_reject_no_retry_no_fallback():
    router, primary, fallback = make_router(
        [STTResult(text="na na na na na na", confidence=1.0,
                   detected_language="hi-IN")],
        [STTResult(text="must never be used", confidence=1.0)],
    )

    result = run(router.transcribe(AUDIO, language="unknown", default_language="hi"))

    assert result.text == ""
    assert primary.calls == ["unknown"]
    assert fallback.calls == []
    assert router.script_reject_count == 0


# ---------------------------------------------------------------------------
# (d) Genuinely clean empty (no speech) → no retry, no fallback
# ---------------------------------------------------------------------------

def test_clean_empty_no_retry_no_fallback():
    router, primary, fallback = make_router(
        [STTResult(text="", confidence=1.0)],
        [STTResult(text="must never be used", confidence=1.0)],
    )

    result = run(router.transcribe(AUDIO, language="unknown", default_language="hi"))

    assert result.text == ""
    assert primary.calls == ["unknown"]
    assert fallback.calls == []
    assert router.script_reject_count == 0
    assert router.script_recovered_count == 0


# ---------------------------------------------------------------------------
# No language hint (first utterance of a call) → today's drop behaviour
# ---------------------------------------------------------------------------

def test_script_reject_without_hint_keeps_drop_behaviour():
    router, primary, fallback = make_router(
        [STTResult(text=GURMUKHI_GREETING, confidence=1.0,
                   detected_language="pa-IN")],
        [STTResult(text="must never be used", confidence=1.0)],
    )

    result = run(router.transcribe(AUDIO, language="unknown"))  # no default_language

    assert result.text == ""
    assert primary.calls == ["unknown"]   # no forced retry without a hint
    assert fallback.calls == []
    # The drop is still counted for observability.
    assert router.script_reject_count == 1
    assert router.script_recovered_count == 0


# ---------------------------------------------------------------------------
# (e) Counters accumulate across turns on the same router instance
# ---------------------------------------------------------------------------

def test_counters_accumulate_across_turns():
    primary = FakeSTT([
        # Turn 1: script reject → forced retry recovers
        STTResult(text=GURMUKHI_GREETING, confidence=1.0, detected_language="pa-IN"),
        STTResult(text=HINDI_GREETING, confidence=1.0, detected_language="hi-IN"),
        # Turn 2: script reject → retry also rejected → Whisper
        STTResult(text=GURMUKHI_ACK, confidence=1.0, detected_language="pa-IN"),
        STTResult(text=GURMUKHI_ACK, confidence=1.0, detected_language="pa-IN"),
    ])
    fallback = FakeSTT([
        STTResult(text=HINDI_ACK, confidence=1.0, detected_language="hi-IN"),
    ])
    router = STTRouter(primary=primary, fallback=fallback)

    r1 = run(router.transcribe(AUDIO, language="unknown", default_language="hi"))
    r2 = run(router.transcribe(AUDIO, language="unknown", default_language="hi"))

    assert r1.text == HINDI_GREETING
    assert r2.text == HINDI_ACK
    assert router.script_reject_count == 2
    assert router.script_recovered_count == 1


# ---------------------------------------------------------------------------
# validate_transcript contract pins (wrapper unchanged for existing callers)
# ---------------------------------------------------------------------------

def test_validate_transcript_wrapper_contract_unchanged():
    # Existing callers keep the text-only contract.
    assert validate_transcript(HINDI_GREETING) == HINDI_GREETING
    assert validate_transcript(GURMUKHI_GREETING) == ""
    assert validate_transcript("thank you for watching") == ""
    assert validate_transcript("") == ""


def test_validate_transcript_ex_reason_codes():
    assert validate_transcript_ex(HINDI_GREETING) == (HINDI_GREETING, None)
    assert validate_transcript_ex(GURMUKHI_GREETING) == ("", SCRIPT_REJECT_REASON)
    assert validate_transcript_ex(GURMUKHI_ACK) == ("", SCRIPT_REJECT_REASON)
    assert validate_transcript_ex("thank you for watching")[1] == "hallucination"
    assert validate_transcript_ex("")[1] == "empty"


# ── digit exemption on the repetition filter (2026-07-23) ───────────────────
# A real caller read out their phone number — "Nain Nain Nain 9810068133",
# confidence 1.00 — and the repetition heuristic (3/4 words identical = 0.75)
# rejected it, so the agent said "couldn't hear you" to the most valuable
# utterance on the call. Number read-outs are inherently repetitive; a
# transcript carrying ≥5 digits must never be dropped by that heuristic.

def test_repetition_waived_for_digit_bearing_transcripts():
    from app.voice.stt.router import validate_transcript_ex
    text, reason = validate_transcript_ex("Nain Nain Nain 9810068133")
    assert text != ""
    assert reason is None


def test_repetition_still_rejects_digitless_repeats():
    from app.voice.stt.router import validate_transcript_ex
    text, reason = validate_transcript_ex("okay okay okay okay")
    assert text == ""
    assert reason == "repetition"


def test_repetition_not_waived_below_five_digits():
    from app.voice.stt.router import validate_transcript_ex
    # "na na na 42" — repetitive noise with an incidental number
    text, reason = validate_transcript_ex("yes yes yes 42")
    assert text == ""
    assert reason == "repetition"
