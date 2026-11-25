"""
Agent Builder API — mounted into the LesCall dashboard (dashboard/server.py).

This exposes the data-driven Agent Builder control plane (create / edit / raw
YAML / routing / documents+RAG / validate / intent-preview / TTS-preview /
activate) on the SAME FastAPI app + port (8080) the React dashboard already
talks to, so the builder lives on the main dashboard with no extra service.

Design notes
------------
* Reuses the real runtime modules under calling-agent/backend/app — there is
  one source of truth for the agent schema/registry/documents, shared with the
  telephony pipeline.
* Drops the standalone builder's subprocess Runtime-Control and Sarvam key-pool
  routes: the ops dashboard already owns runtime control + the key pool, so we
  avoid /api/runtime/* and key-route collisions.
* `activate` writes agents/active_agent.json — the single-active-agent selector
  that conversation_call_bridge.py reads at call time. That file IS the wire
  between "build an agent in the dashboard" and "telephony runs that agent".
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import shutil
import struct
import sys
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response

logger = logging.getLogger("agent_builder_api")

# ── Make the calling-agent backend importable from the dashboard process ────
_REPO_ROOT = Path(__file__).resolve().parents[1]          # /home/angelo/lescall
_CALLING_AGENT = _REPO_ROOT / "calling-agent"
if str(_CALLING_AGENT) not in sys.path:
    sys.path.insert(0, str(_CALLING_AGENT))

# Load calling-agent/.env so the builder's RAG (OpenAI embeddings / yaml_filler)
# and TTS preview (Sarvam) have their keys in os.environ even though the existing
# dashboard reads keys straight from the .env file.
try:
    from dotenv import load_dotenv
    load_dotenv(_CALLING_AGENT / ".env")
except Exception:  # pragma: no cover
    pass

from backend.app.agent_definition.schema import AgentDefinition   # noqa: E402
from backend.app.agent_definition.registry import agent_registry  # noqa: E402
from backend.app.agent_definition import store as agent_store       # noqa: E402

_AGENTS_DIR = _CALLING_AGENT / "agents"
_ACTIVE_FILE = _AGENTS_DIR / "active_agent.json"


# ── multi-tenant boundary ────────────────────────────────────────────────────
# Every /api/agents route runs _tenancy_dep (router-level dependency): it
# resolves the caller's owner email, stashes it in a ContextVar (so the
# Copilot's in-process calls to these handlers inherit the same tenant), and
# 404s any {name} the caller doesn't own. Roles are flattened — every user is
# owner-scoped; nobody sees other tenants' agents.
# Unclaimed slugs (no agents-index row) are hidden from tenants — creation
# claims the slug for its creator.
from contextvars import ContextVar  # noqa: E402

_REQ_OWNER: ContextVar[str | None] = ContextVar("lescall_req_owner", default=None)


def _ctx_owner() -> str:
    """Owner for the current request context (builder or copilot)."""
    owner = _REQ_OWNER.get()
    if owner:
        return owner
    import auth
    return auth.DEFAULT_OWNER  # non-HTTP/internal callers act as the admin


def _ctx_is_admin() -> bool:
    import auth
    return auth.is_admin(_ctx_owner())


def _slug_owner(slug: str) -> str | None:
    """agents-index owner for a slug; None if no row. Fails CLOSED: a DB error
    raises, it never silently grants access."""
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute("SELECT owner FROM agents WHERE slug=?", (slug,)).fetchone()
        return (row["owner"] or None) if row else None


def _claim_slug(slug: str, owner: str, display_name: str = "") -> None:
    from shared.db.database import get_db
    with get_db() as db:
        # E7 backstop. The real gate is _assert_agent_quota() at the top of
        # each create handler — this one runs inside the same transaction as
        # the claim, under an advisory lock, because a bare count-then-write is
        # TOCTOU: two concurrent creates on a cap of 1 both read 0 and both
        # succeed.
        _billing_claim_guard(db, owner, slug)
        db.execute(
            "INSERT INTO agents (slug, owner, display_name) VALUES (?,?,?) "
            "ON CONFLICT (slug) DO NOTHING", (slug, owner, display_name or None))
        db.execute("UPDATE agents SET owner=? WHERE slug=? AND owner IS NULL",
                   (owner, slug))
        # Re-creating a slug you archived makes it LIVE again. The INSERT above
        # is a no-op when the archived row still exists, so without this the
        # new agent kept its old archived_at: on disk and answering calls, but
        # invisible to the plan cap (a free extra agent) and to sharing (404).
        # Owner-scoped, and after the guard, so it can neither resurrect
        # another tenant's archive nor step around the cap.
        db.execute("UPDATE agents SET archived_at=NULL WHERE slug=? AND owner=? "
                   "AND archived_at IS NOT NULL", (slug, owner))


def _billing_claim_guard(db, owner: str, slug: str) -> None:
    """Serialise cap checking per owner and re-verify under the lock.

    pg_advisory_xact_lock is released automatically when the transaction ends,
    so a crashed request cannot leave a tenant permanently unable to create.
    """
    try:
        from shared.billing.entitlements import agent_cap, agent_families_used
        from shared.billing.errors import BillingRefusal, CODE_CAP_AGENTS
    except Exception:
        return
    # No `if not enforcement_enabled(): return` here any more. agent_cap()
    # owns that decision now, because the trial floor has to bind while the
    # global switch stays off — and this guard, not the pre-check, is the one
    # that runs inside the INSERT's transaction. Leaving the short-circuit
    # would have made the cap a race away from bypassable: two concurrent
    # creates both pass _assert_agent_quota, and nothing downstream says no.
    try:
        cap, trial = agent_cap(owner)
    except Exception as exc:
        logger.error("[builder] agent cap lookup failed for %s: %r", owner, exc)
        return
    if cap is None:
        return
    try:
        db.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (owner,))
    except Exception:
        pass  # sqlite dev fallback has no advisory locks
    try:
        family = agent_family(slug)
        existing = db.execute(
            "SELECT 1 FROM agents WHERE owner=? AND archived_at IS NULL "
            "AND (slug=? OR slug=? OR slug=?) LIMIT 1",
            (owner, family, f"{family}-inbound", f"{family}-outbound")).fetchone()
        if existing:
            return  # the other half of a pair the tenant already owns
        used = agent_families_used(owner)
    except BillingRefusal:
        raise
    except Exception as exc:
        logger.error("[builder] agent cap re-check failed for %s: %r", owner, exc)
        return
    if used >= int(cap):
        # Same sentence the pre-check produces — a user who hits the race
        # must not be told something different from a user who does not.
        from shared.billing.entitlements import agent_cap_message
        raise BillingRefusal(
            agent_cap_message(int(cap), used, trial),
            CODE_CAP_AGENTS, agents_used=used, agents_limit=int(cap),
            trial=trial)


def _billing_llm_gate(owner: str, surface: str) -> None:
    """E9-E11. One daily budget across every LLM surface a tenant can drive.

    Raises BillingRefusal (402) when the budget is spent; records the turn
    otherwise. Never blocks on a billing outage — the check itself fails open.

    Both ceilings, via check_copilot_budget: messages per day AND tokens per
    day. These surfaces (document extraction, YAML filling, reindex) do not
    report usage back, so they spend the turn budget and are bounded by the
    token budget the chat surface fills — which is the right way round, since
    a tenant who has already burned the day's tokens in chat should not then
    get an unmetered extraction pipeline.
    """
    try:
        from shared.billing.entitlements import (
            check_copilot_budget, record_copilot_turn)
    except Exception:
        return
    check_copilot_budget(owner).raise_for_http()
    record_copilot_turn(owner, surface)


def _assert_agent_quota(owner: str, family: str = "") -> None:
    """E7 pre-check, BEFORE any filesystem write.

    Putting the only check inside _claim_slug was not enough. write_agent_dir
    wraps the claim in `except Exception` and re-raises a 500, so the SPA's 402
    handling would never fire; the copilot turns it into a ValueError string;
    and in the twin loop the inbound slug's row is already inserted when the
    outbound one raises, so the rollback rmtree leaves an owned, folder-less
    row that eats a cap slot forever and 404s on archive.
    """
    from shared.billing.entitlements import check_agent_create
    check_agent_create(owner, family).raise_for_http()


def _visible_slugs(owner: str) -> set[str] | None:
    """Slugs this owner may see; None = unrestricted (admin)."""
    import auth
    if auth.is_admin(owner):
        return None
    from shared.db.database import get_db
    with get_db() as db:
        rows = db.execute("SELECT slug FROM agents WHERE owner=?", (owner,)).fetchall()
    return {r["slug"] for r in rows}


def _assert_slug_access(slug: str, owner: str) -> None:
    import auth
    if auth.is_admin(owner):
        return
    if _slug_owner(slug) != owner:
        # 404 (not 403): don't confirm a foreign agent's existence
        raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")


async def _tenancy_dep(request: Request):
    """MUST stay async: a sync dependency runs in a threadpool and its
    ContextVar assignment would not reach the handler's task."""
    import auth
    owner = auth.current_owner(request)  # 401 when OAuth is on and no session
    _REQ_OWNER.set(owner)
    request.state.owner = owner
    name = request.path_params.get("name")
    if name:
        _assert_slug_access(name, owner)


