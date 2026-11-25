"""Retrieval and answer metrics.

Retrieval is scored INDEPENDENTLY of generation — the whole point is to be able
to say whether a bad answer came from a retrieval failure or a generation
failure, which a single end-to-end score can never tell you.

Relevance is derived per run: a retrieved chunk is relevant to a case when it
comes from an expected document AND contains that expected source's anchor
phrase. Graded relevance (2 = primary, 1 = supporting) feeds NDCG; everything
else uses the binary "relevance > 0".
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .dataset import GoldenCase, normalize


@dataclass
class RetrievedChunk:
    """One retrieval hit, normalised across pipelines so old and new compare."""
    doc: str
    chunk_id: int
    text: str
    score: float
    rank: int
    stage_scores: dict[str, float] | None = None


def grade(case: GoldenCase, chunk: RetrievedChunk) -> int:
    """Graded relevance of one chunk to one case (0 = irrelevant)."""
    body = normalize(chunk.text)
    best = 0
    for src in case.expected_sources:
        if src.doc != chunk.doc:
            continue
        if normalize(src.anchor) in body:
            best = max(best, src.relevance)
    return best


def recall_at_k(case: GoldenCase, hits: list[RetrievedChunk], k: int) -> float | None:
    """Fraction of the case's distinct expected sources found in the top k.

    None for cases with no expected sources (the abstention cases) — averaging
    a 0 or a 1 for them would silently move the retrieval score for a reason
    that has nothing to do with retrieval.
    """
    if not case.expected_sources:
        return None
    found = set()
    for chunk in hits[:k]:
        for i, src in enumerate(case.expected_sources):
            if src.doc == chunk.doc and normalize(src.anchor) in normalize(chunk.text):
                found.add(i)
    return len(found) / len(case.expected_sources)


def precision_at_k(case: GoldenCase, hits: list[RetrievedChunk], k: int) -> float | None:
    if not case.expected_sources:
        return None
    top = hits[:k]
    if not top:
        return 0.0
    return sum(1 for c in top if grade(case, c) > 0) / len(top)


def reciprocal_rank(case: GoldenCase, hits: list[RetrievedChunk]) -> float | None:
    if not case.expected_sources:
        return None
    for i, chunk in enumerate(hits, 1):
        if grade(case, chunk) > 0:
            return 1.0 / i
    return 0.0


def ndcg_at_k(case: GoldenCase, hits: list[RetrievedChunk], k: int) -> float | None:
    """Graded NDCG with the standard 2^rel - 1 gain.

    Each expected source may be credited ONCE. Chunking overlaps by design (60
    words), so the same anchor phrase legitimately appears in two adjacent
    chunks; counting both as fresh gain made NDCG exceed 1.0 — which is how
    this was caught — and would have rewarded a retriever for returning the
    same passage twice.
    """
    if not case.expected_sources:
        return None
    credited: set[int] = set()
    dcg = 0.0
    for i, chunk in enumerate(hits[:k], 1):
        body = normalize(chunk.text)
        gain = 0
        for j, src in enumerate(case.expected_sources):
            if j in credited or src.doc != chunk.doc:
                continue
            if normalize(src.anchor) in body:
                gain = max(gain, src.relevance)
                credited.add(j)
        if gain:
            dcg += (2 ** gain - 1) / math.log2(i + 1)
    ideal = sorted((s.relevance for s in case.expected_sources), reverse=True)[:k]
    idcg = sum((2 ** r - 1) / math.log2(i + 1) for i, r in enumerate(ideal, 1))
    return (dcg / idcg) if idcg else None


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile. Empty -> 0.0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, math.ceil(p / 100 * len(ordered)) - 1)
    return ordered[idx]


def mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return (sum(present) / len(present)) if present else None


def citation_accuracy(citations: list[dict], context_chunks: list[RetrievedChunk]
                      ) -> float | None:
    """Fraction of returned citations that map to a chunk actually given to the LLM.

    This is an integrity check, not a relevance check: a citation naming
    evidence the model never saw is a fabricated citation regardless of whether
    the answer happens to be right.
    """
    if not citations:
        return None
    supplied = {(c.doc, c.chunk_id) for c in context_chunks}
    ok = sum(1 for c in citations
             if (c.get("document_id"), c.get("chunk_id")) in supplied)
    return ok / len(citations)


def unsupported_claims(answer: str, case: GoldenCase) -> list[str]:
    """Forbidden claims that literally appear in the answer.

    A cheap, deterministic hallucination tripwire that needs no judge model:
    each golden case names phrases that can only come from outside the corpus.

    It is a tripwire, not the faithfulness metric — substring matching cannot
    tell an assertion from a denial. "I don't have information on the number of
    employees" contains "employees" while claiming nothing, and counting that
    as a hallucination overstates the very thing this work is trying to
    measure. Answers that read as a refusal are therefore exempt, and the
    golden cases' forbidden lists avoid words the question itself supplies.
    The LLM judge's `faithfulness` is the primary measure.
    """
    from .judge_markers import looks_like_abstention
    if looks_like_abstention(answer):
        return []
    body = normalize(answer)
    return [claim for claim in case.forbidden_claims if normalize(claim) in body]


def required_facts_present(answer: str, case: GoldenCase) -> float | None:
    """Fraction of required facts the answer states.

    A required fact may be a LIST of acceptable variants, satisfied by any one
    of them. Without that, this measures phrasing rather than correctness: an
    answer saying the system "runs entirely on your own infrastructure" — the
    corpus's own words — scored zero against the single literal "self-hosted",
    which would have been reported as a quality regression when nothing had
    regressed.
    """
    if not case.required_facts:
        return None
    body = normalize(answer)
    hits = 0
    for fact in case.required_facts:
        variants = fact if isinstance(fact, list) else [fact]
        if any(normalize(v) in body for v in variants):
            hits += 1
    return hits / len(case.required_facts)


def contamination(case: GoldenCase, hits: list[RetrievedChunk]) -> float | None:
    """Fraction of supplied evidence that comes from a document that should
    have no part in this answer.

    The sharp form of the failure precision@k only hints at: an agent quoting
    fabric prices on a real-estate call. Precision counts a chunk as bad when
    it lacks the anchor — including a chunk of the RIGHT document that simply
    isn't the answering passage, which is harmless padding. Contamination
    counts only chunks from the WRONG DOCUMENT, which is the one that puts a
    confident, irrelevant fact in front of a caller.

    None for cases with no expected sources: when the right answer is to
    abstain, "which document did it come from" is the wrong question — that is
    what the abstention metrics measure.
    """
    if not case.expected_sources or not hits:
        return None
    expected = case.expected_docs
    return sum(1 for c in hits if c.doc not in expected) / len(hits)
