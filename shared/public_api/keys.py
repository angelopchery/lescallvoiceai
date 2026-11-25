"""API keys: minting, verification, and the full request-admission chain.

Token shape:  lk_live_<key_id>_<secret>

  * `key_id` is 16 hex chars and is PUBLIC. It travels in the token so
    verification is one indexed lookup instead of hashing the candidate against
    every row in the table.
  * `secret` is 256 bits of `secrets.token_urlsafe`. Only its SHA-256 is
    stored, so a database dump cannot be replayed against the API, and the
    plaintext exists exactly once — in the response that minted it.

Plain SHA-256 rather than bcrypt/argon2 on purpose: the secret is 256 random
bits, not a human password, so there is no dictionary to run and no work factor
worth paying on every single API request. (The same reasoning already governs
`calls.download_token`.)

`authenticate()` is the whole admission chain in one place — parse, look up,
compare, check status/expiry, check the operator's IP boundary, then the
tenant's own, then the rate limiter. Order matters: an unlisted IP is refused
BEFORE the key is validated, so a stolen key tried from an unlisted host never
learns whether it was the key or the address that failed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import secrets

from shared.public_api import errors as err
from shared.public_api import policy as policy_mod
from shared.public_api import ratelimit
from shared.public_api.errors import ApiError

logger = logging.getLogger("public_api.keys")

TOKEN_PREFIX = "lk_live_"
KEY_ID_LEN = 16

# The capability vocabulary. Deliberately small: a scope nobody can explain in
# one line is a scope customers will grant blindly.
SCOPES: dict[str, str] = {
    "contacts:read": "Read the contacts in your phone book.",
    "contacts:write": "Create and update contacts, and add them to groups.",
    "calls:read": "List calls and read their details, outcomes and metadata.",
    "transcripts:read": "Read full call transcripts.",
    "intelligence:read": "Read the AI summary, scores, commitments and action items for a call.",
    "recordings:read": "Download call recordings.",
}

DEFAULT_SCOPES = ["contacts:write", "contacts:read", "calls:read",
                  "transcripts:read", "intelligence:read", "recordings:read"]


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_dt(value) -> dt.datetime | None:
    """This codebase's DB layer returns timestamps as STRINGS (see
    database._TextLoader), so every comparison has to parse first."""
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def normalize_scopes(scopes) -> list[str]:
    if isinstance(scopes, str):
        scopes = [s.strip() for s in scopes.split(",")]
    seen, out = set(), []
    for s in (scopes or []):
        s = str(s).strip()
        if s in SCOPES and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def parse_token(token: str) -> tuple[str, str] | None:
    """(key_id, secret), or None if this is not one of our tokens."""
    token = (token or "").strip()
    if not token.startswith(TOKEN_PREFIX):
        return None
    rest = token[len(TOKEN_PREFIX):]
    if len(rest) < KEY_ID_LEN + 2 or rest[KEY_ID_LEN] != "_":
        return None
    key_id, secret = rest[:KEY_ID_LEN], rest[KEY_ID_LEN + 1:]
    if not all(c in "0123456789abcdef" for c in key_id) or len(secret) < 20:
        return None
    return key_id, secret


def bearer_from_request(request) -> str:
    """The credential, from wherever the client put it.

    `Authorization: Bearer` is the documented way. `X-API-Key` is accepted too
    because a good share of CRM/ERP HTTP connectors cannot set an Authorization
    header at all, and the alternative is those customers pasting the key into
    a query string, where it would land in every access log we keep.
    """
    header = (request.headers.get("authorization") or "").strip()
    if header[:7].lower() == "bearer ":
        return header[7:].strip()
    return (request.headers.get("x-api-key") or "").strip()


# ---------------------------------------------------------------------------
# client address
# ---------------------------------------------------------------------------

# Hops we set ourselves. `X-Real-IP`/`X-Forwarded-For` are client-controllable
# strings; trusting them from an arbitrary peer would make the IP allowlist
# decorative, since anyone could claim to be an allowlisted address.
_TRUSTED_PROXIES = ("127.0.0.1", "::1", "localhost")


def _valid_ip(candidate: str) -> str:
    """The address, or "" if it is not one.

    Everything downstream treats "" as an unknown address: it never matches an
    allowlist entry (so a restricted account fails CLOSED) and it is never
    written to the key row, which would otherwise let a caller store arbitrary
    text in a column two UIs render as an IP address.
    """
    import ipaddress
    candidate = (candidate or "").strip()
    if not candidate or len(candidate) > 45:
        return ""
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return ""


def client_ip(request) -> str:
    """The caller's real address, or "" if we cannot determine one.

    nginx terminates TLS on this host and proxies to 127.0.0.1:8080 with
    `X-Real-IP $remote_addr` — a value nginx OVERWRITES, so it cannot be forged
    by the client. We honour it only when the connection actually came from
    loopback; a direct hit on :8080 uses the socket peer instead.
    """
    peer = ""
    try:
        peer = request.client.host if request.client else ""
    except Exception:
        peer = ""
    if peer in _TRUSTED_PROXIES:
        stated = request.headers.get("x-real-ip")
        if stated is None:
            # nginx APPENDS the peer to any client-supplied X-Forwarded-For, so
            # the LAST element is the address it actually saw; everything before
            # it was written by the caller and must be ignored.
            forwarded = (request.headers.get("x-forwarded-for") or "").strip()
            if forwarded:
                return _valid_ip(forwarded.split(",")[-1])
            return _valid_ip(peer)      # a genuinely local caller
        # A stated address that will not parse is not an address. Returning ""
        # rather than the raw text keeps it out of the key row and out of every
        # allowlist comparison, which then fails closed.
        return _valid_ip(stated)
    return _valid_ip(peer)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def _row_to_public(row: dict, *, include_secret: str | None = None) -> dict:
    """The shape both UIs render. Timestamps go through the API's own RFC 3339
    normalizer — Postgres hands this codebase "2026-11-17 05:54:43.075+00",
    which `new Date()` in a browser is not obliged to parse."""
    from shared.public_api.serialize import ts

    out = {
        "id": row.get("id"),
        "key_id": row.get("key_id"),
        "name": row.get("name") or "",
        "masked": f"{TOKEN_PREFIX}{row.get('key_id')}_" + "•" * 8 + (row.get("last4") or ""),
        "scopes": json.loads(row.get("scopes") or "[]"),
        "ip_allowlist": json.loads(row.get("ip_allowlist") or "[]"),
        "rate_per_min": row.get("rate_per_min"),
        "rate_per_day": row.get("rate_per_day"),
        "status": row.get("status") or "active",
        "created_at": ts(row.get("created_at")),
        "created_by": row.get("created_by"),
        "expires_at": ts(row.get("expires_at")),
        "last_used_at": ts(row.get("last_used_at")),
        "last_used_ip": row.get("last_used_ip"),
        "request_count": int(row.get("request_count") or 0),
        "revoked_at": ts(row.get("revoked_at")),
    }
    expires = _as_dt(row.get("expires_at"))
    out["expired"] = bool(expires and expires <= _now())
    if include_secret:
        out["secret"] = include_secret
    return out


def list_keys(owner: str, include_revoked: bool = True) -> list[dict]:
    from shared.db.database import get_db
    owner = (owner or "").strip().lower()
    sql = "SELECT * FROM api_keys WHERE owner=?"
    if not include_revoked:
        sql += " AND status='active'"
    sql += " ORDER BY (status='active') DESC, id DESC"
    with get_db() as db:
        rows = [dict(r) for r in db.execute(sql, (owner,)).fetchall()]
    keys = [_row_to_public(r) for r in rows]
    counts = ratelimit.usage([r["key_id"] for r in rows])
    for k in keys:
        k["usage"] = counts.get(k["key_id"], {"minute": 0, "day": 0})
    return keys


def create_key(owner: str, *, name: str, scopes=None, ip_allowlist=None,
               expires_at: dt.datetime | None = None,
               rate_per_min: int | None = None,
               rate_per_day: int | None = None,
               actor: str = "") -> dict:
    """Mint a key. The returned dict carries `token` — the ONLY time the
    plaintext ever exists outside the caller's clipboard."""
    from shared.db.database import get_db
    owner = (owner or "").strip().lower()
    if not owner:
        raise ValueError("owner required")
    name = (name or "").strip()[:80] or "API key"
    scope_list = normalize_scopes(scopes) or list(DEFAULT_SCOPES)
    allow = policy_mod.normalize_allowlist(ip_allowlist)

    pol = policy_mod.get_policy(owner)
    key_id = secrets.token_hex(KEY_ID_LEN // 2)
    secret = secrets.token_urlsafe(32)
    token = f"{TOKEN_PREFIX}{key_id}_{secret}"

    # Cap check and insert share one transaction so two simultaneous creates
    # cannot both read "4 of 5" and both write.
    with get_db() as db:
        active = db.execute(
            "SELECT COUNT(*) AS n FROM api_keys WHERE owner=? AND status='active'",
            (owner,)).fetchone()
        if int(dict(active)["n"]) >= int(pol.get("max_keys") or 0):
            raise ApiError(409, err.CONFLICT,
                           f"You already have the maximum of {pol.get('max_keys')} "
                           f"active API keys. Revoke one before creating another.")
        db.execute(
            "INSERT INTO api_keys (owner, key_id, secret_hash, last4, name, scopes, "
            " ip_allowlist, rate_per_min, rate_per_day, expires_at, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (owner, key_id, _hash(secret), secret[-4:], name,
             json.dumps(scope_list), json.dumps(allow, ensure_ascii=False),
             rate_per_min, rate_per_day, expires_at, actor or owner))
        row = dict(db.execute("SELECT * FROM api_keys WHERE key_id=?",
                              (key_id,)).fetchone())
    logger.info("[api] key minted for %s by %s (%s, scopes=%s)",
                owner, actor or owner, key_id, ",".join(scope_list))
    out = _row_to_public(row)
    out["token"] = token
    out["usage"] = {"minute": 0, "day": 0}
    return out


