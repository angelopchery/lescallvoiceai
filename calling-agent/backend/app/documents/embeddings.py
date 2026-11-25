"""Chunk embeddings + the vector index for in-call RAG.

WHAT CHANGED AND WHY (2026-08-12)
---------------------------------
The previous version embedded with a hardcoded model, wrote a bare `.npy` next
to the chunks, and recorded nothing about how that file was produced. Three
consequences, all of them silent:

  * Changing `_EMBED_MODEL` (or the provider changing its dimensions) made
    every stored vector meaningless. The query-time cosine then either raised
    a shape error or produced garbage scores; both were swallowed by a
    `except Exception: logger.debug(...)` and the agent simply stopped citing
    its own documents. No log line a human would notice, no error, no answer.
  * A failed embed at upload (no key, API down) left the document searchable
    only by raw word overlap — permanently, with nothing to retry it.
  * The query embedding was a blocking HTTP call made from inside the async
    voice turn, so a slow provider stalled the whole event loop, not just RAG.

Now: every index records the provider, model, dimension and a content hash of
the chunks it was built from (`index_meta.json`). At query time the recorded
identity is checked against the live configuration, and a mismatch is a loud,
named condition (`IndexMismatch`) that the caller reports and an operator can
fix with a re-index — never a silent empty result.

The embedding provider is injected rather than imported, so ingestion,
retrieval, the offline evaluation and the tests all exercise the same code.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

from . import config

logger = logging.getLogger(__name__)

_AGENTS_DIR = Path(__file__).resolve().parents[3] / "agents"

INDEX_META_FILE = "index_meta.json"
EMBEDDINGS_FILE = "embeddings.npy"
CHUNKS_FILE = "chunks.json"
# Why the last embed of this document failed, if it did. Written next to the
# chunks so `index_status` can say "keyword search only: HTTP 429 …" instead of
# a bare `not_embedded` — the difference between an operator re-indexing and
# an operator filing a bug. Removed by the next successful embed.
EMBED_ERROR_FILE = "embed_error.json"

# Bumped when the on-disk index layout or the normalisation contract changes in
# a way that makes existing files unreadable-as-intended. Part of the identity
# an index is checked against, so a format change forces a re-index instead of
# being reinterpreted.
INDEX_FORMAT_VERSION = 2


class IndexMismatch(RuntimeError):
    """A stored index was built with a different embedding identity."""


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class EmbeddingProvider(Protocol):
    provider: str
    model: str
    dim: int | None

    def embed(self, texts: list[str]) -> np.ndarray: ...


@dataclass
class EmbeddingResult:
    vectors: np.ndarray
    tokens: int = 0          # provider-reported; 0 means "not reported"


class OpenAIEmbeddingProvider:
    """The production provider. One client, reused — a new client per call
    re-does TLS on every turn, which is pure latency on the voice path."""

    def __init__(self, model: str | None = None, dim: int | None = None,
                 api_key: str | None = None, base_url: str | None = None,
                 timeout: float | None = None):
        self.provider = "openai"
        self.model = model or config.EMBED_MODEL
        self.dim = dim or config.EMBED_DIM
        self._api_key = api_key
        self._base_url = base_url or config.EMBED_BASE_URL
        self._timeout = timeout if timeout is not None else config.EMBED_TIMEOUT_S
        self._client = None
        self.last_tokens = 0

    def _key(self) -> str:
        return self._api_key or os.environ.get(config.EMBED_API_KEY_ENV, "")

    def available(self) -> bool:
        return bool(self._key())

    def _get_client(self):
        if self._client is None:
            import openai
            kwargs = {"api_key": self._key(), "timeout": self._timeout,
                      "max_retries": config.EMBED_MAX_RETRIES}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = openai.OpenAI(**kwargs)
        return self._client

    def reset(self) -> None:
        """Drop the client so the next call gets a fresh connection.

        Mirrors the STT layer's behaviour after a timeout: a hung connection
        that is reused stays hung.
        """
        self._client = None

    def embed(self, texts: list[str], timeout: float | None = None) -> np.ndarray:
        if not self.available():
            raise RuntimeError(
                f"no embedding credentials ({config.EMBED_API_KEY_ENV} unset)")
        try:
            # Ingestion gets the long deadline; `embed_queries` passes the
            # short one. The client default used to be the 2 s query cap for
            # both, which timed out any document over a few dozen chunks.
            resp = self._get_client().embeddings.create(
                model=self.model, input=texts,
                timeout=timeout if timeout is not None else config.EMBED_INGEST_TIMEOUT_S)
        except Exception:
            self.reset()
            raise
        usage = getattr(resp, "usage", None)
        # Provider-reported only. An estimated token count in a cost ledger is
        # a number that looks authoritative and isn't (same rule as
        # credit_tracker.record_llm).
        self.last_tokens = int(getattr(usage, "prompt_tokens", 0) or
                               getattr(usage, "total_tokens", 0) or 0)
        return np.asarray([e.embedding for e in resp.data], dtype=np.float32)

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        return self.embed(texts, timeout=self._timeout)


def _error_excerpt(resp) -> str:
    """The provider's own sentence from an error body, or the first bytes."""
    try:
        err = (resp.json() or {}).get("error") or {}
        msg = err.get("message") or ""
        if msg:
            return str(msg).strip()[:300]
    except Exception:
        pass
    return (getattr(resp, "text", "") or "")[:300]