router = APIRouter(dependencies=[Depends(_tenancy_dep)])


# ── helpers ─────────────────────────────────────────────────────────────────
def _read_active() -> str | None:
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT slug FROM agents WHERE is_active=1 "
                "ORDER BY updated_at DESC LIMIT 1").fetchone()
        if row and row["slug"]:
            return row["slug"]
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("[builder] DB active-agent lookup failed: %r", exc)
    try:  # legacy fallback while active_agent.json is still dual-written
        if _ACTIVE_FILE.exists():
            data = json.loads(_ACTIVE_FILE.read_text(encoding="utf-8")) or {}
            return data.get("active")
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("[builder] could not read active_agent.json: %r", exc)
    return None


def _write_active(slug: str | None) -> None:
    """Single-active-agent selector: exactly one agents row has is_active=1
    (or none, when slug is None). Dual-writes the legacy JSON until every
    reader has soaked on the DB path."""
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("UPDATE agents SET is_active=0 WHERE is_active=1")
        if slug:
            db.execute(
                "UPDATE agents SET is_active=1, updated_at=CURRENT_TIMESTAMP "
                "WHERE slug=?", (slug,))
    try:
        if slug:
            _ACTIVE_FILE.write_text(json.dumps({"active": slug}, indent=2),
                                    encoding="utf-8")
        elif _ACTIVE_FILE.exists():
            _ACTIVE_FILE.unlink()
    except Exception as exc:  # pragma: no cover - legacy mirror only
        logger.warning("[builder] could not mirror active_agent.json: %r", exc)


DIRECTIONS = ("inbound", "outbound")


def agent_family(slug: str) -> str:
    """The shared base name of a twin pair: 'acme-outbound' -> 'acme'.

    Slugs that predate the twin convention are their own family, so nothing
    created before this change gets grouped with an unrelated agent.
    """
    for d in DIRECTIONS:
        if slug.endswith(f"-{d}"):
            return slug[: -len(d) - 1]
    return slug


def twin_slug(base: str, direction: str) -> str:
    return f"{agent_family(base)}-{direction}"


def counterpart_of(slug: str) -> str | None:
    """The other half of a twin pair, or None for a pre-twin standalone slug."""
    for d in DIRECTIONS:
        if slug.endswith(f"-{d}"):
            other = "outbound" if d == "inbound" else "inbound"
            return twin_slug(slug, other)
    return None


# ── direction adaptation ────────────────────────────────────────────────────
#
# A twin is NOT a copy with one field flipped. Who speaks first changes the
# script: an inbound agent answers a ringing phone and has no idea who is
# calling, while an outbound agent opens the conversation and already knows the
# contact's name. Copying an inbound definition into the outbound slot is
# exactly what produces the "the outbound agent behaves like a receptionist"
# reports that _warn_direction_mismatch was added to explain.
#
# Measured against the hand-built outbound_lead_qualifier pair, the differences
# that actually matter are: the opening greeting, the first stage's goal, the
# name slot (ask vs already-known), and cold-call objection handling. Those four
# are what this transform rewrites; everything else — knowledge base, later
# stages, languages, calendar — is direction-neutral and carried over intact.
#
# The rewrite is deterministic on purpose. It runs instantly, costs no LLM
# quota, and is testable; the result is a working starting point the user then
# refines in the editor or via Copilot, not a finished script.

_OUTBOUND_GREETING = ("Hello, hope you're having a very good {current_time}. "
                      "Am I speaking with {contact_name}?")
_INBOUND_GREETING = "Hello"

_OUTBOUND_OBJECTIONS = {
    "en": [
        "I understand your time is valuable. I will keep this very brief, or if "
        "you prefer, I can call back at a better time.",
        "No problem at all. If you change your mind, feel free to reach out to "
        "us anytime. Thank you!",
    ],
}


