"""
STT Router — tries primary provider (Sarvam), falls back to secondary
(OpenAI Whisper) only on genuine API errors, not on clean empty transcripts.

Key behaviours
--------------
* Sarvam rate-limits (HTTP 429): exponential backoff with per-strike counter.
  During the backoff window every incoming segment is forwarded to Whisper.
  The backoff resets automatically on the next successful Sarvam call.
* Empty transcript from Sarvam (no API error): treated as "no speech" and
  returned immediately — Whisper is NOT called.  Whisper is only invoked when
  Sarvam raises a network/HTTP exception.
* Unsupported scripts: any transcript containing characters from scripts the
  agent doesn't support (Korean, Tamil, Malayalam, Bengali, Arabic, …) is
  rejected before it reaches the conversation engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
import logging
from collections import Counter
from dataclasses import dataclass

import httpx
from openai import APITimeoutError

from .base import STTProvider, STTResult  # noqa: F401 — re-exported for pipeline
from .sarvam import SarvamSTT, SarvamRateLimitError
from .openai_whisper import OpenAIWhisperSTT
from .post_processor import post_process_transcript
from ...agent_definition.schema import STTCorrections

logger = logging.getLogger(__name__)

MIN_TRANSCRIPT_LENGTH = 2
MAX_RETRIES = 1
# Hedged fallback (2026-08-03 Sarvam-hang incident). Healthy Sarvam answers
# in 150-450ms; during the incident ~1-in-3 requests hung server-side while
# the rest stayed fast. Once the primary has been silent this long, the
# fallback provider starts IN PARALLEL and the first valid transcript wins —
# a hung primary then costs hedge-delay + fallback latency (~2.5s) instead
# of read-timeout + sequential fallback (4-10s). On the healthy path the
# hedge never starts, so accuracy and spend are unchanged. 0 disables.
STT_HEDGE_DELAY_S = float(os.environ.get("STT_HEDGE_DELAY_S", "1.0"))
# Delayed parallel RETRY of the primary itself (2026-08-03): during the 08-03
# Sarvam incident individual requests hung while an immediately-issued
# duplicate succeeded in ~200ms — hangs are per-request, not per-service.
# Racing a second primary attempt keeps the turn on the primary model's
# transcript mid-incident instead of surrendering it to Whisper (whose text
# is mishear-prone and never votes on language). Fires BEFORE the Whisper
# hedge (0.6 < 1.0), so a rescued turn never involves Whisper. 0 disables.
STT_PRIMARY_RETRY_HEDGE_S = float(
    os.environ.get("STT_PRIMARY_RETRY_HEDGE_S", "0.6"))
# When the hedge produces a valid transcript first, the primary gets this
# much longer to land before the hedge takes the turn. A near-miss primary
# (answering 100-300ms behind the hedge) then still decides the turn — its
# transcript is more accurate, and its clean-empty verdict is the
# anti-hallucination authority that discards Whisper noise-transcripts.
# Against a genuine hang the grace expires and only adds 0.3s to the win.
_HEDGE_PRIMARY_GRACE_S = 0.3
# Threshold for the DERIVED confidence score (see _derive_confidence).
# Sarvam's REST response has no confidence field, so for months every
# transcript carried a fabricated 1.0 and this gate never fired once in
# production. Below the threshold the segment is returned empty WITHOUT
# Whisper fallback — the pipeline's existing empty-transcript path then
# plays the "sorry, could you repeat that?" re-ask.
MIN_CONFIDENCE = float(os.environ.get("STT_MIN_CONFIDENCE", "0.4"))

# Initial backoff 60 s, doubles each strike, caps at 5 min
_RL_INITIAL_BACKOFF_S = 60.0
_RL_MAX_BACKOFF_S = 300.0

_FILLER_WORDS = {"um", "uh", "like", "you know"}
_FILLER_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _FILLER_WORDS) + r")\b",
    re.IGNORECASE,
)

_HALLUCINATIONS = [
    "thank you for watching",
    "thanks for watching",
    "subscribe",
    "like and subscribe",
    "please subscribe",
    "thanks for listening",
    "youtube originals",
    "youtube",
]

_DEVANAGARI_HALLUCINATIONS = [
    "प्रस्तुत",
    "यूट्यूब",
    "ओरिजिनल्स",
    "सबस्क्राइब",
    "परमओत",
]

_REPETITION_THRESHOLD = 0.6
# Words a real caller repeats for emphasis — "हाँ हाँ हाँ बिल्कुल", "ji ji
# okay". Repetition of one of these beside at least one other word is
# collapsed, not rejected (see validate_transcript_ex).
_REPEAT_OK_WORDS = frozenset({
    "haan", "haa", "han", "ha", "ji", "yes", "yeah", "ok", "okay", "no",
    "nahi", "nahin", "bilkul", "theek", "thik", "accha", "achha", "sahi",
    "right", "correct", "hmm",
    "हाँ", "हां", "जी", "नहीं", "ना", "बिल्कुल", "ठीक", "हा", "अच्छा", "सही",
    "હા", "ના", "જી", "બરાબર", "સારું",
})

# Rejection reasons that mean "real speech, wrong language label" — worth
# ONE forced-language Sarvam retry when the conversation language is known.
# `marathi_*` joined `unsupported_script` on 2026-09-26 (call 971): the
# caller's Hindi "हाँ, कौन बोल रहा है?" came back as Marathi at p=0.52 and
# was dropped as noise; the same forced-hi retry that rescues Gurmukhi
# misfires rescues this.
_SCRIPT_RETRY_REASONS = frozenset({
    "unsupported_script", "marathi_copula", "marathi_transcript"})

# Whisper is the paid fallback. When its account is out of credit every
# call to it is a guaranteed 429 — and one segment used to pay for TWO of
# them (attempt + retry, ~4 s) before being dropped, while the transcripts
# queued behind it waited (call 971: a 3.6 s Hindi answer arrived 5.4 s late
# for this reason). One quota error opens a cooldown during which the
# fallback is not attempted at all.
FALLBACK_QUOTA_COOLDOWN_S = float(
    os.environ.get("STT_FALLBACK_QUOTA_COOLDOWN_S", "900"))
_QUOTA_MARKERS = ("insufficient_quota", "credit_balance_exhausted",
                  "no credits remaining", "exceeded your current quota",
                  "billing")


def _is_quota_error(exc: BaseException) -> bool:
    msg = f"{type(exc).__name__}: {exc}".lower()
    return ("429" in msg or "ratelimit" in msg or "rate_limit" in msg) and \
        any(m in msg for m in _QUOTA_MARKERS)

_REPEATED_CHAR_RE = re.compile(r"^(.{1,3}[- ]?)\1{2,}[-]?$")

_GARBAGE_SINGLE_WORDS = {
    "shoe", "care", "shoes", "show",
    # Confirmed noise-only transcriptions on near-silence — these words have no
    # conversational meaning as a standalone response.  "sure", "yeah", "oh",
    # "well" are NOT included because callers legitimately respond with a single
    # word affirmation/reaction.
    "the", "a", "an", "is", "it", "to", "of", "huh", "hmm", "uh", "um",
    "ah",
}

# Single Devanagari/Gujarati tokens that surface as STT garbage on noise/breath/clicks.
# INTENTIONALLY narrow: "हा" (yes), "ना"/"ना" (no), "ओके"/"ઓ કે" (OK) are valid
# single-word responses and are NOT included.  Only pure phoneme/vowel artifacts
# with zero conversational meaning are blocked here.
_GARBAGE_SCRIPT_TOKENS = {
    "अ", "आ", "ओ", "न", "ह",   # Devanagari single-consonant/vowel noise
    "અ", "આ", "ઓ",              # Gujarati single-vowel noise
}

# ---------------------------------------------------------------------------
# Transcript quality scoring — rejects function-word-only fragments
# ---------------------------------------------------------------------------
# Pure connective/particle words in Hindi and Gujarati that have zero
# semantic content when they appear ALONE.  Verbs, nouns, pronouns,
# question words, and copulas are deliberately excluded — e.g. "hai"
# (is), "che" / "cho" (are), "kem" (how), "ji" (yes/sir) all carry
# meaning and must NOT be flagged.  The sets are intentionally narrow so
# valid short utterances (greetings, short answers) are never rejected.
_HINDI_FUNC_QUALITY = frozenset({
    # Conjunctions
    "और", "या", "लेकिन", "परंतु", "मगर", "तथा", "व", "अथवा",
    "इसलिए", "क्योंकि",
    # Particles / connectors
    "जैसे", "कि", "भी", "तो", "ही", "बस", "जैसा", "सिर्फ",
})

_GUJARATI_FUNC_QUALITY = frozenset({
    # Conjunctions
    "અને", "અથવા", "પણ", "જ", "ત્યારે",
    "જ્યારે", "કારણ", "જો", "તેથી",
    # Relative pronoun / particles
    "જે", "તો",
})

_QUALITY_THRESHOLD = 0.3  # reject if quality score is below this


def _score_transcript_quality(text: str) -> float:
    """
    Score 0.0 (garbage) → 1.0 (good).

    Short phrases composed entirely or mostly of function words / connectors
    are almost certainly STT noise or auto-detect misfires.  Longer
    transcripts are penalised proportionally but rarely rejected outright.
    """
    # Strip trailing punctuation from each token before checking membership
    words = [w.strip("।?!.,।") for w in text.strip().split()]
    words = [w for w in words if w]
    if not words:
        return 0.0

    func_count = sum(
        1 for w in words
        if w in _HINDI_FUNC_QUALITY or w in _GUJARATI_FUNC_QUALITY
    )
    ratio = func_count / len(words)

    # Short phrases (≤3 words) are very suspicious when ≥60 % function words
    if len(words) <= 3 and ratio >= 0.6:
        return max(0.0, 0.3 - ratio)   # 0.6→-0.3 → 0.0,  1.0→-0.7 → 0.0

    # Medium phrases (4-5 words) need ≥80 % to trigger rejection
    if len(words) <= 5 and ratio >= 0.8:
        return 0.1

    # Partial penalty for high function-word density in longer phrases
    return max(0.3, 1.0 - ratio * 0.4)

# Marathi uses Devanagari (shared with Hindi) so the unsupported-script regex
# cannot catch it. Sarvam auto-detect sometimes transcribes Gujarati/Hindi speech
# as Marathi. These markers split into two tiers:
#   HIGH_CONFIDENCE — single occurrence is sufficient to reject (these words
#     literally do not exist in Hindi or Gujarati under any reading).
#   NORMAL — require 2+ hits to avoid false positives.
_MARATHI_HIGH_CONFIDENCE = [
    "आहे",   # Marathi copula "is/are" — Hindi equivalent is "है" (different word)
    "आहेत",  # "they are" — Hindi: हैं
    "आहात",  # "you are" formal — Hindi: हो/हैं
]
_MARATHI_MARKERS = [
    "मला",   # "to me" (Hindi: मुझे)
    "तुला",  # "to you" informal (Hindi: तुम्हें)
    "होय",   # "yes" (Hindi: हाँ)
    "कसे",   # "how" (Hindi: कैसे)
    "कसा",   # "how" masculine (Hindi: कैसा)
    "नाही",  # "no/not" Marathi form (Hindi: नहीं — different spelling)
    "आपण",   # "we/you" Marathi pronoun (Hindi: हम/आप)
]

# Unicode blocks the agent SUPPORTS: Latin (en), Devanagari (hi), Gujarati (gu).
# Everything else is a Sarvam auto-detect mis-fire on noisy/short audio.
# Supported languages: English, Hindi, Gujarati ONLY.
# Malayalam, Tamil, Telugu, Kannada, Bengali, Korean, Arabic, etc. are all
# rejected here — they are never valid caller input for this agent.
_UNSUPPORTED_SCRIPT_RE = re.compile(
    r"["
    r"ঀ-৿"           # Bengali / Assamese
    r"਀-੿"           # Gurmukhi (Punjabi)
    r"଀-୿"           # Oriya (Odia)
    r"஀-௿"           # Tamil
    r"ఀ-౿"           # Telugu
    r"ಀ-೿"           # Kannada
    r"ഀ-ൿ"           # Malayalam
    r"඀-෿"           # Sinhala
    r"؀-ۿ"           # Arabic
    r"ༀ-࿿"           # Tibetan
    r"က-႟"           # Myanmar
    r"ᄀ-ᇿ"  # Hangul Jamo (Korean)
    r"가-힣"  # Hangul Syllables (Korean)
    r"㄰-㆏"  # Hangul Compatibility Jamo (Korean)
    r"ꥠ-꥿"  # Hangul Jamo Extended-A (Korean)
    r"ힰ-퟿"  # Hangul Jamo Extended-B (Korean)
    r"぀-ヿ"  # Hiragana / Katakana (Japanese)
    r"一-鿿"  # CJK Unified Ideographs (Chinese/Japanese)
    r"]"
)


# Rejection-reason code for the unsupported-script filter.  The router uses
# this to distinguish "Sarvam transcribed real speech in the wrong script"
# (retry-worthy — see F16) from every other rejection type (noise/hallucination
# — NOT retry-worthy).
SCRIPT_REJECT_REASON = "unsupported_script"


def validate_transcript(text: str) -> str:
    """
    Clean and validate an STT transcript. Returns empty string to reject.

    Steps: normalize -> strip fillers -> reject short -> reject hallucinations
    -> reject unsupported scripts -> reject excessive repetition.

    Thin wrapper around :func:`validate_transcript_ex` preserving the original
    text-only contract for existing callers.
    """
    cleaned, _reason = validate_transcript_ex(text)
    return cleaned


def validate_transcript_ex(text: str) -> tuple[str, str | None]:
    """
    Clean and validate an STT transcript.

    Returns ``(cleaned_text, None)`` on success or ``("", reason)`` on
    rejection, where ``reason`` is a stable code matching the
    ``[STT-FILTER] reason=...`` log lines (e.g. ``"unsupported_script"``,
    ``"hallucination"``, ``"low_quality"``).
    """
    if not text:
        return "", "empty"

    # Normalize
    cleaned = text.lower().strip()
    cleaned = re.sub(r"\s+", " ", cleaned)

    # Strip filler words
    cleaned = _FILLER_RE.sub("", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)

    # Length check
    if len(cleaned) < 2:
        logger.info("[STT-FILTER] reason=too_short text=%r", text)
        return "", "too_short"

    # Unsupported-script filter — Sarvam's auto-detect sometimes transcribes
    # noisy/short audio into Korean, Malayalam, Tamil, etc. which are never
    # valid user input. Reject immediately.
    if _UNSUPPORTED_SCRIPT_RE.search(text):
        logger.info("[STT-FILTER] reason=unsupported_script text=%r", text)
        return "", SCRIPT_REJECT_REASON

    # Marathi rejection: Marathi shares Devanagari with Hindi so the script
    # regex cannot filter it. High-confidence copulas (आहे/आहेत/आहात) reject
    # on a single hit since they never appear in Hindi or Gujarati.
    # Other Marathi markers require 2+ hits to avoid false positives.
    if any(m in text for m in _MARATHI_HIGH_CONFIDENCE):
        logger.info("[STT-FILTER] reason=marathi_copula text=%r", text)
        return "", "marathi_copula"
    _marathi_hits = sum(1 for m in _MARATHI_MARKERS if m in text)
    if _marathi_hits >= 2:
        logger.info("[STT-FILTER] reason=marathi_transcript hits=%d text=%r", _marathi_hits, text)
        return "", "marathi_transcript"

    # Hallucination filter
    for hallucination in _HALLUCINATIONS:
        if hallucination in cleaned:
            logger.info("[STT-FILTER] reason=hallucination text=%r match=%r", text, hallucination)
            return "", "hallucination"

    # Repeated character/syllable patterns like "s-s-s-s-s-" or "na na na"
    if _REPEATED_CHAR_RE.match(cleaned):
        logger.info("[STT-FILTER] reason=repeated_pattern text=%r", text)
        return "", "repeated_pattern"

    # Devanagari hallucination filter
    for hallucination in _DEVANAGARI_HALLUCINATIONS:
        if hallucination in text:
            logger.info("[STT-FILTER] reason=devanagari_hallucination text=%r match=%r",
                        text, hallucination)
            return "", "devanagari_hallucination"

    # Repetition check: if >60% of words are the same word.
    # Digit exemption (2026-07-23): number read-outs are inherently
    # repetitive — a real caller spoke "Nain Nain Nain 9810068133"
    # (confidence 1.00) and the ratio 0.75 rejection made the agent say
    # "couldn't hear you" to the highest-value utterance on the call. A
    # transcript carrying ≥5 total digits is passed through: phone numbers,
    # budgets and pincodes are exactly what we must never drop.
    words = cleaned.split()
    if len(words) >= 3:
        digit_count = sum(ch.isdigit() for ch in cleaned)
        counts = Counter(words)
        most_common_word, most_common_count = counts.most_common(1)[0]
        if most_common_count / len(words) > _REPETITION_THRESHOLD:
            if digit_count >= 5:
                logger.info(
                    "[STT-FILTER] repetition waived (digits=%d) text=%r",
                    digit_count, text)
            elif (most_common_word in _REPEAT_OK_WORDS
                  and any(w != most_common_word and not w.isdigit()
                          for w in words)):
                # An emphatic yes is not a hallucination. Live 2026-09-26
                # (call 971): "हाँ हाँ हाँ बिल्कुल।" — the caller agreeing to a
                # follow-up — scored 0.75 and was thrown out, the next
                # 300 ms blip made it two empties in a row, and the agent
                # said "माफ़ कीजिए, आपकी आवाज़ स्पष्ट नहीं आई" to a man who
                # had just said yes four times. Collapse the repeats and
                # keep the words that carry the answer. A repeated word with
                # nothing else beside it ("na na na na") is still noise.
                collapsed: list[str] = []
                for w in words:
                    if not (collapsed and w == collapsed[-1] == most_common_word):
                        collapsed.append(w)
                text = " ".join(collapsed)
                cleaned = text.lower().strip()
                words = collapsed
                logger.info("[STT-FILTER] repetition collapsed (affirmation) "
                            "-> %r", text)
            else:
                logger.info("[STT-FILTER] reason=repetition text=%r ratio=%.2f",
                            text, most_common_count / len(words))
                return "", "repetition"

    # Single-word garbage: common misrecognitions from noise
    if len(words) == 1 and cleaned in _GARBAGE_SINGLE_WORDS:
        logger.info("[STT-FILTER] reason=garbage_single_word text=%r", text)
        return "", "garbage_single_word"

    # Single Devanagari/Gujarati script garbage tokens (raw text, not lowercased —
    # the lower/strip pass mangles non-ASCII and we want exact match).
    raw_stripped = text.strip()
    if raw_stripped in _GARBAGE_SCRIPT_TOKENS:
        logger.info("[STT-FILTER] reason=garbage_script_token text=%r", text)
        return "", "garbage_script_token"

    # Quality scoring: reject function-word-only short fragments.
    # Uses the ORIGINAL text so Devanagari/Gujarati characters are intact.
    # Applied after all other filters to minimise false-positive rejections
    # on valid short utterances (greetings, single-word answers).
    quality = _score_transcript_quality(text)
    if quality < _QUALITY_THRESHOLD:
        logger.info("[STT-FILTER] reason=low_quality score=%.2f text=%r", quality, text)
        return "", "low_quality"

    return cleaned, None


@dataclass
class _RejectedTranscript:
    """Details of a non-empty provider transcript rejected by validation."""
    reason: str
    raw_text: str
    detected_language: str | None


class STTRouter:
    """
    Multi-provider STT with intelligent fallback.

    Primary provider: Sarvam (always tried first).
    Fallback provider: OpenAI Whisper (only on Sarvam API errors).

    Fallback policy
    ---------------
    * Sarvam returns empty/short cleanly → return "" immediately, no Whisper.
      (This means the audio was silence or too noisy — Whisper would also fail.)
    * Sarvam raises a network/HTTP exception → try Whisper once.
    * Sarvam returns HTTP 429 → exponential backoff; route to Whisper during
      backoff window; reset on next successful Sarvam call.
    * Sarvam silent past STT_HEDGE_DELAY_S → Whisper starts IN PARALLEL
      (the hedge); a valid hedge transcript takes the turn once Sarvam has
      also outlasted a short grace window. A Sarvam answer inside the grace
      still decides the turn — including its clean-empty verdict, which
      discards hedge text (anti-hallucination gate). A hedge that finishes
      empty simply waits the primary out.

    This prevents the empty-transcript retry loop that exhausted both API
    quotas on every silent/noisy segment.
    """

    def __init__(
        self,
        primary: STTProvider | None = None,
        fallback: STTProvider | None = None,
        min_length: int = MIN_TRANSCRIPT_LENGTH,
        max_retries: int = MAX_RETRIES,
        min_confidence: float = MIN_CONFIDENCE,
        stt_corrections: STTCorrections | None = None,
        hedge_delay: float | None = None,
    ) -> None:
        self._primary = primary or SarvamSTT()
        self._fallback = fallback or OpenAIWhisperSTT()
        self._min_length = min_length
        self._max_retries = max_retries
        self._min_confidence = min_confidence
        self._stt_corrections = stt_corrections
        self._hedge_delay = (hedge_delay if hedge_delay is not None
                             else STT_HEDGE_DELAY_S)

        # Rate-limit state (Sarvam HTTP 429 tracking)
        self._sarvam_rl_until: float = 0.0   # monotonic time when backoff expires
        self._sarvam_rl_count: int = 0        # consecutive rate-limit strikes
        # Fallback (Whisper) quota breaker — see FALLBACK_QUOTA_COOLDOWN_S.
        self._fallback_disabled_until: float = 0.0
        self._fallback_skips_logged: int = 0

        # F16 script-misdetection counters — read by the bridge/pipeline for
        # observability. reject: primary transcript was non-empty but rejected
        # as unsupported_script (real speech transcribed in the wrong script).
        # recovered: the single forced-language Sarvam retry produced a valid
        # transcript for that turn.
        self.script_reject_count: int = 0
        self.script_recovered_count: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fallback_available(self) -> bool:
        """False while the fallback provider is cooling down after a quota
        error (no credit) — nothing is sent to it, no hedge is started."""
        return time.monotonic() >= self._fallback_disabled_until

    async def transcribe(
        self,
        audio_bytes: bytes,
        sample_rate: int = 16_000,
        language: str = "en",
        default_language: str | None = None,
        fallback_hint: str | None = None,
    ) -> STTResult:
        """
        Transcribe one audio segment.

        Parameters
        ----------
        language : str
            Language hint forwarded to the provider. "unknown" = auto-detect.
        default_language : str | None
            F16: best-known conversation language (e.g. "hi", "gu-IN") used
            ONLY for the single forced-language Sarvam retry when the primary
            transcript is rejected as unsupported-script. Ignored otherwise.
            The pipeline passes it from conversation state once a language has
            actually been established; None/"unknown" disables the retry.
        fallback_hint : str | None
            Establishment-aware Whisper hint from the pipeline. A language
            code = hint Whisper with it (caller confirmed it); "" = force
            auto-detect (conversation language is still just the agent's
            default — an agent-default hint makes Whisper transliterate
            Hindi callers into Latin junk, which then starves the script
            evidence the language switcher needs). None = legacy resolution
            below (direct callers/tests).
        """
        duration_ms = len(audio_bytes) / (sample_rate * 2) * 1000
        logger.info("[STT] Audio segment: %.0f ms, %d bytes", duration_ms, len(audio_bytes))

        now = time.monotonic()

        # A4c: the Whisper fallback accepts an ISO-639 hint — the stabilized
        # conversation language stops "हाँजी?" coming back as ", angie?".
        if fallback_hint is not None:
            _fb_lang = fallback_hint or "unknown"
        else:
            _fb_lang = self._resolve_script_retry_language(
                language, default_language) or language

        # ── Sarvam rate-limited: route directly to fallback ──────────────
        if now < self._sarvam_rl_until:
            remaining = self._sarvam_rl_until - now
            logger.warning(
                "[STT] Sarvam rate-limited (%.0fs remaining) — routing to fallback",
                remaining,
            )
            text, _, _, _, conf = await self._try_provider(
                self._fallback, "fallback", audio_bytes, sample_rate, _fb_lang
            )
            return STTResult(text=text, confidence=conf, source="fallback")

        # ── Try primary (Sarvam), hedged by the fallback ─────────────────
        # Hedge race (see STT_HEDGE_DELAY_S): the primary runs as a task; if
        # it stays silent past the hedge delay the fallback starts in
        # parallel. A valid hedge transcript that lands while the primary is
        # still hanging wins immediately. Every sequential-fallback branch
        # below reuses the in-flight hedge instead of a fresh Whisper call.
        primary_task = asyncio.create_task(self._primary_hedged(
            audio_bytes, sample_rate, language))
        hedge_task: asyncio.Task | None = None
        hedge_result: tuple[str, float | None] | None = None

        try:
            if self._hedge_delay > 0 and self.fallback_available():
                done, _ = await asyncio.wait(
                    {primary_task}, timeout=self._hedge_delay)
                if not done:
                    logger.warning(
                        "[STT] Primary silent for %.1fs — starting fallback "
                        "hedge in parallel", self._hedge_delay)
                    hedge_task = asyncio.create_task(self._try_provider(
                        self._fallback, "hedge",
                        audio_bytes, sample_rate, _fb_lang))
                    done, _ = await asyncio.wait(
                        {primary_task, hedge_task},
                        return_when=asyncio.FIRST_COMPLETED)
                    if hedge_task in done and not primary_task.done():
                        hedge_result = self._hedge_result_of(hedge_task)
                        if hedge_result[0]:
                            # Grace: a primary landing within this window
                            # still decides the turn (accuracy + the
                            # clean-empty anti-hallucination verdict).
                            done, _ = await asyncio.wait(
                                {primary_task},
                                timeout=_HEDGE_PRIMARY_GRACE_S)
                            if not done:
                                logger.info(
                                    "[STT] Hedge won the race: %r "
                                    "(primary still hanging — cancelled)",
                                    hedge_result[0])
                                return STTResult(text=hedge_result[0],
                                                 confidence=hedge_result[1],
                                                 source="fallback")
                        else:
                            # Hedge finished empty/errored — wait the
                            # primary out.
                            await asyncio.wait({primary_task})

            try:
                text, had_error, detected_lang, reject, conf = await primary_task
            except SarvamRateLimitError as exc:
                # Apply exponential backoff; reset on the next successful call
                self._apply_sarvam_backoff(exc)
                text, conf = await self._fallback_result(
                    hedge_task, hedge_result, audio_bytes, sample_rate, _fb_lang)
                return STTResult(text=text, confidence=conf, source="fallback")

            if text:
                # Successful Sarvam call → reset rate-limit strike counter
                self._sarvam_rl_count = 0
                logger.info("[STT] Primary succeeded: %r", text)
                return STTResult(text=text, detected_language=detected_lang,
                                 confidence=conf)

            if not had_error:
                # F16: Sarvam produced REAL text but in an unsupported script
                # (e.g. genuine Hindi transcribed as Gurmukhi/pa-IN). The audio
                # demonstrably contained speech — retry once with the language
                # forced, then fall back to Whisper if that also fails.
                if reject is not None and reject.reason in _SCRIPT_RETRY_REASONS:
                    if hedge_task is not None:
                        # A hedge is already in flight (the primary stalled
                        # before answering): its transcript — Whisper WITH the
                        # conversation-language hint — beats paying another
                        # possibly-hanging Sarvam round-trip mid-incident.
                        h_text, h_conf = await self._fallback_result(
                            hedge_task, hedge_result,
                            audio_bytes, sample_rate, _fb_lang)
                        if h_text:
                            logger.info(
                                "[STT] Script-reject resolved by hedge: %r",
                                h_text)
                            return STTResult(text=h_text, confidence=h_conf,
                                             source="fallback")
                    return await self._recover_script_misdetection(
                        reject, audio_bytes, sample_rate, language,
                        default_language
                    )

                # Sarvam processed the audio cleanly but found no speech, or
                # the transcript was noise/hallucination (any non-script
                # rejection). Whisper would produce the same (empty or
                # hallucinated) result — a still-running hedge is cancelled in
                # the finally block and any parked hedge text is deliberately
                # discarded (Sarvam's clean-empty is the anti-hallucination
                # authority).
                logger.info(
                    "[STT] Primary returned no speech (clean empty) — "
                    "skipping fallback"
                )
                return STTResult(text="")

            # ── Primary had a genuine API/network error → fallback ───────
            logger.info("[STT] Primary errored — routing to fallback provider")
            text, conf = await self._fallback_result(
                hedge_task, hedge_result, audio_bytes, sample_rate, _fb_lang)
            if text:
                logger.info("[STT] Fallback succeeded: %r", text)
            else:
                logger.warning(
                    "[STT] Both providers failed — returning empty transcript")
            return STTResult(text=text, confidence=conf, source="fallback")

        finally:
            # Settle whatever is still outstanding: cancel losers, retrieve
            # exceptions of finished-but-unconsumed tasks so asyncio never
            # logs "exception was never retrieved".
            for _t in (primary_task, hedge_task):
                if _t is None:
                    continue
                if not _t.done():
                    _t.cancel()
                with contextlib.suppress(BaseException):
                    await _t

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _primary_hedged(
        self,
        audio_bytes: bytes,
        sample_rate: int,
        language: str,
    ) -> tuple[str, bool, str | None, _RejectedTranscript | None, float | None]:
        """Primary attempt with a delayed parallel RETRY of the primary.

        See STT_PRIMARY_RETRY_HEDGE_S. First leg to produce a usable
        transcript OR a clean no-speech verdict decides; a leg that errors
        waits the other leg out. Rate-limit errors only propagate when no
        leg succeeds. From the caller's perspective this behaves exactly
        like one _try_provider call.
        """
        t1 = asyncio.create_task(self._try_provider(
            self._primary, "primary", audio_bytes, sample_rate, language))
        if STT_PRIMARY_RETRY_HEDGE_S <= 0:
            return await t1
        done, _ = await asyncio.wait({t1}, timeout=STT_PRIMARY_RETRY_HEDGE_S)
        if done:
            return t1.result()
        logger.warning(
            "[STT] Primary silent for %.1fs — racing a second primary "
            "attempt", STT_PRIMARY_RETRY_HEDGE_S)
        t2 = asyncio.create_task(self._try_provider(
            self._primary, "primary-race", audio_bytes, sample_rate, language))
        pending: set[asyncio.Task] = {t1, t2}
        last_result = None
        last_error: BaseException | None = None
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    try:
                        res = t.result()
                    except asyncio.CancelledError:
                        raise
                    except BaseException as exc:
                        last_error = exc
                        continue
                    last_result = res
                    if res[0] or not res[1]:
                        # Usable transcript, or the provider's clean
                        # no-speech verdict (anti-hallucination authority).
                        return res
            if last_result is not None:
                return last_result      # had_error leg — caller falls back
            if last_error is not None:
                raise last_error        # e.g. both legs rate-limited
            return "", True, None, None, None
        finally:
            for t in (t1, t2):
                if not t.done():
                    t.cancel()
                with contextlib.suppress(BaseException):
                    await t

    def _apply_sarvam_backoff(self, exc: SarvamRateLimitError) -> None:
        """Record a 429 strike and open the exponential backoff window."""
        self._sarvam_rl_count += 1
        backoff = min(
            _RL_INITIAL_BACKOFF_S * (2 ** (self._sarvam_rl_count - 1)),
            _RL_MAX_BACKOFF_S,
        )
        self._sarvam_rl_until = time.monotonic() + backoff
        logger.warning(
            "[STT] Sarvam rate-limited (strike %d) — backoff %.0fs, "
            "retry-after=%s — routing to fallback",
            self._sarvam_rl_count,
            backoff,
            exc.retry_after,
        )

    @staticmethod
    def _hedge_result_of(task: "asyncio.Task") -> tuple[str, float | None]:
        """(text, confidence) of a completed hedge task; errors become empty."""
        try:
            text, _, _, _, conf = task.result()
            return text, conf
        except BaseException as exc:  # CancelledError included — hedge is best-effort
            logger.warning("[STT] Hedge fallback errored: %s", exc)
            return "", None

    async def _fallback_result(
        self,
        hedge_task: "asyncio.Task | None",
        hedge_result: tuple[str, float | None] | None,
        audio_bytes: bytes,
        sample_rate: int,
        language: str,
    ) -> tuple[str, float | None]:
        """Fallback transcript, reusing the in-flight hedge when one exists."""
        if hedge_result is not None:
            return hedge_result
        if hedge_task is not None:
            await asyncio.wait({hedge_task})
            return self._hedge_result_of(hedge_task)
        text, _, _, _, conf = await self._try_provider(
            self._fallback, "fallback", audio_bytes, sample_rate, language
        )
        return text, conf

    @staticmethod
    def _resolve_script_retry_language(
        language: str, default_language: str | None
    ) -> str | None:
        """
        Best-known language for the forced-language script retry (F16).

        Preference order: the explicit ``language`` hint the router received
        (when it is a real hint, not "unknown"), then the conversation-state
        ``default_language``. Returns None when no hint exists — the retry
        must not fire on the first utterance of a call.
        """
        if language and language != "unknown":
            return language
        if default_language and default_language != "unknown":
            return default_language
        return None

    async def _recover_script_misdetection(
        self,
        reject: _RejectedTranscript,
        audio_bytes: bytes,
        sample_rate: int,
        language: str,
        default_language: str | None,
    ) -> STTResult:
        """
        F16: the primary transcript contained real speech transcribed in an
        unsupported script (Sarvam auto-detect misfire, e.g. Hindi → Gurmukhi).

        Retry Sarvam ONCE with the language forced to the best-known
        conversation language. If the retry also fails, the segment is
        FALLBACK-ELIGIBLE (speech was demonstrably present) and Whisper runs.
        Without a language hint, today's drop behaviour is preserved.
        """
        self.script_reject_count += 1

        # A4b (2026-07-30): mixed-script salvage BEFORE any retry round-trip.
        # Sarvam's per-segment detection can wrap ONE junk-script token around
        # a perfectly good transcript ('ਇਹ। K L W Y N' — the letters were
        # right); wholesale rejection then costs a 2s retry + a Whisper call
        # and usually a worse transcript. Drop only the unsupported-script
        # tokens and re-validate what remains.
        salvage_tokens = [
            tok for tok in reject.raw_text.split()
            if not _UNSUPPORTED_SCRIPT_RE.search(tok)
        ]
        if salvage_tokens and len(salvage_tokens) < len(reject.raw_text.split()):
            salvaged_raw = " ".join(salvage_tokens)
            salvaged, _salv_reason = validate_transcript_ex(salvaged_raw)
            if salvaged:
                salvaged = self._post_process(salvaged, language)
                self.script_recovered_count += 1
                logger.warning(
                    "[STT] Script salvage: kept %r from rejected %r "
                    "(dropped %d junk-script token(s))",
                    salvaged, reject.raw_text,
                    len(reject.raw_text.split()) - len(salvage_tokens),
                )
                return STTResult(text=salvaged, detected_language=None,
                                 confidence=None, source="primary")

        retry_lang = self._resolve_script_retry_language(language, default_language)

        if not retry_lang:
            logger.warning(
                "[STT] Script misdetection: rejected %r (detected_lang=%s) but no "
                "language hint available — dropping segment (no retry, no fallback)",
                reject.raw_text, reject.detected_language,
            )
            return STTResult(text="")

        logger.warning(
            "[STT] Script misdetection: rejected %r (detected_lang=%s) — retrying "
            "Sarvam once with language forced to %s",
            reject.raw_text, reject.detected_language, retry_lang,
        )

        # A4 fix (2026-07-30): with the streaming provider as primary, the
        # retry used to call the stream's transcribe() — which only FLUSHES
        # (the turn's audio was already consumed server-side), so every
        # "forced-language retry" silently retried nothing and came back
        # empty. Retry through the provider's HTTP rescue client (same key,
        # same semantics) which actually re-uploads audio_bytes.
        _retry_provider = getattr(self._primary, "_rescue", None) or self._primary

        retry_reject: _RejectedTranscript | None = None
        try:
            text, had_error, detected_lang, retry_reject, conf = await self._try_provider(
                _retry_provider, "primary-script-retry",
                audio_bytes, sample_rate, retry_lang,
            )
        except SarvamRateLimitError as exc:
            # Same backoff bookkeeping as the main path; the segment stays
            # fallback-eligible since the audio contained speech.
            self._apply_sarvam_backoff(exc)
            text, had_error, detected_lang, conf = "", True, None, None

        if text:
            self.script_recovered_count += 1
            self._sarvam_rl_count = 0
            logger.warning(
                "[STT] Script-retry recovered transcript %r (lang=%s, forced=%s)",
                text, detected_lang, retry_lang,
            )
            return STTResult(text=text, detected_language=detected_lang,
                             confidence=conf, source="primary-script-retry")

        # Retry rejected/empty/errored → audio contained speech, so this is
        # fallback-eligible (unlike a clean-empty no-speech segment).
        logger.warning(
            "[STT] Script-retry failed (reject_reason=%s, had_error=%s) — "
            "audio contained speech, routing to fallback",
            retry_reject.reason if retry_reject else None, had_error,
        )
        fb_text, _, fb_lang, _, fb_conf = await self._try_provider(
            self._fallback, "fallback", audio_bytes, sample_rate, retry_lang
        )
        if fb_text:
            logger.info("[STT] Fallback succeeded after script reject: %r", fb_text)
        else:
            logger.warning(
                "[STT] Fallback also failed after script reject — dropping segment"
            )
        return STTResult(text=fb_text,
                         detected_language=fb_lang if fb_text else None,
                         confidence=fb_conf if fb_text else None,
                         source="fallback")

    def _post_process(self, text: str, language: str) -> str:
        if not text:
            return text
        return post_process_transcript(text, language, self._stt_corrections)

    async def _try_provider(
        self,
        provider: STTProvider,
        label: str,
        audio_bytes: bytes,
        sample_rate: int,
        language: str,
    ) -> tuple[str, bool, str | None, _RejectedTranscript | None, float | None]:
        """
        Attempt transcription with up to (max_retries + 1) attempts.

        Returns
        -------
        (transcript, had_error, detected_language, reject, confidence)
            transcript : str
                Clean, post-processed text; empty string if nothing usable.
            had_error : bool
                True if at least one attempt raised an exception (network /
                HTTP error). False means the provider responded cleanly but
                found no speech — the caller should NOT fall back in this case.
            detected_language : str | None
                BCP-47 language code returned by the provider (e.g. "gu-IN"),
                or None if the provider did not report one.
            reject : _RejectedTranscript | None
                Set when the provider returned a NON-EMPTY transcript that
                validate_transcript rejected — carries the rejection reason,
                raw text, and detected language so the caller can decide
                whether the rejection is retry-worthy (unsupported script).
            confidence : float | None
                Derived confidence (see _derive_confidence) for an accepted
                transcript; None on every empty/rejected path.
        """
        had_error = False

        if provider is self._fallback and not self.fallback_available():
            if self._fallback_skips_logged < 3:
                self._fallback_skips_logged += 1
                logger.warning(
                    "[STT] %s skipped — fallback provider out of quota, "
                    "cooling down another %.0fs", label,
                    self._fallback_disabled_until - time.monotonic())
            return "", True, None, None, None

        for attempt in range(1, self._max_retries + 2):
            try:
                t0 = time.monotonic()
                result: STTResult = await provider.transcribe(
                    audio_bytes, sample_rate, language
                )
                latency_ms = (time.monotonic() - t0) * 1000.0
                text = result.text
                provider_conf = result.confidence
                detected_lang = result.detected_language

                # Provider-reported confidence (rare — Sarvam REST has none)
                # can still gate early, before validation.
                if (provider_conf is not None
                        and provider_conf < self._min_confidence):
                    logger.info(
                        "[STT-FILTER] reason=low_confidence source=provider "
                        "score=%.2f text=%r provider=%s attempt=%d",
                        provider_conf, text, label, attempt,
                    )
                    return "", False, None, None, None

                if len(text) < self._min_length:
                    logger.info(
                        "[STT] %s short/empty transcript %r (attempt %d) — no speech",
                        label, text, attempt,
                    )
                    return "", False, None, None, None

                validated, reject_reason = validate_transcript_ex(text)
                if validated:
                    processed = self._post_process(validated, language)
                    confidence = self._derive_confidence(
                        processed, audio_bytes, sample_rate,
                        latency_ms, provider_conf,
                    )
                    if confidence < self._min_confidence:
                        # Low-score turns are routed to a re-ask, not
                        # silently accepted: empty + no reject = the caller
                        # treats it as clean-empty (no Whisper burn) and the
                        # pipeline plays its STT-fallback phrase.
                        logger.info(
                            "[STT-FILTER] reason=low_confidence "
                            "source=derived score=%.2f text=%r "
                            "provider=%s attempt=%d",
                            confidence, processed, label, attempt,
                        )
                        return "", False, None, None, None
                    logger.info(
                        "[STT] %s attempt %d: %r -> validated %r -> processed "
                        "%r (confidence=%.2f)",
                        label, attempt, text, validated, processed, confidence,
                    )
                    return processed, False, detected_lang, None, confidence

                logger.info(
                    "[STT] %s transcript %r rejected by validation (attempt %d)",
                    label, text, attempt,
                )
                return "", False, None, _RejectedTranscript(
                    reason=reject_reason or "unknown",
                    raw_text=text,
                    detected_language=detected_lang,
                ), None

            except SarvamRateLimitError:
                raise

            except (httpx.TimeoutException, APITimeoutError):
                # A timeout means the provider is HUNG, not flaky (08-03
                # incident pattern: individual requests stall server-side
                # while parallel ones answer in 300ms). Retrying the hung
                # provider burns another full read-timeout window while the
                # caller waits in silence — route to the fallback provider
                # immediately instead. APITimeoutError is the OpenAI SDK's
                # wrapper for the same condition on the Whisper side.
                logger.warning(
                    "[STT] %s attempt %d/%d timed out — skipping remaining "
                    "attempts, routing to fallback",
                    label, attempt, self._max_retries + 1,
                )
                had_error = True
                break

            except Exception as exc:
                logger.warning(
                    "[STT] %s attempt %d/%d exception: %s",
                    label, attempt, self._max_retries + 1, exc,
                )
                had_error = True
                if provider is self._fallback and _is_quota_error(exc):
                    # The account is out of credit: retrying is a guaranteed
                    # second 429 and so is every segment for the next while.
                    self._fallback_disabled_until = (
                        time.monotonic() + FALLBACK_QUOTA_COOLDOWN_S)
                    self._fallback_skips_logged = 0
                    logger.error(
                        "[STT] fallback provider reports NO QUOTA — disabling "
                        "the fallback for %.0fs (top up the account)",
                        FALLBACK_QUOTA_COOLDOWN_S)
                    break

            if attempt <= self._max_retries:
                logger.info(
                    "[STT] Retrying %s (attempt %d/%d)",
                    label, attempt + 1, self._max_retries + 1,
                )

        return "", had_error, None, None, None

    @staticmethod
    def _derive_confidence(
        text: str,
        audio_bytes: bytes,
        sample_rate: int,
        latency_ms: float,
        provider_confidence: float | None,
    ) -> float:
        """Derived 0–1 confidence for a validated transcript.

        Sarvam/Whisper report no usable confidence, so instead of faking one
        the router scores the transcript against signals it actually has:

        - words-per-second vs the audio duration: beyond ~6 wps the
          transcript carries more words than the audio plausibly did —
          the over-generation signature of hallucination;
        - a single word surviving from a long stretch of speech — most of
          the audio transcribed to nothing;
        - the function-word quality score the validator already computes
          (its hard floor is 0.3; 0.3–0.45 passes validation but is weak);
        - a mild penalty for extreme provider latency.

        A provider-reported confidence, when one ever exists, is the
        starting point instead of 1.0.
        """
        score = (provider_confidence
                 if provider_confidence is not None else 1.0)
        words = len(text.split())
        duration_s = (len(audio_bytes) / (2.0 * sample_rate)
                      if sample_rate else 0.0)
        wps = words / duration_s if duration_s > 0.15 else 0.0
        if wps > 8.0:
            score *= 0.3
        elif wps > 6.0:
            score *= 0.6
        if duration_s >= 1.5 and words == 1:
            score *= 0.6
        if _score_transcript_quality(text) < 0.45:
            score *= 0.75
        if latency_ms > 10_000:
            score *= 0.85
        return max(0.05, min(1.0, score))
