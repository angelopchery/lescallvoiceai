"""LesCall public integration API — `/v1`.

The surface a customer's CRM or ERP talks to. Two jobs:

  * **push contacts in** — one lead, or a batch of 500, idempotently;
  * **pull call records out** — for every call: its intelligence summary and
    scores, its transcript, and its recording.

Design decisions worth knowing before editing this file
-------------------------------------------------------
* **It lives outside `/api/`.** The dashboard's login gate (auth.py) is a
  deny-by-default allowlist over `/api/` prefixes built for *session* auth. A
  key-authenticated surface underneath it would have to be granted a bypass in
  that list, and every future sibling route would inherit the bypass by prefix.
  `/v1` is its own namespace with its own credential and its own gate.

* **`authenticate()` runs before anything reads a row.** Key, IP allowlist and
  rate limit are one chain in shared/public_api/keys.py; no endpoint here may
  re-implement any part of it.

* **Every query filters by `ctx.owner`.** Same tenancy law as the rest of the
  platform, with a sharper edge: the caller here is a machine that will happily
  retry a URL with an incremented id forever. An id belonging to another tenant
  is a 404, never a 403 — the API does not confirm that other tenants' records
  exist.

* **Pagination is keyset, on the integer id.** `?order=asc&cursor=<last id you
  imported>` is the documented sync loop: it cannot skip or duplicate a row
  when new calls land mid-page, which offset pagination does routinely.

* **Errors are one envelope** (shared/public_api/errors.py) with a stable
  `code`. A CRM that can only distinguish "403" cannot tell a blocked IP from a
  missing scope, and that difference is the whole support ticket.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.public_api import errors as err          # noqa: E402
from shared.public_api import idempotency            # noqa: E402
from shared.public_api import keys as keys_mod       # noqa: E402
from shared.public_api import sanitize               # noqa: E402
from shared.public_api import serialize as ser       # noqa: E402
from shared.public_api.errors import ApiError        # noqa: E402

logger = logging.getLogger("dashboard.public_api")

router = APIRouter(prefix="/v1", include_in_schema=False)

API_VERSION = "2026-08-19"
MAX_PAGE = 200
DEFAULT_PAGE = 50
MAX_BULK = 500


def base_url() -> str:
    from shared.recording_storage import public_base_url
    return public_base_url()


# ---------------------------------------------------------------------------
# admission
# ---------------------------------------------------------------------------

def admit(request: Request, response: Response) -> keys_mod.AuthContext:
    """Authenticate and rate-limit. Used directly only by /v1/me, which has no
    scope of its own; every other route goes through `require`."""
    ctx = keys_mod.authenticate(request)
    # Carried for the request-audit middleware in server.py, so an API call is
    # attributable to a key in requests.jsonl and not just to an IP. Stamped
    # here rather than in `require` so /v1/me is attributable too — it is the
    # endpoint a misbehaving integration hits most.
    request.state.api_key_id = ctx.key.get("key_id")
    request.state.api_owner = ctx.owner
    for k, v in ctx.headers().items():
        response.headers[k] = v
    return ctx


def require(request: Request, response: Response, scope: str) -> keys_mod.AuthContext:
    """Authenticate, rate-limit, and check one scope. Every endpoint's first line.

    Not a FastAPI `Depends` because the scope differs per route and because the
    rate-limit headers have to land on the response of a REFUSED request too —
    which is easiest when the same call site owns both.
    """
    ctx = admit(request, response)
    ctx.require(scope)
    _require_feature(ctx, scope)
    return ctx


# Which plan capability each scope reads. `contacts:*` is deliberately absent:
# pushing leads in is how a customer starts using the platform, and putting it
# behind a tier would gate the on-ramp.
_SCOPE_FEATURE = {
    "transcripts:read": "transcripts",
    "intelligence:read": "intelligence",
    "recordings:read": "recordings",
}


def _require_feature(ctx: keys_mod.AuthContext, scope: str) -> None:
    feature = _SCOPE_FEATURE.get(scope)
    if not feature:
        return
    try:
        from shared.billing.entitlements import feature_allowed
    except Exception:
        return
    if not feature_allowed(ctx.owner, feature):
        raise ApiError(402, err.PLAN_REQUIRED,
                       f"Your plan does not include {feature}. Upgrade to export "
                       f"this over the API.", details={"feature": feature})


def _db():
    from shared.db.database import get_db
    return get_db()


# ---------------------------------------------------------------------------
# request helpers
# ---------------------------------------------------------------------------

async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise ApiError(400, err.INVALID_REQUEST, "Request body must be valid JSON.")
    if not isinstance(body, dict):
        raise ApiError(400, err.INVALID_REQUEST, "Request body must be a JSON object.")
    # The ONE place a request body is cleaned. See shared/public_api/sanitize.py:
    # a NUL byte anywhere in here would otherwise reach Postgres and turn a bad
    # request into a 500 that any caller can trigger at will.
    return sanitize.deep(body)


def _q(value: str, limit: int = 200) -> str:
    """A string query parameter, scrubbed. Same reasoning as `_json_body`."""
    return sanitize.text(value, limit).strip()


def _page_limit(limit: int) -> int:
    return max(1, min(int(limit or DEFAULT_PAGE), MAX_PAGE))


def _cursor_id(cursor: str) -> int | None:
    if not cursor:
        return None
    try:
        return int(str(cursor).strip())
    except ValueError:
        raise ApiError(400, err.INVALID_REQUEST,
                       "`cursor` must be the `next_cursor` value from a previous page.")


def _parse_time(value: str, field: str) -> dt.datetime | None:
    """Accept a date (2026-08-01) or a full RFC 3339 timestamp."""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        if len(raw) == 10:
            parsed = dt.datetime.strptime(raw, "%Y-%m-%d")
        else:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(400, err.INVALID_REQUEST,
                       f"`{field}` must be YYYY-MM-DD or an RFC 3339 timestamp.")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _list(data: list, *, has_more: bool, next_cursor: Any = None) -> dict:
    return {"object": "list", "data": data, "has_more": has_more,
            "next_cursor": (str(next_cursor) if next_cursor is not None and has_more
                            else None)}


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

@router.get("")
@router.get("/")
def service_root():
    """Unauthenticated: what this is and where the spec lives. Carries no
    tenant information, so it is safe to leave open — and being able to curl
    the base URL is the first thing anyone integrating tries."""
    b = base_url()
    return {
        "service": "LesCall Integration API",
        "version": API_VERSION,
        "documentation": f"{b}/v1/openapi.json",
        "authentication": "Authorization: Bearer lk_live_… (or X-API-Key)",
        "endpoints": {
            "contacts": f"{b}/v1/contacts",
            "calls": f"{b}/v1/calls",
            "identity": f"{b}/v1/me",
        },
    }


@router.get("/me")
def whoami(request: Request, response: Response):
    """Who this key is, what it may do, and what the limits are.

    The single most useful endpoint when an integration is misbehaving: it
    answers "is my key working", "does it have the scope", "is my server's IP
    allowed" and "am I near the limit" in one call — and it reports the IP WE
    saw, which is the answer people are usually guessing at.
    """
    ctx = admit(request, response)
    return {
        "account": ctx.owner,
        "key": {"key_id": ctx.key.get("key_id"), "name": ctx.key.get("name") or "",
                "scopes": sorted(ctx.scopes)},
        "client_ip": ctx.ip,
        "rate_limits": {"per_minute": ctx.limits["per_min"],
                        "per_day": ctx.limits["per_day"],
                        "applied_from": ("ip_rule" if ctx.entry else "account_policy")},
        "ip_restricted": bool(ctx.policy.get("ip_allowlist")),
        "api_version": API_VERSION,
    }


@router.get("/agents")
def list_agents(request: Request, response: Response):
    """The tenant's agents, so a CRM can map `agent` on a call to something
    meaningful instead of carrying an opaque slug."""
    ctx = require(request, response, "calls:read")
    with _db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT slug, display_name, is_active FROM agents "
            " WHERE owner=? AND archived_at IS NULL ORDER BY slug", (ctx.owner,)).fetchall()]
    return _list([{"slug": r["slug"], "name": r.get("display_name") or r["slug"],
                   "active": bool(r.get("is_active"))} for r in rows], has_more=False)


# ---------------------------------------------------------------------------
# contacts
# ---------------------------------------------------------------------------

_CONTACT_SELECT = (
    "SELECT c.*, g.name AS group_name FROM contacts c "
    " LEFT JOIN contact_groups g ON g.id = c.group_id ")


@router.get("/contacts")
def contacts_list(request: Request, response: Response,
                  cursor: str = Query(""), limit: int = Query(DEFAULT_PAGE),
                  group_id: int | None = Query(None), q: str = Query(""),
                  phone: str = Query(""), order: str = Query("desc")):
    ctx = require(request, response, "contacts:read")
    take = _page_limit(limit)
    asc = order.lower() == "asc"
    where = ["c.owner=?"]
    params: list[Any] = [ctx.owner]
    if group_id is not None:
        where.append("c.group_id=?")
        params.append(group_id)
    if _q(phone):
        from shared.phone import significant_digits
        where.append("c.sig_digits=?")
        params.append(significant_digits(_q(phone)))
    if _q(q):
        needle = f"%{_q(q).lower()}%"
        where.append("(LOWER(c.name) LIKE ? OR c.phone LIKE ?)")
        params += [needle, needle]
    after = _cursor_id(cursor)
    if after is not None:
        where.append("c.id > ?" if asc else "c.id < ?")
        params.append(after)
    sql = (_CONTACT_SELECT + " WHERE " + " AND ".join(where)
           + f" ORDER BY c.id {'ASC' if asc else 'DESC'} LIMIT ?")
    with _db() as db:
        rows = [dict(r) for r in db.execute(sql, (*params, take + 1)).fetchall()]
    has_more = len(rows) > take
    rows = rows[:take]
    return _list([ser.contact(r) for r in rows], has_more=has_more,
                 next_cursor=(rows[-1]["id"] if rows else None))


@router.get("/contacts/{contact_id}")
def contact_get(contact_id: str, request: Request, response: Response):
    ctx = require(request, response, "contacts:read")
    row = _contact_row(ctx.owner, contact_id)
    return ser.contact(row)


def _contact_row(owner: str, contact_id: str) -> dict:
    try:
        pk = int(str(contact_id).strip())
    except ValueError:
        raise ApiError(404, err.NOT_FOUND, "No such contact.")
    with _db() as db:
        row = db.execute(_CONTACT_SELECT + " WHERE c.id=? AND c.owner=?",
                         (pk, owner)).fetchone()
    if not row:
        raise ApiError(404, err.NOT_FOUND, "No such contact.")
    return dict(row)


def _write_contact(ctx, payload: dict, *, allow_update: bool) -> tuple[dict, bool]:
    from shared.contacts import ContactError, resolve_group, upsert_contact
    group_id = resolve_group(ctx.owner, payload.get("group_id"), payload.get("group"))
    fields = payload.get("fields")
    if fields is not None and not isinstance(fields, dict):
        raise ApiError(400, err.INVALID_REQUEST, "`fields` must be a JSON object.")
    try:
        row, created = upsert_contact(
            ctx.owner,
            phone=str(payload.get("phone") or ""),
            name=str(payload.get("name") or "").strip(),
            notes=str(payload.get("notes") or "").strip(),
            gender=str(payload.get("gender") or ""),
            fields=fields or None,
            group_id=group_id,
            allow_update=allow_update,
            source=f"api:{ctx.key.get('key_id')}")
    except ContactError as exc:
        raise ApiError(409 if "already exists" in str(exc) else 400,
                       err.CONFLICT if "already exists" in str(exc) else err.INVALID_REQUEST,
                       str(exc))
    # Re-read through the group join so the response carries group_name.
    return _contact_row(ctx.owner, row["id"]), created


@router.post("/contacts")
async def contact_create(request: Request, response: Response):
    """Push one contact.

    Upserts by default: the same number pushed twice updates rather than
    duplicating, which is what a CRM sync actually wants. Send
    `"upsert": false` to be told about the collision instead (409).
    """
    ctx = require(request, response, "contacts:write")
    payload = await _json_body(request)
    idem = (request.headers.get("idempotency-key") or "").strip()
    rhash = idempotency.body_hash("POST /v1/contacts", payload)
    replay = idempotency.lookup(ctx.key["key_id"], idem, "POST /v1/contacts", rhash)
    if replay:
        status, body = replay
        return JSONResponse(body, status_code=status,
                            headers={**ctx.headers(), "Idempotent-Replay": "true"})

    row, created = _write_contact(ctx, payload,
                                  allow_update=bool(payload.get("upsert", True)))
    body = {**ser.contact(row), "created": created}
    status = 201 if created else 200
    idempotency.store(ctx.key["key_id"], idem, "POST /v1/contacts", rhash, status, body)
    _audit(ctx, "api.contact_upsert", row.get("phone"), {"created": created})
    return JSONResponse(body, status_code=status, headers=ctx.headers())


@router.post("/contacts/bulk")
async def contacts_bulk(request: Request, response: Response):
    """Push up to 500 contacts in one request.

    Always 200 with a PER-ROW result — never all-or-nothing. A 400-row import
    where three numbers are unparseable should land 397 leads and tell you
    which three failed; refusing the batch would make the caller re-send 397
    rows they have already successfully delivered.
    """
    ctx = require(request, response, "contacts:write")
    payload = await _json_body(request)
    rows = payload.get("contacts")
    if not isinstance(rows, list) or not rows:
        raise ApiError(400, err.INVALID_REQUEST,
                       "`contacts` must be a non-empty array.")
    if len(rows) > MAX_BULK:
        raise ApiError(400, err.INVALID_REQUEST,
                       f"At most {MAX_BULK} contacts per request (got {len(rows)}). "
                       f"Send the rest in a second call.")
    idem = (request.headers.get("idempotency-key") or "").strip()
    rhash = idempotency.body_hash("POST /v1/contacts/bulk", payload)
    replay = idempotency.lookup(ctx.key["key_id"], idem, "POST /v1/contacts/bulk", rhash)
    if replay:
        status, body = replay
        return JSONResponse(body, status_code=status,
                            headers={**ctx.headers(), "Idempotent-Replay": "true"})

    default_group = {"group_id": payload.get("group_id"), "group": payload.get("group")}
    allow_update = bool(payload.get("upsert", True))
    results, created_n, updated_n, failed_n = [], 0, 0, 0
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            results.append({"index": index, "status": "failed",
                            "error": "each entry must be a JSON object"})
            failed_n += 1
            continue
        entry = {**default_group, **raw} if not (raw.get("group") or raw.get("group_id")) else raw
        try:
            row, created = _write_contact(ctx, entry, allow_update=allow_update)
        except ApiError as exc:
            results.append({"index": index, "status": "failed", "error": exc.message,
                            "code": exc.code})
            failed_n += 1
            continue
        except Exception as exc:                       # one bad row never kills a batch
            logger.warning("[api] bulk row %d failed for %s: %r", index, ctx.owner, exc)
            results.append({"index": index, "status": "failed",
                            "error": "could not store this contact"})
            failed_n += 1
            continue
        results.append({"index": index, "status": "created" if created else "updated",
                        "id": str(row["id"]), "phone": row.get("phone")})
        created_n += int(created)
        updated_n += int(not created)
    body = {"object": "bulk_result", "submitted": len(rows), "created": created_n,
            "updated": updated_n, "failed": failed_n, "results": results}
    idempotency.store(ctx.key["key_id"], idem, "POST /v1/contacts/bulk", rhash, 200, body)
    _audit(ctx, "api.contacts_bulk", None,
           {"submitted": len(rows), "created": created_n, "updated": updated_n,
            "failed": failed_n})
    return JSONResponse(body, status_code=200, headers=ctx.headers())


@router.patch("/contacts/{contact_id}")
async def contact_patch(contact_id: str, request: Request, response: Response):
    """Update one contact.

    Only the fields you send are written — an omitted field keeps whatever the
    dashboard or an earlier sync put there, which is what stops a partial CRM
    payload from blanking an operator's notes. `fields` merges rather than
    replaces, for the same reason.

    Sending a different `phone` RENUMBERS this contact (the row, and every call
    bound to it, are kept) instead of creating a second person.
    """
    from shared.contacts import ContactError, renumber_contact
    from shared.phone import significant_digits

    ctx = require(request, response, "contacts:write")
    row = _contact_row(ctx.owner, contact_id)
    payload = await _json_body(request)

    new_phone = str(payload.get("phone") or "").strip()
    if new_phone and significant_digits(new_phone) != (row.get("sig_digits") or ""):
        try:
            row = renumber_contact(ctx.owner, int(row["id"]), new_phone)
        except ContactError as exc:
            raise ApiError(409 if "already has" in str(exc) else 400,
                           err.CONFLICT if "already has" in str(exc) else err.INVALID_REQUEST,
                           str(exc))
    payload = {**payload, "phone": row["phone"]}
    updated, _created = _write_contact(ctx, payload, allow_update=True)
    _audit(ctx, "api.contact_update", updated.get("phone"))
    return ser.contact(updated)


@router.delete("/contacts/{contact_id}")
def contact_delete(contact_id: str, request: Request, response: Response):
    ctx = require(request, response, "contacts:write")
    row = _contact_row(ctx.owner, contact_id)
    from shared.contacts import delete_contact
    delete_contact(ctx.owner, int(row["id"]))
    _audit(ctx, "api.contact_delete", row.get("phone"))
    return {"object": "contact", "id": str(row["id"]), "deleted": True}


@router.get("/contact-groups")
def groups_list(request: Request, response: Response):
    ctx = require(request, response, "contacts:read")
    with _db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT g.id, g.name, g.created_at, "
            " (SELECT COUNT(*) FROM contacts c WHERE c.group_id=g.id) AS contacts "
            " FROM contact_groups g WHERE g.owner=? ORDER BY g.name",
            (ctx.owner,)).fetchall()]
    return _list([{"id": r["id"], "name": r["name"], "contacts": int(r["contacts"] or 0),
                   "created_at": ser.ts(r.get("created_at"))} for r in rows],
                 has_more=False)


@router.post("/contact-groups")
async def group_create(request: Request, response: Response):
    ctx = require(request, response, "contacts:write")
    payload = await _json_body(request)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ApiError(400, err.INVALID_REQUEST, "`name` is required.")
    from shared.contacts import RESERVED_GROUP_NAMES, resolve_group
    if name.lower() in RESERVED_GROUP_NAMES:
        raise ApiError(400, err.INVALID_REQUEST,
                       f"'{name}' is a reserved group name.")
    gid = resolve_group(ctx.owner, None, name)
    if gid is None:
        raise ApiError(400, err.INVALID_REQUEST, "Could not create that group.")
    return JSONResponse({"id": gid, "name": name, "contacts": 0}, status_code=201,
                        headers=ctx.headers())


# ---------------------------------------------------------------------------
# calls
# ---------------------------------------------------------------------------

# Scalars the list view needs from the intelligence record. Named explicitly
# rather than `ci.*` so a new internal column can never appear in a customer's
# payload without someone deciding it should.
_CALL_SELECT = (
    "SELECT c.*, ct.name AS contact_name, "
    "       ci.summary AS ci_summary, ci.processed_at AS ci_processed_at, "
    "       ci.lead_score, ci.opportunity_score, ci.risk_score, ci.sentiment, "
    "       ci.conversion_probability "
    "  FROM calls c "
    "  LEFT JOIN call_intelligence ci ON ci.call_id = c.id "
    "  LEFT JOIN contacts ct ON ct.id = c.contact_id ")


@router.get("/calls")
def calls_list(request: Request, response: Response,
               cursor: str = Query(""), limit: int = Query(DEFAULT_PAGE),
               order: str = Query("desc"), since: str = Query(""),
               until: str = Query(""), direction: str = Query(""),
               agent: str = Query(""), sentiment: str = Query(""),
               outcome_kind: str = Query(""), phone: str = Query(""),
               contact_id: int | None = Query(None),
               analyzed: bool | None = Query(None),
               min_lead_score: int | None = Query(None)):
    """List calls, newest first.

    THE SYNC LOOP: `?order=asc&cursor=<the last id you imported>`. Keyset
    pagination on the call's own id means a call landing mid-import cannot make
    you skip or re-import a row — which is exactly what offset pagination does
    against a table that is still being written to.
    """
    ctx = require(request, response, "calls:read")
    take = _page_limit(limit)
    asc = order.lower() == "asc"
    where = ["c.owner=?"]
    params: list[Any] = [ctx.owner]

    start = _parse_time(since, "since")
    end = _parse_time(until, "until")
    if start:
        where.append("c.started_at >= ?")
        params.append(start)
    if end:
        where.append("c.started_at < ?")
        params.append(end)
    if _q(direction):
        where.append("c.direction=?")
        params.append(_q(direction).lower())
    if _q(agent):
        where.append("c.agent_slug=?")
        params.append(_q(agent))
    if _q(outcome_kind):
        where.append("c.outcome_kind=?")
        params.append(_q(outcome_kind))
    if _q(sentiment):
        where.append("ci.sentiment=?")
        params.append(_q(sentiment).lower())
    if _q(phone):
        from shared.phone import significant_digits
        where.append("c.sig_digits=?")
        params.append(significant_digits(_q(phone)))
    if contact_id is not None:
        where.append("c.contact_id=?")
        params.append(contact_id)
    if analyzed is True:
        where.append("ci.processed_at IS NOT NULL")
    elif analyzed is False:
        where.append("ci.processed_at IS NULL")
    if min_lead_score is not None:
        where.append("ci.lead_score >= ?")
        params.append(int(min_lead_score))

    after = _cursor_id(cursor)
    if after is not None:
        where.append("c.id > ?" if asc else "c.id < ?")
        params.append(after)

    sql = (_CALL_SELECT + " WHERE " + " AND ".join(where)
           + f" ORDER BY c.id {'ASC' if asc else 'DESC'} LIMIT ?")
    with _db() as db:
        rows = [dict(r) for r in db.execute(sql, (*params, take + 1)).fetchall()]
    has_more = len(rows) > take
    rows = rows[:take]
    b = base_url()
    return _list([ser.call_summary(r, b) for r in rows], has_more=has_more,
                 next_cursor=(rows[-1]["id"] if rows else None))


# A call_key is "<digits>_<YYYYMMDD>_<HHMMSS>"; a conn_id is a UUID. Nothing
# legitimate carries a slash, a NUL or a quote — and this value reaches both a
# SQL parameter and (via the recording route) a filesystem path.
_SAFE_IDENT = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _call_row(owner: str, call_id: str) -> dict:
    """One call, by its PUBLIC id. `call_key` is the documented identifier;
    `conn_id` is accepted too because the live-call surfaces hand that out."""
    ident = (call_id or "").strip()
    if not _SAFE_IDENT.match(ident):
        raise ApiError(404, err.NOT_FOUND, "No such call.")
    with _db() as db:
        row = db.execute(
            _CALL_SELECT + " WHERE c.owner=? AND (c.call_key=? OR c.conn_id=?)",
            (owner, ident, ident)).fetchone()
    if not row:
        raise ApiError(404, err.NOT_FOUND, "No such call.")
    return dict(row)


def _intel_bundle(call_pk: int) -> tuple[dict | None, list[dict], list[dict], dict | None]:
    with _db() as db:
        ci = db.execute("SELECT * FROM call_intelligence WHERE call_id=?",
                        (call_pk,)).fetchone()
        commitments = [dict(r) for r in db.execute(
            "SELECT * FROM commitments WHERE call_id=? ORDER BY id", (call_pk,)).fetchall()]
        items = [dict(r) for r in db.execute(
            "SELECT * FROM action_items WHERE call_id=? ORDER BY id", (call_pk,)).fetchall()]
        fu = db.execute(
            "SELECT * FROM followups WHERE call_id=? AND deleted_at IS NULL "
            " ORDER BY id DESC LIMIT 1", (call_pk,)).fetchone()
    return (dict(ci) if ci else None), commitments, items, (dict(fu) if fu else None)


@router.get("/calls/{call_id}")
def call_detail(call_id: str, request: Request, response: Response,
                include_transcript: bool = Query(True)):
    """The whole record for one call — the payload a CRM imports.

    Scopes are checked per SECTION, not once for the endpoint: a key without
    `transcripts:read` still gets the call, its summary and its scores, with
    the transcript omitted. Refusing the entire record because one section is
    out of scope would make narrow keys useless.
    """
    ctx = require(request, response, "calls:read")
    row = _call_row(ctx.owner, call_id)
    ci, commitments, items, fu = _intel_bundle(int(row["id"]))

    may_intel = "intelligence:read" in ctx.scopes
    may_text = "transcripts:read" in ctx.scopes
    may_audio = "recordings:read" in ctx.scopes

    payload = ser.call_detail(
        row, ci=(ci if may_intel else None),
        commitments=(commitments if may_intel else []),
        items=(items if may_intel else []), fu=(fu if may_intel else None),
        base_url=base_url(),
        include_transcript=bool(include_transcript and may_text))
    if not may_audio:
        payload["recording"] = {"available": payload["recording"].get("available", False),
                                "scope_required": "recordings:read"}
    omitted = [s for s, ok in (("intelligence:read", may_intel),
                               ("transcripts:read", may_text),
                               ("recordings:read", may_audio)) if not ok]
    if omitted:
        payload["omitted_sections"] = omitted
    return payload


@router.get("/calls/{call_id}/intelligence")
def call_intelligence(call_id: str, request: Request, response: Response):
    ctx = require(request, response, "intelligence:read")
    row = _call_row(ctx.owner, call_id)
    ci, commitments, items, fu = _intel_bundle(int(row["id"]))
    if not ci:
        # Not an error: analysis is asynchronous, and a CRM polling a call that
        # ended 40 seconds ago needs to know it should come back, not that
        # something is broken.
        return {"call_id": row.get("call_key"), "analyzed": False,
                "intelligence": None,
                "message": "This call has not been analyzed yet."}
    return {"call_id": row.get("call_key"), "analyzed": True,
            "intelligence": ser.intelligence(ci),
            "commitments": [ser.commitment(c) for c in commitments],
            "action_items": [ser.action_item(a) for a in items],
            "followup": ser.followup(fu)}


@router.get("/calls/{call_id}/transcript")
def call_transcript(call_id: str, request: Request, response: Response,
                    format: str = Query("json")):
    """The transcript, as structured turns or as plain text (`?format=text`)."""
    ctx = require(request, response, "transcripts:read")
    row = _call_row(ctx.owner, call_id)
    text = row.get("transcript") or ""
    if format.lower() == "text":
        return Response(content=text, media_type="text/plain; charset=utf-8",
                        headers=ctx.headers())
    return {"call_id": row.get("call_key"), "language": row.get("language"),
            "text": text, "turns": ser.transcript_turns(text)}


@router.get("/calls/{call_id}/recording")
def call_recording(call_id: str, request: Request, response: Response,
                   download: bool = Query(True)):
    """Stream the call's WAV, authenticated by the API key.

    The same bytes the dashboard plays (recording_response.py). The tokenized
    `download_url` in the call record is the alternative for systems that need
    a credential-free URL to hand to a browser.
    """
    ctx = require(request, response, "recordings:read")
    row = _call_row(ctx.owner, call_id)
    from recording_response import stream_recording
    result = stream_recording(row.get("call_key") or "", row.get("gcs_object"),
                              bool(download))
    if isinstance(result, JSONResponse) and result.status_code == 404:
        raise ApiError(404, err.NOT_FOUND, "This call has no recording.")
    for k, v in ctx.headers().items():
        result.headers[k] = v
    return result


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

def _audit(ctx, action: str, target: str | None = None, meta: dict | None = None) -> None:
    """API writes land in the same audit trail as dashboard writes, tagged with
    the key that made them — "who added this contact" must be answerable when
    the answer is a machine."""
    try:
        from shared.audit import record
        record(ctx.owner, action, target,
               {**(meta or {}), "key_id": ctx.key.get("key_id"),
                "key_name": ctx.key.get("name")}, ip=ctx.ip)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# mounting
# ---------------------------------------------------------------------------

def _envelope(status: int, code: str, message: str,
              headers: dict | None = None) -> JSONResponse:
    request_id = err.new_request_id()
    return JSONResponse(
        {"error": {"code": code, "message": message, "request_id": request_id}},
        status_code=status, headers={**(headers or {}), "X-Request-Id": request_id})


def install(app) -> None:
    """Mount /v1 with a complete error surface.

    A function rather than a bare router because EVERY way this API can fail
    has to produce the same envelope, and most of those ways are decided
    outside our handlers: an unknown path, a wrong method, a query parameter
    FastAPI rejects before the handler runs, an unhandled exception. A CRM that
    branches on `error.code` breaks the moment one route answers in a different
    shape — so the shape is enforced here, once, for the whole namespace.
    """
    from fastapi.exceptions import RequestValidationError
    from fastapi.exception_handlers import request_validation_exception_handler
    from starlette.responses import PlainTextResponse

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        request_id = getattr(request.state, "api_request_id", "") or err.new_request_id()
        if exc.status >= 500:
            logger.error("[api] %s %s -> %s %s", request.method, request.url.path,
                         exc.status, exc.code)
        return JSONResponse(exc.body(request_id), status_code=exc.status,
                            headers={**exc.headers, "X-Request-Id": request_id})

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        """FastAPI's own 422 body is a nested list of pydantic error objects —
        a second error shape, and one no integration guide documents. Under
        /v1 it becomes the same envelope as everything else, naming the field
        that was wrong."""
        if not _is_v1(request):
            return await request_validation_exception_handler(request, exc)
        first = (exc.errors() or [{}])[0]
        where = ".".join(str(p) for p in (first.get("loc") or [])[1:]) or "request"
        return _envelope(400, err.INVALID_REQUEST,
                         f"`{where}` is invalid: {first.get('msg', 'check the value')}.")

    @app.middleware("http")
    async def _v1_error_envelope(request: Request, call_next):
        """The catch-all. Non-/v1 traffic passes through untouched — this is a
        shared app and the dashboard's own error handling must not change."""
        if not _is_v1(request):
            return await call_next(request)
        try:
            response = await call_next(request)
        except Exception:
            # Logged in full here, described in one sentence to the caller: a
            # database URL or a stack frame in an HTTP body is a gift to
            # whoever is probing us.
            logger.exception("[api] unhandled error on %s %s",
                             request.method, request.url.path)
            return _envelope(500, "internal_error",
                             "Something went wrong on our side. Retry shortly, and "
                             "quote the request id if it persists.")
        # 404 / 405 are produced by the router itself, before any handler runs.
        # Ours already carry X-Request-Id, so that header is the marker for
        # "already an envelope".
        if response.status_code in (404, 405) and "X-Request-Id" not in response.headers:
            if response.status_code == 405:
                return _envelope(405, err.INVALID_REQUEST,
                                 f"{request.method} is not supported on this endpoint.")
            return _envelope(404, err.NOT_FOUND, "No such endpoint or record.")
        return response

    app.include_router(router)
    _install_openapi(app)


def _is_v1(request: Request) -> bool:
    path = request.url.path
    return path == "/v1" or path.startswith("/v1/")


def _install_openapi(app) -> None:
    @app.get("/v1/openapi.json", include_in_schema=False)
    def openapi_spec():
        from public_api_spec import build_spec
        return JSONResponse(build_spec(base_url()))