def _first_stage_goal(direction: str, company: str) -> str:
    if direction == "outbound":
        return (f"Greet the caller warmly by saying: 'Hello, hope you're having a "
                f"very good {{current_time}}. Am I speaking with {{contact_name}}?' "
                f"and confirm you are speaking to the right person. Then introduce "
                f"yourself and {company or 'the company'}, and check this is a good "
                f"time to talk.")
    return ("Ask 'Could I please know who is calling?' to get the caller's name. "
            "Once they provide their name, store it and ask how you can help them "
            "today.")


def adapt_direction(spec: dict, direction: str) -> dict:
    """Return `spec` rewritten to work as a `direction` agent.

    Pure and non-mutating: the caller keeps its original dict. Only the four
    direction-sensitive areas above are touched.
    """
    import copy

    out = copy.deepcopy(spec)
    out["call_direction"] = direction
    outbound = direction == "outbound"
    company = ((out.get("identity") or {}).get("company_name") or "").strip()

    # 1. Opening greeting — outbound speaks first and names the contact.
    #    The engine opens outbound calls from outbound.opening_greeting when
    #    it is set (response_bank.opening_greeting is the inbound/fallback
    #    line), so the outbound opener is written to BOTH homes with the SAME
    #    text — one audible truth, no shadowed field saying something else
    #    (the 2026-08-01 Jethalal edit landed in a shadowed field). An
    #    existing per-language outbound.opening_greeting is kept as-is; the
    #    inbound twin gets the dormant outbound opener cleared for the same
    #    reason.
    bank = out.setdefault("response_bank", {})
    ob_cfg = out.setdefault("outbound", {})
    if isinstance(bank, dict):
        if outbound:
            existing_ob = {k: str(v) for k, v in
                           ((ob_cfg.get("opening_greeting") or {})
                            if isinstance(ob_cfg, dict) else {}).items()
                           if str(v).strip()}
            if existing_ob:  # keep the authored per-language opener; align fallback
                bank["opening_greeting"] = (existing_ob.get("en")
                                            or next(iter(existing_ob.values())))
            else:
                bank["opening_greeting"] = _OUTBOUND_GREETING
                if isinstance(ob_cfg, dict):
                    ob_cfg["opening_greeting"] = {"en": _OUTBOUND_GREETING}
        else:
            bank["opening_greeting"] = _INBOUND_GREETING
            if isinstance(ob_cfg, dict):
                ob_cfg["opening_greeting"] = {}
        # 4. Cold-call objections only make sense when WE placed the call.
        if outbound:
            if not (bank.get("objection_responses") or {}):
                bank["objection_responses"] = copy.deepcopy(_OUTBOUND_OBJECTIONS)
        else:
            bank["objection_responses"] = {}

    # 2. First stage goal — greet-and-confirm vs ask-who-is-calling.
    stages = ((out.get("call_flow") or {}).get("stages") or [])
    if stages and isinstance(stages[0], dict):
        stages[0]["goal"] = _first_stage_goal(direction, company)

    # 3. The name slot. Outbound already has the name from the contact record
    #    ({contact_name}), so asking for it sounds like the agent wasn't paying
    #    attention; max_asks=0 keeps the slot for storage without interrogating.
    for slot in (out.get("slots") or []):
        if not isinstance(slot, dict):
            continue
        if str(slot.get("name") or "").strip().lower() not in _NAME_SLOTS:
            continue
        if outbound:
            slot["question"] = {}
            slot["reask_question"] = {}
            slot["max_asks"] = 0
        else:
            if not (slot.get("question") or {}):
                slot["question"] = {"en": "Could I please know who is calling?"}
            if not (slot.get("reask_question") or {}):
                slot["reask_question"] = {"en": "Could I please get your name?"}
            if not slot.get("max_asks"):
                slot["max_asks"] = 2
    return out


_NAME_SLOTS = {"caller_name", "customer_name", "contact_name", "name",
               "full_name", "person_name"}


def _agent_summary(name: str, agent: AgentDefinition) -> dict:
    return {
        "name": name,
        "family": agent_family(name),
        "agent_id": agent.agent_id,
        "version": agent.version,
        "agent_name": agent.identity.agent_name,
        "company": agent.identity.company_name,
        "languages": agent.languages.supported,
        "locations": agent.knowledge_base.locations,
        "stages": [s.id for s in agent.call_flow.stages],
        "call_direction": getattr(agent, "call_direction", "inbound"),
        "calendar_slots": len(agent.calendar.mock_slots or []),
    }


# ── list / read ─────────────────────────────────────────────────────────────
@router.get("/api/agents")
async def list_agents() -> dict:
    """List the caller's agent definitions, plus which is globally active.

    Tenants see only slugs they own (agents index); admins see everything.
    The global-active slug is admin infrastructure — hidden from tenants.
    """
    visible = _visible_slugs(_ctx_owner())
    is_admin = visible is None
    active = _read_active() if is_admin else None
    result = []
    on_disk = {p.parent.name for p in _AGENTS_DIR.glob("*/agent.yaml")}
    for yaml_path in sorted(_AGENTS_DIR.glob("*/agent.yaml")):
        name = yaml_path.parent.name
        if visible is not None and name not in visible:
            continue
        try:
            agent = AgentDefinition.from_yaml(yaml_path)
            summary = _agent_summary(name, agent)
            summary["active"] = (name == active)
            # Which slug the opposite direction would live under, and whether it
            # exists yet — drives the "Add the twin" affordance. Computed
            # against every slug on disk (not just visible ones) so a name
            # collision with another tenant's agent surfaces as "taken" rather
            # than offering a create that would 409.
            # The SLUG is the identity: a twin's counterpart is the other
            # suffix, whatever its YAML says. Derived from the data, a
            # mis-directed twin (RUNBOOK §60) computed as its OWN counterpart
            # and the family could never be offered its missing version.
            twin = counterpart_of(name)
            if twin is None:  # standalone (pre-twin) slug: only the data can say
                direction = summary.get("call_direction") or "inbound"
                other = "outbound" if direction == "inbound" else "inbound"
                twin = twin_slug(name, other)
            else:
                other = "outbound" if name.endswith("-inbound") else "inbound"
            summary["counterpart"] = twin
            summary["counterpart_direction"] = other
            summary["has_counterpart"] = twin in on_disk
            result.append(summary)
        except Exception as exc:
            result.append({"name": name, "error": str(exc), "active": name == active})
    return {"agents": result, "active": active}


@router.get("/api/agents/active")
async def get_active_agent() -> dict:
    """Slug of the global-fallback agent (admin infrastructure; null for tenants)."""
    return {"active": _read_active() if _ctx_is_admin() else None}


