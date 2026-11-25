"""Where a generated export file lives: Google Cloud Storage, with the same
doctrine as call recordings (RUNBOOK §6b).

    build bytes → PUT to a private GCS bucket → row in call_exports →
    unguessable 256-bit token → /api/exports/dl/<token> streams it back

Two decisions worth stating, because both look like omissions otherwise:

1. NO SIGNED URLS. Signing a GCS object needs a service-account private key,
   and the org policy on this project blocks SA key creation — which is also
   why auth here is the VM's ATTACHED service account via the metadata server
   (never GOOGLE_APPLICATION_CREDENTIALS; an explicit path outranks metadata
   ADC and silently stalled recording uploads for 17 days in July 2026). So
   the bucket stays private and OUR token is the credential, exactly as call
   recordings work.

2. A FAILED UPLOAD IS NOT A FAILED EXPORT. If GCS is unreachable the workbook
   is written to disk and the row is marked storage='local' with the upload
   error kept. The user still gets their file and the retry path
   (`sync_pending`) can push it later — losing an export because a bucket
   blinked would be the worst possible trade.

Config (deploy/.pg_env):
    GCS_EXPORTS_BUCKET=lescall-recordings   # defaults to the recordings bucket
    GCS_EXPORTS_PREFIX=exports
    EXPORTS_DIR=<repo>/exports              # local staging + fallback
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("export_store")

_REPO_ROOT = Path(__file__).resolve().parent.parent
EXPORTS_DIR = Path(os.environ.get("EXPORTS_DIR", _REPO_ROOT / "exports"))

XLSX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".spreadsheetml.sheet")


def bucket_name() -> str:
    return (os.environ.get("GCS_EXPORTS_BUCKET")
            or os.environ.get("GCS_RECORDINGS_BUCKET", "")).strip()


def gcs_enabled() -> bool:
    return bool(bucket_name())


def prefix() -> str:
    return os.environ.get("GCS_EXPORTS_PREFIX", "exports").strip("/")


def public_base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL",
                          "https://agent.lescall.com").rstrip("/")


def new_token() -> str:
    """256 bits from `secrets`. It is never derived from the owner, the title
    or the id, so one leaked link cannot be edited into another tenant's."""
    return secrets.token_urlsafe(32)


def public_download_url(token: str) -> str:
    return f"{public_base_url()}/api/exports/dl/{token}"


_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def safe_slug(text: str, fallback: str = "export") -> str:
    slug = _SLUG_RE.sub("-", str(text or "")).strip("-").lower()
    return (slug or fallback)[:60]


def filename_for(title: str, export_id: int, when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    return f"{safe_slug(title, 'calls')}-{when:%Y%m%d}-{export_id}.xlsx"


def object_name(owner: str, export_id: int, filename: str,
                when: datetime | None = None) -> str:
    """`exports/<owner>/<YYYY>/<MM>/<id>-<name>.xlsx` — owner-partitioned so a
    lifecycle rule or a per-tenant grant can be written against a path prefix
    without a lookup table."""
    when = when or datetime.now(timezone.utc)
    return (f"{prefix()}/{safe_slug(owner, 'account')}/"
            f"{when:%Y/%m}/{export_id}-{filename}")


def gcs_uri(obj: str) -> str:
    return f"gs://{bucket_name()}/{obj}"


def console_url(obj: str) -> str:
    """Where a human opens it in the Cloud console."""
    return (f"https://console.cloud.google.com/storage/browser/_details/"
            f"{bucket_name()}/{obj}")


def _bucket(name: str | None = None):
    from google.cloud import storage

    # No credentials= argument on purpose: the VM's attached service account
    # via metadata ADC. See the module docstring.
    return storage.Client().bucket(name or bucket_name())


def upload_bytes(obj: str, data: bytes, content_type: str = XLSX_MIME) -> None:
    """Raises on failure — the caller decides whether that is fatal."""
    _bucket().blob(obj).upload_from_string(data, content_type=content_type)


def open_stream(obj: str, bucket: str | None = None):
    """Read an object back.

    `bucket` is the one RECORDED ON THE ROW, not the one configured right now.
    Every export stores its own `gcs_bucket`, and the two diverge the moment
    anyone sets GCS_EXPORTS_BUCKET — a switch the runbook actively offers. With
    the live value hard-wired here, every export written before the switch
    would 404 as soon as its local copy aged out, months later, with nothing
    to connect the failure to the config change that caused it.
    """
    return _bucket(bucket).blob(obj).open("rb")


def object_exists(obj: str, bucket: str | None = None) -> bool:
    return _bucket(bucket).blob(obj).exists()


def local_path(filename: str) -> Path:
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    return EXPORTS_DIR / filename


def write_local(filename: str, data: bytes) -> Path:
    path = local_path(filename)
    # Write-then-rename: a reader (or a retry) never sees a half-written file.
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def retention_days() -> float:
    try:
        return float(os.environ.get("EXPORT_LOCAL_RETENTION_DAYS", "14"))
    except ValueError:
        return 14.0


def cleanup_local(max_age_days: float | None = None) -> int:
    """Delete local staging copies that are safely in the bucket.

    The local file is a serving FALLBACK, not an archive — once the row says
    storage='gcs', downloads can stream from the object. Without this the
    directory grows one workbook per export forever on a 2-core VM. A row
    still marked 'local' is never touched: that file IS the only copy.
    """
    if not EXPORTS_DIR.exists():
        return 0
    days = max(0.0, retention_days() if max_age_days is None else max_age_days)
    from shared.db.database import get_db

    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT id, local_path, gcs_object, gcs_bucket FROM call_exports"
                " WHERE storage='gcs' AND gcs_object IS NOT NULL"
                "   AND local_path IS NOT NULL"
                "   AND gcs_uploaded_at < now() - make_interval(secs => ?)",
                (days * 86400,),
            ).fetchall()
    except Exception as exc:
        logger.warning("export cleanup could not read the ledger: %r", exc)
        return 0
    removed = 0
    for r in rows:
        path = Path(r["local_path"])
        if not path.exists():
            continue
        # `storage='gcs'` records that the upload SUCCEEDED once. It is not
        # evidence the object is still there: this VM's service account has
        # object-level access only and cannot read the bucket's lifecycle
        # rules, so a delete-after-N-days rule set by an operator is invisible
        # to us. Deleting the local file on that assumption would make a
        # lifecycle rule silently destroy the last copy of an export. Re-check
        # the object each pass; a bucket we cannot reach keeps the file.
        try:
            if not object_exists(r["gcs_object"], r["gcs_bucket"]):
                logger.warning("export %s is gone from gs://%s/%s — keeping the "
                               "local copy, which is now the only one",
                               r["id"], r["gcs_bucket"], r["gcs_object"])
                continue
        except Exception as exc:
            logger.warning("could not confirm export %s in GCS (%r) — keeping "
                           "the local copy", r["id"], exc)
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("could not delete %s: %r", path, exc)
    return removed
