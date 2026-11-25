"""Stage 2 — embeddings + FTS chunks for hybrid semantic search.

Chunks the speaker-labeled transcript turn-wise (~600 tokens per chunk, one
turn of overlap so commitments spanning a boundary stay findable), embeds
chunks + the AI summary via the OpenAI-compat /embeddings endpoint
(gemini-embedding-001, 768-dim Matryoshka), and stores rows in
call_embeddings. The tsv FTS column is generated in-database.

Vectors are L2-normalized before storage: truncated Matryoshka embeddings are
not unit-length, and normalizing makes cosine and inner-product retrieval
equivalent.
"""

from __future__ import annotations

import math
import re
from typing import Any

from shared.db.database import get_db
from shared.intelligence import config

# A turn header is an elapsed stamp plus any annotations call_audit.py appends
# in brackets — today "[spoke 7.26-9.98s]", the live-VAD span written for CALLER
# turns only (commit bacbd82, 2026-08-04). Those groups MUST stay optional and
# repeatable: this is a finditer, so a header it fails to match is not an error
# anywhere, the turn is simply skipped. When the span shipped, that silently
# deleted 76-99% of caller turns per call from the Intelligence transcript, the
# embeddings and semantic search, while agent turns (which carry no span) stayed
# intact — so every affected call read as an agent answering nothing.
# Minutes are \d+ because a call past 99 minutes writes three digits.
_TURN_RE = re.compile(
    r"\[(\d+:\d{2}(?:\.\d{3})?)\](?:[ \t]*\[[^\]]*\])*[ \t]*\n"
    r"(Agent|Caller):\s*(.+?)(?=\n\[|\Z)",
    re.S)

# chars ≈ 4 × tokens
_CHUNK_CHARS = 4 * 600
_MIN_CHUNK_CHARS = 20


def parse_turns(transcript: str) -> list[tuple[str, str, str]]:
    """[(timestamp, speaker, text)] from the platform transcript format."""
    turns = []
    for m in _TURN_RE.finditer(transcript or ""):
        text = re.sub(r"\s+", " ", m.group(3)).strip()
        if text:
            turns.append((m.group(1), m.group(2), text))
    return turns


def chunk_turns(turns: list[tuple[str, str, str]],
                chunk_chars: int = _CHUNK_CHARS) -> list[str]:
    """Greedy turn-wise chunking with one-turn overlap."""
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for i, (ts, speaker, text) in enumerate(turns):
        line = f"[{ts}] {speaker}: {text}"
        if cur and size + len(line) > chunk_chars:
            chunks.append("\n".join(cur))
            cur = [cur[-1]]  # one-turn overlap
            size = len(cur[0])
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunk = "\n".join(cur)
        if len(chunk) >= _MIN_CHUNK_CHARS:
            chunks.append(chunk)
    return chunks


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch-embed via the OpenAI-compat endpoint; isolated for tests."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url=config.EMBED_BASE_URL,
                         api_key=config.EMBED_API_KEY, timeout=60)
    try:
        out: list[list[float]] = []
        # modest batches — the endpoint caps request size
        for i in range(0, len(texts), 16):
            resp = await client.embeddings.create(
                model=config.EMBED_MODEL,
                input=texts[i:i + 16],
                dimensions=config.EMBED_DIM,
            )
            data = list(resp.data)
            # Gemini's OpenAI-compat endpoint returns index=null; only reorder
            # when real indices are present, else trust the response order.
            if all(getattr(d, "index", None) is not None for d in data):
                data.sort(key=lambda d: d.index)
            out.extend(_normalize(d.embedding) for d in data)
        return out
    finally:
        await client.close()


async def embed_query(text: str) -> list[float]:
    return (await embed_texts([text]))[0]


def store_embeddings(call: dict[str, Any], chunks: list[str],
                     vectors: list[list[float]],
                     summary: str | None, summary_vec: list[float] | None) -> None:
    call_id = call["id"]
    owner = call.get("owner") or ""
    with get_db() as db:
        db.execute("DELETE FROM call_embeddings WHERE call_id=?", (call_id,))
        for idx, (chunk, vec) in enumerate(zip(chunks, vectors)):
            db.execute(
                """INSERT INTO call_embeddings
                       (call_id, owner, kind, chunk_index, content, embedding)
                   VALUES (?, ?, 'chunk', ?, ?, ?::vector)""",
                (call_id, owner, idx, chunk, _vec_literal(vec)),
            )
        if summary and summary_vec:
            db.execute(
                """INSERT INTO call_embeddings
                       (call_id, owner, kind, chunk_index, content, embedding)
                   VALUES (?, ?, 'summary', 0, ?, ?::vector)""",
                (call_id, owner, summary, _vec_literal(summary_vec)),
            )


async def stage_embed(call: dict[str, Any]) -> None:
    if not config.EMBED_ENABLED or not config.EMBED_API_KEY:
        return  # stage skipped by config; search degrades to lexical-only
    turns = parse_turns(call.get("transcript") or "")
    chunks = chunk_turns(turns)

    summary = None
    with get_db() as db:
        row = db.execute(
            "SELECT summary FROM call_intelligence WHERE call_id=?", (call["id"],)
        ).fetchone()
        if row:
            summary = row["summary"]

    if not chunks and not summary:
        store_embeddings(call, [], [], None, None)
        return

    texts = chunks + ([summary] if summary else [])
    vectors = await embed_texts(texts)
    chunk_vecs = vectors[:len(chunks)]
    summary_vec = vectors[len(chunks)] if summary else None
    store_embeddings(call, chunks, chunk_vecs, summary, summary_vec)
