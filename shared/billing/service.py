"""Checkout, verification, webhook handling, and recovery.

This module is where "what if the payment goes wrong" is answered. The
catalogue of things that actually happen, and where each is handled:

  1. gateway not configured        -> GatewayUnavailable, 503 "not set up yet"
  2. gateway refuses to create     -> attempt row `gateway_error`, nothing local
                                      left half-built
  3. customer abandons the modal   -> attempt ages to `abandoned` (sync §4),
                                      subscription `created` -> `expired`
  4. card declined at the bank     -> `payment.failed` -> attempt `failed`,
                                      dunning notification, plan untouched
  5. paid, but the browser never
     called verify (tab closed)    -> the webhook opens the window anyway
  6. paid, but the webhook was
     lost or not yet configured    -> reconcile_owner() polls the gateway;
                                      the tenant can trigger it themselves
  7. paid twice (double submit)    -> idempotent on razorpay_payment_id; the
                                      second is flagged refund_due + alarm
  8. amount does not match the plan -> quarantined, NOT granted, alarm
  9. webhook delivered twice       -> event_id dedupe, one effect
 10. webhooks out of order         -> every handler upserts; none assumes it
                                      ran after another
 11. refund                        -> payment `refunded`, credit note, minutes
                                      recomputed
 12. failed renewal                -> pending -> halted -> grace -> Free, and
                                      the gateway subscription is CANCELLED so
                                      the tenant can subscribe again (§6.7)
 13. webhook names an unknown
     subscription                  -> recorded + alarmed, never granted

The one rule that ties them together: **the webhook is authoritative and the
browser is not**. `verify_checkout` is an optimistic fast path that may only
advance a `created` row exactly once; everything it does, the webhook can also
do on its own.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid

from shared.billing import razorpay_client as rz
from shared.billing import receipts
from shared.billing.entitlements import (
    as_dt, notify, now_utc, resolve_entitlements,
)

log = logging.getLogger("billing.service")

# A tenant may hold at most one open checkout at a time and may not hammer the
# gateway; both limits are per-owner and cheap to evaluate.
CHECKOUT_RATE_PER_HOUR = 5
# How long a `created` row stays claimable before sync expires it.
CHECKOUT_TTL_MIN = 30


class CheckoutError(RuntimeError):
    """A refusal the customer should read. Carries an HTTP status."""

    def __init__(self, message: str, status: int = 400, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def _iso(t: dt.datetime | None) -> str | None:
    return t.astimezone(dt.timezone.utc).isoformat() if t else None


# ---------------------------------------------------------------------------
# transaction log
# ---------------------------------------------------------------------------

def log_attempt(db, *, owner: str, kind: str, plan_code: str = "",
                intent: str = "new", amount_paise: int = 0,
                gateway_ref: str = "", status: str = "initiated",
                subscription_id: int | None = None, topup_id: int | None = None,
                ip: str = "", ua: str = "") -> int:
    row = db.execute(
        "INSERT INTO payment_attempts (owner, plan_code, subscription_id, "
        "topup_id, kind, intent, gateway_mode, gateway_ref, amount_paise, "
        "status, request_ip, user_agent) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
        (owner, plan_code or None, subscription_id, topup_id, kind, intent,
         rz.mode(), gateway_ref or None, int(amount_paise), status,
         ip[:60] or None, ua[:200] or None)).fetchone()
    return int(row["id"])


def update_attempt(db, attempt_id: int | None, **fields) -> None:
    if not attempt_id or not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE payment_attempts SET {cols}, updated_at=CURRENT_TIMESTAMP "
               f"WHERE id=?", (*fields.values(), attempt_id))


# ---------------------------------------------------------------------------
# checkout
# ---------------------------------------------------------------------------

def _plan(db, code: str) -> dict:
    row = db.execute("SELECT * FROM plans WHERE code=? AND active=1", (code,)).fetchone()
    if row is None:
        raise CheckoutError(f"Unknown plan '{code}'.", 404)
    return dict(row)


def _prefill(db, owner: str) -> dict:
    """Contact details for the checkout modal.

    Built strictly from the `users` row and the tenant's own billing profile —
    NEVER from the request body. A body-supplied prefill lets a caller attach
    someone else's phone number to a payment, which matters because the Pilot
    once-ever rule is bound to the verified contact number.
    """
    user = db.execute("SELECT name, phone FROM users WHERE email=?",
                      (owner,)).fetchone() or {}
    profile = receipts.get_billing_profile(owner)
    return {
        "name": profile.get("legal_name") or (user.get("name") or ""),
        "email": owner,
        "contact": profile.get("phone") or (user.get("phone") or ""),
    }


def _rate_limit(db, owner: str) -> None:
    row = db.execute(
        "SELECT COUNT(*) AS n FROM payment_attempts WHERE owner=? "
        "AND created_at > CURRENT_TIMESTAMP - INTERVAL '1 hour'",
        (owner,)).fetchone()
    if int((row or {}).get("n") or 0) >= CHECKOUT_RATE_PER_HOUR:
        raise CheckoutError(
            "Too many payment attempts in the last hour. Please wait a few "
            "minutes, or contact support if a payment is stuck.", 429,
            code="rate_limited")


def start_checkout(owner: str, plan_code: str, ip: str = "", ua: str = "") -> dict:
    """Open a checkout for a plan. Branches on the plan's billing model."""
    from shared.db.database import get_db
    if not rz.configured():
        raise CheckoutError(
            "Online payments are not enabled yet. Please contact us to "
            "activate your plan.", 503, code="gateway_unconfigured")

    with get_db() as db:
        plan = _plan(db, plan_code)
        if not plan["self_serve"]:
            raise CheckoutError(
                f"{plan['name']} is arranged with our team — please contact "
                f"us and we will set it up.", 400, code="not_self_serve")
        _rate_limit(db, owner)

    if plan["period"] == "one_time":
        return _checkout_pilot(owner, plan, ip, ua)
    return _checkout_subscription(owner, plan, ip, ua)


