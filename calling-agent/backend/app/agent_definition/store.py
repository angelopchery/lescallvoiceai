"""
store — THE single write choke point for agent YAMLs (plan §5.1, D14).

Every writer (Builder PUT, PUT /raw, all Copilot tools, yaml_filler) routes
through write_agent(), which provides:

  (a) optimistic concurrency  — base_rev sha mismatch → ConflictError (409)
  (b) revision snapshots      — pre-write YAML text into Postgres
                                `agent_revisions` (retention 50/slug, D11)
  (c) server-managed stamps   — revision+1, updated_at, updated_by
  (d) validation              — AgentDefinition.from_dict before any byte hits disk
  (e) atomic write            — tmp + os.replace (to_yaml)
  (f) cache invalidation      — agent_registry.invalidate
  (g) provable scoping (§5.4) — post-write re-read; every changed path must be
      within the requested paths ∪ server-managed; collateral → auto-restore
      the snapshot and raise ScopeError naming the paths.

Lives in calling-agent (importable from BOTH the dashboard and the runtime
web server — dashboard already imports backend.app.*; the reverse would break
the runtime container).
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
from pathlib import Path
from typing import Any, Optional

import yaml

from .diff import compute_diff, is_within
from .registry import agent_registry
from .schema import AgentDefinition

logger = logging.getLogger(__name__)

_AGENTS_DIR_DEFAULT = Path(__file__).resolve().parents[3] / "agents"

# Paths the store itself mutates — always allowed by the scoping proof.
# The pronoun pair is DERIVED from the selected voice by
# AgentDefinition.from_dict (RUNBOOK §42): a write that changes only
# languages.tts_voices legitimately moves them too, and the scope proof must
# not roll that voice change back as collateral.
SERVER_MANAGED_PATHS = {"revision", "updated_at", "updated_by",
                        "identity.pronouns", "personality.pronouns"}

REVISION_RETENTION = 50


class ConflictError(Exception):
    """base_rev does not match the on-disk revision (concurrent edit)."""

    def __init__(self, message: str, current: dict | None = None,
                 current_rev: str = ""):
        super().__init__(message)
        self.current = current
        self.current_rev = current_rev


class ScopeError(Exception):
    """The write changed paths outside the requested scope (auto-restored)."""

    def __init__(self, message: str, unexpected_paths: list[str]):
        super().__init__(message)
        self.unexpected_paths = unexpected_paths


class OwnerMismatch(Exception):
    """Actor's owner does not own this slug."""


# ── direction is IDENTITY for a twin slug (RUNBOOK §60) ─────────────────────
# `acme-inbound` / `acme-outbound` are one family, and the suffix is what the
# Agents card, the number-binding slots, the Copilot's twin mirror and the
# "Add <direction> version" affordance all key on. A YAML whose call_direction
# contradicts its own suffix makes the family show two rows of one direction,
# computes as its OWN counterpart (so the missing version can never be
# offered), and — bound to the slot its name promises — speaks the other
# direction's opening (conversation.py reads `_def.call_direction` for the
# identity-confirm line and the opening). Standalone, pre-twin slugs carry no
# suffix and are unconstrained.
DIRECTIONS = ("inbound", "outbound")


def slug_direction(slug: str) -> Optional[str]:
    """The direction a twin slug's suffix promises, or None for a standalone slug."""
    for d in DIRECTIONS:
        if slug.endswith(f"-{d}"):
            return d
    return None


def twin_of_slug(slug: str) -> Optional[str]:
    """The opposite-direction slug of a twin, or None for a standalone slug."""
    have = slug_direction(slug)
    if have is None:
        return None
    other = "outbound" if have == "inbound" else "inbound"
    return slug[: -len(have)] + other


class DirectionLocked(ValueError):
    """The definition's call_direction contradicts the slug's own suffix."""


def assert_direction_matches_slug(slug: str, definition: dict) -> None:
    """Refuse a twin definition whose call_direction contradicts its slug.

    Every write path (Copilot update/rollback, Builder PUT, twin create,
    share import) funnels through here, so the invariant holds no matter
    which door the flip comes through. The message names the twin to edit
    instead — it is what the Copilot reads to route the edit correctly."""
    want = slug_direction(slug)
    if want is None:
        return
    have = str((definition or {}).get("call_direction") or "inbound").strip().lower()
    if have == want:
        return
    raise DirectionLocked(
        f"'{slug}' is the {want} version of this agent: its call type is fixed "
        f"by its name and cannot become {have}. To work on the {have} version, "
        f"edit '{twin_of_slug(slug)}' instead (create it with 'Add {have} "
        "version' if it does not exist yet).")


