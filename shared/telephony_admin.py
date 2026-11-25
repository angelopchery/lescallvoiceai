"""Operator-side telephony control: attach, bind, connect and move a DID.

The tenant-facing version of this lives in `dashboard/telephony_api.py` and is
deliberately narrow — a tenant may only ever touch their OWN trunk, their OWN
number and their OWN agents, and `paid_owner` refuses the whole surface unless
they are on a paid plan. That is right for self-service and useless for support:
the operator's jobs are precisely the ones a tenant cannot do.

  * attach a number to an account that has not built one itself
  * re-point a number at a different agent when the customer asks on the phone
  * take a number out of service without deleting it
  * hand a number from one account to another (a company moving its
    number between two of its own logins) — the single case that used to need
    psql, a hand-written gateway XML and `killgw`

Two rules make this safe, and they are enforced HERE rather than in the console
so the JSON API and the HTML forms cannot drift apart:

  1. **The receiving account must be entitled to telephony.** Not "was invited",
     not "looks paid" — `telephony_allowed()`, the same predicate the tenant's
     own Telephony panel and the dial gate use. An admin who could park a number
     on a Free account would create a number that answers calls the billing
     layer then refuses to meter.
  2. **A number may only be bound to agents its own account owns.** The DID
     resolver does not check ownership when it routes an inbound call — it
     trusts the binding — so a cross-tenant binding would have one tenant's
     agent answering another tenant's number, speaking their company name and
     billing their minutes (the RUNBOOK §33 class of bug, from the other end).

Every function returns plain dicts and raises `TelephonyAdminError` with a
message meant to be shown to the operator verbatim.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from shared import freeswitch_config as fsc
from shared import kamailio_acl
from shared.phone import significant_digits

log = logging.getLogger("telephony.admin")

_REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = _REPO_ROOT / "calling-agent" / "agents"

DIRECTIONS = ("both", "inbound", "outbound")
_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class TelephonyAdminError(Exception):
    """A refusal the operator should read and act on."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# eligibility
# ---------------------------------------------------------------------------

def subscription_state(owner: str) -> dict:
    """Is this account allowed to hold a live number, and why / why not."""
    owner = (owner or "").strip().lower()
    from shared.billing.entitlements import get_entitlements
    ent = get_entitlements(owner)
    plan = ent.get("plan_code") or "free"
    status = ent.get("status") or "ok"
    if status == "locked":
        return {"ok": False, "plan_code": plan, "status": status,
                "reason": f"{owner} is locked ({ent.get('lock_reason') or 'no reason recorded'}). "
                          f"Unlock the account before giving it a number."}
    if not int(ent.get("telephony") or 0):
        return {"ok": False, "plan_code": plan, "status": status,
                "reason": f"{owner} is on the {plan} plan, which does not include "
                          f"telephony. Change the plan (or grant the capability) first — "
                          f"a number on an unentitled account answers calls the "
                          f"billing layer then refuses to meter."}
    return {"ok": True, "plan_code": plan, "status": status, "reason": ""}


def require_subscription(owner: str) -> dict:
    state = subscription_state(owner)
    if not state["ok"]:
        raise TelephonyAdminError(state["reason"], status=409)
    return state


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------

def _agent_direction(slug: str) -> str:
    """`call_direction` as authored in the agent's YAML; "" when unreadable."""
    try:
        import yaml
        with open(AGENTS_DIR / slug / "agent.yaml", encoding="utf-8") as fh:
            return str((yaml.safe_load(fh) or {}).get("call_direction") or "")
    except Exception:
        return ""


def tenant_agents(owner: str) -> list[dict]:
    """The agents this account may bind a number to.

    Registry row AND a folder on disk: an archived row with no YAML would
    otherwise be offered in the console and then fail the binding check.
    """
    owner = (owner or "").strip().lower()
    from shared.db.database import get_db
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT slug, display_name, archived_at FROM agents "
            "WHERE owner=? AND archived_at IS NULL ORDER BY slug", (owner,)).fetchall()]
    out = []
    for r in rows:
        if not (AGENTS_DIR / r["slug"] / "agent.yaml").exists():
            continue
        out.append({"slug": r["slug"], "display_name": r["display_name"] or r["slug"],
                    "direction": _agent_direction(r["slug"])})
    return out


