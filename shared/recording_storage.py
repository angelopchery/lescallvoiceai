"""Call-recording storage: GCS sync + unguessable public download links.

Every finished call gets a `download_token` — 32 bytes from `secrets`
(256-bit, URL-safe). The public link is
    {PUBLIC_BASE_URL}/api/recordings/dl/{token}
and the token is the ONLY credential: it is never derived from the call key,
phone number, or date, so one leaked link cannot be edited into another
call's link.

The WAV is uploaded to a private GCS bucket after the call ends (and again by
the `sync_pending` catch-up pass for anything the inline attempt missed).
Once uploaded, the local copy is only kept for RECORDING_LOCAL_RETENTION_DAYS
and then deleted by `cleanup_local`; downloads transparently fall back to the
GCS object when the local file is gone.

Config (deploy/.pg_env — shared by lescall-runtime and lescall-dashboard):
    GCS_RECORDINGS_BUCKET=lescall-recordings     # empty → uploads disabled
    GCS_RECORDINGS_PREFIX=recordings
    PUBLIC_BASE_URL=https://agent.lescall.com
    RECORDING_LOCAL_RETENTION_DAYS=3

Google auth is the VM's attached service account, read from the metadata
server — no key file, no user login, nothing that expires. Never set
GOOGLE_APPLICATION_CREDENTIALS here: an explicit path outranks metadata ADC,
which is how a stale user token silently stalled uploads for 17 days in July
2026. See RUNBOOK §6b.

CLI (catch-up sync + retention cleanup, cron/systemd-timer friendly):
    python -m shared.recording_storage --once
    python -m shared.recording_storage --interval 900
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import Path

logger = logging.getLogger("recording_storage")

_REPO_ROOT = Path(__file__).resolve().parent.parent
RECORDINGS_DIR = Path(os.environ.get("CALL_AUDIT_DIR", _REPO_ROOT / "recordings"))

AUDIO_FILENAME = "caller_audio.wav"
# The full two-party recording (agent + caller), captured from FreeSWITCH's
# record_session WAV at call teardown (see capture_fs_recording). Serving
# prefers this file; caller_audio.wav stays as the STT-side debug artifact.
FULL_AUDIO_FILENAME = "call_audio.wav"
# Where FreeSWITCH's record_session writes (one <channel-uuid>.wav per call).
# Deliberately /tmp: writable by the freeswitch user without permission
# games; files are copied out seconds after hangup, and a janitor in
# cleanup_local prunes stragglers.
FS_RECORDINGS_DIR = Path(os.environ.get("FS_RECORDING_DIR", "/tmp/esl_recordings"))
# Small sidecar files sync too so the bucket holds the complete call artifact.
SIDECAR_FILENAMES = ("transcript.txt", "metadata.json")


def bucket_name() -> str:
    return os.environ.get("GCS_RECORDINGS_BUCKET", "").strip()


def gcs_enabled() -> bool:
    return bool(bucket_name())


def public_base_url() -> str:
    return os.environ.get(
        "PUBLIC_BASE_URL", "https://agent.lescall.com").rstrip("/")


def retention_days() -> float:
    try:
        return float(os.environ.get("RECORDING_LOCAL_RETENTION_DAYS", "3"))
    except ValueError:
        return 3.0


def new_download_token() -> str:
    """256-bit URL-safe random token (43 chars). Collision/guess-proof."""
    return secrets.token_urlsafe(32)


def public_download_url(token: str) -> str:
    return f"{public_base_url()}/api/recordings/dl/{token}"


def local_audio_path(call_key: str) -> Path:
    return RECORDINGS_DIR / call_key / AUDIO_FILENAME


def local_full_audio_path(call_key: str) -> Path:
    return RECORDINGS_DIR / call_key / FULL_AUDIO_FILENAME


def capture_fs_recording(conn_id: str, call_key: str,
                         wait_s: float = 4.0) -> bool:
    """Copy FreeSWITCH's two-party record_session WAV into the call folder.

    Copy-overwrite, NEVER delete the source: on a mid-call WS reconnect the
    session teardown runs while FS is still recording the channel, so an
    early copy may be partial — the final teardown's copy simply overwrites
    it with the full file. Stragglers in FS_RECORDINGS_DIR are pruned by the
    cleanup_local janitor. Never raises; returns True when a copy landed.
    """
    import shutil

    if not (conn_id and call_key):
        return False
    src = FS_RECORDINGS_DIR / f"{conn_id}.wav"
    dst_dir = RECORDINGS_DIR / call_key
    try:
        # Wait briefly for FS to finalize the header after stop_record_session
        # (size-stability check; ~700ms is the observed drain window).
        last_size = -1
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if src.exists():
                size = src.stat().st_size
                if size > 44 and size == last_size:
                    break
                last_size = size
            time.sleep(0.3)
        if not src.exists() or src.stat().st_size <= 44:
            return False
        if not dst_dir.is_dir():
            return False
        shutil.copy2(src, dst_dir / FULL_AUDIO_FILENAME)
        logger.info("captured full recording for %s (%d bytes)",
                    call_key, src.stat().st_size)
        return True
    except Exception as exc:
        logger.warning("full-recording capture failed for %s: %r", call_key, exc)
        return False


# ---------------------------------------------------------------------------
# GCS client (lazy; ADC or GOOGLE_APPLICATION_CREDENTIALS)
# ---------------------------------------------------------------------------

_bucket_cache = None


def _bucket():
    global _bucket_cache
    if _bucket_cache is not None:
        return _bucket_cache
    from google.cloud import storage  # deferred: optional dependency

    client = storage.Client()
    _bucket_cache = client.bucket(bucket_name())
    return _bucket_cache


def _object_name(call_key: str, filename: str) -> str:
    prefix = os.environ.get("GCS_RECORDINGS_PREFIX", "recordings").strip("/")
    return f"{prefix}/{call_key}/{filename}"


def open_gcs_stream(gcs_object: str):
    """Readable binary file-like over a bucket object (for download fallback)."""
    return _bucket().blob(gcs_object).open("rb")


def probe_bucket() -> str | None:
    """Cheap auth+reachability probe. Returns an error string, or None if OK.

    The catch-up sync used to report `uploaded=0` and exit 0 while its GCS
    client could not authenticate at all: `sync_pending` only touches GCS for
    rows that still have a local WAV, so a pass with no such rows never
    exercised auth. That is how a dead credential stayed invisible for weeks
    (2026-08-07: the stale `~/.config/gcloud` ADC file outranked metadata for
    every host process). Probe on EVERY pass, work or not.

    Uses objects.list (one object, discarded) rather than buckets.get: the
    attached SA is an object-level role, so `bucket.exists()` can 403 on a
    bucket it can read and write perfectly well.
    """
    if not gcs_enabled():
        return "GCS_RECORDINGS_BUCKET unset"
    try:
        client = _bucket().client
        next(iter(client.list_blobs(bucket_name(), max_results=1)), None)
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Upload + DB bookkeeping
# ---------------------------------------------------------------------------

def upload_recording(call_key: str) -> bool:
    """Upload one call's artifacts to GCS and stamp the calls row.

    Returns True when the audio object is in the bucket (now or already).
    Never raises — the call path treats upload as best-effort; the sync CLI
    retries anything unstamped.
    """
    if not call_key:
        return False
    if not gcs_enabled():
        logger.debug("GCS_RECORDINGS_BUCKET unset; skipping upload of %s", call_key)
        return False
    wav = local_audio_path(call_key)
    if not wav.exists():
        logger.warning("no local audio for %s; nothing to upload", call_key)
        return False
    try:
        bucket = _bucket()
        audio_obj = _object_name(call_key, AUDIO_FILENAME)
        blob = bucket.blob(audio_obj)
        if not blob.exists():
            blob.upload_from_filename(str(wav), content_type="audio/wav")
        full = local_full_audio_path(call_key)
        if full.exists():
            fblob = bucket.blob(_object_name(call_key, FULL_AUDIO_FILENAME))
            if not fblob.exists():
                fblob.upload_from_filename(str(full), content_type="audio/wav")
        for name in SIDECAR_FILENAMES:
            side = RECORDINGS_DIR / call_key / name
            if side.exists():
                sblob = bucket.blob(_object_name(call_key, name))
                if not sblob.exists():
                    sblob.upload_from_filename(str(side))
        _stamp_uploaded(call_key, audio_obj)
        logger.info("uploaded %s -> gs://%s/%s", call_key, bucket_name(), audio_obj)
        return True
    except Exception as exc:
        logger.warning("GCS upload failed for %s: %r", call_key, exc)
        return False


def _stamp_uploaded(call_key: str, audio_obj: str) -> None:
    import datetime

    from shared.db.database import get_db

    now = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    with get_db() as db:
        db.execute(
            "UPDATE calls SET gcs_object=?, gcs_uploaded_at=? WHERE call_key=?",
            (audio_obj, now, call_key))


def adopt_uploaded(call_key: str) -> bool:
    """Stamp a row whose audio is ALREADY in the bucket but was never recorded
    as uploaded. Verifies the object exists first — never stamps on faith.

    Without this, a row that lost its local WAV before being stamped is stuck
    forever: `sync_pending` skips it (nothing to upload) and `cleanup_local`
    skips it (not stamped), so its full-call WAV is retained past retention
    and the pending count never reaches zero. Eight 2026-07-14 rows sat in
    exactly that state.
    """
    if not (call_key and gcs_enabled()):
        return False
    audio_obj = _object_name(call_key, AUDIO_FILENAME)
    try:
        if not _bucket().blob(audio_obj).exists():
            return False
        _stamp_uploaded(call_key, audio_obj)
        logger.info("adopted already-uploaded %s -> gs://%s/%s",
                    call_key, bucket_name(), audio_obj)
        return True
    except Exception as exc:
        logger.warning("adopt check failed for %s: %r", call_key, exc)
        return False


def ensure_download_token(call_key: str) -> str | None:
    """Assign (or return) the row's download token; None if no row exists."""
    from shared.db.database import get_db

    with get_db() as db:
        row = db.execute(
            "SELECT download_token FROM calls WHERE call_key=?",
            (call_key,)).fetchone()
        if row is None:
            return None
        if row["download_token"]:
            return row["download_token"]
        token = new_download_token()
        db.execute(
            "UPDATE calls SET download_token=?, recording_url=? WHERE call_key=?",
            (token, public_download_url(token), call_key))
        return token


