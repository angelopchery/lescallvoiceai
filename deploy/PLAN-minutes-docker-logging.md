# Plan: Billing-grade minute metering · Production dockerization · Everything-logging → GCS

Drafted 2026-07-14. Status: AWAITING APPROVAL — nothing below is implemented yet.

---

## Part 1 — Minute metering (THE billing foundation)

### Why the current numbers are NOT billing-grade
- `calls.duration_s` = pipeline-session wall time (media-WS connect → teardown), rounded,
  **summed across WS reconnect segments**, and **written only on graceful teardown** — a
  crash mid-call loses the entire call's usage.
- The Usage page derives minutes from `credit_log/*.jsonl` timestamps — a plain file,
  no owner attribution, no durability guarantees, silently swallows I/O errors.
- Neither source bills on *answered* time; both start at media setup.

### Design principles (non-negotiable)
1. **Bill answered seconds only** (`billsec`), never ring time.
2. **Two independent sources, reconciled**: the pipeline's own measurement AND the
   FreeSWITCH CDR (`mod_cdr_csv` is already writing `billsec`,`answer_stamp`,`uuid`
   to `/var/log/freeswitch/cdr-csv/Master.csv`). CDR wins when present.
3. **Append-only ledger** keyed uniquely per call; rollups are *derived*, recomputable,
   never incremented in place.
4. **Heartbeats**: an open ledger row is updated every 15 s during the call, so a crash
   can lose at most 15 s of usage — never the call.
5. **Idempotent everywhere**: every write is an upsert on `conn_id`; re-running any
   step can never double-bill.
6. Store **exact seconds** (integer); minute *rounding is a display/invoice policy*
   applied at read time (default display: ceil per call to next minute — decide before
   first invoice, config `BILLING_ROUNDING=per_call_ceil|per_second`).

### Schema
```sql
CREATE TABLE usage_ledger (
  id {PK},
  conn_id     TEXT NOT NULL UNIQUE,   -- the calling id
  fs_uuid     TEXT,                   -- FreeSWITCH channel UUID (CDR join key)
  owner       TEXT NOT NULL,          -- the billed tenant
  agent_slug  TEXT,
  direction   TEXT,
  contact_id  INTEGER,
  answered_at TIMESTAMP,              -- CHANNEL_ANSWER (ESL), not media start
  ended_at    TIMESTAMP,
  last_beat_at TIMESTAMP,             -- heartbeat watermark
  billsec_pipeline INTEGER,           -- runtime-measured answered seconds
  billsec_cdr      INTEGER,           -- from FS CDR (authoritative)
  billsec_final    INTEGER,           -- reconciled value used for billing
  source      TEXT,                   -- 'cdr' | 'pipeline' | 'manual'
  billable    INTEGER DEFAULT 1,      -- 0 for owner test-calls etc.
  status      TEXT DEFAULT 'open',    -- open -> closed -> reconciled
  note        TEXT,                   -- mandatory free text for any manual touch
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_ledger_owner_time ON usage_ledger(owner, answered_at);
```
No DELETE and no UPDATE of `billsec_final` once `status='reconciled'`, except a
`source='manual'` correction that MUST fill `note` (enforced in code).

### Write path
1. **ANSWER** (socket_server, ESL `CHANNEL_ANSWER`): insert ledger row
   (`status=open`, `answered_at=now`, fs_uuid, conn_id) — socket_server pushes this
   through the same shared-DB helper the bridge uses.
2. **Heartbeat** (runtime, every 15 s per live session): upsert
   `billsec_pipeline = now - answered_at`, `last_beat_at = now`.
3. **Hangup** (bridge `stop()`): close the row (`status=closed`,
   `ended_at`, final `billsec_pipeline`). Reconnect segments extend the same row
   (unique conn_id), never create a second one.
4. **Reconciler** (worker, every 5 min): tail `Master.csv` (needs `angelo` added to
   the `freeswitch` group or a root-readable copy), join on `fs_uuid`, set
   `billsec_cdr`, `billsec_final = billsec_cdr`, `status=reconciled`.
   Rows with no CDR after 24 h: `billsec_final = billsec_pipeline`, `source='pipeline'`.
   Rows still `open` with stale `last_beat_at` (> 2 min): crashed call — close from CDR
   if available, else from `last_beat_at` (never lose, never over-bill).
