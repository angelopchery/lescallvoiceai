"""LesCall public integration API — the customer-facing HTTP surface.

Layout:
    errors.py      the single error envelope + the stable code vocabulary
    policy.py      per-tenant access policy (operator-owned IP allowlist + limits)
    ratelimit.py   fixed-window counters, in Postgres so restarts don't reset them
    keys.py        key minting, hashing, and the request-admission chain
    idempotency.py replay protection for writes
    serialize.py   DB rows -> the documented JSON shapes

The HTTP routes themselves live in dashboard/public_api.py (mounted at /v1);
the tenant's key-management endpoints in dashboard/api_keys_api.py; the
operator's controls in dashboard/admin_api_console.py.
"""
