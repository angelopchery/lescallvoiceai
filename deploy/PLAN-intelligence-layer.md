# PLAN — Intelligence Layer Redesign & Follow-up Intelligence Engine

Status: **v1 FINAL** — audit synthesis complete (6 deep subsystem maps + direct verification
of LLM infra, deploy wiring, and auth patterns). Implementation in progress; per-phase status
tracked in the session task list.

Owner: tech@dhronai.com · Branch: `intelligence` · Date: 2026-07-20

---

## 0. Executive summary

Today, post-call intelligence is a **manual, dashboard-triggered, single-LLM-call feature**
(`POST /api/analysis/run` → gpt-4o-mini → `calls.analysis` TEXT blob) with zero automatic
triggers, a zombie follow-up backend (403-blocked endpoints, deleted UI), a producer-less outbox
queue, and no event infrastructure of any kind. The redesign replaces this with an **automatic,
event-driven intelligence pipeline** that processes every completed call with no human action:

```
Call ends (WS close → TelephonySession.stop)
  → enqueue intel job (+ sweeper catch-up scan for missed/legacy calls)
  → Intelligence Worker (new long-lived process in lescall-workers)
      stage 1: LLM intelligence extraction  (summary, scores, intent, entities, commitments…)
      stage 2: embeddings + FTS indexing    (hybrid semantic search)
      stage 3: follow-up decision engine    (YES/NO/UNSURE + confidence + schedule)
      stage 4: customer memory update
      stage 5: automation rule evaluation   (triggers → actions)
      stage 6: notifications
  → Intelligence Dashboard / Conversation workspace / Follow-ups / Reports (SPA)
```

