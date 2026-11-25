"""BYON self-service API (docs/MULTITENANT_BYON_PLAN.md §3b).

Owner-scoped trunk + DID registration backed by the shared DB, FreeSWITCH
config generation (shared/freeswitch_config.py) and the Kamailio carrier ACL
(shared/kamailio_acl.py). Mounted into dashboard/server.py exactly like
agent_builder_api (APIRouter, try/except include).

Every endpoint takes `owner: str = Depends(current_owner)` — that dependency
IS the tenant boundary (single-tenant fallback until Google OAuth env is set).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("dashboard.telephony")

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from auth import current_owner  # noqa: E402 - same-dir module, like agent_builder_api
from shared.db.database import get_db  # noqa: E402
from shared.phone import significant_digits, to_e164  # noqa: E402
from shared import freeswitch_config as fsc  # noqa: E402
from shared import kamailio_acl  # noqa: E402
from shared import call_forwarding as cf  # noqa: E402

AGENTS_DIR = _REPO_ROOT / "calling-agent" / "agents"
PUBLIC_SIP_IP = os.environ.get("PUBLIC_SIP_IP", "35.207.225.237")
PUBLIC_SIP_PORT = os.environ.get("PUBLIC_SIP_PORT", "5060")
RTP_PORT_RANGE = os.environ.get("PUBLIC_RTP_RANGE", "16384-32768")

router = APIRouter()


def paid_owner(owner: str = Depends(current_owner)) -> str:
    """Tenant boundary PLUS the paid-plan gate for BYON setup.

    Signups are open to any Google account, and this is the one surface that
    puts a SIP trunk and a DID on our carrier edge — so registering telephony
    is a paid capability. Free accounts can sign up, build an agent and explore
    the dashboard; connecting a real number needs a plan.

    Applied to MUTATIONS only. The GET endpoints stay open on purpose: the SPA
    renders the panel in a locked state with an upgrade prompt, and a 402 on
    the list call would give it nothing to render but an error — which reads as
    a broken page rather than a plan boundary.

    Note this gate does NOT consult `enforcement_enabled()`. That switch stages
    quota rollout for existing tenants; this protects the trunk from strangers
    who signed up today, and must be live the moment signups reopen.
    """
    from shared.billing.entitlements import telephony_allowed
    from shared.billing.errors import BillingRefusal, CODE_PLAN_REQUIRED
    if not telephony_allowed(owner):
        raise BillingRefusal(
            "Connecting your own phone number is available on paid plans. "
            "Start with Pilot to run real calls on your own number.",
            CODE_PLAN_REQUIRED, feature="telephony")
    return owner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _owner_slug(owner: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", owner.lower()).strip("-")


def _trunk_or_404(db, trunk_id: int, owner: str) -> dict:
    row = db.execute("SELECT * FROM trunks WHERE id=? AND owner=?",
                     (trunk_id, owner)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="trunk not found")
    return dict(row)


def _all_provider_cidrs(db) -> list[str]:
    """Union of provider_ips across ALL trunks — the Kamailio ACL is one
    global list (grp 1); adding a trunk widens it, never replaces others'."""
    cidrs: list[str] = []
    for row in db.execute("SELECT provider_ips FROM trunks "
                          "WHERE provider_ips IS NOT NULL").fetchall():
        cidrs.extend(p.strip() for p in str(row["provider_ips"]).split(",") if p.strip())
    return cidrs


def _validate_agent_binding(db, slug: str, owner: str) -> None:
    """A DID may only bind to an agent the caller owns.

    Agents created after seeding have no index row yet — binding one CLAIMS it
    for the caller (writes the agents row), so a second tenant can never bind
    the same unclaimed agent afterwards.
    """
    if not slug:
        return
    if not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
        raise HTTPException(status_code=400, detail="invalid agent slug")
    if not (AGENTS_DIR / slug / "agent.yaml").exists():
        raise HTTPException(status_code=400, detail=f"agent '{slug}' has no agent.yaml")
    row = db.execute("SELECT owner FROM agents WHERE slug=?", (slug,)).fetchone()
    if row is None:
        db.execute("INSERT INTO agents (slug, owner) VALUES (?,?)", (slug, owner))
    elif not row["owner"]:
        db.execute("UPDATE agents SET owner=? WHERE slug=?", (owner, slug))
    elif row["owner"] != owner:
        raise HTTPException(status_code=403, detail="agent belongs to another tenant")


