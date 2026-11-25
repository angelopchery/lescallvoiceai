# PLAN — Copilot-built agents must drive the live call (call fidelity)

**Date: 2026-07-15 · Branch: pramukhagentcopy · Status: AWAITING APPROVAL**

Produced from a 22-agent audit (7 dimensions, each adversarially refuted and
golden-safety-checked, plus a completeness critic). Every file:line below was
verified against the actual code, the live logs, and the Kaapd call recording.

## Goal

An agent built through the Copilot must be **the** agent serving the call, and
every piece of user-provided input (flow, scripts, objections, products,
policies) must demonstrably drive the live conversation. The LLM's own
authorship fills gaps only. No agent YAML is hand-edited — every fix is
platform-level (engine, Copilot, extractor), gated so that **both** Pramukh
agents are untouched:

- `agents/pramukh/agent.yaml` — golden-pinned by `test_prompt_golden.py:30`
  (36 byte-identical prompt fixtures + stage trace 1/8/6/99/99/99 + load sweep).
- `agents/pramukh_agent/agent.yaml` — the Copilot-built rev-6 agent that takes
  live outbound calls. **It has NO golden coverage today** (only the load
  sweep) and has materially different data: explicit `role:` on all 9 stages
  (incl. `discovery`), typed slots, `objection_playbook: []`,
  `use_builtin_rules: true`. Several "safe" gating axes (stage.role, slot.type,
  turns==8) are NOT safe for it. Safe axes shared by both: **non-empty
  `stage.collect`**, **genuinely new optional fields defaulting to today's
  behavior**, and **create-path-only code**.

## Evidence (verified from live logs)

### Call A — Kaapd / `shubh_assistant`, conn `cf6b0e23`, 2026-07-15 13:26
`recordings/918076562864_20260715_132643/` + `phase14_runtime/logs/latest/ws_audio_receiver.log`

- **Routing was CORRECT.** WS path `.../918076562864/-/-/shubh_assistant`;
  registry loaded the YAML fresh at call time. Greeting and closing were spoken
  **verbatim** from the YAML. The failure is engine semantics, not routing.
- **Stage machine stalled at stage 0 the whole call.** `GREETING →
  CONFIRM_PERSON` at 13:27:01, then never again across 6 caller turns. Every
  Copilot-emitted stage has `advance_when: {}`, `collect: []`, `role: ''`,
  `turns_until_advance: 8` (PITCH 6, CLOSING 99). In
  `_maybe_advance_stage` (conversation.py:1582-1675) every gate was dead:
  no booking/appointment events, empty advance_when skips the declarative
  gate, role ''→'custom' blocks the discovery early-advance, and the 8-turn
  threshold exceeds the call length. **The PITCH goal — which contains the
  user's three discovery questions — never entered a single prompt** (only the
  current stage's goal renders, conversation.py:2536-2537).
