"""The Sarvam rate card — one source of truth for every ₹ figure in the platform.

Transcribed from https://docs.sarvam.ai/api/getting-started/pricing, in the
card's own units, so a rate change is a one-line edit against the source rather
than an archaeology exercise across three modules.

    Speech to Text   (saarika, real-time / streaming)   ₹30 / hour
    Speech to Text   (batch with diarization)           ₹45 / hour
    Text to Speech   (bulbul:v3, streaming)             ₹30 / 10K chars
                                                     == ₹3.00 / 1K chars

Billing granularity — the part that turns an estimate into a bill:

  STT  billed per SECOND of audio. Each request is rounded UP to the next
       whole second, with a MINIMUM of 1 second per request. Our turns are
       short (median ~2s), so the rounding is not a rounding error: measured
       over the whole corpus it adds ~25% on top of raw audio seconds. Costing
       raw seconds understates the invoice.

  TTS  billed per CHARACTER of the text submitted in the request, no minimum.
       Note "submitted": speak() sends an utterance as several clause-sized
       requests and stops sending on barge-in, so the characters we intended
       to speak are an upper bound, not the bill.

Whisper is OpenAI, not Sarvam — priced here anyway so that "total cost" is a
total. $0.006/min converted to ₹ via WHISPER_PRICE_PER_MIN_INR.

Every rate is env-overridable so a deployment pinned to older pricing (or a
different FX rate) does not need a code change.
"""

from __future__ import annotations

import math
import os

# --- rate card -------------------------------------------------------------

STT_PRICE_PER_HOUR = float(os.environ.get("SARVAM_STT_PRICE_PER_HOUR", "30.0"))
# Legacy env var, still honoured: older deployments set the per-minute rate.
STT_PRICE_PER_MIN = float(
    os.environ.get("SARVAM_STT_PRICE_PER_MIN", str(STT_PRICE_PER_HOUR / 60.0)))
TTS_PRICE_PER_1K_CHARS = float(
    os.environ.get("SARVAM_TTS_PRICE_PER_1K_CHARS", "3.0"))
WHISPER_PRICE_PER_MIN = float(os.environ.get("WHISPER_PRICE_PER_MIN_INR", "0.53"))

# --- billing granularity ---------------------------------------------------

STT_MIN_BILLED_SECONDS = float(os.environ.get("SARVAM_STT_MIN_BILLED_S", "1.0"))


def stt_billed_seconds(audio_seconds: float) -> float:
    """What Sarvam charges for one STT request of `audio_seconds` of audio."""
    if audio_seconds <= 0:
        return 0.0
    return max(STT_MIN_BILLED_SECONDS, float(math.ceil(audio_seconds)))


# --- cost ------------------------------------------------------------------

def stt_cost(billed_seconds: float) -> float:
    """₹ for STT. Takes ALREADY-BILLED seconds (per-request ceiling applied)."""
    return round((billed_seconds / 60.0) * STT_PRICE_PER_MIN, 6)


def whisper_cost(audio_seconds: float) -> float:
    """₹ for the OpenAI Whisper fallback. No per-request ceiling."""
    return round((audio_seconds / 60.0) * WHISPER_PRICE_PER_MIN, 6)


def tts_cost(chars: float) -> float:
    """₹ for TTS. Takes characters SUBMITTED to Sarvam, not characters wanted."""
    return round((chars / 1000.0) * TTS_PRICE_PER_1K_CHARS, 6)


def rate_card() -> dict:
    """The live rates, for display next to any figure derived from them."""
    return {
        "stt_per_hour": STT_PRICE_PER_HOUR,
        "tts_per_1k_chars": TTS_PRICE_PER_1K_CHARS,
        "whisper_per_min_inr": WHISPER_PRICE_PER_MIN,
        "stt_min_billed_seconds": STT_MIN_BILLED_SECONDS,
        "currency": "INR",
        "source": "https://docs.sarvam.ai/api/getting-started/pricing",
    }
