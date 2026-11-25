"""Tenant-facing billing API — /api/billing/*.

Every endpoint is owner-scoped through `auth.current_owner`, except the
webhook, which is opened by EXACT path match in auth._OPEN_EXACT because its
HMAC signature is its credential.

The webhook deserves its own note. It is the only unauthenticated endpoint in
this process, and this process also serves every dashboard request and every
dial. So it reads its body with a hard byte cap instead of `await
request.body()`: unbounded buffering on an unauthenticated, un-rate-limited
endpoint is an OOM lever on a 2-core VM. nginx adds `limit_req` and a 256k
body cap in front (deploy/nginx-agent.lescall.conf).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import auth  # noqa: E402
from shared.billing import razorpay_client as rz  # noqa: E402
from shared.billing import receipts, service, webhooks  # noqa: E402
from shared.billing.entitlements import (  # noqa: E402
    agent_families_used, enforcement_enabled, get_entitlements,
    resolve_entitlements, usage_summary,
)

logger = logging.getLogger("dashboard.billing")
router = APIRouter()

# Razorpay webhook bodies are a few KB. 256 KB is generous; anything larger is
# not the gateway and is refused before it is buffered.
MAX_WEBHOOK_BYTES = 256 * 1024


def _owner(request: Request) -> str:
    return auth.current_owner(request)


def _require_tables() -> None:
    """Answer 503, not 500, in the window between deploying this code and
    running migration 0013.

    The rest of the platform degrades cleanly in that window (every gate fails
    open and every legacy endpoint is untouched), so the only thing that can
    break is this router — and "billing is not set up yet" is a far more useful
    thing for it to say than a stack trace.
    """
    from shared.billing import available
    if not available():
        raise HTTPException(
            status_code=503,
            detail="Billing is not available on this deployment "
                   "(the billing tables are Postgres-only).")
    try:
        from shared.db.database import get_db
        with get_db() as db:
            db.execute("SELECT 1 FROM plans LIMIT 1").fetchone()
    except Exception as exc:
        logger.error("[billing] tables missing — run migration 0013: %r", exc)
        raise HTTPException(
            status_code=503,
            detail="Billing is being set up. Please try again shortly.")


def _err(exc: service.CheckoutError) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "reason": str(exc), "detail": str(exc),
         "code": exc.code or "checkout_error"},
        status_code=exc.status)


# ---------------------------------------------------------------------------
# catalogue + state
# ---------------------------------------------------------------------------

@router.get("/api/billing/plans")
def list_plans(request: Request) -> JSONResponse:
    """The catalogue. Never hardcode these in the SPA — a price that lives in
    two places will eventually disagree with itself."""
    _require_tables()
    owner = _owner(request)
    from shared.db.database import get_db
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM plans WHERE active=1 ORDER BY sort").fetchall()]
        claim = db.execute(
            "SELECT owner FROM pilot_claims WHERE owner=? AND released_at IS NULL",
            (owner,)).fetchone()

    out = []
    for row in rows:
        gross = rz.gross_paise(row["price_paise"])
        out.append({
            "code": row["code"], "name": row["name"], "tagline": row["tagline"],
            "price_paise": row["price_paise"], "gross_paise": gross,
            "period": row["period"], "duration_days": row["duration_days"],
            "included_minutes": row["included_minutes"],
            "max_agent_families": row["max_agent_families"],
            "max_concurrent_calls": row["max_concurrent_calls"],
            "self_serve": bool(row["self_serve"]),
            "purchasable": bool(row["self_serve"]) and row["code"] != "free",
            "topup_allowed": bool(row["topup_allowed"]),
            "topup_pack_minutes": row["topup_pack_minutes"],
            "topup_rate_paise": row["topup_rate_paise"],
            "features": {
                "recordings": bool(row["recordings_enabled"]),
                "summaries": bool(row["summaries_enabled"]),
                "transcripts": bool(row["transcripts_enabled"]),
                "intelligence": bool(row["intelligence_enabled"]),
                "conversation_analytics": bool(row["conversation_analytics"]),
                "advanced_analytics": bool(row["advanced_analytics"]),
                "autocall": bool(row["autocall_enabled"]),
            },
            # Above Rs 15,000 gross, a card/UPI mandate needs an RBI
            # additional-factor approval every single month.
            "afa_free": gross <= 1_500_000,
            "already_used": bool(claim) and row["code"] == "pilot",
        })
    return JSONResponse({"plans": out, "gateway": rz.status()})


@router.get("/api/billing/summary")
def summary(request: Request) -> JSONResponse:
    """Everything the /billing page and the meters need, from the same numbers
    enforcement uses."""
    _require_tables()
    owner = _owner(request)
    usage = usage_summary(owner)
    from shared.db.database import get_db
    with get_db() as db:
        sub = db.execute(
            "SELECT id, plan_code, kind, status, current_start, current_end, "
            "cancel_at_cycle_end, scheduled, razorpay_subscription_id, "
            "razorpay_order_id, grace_until FROM subscriptions WHERE owner=? "
            "ORDER BY id DESC LIMIT 1", (owner,)).fetchone()
        claim = db.execute(
            "SELECT claimed_at FROM pilot_claims WHERE owner=? "
            "AND released_at IS NULL", (owner,)).fetchone()
        open_attempts = db.execute(
            "SELECT COUNT(*) AS n FROM payment_attempts WHERE owner=? "
            "AND status IN ('gateway_created','authorized') "
            "AND created_at > CURRENT_TIMESTAMP - INTERVAL '7 days'",
            (owner,)).fetchone()

    try:
        agents_used = agent_families_used(owner)
    except Exception:
        agents_used = None

    return JSONResponse({
        **usage,
        "owner": owner,
        "agents_used": agents_used,
        "subscription": dict(sub) if sub else None,
        "pilot_used": bool(claim),
        "enforcement": enforcement_enabled(),
        "gateway": rz.status(),
        # Drives the "a payment of yours may be unresolved" banner and the
        # self-serve recovery button.
        "unresolved_payments": int((open_attempts or {}).get("n") or 0),
        "banner": _banner(sub, usage),
    })


def _banner(sub, usage: dict) -> dict | None:
    """The one-line state message. Every subscription state has one — half of
    them were undefined in the first draft, which is how a customer ends up
    staring at a page that says nothing while their plan quietly lapses."""
    status = usage.get("status")
    if status == "locked":
        return {"tone": "rose", "text": "This account is suspended. Contact support."}
    if not sub:
        return None
    s = dict(sub)
    st = s["status"]
    if st == "created":
        return {"tone": "amber", "text": "Complete payment to activate your plan."}
    if st == "authenticated" and s["scheduled"]:
        return {"tone": "sky", "text": f"Switching to {s['plan_code']} on "
                                       f"{(s['current_start'] or '')[:10]}."}
    if st == "pending":
        return {"tone": "amber", "text": "Payment is being retried — update your "
                                         "payment method if this persists."}
    if st == "halted":
        return {"tone": "rose", "text": f"Payment failed. Your plan ends "
                                        f"{(s['grace_until'] or '')[:10]} unless "
                                        f"payment succeeds."}
    if st == "paused":
        return {"tone": "amber", "text": "Subscription paused."}
    if st == "cancelled":
        return {"tone": "amber", "text": f"Ends {(s['current_end'] or '')[:10]}, "
                                         f"then Free."}
    if st == "completed" and s["kind"] == "one_time":
        return {"tone": "slate", "text": "Trial ended — choose Basic or Growth."}
    if st == "completed":
        return {"tone": "slate", "text": "Subscription completed — resubscribe "
                                         "to continue."}
    if st == "expired":
        return {"tone": "rose", "text": "Authorization expired — please try again."}
    if st == "active" and s["kind"] == "one_time":
        remaining = usage.get("minutes_remaining")
        return {"tone": "sky",
                "text": f"Pilot trial — ends {(s['current_end'] or '')[:10]}"
                        + (f", {remaining} minutes left." if remaining is not None else ".")}
    return None


# ---------------------------------------------------------------------------
# checkout
# ---------------------------------------------------------------------------

@router.post("/api/billing/checkout")
async def checkout(request: Request) -> JSONResponse:
    owner = _owner(request)
    body = await request.json()
    plan_code = str((body or {}).get("plan_code") or "").strip()
    if not plan_code:
        raise HTTPException(status_code=400, detail="plan_code is required")
    try:
        result = service.start_checkout(
            owner, plan_code,
            ip=request.client.host if request.client else "",
            ua=request.headers.get("user-agent", ""))
    except service.CheckoutError as exc:
        return _err(exc)
    return JSONResponse({"ok": True, **result})


@router.post("/api/billing/topup")
async def topup(request: Request) -> JSONResponse:
    owner = _owner(request)
    body = await request.json()
    try:
        result = service.start_topup(
            owner, packs=int((body or {}).get("packs") or 1),
            ip=request.client.host if request.client else "",
            ua=request.headers.get("user-agent", ""))
    except service.CheckoutError as exc:
        return _err(exc)
    return JSONResponse({"ok": True, **result})


@router.post("/api/billing/checkout/verify")
async def checkout_verify(request: Request) -> JSONResponse:
    owner = _owner(request)
    body = await request.json()
    try:
        result = service.verify_checkout(owner, body or {})
    except service.CheckoutError as exc:
        return _err(exc)
    return JSONResponse({"ok": True, **result})


@router.post("/api/billing/cancel")
def cancel(request: Request) -> JSONResponse:
    owner = _owner(request)
    try:
        return JSONResponse({"ok": True, **service.cancel_at_cycle_end(owner)})
    except service.CheckoutError as exc:
        return _err(exc)


@router.post("/api/billing/change")
async def change_plan(request: Request) -> JSONResponse:
    """Plan change, uniform for every payment method.

    Razorpay's in-place PATCH works only for card-token subscriptions —
    UPI-Autopay and eMandate subscriptions cannot be updated at all — and it
    never prorates. Rather than a behaviour that silently depends on how the
    customer happened to pay, v1 does the same thing for everyone: the current
    subscription is cancelled at cycle end and the new one starts when it ends.
    """
    owner = _owner(request)
    body = await request.json()
    plan_code = str((body or {}).get("plan_code") or "").strip()
    if not plan_code:
        raise HTTPException(status_code=400, detail="plan_code is required")
    try:
        service.cancel_at_cycle_end(owner)
    except service.CheckoutError as exc:
        if exc.code != "no_subscription":
            return _err(exc)
    try:
        result = service.start_checkout(
            owner, plan_code,
            ip=request.client.host if request.client else "",
            ua=request.headers.get("user-agent", ""))
    except service.CheckoutError as exc:
        return _err(exc)
    return JSONResponse({"ok": True, "changed": True, **result})


# ---------------------------------------------------------------------------
# recovery — "I paid but nothing happened"
# ---------------------------------------------------------------------------

@router.post("/api/billing/reconcile")
def reconcile(request: Request) -> JSONResponse:
    """Self-serve recovery.

    Money can leave a customer's account without reaching us for three
    ordinary reasons — the tab was closed before the callback fired, the
    webhook was lost, or webhooks were not configured yet. All three look
    identical from the customer's side, and all three are fixed by re-asking
    the gateway. Giving them a button beats a support ticket.
    """
    owner = _owner(request)
    result = service.reconcile_owner(owner, actor="tenant")
    if result["recovered"]:
        message = ("We found your payment and your account is up to date. "
                   "Thank you for your patience.")
    elif result["checked"]:
        message = ("We rechecked with the payment provider and found no "
                   "completed payment waiting. If money has left your account, "
                   "contact support with the date and amount.")
    else:
        message = "There are no pending payments on this account."
    return JSONResponse({"ok": True, "message": message, **result})


# ---------------------------------------------------------------------------
# history + receipts
# ---------------------------------------------------------------------------

@router.get("/api/billing/payments")
def payments(request: Request, limit: int = Query(25, ge=1, le=100),
             offset: int = Query(0, ge=0)) -> JSONResponse:
    owner = _owner(request)
    from shared.db.database import get_db
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT id, created_at, issued_at, due_date, paid_at, purpose, "
            "amount_paise, base_paise, tax_paise, status, method, source, "
            "payment_reference, receipt_number, credit_note_number, "
            "refunded_paise, period_start, period_end, razorpay_payment_id "
            "FROM payments WHERE owner=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (owner, limit, offset)).fetchall()]
        total = db.execute("SELECT COUNT(*) AS n FROM payments WHERE owner=?",
                           (owner,)).fetchone()
        # What they still owe. Manual invoices issued on credit are real
        # obligations, so the customer sees them rather than being surprised
        # by a collections call.
        due = db.execute(
            "SELECT COALESCE(SUM(amount_paise),0) AS amount, COUNT(*) AS n "
            "FROM payments WHERE owner=? AND status='due'", (owner,)).fetchone()
    return JSONResponse({"payments": rows,
                         "total": int((total or {}).get("n") or 0),
                         "outstanding_paise": int(dict(due or {}).get("amount") or 0),
                         "outstanding_count": int(dict(due or {}).get("n") or 0),
                         "limit": limit, "offset": offset})


@router.get("/api/billing/receipts/{payment_id}")
def receipt(payment_id: int, request: Request) -> JSONResponse:
    """Owner-scoped: another tenant's payment id is a 404, not a 403 — a 403
    would confirm the id exists."""
    owner = _owner(request)
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute("SELECT * FROM payments WHERE id=? AND owner=?",
                         (payment_id, owner)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="receipt not found")
    return JSONResponse(receipts.render(dict(row)))


@router.get("/api/billing/profile")
def get_profile(request: Request) -> JSONResponse:
    owner = _owner(request)
    return JSONResponse({"profile": receipts.get_billing_profile(owner),
                         "platform_ready": receipts.platform_ready()[0]})


@router.put("/api/billing/profile")
async def put_profile(request: Request) -> JSONResponse:
    owner = _owner(request)
    body = await request.json()
    try:
        saved = receipts.save_billing_profile(owner, body or {})
    except ValueError as exc:
        return JSONResponse({"ok": False, "reason": str(exc), "detail": str(exc),
                             "code": "invalid_gstin"}, status_code=400)
    return JSONResponse({"ok": True, "profile": saved})


@router.get("/api/billing/attempts")
def attempts(request: Request, limit: int = Query(20, ge=1, le=100)) -> JSONResponse:
    """The tenant's own transaction log — including the attempts that never
    became money. This is what a support conversation actually needs."""
    owner = _owner(request)
    from shared.db.database import get_db
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT id, created_at, updated_at, kind, intent, plan_code, "
            "amount_paise, status, failure_step, failure_code, failure_reason, "
            "gateway_ref, reconciled_at, reconcile_note "
            "FROM payment_attempts WHERE owner=? ORDER BY id DESC LIMIT ?",
            (owner, limit)).fetchall()]
    return JSONResponse({"attempts": rows})


# ---------------------------------------------------------------------------
# webhook — the only unauthenticated route in this process
# ---------------------------------------------------------------------------

@router.post("/api/billing/webhook/razorpay")
async def razorpay_webhook(request: Request):
    if not rz.webhook_configured():
        # 503, not 200: a silent success would let the gateway mark events
        # delivered that we never actually stored.
        logger.error("[billing] webhook received but RAZORPAY_WEBHOOK_SECRET "
                     "is unset — refusing")
        return JSONResponse({"error": "webhook not configured"}, status_code=503)

    declared = request.headers.get("content-length")
    if declared and int(declared) > MAX_WEBHOOK_BYTES:
        return JSONResponse({"error": "payload too large"}, status_code=413)

    # Stream-read with a hard cap rather than `await request.body()`: a
    # Content-Length header is a claim, not a guarantee, and this endpoint is
    # unauthenticated.
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_WEBHOOK_BYTES:
            return JSONResponse({"error": "payload too large"}, status_code=413)
        chunks.append(chunk)
    raw = b"".join(chunks)

    signature = request.headers.get("x-razorpay-signature", "")
    if not rz.verify_webhook_signature(raw, signature):
        logger.error("[billing] webhook signature REJECTED (%d bytes)", len(raw))
        return JSONResponse({"error": "invalid signature"}, status_code=400)

    event_id = request.headers.get("x-razorpay-event-id", "")
    if not event_id:
        logger.error("[billing] webhook has no X-Razorpay-Event-Id — cannot "
                     "dedupe, refusing")
        return JSONResponse({"error": "missing event id"}, status_code=400)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    event_type = str(payload.get("event") or "")
    result = webhooks.ingest(event_id, event_type, raw, payload)
    # Always 200 once the event is stored: a non-2xx makes the gateway retry an
    # event we have already durably recorded.
    return JSONResponse({"ok": True, **result})


@router.get("/api/billing/health")
def health(request: Request) -> JSONResponse:
    """Tenant-visible, deliberately thin: platform posture only, no other
    tenant's data."""
    _owner(request)
    ok, detail = receipts.platform_ready()
    return JSONResponse({
        "enforcement": enforcement_enabled(),
        "gateway": rz.status(),
        "invoicing_ready": ok,
        "invoicing_detail": "" if ok else detail,
    })
