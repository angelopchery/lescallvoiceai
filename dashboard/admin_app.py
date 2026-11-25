"""Super-admin plane — a SEPARATE app, deliberately unreachable from the web.

Why it is not a route in the main API
-------------------------------------
Roles are flattened in this platform: `auth.is_admin()` returns False for
everyone, and the tenancy gate is a deny-by-default allowlist of prefixes.
Any prefix added to that allowlist becomes visible to EVERY tenant. So there
is no way to add an admin surface to the main app without either re-introducing
roles or exposing it — hence a second process.

How it is reached
-----------------
A UNIX domain socket, primarily. An SSRF that can make the app POST to an
arbitrary URL (which this platform demonstrably has — the automation webhook
action) cannot address a UNIX socket at all, whereas it can reach any
127.0.0.1 port from a host-network container. An optional loopback TCP bind
exists for tunnelling convenience and is off by default.

    ssh -L 8090:127.0.0.1:8090 angelo@<vm>          # with ADMIN_TCP_ENABLED
    curl --unix-socket /run/lescall-admin/lescall-admin.sock http://local/admin/api/health \
         -H "Authorization: Bearer $ADMIN_TOKEN"

Honest threat model
-------------------
There is no host firewall on this VM (`iptables ACCEPT`, no ufw), the GCP VPC
is the only outer wall, and loopback is shared with every host-network
container. So the bearer token is THE wall, not a second one. That is why this
app refuses to start without a strong one — there is deliberately no
"unset means open" path — and why it audits reads as well as writes: if a
token leaks, reads are what an attacker uses first.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import logging
import os
import secrets
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("lescall.admin")

MIN_TOKEN_LEN = 32
SOCKET_PATH = os.environ.get("LESCALL_ADMIN_SOCKET",
                             "/run/lescall-admin/lescall-admin.sock")
TCP_ENABLED = os.environ.get("ADMIN_TCP_ENABLED", "false").lower() in ("1", "true", "yes")
TCP_HOST = os.environ.get("ADMIN_TCP_HOST", "127.0.0.1")
TCP_PORT = int(os.environ.get("ADMIN_TCP_PORT", "8090"))

app = FastAPI(title="LesCall Admin", docs_url=None, redoc_url=None,
              openapi_url=None)
router = APIRouter(prefix="/admin")


# ---------------------------------------------------------------------------
# boot assertions — fail closed, loudly
# ---------------------------------------------------------------------------

def assert_safe_to_start() -> None:
    token = os.environ.get("ADMIN_TOKEN", "")
    if not token:
        raise SystemExit(
            "FATAL: ADMIN_TOKEN is not set. The admin plane can change any "
            "tenant's plan and issue payments; it will not start without a "
            "token. Generate one with `openssl rand -hex 32` and put it in "
            "/etc/lescall/admin.env (root, 0600).")
    if len(token) < MIN_TOKEN_LEN:
        raise SystemExit(
            f"FATAL: ADMIN_TOKEN is {len(token)} characters; at least "
            f"{MIN_TOKEN_LEN} are required. Use `openssl rand -hex 32`.")
    if TCP_ENABLED and TCP_HOST not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit(
            f"FATAL: refusing to bind the admin plane to {TCP_HOST!r}. It is "
            f"reachable over a UNIX socket or loopback only — never a routable "
            f"interface, and never through nginx.")


def _token() -> str:
    return os.environ.get("ADMIN_TOKEN", "")


ADMIN_COOKIE = "lescall_admin"


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def _cookie_value() -> str:
    """What a valid browser cookie contains: a hash of the token, not the token.

    Hashing means a stolen cookie cannot be replayed as an API bearer token —
    it only unlocks the read-only pages, and only until ADMIN_TOKEN rotates
    (which changes the hash and invalidates every cookie for free).
    """
    return hashlib.sha256(("lescall-admin-v1:" + _token()).encode()).hexdigest()


def require_token(request: Request, allow_cookie_write: bool = False) -> str:
    """Authorize a request. Bearer header always; cookie for GET pages only.

    The bearer token is never a query parameter — query strings land in access
    logs and shell history.

    The browser cookie is limited to SAFE METHODS here. Cookies are ambient
    credential material, so a cookie alone must never authorize a state change:
    while the operator's tunnel is open, any page they visit can attempt a POST
    to 127.0.0.1:8090.

    The console's own forms do mutate, and they are admitted by
    `admin_ui._guard`, which re-runs this check and then additionally demands a
    CSRF token derived from ADMIN_TOKEN plus a same-origin check. So the rule
    is: cookie alone = read; cookie + CSRF + same origin = the console's forms;
    bearer header = full API access with no CSRF needed (a cross-origin page
    cannot set headers).
    """
    supplied = _bearer(request)
    if supplied and secrets.compare_digest(supplied, _token()):
        return "admin"

    # `allow_cookie_write` is set ONLY by admin_ui._guard, which pairs it with a
    # CSRF token and a same-origin check. The JSON API never sets it, so a
    # cookie alone can never drive /admin/api/*.
    if allow_cookie_write or request.method in ("GET", "HEAD"):
        cookie = request.cookies.get(ADMIN_COOKIE, "")
        if cookie and secrets.compare_digest(cookie, _cookie_value()):
            return "admin-browser"

    logger.warning("[admin] REJECTED %s %s from %s", request.method,
                   request.url.path,
                   request.client.host if request.client else "unix-socket")
    raise HTTPException(status_code=401, detail="unauthorized")


def csrf_token() -> str:
    """Anti-CSRF value for browser forms.

    Derived from ADMIN_TOKEN, so an attacker page can neither read it
    (cross-origin responses are opaque) nor compute it — and rotating the admin
    token invalidates every outstanding form for free. Distinct from the
    session-cookie hash so neither value can be substituted for the other.
    """
    return hashlib.sha256(("lescall-admin-csrf-v1:" + _token()).encode()).hexdigest()


def _audit(request: Request, action: str, target: str = "",
           meta: dict | None = None) -> None:
    """Audit EVERY request, reads included.

    Auditing only mutations is the common mistake: a leaked token is used to
    read first — who the tenants are, what they pay, which accounts are
    dormant — and that reconnaissance would leave no trace at all.
    """
    source = request.client.host if request.client else "unix-socket"
    logger.info("[admin] %s %s target=%s from=%s", request.method, action,
                target or "-", source)
    try:
        from shared.audit import record
        record("admin", f"admin.{action}", target or None, meta, source)
    except Exception as exc:
        logger.warning("[admin] audit write failed: %r", exc)


# ---------------------------------------------------------------------------
# read surfaces
# ---------------------------------------------------------------------------

@router.get("/api/health")
def health(request: Request, _: str = Depends(require_token)):
    _audit(request, "health")
    from shared.billing import razorpay_client as rz
    from shared.billing import receipts
    from shared.billing.entitlements import enforcement_enabled
    ok, detail = receipts.platform_ready()
    return {
        "enforcement": enforcement_enabled(refresh=True),
        "gateway": rz.status(),
        "invoicing_ready": ok, "invoicing_detail": detail,
        "bind": {"socket": SOCKET_PATH, "tcp": f"{TCP_HOST}:{TCP_PORT}" if TCP_ENABLED else None},
    }


@router.get("/api/tenants")
def tenants(request: Request, _: str = Depends(require_token)):
    _audit(request, "tenants.list")
    from shared.db.database import get_db
    with get_db() as db:
        rows = [dict(r) for r in db.execute("""
            SELECT u.email AS owner, u.name, u.company, u.last_login,
                   e.plan_code, e.status, e.minutes_limit, e.period_end,
                   e.max_agent_families,
                   (SELECT COUNT(*) FROM agents a
                     WHERE a.owner = u.email AND a.archived_at IS NULL) AS agents,
                   (SELECT COUNT(*) FROM usage_ledger l
                     WHERE l.owner = u.email AND l.billable = 1) AS calls,
                   (SELECT s.status FROM subscriptions s
                     WHERE s.owner = u.email ORDER BY s.id DESC LIMIT 1) AS sub_status
            FROM users u LEFT JOIN entitlements e ON e.owner = u.email
            ORDER BY u.email""").fetchall()]
        # Tenants with rows but no users record (legacy / pre-tenancy data).
        orphans = [dict(r) for r in db.execute("""
            SELECT DISTINCT owner FROM (
              SELECT owner FROM agents UNION SELECT owner FROM usage_ledger
              UNION SELECT owner FROM calls) t
            WHERE owner <> '' AND owner IS NOT NULL
              AND owner NOT IN (SELECT email FROM users)""").fetchall()]
    return {"tenants": rows, "orphans": [o["owner"] for o in orphans]}


@router.get("/api/tenants/{owner}")
def tenant_detail(owner: str, request: Request, _: str = Depends(require_token)):
    _audit(request, "tenant.read", owner)
    from shared.billing.entitlements import usage_summary
    from shared.db.database import get_db
    with get_db() as db:
        subs = [dict(r) for r in db.execute(
            "SELECT * FROM subscriptions WHERE owner=? ORDER BY id DESC LIMIT 20",
            (owner,)).fetchall()]
        pays = [dict(r) for r in db.execute(
            "SELECT * FROM payments WHERE owner=? ORDER BY id DESC LIMIT 20",
            (owner,)).fetchall()]
        attempts = [dict(r) for r in db.execute(
            "SELECT * FROM payment_attempts WHERE owner=? ORDER BY id DESC LIMIT 20",
            (owner,)).fetchall()]
        claim = db.execute("SELECT * FROM pilot_claims WHERE owner=?",
                           (owner,)).fetchone()
    return {"owner": owner, "usage": usage_summary(owner),
            "subscriptions": subs, "payments": pays, "attempts": attempts,
            "pilot_claim": dict(claim) if claim else None}


@router.get("/api/billing-events")
def billing_events(request: Request, _: str = Depends(require_token),
                   unprocessed_only: int = 0, limit: int = 100):
    _audit(request, "events.list")
    from shared.db.database import get_db
    where = "WHERE processed_at IS NULL" if unprocessed_only else ""
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            f"SELECT id, event_id, event_type, owner, processed_at, attempts, "
            f"error, received_at FROM billing_events {where} "
            f"ORDER BY id DESC LIMIT ?", (min(limit, 500),)).fetchall()]
    return {"events": rows}


@router.get("/api/payments")
def payments(request: Request, _: str = Depends(require_token), limit: int = 100):
    _audit(request, "payments.list")
    from shared.db.database import get_db
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT id, owner, created_at, purpose, amount_paise, status, method, "
            "receipt_number, credit_note_number, refunded_paise "
            "FROM payments ORDER BY id DESC LIMIT ?", (min(limit, 500),)).fetchall()]
        totals = db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount_paise),0) AS gross, "
            "COALESCE(SUM(refunded_paise),0) AS refunded FROM payments "
            "WHERE status='captured'").fetchone()
    return {"payments": rows, "totals": dict(totals or {})}


# ---------------------------------------------------------------------------
# mutations — every one audited with a mandatory reason
# ---------------------------------------------------------------------------

def _reason(body: dict, minimum: int = 10) -> str:
    reason = str((body or {}).get("reason") or "").strip()
    if len(reason) < minimum:
        raise HTTPException(
            status_code=400,
            detail=f"a reason of at least {minimum} characters is required — "
                   f"it is written to the audit trail")
    return reason[:500]


@router.post("/api/tenants/{owner}/plan")
async def set_plan(owner: str, request: Request, _: str = Depends(require_token)):
    """Set or override a tenant's plan, including Enterprise custom terms.

    `force` is required while a live gateway subscription exists: overriding a
    plan the customer is actively paying Razorpay for is almost always a
    mistake, and the two would then disagree at the next renewal.
    """
    body = await request.json()
    reason = _reason(body)
    plan_code = str(body.get("plan_code") or "").strip()
    overrides = body.get("overrides") or {}
    if not isinstance(overrides, dict):
        raise HTTPException(status_code=400, detail="overrides must be an object")

    from shared.db.database import get_db
    with get_db() as db:
        if plan_code and not db.execute("SELECT 1 FROM plans WHERE code=?",
                                        (plan_code,)).fetchone():
            raise HTTPException(status_code=404, detail=f"unknown plan {plan_code!r}")
        live = db.execute(
            "SELECT id, razorpay_subscription_id FROM subscriptions WHERE owner=? "
            "AND kind='subscription' AND status IN ('active','authenticated','pending')",
            (owner,)).fetchone()
        if live and not body.get("force"):
            raise HTTPException(
                status_code=409,
                detail=f"{owner} has a live gateway subscription "
                       f"({live['razorpay_subscription_id']}). Cancel it first, "
                       f"or repeat with force:true.")
        before = db.execute("SELECT * FROM entitlements WHERE owner=?",
                            (owner,)).fetchone()
        merged = dict(overrides)
        if plan_code:
            merged["plan_code"] = plan_code
        db.execute(
            "INSERT INTO entitlements (owner, plan_code, overrides, updated_by, "
            "updated_at) VALUES (?, ?, ?, 'admin', CURRENT_TIMESTAMP) "
            "ON CONFLICT (owner) DO UPDATE SET overrides=excluded.overrides, "
            "updated_by='admin', updated_at=CURRENT_TIMESTAMP",
            (owner, plan_code or "free", json.dumps(merged)))

    from shared.billing.entitlements import resolve_entitlements
    after = resolve_entitlements(owner, actor="admin")
    _audit(request, "tenant.plan", owner,
           {"reason": reason, "plan_code": plan_code, "overrides": overrides,
            "before": dict(before) if before else None, "after": after})
    return {"ok": True, "entitlements": after}


@router.post("/api/tenants/{owner}/lock")
async def lock(owner: str, request: Request, _: str = Depends(require_token)):
    """Suspend an account. Wires `entitlements.status='locked'`, which every
    gate already honours — it was a dead enum value until now, and open signup
    means there needs to be a way to stop an abusive account immediately."""
    body = await request.json()
    reason = _reason(body)
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("UPDATE entitlements SET status='locked', lock_reason=?, "
                   "updated_by='admin', updated_at=CURRENT_TIMESTAMP WHERE owner=?",
                   (reason, owner))
    _audit(request, "tenant.lock", owner, {"reason": reason})
    return {"ok": True, "owner": owner, "status": "locked"}


@router.post("/api/tenants/{owner}/unlock")
async def unlock(owner: str, request: Request, _: str = Depends(require_token)):
    body = await request.json()
    reason = _reason(body)
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("UPDATE entitlements SET status='ok', lock_reason=NULL, "
                   "updated_by='admin', updated_at=CURRENT_TIMESTAMP WHERE owner=?",
                   (owner,))
    from shared.billing.entitlements import resolve_entitlements
    _audit(request, "tenant.unlock", owner, {"reason": reason})
    return {"ok": True, "entitlements": resolve_entitlements(owner, actor="admin")}


@router.post("/api/tenants/{owner}/manual-payment")
async def manual_payment(owner: str, request: Request,
                         _: str = Depends(require_token)):
    """Record an Enterprise payment taken outside the gateway (bank transfer,
    cheque) — issues a real GST invoice and extends the period."""
    body = await request.json()
    reason = _reason(body)
    try:
        base_paise = int(body["base_paise"])
        months = int(body.get("months") or 1)
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400,
                            detail="base_paise (int, ex-GST) is required")
    if base_paise <= 0:
        raise HTTPException(status_code=400, detail="base_paise must be positive")

    from shared.billing import razorpay_client as rz
    from shared.billing.entitlements import now_utc, resolve_entitlements
    from shared.billing.service import record_payment
    from shared.db.database import get_db

    start = now_utc()
    end = start + dt.timedelta(days=31 * months)
    gross = rz.gross_paise(base_paise)
    with get_db() as db:
        payment = record_payment(
            db, owner=owner, amount_paise=gross, status="captured",
            purpose="manual", source="manual",
            razorpay_payment_id=f"manual-{secrets.token_hex(12)}",
            method=str(body.get("method") or "bank_transfer"),
            period_start=start.isoformat(), period_end=end.isoformat(),
            notes=reason)
        db.execute(
            "UPDATE entitlements SET period_start=?, period_end=?, status='ok', "
            "lock_reason=NULL, updated_by='admin', updated_at=CURRENT_TIMESTAMP "
            "WHERE owner=?", (start.isoformat(), end.isoformat(), owner))
        row = db.execute("SELECT receipt_number FROM payments WHERE id=?",
                         ((payment or {}).get("id"),)).fetchone() if payment else None

    _audit(request, "tenant.manual_payment", owner,
           {"reason": reason, "base_paise": base_paise, "gross_paise": gross,
            "months": months})
    return {"ok": True, "receipt_number": (row or {}).get("receipt_number"),
            "gross_paise": gross, "period_end": end.isoformat(),
            "entitlements": resolve_entitlements(owner, actor="admin")}


@router.post("/api/tenants/{owner}/pilot-reset")
async def pilot_reset(owner: str, request: Request,
                      _: str = Depends(require_token)):
    """Re-grant the once-ever trial.

    APPEND-ONLY: writes released_by/released_at, never DELETE, so the record of
    who re-granted a trial survives. `confirm_owner` must equal the path owner
    — a body-less SSRF POST cannot supply it, so this cannot be triggered by
    an attacker who can only make the app issue a bare request.
    """
    body = await request.json()
    reason = _reason(body)
    if str((body or {}).get("confirm_owner") or "") != owner:
        raise HTTPException(
            status_code=400,
            detail="confirm_owner must equal the owner in the path")
    from shared.db.database import get_db
    with get_db() as db:
        db.execute(
            "UPDATE pilot_claims SET released_by='admin', "
            "released_at=CURRENT_TIMESTAMP, release_reason=? WHERE owner=?",
            (reason, owner))
    _audit(request, "tenant.pilot_reset", owner, {"reason": reason})
    return {"ok": True, "owner": owner}


@router.post("/api/tenants/{owner}/credits")
async def grant_credit(owner: str, request: Request,
                       _: str = Depends(require_token)):
    """Reward a tenant with free minutes for a number of months.

    `minutes` is a POOL for the whole validity window, not a monthly allowance —
    see shared/billing/credits.py for why that distinction had to be built
    rather than assumed. Takes effect on their next call; no payment or invoice
    row is created, because no money moved.
    """
    body = await request.json()
    reason = _reason(body)
    from shared.billing.credits import CreditError, grant_minutes
    try:
        grant = grant_minutes(owner, body.get("minutes"), body.get("valid_months", 1),
                              actor="admin-api", note=reason)
    except CreditError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(request, "tenant.credit.grant", owner, {"reason": reason, "grant": grant})
    from shared.billing.entitlements import usage_summary
    return {"ok": True, "grant": grant, "usage": usage_summary(owner)}


@router.get("/api/tenants/{owner}/credits")
def list_credits(owner: str, request: Request, _: str = Depends(require_token)):
    _audit(request, "tenant.credit.list", owner)
    from shared.billing.credits import list_grants, live_balance
    return {"owner": owner, "balance": live_balance(owner),
            "grants": list_grants(owner)}


@router.post("/api/credits/{grant_id}/revoke")
async def revoke_credit(grant_id: int, request: Request,
                        _: str = Depends(require_token)):
    """Withdraw the UNUSED part of a grant. Minutes already spent are not
    clawed back — the tenant made those calls in good faith."""
    body = await request.json()
    reason = _reason(body)
    from shared.billing.credits import CreditError, revoke_grant
    try:
        grant = revoke_grant(grant_id, actor="admin-api", reason=reason)
    except CreditError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(request, "tenant.credit.revoke", grant.get("owner") or "",
           {"reason": reason, "grant": grant})
    return {"ok": True, "grant": grant}


# ---------------------------------------------------------------------------
# telephony plane — attach, bind, connect and move a DID
#
# The tenant's own /api/numbers surface can only touch what the tenant already
# owns and is refused entirely on an unpaid plan, so none of this is reachable
# from the product. Every handler delegates to shared/telephony_admin.py, which
# is also what the HTML console calls — the two must not be able to disagree
# about who may bind what.
# ---------------------------------------------------------------------------

def _tel(fn):
    """Run a telephony_admin call, mapping its refusals onto HTTP."""
    from shared.telephony_admin import TelephonyAdminError
    try:
        return fn()
    except TelephonyAdminError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))


@router.get("/api/tenants/{owner}/telephony")
def tenant_telephony(owner: str, request: Request, _: str = Depends(require_token)):
    _audit(request, "telephony.read", owner)
    from shared.telephony_admin import (subscription_state, tenant_agents,
                                        tenant_numbers, tenant_trunks)
    return {"owner": owner, "eligibility": subscription_state(owner),
            "numbers": tenant_numbers(owner), "trunks": tenant_trunks(owner),
            "agents": tenant_agents(owner)}


@router.post("/api/tenants/{owner}/numbers")
async def admin_add_number(owner: str, request: Request,
                           _: str = Depends(require_token)):
    body = await request.json()
    reason = _reason(body)
    from shared.telephony_admin import create_number
    out = _tel(lambda: create_number(
        owner, str(body.get("number") or ""), body.get("trunk_id"),
        inbound_agent_slug=str(body.get("inbound_agent_slug") or ""),
        outbound_agent_slug=str(body.get("outbound_agent_slug") or ""),
        direction=str(body.get("direction") or "both"),
        label=str(body.get("label") or ""), actor="admin-api"))
    _audit(request, "telephony.number.add", owner, {"reason": reason, "result": out})
    return {"ok": True, **out}


@router.post("/api/tenants/{owner}/trunks")
async def admin_add_trunk(owner: str, request: Request,
                          _: str = Depends(require_token)):
    body = await request.json()
    reason = _reason(body)
    from shared.telephony_admin import create_trunk
    out = _tel(lambda: create_trunk(
        owner, str(body.get("name") or ""), str(body.get("sip_domain") or ""),
        provider=str(body.get("provider") or "plivo"),
        caller_id=str(body.get("caller_id") or ""),
        provider_ips=str(body.get("provider_ips") or ""),
        proxy=str(body.get("proxy") or ""), username=str(body.get("username") or ""),
        password=str(body.get("password") or ""), register=bool(body.get("register")),
        transport=str(body.get("transport") or "udp"), actor="admin-api"))
    _audit(request, "telephony.trunk.add", owner, {"reason": reason, "result": out})
    return {"ok": True, **out}


@router.patch("/api/numbers/{number_id}")
async def admin_bind_number(number_id: int, request: Request,
                            _: str = Depends(require_token)):
    """Rebind a number to agents IN ITS OWN ACCOUNT. Live on the next call."""
    body = await request.json()
    from shared.telephony_admin import bind_number
    out = _tel(lambda: bind_number(
        number_id,
        inbound_agent_slug=body.get("inbound_agent_slug"),
        outbound_agent_slug=body.get("outbound_agent_slug"),
        direction=body.get("direction"), label=body.get("label"),
        actor="admin-api"))
    _audit(request, "telephony.number.bind", str(number_id), {"result": out})
    return {"ok": True, **out}


@router.post("/api/numbers/{number_id}/connect")
def admin_connect_number(number_id: int, request: Request,
                         _: str = Depends(require_token)):
    from shared.telephony_admin import connect_number
    out = _tel(lambda: connect_number(number_id, actor="admin-api"))
    _audit(request, "telephony.number.connect", str(number_id), {"result": out})
    return {"ok": out.get("status") == "applied", **out}


@router.post("/api/numbers/{number_id}/disconnect")
def admin_disconnect_number(number_id: int, request: Request,
                            _: str = Depends(require_token)):
    from shared.telephony_admin import disconnect_number
    out = _tel(lambda: disconnect_number(number_id, actor="admin-api"))
    _audit(request, "telephony.number.disconnect", str(number_id), {"result": out})
    return {"ok": True, **out}


@router.post("/api/numbers/{number_id}/test-call")
async def admin_test_call(number_id: int, request: Request,
                          _: str = Depends(require_token)):
    """Ring a phone FROM this number, to prove the path end to end.

    Connect can only report that OUR side is configured; it says `applied` even
    when the carrier is not yet sending us the number. This is what separates
    "ready" from "working". It places a REAL, metered, billable call and goes
    through the tenant's own dial gate.
    """
    body = await request.json()
    reason = _reason(body)
    from shared.telephony_admin import test_call
    out = _tel(lambda: test_call(number_id, str(body.get("destination") or ""),
                                 actor="admin-api"))
    _audit(request, "telephony.number.test_call", str(number_id),
           {"reason": reason, "result": out})
    return out


@router.delete("/api/numbers/{number_id}")
def admin_remove_number(number_id: int, request: Request,
                        _: str = Depends(require_token)):
    from shared.telephony_admin import remove_number
    out = _tel(lambda: remove_number(number_id, actor="admin-api"))
    _audit(request, "telephony.number.remove", str(number_id), {"result": out})
    return {"ok": True, **out}


@router.get("/api/numbers/{number_id}/transfer-preview")
def admin_transfer_preview(number_id: int, to: str, request: Request,
                           _: str = Depends(require_token)):
    from shared.telephony_admin import transfer_preview
    out = _tel(lambda: transfer_preview(number_id, to))
    _audit(request, "telephony.number.transfer_preview", str(number_id), {"to": to})
    return out


@router.post("/api/numbers/{number_id}/transfer")
async def admin_transfer_number(number_id: int, request: Request,
                                _: str = Depends(require_token)):
    """Hand a number to another account, with its trunk and bound agents.

    Moving the row alone is a half-migration: outbound checks the TRUNK's owner
    and the AGENT's owner too, so the number would answer calls and never be
    able to place one. Both move by default.
    """
    body = await request.json()
    reason = _reason(body)
    from shared.telephony_admin import transfer_number
    out = _tel(lambda: transfer_number(
        number_id, str(body.get("to") or body.get("new_owner") or ""),
        move_trunk=body.get("move_trunk", True) is not False,
        move_agents=body.get("move_agents", True) is not False,
        rename_trunk=body.get("rename_trunk", True) is not False,
        actor="admin-api", reason=reason))
    _audit(request, "telephony.number.transfer", str(number_id),
           {"reason": reason, "result": out})
    return {"ok": True, **out}


@router.post("/api/tenants/{owner}/reconcile")
async def reconcile(owner: str, request: Request, _: str = Depends(require_token)):
    """Re-ask the gateway about this tenant's unresolved payments."""
    from shared.billing.service import reconcile_owner
    result = reconcile_owner(owner, actor="admin")
    _audit(request, "tenant.reconcile", owner, result)
    return {"ok": True, **result}


