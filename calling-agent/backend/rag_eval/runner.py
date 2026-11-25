"""The RAG evaluation runner.

    cd calling-agent/backend
    ../venv/bin/python -m rag_eval.runner                      # offline, free
    ../venv/bin/python -m rag_eval.runner --live               # real API + latency
    ../venv/bin/python -m rag_eval.runner --generate --judge   # + answer quality
    ../venv/bin/python -m rag_eval.runner --compare baselines/legacy.json

Default mode is offline and deterministic: embeddings come from the frozen
cache, so the run costs nothing, needs no key, and returns identical retrieval
numbers every time. Latency measured in that mode is NOT production latency and
the report says so on its face — use `--live` for anything you intend to quote.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import statistics
import sys
import tempfile
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:          # calling-agent/backend
    sys.path.insert(0, str(_HERE.parent))
if str(_HERE.parents[2]) not in sys.path:      # repo root, for `shared`
    sys.path.insert(0, str(_HERE.parents[2]))

from rag_eval import dataset as ds                     # noqa: E402
from rag_eval import metrics as M                      # noqa: E402
from rag_eval.harness import (                         # noqa: E402
    CurrentRetriever, LegacyRetriever, build_index,
)
from rag_eval.vectors import CachedEmbedder, EmbeddingCache  # noqa: E402

def _load_env() -> None:
    """Same .env the runtime reads — only needed for --live / --generate.

    Loaded without override so an explicitly exported key still wins, and
    never printed: the runner logs model names, never credentials.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(_HERE.parents[1] / ".env", override=False)
    except Exception:
        pass


CORPUS_DIR = _HERE / "corpus"
VECTOR_CACHE = _HERE / "vectors" / "frozen.npz"
BASELINE_DIR = _HERE / "baselines"
RECALL_KS = (1, 3, 5, 10)


# ---------------------------------------------------------------------------
# Embedding provider for the run
# ---------------------------------------------------------------------------

class _LiveEmbedFn:
    """The real embedding call — same client the ingestion path uses.

    Carries `last_tokens` so a --fresh run reports the provider's own token
    count rather than an estimate; a cached run reports 0, which is the honest
    figure because no request was made.
    """

    def __init__(self, model: str):
        self.model = model
        self.last_tokens = 0
        self._provider = None

    def __call__(self, texts: list[str]):
        if self._provider is None:
            # The production provider for this model — Gemini for
            # `gemini-embedding-*`, OpenAI for `text-embedding-*` — so a live
            # run measures the same client the ingestion path uses.
            from app.documents import embeddings as emb
            self._provider = emb.build_provider(model=self.model)
            if not emb.provider_available(self._provider):
                raise RuntimeError(
                    f"--live needs credentials for the {self._provider.provider} "
                    f"embedding provider ({self.model})")
        vectors = self._provider.embed(texts)
        self.last_tokens = int(getattr(self._provider, "last_tokens", 0) or 0)
        return [np.asarray(v, dtype=np.float32) for v in vectors]


# ---------------------------------------------------------------------------
# Scoring one case
# ---------------------------------------------------------------------------