@router.get("/api/agents/{name}")
async def get_agent(name: str) -> dict:
    """Full configuration of a named agent (the editor reads this).

    `_rev` is the optimistic-concurrency token: send it back as If-Match on
    PUT so an interleaved edit 409s instead of being silently clobbered."""
    yaml_path = _AGENTS_DIR / name / "agent.yaml"
    if not yaml_path.exists():
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    try:
        from backend.app.agent_definition import store as agent_store
        _, rev = agent_store.read_agent(name, _AGENTS_DIR)
        agent = AgentDefinition.from_yaml(yaml_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {**dataclasses.asdict(agent), "_rev": rev}


# ── create / update / archive ───────────────────────────────────────────────
def write_agent_dir(name: str, spec: dict, owner: str) -> AgentDefinition:
    """Validate + persist ONE agent (yaml, registry, ownership claim).

    Raises HTTPException on any failure, having already removed a partial
    directory — the caller is responsible for unwinding earlier siblings.
    """
    target_dir = _AGENTS_DIR / name
    # A twin's direction is its slug (RUNBOOK §60) — the same check every
    # update goes through in store.write_agent, applied at creation too.
    try:
        agent_store.assert_direction_matches_slug(name, spec)
    except agent_store.DirectionLocked as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    try:
        agent = AgentDefinition.from_dict(spec)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Schema validation failed: {exc}")
    target_dir.mkdir(parents=True, exist_ok=True)
    agent.to_yaml(target_dir / "agent.yaml")
    agent_registry.put(name, agent)
    try:
        _claim_slug(name, owner, agent.identity.agent_name)
    except HTTPException:
        # A billing refusal (402) or any other deliberate HTTP error must reach
        # the client UNCHANGED. Folding it into the 500 below is what made the
        # SPA render "could not register ownership: ..." instead of the upgrade
        # panel, so the cap looked like a server bug to every user who hit it.
        shutil.rmtree(target_dir, ignore_errors=True)
        agent_registry.invalidate(name)
        raise
    except Exception as exc:
        # Fail CLOSED: an unclaimed agent would be invisible/unusable for a
        # tenant and grabbable later — undo the create instead.
        if not _ctx_is_admin():
            shutil.rmtree(target_dir, ignore_errors=True)
            agent_registry.invalidate(name)
            raise HTTPException(status_code=500,
                                detail=f"could not register ownership: {exc}")
        logger.warning("[builder] ownership claim failed for %s: %r", name, exc)
    return agent


def _unclaim_slug(slug: str, owner: str) -> None:
    """Undo a claim during a failed multi-agent create.

    Without this the twin rollback deletes the FOLDER but leaves the row, and
    an owned row with no folder is the worst of both worlds: it consumes a cap
    slot forever, it is invisible in the agents list, and DELETE returns 404
    because the archive path checks the filesystem first.
    """
    try:
        from shared.db.database import get_db
        with get_db() as db:
            db.execute("DELETE FROM agents WHERE slug=? AND owner=?", (slug, owner))
    except Exception as exc:
        logger.error("[builder] could not release slug %s: %r", slug, exc)


def _assert_slug_free(name: str, owner: str) -> None:
    if (_AGENTS_DIR / name).exists():
        raise HTTPException(status_code=409,
                            detail=f"Agent '{name}' already exists — use PUT to update")
    # Never write into a slug the DB already assigns to another tenant (guards
    # the narrow orphaned-row case where the folder was removed but the owner
    # row survived). An ARCHIVED row is claimable again — archiving frees the
    # slot, and a tenant who archives 'sales' must be able to create 'sales'
    # afresh rather than being blocked by their own deleted agent.
    existing = _slug_owner(name)
    if existing and existing != owner and not _ctx_is_admin() \
            and not _slug_archived(name):
        raise HTTPException(status_code=409, detail=f"Agent '{name}' already exists")


def _slug_archived(slug: str) -> bool:
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute("SELECT archived_at FROM agents WHERE slug=?",
                             (slug,)).fetchone()
        return bool(row and row["archived_at"])
    except Exception:
        return False


@router.post("/api/agents")
async def create_agent(request: Request) -> dict:
    """Create an agent PAIR. Body: {"name": "<base>", ...agent_definition...}.

    One create yields two agents — `<base>-inbound` and `<base>-outbound` —
    because the two directions need genuinely different prompts (a receptionist
    answering vs a rep opening a cold call). Both start from the submitted
    definition with `call_direction` set accordingly; they are independent
    records from then on and are edited separately.

    Pass `"twin": false` to create a single agent under the exact name given
    (used by migrations/tests and any caller managing its own naming).
    """
    body = await request.json()
    name = body.pop("name", None)
    make_twins = body.pop("twin", True)
    if not name:
        raise HTTPException(status_code=400, detail="'name' field is required")
    if not name.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="'name' must be alphanumeric with _ or -")
    owner = _ctx_owner()

    if not make_twins:
        _assert_slug_free(name, owner)
        _assert_agent_quota(owner, agent_family(name))
        agent = write_agent_dir(name, body, owner)
        logger.info("[builder] created agent '%s' (owner=%s)", name, owner)
        return {"status": "created", "name": name, "agent_id": agent.agent_id}

    # The direction the caller asked for is the one the editor opens on.
    primary_direction = str(body.get("call_direction") or "inbound")
    if primary_direction not in DIRECTIONS:
        primary_direction = "inbound"
    family = agent_family(name)
    slugs = {d: twin_slug(family, d) for d in DIRECTIONS}
    for slug in slugs.values():
        _assert_slug_free(slug, owner)
    # E7 — before ANY filesystem write. A twin pair is ONE family against the
    # cap, so this is checked once for the pair, not once per slug.
    _assert_agent_quota(owner, family)

    created: list[str] = []
    try:
        agents = {}
        for direction, slug in slugs.items():
            # adapt_direction, not a bare call_direction flip: the halves used
            # to be byte-identical apart from that one field, so whichever
            # direction the author did NOT have in mind got the other one's
            # greeting and opening stage.
            agents[direction] = write_agent_dir(
                slug, adapt_direction(body, direction), owner)
            created.append(slug)
    except Exception:
        # Half a pair is worse than none: a number could bind to the survivor
        # and silently use the wrong-direction prompts.
        for slug in created:
            shutil.rmtree(_AGENTS_DIR / slug, ignore_errors=True)
            agent_registry.invalidate(slug)
            # Release the ownership row too. Deleting only the folder leaves an
            # owned, folder-less row that consumes a plan slot forever.
            _unclaim_slug(slug, owner)
        raise

    primary = slugs[primary_direction]
    logger.info("[builder] created agent pair '%s' (%s + %s, owner=%s)",
                family, slugs["inbound"], slugs["outbound"], owner)
    return {"status": "created", "name": primary,
            "agent_id": agents[primary_direction].agent_id,
            "family": family, "twins": slugs}


