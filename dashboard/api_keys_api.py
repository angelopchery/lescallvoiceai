"""Tenant-facing API-key management — `/api/apikeys`.

Session-authenticated (this is the dashboard's Settings → API access panel),
owner-scoped like every other tenant surface. Separate from public_api.py
because the two have different credentials and different threat models: this
one mints keys and is reached with a login cookie; that one is reached with the
key and must never be able to mint another.

What a tenant may and may not do here is deliberate:

  * MAY create, rename, re-scope, narrow to their own IPs, and revoke.
  * MAY NOT widen the operator's IP allowlist, raise their rate limits, or read
    back a secret. The policy is returned read-only so the page can EXPLAIN the
    restriction rather than leaving a customer guessing why their server is
    getting 403s.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from auth import current_owner                       # noqa: E402
from shared.public_api import keys as keys_mod       # noqa: E402
from shared.public_api import policy as policy_mod   # noqa: E402
from shared.public_api import ratelimit              # noqa: E402
from shared.public_api.errors import ApiError        # noqa: E402

logger = logging.getLogger("dashboard.apikeys")

router = APIRouter(prefix="/api/apikeys")

MAX_EXPIRY_DAYS = 3650


def _fail(exc: ApiError) -> JSONResponse:
    return JSONResponse({"error": exc.code, "reason": exc.message},
                        status_code=exc.status)


def _plan_allows(owner: str) -> bool:
    """Does this account's plan include the integration API?

    True while platform enforcement is off, exactly like every other capability
    gate — see shared/billing/entitlements.feature_allowed. The Settings page
    renders an upgrade panel off this, so the panel appears the day enforcement
    is switched on and not a moment before.
    """
    try:
        from shared.billing.entitlements import feature_allowed
        return feature_allowed(owner, "api_access")
    except Exception:
        return True


def _available() -> bool:
    """The key tables live in a versioned migration, which is Postgres-only
    (shared/db/migrate.py). On a SQLite dev box the panel must say so plainly
    instead of 500ing on every read."""
    try:
        from shared.db.database import db_url
        return db_url().startswith(("postgres://", "postgresql://"))
    except Exception:
        return False


@router.get("")
def list_api_keys(owner: str = Depends(current_owner)):
    if not _available():
        return {"available": False, "keys": [], "scopes": keys_mod.SCOPES,
                "reason": "The integration API needs the Postgres database; this "
                          "instance is running on the SQLite dev fallback."}
    try:
        keys = keys_mod.list_keys(owner)
    except Exception as exc:
        logger.error("key list failed for %s: %r", owner, exc)
        return JSONResponse({"error": "unavailable",
                             "reason": "Could not read your API keys."},
                            status_code=500)
    pol = policy_mod.get_policy(owner)
    plan_allows = _plan_allows(owner)
    active_ids = [k["key_id"] for k in keys if k["status"] == "active"]
    from shared.recording_storage import public_base_url
    base = public_base_url()
    return {
        "available": True,
        "keys": keys,
        "scopes": keys_mod.SCOPES,
        "default_scopes": keys_mod.DEFAULT_SCOPES,
        "base_url": base,
        "docs_url": f"{base}/v1/openapi.json",
        # Read-only: the operator owns these. Surfaced so the page can explain a
        # 403 instead of the customer discovering the restriction by trial.
        "policy": {
            "enabled": bool(pol.get("enabled")),
            "rate_per_min": pol.get("rate_per_min"),
            "rate_per_day": pol.get("rate_per_day"),
            "max_keys": pol.get("max_keys"),
            "ip_restricted": bool(pol.get("ip_allowlist")),
            "ip_allowlist": [{"cidr": e["cidr"], "label": e.get("label", "")}
                             for e in pol.get("ip_allowlist") or []],
            "note": pol.get("note") or "",
        },
        # The plan gate, reported separately from the operator's `enabled`
        # switch: "your plan does not include this" and "we turned this off for
        # you" are different problems with different fixes, and a panel that
        # conflates them sends the customer to the wrong place.
        "plan_allowed": plan_allows,
        "plan_required": "growth",
        "upgrade_url": "/billing",
        "usage_by_day": ratelimit.daily_history(owner, days=14),
        "account_usage": ratelimit.account_usage(owner),
        "active_count": len(active_ids),
    }


@router.post("")
async def create_api_key(request: Request, owner: str = Depends(current_owner)):
    """Mint a key. The plaintext token comes back exactly once, here."""
    if not _available():
        return JSONResponse({"error": "unavailable",
                             "reason": "The integration API is not available on "
                                       "this instance."}, status_code=503)
    try:
        body = await request.json()
        assert isinstance(body, dict)
    except Exception:
        return JSONResponse({"error": "invalid_request",
                             "reason": "Body must be a JSON object."}, status_code=400)

    if not _plan_allows(owner):
        from shared.billing.errors import BillingRefusal, CODE_PLAN_REQUIRED
        raise BillingRefusal(
            "The integration API is included in Growth. Upgrade to connect your "
            "CRM or ERP to LesCall.", CODE_PLAN_REQUIRED, feature="api_access")

    expires_at = None
    raw_days = body.get("expires_in_days")
    if raw_days not in (None, "", 0, "0"):
        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            return JSONResponse({"error": "invalid_request",
                                 "reason": "expires_in_days must be a number."},
                                status_code=400)
        if not 1 <= days <= MAX_EXPIRY_DAYS:
            return JSONResponse({"error": "invalid_request",
                                 "reason": f"expires_in_days must be 1..{MAX_EXPIRY_DAYS}."},
                                status_code=400)
        expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)

    try:
        key = keys_mod.create_key(
            owner, name=str(body.get("name") or ""),
            scopes=body.get("scopes"),
            ip_allowlist=body.get("ip_allowlist"),
            expires_at=expires_at, actor=owner)
    except ApiError as exc:
        return _fail(exc)
    except Exception as exc:
        logger.error("key create failed for %s: %r", owner, exc)
        return JSONResponse({"error": "unavailable",
                             "reason": "Could not create the key."}, status_code=500)

    try:
        from shared.audit import record
        record(owner, "api_key_create", key["key_id"],
               {"name": key["name"], "scopes": key["scopes"]},
               ip=request.client.host if request.client else None)
    except Exception:
        pass
    return JSONResponse(key, status_code=201)


@router.patch("/{key_pk}")
async def update_api_key(key_pk: int, request: Request,
                         owner: str = Depends(current_owner)):
    try:
        body = await request.json()
        assert isinstance(body, dict)
    except Exception:
        return JSONResponse({"error": "invalid_request",
                             "reason": "Body must be a JSON object."}, status_code=400)
    try:
        key = keys_mod.update_key(owner, key_pk, name=body.get("name"),
                                  scopes=body.get("scopes"),
                                  ip_allowlist=body.get("ip_allowlist"))
    except ApiError as exc:
        return _fail(exc)
    except Exception as exc:
        logger.error("key update failed for %s/%s: %r", owner, key_pk, exc)
        return JSONResponse({"error": "unavailable",
                             "reason": "Could not update the key."}, status_code=500)
    try:
        from shared.audit import record
        record(owner, "api_key_update", key["key_id"], {"name": key["name"]},
               ip=request.client.host if request.client else None)
    except Exception:
        pass
    return key


@router.delete("/{key_pk}")
def revoke_api_key(key_pk: int, request: Request,
                   owner: str = Depends(current_owner)):
    try:
        key = keys_mod.revoke_key(owner, key_pk, actor=owner)
    except ApiError as exc:
        return _fail(exc)
    except Exception as exc:
        logger.error("key revoke failed for %s/%s: %r", owner, key_pk, exc)
        return JSONResponse({"error": "unavailable",
                             "reason": "Could not revoke the key."}, status_code=500)
    try:
        from shared.audit import record
        record(owner, "api_key_revoke", key["key_id"], {"name": key["name"]},
               ip=request.client.host if request.client else None)
    except Exception:
        pass
    return key
