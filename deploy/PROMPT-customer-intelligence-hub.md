# Mega Prompt — Customer Intelligence Hub, Persistent Customer Memory & Ultra-Low-Latency Pre-Call Context

> **How to use this file:** paste it (or reference it) as the driving prompt for the implementation
> session. It is the original "world's most intelligent AI calling platform" brief, rewritten
> against the *actual* LesCall codebase on this VM — real tables, real files, real hook points,
> real deploy chain. Everything in §1 already exists and must not be rebuilt. The work is §3–§8.

---

## 0. Role

You are acting as Chief AI Architect, Principal Engineer, and Database Architect for the LesCall
platform. You own architectural decisions: where this brief's suggestions are weaker than what the
codebase already supports, implement the better design and document the trade-off in the plan file.
Do not blindly implement — engineer.

**Think first.** Before writing code, re-verify the ground truth in §1 against the live tree
(branch state changes fast here). Only then implement, phase by phase, validating each phase per §9.

---

## 1. Ground truth — what already exists (do NOT rebuild)

### 1.1 Deployment topology (single GCP VM, Docker, host network)

| Container | Image | Role | Code delivery |
|---|---|---|---|
| `lescall-api` | `lescall-app:latest` | FastAPI dashboard backend `dashboard/server.py` on :8080 | **repo bind-mounted** → deploy = `docker compose restart api` |
| `lescall-runtime` | `lescall-app:latest` | `socket_server.py` (:9000 ESL) + `ws_audio_receiver.py` (:9001 media WS) | bind-mounted → restart |
| `lescall-workers` | `lescall-app:latest` | 5-min tick loop (metering/outbox/recsync/logship) + **supervised long-lived intel worker** (`python -m shared.intelligence.worker`) | bind-mounted → restart |
| `lescall-dashboard` (spa) | `lescall-dashboard:latest` | nginx serving the **baked** Vite build, :8081 | SPA changes require **image rebuild** (`docker compose -f deploy/docker-compose.services.yml up -d --build spa`) |
| `lescall-pg` | `postgres:16-alpine` + pgvector 0.8.1 compiled in-container | THE database | **NEVER recreate this container** — it holds billing + call records, and pgvector is compiled inside it (`deploy/pg_provision_pgvector.sh`); recreation loses the extension build |

Backend image rebuild is only needed when `requirements.txt` changes. Env comes from
`deploy/.pg_env` (`LESCALL_DB_URL`, `INTEL_*`, GCS buckets) + `calling-agent/.env`.

### 1.2 Database layer (`shared/db/`)

- **Two schema mechanisms, deliberately split:**
  1. Core tables: `shared/db/schema.sql` + idempotent `MIGRATIONS` ALTER list in
     `shared/db/database.py`, auto-applied at boot, runs on **both** SQLite (dev fallback) and
     Postgres.
  2. Intelligence tables: versioned `shared/db/migrations/NNNN_name.sql` run by
     `python -m shared.db.migrate` — **Postgres-only**, SHA-256 checksum-tracked in
     `schema_migrations`, **checksum drift on an applied file aborts loudly** (never edit an
     applied migration; add a new one). Each file needs a paired `NNNN_name.down.sql`.
     Applied manually: `docker exec -w /home/angelo/lescall lescall-workers python -m shared.db.migrate`.
     **Next free number: 0006.**
