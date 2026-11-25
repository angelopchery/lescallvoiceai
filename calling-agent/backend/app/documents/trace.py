"""End-to-end tracing for RAG retrievals.

One trace per retrieval, carrying a stage-by-stage latency breakdown, the score
distribution, the abstention decision, token usage and cost. Two sinks:

  * a JSONL file per day, alongside the existing AI ledger
    (`credit_log/`), so a retrieval can be correlated with the LLM and speech
    events of the same call by `call_id`;
  * a bounded in-memory ring, so the live p50/p90/p95/p99 and error rates can
    be read straight off a running runtime without parsing files.

The platform has no OpenTelemetry collector, so this deliberately mirrors the
shape already in use (`voice/credit_tracker.py`: append-only JSONL, best-effort
writes, never raises into the caller) instead of introducing a second, competing
observability system. `to_otel_attributes()` keeps the span shape
OTel-compatible for the day a collector does arrive.

WHAT IS NOT LOGGED
------------------
Chunk text and query text are tenant document content and caller speech. The
trace records ids, hashes, scores and counts; the text itself only when
RAG_TRACE_INCLUDE_TEXT is explicitly enabled for debugging. Credentials, keys
and file paths outside the agent folder never enter a trace.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from . import config

logger = logging.getLogger("rag.trace")

_RING_MAX = 500
_ring: deque = deque(maxlen=_RING_MAX)
_ring_lock = threading.Lock()


def _trace_dir() -> Path:
    if config.TRACE_DIR:
        return Path(config.TRACE_DIR)
    base = os.environ.get("SARVAM_CREDIT_LOG_DIR", "")
    if base:
        return Path(base)
    return Path(__file__).resolve().parents[4] / "credit_log"


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass
class RagTrace:
    """Accumulates one retrieval's spans; `finish()` emits it."""

    agent: str
    trace_id: str = field(default_factory=new_trace_id)
    request_id: str = ""
    call_id: str | None = None
    turn_index: int | None = None
    query_chars: int = 0
    query_hash: str = ""
    query_text: str = ""

    corpus_version: str = ""
    embed_model: str = ""
    embed_dim: int | None = None
    retrieval_top_k: int = 0
    final_top_k: int = 0
    rerank_model: str = ""

    stage_ms: dict[str, float] = field(default_factory=dict)
    candidates: int = 0
    returned: int = 0
    retrieval_scores: list[float] = field(default_factory=list)
    rerank_scores: list[float] = field(default_factory=list)
    top_sources: list[str] = field(default_factory=list)

    cache_hit: bool = False
    cache_tier: str = ""
    abstained: bool = False
    abstain_reason: str = ""
    degraded: bool = False
    index_problems: list[str] = field(default_factory=list)
    error: str = ""

    embed_tokens: int = 0
    embed_cost_inr: float | None = None
    context_chars: int = 0

    _t0: float = field(default_factory=time.perf_counter)
    total_ms: float = 0.0

    def set_query(self, text: str) -> None:
        self.query_chars = len(text or "")
        self.query_hash = hashlib.sha256((text or "").encode()).hexdigest()[:12]
        if config.TRACE_INCLUDE_TEXT:
            self.query_text = text

    def span(self, name: str) -> "_Span":
        return _Span(self, name)

    def record(self, name: str, ms: float) -> None:
        self.stage_ms[name] = round(self.stage_ms.get(name, 0.0) + ms, 3)

    def to_record(self) -> dict:
        rec = {
            "type": "rag_retrieval",
            "ts": datetime.datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
            "trace_id": self.trace_id,
            "request_id": self.request_id or self.trace_id,
            "call_id": self.call_id,
            "turn_index": self.turn_index,
            "agent": self.agent,
            "corpus_version": self.corpus_version,
            "embed_model": self.embed_model,
            "embed_dim": self.embed_dim,
            "retrieval_top_k": self.retrieval_top_k,
            "final_top_k": self.final_top_k,
            "rerank_model": self.rerank_model,
            "query_chars": self.query_chars,
            "query_hash": self.query_hash,
            "candidates": self.candidates,
            "returned": self.returned,
            "retrieval_scores": [round(s, 4) for s in self.retrieval_scores[:10]],
            "rerank_scores": [round(s, 5) for s in self.rerank_scores[:10]],
            "top_sources": self.top_sources[:5],
            "cache_hit": self.cache_hit,
            "cache_tier": self.cache_tier,
            "abstained": self.abstained,
            "abstain_reason": self.abstain_reason,
            "degraded": self.degraded,
            "index_problems": self.index_problems[:5],
            "error": self.error,
            "embed_tokens": self.embed_tokens,
            "embed_cost_inr": self.embed_cost_inr,
            "context_chars": self.context_chars,
            "stage_ms": self.stage_ms,
            "total_ms": round(self.total_ms, 3),
        }
        if config.TRACE_INCLUDE_TEXT and self.query_text:
            rec["query_text"] = self.query_text
        return rec

    def to_otel_attributes(self) -> dict:
        """Span attributes under the conventional `gen_ai.*` / `rag.*` keys."""
        rec = self.to_record()
        return {
            "gen_ai.system": "lescall.rag",
            "gen_ai.request.model": self.embed_model,
            "gen_ai.usage.input_tokens": self.embed_tokens,
            "rag.agent": self.agent,
            "rag.corpus_version": self.corpus_version,
            "rag.top_k": self.retrieval_top_k,
            "rag.final_k": self.final_top_k,
            "rag.candidates": self.candidates,
            "rag.returned": self.returned,
            "rag.cache_hit": self.cache_hit,
            "rag.abstained": self.abstained,
            "rag.degraded": self.degraded,
            "rag.latency_ms": rec["total_ms"],
        }

    def finish(self) -> dict:
        self.total_ms = (time.perf_counter() - self._t0) * 1000
        rec = self.to_record()
        with _ring_lock:
            _ring.append(rec)
        if config.TRACING_ENABLED:
            _append(rec)
        level = logging.WARNING if (self.error or self.degraded) else logging.DEBUG
        logger.log(level,
                   "[RAG] %s agent=%s %dms cand=%d ret=%d cache=%s abstain=%s%s",
                   self.trace_id, self.agent, int(self.total_ms), self.candidates,
                   self.returned, self.cache_tier or "miss",
                   self.abstain_reason or self.abstained,
                   f" ERROR={self.error}" if self.error else "")
        return rec