Plus one **live-call capability**: when the caller makes a vague commitment ("call me next
week"), the agent asks exactly one polite clarification question before closing, so the
follow-up can be scheduled precisely instead of as "later".

Everything is additive-first: the legacy `calls.analysis` blob keeps being written in a
backward-compatible shape so existing consumers (ConvAnalytics, callback context builder)
keep working until they are migrated, then the manual AI Analysis module is removed.

---

## 1. Audit findings (what exists, what is wrong)

### 1.1 Current architecture in one paragraph

`TelephonySession.stop()` (conversation_call_bridge.py:498-614) is the only "call completed"
moment: it closes the audit artifacts (`recordings/<call_key>/transcript.txt` + `metadata.json`
— speaker labels **already exist**, no diarization needed), closes the billing ledger row, and
upserts the full call record into `calls` (transcript text, `collected_data` = state-manager
snapshot incl. `extra_flags`, deterministic `outcome`). Nothing fires afterwards. Analysis
happens only if a user clicks "Analyze Selected" in the SPA; results go to `calls.analysis`
(opaque TEXT JSON). Follow-up endpoints exist but are 403 for everyone (missing from
`_TENANT_API_PREFIXES`) and their UI was deleted at HEAD. The workers container is a sequential
bash loop (5-min tick) whose passes must never be starved because billing reconcile lives there
and the sentinel restart-flaps the container if `reconcile:` vanishes from logs for 20 min.

### 1.2 Key defects the redesign must fix (evidence in audit)

| # | Defect | Evidence |
|---|--------|----------|
| D1 | No automatic analysis; `PostCallConfig.scoring.auto_run` parsed but read by nothing | schema.py:557 |
| D2 | Analysis is a synchronous, uncapped, in-request loop of LLM calls | server.py:1626-1658 |
| D3 | Analysis model is OpenAI gpt-4o-mini, divergent from the Gemini live stack; key regex-scraped from `.env` | server.py:1488-1499 |
| D4 | `calls.analysis` is an opaque blob; every analytics request loads & parses ALL blobs (O(n) per request) | server.py:783-796 |
| D5 | No structured turn storage; transcript re-parsed from text file by regex; FTS = O(n) file greps | server.py:859-892, 1823 |
| D6 | Follow-up state split across 3 sources merged at read time; endpoints 403-blocked; UI deleted; cross-tenant leak in the merge (no owner filter on analysis entries) | server.py:3064-3132 |
| D7 | Vague commitments not captured: "call me next week" classifies as `time_preference`, bypassing callback capture entirely; unparseable times default to the literal string `'later'`; no clarification ever asked | conversation.py:1176-1185, 3103 |
| D8 | Phantom-booking trap partially armed: day-words book slots at ANY stage, before confirmation, with no delete path | conversation.py:2178-2261 |
| D9 | Outbox (the only real queue) has zero producers and a single hardcoded sender for all kinds | outbox.py:117-165 |
| D10 | Prompt has no current-date anchor → hallucinated follow-up dates years in the past | call_analysis.json entries |
| D11 | Two `calls` writers with different conflict keys (conn_id vs call_key) can race and silently lose whole call rows | bridge:829 vs server.py:826 |
| D12 | Schema evolution = boot-time ALTER list + regex error swallowing; no migration files, no version table | database.py:37-85,170-180 |
| D13 | Analysis scoring half-wired: score labels injected into prompt but output schema has no score field | server.py:1524-1563 vs 1504-1521 |
| D14 | Call History feed built from credit_log JSONL files + direction heuristics, not the authoritative `calls` table | server.py:1355-1404 |

### 1.3 Load-bearing invariants that MUST be preserved

1. **Teardown order** in `stop()`: audit close (writes transcript) MUST precede `_persist_call_row`;
   every step individually try/except-wrapped; everything off-loop via `asyncio.to_thread`.
2. **Never recreate `lescall-pg`**; `billsec_final` is the only billable number; ledger is
   read-only to intelligence code.
3. **Sentinel contracts**: `reconcile:` log-grep liveness; workers-loop cadence must not be
   starved by long intelligence jobs → intelligence worker is a **separate process**, not a new
   pass in the 5-min tick.
4. **Timestamps-as-strings** loader is load-bearing; new code treats DB timestamps as strings
   and parses defensively (both `T…Z` and space formats).
5. **`_TENANT_API_PREFIXES`** deny-by-default gate: every new endpoint prefix must be registered.
6. **Owner (tenant email) scoping** on every table and every query; `DEFAULT_OWNER` backstop.
7. **Golden prompt fixtures**: the two live Pramukh agents' stage prompts are byte-identical
   acceptance fixtures (`test_kaapd_e2e.py`) — in-call commitment work must not alter prompt
   bands for existing agents.
8. **Phantom-booking doctrine**: "promise recorded" ≠ "slot booked"; `_handle_callback_request`
   never books. The clarification feature must suppress the time-slot matcher on its turns.
9. **Shortcut cascade order** in `process_stream` is load-bearing; new deterministic branches
   must be inserted at a deliberate position (after wrong-number, before/with objection).
10. **`calls` upsert COALESCE semantics** (reconnect-safe) — intelligence writers use row-level
    updates that never clobber bridge-written columns.
11. **SPA served from a built image** (`docker compose build spa`); API containers are
    bind-mounted (restart to deploy). The `spa`/nginx SSE settings (`proxy_buffering off`)
    must be preserved for new streams.
12. **Sarvam key pool stays file-based** — intelligence layer does not touch it.
13. **Legacy `calls.analysis` consumers** (ConvAnalytics shape at server.py:1746-1820,
    `_build_followup_context` keys `summary/customer_intent/follow_up_reason/suggested_next_action`)
    keep working until migrated.

---

## 2. Target architecture

### 2.1 Event backbone: Postgres job queue (`intel_jobs`)

No Redis/Kafka — a 2-core/3GB host and a hard "never recreate lescall-pg" doctrine make
Postgres-native queueing (`SELECT … FOR UPDATE SKIP LOCKED`) the correct, boring, scalable
choice. It gives exactly the semantics required (at-least-once, retries, backoff, DLQ,
horizontal workers later) with zero new infrastructure, and it upgrades transparently: the
`enqueue()` call-site contract survives a future broker swap.

```sql
CREATE TABLE intel_jobs (
  id            BIGSERIAL PRIMARY KEY,
  kind          TEXT NOT NULL,              -- 'call.process' | 'report.generate' | 'automation.run' | 'followup.sweep'
  owner         TEXT NOT NULL DEFAULT '',
  call_id       BIGINT REFERENCES calls(id) ON DELETE CASCADE,  -- nullable for non-call jobs
  dedupe_key    TEXT UNIQUE,                -- e.g. 'call.process:<conn_id>'
  payload       TEXT NOT NULL DEFAULT '{}', -- JSON
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed(dead)|cancelled
  attempts      INT  NOT NULL DEFAULT 0,
  max_attempts  INT  NOT NULL DEFAULT 5,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_by    TEXT, claimed_at TIMESTAMPTZ,
  last_error    TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at   TIMESTAMPTZ
);
CREATE INDEX idx_intel_jobs_ready ON intel_jobs(status, next_attempt_at);
```

- **Producers**: (a) bridge teardown enqueues `call.process` (try/except, to_thread, never
  raises — same pattern as metering hooks); (b) a **sweeper** inside the intelligence worker
  scans `calls` for finished rows without intelligence every 60s (the `sync_pending` idiom) —
  this is the guarantee that *every* call is processed even if the enqueue was lost, and it is
  what backfills the 88 historical calls.
- **Consumer**: new long-lived process `python -m shared.intelligence.worker` in the
  lescall-workers container (started by `workers_entry.sh` as a second process; the sequential
  billing loop is untouched). Claim loop: `UPDATE … SET status='running' WHERE id = (SELECT id
  FROM intel_jobs WHERE status='pending' AND next_attempt_at<=now() ORDER BY id FOR UPDATE
  SKIP LOCKED LIMIT 1) RETURNING *`. Concurrency: small asyncio pool (default 2 jobs) —
  LLM-bound, not CPU-bound. Retries: exponential backoff (60s·2^n, cap 1h), `failed` = DLQ
  after max_attempts; a `/api/intel/jobs` admin surface lists DLQ rows and allows re-drive.
- **Idempotency**: every stage checks its checkpoint before running; jobs are deduped on
  `call.process:<conn_id or call_key>`.
- **Watchdog**: worker heartbeats into `settings(owner='', key='intel_worker_beat')`; sentinel
  gains one check (log-grep pattern `intel:` — additive, does not touch the `reconcile:` contract).

### 2.2 The per-call pipeline (stages inside one `call.process` job)

One job per call executes ordered stages with **per-stage checkpointing** in
`call_intelligence.stages` (JSON: `{extract: 'done', embed: 'done', followup: …}`), so a retry
resumes at the failed stage instead of re-paying LLM calls:

1. **extract** — one structured LLM call (Gemini, JSON schema, current-date anchored, agent
   post_call config + collected_data + outcome as context) producing the full intelligence
   record (§2.3). Writes `call_intelligence` + normalized child rows (commitments, action
   items) + a legacy-shaped `calls.analysis` blob (backward compat, D13 fixed: scores included).
2. **embed** — chunk transcript (by turns, ~600 tokens, 1-turn overlap) + the AI summary;
   Gemini `gemini-embedding-001` (768-dim Matryoshka, multilingual — handles Hinglish/Gujarati);
   rows into `call_embeddings` (pgvector). FTS tsvector columns are generated in-DB.
3. **followup** — decision engine (§2.5): deterministic signals first (in-call clarified
   commitment, `callback_requested` flags, agent YAML follow_up config), the extract-stage's
   commitment list second; produces `followups` rows or an explicit NO with explanation.
4. **memory** — merge call facts into `customer_memory` for the contact (§2.7).
5. **automation** — evaluate enabled `automation_rules` against the event `call.analyzed`
   (and any emitted sub-events: `followup.created`, `sentiment.negative`, …); matches spawn
   `automation.run` jobs (§2.6).
6. **notify** — write `notifications` rows (in-app bell + SSE) per user prefs.

Failure isolation: stages 2-6 failing never un-does stage 1; each stage's errors are recorded
in the checkpoint map; the job only retries stages not yet `done`.

### 2.3 Data model (new tables — all PG migrations, §3)

**`call_intelligence`** — one row per call, scalar columns for everything filterable:

```
call_id BIGINT UNIQUE REFERENCES calls(id) ON DELETE CASCADE,
owner TEXT NOT NULL, contact_id BIGINT, agent_slug TEXT,
schema_version INT, model TEXT, processed_at, stages TEXT(JSON checkpoint map),
-- executive summary
summary TEXT, outcome_summary TEXT, customer_intent TEXT,
conversation_quality REAL, conversion_probability REAL, satisfaction REAL,
urgency TEXT (low|medium|high|critical), confidence REAL,
-- scores (0-100)
lead_score INT, opportunity_score INT, risk_score INT, compliance_score INT,
sentiment TEXT (positive|neutral|negative), sentiment_journey TEXT(JSON per-phase),
-- intelligence detail (JSON lists, each element {text, quote, turn_ref})
topics TEXT(JSON), pain_points TEXT(JSON), objections TEXT(JSON),
buying_signals TEXT(JSON), competitor_mentions TEXT(JSON), product_interest TEXT(JSON),
budget_discussion TEXT(JSON), timeline_discussion TEXT(JSON), decision_makers TEXT(JSON),
compliance_issues TEXT(JSON), escalations TEXT(JSON), risk_indicators TEXT(JSON),
agent_performance TEXT(JSON {score, strengths, improvements}),
key_moments TEXT(JSON timeline events), recommendations TEXT(JSON next actions/emails/docs),
entities TEXT(JSON), tags TEXT(JSON)
```
Indexes: `(owner, processed_at)`, `(owner, lead_score)`, `(owner, sentiment)`, `(call_id)`.

**`commitments`** — `call_id FK, owner, made_by (agent|customer), kind (callback|send_document|
whatsapp|meeting|quote|payment|visit|other), description, quote, deadline_text, deadline_at,
is_vague BOOL, clarified BOOL, clarified_value TEXT, source (in_call|post_call_llm), confidence
REAL, status (open|fulfilled|cancelled)` — the normalized answer to "every promise made on
every call", queryable without JSON parsing (fixes D5-adjacent debt at conversation.py extra_flags).

**`followups`** — replaces the legacy `follow_ups`/`followup_automations` pair for new writes
(legacy tables left untouched for rollback; their 4+2 live rows migrated):

```
id BIGSERIAL, owner, call_id FK, contact_id BIGINT, commitment_id BIGINT,
decision TEXT (yes|no|unsure), confidence REAL, explanation TEXT,
reason TEXT, summary TEXT, followup_type TEXT (callback|send_info|meeting|visit|payment|other),
promised_by TEXT (customer_requested|agent_promised|internal|external_dependency),
scheduled_at TIMESTAMPTZ, timezone TEXT DEFAULT 'Asia/Kolkata', window_start/window_end,
priority TEXT (low|medium|high|urgent), status TEXT (pending|scheduled|completed|cancelled|
  waiting_customer|escalated|missed), assigned_to TEXT, reminder TEXT(JSON),
source TEXT (pipeline|in_call|manual), notes TEXT(JSON list), audit TEXT(JSON list),
created_at, updated_at, completed_at
```
`decision='no'` rows ARE stored (with explanation) — that is the explainability surface for
"why no follow-up"; the dashboard's Archived tab reads them. Overdue detection: worker sweep
marks `scheduled_at < now() AND status IN (pending,scheduled)` → `missed` + emits
`followup.overdue` event.

**`customer_memory`** — `contact_id BIGINT UNIQUE, owner, memory TEXT(JSON: {preferences[],
promises[], objections[], purchases[], followup_history[], communication_style, decision_makers[],
risk_level, relationship_notes[]}), calls_count INT, last_call_at, updated_at, version INT`.
Injected into future calls through the **existing** `call_links.context_note` mechanism
(server.py:3419 → bridge:421 → conversation.py:729) — zero new runtime plumbing.

**`automation_rules`** — `id, owner, name, description, enabled BOOL, trigger TEXT
('call.analyzed'|'followup.created'|'followup.overdue'|'sentiment.negative'|'lead_score.gte'|
'commitment.detected'|'compliance.issue'|…), conditions TEXT(JSON tree {all:[…]|any:[…],
field, op, value}), actions TEXT(JSON ordered list [{type, params, delay_s}]), version INT,
created_by, created_at, updated_at`. Versioning: `automation_rule_versions` snapshot on every
edit (the `agent_revisions` pattern, retention 50).

**`automation_runs`** — `id, rule_id, rule_version, owner, trigger_event TEXT(JSON), status
(pending|running|done|failed|cancelled), steps TEXT(JSON per-action log w/ timestamps+results),
attempts, next_attempt_at, error, created_at, finished_at` — the execution log surface.
Loop safeguard: per (rule, call) dedupe + max 20 runs/rule/hour circuit breaker.

**`call_embeddings`** — PG-only: `id, call_id FK, owner, kind (chunk|summary), chunk_index,
content TEXT, embedding vector(768), tsv tsvector GENERATED (to_tsvector('simple',
unaccent(content)))`; HNSW index on embedding (cosine), GIN on tsv, `(owner, call_id)`.

**`reports`** — `id, owner, title, query_prompt, filters TEXT(JSON), call_ids TEXT(JSON),
call_count INT, status (queued|running|done|failed), document TEXT (markdown), sections
TEXT(JSON), model, error, created_at, completed_at, created_by`.

**`notifications`** — `id, owner, kind, severity, title, body, link, ref TEXT, read_at,
created_at`; index `(owner, created_at)`, partial on `read_at IS NULL`.

**`intel_jobs`** — §2.1.

FK discipline: all new tables FK `calls(id) BIGINT ON DELETE CASCADE` and use BIGINT for
contact refs (existing `calls.contact_id INTEGER` mismatch is documented debt, not copied).
Soft-delete: `followups/automation_rules/reports` get `deleted_at` columns; queries filter it.

### 2.4 Extraction LLM design

- **Client**: OpenAI-compat AsyncOpenAI against the Gemini endpoint. Dedicated env block
  `INTEL_LLM_BASE_URL/INTEL_LLM_MODEL/INTEL_LLM_API_KEY` (falls back to the live-call `LLM_*`
  values) so background load can be pointed at a separate key/quota and never starves live
  calls. `[PENDING-AUDIT: exact env names + quota notes from llm-infra audit]`
- **Prompt**: single system prompt, versioned (`schema_version` stamped on rows), containing:
  current date/time in Asia/Kolkata (fixes D10), agent identity + mission + stage goals +
  outcome taxonomy + scoring criteria from `post_call` config (extends the existing
  `_agent_scoring_context` approach), the deterministic facts (outcome, collected_data flags,
  billsec, direction, contact history summary from customer_memory), then the speaker-labeled
  transcript. Output: strict JSON schema (all §2.3 fields incl. commitments list w/ quotes).
- **Robustness**: JSON parse with one repair-retry; hallucination guards (dates must be
  >= call date; quotes must appear in transcript for commitments — fuzzy substring check;
  scores clamped). Confidence < threshold → fields stored but flagged `low_confidence`.
- **Cost control**: transcripts truncated to a token budget middle-out (keep head+tail);
  calls with empty/1-turn transcripts get a deterministic minimal record, no LLM call.

### 2.5 Follow-up decision engine

Deterministic-first, LLM-second, explainable always:

1. If an **in-call clarified commitment** exists (§2.8) → `decision=yes`, confidence 0.98,
   schedule at the clarified time. Source `in_call`.
2. Else if deterministic flags (`callback_requested` + parseable `callback_time`,
   `whatsapp_promised`, booked appointment needing prep) → `yes` with mapped type/window.
3. Else use the extract-stage commitments + LLM's followup judgment: the extraction schema
   contains `follow_up: {required: yes|no|unsure, confidence, reason, who_promised,
   expected_outcome, priority, suggested_window {start,end}, needs_human_review}`.
   `unsure` or confidence < 0.6 → `decision=unsure`, status `pending` review, explanation
   stored, surfaced in the dashboard's "Needs review" filter.
4. `no` → archived row with explanation (no workflow starts).
Scheduling: vague windows resolve to concrete `scheduled_at` via deterministic rules
(e.g. "next week" → Tue 11:00 next week, "evening" → 18:00) in Asia/Kolkata, flagged
`auto_scheduled` so the UI shows they're AI-picked. The legacy auto-dial path
(`followup_automations` + 30s scheduler) is retired in favor of automation-rule action
`schedule_call` writing through the same `_dial_automation_entry`-equivalent service —
with its cross-tenant `DEFAULT_OWNER` contact-update bug (server.py:3437-3439) fixed.

### 2.6 Automation engine

Deliberately **not** a graph runtime: rules = trigger + condition tree + ordered action list
with per-action `delay_s`, retries, and run logs. That covers every listed trigger/action
scenario, executes deterministically, versions cleanly, and fits the queue. (Nested loops/
parallel branches are out of scope v1 — safeguarded loops exist only as rule-level circuit
breakers; the schema leaves room for a `flow` JSON for a future graph executor.)

- **Events**: pipeline stages emit typed events (`call.analyzed`, `commitment.detected`,
  `followup.created/overdue`, `sentiment.negative`, `lead_score.changed`, `compliance.issue`,
  `escalation`, `competitor.mentioned`, `cancellation.intent`, `refund.request`, …).
  Event = trigger match → condition tree evaluated against a flat context doc
  (call_intelligence scalars + call fields + contact fields) → `automation.run` job.
- **Actions v1** (each an idempotent handler with its own result log):
  `notify_inapp`, `email` (sendmail — the sentinel's existing mechanism), `webhook`
  (through the outbox with per-kind sender dispatch — finally giving it a producer and
  fixing D9), `create_followup`, `assign_followup`, `add_tag`, `update_contact_field`,
  `schedule_call` (the retired auto-dial, done right), `escalate` (notify + priority bump),
  `generate_report` (enqueue report job). SMS/WhatsApp/Slack/Teams/CRM-stage actions ship as
  config-validated stubs that no-op with a logged "channel not configured" result until
  credentials exist — honest degradation, not silent failure.
- **Templates**: 5 seeded rule templates (hot-lead alert, overdue-followup escalation,
  negative-sentiment manager notify, competitor-mention tag+notify, compliance-issue escalate).

### 2.7 Customer memory

Merge policy: append-with-dedupe for lists (preferences, objections, promises), last-write for
style/risk; every entry carries `{call_id, date}` provenance. Injection: when an outbound call
is placed (or an inbound call matches a known contact), the bridge already passes
`context_note`; a compact memory digest (≤600 chars, most recent + highest-signal facts) is
appended to it. Full memory visible in the Conversation workspace and Contacts page.

### 2.8 In-call vague-commitment clarification (live engine)

Smallest possible surgical change, per the agent-engine audit's injection map:

- **Detector** (deterministic, no LLM): extends `_detect_callback_request` patterns with vague
  commitment phrases in en/hi/gu ("next week", "later", "kal baat karte hain", "call karna",
  "I'll get back", "sometime", "baad mein" …). Classifies `{kind, has_day, has_time}` —
  vague = missing day or time.
- **Mid-call capture**: a new deterministic branch positioned **with** the objection branch in
  the cascade (respecting order invariant #9): on vague commitment → record a typed commitment
  into a new `state_mgr.commitments` list (additive `to_dict()` key — established pattern),
  set `extra_flags`, and — once per call, flag-guarded — respond with a canned clarification
  from the response bank ("Sure — what day works best?", localized per language). The turn
  **suppresses `_extract_time_preference`** (invariant #8: a "haan, tomorrow" answer must
  refine the commitment, not create a calendar booking) via a one-turn suppression flag
  checked in `_extract_data_from_input`.
- **Clarified answer**: next turn's day/time extraction routes into the commitment record
  (`clarified=true, deadline_at`), NOT into bookings, while the suppression flag is armed.
- **Farewell checkpoint**: in the farewell branch (conversation.py:1132-1147), if an
  unclarified vague commitment exists and the clarification hasn't been asked → ask it
  instead of the closing line (once), then close on the following turn.
- **Config**: `follow_up.clarify_commitments: true|false` in agent YAML (default **false**
  for existing agents — golden fixtures untouched; enabled per-agent, default true for
  newly built agents). No prompt-band changes at all — pure deterministic layer.
- **Persistence**: commitments ride `collected_data` into the calls row; the pipeline's
  followup stage reads them as source `in_call`.

### 2.9 Hybrid semantic call search (Call History fuzzy filter)

- **pgvector 0.8.1 compiled from source inside lescall-pg** against its exact PG 16.14
  (server headers verified present; Alpine 3.21). Provisioning script
  `deploy/pg_provision_pgvector.sh` (build + `CREATE EXTENSION vector; CREATE EXTENSION
  pg_trgm; CREATE EXTENSION unaccent;`) + RUNBOOK §: re-run after any (forbidden, but
  disaster-recovery) container recreation. Extension survives restarts (container writable
  layer persists).
- **Index-time** (pipeline stage 2): chunk + embed (gemini-embedding-001, 768-dim,
  `task_type=RETRIEVAL_DOCUMENT`) → `call_embeddings`; tsvector generated column.
- **Query-time** (`GET /api/intel/search?q=…&filters`): embed query
  (`task_type=RETRIEVAL_QUERY`, ~80-150ms) → in parallel: (a) HNSW cosine top-40 chunks,
  (b) FTS `websearch_to_tsquery('simple', unaccent(q))` top-40 + trigram fallback for short
  queries → **Reciprocal Rank Fusion** (k=60) at the call level (best chunk per call) →
  return ranked calls with matched-snippet highlights. Owner-scoped in every arm. Embedding
  API down → lexical-only with a `degraded: true` flag in the response. Structured filters
  (date, agent, sentiment, score ranges) compose as SQL WHERE before ranking.
- Optional **query understanding**: a fast LLM pass extracting structured filters from the
  prompt ("complaints about product quality last month" → sentiment=negative + date range +
  semantic残query) — v2, behind a flag; RRF alone is strong.

### 2.10 Reports

`POST /api/reports {title?, query_prompt, call_ids[] | filters}` → `report.generate` job →
worker loads the selected calls' `call_intelligence` rows (never raw transcripts unless
< 10 calls — token budget), synthesizes via LLM into a structured markdown document
(exec summary, key findings, trends, per-call appendix, recommended actions) → stored in
`reports` → SSE/notification on completion → Reports page lists, opens (rendered markdown),
downloads (.md). Report over >200 calls: map-reduce (batch summaries → final synthesis).

### 2.11 API surface (all under `/api/intel/*`, `/api/followups2` → final names below; every
prefix registered in `_TENANT_API_PREFIXES`; every query owner-scoped)

```
GET    /api/intel/calls                 unified dashboard rows (paginated, filter/sort params)
GET    /api/intel/calls/{call_key}      full conversation-intelligence workspace payload
GET    /api/intel/search?q=             hybrid semantic search (§2.9)
GET    /api/intel/status                pipeline health: queue depth, DLQ count, last processed
GET    /api/intel/jobs?status=failed    DLQ list; POST /api/intel/jobs/{id}/retry
GET/POST/PATCH/DELETE /api/intel/followups[…]   follow-up CRUD + complete/reschedule/reassign
GET    /api/intel/followups/board       today / upcoming / overdue / done buckets
GET/POST/PATCH/DELETE /api/intel/automations[…] rules CRUD + enable/disable + test-run
GET    /api/intel/automations/{id}/runs         execution logs
GET/POST /api/intel/reports[…]          reports list/create/get/delete
GET    /api/intel/notifications         + PATCH read; GET /api/intel/notifications/stream (SSE)
GET    /api/intel/memory/{contact_id}   customer memory
PATCH  /api/intel/action-items/{id}     edit/complete action items
GET    /api/intel/stream                SSE: pipeline events for real-time dashboard updates
```

Backward-compat kept during transition: `/api/analytics/conversation` (fed from
`call_intelligence` scalars via SQL aggregation — fixes D4), per-call
`/api/calls/recordings/{folder}/analysis` (served from the compat blob).

### 2.12 Frontend (SPA conventions per audit: nav.ts + App.tsx registration, kit.tsx tables,
CSS-var theming, SSE via useSSE, server pagination at 50)