def tenant_trunks(owner: str) -> list[dict]:
    from shared.db.database import get_db
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT id, name, provider, sip_domain, caller_id, provider_ips, "
            "register, transport, config_status, last_applied_at "
            "FROM trunks WHERE owner=? ORDER BY id", (owner,)).fetchall()]
    return rows


def tenant_numbers(owner: str) -> list[dict]:
    from shared.db.database import get_db
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT p.*, t.name AS trunk_name, t.owner AS trunk_owner, "
            "       t.config_status AS trunk_status "
            "FROM phone_numbers p LEFT JOIN trunks t ON t.id = p.trunk_id "
            "WHERE p.owner=? ORDER BY p.id", (owner,)).fetchall()]
    return rows


def number_or_404(number_id: int) -> dict:
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute(
            "SELECT p.*, t.name AS trunk_name, t.owner AS trunk_owner, "
            "       t.config_status AS trunk_status "
            "FROM phone_numbers p LEFT JOIN trunks t ON t.id = p.trunk_id "
            "WHERE p.id=?", (int(number_id),)).fetchone()
    if row is None:
        raise TelephonyAdminError(f"no number with id {number_id}", status=404)
    return dict(row)


def _trunk_row(db, trunk_id: int) -> dict:
    row = db.execute("SELECT * FROM trunks WHERE id=?", (int(trunk_id),)).fetchone()
    if row is None:
        raise TelephonyAdminError(f"no trunk with id {trunk_id}", status=404)
    return dict(row)


def _all_provider_cidrs(db) -> list[str]:
    """Union of provider_ips across ALL trunks — the Kamailio ACL is one global
    list, so regenerating it from a single trunk would lock every other carrier
    out."""
    cidrs: list[str] = []
    for row in db.execute("SELECT provider_ips FROM trunks "
                          "WHERE provider_ips IS NOT NULL").fetchall():
        cidrs.extend(p.strip() for p in str(row["provider_ips"]).split(",") if p.strip())
    return cidrs


# ---------------------------------------------------------------------------
# binding
# ---------------------------------------------------------------------------

def _check_agent(db, slug: str, owner: str, want: str) -> list[str]:
    """Validate one agent binding for `owner`. Returns advisory warnings.

    An empty slug is legal — it means "this direction is not bound" — but a
    slug that belongs to someone else, or has no YAML, never is.
    """
    warnings: list[str] = []
    if not slug:
        return warnings
    if not _SLUG_RE.match(slug):
        raise TelephonyAdminError(f"invalid agent slug {slug!r}")
    if not (AGENTS_DIR / slug / "agent.yaml").exists():
        raise TelephonyAdminError(f"agent {slug!r} has no agent.yaml on disk")
    row = db.execute("SELECT owner FROM agents WHERE slug=?", (slug,)).fetchone()
    if row is None or not row["owner"]:
        raise TelephonyAdminError(
            f"agent {slug!r} is not registered to any account. Open it once in "
            f"the tenant's Agents page so it is claimed, then bind it.")
    if (row["owner"] or "").strip().lower() != owner:
        raise TelephonyAdminError(
            f"agent {slug!r} belongs to {row['owner']}, not {owner}. A number may "
            f"only be bound to agents in its OWN account.", status=403)
    have = _agent_direction(slug)
    if have and have != want:
        warnings.append(f"{slug} is authored for {have} calls but is bound to the "
                        f"{want} slot — it will use {have}-shaped prompts.")
    return warnings


