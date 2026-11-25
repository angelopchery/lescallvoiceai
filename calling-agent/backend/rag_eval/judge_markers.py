"""Abstention detection — how "the system declined" is recognised in text.

Its own module because both the scorer (`metrics`) and the generator (`judge`)
need it, and `metrics` must stay importable without an LLM client or an API key
so the offline evaluation keeps running with no credentials.

The markers cover both halves of the platform's abstention vocabulary: the
literal refusal the evaluation prompt asks for, and the "I'll check with the
team and follow up" deflection the live agent is instructed to use instead of a
flat refusal (see `documents/grounding.py`).
"""

from __future__ import annotations

ABSTAIN_MARKERS = (
    "don't have that in my sources", "do not have that in my sources",
    "not available in the provided sources", "not in the provided sources",
    "check with the team", "i'll check", "i will check", "let me check",
    "don't have that information", "don't have information",
    "don't have specific information", "do not have specific information",
    "not in my sources", "no information", "don't have details",
    "get back to you", "follow up with",
)


def looks_like_abstention(answer: str) -> bool:
    body = (answer or "").casefold()
    return any(m in body for m in ABSTAIN_MARKERS)