New/changed pages in the **Intelligence** nav group:
1. **Intelligence** (`/intelligence`) — the unified dashboard: KPI StatCards (calls analyzed,
   follow-ups due, avg lead score, negative-sentiment count), then the master table
   (customer, company?, phone, agent, date, duration, sentiment, lead/opportunity/risk/
   compliance scores, intent, follow-up chip, automation status, priority, tags), server
   pagination + column-set picker (localStorage), saved filter views, CSV export, bulk
   actions (tag, assign follow-up, generate report), live updates via `/api/intel/stream`.
2. **Conversation workspace** (`/intelligence/:callKey`) — executive summary band,
   score ring row, transcript panel (timestamped, speaker-labeled, search + jump + copy +
   download, keyword highlight), intelligence sections (topics, objections, buying signals,
   competitors, compliance, emotional journey sparkline, agent performance), AI
   recommendations, timeline (key moments), action items (editable), commitments list,
   follow-up card (with decision + explanation + confidence), customer memory sidebar,
   audio player, "Reanalyze" (admin-ish escape hatch that enqueues a fresh job).
3. **Follow-ups** (`/followups`) — board tabs (Today / Upcoming / Overdue / Completed /
   Cancelled / Waiting / Escalated / Missed / Needs-review / Archived-no), row actions
   (edit, reschedule, complete, cancel, reassign, notes), linked call + explanation.