def bind_number(number_id: int, inbound_agent_slug: str | None = None,
                outbound_agent_slug: str | None = None,
                direction: str | None = None, label: str | None = None,
                actor: str = "admin") -> dict:
    """Re-point a number at the account's own agents. Live on the NEXT call —
    the bridge reads `phone_numbers` per call, so nothing needs a restart."""
    from shared.db.database import get_db
    num = number_or_404(number_id)
    owner = (num["owner"] or "").strip().lower()
    warnings: list[str] = []
    updates: list[str] = []
    params: list = []

    with get_db() as db:
        if inbound_agent_slug is not None:
            slug = (inbound_agent_slug or "").strip()
            warnings += _check_agent(db, slug, owner, "inbound")
            updates.append("inbound_agent_slug=?")
            params.append(slug or None)
        if outbound_agent_slug is not None:
            slug = (outbound_agent_slug or "").strip()
            warnings += _check_agent(db, slug, owner, "outbound")
            updates.append("outbound_agent_slug=?")
            params.append(slug or None)
        if direction is not None:
            if direction not in DIRECTIONS:
                raise TelephonyAdminError(
                    f"direction must be one of {', '.join(DIRECTIONS)}")
            updates.append("direction=?")
            params.append(direction)
        if label is not None:
            updates.append("label=?")
            params.append(str(label)[:200])
        if not updates:
            raise TelephonyAdminError("nothing to change")
        params.append(int(number_id))
        db.execute(f"UPDATE phone_numbers SET {', '.join(updates)} WHERE id=?",
                   tuple(params))
    log.info("[admin-tel] %s rebound %s: %s", actor, num["number"], sorted(updates))
    return {"number": number_or_404(number_id), "warnings": warnings}


# ---------------------------------------------------------------------------
# add / remove
# ---------------------------------------------------------------------------

def _owner_slug(owner: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (owner or "").lower()).strip("-")


def create_trunk(owner: str, name: str, sip_domain: str, provider: str = "plivo",
                 caller_id: str = "", provider_ips: str = "", proxy: str = "",
                 username: str = "", password: str = "", register: bool = False,
                 transport: str = "udp", actor: str = "admin") -> dict:
    """Register a carrier trunk FOR a tenant. Same naming law as self-service:
    the FreeSWITCH gateway name is global, so it is prefixed with the owner."""
    owner = (owner or "").strip().lower()
    require_subscription(owner)
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise TelephonyAdminError("trunk name required ([A-Za-z0-9_-], max 64)")
    if not (sip_domain or "").strip():
        raise TelephonyAdminError("sip_domain is required")
    full_name = f"{_owner_slug(owner)}_{name}"

    from shared.db.database import get_db
    with get_db() as db:
        if db.execute("SELECT 1 FROM trunks WHERE name=?", (full_name,)).fetchone():
            raise TelephonyAdminError(f"a trunk named {full_name} already exists",
                                      status=409)
        row = db.execute(
            "INSERT INTO trunks (name, provider, owner, sip_domain, proxy, username, "
            "password, register, caller_id, provider_ips, transport) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING *",
            (full_name, provider or "plivo", owner, sip_domain.strip(), proxy or "",
             username or "", password or "", 1 if register else 0, caller_id or "",
             provider_ips or "", transport or "udp")).fetchone()
        acl = kamailio_acl.sync_carrier_acl(_all_provider_cidrs(db))
    log.info("[admin-tel] %s created trunk %s for %s", actor, full_name, owner)
    return {"trunk": dict(row), "kamailio_acl": acl}


def create_number(owner: str, number: str, trunk_id: int,
                  inbound_agent_slug: str = "", outbound_agent_slug: str = "",
                  direction: str = "both", label: str = "",
                  actor: str = "admin") -> dict:
    """Attach a DID to an account and bind it to that account's agents.

    The trunk must belong to the SAME account: egress uses that gateway's
    carrier credentials, so a number riding someone else's trunk would send a
    caller ID that carrier has not authorised — and bill the wrong customer.
    """
    owner = (owner or "").strip().lower()
    require_subscription(owner)
    number = (number or "").strip()
    sig = significant_digits(number)
    if len(sig) < 7:
        raise TelephonyAdminError("number must have at least 7 digits")
    if direction not in DIRECTIONS:
        raise TelephonyAdminError(f"direction must be one of {', '.join(DIRECTIONS)}")

    from shared.db.database import get_db
    warnings: list[str] = []
    with get_db() as db:
        trunk = _trunk_row(db, trunk_id)
        if (trunk["owner"] or "").strip().lower() != owner:
            raise TelephonyAdminError(
                f"trunk {trunk['name']} belongs to {trunk['owner']}, not {owner}. "
                f"Create a trunk for this account first, or transfer that one.",
                status=403)
        dup = db.execute("SELECT id, owner FROM phone_numbers WHERE number=? OR sig_digits=?",
                         (number, sig)).fetchone()
        if dup:
            raise TelephonyAdminError(
                f"{number} is already registered (id {dup['id']}, account "
                f"{dup['owner']}). Transfer it instead of adding it twice.",
                status=409)
        warnings += _check_agent(db, (inbound_agent_slug or "").strip(), owner, "inbound")
        warnings += _check_agent(db, (outbound_agent_slug or "").strip(), owner, "outbound")
        row = db.execute(
            "INSERT INTO phone_numbers (number, sig_digits, trunk_id, "
            "inbound_agent_slug, outbound_agent_slug, owner, direction, label, "
            "config_status) VALUES (?,?,?,?,?,?,?,?, 'pending') RETURNING *",
            (number, sig, int(trunk_id), (inbound_agent_slug or "").strip() or None,
             (outbound_agent_slug or "").strip() or None, owner, direction,
             (label or "added by admin")[:200])).fetchone()
    log.info("[admin-tel] %s added %s to %s", actor, number, owner)
    return {"number": dict(row), "warnings": warnings,
            "note": "press Connect to write the FreeSWITCH config for this DID"}