@router.post("/api/tenants/{owner}/subscription/cancel")
async def cancel_subscription(owner: str, request: Request,
                              _: str = Depends(require_token)):
    body = await request.json()
    reason = _reason(body)
    from shared.billing.service import _force_cancel
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute(
            "SELECT id, razorpay_subscription_id FROM subscriptions WHERE owner=? "
            "AND kind='subscription' AND status IN "
            "('active','authenticated','pending','halted','paused')",
            (owner,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no cancellable subscription")
    _force_cancel(owner, row["id"], row["razorpay_subscription_id"], reason)
    from shared.billing.entitlements import resolve_entitlements
    _audit(request, "tenant.subscription_cancel", owner, {"reason": reason})
    return {"ok": True, "entitlements": resolve_entitlements(owner, actor="admin")}


@router.post("/api/enforcement")
async def set_enforcement_flag(request: Request, _: str = Depends(require_token)):
    """The platform-wide enforcement switch. The single most consequential
    control here — it decides whether quotas do anything at all."""
    body = await request.json()
    reason = _reason(body, minimum=15)
    enabled = bool(body.get("enabled"))
    from shared.billing.entitlements import set_enforcement
    set_enforcement(enabled, actor=f"admin: {reason}")
    _audit(request, "enforcement", str(enabled), {"reason": reason})
    logger.critical("[admin] BILLING ENFORCEMENT set to %s :: %s", enabled, reason)
    return {"ok": True, "enforcement": enabled}


@router.post("/api/sync")
def run_sync(request: Request, _: str = Depends(require_token)):
    from shared.billing.sync import run_once
    _audit(request, "sync.run")
    return {"ok": True, **run_once()}


# ---------------------------------------------------------------------------
# minimal server-rendered UI — no React, no build step
# ---------------------------------------------------------------------------

_LOGIN_PAGE = """<!doctype html><meta charset=utf-8>
<title>LesCall admin</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
 body{{font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;margin:0;
      min-height:100vh;display:grid;place-items:center;background:#fafafa;color:#111}}
 form{{background:#fff;border:1px solid #ddd;border-radius:12px;padding:1.5rem;
      width:min(30rem,92vw)}}
 h1{{margin:0 0 .25rem;font-size:1.1rem}}
 p{{margin:0 0 1rem;color:#666;font-size:.85rem}}
 input{{width:100%;box-sizing:border-box;padding:.55rem;font-family:inherit;
       border:1px solid #ccc;border-radius:6px}}
 button{{margin-top:.75rem;width:100%;padding:.55rem;font-family:inherit;
        font-weight:600;background:#059669;color:#fff;border:0;border-radius:6px;
        cursor:pointer}}
 .err{{color:#991b1b;font-size:.85rem;margin-bottom:.75rem}}
</style>
<form method=post action="/admin/login">
  <h1>LesCall admin</h1>
  <p>Paste the ADMIN_TOKEN from <code>/etc/lescall/admin.env</code>.</p>
  {error}
  <input name=token type=password autofocus autocomplete=off
         placeholder="ADMIN_TOKEN" spellcheck=false>
  <button type=submit>Sign in</button>
</form>"""


@router.get("/login", response_class=HTMLResponse)
def login_page(bad: int = 0):
    """The token form. Unauthenticated by necessity — it is how you
    authenticate. Reachable only over the tunnel or the UNIX socket."""
    err = '<div class=err>That token was not accepted.</div>' if bad else ''
    return _LOGIN_PAGE.format(error=err)


@router.post("/login")
async def login_submit(request: Request):
    """Exchange the token for a read-only browser session.

    The token arrives in a POST BODY, never a query string, so it stays out of
    access logs, browser history and the Referer header. The cookie it sets
    carries a HASH of the token and unlocks GET pages only (see require_token).
    """
    form = await request.form()
    supplied = str(form.get("token") or "").strip()
    source = request.client.host if request.client else "unix-socket"
    if not supplied or not secrets.compare_digest(supplied, _token()):
        logger.warning("[admin] failed browser sign-in from %s", source)
        return RedirectResponse("/admin/login?bad=1", status_code=303)
    logger.info("[admin] browser sign-in from %s", source)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(
        ADMIN_COOKIE, _cookie_value(),
        max_age=8 * 3600, httponly=True, samesite="strict",
        # No `secure`: this is served over a plaintext SSH tunnel to loopback,
        # and a Secure cookie would simply never be sent back.
        path="/admin")
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE, path="/admin")
    return resp


# Public alias — admin_ui calls this for its own auditing.
audit = _audit

app.include_router(router)

# Server-rendered console (tenant list, tenant detail, plan controls, money).
from admin_ui import ui as _ui_router  # noqa: E402
app.include_router(_ui_router)

# Platform operations (health, key pool, calls, agents, audit) — everything an
# operator would otherwise need SSH, psql or docker for.
from admin_ops import ops as _ops_router  # noqa: E402
app.include_router(_ops_router)

# Integration API plane — per-tenant client-IP allowlist, rate ceilings and the
# key inventory for the customer-facing /v1 API. Mounted AFTER `router` so its
# HTML routes cannot shadow the bearer-only /admin/api/* JSON namespace.
from admin_api_console import api_console as _api_console_router  # noqa: E402
app.include_router(_api_console_router)

# Telephony plane — which account owns which number, which of THAT account's
# agents answers it, and whether it is in service. Mounted after `router` for
# the same reason as the integration console: its HTML routes must not shadow
# the bearer-only /admin/api/* JSON namespace.
from admin_telephony import telephony as _telephony_router  # noqa: E402
app.include_router(_telephony_router)


@app.exception_handler(401)
async def _unauthorized(request: Request, exc):
    """Send a BROWSER to the sign-in form; leave API clients a plain 401.

    Without this, opening /admin in a browser renders `{"detail":
    "unauthorized"}` and there is no path to authenticating — the HTML UI would
    be unreachable by the only kind of client that can render it.
    """
    wants_html = "text/html" in (request.headers.get("accept") or "")
    if wants_html and not request.url.path.startswith("/admin/api"):
        return RedirectResponse("/admin/login", status_code=303)
    return JSONResponse({"detail": "unauthorized"}, status_code=401)


@app.get("/health")
def bare_health():
    """Unauthenticated liveness only — no data. Lets the sentinel see the
    process is up without holding a token."""
    return JSONResponse({"ok": True})


def main() -> int:
    assert_safe_to_start()
    import asyncio

    import uvicorn

    Path(SOCKET_PATH).parent.mkdir(parents=True, exist_ok=True)

    # Bind the socket ALWAYS, and loopback TCP as well when enabled — not
    # either/or. The UNIX socket is the security-relevant interface (an
    # HTTP-URL SSRF cannot address one at all) and scripts use it; the TCP
    # port exists only so a browser at the far end of `ssh -L` can render the
    # HTML page. Dropping the socket the moment someone wants a browser would
    # trade away the stronger interface for the weaker one.
    servers = [uvicorn.Server(uvicorn.Config(
        app, uds=SOCKET_PATH, log_level="info"))]
    logger.info("[admin] binding UNIX socket %s", SOCKET_PATH)
    if TCP_ENABLED:
        logger.warning("[admin] loopback TCP ALSO enabled on %s:%s — reachable "
                       "by any process on this host; reach it with "
                       "`ssh -L %s:127.0.0.1:%s`", TCP_HOST, TCP_PORT,
                       TCP_PORT, TCP_PORT)
        servers.append(uvicorn.Server(uvicorn.Config(
            app, host=TCP_HOST, port=TCP_PORT, log_level="info")))

    async def _serve():
        await asyncio.gather(*(s.serve() for s in servers))

    asyncio.run(_serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
