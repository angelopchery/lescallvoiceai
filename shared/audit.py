"""Semantic action audit trail — one row per meaningful user action.

record() never raises: auditing must not be able to break the action it
describes. The generic request log (every API hit) lives in the dashboard's
request middleware; this table is for the events a human will ask about
later ("who deleted that agent?", "when was this number attached?").
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("audit")


def record(owner: str | None, action: str, target: str | None = None,
           meta: dict | None = None, ip: str | None = None) -> None:
    try:
        from shared.db.database import get_db
        with get_db() as db:
            db.execute(
                "INSERT INTO audit_events (owner, action, target, meta, ip) "
                "VALUES (?,?,?,?,?)",
                (owner, action, target,
                 json.dumps(meta, ensure_ascii=False, default=str) if meta else None,
                 ip))
    except Exception as exc:
        logger.warning("audit record failed (%s %s): %r", action, target, exc)