def remove_number(number_id: int, actor: str = "admin") -> dict:
    """Delete a number AND its generated dialplan.

    Deleting only the row is what the self-service endpoint does, and it leaves
    FreeSWITCH still answering the DID — which is how a "removed" number keeps
    taking calls for an account that no longer has it.
    """
    from shared.db.database import get_db
    num = number_or_404(number_id)
    from shared import call_forwarding as cf
    cf.remove_forward_file(num, reload=False)
    fs = fsc.remove_inbound_dialplan(num, reload=True)
    with get_db() as db:
        db.execute("DELETE FROM phone_numbers WHERE id=?", (int(number_id),))
    log.info("[admin-tel] %s removed %s (was %s)", actor, num["number"], num["owner"])
    return {"deleted": int(number_id), "number": num["number"], "freeswitch": fs}


def delete_trunk(trunk_id: int, actor: str = "admin") -> dict:
    """Delete a trunk that no number still uses, and retire its gateway."""
    from shared.db.database import get_db
    with get_db() as db:
        trunk = _trunk_row(db, trunk_id)
        n = db.execute("SELECT COUNT(*) AS n FROM phone_numbers WHERE trunk_id=?",
                       (int(trunk_id),)).fetchone()["n"]
        if n:
            raise TelephonyAdminError(
                f"{n} number(s) still use this trunk — remove or move them first",
                status=409)
    fs = fsc.remove_gateway(trunk, reload=True)
    with get_db() as db:
        db.execute("DELETE FROM trunks WHERE id=?", (int(trunk_id),))
        acl = kamailio_acl.sync_carrier_acl(_all_provider_cidrs(db))
    log.info("[admin-tel] %s deleted trunk %s", actor, trunk["name"])
    return {"deleted": int(trunk_id), "name": trunk["name"], "freeswitch": fs,
            "kamailio_acl": acl}


# ---------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------

def connect_number(number_id: int, actor: str = "admin") -> dict:
    """Put a DID in service: write the gateway + dialplan and reload FreeSWITCH.

    Refuses a number whose account is not entitled — this is the moment the
    number starts answering real calls, so it is the right place to check, not
    only when the row was created.
    """
    from shared.db.database import get_db
    num = number_or_404(number_id)
    owner = (num["owner"] or "").strip().lower()
    require_subscription(owner)
    if not num.get("trunk_id"):
        raise TelephonyAdminError(
            f"{num['number']} has no trunk. Attach it to one of {owner}'s trunks first.")
    if not (num.get("inbound_agent_slug") or num.get("outbound_agent_slug")):
        raise TelephonyAdminError(
            f"{num['number']} has no agent bound. A connected DID with no agent "
            f"answers with the neutral fallback persona, not this tenant's agent.")

    with get_db() as db:
        trunk = _trunk_row(db, int(num["trunk_id"]))
        numbers = [dict(r) for r in db.execute(
            "SELECT * FROM phone_numbers WHERE trunk_id=?",
            (int(num["trunk_id"]),)).fetchall()]
        cidrs = _all_provider_cidrs(db)

    try:
        result = fsc.apply_config(trunk, numbers, reload=True, all_provider_cidrs=cidrs)
    except PermissionError as exc:
        raise TelephonyAdminError(
            f"cannot write {fsc.FS_CONF_DIR}: {exc}. The admin container needs "
            f"group-write on sip_profiles/external and dialplan/public.", status=500)
    result["kamailio_acl"] = kamailio_acl.sync_carrier_acl(cidrs)
    status = "applied" if result.get("reloaded") else "error"

    with get_db() as db:
        db.execute("UPDATE trunks SET config_status=?, last_applied_at=CURRENT_TIMESTAMP "
                   "WHERE id=?", (status, int(num["trunk_id"])))
        db.execute("UPDATE phone_numbers SET config_status=?, "
                   "last_applied_at=CURRENT_TIMESTAMP WHERE trunk_id=?",
                   (status, int(num["trunk_id"])))
    from shared import call_forwarding as cf
    result["forwarding"] = cf.sync_numbers([n["id"] for n in numbers])
    log.info("[admin-tel] %s connected %s (%s)", actor, num["number"], status)
    return {"status": status, "number": num["number"], **result}


