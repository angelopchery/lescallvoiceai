"""One-shot copy of every table from the legacy SQLite DB into Postgres.

Idempotent: every insert is ON CONFLICT DO NOTHING and ids are preserved, so
re-running after a partial copy only fills gaps. After copying, each table's
BIGSERIAL sequence is bumped to MAX(id) so new inserts don't collide.

Usage:
    python3 -m shared.db.migrate_sqlite_to_pg [--sqlite PATH] [--pg-url URL]

--pg-url defaults to LESCALL_DB_URL (which must be a postgresql:// URL).
Prints a per-table report and exits non-zero on any count mismatch.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from shared.db.database import DEFAULT_SQLITE_PATH, db_url, get_db, _is_postgres

# Parent tables first (phone_numbers FKs trunks).
TABLES = ["users", "agents", "trunks", "phone_numbers", "campaigns",
          "calls", "contacts", "follow_ups", "outbox"]


def migrate(sqlite_path: str, pg_url: str) -> dict:
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    report = {"sqlite": sqlite_path, "ok": True, "tables": {}}
    with get_db(pg_url) as pg:
        for table in TABLES:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            copied = 0
            for row in rows:
                cols = row.keys()
                placeholders = ",".join("?" for _ in cols)
                cur = pg.execute(
                    f"INSERT INTO {table} ({','.join(cols)}) "
                    f"VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                    tuple(row[c] for c in cols))
                copied += cur.rowcount
            pg.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'),"
                f" GREATEST((SELECT COALESCE(MAX(id), 0) FROM {table}), 1))")
            dst_count = pg.execute(
                f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            ok = dst_count >= len(rows)
            report["tables"][table] = {
                "source_rows": len(rows), "copied": copied,
                "pg_rows_after": dst_count, "ok": ok}
            if not ok:
                report["ok"] = False
    src.close()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sqlite", default=str(DEFAULT_SQLITE_PATH))
    parser.add_argument("--pg-url", default=None)
    args = parser.parse_args(argv)
    pg_url = args.pg_url or db_url()
    if not _is_postgres(pg_url):
        print(f"error: target is not a postgres URL: {pg_url}", file=sys.stderr)
        return 2
    report = migrate(args.sqlite, pg_url)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