# ---------------------------------------------------------------------------
# Catch-up sync + local retention cleanup
# ---------------------------------------------------------------------------

def sync_pending(limit: int = 200) -> dict:
    """Upload every call row that has local audio but no GCS stamp; also
    backfill missing download tokens.

    Returns counts: `uploaded` (this pass), `adopted` (already in the bucket,
    stamp backfilled), `failed` (upload attempted and did NOT land). `failed`
    is what makes a broken pass visible — it drives the CLI's exit code.
    """
    from shared.db.database import get_db

    with get_db() as db:
        rows = db.execute(
            "SELECT call_key, conn_id, download_token FROM calls "
            "WHERE call_key IS NOT NULL AND gcs_uploaded_at IS NULL "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    counts = {"uploaded": 0, "adopted": 0, "failed": 0}
    for r in rows:
        call_key = r["call_key"]
        if not r["download_token"]:
            ensure_download_token(call_key)
        # Catch-up for the full two-party recording: if the teardown copy was
        # missed (crash, race) and the FS WAV still exists, grab it now.
        if r["conn_id"] and not local_full_audio_path(call_key).exists():
            capture_fs_recording(r["conn_id"], call_key, wait_s=0.5)
        if local_audio_path(call_key).exists():
            if upload_recording(call_key):
                counts["uploaded"] += 1
            else:
                counts["failed"] += 1
        elif adopt_uploaded(call_key):
            counts["adopted"] += 1
    return counts


def cleanup_local(max_age_days: float | None = None) -> int:
    """Delete local WAVs that are safely in GCS and older than the retention
    window. Transcript/metadata sidecars stay local (tiny, and the dashboard
    reads them). Returns number of files deleted."""
    from shared.db.database import get_db

    max_age_days = retention_days() if max_age_days is None else max_age_days
    cutoff = time.time() - max_age_days * 86400
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT call_key FROM calls "
                "WHERE gcs_uploaded_at IS NOT NULL AND call_key IS NOT NULL"
            ).fetchall()
    except Exception as exc:
        logger.warning("cleanup: DB read failed: %r", exc)
        return 0
    deleted = 0
    for r in rows:
        for wav in (local_audio_path(r["call_key"]),
                    local_full_audio_path(r["call_key"])):
            try:
                if wav.exists() and wav.stat().st_mtime < cutoff:
                    wav.unlink()
                    deleted += 1
                    logger.info("cleanup: removed local %s", wav)
            except Exception as exc:
                logger.warning("cleanup: could not remove %s: %r", wav, exc)
    # Janitor for the FreeSWITCH staging dir: anything older than 2 days has
    # either been captured into its call folder or belongs to no known call.
    try:
        fs_cutoff = time.time() - 2 * 86400
        for f in FS_RECORDINGS_DIR.glob("*.wav"):
            if f.stat().st_mtime < fs_cutoff:
                f.unlink()
                logger.info("cleanup: pruned FS staging file %s", f.name)
    except Exception as exc:
        logger.warning("cleanup: FS staging prune failed: %r", exc)
    return deleted


