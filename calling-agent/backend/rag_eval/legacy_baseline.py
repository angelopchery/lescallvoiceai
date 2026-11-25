"""The pre-upgrade retrieval algorithm, frozen so before/after stays re-runnable.

This is a faithful copy of `documents/embeddings.retrieve_context` /
`_semantic_search` as they stood at commit 5a3560d (2026-08-11):

    query -> one embedding call -> cosine against every chunk of every document
    -> keep score >= 0.25 -> sort -> take 3 -> join into a text block.

No re-ranking, no lexical arm, no cache, no abstention, no scores or ids
returned to the caller. It is kept here rather than in git history alone
because a baseline you cannot re-execute is a number you have to take on
trust — this one can be re-measured against any future corpus or embedder.

The ONE deliberate difference: this copy carries the document id and chunk
index alongside each hit. The original discarded them (which is why it could
not cite anything); the ranking is unchanged, the ids only make the result
scoreable.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from .harness import RetrievalOutcome
from .metrics import RetrievedChunk

LEGACY_TOP_K = 3
LEGACY_MIN_SIM = 0.25

# The generation half of "before". The old path had no grounding contract at
# all: the only instruction attached to retrieved evidence was the trailer
# inside the context block itself ("weave relevant facts naturally into your
# response"), and nothing anywhere told the model what to do when the evidence
# did not cover the question. Measuring the old retriever against the NEW
# system prompt would credit the old pipeline with the fix, so the before-run
# uses this.
LEGACY_SYSTEM_PROMPT = (
    "You are a helpful business agent on a phone call. Answer the caller's "
    "question. Keep it to one or two spoken sentences."
)


def legacy_retrieve(agents_dir: Path, agent_folder: str, query: str,
                    embedder, id_to_doc: dict[str, str]) -> RetrievalOutcome:
    to_name = {v: k for k, v in id_to_doc.items()}
    docs_dir = agents_dir / agent_folder / "documents"
    index_path = docs_dir / "index.json"
    out = RetrievalOutcome(question=query)
    if not index_path.exists():
        return out
    docs = json.loads(index_path.read_text(encoding="utf-8"))

    t0 = time.perf_counter()
    query_vec = embedder.embed([query])[0]
    out.stage_ms["embed"] = (time.perf_counter() - t0) * 1000

    t1 = time.perf_counter()
    q_norm = query_vec / (np.linalg.norm(query_vec) + 1e-10)
    scored: list[tuple[float, str, int, str]] = []
    for doc in docs:
        doc_id = doc["doc_id"]
        d = docs_dir / doc_id
        if not (d / "embeddings.npy").exists() or not (d / "chunks.json").exists():
            continue
        emb = np.load(str(d / "embeddings.npy"))
        chunks = json.loads((d / "chunks.json").read_text(encoding="utf-8"))
        norms = np.linalg.norm(emb, axis=1, keepdims=True) + 1e-10
        scores = (emb / norms) @ q_norm
        for idx, score in enumerate(scores):
            if float(score) >= LEGACY_MIN_SIM and idx < len(chunks):
                scored.append((float(score), doc_id, idx, chunks[idx]["text"]))
    scored.sort(key=lambda r: -r[0])
    out.stage_ms["search"] = (time.perf_counter() - t1) * 1000

    out.candidates = [
        RetrievedChunk(doc=to_name.get(doc_id, doc_id), chunk_id=idx, text=text,
                       score=score, rank=i)
        for i, (score, doc_id, idx, text) in enumerate(scored, 1)]
    out.hits = out.candidates[:LEGACY_TOP_K]

    if out.hits:
        lines = ["━━ DOCUMENT CONTEXT ━━"]
        lines += [f"[Knowledge] {c.text.strip()}" for c in out.hits]
        lines.append(
            "Above context is from uploaded business documents. "
            "Weave relevant facts naturally into your response — do not quote "
            "or cite them directly.")
        out.context = "\n".join(lines)
    # The legacy path had no notion of abstaining and produced no citations.
    out.abstained = not out.hits
    out.abstain_reason = "no chunk above the 0.25 floor" if not out.hits else ""
    return out
