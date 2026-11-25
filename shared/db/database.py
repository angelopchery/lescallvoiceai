"""Dialect-thin database access for the multi-tenant layer.

Dev/prod default is SQLite (WAL) in this directory; production can move to
Postgres by setting LESCALL_DB_URL=postgres://... — same schema.sql (via the
{PK} placeholder), same qmark ("?") placeholders in callers, translated here.

Usage:
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute("SELECT * FROM agents WHERE slug=?", (slug,)).fetchone()

get_db() commits on clean exit, rolls back on exception, and ensures the schema
exists (idempotent, once per process). Callers in the live call path must wrap
their own try/except — this module raises normally; the never-crash policy
belongs to the caller (see resolve_agent_slug).
"""

from __future__ import annotations

import functools
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

_HERE = Path(__file__).resolve().parent
SCHEMA_PATH = _HERE / "schema.sql"
DEFAULT_SQLITE_PATH = _HERE / "lescall.db"

_PK_SQLITE = "INTEGER PRIMARY KEY AUTOINCREMENT"
_PK_POSTGRES = "BIGSERIAL PRIMARY KEY"

# Idempotent column additions for schema evolution: existing tables don't get
# new columns from CREATE IF NOT EXISTS, so append ALTERs here (never edit old
# ones). Errors for already-present columns are swallowed.
MIGRATIONS: list[str] = [
    "ALTER TABLE phone_numbers ADD COLUMN verified INTEGER DEFAULT 0",
    # Settings-page profile fields; phone is the destination for BYON test calls.
    "ALTER TABLE users ADD COLUMN phone TEXT",
    "ALTER TABLE users ADD COLUMN company TEXT",
    # Call-recording delivery (GCS sync + public tokenized download links).
    # The unique index lives here, not schema.sql: on pre-existing DBs the
    # schema script runs before these ALTERs, when the column doesn't exist.
    "ALTER TABLE calls ADD COLUMN download_token TEXT",
    "ALTER TABLE calls ADD COLUMN recording_url TEXT",
    "ALTER TABLE calls ADD COLUMN gcs_object TEXT",
    "ALTER TABLE calls ADD COLUMN gcs_uploaded_at TIMESTAMP",
    "ALTER TABLE calls ADD COLUMN follow_up TEXT",
    "ALTER TABLE calls ADD COLUMN collected_data TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_calls_download_token"
    " ON calls(download_token)",
    # Contacts phone book: groups + gender/details, call→contact binding.
    # contact_groups itself is created by schema.sql (CREATE IF NOT EXISTS).
    "ALTER TABLE contacts ADD COLUMN gender TEXT",
    "ALTER TABLE contacts ADD COLUMN details TEXT",
    "ALTER TABLE contacts ADD COLUMN group_id INTEGER",
    "ALTER TABLE calls ADD COLUMN contact_id INTEGER",
    # Same 10 significant digits under one owner = same person, regardless of
    # +91/0-prefix formatting (verified duplicate-free before adding).
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_owner_sig"
    " ON contacts(owner, sig_digits)",
    "CREATE INDEX IF NOT EXISTS idx_calls_contact ON calls(contact_id)",
    # AgentSpec v2 post-call outcomes (plan §4.6): deterministic outcome id +
    # kind computed by the bridge at teardown when post_call.outcomes is
    # configured (NULL = today, including Pramukh until backfilled).
    "ALTER TABLE calls ADD COLUMN outcome TEXT",
    "ALTER TABLE calls ADD COLUMN outcome_kind TEXT",
    # Callback auto-dial context: why we are calling back (call_aim / past
    # context) — rendered as a WHY-YOU-ARE-CALLING-BACK prompt block.
    "ALTER TABLE call_links ADD COLUMN context_note TEXT",
    # F8 per-call agent fidelity: WHICH agent revision/sha answered the call,
    # the F6 preflight health marker ('ok' | 'load_failed' |
    # 'preflight_failed:<stage>'), and per-call LLM usage counters. Written by
    # conversation_call_bridge._persist_call_row at teardown.
    "ALTER TABLE calls ADD COLUMN agent_revision INTEGER",
    "ALTER TABLE calls ADD COLUMN agent_sha TEXT",
    "ALTER TABLE calls ADD COLUMN agent_health TEXT",
    "ALTER TABLE calls ADD COLUMN llm_models_used TEXT",
    "ALTER TABLE calls ADD COLUMN llm_fallback_turns INTEGER",
    # Per-direction agent binding: a DID's outbound agent is no longer forced to
    # be its inbound agent. Backfilled once (see POST_MIGRATIONS) so existing
    # numbers keep placing calls with exactly the agent they used before.
    "ALTER TABLE phone_numbers ADD COLUMN outbound_agent_slug TEXT",
    # Customer Intelligence Hub ownership: which of the owner's agents "owns"
    # the relationship. Set by the bridge at call end when NULL (first agent
    # that talked to the contact); editable via the hub API. Cross-owner
    # sharing lives in contact_grants (versioned migration 0006).
    "ALTER TABLE contacts ADD COLUMN primary_agent_slug TEXT",
    # Billing (deploy/PLAN-razorpay-subscriptions.md §4.2). These two touch
    # PRE-EXISTING tables, so they belong here rather than in migration 0013 —
    # versioned migrations and this boot list have no ordering relationship.
    #
    # agents.archived_at: DELETE /api/agents/{name} moves the folder to
    # _archived/ but leaves the agents row owned, so a naive COUNT(*) counts
    # dead agents against the tenant's plan cap forever. Every cap check and
    # every usage display filters archived_at IS NULL.
    "ALTER TABLE agents ADD COLUMN archived_at TIMESTAMP",
    # usage_ledger.entitlement_check: 'ok' | 'error' | 'unattributed'. The
    # inbound gate fails OPEN on a DB error (a live call must not drop because
    # Postgres hiccupped), so the minutes it lets through have to be
    # identifiable afterwards rather than silently indistinguishable from
    # properly admitted usage.
    "ALTER TABLE usage_ledger ADD COLUMN entitlement_check TEXT",
    # answer_state: how the connected call actually went, as opposed to whether
    # the leg connected. 'answered' = a real two-way conversation happened;
    # 'empty' = the callee picked up but never really spoke (silence, noise
    # only, a voicemail greeting, or an instant hang-up). The dial result alone
    # cannot tell these apart — FreeSWITCH reports +OK for all of them — so it
    # is computed at teardown from the transcript. NULL on every pre-existing
    # row and on any call the detector could not classify; readers must treat
    # NULL as 'answered'.
    "ALTER TABLE calls ADD COLUMN answer_state TEXT",
    "ALTER TABLE calls ADD COLUMN answer_state_reason TEXT",
]