def _warn_direction_mismatch(slug: str, want: str) -> None:
    """Log when an agent authored for one direction is bound to the other slot.

    Deliberately NOT an error: the outbound column is backfilled from the
    inbound one, so every pre-existing number would otherwise 400 on its next
    unrelated edit. This is the breadcrumb for 'the outbound agent behaves like
    a receptionist' reports.
    """
    if not slug:
        return
    try:
        import yaml
        with open(AGENTS_DIR / slug / "agent.yaml", encoding="utf-8") as fh:
            have = str((yaml.safe_load(fh) or {}).get("call_direction") or "inbound")
    except Exception:
        return
    if have != want:
        logger.warning("agent '%s' is authored call_direction=%s but is bound to "
                       "the %s slot — it will use %s-shaped prompts on %s calls",
                       slug, have, want, have, want)


# Carrier signaling blocks are /16 or narrower in practice; anything broader
# (or 0.0.0.0/…, loopback) would let one tenant swing the platform-global SIP
# allow-list wide open.
_MIN_CIDR_PREFIX = 16
_MAX_CIDR_ENTRIES = 64


def _validate_provider_ips(provider_ips: str) -> None:
    import ipaddress
    entries = [p.strip() for p in (provider_ips or "").split(",") if p.strip()]
    if len(entries) > _MAX_CIDR_ENTRIES:
        raise HTTPException(status_code=400,
                            detail=f"too many provider_ips (max {_MAX_CIDR_ENTRIES})")
    for item in entries:
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid CIDR: {item}")
        if net.version != 4:
            raise HTTPException(status_code=400, detail=f"IPv4 only: {item}")
        if net.prefixlen < _MIN_CIDR_PREFIX:
            raise HTTPException(
                status_code=400,
                detail=f"{item}: prefix must be /{_MIN_CIDR_PREFIX} or narrower")
        if net.is_loopback or str(net.network_address) == "0.0.0.0":
            raise HTTPException(status_code=400, detail=f"{item}: not allowed")


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    return data


# ---------------------------------------------------------------------------
# Trunks
# ---------------------------------------------------------------------------

@router.get("/api/trunks")
def list_trunks(owner: str = Depends(current_owner)):
    with get_db() as db:
        rows = db.execute("SELECT * FROM trunks WHERE owner=? ORDER BY id DESC",
                          (owner,)).fetchall()
    trunks = []
    for r in rows:
        t = dict(r)
        t["password"] = "***" if t.get("password") else ""
        trunks.append(t)
    return {"trunks": trunks}


@router.post("/api/trunks")
async def create_trunk(request: Request, owner: str = Depends(paid_owner)):
    body = await _body(request)
    name = str(body.get("name") or "").strip()
    sip_domain = str(body.get("sip_domain") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name or ""):
        raise HTTPException(status_code=400,
                            detail="name required ([A-Za-z0-9_-], max 64)")
    if not sip_domain:
        raise HTTPException(status_code=400, detail="sip_domain required")
    _validate_provider_ips(str(body.get("provider_ips") or ""))

    # FreeSWITCH gateway names are global — prefix with the owner slug so two
    # tenants' trunks can never collide.
    full_name = f"{_owner_slug(owner)}_{name}"
    fields = {
        "name": full_name,
        "provider": str(body.get("provider") or "plivo"),
        "owner": owner,
        "sip_domain": sip_domain,
        "proxy": str(body.get("proxy") or ""),
        "username": str(body.get("username") or ""),
        "password": str(body.get("password") or ""),
        "register": 1 if body.get("register") else 0,
        "caller_id": str(body.get("caller_id") or ""),
        "provider_ips": str(body.get("provider_ips") or ""),
        "transport": str(body.get("transport") or "udp"),
    }
    with get_db() as db:
        dup = db.execute("SELECT id FROM trunks WHERE name=?", (full_name,)).fetchone()
        if dup:
            raise HTTPException(status_code=409, detail="trunk name already exists")
        cols = ",".join(fields)
        marks = ",".join("?" * len(fields))
        row = db.execute(
            f"INSERT INTO trunks ({cols}) VALUES ({marks}) RETURNING id",
            tuple(fields.values())).fetchone()
        trunk_id = row["id"]
        acl = kamailio_acl.sync_carrier_acl(_all_provider_cidrs(db))
    logger.info("trunk created: %s (owner=%s) kamailio_acl=%s", full_name, owner, acl)
    return {"id": trunk_id, "name": full_name, "kamailio_acl": acl}


