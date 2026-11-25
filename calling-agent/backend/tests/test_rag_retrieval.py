"""Document-RAG failure modes.

Every test here is a defect that shipped, or a way this pipeline can fail
silently in production. The bar is not "the happy path works" — it is "the
pipeline says so when it doesn't".

Runs fully offline: the embedding provider is a deterministic fake, so there is
no key, no network and no spend.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from conftest import BACKEND_DIR  # noqa: F401  (path bootstrap)


# ---------------------------------------------------------------------------
# A deterministic embedding provider: hashed bag-of-words.
# Similar text -> similar vectors, with no API and no randomness between runs.
# ---------------------------------------------------------------------------

def _stable_hash(word: str) -> int:
    """zlib.crc32, not the built-in hash().

    Python randomises string hashing per process (PYTHONHASHSEED), so a fake
    embedder built on `hash()` produces different vectors on every run — and
    the tests that depend on a similarity score then pass or fail by luck. A
    flaky regression gate is worse than no gate.
    """
    import zlib
    return zlib.crc32(word.encode("utf-8"))


class FakeProvider:
    def __init__(self, dim: int = 64, model: str = "fake-embed-v1"):
        self.provider = "fake"
        self.model = model
        self.dim = dim
        self.calls = 0
        self.last_tokens = 0
        self.fail_with: Exception | None = None

    def available(self) -> bool:
        return True

    def embed(self, texts: list[str]) -> np.ndarray:
        if self.fail_with is not None:
            raise self.fail_with
        self.calls += 1
        self.last_tokens = sum(len(t.split()) for t in texts)
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for word in text.lower().split():
                out[i, _stable_hash(word) % self.dim] += 1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return out / norms


DOCS = {
    "handbook": ("Pramukh Revanta is a three bedroom apartment in Surat priced "
                 "at one point one one crore rupees. Possession is scheduled "
                 "for December. The clubhouse includes a swimming pool."),
    "policy": ("Refund requests must be raised within seven days. Cancellation "
               "charges apply after the booking amount is paid."),
    "menu": ("The cafeteria serves masala dosa, filter coffee and pongal every "
             "morning between seven and eleven."),
}


@pytest.fixture()
def rag(tmp_path, monkeypatch):
    """A built index over a small corpus, wired to the fake provider."""
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

    ids = {}
    for name, text in DOCS.items():
        meta = store.save_document("acme", f"{name}.txt", text.encode(), text,
                                   "Acme", "Knowledge", name)
        assert embeddings.embed_document("acme", meta["doc_id"])
        ids[name] = meta["doc_id"]

    yield {"dir": agents_dir, "ids": ids, "provider": provider,
           "doc_of": {v: k for k, v in ids.items()}}

    embeddings.set_provider(None)
    cache_mod.reset_cache()
    retrieval._index_cache.clear()


def _docs_of(rag, result):
    return [rag["doc_of"].get(c.doc_id, c.doc_id) for c in result.chunks]


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def test_retrieves_the_right_document(rag):
    from app.documents import retrieval
    res = retrieval.retrieve("acme", "what is the price of the three bedroom in Surat")
    assert not res.abstained
    assert "handbook" in _docs_of(rag, res)


def test_lexical_arm_answers_when_the_vector_arm_is_dead(rag):
    """A vector-only pipeline goes blind when embeddings fail; this one does not.

    The provider is broken AFTER the index is built, which is the real
    production shape: stored vectors are fine, the query cannot be embedded.
    """
    from app.documents import retrieval
    rag["provider"].fail_with = RuntimeError("provider down")
    res = retrieval.retrieve("acme", "refund cancellation charges booking")
    assert res.degraded and "embedding unavailable" in res.degraded_reason
    assert not res.abstained, "lexical arm should still answer"
    assert "policy" in _docs_of(rag, res)


def test_weak_candidates_are_pruned_not_padded(rag):
    """final_top_k is a ceiling, not a quota — the old path always emitted 3."""
    from app.documents import retrieval
    res = retrieval.retrieve("acme", "swimming pool clubhouse", final_top_k=3)
    assert not res.abstained
    assert len(res.chunks) < 3 or all(
        c.stage_scores["vector"] >= 0.25 for c in res.chunks)


# ---------------------------------------------------------------------------
# Abstention
# ---------------------------------------------------------------------------

def test_filler_turn_never_reaches_the_embedding_api(rag):
    """"hmm" is a turn, not a question. The old path embedded it and injected
    three unrelated chunks into the prompt."""
    from app.documents import retrieval
    before = rag["provider"].calls
    res = retrieval.retrieve("acme", "hmm")
    assert res.abstained and res.abstain_reason == "not_a_question"
    assert res.context_block == ""
    assert rag["provider"].calls == before, "no API call for a filler turn"


def test_unrelated_question_abstains_and_says_so(rag):
    from app.documents import retrieval
    res = retrieval.retrieve("acme", "who won the football world cup in 1998")
    assert res.abstained
    assert res.chunks == [] and res.citations == []
    assert "No uploaded document covers this question" in res.context_block


def test_abstention_can_be_disabled_for_measurement(rag):
    from app.documents import retrieval
    res = retrieval.retrieve("acme", "who won the football world cup in 1998",
                             abstention_enabled=False)
    assert not res.abstained


# ---------------------------------------------------------------------------
# Citations
# ---------------------------------------------------------------------------

def test_citations_map_to_evidence_actually_supplied(rag):
    """A citation must name a chunk that was placed in the context — always."""
    from app.documents import retrieval
    res = retrieval.retrieve("acme", "when is possession of the apartment")
    assert res.citations
    supplied = {(c.doc_id, c.chunk_id) for c in res.chunks}
    for cite in res.citations:
        assert (cite["document_id"], cite["chunk_id"]) in supplied
        assert f"[{cite['label']}]" in res.context_block
    assert len(res.citations) == len(res.chunks)


def test_abstention_emits_no_citations(rag):
    from app.documents import retrieval
    res = retrieval.retrieve("acme", "who won the football world cup in 1998")
    assert res.citations == []


def test_context_does_not_instruct_the_agent_to_speak_labels(rag):
    """Voice constraint: labels are for the system, not for the caller."""
    from app.documents import retrieval
    res = retrieval.retrieve("acme", "what is in the clubhouse")
    assert "Do NOT read out the [S1] labels" in res.context_block


# ---------------------------------------------------------------------------
# Embedding identity / index integrity
# ---------------------------------------------------------------------------

def test_index_records_its_embedding_identity(rag):
    from app.documents import embeddings
    meta_path = (rag["dir"] / "acme" / "documents" / rag["ids"]["handbook"]
                 / embeddings.INDEX_META_FILE)
    meta = embeddings.IndexMeta.from_json(json.loads(meta_path.read_text()))
    assert meta.model == "fake-embed-v1"
    assert meta.dim == 64
    assert meta.chunks_sha and meta.normalized
    assert meta.chunk_words == 250


def test_model_change_is_detected_not_silently_reinterpreted(rag):
    """The defect this metadata exists for: swapping the embedding model made
    every stored vector meaningless, and the only symptom was an agent that
    quietly stopped using its documents."""
    from app.documents import embeddings, retrieval
    retrieval._index_cache.clear()
    embeddings.set_provider(FakeProvider(dim=64, model="different-model-v2"))

    idx = embeddings.load_index("acme")
    assert not idx.has_vectors
    assert any("re-index required" in p for p in idx.problems)

    status = embeddings.index_status("acme")
    assert set(status["needs_reindex"]) == set(rag["ids"].values())

    res = retrieval.retrieve("acme", "refund within seven days")
    assert res.degraded, "a mismatched index must degrade loudly"
    assert res.index_problems


def test_dimension_change_is_detected(rag):
    from app.documents import embeddings, retrieval
    retrieval._index_cache.clear()
    embeddings.set_provider(FakeProvider(dim=128, model="fake-embed-v1"))
    idx = embeddings.load_index("acme")
    assert not idx.has_vectors
    assert any("dim" in p for p in idx.problems)


def test_edited_document_is_detected_as_stale(rag):
    """chunks.json changed but the vectors did not — retrieval would otherwise
    score the query against text that is no longer there."""
    from app.documents import embeddings, retrieval
    doc_dir = rag["dir"] / "acme" / "documents" / rag["ids"]["menu"]
    chunks = json.loads((doc_dir / "chunks.json").read_text())
    chunks[0]["text"] = "completely different content about tyres and gearboxes"
    (doc_dir / "chunks.json").write_text(json.dumps(chunks))
    retrieval._index_cache.clear()

    idx = embeddings.load_index("acme")
    assert any("changed since it was embedded" in p for p in idx.problems)
    assert embeddings.index_status("acme")["needs_reindex"] == [rag["ids"]["menu"]]


def test_reindex_repairs_a_stale_index(rag):
    from app.documents import embeddings, retrieval
    embeddings.set_provider(FakeProvider(dim=64, model="different-model-v2"))
    retrieval._index_cache.clear()
    assert embeddings.index_status("acme")["needs_reindex"]

    result = embeddings.reindex_agent("acme")
    assert not result["failed"]
    retrieval._index_cache.clear()
    assert embeddings.index_status("acme")["needs_reindex"] == []
    assert embeddings.load_index("acme").has_vectors


def test_chunk_geometry_change_is_reported(rag, monkeypatch):
    from app.documents import embeddings, store
    monkeypatch.setattr(store, "_CHUNK_WORDS", 40)
    status = embeddings.index_status("acme")
    assert status["needs_reindex"]
    detail = [d["detail"] for d in status["documents"] if d["state"] == "stale"]
    assert any("chunked at 250 words" in d for d in detail)


def test_missing_vectors_still_leave_the_document_searchable(rag):
    """Degrade partially and loudly, not totally and silently."""
    from app.documents import embeddings, retrieval
    (rag["dir"] / "acme" / "documents" / rag["ids"]["policy"]
     / embeddings.EMBEDDINGS_FILE).unlink()
    retrieval._index_cache.clear()

    idx = embeddings.load_index("acme")
    assert any("not embedded" in p for p in idx.problems)
    res = retrieval.retrieve("acme", "refund cancellation charges")
    assert not res.abstained
    assert "policy" in _docs_of(rag, res), "lexical arm must still find it"


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------

def test_no_documents_is_not_an_error(tmp_path, monkeypatch):
    from app.documents import embeddings, retrieval, store
    agents_dir = tmp_path / "agents"
    (agents_dir / "empty").mkdir(parents=True)
    monkeypatch.setattr(store, "_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(embeddings, "_AGENTS_DIR", agents_dir)
    retrieval._index_cache.clear()
    res = retrieval.retrieve("empty", "anything at all")
    assert res.abstained and res.abstain_reason == "no_documents"
    assert res.context_block == ""


def test_corrupt_vector_file_does_not_raise(rag):
    from app.documents import embeddings, retrieval
    (rag["dir"] / "acme" / "documents" / rag["ids"]["menu"]
     / embeddings.EMBEDDINGS_FILE).write_bytes(b"not a numpy file")
    retrieval._index_cache.clear()
    res = retrieval.retrieve("acme", "masala dosa filter coffee")
    assert isinstance(res.context_block, str)
    assert any("unreadable" in p or "re-index" in p for p in res.index_problems)


def test_retrieval_never_raises_into_the_caller(rag, monkeypatch):
    from app.documents import retrieval
    monkeypatch.setattr(retrieval, "load_cached_index",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone")))
    res = retrieval.retrieve("acme", "what is the price of the apartment")
    assert res.abstained and res.abstain_reason == "error"
    assert res.degraded


def test_async_retrieval_honours_its_deadline(rag, monkeypatch):
    """A slow provider must cost the turn its context, never the event loop."""
    import asyncio
    import time

    from app.documents import retrieval

    def _slow(*a, **k):
        time.sleep(1.0)
        return ([], None)

    monkeypatch.setattr(retrieval, "_vector_arm", _slow)
    res = asyncio.run(retrieval.retrieve_async("acme", "price of the apartment",
                                               timeout_s=0.2))
    assert res.abstained and res.abstain_reason == "timeout"
    assert res.degraded


def test_async_retrieval_does_not_block_the_event_loop(rag, monkeypatch):
    """The defect this async path exists for: the embedding HTTP call ran
    inline in the async turn, so a slow provider froze VAD, STT and TTS with
    it. The heartbeat is the assertion — it can only keep ticking if the
    blocking work really left the loop."""
    import asyncio
    import time

    from app.documents import retrieval

    real_embed = retrieval.embeddings.embed_query

    def _slow_embed(text, provider=None):
        time.sleep(0.4)
        return real_embed(text, provider)

    monkeypatch.setattr(retrieval.embeddings, "embed_query", _slow_embed)

    async def _drive():
        ticks = 0

        async def _heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.ensure_future(_heartbeat())
        try:
            await retrieval.retrieve_async(
                "acme", "price of the apartment in Surat", timeout_s=5)
        finally:
            beat.cancel()
        return ticks

    ticks = asyncio.run(_drive())
    assert ticks >= 5, f"event loop was blocked during retrieval (ticks={ticks})"


# ---------------------------------------------------------------------------
# Agent-folder resolution (called on the LLM path of every turn)
# ---------------------------------------------------------------------------

def test_unresolvable_agent_id_is_not_rescanned_every_turn(tmp_path, monkeypatch):
    """A miss used to re-parse every agent.yaml on disk — measured at 1.4s on
    the 44 agents of this deployment — on the event loop, on EVERY turn,
    because only hits were cached."""
    from app.documents import store

    agents_dir = tmp_path / "agents"
    for i in range(6):
        (agents_dir / f"a{i}").mkdir(parents=True)
        (agents_dir / f"a{i}" / "agent.yaml").write_text(f"agent_id: id-{i}\n")
    monkeypatch.setattr(store, "_AGENTS_DIR", agents_dir)
    store.forget_agent_folders()

    scans = {"n": 0}
    real_glob = agents_dir.glob

    class _CountingDir(type(agents_dir)):
        def glob(self, pattern):
            scans["n"] += 1
            return real_glob(pattern)

    monkeypatch.setattr(store, "_AGENTS_DIR", _CountingDir(agents_dir))

    for _ in range(5):
        assert store.get_folder_by_agent_id("ghost-agent") == ""
    assert scans["n"] == 1, "a cached miss must not rescan the agents directory"

    for _ in range(5):
        assert store.get_folder_by_agent_id("id-3") == "a3"
    assert scans["n"] == 2, "a cached hit must not rescan either"


def test_a_cached_miss_expires_so_a_new_agent_becomes_visible(tmp_path, monkeypatch):
    """Caching a miss forever would hide any agent created after the miss."""
    import time

    from app.documents import store

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    monkeypatch.setattr(store, "_AGENTS_DIR", agents_dir)
    store.forget_agent_folders()

    assert store.get_folder_by_agent_id("later-agent") == ""

    (agents_dir / "later").mkdir()
    (agents_dir / "later" / "agent.yaml").write_text("agent_id: later-agent\n")
    assert store.get_folder_by_agent_id("later-agent") == "", "still within the TTL"

    store._miss_at["later-agent"] = time.monotonic() - store._MISS_TTL_S - 1
    assert store.get_folder_by_agent_id("later-agent") == "later"


def test_turn_trace_carries_the_evidence_that_backed_it(rag, monkeypatch):
    """The audit claim, pinned: citations must reach the turn record, not just
    a rotating trace file. `_rag_citations` was a dead assignment until this."""
    import asyncio
    from types import SimpleNamespace

    from app.documents import retrieval

    res = asyncio.run(retrieval.retrieve_async(
        "acme", "what is the price of the three bedroom in Surat"))
    assert res.citations, "fixture question should retrieve evidence"

    # What pipeline.py does with the router's per-turn citations.
    conversation = SimpleNamespace(_rag_citations=res.citations,
                                   _rag_trace_id=res.trace_id)
    trace: dict = {}
    cites = getattr(conversation, "_rag_citations", None)
    if cites:
        trace["rag_sources"] = [
            {"label": c.get("label"), "document_id": c.get("document_id"),
             "chunk_id": c.get("chunk_id"), "score": c.get("relevance_score")}
            for c in cites]
    trace["rag_trace_id"] = getattr(conversation, "_rag_trace_id", "")

    assert len(trace["rag_sources"]) == len(res.chunks)
    supplied = {(c.doc_id, c.chunk_id) for c in res.chunks}
    for s in trace["rag_sources"]:
        assert (s["document_id"], s["chunk_id"]) in supplied
        assert s["label"] and s["score"] is not None
    assert trace["rag_trace_id"] == res.trace_id


def test_relative_floor_drops_an_unrelated_document(rag):
    """The 'console catalogue on a real-estate call' failure: a chunk that
    clears the absolute floor only because K had a slot free."""
    from app.documents import config, retrieval

    q = "three bedroom apartment price in Surat"
    monkey = config.RELATIVE_FLOOR
    try:
        config.RELATIVE_FLOOR = 0.0
        retrieval._index_cache.clear()
        wide = retrieval.retrieve("acme", q, use_cache=False)
        config.RELATIVE_FLOOR = 0.6
        tight = retrieval.retrieve("acme", q, use_cache=False)
    finally:
        config.RELATIVE_FLOOR = monkey
    assert len(tight.chunks) <= len(wide.chunks)
    top = max((c.stage_scores["vector"] for c in tight.chunks if c.has_vector),
              default=0.0)
    for c in tight.chunks:
        if c.has_vector:
            assert c.stage_scores["vector"] >= 0.6 * top - 1e-9


def test_lexical_rescue_only_fires_when_the_vector_arm_is_empty(rag):
    """Embeddings under-score short exact-term questions, so BM25 gets a
    fallback — but only a fallback. Adding its hits alongside working vector
    results measured Precision@3 0.53 -> 0.46 and contamination 0.05 -> 0.17,
    because one shared common word drags in another document."""
    from app.documents import retrieval

    # Vector arm works here: nothing should be admitted on lexical grounds.
    good = retrieval.retrieve("acme", "what is the price of the three bedroom in Surat",
                              use_cache=False)
    assert good.chunks
    assert all(c.kept_by == "vector" for c in good.chunks), \
        "lexical hits must not pad a working vector result"


def test_exact_term_question_survives_a_low_cosine(rag, monkeypatch):
    """The failure this rescue exists for: the answering chunk contains the
    query's exact terms but scores under the absolute floor."""
    from app.documents import config, retrieval

    # Force every cosine under the floor; BM25 is untouched.
    monkeypatch.setattr(config, "ABSTAIN_MIN_SCORE", 0.99)
    retrieval._index_cache.clear()
    res = retrieval.retrieve("acme", "masala dosa filter coffee pongal",
                             use_cache=False)
    assert not res.abstained, "a strong exact-term match must not be refused"
    assert res.chunks and all(c.kept_by == "lexical" for c in res.chunks)
    assert "menu" == rag["doc_of"][res.chunks[0].doc_id]


def test_lexical_rescue_does_not_resurrect_an_unrelated_question(rag, monkeypatch):
    """The rescue must not become a way to always return something."""
    from app.documents import config, retrieval

    monkeypatch.setattr(config, "ABSTAIN_MIN_SCORE", 0.99)
    retrieval._index_cache.clear()
    res = retrieval.retrieve("acme", "who won the football world cup in 1998",
                             use_cache=False)
    assert res.abstained
