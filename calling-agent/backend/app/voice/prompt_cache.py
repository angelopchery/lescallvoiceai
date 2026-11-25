"""Prompt-cache plumbing: split the system prompt, fingerprint it, ask the
provider whether it will actually cache anything.

BACKGROUND — measured 2026-08-11, not assumed
---------------------------------------------
Every turn of a call re-sends the whole system prompt. Measured on the live
Pramukh agent: 12,724 characters = 3,236 input tokens per turn, at 4.8 turns
per call-minute ≈ 19,300 input tokens/minute. Input is ~98% of the LLM bill,
and 86% of those tokens are byte-identical from one turn to the next — the
prompt was reordered into STATIC / PER-CALL / PER-TURN bands (see
`conversation._build_system_prompt`) precisely so a provider prefix cache
could serve that 86%.

It does not. Probing the live key on 2026-08-11:

    cachedContentTokenCount = 0 on six consecutive byte-identical requests
    cachedContents.create   → 429 TotalCachedContentStorageTokensPerModel
                              FreeTier limit exceeded: limit=0

on gemini-3.5-flash, gemini-3.1-flash-lite, gemini-3.5-flash-lite and
gemini-2.5-flash alike. The key is on Google's free tier, where the prompt-cache
storage quota is zero — implicit AND explicit caching are both unavailable by
policy, not by misconfiguration. No amount of application code changes that;
it needs billing enabled on the Google Cloud project.

WHAT THIS MODULE THEREFORE DOES
-------------------------------
It does NOT implement an application-side cache of model responses. Caching a
generated reply and replaying it into a live call is a correctness hazard the
measured data does not justify: 85.5% of agent utterances across 45 days of
transcripts are unique, so there is almost nothing to hit, and the 14.5% that
repeat are canned lines already served from the TTS disk cache.

Instead it does the three things that make provider-side caching work the day
the quota exists, and that are useful now regardless:

1. `split_prompt()`  — the STATIC/dynamic boundary as a function, so the split
   is defined once instead of re-derived by each caller.
2. `prefix_fingerprint()` — a hash of the static band, logged once per turn.
   Prefix stability is the entire precondition for prompt caching, and it is
   destroyed by anything as small as a timestamp in the identity block. The
   fingerprint makes a break visible in production on the turn it happens
   rather than in a bill three weeks later. `tests/test_prompt_cache.py` pins
   the same property in CI.
3. `probe_cache_capability()` — asks the provider, off the call path, whether
   it will grant a cache. Turns an invisible ₹2.88/min leak into one startup
   log line.

None of this is on the latency path: the split is a string partition, the
fingerprint is one sha256 over ~11KB (~20µs), and the probe runs in the
background at most once per process.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# The first byte of per-turn state. `_build_system_prompt` emits its bands in
# STATIC → PER-CALL → PER-TURN order and this header opens the PER-TURN band,
# so everything before it is identical across the turns of a call. Kept as a
# constant because it is load-bearing in three places (the builder, this split
# and the CI guard) and a silent rename would disable prefix caching without
# failing anything.
STATIC_PREFIX_MARKER = "━━ CALL STATE ━━"

# A provider only caches a prefix above some minimum length; below it the
# request is billed entirely fresh however stable the prefix is. 1024 is
# Gemini 2.5 Flash's documented floor and the smallest of any model in the
# fleet, so it is the conservative threshold at which to warn.
MIN_CACHEABLE_TOKENS = int(os.environ.get("PROMPT_CACHE_MIN_TOKENS", "1024"))

ENABLE_PROMPT_CACHE_PROBE = (
    os.environ.get("ENABLE_PROMPT_CACHE_PROBE", "true").lower() == "true")


def split_prompt(prompt: str) -> tuple[str, str]:
    """Split a rendered system prompt into (static_prefix, dynamic_suffix).

    The static prefix is everything before the per-turn state header. Returns
    ("", prompt) when the marker is absent — an agent whose prompt has no state
    block has no stable prefix to speak of, and claiming one would overstate
    the cacheable fraction.
    """
    if not prompt:
        return "", ""
    idx = prompt.find(STATIC_PREFIX_MARKER)
    if idx < 0:
        return "", prompt
    return prompt[:idx], prompt[idx:]


def estimate_tokens(text: str) -> int:
    """Rough token count for a mixed Latin/Indic string.

    Latin at ~4 chars/token, Devanagari and Gujarati at ~1.7 — Indic scripts
    tokenize far worse than English and treating the prompt as uniformly
    English understates a Hindi prompt by a third. Calibrated against the live
    endpoint on 2026-08-11 (re-verified 2026-09-08 after the SCRIPT FIDELITY
    block, RUNBOOK §36, grew every prompt by ~430 tokens): this estimator
    gives 3,700 for the English Pramukh prompt where the API reported 3,666
    (+0.9%). Good enough for a threshold warning; never used to compute
    money, which only ever comes from provider-reported counts.
    """
    if not text:
        return 0
    latin = sum(1 for c in text if ord(c) < 0x0500)
    return int(latin / 4.0 + (len(text) - latin) / 1.7)


def prefix_fingerprint(prompt: str) -> str:
    """Short hash of the static prefix — identical across turns iff cacheable.

    Truncated to 12 hex chars: this goes in a log line once per turn, and 48
    bits is far past enough to notice that two turns differ.
    """
    static, _ = split_prompt(prompt)
    if not static:
        return "none"
    return hashlib.sha256(static.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class CacheCapability:
    """What the provider will actually do with a repeated prefix."""

    available: bool
    reason: str
    provider: str = "unknown"
    model: str = ""

    def log(self) -> None:
        if self.available:
            logger.info(
                "[PROMPT-CACHE] %s/%s: provider caching AVAILABLE — %s",
                self.provider, self.model, self.reason)
        else:
            logger.warning(
                "[PROMPT-CACHE] %s/%s: provider caching UNAVAILABLE — %s. "
                "The static prompt prefix is re-billed fresh on every turn.",
                self.provider, self.model, self.reason)


async def probe_cache_capability(
    model: str,
    api_key: str,
    base_url: str | None,
    timeout_s: float = 10.0,
) -> CacheCapability:
    """Ask the provider whether it will grant a prompt cache. Never raises.

    Gemini is probed by attempting to create a `cachedContents` entry and
    reading the error: the free tier answers `limit=0` explicitly, which is a
    definitive negative no amount of retrying changes. The probe creates the
    smallest object the API accepts and deletes it immediately on success, so a
    positive answer costs one cache-storage-second.

    Any non-Gemini base URL returns available=False with reason "unprobed"
    rather than a guess — OpenAI's prompt caching is automatic and unqueryable,
    so absence of a probe result is not evidence of absence of caching.
    """
    if not api_key:
        return CacheCapability(False, "no API key configured", "unknown", model)

    url = (base_url or "").rstrip("/")
    if "generativelanguage.googleapis.com" not in url:
        return CacheCapability(
            False, "unprobed (no capability API for this provider)",
            _provider_of(url), model)

    root = url.split("/openai")[0].rstrip("/")
    try:
        import httpx
        # Padded far above any provider minimum: a cache create rejected for
        # being too small would look identical to one rejected for quota, and
        # the two call for opposite fixes.
        filler = ("You are a helpful assistant for a real estate call centre. "
                  * 400)
        # The key rides in a header, never `?key=`: httpx logs every request
        # URL at INFO, which wrote the live Gemini key into the runtime log
        # on every process start.
        async with httpx.AsyncClient(timeout=timeout_s,
                                     headers={"x-goog-api-key": api_key}) as client:
            resp = await client.post(
                f"{root}/cachedContents",
                json={
                    "model": f"models/{model}",
                    "systemInstruction": {"parts": [{"text": filler}]},
                    "ttl": "60s",
                    "displayName": "lescall-cache-capability-probe",
                },
            )
            if resp.status_code == 200:
                name = resp.json().get("name", "")
                if name:
                    try:
                        await client.delete(f"{root}/{name}")
                    except Exception:
                        pass  # 60s TTL reaps it anyway
                return CacheCapability(
                    True, "cachedContents.create succeeded", "gemini", model)
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")[:200]
            except Exception:
                detail = resp.text[:200]
            if "FreeTier" in detail or "limit=0" in detail:
                return CacheCapability(
                    False,
                    f"free tier, cache quota is zero ({detail[:120]}) — "
                    "enable billing on the Google Cloud project",
                    "gemini", model)
            return CacheCapability(
                False, f"HTTP {resp.status_code}: {detail}", "gemini", model)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return CacheCapability(False, f"probe failed: {exc!r}", "gemini", model)


def _provider_of(base_url: str) -> str:
    if not base_url:
        return "openai"
    if "generativelanguage" in base_url:
        return "gemini"
    if "openai.azure" in base_url:
        return "azure-openai"
    if "groq" in base_url:
        return "groq"
    return "openai-compatible"


def spawn_capability_probe(
    model: str, api_key: str, base_url: str | None,
) -> asyncio.Task | None:
    """Fire the probe in the background, once per process. Never blocks a call.

    Returns the task (tests await it) or None when disabled or already run. The
    result is logged, not returned to the caller: nothing on the call path may
    branch on it, because a probe that hangs must never be able to delay a turn.
    """
    global _PROBE_DONE
    if not ENABLE_PROMPT_CACHE_PROBE or _PROBE_DONE:
        return None
    _PROBE_DONE = True

    async def _run() -> None:
        cap = await probe_cache_capability(model, api_key, base_url)
        cap.log()

    try:
        return asyncio.ensure_future(_run())
    except RuntimeError:
        return None  # no running loop (sync CLI context) — not worth forcing one


_PROBE_DONE = False