@router.post("/api/agents/{name}/twin")
async def create_twin(name: str) -> dict:
    """Create the opposite-direction counterpart of an existing agent.

    For the agents that predate the twin convention (a standalone slug with no
    `-inbound`/`-outbound` suffix) and for any pair where one half was archived.
    The source agent is left untouched; the new one is an INDEPENDENT record
    from creation — editing one never changes the other.

    The copy is run through adapt_direction(), so the twin opens the call the
    way its direction requires instead of inheriting a greeting written for the
    other one. It is a starting point: refine it in the editor or via Copilot.
    """
    src_path = _AGENTS_DIR / name / "agent.yaml"
    if not src_path.exists():
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    owner = _ctx_owner()
    _assert_slug_access(name, owner)

    src = AgentDefinition.from_yaml(src_path)
    src_direction = str(getattr(src, "call_direction", "inbound") or "inbound")
    if src_direction not in DIRECTIONS:
        src_direction = "inbound"
    direction = "outbound" if src_direction == "inbound" else "inbound"

    family = agent_family(name)
    target = twin_slug(family, direction)
    if (_AGENTS_DIR / target).exists():
        raise HTTPException(status_code=409, detail={
            "error": "exists", "name": target,
            "message": f"'{target}' already exists — open it instead."})
    _assert_slug_free(target, owner)
    # A twin completes an EXISTING family, so this normally passes even at cap
    # — check_agent_create() returns ok when the family is already owned.
    _assert_agent_quota(owner, family)

    import yaml as _yaml
    with src_path.open(encoding="utf-8") as fh:
        spec = _yaml.safe_load(fh) or {}
    # Revision/audit fields belong to the SOURCE's history, not the new record.
    for k in ("revision", "updated_at", "created_at", "_rev", "audit"):
        spec.pop(k, None)

    agent = write_agent_dir(target, adapt_direction(spec, direction), owner)
    logger.info("[builder] twin created: %s -> %s (%s, owner=%s)",
                name, target, direction, owner)
    return {"status": "created", "name": target, "direction": direction,
            "family": family, "source": name, "agent_id": agent.agent_id}


@router.put("/api/agents/{name}")
async def update_agent(name: str, request: Request) -> dict:
    """Replace an existing agent's YAML (full agent definition body).

    Routes through the store choke point: revision snapshot + audit stamps +
    atomic write. Optional If-Match (the `_rev` from GET) → 409 on a
    concurrent edit; absent = legacy last-write-wins (old clients keep working)."""
    yaml_path = _AGENTS_DIR / name / "agent.yaml"
    if not yaml_path.exists():
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found — POST to create")
    body = await request.json()
    body.pop("name", None)
    body.pop("_rev", None)
    base_rev = request.headers.get("If-Match") or None
    from backend.app.agent_definition import store as agent_store
    try:
        result = agent_store.write_agent(
            name, body, actor=f"{_ctx_owner()}:builder", base_rev=base_rev,
            reason="builder full update", owner=_ctx_owner(),
            is_admin=_ctx_is_admin(), agents_dir=_AGENTS_DIR)
    except agent_store.ConflictError as exc:
        raise HTTPException(status_code=409, detail={
            "error": "conflict",
            "message": "This agent changed since you loaded it — reload and reapply.",
            "current_rev": exc.current_rev})
    except agent_store.OwnerMismatch as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except agent_store.DirectionLocked as exc:
        # Not a schema problem: the form tried to flip a twin's call type.
        # The message names the version to edit instead.
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Schema validation failed: {exc}")
    logger.info("[builder] updated agent '%s' rev=%s", name, result["revision"])
    return {"status": "updated", "name": name, "revision": result["revision"],
            "rev": result["rev"]}


@router.delete("/api/agents/{name}")
async def archive_agent(name: str) -> dict:
    """Archive an agent (move to agents/_archived/). Not a hard delete."""
    agent_dir = _AGENTS_DIR / name
    if not agent_dir.exists():
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    archived_base = _AGENTS_DIR / "_archived"
    archived_base.mkdir(exist_ok=True)
    dest = archived_base / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(agent_dir), str(dest))
    agent_registry.invalidate(name)
    # Stamp the index row so the plan cap stops counting it. The row is kept
    # (not deleted) so history, call attribution and un-archive all still work
    # — but every cap check and usage display filters archived_at IS NULL.
    try:
        from shared.db.database import get_db
        with get_db() as db:
            db.execute("UPDATE agents SET archived_at=CURRENT_TIMESTAMP "
                       "WHERE slug=? AND archived_at IS NULL", (name,))
    except Exception as exc:
        logger.error("[builder] could not stamp archived_at for %s: %r", name, exc)
    # If the archived agent was active, clear the selection.
    if _read_active() == name:
        try:
            _write_active(None)
        except Exception:
            pass
    logger.info("[builder] archived agent '%s'", name)
    return {"status": "archived", "name": name, "archived_to": str(dest)}


@router.post("/api/agents/{name}/validate")
async def validate_agent(name: str, request: Request) -> dict:
    """Dry-run schema validation. Accepts raw YAML (text/*) or JSON body."""
    content_type = request.headers.get("content-type", "")
    try:
        if "text/" in content_type or "yaml" in content_type:
            raw = (await request.body()).decode("utf-8")
            body = yaml.safe_load(raw)
        else:
            body = await request.json()
            body.pop("name", None)
        agent = AgentDefinition.from_dict(body)
        return {
            "valid": True,
            "agent_id": agent.agent_id,
            "version": agent.version,
            "stages": [s.id for s in agent.call_flow.stages],
            "languages": agent.languages.supported,
        }
    except Exception as exc:
        return {"valid": False, "error": str(exc)}


# ── raw YAML ────────────────────────────────────────────────────────────────
@router.get("/api/agents/{name}/raw")
async def get_agent_raw(name: str) -> PlainTextResponse:
    yaml_path = _AGENTS_DIR / name / "agent.yaml"
    if not yaml_path.exists():
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return PlainTextResponse(yaml_path.read_text(encoding="utf-8"))