@router.patch("/api/trunks/{trunk_id}")
async def update_trunk(trunk_id: int, request: Request,
                       owner: str = Depends(paid_owner)):
    """Edit a trunk in place: SIP domain, credentials, caller ID, provider IPs.

    Previously the only way to correct any of these was delete-and-recreate,
    which also drops the trunk's DIDs (FK) and their agent bindings. That is a
    harsh penalty for a typo, and for the common real case: a carrier trunk
    registered against the wrong Zentrunk domain, or one that turns out to need
    SIP credentials rather than IP auth.

    `name` is deliberately NOT editable — it is the FreeSWITCH gateway name and
    its config filename, so renaming would orphan the old XML and change the
    gateway every live DID points at. Delete and recreate for that.

    Sets config_status back to 'pending': the DB now disagrees with what is on
    disk in /etc/freeswitch until Apply runs again.
    """
    body = await _body(request)
    editable = {
        "sip_domain": str, "proxy": str, "username": str, "password": str,
        "caller_id": str, "provider_ips": str, "transport": str, "provider": str,
    }
    fields: dict = {}
    for key, cast in editable.items():
        if key in body:
            fields[key] = cast(body.get(key) or "")
    if "register" in body:
        fields["register"] = 1 if body.get("register") else 0
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")

    if "sip_domain" in fields and not fields["sip_domain"].strip():
        raise HTTPException(status_code=400, detail="sip_domain cannot be empty")
    if "provider_ips" in fields:
        _validate_provider_ips(fields["provider_ips"])
    # An empty password in the body means "leave it alone", not "clear it" —
    # the GET masks it as "***", so a round-tripped form would otherwise wipe
    # the credential every time someone edited an unrelated field.
    if fields.get("password") in ("", "***"):
        fields.pop("password", None)

    with get_db() as db:
        _trunk_or_404(db, trunk_id, owner)
        sets = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE trunks SET {sets}, config_status='pending' WHERE id=? AND owner=?",
                   (*fields.values(), trunk_id, owner))
        db.execute("UPDATE phone_numbers SET config_status='pending' "
                   "WHERE trunk_id=? AND owner=?", (trunk_id, owner))
        acl = kamailio_acl.sync_carrier_acl(_all_provider_cidrs(db))
        row = dict(_trunk_or_404(db, trunk_id, owner))
    row["password"] = "***" if row.get("password") else ""
    logger.info("trunk %s updated by %s: %s", trunk_id, owner, sorted(fields))
    return {"status": "updated", "trunk": row, "kamailio_acl": acl,
            "hint": "press Apply to write the new config to FreeSWITCH"}


@router.delete("/api/trunks/{trunk_id}")
def delete_trunk(trunk_id: int, owner: str = Depends(paid_owner)):
    with get_db() as db:
        _trunk_or_404(db, trunk_id, owner)
        n = db.execute("SELECT COUNT(*) AS n FROM phone_numbers WHERE trunk_id=?",
                       (trunk_id,)).fetchone()["n"]
        if n:
            raise HTTPException(status_code=409,
                                detail=f"{n} number(s) still bound to this trunk")
        db.execute("DELETE FROM trunks WHERE id=?", (trunk_id,))
        acl = kamailio_acl.sync_carrier_acl(_all_provider_cidrs(db))
    return {"deleted": trunk_id, "kamailio_acl": acl,
            "note": "generated FS gateway file (if applied) is not auto-removed; "
                    "see deploy/RUNBOOK.md"}