# Follow-up statements run ONLY on the migration run that actually added the
# column (keyed by the MIGRATIONS entry above). A plain backfill in MIGRATIONS
# would re-run on every boot and silently re-bind an outbound slot a tenant
# deliberately cleared.
POST_MIGRATIONS: dict[str, list[str]] = {
    "ALTER TABLE phone_numbers ADD COLUMN outbound_agent_slug TEXT": [
        "UPDATE phone_numbers SET outbound_agent_slug = inbound_agent_slug",
    ],
}

_init_lock = threading.Lock()
_initialized_urls: set[str] = set()


def db_url() -> str:
    return os.environ.get("LESCALL_DB_URL", f"sqlite:///{DEFAULT_SQLITE_PATH}")


def _is_postgres(url: str) -> bool:
    return url.startswith(("postgres://", "postgresql://"))


def _schema_sql(url: str) -> str:
    raw = SCHEMA_PATH.read_text(encoding="utf-8")
    return raw.replace("{PK}", _PK_POSTGRES if _is_postgres(url) else _PK_SQLITE)


# ---------------------------------------------------------------------------
# qmark -> psycopg translation
#
# `sql.replace("?", "%s")` was wrong in a way that had not bitten yet but was
# a landmine under every future query: it rewrites question marks INSIDE STRING
# LITERALS too. `WHERE label = 'why?'` became `WHERE label = 'why%s'`, which
# psycopg then reads as a placeholder — so the statement either raises
# "the query has 2 placeholders but 1 parameters were passed" or, worse,
# silently consumes the NEXT bind value in the wrong position and runs a
# perfectly valid query against the wrong data. The same applies to a `?`
# inside a quoted identifier, a comment, or a dollar-quoted body (Postgres
# operators `?`, `?|`, `?&` on jsonb are the same hazard from the other side:
# they would be turned into placeholders that nothing binds).
#
# So: a scanner that knows where SQL text is and where it is not. It is not a
# full parser and does not need to be — it only has to know when it is inside
# something the server will read literally.
#
# `%` is deliberately NOT touched. Callers that need a literal one already
# write `%%` (see shared/intelligence/search.py's pg_trgm `%%` operator), and
# silently doubling them here would turn those into `%%%%`.
# ---------------------------------------------------------------------------

def _translate_qmarks(sql: str) -> str:
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'" or ch == '"':
            # Standard SQL literals/identifiers. The closing quote may be
            # doubled to escape itself ('it''s'), which this handles by simply
            # not treating the pair as a close.
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        out.append(sql[i:i + 2])
                        i += 2
                        continue
                    out.append(sql[i])
                    i += 1
                    break
                out.append(sql[i])
                i += 1
            continue
        if ch == "$":
            # Dollar quoting: $$...$$ or $tag$...$tag$. Used by every DO block
            # and function body in the migrations.
            m = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[i:])
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                end = n if end == -1 else end + len(tag)
                out.append(sql[i:end])
                i = end
                continue
        if ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end + 1
            out.append(sql[i:end])
            i = end
            continue
        if ch == "/" and sql.startswith("/*", i):
            # Postgres block comments NEST, unlike C's.
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            out.append(sql[i:j])
            i = j
            continue
        out.append("%s" if ch == "?" else ch)
        i += 1
    return "".join(out)