4. **Automations** (`/automations`) — rules list, enable toggle, editor (trigger picker,
   condition rows, action list with params), run history with step logs, templates gallery.
5. **Reports** (`/reports`) — list + status chips, report viewer (rendered markdown),
   "Generate report" entry also lives on Call History/Intelligence selection toolbar.
6. **Call History (Activity)** — gains the semantic filter box ("Describe the calls you're
   looking for…") calling `/api/intel/search`, result chips show match snippets; selection
   checkboxes + "Generate report" button.
7. **Removed at the end**: AiAnalysis page + nav item; ConvAnalytics repointed to the new
   aggregation (page kept, backend swapped).

Design system: existing tokens/components only (Card/Table/StatusBadge/StatCard/Sparkline/
SlideOver/AsyncState); STATUS_TONE map extended with new vocab (scores tiers, followup
statuses, decisions). Dark mode + 36px tap targets per convention. No new chart lib.

### 2.13 Notification engine

In-app first: `notifications` table + SSE stream + Topbar bell (exists, currently
health-only — extended). Outward: email (sendmail) and webhook (outbox with per-kind sender
dispatch). Everything else via automation stubs (§2.6). Digest: daily summary notification
(followups due today, overnight analyzed calls) generated by the worker at 08:00 IST.

---

## 3. Database migration mechanism (new, production-grade)

The boot-time ALTER list (D12) is unfit for this scale of schema work. Introduce **versioned
SQL migration files** without touching the legacy mechanism:

```
shared/db/migrations/
  0001_intel_core.sql          (intel_jobs, call_intelligence, commitments, followups,
                                action items in call_intelligence JSON → separate table
                                `action_items`, notifications)
  0002_intel_search.sql        (PG-only: CREATE EXTENSION vector/pg_trgm/unaccent,
                                call_embeddings, HNSW + GIN indexes)
  0003_intel_automation.sql    (automation_rules, automation_rule_versions, automation_runs)
  0004_intel_memory_reports.sql(customer_memory, reports)
  0005_intel_backfill.sql      (data migration: legacy follow_ups/followup_automations rows,
                                calls.analysis blobs → call_intelligence where parseable)
```

Runner `python -m shared.db.migrate` (new): `schema_migrations(version PRIMARY KEY, applied_at,
checksum)` table; applies pending files in order inside transactions (except CREATE INDEX
CONCURRENTLY / CREATE EXTENSION which run autocommit); `--dry-run` prints; refuses checksum
drift. Invoked **manually at deploy** (documented in RUNBOOK), never at boot — three
containers racing DDL stays a legacy-tables-only behavior. Legacy MIGRATIONS list remains
for old tables; new tables live exclusively in files. sqlite dev fallback: migrations 0002's
vector DDL is guarded (`-- pg-only` pragma the runner respects); embedding stage no-ops on
sqlite.

Rollback: each file has a paired `NNNN_name.down.sql`; `migrate --down <version>` for the
testing environment. (Prod rollback = restore from pg_backup dumps per existing doctrine.)

---

## 4. Implementation phases (each ends: build passes, containers healthy, migrations applied,
logs clean, feature smoke-tested; nothing proceeds while its predecessor is red)

