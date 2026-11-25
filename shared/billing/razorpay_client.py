"""Razorpay REST wrapper + signature verification.

Designed to be SAFE WHEN UNCONFIGURED. There is no Razorpay account yet, so
every entry point either works or fails loudly with `GatewayUnavailable` — none
of them ever guesses, retries blindly, or fabricates a payment. With blank
credentials the rest of the platform (catalogue, entitlements, quotas,
receipts, admin plane) is fully functional; only checkout is unavailable, and
the API says exactly that.

    THE SIGNATURE TRAP
    ------------------
    Razorpay uses the SAME HMAC construction with the OPPOSITE field order for
    its two flows, and this system runs both:

        one-time Order:   HMAC(order_id + "|" + payment_id)   <- order first
        Subscription:     HMAC(payment_id + "|" + subscription_id)
                                                              <- payment first

    Getting them the wrong way round produces a verifier that rejects every
    genuine payment (annoying) or, if you also "fix" it by comparing against
    the id the client echoed back, one that accepts forged ones (fatal). Both
    verifiers below compare against the id STORED LOCALLY, never the echoed
    one, and there is a unit test asserting each signature FAILS the other
    verifier.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

log = logging.getLogger("billing.razorpay")

API_BASE = "https://api.razorpay.com/v1"
# A payment call must not be able to wedge a request handler. 10s total.
TIMEOUT_S = float(os.environ.get("RAZORPAY_TIMEOUT_S", "10"))
GST_RATE = 0.18


class GatewayError(RuntimeError):
    """The gateway was reached and refused, or answered unusably."""

    def __init__(self, message: str, status: int = 0, code: str = "",
                 payload: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = payload or {}


class GatewayUnavailable(GatewayError):
    """No credentials configured. Distinct from GatewayError so the API can
    answer 503 "payments are not set up yet" instead of "your payment
    failed" — those are very different messages to show a customer."""


def mode() -> str:
    return (os.environ.get("RAZORPAY_MODE") or "test").strip().lower()


def key_id() -> str:
    return (os.environ.get("RAZORPAY_KEY_ID") or "").strip()


def _key_secret() -> str:
    return (os.environ.get("RAZORPAY_KEY_SECRET") or "").strip()


def _webhook_secrets() -> list[str]:
    """Current secret first, then the previous one if a rotation is in flight.

    Rotating a webhook secret is not atomic: for a few minutes Razorpay may
    still be retrying events signed with the old one. Accepting both during
    the window is the difference between a clean rotation and a handful of
    lost payment events.
    """
    out = []
    for var in ("RAZORPAY_WEBHOOK_SECRET", "RAZORPAY_WEBHOOK_SECRET_PREVIOUS"):
        val = (os.environ.get(var) or "").strip()
        if val:
            out.append(val)
    return out


def configured() -> bool:
    """Can we create charges? (Checkout needs this; nothing else does.)"""
    return bool(key_id() and _key_secret())


def webhook_configured() -> bool:
    return bool(_webhook_secrets())


def status() -> dict:
    """For /api/billing/summary and the admin health page."""
    return {
        "configured": configured(),
        "webhook_configured": webhook_configured(),
        "mode": mode(),
        "key_id": key_id() if mode() == "test" else _masked(key_id()),
    }


def _masked(value: str) -> str:
    return f"{value[:8]}…{value[-4:]}" if len(value) > 14 else ("set" if value else "")


def gross_paise(base_paise: int) -> int:
    """What the gateway is actually charged: base + 18% GST.

    Prices in `plans` are GST-EXCLUSIVE. Rounded to the nearest paisa, and
    computed in integers throughout — a float rupee amount that is off by one
    paisa fails signature-independent amount verification in the webhook.
    """
    return int(round(int(base_paise) * (1 + GST_RATE)))


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------

def _request(method: str, path: str, json_body: dict | None = None,
             params: dict | None = None) -> dict:
    if not configured():
        raise GatewayUnavailable(
            "Razorpay is not configured (RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET "
            "are unset in /etc/lescall/billing.env).")
    import httpx

    url = f"{API_BASE}{path}"
    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            resp = client.request(method, url, json=json_body, params=params,
                                  auth=(key_id(), _key_secret()))
    except Exception as exc:
        raise GatewayError(f"razorpay unreachable: {exc!r}") from exc
    dt_ms = (time.monotonic() - t0) * 1000

    try:
        payload = resp.json()
    except Exception:
        payload = {"raw": resp.text[:500]}

    if resp.status_code >= 400:
        err = (payload or {}).get("error") or {}
        log.error("[razorpay] %s %s -> %s in %.0fms: %s",
                  method, path, resp.status_code, dt_ms, err)
        raise GatewayError(
            err.get("description") or f"razorpay error {resp.status_code}",
            status=resp.status_code, code=err.get("code") or "", payload=payload)
    log.info("[razorpay] %s %s -> %s in %.0fms", method, path,
             resp.status_code, dt_ms)
    return payload if isinstance(payload, dict) else {"data": payload}


