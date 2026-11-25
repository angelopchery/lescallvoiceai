"""Replay protection for API writes.

A CRM that pushes a contact, times out waiting for our response and retries
must end up with ONE contact, not two. The client sends `Idempotency-Key: <its
own uuid>`; the first request stores its response, and any repeat of the same
key replays that stored response verbatim rather than re-running the write.

The stored `request_hash` pins a key to its payload. Reusing one key for a
different body is a client bug that would otherwise silently hand back the
wrong contact's id, so it is refused loudly (409) instead.

Scoped per API key, not per tenant: two integrations at one customer can pick
the same uuid without colliding, and a leaked key cannot be used to probe what
another key has been sending.
"""

from __future__ import annotations

import hashlib
import json
import logging

from shared.public_api import errors as err
from shared.public_api.errors import ApiError

logger = logging.getLogger("public_api.idempotency")

MAX_KEY_LEN = 200


def body_hash(endpoint: str, body) -> str:
    payload = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{endpoint}\n{payload}".encode("utf-8")).hexdigest()


def lookup(key_id: str, idem_key: str, endpoint: str, request_hash: str):
    """The stored (status, body) for a repeat, or None for a first attempt."""
    if not idem_key:
        return None
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM api_idempotency WHERE key_id=? AND idem_key=?",
                (key_id, idem_key[:MAX_KEY_LEN])).fetchone()
    except Exception as exc:
        logger.warning("[api] idempotency lookup failed: %r", exc)
        return None            # degrade to "not seen"; the write is re-run
    if not row:
        return None
    row = dict(row)
    if row.get("request_hash") != request_hash:
        raise ApiError(409, err.IDEMPOTENCY_MISMATCH,
                       "This Idempotency-Key was already used with a different "
                       "request body. Use a new key for a new request.")
    try:
        return int(row["status_code"]), json.loads(row["response"])
    except Exception:
        return None


def store(key_id: str, idem_key: str, endpoint: str, request_hash: str,
          status_code: int, response) -> None:
    if not idem_key:
        return
    try:
        from shared.db.database import get_db
        with get_db() as db:
            db.execute(
                "INSERT INTO api_idempotency (key_id, idem_key, endpoint, "
                " request_hash, status_code, response) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT (key_id, idem_key) DO NOTHING",
                (key_id, idem_key[:MAX_KEY_LEN], endpoint[:200], request_hash,
                 int(status_code), json.dumps(response, ensure_ascii=False, default=str)))
    except Exception as exc:
        logger.warning("[api] idempotency store failed: %r", exc)