class GeminiEmbeddingProvider:
    """Google's native embedContent API — the production provider since
    2026-09-25 (`gemini-embedding-2`, RUNBOOK §57).

    Native REST rather than the OpenAI-compat `/embeddings` route the
    intelligence module uses, for three things that route does not carry:

      * usage — the compat response has `usage: null`, so the ledger would
        record every request as 0 tokens. `usageMetadata.promptTokenCount` is
        the provider's own number, the only kind the ledger accepts;
      * task type — documents are embedded as RETRIEVAL_DOCUMENT and the
        caller's question as RETRIEVAL_QUERY. The model is trained on that
        asymmetry and the compat route cannot express it;
      * ordering — the compat route returns `index: null` on every row and
        the caller has to trust response order.

    One httpx client, reused: TLS once, not once per voice turn. A failed or
    timed-out request drops it, same as the STT layer — a hung connection that
    is reused stays hung.
    """

    def __init__(self, model: str | None = None, dim: int | None = None,
                 api_key: str | None = None, base_url: str | None = None,
                 timeout: float | None = None, ingest_timeout: float | None = None,
                 batch_size: int | None = None):
        self.provider = "gemini"
        self.model = model or config.EMBED_MODEL
        self.dim = int(dim or config.EMBED_DIM)
        self._api_key = api_key
        self._base_url = (base_url or config.EMBED_BASE_URL
                          or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self._timeout = timeout if timeout is not None else config.EMBED_TIMEOUT_S
        self._ingest_timeout = (ingest_timeout if ingest_timeout is not None
                                else config.EMBED_INGEST_TIMEOUT_S)
        self._batch = max(1, min(int(batch_size or config.EMBED_BATCH_SIZE), 100))
        self._client = None
        self.last_tokens = 0

    def _key(self) -> str:
        if self._api_key:
            return self._api_key
        # The configured variable first; then the two names the OCR step
        # already reads, so one funded Gemini key serves every document stage.
        for name in (config.EMBED_API_KEY_ENV, "GEMINI_API_KEY", "LLM_API_KEY"):
            value = os.environ.get(name, "").strip()
            if value:
                return value
        return ""

    def available(self) -> bool:
        return bool(self._key())

    def _get_client(self):
        if self._client is None:
            import httpx
            self._client = httpx.Client(timeout=self._ingest_timeout)
        return self._client

    def reset(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def embed(self, texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT",
              timeout: float | None = None) -> np.ndarray:
        if not self.available():
            raise RuntimeError(
                f"no embedding credentials ({config.EMBED_API_KEY_ENV} unset)")
        if not texts:
            self.last_tokens = 0
            return np.zeros((0, self.dim), dtype=np.float32)
        url = f"{self._base_url}/models/{self.model}:batchEmbedContents"
        rows: list[list[float]] = []
        tokens = 0
        for start in range(0, len(texts), self._batch):
            batch = texts[start:start + self._batch]
            payload = {"requests": [{
                "model": f"models/{self.model}",
                # An empty part is a 400; a chunk is never empty in practice
                # but a caller turn of pure punctuation can be.
                "content": {"parts": [{"text": t if t.strip() else " "}]},
                "taskType": task_type,
                "outputDimensionality": self.dim,
            } for t in batch]}
            try:
                resp = self._get_client().post(
                    url, json=payload, headers={"x-goog-api-key": self._key()},
                    timeout=timeout if timeout is not None else self._ingest_timeout)
            except Exception:
                self.reset()
                raise
            if resp.status_code != 200:
                self.reset()
                raise RuntimeError(
                    f"gemini embeddings HTTP {resp.status_code}: {_error_excerpt(resp)}")
            data = resp.json() or {}
            embs = data.get("embeddings") or []
            if len(embs) != len(batch):
                raise RuntimeError(
                    f"gemini embeddings returned {len(embs)} vectors for {len(batch)} texts")
            rows.extend(list(e.get("values") or []) for e in embs)
            usage = data.get("usageMetadata") or {}
            tokens += int(usage.get("promptTokenCount") or usage.get("totalTokenCount") or 0)
        self.last_tokens = tokens
        return np.asarray(rows, dtype=np.float32)

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        return self.embed(texts, task_type="RETRIEVAL_QUERY", timeout=self._timeout)


_provider: EmbeddingProvider | None = None


def build_provider(model: str | None = None, dim: int | None = None,
                   provider_name: str | None = None) -> EmbeddingProvider:
    """The provider the configuration names — or the one the MODEL implies,
    so `--embed-model text-embedding-3-small` still builds an OpenAI client
    while the platform default is Gemini."""
    name = (provider_name or "").strip().lower()
    if not name and model:
        if model.startswith("text-embedding"):
            name = "openai"
        elif model.startswith("gemini"):
            name = "gemini"
    name = name or config.EMBED_PROVIDER
    if name == "gemini":
        return GeminiEmbeddingProvider(model=model, dim=dim)
    return OpenAIEmbeddingProvider(model=model, dim=dim)


def get_provider() -> EmbeddingProvider | None:
    global _provider
    if _provider is None:
        _provider = build_provider()
    return _provider


def set_provider(provider: EmbeddingProvider | None) -> None:
    """Dependency injection point for ingestion, evaluation and tests."""
    global _provider
    _provider = provider


def provider_available(provider: EmbeddingProvider | None = None) -> bool:
    p = provider or get_provider()
    check = getattr(p, "available", None)
    return bool(check()) if callable(check) else p is not None


# ---------------------------------------------------------------------------
# Index identity
# ---------------------------------------------------------------------------

@dataclass
class IndexMeta:
    provider: str
    model: str
    dim: int
    chunk_count: int
    chunks_sha: str
    normalized: bool = True
    format_version: int = INDEX_FORMAT_VERSION
    built_at: str = ""
    tokens: int = 0
    # The chunk geometry the text was split with. Recorded because retuning
    # RAG_CHUNK_WORDS does NOT rewrite existing chunks.json files: without this
    # an agent quietly serves a mix of old- and new-geometry documents forever,
    # and the evaluation that justified the new geometry describes nothing that
    # is actually running.
    chunk_words: int = 0
    chunk_overlap: int = 0

    def to_json(self) -> dict:
        return {
            "provider": self.provider, "model": self.model, "dim": self.dim,
            "chunk_count": self.chunk_count, "chunks_sha": self.chunks_sha,
            "normalized": self.normalized, "format_version": self.format_version,
            "built_at": self.built_at, "tokens": self.tokens,
            "chunk_words": self.chunk_words, "chunk_overlap": self.chunk_overlap,
        }

    @classmethod
    def from_json(cls, raw: dict) -> "IndexMeta":
        return cls(
            provider=raw.get("provider", "unknown"),
            model=raw.get("model", "unknown"),
            dim=int(raw.get("dim") or 0),
            chunk_count=int(raw.get("chunk_count") or 0),
            chunks_sha=raw.get("chunks_sha", ""),
            normalized=bool(raw.get("normalized", False)),
            format_version=int(raw.get("format_version") or 1),
            built_at=raw.get("built_at", ""),
            tokens=int(raw.get("tokens") or 0),
            chunk_words=int(raw.get("chunk_words") or 0),
            chunk_overlap=int(raw.get("chunk_overlap") or 0),
        )

    def compatible_with(self, provider: EmbeddingProvider) -> tuple[bool, str]:
        """Can a query embedded by `provider` be compared with this index?"""
        if self.format_version != INDEX_FORMAT_VERSION:
            return False, (f"index format v{self.format_version}, "
                           f"runtime expects v{INDEX_FORMAT_VERSION}")
        if self.model != getattr(provider, "model", ""):
            return False, (f"index built with {self.model!r}, "
                           f"runtime queries with {getattr(provider, 'model', '?')!r}")
        pdim = getattr(provider, "dim", None)
        if pdim and self.dim and int(pdim) != int(self.dim):
            return False, f"index dim {self.dim}, runtime dim {pdim}"
        return True, ""


def chunks_sha(chunks: list[dict]) -> str:
    """Content hash of a document's chunks — the re-index / cache trigger."""
    h = hashlib.sha256()
    for c in chunks:
        h.update(str(c.get("chunk_id", "")).encode())
        h.update(b"\x00")
        h.update((c.get("text") or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def _docs_dir(agent_folder: str) -> Path:
    return _AGENTS_DIR / agent_folder / "documents"


def _normalize_rows(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32)


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

def embed_document(agent_folder: str, doc_id: str,
                   provider: EmbeddingProvider | None = None) -> bool:
    """Embed one document's chunks and record how it was built.

    Returns False (and logs) rather than raising, so a failed embed never
    fails the upload — but it now leaves a readable reason behind, and
    `index_status()` reports the document as un-embedded so it can be retried.
    """
    provider = provider or get_provider()
    doc_dir = _docs_dir(agent_folder) / doc_id
    chunks_path = doc_dir / CHUNKS_FILE
    if not chunks_path.exists():
        logger.warning("[docs/emb] no chunks for %s/%s", agent_folder, doc_id)
        return False
    try:
        chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[docs/emb] unreadable chunks for %s/%s: %s",
                       agent_folder, doc_id, exc)
        return False
    if not chunks:
        return True

    texts = [c["text"] for c in chunks]
    try:
        vectors = _embed_batch(texts, provider)
    except Exception as exc:
        logger.warning("[docs/emb] embedding failed for %s/%s: %s — document "
                       "stays retrievable by lexical search only, re-index to fix",
                       agent_folder, doc_id, exc)
        _write_embed_error(doc_dir, provider, f"{exc.__class__.__name__}: {exc}")
        return False
    if vectors is None or not len(vectors):
        _write_embed_error(doc_dir, provider, "provider returned no vectors")
        return False

    vectors = _normalize_rows(np.asarray(vectors, dtype=np.float32))
    np.save(str(doc_dir / EMBEDDINGS_FILE), vectors)
    try:
        (doc_dir / EMBED_ERROR_FILE).unlink(missing_ok=True)
    except Exception:
        pass
    meta = IndexMeta(
        provider=getattr(provider, "provider", "unknown"),
        model=getattr(provider, "model", "unknown"),
        dim=int(vectors.shape[1]),
        chunk_count=len(chunks),
        chunks_sha=chunks_sha(chunks),
        normalized=True,
        built_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        tokens=int(getattr(provider, "last_tokens", 0) or 0),
        chunk_words=_store_geometry()[0],
        chunk_overlap=_store_geometry()[1],
    )
    (doc_dir / INDEX_META_FILE).write_text(
        json.dumps(meta.to_json(), indent=2), encoding="utf-8")
    logger.info("[docs/emb] embedded %d chunks for %s/%s (%s, dim=%d, %d tokens)",
                len(chunks), agent_folder, doc_id, meta.model, meta.dim, meta.tokens)
    _record_embedding_cost(meta.tokens, meta.model, "ingest")
    return True


def _embed_batch(texts: list[str],
                 provider: EmbeddingProvider | None = None) -> np.ndarray | None:
    provider = provider or get_provider()
    if provider is None:
        return None
    return provider.embed(texts)


def _write_embed_error(doc_dir: Path, provider, reason: str) -> None:
    try:
        (doc_dir / EMBED_ERROR_FILE).write_text(json.dumps({
            "error": reason[:500],
            "provider": getattr(provider, "provider", None),
            "model": getattr(provider, "model", None),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, indent=2), encoding="utf-8")
    except Exception:  # the reason is a courtesy, never a second failure
        pass


def embed_error(agent_folder: str, doc_id: str) -> dict | None:
    """The recorded reason a document is not embedded, or None."""
    path = _docs_dir(agent_folder) / doc_id / EMBED_ERROR_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _store_geometry() -> tuple[int, int]:
    """Live chunk geometry, read from `store` so the two cannot disagree."""
    from . import store
    return int(store._CHUNK_WORDS), int(store._CHUNK_OVERLAP)


def reindex_agent(agent_folder: str,
                  provider: EmbeddingProvider | None = None,
                  rechunk: bool = True) -> dict:
    """Rebuild an agent's index. The deliberate migration step — nothing
    re-indexes itself implicitly, because re-embedding a tenant's whole corpus
    is a spend and a several-second operation, not a side effect of a config
    change.

    `rechunk=True` re-splits the stored text with the CURRENT chunk geometry
    first. Without it, retuning RAG_CHUNK_WORDS would apply only to documents
    uploaded afterwards.
    """
    from . import store
    results = {"reindexed": [], "rechunked": [], "failed": []}
    for doc in store.list_documents(agent_folder):
        doc_id = doc["doc_id"]
        if rechunk and store.rechunk_document(agent_folder, doc_id):
            results["rechunked"].append(doc_id)
        if embed_document(agent_folder, doc_id, provider):
            results["reindexed"].append(doc_id)
        else:
            results["failed"].append(doc_id)
    return results


# ---------------------------------------------------------------------------
# Index loading
# ---------------------------------------------------------------------------

@dataclass
class ChunkRef:
    doc_id: str
    chunk_id: int
    text: str
    doc_type: str = ""
    title: str = ""


@dataclass
class LoadedIndex:
    chunks: list[ChunkRef] = field(default_factory=list)
    matrix: np.ndarray | None = None            # rows align with `chunks`
    vector_rows: list[int] = field(default_factory=list)  # index into `chunks`
    problems: list[str] = field(default_factory=list)
    corpus_version: str = ""

    @property
    def has_vectors(self) -> bool:
        return self.matrix is not None and len(self.vector_rows) > 0


def load_index(agent_folder: str,
               provider: EmbeddingProvider | None = None) -> LoadedIndex:
    """Load every chunk of every document, plus whichever vectors are usable.

    Chunks whose vectors are missing or incompatible are still returned — they
    remain reachable through the lexical arm — but they are excluded from the
    vector matrix and the reason is recorded in `problems`. Degrading loudly
    and partially beats degrading silently and totally.
    """
    from . import store
    provider = provider or get_provider()
    idx = LoadedIndex()
    docs = store.list_documents(agent_folder)
    if not docs:
        return idx

    mats: list[np.ndarray] = []
    fingerprint = hashlib.sha256()
    for doc in sorted(docs, key=lambda d: d.get("doc_id", "")):
        doc_id = doc["doc_id"]
        doc_dir = _docs_dir(agent_folder) / doc_id
        try:
            chunks = json.loads((doc_dir / CHUNKS_FILE).read_text(encoding="utf-8"))
        except Exception as exc:
            idx.problems.append(f"{doc_id}: chunks unreadable ({exc})")
            continue

        first_row = len(idx.chunks)
        for c in chunks:
            idx.chunks.append(ChunkRef(
                doc_id=doc_id, chunk_id=int(c.get("chunk_id", 0)),
                text=c.get("text", ""), doc_type=doc.get("doc_type", ""),
                title=doc.get("filename", "") or doc.get("description", "")))

        sha = chunks_sha(chunks)
        fingerprint.update(f"{doc_id}:{sha}".encode())

        meta_path = doc_dir / INDEX_META_FILE
        emb_path = doc_dir / EMBEDDINGS_FILE
        if not emb_path.exists():
            idx.problems.append(f"{doc_id}: not embedded (lexical only)")
            continue

        if meta_path.exists():
            try:
                meta = IndexMeta.from_json(json.loads(meta_path.read_text(encoding="utf-8")))
            except Exception as exc:
                idx.problems.append(f"{doc_id}: index metadata unreadable ({exc})")
                continue
            ok, why = meta.compatible_with(provider) if provider else (True, "")
            if not ok:
                idx.problems.append(f"{doc_id}: {why} — re-index required")
                continue
            if meta.chunks_sha and meta.chunks_sha != sha:
                idx.problems.append(
                    f"{doc_id}: document changed since it was embedded — re-index required")
                continue
        else:
            # Pre-upgrade index: no recorded identity. Usable only if its shape
            # matches the configured dimension, and flagged either way — an
            # unlabelled index is exactly the failure this metadata exists to
            # prevent, so it is reported until someone re-indexes it.
            idx.problems.append(
                f"{doc_id}: legacy index with no recorded embedding identity "
                "— re-index to make it verifiable")

        try:
            mat = np.load(str(emb_path))
        except Exception as exc:
            idx.problems.append(f"{doc_id}: vectors unreadable ({exc})")
            continue
        if mat.ndim != 2 or mat.shape[0] != len(chunks):
            idx.problems.append(
                f"{doc_id}: {mat.shape[0] if mat.ndim == 2 else '?'} vectors for "
                f"{len(chunks)} chunks — re-index required")
            continue
        pdim = getattr(provider, "dim", None) if provider else None
        if pdim and mat.shape[1] != int(pdim):
            idx.problems.append(
                f"{doc_id}: vector dim {mat.shape[1]}, runtime dim {pdim} — "
                "re-index required")
            continue
        mats.append(_normalize_rows(mat.astype(np.float32)))
        idx.vector_rows.extend(range(first_row, first_row + len(chunks)))

    if mats:
        idx.matrix = np.vstack(mats)
    idx.corpus_version = fingerprint.hexdigest()[:16]
    if idx.problems:
        logger.warning("[docs/emb] %s index problems: %s",
                       agent_folder, "; ".join(idx.problems))
    return idx


def index_status(agent_folder: str,
                 provider: EmbeddingProvider | None = None) -> dict:
    """Per-document index health, for the Agent Builder API."""
    from . import store
    provider = provider or get_provider()
    out = []
    for doc in store.list_documents(agent_folder):
        doc_dir = _docs_dir(agent_folder) / doc["doc_id"]
        row = {"doc_id": doc["doc_id"], "filename": doc.get("filename", ""),
               "embedded": False, "state": "not_embedded", "detail": ""}
        meta_path = doc_dir / INDEX_META_FILE
        if (doc_dir / EMBEDDINGS_FILE).exists():
            row["embedded"] = True
            row["state"] = "ok"
        if meta_path.exists():
            try:
                meta = IndexMeta.from_json(json.loads(meta_path.read_text(encoding="utf-8")))
                row.update({"model": meta.model, "dim": meta.dim,
                            "built_at": meta.built_at, "chunks": meta.chunk_count,
                            "chunk_words": meta.chunk_words})
                ok, why = meta.compatible_with(provider) if provider else (True, "")
                live_words, _ = _store_geometry()
                if not ok:
                    row["state"], row["detail"] = "stale", why
                elif meta.chunk_words and meta.chunk_words != live_words:
                    row["state"] = "stale"
                    row["detail"] = (f"chunked at {meta.chunk_words} words, "
                                     f"runtime configured for {live_words}")
                else:
                    try:
                        chunks = json.loads(
                            (doc_dir / CHUNKS_FILE).read_text(encoding="utf-8"))
                        if meta.chunks_sha != chunks_sha(chunks):
                            row["state"] = "stale"
                            row["detail"] = "document changed since embedding"
                    except Exception:
                        pass
            except Exception as exc:
                row["state"], row["detail"] = "unreadable", str(exc)
        elif row["embedded"]:
            row["state"] = "unlabelled"
            row["detail"] = "built before index metadata existed"
        if row["state"] == "not_embedded":
            err = embed_error(agent_folder, doc["doc_id"])
            if err:
                row["detail"] = str(err.get("error") or "")
                row["failed_at"] = err.get("at")
        out.append(row)
    return {"documents": out,
            "runtime": {"provider": getattr(provider, "provider", None),
                        "model": getattr(provider, "model", None),
                        "dim": getattr(provider, "dim", None),
                        # False = every upload will be keyword-only, and the
                        # UI should say so before the user hits Re-index.
                        "available": provider_available(provider) if provider else False,
                        "key_env": config.EMBED_API_KEY_ENV},
            "needs_reindex": [r["doc_id"] for r in out
                              if r["state"] in ("stale", "not_embedded", "unlabelled")]}


# ---------------------------------------------------------------------------
# Query embedding + cost
# ---------------------------------------------------------------------------

def embed_query(text: str, provider: EmbeddingProvider | None = None
                ) -> tuple[np.ndarray, int]:
    """Embed one query. Returns (unit vector, provider-reported tokens).

    A provider that distinguishes queries from documents (task type, the
    short live-turn deadline) exposes `embed_queries`; the fake providers in
    the tests and the eval harness only have `embed`, and both are fine.
    """
    provider = provider or get_provider()
    fn = getattr(provider, "embed_queries", None)
    vecs = fn([text]) if callable(fn) else provider.embed([text])
    tokens = int(getattr(provider, "last_tokens", 0) or 0)
    return _normalize_rows(np.asarray(vecs, dtype=np.float32))[0], tokens


def _record_embedding_cost(tokens: int, model: str, stage: str) -> None:
    """Push embedding tokens into the existing AI ledger. Never raises."""
    if not (config.COST_TRACKING_ENABLED and tokens):
        return
    try:
        from app.voice.credit_tracker import SarvamCreditTracker
        SarvamCreditTracker.get().record_embedding_sync(
            tokens=tokens, model=model, stage=stage)
    except Exception:  # ledger is best-effort, never on the critical path
        pass


# ---------------------------------------------------------------------------
# Backwards-compatible entry point
# ---------------------------------------------------------------------------

def retrieve_context(agent_folder: str, query: str, top_k: int | None = None) -> str:
    """The pre-existing call signature: a prompt-ready context block, or "".

    Kept so nothing that imports it has to change. New callers should use
    `retrieval.retrieve()`, which returns scores, citations and the abstention
    decision instead of only the text.
    """
    from . import retrieval
    result = retrieval.retrieve(agent_folder, query, final_top_k=top_k)
    return result.context_block