def update_key(owner: str, key_pk: int, *, name: str | None = None,
               scopes=None, ip_allowlist=None) -> dict:
    """Edit the non-secret parts of a key. The secret itself is never
    re-issuable — rotating means minting a new key and revoking this one, which
    is the only sequence with no window where both halves are unknown."""
    from shared.db.database import get_db
    owner = (owner or "").strip().lower()
    with get_db() as db:
        row = db.execute("SELECT * FROM api_keys WHERE id=? AND owner=?",
                         (key_pk, owner)).fetchone()
        if not row:
            raise ApiError(404, err.NOT_FOUND, "No such API key.")
        row = dict(row)
        if name is not None:
            row["name"] = (name or "").strip()[:80] or "API key"
        if scopes is not None:
            row["scopes"] = json.dumps(normalize_scopes(scopes))
        if ip_allowlist is not None:
            row["ip_allowlist"] = json.dumps(
                policy_mod.normalize_allowlist(ip_allowlist), ensure_ascii=False)
        db.execute("UPDATE api_keys SET name=?, scopes=?, ip_allowlist=? WHERE id=?",
                   (row["name"], row["scopes"], row["ip_allowlist"], key_pk))
        row = dict(db.execute("SELECT * FROM api_keys WHERE id=?", (key_pk,)).fetchone())
    return _row_to_public(row)