- **The one-line ask-directive WAS in the prompt every turn and was ignored.**
  Because stage index ≤ 1 forces discovery prompting (conversation.py:2614-2618),
  every turn's prompt carried "Ask ONLY ONE question next, about: Current
  Stock. Suggested phrasing: आपके पास अभी कौन से कपड़े हैं?" — the live model
  (env `LLM_MODEL=gemini-3.1-flash-lite`, `LLM_REASONING_EFFORT=none`,
  max_tokens 160; YAML's gpt-4o-mini is env-shadowed per conversation.py:588)
  ignored it for 6 straight turns and also violated the rendered "✗ NEVER
  pretends to make cotton or natural fibres" rule. **Stage stall and model
  non-compliance are co-causes** — prompt fixes alone are necessary, not
  sufficient.
- **The user's 4 scripted objection answers never reached the prompt.**
  `objection_block` (prompt_blocks.py:474-492) renders only entries carrying
  `prompt_guidance`; no writer (Copilot or extractor) ever produces it, so the
  hardcoded real-estate fallback rendered instead ("'Comparing builders' → …
  30 years, zero stalls, RERA"). The deterministic path was also dead: the
  agent's 5 custom English `objection_patterns` REPLACE the ~28-phrase default
  deferral set (`objection_patterns or _DEFAULT…`, conversation.py:151), and
  the playbook's English-only patterns can't substring-match Hindi speech.
- **Real-estate firm rules injected.** `use_builtin_rules: true` (Copilot
  skeleton default, agent_copilot_api.py:415; schema default schema.py:499)
  renders the only builtin pack that exists — real-estate ("site visit",
  "recommend a project", "property pitches", prompt_blocks.py:537-573).
- **Knowledge vacuum.** `catalog: []` → the entire knowledge band is one
  271-char paragraph (catalog.py:21-23). `known_projects` is never rendered
  into any prompt. No grounding rule ties product/price/stock claims to the
  KNOWLEDGE BASE; the only fabrication rule covers "prices or dates". The
  model invented sarees/suits/cotton into that vacuum and mirrored the
  real-estate objection line "'Price too high' → suggest lower options" with
  "best price पर final कर दूँगा" (against `pricing_policy: defer`).
- **Slots never captured** (`domain={}` all turns — that log field is the
  legacy real-estate store, but the slot store was also empty:
  `recordings/_state/state_cf6b0e23….json` has all three slots null,
  `audit_log: []`). Untyped slots with no patterns have no regex extractor
  (state_manager.py:363-418 dispatches on legacy real-estate names only); the
  LLM gap-filler can only capture what the caller says, and the caller was
  never asked.
- **Junk calendar band.** The Copilot LLM authored six identical
  "10:00 AM" mock_slots at create time (anchored by the '10:00 AM' example in
  both prompts — agent_copilot_api.py:3354, yaml_filler.py:69); the shape-only
  calendar sanitizer passed them; `calendar_block` renders any non-empty
  mock_slots as live availability (prompt_blocks.py:319) — steering a
  no-booking B2B flow toward appointment behavior. The seed `slots_text` was
  never regenerated and contradicts the mock_slots.
- **Latency filler on every turn.** Copilot skeleton writes
  `filler_timeout_s: 0.6` (agent_copilot_api.py:405) vs platform default 1.5;
  live first-chunk latencies were 630–1178ms, so the filler fired on
  essentially every LLM turn; `_ACK_TOKENS` are romanized-only so Devanagari
  approvals always got the 2-entry "thinking" bank rotating "एक मिनट…"/"हाँ,
  एक सेकंड…" (conversation.py:2985-3001); once the filler fired first, the
  real reply queued up to ~718ms behind its TTS.
- **English farewell on a Hindi call.** The caller's Latin-script "bye bye,
  thank you" flipped `state.language` to `en` (both via the conversation-layer
  switch — unlocked threshold is 2 words — and the pipeline's ungated
  per-segment override at pipeline.py:814-829) one turn before
  `closing_responses` was rendered.
- **STT input loss.** Two genuine Hindi utterances were transcribed in
  Gurmukhi (`pa-IN`, confidence 1.00), rejected as unsupported_script, and
  treated as "clean empty — skipping fallback" (stt/router.py:394) — 2 of ~8
  caller utterances never reached the conversation at all.

### Calls B — `pramukh_agent`, 12:11–12:13 (3 calls)
Every turn answered "Sorry, could you repeat that?": `_build_system_prompt →
firm_rules_block → _fabrication_rule` raised TypeError
(`forbidden_fabrications: true`, a bool where the engine expects a list),
swallowed per-turn by the blanket `except` in `_run_llm_to_queue`
(conversation.py:2352-2358) into the same canned fallback used for transient
LLM errors. Spot-patched since (sanitizer + type-guard), but the class remains:
**nothing validates a Copilot-built agent against the engine at create/update
time, and a live prompt-build crash is indistinguishable from a network blip.**

### Platform-level findings beyond the two incidents
- **Registry staleness risk:** `AgentRegistry.get()` caches parsed definitions
  forever with no mtime check (registry.py:42-61); freshness depends entirely
  on a watchfiles thread that dies silently (DEBUG log, never restarted) with a
  ~1.6s debounce window. It works today (log-verified cross-container
  invalidations) but is not a guarantee.
- **No per-call fidelity record:** `calls.agent_slug` exists; the revision/sha
  that served the call is recorded nowhere (not in calls, not in
  metadata.json).
- **Silent model switches:** the per-turn fallback chain
  (conversation.py:2436-2450) has switched live calls to US-hosted gpt-4o-mini
  **666 times** in the current log (Groq era) with only a WARNING line.
- **False model affordance:** `llm_config.model` is editable in the wizard UI
  (AgentFormSections.tsx:76-77, round-tripped by agents.ts) and written by the
  skeleton, but env `LLM_MODEL` always wins (conversation.py:588) — users can
  "choose" a model that is silently ignored.
- **Interview certifies dead content:** `post_call.success_definition` has zero
  consumers repo-wide; `callback_policy` without `enabled: true` is dead;
  `whatsapp_promise` is consumed only via a branch action no Copilot agent
  gets; `decline_responses`/`slots_prompt`/`ai_disclosure_responses`/
  `proactive_prompts` are parsed but never read. The knowledge gate
  (`_done_knowledge`) closes on 80 chars of prose with an empty catalog.
- **No CI:** `.github` does not exist; no hooks; the golden harness runs only
  when someone remembers to run pytest. The byte-identity "hard constraint" is
  a convention, not a guarantee.
- **No pre-dial acceptance path:** the first execution of a built agent is a
  production phone call. `ConversationRouter` is text-in/text-out and trivially
  simulable; nothing uses that.
- **Evidence chain down:** GCS uploads failing since 03:00 (ADC RefreshError),
  `lescall-recsync.timer` inactive, and the sentinel has alerted hourly for
  13+ hours with no human receiving it (GCE blocks outbound SMTP).

## Root-cause summary

| # | Failure seen on call | Root cause | Fix |
|---|---------------------|-----------|-----|
| 1 | No discovery, free-styled pitch | Copilot emits stages with no advance_when/collect/role; 8-turn default unreachable; engine has no goal-met exit | F3, F4, F10 |
| 2 | Real-estate objections spoken | `objection_block` renders `prompt_guidance` only; writers never produce it; hardcoded RE fallback | F1 |
| 3 | Real-estate rules on a fabric agent | Only builtin rule pack is real-estate; default-on everywhere | F2 |
| 4 | Invented products, "best price" | Empty catalog + no grounding rule + flash-lite/reasoning-none ignoring rendered rules | F2, F11, F12, F17 |
| 5 | Slots never captured | Slot↔stage wiring never emitted; untyped slots have no extractor; agent never asked | F4, F10 |
| 6 | Dead calls (Incident B), silent | No create-time engine validation; prompt crash = canned fallback loop | F6, F7 |
| 7 | Junk calendar / filler spam / EN farewell / dead fields | Shape-only sanitizers, bad skeleton defaults, dead-field emission | F5, F13, F14, F15 |
| 8 | "Is the built agent the served agent?" unprovable | No revision/sha on call records; registry cache best-effort | F7, F8 |
| 9 | Caller speech dropped (pa-IN) | Script-rejection treated as clean-empty, no retry | F16 |

## Design doctrine (reconciled across dimensions)

1. **The engine renders what IS; the Copilot decides what to WRITE.** No
   engine-side data heuristics (e.g. "thin knowledge → inject band"): prompt
   rendering stays a deterministic function of the YAML, gated by explicit
   fields. Thin-knowledge detection lives in the Copilot.
2. **One canonical field per feature.** Rules pack:
   `guardrails.builtin_rules_pack` ('real_estate' default | 'generic').
   Grounding: `guardrails.honesty.knowledge_grounding` ('strict' | absent).
   No duplicate/competing field names.
3. **Gate on `stage.collect` (non-empty), never on `stage.role`.** Role-based
   gates change live `pramukh_agent` prompts (it has explicit `discovery`
   roles); collect is empty on every stage of both Pramukh agents.
4. **Create-path-only enrichment.** All derivation lives in ONE function called
   exclusively from the copilot `create_agent` handler (after
   `_enforce_invariants` ~L2711, before `from_dict`/`to_yaml` at ~L2712-2714).
   `_normalize_definition` stays shape-repair-only forever: it re-runs over
   EXISTING agents' change subtrees on every copilot edit (L2742), so any
   enrichment there rewires live Pramukh. Enforced by a unit test:
   `_normalize_definition({'guardrails': {}})` and `({'call_flow': {...pramukh
   stages…}})` inject nothing.
5. **Fills-empty-only + provenance-respecting.** Enrichment writes only
   absent/empty fields, records provenance `proposal`, and emits notes; user
   input always wins (existing input-priority regime).
6. **Checks report, never repair.** The boot-check/quality warnings never
   mutate YAML; repairs go through the Copilot LLM regenerating the spec, or
   the explicit opt-in repair tool with a dry-run diff.

---

## Fixes

### PHASE 0 — Safety rails (land FIRST, before any engine change)

**F0.1 — Deploy gate / CI.** Add `deploy/deploy.sh`: runs
`../venv/bin/python -m pytest tests/ -q` (golden + load sweep + stage traces +
all new tests) inside the lescall-app image and refuses `docker compose
restart` on failure. Optional GitHub Actions workflow for PR hygiene. Without
this, every guarantee below is manual.

**F0.2 — Golden fixtures for `pramukh_agent`.** Extend
`test_prompt_golden.py` with a second fixture set pinning
`agents/pramukh_agent/agent.yaml` rendered prompts (9 stages × 3 langs × 2
directions). Three dimensions independently found engine changes that CI would
have passed while changing this live agent's prompts.

**F0.3 — Kaapd end-to-end acceptance fixture.** New test: run
`deploy/kaapd_test_brief.txt` through extractor + copilot create with all
enrichments (LLM mocked/recorded), pin the emitted agent.yaml and rendered
prompts, and drive a scripted 6-turn stage-advance trace mirroring the incident
transcript. Asserts: leaves CONFIRM_PERSON by turn 2; PITCH renders the
current_stock ask-directive; no real-estate FIRM RULES/objection lines; the
user's objection answers render; no junk calendar band. **This test is the
acceptance criterion for the whole remediation.**

### PHASE 1 — Engine (all gated; golden must pass unregenerated)

**F1 — Objection answers reach the prompt.**
`prompt_blocks.objection_block(agent_def, lang='en')` gains a middle branch:
playbook non-empty AND no entry has `prompt_guidance` → render one line per
entry (cap 8): `'{up to 2 patterns}' → {answer[lang]→en→first}` (~240 chars
each, `{trust_proofs}` substituted as today). Empty playbook → legacy 4 lines
**byte-exact**. Call site conversation.py:2638 passes the current language.
*Gating:* golden pramukh has no playbook key; pramukh_agent has `[]` — both
stay on the untouched fallback branch (its bytes are pinned in all 36
fixtures). Fixes shubh_assistant with **zero data migration**.

**F2 — Industry-neutral builtin rules + knowledge grounding.**
(a) New schema field `guardrails.builtin_rules_pack: 'real_estate'` (default).
`firm_rules_block`: 'real_estate' → today's bytes untouched (including the
flexible-scheduling branch pramukh_agent renders); 'generic' → a new band
keeping every domain-neutral line and replacing the five real-estate lines
("site visit"→"appointment or callback", "portfolio"→"catalogue",
"property pitches"→"detailed pitch before confirming it's a good time", etc.),
with the fabrication line routed through `_fabrication_rule` with 'products'
added for the generic pack.
(b) New honesty key `guardrails.honesty.knowledge_grounding: 'strict'`
(free-dict — zero schema change): renders a KNOWLEDGE GROUNDING band directly
after `portfolio_block` — "the section above is your ONLY source of truth
about what this business makes/sells/charges; if it's not there, deflect in
one sentence (offer to send the catalogue / have the team check) and return to
your CURRENT GOAL."
*Gating:* defaults reproduce today's bytes; both Pramukh agents lack both keys.
Normalizer: coerce-only-when-present, never inject (tested invariant, doctrine
4). Deploy note: `store.write_agent` canonicalizes YAML, so the new field
appears as `builtin_rules_pack: real_estate` in files on their next save —
prompt-identical, but **engine schema must deploy to BOTH bind-mount containers
before any dashboard write** or the key churns/drops in revision diffs.