# SQL strings in this codebase are literals in the source, so the same handful
# of thousands recur for the life of the process.
_translate_qmarks = functools.lru_cache(maxsize=4096)(_translate_qmarks)


def _scrub_params(params):
    """Strip NUL bytes from string parameters.

    Postgres text columns CANNOT hold a 0x00 byte. psycopg does not truncate
    or escape it — it raises `DataError: PostgreSQL text fields cannot contain
    NUL (0x00) bytes`, which surfaces as a 500 for what is plainly a bad
    input. A 500 any caller can trigger at will (`?q=%00`, a `\\u0000` in a
    JSON name, a control byte in an uploaded CSV) is a denial of service with
    extra steps. That exact hazard was closed for the public API at its own
    boundary (shared/public_api/sanitize.py) and left open everywhere else:
    the dashboard's /api surface, CSV upload, the intelligence pipeline, and
    every background worker.

    Fixing it HERE rather than in a request middleware is deliberate. This is
    the one place every writer passes through, HTTP or not, so a new endpoint
    or a new worker cannot forget to opt in — and a middleware could not cover
    the non-HTTP writers at all.

    Only NUL is removed, and only from `str`. Nothing else is touched: `bytes`
    are binary and a NUL in them is legitimate, and the wider control-character
    hygiene that `sanitize.py` applies is a product decision that belongs at an
    API boundary, not a silent rewrite of everything the platform stores.
    """
    if not params:
        return params
    def _one(v):
        if isinstance(v, str) and "\x00" in v:
            return v.replace("\x00", "")
        if isinstance(v, (list, tuple)):
            # psycopg maps a Python list to a Postgres array (`= ANY(?)`), so
            # its elements land in text columns exactly like scalars do.
            scrubbed = [_one(x) for x in v]
            return type(v)(scrubbed) if isinstance(v, list) else tuple(scrubbed)
        return v
    if isinstance(params, dict):
        return {k: _one(v) for k, v in params.items()}
    if isinstance(params, (list, tuple)):
        scrubbed = [_one(v) for v in params]
        return scrubbed if isinstance(params, list) else tuple(scrubbed)
    return params


class _QmarkPgConnection:
    """Adapts psycopg so callers can keep sqlite-style "?" placeholders."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params=()):
        return self._conn.execute(_translate_qmarks(sql), _scrub_params(params))

    def executescript(self, sql: str):
        return self._conn.execute(sql)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def _connect(url: str):
    if _is_postgres(url):
        import psycopg
        from psycopg.rows import dict_row

        class _TextLoader(psycopg.adapt.Loader):
            """Return timestamps as strings, matching what SQLite callers get
            (rows are json.dumps'd all over the dashboard)."""

            def load(self, data):
                return bytes(data).decode()

        conn = psycopg.connect(url, row_factory=dict_row)
        for pgtype in ("timestamp", "timestamptz", "date"):
            conn.adapters.register_loader(pgtype, _TextLoader)
        return _QmarkPgConnection(conn)

    path = url[len("sqlite:///"):] if url.startswith("sqlite:///") else url
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn, url: str | None = None) -> None:
    """Create tables/indexes and apply idempotent migrations. Safe to re-run."""
    url = url or db_url()
    conn.executescript(_schema_sql(url))
    conn.commit()
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
            conn.commit()
        except Exception as exc:  # column already exists — expected on re-run
            if not re.search(r"duplicate|already exists", str(exc), re.I):
                raise
            # Postgres aborts the transaction on any error; clear it so the
            # next migration statement can run.
            conn.rollback()
            continue
        # Reached only when the statement genuinely applied — so these run once,
        # on the boot that introduced the column, and never again.
        for follow_up in POST_MIGRATIONS.get(stmt, []):
            conn.execute(follow_up)
            conn.commit()


def _ensure_initialized(url: str) -> None:
    if url in _initialized_urls:
        return
    with _init_lock:
        if url in _initialized_urls:
            return
        conn = _connect(url)
        try:
            init_db(conn, url)
        finally:
            conn.close()
        _initialized_urls.add(url)


@contextmanager
def get_db(url: str | None = None):
    """Yield a connection; commit on success, roll back on exception."""
    url = url or db_url()
    # :memory: gets a fresh empty DB per connection, so init must happen on
    # THIS connection, not a throwaway one.
    in_memory = not _is_postgres(url) and url.endswith(":memory:")
    if not in_memory:
        _ensure_initialized(url)
    conn = _connect(url)
    try:
        if in_memory:
            init_db(conn, url)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