def _yaml_path(slug: str, agents_dir: Optional[Path] = None) -> Path:
    return (agents_dir or _AGENTS_DIR_DEFAULT) / slug / "agent.yaml"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def read_agent(slug: str, agents_dir: Optional[Path] = None) -> tuple[dict, str]:
    """(definition dict, rev sha256 of the raw YAML text)."""
    path = _yaml_path(slug, agents_dir)
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    return data, _sha(text)


def _slug_db_owner(slug: str) -> Optional[str]:
    """agents-index owner for a slug; None if no row / DB unavailable is an
    ERROR (fail closed) only when an owner check was requested."""
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute("SELECT owner FROM agents WHERE slug=?", (slug,)).fetchone()
        return (row["owner"] or None) if row else None


def _snapshot_revision(slug: str, owner: str, actor: str, reason: str,
                       sha: str, yaml_text: str) -> int | None:
    """Pre-write snapshot into agent_revisions (best-effort must NOT be
    best-effort: losing the undo trail silently defeats the feature — raise).
    Returns the row id so a scope-check rollback can retract it."""
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute(
            "INSERT INTO agent_revisions (slug, owner, actor, reason, sha256, "
            "yaml_text) VALUES (?,?,?,?,?,?) RETURNING id",
            (slug, owner or None, actor or None, reason or None, sha,
             yaml_text)).fetchone()
        snap_id = row["id"] if row else None
        # Retention: keep the newest REVISION_RETENTION per slug.
        rows = db.execute(
            "SELECT id FROM agent_revisions WHERE slug=? ORDER BY id DESC",
            (slug,)).fetchall()
        stale = [r["id"] for r in rows[REVISION_RETENTION:]]
        for rid in stale:
            db.execute("DELETE FROM agent_revisions WHERE id=?", (rid,))
    return snap_id


def _retract_snapshot(snap_id: int | None) -> None:
    """Remove a snapshot whose write was rolled back by the scope check —
    otherwise every rejected apply leaves a phantom revision row describing a
    change that never happened."""
    if snap_id is None:
        return
    try:
        from shared.db.database import get_db
        with get_db() as db:
            db.execute("DELETE FROM agent_revisions WHERE id=?", (snap_id,))
    except Exception:
        logger.warning("[store] could not retract snapshot %s", snap_id,
                       exc_info=True)


def list_revisions(slug: str, limit: int = 50) -> list[dict]:
    from shared.db.database import get_db
    with get_db() as db:
        rows = db.execute(
            "SELECT id, slug, owner, actor, reason, sha256, created_at "
            "FROM agent_revisions WHERE slug=? ORDER BY id DESC LIMIT ?",
            (slug, limit)).fetchall()
    return [dict(r) for r in rows]


def get_revision(slug: str, rev_id: int) -> Optional[dict]:
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM agent_revisions WHERE slug=? AND id=?",
            (slug, rev_id)).fetchone()
    return dict(row) if row else None


