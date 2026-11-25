"""Semantic cache: isolation, invalidation, expiry, and honest accounting.

The isolation tests are the important ones. A retrieval cache holds tenant
document content, and the classic way one leaks is a global similarity search
with a tenant filter applied afterwards — one refactor away from serving
another business's brochure to a caller.
"""
from __future__ import annotations

import numpy as np
import pytest

from conftest import BACKEND_DIR  # noqa: F401  (path bootstrap)


@pytest.fixture()
def cache():
    from app.documents import cache as cache_mod
    cache_mod.reset_cache()
    c = cache_mod.SemanticCache(ttl_s=900, max_entries=4, threshold=0.95)
    yield c
    cache_mod.reset_cache()


def _vec(*vals) -> np.ndarray:
    v = np.array(vals, dtype=np.float32)
    return v / np.linalg.norm(v)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

def test_one_agent_never_sees_another_agents_entry(cache):
    from app.documents import cache as cache_mod
    ns_a = cache_mod.namespace("acme", "corpus1", "cfg1")
    ns_b = cache_mod.namespace("globex", "corpus2", "cfg1")
    cache.store(ns_a, "what is the price", _vec(1, 0, 0), "ACME EVIDENCE")

    assert cache.lookup_exact(ns_b, "what is the price") is None
    assert cache.lookup_semantic(ns_b, _vec(1, 0, 0)) is None
    assert cache.lookup_exact(ns_a, "what is the price").payload == "ACME EVIDENCE"


def test_identical_vectors_do_not_cross_namespaces(cache):
    """A byte-identical query from two tenants stays two entries.

    Similarity is only ever computed within one namespace's bucket, so a
    perfect vector match from another tenant cannot surface — this is the
    property that makes the cache safe by construction rather than by filter.
    """
    from app.documents import cache as cache_mod
    ns_a = cache_mod.namespace("acme", "c1", "cfg")
    ns_b = cache_mod.namespace("globex", "c1", "cfg")
    v = _vec(0.3, 0.9, 0.1)
    cache.store(ns_a, "do you deliver", v, "ACME")
    cache.store(ns_b, "do you deliver", v, "GLOBEX")

    assert cache.lookup_semantic(ns_a, v).payload == "ACME"
    assert cache.lookup_semantic(ns_b, v).payload == "GLOBEX"


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------

def test_changed_corpus_makes_old_answers_unreachable(cache):
    """Uploading or editing a document changes the corpus version, and the
    namespace is built from it — so stale evidence cannot be served even
    before anything is explicitly evicted."""
    from app.documents import cache as cache_mod
    before = cache_mod.namespace("acme", "corpus-v1", "cfg")
    after = cache_mod.namespace("acme", "corpus-v2", "cfg")
    cache.store(before, "what is the price", _vec(1, 0, 0), "OLD EVIDENCE")

    assert cache.lookup_exact(after, "what is the price") is None
    assert cache.lookup_semantic(after, _vec(1, 0, 0)) is None


def test_config_change_makes_old_answers_unreachable(cache):
    from app.documents import cache as cache_mod
    a = cache_mod.namespace("acme", "c1", cache_mod.config_fingerprint(final_k=3))
    b = cache_mod.namespace("acme", "c1", cache_mod.config_fingerprint(final_k=5))
    assert a != b
    cache.store(a, "price", _vec(1, 0, 0), "K3")
    assert cache.lookup_exact(b, "price") is None


def test_invalidate_agent_clears_every_version(cache):
    from app.documents import cache as cache_mod
    cache.store(cache_mod.namespace("acme", "v1", "cfg"), "q", _vec(1, 0, 0), "A")
    cache.store(cache_mod.namespace("acme", "v2", "cfg"), "q", _vec(1, 0, 0), "B")
    cache.store(cache_mod.namespace("other", "v1", "cfg"), "q", _vec(1, 0, 0), "C")

    assert cache.invalidate_agent("acme") == 2
    assert cache.size() == 1
    assert cache.lookup_exact(cache_mod.namespace("other", "v1", "cfg"), "q").payload == "C"