def _score_case(case: ds.GoldenCase, outcome, generated: dict | None) -> dict:
    row: dict = {
        "id": case.id,
        "category": case.category,
        "difficulty": case.difficulty,
        "answerable": case.answerable,
        "question": case.question,
        "abstained": outcome.abstained,
        "abstain_reason": outcome.abstain_reason,
        "degraded": outcome.degraded,
        "cache_hit": outcome.cache_hit,
        "n_candidates": len(outcome.candidates),
        "n_hits": len(outcome.hits),
        "top_score": round(outcome.hits[0].score, 4) if outcome.hits else None,
        "total_ms": round(outcome.total_ms, 2),
        "stage_ms": {k: round(v, 2) for k, v in outcome.stage_ms.items()},
        "embed_tokens": outcome.embed_tokens,
        "embed_cost_inr": outcome.embed_cost_inr,
        "retrieved": [{"doc": c.doc, "chunk": c.chunk_id, "score": round(c.score, 4)}
                      for c in outcome.hits],
    }

    for k in RECALL_KS:
        row[f"recall@{k}"] = M.recall_at_k(case, outcome.hits, k)
    # Recall over the pre-rerank candidate set: the ceiling the reranker is
    # working against. recall@final < recall@candidates means the reranker
    # threw away evidence retrieval had already found.
    row["recall@candidates"] = M.recall_at_k(case, outcome.candidates, 50)
    row["precision@3"] = M.precision_at_k(case, outcome.hits, 3)
    row["precision@5"] = M.precision_at_k(case, outcome.hits, 5)
    row["mrr"] = M.reciprocal_rank(case, outcome.hits)
    row["contamination"] = M.contamination(case, outcome.hits)
    row["ndcg@5"] = M.ndcg_at_k(case, outcome.hits, 5)

    # Abstention is scored on the two cases that matter: a question with no
    # answer in the corpus must abstain, and an answerable one must not.
    if case.must_abstain:
        row["abstention_correct"] = bool(outcome.abstained)
    else:
        row["over_abstained"] = bool(outcome.abstained)

    if outcome.citations:
        row["citation_accuracy"] = M.citation_accuracy(outcome.citations, outcome.hits)
        row["n_citations"] = len(outcome.citations)

    if generated:
        answer = generated.get("answer", "")
        row["answer"] = answer
        row["answer_refused"] = bool(generated.get("refused"))
        # What the caller actually experiences: the retrieval gate refusing, OR
        # the model declining because the grounding contract told it to. Either
        # is a correct outcome on a no-answer question; only the pair of them
        # is the number worth quoting.
        row["e2e_abstained"] = bool(outcome.abstained or generated.get("refused"))
        if case.must_abstain:
            row["e2e_abstention_correct"] = row["e2e_abstained"]
        else:
            row["e2e_over_abstained"] = row["e2e_abstained"]
        if generated.get("error"):
            row["generation_error"] = generated["error"]
        row["unsupported_claims"] = M.unsupported_claims(answer, case)
        row["required_facts"] = M.required_facts_present(answer, case)
        for key in ("faithfulness", "answer_relevance", "judge_note"):
            if key in generated:
                row[key] = generated[key]
        for key in ("input_tokens", "output_tokens", "cost_inr", "llm_ms", "model"):
            if key in generated:
                row[key] = generated[key]
    return row


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _agg(rows: list[dict]) -> dict:
    def avg(key, places: int = 4):
        # Embedding cost per query is ~1e-5 rupees. Rounding it to 4 decimals
        # reported it as 0.00000000 — a cost metric that always reads zero is
        # worse than no cost metric, because it looks measured.
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), places) if vals else None

    lat = [r["total_ms"] for r in rows]
    answerable = [r for r in rows if r["answerable"]]
    abstain_cases = [r for r in rows if not r["answerable"]]

    summary = {
        "questions": len(rows),
        "answerable": len(answerable),
        "must_abstain": len(abstain_cases),
        "retrieval": {
            **{f"recall@{k}": avg(f"recall@{k}") for k in RECALL_KS},
            "recall@candidates": avg("recall@candidates"),
            "precision@3": avg("precision@3"),
            "precision@5": avg("precision@5"),
            "mrr": avg("mrr"),
            "ndcg@5": avg("ndcg@5"),
            "contamination": avg("contamination"),
        },
        "abstention": {
            "correct_abstention_rate": (
                round(sum(1 for r in abstain_cases if r.get("abstention_correct")) /
                      len(abstain_cases), 4) if abstain_cases else None),
            "over_abstention_rate": (
                round(sum(1 for r in answerable if r.get("over_abstained")) /
                      len(answerable), 4) if answerable else None),
        },
        "latency_ms": {
            "p50": round(M.percentile(lat, 50), 2),
            "p90": round(M.percentile(lat, 90), 2),
            "p95": round(M.percentile(lat, 95), 2),
            "p99": round(M.percentile(lat, 99), 2),
            "mean": round(statistics.fmean(lat), 2) if lat else 0.0,
        },
        "cost": {
            "embed_tokens_per_query": avg("embed_tokens"),
            "embed_cost_inr_per_query": avg("embed_cost_inr", 10),
            "cache_hit_rate": round(
                sum(1 for r in rows if r.get("cache_hit")) / len(rows), 4) if rows else None,
        },
    }

    stage_keys = sorted({k for r in rows for k in r.get("stage_ms", {})})
    summary["latency_ms"]["stages_p95"] = {
        k: round(M.percentile([r["stage_ms"][k] for r in rows if k in r["stage_ms"]], 95), 2)
        for k in stage_keys}

    if any("citation_accuracy" in r for r in rows):
        summary["citations"] = {
            "accuracy": avg("citation_accuracy"),
            "avg_per_answer": avg("n_citations"),
        }
    if any("answer" in r for r in rows):
        hallucinated = [r for r in rows if r.get("unsupported_claims")]
        e2e_ab = [r for r in rows if not r["answerable"] and "e2e_abstained" in r]
        e2e_ans = [r for r in rows if r["answerable"] and "e2e_abstained" in r]
        summary["generation"] = {
            "correct_abstention_rate": (
                round(sum(1 for r in e2e_ab if r.get("e2e_abstention_correct")) /
                      len(e2e_ab), 4) if e2e_ab else None),
            "over_abstention_rate": (
                round(sum(1 for r in e2e_ans if r.get("e2e_over_abstained")) /
                      len(e2e_ans), 4) if e2e_ans else None),
            "errors": sum(1 for r in rows if r.get("generation_error")),
            "faithfulness": avg("faithfulness"),
            "answer_relevance": avg("answer_relevance"),
            "required_facts_present": avg("required_facts"),
            "unsupported_claim_rate": round(len(hallucinated) / len(rows), 4),
            "input_tokens_per_query": avg("input_tokens"),
            "output_tokens_per_query": avg("output_tokens"),
            "cost_inr_per_query": avg("cost_inr", 8),
        }

    by_cat: dict[str, dict] = {}
    for r in rows:
        c = by_cat.setdefault(r["category"],
                              {"n": 0, "recall@5": [], "mrr": [], "contamination": []})
        c["n"] += 1
        for key in ("recall@5", "mrr", "contamination"):
            if r.get(key) is not None:
                c[key].append(r[key])
    summary["by_category"] = {
        k: {"n": v["n"],
            "recall@5": round(sum(v["recall@5"]) / len(v["recall@5"]), 3) if v["recall@5"] else None,
            "mrr": round(sum(v["mrr"]) / len(v["mrr"]), 3) if v["mrr"] else None,
            "contamination": round(sum(v["contamination"]) / len(v["contamination"]), 3)
            if v["contamination"] else None}
        for k, v in sorted(by_cat.items())}
    return summary


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def render(report: dict) -> str:
    s, cfg = report["summary"], report["config"]
    L = []
    A = L.append
    A("Evaluation Run")
    A("-" * 56)
    A(f"Retriever:       {cfg['retriever']}")
    A(f"Mode:            {cfg['mode']}"
      + ("" if cfg.get("latency_is_real") else
         "   (cached vectors — latency is NOT production latency)"))
    A(f"Corpus:          {cfg['documents']} documents / {cfg['chunks']} chunks"
      f" ({cfg['chunk_words']} words per chunk)")
    A(f"Total Questions: {s['questions']}"
      f"   ({s['answerable']} answerable / {s['must_abstain']} must-abstain)")
    A("")
    r = s["retrieval"]

    def f(v, w=6):
        return "   n/a" if v is None else f"{v:.{w-4}f}".rjust(w)

    for k in RECALL_KS:
        A(f"Retrieval Recall@{k}:".ljust(26) + f(r[f'recall@{k}']))
    A("Recall@candidates:".ljust(26) + f(r["recall@candidates"])
      + "   (ceiling before re-ranking)")
    A("Precision@3:".ljust(26) + f(r["precision@3"]))
    A("Precision@5:".ljust(26) + f(r["precision@5"]))
    A("MRR:".ljust(26) + f(r["mrr"]))
    A("NDCG@5:".ljust(26) + f(r["ndcg@5"]))
    A("Cross-doc contamination:".ljust(26) + f(r["contamination"])
      + "   (evidence from the wrong document — lower is better)")
    A("")
    ab = s["abstention"]
    A("Correct abstention:".ljust(26) + f(ab["correct_abstention_rate"])
      + "   (no-answer questions refused)")
    A("Over-abstention:".ljust(26) + f(ab["over_abstention_rate"])
      + "   (answerable questions refused — lower is better)")
    if "citations" in s:
        A("Citation accuracy:".ljust(26) + f(s["citations"]["accuracy"])
          + "   (citations that map to supplied evidence)")
    if "generation" in s:
        g = s["generation"]
        A("")
        A("End-to-end abstention:".ljust(26) + f(g["correct_abstention_rate"])
          + "   (retrieval gate OR the model declining)")
        A("End-to-end over-abstain:".ljust(26) + f(g["over_abstention_rate"]))
        A("Answer faithfulness:".ljust(26) + f(g["faithfulness"]))
        A("Answer relevance:".ljust(26) + f(g["answer_relevance"]))
        A("Required facts present:".ljust(26) + f(g["required_facts_present"]))
        A("Unsupported claim rate:".ljust(26) + f(g["unsupported_claim_rate"]))
    A("")
    lat = s["latency_ms"]
    for p in ("p50", "p90", "p95", "p99"):
        A(f"{p.upper()} Latency:".ljust(26) + f"{lat[p]:>8.1f} ms")
    if lat["stages_p95"]:
        A("  per stage (p95):        "
          + "  ".join(f"{k}={v:.1f}ms" for k, v in lat["stages_p95"].items()))
    A("")
    c = s["cost"]
    A("Embed tokens/query:".ljust(26)
      + (f"{c['embed_tokens_per_query']:>8.1f}" if c["embed_tokens_per_query"] is not None else "     n/a"))
    A("Embed cost/query:".ljust(26)
      + (f"  ₹{c['embed_cost_inr_per_query']:.8f}" if c["embed_cost_inr_per_query"] is not None else "     n/a"))
    A("Cache hit rate:".ljust(26) + f(c["cache_hit_rate"]))
    if "generation" in s:
        g = s["generation"]
        A("LLM tokens/query:".ljust(26)
          + (f"{(g['input_tokens_per_query'] or 0) + (g['output_tokens_per_query'] or 0):>8.1f}"))
        A("LLM cost/query:".ljust(26)
          + (f"  ₹{g['cost_inr_per_query']:.6f}" if g["cost_inr_per_query"] is not None else "     n/a"))
    A("")
    A("By category:")
    for cat, v in s["by_category"].items():
        A(f"  {cat:<16} n={v['n']:<3} recall@5="
          + ("n/a " if v["recall@5"] is None else f"{v['recall@5']:.2f}")
          + "  mrr=" + ("n/a" if v["mrr"] is None else f"{v['mrr']:.2f}")
          + "  contam=" + ("n/a" if v.get("contamination") is None
                           else f"{v['contamination']:.2f}"))
    return "\n".join(L)


