"""The retrieval pipeline: query -> evidence, with a decision attached.

    query
      -> guard          (is this turn even a question?)
      -> cache          (exact tier: answers with no API call at all)
      -> embed          (vector arm; hard timeout, degrades instead of hanging)
      -> lexical        (BM25 arm; always available, no network)
      -> fuse           (RRF, then MMR for redundancy)
      -> abstain?       (evidence gate — conservative by measurement)
      -> context        (labelled sources + the grounding contract)
      -> citations      (structured, machine-side, never spoken)
      -> trace

Everything returns a `RetrievalResult`, never a bare string: the caller needs
to know whether evidence was found, how strong it was, and what to tell the
model when it wasn't. The old signature (`embeddings.retrieve_context`) is kept
as a thin wrapper over this.

FAILURE POSTURE
---------------
Every external dependency here is optional at query time. No embedding
credentials, a provider timeout, an unreadable index, a stale index: each
degrades to the lexical arm and sets `degraded`, rather than raising into a
live call or — the previous behaviour — silently returning nothing at all.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import cache as cache_mod
from . import config, embeddings, grounding, rerank, trace as trace_mod

logger = logging.getLogger(__name__)


@dataclass
class RetrievedChunk:
    doc_id: str
    chunk_id: int
    text: str
    score: float                       # fused score (or cosine when unfused)
    doc_type: str = ""
    title: str = ""
    label: str = ""                    # S1, S2, … — the citation handle
    has_vector: bool = False           # False = judged by the lexical arm only
    kept_by: str = ""                  # "vector" | "lexical" — why it survived
    stage_scores: dict[str, float] = field(default_factory=dict)


@dataclass
class RetrievalResult:
    query: str
    chunks: list[RetrievedChunk] = field(default_factory=list)
    candidates: list[RetrievedChunk] = field(default_factory=list)
    context_block: str = ""
    citations: list[dict] = field(default_factory=list)
    abstained: bool = False
    abstain_reason: str = ""
    degraded: bool = False
    degraded_reason: str = ""
    cache_hit: bool = False
    cache_tier: str = ""
    corpus_version: str = ""
    index_problems: list[str] = field(default_factory=list)
    stage_ms: dict[str, float] = field(default_factory=dict)
    embed_tokens: int = 0
    embed_cost_inr: float | None = None
    trace_id: str = ""

    @property
    def has_evidence(self) -> bool:
        return bool(self.chunks) and not self.abstained

    def to_api(self) -> dict:
        """Additive payload for API responses — never replaces an existing field."""
        return {
            "sources": self.citations,
            "metadata": {
                "trace_id": self.trace_id,
                "cache_hit": self.cache_hit,
                "abstained": self.abstained,
                "abstain_reason": self.abstain_reason or None,
                "degraded": self.degraded,
                "corpus_version": self.corpus_version,
                "latency_ms": round(sum(self.stage_ms.values()), 2),
                "embed_tokens": self.embed_tokens,
            },
        }


# ---------------------------------------------------------------------------
# Index cache — the index is re-read only when the documents change
# ---------------------------------------------------------------------------

@dataclass
class _CachedIndex:
    signature: tuple
    index: embeddings.LoadedIndex
    bm25: rerank.BM25


_index_cache: dict[str, _CachedIndex] = {}


def _signature(agent_folder: str) -> tuple:
    """Cheap staleness probe: mtimes of the index and each vector file.

    Loading and normalising every `.npy` on every turn of every call is pure
    waste — the documents change on upload, not on turn. A few stat() calls
    detect that without trusting a TTL.
    """
    docs_dir = embeddings._docs_dir(agent_folder)
    parts: list = []
    try:
        idx = docs_dir / "index.json"
        parts.append((idx.stat().st_mtime_ns, idx.stat().st_size))
        for sub in sorted(docs_dir.iterdir()):
            if sub.is_dir():
                for name in (embeddings.EMBEDDINGS_FILE, embeddings.CHUNKS_FILE,
                             embeddings.INDEX_META_FILE):
                    f = sub / name
                    parts.append((sub.name, name,
                                  f.stat().st_mtime_ns if f.exists() else 0))
    except FileNotFoundError:
        return ()
    return tuple(parts)


def load_cached_index(agent_folder: str, provider=None) -> tuple[embeddings.LoadedIndex,
                                                                 rerank.BM25]:
    sig = _signature(agent_folder)
    hit = _index_cache.get(agent_folder)
    if hit is not None and hit.signature == sig:
        return hit.index, hit.bm25
    index = embeddings.load_index(agent_folder, provider)
    bm25 = rerank.BM25.build([c.text for c in index.chunks])
    _index_cache[agent_folder] = _CachedIndex(sig, index, bm25)
    return index, bm25


def invalidate(agent_folder: str) -> None:
    """Called after an upload/delete/re-index: drop the index and every cached
    answer derived from it."""
    _index_cache.pop(agent_folder, None)
    cache_mod.get_cache().invalidate_agent(agent_folder)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _is_informative(query: str) -> bool:
    terms = rerank.content_terms(query)
    return (len(terms) >= config.ABSTAIN_MIN_QUERY_TERMS
            and len(" ".join(terms)) >= config.ABSTAIN_MIN_QUERY_CHARS)


def _embed_cost(tokens: int, model: str) -> float | None:
    if not tokens:
        return None
    try:
        from shared.ai_pricing import embedding_cost
        return embedding_cost(model, tokens)
    except Exception:
        return None


def retrieve(agent_folder: str, query: str, *,
             provider=None,
             retrieval_top_k: int | None = None,
             final_top_k: int | None = None,
             rerank_enabled: bool | None = None,
             abstention_enabled: bool | None = None,
             use_cache: bool | None = None,
             call_id: str | None = None,
             turn_index: int | None = None,
             request_id: str = "") -> RetrievalResult:
    """Retrieve evidence for one query. Never raises."""
    top_k = retrieval_top_k or config.RETRIEVAL_TOP_K
    final_k = final_top_k or config.FINAL_TOP_K
    do_rerank = config.RERANK_ENABLED if rerank_enabled is None else rerank_enabled
    do_abstain = (config.ABSTENTION_ENABLED if abstention_enabled is None
                  else abstention_enabled)
    do_cache = config.CACHE_ENABLED if use_cache is None else use_cache

    provider = provider or embeddings.get_provider()
    tr = trace_mod.RagTrace(agent=agent_folder, call_id=call_id,
                            turn_index=turn_index, request_id=request_id)
    tr.set_query(query)
    tr.retrieval_top_k, tr.final_top_k = top_k, final_k
    tr.rerank_model = config.RERANK_MODEL if do_rerank else "none"
    tr.embed_model = getattr(provider, "model", "")
    tr.embed_dim = getattr(provider, "dim", None)

    result = RetrievalResult(query=query, trace_id=tr.trace_id)

    try:
        if not config.ENABLED:
            return _abstain(result, tr, "rag_disabled")
        if not _is_informative(query):
            # No embedding call, no index read: a filler turn is not a question.
            return _abstain(result, tr, "not_a_question")

        with tr.span("index"):
            index, bm25 = load_cached_index(agent_folder, provider)
        result.corpus_version = tr.corpus_version = index.corpus_version
        result.index_problems = tr.index_problems = list(index.problems)
        if not index.chunks:
            return _abstain(result, tr, "no_documents")

        ns = cache_mod.namespace(
            agent_folder, index.corpus_version,
            cache_mod.config_fingerprint(top_k=top_k, final_k=final_k,
                                         rerank=do_rerank, abstain=do_abstain))
        cache = cache_mod.get_cache()

        if do_cache:
            with tr.span("cache_lookup"):
                entry = cache.lookup_exact(ns, query)
            if entry is not None:
                cached: RetrievalResult = entry.payload
                return _from_cache(cached, result, tr, "exact")

        # -- vector arm ---------------------------------------------------
        qvec = None
        if index.has_vectors and embeddings.provider_available(provider):
            try:
                with tr.span("embed"):
                    qvec, tokens = embeddings.embed_query(query, provider)
                result.embed_tokens = tr.embed_tokens = tokens
                result.embed_cost_inr = tr.embed_cost_inr = _embed_cost(
                    tokens, getattr(provider, "model", ""))
                embeddings._record_embedding_cost(
                    tokens, getattr(provider, "model", ""), "query")
            except Exception as exc:
                result.degraded = tr.degraded = True
                result.degraded_reason = f"embedding unavailable: {exc.__class__.__name__}"
                logger.warning("[RAG] %s embedding failed, lexical-only: %r",
                               agent_folder, exc)
        elif not index.has_vectors:
            result.degraded = tr.degraded = True
            result.degraded_reason = "no usable vector index"

        if qvec is not None and do_cache:
            with tr.span("cache_lookup"):
                entry = cache.lookup_semantic(ns, qvec)
            if entry is not None:
                return _from_cache(entry.payload, result, tr, "semantic")

        # -- candidate generation -----------------------------------------
        with tr.span("search"):
            vector_ranked, cosines = _vector_arm(index, qvec, top_k)
            lexical_ranked, bm_scores = _lexical_arm(bm25, query, top_k)

        with tr.span("rerank"):
            ordered, fused = _fuse(index, vector_ranked, lexical_ranked, cosines,
                                   do_rerank, top_k, final_k)

        candidates = [_mk_chunk(index, i, fused, cosines, bm_scores)
                      for i in ordered]
        result.candidates = candidates
        tr.candidates = len(candidates)
        # `.get` and not `[...]`: a document whose vectors are missing or stale
        # is still reachable through the lexical arm, so `ordered` legitimately
        # contains chunks that have no cosine at all.
        tr.retrieval_scores = ([float(cosines.get(i, 0.0)) for i in ordered[:10]]
                               if cosines else [])
        tr.rerank_scores = [float(fused.get(i, 0.0)) for i in ordered[:10]]

        # Pruning is part of withholding weak evidence, so it lives under the
        # same switch: `abstention_enabled=False` means "hand back the best
        # candidates whatever their confidence", which is what the evaluation
        # needs to measure raw ranking.
        # Chunks the lexical arm ranks at the top, whatever their cosine. A
        # short exact-term question ("what are the six stages?") scores 0.22
        # against the chunk that literally contains "Six Stage Implementation
        # Approach" — under the absolute floor — while BM25 puts it first.
        # Pruning on the vector score alone threw those away, which defeats the
        # entire purpose of running a lexical arm.
        lexical_keep = {i for i in lexical_ranked[:final_k] if bm_scores.get(i, 0) > 0}
        chosen = (_prune_weak(candidates, lexical_keep, ordered) if do_abstain
                  else candidates)[:final_k]

        # -- abstention gate ----------------------------------------------
        cache_key = (ns, query, qvec) if do_cache else None
        if do_abstain:
            reason = _abstain_reason(chosen, query, cosines)
            if reason:
                result.candidates = candidates
                return _abstain(result, tr, reason, cache_key)
        if not chosen:
            return _abstain(result, tr, "no_candidates", cache_key)

        for n, chunk in enumerate(chosen, 1):
            chunk.label = f"S{n}"
        result.chunks = chosen
        result.citations = _citations(chosen)
        result.context_block = grounding.render_context(
            [{"label": c.label, "text": _clip(c.text), "doc_type": c.doc_type}
             for c in chosen])
        tr.returned = len(chosen)
        tr.top_sources = [f"{c.doc_id}#{c.chunk_id}" for c in chosen]
        tr.context_chars = len(result.context_block)

        if do_cache:
            cache.store(ns, query, qvec, result, embed_tokens=result.embed_tokens)
        return _finish(result, tr)

    except Exception as exc:                      # never break a live turn
        logger.exception("[RAG] retrieval failed for %s", agent_folder)
        result.degraded = tr.degraded = True
        result.degraded_reason = repr(exc)
        tr.error = f"{exc.__class__.__name__}: {exc}"
        return _abstain(result, tr, "error")


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

def _vector_arm(index: embeddings.LoadedIndex, qvec, top_k: int):
    """Cosine over the (already L2-normalised) matrix -> ranked chunk indices."""
    if qvec is None or index.matrix is None:
        return [], None
    sims = index.matrix @ qvec                     # unit vectors -> dot == cosine
    cosines = {row: float(sims[i]) for i, row in enumerate(index.vector_rows)}
    ranked = sorted(cosines, key=lambda r: -cosines[r])[:top_k]
    return ranked, cosines


def _lexical_arm(bm25: rerank.BM25, query: str, top_k: int):
    scores = bm25.scores(query)
    if not len(scores):
        return [], {}
    order = [int(i) for i in np.argsort(-scores)[:top_k] if scores[i] > 0]
    return order, {int(i): float(scores[i]) for i in range(len(scores))}


def _fuse(index, vector_ranked, lexical_ranked, cosines, do_rerank, top_k, final_k):
    if not do_rerank:
        # Vector-only ordering, i.e. the pre-upgrade behaviour, kept reachable
        # by configuration so the two can be compared on the same corpus.
        ordered = vector_ranked or lexical_ranked
        return ordered[:top_k], {i: float(cosines.get(i, 0.0)) if cosines else 0.0
                                 for i in ordered}

    fused = rerank.rrf_fuse([vector_ranked, lexical_ranked], k=config.RRF_K,
                            weights=[1.0, config.LEXICAL_WEIGHT])
    ordered = sorted(fused, key=lambda i: -fused[i])[:top_k]
    if len(ordered) > final_k and config.MMR_LAMBDA < 1.0:
        texts = {i: index.chunks[i].text for i in ordered}

        def similarity(a: int, b: int) -> float:
            if index.matrix is not None and cosines:
                ra, rb = _row(index, a), _row(index, b)
                if ra is not None and rb is not None:
                    return float(np.dot(index.matrix[ra], index.matrix[rb]))
            return rerank.jaccard(texts[a], texts[b])

        ordered = rerank.mmr(ordered, fused, similarity,
                             lambda_=config.MMR_LAMBDA, limit=len(ordered))
    return ordered, fused


def _row(index: embeddings.LoadedIndex, chunk_idx: int) -> int | None:
    try:
        return index.vector_rows.index(chunk_idx)
    except ValueError:
        return None


def _mk_chunk(index, i: int, fused, cosines, bm_scores) -> RetrievedChunk:
    ref = index.chunks[i]
    return RetrievedChunk(
        doc_id=ref.doc_id, chunk_id=ref.chunk_id, text=ref.text,
        doc_type=ref.doc_type, title=ref.title,
        score=float(fused.get(i, 0.0)),
        # "no vector" and "a vector that scored zero" are different facts and
        # the pruner must not confuse them: the first means the document is
        # un-embedded or stale and can only be judged lexically.
        has_vector=bool(cosines) and i in cosines,
        stage_scores={
            "vector": round(float(cosines.get(i, 0.0)), 5) if cosines else 0.0,
            "lexical": round(float(bm_scores.get(i, 0.0)), 5),
            "fused": round(float(fused.get(i, 0.0)), 6),
        })


def _prune_weak(candidates: list[RetrievedChunk], lexical_keep: set | None = None,
                order: list | None = None) -> list[RetrievedChunk]:
    """Drop candidates that only made the list because the list had room.

    FINAL_TOP_K is a ceiling, not a quota. The previous path always emitted its
    full K, so a question with one good answer arrived at the model wrapped in
    two unrelated passages — measured at Precision@3 0.55, i.e. nearly half the
    supplied evidence was irrelevant, on a prompt that is re-sent every turn.
    Irrelevant context is not free: it costs tokens and it gives the model
    something wrong to be confidently grounded in.
    """
    if not candidates:
        return []
    # Relative floor is computed against the best VECTOR-scored candidate for
    # this query, so it scales with how well the corpus covers the question
    # instead of assuming a fixed similarity is meaningful across queries.
    scored = [c.stage_scores.get("vector", 0.0) for c in candidates if c.has_vector]
    floor = config.ABSTAIN_MIN_SCORE
    if scored and config.RELATIVE_FLOOR > 0:
        floor = max(floor, max(scored) * config.RELATIVE_FLOOR)
    kept = []
    for c in candidates:
        if c.has_vector:
            if c.stage_scores.get("vector", 0.0) >= floor:
                c.kept_by = "vector"
                kept.append(c)
        elif c.stage_scores.get("lexical", 0.0) > 0:
            # No vector to judge it by — an un-embedded or stale document.
            # Holding it to a cosine floor it cannot have would silently make
            # every such document unreachable, which is the exact failure the
            # index metadata was added to surface.
            c.kept_by = "lexical"
            kept.append(c)
    if kept:
        return kept

    # LEXICAL RESCUE — a FALLBACK, not an addition.
    #
    # Embeddings systematically under-score short exact-term questions: "what
    # are the six stages?" scores 0.222 against the chunk that literally
    # contains "Six Stage Implementation Approach", below the absolute floor,
    # while BM25 ranks it first. Pruning on cosine alone discarded it and the
    # agent abstained on a question its own document answers in a heading.
    #
    # Adding these hits alongside the vector results instead of only when there
    # are none was measurably worse — Precision@3 0.53 -> 0.46 and cross-doc
    # contamination 0.05 -> 0.17 on the golden set, because a shared common word
    # is enough to drag another tenant's document in. As a fallback it costs
    # nothing when the vector arm is working and rescues the case where it is
    # blind.
    lexical_keep = lexical_keep or set()
    if lexical_keep and order:
        for pos, c in enumerate(candidates):
            if pos < len(order) and order[pos] in lexical_keep:
                c.kept_by = "lexical"
                kept.append(c)
    return kept


def _abstain_reason(chosen: list[RetrievedChunk], query: str, cosines) -> str:
    """The evidence gate. High precision by design — see config.py.

    Only signals that are nearly always right are allowed to refuse a caller:
    nothing retrieved, nothing above the similarity floor, or evidence with no
    lexical connection to the question whatsoever. Semantic absence ("the
    document is about DhronAI but never mentions employees") is NOT decidable
    here — measured, the score distributions of answerable and unanswerable
    questions overlap almost entirely — and is handled by the grounding
    contract at generation time instead.
    """
    if not chosen:
        return "no_candidates"
    if any(c.kept_by == "lexical" for c in chosen):
        # The lexical arm found an exact term match. Refusing on the cosine
        # floor here would discard the one signal that is RIGHT about
        # exact-term questions, which embeddings are worst at.
        return ""
    vector_scored = [c for c in chosen if c.has_vector]
    if vector_scored:
        best = max(c.stage_scores.get("vector", 0.0) for c in vector_scored)
        if best < config.ABSTAIN_MIN_SCORE:
            return "below_score_floor"
    if config.ABSTAIN_ON_ZERO_OVERLAP:
        terms = set(rerank.content_terms(query))
        if terms:
            evidence = set(rerank.tokenize(" ".join(c.text for c in chosen)))
            if not (terms & evidence):
                return "no_lexical_overlap"
    return ""


def _clip(text: str) -> str:
    limit = config.CONTEXT_MAX_CHARS
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


def _citations(chunks: list[RetrievedChunk]) -> list[dict]:
    """Structured citations — one per chunk actually placed in the context.

    Built from the supplied evidence rather than from anything the model says,
    which is what makes a fabricated citation impossible by construction.
    """
    if not config.CITATIONS_ENABLED:
        return []
    return [{
        "label": c.label,
        "document_id": c.doc_id,
        "chunk_id": c.chunk_id,
        "title": c.title or c.doc_id,
        "doc_type": c.doc_type,
        "relevance_score": round(c.score, 6),
        "vector_score": c.stage_scores.get("vector"),
        "lexical_score": c.stage_scores.get("lexical"),
    } for c in chunks]


# Abstention reasons decided BEFORE any embedding call. Caching them would
# store an entry to save work that was never done.
_FREE_ABSTENTIONS = frozenset({"not_a_question", "rag_disabled", "no_documents",
                               "error", "timeout"})


def _abstain(result: RetrievalResult, tr, reason: str,
             cache_key: tuple | None = None) -> RetrievalResult:
    result.abstained = tr.abstained = True
    result.abstain_reason = tr.abstain_reason = reason
    result.chunks = []
    result.citations = []
    # Tell the model there is no evidence rather than leaving it to guess from
    # silence — except for turns that were never questions, where a "no
    # evidence" banner would be noise on every "hmm".
    result.context_block = ("" if reason in ("not_a_question", "rag_disabled",
                                             "no_documents")
                            else grounding.NO_EVIDENCE_BLOCK)
    # "There is no evidence for this" is a retrieval result like any other, and
    # on real traffic it is the COMMON one — 49% of replayed caller turns that
    # reached retrieval. Not caching it meant every repeat of an unanswerable
    # question paid for another embedding call.
    if cache_key is not None and reason not in _FREE_ABSTENTIONS:
        ns, query, qvec = cache_key
        cache_mod.get_cache().store(ns, query, qvec, result,
                                    embed_tokens=result.embed_tokens)
    return _finish(result, tr)


def _from_cache(cached: RetrievalResult, result: RetrievalResult, tr,
                tier: str) -> RetrievalResult:
    result.chunks = cached.chunks
    result.candidates = cached.candidates
    result.context_block = cached.context_block
    result.citations = cached.citations
    result.abstained = cached.abstained
    result.abstain_reason = cached.abstain_reason
    result.corpus_version = cached.corpus_version
    result.index_problems = cached.index_problems
    result.cache_hit = tr.cache_hit = True
    result.cache_tier = tr.cache_tier = tier
    tr.abstained = cached.abstained
    tr.returned = len(cached.chunks)
    tr.candidates = len(cached.candidates)
    tr.top_sources = [f"{c.doc_id}#{c.chunk_id}" for c in cached.chunks]
    return _finish(result, tr)


def _finish(result: RetrievalResult, tr) -> RetrievalResult:
    rec = tr.finish()
    result.stage_ms = dict(tr.stage_ms)
    result.stage_ms["total"] = rec["total_ms"]
    return result


# ---------------------------------------------------------------------------
# Async entry point for the live call path
# ---------------------------------------------------------------------------

async def retrieve_async(agent_folder: str, query: str, *,
                         timeout_s: float | None = None, **kwargs) -> RetrievalResult:
    """Off-loop retrieval with a deadline.

    The previous code called the blocking embedding HTTP request directly from
    the async turn, so a slow provider stalled VAD, STT and TTS alongside it —
    a p95 of 622ms of dead event loop on every turn that reached RAG. Running
    it in a worker thread costs one context switch and gives the deadline
    somewhere to land: past it, the turn proceeds with no document context
    rather than holding the caller in silence.
    """
    import asyncio

    budget = timeout_s if timeout_s is not None else (config.EMBED_TIMEOUT_S + 1.0)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(retrieve, agent_folder, query, **kwargs), budget)
    except asyncio.TimeoutError:
        logger.warning("[RAG] retrieval exceeded %.1fs for %s — turn proceeds "
                       "without document context", budget, agent_folder)
        res = RetrievalResult(query=query, degraded=True,
                              degraded_reason="timeout", abstained=True,
                              abstain_reason="timeout")
        return res
    except Exception as exc:
        logger.warning("[RAG] retrieval error for %s: %r", agent_folder, exc)
        return RetrievalResult(query=query, degraded=True,
                               degraded_reason=repr(exc), abstained=True,
                               abstain_reason="error")
