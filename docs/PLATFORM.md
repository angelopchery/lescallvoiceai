# LesCall — Platform Reference

Maintained map of the platform: what runs, where each feature lives, what a
tenant can do, which knobs exist and where, how it is tested, and what is
measured. Read this before any broad inspection; it replaces re-deriving the
system from the tree each time.

- **Last full inspection:** 2026-10-18 (branch `Bettercallimprov`, HEAD
  `061464c` + 227 modified / 159 untracked files not yet committed).
- **Companion documents:** `CLAUDE.md` = operating doctrine and invariants
  (how to change things safely); `deploy/RUNBOOK.md` = dated incident /
  change log (§0–§83, cite by section); `PROJECT_INSPECTION.md` = the
  2026-09-18 product brief written for the launch video; `deploy/PLAN-*.md`
  = historical design plans (all implemented or superseded; new plans are
  not written as files).
- **Update rule:** when a container, port, table, page, tool or measured
  number in here changes, change it here in the same session. Keep facts,
  not narrative; the RUNBOOK carries the story.

---

## 1. What the product is

**LesCall** (Dhron AI; app `agent.lescall.com`) is a multi-tenant platform
for AI voice agents that **make and answer real phone calls** for Indian
businesses in English, Hindi and Gujarati. Three layers:

1. **Real-time voice pipeline** — FreeSWITCH → neural VAD → Sarvam STT →
   stage-driven conversation engine (Gemini) → Sarvam TTS → caller, with
   barge-in, pause tolerance and continuation merge.
2. **Agent platform** — an agent is one YAML file; a chat **Copilot** builds
   and edits it; **Try agent** runs it as a text call; self-service
   **bring-your-own-number** puts it on a line; campaigns and scheduling
   drive outbound; WhatsApp follow-ups go out after the call.
3. **Call intelligence** — every call is summarised, scored and mined for
   commitments; a **verified** natural-language filter; follow-up calls behind
   human approval; Excel/Word exports; a public `/v1` pull API.

**Live numbers (2026-10-18, read-only probe of the production DB on this VM):**

| | |
|---|---|
| calls / contacts / users | 1,070 / 1,035 / 20 |
| tenants that have taken calls | 10 |
| per-turn telemetry rows (`call_turns`) | 7,006 |
| agent folders on disk (`calling-agent/agents/`) | 101 (inbound/outbound twins count separately) |
| recording folders (`recordings/`) | 1,202 |
| Postgres tables / migrations | 65 / `0001`–`0031` |
| engine + API tests | 145 files, 2,319 `def test_` (`calling-agent/backend/tests/`) |

---

## 2. Process topology (this VM is production)

```
PSTN/SIP carrier (Plivo trunk; Kamailio edge on 10.160.0.8:5060/5080)
      │
 FreeSWITCH ── ESL 127.0.0.1:9000 ──► socket_server.py        call control, playback, uuid_break, hangup
      │  mod_audio_stream (L16 @ 8 kHz, 20 ms frames)
      └─ WS 127.0.0.1:9001 ──────────► ws_audio_receiver.py   resample 8k→16k, audit, live_calls_state.json
                                              │
                                   conversation_call_bridge.py  one TelephonySession per call; DID→agent
                                              │                 routing; preflight; metering; call_turns
                                              ▼
                  calling-agent/backend/app/voice/pipeline.py   6 asyncio tasks, bounded queues:
                  audio/VAD → stt_submitter → stt_worker → conversation → tts (+ silence watchdog)
                                              │
            TTS WAVs ── TCP 127.0.0.1:9004 ──► socket_server.py ──► FreeSWITCH ──► caller
```

| Container (`deploy/docker-compose.services.yml`) | Runs | Notes |
|---|---|---|
| `lescall-runtime` | `deploy/runtime_entry.sh` → `socket_server.py` + `ws_audio_receiver.py` | host network; bind-mounts the repo, so `docker restart lescall-runtime` loads new `.py`; env: `VAD_ENGINE=fsmn`, `LLM_TOOLS_MODE=on`, `ENABLE_CONTINUATION_MERGE=true`, `TELEPHONY_MODE=true`; sources `calling-agent/.env` |
| `lescall-api` | FastAPI `dashboard/server.py` on `0.0.0.0:8080` | dashboard API, builder, Copilot, intelligence, billing, telephony, WhatsApp, `/v1`; also runs in-process the follow-up autodialer, the scheduled-call loop and the WhatsApp sender |
| `lescall-workers` | `deploy/workers_entry.sh` | intel worker (concurrency 2), billing sync, 5-min tick: metering reconcile, outbox, recording sync (15 min), log shipping (60 min) |
| `lescall-admin` | operator plane (`dashboard/admin_app.py`) | loopback / UNIX socket only, `ADMIN_TOKEN`; not proxied by nginx |
| `lescall-dashboard` | nginx serving `frontend/dist` on 127.0.0.1:8081 | public nginx on :80/:443 fronts it and the API |
| `lescall-pg` | Postgres 16 + pgvector on 127.0.0.1:5432 | daily `pg_dump` by cron 03:20 → `/home/angelo/backups/` (14 kept, **same VM, no off-box copy**) |

