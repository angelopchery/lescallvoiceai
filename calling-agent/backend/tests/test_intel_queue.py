"""Intelligence job queue (shared/intelligence/queue.py) + migration chain.

Runs against a dedicated lescall_test database inside the live lescall-pg
instance: the session fixture rebuilds it from scratch (legacy schema via
database.init_db, then versioned migrations via shared.db.migrate), which
doubles as a disaster-recovery drill for the whole DDL chain.

Skips cleanly when Postgres is unreachable (e.g. laptop dev without the stack).
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PG_ENV = REPO_ROOT / "deploy" / ".pg_env"


def _prod_url() -> str | None:
    if not PG_ENV.exists():
        return None
    for line in PG_ENV.read_text().splitlines():
        m = re.match(r"^LESCALL_DB_URL=(.+)$", line.strip())
        if m:
            return m.group(1).strip()
    return None


def _test_url(prod: str) -> str:
    # postgresql://user:pw@host:port/lescall -> .../lescall_test
    return re.sub(r"/([A-Za-z0-9_]+)$", r"/\1_test", prod)


@pytest.fixture(scope="session")
def pg_test_db(session_mocker=None):
    prod = _prod_url()
    if not prod or not prod.startswith(("postgres://", "postgresql://")):
        pytest.skip("no Postgres LESCALL_DB_URL in deploy/.pg_env")
    test = _test_url(prod)
    assert test != prod, "refusing to run queue tests against the production DB"

    import psycopg

    # Create the test database if missing (autocommit; CREATE DATABASE cannot
    # run inside a transaction).
    admin = psycopg.connect(prod, autocommit=True)
    try:
        dbname = test.rsplit("/", 1)[1]
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (dbname,)
        ).fetchone()
        if not exists:
            admin.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        admin.close()

    # Rebuild from scratch: extensions, legacy schema, versioned migrations.
    conn = psycopg.connect(test)
    try:
        with conn.transaction():
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        conn.autocommit = True
        for ext in ("vector", "pg_trgm", "unaccent"):
            conn.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
        conn.autocommit = False
    finally:
        conn.close()

    from shared.db import database, migrate

    legacy = database._connect(test)
    try:
        database.init_db(legacy, test)
    finally:
        legacy.close()

    mconn = psycopg.connect(test)
    try:
        migrate._ensure_tracking(mconn)
        migrate.apply_pending(mconn)
    finally:
        mconn.close()

    return test


@pytest.fixture(autouse=True)
def _use_test_db(pg_test_db, monkeypatch):
    monkeypatch.setenv("LESCALL_DB_URL", pg_test_db)
    from shared.db.database import get_db

    with get_db() as db:
        db.execute("DELETE FROM intel_jobs")
    yield


@pytest.fixture()
def q():
    from shared.intelligence import queue

    return queue


def _make_ready(job_id: int) -> None:
    from shared.db.database import get_db

    with get_db() as db:
        db.execute(
            "UPDATE intel_jobs SET next_attempt_at = now() - interval '1 hour'"
            " WHERE id=?",
            (job_id,),
        )


def test_migration_chain_applied_from_scratch(pg_test_db):
    import psycopg

    with psycopg.connect(pg_test_db) as conn:
        n = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        assert n >= 4
        # vector column really is a vector (extension wired in this DB too)
        typ = conn.execute(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute"
            " WHERE attrelid='call_embeddings'::regclass AND attname='embedding'"
        ).fetchone()[0]
        assert typ == "vector(768)"


def test_enqueue_dedupes_on_key(q):
    first = q.enqueue(q.KIND_CALL_PROCESS, owner="t@x.com", dedupe_key="call.process:abc")
    dup = q.enqueue(q.KIND_CALL_PROCESS, owner="t@x.com", dedupe_key="call.process:abc")
    assert first is not None
    assert dup is None


def test_claim_complete_lifecycle(q):
    job_id = q.enqueue(q.KIND_CALL_PROCESS, owner="t@x.com", payload={"conn_id": "u1"})
    job = q.claim("worker-test")
    assert job and job["id"] == job_id
    assert job["payload"] == {"conn_id": "u1"}
    assert job["attempts"] == 1
    q.complete(job_id)
    assert q.claim("worker-test") is None
    assert q.counts().get("done") == 1


def test_delayed_job_not_claimable_until_due(q):
    job_id = q.enqueue(q.KIND_REPORT_GENERATE, delay_s=3600)
    assert q.claim("w") is None
    _make_ready(job_id)
    job = q.claim("w")
    assert job and job["id"] == job_id


def test_fail_retries_with_backoff_then_dead_letters(q):
    job_id = q.enqueue(q.KIND_CALL_PROCESS, max_attempts=2)
    q.claim("w")
    assert q.fail(job_id, "boom 1") == "pending"
    assert q.claim("w") is None  # backoff pushed next_attempt_at into the future
    _make_ready(job_id)
    q.claim("w")
    assert q.fail(job_id, "boom 2") == "failed"
    assert q.counts().get("failed") == 1
    # dead-lettered jobs can be re-driven
    assert q.requeue(job_id) is True
    job = q.claim("w")
    assert job and job["id"] == job_id


def test_kind_filter(q):
    q.enqueue(q.KIND_REPORT_GENERATE, dedupe_key="r1")
    call_id = q.enqueue(q.KIND_CALL_PROCESS, dedupe_key="c1")
    job = q.claim("w", kinds=(q.KIND_CALL_PROCESS,))
    assert job and job["id"] == call_id


def test_release_stale_recovers_crashed_worker_jobs(q):
    from shared.db.database import get_db

    job_id = q.enqueue(q.KIND_CALL_PROCESS)
    q.claim("w")
    with get_db() as db:
        db.execute(
            "UPDATE intel_jobs SET claimed_at = now() - interval '1 hour' WHERE id=?",
            (job_id,),
        )
    assert q.release_stale(older_than_s=900) == 1
    job = q.claim("w2")
    assert job and job["id"] == job_id and job["attempts"] == 2


def test_concurrent_claims_hand_out_distinct_jobs(q):
    ids = {q.enqueue(q.KIND_CALL_PROCESS, dedupe_key=f"cc{i}") for i in range(4)}
    got: list[int] = []
    lock = threading.Lock()

    def worker(name: str):
        while True:
            job = q.claim(name)
            if not job:
                return
            with lock:
                got.append(job["id"])
            q.complete(job["id"])

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == sorted(ids)  # every job exactly once, none double-claimed


def test_fk_to_calls_cascades(q):
    from shared.db.database import get_db

    with get_db() as db:
        row = db.execute(
            "INSERT INTO calls (conn_id, call_key, direction) VALUES (?, ?, ?)"
            " RETURNING id",
            ("test-conn-fk", "9999999999_20260101_000000", "inbound"),
        ).fetchone()
        call_id = row["id"]
    q.enqueue(q.KIND_CALL_PROCESS, call_id=call_id, dedupe_key="fk1")
    with get_db() as db:
        db.execute("DELETE FROM calls WHERE id=?", (call_id,))
        left = db.execute(
            "SELECT count(*) AS n FROM intel_jobs WHERE call_id=?", (call_id,)
        ).fetchone()["n"]
    assert left == 0
