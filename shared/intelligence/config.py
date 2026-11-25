"""Configuration for the intelligence pipeline.

All knobs are env vars so behavior is configuration-driven (no code edits to
retune). The INTEL_LLM_* block falls back to the live-call LLM_* values, but
production should point it at a dedicated key so background analysis can never
starve live-call quota.
"""

from __future__ import annotations

import os


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Master switch — set INTEL_ENABLED=false to freeze the whole pipeline.
ENABLED = _flag("INTEL_ENABLED", True)

# Extraction LLM (OpenAI-compatible endpoint; base URL/key default to the
# live-call stack, but the MODEL is deliberately different: gemini-2.5-flash is
# stronger for structured extraction and sits in a separate quota bucket from
# the live-call gemini-3.1-flash-lite, so background analysis never competes
# with live calls (verified against the key's /models list 2026-07-20).
LLM_BASE_URL = os.environ.get("INTEL_LLM_BASE_URL") or os.environ.get("LLM_BASE_URL", "")
LLM_MODEL = os.environ.get("INTEL_LLM_MODEL", "gemini-2.5-flash")
LLM_API_KEY = os.environ.get("INTEL_LLM_API_KEY") or os.environ.get("LLM_API_KEY", "")
LLM_TIMEOUT_S = _int("INTEL_LLM_TIMEOUT_S", 90)
# Gemini's thinking tokens count against max_tokens on the OpenAI-compat
# endpoint (a truncated-JSON trap; see project history). Keep effort low and
# the budget generous.
LLM_REASONING_EFFORT = os.environ.get("INTEL_LLM_REASONING_EFFORT", "low")
LLM_MAX_OUTPUT_TOKENS = _int("INTEL_LLM_MAX_OUTPUT_TOKENS", 10000)