**F3 — Stage machine: `advance_when.max_turns` OR-escape (+ optional
confirm-person advance).**
(a) In the `if aw:` block (conversation.py:1635-1642):
`(_has_and_conditions(aw) and self._advance_when_met(...)) or (mx and dt >= mx)`
where `_has_and_conditions` requires ≥1 of min_turns/all_collect_filled/slots/
intent/appointment_captured — this fixes both today's stranding hazard (a
non-empty advance_when disables turn pacing forever) and the vacuous-True bug
(`_advance_when_met` returns True on unrecognized-keys-only dicts).
(b) Optional belt-and-braces: stages with **explicit** `role: 'confirm_person'`
(no agent on disk carries it; the derived-role shim must NOT be used — it maps
legacy OPENING→confirm_person) advance on `last_intent == 'affirmation'` at
`dt >= 1` when advance_when is empty. Reuses the existing deterministic
[INTENT] classifier — three of the Kaapd call's six turns were classified
`affirmation` while the stage goal was a yes/no confirmation.
*Gating:* (a) unreachable for every existing agent (zero non-empty
advance_when across all 18 YAMLs, grep-verified); (b) explicit-role opt-in
only. No prompt bytes change.

**F4 — Strong ask-directive follows `stage.collect`.** Extend the
`stage_discovery` boolean (conversation.py:2614-2618) with
`or bool(current_stage.collect)` — a stage that declares slots gets the strong
"Ask ONLY ONE question next" directive at any index. **Collect-only gate; role
is explicitly rejected as a gate** (would flip pramukh_agent's index-2/3
prompts). Companion: `note_asked` counts once per turn_index, not per prompt
build (double-count bug); golden proves byte-identity (no Pramukh slot has
max_asks>0 or reask_question).

