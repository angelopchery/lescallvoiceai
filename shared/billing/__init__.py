"""Billing: plans, entitlements, gateway, receipts, enforcement helpers.

deploy/PLAN-razorpay-subscriptions.md.

Import rules for callers in the live call path (the bridge, the dial
endpoints): every public function here is safe to call and NEVER raises for a
reason the caller did not ask about. Enforcement decisions are returned as
values (`Decision`), not exceptions, except for the deliberate
`BillingRefusal` HTTP error used by the API layer.

Two switches gate everything:
  * `enforcement_enabled()` — a DB setting, not env, because the inbound gate
    runs in the runtime container which never receives api-only env.
  * `gateway_configured()` — whether payment credentials exist at all. With no
    Razorpay account the catalogue, entitlements, receipts, quotas and the
    admin plane all work; only checkout is unavailable, and it says so.
"""

from __future__ import annotations

__all__ = [
    "BillingRefusal",
    "Decision",
    "available",
]


def available() -> bool:
    """False on SQLite, where the versioned billing tables do not exist.

    Same doctrine as the intelligence layer: the dev fallback database never
    gets migration-managed tables, so the feature disables itself rather than
    exploding on every call.
    """
    try:
        from shared.db.database import db_url
        return db_url().startswith(("postgres://", "postgresql://"))
    except Exception:
        return False


from shared.billing.errors import BillingRefusal, Decision  # noqa: E402
