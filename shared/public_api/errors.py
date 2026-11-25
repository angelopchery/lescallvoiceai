"""The one error shape the public API ever returns.

Every integration failure a customer's CRM will see is this envelope:

    {"error": {"code": "ip_not_allowed",
               "message": "203.0.113.9 is not in the allowlist for this account.",
               "request_id": "req_9f2c…"}}

`code` is the machine-readable contract (stable forever); `message` is for the
human reading their integration log. Both matter: a CRM that can only string-
match "Forbidden" cannot tell a revoked key from a blocked IP, and the support
ticket that follows costs more than the field.
"""

from __future__ import annotations

import secrets


class ApiError(Exception):
    """An HTTP failure with a stable code. Raised anywhere under /v1 and
    rendered by the router's exception handler."""

    def __init__(self, status: int, code: str, message: str,
                 headers: dict[str, str] | None = None,
                 details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}
        self.details = details or {}

    def body(self, request_id: str = "") -> dict:
        err: dict = {"code": self.code, "message": self.message}
        if self.details:
            err["details"] = self.details
        if request_id:
            err["request_id"] = request_id
        return {"error": err}


def new_request_id() -> str:
    return "req_" + secrets.token_hex(8)


# Codes used across the surface. Kept in one place so a new call site cannot
# invent a near-duplicate ("bad_key" vs "invalid_key") that clients then have
# to match on both spellings of.
UNAUTHENTICATED = "unauthenticated"
INVALID_KEY = "invalid_key"
KEY_REVOKED = "key_revoked"
KEY_EXPIRED = "key_expired"
API_DISABLED = "api_disabled"
IP_NOT_ALLOWED = "ip_not_allowed"
INSUFFICIENT_SCOPE = "insufficient_scope"
RATE_LIMITED = "rate_limited"
NOT_FOUND = "not_found"
INVALID_REQUEST = "invalid_request"
CONFLICT = "conflict"
IDEMPOTENCY_MISMATCH = "idempotency_mismatch"
UNAVAILABLE = "unavailable"
PLAN_REQUIRED = "plan_required"