class _Span:
    def __init__(self, trace: RagTrace, name: str):
        self.trace, self.name = trace, name

    def __enter__(self):
        self._t = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.trace.record(self.name, (time.perf_counter() - self._t) * 1000)
        return False


def _append(record: dict) -> None:
    """One JSON line to today's file. Never raises."""
    try:
        path = _trace_dir() / f"rag-{datetime.date.today().isoformat()}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug("[RAG] trace write failed: %s", exc)


# ---------------------------------------------------------------------------
# Live metrics off the ring
# ---------------------------------------------------------------------------

def recent(limit: int = 50) -> list[dict]:
    with _ring_lock:
        return list(_ring)[-limit:]


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    import math
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def metrics_snapshot() -> dict:
    """Production RAG health over the last `_RING_MAX` retrievals.

    Percentiles are computed over what actually happened in this process, not
    over a benchmark — which is the only kind of p95 worth alerting on.
    """
    with _ring_lock:
        rows = list(_ring)
    if not rows:
        return {"retrievals": 0}
    lat = [r["total_ms"] for r in rows]
    served = [r for r in rows if not r["abstained"]]
    stages = sorted({k for r in rows for k in r.get("stage_ms", {})})
    costs = [r["embed_cost_inr"] for r in rows if r.get("embed_cost_inr") is not None]
    from .cache import get_cache
    return {
        "window": len(rows),
        "retrievals": len(rows),
        "latency_ms": {
            "p50": round(_pct(lat, 50), 2), "p90": round(_pct(lat, 90), 2),
            "p95": round(_pct(lat, 95), 2), "p99": round(_pct(lat, 99), 2),
            "max": round(max(lat), 2),
            "stages_p95": {s: round(_pct([r["stage_ms"][s] for r in rows
                                          if s in r.get("stage_ms", {})], 95), 2)
                           for s in stages},
        },
        "quality": {
            "abstention_rate": round(sum(1 for r in rows if r["abstained"]) / len(rows), 4),
            "degraded_rate": round(sum(1 for r in rows if r["degraded"]) / len(rows), 4),
            "error_rate": round(sum(1 for r in rows if r["error"]) / len(rows), 4),
            "avg_candidates": round(sum(r["candidates"] for r in rows) / len(rows), 2),
            "avg_returned": round(sum(r["returned"] for r in served) / len(served), 2)
            if served else 0.0,
            "avg_top_score": round(sum(r["retrieval_scores"][0] for r in rows
                                       if r["retrieval_scores"]) /
                                   max(1, sum(1 for r in rows if r["retrieval_scores"])), 4),
            "index_problem_rate": round(
                sum(1 for r in rows if r.get("index_problems")) / len(rows), 4),
        },
        "cost": {
            "embed_tokens_total": sum(r["embed_tokens"] for r in rows),
            "embed_tokens_per_query": round(
                sum(r["embed_tokens"] for r in rows) / len(rows), 2),
            "embed_cost_inr_total": round(sum(costs), 6) if costs else None,
            "embed_cost_inr_per_query": round(sum(costs) / len(costs), 8) if costs else None,
            "cache": get_cache().stats.as_dict(),
        },
    }
