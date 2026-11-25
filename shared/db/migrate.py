"""Versioned SQL migrations for Postgres (the intelligence layer and onward).

The legacy mechanism (schema.sql CREATE IF NOT EXISTS + the boot-time MIGRATIONS
ALTER list in database.py) stays authoritative for the pre-existing tables. New
subsystems use proper migration files instead:

    shared/db/migrations/NNNN_name.sql        -- forward migration
    shared/db/migrations/NNNN_name.down.sql   -- optional rollback (testing env)

Applied versions are tracked in schema_migrations (version, name, checksum,
applied_at). Each file runs inside one transaction; a failure rolls the file
back and stops the run, leaving the database at the last good version. Checksum
drift on an already-applied file aborts loudly — edit history by adding a new
migration, never by rewriting an applied one.

Postgres-only by design: production is PG (deploy/.pg_env) and tests use a
lescall_test database in the same instance. The sqlite dev fallback never gets
these tables; intelligence features detect that and disable themselves.

Usage (run from the repo root with deploy/.pg_env loaded, or inside a container):
    python -m shared.db.migrate                # apply pending
    python -m shared.db.migrate --status       # show applied/pending
    python -m shared.db.migrate --dry-run      # print what would run
    python -m shared.db.migrate --down 0003    # roll back one version (needs .down.sql)
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

from shared.db.database import db_url

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


def _connect():
    url = db_url()
    if not url.startswith(("postgres://", "postgresql://")):
        raise SystemExit(
            "migrate: LESCALL_DB_URL is not Postgres (%r). Versioned migrations "
            "are PG-only; source deploy/.pg_env first." % url
        )
    import psycopg

    return psycopg.connect(url)


def _discover() -> list[tuple[int, str, Path]]:
    out = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _FILE_RE.match(path.name)
        if not m:  # .down.sql files and strays
            continue
        out.append((int(m.group(1)), m.group(2), path))
    versions = [v for v, _, _ in out]
    if len(versions) != len(set(versions)):
        raise SystemExit("migrate: duplicate migration version numbers")
    return out


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ensure_tracking(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version    INTEGER PRIMARY KEY,
               name       TEXT NOT NULL,
               checksum   TEXT NOT NULL,
               applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
           )"""
    )
    conn.commit()


def _applied(conn) -> dict[int, tuple[str, str]]:
    rows = conn.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def status(conn) -> int:
    applied = _applied(conn)
    for version, name, path in _discover():
        if version in applied:
            drift = "" if applied[version][1] == _checksum(path) else "  !! CHECKSUM DRIFT"
            print(f"  [applied] {version:04d}_{name}{drift}")
        else:
            print(f"  [pending] {version:04d}_{name}")
    return 0


def apply_pending(conn, dry_run: bool = False) -> int:
    applied = _applied(conn)
    ran = 0
    for version, name, path in _discover():
        if version in applied:
            if applied[version][1] != _checksum(path):
                raise SystemExit(
                    f"migrate: {path.name} was modified after being applied "
                    f"(checksum drift). Add a new migration instead."
                )
            continue
        if dry_run:
            print(f"would apply {path.name}")
            ran += 1
            continue
        sql = path.read_text(encoding="utf-8")
        print(f"applying {path.name} ...", flush=True)
        try:
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                (version, name, _checksum(path)),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            print(f"FAILED in {path.name}; database left at previous version", file=sys.stderr)
            raise
        ran += 1
    print(f"{ran} migration(s) {'pending' if dry_run else 'applied'}, "
          f"{len(applied)} previously applied")
    return 0


def down(conn, version: int) -> int:
    applied = _applied(conn)
    if version not in applied:
        raise SystemExit(f"migrate: version {version:04d} is not applied")
    if max(applied) != version:
        raise SystemExit("migrate: can only roll back the newest applied version")
    name = applied[version][0]
    down_path = MIGRATIONS_DIR / f"{version:04d}_{name}.down.sql"
    if not down_path.exists():
        raise SystemExit(f"migrate: {down_path.name} does not exist")
    print(f"rolling back {version:04d}_{name} ...")
    try:
        conn.execute(down_path.read_text(encoding="utf-8"))
        conn.execute("DELETE FROM schema_migrations WHERE version = %s", (version,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--down", metavar="VERSION")
    args = ap.parse_args()

    conn = _connect()
    try:
        _ensure_tracking(conn)
        if args.status:
            return status(conn)
        if args.down:
            return down(conn, int(args.down))
        return apply_pending(conn, dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