- **Connection model:** `get_db()` opens a fresh psycopg3 connection per call (no pool), rewrites
  `?`→`%s`, and returns **timestamps as strings** (load-bearing — dashboard `json.dumps`'s rows).
  Keep both quirks intact.
- **Contact identity:** `contacts` is owner-scoped, `UNIQUE(owner, phone)` +
  `UNIQUE(owner, sig_digits)` (last-10-digit dedup). `calls.contact_id` (int FK-by-convention) is
  **auto-bound at call end** (`conversation_call_bridge.py:_resolve_contact_id`) and a bare contact
  is auto-created for unknown callers — **every finished call row has a contact_id**. The same
  contact_id threads through `call_intelligence`, `commitments`, `followups`, `usage_ledger`,
  `customer_memory`.

### 1.3 Post-call intelligence layer (LIVE — `shared/intelligence/`)

- **Queue:** `intel_jobs` table, `FOR UPDATE SKIP LOCKED` claim, dedupe-key idempotent enqueue,
  exponential backoff (60s→3600s), max 5 attempts → DLQ, stale-job release. At-least-once;
  handlers idempotent.
- **Pipeline** (`pipeline.py`): `call.process` enqueued at call teardown; stages
  `extract → embed → followup → memory → automation → notify` with **per-stage checkpointing** in
  `call_intelligence.stages` — retries never repay finished stages, and the 60s **sweeper
  automatically retrofits newly-registered stages onto old calls**. This means: *a new stage you
  add gets backfilled across historical calls for free.*
- **Extraction** (`extract.py`): one structured LLM call (`INTEL_LLM_MODEL`, default
  `gemini-2.5-flash` — deliberately a separate quota from the live-call `gemini-3.1-flash-lite`),
  date-anchored IST prompt, hallucination guard (commitment quotes must fuzzy-match the
  transcript), past-date nulling, one repair retry. Produces the full `call_intelligence` record:
  summaries, lead/opportunity/risk/compliance scores, sentiment journey, topics, objections,
  buying signals, pain points, decision makers, entities, agent performance, key moments,
  recommendations, commitments, action items, follow-up judgment. `SCHEMA_VERSION` stamped.
- **Semantic memory (per-call):** `call_embeddings` — `vector(768)` (`gemini-embedding-001`,
  Matryoshka-truncated, L2-normalized), HNSW cosine + generated `tsvector` + trigram GIN. Hybrid
  search = semantic ∪ lexical fused with RRF k=60, graceful lexical-only degradation
  (`search.py`).
- **Follow-ups v2:** deterministic-first decision engine (`followup.py`) — clarified in-call
  commitment (conf 0.98) > live `callback_requested` flag (0.95) > LLM judgment; every decision
  (incl. NO) persisted with audit trail; auto-scheduling; overdue sweep.
- **Automations:** versioned rules, condition trees, action checkpointing, per-(rule,call)
  dedupe, 20-runs/hour circuit breaker. **Reports:** map-reduce LLM synthesis over stored
  digests. **Notifications:** in-app.
- **Customer memory (EXISTS but write-only):** `customer_memory` table — one row per
  `contact_id`, JSON doc with capped, deduped, provenance-tagged lists (preferences, objections,
  promises, decision_makers, relationship_notes, followup_history) + risk_level/last_intent/
  last_sentiment, `calls_count`, `version`. `digest_for_contact()` renders a ≤600-char prompt
  digest. Read API: `GET /api/intel/memory/{contact_id}`.
- **THE GAP:** the digest is **never injected anywhere**. `stage_extract` passes
  `memory_digest = ""` (`pipeline.py` ~line 219) and the live-call runtime never reads
  `customer_memory` or past calls. **Every call today starts as a stranger.** Closing this gap is
  the heart of this project.

### 1.4 Live-call runtime & the pre-call hook points

Sequence: FreeSWITCH → `socket_server.py` (WS URL carries uuid/caller/did/campaign/slug) →
`ws_audio_receiver.py:handle_connection` → `conversation_call_bridge.py:SessionManager.get_or_create`
→ `TelephonySession.create` → **`_resolve_call_routing` (runs in `asyncio.to_thread`, ~250ms of
serial DB reads: recent `call_links` dial, agent slug, agent owner)** → `TelephonySession.__init__`
(agent load + preflight cache, then `set_call_context`/`set_callback_context` for outbound
name/phone/context_note) → pipeline start → greeting.

Latency facts that shape the design:
- The greeting is **canned TTS** (`generate_opening`), gated only on the first audio frame — it
  does not wait for the LLM.
- The **first LLM turn fires on the caller's first reply** — there are seconds of slack between
  session creation and the first prompt build.
- The system prompt is rebuilt every turn in `conversation.py:_build_system_prompt` from ordered
  bands (`prompt_blocks.py`): identity/mission/direction/brand → `━━ CALL STATE ━━` → state-manager
  slots → active context → stage goal → agent-YAML bands → QA no-re-ask → context memory → RAG.
- Outbound calls already inject `contact_name`/`phone` and a "WHY YOU ARE CALLING BACK" block from
  `call_links.context_note` — the injection *pattern* exists; it just carries no historical memory.

**Natural hook points (verified):**
1. `_resolve_call_routing` (bridge ~line 286) — already off-loop, already has owner + phone;
   add the context fetch here.
2. `TelephonySession.__init__` right after `set_call_context` (~lines 481–498) — hand the fetched
   context to the router via a new setter beside `set_call_context`/`set_callback_context`
   (`conversation.py:729/756`).
3. A new render band in `prompt_blocks.py` beside `direction_block` (line ~170).

### 1.5 Dashboard & frontend

- **Tenancy:** owner = user email (`dashboard/auth.py:current_owner`); Google OAuth in
  multi-tenant mode; **roles are deliberately flattened — `is_admin()` returns False, there is no
  admin**. Deny-by-default middleware: any `/api/` prefix not in `_TENANT_API_PREFIXES`
  (`auth.py:~408`) returns 403 — **new endpoints must live under an allowlisted prefix**
  (`/api/intel` and `/api/outbound` already are) **or be added to the list**.
- **Contacts today** (`frontend/src/pages/Contacts.tsx`, `/contacts`): flat table (name, phone,
  gender, group, details preview), groups sidebar, CSV import, add/edit/delete. **Rows are not
  clickable; there is no contact detail view; no calls, memory, or intelligence surfaced.** Dial
  lives on `/outbound`. Backing APIs: `/api/outbound/contacts*`, `/api/outbound/groups*`
  (`dashboard/server.py` ~2729–2960).
- **The reference implementation for a detail workspace:** `IntelligenceCall.tsx`
  (`/intelligence/:callKey`, backed by `GET /api/intel/calls/{call_key}`) — three-column layout,
  `RadialRing` score gauges, transcript viewer, commitments/action-items/memory side panel.
- **Conventions:** React+TS+Vite, React Router v6 flat routes in `App.tsx`, nav in `lib/nav.ts`,
  TanStack Query via `useGet<T>(key[], path, {refetchInterval})`, mutations via
  `post/patch/del` in `api/mutate.ts`, typed API modules in `api/*.ts`, UI kit in
  `components/kit.tsx` + `ui.tsx` (PageHeader, Table, Badge, StatusBadge, AsyncState, Modal,
  Toast, RadialRing), Tailwind + CSS-variable theming with light/dark.

### 1.6 Tests that pin behavior (acceptance floor)

`calling-agent/backend/tests/test_intel_{queue,extract,stages,automation}.py` (queue lifecycle,
SKIP-LOCKED concurrency, hallucination-quote drop, 6-stage e2e with mocked LLM),
`test_commitment_clarify.py`, `test_continuation_merge.py`, and `test_kaapd_e2e.py` (live-engine
acceptance floor). All must stay green; new subsystems get equivalent test files.

---

## 2. Mission

Make every agent on this platform greet a returning customer as a *known* customer. Concretely:

1. **Close the memory loop** — customer memory is already written after every call; wire it into
   the *next* call (live prompt) and into extraction (better analysis), retrieved in ~1ms.
2. **Upgrade memory from lists to a lifecycle** — confidence, dedup-by-meaning, supersession,
   expiry, conflict resolution.
3. **Give every contact a unified timeline** — every dial, call, outcome, follow-up, memory
   change, note — with zero manual linking.
4. **Classify every interaction outcome**, including calls that never connected.
5. **Turn `/contacts` into a Customer Intelligence Hub** — a per-contact workspace on par with
   the existing per-call workspace.
6. **Contact ownership & delegation** that respects the flattened-roles doctrine.

---## 3. Objective A — Ultra-low-latency pre-call context (the core wire)

### 3.1 Architecture decision (already made — implement this, not a cache farm)

The original brief suggests Redis. **Rejected for now.** This is a single-VM, host-network
deployment with no Redis container, no connection pooling, and a greeting path that is already
fully off-loop. The superior design at this scale is **write-time precomputation + one indexed
read**:

- **Layer 1 (authoritative):** Postgres — unchanged.
- **Layer 2 (semantic):** `call_embeddings` + hybrid search — unchanged, used for on-demand deep
  recall (§3.4), not the hot path.
- **Layer 3 ("hot cache") = precomputed digest.** Add a new pipeline stage (`digest`, after
  `memory`) that renders and stores the ready-to-inject prompt digest **at write time** — a
  `context_digest TEXT` + `digest_updated_at` on `customer_memory` (migration 0006). The pre-call
  read is then a single PK-indexed row fetch (~1ms) with zero LLM/embedding calls and zero
  assembly cost. Layer on a small in-process TTL cache (dict, ~5 min, keyed
  `(owner, sig_digits)`) in the runtime for rapid re-calls. Redis becomes justified only when the
  runtime spans multiple VMs — note this in the plan, don't build it.
- **Layer 4 (runtime session):** the existing `CallState`/`ContextMemory`/state-manager — extend,
  don't replace.

### 3.2 Fetch (bridge)

In `_resolve_call_routing` (already in `asyncio.to_thread`): resolve
`(owner, significant_digits(caller_number or dial.phone))` → `contacts` row →
`customer_memory.context_digest` + a thin structured header (name, calls_count, last_call_at,
last outcome, open followups count, open commitments count). Hard budget: **≤2 indexed queries,
≤50ms**; on any failure return `None` and proceed — **context must never delay or kill a call**.
Wire the result through `TelephonySession.__init__` via a new
`conversation.set_customer_context(...)` beside `set_call_context`.

### 3.3 Inject (prompt)

New `customer_context_block` band in `prompt_blocks.py`, rendered between the direction block and
`━━ CALL STATE ━━`:

```
━━ KNOWN CUSTOMER ━━
Ramesh Shah · 4 previous calls · last 2026-07-14 (outcome: callback_requested)
Interested in: 3BHK Skyline · Budget discussed: 85–90L · Prefers Gujarati, evenings
Open promise: agent to send brochure (2026-07-15) · Pending follow-up: today 18:00
Past objections: possession timeline
Use naturally and only when relevant. Never recite this list to the customer.
```

Rules: ≤600 chars (the existing digest budget); known-name greeting personalization for inbound
(reuse the `{contact_name}` mechanism outbound already has); a per-agent YAML flag
`memory.use_customer_context` (schema.py `follow_up.clarify_commitments` is the pattern to copy)
defaulting **on**, plus env kill-switch `INTEL_PRECALL_CONTEXT=true`.

### 3.4 Also close the extraction loop

Wire `pipeline.py:stage_extract` `memory_digest` (currently `""`) to `digest_for_contact()` —
the analysis LLM should know prior context too. One-line-ish fix; do it first.

---

## 4. Objective B — Memory lifecycle upgrade

Evolve `shared/intelligence/memory.py:merge_call` from capped text lists into a governed
lifecycle, keeping the JSON-doc-per-contact shape (right for this scale) and full provenance:

- **Fact model:** each entry gains `{text, call_id, date, confidence, status: active|superseded|expired, updated_at}`.
- **Dedup by meaning, not string:** normalized-text match first (exists); add cheap similarity
  (trigram via SQL or difflib) so "budget 85 lakh" updates "budget around 85L" instead of appending.
- **Conflict resolution:** newer statement about the same slot supersedes (status flip, not
  delete) — budget changes, preference reversals. Keep superseded entries for the timeline.
- **Expiry:** time-sensitive facts (urgency, "call me tomorrow") decay; stable facts (language
  preference, decision makers) don't. Encode per-category TTLs, applied at merge time.
- **Write the fields the DDL comment already promises:** `purchases`, `communication_style`, plus
  `preferred_language` (from `calls.language`), `preferred_call_time` (from callback patterns).
- `version` increments per merge (exists); memory stays a **concise profile, not a transcript dump**.

All pure-function logic → unit tests in a new `test_intel_memory.py` mirroring the existing
mocked-LLM test style.

---

## 5. Objective C — Contact timeline + outcome intelligence

### 5.1 `contact_events` (migration 0006, with `.down.sql`)

Append-only, written by the pipeline and the dial/CRUD paths — never manually linked:

```sql
CREATE TABLE contact_events (
  id BIGSERIAL PRIMARY KEY,
  contact_id BIGINT NOT NULL,
  owner TEXT NOT NULL,
  kind TEXT NOT NULL,            -- call.inbound|call.outbound|call.missed|dial.attempted|
                                 -- call.analyzed|followup.created|followup.completed|
                                 -- commitment.made|memory.updated|note.added|contact.updated|
                                 -- automation.fired
  ref_table TEXT, ref_id BIGINT, -- soft ref to calls/followups/commitments/...
  occurred_at TIMESTAMPTZ NOT NULL,
  payload TEXT,                  -- small JSON summary for rendering (no transcripts)
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_contact_events_contact ON contact_events(contact_id, occurred_at DESC);
CREATE INDEX idx_contact_events_owner ON contact_events(owner, occurred_at DESC);
```

Emitters: a new `timeline` pipeline stage (sweeper backfills history for free — see §1.3),
`_record_dial` in `server.py`, contact PATCH, follow-up transitions, memory merges. Idempotent
writes (dedupe on `(kind, ref_table, ref_id)` where sensible).

### 5.2 Outcomes for calls that never connected

Connected calls already get `calls.outcome`/`outcome_kind`. The gap is **unanswered/failed
outbound attempts** — today they leave only a `call_links` row. The workers billing reconcile
already parses the FreeSWITCH CDR CSV: extend that path to map hangup causes
(`USER_BUSY`, `NO_ANSWER`, `CALL_REJECTED`, `UNALLOCATED_NUMBER`, `NORMAL_TEMPORARY_FAILURE`…) to
a taxonomy (`busy|no_answer|rejected|invalid_number|network_failure|voicemail`), bind by
owner+sig_digits, and emit `dial.attempted` / `call.missed` events with timestamps and a retry
recommendation (busy → retry 30m; no_answer → next preferred window from memory; invalid → flag
contact). Keep billing logic untouched — this is a read-alongside, not a modification of
`usage_ledger` semantics.

---

## 6. Objective D — Contact Intelligence Hub (frontend + API)

### 6.1 API — under the already-allowlisted `/api/intel` prefix (`dashboard/intel_api.py`)

- `GET /api/intel/contacts/{contact_id}` — hub payload: contact + lifetime stats (calls, minutes
  via `usage_ledger`, outcome mix), memory + digest, scores (latest + trend from
  `call_intelligence` history), open commitments, upcoming follow-ups, recommended next actions
  (from latest `recommendations`), engagement metrics.
- `GET /api/intel/contacts/{contact_id}/timeline?before=&kinds=` — paginated `contact_events`,
  hydrated for rendering.
- `GET /api/intel/contacts/{contact_id}/calls` — call list with per-call intelligence row (reuse
  the `/api/intel/calls` row shape).
- All owner-scoped via `current_owner`; contact must belong to owner (404 otherwise).

### 6.2 Frontend — `/contacts/:id`

New `frontend/src/pages/ContactHub.tsx` following the `IntelligenceCall.tsx` pattern (route with
`useParams`, `useGet`, `AsyncState`, kit components):

- **Header:** name, phone, gender, group, language preference, primary agent; actions: Call
  (reuse `POST /api/outbound/dial/{id}` + the 60s duplicate-dial guard), Edit, Add note.
- **Left (2/3):** AI customer profile (digest, prose), score trend rings/sparklines
  (lead/opportunity/risk/engagement — `RadialRing` + `dataviz` conventions), unified timeline
  (infinite scroll, kind filters, each entry deep-linking to `/intelligence/:callKey` or
  `/followups`), calls table.
- **Right (1/3):** memory panel (preferences, open promises with deadlines, past objections,
  decision makers — superseded facts collapsed), upcoming follow-ups, recommended next actions,
  lifetime stats.
- Make `/contacts` rows clickable (`navigate`), add `call_count`/`last_called_at` columns (data
  already returned, just unrendered). Keep `/outbound` untouched.
- **Deploy note:** SPA is baked — this phase ends with an spa image rebuild, not just a restart.

---

## 7. Objective E — Ownership & sharing (design-constrained, LAST phase)

The brief's "administrators transfer ownership" assumption conflicts with this platform's
**flattened-roles doctrine** (no admin exists, deliberately — see `auth.py:is_admin`). Recreate
the feature within that doctrine:

- `contacts.primary_agent_slug` (boot MIGRATIONS ALTER — core table, not a versioned migration):
  which of the owner's agents "owns" the relationship; default = last agent that called. Shown in
  the hub; used as the outbound default.
- Cross-owner sharing = explicit grants: a `contact_grants(contact_id, owner, grantee_email,
  access: read|edit, created_at)` table, granted/revoked only by the owning tenant, enforced in
  the `/api/intel/contacts/*` handlers (deny-by-default, exactly like the middleware). Grantees
  get the same memory/context read path.
- Ownership history = `contact.updated` timeline events, not a separate mechanism.
- **Gate:** implement the schema + API behind it only if trivially clean; the UI for sharing is
  optional scope — flag it in the plan and confirm before building UI. Everything else in this
  prompt is fully in scope.

---

## 8. Event flow after this work

```
Call ends (bridge teardown)                    Outbound dial fails (CDR reconcile, workers)
  → intel_jobs: call.process                     → outcome classify → contact_events + retry hint
    → extract      (now WITH memory_digest)
    → embed        (unchanged)
    → followup     (unchanged)
    → memory       (lifecycle merge: confidence/supersede/expire)
    → digest       (NEW: render context_digest at write time)
    → timeline     (NEW: contact_events emit)
    → automation   (unchanged)
    → notify       (unchanged)

Next call arrives
  → _resolve_call_routing (off-loop): +1 indexed read → digest + header (~1ms, in-proc TTL cache)
  → set_customer_context → KNOWN CUSTOMER prompt band
  → agent greets a known customer.
```

The sweeper retrofits `digest` and `timeline` across all historical calls automatically.

---

## 9. Non-negotiable platform rules

1. **Migrations:** versioned SQL only (`0006_…`, paired `.down.sql`), applied via
   `docker exec … python -m shared.db.migrate`; never edit applied files; core-table tweaks go in
   `database.py` MIGRATIONS (idempotent ALTERs). No broken FKs, no orphans, rollback path for
   every change.
2. **Never recreate `lescall-pg`.** pgvector provisioning lives inside the container.
3. **No new JSON stores.** Postgres is the source of truth; the remaining JSON files are legacy
   fallbacks being retired, not a pattern to extend.
4. **Owner-scope everything**; new API surface stays under allowlisted prefixes or extends
   `_TENANT_API_PREFIXES` deliberately.
5. **The greeting path is sacred.** All context work stays inside the existing `to_thread`
   routing call with a strict failure-to-None budget. Never block, never raise into the call.
6. **Additive to the live engine.** `conversation.py` changes are new bands/setters behind flags;
   `test_kaapd_e2e.py` and all `test_intel_*.py` stay green; new logic gets new tests.
7. **Feature flags:** `INTEL_PRECALL_CONTEXT` env + per-agent YAML toggle, so canary = one agent.
8. **Sarvam key pool doctrine unchanged** (file-based, platform infra, never per-tenant/DB).

## 10. Implementation phases (each ends stable, deployed, verified)

1. **P0 — Quick wire:** `memory_digest` into `stage_extract` (§3.4) + tests. Restart workers.
2. **P1 — Migration 0006:** `context_digest` on `customer_memory`, `contact_events`,
   (`contact_grants` if §7 proceeds) + down-migrations. Apply, verify `--status`.
3. **P2 — Write side:** memory lifecycle (§4), `digest` + `timeline` stages, CDR outcome
   classification (§5.2). Restart workers; watch sweeper backfill via `GET /api/intel/status` and
   `intel: beat` logs; spot-check `contact_events` and digests in psql.
4. **P3 — Read side:** bridge fetch + `set_customer_context` + prompt band + flags (§3). Restart
   runtime. Canary on one agent; verify with a test call to a number that has history (the
   `/api/numbers/{id}/test-call` path), confirm the band in the turn logs and no greeting-latency
   regression (compare against RUNBOOK §11 latency notes).
5. **P4 — Hub API** (§6.1) + tests. Restart api.
6. **P5 — Hub UI** (§6.2). **Rebuild spa image**, smoke-test `/contacts/:id` on mobile + desktop,
   light + dark.
7. **P6 — Ownership/sharing** per §7 gate.

After every phase: restart only affected containers, check `docker ps` health, tail logs, hit
`/api/health` + `/api/intel/status`, run the test suite. Do not proceed on a red step.

## 11. Definition of done

A returning caller is greeted by name in their preferred language; the agent knows the open
promise from last week without being told; the follow-up it makes lands in the timeline; the
dashboard shows that customer's whole relationship on one page; the pre-call context cost is one
indexed read; every table change has a down-migration; `lescall-pg` was never touched; all tests
pass; and nothing about it required Redis, a rewrite, or a second of added greeting latency.