Host services: `lescall-sentinel.service` (30 s watchdog, restarts with
cooldowns, emails `tech@dhronai.com`; never restarts the runtime mid-call),
`lescall-recsync.timer` (15 min; duplicates the workers' recording sync),
Kamailio resilience drop-in. Logs: `phase14_runtime/logs/docker/*.log`
(rotated nightly, `.gz` after two days). Live-call state file:
`phase14_runtime/live_calls_state.json` (written every 1.5 s).

**Never** run `phase14_runtime/start_all.sh` on this host (second pair on the
same ports); verify with `docker ps` and `ss -ltnp | grep -E ':900[014]'`.

---

## 3. Repository map

| Path | What | Size |
|---|---|---|
| `socket_server.py`, `ws_audio_receiver.py`, `conversation_call_bridge.py`, `call_audit.py` | telephony edge (system Python for `socket_server.py`, `calling-agent/venv` for the rest) | 2.7k / 1.1k / 2.2k / 0.4k lines |
| `calling-agent/backend/app/voice/` | the engine: `conversation.py` (11.2k), `pipeline.py` (5.0k), `prompt_blocks.py`, `state_manager.py`, `stt/`, `tts_sarvam.py`, `vad_stack.py`, `endpointer.py`, `address.py`, `machine_line.py`, `pitch_switch.py`, `context_cache.py`, `spoken_numbers.py`, `ai_disclosure.py` | 31k |
| `calling-agent/backend/app/agent_definition/` | `schema.py` (agent dataclasses), `store.py` (the one write path: optimistic concurrency, revisions, scope proof), `registry.py` (mtime reload), `controls.py` (what is a real setting), `catalog.py`, `spoken_surfaces.py`, `boot_check.py`, `preflight.py`, `calendar_store.py`, `routing.py` | 4k |
| `calling-agent/backend/app/documents/` | document RAG: extractor, embeddings (Gemini native), retrieval (BM25 + vector, RRF), rerank, cache, trace | 3.5k |
| `calling-agent/backend/app/web/` | the `run.py --web` local console (EventHub → `/ws`) — CLI/mic mode only | 0.8k |
| `calling-agent/agents/<slug>/agent.yaml` | **gitignored** tenant agents (+ `documents/` indexes) | 101 folders |
| `calling-agent/backend/tests/` + `tests/fixtures/` | engine, API, intelligence, WhatsApp, security tests; frozen agent snapshots | 145 files |
| `calling-agent/backend/rag_eval/` | RAG golden set, runner, sweep, replay; CI gate `.github/workflows/rag-eval.yml` | |
| `dashboard/` | FastAPI tenant app: `server.py` (6.0k), `agent_copilot_api.py` (8.1k), `intel_api.py` (2.7k), `agent_builder_api.py`, `agent_share_api.py`, `telephony_api.py`, `billing_api.py`, `whatsapp_api.py`, `public_api.py` (+`_spec`), `api_keys_api.py`, `auth.py`, `copilot_voice.py`, `copilot_interview.py`; admin plane `admin_app.py`, `admin_ui.py`, `admin_ops.py`, `admin_telephony.py`, `admin_api_console.py` | 28k |
| `shared/` | cross-process libraries: `db/` (one write choke point + migrations), `intelligence/` (pipeline, extract, search, verify, followup, autodial, exports, reports), `billing/`, `whatsapp/`, `public_api/`, `sentinel.py`, `voice_health.py`, `usage_metering.py`, `scheduling.py`, `recording_storage.py`, `freeswitch_config.py`, `telephony_admin.py`, `contacts.py`, `phone.py` | 21.6k |
| `frontend/` | React 18 + TS + Vite + Tailwind v3 + shadcn/ui SPA; `src/pages/*`, `src/components/*`, `src/api/*`, `src/lib/*`; one e2e (`e2e/intel_browse.e2e.mjs`) | |
| `deploy/` | compose files, Dockerfile, nginx/FreeSWITCH/Kamailio config, systemd units, `RUNBOOK.md`, `PLAN-*.md`, per-tenant build scripts (`build_*.py`, `agents_dry*`), `lint_fleet_surfaces.py`, `pg_backup.sh`, `provision_vm_sa.sh` | |
| `data/` | vendored FSMN VAD model, TTS cache, voice samples, sentinel state, LLM context-cache registry, WhatsApp media | |
| `docs/` | this file, `MULTITENANT_BYON_PLAN.md` (2026-07 design), `Pramukh Project Details.md` (a tenant fact sheet), `reports/` | |
| Outside the repo | `/home/angelo/calltester` (text replay tester), `/home/angelo/callsim` (audio-level sandbox simulator), `/home/angelo/lescall-video` (launch video workspace) | |

---

## 4. The voice call path, per turn

```
8 kHz L16 frame ─► resample 16 kHz ─► WebRTC pre-gate ─► FSMN VAD (100 ms hops)
  ─► EndpointController (fast / default / hold residual) ─► segment gates (MIN_REAL_SPEECH_MS, SNR)
  ─► STT: Sarvam saarika:v2.5 (2nd Sarvam at 0.6 s, Whisper hedge at 1.0 s; clean-empty beats any hedge)
  ─► validate_transcript ─► merge window / pause tolerance / continuation merge
  ─► ConversationRouter.process_stream: machine-line gate → deterministic branches
     (shortcuts, fast paths, identity, AI disclosure, FAQ playbook) → LLM
     (gemini-3.5-flash-lite, reasoning minimal, native tools gated in code, agent-scoped context cache)
  ─► clause-chunked Sarvam bulbul:v3 TTS (early flush, canned cache with duration gate)
  ─► TelephonySink → :9004 → FreeSWITCH; barge-in = uuid_break at the switch
```

- **Audio in** is L16 PCM, not PCMU (a PCMU branch exists, unused).
- **VAD** is the FSMN stack (`vad_stack.py`, `PLAN-vad-stack.md`), energy VAD
  only as fallback; `[VAD-STACK] DEGRADED` announces the fallback.
- **STT** language is pinned once Sarvam reports ≥0.75 probability and
  re-checked every 8 turns; every automatic language switch is bounded by
  `languages.supported`.
- **LLM**: one explicit `cachedContents` per agent × model × band hash
  (`context_cache.py`, RUNBOOK §62); PER-CALL and PER-TURN bands are never
  cached. Fallback model chain on failure streaks.
- **Per-turn telemetry**: logged `[LATENCY] speech_end→engine` /
  `speech_end→first_audio`, `[ENDPOINT] flush profile=…`, `[VAD-STACK]`,
  `[BARGE-IN]`, `[ECHO-GATE]`, `[CONT-MERGE]`, `[STT]`, `[ENGINE]`, `[LLM] TTFT`;
  **persisted** to `call_turns` (stage, intent, language, stt_latency_ms,
  stt_confidence, speech_end_to_engine_ms, llm_first_chunk_ms, llm_total_ms,
  speech_end_to_first_audio_ms, is_filler, barged_in, rag_sources).
  `session_state` and `endpoint_profile` are traced but not stored; VAD
  energy, SNR, discard reasons and echo-gate events exist only in the log.
- **Four artifacts per call**: `recordings/<conn_id>/` (caller WAV,
  transcript with `[spoke a-b s]` spans, metadata), two-party recording via
  FreeSWITCH (GCS-synced, tokenised download), `calls` row, `call_turns` rows.

---

## 5. The agent model

An agent is `calling-agent/agents/<slug>/agent.yaml`, loaded by
`AgentDefinition.from_dict` (`schema.py`). Sections: `identity`,
`personality` (energy/tone/style/traits/custom_instructions/
`verbosity_max_sentences`/`style_variants`), `brand`, `languages`
(`supported`, `default`, `tts_voices[lang]{model,speaker,pace}`, switch
policies, `tts_numbers_english`), `call_flow` (`stages[]` with `goal`,
`collect`, `advance_when`, `branches`, `script`; `termination`, `connect`,
`turn_taking`, `scheduling`, `callback_policy`, `completed_when`), `slots[]`
(typed, `ask_gated`, `merge_policy`, `max_asks`, `depends_on`),
`knowledge_base` (catalog, match rules, `primary_project`, pricing/fact
policy, STT corrections, `qa_field_signals`), `response_bank` (opening,
objection playbook, canned phrases, closings, variants), `calendar` (mock
slots only), `llm_config` (model, max_tokens, timeouts, `filler_timeout_s`,
`max_response_length`, `record_details_on_main_call`), `outbound`,
`guardrails`, `follow_up`, `post_call`, `memory`.

- **Twins**: `<name>-inbound` / `<name>-outbound`; direction is the slug
  (`store.assert_direction_matches_slug`).
- **One write path**: `store.write_agent` — optimistic `base_rev`,
  Postgres revision snapshots (last 50, Restore in the UI), atomic replace,
  post-write proof that nothing outside `expected_paths` changed.
- **Real settings only**: `controls.py` is the allowlist for the untyped
  dict subtrees; an unknown key is dropped by `from_dict` and would otherwise
  bump a revision while changing nothing.
- **Engine validation at write time**: `boot_check.engine_boot_check` renders
  every stage × language × direction prompt; `preflight.preflight_agent` does
  it again at call start and swaps in the neutral fallback agent on failure.
- **Copilot** (`agent_copilot_api.py`): 13 server-computed interview layers
  (`copilot_interview.py`: business, objective, persona, languages & voice,
  brand, call flow, data capture, knowledge, objections, scheduling,
  guardrails, greeting & close, outcomes); 19 tools (`create_agent`,
  `update_agent`, `propose_update`/`apply_update`/`discard_update`,
  `add_stage`/`update_stage`/`remove_stage`, `enrich_agent`,
  `ingest_document`, `preview_opening`, `validate_agent`, `list_controls`,
  `find_text`, `replace_mentions`, `get_agent`, `list_agents`, `skip_layer`,
  `update_draft`); proposed-change diff cards; voice dictation; quick
  controls for speaker + pace (RUNBOOK §67); per-tenant token budget.
- **Builder UI** (`/build/agents/:name`): Settings (basics, call type,
  languages & voice, personality, call script, knowledge, calendar,
  objections & rules), Stages, Advanced YAML (validate + save), Routing,
  Documents · RAG, Voice (read-only list + preview), History (restore).
  Pace, fillers, response caps, turn-taking, connect, termination and LLM
  timeouts are **YAML-only** in the Builder (see §12).

---

## 6. Tenant product surfaces (SPA routes → what they do)

| Route | Page | Does |
|---|---|---|
| `/` | Overview | 4 stat tiles, 14-day sparkline, hot leads, 4-step "Get set up" checklist |
| `/copilot` | Copilot | chat build/edit, sessions, file attach, dictation, change cards (Apply/Discard), interview rail + spec preview, Try/Activate chips, Voice & speed sheet |
| `/build/agents` | Agents | cards grouping twins; Activate/Change number, Chat with agent (Try), Edit, Archive, Add other direction, Share link / import |
| `/build/agents/new` | Create agent | 9-step wizard, "start from a document" |
| `/build/agents/:name` | Edit agent | tabs listed in §5 |
| `/live` | Live | SSE `/api/calls/live-state/stream`: speaking badge, language, fixed 6-segment stage strip, latest line, full transcript; **no actions, no latency** |
| `/activity` | Activity | all calls with filters, expand for times + estimated STT/TTS cost |
| `/recordings` | Recordings | inline player, download, copy public link |
| `/transcripts` | Transcripts | search inside conversations, per-turn timestamps |
| `/intelligence` | Intelligence | KPI tiles, AI filter (verified judge), filters, selection → Export (xlsx to GCS) / Report |
| `/intelligence/:callKey` | Call workspace | summary, 4 scores, transcript find/copy/download, signals, agent performance, key moments, follow-up card, commitments, action items, recommendations, customer memory, Reanalyze, Prev/Next; **no per-turn metrics, player not synced to transcript** |
| `/followups` | Follow-ups | buckets, Call now (approve + dial), edit, complete, cancel, new manual, Review queue (approve/decline) |
| `/reports` | Reports | generate (filters + focus prompt), read, export docx/md/txt/xlsx |
| `/conv-analytics` | Conversation analytics | funnel, agreements, sentiment volume, lead score, heatmap, voice of customer, agent table, channel/language mix |
| `/outbound` | Outbound | groups, contact upload (CSV/XLSX), campaign runner (preview/start/schedule/stop), per-row dial/edit/disposition, bulk actions |
| `/contacts`, `/contacts/:id` | Contacts / Contact hub | phone book + groups; hub: timeline, calls, notes, dial, share-with-teammate grants, memory, promises |
| `/integrations/whatsapp` | WhatsApp | Setup (Meta or Interakt), Templates, Playbooks (per-project brochure), Approvals, Activity |
| `/telephony` | Telephony (BYON) | origination URI + ACL, connect number (trunk → number → apply), per-number inbound/outbound binding, **Test call** (rings the owner's profile phone; result = a toast), disconnect, readiness checklist |
| `/settings` | Settings | name, phone (test-call target), company; signposts |
| `/developers`, `/developers/reference` | Developers | API keys (scopes, expiry, IP allowlist), limits, 14-day request chart, printable reference |
| `/billing`, `/billing/receipt/:id` | Billing | meters, plans (Razorpay checkout — unconfigured), invoices, attempts, billing profile; **no minute top-up** |

Global: topbar health pill, minutes pill, notification bell (in-app only),
help popover (3 static lines), dark/light theme, quick-jump (page names
only), mobile drawer. **No** team/roles UI, no UI i18n, no onboarding tour,
no in-app documentation.

---

## 7. API surface

| Module | Routes | Prefix |
|---|---|---|
| `server.py` | 72 | `/api/*` (calls, recordings, overview, outbound, campaigns, scheduling, contacts, legacy follow-ups, ops routes gated off for tenants) |
| `agent_builder_api.py` | 28 | `/api/agents…`, `/api/tts/preview`, `/api/agent-tools/extract-document` |
| `agent_copilot_api.py` | 12 | `/api/copilot/*` incl. `try/{name}/start`, `try/{name}`, `sessions/{sid}/quick-controls` |
| `intel_api.py` | 47 | `/api/intel/*` (calls, search, followups, reports, exports, notifications, contacts, automations, status, analytics) |
| `whatsapp_api.py` | 36 | `/api/integrations/whatsapp/*`, open `/api/whatsapp/webhook*`, `/r/{token}`, `/r/f/{token}` |
| `telephony_api.py` | 11 | `/api/trunks`, `/api/numbers`, `/api/telephony/server-info` |
| `billing_api.py` | 15 | `/api/billing/*` |
| `agent_share_api.py` | 5 | `/api/share*` |
| `api_keys_api.py` | 4 | `/api/apikeys` |
| `auth.py` | 7 | `/auth/*`, `/login` |
| `public_api.py` | 18 | **`/v1`** — pull-only integration API: me, agents, contacts (+bulk, groups), calls, intelligence, transcript, recording, `openapi.json`; own key → account IP allowlist → status → per-key allowlist → per-account + per-key rate limits in Postgres; one error envelope |
| admin plane (5 modules) | 72 | `/admin/*` on loopback: tenants, plans, enforcement toggle, credits, numbers/trunk handover, invoices, ops, API console |

Tenancy gate: every `/api/*` path must match `auth._TENANT_API_PREFIXES`
(deny by default); `_OPEN_PREFIXES` / `_OPEN_EXACT` list the unauthenticated
routes (OAuth, static, tokenised downloads, `/v1`, `/r/`, webhooks).

---

## 8. Post-call intelligence and follow-through

- **Pipeline** (`shared/intelligence/pipeline.py`, resumable per stage):
  `extract → embed → followup → memory → digest → timeline → automation →
  notify → whatsapp` (WhatsApp only for calls < 1 h old). Worker claims
  `intel_jobs`; a 60 s sweeper enqueues unprocessed calls and classifies
  failed dials.
- **Extraction** with code-side guards: every commitment quote must occur in
  the transcript or is dropped.
- **Search**: pgvector HNSW + FTS + trigram, RRF-fused, lexical fallback.
- **Verified filter** (`verify.py`, RUNBOOK §31–§32, §53): planner →
  candidate retrieval (`INTEL_SEARCH_ARM_LIMIT=150`) → one judge call per
  candidate → three gates, the last being *the quoted evidence occurs in the
  transcript*. Reports `verified`, `truncated`, `pool_full` honestly.
- **Follow-up calls**: human approval behind three independent gates; DND
  overrides a click; autodialer loop in `lescall-api`.
- **Scheduled calls / campaigns**: one guarded dial funnel (duplicates, DND,
  hours, voice health, billing); a scheduled call older than 30 min is
  MISSED, never fired late.
- **Exports**: ticked rows → multi-sheet xlsx to GCS (local fallback,
  tokenised download). **Reports**: docx/md/txt/xlsx.
- **WhatsApp** (`shared/whatsapp/`, RUNBOOK §64, §80): consent table, playbook
  → template (+ per-project brochure), approval or auto, send window, daily
  cap; providers Meta Cloud API and Interakt; `WA_SEND_ENABLED=true` since
  2026-10-13.
- **Notifications to tenants**: in-app bell only. Automation actions:
  `notify_inapp`, `escalate`, `webhook` (HTTPS, SSRF-guarded, unsigned),
  `create_followup`, `add_tag` work; `email` needs sendmail that the workers
  image lacks; `send_sms`, `send_whatsapp`, `slack`, `teams`, `crm_update`
  return `not_configured`.
- **Calendar**: `mock` slots + `bookings` table only; `webhook` type is a
  schema placeholder. **CRM outbox**: worker exists, nothing enqueues.

---

## 9. Data model (Postgres, `shared/db/schema.sql` + `migrations/`)

| Domain | Tables |
|---|---|
| core | users, agents, agent_revisions, trunks, phone_numbers, campaigns, calls, call_turns, call_links, bookings, settings, audit_events, shipped_logs, scheduled_calls |
| contacts | contact_groups, contacts, contact_events, contact_grants, follow_ups (legacy), outbox |
| intelligence | intel_jobs, call_intelligence, call_embeddings, commitments, action_items, followups, notifications, customer_memory, reports, automation_rules(+versions, runs), intel_filter_verdicts, intel_filter_plans, call_exports |
| billing / metering | usage_ledger, usage_monthly, plans, subscriptions, entitlements, pilot_claims, billing_reservations, payments, receipt_counters, minute_topups, billing_events, payment_attempts, copilot_token_usage |
| sharing / API | agent_shares, agent_share_imports, api_keys, api_access_policy, api_rate_windows, api_idempotency |
| whatsapp | wa_accounts, wa_templates, wa_media, wa_playbooks, wa_consents, wa_sends, wa_links, wa_inbound, wa_webhook_events |

`calls` carries direction, agent slug/revision/sha/health, language
(dominant), transcript, summary, analysis, recording path/GCS object/token,
collected_data, contact_id, llm_models_used. `usage_ledger` is the
CDR-refereed minute meter (pipeline vs CDR billsec, reconciled by the
workers). All writes go through `shared/db/database.py` (qmark translator,
NUL stripping); `tests/test_sql_safety.py` is a fail-closed gate.

---

## 10. Auth, tenancy, billing

- **Login**: Google OAuth only; stateless HMAC cookie (7 days, no server-side
  revocation). `LESCALL_ALLOWED_EMAILS=*` in the live env → open signup.
- **Roles**: flattened (`is_admin()` is always False); one email = one tenant;
  the only cross-user features are agent share links (import a copy) and
  per-contact grants. "Staff / enterprise" is an entitlement override, not a
  role. The operator plane is a separate app.
- **Plans** (`0013`): free ₹0 (30 min, 1 agent), pilot ₹2,999 one-time 7 days,
  basic ₹14,999/mo (3,000 min, 10 agents), growth ₹49,999/mo (8,000 min, 25
  agents, API access), enterprise unlimited. **Enforcement is OFF since
  2026-08-01** (admin toggle); only the trial floor (free: 1 agent family,
  20 Copilot messages/day, 150k Copilot tokens/day, 25k per message) and the
  telephony gate bind regardless. Razorpay client is safe-when-unconfigured
  and no account exists; offline invoices and admin credit grants are the
  working payment paths.

---

## 11. Background jobs and self-healing

| Job | Where | Cadence |
|---|---|---|
| intel worker + sweeper | workers | continuous / 60 s |
| metering reconcile (CDR join, crashed-call close, monthly rollup) | workers | 5 min |
| outbox delivery (no producer today) | workers | 5 min |
| recording → GCS sync + local prune after 3 days | workers **and** host timer | 15 min each |
| log shipping to `gs://$GCS_LOGS_BUCKET` | workers | 60 min |
| billing sync / dunning | workers | own process |
| follow-up autodialer, scheduled-call loop (30 s), WhatsApp sender | `lescall-api` | in-process |
| sentinel: containers, ports, FS, SIP OPTIONS, nginx, DB, live-state freshness, inbound flow, open ledger rows, disk, memory, GCS **write** probe, voice provider health (passive log tail + active Sarvam probes, optional alert call) | host | 30 s |
| `pg_dump` | host cron | daily 03:20, 14 kept locally |

---

## 12. Where each call-quality knob lives (the "who can change it" map)

| Quality | Knob(s) | Lives in | Changeable by | Takes effect |
|---|---|---|---|---|
| Speaking speed | `languages.tts_voices[lang].pace` (0.5–2.0, step 0.05; `_effective_pace` applies hi ×1.08 and long-sentence ×0.93 unless `TTS_ADAPTIVE_PACE=false`) | agent YAML | Copilot "Voice & speed" sheet, Copilot chat, YAML tab | next call |
| Voice | `languages.tts_voices[lang].speaker` (drives agent gender) | agent YAML | same + Builder text field | next call |
| Tone / energy / style / verbosity | `personality.tone`, `energy`, `style`, `verbosity_max_sentences`, `traits`, `custom_instructions`, `style_variants`, `languages.register_notes` | agent YAML | Builder (tone/energy/style/lingo/custom), Copilot; the rest YAML-only | next call |
| Reply length | `llm_config.max_response_length` (400), `max_tokens`, `PLATFORM_REPLY_BREVITY` (~40 words) | YAML / env | YAML tab; env = restart | next call / restart |
| Fillers | `llm_config.filler_timeout_s` (floor env `FILLER_TIMEOUT_FLOOR_S`=1.5), `filler_min_gap_turns`, `filler_max_per_call`, `record_details_on_main_call` | agent YAML | YAML tab only (Builder hard-codes 1.5 at create) | next call |
| LLM | `LLM_MODEL` (env overrides every agent), `LLM_REASONING_EFFORT`, `llm_config.timeout_s` / `stream_timeout_s`, temperature 0.7 (hardcoded) | env / YAML / code | developer | restart / next call |
| Turn-taking per agent | `call_flow.turn_taking.{pause_hold_ms, max_merged_fragments, backchannel_guard, sustained_barge_ms, tail_barge_in}`; `call_flow.connect.{hello_fastpath, delivery_guard, intro_line}`; `termination.{silence_hangup, max_call_minutes, …}` | agent YAML | Copilot (knows the keys via `controls.py`), YAML tab; **no Builder control** | next call |
| VAD + endpointing | `FSMN_MAX_END_SIL_MS` 280, `VAD_PREGATE_IDLE_MS`, `EP_FAST/DEFAULT/HOLD_RESIDUAL_FRAMES` 2/7/26, `MIN_REAL_SPEECH_MS` 200, `MIN_SPEECH_SNR` 1.2 + `SNR_*`, `TURN_SILENCE_THRESHOLD_MS`, energy floors 260/500 (hardcoded), FSMN model thresholds (vendored `config.yaml`) | **env, module-level constants** | developer only | `docker restart lescall-runtime` |
| Pause tolerance / merging | `TURN_CONTINUATION_HOLD_MS` 900, `TURN_NARRATIVE_SPEECH_MS` 2500, `TURN_MAX_MERGED_FRAGMENTS` 4, `TRANSCRIPT_MERGE_WINDOW_MS` 550, `TRANSCRIPT_MERGE_SKIP_WHEN_IDLE`, `MERGE_*`, `STALE_FRAGMENT_MAX_MS` 3000, `CONTINUATION_*`, `TURN_RETRANSCRIBE*` | env | developer only | restart |
| Barge-in / echo | `ENABLE_BARGE_IN`, `BARGE_IN_MIN_FRAMES` 5 / `_MIN_ELAPSED_MS` 250 (constants), `TTS_ECHO_COOLDOWN_MS` 400, `ECHO_*`, `SHADOW_*`, `BARGE_GUARD_ALL_REPLIES`, `BACKCHANNEL_MIN_REMAINING_S`, `TAIL_CUT_MIN_REMAINING_S` | env / constants | developer only | restart |
| Connect window, silence, dead media, machine lines | `CONNECT_*`, `SILENCE_REPROMPT_S` 6 / `_MAX` 2, `SILENCE_HANGUP_GRACE_S`, `DEAD_MEDIA_S` 5, `MACHINE_*` | env | developer only | restart |
| STT latency | `STT_PRIMARY_RETRY_HEDGE_S` 0.6, `STT_HEDGE_DELAY_S` 1.0, `STT_PREFETCH_AHEAD`, `SARVAM_CONNECT/READ_TIMEOUT_S`, `STT_MIN_CONFIDENCE` | env | developer only | restart |
| TTS latency | `TTS_EARLY_FLUSH*` 0.9–1.6 s, `TTS_MAX_FLUSH_S` 4, chunk sizes 90/180 chars (constants), canned cache + `TTS_DURATION_MAX_RATIO` 1.2 | env / constants | developer only | restart |

Roughly 135 env-level voice knobs are read once at import; none is
per-tenant. The production overrides in compose are `VAD_ENGINE=fsmn`,
`ENABLE_CONTINUATION_MERGE=true`, `HARD_FLUSH_SILENCE_FRAMES=20` (inert on
the FSMN path), `LLM_TOOLS_MODE=on`; `.env` adds `LLM_MODEL`,
`LLM_REASONING_EFFORT=minimal`, `ENABLE_CALL_AUDIT=true`.
`/etc/lescall/telephony.env` may add more (not readable here).

---

## 13. Testing and simulation tools

| Tool | Where | Exercises | Measures | Spends |
|---|---|---|---|---|
| pytest suites | `calling-agent/backend/tests/` (2,319 tests; never load a live agent YAML — fixtures only; never two pytest processes at once) | engine logic, API, intelligence, WhatsApp, security, SQL safety | pass/fail | nothing (LLM stubbed) |
| RAG eval | `rag_eval/` + CI `rag-eval.yml` | document retrieval | Recall@1, abstention, citation accuracy | nothing offline |
| **Try agent** (UI "Chat with agent", `POST /api/copilot/try/{slug}/start` + `/try/{slug}`) | dashboard | the real `ConversationRouter` as a text call: opening first, stage/slots/flags/would-book per turn, fillers shown, agent hang-up honoured; bookings captured not written | the live engine readout; no scoring, no scripts, one turn at a time | LLM quota (shared with live calls); **runs the dashboard's env, so `LLM_TOOLS_MODE` may differ from the runtime** |
| **Number Test call** (`POST /api/numbers/{id}/test-call`) | Telephony page | a real call from the number to the owner's profile phone with the bound outbound (else inbound) agent | nothing beyond a toast; the call then appears in Activity/Intelligence like any other | voice credits + minutes |
| **calltester** | `/home/angelo/calltester` (outside repo) | real router, text only; replays a past call's caller lines (with timestamps) or a hand-written script; `expect` assertions (stage, slots, contains, bands, hangup); `--judge` LLM review per turn; `compare` runs | FAIL/WARN/DIFF/INFO verdicts, side-by-side transcript, prompts per turn | LLM quota; refuses to run while calls are live unless `--force` |
| **callsim** | `/home/angelo/callsim` (outside repo) | the **unmodified production** `ws_audio_receiver` + bridge on sandbox ports 19001/19004 behind a fake FreeSWITCH; caller audio from recordings or TTS-synthesised scenarios (barge-in, silence, pre-pause, echo mix) | wire `response_ms`, `speech_end→first_audio`, VAD end lag, endpoint lag, STT similarity, barge→`uuid_break` ms, anomalies (double replies, Whisper fallback, lost turns); stereo WAV of the call | Sarvam + LLM like a real call |
| `deploy/lint_fleet_surfaces.py` | repo | every agent's actual opening via the engine | shadowed/dormant greeting fields | nothing |
| `boot_check` / `preflight` | engine | every prompt surface renders | refuse write / fallback agent | nothing |
| local console `run.py --web` | CLI mode | mic → pipeline; EventHub `/ws` with per-turn latency events | user/agent latency, merges | credits |
| e2e | `frontend/e2e/intel_browse.e2e.mjs` | Intelligence browse cost regression | `/intel/search` hit count | nothing |

---

## 14. Measured numbers (keep dated)

| What | Value | Source |
|---|---|---|
| speech_end → first agent audio, live calls 2026-10-13..18 (104 turns, incl. scripted test agents with `pause_hold_ms`) | p50 **3.4 s**, p90 4.5 s, max 7.5 s | `ws_audio_receiver.log*` `[LATENCY]` |
| speech_end → engine (VAD close + STT), same window | p50 1.5 s, p90 2.0 s | same |
| LLM TTFT p50 (172 turns, 2026-09-11) | 0.98 s on gemini-3.5-flash-lite | RUNBOOK §43.2 |
| same metric 2026-09-15/16 before the tool-only-turn fix (518 turns) | p50 3.0 s, p90 4.5 s; 77% of turns answered with a tool call and no words | RUNBOOK §51.1 |
| platform cost | ₹4.29 per call-minute over 3,375 calls; ₹14.55 / call, ₹2.99 / min on Viraj call 972 (TTS 44%) | RUNBOOK §27.1, memory 2026-09-26 |
| verified filter | 0 false positives across six filters; recall 51% → ~100% after `ARM_LIMIT` 40 → 150 | RUNBOOK §32 |
| RAG (37 golden questions) | Recall@1 0.66 → 0.79, abstention 0.56 → 1.00, citation accuracy 1.00 | RUNBOOK §28 |
| TTS duration gate | worst legitimate ratio 0.95 over 88 syntheses; hallucinations 1.27 / 1.44; gate 1.2 | RUNBOOK §45.1 |
| voice speed is the VOICE | same line, pace 1.0: neha 10.6 s, simran 7.0 s, shubh 5.9 s | RUNBOOK §51.1 |
| sentinel repair of a killed container | 18 s | RUNBOOK §9 |

---

## 15. Known gaps and non-features (verified 2026-10-18)

- No user-facing control for VAD, endpointing, pause tolerance, barge-in,
  echo, silence re-prompt, STT hedging or TTS flushing — all env, all
  process-global, restart to change (§12).
- No call-quality diagnostics in the product: latency per turn is stored in
  `call_turns` but shown nowhere; Live shows no latency; the call page has no
  per-turn data and no audio↔transcript sync; VAD/echo/merge events live only
  in logs.
- Test call = a toast; Try agent = free text, no scripts, no scoring, no
  batch, env may differ from the runtime; calltester and callsim are
  developer-only tools outside the repo.
- Calendar is mock-only; no external calendar, CRM push (outbox has no
  producer), SMS, email (workers image has no sendmail), Slack/Teams; `/v1`
  is pull-only with no outbound webhooks.
- No teams/roles/seats, no SSO beyond Google, no session revocation, no UI
  i18n, no onboarding tour, no in-app docs, no minute top-up, Razorpay
  unconfigured, billing enforcement off, open signup.
- Backups: daily `pg_dump` on the same VM only; agent YAMLs are gitignored
  (revision snapshots in Postgres are the second copy).
- Single VM, single Sarvam/Gemini dependency (key rotation exists; no second
  STT/TTS vendor on the live path); no measured concurrency ceiling.
- `lescall-api` listens on `0.0.0.0:8080` (login gate applies; firewall not
  verified from the host); recording download tokens never expire; trunk
  passwords stored in plain text (masked by the API).
- Dead or stale in tree: `tts_sarvam_ws.py`, `tts_cartesia.py`,
  `stt_stream.py` (no importers), the fixed six-name stage strip on Live,
  legacy `/api/followups*` and ops routes mounted but gated off, duplicated
  recording sync (workers + host timer), `HARD_FLUSH_SILENCE_FRAMES` in
  compose (inert on FSMN), 259 MB `core` dump at the repo root (gitignored).
- Git: no commit since `061464c` (2026-09-09); ~37k inserted lines across
  227 modified + 159 untracked files (RUNBOOK §39–§83) exist only in the
  working tree and the nightly dump.

---

## 16. Document map

| Document | What it is | Status |
|---|---|---|
| `CLAUDE.md` | operating doctrine: how to run, invariants, every hard-won rule by RUNBOOK section | maintained; stale lines fixed 2026-10-18 |
| `deploy/RUNBOOK.md` | dated change / incident log §0–§83 with measurements and rollbacks | maintained, append-only |
| `docs/PLATFORM.md` | this map | maintained |
| `PROJECT_INSPECTION.md` | 2026-09-18 product brief for the launch video | historical |
| `dhron-voice-interface-report.md` | 2026-08-13 discovery of the voice stack for a WebRTC consumer | historical, still accurate on ingress |
| `docs/MULTITENANT_BYON_PLAN.md` | 2026-07-11 multi-tenant + BYON + Kamailio design | implemented |
| `deploy/PLAN-*.md` | design plans (VAD stack, call fidelity, intelligence, UX redesign, WhatsApp, billing…) | implemented / superseded |
| `calling-agent/backend/docs/conversation_state_management.md` | the slot/state layer | mechanism current, LLM names historical |
| `deploy/CRM-pramukh-integration-email.md`, `deploy/pramukh_prakhar_brief.md`, `docs/Pramukh Project Details.md`, `docs/reports/` | tenant-specific briefs and investigations | reference |
| `/home/angelo/calltester/README.md`, `/home/angelo/callsim/README.md` | the two external testers | maintained with the tools |
