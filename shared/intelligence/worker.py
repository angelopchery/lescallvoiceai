"""Intelligence worker — long-lived job consumer (lescall-workers container).

Runs OUTSIDE the sequential 5-minute workers tick so slow LLM jobs can never
starve billing reconcile (see deploy/workers_entry.sh: this process is
launched alongside the loop, not inside it).

Responsibilities:
    * claim + execute intel_jobs (asyncio, small concurrency)
    * sweeper: enqueue missing calls (the every-call guarantee + backfill)
    * crash recovery: release stale 'running' jobs
    * liveness: 'intel: beat ...' log line + settings heartbeat row

Env bootstrap: deploy/.pg_env comes from the container env_file; the LLM key
lives in calling-agent/.env on the bind mount, loaded here without overriding
anything already set in the environment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("intel.worker")


def _load_env_file(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines); never overrides existing env."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except FileNotFoundError:
        pass


_load_env_file(REPO_ROOT / "calling-agent" / ".env")

from shared.intelligence import config, pipeline, queue  # noqa: E402  (env first)

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
_shutdown = asyncio.Event()


def _beat(extra: dict | None = None) -> None:
    """Liveness: log line (greppable) + settings row (dashboard-visible)."""
    counts = {}
    try:
        counts = queue.counts()
    except Exception:
        pass
    log.info("intel: beat worker=%s queue=%s %s", WORKER_ID, counts, extra or "")
    try:
        from shared.db.database import get_db

        import datetime as _dt

        with get_db() as db:
            # A real timestamp, and updated_at bumped on every upsert: the
            # old payload wrote the literal string "now" and updated_at only
            # got its INSERT default, so a dead worker was indistinguishable
            # from a live one — the dashboard would show a fresh-looking
            # beat forever.
            db.execute(
                """INSERT INTO settings (owner, key, value)
                   VALUES ('', 'intel_worker_beat', ?)
                   ON CONFLICT (owner, key)
                   DO UPDATE SET value=EXCLUDED.value, updated_at=now()""",
                (json.dumps({
                    "worker": WORKER_ID,
                    "at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    "queue": counts,
                }),),
            )
    except Exception:
        pass


async def _run_job(job: dict) -> None:
    kind = job["kind"]
    try:
        if kind == queue.KIND_CALL_PROCESS:
            await pipeline.process_call(job)
        elif kind == queue.KIND_REPORT_GENERATE:
            from shared.intelligence import reports

            await reports.generate(job)
        elif kind == queue.KIND_AUTOMATION_RUN:
            from shared.intelligence import automation

            await automation.execute_run(job)
        else:
            raise ValueError(f"unknown job kind {kind!r}")
        await asyncio.to_thread(queue.complete, job["id"])
        log.info("intel: job %s (%s) done", job["id"], kind)
    except Exception as exc:
        status = await asyncio.to_thread(queue.fail, job["id"], repr(exc))
        log.warning("intel: job %s (%s) failed -> %s: %r", job["id"], kind, status, exc)


async def _consumer_loop() -> None:
    sem = asyncio.Semaphore(config.WORKER_CONCURRENCY)
    pending: set[asyncio.Task] = set()
    while not _shutdown.is_set():
        await sem.acquire()
        job = await asyncio.to_thread(queue.claim, WORKER_ID)
        if job is None:
            sem.release()
            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=config.POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
            continue

        async def _wrapped(j=job):
            try:
                await _run_job(j)
            finally:
                sem.release()

        task = asyncio.create_task(_wrapped())
        pending.add(task)
        task.add_done_callback(pending.discard)
    if pending:
        log.info("intel: draining %d in-flight job(s)", len(pending))
        await asyncio.gather(*pending, return_exceptions=True)


_dial_outcomes_last = 0.0


async def _sweeper_loop() -> None:
    global _dial_outcomes_last
    import time as _time

    while not _shutdown.is_set():
        try:
            released = await asyncio.to_thread(queue.release_stale, 900)
            enqueued = await asyncio.to_thread(pipeline.sweep_missing, 25)
            from shared.intelligence import followup

            overdue = await asyncio.to_thread(followup.sweep_overdue)
            # Failed-dial outcome classification (CDR read-alongside) on its
            # own cadence; a failure here never disturbs the sweep proper.
            if (config.DIAL_OUTCOMES_ENABLED
                    and _time.monotonic() - _dial_outcomes_last
                    >= config.DIAL_OUTCOMES_INTERVAL_S):
                _dial_outcomes_last = _time.monotonic()
                try:
                    from shared.intelligence import dial_outcomes

                    await asyncio.to_thread(dial_outcomes.classify_recent)
                except Exception as exc:
                    log.warning("intel: dial-outcome pass error: %r", exc)
            if released or enqueued or overdue:
                log.info("intel: sweep released=%d enqueued=%d overdue=%d",
                         released, enqueued, overdue)
            _beat({"released": released, "enqueued": enqueued, "overdue": overdue})
        except Exception as exc:
            log.warning("intel: sweep error: %r", exc)
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=config.SWEEP_INTERVAL_S)
        except asyncio.TimeoutError:
            pass


async def main() -> int:
    if not config.ENABLED:
        log.info("intel: INTEL_ENABLED=false — worker idle (sleeping)")
        await _shutdown.wait()
        return 0
    if not queue.is_pg():
        log.error("intel: LESCALL_DB_URL is not Postgres — worker exiting")
        return 1
    if not config.LLM_API_KEY:
        log.error("intel: no LLM API key (INTEL_LLM_API_KEY / LLM_API_KEY) — worker exiting")
        return 1

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown.set)

    log.info("intel: worker %s starting (model=%s, concurrency=%d)",
             WORKER_ID, config.LLM_MODEL, config.WORKER_CONCURRENCY)
    _beat()
    await asyncio.gather(_consumer_loop(), _sweeper_loop())
    log.info("intel: worker stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