def _checkout_pilot(owner: str, plan: dict, ip: str, ua: str) -> dict:
    """The 7-day Pilot: a one-time Order, no mandate.

    The once-ever claim is taken HERE, at checkout, in the same transaction as
    the local row — not at payment. Claiming at payment lets two concurrent
    checkouts both create real gateway orders, and the constraint then fires
    inside the webhook, after the customer has been charged twice.
    """
    from shared.db.database import get_db
    gross = rz.gross_paise(plan["price_paise"])

    with get_db() as db:
        claim = db.execute(
            "SELECT owner, released_at FROM pilot_claims WHERE owner=?",
            (owner,)).fetchone()
        if claim and not claim["released_at"]:
            raise CheckoutError(
                "The Pilot is a one-time starting point and this account has "
                "already used it. Choose Basic or Growth to continue.",
                409, code="pilot_used")
        paid = db.execute(
            "SELECT 1 FROM subscriptions WHERE owner=? AND kind='subscription' "
            "AND status IN ('active','authenticated','pending','halted',"
            "'paused','cancelled','completed') LIMIT 1", (owner,)).fetchone()
        if paid:
            raise CheckoutError(
                "The Pilot is for new accounts. This account already has a "
                "paid plan on record.", 409, code="pilot_not_eligible")

        attempt_id = log_attempt(
            db, owner=owner, kind="one_time", plan_code=plan["code"],
            amount_paise=gross, ip=ip, ua=ua)

    receipt_ref = f"pilot-{uuid.uuid4().hex[:16]}"
    try:
        order = rz.create_order(
            amount_paise=gross, receipt=receipt_ref,
            notes={"owner": owner, "plan": plan["code"], "kind": "pilot"})
    except rz.GatewayError as exc:
        with get_db() as db:
            update_attempt(db, attempt_id, status="gateway_error",
                           failure_step="create", failure_code=exc.code or "",
                           failure_reason=str(exc)[:400])
        raise CheckoutError(
            "We could not start the payment. No money has been taken. "
            "Please try again in a moment.", 502, code="gateway_error") from exc

    order_id = order["id"]
    with get_db() as db:
        sub = db.execute(
            "INSERT INTO subscriptions (owner, plan_code, kind, status, "
            "razorpay_order_id, razorpay_mode, meta) "
            "VALUES (?,?,'one_time','created',?,?,?) RETURNING id",
            (owner, plan["code"], order_id, rz.mode(),
             json.dumps({"receipt": receipt_ref, "gross_paise": gross}))).fetchone()
        sub_id = int(sub["id"])
        # Claim and row in ONE transaction — this is the race fix.
        db.execute(
            "INSERT INTO pilot_claims (owner, subscription_id) VALUES (?,?) "
            "ON CONFLICT (owner) DO UPDATE SET subscription_id=excluded.subscription_id, "
            "claimed_at=CURRENT_TIMESTAMP, released_by=NULL, released_at=NULL, "
            "release_reason=NULL WHERE pilot_claims.released_at IS NOT NULL",
            (owner, sub_id))
        update_attempt(db, attempt_id, status="gateway_created",
                       gateway_ref=order_id, subscription_id=sub_id)
        prefill = _prefill(db, owner)

    log.info("[billing] pilot checkout opened owner=%s order=%s", owner, order_id)
    return {"flow": "order", "order_id": order_id, "key_id": rz.key_id(),
            "amount_paise": gross, "currency": "INR", "plan": plan["code"],
            "plan_name": plan["name"], "prefill": prefill,
            "attempt_id": attempt_id}