| Phase | Scope | Validation gate |
|-------|-------|-----------------|
| **P1** | pgvector provision script + run; migration runner + 0001-0004; `shared/intelligence/` package skeleton (config, db helpers, job queue lib w/ tests) | `\dx` shows vector; schema_migrations rows; unit tests green; all containers healthy |
| **P2** | Extraction stage + worker process + bridge enqueue + sweeper + backfill of 88 calls; compat `calls.analysis` writes; `/api/intel/status` | every historical call has `call_intelligence`; ConvAnalytics still renders; live-call smoke test analyzes automatically |
| **P3** | Embeddings stage + hybrid search endpoint + Call History filter UI | NL queries return sane ranked calls < 300ms server-side (excl. embed latency); degraded mode works with embeddings disabled |
| **P4** | Follow-up decision engine + followups API + Follow-ups page; overdue sweep; legacy row migration | decisions + explanations visible; YES calls produce scheduled followups; NO calls archived with reason |
| **P5** | In-call clarification (deterministic branch + suppression + farewell checkpoint) behind agent YAML flag; e2e test with scripted dialogue; golden fixtures verified unchanged | test_kaapd_e2e.py green; new engine test proves: vague promise → 1 question → clarified commitment, no booking row |
| **P6** | Automation engine (rules, runs, actions v1, templates) + Automations UI + outbox sender dispatch | template rule fires end-to-end on a test call; run logs visible; circuit breaker test |
| **P7** | Customer memory + context_note injection + workspace/Contacts surfacing | second call from same contact carries memory digest |
| **P8** | Intelligence Dashboard + Conversation workspace pages + notifications/SSE | SPA rebuilt+deployed; realtime row appears when a call completes |
| **P9** | Reports engine + Reports page + selection toolbar | filtered-selection → report generated async → viewable/downloadable |
| **P10** | Legacy removal: AiAnalysis page+nav, `/api/analysis/run` manual path (endpoint kept returning 410 pointer), dead JSON stores archived, ConvAnalytics repointed; docs (RUNBOOK, CLAUDE.md stale sections incl. the nonexistent `backend/app/agent/`) | no references to removed routes; full regression pass |

