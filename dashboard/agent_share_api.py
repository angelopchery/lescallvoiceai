"""
Share an agent by link — the recipient IMPORTS an independent copy.

The product rule this module exists to enforce, stated once:

    An imported agent has NO relationship to the agent it came from.

It is a new folder, a new globally-unique slug, a new agent_id, a fresh
revision history, owned by the importer. Editing either side never touches the
other. Archiving the source does not break the copy. Nothing in the call path
ever reads the tables below — `agent_shares` / `agent_share_imports` are the
sharing control plane and the abuse trail, not a runtime join.

Why this is a separate router from agent_builder_api
----------------------------------------------------
That router's `_tenancy_dep` 404s any `{name}` path param the caller does not
own — correct there, fatal here: the whole point of a share link is that
someone who does NOT own the agent reads it. So the preview/import routes are
keyed by an unguessable TOKEN rather than by slug, and this module does its
own owner checks explicitly at each site.

The unit of sharing is the FAMILY
---------------------------------
Agents come in twin pairs (`acme-inbound` + `acme-outbound`); the plan cap
counts families, and the card in the UI is a family. So one link carries the
whole family and one import consumes exactly one agent slot. Whichever
direction variants exist at import time are what the recipient gets.

What is deliberately NOT copied
-------------------------------
* `routing.yaml`  — the sharer's DID binding. Copying it would point the
  importer's brand-new agent at someone else's phone number.
* `bookings.json` — the sharer's live appointments, i.e. their customers.
* `is_active`     — an import must never change which agent answers YOUR
  calls (see _deactivate below).
Everything else (agent.yaml, and the uploaded knowledge-base documents with
their embeddings) travels, because otherwise the imported agent references
knowledge it does not have and reads as broken.

Opening a link you have already imported
----------------------------------------
Sharing is how one person builds an agent FOR another account, and they keep
improving it and re-send the same link. So a second open is a CHOICE, not a
refusal: REPLACE a copy you hold with the shared version as it is now, or
import it again as a NEW agent. Replace is still a one-off copy — it creates
no link between the two agents, the rule above is untouched — and it keeps
everything that is the recipient's rather than the sharer's: the slug, the
agent_id, routing.yaml, bookings.json, is_active, the revision history (the
overwritten version is one rollback away) and their own uploaded documents.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import secrets
import shutil
import sys
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request

logger = logging.getLogger("agent_share_api")

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CALLING_AGENT = _REPO_ROOT / "calling-agent"
if str(_CALLING_AGENT) not in sys.path:
    sys.path.insert(0, str(_CALLING_AGENT))

from backend.app.agent_definition.schema import AgentDefinition   # noqa: E402
from backend.app.agent_definition.registry import agent_registry  # noqa: E402

import agent_builder_api as builder  # noqa: E402  (slug algebra + the ONE write path)

_AGENTS_DIR = builder._AGENTS_DIR
_ARCHIVE_DIR = _AGENTS_DIR / "_archived"

# Default link lifetime. A link that outlives the reason it was created is a
# standing invitation to copy an agent that has since gained real pricing and
# real customer knowledge, so it expires unless the sharer says otherwise.
SHARE_TTL_DAYS = int(os.environ.get("LESCALL_SHARE_TTL_DAYS", "30"))

# Abuse ceiling on imports per account per rolling 24h. Not a product limit —
# the plan cap is the product limit. This stops a script from churning
# create/archive cycles to fill the disk with agent folders.
MAX_IMPORTS_PER_DAY = int(os.environ.get("LESCALL_MAX_IMPORTS_PER_DAY", "20"))

_SLUG_SAFE = re.compile(r"[^a-z0-9_-]+")
# agent_id sits at column 0 in every generated agent.yaml, so a line scan
# avoids parsing ~30 full YAML documents just to check one string.
_AGENT_ID_RE = re.compile(r"^agent_id:\s*['\"]?([^'\"\n]+)", re.M)


# ── request context ─────────────────────────────────────────────────────────

async def _session_dep(request: Request):
    """Every route here requires a logged-in account.

    MUST stay async for the same reason builder._tenancy_dep does: a sync
    dependency runs in a threadpool and its ContextVar write would not reach
    the handler's task. The builder's ContextVar is set too, because
    write_agent_dir() and friends read it.
    """
    import auth
    owner = auth.normalize_owner(auth.current_owner(request))
    builder._REQ_OWNER.set(owner)
    request.state.owner = owner


router = APIRouter(dependencies=[Depends(_session_dep)])


def _owner(request: Request) -> str:
    return getattr(request.state, "owner", "") or builder._ctx_owner()


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# ── availability ────────────────────────────────────────────────────────────
#
# agent_shares lives in a versioned migration (0017), which is Postgres-only
# and applied by hand. On a box where it has not run yet, every route here
# would raise an opaque 500 mentioning a missing relation. Say so instead.

_TABLES_READY = False


def _assert_shares_ready() -> None:
    global _TABLES_READY
    if _TABLES_READY:
        return
    try:
        from shared.db.database import get_db
        with get_db() as db:
            db.execute("SELECT 1 FROM agent_shares LIMIT 1").fetchone()
            # copy_no arrives with 0029. Probed separately so the message names
            # the migration that is actually missing.
            db.execute("SELECT copy_no FROM agent_share_imports LIMIT 1").fetchone()
        _TABLES_READY = True
    except Exception as exc:
        if re.search(r"does not exist|no such (table|column)|undefined",
                     str(exc), re.I):
            missing = "0029" if "copy_no" in str(exc) else "0017"
            raise HTTPException(
                status_code=503,
                detail="Agent sharing is not enabled on this server yet "
                       f"(migration {missing} has not been applied).")
        raise


# ── helpers: the family on disk ─────────────────────────────────────────────

def _assert_slug_shape(slug: str) -> str:
    """Reject anything that is not a plain agent slug.

    The ownership check below would already stop a traversal (`../../etc` can
    never match a row in the agents table), but a slug reaches
    `_AGENTS_DIR / slug` before that check runs, so the shape is validated
    where it enters rather than relied upon downstream.
    """
    if not slug or len(slug) > 64 or not slug.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400,
                            detail="'slug' must be alphanumeric with _ or -")
    return slug


def _family_of(slug: str) -> str:
    return builder.agent_family(_assert_slug_shape(slug))


def _variant_key(slug: str, family: str) -> str:
    """Which member of `family` a slug is: 'inbound', 'outbound', or '' for the
    bare pre-twin slug.

    Source and copy are matched up by this key, never by position in a list: a
    recipient may have archived one half, and a standalone agent may have
    gained a twin since the link was made. (The import used to derive the
    direction from the suffix alone, so a bare slug travelling alongside a twin
    would have been written as `<family>-None`.)
    """
    for d in builder.DIRECTIONS:
        if slug == f"{family}-{d}":
            return d
    return ""


def _variant_slug(family: str, key: str) -> str:
    return f"{family}-{key}" if key else family


def _family_slugs_on_disk(family: str) -> list[str]:
    """Live slugs of `family`, inbound first. A standalone (pre-twin) agent is
    its own family and appears under its bare slug."""
    out = []
    for candidate in (f"{family}-inbound", f"{family}-outbound", family):
        if candidate in out:
            continue
        if (_AGENTS_DIR / candidate / "agent.yaml").exists():
            out.append(candidate)
    return out


def _owned_family_slugs(family: str, owner: str) -> list[str]:
    """Slugs of `family` that exist on disk AND are owned, unarchived, by
    `owner`. Both halves must hold: a folder with no row is unclaimed
    infrastructure, and an archived row is a deleted agent."""
    on_disk = _family_slugs_on_disk(family)
    if not on_disk:
        return []
    from shared.db.database import get_db
    with get_db() as db:
        rows = db.execute(
            "SELECT slug FROM agents WHERE owner=? AND archived_at IS NULL",
            (owner,)).fetchall()
    owned = {r["slug"] for r in rows}
    return [s for s in on_disk if s in owned]


def _slug_is_live_for(slug: str, owner: str) -> bool:
    if not (_AGENTS_DIR / slug / "agent.yaml").exists():
        return False
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT 1 FROM agents WHERE slug=? AND owner=? "
                "AND archived_at IS NULL", (slug, owner)).fetchone()
        return bool(row)
    except Exception:
        return False


# ── helpers: minting a name nobody else holds ───────────────────────────────

def _sanitize_family(name: str) -> str:
    base = _SLUG_SAFE.sub("-", (name or "").strip().lower()).strip("-_")
    base = re.sub(r"-{2,}", "-", base)[:40].strip("-_")
    return base or "agent"


def _family_free(family: str) -> bool:
    """True when NOTHING anywhere holds this family's slugs.

    Stricter than the builder's `_assert_slug_free`, which lets a tenant
    re-claim an archived slug. An import must never resurrect a slug that
    belongs to someone else's archive, and it must never collide with a folder
    that exists without a row — so this checks the live tree, the archive tree
    AND every agents row regardless of owner or archived state.
    """
    slugs = [family, f"{family}-inbound", f"{family}-outbound"]
    for slug in slugs:
        if (_AGENTS_DIR / slug).exists() or (_ARCHIVE_DIR / slug).exists():
            return False
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT 1 FROM agents WHERE slug=? OR slug=? OR slug=? LIMIT 1",
                tuple(slugs)).fetchone()
        return row is None
    except Exception as exc:
        # Fail CLOSED: if we cannot prove the name is free, do not take it.
        logger.error("[share] family availability check failed for %s: %r",
                     family, exc)
        return False


def _mint_family(source_family: str) -> str:
    base = _sanitize_family(source_family)
    if _family_free(base):
        return base
    for n in range(2, 51):
        candidate = f"{base}-{n}"
        if _family_free(candidate):
            return candidate
    for _ in range(20):  # pathological: fall back to randomness
        candidate = f"{base}-{secrets.token_hex(3)}"
        if _family_free(candidate):
            return candidate
    raise HTTPException(
        status_code=409,
        detail="Could not find a free name for the imported agent. Rename or "
               "archive one of your existing agents and try again.")


def _existing_agent_ids() -> set[str]:
    ids: set[str] = set()
    for path in _AGENTS_DIR.glob("*/agent.yaml"):
        try:
            match = _AGENT_ID_RE.search(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if match:
            ids.add(match.group(1).strip())
    return ids


def _unique_agent_id(preferred: str, taken: set[str]) -> str:
    """agent_id must be unique platform-wide.

    documents/store.get_folder_by_agent_id() resolves an agent's document
    folder by scanning agent.yaml files for a matching agent_id and returning
    the FIRST hit. Two agents sharing an id therefore means one tenant's RAG
    lookups can land in another tenant's document folder — so an import always
    remints, and verifies.
    """
    if preferred not in taken:
        return preferred
    for n in range(2, 100):
        candidate = f"{preferred}-{n}"
        if candidate not in taken:
            return candidate
    return f"{preferred}-{secrets.token_hex(4)}"


# ── helpers: the copied definition ──────────────────────────────────────────

# Fields that describe the SOURCE record's history, not its behaviour. They
# must not travel: a copy that arrives at "revision 47, updated_by
# someone@else.com" lies about its own history and makes the first real edit
# look like a rollback.
_HISTORY_FIELDS = ("revision", "updated_at", "updated_by", "created_at",
                   "_rev", "audit")


def _prepare_spec(source_yaml: Path, new_slug: str, taken_ids: set[str]) -> dict:
    spec = yaml.safe_load(source_yaml.read_text(encoding="utf-8")) or {}
    if not isinstance(spec, dict):
        raise HTTPException(status_code=422,
                            detail="The shared agent's definition is not readable.")
    for field in _HISTORY_FIELDS:
        spec.pop(field, None)
    spec["agent_id"] = _unique_agent_id(f"{new_slug}-v1", taken_ids)
    taken_ids.add(spec["agent_id"])
    spec["revision"] = 1
    return spec


def _copy_documents(src_slug: str, dst_slug: str) -> int:
    """Copy the knowledge-base documents (text, chunks and .npy embeddings).

    Everything under documents/ is folder-relative — index.json holds doc_ids,
    never paths — so a plain tree copy is a working knowledge base. Returns
    the number of documents copied; a failure here is logged and swallowed
    because a missing PDF must not undo an otherwise-complete import.
    """
    src = _AGENTS_DIR / src_slug / "documents"
    if not src.is_dir():
        return 0
    dst = _AGENTS_DIR / dst_slug / "documents"
    try:
        shutil.copytree(src, dst, dirs_exist_ok=True)
        # ALWAYS rewritten, never inherited: when the source is itself an
        # imported copy, the tree copy just brought ITS manifest along.
        _write_shared_manifest(
            dst_slug, {d["doc_id"] for d in _read_doc_index(dst_slug)})
        index = dst / "index.json"
        if index.exists():
            return len(json.loads(index.read_text(encoding="utf-8")) or [])
        return 0
    except Exception as exc:
        logger.warning("[share] document copy %s -> %s failed: %r",
                       src_slug, dst_slug, exc)
        shutil.rmtree(dst, ignore_errors=True)
        return 0


def _read_doc_index(slug: str) -> list[dict]:
    index = _AGENTS_DIR / slug / "documents" / "index.json"
    if not index.exists():
        return []
    try:
        data = json.loads(index.read_text(encoding="utf-8")) or []
    except Exception:
        return []
    return [d for d in data if isinstance(d, dict) and d.get("doc_id")]


# Which documents in a copy came FROM the share, as opposed to being uploaded
# by the recipient afterwards. A dotfile next to index.json: everything in the
# document pipeline reads index.json or walks sub-DIRECTORIES, so it is inert.
_SHARED_MANIFEST = ".shared.json"


def _shared_doc_ids(slug: str) -> set[str] | None:
    """None = no record (a copy imported before replace existed)."""
    path = _AGENTS_DIR / slug / "documents" / _SHARED_MANIFEST
    try:
        return {str(x) for x in json.loads(path.read_text(encoding="utf-8"))}
    except Exception:
        return None


def _write_shared_manifest(slug: str, doc_ids: set[str]) -> None:
    path = _AGENTS_DIR / slug / "documents" / _SHARED_MANIFEST
    path.write_text(json.dumps(sorted(doc_ids)), encoding="utf-8")


def _doc_uploaded_at(entry: dict):
    """documents/store stamps `uploaded_at` as '2026-06-26 06:37 UTC'. None when
    it is absent or in any other shape — and None must mean KEEP, never drop."""
    try:
        return dt.datetime.strptime(
            str(entry.get("uploaded_at") or ""), "%Y-%m-%d %H:%M UTC"
        ).replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def _sync_documents(src_slug: str, dst_slug: str, imported_at) -> dict:
    """Bring a copy's knowledge base up to the shared version WITHOUT eating
    the recipient's own uploads.

    Three kinds of document can be sitting in the copy, and they need three
    different answers:

      in the source            → take the source's bytes (new or changed)
      came from the share, no
        longer in the source   → the sharer removed it — remove it. Leaving it
                                 is the dangerous choice: a retired price list
                                 keeps being quoted on calls and nothing in the
                                 UI says why.
      uploaded by the recipient → keep it. "Replace with the shared version" is
                                 a statement about the shared agent; it is not
                                 consent to delete their files.

    The manifest (_SHARED_MANIFEST) tells the last two apart. A copy imported
    before it existed has none, so for that FIRST replace only, `uploaded_at`
    stands in: a tree copy preserves the sharer's stamp, which necessarily
    predates the import, and an upload cannot land in a copy before the copy
    exists. That stamp is NOT good enough after a replace — a document the
    sharer added and later withdrew would carry a post-import stamp and read as
    the recipient's — which is why the manifest is written here and is what
    every later replace reads. An unreadable stamp counts as the recipient's.

    Best-effort like _copy_documents: a failure is logged and leaves the copy's
    documents exactly as they were, because a half-merged index is worse than a
    stale one.
    """
    out = {"updated": 0, "removed": 0, "kept": 0}
    src_dir = _AGENTS_DIR / src_slug / "documents"
    dst_dir = _AGENTS_DIR / dst_slug / "documents"
    src_index = _read_doc_index(src_slug)
    dst_index = _read_doc_index(dst_slug)
    if not src_index and not dst_index:
        return out

    cutoff = _as_dt(imported_at)
    shared_ids = _shared_doc_ids(dst_slug)
    src_ids = {d["doc_id"] for d in src_index}
    keep, drop = [], []
    for entry in dst_index:
        if entry["doc_id"] in src_ids:
            continue                      # superseded by the source's copy
        if shared_ids is not None:
            from_share = entry["doc_id"] in shared_ids
        else:
            stamp = _doc_uploaded_at(entry)
            from_share = bool(cutoff and stamp and
                              stamp < cutoff.replace(second=0, microsecond=0))
        (drop if from_share else keep).append(entry)

    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        for entry in src_index:
            doc_src = src_dir / entry["doc_id"]
            if not doc_src.is_dir():
                continue
            doc_dst = dst_dir / entry["doc_id"]
            staged = dst_dir / f".incoming-{entry['doc_id']}"
            shutil.rmtree(staged, ignore_errors=True)
            shutil.copytree(doc_src, staged)
            shutil.rmtree(doc_dst, ignore_errors=True)
            staged.rename(doc_dst)
            out["updated"] += 1
        merged = [e for e in src_index if (dst_dir / e["doc_id"]).is_dir()] + keep
        (dst_dir / "index.json").write_text(
            json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
        _write_shared_manifest(
            dst_slug, {e["doc_id"] for e in merged if e["doc_id"] in src_ids})
        # Only once the index no longer references them.
        for entry in drop:
            shutil.rmtree(dst_dir / entry["doc_id"], ignore_errors=True)
        out["removed"], out["kept"] = len(drop), len(keep)
    except Exception as exc:
        logger.warning("[share] document sync %s -> %s failed: %r",
                       src_slug, dst_slug, exc)
        return {"updated": 0, "removed": 0, "kept": len(dst_index), "failed": True}

    try:  # frees the in-process index now rather than at TTL; never required
        from backend.app.documents.store import _invalidate
        _invalidate(dst_slug)
    except Exception:
        pass
    return out


def _doc_count(slug: str) -> int:
    index = _AGENTS_DIR / slug / "documents" / "index.json"
    if not index.exists():
        return 0
    try:
        return len(json.loads(index.read_text(encoding="utf-8")) or [])
    except Exception:
        return 0


def _deactivate(slug: str) -> None:
    """Imported agents are never the account's active fallback.

    `agents.is_active` defaults to 1, and conversation_call_bridge picks the
    most recently updated is_active row for an owner as the agent for calls
    that match no DID binding. Left alone, importing an agent to look at it
    would silently take over answering that account's unbound calls.
    """
    try:
        from shared.db.database import get_db
        with get_db() as db:
            db.execute("UPDATE agents SET is_active=0 WHERE slug=?", (slug,))
    except Exception as exc:
        logger.error("[share] could not clear is_active for %s: %r", slug, exc)


# ── helpers: link URL ───────────────────────────────────────────────────────

def _public_base(request: Request) -> str:
    explicit = os.environ.get("LESCALL_PUBLIC_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    try:
        import auth
        redirect = (auth.OAUTH_REDIRECT_URL or "").strip()
        if redirect.endswith("/auth/callback"):
            return redirect[: -len("/auth/callback")]
    except Exception:
        pass
    return str(request.base_url).rstrip("/")


def _share_url(request: Request, token: str) -> str:
    # /s/<token> is a thin SPA route that forwards to
    # /build/agents?import=<token>. Short links survive being pasted into
    # chat apps that break on long query strings.
    return f"{_public_base(request)}/s/{token}"


# ── helpers: rows ───────────────────────────────────────────────────────────

def _as_dt(value):
    from shared.billing.entitlements import as_dt
    return as_dt(value)


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _link_state(row: dict) -> str:
    if row.get("revoked_at"):
        return "revoked"
    expires = _as_dt(row.get("expires_at"))
    if expires and expires <= _now():
        return "expired"
    return "live"


def _link_payload(request: Request, row: dict) -> dict:
    return {
        "token": row["token"],
        "url": _share_url(request, row["token"]),
        "family": row["family"],
        "label": row.get("label") or row["family"],
        "state": _link_state(row),
        "import_count": int(row.get("import_count") or 0),
        "expires_at": row.get("expires_at"),
        "created_at": row.get("created_at"),
    }


def _fetch_share(token: str) -> dict | None:
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute("SELECT * FROM agent_shares WHERE token=?",
                         (token,)).fetchone()
    return dict(row) if row else None


# ═══════════════════════════════════════════════════════════════════════════
# Sharer side
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/api/share")
async def get_link(request: Request, slug: str = "") -> dict:
    """The live link for one of MY agents, or {"link": null}."""
    _assert_shares_ready()
    owner = _owner(request)
    if not slug:
        raise HTTPException(status_code=400, detail="'slug' is required")
    family = _family_of(slug)
    if not _owned_family_slugs(family, owner):
        raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM agent_shares WHERE owner=? AND family=? "
            "AND revoked_at IS NULL", (owner, family)).fetchone()
    if not row:
        return {"link": None, "family": family}
    payload = _link_payload(request, dict(row))
    if payload["state"] != "live":
        return {"link": None, "family": family, "previous_state": payload["state"]}
    return {"link": payload, "family": family}


@router.post("/api/share")
async def create_link(request: Request) -> dict:
    """Mint (or return) the share link for one of my agents.

    Idempotent by design: pressing Share twice must hand back the SAME link,
    not a second one that has to be revoked separately. An expired link is
    retired and replaced.
    """
    _assert_shares_ready()
    owner = _owner(request)
    try:
        body = await request.json()
        assert isinstance(body, dict)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    slug = str(body.get("slug") or "").strip()
    if not slug:
        raise HTTPException(status_code=400, detail="'slug' is required")

    family = _family_of(slug)
    slugs = _owned_family_slugs(family, owner)
    if not slugs:
        # 404 rather than 403 — never confirm that another tenant's agent exists.
        raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")

    # Refuse to hand out a link to a definition that no longer loads: the
    # recipient's import would otherwise 422 at them with a message about
    # someone else's YAML, which they can do nothing about.
    try:
        agent = AgentDefinition.from_yaml(_AGENTS_DIR / slugs[0] / "agent.yaml")
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"This agent has a configuration error and cannot be "
                   f"shared until it is fixed: {exc}")
    label = agent.identity.agent_name or family

    from shared.db.database import get_db
    with get_db() as db:
        existing = db.execute(
            "SELECT * FROM agent_shares WHERE owner=? AND family=? "
            "AND revoked_at IS NULL", (owner, family)).fetchone()
        if existing:
            row = dict(existing)
            if _link_state(row) == "live":
                # Same link, refreshed label. Nothing else changes — in
                # particular the token stays put, so links already sent keep
                # working.
                db.execute("UPDATE agent_shares SET label=? WHERE id=?",
                           (label, row["id"]))
                row["label"] = label
                return {"link": _link_payload(request, row), "created": False}
            db.execute("UPDATE agent_shares SET revoked_at=CURRENT_TIMESTAMP "
                       "WHERE id=?", (row["id"],))

        token = secrets.token_urlsafe(32)
        expires = (_now() + dt.timedelta(days=SHARE_TTL_DAYS)
                   ).isoformat() if SHARE_TTL_DAYS > 0 else None
        db.execute(
            "INSERT INTO agent_shares (token, owner, family, label, expires_at) "
            "VALUES (?,?,?,?,?)", (token, owner, family, label, expires))
        row = dict(db.execute("SELECT * FROM agent_shares WHERE token=?",
                              (token,)).fetchone())

    try:
        from shared.audit import record
        record(owner, "agent_share_created", target=family,
               meta={"slugs": slugs}, ip=_client_ip(request))
    except Exception:
        pass
    logger.info("[share] link created for %s by %s (%d variants)",
                family, owner, len(slugs))
    return {"link": _link_payload(request, row), "created": True}


@router.delete("/api/share/{token}")
async def revoke_link(token: str, request: Request) -> dict:
    """Kill a link. Copies already imported are unaffected — they are
    independent agents, and taking them back is not a thing sharing can do."""
    _assert_shares_ready()
    owner = _owner(request)
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute("SELECT * FROM agent_shares WHERE token=? AND owner=?",
                         (token, owner)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Share link not found")
        if not row["revoked_at"]:
            db.execute("UPDATE agent_shares SET revoked_at=CURRENT_TIMESTAMP "
                       "WHERE id=?", (row["id"],))
    try:
        from shared.audit import record
        record(owner, "agent_share_revoked", target=row["family"],
               ip=_client_ip(request))
    except Exception:
        pass
    return {"status": "revoked", "family": row["family"],
            "import_count": int(row["import_count"] or 0)}


# ═══════════════════════════════════════════════════════════════════════════
# Recipient side
# ═══════════════════════════════════════════════════════════════════════════

def _summarize(slugs: list[str]) -> dict:
    """What the popup shows. Read from the CURRENT definition, so the
    recipient previews exactly the bytes they would import."""
    variants, languages, stages = [], [], []
    agent_name = company = ""
    calendar_slots = docs = 0
    for slug in slugs:
        try:
            agent = AgentDefinition.from_yaml(_AGENTS_DIR / slug / "agent.yaml")
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=f"The shared agent has a configuration error and "
                       f"cannot be imported: {exc}")
        direction = str(getattr(agent, "call_direction", "inbound") or "inbound")
        variants.append({"slug": slug, "direction": direction})
        agent_name = agent_name or agent.identity.agent_name
        company = company or agent.identity.company_name
        for lang in agent.languages.supported:
            if lang not in languages:
                languages.append(lang)
        if not stages:
            stages = [s.id for s in agent.call_flow.stages]
        calendar_slots = max(calendar_slots, len(agent.calendar.mock_slots or []))
        docs += _doc_count(slug)
    return {
        "agent_name": agent_name, "company": company,
        "languages": languages, "stages": stages,
        "directions": [v["direction"] for v in variants],
        "variants": variants, "calendar_slots": calendar_slots,
        "documents": docs,
    }


def _sharer_identity(email: str) -> dict:
    name = ""
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute("SELECT name, company FROM users WHERE email=?",
                             (email,)).fetchone()
        if row:
            name = (row["name"] or row["company"] or "") or ""
    except Exception:
        pass
    return {"email": email, "name": name}


# A reservation row whose slugs are still "[]" is an import that is running
# right now — or one whose process died mid-copy. Young means running.
_RESERVATION_GRACE_S = 300


def _copies(share_id: int, importer: str) -> list[dict]:
    """Every import of this link into this account, oldest first.

    `live` is what decides everything downstream: a copy with no live slug is
    one the recipient archived, and it neither blocks a fresh import nor can
    be replaced. `pending` marks a reservation still being filled in.
    """
    from shared.db.database import get_db
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM agent_share_imports WHERE share_id=? AND importer=? "
            "ORDER BY copy_no", (share_id, importer)).fetchall()
    out = []
    for row in rows:
        try:
            slugs = json.loads(row["new_slugs"] or "[]")
        except Exception:
            slugs = []
        created = _as_dt(row["created_at"])
        pending = (not slugs and created is not None and
                   (_now() - created).total_seconds() < _RESERVATION_GRACE_S)
        out.append({
            "id": row["id"], "copy_no": int(row["copy_no"] or 1),
            "family": row["new_family"], "slugs": slugs,
            "live": [s for s in slugs if _slug_is_live_for(s, importer)],
            "created_at": row["created_at"], "synced_at": row["synced_at"],
            "pending": pending,
        })
    return out


def _copy_payload(copy: dict) -> dict:
    return {"family": copy["family"], "slugs": copy["live"],
            "primary": copy["live"][0], "imported_at": copy["created_at"],
            "replaced_at": copy["synced_at"]}


def _source_updated_at(slugs: list[str]) -> str | None:
    """When the sharer last changed the agent — the newest agent.yaml mtime.

    mtime rather than the YAML's own `updated_at`, which only the store stamps;
    a hand edit or a Copilot rebuild that bypasses it would read as unchanged.
    """
    newest = 0.0
    for slug in slugs:
        try:
            newest = max(newest, (_AGENTS_DIR / slug / "agent.yaml").stat().st_mtime)
        except OSError:
            pass
    if not newest:
        return None
    return dt.datetime.fromtimestamp(newest, dt.timezone.utc).isoformat()


def _imports_today(importer: str) -> int:
    try:
        from shared.db.database import get_db
        since = (_now() - dt.timedelta(days=1)).isoformat()
        with get_db() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM agent_share_imports "
                "WHERE importer=? AND created_at >= ?", (importer, since)).fetchone()
        return int((row or {}).get("n") or 0)
    except Exception:
        return 0


def _resolve_for_import(token: str, importer: str) -> tuple[dict, list[str]]:
    """(share row, live source slugs) or the right HTTP error.

    404 for a token that never existed; 410 for one that did but no longer
    works. A recipient staring at "not found" for a link a colleague just sent
    cannot tell those apart, and they need different next steps.
    """
    share = _fetch_share(token)
    if not share:
        raise HTTPException(status_code=404, detail="This share link is not valid.")
    state = _link_state(share)
    if state == "revoked":
        raise HTTPException(
            status_code=410,
            detail="This share link was revoked by the person who created it.")
    if state == "expired":
        raise HTTPException(
            status_code=410,
            detail="This share link has expired. Ask for a fresh one.")
    if share["owner"] == importer:
        raise HTTPException(
            status_code=409,
            detail="This is your own agent — it is already in your account.")
    slugs = _owned_family_slugs(share["family"], share["owner"])
    if not slugs:
        raise HTTPException(
            status_code=410,
            detail="The shared agent is no longer available — its owner "
                   "deleted or archived it.")
    return share, slugs


@router.get("/api/share/{token}")
async def preview_share(token: str, request: Request) -> dict:
    """What the import popup renders. Never mutates anything.

    Deliberately reports refusals in the BODY (can_import=false + reason)
    rather than as an HTTP error for the cases the user can act on — at plan
    cap, over the daily import ceiling — because the popup has to render the
    agent's details alongside the explanation. Dead links stay HTTP errors.

    `copies` lists the live agents in THIS account that came from this link.
    When it is non-empty the popup offers a choice instead of one button:
    replace one of them, or (if can_import) take another copy.
    """
    _assert_shares_ready()
    importer = _owner(request)
    share, slugs = _resolve_for_import(token, importer)
    summary = _summarize(slugs)

    can_import, reason, code = True, "", ""
    billing: dict = {}
    try:
        from shared.billing.entitlements import (
            agent_families_used, enforcement_enabled, get_entitlements)
        ent = get_entitlements(importer)
        limit = ent.get("max_agent_families")
        used = agent_families_used(importer)
        billing = {"agents_used": used, "agents_limit": limit,
                   "enforcement": enforcement_enabled()}
        from shared.billing.entitlements import check_agent_create
        decision = check_agent_create(importer)
        if not decision:
            can_import, reason, code = False, decision.reason, decision.code
    except Exception as exc:
        logger.warning("[share] entitlement preview failed for %s: %r",
                       importer, exc)

    if can_import and _imports_today(importer) >= MAX_IMPORTS_PER_DAY:
        can_import, code = False, "rate_limited"
        reason = (f"You have imported {MAX_IMPORTS_PER_DAY} agents in the last "
                  f"24 hours. Try again later.")

    # `can_import` answers ONE question: may this account take a NEW copy?
    # Already holding a copy no longer makes that false — it makes REPLACE
    # available as well, and replace needs neither a plan slot nor import
    # budget, so an account at its cap (every free account with one agent) can
    # still bring its copy up to date.
    copies = [c for c in _copies(share["id"], importer) if c["live"]]

    return {
        "token": token,
        "agent": summary,
        "shared_by": _sharer_identity(share["owner"]),
        "expires_at": share["expires_at"],
        "source_updated_at": _source_updated_at(slugs),
        "can_import": can_import,
        "reason": reason,
        "code": code,
        "billing": billing,
        "copies": [_copy_payload(c) for c in copies],
        # Kept for a dashboard tab that was open across the deploy.
        "already_imported": ({"slugs": copies[-1]["live"],
                              "at": copies[-1]["created_at"]}
                             if copies else None),
    }


async def _import_options(request: Request) -> tuple[str, str]:
    """(mode, target) from the optional JSON body. No body at all is the
    original contract and stays valid: it means "import it", full stop."""
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        body = {}
    mode = str(body.get("mode") or "").strip().lower()
    if mode not in ("", "new", "replace"):
        raise HTTPException(status_code=400,
                            detail="'mode' must be 'new' or 'replace'")
    return mode, str(body.get("target") or "").strip()


@router.post("/api/share/{token}/import")
async def import_share(token: str, request: Request) -> dict:
    """Bring the shared agent into MY account.

    Body (optional): {"mode": "new" | "replace", "target": "<my family>"}

      no mode   the first import. Refused with 409 `already_imported` once the
                account holds a live copy — the caller then has to SAY which of
                the two things below it wants; guessing either one is wrong.
      new       another independent copy under a fresh name.
      replace   overwrite a copy I already hold with the shared version,
                keeping its name, phone-number bindings, bookings and calls.

    Either way the result is mine alone: nothing here links the two agents, and
    a replace is a one-off copy, not a subscription to the sharer's edits.
    """
    _assert_shares_ready()
    importer = _owner(request)
    share, source_slugs = _resolve_for_import(token, importer)
    mode, target = await _import_options(request)

    copies = _copies(share["id"], importer)
    live = [c for c in copies if c["live"]]

    if mode == "replace":
        if not live:
            raise HTTPException(
                status_code=409,
                detail="You no longer have a copy of this agent to replace. "
                       "Import it as a new agent instead.")
        if target:
            match = [c for c in live if c["family"] == target]
        else:
            # Unambiguous only when there is exactly one. Picking "the newest"
            # for the caller would overwrite an agent they never pointed at.
            match = live if len(live) == 1 else []
        if not match:
            # `reason` is the key the SPA's error unwrapping reads; without
            # it a structured detail surfaces as a bare "400 Bad Request".
            raise HTTPException(status_code=400, detail={
                "error": "target_required",
                "copies": [_copy_payload(c) for c in live],
                "reason": "Say which of your copies to replace."})
        return _replace_copy(share, source_slugs, match[0], importer, request)

    if live and mode != "new":
        sentence = (f"You already imported this agent as "
                    f"“{live[-1]['live'][0]}”. Replace that copy, "
                    f"or import it again as a new agent.")
        raise HTTPException(status_code=409, detail={
            "error": "already_imported",
            "slugs": live[-1]["live"],
            "copies": [_copy_payload(c) for c in live],
            "reason": sentence, "message": sentence})
    return _import_new(share, source_slugs, copies, importer, request)


def _import_new(share: dict, source_slugs: list[str], copies: list[dict],
                importer: str, request: Request) -> dict:
    """Copy the shared agent into MY account as a brand-new, unrelated agent.

    Ordering matters and is not arbitrary:

      1. validate the link, then the plan cap, BEFORE any filesystem write
      2. RESERVE the next copy_no — the unique index on (share_id, importer,
         copy_no) is what makes a double-clicked button and two browser
         windows converge on one copy instead of three
      3. create the folders + ownership rows, rolling every one of them back
         together if any single step fails (half an imported twin pair is
         worse than none: a number could bind to the survivor and run
         wrong-direction prompts)
      4. only then record what it became and bump the counter
    """
    # Step 2 only catches two requests that reach the INSERT together. A second
    # click that lands after the first one's reservation has committed would
    # compute the NEXT number and sail through — so a reservation that is still
    # being filled in refuses everyone else outright.
    if any(c["pending"] for c in copies):
        raise HTTPException(
            status_code=409,
            detail="This agent is already being imported into your "
                   "account. Refresh your agents list.")
    if _imports_today(importer) >= MAX_IMPORTS_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail=f"You have imported {MAX_IMPORTS_PER_DAY} agents in the "
                   f"last 24 hours. Try again later.")

    # Validate every source definition up front — a 422 after three folders
    # exist is a mess to unwind for no reason.
    _summarize(source_slugs)

    # E7 pre-check before ANY filesystem write. The authoritative re-check runs
    # under an advisory lock inside _claim_slug (via write_agent_dir), which is
    # what makes two concurrent imports at a cap of 1 resolve to one agent.
    builder._assert_agent_quota(importer)

    new_family = _mint_family(share["family"])
    # Never reused: rows for archived copies stay as the trail, and they keep
    # counting toward MAX_IMPORTS_PER_DAY.
    copy_no = max([c["copy_no"] for c in copies], default=0) + 1

    from shared.db.database import get_db
    with get_db() as db:
        try:
            db.execute(
                "INSERT INTO agent_share_imports (share_id, importer, "
                "source_owner, source_family, new_family, new_slugs, copy_no) "
                "VALUES (?,?,?,?,?,?,?)",
                (share["id"], importer, share["owner"], share["family"],
                 new_family, "[]", copy_no))
        except Exception as exc:
            if re.search(r"unique|duplicate", str(exc), re.I):
                raise HTTPException(
                    status_code=409,
                    detail="This agent is already being imported into your "
                           "account. Refresh your agents list.")
            raise

    created: list[str] = []
    taken_ids = _existing_agent_ids()
    try:
        for src in source_slugs:
            new_slug = _variant_slug(new_family,
                                     _variant_key(src, share["family"]))
            spec = _prepare_spec(_AGENTS_DIR / src / "agent.yaml", new_slug,
                                 taken_ids)
            builder.write_agent_dir(new_slug, spec, importer)
            created.append(new_slug)
            # write_agent_dir claims the slug with ON CONFLICT DO NOTHING, so
            # a lost race leaves the folder written but owned by someone else.
            # Prove ownership rather than assume it.
            if builder._slug_owner(new_slug) != importer:
                raise HTTPException(
                    status_code=409,
                    detail="That agent name was taken while importing. "
                           "Try again.")
            _deactivate(new_slug)
            _copy_documents(src, new_slug)
    except Exception:
        for slug in created:
            shutil.rmtree(_AGENTS_DIR / slug, ignore_errors=True)
            agent_registry.invalidate(slug)
            builder._unclaim_slug(slug, importer)
        try:
            with get_db() as db:
                db.execute("DELETE FROM agent_share_imports WHERE share_id=? "
                           "AND importer=? AND copy_no=?",
                           (share["id"], importer, copy_no))
        except Exception as exc:
            logger.error("[share] could not release import reservation "
                         "(%s -> %s): %r", share["token"], importer, exc)
        raise

    with get_db() as db:
        db.execute(
            "UPDATE agent_share_imports SET new_family=?, new_slugs=? "
            "WHERE share_id=? AND importer=? AND copy_no=?",
            (new_family, json.dumps(created), share["id"], importer, copy_no))
        db.execute("UPDATE agent_shares SET import_count=import_count+1 "
                   "WHERE id=?", (share["id"],))

    try:
        from shared.audit import record
        record(importer, "agent_share_imported", target=new_family,
               meta={"source_owner": share["owner"],
                     "source_family": share["family"], "slugs": created,
                     "copy_no": copy_no},
               ip=_client_ip(request))
    except Exception:
        pass

    primary = created[0]
    logger.info("[share] %s imported %s from %s as %s (%s)",
                importer, share["family"], share["owner"], new_family,
                ", ".join(created))
    return {"status": "imported", "family": new_family, "slugs": created,
            "primary": primary}


# ── replace ─────────────────────────────────────────────────────────────────

def _restorable(slug: str, owner: str) -> bool:
    """May `owner` have `slug` (re)created as part of a family they hold?

    Yes for a half they archived (their row, no live folder) and for a name
    nothing has ever held. No — and FAIL CLOSED on a DB error — for anything
    else: a row belonging to someone else, or an archive folder nobody owns.
    """
    if (_AGENTS_DIR / slug).exists():
        return False
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute("SELECT owner FROM agents WHERE slug=?",
                         (slug,)).fetchone()
    if row is None:
        return not (_ARCHIVE_DIR / slug).exists()
    return row["owner"] == owner


def _replace_spec(source_yaml: Path, keep_agent_id: str) -> dict:
    """The shared definition, wearing the COPY's identity.

    agent_id stays the copy's own. It is what resolves the document folder
    (documents/store.get_folder_by_agent_id returns the FIRST match platform
    wide), so taking the source's id would point this tenant's RAG lookups at
    the sharer's knowledge base — the exact hazard the import remints to avoid.
    revision/updated_* are left to the store, which stamps them.
    """
    spec = yaml.safe_load(source_yaml.read_text(encoding="utf-8")) or {}
    if not isinstance(spec, dict):
        raise HTTPException(status_code=422,
                            detail="The shared agent's definition is not readable.")
    for field in _HISTORY_FIELDS:
        spec.pop(field, None)
    spec["agent_id"] = keep_agent_id
    return spec


def _replace_copy(share: dict, source_slugs: list[str], copy: dict,
                  importer: str, request: Request) -> dict:
    """Overwrite one of MY copies with the shared agent as it is right now.

    What changes: the definition (script, stages, voice, knowledge) of every
    direction the shared agent has. What does NOT: the slug, the agent_id,
    routing.yaml (MY phone-number binding), bookings.json (MY customers),
    is_active, call history, campaigns — everything that hangs off the slug
    and is mine rather than the sharer's.

    Per direction:
      live in my copy          → versioned overwrite through the store, so the
                                 version I had is one click away in the
                                 agent's revision history
      archived / never existed → created from the shared version. Replacing
                                 "my copy" means the whole agent; silently
                                 leaving a direction behind would hand back
                                 half of what was shared, and the only other
                                 way to get it is a twin ADAPTED from the wrong
                                 script.
      in my copy, not shared   → left exactly as it is

    All validation happens before the first write, and every write is undone
    if a later one fails: a pair running two different versions of a script is
    the state this exists to get people out of.
    """
    from backend.app.agent_definition import store as agent_store

    family = copy["family"]
    _summarize(source_slugs)                 # every source definition loads
    taken_ids = _existing_agent_ids()

    plan: list[dict] = []
    for src in source_slugs:
        key = _variant_key(src, share["family"])
        dst = _variant_slug(family, key)
        src_yaml = _AGENTS_DIR / src / "agent.yaml"
        if dst in copy["live"]:
            current, _ = agent_store.read_agent(dst, agents_dir=_AGENTS_DIR)
            spec = _replace_spec(src_yaml, str(current.get("agent_id") or "")
                                 or _unique_agent_id(f"{dst}-v1", taken_ids))
            action = "replaced"
        elif _restorable(dst, importer):
            spec = _prepare_spec(src_yaml, dst, taken_ids)
            action = "added"
        else:
            plan.append({"src": src, "dst": dst, "action": "skipped"})
            continue
        try:
            AgentDefinition.from_dict(dict(spec))
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=f"The shared agent has a configuration error and "
                       f"cannot be imported: {exc}")
        plan.append({"src": src, "dst": dst, "action": action, "spec": spec})

    if not any(p["action"] == "replaced" for p in plan):
        # Nothing of the shared agent lines up with anything live here (the
        # sharer archived the only direction this copy still has). Adding
        # directions to it would be an import wearing a replace's name.
        raise HTTPException(
            status_code=409,
            detail="The shared agent no longer has the version you hold, so "
                   "there is nothing to replace. Import it as a new agent "
                   "instead.")
    if any(p["action"] == "added" for p in plan):
        # Completing a family I already hold — passes at cap by design.
        builder._assert_agent_quota(importer, family)

    actor = f"{importer}:share"
    reason = f"replaced with the version shared by {share['owner']}"
    undo: list = []
    docs = {"updated": 0, "removed": 0, "kept": 0}
    try:
        for step in plan:
            dst = step["dst"]
            if step["action"] == "replaced":
                path = _AGENTS_DIR / dst / "agent.yaml"
                before = path.read_text(encoding="utf-8")
                agent_store.write_agent(
                    dst, step["spec"], actor=actor, reason=reason,
                    owner=importer, agents_dir=_AGENTS_DIR)

                def _restore(path=path, before=before, dst=dst):
                    path.write_text(before, encoding="utf-8")
                    agent_registry.invalidate(dst)
                undo.append(_restore)
                moved = _sync_documents(step["src"], dst, copy["created_at"])
                for k in docs:
                    docs[k] += int(moved.get(k) or 0)
            elif step["action"] == "added":
                builder.write_agent_dir(dst, step["spec"], importer)

                def _remove(dst=dst):
                    shutil.rmtree(_AGENTS_DIR / dst, ignore_errors=True)
                    agent_registry.invalidate(dst)
                    _rearchive(dst, importer)
                undo.append(_remove)
                if builder._slug_owner(dst) != importer or \
                        not _slug_is_live_for(dst, importer):
                    raise HTTPException(
                        status_code=409,
                        detail="That agent name was taken while importing. "
                               "Try again.")
                _deactivate(dst)
                docs["updated"] += _copy_documents(step["src"], dst)
    except agent_store.OwnerMismatch:
        _run_undo(undo)
        raise HTTPException(status_code=404, detail="Agent not found")
    except Exception:
        _run_undo(undo)
        raise

    touched = [p["dst"] for p in plan if p["action"] in ("replaced", "added")]
    slugs = list(dict.fromkeys(list(copy["slugs"]) + touched))
    from shared.db.database import get_db
    try:
        with get_db() as db:
            db.execute(
                "UPDATE agent_share_imports SET new_slugs=?, "
                "synced_at=CURRENT_TIMESTAMP, sync_count=sync_count+1 "
                "WHERE id=?", (json.dumps(slugs), copy["id"]))
    except Exception as exc:   # the agent IS replaced; this is bookkeeping
        logger.error("[share] could not stamp replace on import %s: %r",
                     copy["id"], exc)

    outcome = {a: [p["dst"] for p in plan if p["action"] == a]
               for a in ("replaced", "added", "skipped")}
    try:
        from shared.audit import record
        record(importer, "agent_share_replaced", target=family,
               meta={"source_owner": share["owner"],
                     "source_family": share["family"], **outcome},
               ip=_client_ip(request))
    except Exception:
        pass
    logger.info("[share] %s replaced %s with %s from %s (replaced=%s added=%s "
                "skipped=%s)", importer, family, share["family"],
                share["owner"], outcome["replaced"], outcome["added"],
                outcome["skipped"])
    return {"status": "replaced", "family": family, "slugs": touched,
            "primary": touched[0], "documents": docs, **outcome}


def _rearchive(slug: str, owner: str) -> None:
    """Undo for an 'added' direction. The row may have existed before (the
    archived half) or been created by this request; re-stamping archived_at
    returns both to a state every reader already treats as gone, without
    deleting a row that calls or revisions may reference."""
    try:
        from shared.db.database import get_db
        with get_db() as db:
            db.execute("UPDATE agents SET archived_at=CURRENT_TIMESTAMP "
                       "WHERE slug=? AND owner=? AND archived_at IS NULL",
                       (slug, owner))
    except Exception as exc:
        logger.error("[share] could not re-archive %s: %r", slug, exc)


def _run_undo(undo: list) -> None:
    for step in reversed(undo):
        try:
            step()
        except Exception as exc:
            logger.error("[share] replace rollback step failed: %r", exc)