def _flatten(summary: dict) -> dict[str, float]:
    flat = {}
    for group, vals in summary.items():
        if isinstance(vals, dict) and group != "by_category":
            for k, v in vals.items():
                if isinstance(v, (int, float)):
                    flat[f"{group}.{k}"] = float(v)
    return flat


# Regression thresholds. Quality metrics may not fall; cost/latency may not
# rise. The starting values follow the brief's defaults; `direction` is what
# makes them meaningful — a 3% *rise* in recall is not a regression.
THRESHOLDS: dict[str, tuple[str, float]] = {
    "retrieval.recall@5": ("up", 0.03),
    "retrieval.recall@3": ("up", 0.03),
    "retrieval.mrr": ("up", 0.03),
    "retrieval.ndcg@5": ("up", 0.03),
    "retrieval.contamination": ("down", 0.10),
    "abstention.correct_abstention_rate": ("up", 0.05),
    "generation.faithfulness": ("up", 0.03),
    "generation.correct_abstention_rate": ("up", 0.05),
    "generation.answer_relevance": ("up", 0.03),
    "citations.accuracy": ("up", 0.03),
    "latency_ms.p95": ("down", 0.20),
    "cost.embed_cost_inr_per_query": ("down", 0.20),
}


# Below this, a wall-clock comparison is measuring the machine, not the code.
# The offline run's p95 is ~3ms (the embedding API is replaced by a frozen
# cache), and 20% of 3ms is noise — gating on it made the CI job fail at random
# roughly one run in six. Latency thresholds therefore apply only to runs whose
# baseline is a real measurement, i.e. `--fresh`.
LATENCY_NOISE_FLOOR_MS = 50.0


