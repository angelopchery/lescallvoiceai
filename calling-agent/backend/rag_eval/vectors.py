"""Frozen embedding cache — deterministic, free, offline evaluation runs.

Every embedding the evaluation ever requests is memoised to an `.npz` keyed by
sha256(model | text). Two consequences, both wanted:

  * CI can run the full retrieval evaluation with no API key, no network and no
    spend, and get bit-identical numbers on every run. A retrieval regression
    then means someone changed retrieval — not that the embedding endpoint had
    a bad afternoon.
  * A LIVE run (`--live`) still measures the real thing, including real API
    latency, and folds anything new into the cache for the next offline run.

The cache is NOT a substitute for live measurement: it cannot detect provider
drift, which is precisely why `--live` exists and why the embedding index
records the model and dimension it was built with (see documents/embeddings.py).

The stored text is never written to disk — only its hash — so a corpus with
private content leaves no plaintext in a committed artifact.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np


def key_for(model: str, text: str) -> str:
    return hashlib.sha256(f"{model}\x00{text}".encode("utf-8")).hexdigest()


class EmbeddingCache:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._vecs: dict[str, np.ndarray] = {}
        self._dirty = False
        self.hits = 0
        self.misses = 0
        if self.path.exists():
            with np.load(self.path) as data:
                self._vecs = {k: data[k] for k in data.files}

    def __len__(self) -> int:
        return len(self._vecs)

    def get(self, model: str, text: str) -> np.ndarray | None:
        vec = self._vecs.get(key_for(model, text))
        if vec is None:
            self.misses += 1
        else:
            self.hits += 1
        return vec

    def put(self, model: str, text: str, vec: np.ndarray) -> None:
        self._vecs[key_for(model, text)] = np.asarray(vec, dtype=np.float32)
        self._dirty = True

    def save(self) -> bool:
        if not self._dirty:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(self.path, **self._vecs)
        self._dirty = False
        return True


class CachedEmbedder:
    """Wraps a live embedding callable with the frozen cache.

    `live=False` turns a cache miss into a loud error instead of a silent
    network call — that is what makes an offline run trustworthy: it cannot
    quietly become a live run because someone added a golden question.
    """

    def __init__(self, model: str, cache: EmbeddingCache,
                 live_fn=None, live: bool = False, bypass: bool = False):
        self.model = model
        self.cache = cache
        self._live_fn = live_fn
        self.live = live
        # `bypass` is what makes a latency measurement honest: with the cache
        # warm, a --live re-run would serve every vector from memory and report
        # a p95 of 4ms that no caller will ever experience.
        self.bypass = bypass
        self.api_calls = 0
        self.api_texts = 0

    def embed(self, texts: list[str]) -> np.ndarray:
        out: list[np.ndarray | None] = (
            [None] * len(texts) if self.bypass
            else [self.cache.get(self.model, t) for t in texts])
        missing = [i for i, v in enumerate(out) if v is None]
        if missing:
            if not self.live:
                sample = texts[missing[0]][:80].replace("\n", " ")
                raise KeyError(
                    f"{len(missing)} embedding(s) not in the frozen cache "
                    f"{self.cache.path.name} (first: {sample!r}). "
                    "Re-run once with --live to extend the cache, then commit it.")
            if self._live_fn is None:
                raise RuntimeError("live embedding requested but no provider configured")
            fresh = self._live_fn([texts[i] for i in missing])
            self.api_calls += 1
            self.api_texts += len(missing)
            for i, vec in zip(missing, fresh):
                arr = np.asarray(vec, dtype=np.float32)
                self.cache.put(self.model, texts[i], arr)
                out[i] = arr
        return np.stack([v for v in out if v is not None]).astype(np.float32)
