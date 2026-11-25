"""Semantic cache for RETRIEVAL results (never for generated answers).

WHAT IS CACHED, AND WHAT IS DELIBERATELY NOT
--------------------------------------------
This caches the evidence a question retrieves, not the sentence the agent
speaks. Replaying a generated reply into a live call is a correctness hazard
the data does not justify — 85.5% of agent utterances across 45 days of
transcripts are unique (measured in `voice/prompt_cache.py`), so there is
almost nothing to hit, and the lines that do repeat are canned responses
already served from the TTS disk cache.

WHERE THE SAVING ACTUALLY IS
----------------------------
The expensive part of a retrieval is the query embedding: one HTTPS round trip,
measured at p50 320ms / p95 622ms against the live provider. So the cache has
two tiers and only one of them saves that:

  exact     — normalised query text matches a previous one. Answered with NO
              embedding call at all. This is the tier that saves latency and
              tokens.
  semantic  — the query had to be embedded, but its vector is within
              `CACHE_SIMILARITY_THRESHOLD` of a previous query's. Saves the
              index scan (microseconds) and, more usefully, guarantees two
              phrasings of one question see the same evidence.

Reporting a semantic hit as a saved embedding call would be a fabricated
saving; `stats()` therefore counts the two tiers separately and only attributes
token savings to the exact tier.

ISOLATION
---------
Entries live in per-namespace buckets and every lookup takes its namespace as
an argument, so a lookup cannot reach another namespace's entries even in
principle. That is the difference between an isolation boundary and a filter
applied after a global similarity search — the latter is one refactor away from
leaking one tenant's documents to another. The namespace binds the agent
folder (which is the document-ownership boundary), the corpus version (so an
edited or deleted document invalidates every answer derived from it) and a
fingerprint of the retrieval configuration (so changing top-K or the re-ranker
does not serve results produced by the old settings).
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from . import config

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+", re.UNICODE)


def normalize_query(text: str) -> str:
    """Casefold, strip punctuation, collapse whitespace.

    Enough for "What is the price?" / "what is the price" to share an entry;
    deliberately not stemming or stopword-stripping, which would collapse
    questions that mean different things ("is it available" vs "it is available
    now?").
    """
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", (text or "").casefold())).strip()


def namespace(agent_folder: str, corpus_version: str, config_fp: str) -> str:
    return f"{agent_folder}|{corpus_version}|{config_fp}"


def config_fingerprint(**overrides: Any) -> str:
    """Hash of the retrieval settings that change what evidence comes back."""
    base = {
        "top_k": config.RETRIEVAL_TOP_K,
        "final_k": config.FINAL_TOP_K,
        "rerank": config.RERANK_ENABLED,
        "rerank_model": config.RERANK_MODEL,
        "abstain": config.ABSTENTION_ENABLED,
        "min_score": config.ABSTAIN_MIN_SCORE,
        "embed_model": config.EMBED_MODEL,
        "mmr": config.MMR_LAMBDA,
    }
    base.update({k: v for k, v in overrides.items() if v is not None})
    raw = "|".join(f"{k}={base[k]}" for k in sorted(base))
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


@dataclass
class Entry:
    query: str
    vector: np.ndarray | None
    payload: Any
    created: float
    hits: int = 0
    embed_tokens: int = 0


@dataclass
class CacheStats:
    exact_hits: int = 0
    semantic_hits: int = 0
    misses: int = 0
    evictions: int = 0
    expired: int = 0
    stores: int = 0
    embed_tokens_saved: int = 0
    embed_calls_saved: int = 0
    lookup_ns_total: float = 0.0
    lookups: int = 0

    def as_dict(self) -> dict:
        total = self.exact_hits + self.semantic_hits + self.misses
        return {
            "lookups": total,
            "exact_hits": self.exact_hits,
            "semantic_hits": self.semantic_hits,
            "misses": self.misses,
            "hit_rate": round((self.exact_hits + self.semantic_hits) / total, 4) if total else None,
            "exact_hit_rate": round(self.exact_hits / total, 4) if total else None,
            "evictions": self.evictions,
            "expired": self.expired,
            "entries_stored": self.stores,
            # Only the exact tier avoids the API call, so only it may claim a
            # token saving.
            "embed_calls_saved": self.embed_calls_saved,
            "embed_tokens_saved": self.embed_tokens_saved,
            "avg_lookup_us": round(self.lookup_ns_total / self.lookups / 1000, 2)
            if self.lookups else None,
        }


class SemanticCache:
    """Process-local, bounded, thread-safe. No external store on purpose:
    the runtime is a single container per host and a Redis dependency on the
    live-call path would add a failure mode to save a sub-millisecond lookup."""

    def __init__(self, ttl_s: int | None = None, max_entries: int | None = None,
                 threshold: float | None = None):
        self.ttl_s = ttl_s if ttl_s is not None else config.CACHE_TTL_S
        self.max_entries = max_entries if max_entries is not None else config.CACHE_MAX_ENTRIES
        self.threshold = threshold if threshold is not None else config.CACHE_SIMILARITY_THRESHOLD
        self._entries: OrderedDict[tuple[str, str], Entry] = OrderedDict()
        self._by_ns: dict[str, list[str]] = {}
        self._lock = threading.Lock()
        self.stats = CacheStats()

    # -- internals ---------------------------------------------------------

    def _expired(self, entry: Entry, now: float) -> bool:
        return self.ttl_s > 0 and (now - entry.created) > self.ttl_s

    def _drop(self, key: tuple[str, str]) -> None:
        self._entries.pop(key, None)
        ns, qkey = key
        bucket = self._by_ns.get(ns)
        if bucket and qkey in bucket:
            bucket.remove(qkey)
            if not bucket:
                self._by_ns.pop(ns, None)

    # -- public ------------------------------------------------------------

    def lookup_exact(self, ns: str, query: str) -> Entry | None:
        """Tier 1 — no embedding needed, so a hit skips the API call."""
        t0 = time.perf_counter_ns()
        qkey = normalize_query(query)
        with self._lock:
            self.stats.lookups += 1
            entry = self._entries.get((ns, qkey))
            if entry is None:
                self.stats.lookup_ns_total += time.perf_counter_ns() - t0
                return None
            if self._expired(entry, time.time()):
                self._drop((ns, qkey))
                self.stats.expired += 1
                self.stats.lookup_ns_total += time.perf_counter_ns() - t0
                return None
            self._entries.move_to_end((ns, qkey))
            entry.hits += 1
            self.stats.exact_hits += 1
            self.stats.embed_calls_saved += 1
            self.stats.embed_tokens_saved += entry.embed_tokens
            self.stats.lookup_ns_total += time.perf_counter_ns() - t0
            return entry

    def lookup_semantic(self, ns: str, vector: np.ndarray) -> Entry | None:
        """Tier 2 — the query is already embedded; reuse a near-identical one."""
        t0 = time.perf_counter_ns()
        now = time.time()
        with self._lock:
            keys = list(self._by_ns.get(ns, ()))
            best, best_sim = None, 0.0
            for qkey in keys:
                entry = self._entries.get((ns, qkey))
                if entry is None:
                    continue
                if self._expired(entry, now):
                    self._drop((ns, qkey))
                    self.stats.expired += 1
                    continue
                if entry.vector is None:
                    continue
                sim = float(np.dot(entry.vector, vector))
                if sim > best_sim:
                    best, best_sim = entry, sim
            if best is not None and best_sim >= self.threshold:
                best.hits += 1
                self.stats.semantic_hits += 1
                self.stats.lookup_ns_total += time.perf_counter_ns() - t0
                return best
            self.stats.misses += 1
            self.stats.lookup_ns_total += time.perf_counter_ns() - t0
            return None

    def store(self, ns: str, query: str, vector: np.ndarray | None,
              payload: Any, embed_tokens: int = 0) -> None:
        qkey = normalize_query(query)
        if not qkey:
            return
        vec = None
        if vector is not None:
            vec = np.asarray(vector, dtype=np.float32)
            norm = float(np.linalg.norm(vec))
            if norm:
                vec = vec / norm          # so lookup_semantic is a plain dot
        with self._lock:
            key = (ns, qkey)
            if key in self._entries:
                self._drop(key)
            self._entries[key] = Entry(query=query, vector=vec, payload=payload,
                                       created=time.time(), embed_tokens=embed_tokens)
            self._by_ns.setdefault(ns, []).append(qkey)
            self.stats.stores += 1
            while len(self._entries) > self.max_entries:
                oldest, _ = next(iter(self._entries.items()))
                self._drop(oldest)
                self.stats.evictions += 1

    def invalidate_agent(self, agent_folder: str) -> int:
        """Drop every namespace belonging to one agent, whatever its corpus
        version or configuration. Called when a document is added or deleted."""
        prefix = f"{agent_folder}|"
        with self._lock:
            doomed = [k for k in self._entries if k[0].startswith(prefix)]
            for key in doomed:
                self._drop(key)
        return len(doomed)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._by_ns.clear()

    def size(self) -> int:
        return len(self._entries)


_cache: SemanticCache | None = None


def get_cache() -> SemanticCache:
    global _cache
    if _cache is None:
        _cache = SemanticCache()
    return _cache


def reset_cache() -> None:
    global _cache
    _cache = None