def test_uploading_a_document_invalidates_the_agent(tmp_path, monkeypatch):
    """End-to-end: the store hook, not just the cache method."""
    from app.documents import cache as cache_mod
    from app.documents import embeddings, retrieval, store

    agents_dir = tmp_path / "agents"
    (agents_dir / "acme").mkdir(parents=True)
    monkeypatch.setattr(store, "_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(embeddings, "_AGENTS_DIR", agents_dir)
    cache_mod.reset_cache()
    retrieval._index_cache.clear()

    c = cache_mod.get_cache()
    c.store(cache_mod.namespace("acme", "v1", "cfg"), "q", _vec(1, 0, 0), "STALE")
    assert c.size() == 1

    store.save_document("acme", "new.txt", b"hello world", "hello world",
                        "Acme", "Knowledge", "")
    assert c.size() == 0, "an upload must drop the agent's cached retrievals"


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------

def test_normalisation_makes_punctuation_and_case_one_entry(cache):
    from app.documents import cache as cache_mod
    ns = cache_mod.namespace("acme", "c1", "cfg")
    cache.store(ns, "What is the price?", _vec(1, 0, 0), "EV")
    assert cache.lookup_exact(ns, "what is the price").payload == "EV"
    assert cache.lookup_exact(ns, "  WHAT IS THE PRICE!! ").payload == "EV"


def test_near_miss_below_threshold_is_a_miss(cache):
    from app.documents import cache as cache_mod
    ns = cache_mod.namespace("acme", "c1", "cfg")
    cache.store(ns, "what is the price of the 3bhk", _vec(1, 0, 0), "EV")
    assert cache.lookup_semantic(ns, _vec(0.7, 0.7, 0)) is None   # cos ~0.707
    assert cache.lookup_semantic(ns, _vec(1, 0.05, 0)) is not None  # cos ~0.999


def test_expiry_evicts_and_counts(cache):
    from app.documents import cache as cache_mod
    ns = cache_mod.namespace("acme", "c1", "cfg")
    cache.ttl_s = 0.0001
    cache.store(ns, "q", _vec(1, 0, 0), "EV")
    import time
    time.sleep(0.01)
    assert cache.lookup_exact(ns, "q") is None
    assert cache.stats.expired == 1


def test_lru_eviction_respects_the_bound(cache):
    from app.documents import cache as cache_mod
    ns = cache_mod.namespace("acme", "c1", "cfg")
    for i in range(6):
        cache.store(ns, f"question {i}", _vec(1, i, 0), f"EV{i}")
    assert cache.size() == 4
    assert cache.stats.evictions == 2
    assert cache.lookup_exact(ns, "question 0") is None
    assert cache.lookup_exact(ns, "question 5") is not None


def test_only_the_exact_tier_claims_a_token_saving(cache):
    """A semantic hit happens AFTER the query was embedded, so it saves no
    tokens. Counting it as a saving would fabricate the headline number."""
    from app.documents import cache as cache_mod
    ns = cache_mod.namespace("acme", "c1", "cfg")
    cache.store(ns, "what is the price", _vec(1, 0, 0), "EV", embed_tokens=7)

    cache.lookup_semantic(ns, _vec(1, 0.01, 0))
    assert cache.stats.semantic_hits == 1
    assert cache.stats.embed_tokens_saved == 0
    assert cache.stats.embed_calls_saved == 0

    cache.lookup_exact(ns, "what is the price")
    assert cache.stats.embed_calls_saved == 1
    assert cache.stats.embed_tokens_saved == 7


def test_exact_hit_skips_the_embedding_call_end_to_end(tmp_path, monkeypatch):
    from test_rag_retrieval import DOCS, FakeProvider

    from app.documents import cache as cache_mod
    from app.documents import embeddings, retrieval, store

    agents_dir = tmp_path / "agents"
    (agents_dir / "acme").mkdir(parents=True)
    monkeypatch.setattr(store, "_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(embeddings, "_AGENTS_DIR", agents_dir)
    provider = FakeProvider()
    embeddings.set_provider(provider)
    cache_mod.reset_cache()
    retrieval._index_cache.clear()
    try:
        meta = store.save_document("acme", "h.txt", DOCS["handbook"].encode(),
                                   DOCS["handbook"], "Acme", "Knowledge", "")
        embeddings.embed_document("acme", meta["doc_id"])

        q = "what is the price of the three bedroom in Surat"
        first = retrieval.retrieve("acme", q)
        calls_after_first = provider.calls
        second = retrieval.retrieve("acme", q)

        assert not first.cache_hit
        assert second.cache_hit and second.cache_tier == "exact"
        assert provider.calls == calls_after_first, "cached turn must not embed"
        assert [c.chunk_id for c in second.chunks] == [c.chunk_id for c in first.chunks]
        assert second.context_block == first.context_block
    finally:
        embeddings.set_provider(None)
        cache_mod.reset_cache()
        retrieval._index_cache.clear()


def test_stats_report_a_hit_rate(cache):
    from app.documents import cache as cache_mod
    ns = cache_mod.namespace("acme", "c1", "cfg")
    cache.store(ns, "q1", _vec(1, 0, 0), "EV")
    cache.lookup_exact(ns, "q1")
    cache.lookup_exact(ns, "q2")          # miss at the exact tier
    cache.lookup_semantic(ns, _vec(0, 1, 0))   # miss at the semantic tier
    stats = cache.stats.as_dict()
    assert stats["exact_hits"] == 1 and stats["misses"] == 1
    assert 0 < stats["hit_rate"] <= 1