@router.post("/api/trunks/{trunk_id}/apply")
def apply_trunk(trunk_id: int, owner: str = Depends(paid_owner)):
    """Generate + write FreeSWITCH config for this trunk and its DIDs, reload
    FS, and re-sync the Kamailio carrier ACL."""
    with get_db() as db:
        trunk = _trunk_or_404(db, trunk_id, owner)
        numbers = [dict(r) for r in db.execute(
            "SELECT * FROM phone_numbers WHERE trunk_id=? AND owner=?",
            (trunk_id, owner)).fetchall()]
        cidrs = _all_provider_cidrs(db)

    try:
        result = fsc.apply_config(trunk, numbers, reload=True, all_provider_cidrs=cidrs)
    except PermissionError as exc:
        return JSONResponse(status_code=500, content={
            "status": "error",
            "error": f"cannot write {fsc.FS_CONF_DIR}: {exc}",
            "hint": "grant the dashboard user group-write on sip_profiles/external "
                    "and dialplan/public (see deploy/RUNBOOK.md §FS permissions)"})
    result["kamailio_acl"] = kamailio_acl.sync_carrier_acl(cidrs)

    status = "applied" if result.get("reloaded") else "error"
    with get_db() as db:
        db.execute("UPDATE trunks SET config_status=?, last_applied_at=CURRENT_TIMESTAMP "
                   "WHERE id=?", (status, trunk_id))
        if numbers:
            db.execute(
                "UPDATE phone_numbers SET config_status=?, last_applied_at=CURRENT_TIMESTAMP "
                "WHERE trunk_id=?", (status, trunk_id))
    if numbers:
        # Re-sync forwarding files: a newly applied or renamed gateway must be
        # reflected in them too.
        result["forwarding"] = cf.sync_numbers([n["id"] for n in numbers])
    return {"status": status, **result}


@router.get("/api/trunks/{trunk_id}/instructions")
def trunk_instructions(trunk_id: int, owner: str = Depends(current_owner)):
    with get_db() as db:
        trunk = _trunk_or_404(db, trunk_id, owner)
        numbers = [r["number"] for r in db.execute(
            "SELECT number FROM phone_numbers WHERE trunk_id=?", (trunk_id,)).fetchall()]
    steps = [
        f"Point your carrier's origination/termination URI at "
        f"sip:{PUBLIC_SIP_IP}:{PUBLIC_SIP_PORT} (UDP).",
        "Add your carrier's SIP signaling IPs/CIDRs to this trunk's provider_ips "
        "and re-apply — inbound from unlisted IPs is rejected at the SIP edge.",
        f"Allow RTP (media) to/from {PUBLIC_SIP_IP} UDP {RTP_PORT_RANGE} in any "
        "carrier-side firewall.",
        "If the carrier authenticates by IP allow-list (Plivo zentrunk style), add "
        f"this server's egress IP {PUBLIC_SIP_IP} to the carrier trunk's IP ACL.",
    ]
    if trunk.get("provider") == "plivo":
        steps.append(
            "Plivo: use SEPARATE inbound and outbound zentrunks — origination URI "
            "goes on the INBOUND trunk; this trunk's sip_domain must be the "
            "OUTBOUND trunk's domain or outbound calls fail with 403/4180.")
    return {"trunk": trunk["name"], "numbers": numbers,
            "sip_uri": f"sip:{PUBLIC_SIP_IP}:{PUBLIC_SIP_PORT}",
            "rtp_range": RTP_PORT_RANGE, "steps": steps}


# ---------------------------------------------------------------------------
# Phone numbers (DIDs) — THE inbound routing table
# ---------------------------------------------------------------------------

@router.get("/api/numbers")
def list_numbers(owner: str = Depends(current_owner)):
    with get_db() as db:
        rows = db.execute("SELECT * FROM phone_numbers WHERE owner=? ORDER BY id DESC",
                          (owner,)).fetchall()
    return {"numbers": [dict(r) for r in rows]}


@router.post("/api/numbers")
async def create_number(request: Request, owner: str = Depends(paid_owner)):
    body = await _body(request)
    number = str(body.get("number") or "").strip()
    sig = significant_digits(number)
    if len(sig) < 7:
        raise HTTPException(status_code=400, detail="number must have at least 7 digits")
    trunk_id = body.get("trunk_id")
    slug = str(body.get("inbound_agent_slug") or "").strip()
    # Omitting the outbound slug mirrors the inbound one, so a number registered
    # by an older client (or the simple form) can place calls immediately.
    out_slug = str(body.get("outbound_agent_slug") or slug).strip()

    with get_db() as db:
        if trunk_id is not None:
            _trunk_or_404(db, int(trunk_id), owner)  # ownership check
        _validate_agent_binding(db, slug, owner)
        _validate_agent_binding(db, out_slug, owner)
        _warn_direction_mismatch(slug, "inbound")
        _warn_direction_mismatch(out_slug, "outbound")
        dup = db.execute(
            "SELECT id, owner FROM phone_numbers WHERE number=? OR sig_digits=?",
            (number, sig)).fetchone()
        if dup:
            # deliberately vague: don't confirm to one tenant that another has
            # this number (enumeration oracle)
            raise HTTPException(status_code=409, detail="number unavailable")
        row = db.execute(
            "INSERT INTO phone_numbers (number, sig_digits, trunk_id, "
            "inbound_agent_slug, outbound_agent_slug, owner, direction, label) "
            "VALUES (?,?,?,?,?,?,?,?) RETURNING id",
            (number, sig, trunk_id, slug or None, out_slug or None, owner,
             str(body.get("direction") or "both"),
             str(body.get("label") or ""))).fetchone()
    logger.info("number registered: %s -> in=%s out=%s (owner=%s)",
                number, slug, out_slug, owner)
    return {"id": row["id"], "number": number, "sig_digits": sig,
            "inbound_agent_slug": slug or None,
            "outbound_agent_slug": out_slug or None,
            "note": "run POST /api/trunks/{trunk_id}/apply to generate the "
                    "FreeSWITCH dialplan for this DID"}


