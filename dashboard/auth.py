"""Dashboard auth: Google OAuth sessions + the `current_owner` tenant boundary.

The multi-tenant rule (docs/MULTITENANT_BYON_PLAN.md §1): every tenant-facing
endpoint takes `owner: str = Depends(current_owner)` and filters by it. This
module provides that dependency plus the login plumbing.

Two modes, switched by whether GOOGLE_CLIENT_ID is configured:

* SINGLE-TENANT FALLBACK (default, no env set): current_owner() returns
  LESCALL_DEFAULT_OWNER (tech@dhronai.com). Nothing requires login — the
  dashboard behaves exactly as before multi-tenancy. This is deliberate: the
  static UI has no login page yet, so enforcement can't precede it.
* MULTI-TENANT (GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET set): /auth/login →
  Google consent → /auth/callback verifies the code, upserts a users row in
  the shared DB, and sets a signed session cookie. current_owner() then
  requires a valid session (401 otherwise).

Sessions are stateless signed cookies (HMAC-SHA256 over a JSON payload with
expiry) — no server-side session store to lose on restart. Secret comes from
LESCALL_SESSION_SECRET or a generated dashboard/.session_secret file.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

logger = logging.getLogger("dashboard.auth")

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# OAuth creds may live in calling-agent/.env alongside the other keys; load it
# BEFORE the env reads below (mount order in server.py must not matter).
try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / "calling-agent" / ".env")
except Exception:  # pragma: no cover - dotenv is a venv dep
    pass

DEFAULT_OWNER = os.environ.get("LESCALL_DEFAULT_OWNER", "tech@dhronai.com")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
# Public callback URL; must be registered in the Google OAuth client.
OAUTH_REDIRECT_URL = os.environ.get(
    "OAUTH_REDIRECT_URL", "http://localhost:8080/auth/callback")
SESSION_COOKIE = "lescall_session"
STATE_COOKIE = "lescall_oauth_state"
# Where to land AFTER logging in. Every deep link into the SPA — a shared
# agent, a linked call, a bookmarked contact — used to be discarded on the way
# to Google and dropped the user on the homepage instead. Short-lived because
# it is only meant to survive one consent round-trip.
RETURN_COOKIE = "lescall_return_to"
RETURN_TTL_S = 900
SESSION_TTL_S = int(os.environ.get("LESCALL_SESSION_TTL_S", str(7 * 24 * 3600)))

_SECRET_FILE = Path(__file__).parent / ".session_secret"


def _session_secret() -> bytes:
    env = os.environ.get("LESCALL_SESSION_SECRET")
    if env:
        return env.encode("utf-8")
    try:
        if _SECRET_FILE.exists():
            return _SECRET_FILE.read_bytes()
        secret = secrets.token_bytes(32)
        _SECRET_FILE.write_bytes(secret)
        _SECRET_FILE.chmod(0o600)
        return secret
    except Exception as exc:  # unwritable dir — sessions won't survive restart
        logger.warning("session secret not persistable (%r); using process-local", exc)
        global _FALLBACK_SECRET
        try:
            return _FALLBACK_SECRET
        except NameError:
            _FALLBACK_SECRET = secrets.token_bytes(32)
            return _FALLBACK_SECRET


def oauth_enabled() -> bool:
    """True when ANY OAuth env is set — a partial configuration fails CLOSED
    (sessions required, login unusable until fixed) rather than silently
    falling back to unauthenticated single-tenant mode."""
    return bool(GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET)


def assert_auth_configured() -> None:
    """Fail the boot if the whole auth wall has silently evaporated.

    `oauth_enabled()` is decided by env that arrives through a best-effort
    dotenv load of calling-agent/.env wrapped in `try/except: pass`. Lose or
    mistype that file and every check above degrades to single-tenant
    fallback: no login page, no session requirement, and `current_owner()`
    returning DEFAULT_OWNER — i.e. the entire API served to the internet as
    the founder, whose entitlements are unlimited. That is a silent,
    total failure, so production asserts instead of degrading.

    Set LESCALL_REQUIRE_AUTH=false ONLY for local single-tenant development.
    """
    require = os.environ.get("LESCALL_REQUIRE_AUTH", "true").lower()
    if require not in ("1", "true", "yes"):
        logger.warning(
            "LESCALL_REQUIRE_AUTH is disabled — the API is running WITHOUT a "
            "login wall as %s. Never do this on a public host.", DEFAULT_OWNER)
        return
    if not oauth_enabled():
        raise SystemExit(
            "FATAL: LESCALL_REQUIRE_AUTH=true but no OAuth is configured — "
            "GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET are unset, so the login "
            "wall would be OFF and every request would act as the founding "
            "tenant. Check that calling-agent/.env is present and readable, "
            "or set LESCALL_REQUIRE_AUTH=false for local development.")
    if not _oauth_fully_configured():
        raise SystemExit(
            "FATAL: OAuth is half-configured (only one of GOOGLE_CLIENT_ID / "
            "GOOGLE_CLIENT_SECRET is set) — login cannot complete.")
    logger.info("auth: login wall ACTIVE (multi-tenant, signups=%s)",
                "OPEN" if "*" in allowed_emails() else
                f"{len(allowed_emails())} allowlisted")


def normalize_owner(email: str) -> str:
    """The canonical tenant key.

    `owner TEXT` is an email on every table in this system, including the
    billing tables — so two spellings of one address are two tenants, and the
    second gets a fresh quota, a fresh agent cap and a fresh Pilot claim.
    Google returns the address as registered, which is NOT necessarily
    lowercase, and `allowed_emails()` only ever lowercased for *comparison*.
    Normalize at every boundary that mints or reads an identity.
    """
    return unicodedata.normalize("NFC", str(email or "").strip()).lower()


def _oauth_fully_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


# File extensions a browser fetches on its own (icons, manifests, fonts,
# bundle chunks). Never a place a person navigated to.
_ASSET_EXTENSIONS = frozenset({
    "ico", "png", "jpg", "jpeg", "gif", "svg", "webp", "avif",
    "css", "js", "map", "txt", "xml", "json", "webmanifest",
    "woff", "woff2", "ttf", "otf",
})


def safe_return_path(raw: str | None) -> str:
    """A post-login destination we are willing to redirect to, or "".

    This value comes from a query string, i.e. from whoever wrote the link, so
    it is an open-redirect vector: `?next=https://evil.example` or the
    protocol-relative `?next=//evil.example` would send a freshly
    authenticated user off-site with our login flow as the referrer. Only
    same-origin ABSOLUTE PATHS pass, and the login surface itself is excluded
    so a bounce cannot loop.
    """
    path = (raw or "").strip()
    if not path or len(path) > 512:
        return ""
    if not path.startswith("/") or path.startswith("//") or path.startswith("/\\"):
        return ""
    if "\\" in path or "\n" in path or "\r" in path:
        return ""
    for blocked in ("/login", "/auth/"):
        if path == blocked or path.startswith(blocked):
            return ""
    # Asset fetches bounce off the login wall too — the browser's automatic
    # /favicon.ico probe on the login page most of all — and their `next`
    # arrives one request AFTER the real navigation's, overwriting the stashed
    # destination. Every fresh login then landed on /favicon.ico.
    leaf = path.split("?", 1)[0].rsplit("/", 1)[-1]
    if "." in leaf and leaf.rsplit(".", 1)[1].lower() in _ASSET_EXTENSIONS:
        return ""
    return path


def _set_return_cookie(resp, path: str) -> None:
    if not path:
        return
    resp.set_cookie(RETURN_COOKIE, path, max_age=RETURN_TTL_S, httponly=True,
                    samesite="lax", secure=_cookie_secure())


def _cookie_secure() -> bool:
    return OAUTH_REDIRECT_URL.startswith("https://")


def is_admin(email: str) -> bool:
    """Roles are flattened (2026-07-15): NO account has admin status — every
    user gets the same owner-scoped view and the same dashboard options. The
    function is kept as the single choke point so the remaining admin-only
    branches (legacy single-tenant endpoints, see-all-rows scopes, the
    platform-fallback-agent controls) stay uniformly dead instead of being
    half-removed. Legacy pre-tenancy RESOURCES (the shared Plivo trunk,
    unowned rows) still belong to DEFAULT_OWNER — that is data ownership,
    not a role."""
    return False


def allowed_emails() -> set[str]:
    """Who may log in. Comma-separated LESCALL_ALLOWED_EMAILS; "*" opens
    signup to any verified Google account.

    Defaults to DEFAULT_OWNER only: the legacy dashboard endpoints are not yet
    owner-scoped, so open signup would let any Google account see this
    tenant's data. Widen deliberately, per email, until that follow-on lands.
    """
    raw = os.environ.get("LESCALL_ALLOWED_EMAILS", DEFAULT_OWNER)
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


# ---------------------------------------------------------------------------
# Signed-cookie sessions
# ---------------------------------------------------------------------------

def _sign(payload: bytes) -> str:
    return hmac.new(_session_secret(), payload, hashlib.sha256).hexdigest()


def make_session_cookie(email: str, name: str = "", picture: str = "") -> str:
    payload = json.dumps({
        "email": normalize_owner(email), "name": name, "picture": picture,
        "exp": int(time.time()) + SESSION_TTL_S,
    }, separators=(",", ":")).encode("utf-8")
    b64 = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{b64}.{_sign(payload)}"


def read_session_cookie(cookie: str | None) -> dict | None:
    """Verified session payload, or None for missing/tampered/expired."""
    if not cookie or "." not in cookie:
        return None
    b64, sig = cookie.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    except Exception:
        return None
    if not hmac.compare_digest(_sign(payload), sig):
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("email"):
        return None
    if int(data.get("exp", 0)) < time.time():
        return None
    return data


# ---------------------------------------------------------------------------
# The tenant boundary
# ---------------------------------------------------------------------------

def current_owner(request: Request) -> str:
    """FastAPI dependency: the logged-in tenant's email.

    Single-tenant fallback returns DEFAULT_OWNER so existing deployments keep
    working before OAuth is configured; once configured, a valid session is
    required (401 otherwise).
    """
    if not oauth_enabled():
        return normalize_owner(DEFAULT_OWNER)
    session = read_session_cookie(request.cookies.get(SESSION_COOKIE))
    if not session:
        raise HTTPException(status_code=401, detail="login required")
    # Normalized here too, not only at mint time: sessions issued before this
    # change carry the raw Google claim and stay valid for up to 7 days.
    return normalize_owner(session["email"])


def _upsert_user(email: str, name: str, google_sub: str, picture: str) -> None:
    """Record the login in the shared DB. Best-effort — login must not fail
    because the DB is momentarily unavailable."""
    email = normalize_owner(email)
    try:
        from shared.db.database import get_db
        with get_db() as db:
            db.execute(
                "INSERT INTO users (email, name, google_sub, picture) VALUES (?,?,?,?) "
                "ON CONFLICT (email) DO NOTHING",
                (email, name, google_sub, picture))
            db.execute(
                "UPDATE users SET last_login=CURRENT_TIMESTAMP, name=?, picture=? "
                "WHERE email=?",
                (name, picture, email))
    except Exception as exc:
        logger.warning("user upsert failed for %s: %r", email, exc)

    # Give every new account an EXPLICIT entitlements row at first login.
    #
    # Without it a tenant's plan is implicit — it works (get_entitlements falls
    # back to Free defaults), but the P6 discovery query starts returning rows
    # again the moment signups reopen, and "no row" is indistinguishable from
    # "row we forgot to create". Best-effort: a billing hiccup must never stop
    # someone logging in.
    try:
        from shared.billing.entitlements import resolve_entitlements
        resolve_entitlements(email, actor="signup")
    except Exception as exc:
        logger.warning("entitlement provisioning skipped for %s: %r", email, exc)


# ---------------------------------------------------------------------------
# OAuth endpoints
# ---------------------------------------------------------------------------

router = APIRouter()


def _saved_profile(email: str) -> dict:
    """The user's own Settings row ({} when absent or unreadable).

    The session cookie carries the name Google knew at login and is only
    rewritten by a fresh sign-in — so it must never be the last word on who
    the user is. Whatever they typed in Settings wins everywhere the identity
    is rendered; a failed read degrades to the Google name rather than to a
    blank account chip.
    """
    if not email:
        return {}
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute("SELECT name, picture FROM users WHERE email=?",
                             (email,)).fetchone()
        return dict(row) if row else {}
    except Exception as exc:
        logger.warning("profile lookup failed for %s: %r", email, exc)
        return {}


@router.get("/api/auth/me")
def me(request: Request):
    if not oauth_enabled():
        saved = _saved_profile(DEFAULT_OWNER)
        return {"mode": "single-tenant", "email": DEFAULT_OWNER,
                "login_required": False,
                "name": (saved.get("name") or ""),
                "picture": (saved.get("picture") or "")}
    session = read_session_cookie(request.cookies.get(SESSION_COOKIE))
    if not session:
        return JSONResponse({"mode": "multi-tenant", "login_required": True}, status_code=401)
    saved = _saved_profile(session["email"])
    return {"mode": "multi-tenant", "login_required": False,
            "email": session["email"],
            "name": (saved.get("name") or "").strip() or session.get("name", ""),
            "picture": session.get("picture", "") or (saved.get("picture") or "")}


# ---------------------------------------------------------------------------
# Account profile (Settings page). Owner-scoped: each user reads/writes only
# their own users row. `phone` is where BYON test calls are placed.
# ---------------------------------------------------------------------------

@router.get("/api/auth/profile")
def get_profile(request: Request):
    owner = current_owner(request)
    row = None
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT name, phone, company, picture FROM users WHERE email=?",
                (owner,)).fetchone()
    except Exception as exc:
        logger.warning("profile read failed for %s: %r", owner, exc)
    return {
        "email": owner,
        "name": (row["name"] if row else "") or "",
        "phone": (row["phone"] if row else "") or "",
        "company": (row["company"] if row else "") or "",
        "picture": (row["picture"] if row else "") or "",
    }


@router.put("/api/auth/profile")
async def put_profile(request: Request):
    owner = current_owner(request)
    try:
        body = await request.json()
        assert isinstance(body, dict)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    name = str(body.get("name") or "").strip()[:120]
    company = str(body.get("company") or "").strip()[:120]
    phone_raw = str(body.get("phone") or "").strip()
    phone_digits = "".join(ch for ch in phone_raw if ch.isdigit())
    if phone_raw and len(phone_digits) < 7:
        raise HTTPException(status_code=400,
                            detail="phone must have at least 7 digits (include country code)")
    # Store digits-only with country code so fs_cli originate can dial it as-is.
    phone = phone_digits
    try:
        from shared.db.database import get_db
        with get_db() as db:
            existing = db.execute("SELECT id FROM users WHERE email=?", (owner,)).fetchone()
            if existing:
                db.execute("UPDATE users SET name=?, phone=?, company=? WHERE email=?",
                           (name, phone, company, owner))
            else:
                db.execute("INSERT INTO users (email, name, phone, company) VALUES (?,?,?,?)",
                           (owner, name, phone, company))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("profile save failed for %s: %r", owner, exc)
        raise HTTPException(status_code=500, detail="could not save profile")
    logger.info("profile updated for %s", owner)
    return {"status": "saved", "email": owner, "name": name, "phone": phone,
            "company": company}


@router.get("/auth/login")
def login(request: Request, next: str = ""):
    if not _oauth_fully_configured():
        return JSONResponse(
            {"error": "OAuth not (fully) configured — set BOTH GOOGLE_CLIENT_ID "
                      "and GOOGLE_CLIENT_SECRET"},
            status_code=503)
    # `next` may arrive here directly (a link straight to /auth/login) or have
    # been stashed by /login one hop earlier; the explicit param wins.
    destination = safe_return_path(next) or safe_return_path(
        request.cookies.get(RETURN_COOKIE))
    state = secrets.token_urlsafe(24)
    params = urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URL,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    })
    resp = RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")
    resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax",
                    secure=_cookie_secure())
    _set_return_cookie(resp, destination)
    return resp


@router.get("/auth/callback")
def callback(request: Request, code: str = "", state: str = ""):
    if not _oauth_fully_configured():
        return JSONResponse({"error": "OAuth not configured"}, status_code=503)
    if not code or not state or state != request.cookies.get(STATE_COOKIE):
        return RedirectResponse("/login?error=state")

    body = urllib.parse.urlencode({
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": OAUTH_REDIRECT_URL,
        "grant_type": "authorization_code",
    }).encode("ascii")
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10) as fh:
            token = json.loads(fh.read().decode("utf-8"))
    except Exception as exc:
        logger.error("OAuth code exchange failed: %r", exc)
        return RedirectResponse("/login?error=exchange")

    # The id_token arrives directly from Google's token endpoint over TLS, so
    # decoding its payload without JWKS signature verification is sufficient.
    id_token = token.get("id_token", "")
    try:
        payload_b64 = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(
            payload_b64 + "=" * (-len(payload_b64) % 4)))
    except Exception:
        return RedirectResponse("/login?error=exchange")

    email = claims.get("email", "")
    if not email or not claims.get("email_verified", False):
        return RedirectResponse("/login?error=unverified")

    allowed = allowed_emails()
    if "*" not in allowed and email.lower() not in allowed:
        logger.warning("login rejected (not in LESCALL_ALLOWED_EMAILS): %s", email)
        return RedirectResponse("/login?error=not_authorized")

    _upsert_user(email, claims.get("name", ""), claims.get("sub", ""),
                 claims.get("picture", ""))
    # Back to whatever they were trying to open before the login wall caught
    # them — a share link's token lives in that path, so dropping it means the
    # invitation silently evaporates on first use.
    destination = safe_return_path(request.cookies.get(RETURN_COOKIE)) or "/"
    resp = RedirectResponse(destination)
    resp.delete_cookie(RETURN_COOKIE)
    resp.set_cookie(
        SESSION_COOKIE, make_session_cookie(email, claims.get("name", ""),
                                            claims.get("picture", "")),
        max_age=SESSION_TTL_S, httponly=True, samesite="lax",
        secure=_cookie_secure())
    resp.delete_cookie(STATE_COOKIE)
    logger.info("login: %s", email)
    try:
        from shared.audit import record
        record(email, "login", ip=request.client.host if request.client else None)
    except Exception:
        pass
    return resp


@router.get("/auth/logout")
def logout():
    resp = RedirectResponse("/login" if oauth_enabled() else "/")
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/login")
def login_page(request: Request, next: str = ""):
    """The branded login page (static/login.html).

    `?next=` is the path the visitor was actually trying to open; nginx's
    auth_request wall appends it when it bounces an unauthenticated SPA
    navigation here. It is stashed in a short-lived cookie rather than
    threaded through login.html, so the static page needs no knowledge of it.
    """
    destination = safe_return_path(next)
    if not oauth_enabled():
        return RedirectResponse(destination or "/")  # single-tenant: no wall
    if read_session_cookie(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(destination or "/")
    page = Path(__file__).parent / "static" / "login.html"
    resp = (FileResponse(str(page), media_type="text/html") if page.exists()
            else RedirectResponse("/auth/login"))  # page missing — go to Google
    # The response carried no Cache-Control, so browsers applied HEURISTIC
    # freshness off last-modified and kept serving a stored copy without
    # asking — a redesigned or fixed login page reached returning visitors
    # only once their cache aged out. "no-cache" is revalidate-before-use,
    # not don't-store.
    #
    # Starlette's FileResponse sends an ETag but does NOT honour
    # If-None-Match, so each revalidation is a full 200, not a 304. That is
    # the right trade here and nowhere else: this page is ~15 KB, unauthed,
    # and served once per sign-in. Do not copy this header onto the SPA's
    # hashed assets, which are immutable and must stay cacheable.
    resp.headers["Cache-Control"] = "no-cache"
    _set_return_cookie(resp, destination)
    return resp


# ---------------------------------------------------------------------------
# The login wall
# ---------------------------------------------------------------------------

# Reachable without a session: the login surface itself + static assets
# (login.html needs its fonts/styles) + the session probe.
# /api/recordings/dl/ and /api/exports/dl/ are the public tokenized downloads
# (a call recording, a generated .xlsx export): the 256-bit random token in the
# path is the credential, so no session is required. Both live under a /dl/
# path segment rather than at the resource root, so opening them by prefix
# cannot widen to the owner-scoped routes beside them.
# /v1/ is the PUBLIC INTEGRATION API. It is exempt from the session wall
# because it carries its own credential — an API key — and enforces its own
# tenancy, IP allowlist and rate limit in shared/public_api/keys.py. It lives
# OUTSIDE /api/ precisely so it cannot inherit, or accidentally widen, the
# tenancy allowlist below: a bypass granted there would apply by prefix to
# every future sibling route.
# /r/ is a tracked link inside a WhatsApp message a CUSTOMER taps: it has to
# work with no session at all. The token names one URL and nothing else, and
# the handler validates its shape before touching the database.
_OPEN_PREFIXES = ("/auth/", "/login", "/static/", "/api/auth/me", "/favicon",
                  "/api/recordings/dl/", "/api/exports/dl/", "/v1/", "/r/",
                  # Interakt's WhatsApp webhook is per tenant — the account is
                  # named by the token in the path, so it cannot be an exact
                  # path. It authenticates itself with an HMAC over the raw
                  # body keyed by that tenant's secret (whatsapp_api.py), and
                  # the prefix ends in a slash + token, so no sibling route can
                  # ever start with it (RUNBOOK §80).
                  "/api/whatsapp/webhook/interakt/")

# Paths open to unauthenticated callers by EXACT match only. Prefix matching is
# wrong for these: a prefix short-circuits both the session requirement and the
# tenancy gate, so adding "/api/billing/webhook/razorpay" to _OPEN_PREFIXES
# would silently un-authenticate any future sibling route whose path starts
# with those characters (…/razorpay-test, …/razorpay2). The webhook
# authenticates itself with an HMAC over the raw body — that signature IS its
# credential — so exactly one path, compared with ==, is what it needs.
# "/v1" is the API's service descriptor. It is EXACT here rather than a prefix
# so that only the documented "/v1/..." namespace (above) and this one path are
# opened — "/v1" as a prefix would also un-gate any future "/v1beta"-shaped
# route by accident.
# The Meta WhatsApp webhook is the same shape as Razorpay's: its credential is
# the X-Hub-Signature-256 HMAC over the raw body, so exactly one path opens.
_OPEN_EXACT = ("/api/billing/webhook/razorpay", "/v1", "/api/whatsapp/webhook")

# API prefixes tenants may use — every handler under these performs its own
# per-owner scoping. EVERYTHING ELSE under /api/ is DENIED for every session
# (roles are flattened; there are no admins): the legacy stores (runtime
# control, keys, follow-up automation…) are single-tenant surfaces with no
# owner column, so exposing them to any session would leak the founding
# tenant's data. Deny by default: a newly added legacy endpoint is
# automatically unreachable until it is owner-scoped and listed here.
_TENANT_API_PREFIXES = (
    "/api/auth/",
    "/api/agents",       # owner-scoped in agent_builder_api
    # Agent share links. NOT owner-scoped by slug — that is the point: a
    # recipient reads an agent they do not own, keyed by an unguessable token.
    # A session is still required (this list only bypasses the tenancy gate,
    # never the login wall), and every route re-checks ownership itself.
    "/api/share",
    "/api/copilot",      # owner-scoped agent creation chat
    "/api/trunks",
    "/api/numbers",
    "/api/telephony",
    # Per-tenant contacts + call history (Part B Phase 3). Every endpoint under
    # these is owner-scoped: contacts by owner column; recordings/history/
    # analysis by the calls-table folder→owner map (legacy folders = admin);
    # live calls by the snapshot's owner field (see _scope_live_state).
    "/api/outbound",
    "/api/calls/history",
    "/api/calls/recordings",
    "/api/calls/transcripts",
    "/api/calls/active",
    "/api/calls/live-state",
    "/api/usage/minutes",   # billing view; owner-scoped inside the endpoint
    # Subscriptions, checkout, receipts, top-ups. Every endpoint under it takes
    # Depends(current_owner) and scopes by that owner; the Razorpay webhook is
    # the one exception and is opened by EXACT match in _OPEN_EXACT above.
    "/api/billing",
    "/api/analysis",
    "/api/analytics/conversation",
    # Homepage KPIs. Was deliberately absent — every figure came from the
    # credit-log JSONL, which has no owner column, so the page would have shown
    # platform-wide totals to every tenant. Now sourced from the calls table
    # via _owner_call_stats() and scoped, so it can be exposed (2026-07-22).
    "/api/overview",
    # Intelligence layer (deploy/PLAN-intelligence-layer.md): every endpoint
    # under /api/intel is owner-scoped via the current_owner dependency.
    "/api/intel",
    # Shared-infrastructure health (pipeline/telephony up-down) — not tenant
    # data, and every tenant legitimately needs to see whether the platform is
    # up. Returns only service running-flags + ports, no per-call info.
    "/api/health",
    # TTS voice previews — platform-wide stock samples (canned line per Sarvam
    # speaker), no tenant data. Session still required. Absent from this list
    # 07-24→08-01, which made every "tap to hear" chip in the copilot 403.
    "/api/voices",
    # Live voice preview (a fixed sample line at a chosen speed) for the
    # Builder's Preview button and the Copilot voice picker — platform
    # synthesis, no tenant data; session still required. Unlisted, every
    # tenant's preview 403'd (RUNBOOK §67).
    "/api/tts/preview",
    # Settings → API access: the tenant mints and revokes their OWN integration
    # keys here, with a session. Owner-scoped in api_keys_api. The keys it
    # mints are spent against /v1, which is not under /api/ at all.
    "/api/apikeys",
    # Integrations → WhatsApp (dashboard/whatsapp_api.py): every route takes
    # Depends(current_owner). The Meta webhook is NOT under this prefix — it
    # is opened by exact match above and authenticates by signature.
    "/api/integrations",
)


def install_login_gate(app) -> None:
    """Session wall + tenancy gate whenever OAuth is enabled. A complete no-op
    in single-tenant fallback mode, so installing it is always safe."""

    @app.middleware("http")
    async def _login_gate(request: Request, call_next):
        if oauth_enabled():
            path = request.url.path
            if (path not in _OPEN_EXACT
                    and not any(path.startswith(p) for p in _OPEN_PREFIXES)):
                session = read_session_cookie(request.cookies.get(SESSION_COOKIE))
                if not session:
                    accepts_html = "text/html" in (request.headers.get("accept") or "")
                    if not path.startswith("/api/") and (path == "/" or accepts_html):
                        # Carry the destination across the login round-trip.
                        target = path + (f"?{request.url.query}"
                                         if request.url.query else "")
                        dest = safe_return_path(target)
                        return RedirectResponse(
                            f"/login?next={urllib.parse.quote(dest, safe='')}"
                            if dest else "/login")
                    return JSONResponse({"detail": "login required"}, status_code=401)
                if (path.startswith("/api/")
                        and not any(path.startswith(p) for p in _TENANT_API_PREFIXES)):
                    return JSONResponse(
                        {"detail": "not available"}, status_code=403)
        return await call_next(request)