**F5 — Calendar band only for flows that schedule.** `calendar_block` returns
[] when no stage's **effective role** (`stage_roles.stage_role` —
explicit-or-derived) is scheduling/confirmation. Pramukh keeps its band
(INVITE_VISIT→scheduling derived; pramukh_agent has explicit scheduling role).
Ship together with F10's role wiring so copilot agents that DO book keep their
band.

**F6 — Call-start preflight + failure-streak guard.** In `_build_agent_stack`
(conversation_call_bridge.py:~280): render `_build_system_prompt()` for every
stage × every supported language on a throwaway router (mock calendar — the
golden-harness recipe; measured ~5-15ms/agent). On exception: ERROR log with
stage/exc, set `agent_health='preflight_failed:<stage>'`, swap to the existing
`get_neutral_fallback_agent()` path — one honest apology instead of an infinite
canned loop. Per-turn: `self._llm_failure_streak`; when
`limit > 0 and streak >= limit` (env `LLM_FAILURE_STREAK_LIMIT`, default 0=off
— the `limit>0` guard is mandatory or the first transient error would end
healthy calls), queue the graceful-close line. **Note: no engine-initiated
hangup exists today** — the close line plays but the call stays up until the
caller hangs up; a bridge-driven `uuid_kill` hook is a stretch item.
*Gating:* env `PROMPT_PREFLIGHT=true` — a successful preflight discards its
output; behavior changes only for calls that would crash-loop today.