@router.patch("/api/numbers/{number_id}")
async def update_number(number_id: int, request: Request,
                        owner: str = Depends(paid_owner)):
    """Rebind a DID (agent / direction / label). Routing changes take effect on
    the NEXT call — the bridge reads phone_numbers per call."""
    body = await _body(request)
    with get_db() as db:
        row = db.execute("SELECT * FROM phone_numbers WHERE id=? AND owner=?",
                         (number_id, owner)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="number not found")
        updates, params = [], []
        for field, want in (("inbound_agent_slug", "inbound"),
                            ("outbound_agent_slug", "outbound")):
            if field in body:
                slug = str(body.get(field) or "").strip()
                _validate_agent_binding(db, slug, owner)
                _warn_direction_mismatch(slug, want)
                updates.append(f"{field}=?")
                params.append(slug or None)
        if "direction" in body:
            updates.append("direction=?")
            params.append(str(body["direction"]))
        if "label" in body:
            updates.append("label=?")
            params.append(str(body["label"]))
        forward_touched = _collect_forward_updates(db, dict(row), body, owner,
                                                   updates, params)
        if not updates:
            raise HTTPException(status_code=400, detail="nothing to update")
        params.append(number_id)
        db.execute(f"UPDATE phone_numbers SET {', '.join(updates)} WHERE id=?",
                   tuple(params))
        out = dict(db.execute("SELECT * FROM phone_numbers WHERE id=?",
                              (number_id,)).fetchone())
    if forward_touched:
        # The setting is saved either way; `forwarding` tells the owner whether
        # it is actually live, and why not when it is not.
        try:
            out["forwarding"] = await asyncio.to_thread(cf.sync_number, number_id)
        except cf.ForwardingError as exc:
            out["forwarding"] = {"state": "error", "reason": str(exc)}
    return out


@router.delete("/api/numbers/{number_id}")
def delete_number(number_id: int, owner: str = Depends(paid_owner)):
    with get_db() as db:
        row = db.execute("SELECT * FROM phone_numbers WHERE id=? AND owner=?",
                         (number_id, owner)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="number not found")
        db.execute("DELETE FROM phone_numbers WHERE id=?", (number_id,))
    try:
        cf.remove_forward_file(dict(row))
    except OSError as exc:
        logger.warning("forward file removal failed for number %s: %r", number_id, exc)
    return {"deleted": number_id,
            "note": "generated dialplan file (if applied) is not auto-removed; "
                    "see deploy/RUNBOOK.md"}


# ---------------------------------------------------------------------------
# Conditional call forwarding (RUNBOOK §84) — see shared/call_forwarding.py
#
# A number is answered by its agent (forward_mode='agent', the default) or
# forwarded (forward_mode='forward'): FreeSWITCH first rings one of the owner's
# own VERIFIED Indian mobiles; the person presses 1 and talks to the caller;
# otherwise the agent answers (or the call ends). Saving a setting writes or
# deletes that number's file in dialplan/public_forward and reloads FreeSWITCH.
# ---------------------------------------------------------------------------

def _fwd_error(exc: "cf.ForwardingError") -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


def _contact_owned(db, contact_id: int, owner: str) -> dict:
    row = db.execute(
        "SELECT id, owner, label, number, sig_digits, verified_at, created_at "
        "FROM forward_contacts WHERE id=? AND owner=?",
        (int(contact_id), owner)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="forward phone not found")
    return dict(row)


