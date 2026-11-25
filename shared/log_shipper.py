"""Ship service logs to GCS (deploy/PLAN-minutes-docker-logging.md §3).

Collects log files matching LOG_SHIP_GLOBS (relative to the repo root),
skips files written to in the last IDLE_MIN minutes (they're still hot),
gzips each and uploads to

    gs://$GCS_LOGS_BUCKET/logs/<YYYY-MM>/<relative__path>.gz

Idempotent: the shipped_logs table remembers (path, mtime, size); unchanged
files are skipped, changed files are re-uploaded over the same object so the
bucket always has the latest full copy. Retention is the bucket's lifecycle
rule (90 days), not this script's concern.

CLI: python -m shared.log_shipper --once | --interval 3600
"""

from __future__ import annotations

import glob
import gzip
import io
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("log_shipper")

_REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_GLOBS = (
    "phase14_runtime/logs/*/*.log",
    "dashboard/logs/requests.jsonl*",
    "dashboard/dashboard.log",
    "credit_log/*.jsonl",
)
IDLE_MIN = float(os.environ.get("LOG_SHIP_IDLE_MIN", "30"))
MAX_BYTES = int(os.environ.get("LOG_SHIP_MAX_MB", "512")) * 1024 * 1024


def bucket_name() -> str:
    return os.environ.get("GCS_LOGS_BUCKET", "").strip()


def enabled() -> bool:
    return bool(bucket_name())


def _globs() -> list[str]:
    env = os.environ.get("LOG_SHIP_GLOBS", "").strip()
    return env.split(":") if env else list(DEFAULT_GLOBS)


def _object_name(rel: str, mtime: float) -> str:
    month = time.strftime("%Y-%m", time.gmtime(mtime))
    return f"logs/{month}/{rel.replace('/', '__')}.gz"


def ship_once() -> dict:
    stats = {"shipped": 0, "skipped": 0, "failed": 0}
    if not enabled():
        logger.info("GCS_LOGS_BUCKET unset — log shipping disabled")
        return stats

    from google.cloud import storage
    from shared.db.database import get_db

    client = storage.Client()
    bucket = client.bucket(bucket_name())
    cutoff = time.time() - IDLE_MIN * 60

    candidates: list[Path] = []
    for pattern in _globs():
        for p in glob.glob(str(_REPO_ROOT / pattern)):
            path = Path(p)
            if path.is_file():
                candidates.append(path)

    with get_db() as db:
        state = {r["path"]: (r["mtime"], r["size"]) for r in db.execute(
            "SELECT path, mtime, size FROM shipped_logs").fetchall()}

    for path in candidates:
        try:
            st = path.stat()
        except OSError:
            continue
        rel = str(path.relative_to(_REPO_ROOT))
        if st.st_mtime > cutoff or st.st_size == 0 or st.st_size > MAX_BYTES:
            stats["skipped"] += 1
            continue
        prev = state.get(rel)
        if prev and abs(prev[0] - st.st_mtime) < 1 and prev[1] == st.st_size:
            stats["skipped"] += 1
            continue
        try:
            buf = io.BytesIO()
            with open(path, "rb") as f, gzip.GzipFile(
                    fileobj=buf, mode="wb", mtime=st.st_mtime) as gz:
                while chunk := f.read(1024 * 1024):
                    gz.write(chunk)
            buf.seek(0)
            blob = bucket.blob(_object_name(rel, st.st_mtime))
            blob.upload_from_file(buf, content_type="application/gzip")
            with get_db() as db:
                db.execute(
                    "INSERT INTO shipped_logs (path, mtime, size) VALUES (?,?,?) "
                    "ON CONFLICT (path) DO UPDATE SET mtime=excluded.mtime, "
                    "size=excluded.size, shipped_at=CURRENT_TIMESTAMP",
                    (rel, st.st_mtime, st.st_size))
            stats["shipped"] += 1
            logger.info("shipped %s -> gs://%s/%s", rel, bucket_name(),
                        blob.name)
        except Exception as exc:
            stats["failed"] += 1
            logger.warning("ship failed for %s: %r", rel, exc)

    logger.info("log ship pass: %s", stats)
    return stats


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.interval > 0 and not args.once:
        while True:
            try:
                ship_once()
            except Exception:
                logger.exception("ship pass failed")
            time.sleep(args.interval)
    else:
        ship_once()


if __name__ == "__main__":
    main()