def revoke_key(owner: str, key_pk: int, actor: str = "") -> dict:
    """Revoke, never delete: the row is the record of what that credential was
    allowed to do and when it was last used, which is exactly what anyone
    investigating a leak needs after it is turned off."""
    from shared.db.database import get_db
    owner = (owner or "").strip().lower()
    with get_db() as db:
        row = db.execute("SELECT * FROM api_keys WHERE id=? AND owner=?",
                         (key_pk, owner)).fetchone()
        if not row:
            raise ApiError(404, err.NOT_FOUND, "No such API key.")
        db.execute(
            "UPDATE api_keys SET status='revoked', revoked_at=?, revoked_by=? WHERE id=?",
            (_now(), actor or owner, key_pk))
        row = dict(db.execute("SELECT * FROM api_keys WHERE id=?", (key_pk,)).fetchone())
    logger.info("[api] key %s revoked for %s by %s", row["key_id"], owner, actor or owner)
    return _row_to_public(row)


def _touch(key_id: str, ip: str) -> None:
    try:
        from shared.db.database import get_db
        with get_db() as db:
            db.execute(
                "UPDATE api_keys SET last_used_at=?, last_used_ip=?, "
                " request_count=request_count+1 WHERE key_id=?",
                (_now(), ip[:64], key_id))
    except Exception as exc:
        logger.warning("[api] key touch failed for %s: %r", key_id, exc)