5. **Rollups**: `usage_monthly(owner, month, seconds, calls)` — recomputed (full
   REPLACE per owner+month) by the reconciler from the ledger. Never incremented.

### Read path / UI
- `GET /api/usage/minutes` → `{month_seconds, month_minutes_display, today_seconds,
  total_seconds, per_day[]}` — owner-scoped; admin can pass `?owner=`.
- **Topbar**: replace the hardcoded `$5.00` wallet chip with a live
  `⏱ 128 min this month` chip (links to Usage page).
- **Usage page**: minutes-first — monthly counter, per-day bar chart, per-call table
  (answered_at, callee, agent, exact m:ss, reconciliation status). Sarvam-credit cost
  view moves to an admin-only tab (it is platform cost, not tenant billing).

### Verification before we trust it (gate for "done")
- Unit tests: rounding policy, reconnect single-row, heartbeat crash recovery,
  idempotent double-close, unanswered call → zero rows.
- Synthetic end-to-end: place a real test call of known length (e.g. 90 s), assert
  ledger row within ±2 s and CDR reconciliation lands.
- Nightly cross-check job: report any call where
  |billsec_cdr − billsec_pipeline| > 5 s or ledger vs `calls.duration_s` diverges
  wildly; alert (log + dashboard health) on any `open` row older than 1 h.
- Backfill: existing `calls` rows get ledger rows (`source='pipeline'`,
  `billsec_final = round(duration_s)`, flagged `note='backfill pre-metering'`).

---

## Part 2 — Production dockerization

Containerize (new `deploy/docker-compose.yml`, one stack):
- **pg** — as today (postgres:16-alpine, named volume).
- **spa** — as today (nginx static).
- **api** — `dashboard/server.py` on python:3.11-slim + requirements; volumes:
  `recordings/` (ro), agents dir, env_file `.pg_env`; publishes 127.0.0.1:8080;
  healthcheck `/api/health`.
- **runtime** — socket_server.py + ws_audio_receiver.py (supervisord, one image);
  publishes 127.0.0.1:9000/9001/9004 so FreeSWITCH's existing loopback config keeps
  working unchanged; volumes: `recordings/` (rw), `/tmp/esl_recordings`, env_file.
- **workers** — recsync loop (`--interval 900`), outbox worker, and the new usage
  reconciler in one lightweight image (or three tiny services).
- **Stays on host** (explicitly out of scope): FreeSWITCH, Kamailio (RTP port ranges/
  kernel realities), nginx TLS edge, and the Sarvam key-rotation scripts (per user).
- All services: `restart: unless-stopped`, `logging: {driver: json-file,
  options: {max-size: 20m, max-file: 5}}`, ADC/GCS credentials mounted read-only.
- Cutover: build images → `docker compose up -d` → verify (test call end-to-end) →
  disable lescall-runtime/lescall-dashboard systemd units (keep for rollback).
- CDR access: mount `/var/log/freeswitch/cdr-csv` ro into the workers container.

## Part 3 — Track everything: logging → GCS

1. **Structured app logs**: JSON formatter for api + runtime loggers (ts, service,
   level, conn_id/owner where known, event, detail).
2. **Request audit middleware** (FastAPI): one line per API call — owner, method,
   path, status, latency, client IP. Sensitive bodies never logged.
3. **Action audit table** `audit_events(owner, action, target, meta, at)` written by
   mutating endpoints (login, agent edit, dial, contact/group changes, deletes,
   settings) — queryable in-app, not just in files.
4. **Shipper**: extend the recsync/worker loop — hourly, gzip rotated log files and
   docker json logs, upload to `gs://lescall-logs/<service>/<YYYY-MM-DD>/<file>.gz`
   (same impersonated-ADC auth as recordings), delete local shipped files after 3
   days; bucket lifecycle rule deletes objects after 90 days (configurable).
5. Call-level artifacts (recordings, transcripts) already ship to GCS — unchanged.

## Order of execution (proposed)
1. Minute-metering ledger + heartbeats + reconciler + backfill + tests  ← the critical path
2. Usage API + Topbar minutes chip + Usage page rework
3. Request/action audit logging + JSON logs
4. Dockerization of api/runtime/workers + cutover
5. Log shipper → GCS + lifecycle rules
6. Cross-check alerts + RUNBOOK + burn-in (48 h shadow period comparing sources
   before the numbers are called billable)
