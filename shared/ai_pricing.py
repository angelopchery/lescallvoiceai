"""The AI rate card — every ₹ figure for LLM/embedding usage in one place.

Companion to `sarvam_pricing.py`, which owns the speech side (STT/TTS) and is
re-exported at the bottom so callers need exactly one import to price a whole
call. Split by vendor rather than merged because Sarvam's card is denominated
in ₹ per hour / per 10K chars while every LLM vendor quotes USD per 1M tokens;
folding them into one unit would bury a conversion in the middle of a table.

WHY THIS FILE EXISTS
--------------------
Until 2026-08-11 the platform recorded ZERO LLM tokens. `credit_tracker.py`
knew what every call cost in Sarvam credits to six decimal places and nothing
at all about the model that did the talking — so "what does a minute cost?"
could only be answered for the cheaper half of the bill. Token counts now flow
through `credit_tracker.record_llm()`; this module turns them into money.

UNPRICED MODELS ARE NOT FREE MODELS
-----------------------------------
A model missing from `_DEFAULT_LLM_RATES` returns cost=None, NOT 0.0. Tokens
are still counted and reported. Inventing a plausible rate for an unlisted
model is how a cost report becomes confidently wrong, so the report prints
"unpriced" and names the env key that would fix it. Add a rate only with a
vendor page in hand.

RATES ARE CONFIGURATION, NOT CODE
---------------------------------
Vendor pricing changes without warning and this repo is deployed from a bind
mount, so rates are overridable without a code change:

    AI_PRICING_FILE=/etc/lescall/ai_pricing.json   # JSON file, merged over defaults
    AI_PRICING_OVERRIDES={"gemini-3.5-flash": {"in": 1.25}}   # inline JSON

Both merge per-field over the defaults below, file first then inline, so a
deployment can correct one number without restating the table.

  in         USD per 1M input (prompt) tokens, uncached
  cached_in  USD per 1M input tokens served from the provider's prompt cache
  out        USD per 1M output tokens — INCLUDING reasoning/"thinking" tokens,
             which Gemini bills at the output rate and which the OpenAI-compat
             endpoint does not report separately (see prompt_cache.py)

Sources (checked 2026-08-11; Gemini rows re-checked 2026-09-11 against
ai.google.dev/gemini-api/docs/pricing, and 3.5-flash against the August 2026
invoice, which matches it to the cent at ~₹95.65/$):
  gemini-3.5-flash       $1.50 / $0.15 cached  / $9.00
  gemini-3.5-flash-lite  $0.30 / $0.03 cached  / $2.50
  gemini-3.1-flash-lite  $0.25 / $0.025 cached / $1.50
  gpt-4o-mini            $0.15 / $0.075 cached / $0.60 OpenAI published card
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

# --- currency --------------------------------------------------------------

# Sarvam bills in ₹, every LLM vendor in $, and the platform reports one
# number. The default is not a market rate — it is the rate already implied by
# sarvam_pricing.WHISPER_PRICE_PER_MIN (₹0.53 for $0.006/min = 88.33), kept
# identical so the two modules cannot disagree about what a dollar costs.
USD_INR = float(os.environ.get("AI_USD_INR", "88.33"))


# --- LLM rate card (USD per 1M tokens) -------------------------------------

_DEFAULT_LLM_RATES: dict[str, dict[str, float]] = {
    # Voice path (LLM_MODEL). Output is expensive relative to input here, and
    # Gemini's thinking tokens are billed as output — a "minimal" reasoning
    # setting is not a zero-cost one.
    "gemini-3.5-flash": {"in": 1.50, "cached_in": 0.15, "out": 9.00},
    # What 78% of the last 30 days' calls actually ran, via per-agent override.
    "gemini-3.1-flash-lite": {"in": 0.25, "cached_in": 0.025, "out": 1.50},
    # The Flash-Lite tier: 80% cheaper input than 3.5-flash, 72% cheaper output.
    "gemini-3.5-flash-lite": {"in": 0.30, "cached_in": 0.03, "out": 2.50},
    # LLM_FALLBACK / legacy default.
    "gpt-4o-mini": {"in": 0.15, "cached_in": 0.075, "out": 0.60},
}

# Deliberately absent: gemini-3.6-flash (COPILOT_LLM_MODEL — its $0.75/$3.75
# card is a promotion that doubles on 2027-01-01, a date a hardcoded rate would
# silently outlive), gemini-2.5-flash (INTEL_LLM_MODEL default),
# gemini-embedding-001. Their tokens are counted and reported as unpriced until
# someone adds a sourced rate.


# --- embedding rate card (USD per 1M input tokens) --------------------------
#
# Embeddings are billed on input only — there is no output side — so this is a
# flat per-token rate rather than the in/out pair above. Two callers today: the
# document RAG that grounds live calls (`RAG_EMBED_MODEL`) and the call-search
# index (`INTEL_EMBED_MODEL`).
#
# The same "unpriced is not free" rule applies: a model absent here returns
# None, its tokens are still counted, and the report names it rather than
# inventing a rate.
#
# Sources (checked 2026-08-12; gemini-embedding-2 checked 2026-09-25 against
# ai.google.dev/gemini-api/docs/pricing, "Standard" tier, text input):
#   text-embedding-3-small  $0.02 / 1M   OpenAI published card
#   text-embedding-3-large  $0.13 / 1M   OpenAI published card
#   text-embedding-ada-002  $0.10 / 1M   OpenAI published card (legacy)
#   gemini-embedding-2      $0.20 / 1M   Google published card (RAG_EMBED_MODEL since §57)
_DEFAULT_EMBED_RATES: dict[str, float] = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
    "text-embedding-ada-002": 0.10,
    "gemini-embedding-2": 0.20,
}
# gemini-embedding-001 (INTEL_EMBED_MODEL) is deliberately absent — it is no
# longer on Google's pricing page (checked 2026-09-25), so there is no sourced
# rate to record; its tokens are counted and reported as unpriced.


def _load_embed_overrides() -> dict[str, float]:
    """`AI_PRICING_FILE` / `AI_PRICING_OVERRIDES` may carry an `embeddings` key."""
    merged: dict[str, float] = {}
    path = os.environ.get("AI_PRICING_FILE", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                merged.update((json.load(f).get("embeddings") or {}))
        except Exception:
            pass  # the LLM loader below already warns about an unreadable file
    inline = os.environ.get("AI_PRICING_OVERRIDES", "").strip()
    if inline:
        try:
            merged.update((json.loads(inline).get("embeddings") or {}))
        except Exception:
            pass
    return {k: float(v) for k, v in merged.items() if v is not None}


def _load_overrides() -> dict[str, dict[str, float]]:
    """File then inline JSON, merged per-field over the defaults. Never raises."""
    merged: dict[str, dict[str, float]] = {}
    path = os.environ.get("AI_PRICING_FILE", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                merged.update(json.load(f))
        except Exception as exc:
            logger.warning("[PRICING] AI_PRICING_FILE %s unreadable: %s", path, exc)
    inline = os.environ.get("AI_PRICING_OVERRIDES", "").strip()
    if inline:
        try:
            for model, rates in json.loads(inline).items():
                merged.setdefault(model, {}).update(rates)
        except Exception as exc:
            logger.warning("[PRICING] AI_PRICING_OVERRIDES unparseable: %s", exc)
    return merged


def _build_rates() -> dict[str, dict[str, float]]:
    rates = {m: dict(r) for m, r in _DEFAULT_LLM_RATES.items()}
    for model, over in _load_overrides().items():
        rates.setdefault(model, {}).update(
            {k: float(v) for k, v in over.items() if v is not None})
    return rates


LLM_RATES = _build_rates()


# Segments that denote a DIFFERENT model at a different price, not a release
# qualifier of the same one. `gemini-3.5-flash-lite` starting with the string
# `gemini-3.5-flash` is a naming coincidence, not a pricing relationship: a
# naive prefix match priced it at $1.50/1M instead of its own far lower rate,
# a 6x overstatement on the model that serves most of the fleet.
_TIER_TOKENS = frozenset({
    "lite", "mini", "nano", "micro", "small", "pro", "ultra", "max",
    "flash", "opus", "sonnet", "haiku", "thinking", "reasoning", "turbo",
})


def llm_rate(model: str) -> dict[str, float] | None:
    """Rate entry for `model`, or None if unpriced.

    Exact id first, then a segment-wise prefix match so a dated or preview
    suffix on an otherwise unchanged model stays priced
    (`gemini-3.5-flash-preview-09-2026` → `gemini-3.5-flash`). An exact-only
    lookup would silently unprice the whole fleet the day a vendor appends a
    date; an unrestricted prefix match would mis-price every tier variant. The
    match is therefore rejected when any extra segment names a tier — that is a
    different model and belongs in the table under its own name.
    """
    if not model:
        return None
    if model in LLM_RATES:
        return LLM_RATES[model]
    parts = model.split("-")
    for known in sorted(LLM_RATES, key=len, reverse=True):
        kparts = known.split("-")
        if parts[:len(kparts)] != kparts:
            continue
        if any(seg.lower() in _TIER_TOKENS for seg in parts[len(kparts):]):
            continue  # e.g. ...-flash vs ...-flash-lite — genuinely different
        return LLM_RATES[known]
    return None


def llm_cost(
    model: str,
    input_tokens: float = 0.0,
    output_tokens: float = 0.0,
    cached_input_tokens: float = 0.0,
) -> float | None:
    """₹ for one LLM request, or None when `model` has no registered rate.

    `cached_input_tokens` is the portion of `input_tokens` the provider served
    from its prompt cache — a SUBSET, not an addition, matching how both
    OpenAI's `prompt_tokens_details.cached_tokens` and Gemini's
    `cachedContentTokenCount` report it. Passing it separately is what makes a
    cache saving visible instead of averaged away. When a model has no
    `cached_in` rate the cached portion is billed at the full input rate, which
    is the conservative direction: it under-claims the saving, never over.
    """
    rate = llm_rate(model)
    if rate is None:
        return None
    cached = max(0.0, min(float(cached_input_tokens), float(input_tokens)))
    fresh = float(input_tokens) - cached
    cached_rate = rate.get("cached_in", rate.get("in", 0.0))
    usd = (
        fresh / 1e6 * rate.get("in", 0.0)
        + cached / 1e6 * cached_rate
        + float(output_tokens) / 1e6 * rate.get("out", 0.0)
    )
    return round(usd * USD_INR, 6)


def llm_cost_without_cache(
    model: str, input_tokens: float = 0.0, output_tokens: float = 0.0,
) -> float | None:
    """What the same request would have cost with every input token billed fresh.

    The counterfactual behind any "saved ₹X" claim. Reported alongside the real
    cost so a saving is always a subtraction of two measured numbers rather
    than an estimate.
    """
    return llm_cost(model, input_tokens, output_tokens, cached_input_tokens=0.0)


EMBED_RATES: dict[str, float] = {**_DEFAULT_EMBED_RATES, **_load_embed_overrides()}


def embedding_rate(model: str) -> float | None:
    """USD per 1M tokens for `model`, or None when unpriced.

    Uses the same segment-wise prefix match as `llm_rate` so a dated suffix
    stays priced while a different tier (`-large` vs `-small`) never inherits
    its sibling's rate.
    """
    if not model:
        return None
    if model in EMBED_RATES:
        return EMBED_RATES[model]
    parts = model.split("-")
    for known in sorted(EMBED_RATES, key=len, reverse=True):
        kparts = known.split("-")
        if parts[:len(kparts)] != kparts:
            continue
        if any(seg.lower() in _TIER_TOKENS for seg in parts[len(kparts):]):
            continue
        return EMBED_RATES[known]
    return None


def embedding_cost(model: str, tokens: float) -> float | None:
    """₹ for one embedding request, or None when `model` has no registered rate."""
    rate = embedding_rate(model)
    if rate is None:
        return None
    return round(float(tokens) / 1e6 * rate * USD_INR, 8)


def rate_card() -> dict:
    """The live rates, for display next to any figure derived from them."""
    from . import sarvam_pricing as _sp
    return {
        "llm": LLM_RATES,
        "llm_unit": "USD per 1M tokens",
        "embeddings": EMBED_RATES,
        "embedding_unit": "USD per 1M input tokens",
        "usd_inr": USD_INR,
        "speech": _sp.rate_card(),
        "currency": "INR",
    }


# --- speech re-exports -----------------------------------------------------
# One import prices a whole call. These are aliases, not copies: sarvam_pricing
# stays the single definition of the speech rates and its own tests keep
# guarding them.

from .sarvam_pricing import (  # noqa: E402,F401
    stt_billed_seconds,
    stt_cost,
    tts_cost,
    whisper_cost,
)
