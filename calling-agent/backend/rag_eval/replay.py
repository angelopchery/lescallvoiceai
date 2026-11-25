"""Replay REAL caller turns through the retrieval pipeline.

The golden set measures quality. It cannot measure two things that only a real
query distribution decides:

  * latency at production shape — golden questions are well-formed and roughly
    uniform in length; real turns are short, code-mixed, full of fillers, and
    a third of them are not questions at all;
  * cache hit rate — 37 deliberately distinct questions can only ever miss.
    Repetition is a property of real callers, not of a benchmark, so a hit rate
    quoted from the golden set would be a fabricated number.

So this pulls actual caller turns out of the call transcripts in Postgres and
runs them through the same pipeline.

    ../venv/bin/python -m rag_eval.replay --limit 300 --live

`--live` makes the embedding calls real, which is the only mode whose latency
means anything. Without it the run is offline and only the retrieval mix
(abstention rate, candidate counts, cache tiers) is meaningful.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for p in (str(_HERE.parent), str(_HERE.parents[2])):
    if p not in sys.path:
        sys.path.insert(0, p)

from rag_eval import metrics as M                            # noqa: E402
from rag_eval import runner                                  # noqa: E402
from rag_eval.harness import build_index                     # noqa: E402
from rag_eval.vectors import CachedEmbedder, EmbeddingCache  # noqa: E402


def load_caller_turns(limit: int) -> list[str]:
    """Caller utterances from real call transcripts, newest first.

    Parsed with the platform's own transcript parser so a format change cannot
    make this silently harvest nothing (which is exactly what happened to the
    intelligence pipeline when the live-VAD span was added to turn headers).
    """
    from shared.db.database import get_db
    from shared.intelligence.embed import parse_turns

    turns: list[str] = []
    with get_db() as db:
        rows = db.execute(
            "SELECT transcript FROM calls WHERE transcript IS NOT NULL "
            "AND length(transcript) > 50 ORDER BY id DESC LIMIT 400").fetchall()
    for row in rows:
        for _ts, speaker, text in parse_turns(row["transcript"]):
            if speaker == "Caller" and text.strip():
                turns.append(text.strip())
        if len(turns) >= limit * 3:
            break
    # Sequential, not sampled: repetition between consecutive turns of a call
    # ("haan", "kitna hai", a re-asked question after a mishear) is exactly
    # what a cache hit rate is measuring, and random sampling destroys it.
    return turns[:limit]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--live", action="store_true",
                    help="real embedding API calls — required for real latency")
    ap.add_argument("--no-cache", action="store_true",
                    help="measure the pipeline with the semantic cache disabled")
    ap.add_argument("--retriever", choices=("current", "legacy"), default="current",
                    help="legacy replays the same turns through the pre-upgrade "
                         "algorithm — how often it injected context is the "
                         "before-number for irrelevant-context rate")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    runner._load_env()
    turns = load_caller_turns(args.limit)
    if not turns:
        print("no caller turns found in the calls table", file=sys.stderr)
        return 2
    print(f"replaying {len(turns)} real caller turns "
          f"({'live embeddings' if args.live else 'cached vectors'})")

    from app.documents import cache as cache_mod
    from app.documents import retrieval

    model = os.environ.get("RAG_EMBED_MODEL", "text-embedding-3-small")
    cache = EmbeddingCache(runner.VECTOR_CACHE)
    embedder = CachedEmbedder(model, cache, live_fn=runner._LiveEmbedFn(model),
                              live=args.live, bypass=args.live)

    workdir = Path(tempfile.mkdtemp(prefix="rag_replay_"))
    cache_mod.reset_cache()
    try:
        ids = build_index(workdir, runner.CORPUS_DIR, embedder)
        agents_dir = workdir / "agents"
        from app.documents import embeddings as emb_mod
        from app.documents import store as store_mod
        from rag_eval.harness import _ProviderShim, _embedder_installed
        store_mod._AGENTS_DIR = agents_dir
        emb_mod._AGENTS_DIR = agents_dir
        retrieval._index_cache.clear()

        rows = []
        legacy = None
        if args.retriever == "legacy":
            from rag_eval.legacy_baseline import legacy_retrieve
            legacy = lambda q: legacy_retrieve(agents_dir, "rag_eval_agent", q,
                                               embedder, ids)
        with _embedder_installed(emb_mod, embedder):
            for i, turn in enumerate(turns):
                t0 = time.perf_counter()
                if legacy is not None:
                    out = legacy(turn)
                    row = {"cache_hit": False, "cache_tier": "",
                           "abstained": out.abstained,
                           "reason": out.abstain_reason,
                           "returned": len(out.hits),
                           "embed_tokens": 0, "embed_cost": None}
                else:
                    res = retrieval.retrieve("rag_eval_agent", turn,
                                             use_cache=not args.no_cache,
                                             call_id=f"replay-{i}", turn_index=i)
                    row = {"cache_hit": res.cache_hit, "cache_tier": res.cache_tier,
                           "abstained": res.abstained, "reason": res.abstain_reason,
                           "returned": len(res.chunks),
                           "embed_tokens": res.embed_tokens,
                           "embed_cost": res.embed_cost_inr}
                row.update({"chars": len(turn),
                            "ms": (time.perf_counter() - t0) * 1000})
                rows.append(row)
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
    cache.save()

    lat = [r["ms"] for r in rows]
    hits = [r for r in rows if r["cache_hit"]]
    exact = [r for r in hits if r["cache_tier"] == "exact"]
    stats = cache_mod.get_cache().stats.as_dict()
    embed_calls = sum(1 for r in rows if r["embed_tokens"])
    tokens = sum(r["embed_tokens"] for r in rows)
    costs = [r["embed_cost"] for r in rows if r["embed_cost"] is not None]
    reasons: dict[str, int] = {}
    for r in rows:
        if r["abstained"]:
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1

    out = {
        "turns": len(rows),
        "retriever": args.retriever,
        "mode": "live" if args.live else "cached-vectors",
        "cache_enabled": not args.no_cache,
        "latency_ms": {p: round(M.percentile(lat, v), 2)
                       for p, v in (("p50", 50), ("p90", 90), ("p95", 95), ("p99", 99))},
        "latency_max_ms": round(max(lat), 2),
        "cache": {
            "hit_rate": round(len(hits) / len(rows), 4),
            "exact_hit_rate": round(len(exact) / len(rows), 4),
            "embed_calls_saved": stats["embed_calls_saved"],
            "embed_tokens_saved": stats["embed_tokens_saved"],
        },
        "retrieval": {
            "abstention_rate": round(sum(1 for r in rows if r["abstained"]) / len(rows), 4),
            # The number that matters on out-of-domain traffic: how often
            # unrelated document text was pushed into the prompt anyway.
            "context_injected_rate": round(
                sum(1 for r in rows if r["returned"]) / len(rows), 4),
            "abstain_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
            "avg_returned": round(sum(r["returned"] for r in rows) / len(rows), 2),
        },
        "cost": {
            "embedding_requests": embed_calls,
            "embed_tokens_total": tokens,
            "embed_tokens_per_turn": round(tokens / len(rows), 2),
            "embed_cost_inr_total": round(sum(costs), 8) if costs else None,
            "embed_cost_inr_per_turn": round(sum(costs) / len(rows), 10) if costs else None,
        },
    }
    print(json.dumps(out, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": out, "rows": rows},
                                             indent=2), encoding="utf-8")
        print(f"[replay] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