def _checkout_subscription(owner: str, plan: dict, ip: str, ua: str) -> dict:
    """Basic / Growth: a recurring gateway Subscription."""
    from shared.db.database import get_db
    column = "razorpay_plan_id_live" if rz.mode() == "live" else "razorpay_plan_id_test"
    gateway_plan = plan.get(column)
    if not gateway_plan:
        raise CheckoutError(
            f"{plan['name']} is not available for online purchase yet. "
            f"Please contact us.", 503, code="plan_not_bootstrapped")

    with get_db() as db:
        current = db.execute(
            "SELECT id, status, razorpay_subscription_id FROM subscriptions "
            "WHERE owner=? AND kind='subscription' AND scheduled=0 "
            "AND status IN ('authenticated','active','pending','halted','paused')",
            (owner,)).fetchone()

    if current:
        if current["status"] in ("active", "authenticated", "pending"):
            raise CheckoutError(
                "You already have an active subscription. Use Change plan "
                "instead.", 409, code="already_subscribed")
        # halted / paused: §6.7, the trap. Grace expiry used to downgrade the
        # entitlement but leave the row `halted` — which sits INSIDE the
        # uniqueness index, so the tenant could never check out any plan
        # again. Cancel it at the gateway first so the index frees up.
        log.info("[billing] cancelling lingering %s subscription for %s before "
                 "re-checkout", current["status"], owner)
        _force_cancel(owner, current["id"], current["razorpay_subscription_id"],
                      reason="re-subscribe after failed payment")

    gross = rz.gross_paise(plan["price_paise"])
    with get_db() as db:
        attempt_id = log_attempt(
            db, owner=owner, kind="subscription", plan_code=plan["code"],
            amount_paise=gross, ip=ip, ua=ua)

    try:
        sub = rz.create_subscription(
            plan_id=gateway_plan, total_count=120,
            notes={"owner": owner, "plan": plan["code"]})
    except rz.GatewayError as exc:
        with get_db() as db:
            update_attempt(db, attempt_id, status="gateway_error",
                           failure_step="create", failure_code=exc.code or "",
                           failure_reason=str(exc)[:400])
        raise CheckoutError(
            "We could not start the subscription. No money has been taken. "
            "Please try again in a moment.", 502, code="gateway_error") from exc

    sub_id_remote = sub["id"]
    with get_db() as db:
        row = db.execute(
            "INSERT INTO subscriptions (owner, plan_code, kind, status, "
            "razorpay_subscription_id, razorpay_mode, meta) "
            "VALUES (?,?,'subscription','created',?,?,?) RETURNING id",
            (owner, plan["code"], sub_id_remote, rz.mode(),
             json.dumps({"gross_paise": gross}))).fetchone()
        update_attempt(db, attempt_id, status="gateway_created",
                       gateway_ref=sub_id_remote, subscription_id=int(row["id"]))
        prefill = _prefill(db, owner)

    log.info("[billing] subscription checkout opened owner=%s sub=%s",
             owner, sub_id_remote)
    return {"flow": "subscription", "subscription_id": sub_id_remote,
            "key_id": rz.key_id(), "amount_paise": gross, "currency": "INR",
            "plan": plan["code"], "plan_name": plan["name"],
            "prefill": prefill, "attempt_id": attempt_id,
            # Above Rs 15,000 gross a card/UPI mandate triggers an RBI
            # additional-factor prompt EVERY month. eMandate (bank debit on the
            # NACH rail) has no per-debit AFA up to Rs 99,999, so the UI
            # recommends it for these amounts.
            "prefer_emandate": gross > 1_500_000}


