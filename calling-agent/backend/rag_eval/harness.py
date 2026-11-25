"""Corpus ingestion + retriever adapters for the evaluation.

The harness ingests the evaluation corpus through the REAL production
ingestion path (`documents.store.save_document` for chunking,
`documents.embeddings.embed_document` for vectors), into a throwaway agent
folder. Evaluating a reimplementation of ingestion would measure the
reimplementation; this measures what ships.

Two retrievers are exposed behind one interface so before/after is a like-for-
like comparison on identical inputs:

    LegacyRetriever   — the algorithm as it stood before this work (frozen copy,
                        see legacy_baseline.py)
    CurrentRetriever  — whatever documents/retrieval.py does today
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .metrics import RetrievedChunk


@dataclass
class RetrievalOutcome:
    """One question's trip through a retriever, normalised for scoring."""
    question: str
    hits: list[RetrievedChunk] = field(default_factory=list)      # final, post-rerank
    candidates: list[RetrievedChunk] = field(default_factory=list)  # pre-rerank
    context: str = ""
    citations: list[dict] = field(default_factory=list)
    abstained: bool = False
    abstain_reason: str = ""
    degraded: bool = False
    cache_hit: bool = False
    stage_ms: dict[str, float] = field(default_factory=dict)
    total_ms: float = 0.0
    embed_tokens: int = 0
    embed_cost_inr: float | None = None


# ---------------------------------------------------------------------------
# Corpus ingestion
# ---------------------------------------------------------------------------

def build_index(workdir: Path, corpus_dir: Path, embedder,
                agent_folder: str = "rag_eval_agent",
                chunk_words: int | None = None) -> dict[str, str]:
    """Ingest every corpus/*.txt as a document. Returns {doc name: doc_id}.

    `embedder` is anything with `.embed(list[str]) -> np.ndarray`; it is
    injected into the production embedding module for the duration of the
    ingest so the frozen vector cache is used instead of a live API call.
    """
    from app.documents import embeddings as emb_mod
    from app.documents import store as store_mod

    agents_dir = workdir / "agents"
    if agents_dir.exists():
        shutil.rmtree(agents_dir)
    (agents_dir / agent_folder).mkdir(parents=True)

    prev_store, prev_emb = store_mod._AGENTS_DIR, emb_mod._AGENTS_DIR
    store_mod._AGENTS_DIR = agents_dir
    emb_mod._AGENTS_DIR = agents_dir
    prev_chunk = getattr(store_mod, "_CHUNK_WORDS", None)
    if chunk_words:
        store_mod._CHUNK_WORDS = chunk_words
    try:
        with _embedder_installed(emb_mod, embedder):
            ids: dict[str, str] = {}
            for path in sorted(corpus_dir.glob("*.txt")):
                text = path.read_text(encoding="utf-8")
                meta = store_mod.save_document(
                    agent_folder=agent_folder,
                    filename=f"{path.stem}.txt",
                    content=text.encode("utf-8"),
                    extracted_text=text,
                    org_name="rag-eval",
                    doc_type="Knowledge",
                    description=f"eval corpus: {path.stem}",
                )
                ok = emb_mod.embed_document(agent_folder, meta["doc_id"])
                if not ok:
                    raise RuntimeError(f"embedding failed for {path.stem}")
                ids[path.stem] = meta["doc_id"]
    finally:
        store_mod._AGENTS_DIR, emb_mod._AGENTS_DIR = prev_store, prev_emb
        if chunk_words and prev_chunk is not None:
            store_mod._CHUNK_WORDS = prev_chunk
    return ids


class _embedder_installed:
    """Inject the eval embedder into the documents module, either shape.

    The pre-upgrade module embeds through a private `_embed_batch`; the
    upgraded one takes an injected provider. Supporting both is what lets the
    same harness measure before and after.
    """

    def __init__(self, emb_mod, embedder):
        self.mod, self.embedder = emb_mod, embedder
        self._saved = {}

    def __enter__(self):
        # An evaluation is not a call. Its embedding spend is real but belongs
        # in the eval report, not in the per-call AI ledger that prices what
        # customers were served.
        try:
            from app.documents import config as _dcfg
            self._saved["cost_tracking"] = _dcfg.COST_TRACKING_ENABLED
            _dcfg.COST_TRACKING_ENABLED = False
        except Exception:
            pass
        if hasattr(self.mod, "set_provider"):          # upgraded shape
            self._saved["provider"] = self.mod.get_provider()
            self.mod.set_provider(_ProviderShim(self.embedder))
        else:                                           # legacy shape
            self._saved["_embed_batch"] = self.mod._embed_batch
            self.mod._embed_batch = lambda texts: self.embedder.embed(texts)
        return self

    def __exit__(self, *exc):
        if "cost_tracking" in self._saved:
            from app.documents import config as _dcfg
            _dcfg.COST_TRACKING_ENABLED = self._saved["cost_tracking"]
        if "provider" in self._saved:
            self.mod.set_provider(self._saved["provider"])
        else:
            self.mod._embed_batch = self._saved["_embed_batch"]
        return False