@router.put("/api/agents/{name}/raw")
async def update_agent_raw(name: str, request: Request) -> dict:
    yaml_path = _AGENTS_DIR / name / "agent.yaml"
    if not yaml_path.exists():
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found — POST to create")
    text = (await request.body()).decode("utf-8")
    try:
        data = yaml.safe_load(text)
        AgentDefinition.from_dict(data)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"YAML parse/validation failed: {exc}")
    base_rev = request.headers.get("If-Match") or None
    from backend.app.agent_definition import store as agent_store
    try:
        result = agent_store.write_agent(
            name, data, actor=f"{_ctx_owner()}:builder", base_rev=base_rev,
            reason="builder raw YAML update", owner=_ctx_owner(),
            is_admin=_ctx_is_admin(), agents_dir=_AGENTS_DIR)
    except agent_store.ConflictError as exc:
        raise HTTPException(status_code=409, detail={
            "error": "conflict",
            "message": "This agent changed since you loaded it — reload and reapply.",
            "current_rev": exc.current_rev})
    except agent_store.OwnerMismatch as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    logger.info("[builder] raw-updated agent '%s' rev=%s", name, result["revision"])
    return {"status": "updated", "name": name, "revision": result["revision"],
            "rev": result["rev"]}


@router.get("/api/agents/{name}/revisions")
async def agent_revisions(name: str) -> dict:
    """Edit history (agent_revisions snapshots), newest first."""
    if not (_AGENTS_DIR / name / "agent.yaml").exists():
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    from backend.app.agent_definition import store as agent_store
    revs = agent_store.list_revisions(name)
    return {"revisions": [
        {"rev_id": r["id"], "actor": r["actor"], "reason": r["reason"],
         "sha256": r["sha256"], "created_at": str(r["created_at"])}
        for r in revs]}


@router.post("/api/agents/{name}/rollback/{rev_id}")
async def agent_rollback(name: str, rev_id: int) -> dict:
    """Restore a snapshot (itself snapshotted — rollback is undoable)."""
    from backend.app.agent_definition import store as agent_store
    try:
        result = agent_store.rollback_agent(
            name, rev_id, actor=f"{_ctx_owner()}:rollback",
            owner=_ctx_owner(), is_admin=_ctx_is_admin(), agents_dir=_AGENTS_DIR)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except agent_store.OwnerMismatch as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "rolled_back", "name": name, "rev_id": rev_id,
            "revision": result["revision"], "effective": "next_call"}


@router.patch("/api/agents/{name}/ops")
async def agent_ops(name: str, request: Request) -> dict:
    """Surgical path operations (the same machinery the Copilot uses):
    body {ops: [{op: set|append|remove_at|move, path, value?, to?}], base_rev?}."""
    yaml_path = _AGENTS_DIR / name / "agent.yaml"
    if not yaml_path.exists():
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    body = await request.json()
    ops = body.get("ops") or []
    if not isinstance(ops, list) or not ops:
        raise HTTPException(status_code=400, detail="'ops' (non-empty list) is required")
    from backend.app.agent_definition import store as agent_store
    from backend.app.agent_definition.diff import apply_ops, compute_diff, render_human
    current, cur_rev = agent_store.read_agent(name, _AGENTS_DIR)
    base_rev = body.get("base_rev") or request.headers.get("If-Match") or cur_rev
    try:
        candidate = apply_ops(current, ops)
        AgentDefinition.from_dict(candidate)
        expected = {str(o.get("path") or "").split("[")[0].rstrip(".")
                    for o in ops if o.get("path")}
        result = agent_store.write_agent(
            name, candidate, actor=f"{_ctx_owner()}:builder", base_rev=base_rev,
            reason="builder ops: " + ", ".join(str(o.get("op")) for o in ops)[:120],
            owner=_ctx_owner(), is_admin=_ctx_is_admin(),
            expected_paths=expected, agents_dir=_AGENTS_DIR)
    except agent_store.ConflictError as exc:
        raise HTTPException(status_code=409, detail={
            "error": "conflict",
            "message": "This agent changed since you loaded it — reload and reapply.",
            "current_rev": exc.current_rev})
    except agent_store.ScopeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    diff = compute_diff(current, candidate)
    return {"status": "updated", "name": name, "revision": result["revision"],
            "rev": result["rev"], "diff": render_human(diff),
            "effective": "next_call"}


# ── routing.yaml ────────────────────────────────────────────────────────────
@router.get("/api/agents/{name}/routing")
async def get_routing(name: str) -> dict:
    routing_path = _AGENTS_DIR / name / "routing.yaml"
    if not routing_path.exists():
        return {"exists": False, "did": "", "caller_id_prefixes": [], "time_windows": [], "fallback_agent": ""}
    data = yaml.safe_load(routing_path.read_text(encoding="utf-8")) or {}
    return {"exists": True, **data}


@router.put("/api/agents/{name}/routing")
async def update_routing(name: str, request: Request) -> dict:
    if not (_AGENTS_DIR / name).exists():
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    routing_path = _AGENTS_DIR / name / "routing.yaml"
    body = await request.json()
    with open(routing_path, "w", encoding="utf-8") as f:
        yaml.dump(body, f, allow_unicode=True, default_flow_style=False)
    logger.info("[builder] updated routing for agent '%s'", name)
    return {"status": "updated"}


# ── document uploads: one gate for every surface ───────────────────────────
#
# nginx caps the whole request at 25m (deploy/nginx-agent.lescall.conf); 20 MB
# here leaves room for the multipart envelope. Before this cap the endpoint
# read the entire body into memory unconditionally (the Copilot's upload had
# 15 MB; the Builder's had none).
_DOC_MAX_BYTES = int(os.environ.get("DOC_UPLOAD_MAX_MB", "20") or 20) * 1024 * 1024


async def _read_document_upload(file: UploadFile) -> tuple[bytes, str]:
    """Body + filename of an uploaded document, or the refusal that names what
    is wrong: a type we cannot read (415, with the way out — "save as .docx"),
    empty (400), too big (413 with the limit)."""
    from backend.app.documents.extractor import unsupported_reason
    filename = file.filename or "document.txt"
    reason = unsupported_reason(filename)
    if reason:
        raise HTTPException(status_code=415, detail=reason)
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(content) > _DOC_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(f"{filename} is {len(content) / 1048576:.1f} MB — the limit is "
                    f"{_DOC_MAX_BYTES // 1048576} MB. Split it, or export a smaller PDF."))
    return content, filename


async def _extract_document_text(content: bytes, filename: str) -> str:
    """Text of the document, off the event loop.

    Parsing a PDF, and above all the Gemini OCR fallback for a scanned one
    (a network call of up to two minutes), used to run INLINE in the async
    handler — the same defect that took every endpoint of this API down on
    2026-07-20 when the Copilot's ingest did it (RUNBOOK, copilot upload 502).
    """
    from backend.app.documents.extractor import extract_text
    try:
        extracted = await asyncio.to_thread(extract_text, content, filename)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Text extraction failed: {exc}")
    if not extracted.strip():
        raise HTTPException(
            status_code=422,
            detail=("No text could be extracted from this file. If it is a scanned "
                    "document, make sure the pages are legible; if it is an image, "
                    "make sure it contains text."))
    return extracted