# ---------------------------------------------------------------------------
# recurring (Basic / Growth)
# ---------------------------------------------------------------------------

def create_plan(*, name: str, amount_paise: int, period: str = "monthly",
                interval: int = 1, notes: dict | None = None) -> dict:
    """Create a gateway Plan.

    NOTE FOR THE OPERATOR: Razorpay Plans are IMMUTABLE FOREVER. A price change
    means a brand-new plan id and a migration of every existing subscriber.
    plans_bootstrap.py refuses to overwrite an id that is already stored.
    """
    return _request("POST", "/plans", {
        "period": period, "interval": interval,
        "item": {"name": name, "amount": int(amount_paise), "currency": "INR"},
        "notes": notes or {},
    })


def create_subscription(*, plan_id: str, total_count: int = 120,
                        customer_notify: bool = True,
                        start_at: int | None = None,
                        notes: dict | None = None) -> dict:
    body = {"plan_id": plan_id, "total_count": int(total_count),
            "customer_notify": 1 if customer_notify else 0,
            "notes": notes or {}}
    if start_at:
        body["start_at"] = int(start_at)
    return _request("POST", "/subscriptions", body)


def fetch_subscription(subscription_id: str) -> dict:
    return _request("GET", f"/subscriptions/{subscription_id}")


def cancel_subscription(subscription_id: str, at_cycle_end: bool = False) -> dict:
    return _request("POST", f"/subscriptions/{subscription_id}/cancel",
                    {"cancel_at_cycle_end": 1 if at_cycle_end else 0})


# ---------------------------------------------------------------------------
# one-time (Pilot + minute top-ups)
# ---------------------------------------------------------------------------

def create_order(*, amount_paise: int, receipt: str,
                 notes: dict | None = None) -> dict:
    """A one-time charge. No mandate, therefore no e-mandate authorization, no
    RBI pre-debit notification, no AFA exposure, and every payment method
    works (card / UPI / netbanking / wallet)."""
    return _request("POST", "/orders", {
        "amount": int(amount_paise), "currency": "INR",
        "receipt": receipt[:40], "notes": notes or {},
    })


def fetch_order(order_id: str) -> dict:
    return _request("GET", f"/orders/{order_id}")


def fetch_order_payments(order_id: str) -> list[dict]:
    """Payments against an order — the recovery path for "I paid but nothing
    happened": if the webhook was lost AND the browser never called verify,
    this is how the money is found again."""
    out = _request("GET", f"/orders/{order_id}/payments")
    items = out.get("items")
    return items if isinstance(items, list) else []


def fetch_payment(payment_id: str) -> dict:
    return _request("GET", f"/payments/{payment_id}")


def fetch_refunds(payment_id: str) -> list[dict]:
    out = _request("GET", f"/payments/{payment_id}/refunds")
    items = out.get("items")
    return items if isinstance(items, list) else []


# ---------------------------------------------------------------------------
# signatures — READ THE MODULE DOCSTRING BEFORE TOUCHING
# ---------------------------------------------------------------------------

def _hmac_hex(message: str, secret: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def verify_order_signature(*, order_id: str, payment_id: str,
                           signature: str) -> bool:
    """One-time Order flow. ORDER ID FIRST.

    `order_id` MUST be the id read from our own `subscriptions` /
    `minute_topups` row, never the one the browser echoed back — otherwise an
    attacker signs a pair of ids they chose and it verifies perfectly.
    """
    secret = _key_secret()
    if not (secret and order_id and payment_id and signature):
        return False
    expected = _hmac_hex(f"{order_id}|{payment_id}", secret)
    return hmac.compare_digest(expected, signature)


def verify_subscription_signature(*, subscription_id: str, payment_id: str,
                                  signature: str) -> bool:
    """Recurring Subscription flow. PAYMENT ID FIRST — the opposite order to
    `verify_order_signature`. Same locally-stored-id rule."""
    secret = _key_secret()
    if not (secret and subscription_id and payment_id and signature):
        return False
    expected = _hmac_hex(f"{payment_id}|{subscription_id}", secret)
    return hmac.compare_digest(expected, signature)


def verify_webhook_signature(raw_body: bytes, header_signature: str) -> bool:
    """HMAC over the EXACT bytes received.

    Re-serialising the parsed JSON and signing that would not match — key order
    and whitespace are part of the signed message. The handler must therefore
    keep the raw body around, which is why it stream-reads rather than calling
    `await request.json()` first.
    """
    if not header_signature:
        return False
    for secret in _webhook_secrets():
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, header_signature):
            return True
    return False