class _ProviderShim:
    """Adapts the eval embedder to the production EmbeddingProvider surface."""

    def __init__(self, embedder):
        self._e = embedder
        self.model = getattr(embedder, "model", "eval-cached")
        self.provider = "eval-cache"
        self.dim = None
        # Provider-reported tokens, passed through so a --fresh/--live run
        # reports real embedding cost. A cache hit reports 0, which is the
        # honest figure: no request was made, so no tokens were spent.
        self.last_tokens = 0

    def available(self) -> bool:
        return True

    def embed(self, texts: list[str]) -> np.ndarray:
        vecs = self._e.embed(texts)
        self.last_tokens = int(getattr(getattr(self._e, "_live_fn", None),
                                       "last_tokens", 0) or 0)
        if self.dim is None and len(vecs):
            self.dim = int(vecs.shape[1])
        return vecs


# ---------------------------------------------------------------------------
# Retrievers
# ---------------------------------------------------------------------------

class LegacyRetriever:
    """Pre-upgrade behaviour: cosine top-3 over per-document .npy, floor 0.25."""

    name = "legacy"

    def __init__(self, agents_dir: Path, agent_folder: str, embedder,
                 id_to_doc: dict[str, str]):
        self.agents_dir, self.agent_folder = agents_dir, agent_folder
        self.embedder, self.id_to_doc = embedder, id_to_doc

    def run(self, question: str) -> RetrievalOutcome:
        from .legacy_baseline import legacy_retrieve
        t0 = time.perf_counter()
        out = legacy_retrieve(self.agents_dir, self.agent_folder, question,
                              self.embedder, self.id_to_doc)
        out.total_ms = (time.perf_counter() - t0) * 1000
        return out


class CurrentRetriever:
    """The shipped pipeline: documents/retrieval.py, exercised end to end."""

    name = "current"

    def __init__(self, agents_dir: Path, agent_folder: str, embedder,
                 id_to_doc: dict[str, str], **overrides):
        self.agents_dir, self.agent_folder = agents_dir, agent_folder
        self.embedder, self.id_to_doc = embedder, id_to_doc
        self.overrides = overrides

    def run(self, question: str) -> RetrievalOutcome:
        from app.documents import embeddings as emb_mod
        from app.documents import retrieval, store as store_mod

        prev_store, prev_emb = store_mod._AGENTS_DIR, emb_mod._AGENTS_DIR
        store_mod._AGENTS_DIR = self.agents_dir
        emb_mod._AGENTS_DIR = self.agents_dir
        try:
            with _embedder_installed(emb_mod, self.embedder):
                t0 = time.perf_counter()
                result = retrieval.retrieve(self.agent_folder, question,
                                            **self.overrides)
                total = (time.perf_counter() - t0) * 1000
        finally:
            store_mod._AGENTS_DIR, emb_mod._AGENTS_DIR = prev_store, prev_emb

        to_name = {v: k for k, v in self.id_to_doc.items()}
        def _chunks(items):
            return [RetrievedChunk(doc=to_name.get(c.doc_id, c.doc_id),
                                   chunk_id=c.chunk_id, text=c.text,
                                   score=c.score, rank=i,
                                   stage_scores=c.stage_scores)
                    for i, c in enumerate(items, 1)]

        return RetrievalOutcome(
            question=question,
            hits=_chunks(result.chunks),
            candidates=_chunks(result.candidates),
            context=result.context_block,
            citations=[dict(c, document_id=to_name.get(c["document_id"],
                                                       c["document_id"]))
                       for c in result.citations],
            abstained=result.abstained,
            abstain_reason=result.abstain_reason,
            degraded=result.degraded,
            cache_hit=result.cache_hit,
            stage_ms=dict(result.stage_ms),
            total_ms=total,
            embed_tokens=result.embed_tokens,
            embed_cost_inr=result.embed_cost_inr,
        )