def start_topup(owner: str, packs: int = 1, ip: str = "", ua: str = "") -> dict:
    """Buy additional minutes for the current cycle. Same Orders rail as Pilot
    — no mandate, no proration, no plan migration, instant effect.

    CURRENTLY UNREACHABLE BY DESIGN. Additional minutes are not sold (owner
    decision, 2026-08-01): every plan is seeded `topup_allowed=0`, so this
    refuses for everyone and the SPA renders no purchase control. The code is
    kept because it is built, tested and shares the Pilot rail — re-enabling it
    later is a data change (set topup_allowed + a rate on a plan row), not a
    migration.
    """
    from shared.db.database import get_db
    if not rz.configured():
        raise CheckoutError(
            "Online payments are not enabled yet. Please contact us.", 503,
            code="gateway_unconfigured")
    packs = max(1, min(20, int(packs or 1)))

    with get_db() as db:
        ent = db.execute(
            "SELECT e.plan_code, e.period_start, e.period_end, e.source_subscription_id, "
            "p.topup_allowed, p.topup_pack_minutes, p.topup_rate_paise, p.name "
            "FROM entitlements e JOIN plans p ON p.code=e.plan_code "
            "WHERE e.owner=?", (owner,)).fetchone()
        if not ent or not ent["topup_allowed"]:
            raise CheckoutError(
                "Additional minutes are not sold separately. Upgrade your plan "
                "for a larger monthly allowance.", 400, code="topup_not_allowed")
        _rate_limit(db, owner)
        minutes = int(ent["topup_pack_minutes"]) * packs
        base = int(ent["topup_rate_paise"]) * minutes
        gross = rz.gross_paise(base)
        attempt_id = log_attempt(db, owner=owner, kind="topup",
                                 plan_code=ent["plan_code"], intent="topup",
                                 amount_paise=gross, ip=ip, ua=ua)
        period_start, period_end = ent["period_start"], ent["period_end"]
        sub_id = ent["source_subscription_id"]

    receipt_ref = f"topup-{uuid.uuid4().hex[:16]}"
    try:
        order = rz.create_order(
            amount_paise=gross, receipt=receipt_ref,
            notes={"owner": owner, "kind": "topup", "minutes": str(minutes)})
    except rz.GatewayError as exc:
        with get_db() as db:
            update_attempt(db, attempt_id, status="gateway_error",
                           failure_step="create", failure_code=exc.code or "",
                           failure_reason=str(exc)[:400])
        raise CheckoutError(
            "We could not start the payment. No money has been taken.", 502,
            code="gateway_error") from exc

    with get_db() as db:
        row = db.execute(
            "INSERT INTO minute_topups (owner, subscription_id, razorpay_order_id, "
            "minutes, amount_paise, status, period_start, period_end) "
            "VALUES (?,?,?,?,?, 'created', ?, ?) RETURNING id",
            (owner, sub_id, order["id"], minutes, base,
             period_start, period_end)).fetchone()
        update_attempt(db, attempt_id, status="gateway_created",
                       gateway_ref=order["id"], topup_id=int(row["id"]))
        prefill = _prefill(db, owner)

    return {"flow": "order", "order_id": order["id"], "key_id": rz.key_id(),
            "amount_paise": gross, "currency": "INR", "purpose": "topup",
            "minutes": minutes, "packs": packs, "prefill": prefill,
            "attempt_id": attempt_id}


