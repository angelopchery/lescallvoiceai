"""Golden dataset loading + validation.

A golden case names its relevant evidence by DOCUMENT + ANCHOR PHRASE, never by
chunk index. That is deliberate: chunk size is itself a tuned RAG parameter
(`RAG_CHUNK_WORDS`), and a dataset keyed on chunk ids silently invalidates
itself the moment anyone retunes chunking — the metrics keep computing, they
just stop meaning anything. An anchor is a verbatim phrase from the source
document, so "which chunk is relevant" is re-derived from the live index on
every run and stays correct across any chunking.

`validate()` fails loudly if an anchor no longer appears in its document. That
is the tripwire for corpus drift: it is much better to fail the eval than to
score a retriever against evidence that no longer exists.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
GOLDEN_DIR = _HERE / "golden"
DEFAULT_DATASET = GOLDEN_DIR / "docs_rag.jsonl"

# Categories that assert the system must NOT produce a grounded answer.
ABSTAIN_CATEGORIES = frozenset({"no_answer", "ambiguous"})


def normalize(text: str) -> str:
    """Whitespace-collapsed, case-folded — how anchors are matched.

    Chunking joins on single spaces, so a raw-file anchor spanning a newline
    would never match its own chunk without this.
    """
    return re.sub(r"\s+", " ", (text or "")).strip().casefold()


@dataclass(frozen=True)
class ExpectedSource:
    doc: str
    anchor: str
    relevance: int = 2      # graded for NDCG: 2 = primary evidence, 1 = supporting


@dataclass
class GoldenCase:
    id: str
    question: str
    expected_answer: str
    expected_sources: list[ExpectedSource]
    category: str
    difficulty: str
    answerable: bool
    required_facts: list[str] = field(default_factory=list)
    forbidden_claims: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def must_abstain(self) -> bool:
        """True when a grounded answer is itself the failure."""
        return not self.answerable

    @property
    def expected_docs(self) -> set[str]:
        return {s.doc for s in self.expected_sources}


def load(path: Path | str = DEFAULT_DATASET) -> list[GoldenCase]:
    cases: list[GoldenCase] = []
    seen: set[str] = set()
    for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: invalid JSON — {exc}") from exc
        cid = raw["id"]
        if cid in seen:
            raise ValueError(f"{path}:{lineno}: duplicate case id {cid!r}")
        seen.add(cid)
        cases.append(GoldenCase(
            id=cid,
            question=raw["question"],
            expected_answer=raw.get("expected_answer", ""),
            expected_sources=[
                ExpectedSource(doc=s["doc"], anchor=s["anchor"],
                               relevance=int(s.get("relevance", 2)))
                for s in raw.get("expected_sources", [])
            ],
            category=raw.get("category", "uncategorised"),
            difficulty=raw.get("difficulty", "medium"),
            answerable=bool(raw.get("answerable", True)),
            required_facts=list(raw.get("required_facts", [])),
            forbidden_claims=list(raw.get("forbidden_claims", [])),
            metadata=dict(raw.get("metadata", {})),
        ))
    return cases


def validate(cases: list[GoldenCase], corpus: dict[str, str]) -> list[str]:
    """Return a list of problems; empty means the dataset is coherent.

    `corpus` maps document name -> full text.
    """
    problems: list[str] = []
    for case in cases:
        if case.answerable and not case.expected_sources:
            problems.append(f"{case.id}: answerable case with no expected_sources")
        if case.must_abstain and case.expected_sources:
            problems.append(
                f"{case.id}: category {case.category!r} must abstain but names sources")
        for src in case.expected_sources:
            if src.doc not in corpus:
                problems.append(f"{case.id}: unknown document {src.doc!r}")
                continue
            if normalize(src.anchor) not in normalize(corpus[src.doc]):
                problems.append(
                    f"{case.id}: anchor {src.anchor!r} no longer occurs in {src.doc!r} "
                    "(corpus drifted, or the anchor was mistyped)")
    return problems


def load_corpus(corpus_dir: Path | str) -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8")
            for p in sorted(Path(corpus_dir).glob("*.txt"))}