def _collect_forward_updates(db, row: dict, body: dict, owner: str,
                             updates: list, params: list) -> bool:
    """Validate and stage the forward_* columns of a PATCH /api/numbers call.

    Returns True when any forwarding field is being changed (the caller then
    re-syncs FreeSWITCH). The one cross-field rule: a number in 'forward' mode
    must point at a VERIFIED phone of this account — otherwise an inbound call
    would ring nobody, or a stranger.
    """
    touched = False
    try:
        if "forward_contact_id" in body:
            cid = body["forward_contact_id"]
            if cid in (None, "", 0):
                params.append(None)
            else:
                contact = _contact_owned(db, int(cid), owner)
                if not contact.get("verified_at"):
                    raise cf.ForwardingError(
                        "Verify this phone first — we call it once with a code.")
                params.append(int(cid))
            updates.append("forward_contact_id=?")
            touched = True
        if "forward_ring_s" in body:
            updates.append("forward_ring_s=?")
            params.append(cf.clamp_ring(body["forward_ring_s"]))
            touched = True
        if "forward_confirm" in body:
            updates.append("forward_confirm=?")
            params.append(bool(body["forward_confirm"]))
            touched = True
        if "forward_fallback" in body:
            fb = str(body["forward_fallback"])
            if fb not in cf.FALLBACKS:
                raise cf.ForwardingError(f"forward_fallback must be one of {cf.FALLBACKS}")
            updates.append("forward_fallback=?")
            params.append(fb)
            touched = True
        if "forward_line_name" in body:
            updates.append("forward_line_name=?")
            params.append(cf.clean_line_name(body["forward_line_name"]))
            touched = True
        if "forward_mode" in body:
            mode = str(body["forward_mode"])
            if mode not in cf.MODES:
                raise cf.ForwardingError(f"forward_mode must be one of {cf.MODES}")
            updates.append("forward_mode=?")
            params.append(mode)
            touched = True
        else:
            mode = row.get("forward_mode") or "agent"
    except cf.ForwardingError as exc:
        raise _fwd_error(exc)

    if mode == "forward":
        if "forward_contact_id" in body:
            cid = body["forward_contact_id"]
            contact_after = None if cid in (None, "", 0) else int(cid)
        else:
            contact_after = row.get("forward_contact_id")
        if not contact_after:
            raise HTTPException(
                status_code=400,
                detail="Pick the phone to ring first (add and verify one if needed).")
        if "forward_contact_id" not in body:
            # Unchanged contact: still has to be this account's and verified.
            contact = _contact_owned(db, int(contact_after), owner)
            if not contact.get("verified_at"):
                raise HTTPException(status_code=400,
                                    detail="Verify this phone first — we call it once with a code.")
    return touched


@router.get("/api/forward-contacts")
def list_forward_contacts(owner: str = Depends(current_owner)):
    with get_db() as db:
        rows = db.execute(
            "SELECT id, owner, label, number, sig_digits, verified_at, created_at "
            "FROM forward_contacts WHERE owner=? ORDER BY id",
            (owner,)).fetchall()
    contacts = [{**dict(r), "verified": bool(r["verified_at"])} for r in rows]
    missing = cf.infrastructure_missing()
    return {"contacts": contacts, "max": cf.MAX_CONTACTS,
            "server_ready": not missing, "server_missing": missing}


@router.post("/api/forward-contacts")
async def create_forward_contact(request: Request, owner: str = Depends(paid_owner)):
    body = await _body(request)
    try:
        number = cf.indian_number(body.get("number"))
    except cf.ForwardingError as exc:
        raise _fwd_error(exc)
    sig = significant_digits(number)
    label = re.sub(r"\s+", " ", str(body.get("label") or "")).strip()[:60]
    with get_db() as db:
        try:
            cf.refuse_own_lines(db, number)
        except cf.ForwardingError as exc:
            raise _fwd_error(exc)
        n = db.execute("SELECT COUNT(*) AS n FROM forward_contacts WHERE owner=?",
                       (owner,)).fetchone()["n"]
        if n >= cf.MAX_CONTACTS:
            raise HTTPException(status_code=409,
                                detail=f"You can save up to {cf.MAX_CONTACTS} phones. "
                                       "Remove one to add another.")
        if db.execute("SELECT 1 FROM forward_contacts WHERE owner=? AND sig_digits=?",
                      (owner, sig)).fetchone():
            raise HTTPException(status_code=409, detail="This phone is already saved.")
        row = db.execute(
            "INSERT INTO forward_contacts (owner, label, number, sig_digits) "
            "VALUES (?,?,?,?) RETURNING id", (owner, label, number, sig)).fetchone()
    logger.info("forward phone added for %s (%s)", owner, cf.mask(number))
    return {"id": row["id"], "label": label, "number": number,
            "sig_digits": sig, "verified": False}


