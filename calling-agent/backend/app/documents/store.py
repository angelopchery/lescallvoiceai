"""
Document storage: save / list / delete per-agent documents.
Layout: agents/<agent_folder>/documents/<doc_id>/{metadata.json, text.txt, chunks.json}
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
import uuid
import yaml
from datetime import datetime
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

_AGENTS_DIR = Path(__file__).resolve().parents[3] / "agents"

# Chunk geometry is a tuned retrieval parameter, not a constant: it decides how
# much unrelated text rides along with every answer (context cost on every turn
# of the call) and how often the sentence that answers a question is split
# across two chunks. Configured via RAG_CHUNK_WORDS / RAG_CHUNK_OVERLAP and
# swept by `rag_eval`. Changing it changes the stored chunks, so documents
# embedded under the old geometry are reported as stale until re-indexed.
_CHUNK_WORDS = config.CHUNK_WORDS
_CHUNK_OVERLAP = config.CHUNK_OVERLAP

# Cache: agent_id -> folder name (populated on first lookup per agent_id).
# "" is a cached MISS, timestamped in _miss_at and expiring after _MISS_TTL_S.
_id_to_folder: dict[str, str] = {}
_miss_at: dict[str, float] = {}
_MISS_TTL_S = 60.0


# ---------------------------------------------------------------------------
# Folder resolution
# ---------------------------------------------------------------------------

def get_folder_by_agent_id(agent_id: str) -> str:
    """Return the agents/ subfolder name whose agent.yaml matches agent_id.

    Cached BOTH ways. A miss costs a `yaml.safe_load` of every agent.yaml on
    disk — 44 files, measured at ~1.4s — and this is called on the LLM path of
    every turn. Caching only hits meant an agent_id that resolves to nothing
    (the neutral fallback, an agent whose YAML was renamed) re-ran that scan on
    the event loop on EVERY TURN, blocking audio for over a second each time.
    The negative entry is what makes the miss cost once per process instead.
    """
    hit = _id_to_folder.get(agent_id)
    if hit:
        return hit
    if hit == "" and agent_id in _id_to_folder:
        # Negative entries expire. Caching a miss forever would mean an agent
        # created or renamed after this process started could never resolve its
        # documents again; expiring it caps the rescan at once per interval
        # instead of once per turn, and a new agent becomes visible within it.
        if (time.monotonic() - _miss_at.get(agent_id, 0.0)) < _MISS_TTL_S:
            return ""
    for p in _AGENTS_DIR.glob("*/agent.yaml"):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if data.get("agent_id") == agent_id:
                _id_to_folder[agent_id] = p.parent.name
                return p.parent.name
        except Exception:
            pass
    _id_to_folder[agent_id] = ""      # negative result — do not rescan per turn
    _miss_at[agent_id] = time.monotonic()
    return ""


def forget_agent_folders() -> None:
    """Drop the id→folder cache. For tests, and for a caller that has just
    created or renamed an agent and does not want to wait out the miss TTL."""
    _id_to_folder.clear()
    _miss_at.clear()


def _docs_dir(agent_folder: str) -> Path:
    return _AGENTS_DIR / agent_folder / "documents"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def save_document(
    agent_folder: str,
    filename: str,
    content: bytes,
    extracted_text: str,
    org_name: str,
    doc_type: str,
    description: str,
) -> dict:
    """Persist a document and its chunks; return its metadata dict."""
    doc_id = uuid.uuid4().hex[:10]
    doc_dir = _docs_dir(agent_folder) / doc_id
    doc_dir.mkdir(parents=True, exist_ok=True)

    # Raw file
    (doc_dir / filename).write_bytes(content)
    # Extracted text
    (doc_dir / "text.txt").write_text(extracted_text, encoding="utf-8")
    # Chunks
    chunks = _chunk_text(extracted_text)
    (doc_dir / "chunks.json").write_text(
        json.dumps(chunks, ensure_ascii=False), encoding="utf-8"
    )

    meta = {
        "doc_id": doc_id,
        "filename": filename,
        "org_name": org_name,
        "doc_type": doc_type,
        "description": description,
        "uploaded_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "chunk_count": len(chunks),
        "char_count": len(extracted_text),
        # Content hash of the RAW file — what `find_document_by_hash` matches
        # so the same brochure uploaded twice is answered with the existing
        # document instead of a second copy competing with itself.
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }
    (doc_dir / "metadata.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    _update_index(agent_folder, meta)
    _invalidate(agent_folder)
    logger.info(
        "[docs] saved doc_id=%s (%d chars, %d chunks) for agent=%s",
        doc_id, len(extracted_text), len(chunks), agent_folder,
    )
    return meta


def _invalidate(agent_folder: str) -> None:
    """Drop the in-memory index and any cached retrievals for this agent.

    Correctness does not depend on this — the semantic cache namespaces on a
    content hash of the chunks, so an added or removed document makes every
    older entry unreachable by construction, and the index cache keys on file
    mtimes. This just frees the memory immediately instead of at TTL, and keeps
    the two mechanisms from being the only thing standing between an edited
    document and a stale answer.
    """
    try:
        from . import retrieval
        retrieval.invalidate(agent_folder)
    except Exception:  # never fail an upload over cache bookkeeping
        pass


def list_documents(agent_folder: str) -> list[dict]:
    index_path = _docs_dir(agent_folder) / "index.json"
    if not index_path.exists():
        return []
    try:
        return json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return []


def delete_document(agent_folder: str, doc_id: str) -> bool:
    doc_dir = _docs_dir(agent_folder) / doc_id
    if not doc_dir.exists():
        return False
    shutil.rmtree(doc_dir)
    _invalidate(agent_folder)
    # Remove from index
    index_path = _docs_dir(agent_folder) / "index.json"
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index = [d for d in index if d["doc_id"] != doc_id]
            index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return True


def has_documents(agent_folder: str) -> bool:
    return bool(list_documents(agent_folder))


def find_document_by_hash(agent_folder: str, sha256: str) -> dict | None:
    """The already-uploaded document with this raw-content hash, if any.
    Entries written before the hash existed never match — they are not
    duplicates we can prove."""
    if not sha256:
        return None
    for doc in list_documents(agent_folder):
        if doc.get("sha256") == sha256:
            return doc
    return None


def get_document_text(agent_folder: str, doc_id: str) -> str:
    text_path = _docs_dir(agent_folder) / doc_id / "text.txt"
    if not text_path.exists():
        return ""
    return text_path.read_text(encoding="utf-8")


def rechunk_document(agent_folder: str, doc_id: str) -> bool:
    """Re-split a stored document with the current chunk geometry.

    Reads the extracted text that was kept at upload, so re-chunking never
    needs the original file or another extraction pass. Returns False when the
    chunks are already what the current geometry produces — so a re-index over
    an unchanged corpus rewrites nothing.
    """
    doc_dir = _docs_dir(agent_folder) / doc_id
    text_path = doc_dir / "text.txt"
    chunks_path = doc_dir / "chunks.json"
    if not text_path.exists():
        return False
    chunks = _chunk_text(text_path.read_text(encoding="utf-8"))
    if chunks_path.exists():
        try:
            if json.loads(chunks_path.read_text(encoding="utf-8")) == chunks:
                return False
        except Exception:
            pass
    chunks_path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
    _bump_index(agent_folder, doc_id, {"chunk_count": len(chunks)})
    _invalidate(agent_folder)
    logger.info("[docs] rechunked %s/%s into %d chunks (%d words, %d overlap)",
                agent_folder, doc_id, len(chunks), _CHUNK_WORDS, _CHUNK_OVERLAP)
    return True


def _bump_index(agent_folder: str, doc_id: str, fields: dict) -> None:
    index_path = _docs_dir(agent_folder) / "index.json"
    if not index_path.exists():
        return
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return
    for entry in index:
        if entry.get("doc_id") == doc_id:
            entry.update(fields)
    index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False),
                          encoding="utf-8")


def get_all_chunks(agent_folder: str) -> list[dict]:
    """Return all text chunks from all documents for this agent."""
    all_chunks: list[dict] = []
    for doc in list_documents(agent_folder):
        chunks_path = _docs_dir(agent_folder) / doc["doc_id"] / "chunks.json"
        if not chunks_path.exists():
            continue
        try:
            chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
            for c in chunks:
                c["doc_id"] = doc["doc_id"]
                c["doc_type"] = doc.get("doc_type", "")
            all_chunks.extend(chunks)
        except Exception:
            pass
    return all_chunks


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _chunk_text(text: str) -> list[dict]:
    words = text.split()
    chunks: list[dict] = []
    i, cid = 0, 0
    while i < len(words):
        chunk_words = words[i : i + _CHUNK_WORDS]
        chunks.append({"chunk_id": cid, "text": " ".join(chunk_words), "word_start": i})
        cid += 1
        i += _CHUNK_WORDS - _CHUNK_OVERLAP
    return chunks


def _update_index(agent_folder: str, meta: dict) -> None:
    index_path = _docs_dir(agent_folder) / "index.json"
    index: list[dict] = []
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    index.append(meta)
    index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