def compare(current: dict, baseline: dict) -> tuple[list[dict], bool]:
    cur, base = _flatten(current["summary"]), _flatten(baseline["summary"])
    rows, ok = [], True
    for metric, (direction, tol) in THRESHOLDS.items():
        if metric not in cur or metric not in base:
            continue
        c, b = cur[metric], base[metric]
        if metric.startswith("latency_ms.") and b < LATENCY_NOISE_FLOOR_MS:
            rows.append({"metric": metric, "baseline": round(b, 6),
                         "current": round(c, 6), "limit": "n/a",
                         "delta": round(c - b, 6), "status": "SKIP"})
            continue
        if direction == "up":                      # higher is better
            allowed = b * (1 - tol)
            passed = c >= allowed - 1e-9
            limit = f">= {allowed:.4f}"
        else:                                      # lower is better
            allowed = b * (1 + tol) if b > 0 else tol
            passed = c <= allowed + 1e-9
            limit = f"<= {allowed:.4f}"
        ok &= passed
        rows.append({"metric": metric, "baseline": round(b, 6),
                     "current": round(c, 6), "limit": limit,
                     "delta": round(c - b, 6),
                     "status": "PASS" if passed else "FAIL"})
    return rows, ok


def render_compare(rows: list[dict], ok: bool, base_label: str) -> str:
    L = [f"Regression check vs {base_label}", "-" * 78,
         f"{'metric':<40}{'baseline':>10}{'current':>10}{'delta':>10}  status"]
    for r in rows:
        L.append(f"{r['metric']:<40}{r['baseline']:>10.4f}{r['current']:>10.4f}"
                 f"{r['delta']:>+10.4f}  {r['status']}")
    L.append("-" * 78)
    skipped = sum(1 for r in rows if r["status"] == "SKIP")
    if skipped:
        L.append(f"({skipped} metric(s) SKIPped — baseline below the measurement "
                 f"noise floor; re-run with --fresh for real latency)")
    L.append("RESULT: " + ("PASS" if ok else "FAIL"))
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args) -> dict:
    if args.live or args.generate:
        _load_env()
    cases = ds.load(args.dataset)
    corpus = ds.load_corpus(args.corpus)
    problems = ds.validate(cases, corpus)
    if problems:
        raise SystemExit("golden dataset invalid:\n  " + "\n  ".join(problems))

    cache = EmbeddingCache(args.vector_cache)
    model = args.embed_model
    embedder = CachedEmbedder(model, cache, live_fn=_LiveEmbedFn(model),
                              live=args.live, bypass=args.fresh)

    workdir = Path(tempfile.mkdtemp(prefix="rag_eval_"))
    try:
        ids = build_index(workdir, Path(args.corpus), embedder,
                          chunk_words=args.chunk_words)
        agents_dir = workdir / "agents"
        n_chunks = sum(
            len(json.loads((agents_dir / "rag_eval_agent" / "documents" / d /
                            "chunks.json").read_text(encoding="utf-8")))
            for d in ids.values())

        overrides = {}
        if args.top_k:
            overrides["retrieval_top_k"] = args.top_k
        if args.final_k:
            overrides["final_top_k"] = args.final_k
        if args.rerank is not None:
            overrides["rerank_enabled"] = args.rerank
        if args.abstention is not None:
            overrides["abstention_enabled"] = args.abstention

        if args.retriever == "legacy":
            retriever = LegacyRetriever(agents_dir, "rag_eval_agent", embedder, ids)
        else:
            retriever = CurrentRetriever(agents_dir, "rag_eval_agent", embedder,
                                         ids, **overrides)

        generator = None
        if args.generate:
            from rag_eval.judge import Generator
            system_prompt = None
            if args.retriever == "legacy":
                from rag_eval.legacy_baseline import LEGACY_SYSTEM_PROMPT
                system_prompt = LEGACY_SYSTEM_PROMPT
            generator = Generator(judge=args.judge, model=args.gen_model,
                                  judge_model=args.judge_model,
                                  provider=args.gen_provider,
                                  judge_provider=args.judge_provider,
                                  system_prompt=system_prompt)

        rows = []
        for case in cases:
            outcome = retriever.run(case.question)
            generated = generator.answer(case, outcome) if generator else None
            rows.append(_score_case(case, outcome, generated))
            if args.verbose:
                print(f"  {case.id} {case.category:<15} "
                      f"hits={len(outcome.hits)} "
                      f"top={outcome.hits[0].doc if outcome.hits else '-'} "
                      f"abstain={outcome.abstained}")
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)

    if cache.save():
        print(f"[vectors] cache extended -> {args.vector_cache} ({len(cache)} vectors)")

    return {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "label": args.label or args.retriever,
        "config": {
            "retriever": args.retriever,
            "mode": "live" if args.fresh else ("live-cached" if args.live else "frozen"),
            "latency_is_real": bool(args.fresh),
            "embed_model": model,
            "chunk_words": args.chunk_words or "default",
            "documents": len(ids),
            "chunks": n_chunks,
            "dataset": str(Path(args.dataset).name),
            "overrides": overrides,
            "generation": bool(args.generate),
            "judge": bool(args.judge),
            "embedding_api_calls": embedder.api_calls,
        },
        "summary": _agg(rows),
        "cases": rows,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default=str(ds.DEFAULT_DATASET))
    ap.add_argument("--corpus", default=str(CORPUS_DIR))
    ap.add_argument("--vector-cache", default=str(VECTOR_CACHE))
    # The platform default since RUNBOOK §57. The frozen cache holds vectors
    # for BOTH models, so `--embed-model text-embedding-3-small` still runs
    # offline for a rollback comparison.
    ap.add_argument("--embed-model", default=os.environ.get(
        "RAG_EMBED_MODEL", "gemini-embedding-2"))
    ap.add_argument("--retriever", choices=("current", "legacy"), default="current")
    ap.add_argument("--live", action="store_true",
                    help="call the real embedding API on cache misses")
    ap.add_argument("--fresh", action="store_true",
                    help="bypass the vector cache entirely — the only mode whose "
                         "latency numbers are real (implies --live)")
    ap.add_argument("--generate", action="store_true",
                    help="also generate answers from the retrieved context")
    ap.add_argument("--judge", action="store_true",
                    help="LLM-judge faithfulness/relevance (implies --generate)")
    ap.add_argument("--gen-model", default=None)
    ap.add_argument("--judge-model", default=None)
    ap.add_argument("--gen-provider", choices=("llm", "openai"), default="llm",
                    help="llm = the endpoint the live call path uses (default)")
    ap.add_argument("--judge-provider", choices=("llm", "openai"), default=None)
    ap.add_argument("--chunk-words", type=int, default=None)
    ap.add_argument("--top-k", type=int, default=None)
    ap.add_argument("--final-k", type=int, default=None)
    ap.add_argument("--rerank", type=lambda s: s.lower() in ("1", "true", "yes", "on"),
                    default=None)
    ap.add_argument("--abstention", type=lambda s: s.lower() in ("1", "true", "yes", "on"),
                    default=None)
    ap.add_argument("--out", default=None, help="write the JSON report here")
    ap.add_argument("--label", default=None)
    ap.add_argument("--compare", default=None, help="baseline JSON to check against")
    ap.add_argument("--gate", action="store_true",
                    help="exit non-zero when --compare finds a regression")
    ap.add_argument("--save-baseline", default=None,
                    help="also write the report to baselines/<name>.json")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    if args.fresh:
        args.live = True
    if args.judge:
        args.generate = True

    report = run(args)
    print(render(report))

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
        print(f"\n[report] {args.out}")
    if args.save_baseline:
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        dest = BASELINE_DIR / f"{args.save_baseline}.json"
        dest.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        print(f"[baseline] {dest}")

    if args.compare:
        base = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        rows, ok = compare(report, base)
        print("\n" + render_compare(rows, ok, Path(args.compare).name))
        if args.gate and not ok:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