@router.post("/api/forward-contacts/{contact_id}/verify-call")
async def forward_contact_verify_call(contact_id: int, owner: str = Depends(paid_owner)):
    """Ring the phone; a voice reads a 4-digit code twice."""
    try:
        started = await asyncio.to_thread(cf.start_verification, contact_id, owner)
    except cf.ForwardingError as exc:
        raise _fwd_error(exc)

    async def _ring():
        reply = await asyncio.to_thread(cf.place_call, started["command"])
        logger.info("forward verification call for %s -> %s", owner, reply[:80])

    asyncio.create_task(_ring())
    return {"calling": started["phone"], "expires_in_s": started["expires_in_s"],
            "calls_left_today": started["calls_left_today"]}


@router.post("/api/forward-contacts/{contact_id}/verify")
async def forward_contact_verify(contact_id: int, request: Request,
                                 owner: str = Depends(paid_owner)):
    body = await _body(request)
    try:
        return cf.check_verification(contact_id, owner, str(body.get("code") or ""))
    except cf.ForwardingError as exc:
        raise _fwd_error(exc)


@router.delete("/api/forward-contacts/{contact_id}")
def delete_forward_contact(contact_id: int, owner: str = Depends(paid_owner)):
    with get_db() as db:
        _contact_owned(db, contact_id, owner)
        affected = [r["id"] for r in db.execute(
            "SELECT id FROM phone_numbers WHERE owner=? AND forward_contact_id=?",
            (owner, contact_id)).fetchall()]
        # Any number pointing here goes back to its agent, so no call ever
        # routes to a phone that is no longer saved.
        db.execute(
            "UPDATE phone_numbers SET forward_mode='agent', forward_contact_id=NULL "
            "WHERE owner=? AND forward_contact_id=?", (owner, contact_id))
        db.execute("DELETE FROM forward_contacts WHERE id=?", (contact_id,))
    synced = cf.sync_numbers(affected) if affected else {}
    return {"deleted": contact_id, "numbers_back_to_agent": affected, "forwarding": synced}


@router.get("/api/forwarded-calls")
def list_forwarded_calls(limit: int = 50, owner: str = Depends(current_owner)):
    limit = max(1, min(int(limit or 50), 200))
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT f.id, f.number_id, f.did, f.caller, f.contact_number, f.outcome, "
            "f.cause, f.fallback, f.ring_s, f.talk_s, f.started_at, p.number AS line "
            "FROM forwarded_calls f LEFT JOIN phone_numbers p ON p.id = f.number_id "
            "WHERE f.owner=? ORDER BY f.started_at DESC NULLS LAST LIMIT ?",
            (owner, limit)).fetchall()]
    return {"calls": rows, "summary": cf.month_summary(owner)}


# ---------------------------------------------------------------------------
# (Caller-ID OTP verification removed: egressing through the tenant's OWN
# trunk means the carrier enforces which caller IDs the account may send —
# a platform-side OTP call to an AI-answered DID proved nothing. The way to
# check a number end-to-end is POST /api/numbers/{id}/test-call in server.py,
# which rings the account owner's phone from that number with its agent live.)
# ---------------------------------------------------------------------------
# Server info (the BYON "what to configure at your carrier" card)
# ---------------------------------------------------------------------------

@router.get("/api/telephony/server-info")
def server_info(owner: str = Depends(current_owner)):
    return {
        "sip_uri": f"sip:{PUBLIC_SIP_IP}:{PUBLIC_SIP_PORT}",
        "public_ip": PUBLIC_SIP_IP,
        "sip_port": int(PUBLIC_SIP_PORT),
        "transport": "udp",
        "rtp_range": RTP_PORT_RANGE,
        "notes": [
            "Signaling must originate from IPs listed in your trunk's provider_ips "
            "(enforced at the Kamailio SIP edge).",
            "Media (RTP) flows directly between your carrier and this server.",
            "Provider-agnostic: Plivo, Twilio, Telnyx, Vonage etc. all work once "
            "their signaling IPs are in provider_ips.",
        ],
    }