def _run_once() -> int:
    """One sync pass. Returns a process exit code: 0 healthy, 2 GCS
    unreachable, 1 some upload failed. A pass that cannot reach GCS must NOT
    exit 0 — a green `systemctl status` on a dead credential is how this
    silently stopped being a backup at all."""
    if not gcs_enabled():
        logger.warning("GCS_RECORDINGS_BUCKET unset — token backfill only, "
                       "no uploads/cleanup")
        from shared.db.database import get_db
        with get_db() as db:
            rows = db.execute(
                "SELECT call_key FROM calls WHERE call_key IS NOT NULL "
                "AND download_token IS NULL").fetchall()
        for r in rows:
            ensure_download_token(r["call_key"])
        logger.info("backfilled %d download tokens", len(rows))
        return 0
    err = probe_bucket()
    if err is not None:
        logger.error("GCS unreachable for bucket %r — recordings are NOT "
                     "being backed up: %s", bucket_name(), err)
        return 2
    c = sync_pending()
    d = cleanup_local()
    # Generated .xlsx exports share this timer rather than getting one of
    # their own: same bucket, same "local copy is a fallback, not an archive"
    # rule, and one fewer thing to notice has stopped running.
    x = 0
    try:
        from shared import export_store
        x = export_store.cleanup_local()
    except Exception as exc:
        logger.warning("export cleanup skipped: %r", exc)
    logger.info("sync complete: uploaded=%d adopted=%d failed=%d "
                "local_deleted=%d exports_deleted=%d",
                c["uploaded"], c["adopted"], c["failed"], d, x)
    if c["failed"]:
        logger.error("%d recording(s) failed to upload this pass", c["failed"])
        return 1
    return 0


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="single pass")
    parser.add_argument("--interval", type=int, default=0,
                        help="loop every N seconds")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.interval > 0 and not args.once:
        while True:
            try:
                _run_once()
            except Exception:
                logger.exception("sync pass failed")
            time.sleep(args.interval)
    else:
        import sys
        try:
            sys.exit(_run_once())
        except SystemExit:
            raise
        except Exception:
            logger.exception("sync pass failed")
            sys.exit(1)


if __name__ == "__main__":
    main()