# ---------------------------------------------------------------------------
# admission
# ---------------------------------------------------------------------------

class AuthContext:
    """Everything downstream needs to know about an admitted request."""

    def __init__(self, owner: str, key: dict, policy: dict, entry: dict | None,
                 limits: dict, verdict, ip: str):
        self.owner = owner
        self.key = key
        self.policy = policy
        self.entry = entry
        self.limits = limits
        self.verdict = verdict
        self.ip = ip
        self.scopes = set(json.loads(key.get("scopes") or "[]"))

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise ApiError(
                403, err.INSUFFICIENT_SCOPE,
                f"This key does not have the '{scope}' scope. Add it in "
                f"Settings → API access, or mint a key that has it.",
                details={"required_scope": scope, "granted": sorted(self.scopes)})

    def headers(self) -> dict[str, str]:
        return self.verdict.headers() if self.verdict else {}


def authenticate(request) -> AuthContext:
    """Admit or refuse one API request. Raises ApiError with a stable code."""
    from shared.db.database import get_db

    ip = client_ip(request)
    if ratelimit.auth_failures_exceeded(ip):
        # Refused in memory: a scanner walking key ids must not get a free
        # indexed SELECT per attempt on a box that is also carrying calls.
        raise ApiError(429, err.RATE_LIMITED,
                       "Too many failed authentication attempts. Wait a minute "
                       "and retry with a valid key.",
                       headers={"Retry-After": "60"})
    token = bearer_from_request(request)
    if not token:
        raise ApiError(401, err.UNAUTHENTICATED,
                       "Send your key as 'Authorization: Bearer lk_live_…' "
                       "(or the X-API-Key header).",
                       headers={"WWW-Authenticate": "Bearer"})
    parsed = parse_token(token)
    if not parsed:
        ratelimit.note_auth_failure(ip)
        raise ApiError(401, err.INVALID_KEY, "That is not a valid LesCall API key.")
    key_id, secret = parsed

    try:
        with get_db() as db:
            row = db.execute("SELECT * FROM api_keys WHERE key_id=?", (key_id,)).fetchone()
    except Exception as exc:
        logger.error("[api] key lookup failed: %r", exc)
        raise ApiError(503, err.UNAVAILABLE,
                       "The API is temporarily unavailable. Retry shortly.")
    # One message for "no such key id" and for "wrong secret". Distinguishing
    # them turns the API into an oracle: a partially-leaked key — a truncated
    # log line, a screenshot — could be confirmed as real before anyone spent
    # effort on the rest of it.
    _INVALID = "That API key is not valid."
    if not row:
        ratelimit.note_auth_failure(ip)
        logger.warning("[api] unknown key id %s from %s", key_id, ip)
        raise ApiError(401, err.INVALID_KEY, _INVALID)
    key = dict(row)

    if not secrets.compare_digest(_hash(secret), key.get("secret_hash") or ""):
        ratelimit.note_auth_failure(ip)
        logger.warning("[api] bad secret for key %s from %s", key_id, ip)
        raise ApiError(401, err.INVALID_KEY, _INVALID)

    owner = (key.get("owner") or "").strip().lower()
    pol = policy_mod.get_policy(owner)

    # The operator's boundary comes FIRST — before status, before expiry. An
    # unlisted address learns nothing about the key it presented.
    allowed, entry = policy_mod.match_ip(ip, pol.get("ip_allowlist"))
    if not allowed:
        logger.warning("[api] %s refused for %s: IP not in account allowlist", ip, owner)
        raise ApiError(403, err.IP_NOT_ALLOWED,
                       f"Requests from {ip or 'an unrecognised address'} are not "
                       f"permitted for this account. Ask your LesCall contact to "
                       f"add this address.")

    if key.get("status") != "active":
        raise ApiError(401, err.KEY_REVOKED, "That API key has been revoked.")
    expires = _as_dt(key.get("expires_at"))
    if expires and expires <= _now():
        raise ApiError(401, err.KEY_EXPIRED, "That API key has expired.")

    if not int(pol.get("enabled") or 0):
        raise ApiError(403, err.API_DISABLED,
                       "API access is disabled for this account. Contact support.")

    # The integration API is a Growth capability (migration 0021). Checked here
    # rather than per route so there is one answer for the whole surface, and
    # checked AFTER key validity so a customer on the wrong plan is told about
    # the plan rather than being left to wonder about their key.
    #
    # `feature_allowed` is a no-op while platform billing enforcement is off,
    # which is deliberate: this gate arms itself the day enforcement is turned
    # on, and needs no second deployment to do it.
    try:
        from shared.billing.entitlements import feature_allowed
        allowed = feature_allowed(owner, "api_access")
    except Exception:           # never let a billing hiccup close a live API
        allowed = True
    if not allowed:
        raise ApiError(402, err.PLAN_REQUIRED,
                       "The integration API is included in Growth. Upgrade the "
                       "account to push contacts and export call data over the API.",
                       details={"feature": "api_access", "upgrade": "/billing"})

    # The tenant's own narrowing list, on top of the operator's.
    key_allow = policy_mod.normalize_allowlist(json.loads(key.get("ip_allowlist") or "[]"))
    if key_allow:
        ok, _ = policy_mod.match_ip(ip, key_allow)
        if not ok:
            raise ApiError(403, err.IP_NOT_ALLOWED,
                           f"This key is restricted to specific addresses and "
                           f"{ip or 'the address you connected from'} is not one "
                           f"of them.")

    # TWO budgets, both incremented: this key's, and the account's across every
    # key it owns. Without the second, "60 requests a minute" would mean
    # 60 per key — and a tenant could lift their own ceiling by minting keys.
    limits = policy_mod.effective_limits(pol, key, entry)
    ceiling = policy_mod.account_ceiling(pol)
    verdict = ratelimit.consume([
        ("key", ratelimit.key_subject(key_id), limits["per_min"], limits["per_day"]),
        ("account", ratelimit.account_subject(owner), ceiling["per_min"], ceiling["per_day"]),
    ])
    # The FIRST refusal in a window is recorded on the key row — a hammering
    # client should be visible there. Subsequent ones are served from the
    # limiter's in-process cache and write nothing, so sustained abuse costs
    # exactly one indexed SELECT per request instead of six statements.
    if not verdict.cached:
        _touch(key_id, ip)
    if not verdict.allowed:
        whose = ("this account" if verdict.scope == "account" else "this key")
        raise ApiError(429, err.RATE_LIMITED,
                       f"Rate limit exceeded for {whose}: {verdict.limit} requests "
                       f"per {verdict.kind}. Retry in {verdict.retry_after}s.",
                       headers=verdict.headers())

    return AuthContext(owner, key, pol, entry, limits, verdict, ip)
