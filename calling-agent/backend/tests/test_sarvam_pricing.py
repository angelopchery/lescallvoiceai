"""Pins the Sarvam billing rules — the ₹ figures the platform reports.

Every number here is checked against the published rate card
(https://docs.sarvam.ai/api/getting-started/pricing), not against whatever
the code currently happens to compute. If Sarvam changes pricing, these
tests are what should be edited first, and their failure is the reminder
that credit_log history reprices too.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared import sarvam_pricing as pricing  # noqa: E402

from app.voice import credit_tracker  # noqa: E402
from app.voice.credit_tracker import SarvamCreditTracker, _CallAccum, _usage_fields  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_credit_log(tmp_path, monkeypatch):
    """Never let a test write into the real credit_log.

    _CREDIT_LOG_DIR is resolved from the environment at import time and
    defaults to the production directory, so any tracker call in a test
    appends fake usage to the file the ₹ figures are computed from —
    invented calls in the billing record. Autouse: this must hold for
    every test in the file, including ones added later.
    """
    monkeypatch.setattr(credit_tracker, "_CREDIT_LOG_DIR", tmp_path / "credit_log")


def test_the_isolation_fixture_actually_isolates():
    """If this fails, the fixture stopped working and other tests in this
    file are writing into production. Fail loudly rather than silently."""
    assert "lescall/credit_log" not in str(credit_tracker._CREDIT_LOG_DIR)


# ---------------------------------------------------------------------------
# Rate card
# ---------------------------------------------------------------------------

def test_stt_is_thirty_rupees_per_hour():
    assert pricing.stt_cost(3600) == pytest.approx(30.0)


def test_tts_is_three_rupees_per_thousand_chars():
    # Card quotes bulbul:v3 as ₹30 / 10K chars — the same rate.
    assert pricing.tts_cost(1000) == pytest.approx(3.0)
    assert pricing.tts_cost(10_000) == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# Billing granularity — where an estimate stops being a guess
# ---------------------------------------------------------------------------

def test_stt_rounds_each_request_up_to_the_next_second():
    # The docs' own worked example: a 4.3s clip bills as 5s.
    assert pricing.stt_billed_seconds(4.3) == 5.0


def test_stt_charges_a_minimum_of_one_second_per_request():
    # Short turns are the common case; charging ceil() alone would bill 0
    # for sub-second requests that Sarvam bills a full second for.
    assert pricing.stt_billed_seconds(0.2) == 1.0
    assert pricing.stt_billed_seconds(1e-6) == 1.0


def test_no_audio_costs_nothing():
    """A request that never happened must not invent a minimum charge."""
    assert pricing.stt_billed_seconds(0) == 0.0
    assert pricing.stt_cost(0) == 0.0
    assert pricing.tts_cost(0) == 0.0


def test_ceiling_overhead_is_material_on_short_turns():
    """Guards the tempting 'just use raw seconds' simplification: our median
    turn is ~2s, so per-request rounding is a real fraction of the bill."""
    turns = [1.2, 2.4, 0.6, 3.1, 1.9]
    raw = pricing.stt_cost(sum(turns) / 1.0)
    billed = pricing.stt_cost(sum(pricing.stt_billed_seconds(t) for t in turns))
    assert billed > raw * 1.15


# ---------------------------------------------------------------------------
# TTS is billed on what Sarvam RECEIVED, not what we wanted spoken
# ---------------------------------------------------------------------------

def test_barge_in_does_not_bill_the_clauses_never_sent():
    """speak() posts an utterance as clause-sized requests and stops on
    barge-in. Charging the full text invented cost that never happened."""
    tracker = SarvamCreditTracker()
    tracker.start_call("c1")
    asyncio.run(tracker.record_tts("c1", 300, completed=False, billed_chars=90))
    summary = tracker.get_call_summary("c1")

    assert summary["tts_chars"] == 300           # what we wanted to say
    assert summary["tts_billed_chars"] == 90     # what Sarvam was sent
    assert summary["est_tts_cost"] == pytest.approx(pricing.tts_cost(90))


def test_billed_chars_defaults_to_requested_when_caller_cannot_tell():
    """Older callers / non-chunked paths pass no billed_chars. The intended
    count is then exact (the utterance completed) or an upper bound."""
    tracker = SarvamCreditTracker()
    tracker.start_call("c1")
    asyncio.run(tracker.record_tts("c1", 120, completed=True))
    assert tracker.get_call_summary("c1")["tts_billed_chars"] == 120


def test_speak_bills_only_the_parts_actually_posted():
    """End-to-end through the real splitter and the real speak() loop."""
    sys.modules.setdefault("sounddevice", types.ModuleType("sounddevice"))
    from app.voice.tts_sarvam import SarvamTTS, _split_for_streaming

    text = ("Great, thank you. We have two projects in that area. "
            "Would you like to visit this weekend, or would a weekday suit you?")
    parts = _split_for_streaming(text)
    assert len(parts) > 1, "fixture must split, or the test proves nothing"

    class _Sink:
        last_flush_duration = 0.0

        async def start(self): ...
        async def write_frame(self, b): ...
        async def flush(self): ...

    class _Fake(SarvamTTS):
        def __init__(self, cancel_after_part):
            super().__init__(api_key="k", audio_sink=_Sink())
            self._cancel_after = cancel_after_part
            self._parts_started = 0

        async def _stream_audio(self, part):
            self._parts_started += 1
            yield types.SimpleNamespace(audio=b"\x00" * 320)
            if self._parts_started >= self._cancel_after:
                self._cancelled = True

    cut = _Fake(cancel_after_part=1)
    asyncio.run(cut.speak(text))
    assert cut._last_speak_billed_chars == len(parts[0])
    assert cut._last_speak_billed_chars < len(text)

    whole = _Fake(cancel_after_part=99)
    asyncio.run(whole.speak(text))
    assert whole._last_speak_billed_chars == sum(len(p) for p in parts)


def test_early_return_bills_nothing(monkeypatch):
    """No API key / empty text never reaches Sarvam, so it never bills."""
    sys.modules.setdefault("sounddevice", types.ModuleType("sounddevice"))
    from app.voice.tts_sarvam import SarvamTTS

    class _Sink:
        last_flush_duration = 0.0

        async def start(self): ...
        async def write_frame(self, b): ...
        async def flush(self): ...

    # The constructor is `api_key or os.environ["SARVAM_API_KEY"]`, so passing
    # "" does NOT mean "no key" whenever something earlier in the session has
    # loaded .env — the test then falls through to a real HTTP call against
    # the live key. Clear the environment too.
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)

    tts = SarvamTTS(api_key="", audio_sink=_Sink())
    assert tts._api_key == "", "fixture failed: this test would hit the real API"
    asyncio.run(tts.speak("something we cannot send"))
    assert tts._last_speak_billed_chars == 0

    # Empty text is the other early return: nothing is sent, nothing is billed.
    monkeypatch.setenv("SARVAM_API_KEY", "test-key-not-real")
    tts2 = SarvamTTS(api_key="test-key-not-real", audio_sink=_Sink())
    asyncio.run(tts2.speak(" "))
    assert tts2._last_speak_billed_chars == 0


# ---------------------------------------------------------------------------
# A total must be a total
# ---------------------------------------------------------------------------

def test_whisper_fallback_is_priced_not_free():
    """Whisper is OpenAI, not Sarvam — but it is still money, and pricing it
    at zero understated every turn where Sarvam timed out."""
    fields = _usage_fields(_CallAccum(
        call_id="c", start_time="t", stt_whisper_seconds=60.0))
    assert fields["est_whisper_cost"] > 0
    assert fields["est_total_cost"] == pytest.approx(fields["est_whisper_cost"])


def test_total_is_the_sum_of_its_parts():
    fields = _usage_fields(_CallAccum(
        call_id="c", start_time="t",
        stt_sarvam_billed_seconds=120.0,
        tts_chars=900, tts_billed_chars=600,
        stt_whisper_seconds=30.0))
    assert fields["est_total_cost"] == pytest.approx(
        fields["est_stt_cost"] + fields["est_tts_cost"]
        + fields["est_whisper_cost"])
    # and TTS priced off submitted chars, not requested
    assert fields["est_tts_cost"] == pytest.approx(pricing.tts_cost(600))