**F7 — Create/update-time ENGINE BOOT-CHECK (the Incident-B killer).** New
`calling-agent/backend/app/agent_definition/boot_check.py`:
`engine_boot_check(agent) -> {errors, warnings}` — for both directions × every
supported language: throwaway `ConversationRouter`, mock calendar,
`generate_opening()` via `asyncio.to_thread` (assert no raw `{token}`
survives), `_build_system_prompt()` at **every stage id** (implemented as the
full router sweep, mirroring the golden harness, so it can never drift from
the live prompt path). Errors name the innermost prompt_blocks frame. Wire
into: copilot `create_agent` (raise ValueError — the established tool-error
path the LLM repairs from), copilot `update_agent`, builder create/update/
validate (HTTP 422). Non-fatal warnings: playbook without guidance (pre-F1),
`use_builtin_rules` real-estate pack on a non-RE-looking agent, inert stage
machine (all advance_when empty AND all non-final thresholds ≥6). Empirically:
imports ~0.9s once; 5-13ms per agent; both Pramukh agents pass strict today
(verified). Incident B's exact YAML is caught pre-write.
*Gating:* env `ENGINE_BOOT_CHECK=strict|warn|off` — strict for CREATE from day
one; warn for UPDATE for one burn-in cycle then strict. Read-only: never
mutates the draft. Warnings must stay warnings forever (doctrine 6).

**F8 — Registry hard-freshness + per-call fidelity record.**
(a) `AgentRegistry.get()`: on cache hit, `os.stat` the YAML (µs) and reload on
mtime_ns/size change; keep `{sha256, revision, mtime}` meta per slug. Handle
FileNotFoundError (agent archived) by serving the cached def with a WARNING.
Watcher: wrap in restart-loop with 5s backoff; death logs at ERROR; new
`registry.health()`; normalize `AGENTS_DIR` in conversation_call_bridge.py:54
to a package-relative path. Env `AGENT_CACHE_STAT_CHECK=true` (off restores
event-only semantics).
(b) Fidelity record: calls table + metadata.json + the "Active agent:" log
line gain `agent_revision`, `agent_sha`, `agent_health`, `llm_models_used`,
`fallback_turns` (additive MIGRATIONS; optional kwargs on
`audit_manager.create`; sidecar-style additions — don't restructure
transcript.txt). **Off-by-one caution (verified):** `agent_revisions` stores
the PRE-write snapshot; compare a served sha against `write_agent`'s returned
rev, or persist the new sha into the revision row as an additive column.
(c) Loud routing: WARN when a WS slug segment is rejected; log which
resolution level picked the agent (explicit/campaign/did/dial-meta/legacy).

**F9 — LLM config hygiene + observability.**
(a) Per-call model record: `llm_models_used` starts `[self._model]`; each
fallback appends the fallback model and bumps `fallback_turns` (the 666 silent
switches become measurable; also the burn-in metric for any model upgrade).
(b) max_tokens validation: copilot CREATE path coerces non-int/out-of-range to
160 with a note; UPDATE path **rejects** (check, not mutation — §5.4 doctrine).
(c) `llm_config.model` is a false affordance: document env precedence in
schema.py + conversation.py:588; DON'T chase the disk field (to_yaml
re-materializes the schema default — churn for nothing). Optional later:
`llm_config.model_tier: ''|'fast'|'quality'` mapped to env-declared models on
the same endpoint; frontend field removal/annotation needs the SPA image
rebuild (Phase 4).
(d) Model upgrade runbook (doc only, no env change now): prerequisite = F1/F2
land first (a stronger model follows the WRONG rules more faithfully);
prefer the tier-split path over a fleet-wide swap — a fleet swap changes
Pramukh's live model and the golden harness is model-agnostic (zero
protection); burn-in gates = golden green, owner test call, TTFT p50/p95
compare, `fallback_turns == 0`.