# ---------------------------------------------------------------------------
# verification — the optimistic fast path
# ---------------------------------------------------------------------------

def verify_checkout(owner: str, payload: dict) -> dict:
    """Confirm a completed checkout from the browser's callback.

    STRICT state transition, and it matters. Without the `status='created'`
    and `verified_at IS NULL` conditions a tenant can replay their own old
    signature forever: each replay resurrects a dead subscription row and
    re-grants a paid plan they no longer pay for. So: exactly one advance,
    from `created`, for a row this owner owns, ever.

    Anything else answers 200 `already_processed` and changes nothing — a
    duplicate callback is normal (the SPA retries), not an error.
    """
    from shared.db.database import get_db

    payment_id = str(payload.get("razorpay_payment_id") or "").strip()
    signature = str(payload.get("razorpay_signature") or "").strip()
    echoed_order = str(payload.get("razorpay_order_id") or "").strip()
    echoed_sub = str(payload.get("razorpay_subscription_id") or "").strip()
    if not payment_id or not signature:
        raise CheckoutError("Incomplete payment confirmation.", 400)

    with get_db() as db:
        # Look the row up by OUR stored id. The echoed ids are used only to
        # find the candidate row, never to verify against.
        row = None
        if echoed_order:
            row = db.execute(
                "SELECT * FROM subscriptions WHERE razorpay_order_id=? AND owner=?",
                (echoed_order, owner)).fetchone()
        if row is None and echoed_sub:
            row = db.execute(
                "SELECT * FROM subscriptions WHERE razorpay_subscription_id=? AND owner=?",
                (echoed_sub, owner)).fetchone()
        topup = None
        if row is None and echoed_order:
            topup = db.execute(
                "SELECT * FROM minute_topups WHERE razorpay_order_id=? AND owner=?",
                (echoed_order, owner)).fetchone()
        if row is None and topup is None:
            raise CheckoutError("We could not find that payment on your "
                                "account.", 404, code="unknown_payment")

    if topup is not None:
        return _verify_topup(owner, dict(topup), payment_id, signature)
    return _verify_subscription(owner, dict(row), payment_id, signature)


