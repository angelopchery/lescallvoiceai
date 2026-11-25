"""
SarvamCreditTracker — estimates Sarvam API credit consumption from runtime signals.

Since Sarvam exposes no quota endpoint, all tracking is derived from:
  - STT: seconds of audio sent (len(audio_bytes) / sample_rate / 2)
  - TTS: characters of text sent (len(text))

Events are persisted to JSONL files under SARVAM_CREDIT_LOG_DIR so
usage survives restarts and can be queried offline by credit_report.py.

Each JSONL line is one of:
  {"type": "call_start", "call_id": "...", "ts": "ISO"}
  {"type": "stt_event",  "call_id": "...", "ts": "ISO", "provider": "sarvam"|"whisper", "audio_seconds": float}
  {"type": "tts_event",  "call_id": "...", "ts": "ISO", "chars": int, "completed": bool}
  {"type": "llm_event",  "call_id": "...", "ts": "ISO", "model": str, "input_tokens": int,
                         "cached_input_tokens": int, "output_tokens": int, ...}
  {"type": "call_end",   "call_id": "...", "ts": "ISO", ...aggregated fields...}

Despite the class name this is the whole AI ledger, not just Sarvam's half:
LLM token usage joined it on 2026-08-11, when the platform still had no record
of a single prompt token. Readers of old log files should expect llm_* fields
to be absent before that date — `credit_report.py` treats missing as zero.

Thread/task safety: asyncio.Lock protects in-memory accumulators.
File writes are append-only and wrapped in try/except — I/O errors
never surface to callers.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Resolved from THIS file's location, not hardcoded to the deployment path.
# Identical in production (the module lives under that repo), but a second
# checkout on the same host — a git worktree, a clone, a CI job — used to write
# its test usage straight into the production ledger. That happened twice while
# this was being built: a worktree running the suite filed 3,457 embedding
# requests against a model named `fake-embed-v1` into the file credit_report.py
# reads. SARVAM_CREDIT_LOG_DIR still overrides.
_DEFAULT_LOG_DIR = str(Path(__file__).resolve().parents[4] / "credit_log")
_CREDIT_LOG_DIR = Path(os.environ.get("SARVAM_CREDIT_LOG_DIR", _DEFAULT_LOG_DIR))

# ---------------------------------------------------------------------------
# Pricing lives in shared/sarvam_pricing.py — the single rate card the tracker,
# the offline report and the dashboard all read, so no two of them can disagree
# about what a call cost. Imported by path rather than assumed to be on
# sys.path: this module is loaded both from the repo root (the telephony
# bridge) and from calling-agent/backend (CLI run.py), and only the former has
# `shared` importable by default.
_REPO_ROOT = str(Path(__file__).resolve().parents[4])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from shared.sarvam_pricing import (  # noqa: E402
    stt_billed_seconds as _ceil_seconds,
    stt_cost as _calc_stt_cost,
    tts_cost as _calc_tts_cost,
    whisper_cost as _calc_whisper_cost,
)
from shared.ai_pricing import (  # noqa: E402
    embedding_cost as _calc_embed_cost,
    llm_cost as _calc_llm_cost,
    llm_cost_without_cache as _calc_llm_cost_uncached,
)

_PRICING_CONFIGURED = True  # real rates are known; always show estimates


def _usage_fields(a: "_CallAccum") -> dict:
    """The usage + cost block shared by call_end, per-call and session summaries.

    est_tts_cost is computed from tts_billed_chars (what Sarvam received), not
    tts_chars (what we wanted spoken) — see record_tts. est_total_cost is the
    real all-in figure and includes the Whisper fallback, which is a different
    vendor and was previously priced at zero.
    """
    stt = _calc_stt_cost(a.stt_sarvam_billed_seconds)
    tts = _calc_tts_cost(a.tts_billed_chars)
    whisper = _calc_whisper_cost(a.stt_whisper_seconds)
    llm = a.llm_cost  # None while every model seen is unpriced — see below
    return {
        "stt_sarvam_seconds": round(a.stt_sarvam_seconds, 3),
        "stt_sarvam_billed_seconds": round(a.stt_sarvam_billed_seconds, 3),
        "stt_sarvam_calls": a.stt_sarvam_calls,
        "stt_whisper_seconds": round(a.stt_whisper_seconds, 3),
        "stt_whisper_calls": a.stt_whisper_calls,
        "tts_chars": a.tts_chars,
        "tts_billed_chars": a.tts_billed_chars,
        "tts_calls": a.tts_calls,
        "tts_cancelled_calls": a.tts_cancelled_calls,
        "llm_requests": a.llm_requests,
        "llm_input_tokens": a.llm_input_tokens,
        "llm_cached_input_tokens": a.llm_cached_input_tokens,
        "llm_output_tokens": a.llm_output_tokens,
        "llm_models": sorted(a.llm_models),
        "llm_unpriced_models": sorted(a.llm_unpriced_models),
        "embed_requests": a.embed_requests,
        "embed_tokens": a.embed_tokens,
        "embed_models": sorted(a.embed_models),
        "est_embed_cost": a.embed_cost,
        "est_stt_cost": stt,
        "est_tts_cost": tts,
        "est_whisper_cost": whisper,
        "est_llm_cost": llm,
        # What the LLM would have cost with no prefix cache. Equal to
        # est_llm_cost while cached_input_tokens is 0, which is every call
        # today (the provider grants no cache — see prompt_cache.py), so any
        # future "caching saved X" is a subtraction of two measured numbers.
        "est_llm_cost_uncached": a.llm_cost_uncached,
        # Speech + LLM + embeddings. `llm or 0.0` treats an unpriced model as
        # zero HERE and only here, so the total stays a number;
        # llm_unpriced_models is what tells the reader the total is a floor
        # rather than the real figure.
        "est_total_cost": round(
            stt + tts + whisper + (llm or 0.0) + (a.embed_cost or 0.0), 6),
    }


@dataclass
class _CallAccum:
    call_id: str
    start_time: str
    end_time: str | None = None
    stt_sarvam_seconds: float = 0.0       # raw actual seconds
    stt_sarvam_billed_seconds: float = 0.0  # ceil-per-request (what Sarvam charges)
    stt_sarvam_calls: int = 0
    stt_whisper_seconds: float = 0.0
    stt_whisper_calls: int = 0
    tts_chars: int = 0            # characters the pipeline ASKED to speak
    tts_billed_chars: int = 0     # characters actually SUBMITTED to Sarvam
    tts_calls: int = 0
    tts_cancelled_calls: int = 0
    # LLM. Cost is accumulated per request rather than derived at the end
    # because a single call can span models (primary → fallback on a 429) at
    # different rates, and re-pricing a summed token count against one of them
    # would silently misprice every call that ever fell back.
    llm_requests: int = 0
    llm_input_tokens: int = 0
    llm_cached_input_tokens: int = 0   # subset of llm_input_tokens
    llm_output_tokens: int = 0
    llm_cost: float | None = None
    llm_cost_uncached: float | None = None
    llm_models: set = field(default_factory=set)
    llm_unpriced_models: set = field(default_factory=set)
    # Embeddings — the document-RAG query embedding, one per turn that reaches
    # retrieval, plus whatever ingestion spent. Small per request and
    # relentless per call, which is exactly the shape of cost that goes
    # unnoticed until it is counted.
    embed_requests: int = 0
    embed_tokens: int = 0
    embed_cost: float | None = None
    embed_models: set = field(default_factory=set)


class SarvamCreditTracker:
    """
    Singleton credit tracker for Sarvam STT + TTS usage.

    Obtain the instance via SarvamCreditTracker.get().
    All public methods are safe to call with call_id=None (no-ops at
    call level, still accumulates session totals).
    """

    _instance: "SarvamCreditTracker | None" = None

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active_calls: dict[str, _CallAccum] = {}
        # Session-level accumulators (reset only on process restart)
        self._s_stt_sarvam_sec: float = 0.0
        self._s_stt_sarvam_billed_sec: float = 0.0
        self._s_stt_sarvam_calls: int = 0
        self._s_stt_whisper_sec: float = 0.0
        self._s_stt_whisper_calls: int = 0
        self._s_tts_chars: int = 0
        self._s_tts_billed_chars: int = 0
        self._s_tts_calls: int = 0
        self._s_tts_cancelled: int = 0
        self._s_call_count: int = 0
        self._s_llm_requests: int = 0
        self._s_llm_input_tokens: int = 0
        self._s_llm_cached_input_tokens: int = 0
        self._s_llm_output_tokens: int = 0
        self._s_llm_cost: float | None = None
        self._s_llm_cost_uncached: float | None = None
        self._s_llm_models: set[str] = set()
        self._s_llm_unpriced_models: set[str] = set()
        self._s_embed_requests: int = 0
        self._s_embed_tokens: int = 0
        self._s_embed_cost: float | None = None
        self._s_embed_models: set[str] = set()

    @classmethod
    def get(cls) -> "SarvamCreditTracker":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # Call lifecycle
    # ------------------------------------------------------------------

    def start_call(self, call_id: str) -> None:
        """Register a new call. Call before the pipeline starts."""
        if not call_id:
            return
        ts = _now_iso()
        accum = _CallAccum(call_id=call_id, start_time=ts)
        self._active_calls[call_id] = accum
        self._s_call_count += 1
        _append_event({"type": "call_start", "call_id": call_id, "ts": ts})
        logger.info("[CREDITS] Call started: %s (session call #%d)", call_id, self._s_call_count)

    def end_call(self, call_id: str) -> None:
        """Finalise a call record. Call when the pipeline stops."""
        if not call_id:
            return
        ts = _now_iso()
        accum = self._active_calls.pop(call_id, None)
        if accum is None:
            return
        accum.end_time = ts
        event = {
            "type": "call_end",
            "call_id": call_id,
            "ts": ts,
            "start_time": accum.start_time,
            **_usage_fields(accum),
        }
        _append_event(event)
        logger.info(
            "[CREDITS] Call ended: %s | STT: %.1fs billed=%ds (%d calls Sarvam / %d Whisper) | "
            "TTS: %d chars billed=%d (%d calls, %d barge-in) | "
            "LLM: %d req in=%d (cached=%d) out=%d %s | est ₹%.4f%s",
            call_id,
            accum.stt_sarvam_seconds, accum.stt_sarvam_billed_seconds,
            accum.stt_sarvam_calls, accum.stt_whisper_calls,
            accum.tts_chars, accum.tts_billed_chars,
            accum.tts_calls, accum.tts_cancelled_calls,
            accum.llm_requests, accum.llm_input_tokens,
            accum.llm_cached_input_tokens, accum.llm_output_tokens,
            ",".join(sorted(accum.llm_models)) or "-",
            event["est_total_cost"],
            (" (excludes unpriced " + ",".join(sorted(accum.llm_unpriced_models)) + ")")
            if accum.llm_unpriced_models else "",
        )

    # ------------------------------------------------------------------
    # Per-event recording (async — acquires lock for in-memory state)
    # ------------------------------------------------------------------

    async def record_stt(
        self,
        call_id: str | None,
        audio_seconds: float,
        provider: str = "sarvam",
    ) -> None:
        """Record one STT API call that succeeded (HTTP 200).

        audio_seconds is the raw duration. For Sarvam, billing rounds up
        to the nearest second per request — billed_seconds = ceil(audio_seconds).
        """
        billed_seconds = _ceil_seconds(audio_seconds) if provider == "sarvam" else audio_seconds
        ts = _now_iso()
        async with self._lock:
            if provider == "sarvam":
                self._s_stt_sarvam_sec += audio_seconds
                self._s_stt_sarvam_billed_sec += billed_seconds
                self._s_stt_sarvam_calls += 1
                if call_id and call_id in self._active_calls:
                    self._active_calls[call_id].stt_sarvam_seconds += audio_seconds
                    self._active_calls[call_id].stt_sarvam_billed_seconds += billed_seconds
                    self._active_calls[call_id].stt_sarvam_calls += 1
            else:
                self._s_stt_whisper_sec += audio_seconds
                self._s_stt_whisper_calls += 1
                if call_id and call_id in self._active_calls:
                    self._active_calls[call_id].stt_whisper_seconds += audio_seconds
                    self._active_calls[call_id].stt_whisper_calls += 1
        _append_event({
            "type": "stt_event",
            "call_id": call_id,
            "ts": ts,
            "provider": provider,
            "audio_seconds": round(audio_seconds, 3),
            "billed_seconds": billed_seconds,
        })

    async def record_tts(
        self,
        call_id: str | None,
        chars: int,
        completed: bool = True,
        billed_chars: int | None = None,
    ) -> None:
        """Record one TTS utterance. completed=False means barge-in cancelled it.

        `chars` is what the pipeline asked to speak; `billed_chars` is what was
        actually POSTed to Sarvam. They differ on barge-in: speak() synthesizes
        an utterance as a sequence of clause-sized requests and breaks out of
        that loop the moment the caller interrupts, so the tail clauses are
        never sent — and Sarvam bills text it received, not text we intended.
        Charging `chars` there invented cost that was never incurred.

        billed_chars=None means the caller can't distinguish (older callers,
        non-chunked paths) — fall back to `chars`, which is exact whenever the
        utterance completed.
        """
        billed = chars if billed_chars is None else max(0, int(billed_chars))
        ts = _now_iso()
        async with self._lock:
            self._s_tts_chars += chars
            self._s_tts_billed_chars += billed
            self._s_tts_calls += 1
            if not completed:
                self._s_tts_cancelled += 1
            if call_id and call_id in self._active_calls:
                self._active_calls[call_id].tts_chars += chars
                self._active_calls[call_id].tts_billed_chars += billed
                self._active_calls[call_id].tts_calls += 1
                if not completed:
                    self._active_calls[call_id].tts_cancelled_calls += 1
        _append_event({
            "type": "tts_event",
            "call_id": call_id,
            "ts": ts,
            "chars": chars,
            "billed_chars": billed,
            "completed": completed,
        })

    async def record_llm(
        self,
        call_id: str | None,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        request_type: str = "turn",
        prefix_fingerprint: str | None = None,
    ) -> None:
        """Record one LLM request's token usage.

        Counts come from the provider's own usage block, never from an
        estimate — a tokenizer guess in a cost ledger is a number that looks
        authoritative and isn't. Callers that cannot obtain usage must not call
        this at all; a missing request is visibly missing (llm_requests lags
        the turn count), whereas an invented one is not.

        `cached_input_tokens` is the SUBSET of input_tokens the provider served
        from its prompt cache. It is 0 on every request today: the live key's
        cache quota is zero and the OpenAI-compat endpoint does not report
        `prompt_tokens_details` at all (both measured 2026-08-11 — see
        prompt_cache.py). Plumbed through now so the day either changes, the
        saving lands in the ledger without a schema change.

        `request_type` separates the spoken turn from the background extractor
        and the language classifier, which have wildly different token shapes;
        `prefix_fingerprint` is prompt_cache.prefix_fingerprint() of the system
        prompt, so a broken static prefix is greppable per turn.
        """
        cached = max(0, min(int(cached_input_tokens), int(input_tokens)))
        cost = _calc_llm_cost(model, input_tokens, output_tokens, cached)
        cost_uncached = _calc_llm_cost_uncached(model, input_tokens, output_tokens)
        ts = _now_iso()
        async with self._lock:
            self._s_llm_requests += 1
            self._s_llm_input_tokens += int(input_tokens)
            self._s_llm_cached_input_tokens += cached
            self._s_llm_output_tokens += int(output_tokens)
            self._s_llm_models.add(model)
            if cost is None:
                self._s_llm_unpriced_models.add(model)
            else:
                self._s_llm_cost = (self._s_llm_cost or 0.0) + cost
                self._s_llm_cost_uncached = (
                    (self._s_llm_cost_uncached or 0.0) + (cost_uncached or 0.0))
            accum = self._active_calls.get(call_id) if call_id else None
            if accum is not None:
                accum.llm_requests += 1
                accum.llm_input_tokens += int(input_tokens)
                accum.llm_cached_input_tokens += cached
                accum.llm_output_tokens += int(output_tokens)
                accum.llm_models.add(model)
                if cost is None:
                    accum.llm_unpriced_models.add(model)
                else:
                    accum.llm_cost = (accum.llm_cost or 0.0) + cost
                    accum.llm_cost_uncached = (
                        (accum.llm_cost_uncached or 0.0) + (cost_uncached or 0.0))
        _append_event({
            "type": "llm_event",
            "call_id": call_id,
            "ts": ts,
            "model": model,
            "request_type": request_type,
            "input_tokens": int(input_tokens),
            "cached_input_tokens": cached,
            "output_tokens": int(output_tokens),
            "prefix_fingerprint": prefix_fingerprint,
            "est_cost": cost,
        })

    def record_embedding_sync(self, tokens: int, model: str,
                              stage: str = "query",
                              call_id: str | None = None) -> None:
        """Record one embedding request. Synchronous by design.

        The document-RAG retrieval runs in a worker thread (so it cannot stall
        the audio loop), and a thread has no event loop to await the async
        recorders on. Accumulation is guarded by the GIL-atomic `+=` on ints
        rather than the asyncio lock the other recorders use; the ledger file
        write is append-only and already tolerant of interleaving.

        Like `record_llm`, `tokens` must be the provider's own count — callers
        that cannot obtain one pass 0 and the request is visibly uncounted.
        """
        if not tokens:
            return
        cost = _calc_embed_cost(model, tokens)
        self._s_embed_requests += 1
        self._s_embed_tokens += int(tokens)
        self._s_embed_models.add(model)
        if cost is not None:
            self._s_embed_cost = (self._s_embed_cost or 0.0) + cost
        accum = self._active_calls.get(call_id) if call_id else None
        if accum is not None:
            accum.embed_requests += 1
            accum.embed_tokens += int(tokens)
            accum.embed_models.add(model)
            if cost is not None:
                accum.embed_cost = (accum.embed_cost or 0.0) + cost
        _append_event({
            "type": "embed_event",
            "call_id": call_id,
            "ts": _now_iso(),
            "model": model,
            "stage": stage,
            "tokens": int(tokens),
            "est_cost": cost,
        })

    # ------------------------------------------------------------------
    # Summary accessors
    # ------------------------------------------------------------------

    def get_call_summary(self, call_id: str) -> dict | None:
        """Return in-progress accumulator for an active call, or None."""
        accum = self._active_calls.get(call_id)
        if accum is None:
            return None
        return {
            "call_id": call_id,
            "start_time": accum.start_time,
            **_usage_fields(accum),
        }

    def get_session_summary(self) -> dict:
        """Return totals since process start."""
        return {
            "call_count": self._s_call_count,
            **_usage_fields(_CallAccum(
                call_id="", start_time="",
                stt_sarvam_seconds=self._s_stt_sarvam_sec,
                stt_sarvam_billed_seconds=self._s_stt_sarvam_billed_sec,
                stt_sarvam_calls=self._s_stt_sarvam_calls,
                stt_whisper_seconds=self._s_stt_whisper_sec,
                stt_whisper_calls=self._s_stt_whisper_calls,
                tts_chars=self._s_tts_chars,
                tts_billed_chars=self._s_tts_billed_chars,
                tts_calls=self._s_tts_calls,
                tts_cancelled_calls=self._s_tts_cancelled,
                llm_requests=self._s_llm_requests,
                llm_input_tokens=self._s_llm_input_tokens,
                llm_cached_input_tokens=self._s_llm_cached_input_tokens,
                llm_output_tokens=self._s_llm_output_tokens,
                llm_cost=self._s_llm_cost,
                llm_cost_uncached=self._s_llm_cost_uncached,
                llm_models=set(self._s_llm_models),
                llm_unpriced_models=set(self._s_llm_unpriced_models),
                embed_requests=self._s_embed_requests,
                embed_tokens=self._s_embed_tokens,
                embed_cost=self._s_embed_cost,
                embed_models=set(self._s_embed_models),
            )),
        }


# ------------------------------------------------------------------
# File I/O helpers — module-level so they don't touch instance state
# ------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat(timespec="milliseconds") + "Z"


def _log_path(date: datetime.date | None = None) -> Path:
    d = date or datetime.date.today()
    return _CREDIT_LOG_DIR / f"{d.isoformat()}.jsonl"


def _append_event(event: dict) -> None:
    """Append one JSON line to today's log. Never raises."""
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug("[CREDITS] Event write failed: %s", exc)