# Embeddings (hybrid semantic search). Same OpenAI-compat endpoint family.
EMBED_ENABLED = _flag("INTEL_EMBED_ENABLED", True)
EMBED_BASE_URL = os.environ.get("INTEL_EMBED_BASE_URL") or LLM_BASE_URL
EMBED_API_KEY = os.environ.get("INTEL_EMBED_API_KEY") or LLM_API_KEY
EMBED_MODEL = os.environ.get("INTEL_EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM = _int("INTEL_EMBED_DIM", 768)
EMBED_CHUNK_TOKENS = _int("INTEL_EMBED_CHUNK_TOKENS", 600)

# ---------------------------------------------------------------------------
# Verified smart filter (Intelligence → natural-language call filter)
#
# Hybrid retrieval answers "which calls are ABOUT this?". That is not the
# question a filter asks. "whoever relocated to Vapi" and "whoever agreed to
# relocate to Delhi" have nearly identical embeddings, and every call that
# discusses relocation scores high on both — so the second query returned
# Vapi calls instead of the empty result that is the truthful answer.
#
# So retrieval is demoted to CANDIDATE GENERATION and a small model decides
# membership per call, against an explicit requirement list, with a verbatim
# quote it must be able to produce. Recall comes from the retrieval arms;
# precision comes from the judge. An empty result is a valid answer here and
# the UI says so rather than falling back to "close enough".
# ---------------------------------------------------------------------------

FILTER_VERIFY_ENABLED = _flag("INTEL_FILTER_VERIFY_ENABLED", True)
# Deliberately the SMALLEST model on the key: this is one call per candidate,
# and the task is a yes/no read of a 1.7k-char transcript against a checklist
# — not a task a bigger model does better, just slower and dearer.
FILTER_LLM_MODEL = os.environ.get("INTEL_FILTER_LLM_MODEL", "gemini-3.5-flash-lite")
FILTER_LLM_BASE_URL = os.environ.get("INTEL_FILTER_LLM_BASE_URL") or LLM_BASE_URL
FILTER_LLM_API_KEY = os.environ.get("INTEL_FILTER_LLM_API_KEY") or LLM_API_KEY
FILTER_LLM_REASONING_EFFORT = os.environ.get("INTEL_FILTER_REASONING_EFFORT", "minimal")
FILTER_LLM_MAX_OUTPUT_TOKENS = _int("INTEL_FILTER_MAX_OUTPUT_TOKENS", 1200)
# Per-request ceiling. A judge that has not answered in this long is not going
# to say anything useful about a 40-second phone call.
FILTER_LLM_TIMEOUT_S = _int("INTEL_FILTER_LLM_TIMEOUT_S", 25)
# How many retrieved calls the judge is allowed to examine. Recall is bounded
# HERE, not by the number of rows shown — the two used to be the same number,
# which is why widening the result list also had to widen the cost.
# 120 was too small for a real campaign: on 2026-08-29 a 609-call account with
# 98 genuine matches could seat at most 120 candidates, and the arms only
# reached 103 of them (see ARM_LIMIT in search.py). Raising the arms without
# raising this would just move the ceiling here.
FILTER_MAX_CANDIDATES = _int("INTEL_FILTER_MAX_CANDIDATES", 300)
# Judged in parallel; Gemini flash-lite answers in ~1.3s, so 16 in flight
# turns 300 candidates into ~25s worst case, and the verdict cache makes the
# second run of the same filter free.
FILTER_CONCURRENCY = _int("INTEL_FILTER_CONCURRENCY", 16)
# Whole-pass wall-clock budget. On exhaustion the pass STOPS and reports how
# many candidates it examined rather than quietly admitting unjudged rows —
# a filter that says "checked 60 of 120" is honest; one that pads the tail
# with unchecked matches is the bug this feature exists to fix.
# Raised with the candidate pool: 300 candidates at 16 in flight is ~25s of
# real work, and a 60s budget left no headroom for a slow provider — the pass
# would truncate and quietly return a short answer, which is the same silent
# under-reporting the wider pool was meant to fix.
FILTER_BUDGET_S = _int("INTEL_FILTER_BUDGET_S", 120)
# Below this the judge's own stated confidence is treated as a non-match.
FILTER_MIN_CONFIDENCE = float(os.environ.get("INTEL_FILTER_MIN_CONFIDENCE", "0.55"))
# Transcript budget per judged call (chars). p99 of the live corpus is ~13k.
FILTER_MAX_TRANSCRIPT_CHARS = _int("INTEL_FILTER_MAX_TRANSCRIPT_CHARS", 14000)
# Bump when the planner/judge prompt changes meaning: it is part of the cache
# key, so old verdicts stop being reused instead of silently outliving the
# rubric that produced them.
FILTER_RUBRIC_VERSION = "v1"


# Worker tuning.
WORKER_CONCURRENCY = _int("INTEL_WORKER_CONCURRENCY", 2)
POLL_INTERVAL_S = _int("INTEL_POLL_INTERVAL_S", 5)
SWEEP_INTERVAL_S = _int("INTEL_SWEEP_INTERVAL_S", 60)
SWEEP_MIN_CALL_AGE_S = _int("INTEL_SWEEP_MIN_CALL_AGE_S", 30)

# Retry policy (queue-level).
BACKOFF_BASE_S = _int("INTEL_BACKOFF_BASE_S", 60)
BACKOFF_CAP_S = _int("INTEL_BACKOFF_CAP_S", 3600)

# Transcript budget for extraction (chars ≈ 4×tokens; middle-out truncation).
EXTRACT_MAX_TRANSCRIPT_CHARS = _int("INTEL_EXTRACT_MAX_TRANSCRIPT_CHARS", 48000)

# Failed-dial outcome classification (CDR read-alongside; never touches
# billing). Runs in the intel worker's sweeper loop on its own cadence.
DIAL_OUTCOMES_ENABLED = _flag("INTEL_DIAL_OUTCOMES_ENABLED", True)
DIAL_OUTCOMES_INTERVAL_S = _int("INTEL_DIAL_OUTCOMES_INTERVAL_S", 300)

TIMEZONE = os.environ.get("INTEL_TIMEZONE", "Asia/Kolkata")


# ---------------------------------------------------------------------------
# Follow-up auto-dialer
#
# This is the only part of the platform that places a phone call to a real
# customer with no human in the loop, so every knob below is a brake, and the
# defaults are the conservative end of what is still useful. A previous
# auto-dial scheduler was disabled precisely because it had none of these: it
# dialled any 'scheduled' row whose time had passed, at any hour, forever.
#
# The kill switch is INTEL_AUTODIAL_ENABLED=false — it takes effect on the next
# tick (<=2 min), no restart needed, because the loop re-reads it every pass.
# ---------------------------------------------------------------------------

AUTODIAL_ENABLED = _flag("INTEL_AUTODIAL_ENABLED", True)
# Seconds between sweeps. 60 keeps a "call me in 10 minutes" promise honest
# (worst case: due + one sweep) while still pacing any backlog drain.
AUTODIAL_POLL_S = _int("INTEL_AUTODIAL_POLL_S", 60)
# Local-time calling window (inclusive start, exclusive end), in TIMEZONE.
AUTODIAL_HOUR_START = _int("INTEL_AUTODIAL_HOUR_START", 10)
AUTODIAL_HOUR_END = _int("INTEL_AUTODIAL_HOUR_END", 19)
# Courtesy window for CUSTOMER-STATED callback times (auto_scheduled=0):
# "call me at 8 tonight" is honoured at 8 tonight, any weekday — but never
# before 07:00 or after 21:00 local, whatever the caller said.
AUTODIAL_CB_HOUR_START = _int("INTEL_AUTODIAL_CB_HOUR_START", 7)
AUTODIAL_CB_HOUR_END = _int("INTEL_AUTODIAL_CB_HOUR_END", 21)
# Permitted weekdays, Monday=0 … Sunday=6. Default Mon-Sat: Sunday calling is
# the fastest way to generate complaints.
AUTODIAL_WEEKDAYS = os.environ.get("INTEL_AUTODIAL_WEEKDAYS", "0,1,2,3,4,5")
# How many times one follow-up may be auto-dialled before it stops and waits
# for a human.
AUTODIAL_MAX_ATTEMPTS = _int("INTEL_AUTODIAL_MAX_ATTEMPTS", 3)
# Minimum minutes between two auto-attempts on the SAME follow-up.
AUTODIAL_RETRY_GAP_MIN = _int("INTEL_AUTODIAL_RETRY_GAP_MIN", 180)
# Hard ceiling on auto-placed calls per tenant per local day.
AUTODIAL_MAX_PER_DAY = _int("INTEL_AUTODIAL_MAX_PER_DAY", 50)
# How many calls one sweep may place PER TENANT. At the default poll of 120s,
# 1 means a backlog drains at ~30 calls/hour per tenant — roughly one every two
# minutes — instead of emptying in a single burst. That pacing is also what
# makes a mistake survivable: a bad batch is noticed after one call, not five.
# Per-tenant rather than global so a tenant with 30 queued rows cannot starve a
# tenant with 3; the real ceiling per tenant remains AUTODIAL_MAX_PER_DAY.
AUTODIAL_BATCH = _int("INTEL_AUTODIAL_BATCH", 1)
# Grace period after scheduled_at before the row is considered due, so a
# human still has a moment to intervene on something just scheduled.
AUTODIAL_GRACE_MIN = _int("INTEL_AUTODIAL_GRACE_MIN", 5)
# How stale a due row may be and still be auto-dialled. Beyond this the
# context is cold and calling is worse than not calling.
AUTODIAL_MAX_AGE_H = _int("INTEL_AUTODIAL_MAX_AGE_H", 72)


def autodial_weekdays() -> set[int]:
    out: set[int] = set()
    for part in (AUTODIAL_WEEKDAYS or "").split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 6:
            out.add(int(part))
    return out or {0, 1, 2, 3, 4, 5}
