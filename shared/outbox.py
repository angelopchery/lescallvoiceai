"""Reliable to-send tray for external hand-offs (Part B Phase 2).

Qualified leads (or any external hand-off) become rows in the `outbox` table
(shared/db/schema.sql). `enqueue()` is idempotent via `dedupe_key` — the same
lead enqueued twice yields one row. `deliver_pending()` drains due pending rows
through a sender callable, recording sent / duplicate / retry-with-backoff /
failed, so a crash or ERP outage never loses a lead.

The default sender POSTs JSON to LESCALL_ERP_WEBHOOK_URL; when that env var is
unset it logs the payload and reports success ("log-only mode"), so the
pipeline can run before the client's ERP endpoint exists. Summaries flag this
via "log_only": true.

All timestamps are UTC ISO strings ("YYYY-MM-DDTHH:MM:SS"); every value this
module writes or compares goes through the same formatter, so SQLite's
lexicographic TIMESTAMP comparison in the due-rows query stays correct.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from shared.db.database import get_db

log = logging.getLogger("lescall.outbox")

DEFAULT_KIND = "erp_lead"
MAX_ATTEMPTS = 8          # attempts >= this after a failure -> status 'failed'
BACKOFF_CAP_S = 3600      # backoff = min(2**attempts * 60, BACKOFF_CAP_S)
HTTP_TIMEOUT_S = 10

WEBHOOK_URL_ENV = "LESCALL_ERP_WEBHOOK_URL"
WEBHOOK_TOKEN_ENV = "LESCALL_ERP_WEBHOOK_TOKEN"


# --------------------------------------------------------------------------
# time helpers — `now` is injectable (ISO string or datetime) for tests
# --------------------------------------------------------------------------

def _coerce_now(now: str | datetime | None) -> datetime:
    """Normalize `now` to a naive UTC datetime."""
    if now is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if isinstance(now, str):
        now = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    return now


def _iso(dt: datetime) -> str:
    return dt.isoformat(sep="T", timespec="seconds")


# --------------------------------------------------------------------------
# enqueue
# --------------------------------------------------------------------------

def default_dedupe_key(payload: dict, kind: str = DEFAULT_KIND,
                       owner: str = "") -> str:
    """owner+kind-namespaced sha256 of the canonical JSON form of the payload.

    Owner is part of the key so two tenants enqueueing byte-identical payloads
    get two rows — dedupe is per-tenant, never cross-tenant.
    """
    canonical = json.dumps(payload, sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{kind}:{owner}:{digest}"


def enqueue(payload: dict, *, owner: str, kind: str = DEFAULT_KIND,
            dedupe_key: str | None = None, url: str | None = None) -> dict:
    """Add one hand-off to the outbox. Idempotent by dedupe_key.

    Returns {"id": <row id or None>, "status": "enqueued" | "duplicate_enqueue"}.
    Re-enqueuing the same payload returns the existing row's id and does NOT
    create a second row. Caller-supplied dedupe_keys are owner-namespaced for
    the same reason the default is.
    """
    if dedupe_key is None:
        dedupe_key = default_dedupe_key(payload, kind, owner)
    else:
        dedupe_key = f"{owner}:{dedupe_key}"
    with get_db(url) as db:
        cur = db.execute(
            "INSERT INTO outbox (owner, kind, dedupe_key, payload) "
            "VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
            (owner, kind, dedupe_key, json.dumps(payload, sort_keys=True)))
        inserted = cur.rowcount == 1
        row = db.execute("SELECT id FROM outbox WHERE dedupe_key=?",
                         (dedupe_key,)).fetchone()
        return {"id": row["id"] if row else None,
                "status": "enqueued" if inserted else "duplicate_enqueue"}


# --------------------------------------------------------------------------
# delivery
# --------------------------------------------------------------------------

def default_sender(kind: str, payload: dict, dedupe_key: str = "") -> str:
    """POST the payload as JSON to LESCALL_ERP_WEBHOOK_URL.

    Returns "sent" on HTTP 2xx, "duplicate" on HTTP 409 or a response body
    containing "duplicate"; raises on anything else. When the env var is
    unset, logs the payload at INFO and returns "sent" (log-only mode).

    Delivery is at-least-once (a crash between a successful POST and the DB
    update replays the row), so the row's dedupe_key rides along as an
    Idempotency-Key header + body field, letting the receiver deduplicate.
    """
    webhook_url = os.environ.get(WEBHOOK_URL_ENV)
    if not webhook_url:
        log.info("outbox log-only delivery kind=%s payload=%s",
                 kind, json.dumps(payload, sort_keys=True))
        return "sent"

    headers = {"Content-Type": "application/json"}
    if dedupe_key:
        headers["Idempotency-Key"] = dedupe_key
    token = os.environ.get(WEBHOOK_TOKEN_ENV)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps({"kind": kind, "payload": payload,
                       "dedupe_key": dedupe_key}).encode("utf-8")
    req = urllib.request.Request(webhook_url, data=body, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = getattr(resp, "status", resp.getcode())
            if 200 <= status < 300:
                return "duplicate" if "duplicate" in raw.lower() else "sent"
            raise RuntimeError(f"unexpected HTTP status {status}: {raw[:200]}")
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        if exc.code == 409 or "duplicate" in raw.lower():
            return "duplicate"
        raise


def deliver_pending(sender=None, *, url: str | None = None,
                    now: str | datetime | None = None,
                    max_batch: int = 50) -> dict:
    """Deliver due pending outbox rows through `sender(kind, payload_dict)`.

    The sender must return "sent" or "duplicate", or raise on failure.
    On success/duplicate the row is finalized (status + sent_at). On failure
    the row stays pending with exponential backoff in next_attempt_at, until
    MAX_ATTEMPTS is reached and it is marked 'failed'.

    Returns a summary dict:
        {"selected", "sent", "duplicate", "retry", "failed", "log_only", "now"}
    """
    log_only = sender is None and not os.environ.get(WEBHOOK_URL_ENV)
    if sender is None:
        sender = default_sender
    # Decide ONCE whether the sender takes the idempotency key — never probe
    # by retrying on TypeError, which could double-send.
    import inspect
    try:
        params = inspect.signature(sender).parameters
        accepts_key = ("dedupe_key" in params
                       or any(p.kind is inspect.Parameter.VAR_KEYWORD
                              for p in params.values()))
    except (TypeError, ValueError):
        accepts_key = False
    now_iso = _iso(_coerce_now(now))

    summary = {"selected": 0, "sent": 0, "duplicate": 0, "retry": 0,
               "failed": 0, "log_only": log_only, "now": now_iso}
    with get_db(url) as db:
        rows = db.execute(
            "SELECT id, kind, payload, attempts, dedupe_key FROM outbox "
            "WHERE status='pending' "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
            "ORDER BY id LIMIT ?",
            (now_iso, max_batch)).fetchall()
        summary["selected"] = len(rows)

        for row in rows:
            attempts = (row["attempts"] or 0) + 1
            try:
                payload = json.loads(row["payload"])
                # Pass the idempotency key to senders that accept it; plain
                # 2-arg senders (tests, custom integrations) keep working.
                if accepts_key:
                    result = sender(row["kind"], payload,
                                    dedupe_key=row["dedupe_key"] or "")
                else:
                    result = sender(row["kind"], payload)
                if result not in ("sent", "duplicate"):
                    raise ValueError(
                        f"sender returned unexpected result: {result!r}")
            except Exception as exc:
                backoff_s = min(2 ** attempts * 60, BACKOFF_CAP_S)
                retry_at = _iso(_coerce_now(now_iso) + timedelta(seconds=backoff_s))
                new_status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
                db.execute(
                    "UPDATE outbox SET attempts=?, last_error=?, "
                    "next_attempt_at=?, status=? WHERE id=?",
                    (attempts, repr(exc)[:500], retry_at, new_status, row["id"]))
                summary["failed" if new_status == "failed" else "retry"] += 1
                log.warning("outbox row %s attempt %s %s: %r",
                            row["id"], attempts, new_status, exc)
            else:
                db.execute(
                    "UPDATE outbox SET status=?, sent_at=?, attempts=?, "
                    "last_error=NULL, next_attempt_at=NULL WHERE id=?",
                    (result, now_iso, attempts, row["id"]))
                summary[result] += 1
            db.commit()  # per-row commit: a crash mid-batch loses nothing
    return summary