Deploy per phase: api/workers/runtime = `docker compose restart` (bind-mount); SPA phases =
`docker compose build spa && up -d spa`; migrations applied manually before restart.
Test layers: unit (queue lib, decision engine, detector patterns, RRF), integration
(pipeline against a fixture transcript in sqlite+PG), e2e (existing `test_kaapd_e2e.py`
acceptance floor + new `test_intel_e2e.py` driving a synthetic call through the full chain).

## 5. Formerly open questions — RESOLVED (verified 2026-07-20)

1. **LLM infra** (verified by live probes): `calling-agent/.env` carries
   `LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/`,
   `LLM_MODEL=gemini-3.1-flash-lite`, `LLM_API_KEY` (= GEMINI_API_KEY),
   `LLM_REASONING_EFFORT=none`. The key's `/models` list includes `gemini-2.5-flash`
   (extraction default — separate quota bucket from the live-call model) and
   `gemini-embedding-001`; the OpenAI-compat `/embeddings` endpoint works with
   `dimensions: 768` (probe returned a 768-dim vector for Hinglish input). Two traps
   confirmed and handled: thinking tokens count against `max_tokens`
   (`reasoning_effort` set explicitly, budget 10k), and the embeddings response returns
   `index: null` (response order trusted when indices are absent).
2. **Deploy**: workers run `bash deploy/workers_entry.sh` off the bind mount — restart
   deploys. The intel worker is a separate supervised process in that script; the
   sequential billing tick is untouched, so the sentinel's `reconcile:` contract is safe.
   Ops follow-up: optionally add an `intel:` log-grep check to shared/sentinel.py.
3. **Auth**: new endpoints follow `_acting_owner(request)` for tenancy and MUST be added
   to `_TENANT_API_PREFIXES` in dashboard/auth.py (deny-by-default gate).