def disconnect_number(number_id: int, actor: str = "admin") -> dict:
    """Take a DID out of service WITHOUT deleting it.

    The dialplan goes; the row, its trunk and its agent bindings stay, so
    reconnecting is one click and nothing about the account's setup is lost.
    The gateway is left alone — other numbers on the same trunk still need it.
    """
    from shared.db.database import get_db
    num = number_or_404(number_id)
    from shared import call_forwarding as cf
    cf.remove_forward_file(num, reload=False)
    fs = fsc.remove_inbound_dialplan(num, reload=True)
    with get_db() as db:
        db.execute("UPDATE phone_numbers SET config_status='disconnected' WHERE id=?",
                   (int(number_id),))
    log.info("[admin-tel] %s disconnected %s", actor, num["number"])
    return {"status": "disconnected", "number": num["number"], "freeswitch": fs}


# ---------------------------------------------------------------------------
# proving it actually works
# ---------------------------------------------------------------------------

def test_call(number_id: int, destination: str, actor: str = "admin") -> dict:
    """Ring `destination` FROM this number, over its trunk, with its agent live.

    Connect can only ever report that OUR side is configured. It writes the
    gateway and the dialplan and says `applied`, and it says exactly that when
    the number is not yet pointed at us by the carrier — FreeSWITCH is then
    correctly set up to answer a call that will never arrive. This is the only
    thing on the admin plane that distinguishes "ready" from "working".

    It exercises the OUTBOUND binding, which is the one worth proving before a
    campaign runs: caller id + trunk + agent, end to end.

    THIS PLACES A REAL CALL. It meters, it costs money and it counts against
    the tenant's quota exactly like any other call — so it goes through the
    same dial gate, and the reservation it takes is released here rather than
    left to expire.
    """
    dest = "".join(c for c in str(destination or "") if c.isdigit())
    if len(dest) < 7:
        raise TelephonyAdminError(
            "Enter the number to ring, with country code — the test call dials it.")

    num = number_or_404(number_id)
    owner = (num["owner"] or "").strip().lower()
    require_subscription(owner)
    if not num.get("trunk_name"):
        raise TelephonyAdminError(f"{num['number']} has no trunk to dial out through.")
    if (num.get("trunk_status") or "") != "applied":
        raise TelephonyAdminError(
            f"Press Connect on {num['number']} first — its trunk's gateway is "
            f"{num.get('trunk_status') or 'not applied'}, so there is nothing to dial through.")

    # The OUTBOUND slot, falling back to inbound exactly as the live dial path
    # does, so the test proves what a real outbound call would use.
    slug = num.get("outbound_agent_slug") or num.get("inbound_agent_slug") or ""
    from shared.db.database import get_db
    with get_db() as db:
        _check_agent(db, slug, owner, "outbound")

    from shared.billing.entitlements import check_dial_allowed, release_reservation
    decision = check_dial_allowed(owner, origin="admin-test")
    if not decision.allowed:
        raise TelephonyAdminError(
            f"The billing gate refused this call for {owner}: {decision.message}",
            status=402)

    from shared.fs_esl import fs_api
    from shared.fs_originate import originate_cmd
    caller = "".join(c for c in num["number"] if c.isdigit())
    cmd = originate_cmd(dest, agent_slug=slug, caller_id=caller,
                        gateway_name=num["trunk_name"])
    try:
        out = fs_api(cmd, timeout=60)
    except Exception as exc:
        release_reservation(getattr(decision, "reservation", "") or "")
        raise TelephonyAdminError(f"Could not reach FreeSWITCH: {exc!r}", status=502)
    finally:
        # The ledger row for a real answered call closes the reservation on its
        # own; this one is ours to drop either way, so a refused or unanswered
        # test never leaves a hold against the tenant's quota.
        release_reservation(getattr(decision, "reservation", "") or "")

    body = (out or "").strip()
    ok = body.startswith("+OK")
    log.info("[admin-tel] %s test-called %s from %s via %s: %s",
             actor, dest, num["number"], num["trunk_name"], body[:120])
    return {"ok": ok, "number": num["number"], "destination": dest,
            "agent_slug": slug, "gateway": num["trunk_name"],
            "response": body[:300]}