async def _extract_fields(extracted: str) -> dict:
    """LLM field extraction off the event loop; an error is a 502 that carries
    the extractor's own sentence (quota, key, unreachable) instead of a
    generic 'Extraction failed'."""
    from backend.app.documents.yaml_filler import extract_fields_from_text
    result = await asyncio.to_thread(extract_fields_from_text, extracted)
    if result.get("status") == "error":
        raise HTTPException(status_code=502, detail=result.get("error", "Extraction failed"))
    return result


# ── stateless document extraction (Create-agent flow) ──────────────────────
@router.post("/api/agent-tools/extract-document")
async def extract_from_document(file: UploadFile = File(...)) -> dict:
    """Extract agent fields from an uploaded document WITHOUT needing an
    existing agent — used by the Create wizard to prefill the form. Returns
    {"fields": {...}, "missing": [{key,label,hint}...]} so the UI can ask the
    user for everything the document lacked."""
    content, filename = await _read_document_upload(file)
    extracted = await _extract_document_text(content, filename)
    # E11 — LLM extraction on a tenant-uploaded file. Unmetered today, and the
    # model quota it draws on is SHARED with live calls, so an upload loop is a
    # way to degrade other tenants' phone calls at no cost to yourself.
    _billing_llm_gate(_ctx_owner(), "extract-document")
    result = await _extract_fields(extracted)
    result["filename"] = filename
    result["char_count"] = len(extracted)
    return result


# ── calendar bookings ───────────────────────────────────────────────────────
@router.get("/api/agents/{name}/bookings")
async def list_agent_bookings(name: str) -> dict:
    """Booked appointment slots recorded by live calls (bookings.json)."""
    if not (_AGENTS_DIR / name).exists():
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    from backend.app.agent_definition.calendar_store import list_bookings
    return {"bookings": list_bookings(name)}


@router.delete("/api/agents/{name}/bookings/{index}")
async def delete_agent_booking(name: str, index: int) -> dict:
    """Free a booked slot again (e.g. cancelled appointment)."""
    from backend.app.agent_definition.calendar_store import delete_booking
    if not delete_booking(name, index):
        raise HTTPException(status_code=404, detail="Booking not found")
    return {"status": "deleted", "index": index}


# ── documents + RAG ─────────────────────────────────────────────────────────
@router.post("/api/agents/{name}/documents")
async def upload_document(
    name: str,
    file: UploadFile = File(...),
    org_name: str = Form(""),
    doc_type: str = Form("General"),
    description: str = Form(""),
) -> dict:
    """Upload a document: extract text, chunk, embed (for in-call RAG).

    The response says what actually happened. `embedded` is whether the
    vector index was built; when it is False, `index_state.detail` carries the
    provider's reason and the document is keyword-searchable until a
    re-index succeeds. The old reply said "uploaded" either way, and the UI
    toasted "Uploaded & embedded" over fourteen days of failed embeds.

    A byte-identical file already on this agent is answered with THAT
    document (`status: duplicate`) rather than a second copy — two copies of
    one brochure compete with each other for the three evidence slots.
    """
    if not (_AGENTS_DIR / name).exists():
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    content, filename = await _read_document_upload(file)
    from backend.app.documents.embeddings import embed_document, index_status
    from backend.app.documents.store import find_document_by_hash, save_document

    def _state(status: dict, doc_id: str) -> dict | None:
        return next((r for r in status.get("documents", []) if r.get("doc_id") == doc_id), None)

    existing = find_document_by_hash(name, hashlib.sha256(content).hexdigest())
    if existing:
        status = index_status(name)
        row = _state(status, existing["doc_id"])
        return {"status": "duplicate", "doc_id": existing["doc_id"], "meta": existing,
                "embedded": bool(row and row.get("embedded")),
                "index_state": row, "index": status}

    extracted = await _extract_document_text(content, filename)
    meta = await asyncio.to_thread(
        save_document, agent_folder=name, filename=filename, content=content,
        extracted_text=extracted, org_name=org_name, doc_type=doc_type,
        description=description)
    embedded = False
    try:
        embedded = bool(await asyncio.to_thread(embed_document, name, meta["doc_id"]))
    except Exception as exc:
        logger.warning("[builder/docs] embedding failed for %s/%s: %s", name, meta["doc_id"], exc)
    status = index_status(name)
    return {"status": "uploaded", "doc_id": meta["doc_id"], "meta": meta,
            "embedded": embedded, "index_state": _state(status, meta["doc_id"]),
            "index": status}


@router.get("/api/agents/{name}/documents")
async def list_agent_documents(name: str) -> dict:
    """List documents, plus the health of the vector index built from them.

    `documents` keeps its exact previous shape and meaning — the Agent Builder
    UI reads it and must not break. `index` is additive: it is how an operator
    finds out that a document is uploaded but NOT searchable, which used to be
    invisible (a failed embed left no trace, and the agent simply stopped
    citing that document forever).
    """
    index_path = _AGENTS_DIR / name / "documents" / "index.json"
    docs: list = []
    if index_path.exists():
        try:
            docs = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[builder/docs] could not read index for '%s': %s", name, exc)
    payload: dict = {"documents": docs}
    try:
        from backend.app.documents.embeddings import index_status
        payload["index"] = index_status(name)
    except Exception as exc:
        logger.warning("[builder/docs] index status failed for '%s': %s", name, exc)
    return payload


@router.post("/api/agents/{name}/documents/reindex")
async def reindex_agent_documents(name: str) -> dict:
    """Re-chunk and re-embed every document of this agent.

    The deliberate migration step after a chunking or embedding-model change.
    Nothing re-indexes implicitly: it spends real embedding tokens and takes
    seconds per document, so it is an operator's decision, and
    `GET /documents` (`index.needs_reindex`) is what tells them it is due.
    """
    if not (_AGENTS_DIR / name).exists():
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    _billing_llm_gate(_ctx_owner(), "documents-reindex")
    from backend.app.documents.embeddings import index_status, reindex_agent
    result = await asyncio.to_thread(reindex_agent, name)
    result["index"] = index_status(name)
    logger.info("[builder/docs] reindexed %s: %d ok, %d failed", name,
                len(result["reindexed"]), len(result["failed"]))
    return result


@router.get("/api/agents/{name}/rag/metrics")
async def rag_metrics(name: str) -> dict:
    """Live RAG health: latency percentiles, abstention, cost, cache, errors.

    Read off the in-process ring of recent retrievals, so these are the
    percentiles of what actually happened rather than of a benchmark. Note the
    window is per API process; the voice runtime keeps its own.
    """
    from backend.app.documents import config as rag_config
    from backend.app.documents import trace as rag_trace
    from backend.app.documents.embeddings import index_status
    return {
        "agent": name,
        "config": rag_config.snapshot(),
        "index": index_status(name),
        "live": rag_trace.metrics_snapshot(),
    }