def _verify_subscription(owner: str, row: dict, payment_id: str,
                         signature: str) -> dict:
    from shared.db.database import get_db

    if row["status"] != "created" or row["verified_at"]:
        return {"status": "already_processed", "plan": row["plan_code"]}

    if row["kind"] == "one_time":
        ok = rz.verify_order_signature(
            order_id=row["razorpay_order_id"], payment_id=payment_id,
            signature=signature)
    else:
        ok = rz.verify_subscription_signature(
            subscription_id=row["razorpay_subscription_id"],
            payment_id=payment_id, signature=signature)
    if not ok:
        with get_db() as db:
            db.execute(
                "UPDATE payment_attempts SET status='failed', failure_step='verify', "
                "failure_reason='signature mismatch', updated_at=CURRENT_TIMESTAMP "
                "WHERE gateway_ref IN (?,?) AND status='gateway_created'",
                (row.get("razorpay_order_id") or "", row.get("razorpay_subscription_id") or ""))
        log.error("[billing] SIGNATURE MISMATCH owner=%s sub=%s payment=%s",
                  owner, row["id"], payment_id)
        raise CheckoutError("We could not verify that payment. If money has "
                            "left your account, use 'I paid but nothing "
                            "happened' and we will recheck.", 400,
                            code="signature_mismatch")

    now = now_utc()
    with get_db() as db:
        if row["kind"] == "one_time":
            plan = db.execute("SELECT duration_days, price_paise FROM plans WHERE code=?",
                              (row["plan_code"],)).fetchone()
            days = int((plan or {}).get("duration_days") or 7)
            end = now + dt.timedelta(days=days)
            updated = db.execute(
                "UPDATE subscriptions SET status='active', current_start=?, "
                "current_end=?, verified_at=CURRENT_TIMESTAMP, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='created' "
                "AND verified_at IS NULL RETURNING id",
                (_iso(now), _iso(end), row["id"])).fetchone()
        else:
            updated = db.execute(
                "UPDATE subscriptions SET status='authenticated', "
                "verified_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='created' AND verified_at IS NULL "
                "RETURNING id", (row["id"],)).fetchone()
        if updated is None:
            # The webhook beat us to it. That is the normal, good outcome.
            return {"status": "already_processed", "plan": row["plan_code"]}
        db.execute(
            "UPDATE payment_attempts SET status='authorized', "
            "updated_at=CURRENT_TIMESTAMP WHERE subscription_id=? "
            "AND status='gateway_created'", (row["id"],))

    ent = resolve_entitlements(owner, actor="checkout.verify")
    log.info("[billing] verified owner=%s plan=%s kind=%s", owner,
             row["plan_code"], row["kind"])
    return {"status": "activated", "plan": row["plan_code"],
            "entitlements": {"plan_code": ent.get("plan_code"),
                             "minutes_limit": ent.get("minutes_limit")}}


def _verify_topup(owner: str, row: dict, payment_id: str, signature: str) -> dict:
    from shared.db.database import get_db
    if row["status"] != "created":
        return {"status": "already_processed", "minutes": row["minutes"]}
    if not rz.verify_order_signature(order_id=row["razorpay_order_id"],
                                     payment_id=payment_id, signature=signature):
        raise CheckoutError("We could not verify that payment.", 400,
                            code="signature_mismatch")
    with get_db() as db:
        updated = db.execute(
            "UPDATE minute_topups SET status='active', updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status='created' RETURNING id", (row["id"],)).fetchone()
        if updated is None:
            return {"status": "already_processed", "minutes": row["minutes"]}
    resolve_entitlements(owner, actor="checkout.verify.topup")
    return {"status": "activated", "minutes": row["minutes"]}


# ---------------------------------------------------------------------------
# payments + receipts
# ---------------------------------------------------------------------------

def record_payment(db, *, owner: str, amount_paise: int, status: str,
                   purpose: str = "subscription", subscription_id: int | None = None,
                   razorpay_payment_id: str = "", razorpay_order_id: str = "",
                   razorpay_invoice_id: str = "", method: str = "",
                   period_start=None, period_end=None,
                   source: str = "razorpay", notes: str = "") -> dict | None:
    """Insert a payment and allocate its invoice number. Idempotent.

    The receipt number is allocated ONLY when the INSERT actually inserted
    (`ON CONFLICT ... DO NOTHING RETURNING id` returns no row on a duplicate).
    Allocating first and inserting second would burn a number from the GST
    series on every webhook retry, leaving gaps an auditor will ask about.
    """
    base = int(round(int(amount_paise) / (1 + receipts.GST_RATE)))
    tax = int(amount_paise) - base

    inserted = db.execute(
        "INSERT INTO payments (owner, subscription_id, source, purpose, "
        "razorpay_payment_id, razorpay_order_id, razorpay_invoice_id, "
        "amount_paise, base_paise, tax_paise, status, method, period_start, "
        "period_end, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT (razorpay_payment_id) DO NOTHING RETURNING id",
        (owner, subscription_id, source, purpose,
         razorpay_payment_id or None, razorpay_order_id or None,
         razorpay_invoice_id or None, int(amount_paise), base, tax, status,
         method or None, period_start, period_end, notes or None)).fetchone()
    if inserted is None:
        return None  # already recorded — a retry, not a second payment

    payment_id = int(inserted["id"])
    if status == "captured":
        snapshot = receipts.build_snapshot(owner, base)
        number = receipts.allocate_number(db, now_utc())
        db.execute(
            "UPDATE payments SET receipt_number=?, billing_snapshot=?, "
            "base_paise=?, tax_paise=? WHERE id=?",
            (number, json.dumps(snapshot, ensure_ascii=False),
             snapshot["tax"]["base_paise"], snapshot["tax"]["tax_paise"],
             payment_id))
        log.info("[billing] payment recorded owner=%s receipt=%s amount=%s",
                 owner, number, amount_paise)
    return {"id": payment_id}