# ---------------------------------------------------------------------------
# transfer
# ---------------------------------------------------------------------------

def transfer_preview(number_id: int, new_owner: str) -> dict:
    """What moving this number would take, and what would break if it did not.

    Called before the transfer so the console can show the operator the whole
    consequence — which trunk comes along, which agents would be orphaned —
    rather than discovering it afterwards.
    """
    new_owner = (new_owner or "").strip().lower()
    num = number_or_404(number_id)
    old_owner = (num["owner"] or "").strip().lower()
    from shared.db.database import get_db
    with get_db() as db:
        siblings = db.execute(
            "SELECT COUNT(*) AS n FROM phone_numbers WHERE trunk_id=? AND id<>?",
            (num.get("trunk_id"), int(number_id))).fetchone()["n"] if num.get("trunk_id") else 0
        bound = [s for s in (num.get("inbound_agent_slug"), num.get("outbound_agent_slug")) if s]
        agent_rows = []
        for slug in dict.fromkeys(bound):
            r = db.execute("SELECT slug, owner, display_name FROM agents WHERE slug=?",
                           (slug,)).fetchone()
            if r:
                agent_rows.append(dict(r))
    return {
        "number": num["number"], "from": old_owner, "to": new_owner,
        "trunk": {"id": num.get("trunk_id"), "name": num.get("trunk_name"),
                  "owner": num.get("trunk_owner"), "other_numbers": int(siblings or 0),
                  "can_move": bool(num.get("trunk_id")) and not int(siblings or 0)},
        "bound_agents": agent_rows,
        "eligibility": subscription_state(new_owner),
    }