### PHASE 2 — Copilot writers (create-path; extractor; interview)

**F10 — ONE create-time enricher `_enrich_new_agent(merged, notes)`.** Called
ONLY from copilot `create_agent` (insertion: after `_enforce_invariants`
~L2711, before `from_dict` at ~L2712 — NOT "near _quality_warnings", which
runs after the write). Ordered, conflict-free rule table (merges the three
dimensions' competing enrichers; all rules fills-empty-only, idempotent,
provenance `proposal`, note-emitting):
1. **collect:** match slot identity tokens (name, label, question words ≥4
   chars) against stage goal text; assign each required slot to its
   best-matching stage — **goal-token based, independent of stage id/role**,
   so Kaapd's legacy-id PITCH (whose goal names all three slots) gets
   `collect: [current_stock, monthly_quantity, operating_city]`. Unmatched
   required slots → the stage with the most matches, else the first
   non-(greeting/confirm/closing) stage.
2. **advance_when:** stage got collect and advance_when=={} →
   `{all_collect_filled: true, max_turns: 6}` (requires F3a). Order-0 stage
   whose goal is a confirmation and direction is outbound →
   `{intent: affirmation, min_turns: 1, max_turns: 3}`.
3. **role:** only when `role==''` AND id not in LEGACY_STAGE_ROLES: keyword map
   (confirm/right person/owner → confirm_person; introduce/good time →
   greeting; got collect → discovery; recommend/offer/match/sample →
   recommend; delivery/book/schedule → scheduling; thank/goodbye → closing).