# ---------------------------------------------------------------------------
# recovery: "I paid and nothing happened"
# ---------------------------------------------------------------------------

def reconcile_owner(owner: str, actor: str = "self-serve") -> dict:
    """Re-ask the gateway about every unresolved attempt for this tenant.

    This is the answer to the single most common billing support ticket. Money
    can leave a customer's account and not reach us for three ordinary
    reasons: the browser never called verify (tab closed at the wrong moment),
    the webhook was lost in transit, or the webhook was not configured yet.
    All three look identical to the customer, and all three are fixed by
    asking the gateway what it knows.

    Safe to run repeatedly: everything it does is idempotent.
    """
    from shared.db.database import get_db
    result = {"checked": 0, "recovered": 0, "still_open": 0, "notes": []}
    if not rz.configured():
        result["notes"].append("payment gateway not configured")
        return result

    with get_db() as db:
        attempts = [dict(r) for r in db.execute(
            "SELECT * FROM payment_attempts WHERE owner=? "
            "AND status IN ('gateway_created','authorized','initiated') "
            "ORDER BY id DESC LIMIT 25", (owner,)).fetchall()]

    for attempt in attempts:
        result["checked"] += 1
        ref = attempt.get("gateway_ref") or ""
        if not ref:
            continue
        try:
            if ref.startswith("order_"):
                recovered = _recover_order(owner, attempt, ref)
            elif ref.startswith("sub_"):
                recovered = _recover_subscription(owner, attempt, ref)
            else:
                continue
        except rz.GatewayError as exc:
            log.warning("[billing] recovery failed for %s (%s): %r", owner, ref, exc)
            result["notes"].append(f"{ref}: gateway error")
            continue
        if recovered:
            result["recovered"] += 1
        else:
            result["still_open"] += 1

    if result["recovered"]:
        resolve_entitlements(owner, actor=f"reconcile:{actor}")
    log.info("[billing] reconcile owner=%s -> %s", owner, result)
    return result


def _recover_order(owner: str, attempt: dict, order_id: str) -> bool:
    """Find a captured payment for an order we never completed locally."""
    from shared.db.database import get_db
    payments = rz.fetch_order_payments(order_id)
    captured = next((p for p in payments if p.get("status") == "captured"), None)
    if not captured:
        return False

    with get_db() as db:
        sub = db.execute("SELECT * FROM subscriptions WHERE razorpay_order_id=?",
                         (order_id,)).fetchone()
        topup = db.execute("SELECT * FROM minute_topups WHERE razorpay_order_id=?",
                           (order_id,)).fetchone()
        target = dict(sub) if sub else (dict(topup) if topup else None)
        if target is None:
            return False

        purpose = "pilot" if sub else "topup"
        payment = record_payment(
            db, owner=owner, amount_paise=int(captured.get("amount") or 0),
            status="captured", purpose=purpose,
            subscription_id=target["id"] if sub else target.get("subscription_id"),
            razorpay_payment_id=captured.get("id", ""),
            razorpay_order_id=order_id, method=captured.get("method", ""),
            notes="recovered by reconcile")

        if sub and target["status"] == "created":
            plan = db.execute("SELECT duration_days FROM plans WHERE code=?",
                              (target["plan_code"],)).fetchone()
            days = int((plan or {}).get("duration_days") or 7)
            now = now_utc()
            db.execute(
                "UPDATE subscriptions SET status='active', current_start=?, "
                "current_end=?, updated_at=CURRENT_TIMESTAMP WHERE id=? "
                "AND status='created'",
                (_iso(now), _iso(now + dt.timedelta(days=days)), target["id"]))
        elif topup and target["status"] == "created":
            db.execute("UPDATE minute_topups SET status='active', "
                       "updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='created'",
                       (target["id"],))

        update_attempt(db, attempt["id"], status="reconciled",
                       resolved_payment_id=(payment or {}).get("id"),
                       reconciled_at=_iso(now_utc()),
                       reconcile_note=f"recovered payment {captured.get('id')}")

    notify(owner, "Payment confirmed",
           "We found your payment and your plan is now active. Sorry for the "
           "delay.", severity="info", ref=f"recovered:{order_id}")
    log.info("[billing] RECOVERED order=%s owner=%s payment=%s",
             order_id, owner, captured.get("id"))
    return True


