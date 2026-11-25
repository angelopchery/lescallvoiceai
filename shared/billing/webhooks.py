"""Gateway webhook ingestion. The authoritative source of billing truth.

Four rules, each earned from a specific way this goes wrong:

  1. **Dedupe on the gateway's event id, before doing any work.**
     `INSERT ... ON CONFLICT (event_id) DO NOTHING` — a conflict means we have
     seen it, so answer 200 and stop. Razorpay retries aggressively.

  2. **Never raise after the event is marked seen.** Razorpay dedupes its own
     retries, so an exception thrown mid-handler strands a real payment with no
     second delivery ever coming. Handlers upsert; on an impossible conflict
     they record `error`, leave `processed_at` NULL for the reprocess sweep,
     and alarm.

  3. **Trust only the local `subscriptions` row, matched on the gateway id.**
     `notes` is display data the caller controls — it must never be the thing
     that decides which tenant gets a plan. An unknown id is recorded and
     alarmed; it never creates an entitlement.

  4. **Verify the amount against the plan.** A captured payment whose amount
     does not match what the plan costs is quarantined rather than granted.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os

from shared.billing import razorpay_client as rz
from shared.billing import receipts
from shared.billing.entitlements import (
    _notify_critical, as_dt, notify, now_utc, resolve_entitlements,
)
from shared.billing.service import _epoch, record_payment, update_attempt

log = logging.getLogger("billing.webhooks")

GRACE_HOURS = int(os.environ.get("BILLING_GRACE_HOURS", "72"))

# Events we subscribe to. Anything else is acknowledged and ignored, which is
# deliberate: an unhandled-but-subscribed event would otherwise sit with
# processed_at NULL forever and trip the stuck-event alarm every 15 minutes.
HANDLED = {
    "subscription.authenticated", "subscription.activated",
    "subscription.charged", "subscription.pending", "subscription.halted",
    "subscription.cancelled", "subscription.completed", "subscription.paused",
    "subscription.resumed", "subscription.updated",
    "payment.captured", "payment.failed", "order.paid",
    "refund.processed", "refund.created", "payment.refunded",
    "invoice.paid",
}


def _iso(t: dt.datetime | None) -> str | None:
    return t.astimezone(dt.timezone.utc).isoformat() if t else None


def ingest(event_id: str, event_type: str, raw_body: bytes,
           payload: dict) -> dict:
    """Record + dispatch one webhook. Never raises."""
    from shared.db.database import get_db

    with get_db() as db:
        inserted = db.execute(
            "INSERT INTO billing_events (event_id, event_type, payload) "
            "VALUES (?,?,?) ON CONFLICT (event_id) DO NOTHING RETURNING id",
            (event_id, event_type,
             raw_body.decode("utf-8", "replace")[:200_000])).fetchone()
    if inserted is None:
        log.info("[billing] duplicate webhook %s (%s) — already handled",
                 event_id, event_type)
        return {"status": "duplicate"}

    row_id = int(inserted["id"])
    if event_type not in HANDLED:
        _mark(row_id, processed=True, note="unsubscribed event type")
        return {"status": "ignored"}

    try:
        outcome = dispatch(event_type, payload)
        _mark(row_id, processed=True, owner=outcome.get("owner"))
        return {"status": "processed", **outcome}
    except Exception as exc:
        # Rule 2: do NOT re-raise. The event stays unprocessed for the
        # reprocess sweep, which can replay it because the payload is stored
        # verbatim above.
        log.exception("[billing] webhook handler failed: %s (%s)", event_id, event_type)
        _mark(row_id, processed=False, error=repr(exc)[:500])
        _notify_critical("", "billing webhook failed",
                         f"{event_type} {event_id}: {exc!r}")
        return {"status": "deferred", "error": repr(exc)[:200]}


def _mark(row_id: int, *, processed: bool, error: str = "",
          owner: str = "", note: str = "") -> None:
    from shared.db.database import get_db
    try:
        with get_db() as db:
            db.execute(
                "UPDATE billing_events SET processed_at=?, error=?, owner=?, "
                "attempts=attempts+1 WHERE id=?",
                (_iso(now_utc()) if processed else None,
                 (error or note) or None, owner or None, row_id))
    except Exception as exc:
        log.error("[billing] could not mark event %s: %r", row_id, exc)


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def dispatch(event_type: str, payload: dict) -> dict:
    entity = payload.get("payload") or {}

    def ent(name: str) -> dict:
        return ((entity.get(name) or {}).get("entity")) or {}

    if event_type.startswith("subscription."):
        return _on_subscription(event_type, ent("subscription"), ent("payment"))
    if event_type in ("order.paid",):
        return _on_order_paid(ent("order"), ent("payment"))
    if event_type == "payment.captured":
        return _on_payment_captured(ent("payment"))
    if event_type == "payment.failed":
        return _on_payment_failed(ent("payment"))
    if event_type in ("refund.processed", "refund.created", "payment.refunded"):
        return _on_refund(ent("refund"), ent("payment"))
    if event_type == "invoice.paid":
        # Explicit no-op ack. Subscribed because Razorpay's dashboard groups it
        # with the subscription events; the money is already handled by
        # subscription.charged.
        return {"handled": "invoice.paid noop"}
    return {"handled": "unrouted"}


def _local_subscription(db, *, subscription_id: str = "", order_id: str = ""):
    if subscription_id:
        return db.execute(
            "SELECT * FROM subscriptions WHERE razorpay_subscription_id=?",
            (subscription_id,)).fetchone()
    if order_id:
        return db.execute(
            "SELECT * FROM subscriptions WHERE razorpay_order_id=?",
            (order_id,)).fetchone()
    return None


def _unknown(kind: str, ref: str) -> dict:
    """Rule 3. Record loudly, grant nothing."""
    log.error("[billing] webhook references UNKNOWN %s %r — no entitlement "
              "granted", kind, ref)
    _notify_critical("", "unknown subscription in webhook",
                     f"{kind} {ref} has no local row. Money may have been "
                     f"taken with no account to attribute it to.")
    return {"handled": "unknown_reference", "ref": ref}


# ---------------------------------------------------------------------------
# recurring
# ---------------------------------------------------------------------------

def _on_subscription(event_type: str, sub: dict, payment: dict) -> dict:
    from shared.db.database import get_db
    remote_id = sub.get("id") or ""
    if not remote_id:
        return {"handled": "no subscription id"}

    with get_db() as db:
        row = _local_subscription(db, subscription_id=remote_id)
        if row is None:
            return _unknown("subscription", remote_id)
        row = dict(row)
        owner = row["owner"]
        action = event_type.split(".", 1)[1]

        status_map = {
            "authenticated": "authenticated", "activated": "active",
            "charged": "active", "pending": "pending", "halted": "halted",
            "cancelled": "cancelled", "completed": "completed",
            "paused": "paused", "resumed": "active", "updated": None,
        }
        new_status = status_map.get(action)

        # Every field is taken from the payload rather than incremented, so
        # out-of-order delivery (charged before activated) converges correctly.
        fields = {
            "current_start": _epoch(sub.get("current_start")),
            "current_end": _epoch(sub.get("current_end")),
            "charge_at": _epoch(sub.get("charge_at")),
        }
        if new_status:
            fields["status"] = new_status
        if action == "authenticated":
            start = _epoch(sub.get("start_at"))
            start_dt = as_dt(start)
            fields["scheduled"] = 1 if (start_dt and start_dt > now_utc()) else 0
        if action == "halted":
            fields["grace_until"] = _iso(now_utc() + dt.timedelta(hours=GRACE_HOURS))
        if action in ("activated", "resumed", "charged"):
            fields["grace_until"] = None
            fields["scheduled"] = 0

        sets = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE subscriptions SET {sets}, updated_at=CURRENT_TIMESTAMP "
                   f"WHERE id=?", (*fields.values(), row["id"]))

        if action == "charged" and payment.get("id"):
            _record_subscription_charge(db, row, payment)

    resolve_entitlements(owner, actor=f"webhook:{event_type}")
    _notify_status(owner, action, row)
    return {"handled": event_type, "owner": owner}


def _record_subscription_charge(db, row: dict, payment: dict) -> None:
    """Money for a renewal. Amount-checked before it is trusted (rule 4)."""
    amount = int(payment.get("amount") or 0)
    plan = db.execute("SELECT price_paise, name FROM plans WHERE code=?",
                      (row["plan_code"],)).fetchone()
    expected = rz.gross_paise(int((plan or {}).get("price_paise") or 0))
    # A rupee of slack absorbs the gateway's own rounding without accepting a
    # materially different amount.
    if expected and abs(amount - expected) > 100:
        log.error("[billing] AMOUNT MISMATCH sub=%s owner=%s charged=%s expected=%s",
                  row["id"], row["owner"], amount, expected)
        _notify_critical(
            row["owner"], "payment amount mismatch",
            f"Subscription {row['razorpay_subscription_id']} charged {amount} "
            f"paise but {row['plan_code']} costs {expected}. Quarantined — "
            f"no entitlement granted from this payment.")
        record_payment(
            db, owner=row["owner"], amount_paise=amount, status="failed",
            purpose="subscription", subscription_id=row["id"],
            razorpay_payment_id=payment.get("id", ""),
            method=payment.get("method", ""),
            notes=f"QUARANTINED: amount mismatch, expected {expected}")
        return

    record_payment(
        db, owner=row["owner"], amount_paise=amount, status="captured",
        purpose="subscription", subscription_id=row["id"],
        razorpay_payment_id=payment.get("id", ""),
        razorpay_invoice_id=payment.get("invoice_id", "") or "",
        method=payment.get("method", ""),
        period_start=row.get("current_start"), period_end=row.get("current_end"))
    db.execute(
        "UPDATE payment_attempts SET status='captured', updated_at=CURRENT_TIMESTAMP "
        "WHERE subscription_id=? AND status IN ('gateway_created','authorized')",
        (row["id"],))


def _notify_status(owner: str, action: str, row: dict) -> None:
    plan = row.get("plan_code", "your plan")
    if action == "charged":
        notify(owner, "Payment received",
               f"Your {plan} subscription has renewed. The invoice is in "
               f"Billing → Payment history.", ref=f"charged:{row['id']}:{action}")
    elif action == "pending":
        notify(owner, "Payment retrying",
               "We could not take this month's payment. Your bank will be "
               "retried automatically — update your payment method if it keeps "
               "failing.", severity="warning", ref=f"pending:{row['id']}")
    elif action == "halted":
        notify(owner, "Payment failed — action needed",
               f"We could not collect payment for {plan}. Your plan stays "
               f"active for {GRACE_HOURS} hours; update your payment method to "
               f"avoid interruption.", severity="critical",
               ref=f"halted:{row['id']}")
    elif action == "cancelled":
        notify(owner, "Subscription cancelled",
               "Your plan stays active until the end of the current period.",
               ref=f"cancelled:{row['id']}")


# ---------------------------------------------------------------------------
# one-time (Pilot + top-ups)
# ---------------------------------------------------------------------------

def _on_order_paid(order: dict, payment: dict) -> dict:
    return _settle_order(order.get("id") or payment.get("order_id") or "", payment)


def _on_payment_captured(payment: dict) -> dict:
    order_id = payment.get("order_id") or ""
    if not order_id:
        # A subscription charge arrives as subscription.charged; a bare
        # payment.captured with no order is not ours to act on.
        return {"handled": "payment.captured without order"}
    return _settle_order(order_id, payment)


def _settle_order(order_id: str, payment: dict) -> dict:
    """Open a Pilot window or activate a top-up. Idempotent.

    This is also the path that saves a customer who closed the tab straight
    after paying: `verify` never fired, but the window opens here anyway.
    """
    from shared.db.database import get_db
    if not order_id:
        return {"handled": "no order id"}
    amount = int(payment.get("amount") or 0)

    with get_db() as db:
        sub = db.execute("SELECT * FROM subscriptions WHERE razorpay_order_id=?",
                         (order_id,)).fetchone()
        topup = db.execute("SELECT * FROM minute_topups WHERE razorpay_order_id=?",
                           (order_id,)).fetchone()
        if sub is None and topup is None:
            return _unknown("order", order_id)

        if sub is not None:
            row = dict(sub)
            owner = row["owner"]
            plan = db.execute(
                "SELECT price_paise, duration_days FROM plans WHERE code=?",
                (row["plan_code"],)).fetchone()
            expected = rz.gross_paise(int((plan or {}).get("price_paise") or 0))
            if expected and abs(amount - expected) > 100:
                _notify_critical(owner, "payment amount mismatch",
                                 f"Order {order_id} paid {amount} paise, "
                                 f"expected {expected}. Quarantined.")
                record_payment(db, owner=owner, amount_paise=amount,
                               status="failed", purpose="pilot",
                               subscription_id=row["id"],
                               razorpay_payment_id=payment.get("id", ""),
                               razorpay_order_id=order_id,
                               notes=f"QUARANTINED: expected {expected}")
                return {"handled": "amount_mismatch", "owner": owner}

            days = int((plan or {}).get("duration_days") or 7)
            now = now_utc()
            # Only opens a window that has not been opened; a duplicate webhook
            # cannot extend an existing trial.
            db.execute(
                "UPDATE subscriptions SET status='active', current_start=?, "
                "current_end=?, updated_at=CURRENT_TIMESTAMP WHERE id=? "
                "AND status='created'",
                (_iso(now), _iso(now + dt.timedelta(days=days)), row["id"]))
            created = record_payment(
                db, owner=owner, amount_paise=amount, status="captured",
                purpose="pilot", subscription_id=row["id"],
                razorpay_payment_id=payment.get("id", ""),
                razorpay_order_id=order_id, method=payment.get("method", ""),
                period_start=_iso(now),
                period_end=_iso(now + dt.timedelta(days=days)))
            _bind_pilot_contact(db, owner, payment.get("contact") or "")
            db.execute(
                "UPDATE payment_attempts SET status='captured', "
                "resolved_payment_id=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE gateway_ref=? AND status IN ('gateway_created','authorized')",
                ((created or {}).get("id"), order_id))
        else:
            row = dict(topup)
            owner = row["owner"]
            db.execute("UPDATE minute_topups SET status='active', "
                       "updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='created'",
                       (row["id"],))
            created = record_payment(
                db, owner=owner, amount_paise=amount, status="captured",
                purpose="topup", subscription_id=row.get("subscription_id"),
                razorpay_payment_id=payment.get("id", ""),
                razorpay_order_id=order_id, method=payment.get("method", ""),
                period_start=row.get("period_start"),
                period_end=row.get("period_end"))
            if created:
                db.execute("UPDATE minute_topups SET payment_id=? WHERE id=?",
                           (created["id"], row["id"]))
            db.execute(
                "UPDATE payment_attempts SET status='captured', "
                "resolved_payment_id=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE gateway_ref=? AND status IN ('gateway_created','authorized')",
                ((created or {}).get("id"), order_id))

    resolve_entitlements(owner, actor="webhook:order.paid")
    if sub is not None:
        notify(owner, "Pilot started",
               "Your 7-day Pilot is live — 500 minutes to try LesCall with "
               "your own customers.", ref=f"pilot-start:{order_id}")
    else:
        notify(owner, "Minutes added",
               f"{row['minutes']} minutes have been added to this billing "
               f"period.", ref=f"topup:{order_id}")
    return {"handled": "order.paid", "owner": owner}


def _bind_pilot_contact(db, owner: str, contact: str) -> None:
    """Bind the trial claim to the payer's verified phone number as well as
    their email. An email is free to mint; a payment-verified number is not,
    so it is the stronger half of the once-ever rule."""
    contact = (contact or "").strip()
    if not contact:
        return
    try:
        db.execute("UPDATE pilot_claims SET razorpay_contact=? WHERE owner=? "
                   "AND razorpay_contact IS NULL", (contact, owner))
    except Exception as exc:
        # The partial unique index fires when this number already claimed a
        # trial on another account. Not fatal — the trial is already paid for —
        # but worth knowing about.
        log.warning("[billing] pilot contact bind refused for %s: %r", owner, exc)


# ---------------------------------------------------------------------------
# failures + refunds
# ---------------------------------------------------------------------------

def _on_payment_failed(payment: dict) -> dict:
    """A declined payment. No plan change: the subscription's own
    pending/halted events drive entitlement, not this."""
    from shared.db.database import get_db
    order_id = payment.get("order_id") or ""
    error = (payment.get("error_description")
             or payment.get("error_reason") or "payment failed")
    code = payment.get("error_code") or ""
    owner = ""

    with get_db() as db:
        row = None
        if order_id:
            row = _local_subscription(db, order_id=order_id)
        if row is None and payment.get("subscription_id"):
            row = _local_subscription(db, subscription_id=payment["subscription_id"])
        if row is not None:
            owner = row["owner"]
            record_payment(
                db, owner=owner, amount_paise=int(payment.get("amount") or 0),
                status="failed", purpose="subscription",
                subscription_id=row["id"],
                razorpay_payment_id=payment.get("id", ""),
                razorpay_order_id=order_id, method=payment.get("method", ""),
                notes=f"{code}: {error}"[:400])
        db.execute(
            "UPDATE payment_attempts SET status='failed', failure_step='checkout', "
            "failure_code=?, failure_reason=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE gateway_ref IN (?,?) AND status IN ('gateway_created','authorized')",
            (code[:80] or None, str(error)[:400], order_id,
             payment.get("subscription_id") or ""))

    if owner:
        notify(owner, "Payment did not go through",
               f"{error} No money has been taken. You can try again from the "
               f"Billing page.", severity="warning",
               ref=f"failed:{payment.get('id')}")
    log.info("[billing] payment failed order=%s code=%s: %s", order_id, code, error)
    return {"handled": "payment.failed", "owner": owner}


def _on_refund(refund: dict, payment: dict) -> dict:
    """A refund. Flips the payment, recomputes entitlements, and flags that a
    GST credit note is due — an issued tax invoice cannot simply be deleted."""
    from shared.db.database import get_db
    payment_id = refund.get("payment_id") or payment.get("id") or ""
    amount = int(refund.get("amount") or payment.get("amount_refunded") or 0)
    if not payment_id:
        return {"handled": "refund without payment id"}

    with get_db() as db:
        row = db.execute("SELECT * FROM payments WHERE razorpay_payment_id=?",
                         (payment_id,)).fetchone()
        if row is None:
            return _unknown("payment (refund)", payment_id)
        row = dict(row)
        owner = row["owner"]
        full = amount >= int(row["amount_paise"] or 0)
        credit_number = row.get("credit_note_number")
        if full and not credit_number:
            credit_number = receipts.allocate_number(db, now_utc(), credit_note=True)
        db.execute(
            "UPDATE payments SET status=?, refunded_paise=?, refunded_at=?, "
            "credit_note_number=COALESCE(credit_note_number, ?) WHERE id=?",
            ("refunded" if full else row["status"], amount, _iso(now_utc()),
             credit_number, row["id"]))
        # A refunded top-up gives its minutes back.
        db.execute("UPDATE minute_topups SET status='refunded', "
                   "updated_at=CURRENT_TIMESTAMP WHERE payment_id=?", (row["id"],))

    resolve_entitlements(owner, actor="webhook:refund")
    notify(owner, "Refund processed",
           f"A refund of Rs {amount / 100:,.2f} has been issued. A credit note "
           f"is available in Payment history.", ref=f"refund:{payment_id}")
    log.info("[billing] refund %s for payment %s (credit note %s)",
             amount, payment_id, credit_number)
    return {"handled": "refund", "owner": owner}
