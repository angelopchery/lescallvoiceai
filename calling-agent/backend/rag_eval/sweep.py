"""Parameter sweep — pick retrieval settings from measurements, not taste.

    ../venv/bin/python -m rag_eval.sweep --live

Runs the golden evaluation across a grid of chunk sizes, K values, re-ranker
settings and lexical weights, and prints one line per configuration. Every
default in `app/documents/config.py` that could have been an arbitrary number
is expected to be traceable to a row of this table.

`--live` is needed whenever the grid changes chunking, because different chunk
text means embeddings the frozen cache has never seen.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from rag_eval import runner  # noqa: E402

GRID = {
    "chunk_words": [400, 250, 150],
    "final_k": [3, 5],
    "rerank": [True, False],
    "mmr": [1.0, 0.75],
    "lexical_weight": [1.0, 0.5],
}


def _args(**kw):
    ns = argparse.Namespace(
        dataset=str(runner.ds.DEFAULT_DATASET), corpus=str(runner.CORPUS_DIR),
        vector_cache=str(runner.VECTOR_CACHE),
        embed_model=os.environ.get("RAG_EMBED_MODEL", "text-embedding-3-small"),
        retriever="current", live=kw.pop("live", False), fresh=False,
        generate=False, judge=False, gen_model=None, judge_model=None,
        chunk_words=None, top_k=None, final_k=None, rerank=None,
        abstention=None, out=None, label=None, compare=None, gate=False,
        save_baseline=None, verbose=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--quick", action="store_true",
                    help="only vary re-ranker settings at the default chunking")
    a = ap.parse_args(argv)

    grid = dict(GRID)
    if a.quick:
        grid["chunk_words"] = [None]
    keys = list(grid)
    rows = []
    header = (f"{'chunk':>6}{'K':>3}{'rrf':>5}{'mmr':>6}{'lexw':>6}  "
              f"{'R@1':>5}{'R@3':>6}{'R@5':>6}{'P@3':>6}{'MRR':>6}{'NDCG':>6}"
              f"{'abst':>6}{'over':>6}")
    print(header)
    print("-" * len(header))
    for combo in itertools.product(*(grid[k] for k in keys)):
        cfg = dict(zip(keys, combo))
        if not cfg["rerank"] and cfg["lexical_weight"] != 1.0:
            continue          # lexical weight is meaningless with fusion off
        if not cfg["rerank"] and cfg["mmr"] != 1.0:
            continue          # MMR only runs inside the fusion path
        os.environ["RAG_MMR_LAMBDA"] = str(cfg["mmr"])
        os.environ["RAG_LEXICAL_WEIGHT"] = str(cfg["lexical_weight"])
        import importlib
        from app.documents import config as dcfg
        importlib.reload(dcfg)
        for mod in ("app.documents.rerank", "app.documents.retrieval",
                    "app.documents.cache", "app.documents.embeddings",
                    "app.documents.store", "app.documents.grounding"):
            importlib.reload(importlib.import_module(mod))

        rep = runner.run(_args(live=a.live, chunk_words=cfg["chunk_words"],
                               final_k=cfg["final_k"], rerank=cfg["rerank"]))
        s = rep["summary"]
        r, ab = s["retrieval"], s["abstention"]
        rows.append({**cfg, "chunks": rep["config"]["chunks"], **r, **ab})

        def f(v):
            return "  n/a" if v is None else f"{v:.2f}"
        print(f"{str(cfg['chunk_words'] or 400):>6}{cfg['final_k']:>3}"
              f"{str(cfg['rerank'])[0]:>5}{cfg['mmr']:>6}{cfg['lexical_weight']:>6}  "
              f"{f(r['recall@1']):>5}{f(r['recall@3']):>6}{f(r['recall@5']):>6}"
              f"{f(r['precision@3']):>6}{f(r['mrr']):>6}{f(r['ndcg@5']):>6}"
              f"{f(ab['correct_abstention_rate']):>6}{f(ab['over_abstention_rate']):>6}")

    if a.out:
        Path(a.out).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"\n[sweep] {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