def _recover_subscription(owner: str, attempt: dict, sub_id: str) -> bool:
    """Re-sync a recurring subscription whose webhooks we missed."""
    from shared.db.database import get_db
    remote = rz.fetch_subscription(sub_id)
    status = remote.get("status") or ""
    if status in ("created",):
        return False
    with get_db() as db:
        db.execute(
            "UPDATE subscriptions SET status=?, current_start=?, current_end=?, "
            "charge_at=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE razorpay_subscription_id=?",
            (status, _epoch(remote.get("current_start")),
             _epoch(remote.get("current_end")), _epoch(remote.get("charge_at")),
             sub_id))
        update_attempt(db, attempt["id"], status="reconciled",
                       reconciled_at=_iso(now_utc()),
                       reconcile_note=f"gateway status {status}")
    resolve_entitlements(owner, actor="reconcile")
    return status in ("active", "authenticated")


def _epoch(value) -> str | None:
    """Razorpay sends unix seconds; our columns are TIMESTAMPTZ."""
    if not value:
        return None
    try:
        return _iso(dt.datetime.fromtimestamp(int(value), dt.timezone.utc))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# cancellation
# ---------------------------------------------------------------------------

def _force_cancel(owner: str, local_id: int, remote_id: str | None,
                  reason: str = "") -> None:
    """Cancel immediately at the gateway and locally. Used by the §6.7 fix.

    Gateway failure is logged, not raised: the local row must still leave the
    uniqueness index or the tenant stays locked out of checkout forever, which
    is a worse outcome than a stale remote subscription an operator can clean
    up from the Razorpay dashboard.
    """
    from shared.db.database import get_db
    if remote_id and rz.configured():
        try:
            rz.cancel_subscription(remote_id, at_cycle_end=False)
        except rz.GatewayError as exc:
            log.error("[billing] gateway cancel failed for %s (%s) — freeing the "
                      "local row anyway: %r", owner, remote_id, exc)
    with get_db() as db:
        db.execute(
            "UPDATE subscriptions SET status='cancelled', "
            "meta=COALESCE(meta,'{}'), updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (local_id,))
    log.info("[billing] subscription %s cancelled for %s (%s)",
             local_id, owner, reason)


def cancel_at_cycle_end(owner: str) -> dict:
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM subscriptions WHERE owner=? AND kind='subscription' "
            "AND scheduled=0 AND status IN ('active','authenticated','pending')",
            (owner,)).fetchone()
    if row is None:
        raise CheckoutError("You do not have an active subscription to cancel.",
                            404, code="no_subscription")
    row = dict(row)
    if rz.configured() and row["razorpay_subscription_id"]:
        try:
            rz.cancel_subscription(row["razorpay_subscription_id"], at_cycle_end=True)
        except rz.GatewayError as exc:
            # "Already in its final cycle" is a success for our purposes.
            if "final" not in str(exc).lower():
                raise CheckoutError(
                    "We could not cancel with the payment provider. Please "
                    "try again or contact support.", 502) from exc
    with get_db() as db:
        db.execute("UPDATE subscriptions SET cancel_at_cycle_end=1, "
                   "updated_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
    end = as_dt(row["current_end"])
    return {"status": "cancelling", "active_until": _iso(end)}
