"""The regression gate: golden evaluation, run offline, compared to a baseline.

This is the test that makes every other RAG change safe to merge. Anything that
touches chunking, embeddings, retrieval, re-ranking, the abstention gate, the
grounding prompt or the context assembly moves these numbers, and this fails
when they move the wrong way by more than the tolerance in
`rag_eval.runner.THRESHOLDS`.

It runs entirely offline against the frozen vector cache — no key, no network,
no spend — so it is deterministic and can gate a pull request. Answer-quality
metrics (faithfulness, relevance) need a live model and are therefore NOT
gated here; they are measured by
`python -m rag_eval.runner --generate --judge` before a release.

To re-baseline after a deliberate improvement:
    cd calling-agent/backend
    ../venv/bin/python -m rag_eval.runner --save-baseline current
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from conftest import BACKEND_DIR

RAG_EVAL = BACKEND_DIR / "rag_eval"
BASELINE = RAG_EVAL / "baselines" / "current.json"


def _run_eval(**overrides):
    from rag_eval import runner
    args = argparse.Namespace(
        dataset=str(runner.ds.DEFAULT_DATASET), corpus=str(runner.CORPUS_DIR),
        vector_cache=str(runner.VECTOR_CACHE),
        embed_model=os.environ.get("RAG_EMBED_MODEL", "gemini-embedding-2"),
        retriever="current", live=False, fresh=False, generate=False, judge=False,
        gen_model=None, judge_model=None, gen_provider="llm", judge_provider=None,
        chunk_words=None, top_k=None, final_k=None, rerank=None, abstention=None,
        out=None, label="ci", compare=None, gate=False, save_baseline=None,
        verbose=False)
    for k, v in overrides.items():
        setattr(args, k, v)
    return runner.run(args)


@pytest.fixture(scope="module")
def report():
    return _run_eval()


def test_golden_dataset_is_coherent():
    """Anchors must still occur in the corpus. This is the corpus-drift
    tripwire: scoring a retriever against evidence that no longer exists
    produces numbers that look fine and mean nothing."""
    from rag_eval import dataset as ds
    cases = ds.load()
    corpus = ds.load_corpus(RAG_EVAL / "corpus")
    assert ds.validate(cases, corpus) == []
    assert len(cases) >= 30
    categories = {c.category for c in cases}
    for required in ("no_answer", "multi_hop", "wrong_context", "edge_case"):
        assert required in categories, f"golden set lost its {required} cases"


def test_corpus_matches_its_sources():
    """corpus/ is a snapshot of live platform content; drift must be explicit."""
    from rag_eval.build_corpus import build
    assert build(check_only=True) == 0


def test_no_regression_against_baseline(report):
    from rag_eval.runner import compare, render_compare
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    rows, ok = compare(report, baseline)
    assert rows, "no comparable metrics — baseline and report disagree in shape"
    assert ok, "\n" + render_compare(rows, ok, BASELINE.name)


def test_retrieval_floor(report):
    """Absolute floors, so a baseline refreshed too generously still trips."""
    r = report["summary"]["retrieval"]
    assert r["recall@3"] >= 0.90, r
    assert r["mrr"] >= 0.80, r
    assert r["ndcg@5"] >= 0.80, r


def test_never_refuses_an_answerable_question(report):
    """Over-abstention is the expensive failure on a sales call — this platform
    has already shipped one such regression."""
    assert report["summary"]["abstention"]["over_abstention_rate"] == 0.0


def test_every_citation_maps_to_supplied_evidence(report):
    assert report["summary"]["citations"]["accuracy"] == 1.0


def test_reranking_never_loses_what_retrieval_found(report):
    """recall@candidates is the ceiling; the final set must not fall far below
    it. Catching exactly this is why the metric exists — a scale bug in MMR
    once dropped Recall@5 from 0.98 to 0.70 with a perfect candidate set."""
    r = report["summary"]["retrieval"]
    assert r["recall@5"] >= r["recall@candidates"] - 0.05, r


def test_filler_turns_retrieve_nothing(report):
    fillers = [c for c in report["cases"] if c["question"] in ("hmm", "ok")]
    assert fillers
    for case in fillers:
        assert case["abstained"] and case["abstain_reason"] == "not_a_question"