@router.post("/api/agents/{name}/rag/probe")
async def rag_probe(name: str, request: Request) -> dict:
    """Run one retrieval and return exactly what the agent would be given.

    The debugging tool the old pipeline had no equivalent of: when a caller
    says "it didn't know its own brochure", this answers whether retrieval
    missed, the index was stale, or the evidence was there and the model
    ignored it — without placing a call.
    """
    body = await request.json()
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    from backend.app.documents import retrieval
    result = await retrieval.retrieve_async(name, query, request_id=f"probe:{name}")
    return {
        "query": query,
        "context": result.context_block,
        "abstained": result.abstained,
        "abstain_reason": result.abstain_reason,
        "degraded": result.degraded,
        "degraded_reason": result.degraded_reason,
        "index_problems": result.index_problems,
        "chunks": [{
            "label": c.label, "document_id": c.doc_id, "chunk_id": c.chunk_id,
            "scores": c.stage_scores,
            "preview": c.text[:300],
        } for c in result.chunks],
        **result.to_api(),
    }


@router.delete("/api/agents/{name}/documents/{doc_id}")
async def delete_agent_document(name: str, doc_id: str) -> dict:
    from backend.app.documents.store import delete_document
    if not delete_document(name, doc_id):
        raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found")
    return {"status": "deleted", "doc_id": doc_id}


@router.post("/api/agents/{name}/documents/{doc_id}/extract-yaml")
async def extract_yaml_from_document(name: str, doc_id: str) -> dict:
    """LLM-extract agent fields from a document; returns a diff preview (no write)."""
    from backend.app.documents.store import get_document_text
    text = get_document_text(name, doc_id)
    if not text:
        raise HTTPException(status_code=404, detail="Document text not found")
    _billing_llm_gate(_ctx_owner(), "extract-yaml")   # E11
    # Off the loop, and an error is a 502 — this used to return the error dict
    # with a 200, and the review dialog opened on a payload with no fields.
    result = await _extract_fields(text)
    result["agent_folder"] = name
    result["doc_id"] = doc_id
    return result


@router.post("/api/agents/{name}/documents/{doc_id}/apply-yaml")
async def apply_yaml_from_document(name: str, doc_id: str, request: Request) -> dict:
    """Apply extracted fields (from the diff preview) into the agent's YAML."""
    body = await request.json()
    fields = body.get("fields", {})
    if not fields:
        raise HTTPException(status_code=400, detail="No fields provided to apply")
    from backend.app.documents.yaml_filler import apply_fields
    result = apply_fields(name, fields)
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error", "Apply failed"))
    agent_registry.invalidate(name)
    return result


# ── TTS voice preview ───────────────────────────────────────────────────────
@router.get("/api/tts/preview")
async def tts_voice_preview(speaker: str, model: str = "bulbul:v3", language: str = "en-IN",
                            pace: float = 1.0):
    """Stream a short TTS sample as audio/wav for in-browser playback."""
    import httpx
    api_key = os.environ.get("SARVAM_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="SARVAM_API_KEY not configured")
    # The sample speaks in the voice's own gender — a male voice reading
    # "मैं आपकी AI सहायक हूँ … सकती हूँ" previewed a mismatch no call has.
    from backend.app.agent_definition.voice_catalog import speaker_gender
    _male = speaker_gender(speaker) == "male"
    preview_text = {
        "en-IN": "Hi there! I'm your AI assistant. How can I help you today?",
        "hi-IN": ("नमस्ते! मैं आपका AI सहायक हूँ। आज मैं आपकी कैसे मदद कर सकता हूँ?"
                  if _male else
                  "नमस्ते! मैं आपकी AI सहायक हूँ। आज मैं आपकी कैसे मदद कर सकती हूँ?"),
        "gu-IN": ("નમસ્તે! હું તમારો AI સહાયક છું. આજે હું તમને કેવી રીતે મદદ કરી શકું?"
                  if _male else
                  "નમસ્તે! હું તમારી AI સહાયક છું. આજે હું તમને કેવી રીતે મદદ કરી શકું?"),
    }
    text = preview_text.get(language, preview_text["en-IN"])
    payload = {
        "text": text,
        "target_language_code": language,
        "speaker": speaker,
        "model": model,
        "pace": min(max(pace, 0.5), 2.0),
        "speech_sample_rate": 16000,
        "output_audio_codec": "linear16",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            async with client.stream(
                "POST",
                "https://api.sarvam.ai/text-to-speech/stream",
                headers={"api-subscription-key": api_key, "Content-Type": "application/json"},
                json=payload,
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise HTTPException(status_code=resp.status_code, detail=body.decode(errors="replace"))
                pcm = b"".join([chunk async for chunk in resp.aiter_bytes(4096)])
        sr, ch, bps = 16000, 1, 16
        wav = struct.pack("<4sI4s", b"RIFF", 36 + len(pcm), b"WAVE")
        wav += struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, ch, sr, sr * ch * bps // 8, ch * bps // 8, bps)
        wav += struct.pack("<4sI", b"data", len(pcm)) + pcm
        return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── activate (THE wire to telephony) ────────────────────────────────────────
@router.post("/api/agents/{name}/activate")
async def activate_agent(name: str) -> dict:
    """Make this agent answer inbound calls.

    Writes agents/active_agent.json — the single-active-agent selector that
    conversation_call_bridge.py reads when each call starts. The next call will
    run this agent; calls already in progress are unaffected.

    ADMIN ONLY: this is the platform-wide FALLBACK for calls that don't match
    a DID binding — letting a tenant flip it would re-route other tenants'
    calls. Tenants route their own numbers via the Telephony panel instead.
    """
    if not _ctx_is_admin():
        raise HTTPException(
            status_code=403,
            detail="Activation is platform-admin only — bind your number to "
                   "this agent in the Telephony panel instead")
    yaml_path = _AGENTS_DIR / name / "agent.yaml"
    if not yaml_path.exists():
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    try:
        agent = AgentDefinition.from_yaml(yaml_path)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Failed to load agent: {exc}")
    _write_active(name)
    agent_registry.invalidate(name)
    logger.info("[builder] activated agent '%s' (%s) for inbound calls", name, agent.agent_id)
    return {
        "status": "activated",
        "name": name,
        "agent_id": agent.agent_id,
        "agent_name": agent.identity.agent_name,
        "note": "Next inbound call will use this agent.",
    }