def transfer_number(number_id: int, new_owner: str, move_trunk: bool = True,
                    move_agents: bool = True, rename_trunk: bool = True,
                    actor: str = "admin", reason: str = "") -> dict:
    """Hand a number (and, normally, its trunk and bound agents) to another account.

    All three pieces move together by default because all three are checked
    against the number's owner on the live path:

      * `phone_numbers.owner` decides who is BILLED for the call and whose
        contacts the caller is matched against;
      * `trunks.owner` is checked before every outbound dial — a number whose
        trunk stayed behind can receive calls but can never place one;
      * `agents.owner` is checked too, and an agent left with the old account
        makes the number unusable in the direction it was bound to.

    Moving the number alone is therefore not a transfer; it is a half-migration
    that looks complete in the console and fails on the first outbound call.

    The trunk is also RENAMED to the new owner's prefix, because the name is
    the FreeSWITCH gateway name and is shown to the tenant — leaving it means
    the receiving account reads the previous owner's email address in their own
    Telephony panel. The gateway is rewritten and the old one retired in the
    same step; skip with `rename_trunk=False` if FreeSWITCH is unreachable.
    """
    new_owner = (new_owner or "").strip().lower()
    if not new_owner:
        raise TelephonyAdminError("a destination account is required")
    require_subscription(new_owner)

    num = number_or_404(number_id)
    old_owner = (num["owner"] or "").strip().lower()
    if old_owner == new_owner:
        raise TelephonyAdminError(f"{num['number']} already belongs to {new_owner}")

    from shared.db.database import get_db
    moved: dict = {"number": num["number"], "from": old_owner, "to": new_owner,
                   "trunk": None, "agents": [], "freeswitch": {}}

    with get_db() as db:
        trunk = None
        if move_trunk and num.get("trunk_id"):
            trunk = _trunk_row(db, int(num["trunk_id"]))
            others = db.execute(
                "SELECT COUNT(*) AS n FROM phone_numbers WHERE trunk_id=? AND id<>?",
                (int(num["trunk_id"]), int(number_id))).fetchone()["n"]
            if others:
                raise TelephonyAdminError(
                    f"trunk {trunk['name']} still carries {others} other number(s) "
                    f"for {trunk['owner']}; it cannot move with this one. Re-run "
                    f"with move_trunk=false and give {new_owner} their own trunk.",
                    status=409)

        agents_to_move: list[str] = []
        if move_agents:
            for slug in dict.fromkeys(
                    s for s in (num.get("inbound_agent_slug"),
                                num.get("outbound_agent_slug")) if s):
                row = db.execute("SELECT owner FROM agents WHERE slug=?", (slug,)).fetchone()
                if row and (row["owner"] or "").strip().lower() == old_owner:
                    agents_to_move.append(slug)

        # One transaction: a number that moved without its trunk, or with an
        # agent left behind, is worse than a transfer that did not happen.
        db.execute("UPDATE phone_numbers SET owner=? WHERE id=?",
                   (new_owner, int(number_id)))
        # A forwarding number would otherwise keep ringing the PREVIOUS
        # owner's phone — the new account's callers reaching a stranger.
        db.execute("UPDATE phone_numbers SET forward_mode='agent', "
                   "forward_contact_id=NULL WHERE id=?", (int(number_id),))
        if trunk is not None:
            new_name = trunk["name"]
            if rename_trunk:
                suffix = trunk["name"].split("_", 1)[1] if "_" in trunk["name"] else trunk["name"]
                new_name = f"{_owner_slug(new_owner)}_{suffix}"
                if db.execute("SELECT 1 FROM trunks WHERE name=? AND id<>?",
                              (new_name, trunk["id"])).fetchone():
                    new_name = trunk["name"]        # collision: keep the old name
            db.execute("UPDATE trunks SET owner=?, name=? WHERE id=?",
                       (new_owner, new_name, int(trunk["id"])))
            moved["trunk"] = {"id": trunk["id"], "was": trunk["name"], "now": new_name}
        for slug in agents_to_move:
            db.execute("UPDATE agents SET owner=?, updated_at=CURRENT_TIMESTAMP "
                       "WHERE slug=?", (new_owner, slug))
        moved["agents"] = agents_to_move

    # FreeSWITCH last: the DB is now the truth, and a reload failure here is
    # recoverable with Connect rather than leaving a half-moved number.
    from shared import call_forwarding as cf
    moved["forwarding"] = cf.remove_forward_file(num, reload=True)
    if moved["trunk"] and moved["trunk"]["now"] != moved["trunk"]["was"]:
        with get_db() as db:
            new_trunk = _trunk_row(db, int(moved["trunk"]["id"]))
            numbers = [dict(r) for r in db.execute(
                "SELECT * FROM phone_numbers WHERE trunk_id=?",
                (int(moved["trunk"]["id"]),)).fetchall()]
            cidrs = _all_provider_cidrs(db)
        try:
            moved["freeswitch"]["applied"] = fsc.apply_config(
                new_trunk, numbers, reload=True, all_provider_cidrs=cidrs)
            moved["freeswitch"]["retired"] = fsc.remove_gateway(
                {"name": moved["trunk"]["was"]}, reload=True)
        except Exception as exc:                    # pragma: no cover - FS is external
            log.exception("[admin-tel] FreeSWITCH rewrite failed after transfer")
            moved["freeswitch"]["error"] = repr(exc)

    log.info("[admin-tel] %s transferred %s from %s to %s (trunk=%s agents=%s) — %s",
             actor, num["number"], old_owner, new_owner,
             moved["trunk"], moved["agents"], reason or "no reason given")
    return moved
