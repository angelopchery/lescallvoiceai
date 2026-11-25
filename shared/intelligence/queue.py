"""intel_jobs Postgres job queue — the event backbone of the pipeline.

Semantics:
    * at-least-once delivery; handlers must be idempotent
    * idempotent enqueue via dedupe_key (ON CONFLICT DO NOTHING)
    * concurrent-safe claims via FOR UPDATE SKIP LOCKED
    * exponential backoff (BACKOFF_BASE_S * 2^attempts, capped), then a
      dead-letter state (status='failed') that is re-drivable via requeue()

Postgres-only: on the sqlite dev fallback enqueue() returns None and claim()
returns nothing, so callers degrade to no-ops without special-casing.

All functions open their own short-lived connection via get_db() — the same
discipline as shared/outbox.py — so they are safe to call from any process.
"""

from __future__ import annotations

import json
from typing import Any

from shared.db.database import db_url, get_db
from shared.intelligence import config

# Job kinds
KIND_CALL_PROCESS = "call.process"
KIND_REPORT_GENERATE = "report.generate"
KIND_AUTOMATION_RUN = "automation.run"

TERMINAL_STATUSES = ("done", "failed", "cancelled")


def is_pg() -> bool:
    return db_url().startswith(("postgres://", "postgresql://"))


def backoff_seconds(attempts: int) -> int:
    return min(config.BACKOFF_BASE_S * (2 ** max(attempts - 1, 0)), config.BACKOFF_CAP_S)


def enqueue(
    kind: str,
    *,
    owner: str = "",
    call_id: int | None = None,
    dedupe_key: str | None = None,
    payload: dict[str, Any] | None = None,
    delay_s: int = 0,
    max_attempts: int = 5,
) -> int | None:
    """Insert a job; returns its id, or None if deduped / not on Postgres."""
    if not is_pg():
        return None
    with get_db() as db:
        row = db.execute(
            """INSERT INTO intel_jobs
                   (kind, owner, call_id, dedupe_key, payload, max_attempts,
                    next_attempt_at)
               VALUES (?, ?, ?, ?, ?, ?, now() + make_interval(secs => ?))
               ON CONFLICT (dedupe_key) DO NOTHING
               RETURNING id""",
            (kind, owner, call_id, dedupe_key,
             json.dumps(payload or {}), max_attempts, delay_s),
        ).fetchone()
        return row["id"] if row else None


def claim(worker_id: str, kinds: tuple[str, ...] | None = None) -> dict[str, Any] | None:
    """Atomically claim the oldest ready job (attempts incremented at claim)."""
    if not is_pg():
        return None
    kind_filter = ""
    params: list[Any] = []
    if kinds:
        kind_filter = " AND kind = ANY(?)"
        params.append(list(kinds))
    with get_db() as db:
        row = db.execute(
            f"""UPDATE intel_jobs
                   SET status='running', claimed_by=?, claimed_at=now(),
                       attempts=attempts+1
                 WHERE id = (SELECT id FROM intel_jobs
                              WHERE status='pending' AND next_attempt_at <= now()
                              {kind_filter}
                              ORDER BY id
                              LIMIT 1
                              FOR UPDATE SKIP LOCKED)
             RETURNING id, kind, owner, call_id, payload, attempts, max_attempts""",
            (worker_id, *params),
        ).fetchone()
        if not row:
            return None
        job = dict(row)
        job["payload"] = json.loads(job.get("payload") or "{}")
        return job


def complete(job_id: int) -> None:
    with get_db() as db:
        db.execute(
            "UPDATE intel_jobs SET status='done', finished_at=now(), last_error=NULL"
            " WHERE id=?",
            (job_id,),
        )


def fail(job_id: int, error: str, *, retry: bool = True) -> str:
    """Record a failure; reschedule with backoff or dead-letter. Returns the
    resulting status ('pending' or 'failed')."""
    error = (error or "")[:2000]
    with get_db() as db:
        row = db.execute(
            "SELECT attempts, max_attempts FROM intel_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not row:
            return "failed"
        if retry and row["attempts"] < row["max_attempts"]:
            db.execute(
                """UPDATE intel_jobs
                       SET status='pending', last_error=?,
                           next_attempt_at = now() + make_interval(secs => ?)
                     WHERE id=?""",
                (error, backoff_seconds(row["attempts"]), job_id),
            )
            return "pending"
        db.execute(
            "UPDATE intel_jobs SET status='failed', last_error=?, finished_at=now()"
            " WHERE id=?",
            (error, job_id),
        )
        return "failed"


def requeue(job_id: int) -> bool:
    """Re-drive a job immediately: dead-lettered/stuck ones, or 'done' jobs
    that need to re-run because a new pipeline stage shipped after them."""
    with get_db() as db:
        cur = db.execute(
            """UPDATE intel_jobs
                   SET status='pending', attempts=0, next_attempt_at=now(),
                       last_error=NULL, finished_at=NULL
                 WHERE id=? AND status IN ('failed', 'cancelled', 'running', 'done')""",
            (job_id,),
        )
        return bool(getattr(cur, "rowcount", 0))


def release_stale(older_than_s: int = 900) -> int:
    """Crash recovery: return long-'running' jobs (dead worker) to the queue.
    Attempts were already counted at claim, so repeated crashes still converge
    to the dead-letter state."""
    with get_db() as db:
        cur = db.execute(
            """UPDATE intel_jobs
                   SET status='pending', next_attempt_at=now()
                 WHERE status='running'
                   AND claimed_at < now() - make_interval(secs => ?)
                   AND attempts < max_attempts""",
            (older_than_s,),
        )
        dead = db.execute(
            """UPDATE intel_jobs
                   SET status='failed', finished_at=now(),
                       last_error=COALESCE(last_error, 'worker died mid-job')
                 WHERE status='running'
                   AND claimed_at < now() - make_interval(secs => ?)
                   AND attempts >= max_attempts""",
            (older_than_s,),
        )
        return (getattr(cur, "rowcount", 0) or 0) + (getattr(dead, "rowcount", 0) or 0)


def counts() -> dict[str, int]:
    """Queue depth by status — the /api/intel/status surface."""
    if not is_pg():
        return {}
    with get_db() as db:
        rows = db.execute(
            "SELECT status, count(*) AS n FROM intel_jobs GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}