def write_agent(
    slug: str,
    new_dict: dict,
    actor: str,
    base_rev: Optional[str] = None,
    reason: str = "",
    owner: str = "",
    is_admin: bool = False,
    expected_paths: Optional[set[str]] = None,
    agents_dir: Optional[Path] = None,
) -> dict:
    """Validated, versioned, concurrency-safe, scope-proven agent write.

    expected_paths: dotted path prefixes the caller intended to change.
      None  → no scoping proof (full-document replace, e.g. Builder PUT).
      set() → only server-managed fields may change.
    Returns {revision, rev, effective, changed_paths}.
    """
    path = _yaml_path(slug, agents_dir)
    if not path.exists():
        raise FileNotFoundError(f"Agent '{slug}' not found at {path}")

    # (a) current + sha
    current_text = path.read_text(encoding="utf-8")
    current_sha = _sha(current_text)
    current_dict = yaml.safe_load(current_text) or {}

    # Owner re-verification (plan §9): every write path inherits this guard.
    if owner and not is_admin:
        db_owner = _slug_db_owner(slug)
        if db_owner is not None and db_owner != owner:
            raise OwnerMismatch(f"'{slug}' is not owned by {owner}")

    # (b) optimistic concurrency
    if base_rev is not None and base_rev != current_sha:
        raise ConflictError(
            f"Agent '{slug}' changed since it was loaded (rev mismatch)",
            current=current_dict, current_rev=current_sha)

    # (b2) direction is identity for a twin slug (RUNBOOK §60). Checked BEFORE
    # the snapshot so a refused write leaves no revision row behind. A missing
    # field is filled from the slug rather than refused — absent, the schema
    # would default it to inbound, which is the very corruption this guards.
    new_dict = dict(new_dict)
    if slug_direction(slug) and not new_dict.get("call_direction"):
        new_dict["call_direction"] = slug_direction(slug)
    assert_direction_matches_slug(slug, new_dict)

    # (c) snapshot the PRE-write YAML text (retracted if the scope check
    # rolls the write back — see below)
    _snap_id = _snapshot_revision(slug, owner, actor, reason, current_sha,
                                  current_text)

    # (d) stamp server-managed fields
    new_dict = dict(new_dict)
    try:
        new_dict["revision"] = int(current_dict.get("revision") or 0) + 1
    except (TypeError, ValueError):
        new_dict["revision"] = 1
    new_dict["updated_at"] = _now_iso()
    new_dict["updated_by"] = actor

    # (e) validate BEFORE any byte hits disk
    agent = AgentDefinition.from_dict(new_dict)

    # (f) atomic write
    agent.to_yaml(path)

    # (g) provable scoping: re-read and assert every changed path is within
    # the requested scope ∪ server-managed. NOTE the comparison runs against
    # the canonical (from_dict→asdict) forms of both sides so serialization
    # normalization (e.g. tuple→list) never reads as collateral.
    changed_paths: list[str] = []
    if expected_paths is not None:
        reread_text = path.read_text(encoding="utf-8")
        reread = yaml.safe_load(reread_text) or {}
        canon_old = dataclasses.asdict(AgentDefinition.from_dict(current_dict))
        canon_new = dataclasses.asdict(AgentDefinition.from_dict(reread))
        diff = compute_diff(canon_old, canon_new)
        allowed = set(expected_paths) | SERVER_MANAGED_PATHS
        unexpected = [d["path"] for d in diff if not is_within(d["path"], allowed)]
        changed_paths = [d["path"] for d in diff]
        if unexpected:
            # Auto-restore the snapshot, loudly — and retract the revision
            # row, which described a write that now never happened.
            path.write_text(current_text, encoding="utf-8")
            agent_registry.invalidate(slug)
            _retract_snapshot(_snap_id)
            raise ScopeError(
                "Write touched paths outside the requested change and was "
                f"rolled back: {', '.join(unexpected[:10])}",
                unexpected_paths=unexpected)

    agent_registry.invalidate(slug)
    new_sha = _sha(path.read_text(encoding="utf-8"))
    logger.info("[store] wrote '%s' rev=%s actor=%s reason=%r (%d changed paths)",
                slug, new_dict["revision"], actor, reason, len(changed_paths))
    return {
        "revision": new_dict["revision"],
        "rev": new_sha,
        "effective": "next_call",
        "changed_paths": changed_paths,
    }


def rollback_agent(slug: str, rev_id: int, actor: str, owner: str = "",
                   is_admin: bool = False,
                   agents_dir: Optional[Path] = None) -> dict:
    """Restore a snapshot via write_agent (itself snapshotting — rollback is
    undoable). The snapshot is validated before it lands."""
    snap = get_revision(slug, rev_id)
    if not snap:
        raise FileNotFoundError(f"Revision {rev_id} for '{slug}' not found")
    data = yaml.safe_load(snap["yaml_text"]) or {}
    result = write_agent(
        slug, data, actor=actor, reason=f"rollback to revision {rev_id}",
        owner=owner, is_admin=is_admin, agents_dir=agents_dir)
    result["rolled_back_to"] = rev_id
    return result