4. **pacing:** only when the value equals the injected default (8 — provable
   for post-provenance drafts whose stage rows never carried the key):
   confirm/greeting-shaped → 2, discovery → max(4, len(collect)+2),
   single-question stages → 3; last stage stays 99. Role derivation and turns
   are co-located here (the L865 id-keyed fallback stays untouched for legacy
   ids — seed `PROJECT_MATCH: 8`/`SLOT_BOOKING: 8` into `turns_default` so a
   future pramukh stage-list edit can't pick up a new default).
5. **prompt_guidance:** entry has answer, no guidance → `"'{patterns[0]}' →
   {answer[default_lang] or answer.en}"` — the user's verbatim words, zero
   authorship.
6. **qa_field_signals:** per slot, setdefault([label] + question keywords).
7. **pricing defer_line:** mode==defer, defer_line absent, playbook has a
   rates/price entry → defer_line = that answer (user's words).
8. **slot types:** type=='' → 'text', **except** legacy-dispatch names
   (location/bhk/budget/…: typing them kills their regex extractor —
   state_manager.py:374-418 gates on `_untyped`); map quantity-shaped →
   number, city-shaped → location when kb.locations is non-empty.
*Gating:* create-only is the load-bearing gate — pramukh_agent EXACTLY matches
the enrichment precondition (all stages collect-empty/advance_when-empty), so
this function must be provably unreachable from update paths (regression test:
update_agent on a pramukh_agent-shaped YAML leaves call_flow byte-identical;
enrich(enrich(x))==enrich(x); a draft with any user-set wiring passes through
byte-identical).

**F11 — Extractor emission upgrade** (`yaml_filler.py` `_GROUP_FIELDS`):
stages as full objects ({id, goal, collect, advance_when, role — omit when the
doc doesn't say}); playbook entries with `prompt_guidance` + patterns "in
every language the call uses, incl. Hindi transliterations"; catalog reworded
industry-neutral ("EVERY distinct product/service/family; a single product
family is still one catalog item; derive pitch_points from the document's own
claims; NEVER invent items"); portfolio_prompt_text mandate (history, founders,
address, contact channels, process facts); NEW 'logistics' group → scheduling
{days, window}, callback_policy {enabled, capture_exact_time},
whatsapp_promise, opening_greeting_by_language. Known_projects→catalog
backstop in `_merge_extracted_into_draft` (draft-only; NEVER in `apply_fields`
— that path reaches existing agents incl. pramukh_agent and would flip its
knowledge band golden-invisibly): synthesize one catalog item per
known_projects key when catalog is empty in both extraction and draft.
`_REQUIRED_FIELDS` += catalog/objections, reconciled with the no-re-ask
doctrine: warn-first, ask the user ONLY if the corpus has no answer.

**F12 — Interview gates tell the truth.**
(a) `_done_knowledge`: sales drafts (call_direction outbound OR any pitch/
recommend-role stage — stated limitation: inbound sales with roleless stages
degenerate to the 80-char rule) require ≥1 catalog item with a name;
`skip_layer` remains the escape hatch and stamps
`honesty.knowledge_grounding: 'strict'` so a skipped layer produces a
deflecting agent, not an inventing one.
(b) `_done_scheduling`: accept an explicit no-booking marker
(`calendar: {type: 'none'}` / `scheduling: {enabled: false}`) — today
`mock_slots=[]` can never close the layer.
(c) Stop soliciting dead fields: remove `proactive_prompts`/`slots_prompt`/
`post_call.success_definition` from the spec doc + skeleton (schema parsing
stays — golden pramukh has populated proactive_prompts); fix misleading schema
comments (graceful_close is the STT-failure loop breaker, not a warm close).
(d) New quality warnings (emit for create/validate only, or suppress when all
stage ids are legacy — they'd otherwise nag every future Pramukh edit and
LLM-self-repair would push edits onto the production agent): custom-id stages
with role ''; required slots in no stage.collect; non-final custom-id stage
with turns ≥8 (exempt 99-sentinels and scheduling/confirmation/closing roles);
advance_when using intent/slots without max_turns; playbook patterns/answers
missing the default language; en-only patterns on a hi-default agent.

**F13 — Calendar sanity.** Seed calendar becomes empty (no phantom 3-slot
default); sanitizer (only when the incoming payload carries a calendar key —
never synthesizing keys not in the payload): dedupe exact (day,time) pairs,
trim ≥4-identical-times to 2 + note, cap 12; regenerate slots_text ONLY when
the existing value is byte-equal to a platform default (never touch authored
text — pramukh's is hand-authored trilingual); vary example times in both
prompts ("Tomorrow 11:00 AM", "Friday 4:30 PM") + "omit entirely if the
business doesn't book time slots".

**F14 — Skeleton defaults for NEW agents** (`_default_definition` — provably
create/preview-only): `builtin_rules_pack: 'generic'` (system prompt
instructs: set 'real_estate' for real-estate businesses; quality warning when
kb looks RE but pack is generic), `honesty.knowledge_grounding: 'strict'` +
default unknown_answer_policy, `filler_timeout_s: 1.2` (live TTFT is
630-1178ms; 0.6 guarantees filler spam), `filler_min_gap_turns: 2`,
`filler_max_per_call: 3`, `filler_ack_extended: true`,
`languages.closing_language_policy: 'dominant'`,
`objection_patterns_mode: 'extend'`, drop `llm_config.model` from the skeleton
dict (schema default re-materializes it — that's fine; the doc comment is the
fix), drop the phantom calendar.

**F15 — Response-bank runtime knobs (engine, all default=today).**
(a) Filler: `filler_min_gap_turns`/`filler_max_per_call`/`filler_ack_extended`
in LLMConfig (dataclass defaults 0/0/False — keyword-parsed so the load sweep
passes); suppressed filler = wait silently for the first chunk; ack-extension
adds Devanagari/Gujarati affirmations as a per-call conditional union (never
mutate the module set).
(b) Closing language: `languages.closing_language_policy: ''|'dominant'`;
'dominant' = majority over `memory.turns` per-turn languages (NOT
state.language_history — it's clobbered to [lang]*5 on switches), applied at
the four closing sites (farewell, `_localized('closing')`, decline_farewell,
wrong_number_close) via optional `lang=None` params. The '' default is
**load-bearing**: pramukh_agent HAS closing_responses and speaks them today.
(c) `objection_patterns_mode: 'replace'` (default — mandatory: both Pramukh
agents carry custom pattern lists and 'extend' would widen their objection
intent) | 'extend' (defaults + custom merged at the detect_intent call site).
(d) Wire `decline_responses` (prefer over decline_farewell when non-empty).
`ai_disclosure_responses` wiring needs NEW are-you-an-AI intent detection
(identity_question doesn't cover it) — scoped as its own small item, gated on
non-empty bank.

**F16 — STT script-misdetection retry.** In STTRouter: when
`validate_transcript` rejects for unsupported_script AND the segment has real
speech duration, retry Sarvam ONCE with `language_code` forced to the current
conversation language (1 retry/turn cap); classify script-rejection as
fallback-eligible instead of "clean empty". Copilot: emit
`languages.stt_forced_language` when the user declares a fixed language set.
Add per-call STT drop counters to the fidelity record.

### PHASE 3 — Acceptance layer & ops

**F17 — Pre-dial acceptance.**
(a) Create-time dry-run: script N synthetic turns (affirmations + slot fills)
through `_maybe_advance_stage`/`_maybe_branch`; assert every stage dynamically
reachable and every required slot has an asking stage. This replaces a static
reachability gate — a static "turns<99 OR advance_when OR branch" rule
false-blocks golden pramukh (INVITE_VISIT/CONFIRMATION are 99/empty/branchless
and advance via universal booking gates; simulation with those gates passes).
(b) "Try your agent" text chat: dashboard endpoint instantiating
`ConversationRouter` over the just-built YAML with the LIVE env model,
streaming turns in the Copilot UI. The Kaapd failures were all discoverable in
60 seconds of text chat.
(c) Test-call UX: display which agent the dialed number resolves to before
dialing.

**F18 — Post-call compliance audit** (lescall-workers job, read-only):
deterministic checks per call — price/number regex when pricing_policy=defer;
product nouns vs catalog membership; discovery-question-asked vs required
slots; optional cheap LLM-judge scoring the transcript against never_rules.
Writes a compliance JSON + violation flags to the calls record; dashboard
badge; violation streaks feed the sentinel. This is the only layer that
catches "correct prompt, disobedient model" — proven necessary twice in this
audit. Doubles as the model-upgrade burn-in metric.

**F19 — Ops repairs (immediate, independent of code).**
(a) Execute RUNBOOK §6b: attach the recsync SA to the VM (ADC RefreshError has
had every GCS upload failing since 03:00); enable `lescall-recsync.timer` to
backfill today's failed uploads; disk-usage guard on recordings/.
(b) Sentinel: fix the delivery channel (GCE blocks port 25 — relay via 587 or
WhatsApp/dashboard-banner via existing infra); add call-health checks over the
new fidelity fields (fallback_turns>0 streaks, preflight failures, canned-
fallback streaks = Incident-B signature, STT drop counts, GCS failure streaks);
louder second channel when an alert key persists.

**F20 — Repair tool for existing broken agents** (`enrich_agent(name)` copilot
tool): runs F10's enricher over an existing agent, **dry-run diff first,
explicit user confirmation required**; production guard = refuse auto-apply
for any agent that is active, DID-bound, or present in recent calls
(active_agent.json alone is NOT the production signal — pramukh_agent is
DID-routed). Never wired into any automatic path. This is how shubh_assistant
gets fixed without hand-editing YAML, honoring the platform-not-agent
directive.

---

## Rollout choreography

| Change class | Files | Deploy action |
|---|---|---|
| Engine schema (new fields) | schema.py | restart **both** lescall-runtime AND lescall-api (both import engine schema) — BEFORE any dashboard write persists new keys |
| Engine code paths | conversation.py, prompt_blocks.py, state_manager.py, registry.py, stt/, boot_check.py | restart lescall-runtime + lescall-api |
| Copilot/extractor | agent_copilot_api.py, copilot_interview.py, yaml_filler.py, agent_builder_api.py | restart lescall-api |
| Bridge/audit | conversation_call_bridge.py, call_audit.py, ws_audio_receiver.py | restart lescall-runtime |
| DB migrations | shared/db | additive ALTERs via MIGRATIONS (never recreate lescall-pg) |
| Frontend (model field, Try-agent UI) | frontend/src | rebuild lescall-dashboard image |

Order: **Phase 0 → engine schema + gated code (F1-F9) → copilot writers
(F10-F16) → acceptance/ops (F17-F20) → frontend.** Post-deploy verification per
phase: run the full suite inside the restarted container + one owner test-call.

## Acceptance criteria ("does not occur again")

1. F0.3's Kaapd fixture passes: brief → built agent → advancing stages, user's
   objection lines rendered, no real-estate leakage, no phantom calendar.
2. Incident-B YAML shape is refused at create with an actionable error
   (boot-check test), and a live prompt crash degrades to ONE honest line +
   ERROR + `agent_health` flag, never a canned loop.
3. Every call record answers "which build served this call" (slug + revision +
   sha + model(s) used).
4. Golden: `agents/pramukh` 36 fixtures byte-identical AND new
   `agents/pramukh_agent` fixtures byte-identical, enforced by the deploy gate.
5. A built agent can be exercised by text chat before any phone call.
6. Post-call compliance flags fire on a deliberate violation transcript
   (seeded test).

## Explicitly out of scope / decisions deferred to you

- Fleet model upgrade (gemini-3.5-flash): documented path only; tier-split
  recommended over fleet-swap. No env change in this plan.
- Engine-initiated hangup on failure streaks (needs a new bridge→socket_server
  kill hook) — stretch.
- Repairing shubh_assistant/pramukh_agent data via F20 requires your explicit
  per-agent confirmation after seeing the dry-run diff.
- 'Are you an AI' deterministic intent (for ai_disclosure_responses wiring) —
  small standalone item, needs new patterns with false-positive review.
