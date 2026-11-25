# PLAN — Agent Copilot v2: Guided Agent Builder + Surgical Edit Engine

**Status:** awaiting approval · **Date:** 2026-07-14
**Grounding:** 21-agent code audit (8 subsystem readers, 12 layer analysts, 1 completeness critic) + first-hand reads of `agent_copilot_api.py`, `schema.py`, `state_manager.py`, `registry.py`, `conversation.py` prompt region, `agents/pramukh/agent.yaml`. Every claim below carries a file:line reference verified against the current tree.

---

## 0. Executive summary

The goal: Copilot runs a **guided, back-and-forth interview** that compulsorily covers ~10 config layers, stores every answer in a rich **AgentSpec**, and a runtime that reads **all** of it — so a built agent calls exactly like Pramukh's does, for any business. Plus **surgical editing**: any change lands on exactly the field asked about, provably touching nothing else, versioned and reversible.

What the audit found, in one paragraph: the platform is much further along than the "7 persona fields" picture — `AgentDefinition` already carries identity, personality, per-language voices/register notes, staged call flow, knowledge base, response bank, calendar, per-agent slots with per-language questions, and `call_direction`, all hot-reloaded from `agents/<slug>/agent.yaml`. **But** the Copilot interview is 100% prompt-prose with zero server-side state (nothing tracks which questions were answered; "just build it with defaults" is an explicit escape hatch), the Copilot's own prompt *hides* the `slots` section so no built agent ever gets a qualification schema, the stage machine is strictly linear with no gates/branches, and a large band of behavior is still platform-hardcoded Pramukh doctrine injected into *every* agent — objection scripts ("30 years, zero stalls, RERA"), "push toward a site visit", "never volunteer pricing", "exactly TWO time options", never-read-back-phone, the 2-strike decline. Edits apply instantly with no diff preview, no versioning, no rollback, no concurrency control across 4 independent write paths — and a failed agent load silently answers the call **as Vishal Patel from Pramukh Group** (`conversation.py:558`).

The plan: (1) a **safety substrate** (golden-prompt harness, atomic writes, kill the Pramukh fallback); (2) **AgentSpec v2** — one coherent schema extension where every currently-hardcoded behavior gets exactly one data home, all defaults reproducing today's behavior byte-for-byte; (3) **engine wiring** in `conversation.py`/`state_manager.py` behind those defaults; (4) a **surgical edit engine** (single write choke point, revisions in Postgres, propose→diff→confirm→apply, path-level ops, rollback); (5) a **stateful interview engine** (data-driven layer blueprint, server-tracked draft + completeness, document upload, provenance); (6) **frontend** (progress checklist, live spec preview, diff cards, stage editor); (7) phased rollout where the live Pramukh agent is provably unchanged at every step.

---

## 1. Diagnosis — why what you asked for was not achieved

### 1.1 The interview is prompt-doctrine, not a machine

- The *entire* interview exists as prose in `_system_prompt()` (`dashboard/agent_copilot_api.py:1551-1615`). Seven bullets, "usually 5-8 answers". Nothing in code tracks which bullets were answered.
- The only mechanical gate is `user_turns >= 2` before `create_agent` (`agent_copilot_api.py:1413-1418`). An info dump + "yes" clears it. Line 1567 explicitly authorizes *"just build it with defaults"* — dump-and-done is sanctioned, not prevented.
- Session state is only `{messages, transcript, meta}` (`agent_copilot_api.py:1621`): **no draft object, no per-layer progress, no provenance** of confirmed-vs-defaulted values.
- Context is trimmed to 48 messages and old tool results truncated to 900 chars (`agent_copilot_api.py:80, 1750-1783`) — a thorough 10-layer interview *exceeds the window and the LLM forgets early answers by create time*. Nothing durable captures them.
- Quality control is post-hoc string-length heuristics (`_quality_warnings`, `agent_copilot_api.py:1091-1201`) that fire *after* the YAML is written, relying on the LLM to self-repair.
- No document upload exists in the Copilot at all — chat is text-only (`agent_copilot_api.py:1876-1882`); extract-document/upload live only as Build-page endpoints (`agent_builder_api.py:398, 444`).

### 1.2 The Copilot can't even write the most important layer

- `slots` (the qualification schema — the heart of "stores their response perfectly") is a real, working spec section: `SlotDefinition` (`schema.py:150-162`), consumed by `build_state_manager` (`state_manager.py:623-661`), fully populated in `agents/pramukh/agent.yaml:457-521` with hi/en/gu question phrasing per slot.
- **The Copilot's system prompt never mentions it** (`agent_copilot_api.py:1581-1597` documents every section *except* `slots`, `qa_field_signals`, `stt_corrections`, `proactive_prompts`). Verified: 15 of 16 agents have `slots: []` and fall back to hardcoded heuristic slot sets (`state_manager.py:585-651`) — a non-real-estate agent whose signals include "bhk" would interrogate callers about home loans.

### 1.3 The stage machine can't express your example flow

- `StageDefinition` = `{id, order, goal, turns_until_advance}` only (`schema.py:136-146`). No per-stage data capture, no advancement gates ("advance when location+config+budget stored"), **no branching** (`recommend → other options | rejection | capture phone + WhatsApp promise | book visit` is inexpressible — advancement is strictly `stage_list[idx+1]`, `conversation.py:1303-1364`).
- Stage behavior is keyed to *legacy id literals*: slot-based early advance fires only when the id is literally `DISCOVERY` (`conversation.py:1340-1351`); bare-affirmation slot booking only in literal `INVITE_VISIT`/`CONFIRMATION` (`conversation.py:1846-1856`); meeting-slot derivation keys on literal ids (`state_manager.py:644`). Custom-id stages silently lose these behaviors.
- The completed-call gate is dead code: `_validate_booking_for_closing` has zero callers (`conversation.py:1544-1561`), `booking.confirmed` has no writer (`memory.py:49`).

### 1.4 A band of Pramukh/real-estate doctrine is hardcoded into every agent

`conversation.py:2393-2465` injects into **every** tenant's prompt: objection scripts with Pramukh's credentials (`2440-2444`), "NEVER volunteer pricing" (`2450`), "NEVER give more than 2 time options for site visit" (`2453, 2456`), "NEVER give property pitches until genuine interest" (`2459`), never-read-back-phone (`2451`, also `2358-2359`, `memory.py:80`). The 2-strike decline is hardcoded `>= 2` (`conversation.py:1068`) even though `decline_threshold` is parsed from YAML (`schema.py:397`). `llm_config.extra_never_rules`/`extra_always_rules` — the one per-agent doctrine mechanism — are **parsed but consumed nowhere** (grep-verified zero hits in `conversation.py`): dead config that the Copilot writes and agents like angelobot/kaapdfabrics carry, silently never reaching a call.

### 1.5 Edits are immediate, unversioned, unpreviewed, and race-prone

- `update_agent` is a genuine read→deep-merge→validate→write (good), but **lists replace whole** (`agent_copilot_api.py:394-401, 1288`): "swap stage 3 and 4" forces the LLM to resend the entire stage list from a context where old tool results were truncated to 900 chars — the documented silent-item-drop failure mode.
- No diff preview, no confirmation gate on edits, no versioning (nothing ever bumps `version`), no rollback, no audit trail beyond log lines.
- **Four** unlocked write paths to the same YAML: Builder PUT full-replace (`agent_builder_api.py:285-300`), PUT /raw (`358-372`), Copilot tools, and `yaml_filler.apply_fields` (`calling-agent/backend/app/documents/yaml_filler.py:116-184`) — no optimistic concurrency anywhere; a stale Builder form silently reverts Copilot edits.
- Writes are non-atomic (`schema.py:482-504` opens and dumps in place). A call arriving mid-write parse-fails → bridge passes `agent_def=None` → **`ConversationRouter` falls back to `get_pramukh_agent()`** (`conversation.py:558`): a tenant's caller is answered as Vishal Patel from Pramukh Group. This is the single worst latent bug in the platform and every schema extension enlarges its surface.

### 1.6 The frontend gives the interview no body

`CopilotChat.tsx` is a plain chat: no interview progress, no live spec preview, no diff view, no upload button, no stage editor (verified exhaustively). Creation results surface as prose + action chips only.

---

## 2. Design decisions (conflict resolutions — binding)

The audit's 12 layer analyses proposed overlapping homes for the same concepts. These are the **binding** resolutions; any implementation must follow this table.

| # | Concept | Canonical home (single source of truth) | Explicitly rejected alternatives |
|---|---------|------------------------------------------|----------------------------------|
| D1 | Ultimate call goal | `call_flow.call_goal: str` | `call_flow.objective` (E1 naming) |
| D2 | Stage branching | `call_flow.stages[].branches` | `call_flow.branches` (flow-level) |
| D3 | Objection playbook | `response_bank.objection_playbook: [{id, patterns[], answer{lang}, prompt_guidance}]` — feeds BOTH the deterministic shortcut and the prompt block | `knowledge_base.objection_playbook`, `guardrails.objection_playbook` |
| D4 | NEVER/ALWAYS rules | new `guardrails:` section (`never_rules`, `always_rules`, `use_builtin_rules`); `from_dict` **aliases** `llm_config.extra_*_rules` into it when guardrails is empty — one render path, no duplication | double-rendering both fields |
| D5 | Phone read-back | per-slot: `slots[].confirm_readback` (`""` \| `verbatim` \| `digit_groups_2`) on a `type: phone` slot. Prompt's never-read-back line flips **only** when such a slot exists | `guardrails.phone_readback`, `privacy.phone_readback` |
| D6 | Outbound scripts (steps 1–2 + wrong-number/wrong-person/busy) | new `outbound:` section owns ALL of it: `identity_confirm_line{lang}`, `good_time_check_line{lang}`, `pitch{lang}`, `wrong_person_response{lang}`, `busy_response{lang}`, `wrong_number_close{lang}`, `wrong_number_patterns[]`, `opening_greeting{lang}` (outbound variant), `direction_brief` | `response_bank.outbound_*` fields |
| D7 | WhatsApp media promise | `follow_up.whatsapp_promise {enabled, assets[], promise_line{lang}}`; the number comes from `brand.whatsapp_number` → fallback `brand.contact_phone` → `identity.contact` | `brand.whatsapp_media_promise`, `knowledge_base.media_promise` |
| D8 | Reply length | `personality.verbosity_max_sentences` (prompt rule) + `llm_config.max_response_length` (char truncation, finally wired). Two mechanisms, two fields, documented | `guardrails.max_sentences_per_reply` |
| D9 | Closing line | `response_bank.closing_responses{lang}` wired into the real farewell path (`conversation.py:1051-1058`). `graceful_close` keeps its current single job (STT-failure loop-breaker, `pipeline.py:920`) | mapping the interview's closing answer to `graceful_close` |
| D10 | Decline machine | one owner: wire `call_flow.decline_threshold` at `conversation.py:1068`; `callback_policy.counts_as_decline` decides strike counting; `escalation.handoff_message` replaces the farewell only when `escalation.enabled`. DND phrase list defaults `[]` (no live behavior change; Copilot writes it for new agents) | default-on DND builtin list (would change live Pramukh: "stop calling" is in its objection_patterns) |
| D11 | Revisions/rollback storage | Postgres table `agent_revisions` (PG16-in-docker is THE main DB — platform doctrine). YAML stays the runtime source of truth; the PG `agents` table stays an index (`shared/db/schema.sql:20-21`) | `.revisions/` JSON dirs inside `agents/<slug>/` |
| D12 | Copilot system prompt ownership | E1's dynamic `INTERVIEW_LAYERS` render **replaces** the static checklist region; every layer's compulsory questions land as `INTERVIEW_LAYERS` entries, never as ad-hoc prompt bullets. One owner for prompt token budget | 8 layers each appending static bullets |
| D13 | Runtime prompt-block ownership | ONE refactor extracts `conversation.py:2280-2482` into `app/voice/prompt_blocks.py` with ONE golden-prompt harness. All layer parameterizations land inside it afterwards, each gated by the same harness | six independent refactors of the same lines with three separate test files |
| D14 | Write choke point location | `calling-agent/backend/app/agent_definition/store.py` (importable from dashboard AND runtime web server — dashboard already imports `backend.app.agent_definition`; putting it in `dashboard/` would make calling-agent import dashboard code, breaking the runtime container) | `dashboard/agent_store.py` |
| D15 | Post-call/outcomes | `post_call:` section (success definition, outcome taxonomy, scoring, lead profile) | separate top-level `outcomes:` field |
| D16 | Structured catalog | `knowledge_base.catalog: [CatalogItem]` + `knowledge_base.match_rules`; `portfolio_prompt_text` remains as verbatim fallback when catalog is empty | replacing portfolio_prompt_text |

---

## 3. AgentSpec v2 — the schema

All changes to `calling-agent/backend/app/agent_definition/schema.py`. **Iron rules:**

1. Every new field/section is optional with a default that reproduces today's behavior **byte-for-byte** (defaults ledger in §12).
2. All parsing via `d.get(...)` with per-key defaults. **Replace the strict constructors** — `StageDefinition(**s)` (`schema.py:394`), `ResponseBank(**d)` (`:422`), `TTSVoiceConfig(**v)` (`:375`) — with explicit `.get()` field reads, so an unknown YAML key can never `TypeError` an agent load (today that failure answers the call as Pramukh).
3. Deploy order per phase: **runtime schema first** (both containers — dashboard AND runtime share the repo mount but import at startup; redeploy both), then engine, then Copilot may write the new keys, then Pramukh backfill (optional/explicit), then new-agent defaults.

### 3.1 New/extended sections

**`identity`** (extend): `role_line: str = ''` — full replacement for the hardcoded "warm, genuine, experienced {role_description}" framing (`conversation.py:2345`).

**`personality`** (extend): `traits: list[str] = []`, `energy_mirroring: bool = True`, `ai_disclosure: str = 'deflect'` (`deflect|admit|admit_if_pressed`; Pramukh doctrine = admit → set explicitly in its backfill, engine default `deflect` = today's hardcoded not-a-script line), `verbosity_max_sentences: int = 3`.

**`brand`** (NEW section):
```yaml
brand:
  spoken_company_name: ''        # fallback identity.company_name
  positioning_tagline: ''        # fallback identity.company_tagline
  credibility_markers: []        # ['30 years in business', '60+ completed projects', ...]
  trust_proofs: []               # ranked top 2-3, objection ammo; {trust_proofs} token in rebuttals
  regions_served: []             # fallback knowledge_base.locations
  contact_phone: ''              # fallback identity.contact
  whatsapp_number: ''            # fallback contact_phone
```
Resolver helpers on `AgentDefinition` (`effective_spoken_name()` etc.) — runtime reads only resolvers, guaranteeing old YAMLs behave identically. Fixes the dangling `· Contact:` render for empty-contact agents.

**`languages`** (extend): `greeting_language: str = ''` (fallback `default`), `bilingual_greeting: bool = False`. `TTSVoiceConfig` gains `gender: str = ''` (informational, warn-only validation). New module `agent_definition/voice_catalog.py`: `PLATFORM_LANGUAGES`, `SPEAKER_CATALOG` (speaker→gender/models), single source of truth re-exported to `schema.py:55` and `tts_sarvam.py`. `from_dict` warns-and-drops unsupported language codes and clamps `default` into `supported`.

**`call_flow`** (extend):
```yaml
call_flow:
  call_goal: ''                        # D1 — one-sentence mission, rendered as ━━ CALL MISSION ━━
  completed_when: {}                   # {required_slots: [...], require_appointment: bool}
  scheduling: {}                       # {offer_count: 2, style: exact_n|flexible, allow_caller_proposed: bool}
  callback_policy: {}                  # {enabled: false, capture_slot: callback_time, responses: {lang: str}, counts_as_decline: true}
  stages:
    - id: RECOMMEND
      order: 3
      goal: '...'
      turns_until_advance: 6
      role: pitch                      # greeting|confirm_person|discovery|pitch|recommend|scheduling|confirmation|closing|custom
      label: 'Recommend a property'
      collect: [location, bhk, budget] # slot names this stage captures
      advance_when: {}                 # {all_collect_filled: bool, slots: [...], min_turns: int, intent: str, appointment_captured: bool}
      branches: []                     # [{when_intent: negation|affirmation|question | when_slot: {name, equals}, goto: STAGE_ID, max_takes: 1, actions: [...]}]
```
`branches[].actions` (list of strings) is how step-7 side effects are recorded: `capture_phone`, `promise_whatsapp_media`, `book_visit`, `schedule_callback` — each action maps to a deterministic engine behavior (§4.4) and a `collected_data` flag, so promises are stored, not just spoken.

**`slots`** (extend `SlotDefinition`):
```yaml
slots:
  - name: budget
    priority: 40
    required: true
    label: Budget
    question: {en: '...', hi: '...', gu: '...'}
    type: money                  # ''(legacy)|text|number|money|enum|boolean|phone|date|time|location
    enum_values: []              # for type: enum
    enum_synonyms: {}            # canonical -> [caller phrasings incl. hi/gu]
    extract_patterns: []         # optional custom regexes (precompiled at load, complexity-capped)
    depends_on: [location, bhk]  # never ASKED before these are filled (volunteered values still stored)
    merge_policy: free           # free|correction_gated|confirm
    confirm_readback: ''         # ''|verbatim|digit_groups_2   (D5)
    max_asks: 0                  # 0 = unlimited (legacy); Copilot writes 2 for new agents
    reask_question: {}           # lang -> second-attempt phrasing
    summary_known: ''            # '{value}' templates for the lead summary
    summary_unknown: ''
```

**`knowledge_base`** (extend):
```yaml
knowledge_base:
  catalog:                       # D16 — structured, per-item
    - id: sahaj
      name: Sahaj
      location: Vapi             # doubles as variant
      configurations: [1BHK, 2BHK]
      price_range: '₹22-29L'
      availability: completed
      positioning_tag: value
      pitch_points: []
      aliases: [sahaj]
      facts: {}
  match_rules: []                # [{when: {budget: '<=40L', location: Vapi}, recommend: [sahaj, aura], reason: '...'}]
  pricing_policy: {}             # {mode: on_request|state_freely|defer, defer_line: {lang}}  — absent = on_request (today)
  fact_policy: {}                # {state_freely: [...], defer: [...], defer_line: {lang}}
  product_lock: {}               # {enabled: true, allow_switch: false, switch_on_request: false} — absent = today's set-once lock
  objection_playbook: []         # REMOVED — lives in response_bank (D3)
  stt_corrections:
    use_builtin_tables: true     # false disables the unconditional Pramukh tables in post_processor.py:301-411
```
New pure-function module `agent_definition/catalog.py`: `render_catalog_prompt(kb)` (falls back to `portfolio_prompt_text` verbatim when catalog empty), `derive_known_projects(kb)` (auto-extends the extractor map from catalog aliases), `evaluate_match_rules(kb, slots)` (deterministic recommendation candidates injected below CURRENT GOAL in the recommend stage).

**`response_bank`** (extend): `objection_playbook` (D3 shape), `closing_responses{lang}` (D9), `canned_phrases: [{patterns[], responses{lang}}]`, `ai_disclosure_responses{lang}`, `enable_greeting_shortcut: bool = False`. Fix the `['en']` KeyError traps (`conversation.py:1072, 1098, 1120, 2143, 2697`) with `.get(lang) or .get('en') or first-value or generated-fallback` chains.

**`outbound`** (NEW section — D6): fields as in the D6 row. `generate_opening` (`conversation.py:730`) prefers `outbound.opening_greeting[lang]` when direction is outbound and the field is non-empty; the direction prompt block (`conversation.py:2282-2313`) substitutes the per-agent lines when present, emits today's exact prose when absent.

**`guardrails`** (NEW section — D4):
```yaml
guardrails:
  use_builtin_rules: true        # true + all defaults = today's FIRM RULES byte-for-byte
  never_rules: []                # rendered '✗ NEVER <rule>'  (aliases llm_config.extra_never_rules)
  always_rules: []               # rendered '✓ <rule>'
  honesty: {unknown_answer_policy: '', forbidden_fabrications: [prices, dates]}
  compliance: {disclaimers: [], consent_line: '', dnd_phrases: [], restricted_topics: []}   # dnd_phrases default [] — D10
  escalation: {enabled: false, action: offer_callback, triggers: [], handoff_message: {}}
```

**`follow_up`** (NEW section — D7): `whatsapp_promise {enabled: false, assets: [], promise_line: {lang}}`, `callback {enabled: false, default_delay_hours: 24}`.

**`calendar`** (extend): `day_synonyms: dict[str, list[str]] = {}` (canonical slot-day label → caller phrases, consulted before the hardcoded Today/Tomorrow map at `conversation.py:1793-1803`), `timezone: str = 'Asia/Kolkata'`, `booking_confirmation_line: dict[lang,str] = {}`.

**`post_call`** (NEW section — D15):
```yaml
post_call:
  success_definition: {mandatory_slots: [], success_outcome: '', gate_closing: false}
  outcomes: []                   # [{id, label, kind: success|progress|failure, description}]
  scoring: {enabled: false, auto_run: false, criteria_prompt: '', score_labels: [hot, warm, cold]}
  lead_profile: {summary_template: {}, include_slots: []}
```

**Top-level** (extend): `revision: int = 0`, `updated_at: str = ''`, `updated_by: str = ''` (server-managed — the Copilot normalizer strips them from LLM input; added to `_TOP_LEVEL_KEYS` in the same commit that starts stamping them, or every Copilot edit resets the audit trail).

### 3.2 Sanitizer registration (same commit as each schema change)

Every new section must be registered in `dashboard/agent_copilot_api.py`: `_SECTION_DATACLASSES`/`_TOP_LEVEL_KEYS` (`:416-426`), `_default_definition` empty defaults, `_normalize_definition` shape coercions with **value validation** for the new enum-ish fields (coerce invalid `merge_policy`/`type`/`confirm_readback`/`ai_disclosure` strings to their defaults; int-coerce `priority`/`max_asks`; validate `depends_on` names against the slot set and drop cycles; cap `extract_patterns` length/complexity) — otherwise a hallucinated value aborts saves that succeed today, or the deep-merge silently strips Builder-written sections.

---

## 4. Runtime engine changes

### 4.1 Phase-0 safety substrate (before anything else)

1. **Golden-prompt harness** (`calling-agent/backend/tests/test_prompt_golden.py`) — THE shared regression gate for every phase: load `agents/pramukh/agent.yaml`, build `_build_system_prompt()` for a fixed `CallState` at each of its 6 stages × {hi,en,gu} × {inbound,outbound}, snapshot as fixtures. Also: `AgentDefinition.from_yaml` sweep over **all** agent dirs (no TypeError/KeyError), and a scripted stage-advance trace test (thresholds 1/8/6/99/99/99). Every subsequent PR must pass it unchanged (until a deliberate, reviewed fixture update).
2. **Atomic writes**: convert `to_yaml` (`schema.py:482-504`) to tmp-file + `os.replace` (same-filesystem safe on the bind mount; watchfiles treats replace as a change so hot reload keeps working — verify on the ext4 host mount). This alone closes the mid-write corruption window.
3. **Kill the Pramukh impersonation fallback** (`conversation.py:558`): `ConversationRouter` requires an explicit `agent_def`. `backend/run.py` passes `get_pramukh_agent()` explicitly (CLI behavior unchanged). The bridge, on load failure, substitutes a built-in `NEUTRAL_FALLBACK_AGENT` (generic assistant persona, no brand, apologetic close: "I'm sorry, we're having a technical issue — please call back shortly") and logs `[BRIDGE] agent load FAILED for <slug>` at ERROR. A tenant's caller must never again be answered as Vishal Patel.
4. **Loud load-failure visibility**: grep-able error + counter; surface last-load-error per agent in `GET /api/agents` so the dashboard can flag a broken agent instead of masking it.

### 4.2 Prompt-block refactor (D13 — one PR, one owner)

Extract `conversation.py:2280-2482` (direction block, BRAND, portfolio, calendar, guardrails/FIRM RULES, objection handling, conversation-style, state block splice) into `app/voice/prompt_blocks.py` with pure functions taking `(agent_def, call_state, state_mgr, lang)`. Byte-identity for Pramukh enforced by the Phase-0 harness. **All** parameterizations below then land inside this module:

- `role_line` / AI-disclosure line / `verbosity_max_sentences` / `energy_mirroring` gate (L1) — defaults render today's exact strings.
- `_build_brand_block()` (L2) — default = the exact current two lines; structured render when `brand` present; `{trust_proofs}` substitution.
- Catalog render + MATCH RULES + pricing/fact policy switches (L3) — `pricing_policy` absent = today's exact `:2450` string.
- CALL MISSION block when `call_goal` non-empty (L5); scheduling lines from `scheduling` config rendering today's exact "TWO options" strings on defaults.
- Objection block from `response_bank.objection_playbook.prompt_guidance` when non-empty, else the current 4 lines verbatim (D3).
- Guardrails: `use_builtin_rules` + appended never/always rules; COMPLIANCE/ESCALATION sections only when configured (L8).
- Outbound direction block substitution (D6); WHY-YOU-ARE-CALLING-BACK block when callback context present (L9).

**Pramukh backfill note (from the critic):** when moving the objection block into Pramukh's YAML, the `prompt_guidance` strings must be the **literal current lines** ("…30 years, zero stalls, RERA.") — never re-rendered from `trust_proofs`, which produces a different string and breaks byte-identity.

### 4.3 Stage machine (L5)

1. **Role shim**: `_derive_stage_role(stage_id)` maps legacy ids → roles (GREETING→greeting, DISCOVERY→discovery, PROJECT_MATCH→pitch, INVITE_VISIT→scheduling, CONFIRMATION→confirmation, CLOSING→closing, OPENING→confirm_person, PITCH→pitch, HANDLE_QUESTIONS→pitch, ASK→scheduling). `self._stage_role()` = explicit `StageDefinition.role` or derived. Replace the id-literal gates with role checks, keeping literals as fallback: early-advance (`conversation.py:1340`), affirmation booking (`:1851`), discovery prompting (`:2372-2376`), meeting-slot derivation (`state_manager.py:644`).
2. **Declarative gates**: `_maybe_advance_stage` evaluates `advance_when` when non-empty (min_turns AND-gate; `all_collect_filled` over the stage's `collect` slots incl. pending-excluded; named `slots`; `intent`; `appointment_captured`). Empty = today's exact turn-count behavior.
3. **Branches**: new `_maybe_branch(intent)` called just before `_maybe_advance_stage` (`conversation.py:1177`): match `when_intent`/`when_slot`, validate `goto` exists (load-time check in registry too), cap with `max_takes` per-call counters (loop prevention), log `[STAGE] → X (branch)`. Empty lists = no-op.
4. **Branch actions** (the step-7 requirement): on branch take, execute `actions`: `capture_phone` → mark the phone slot as the next ask with read-back per its `confirm_readback`; `promise_whatsapp_media` → set `whatsapp_promised=true` in state (lands in `calls.collected_data` via the existing bridge snapshot) + render the `follow_up.whatsapp_promise.promise_line`; `book_visit` → route to the scheduling stage machinery; `schedule_callback` → callback capture path (below). New dashboard endpoint `GET /api/followups/whatsapp-pending` lists calls with `collected_data.whatsapp_promised=true` — the fulfillment queue's first consumer.
5. **Completed-call gate + outcome**: wire `completed_when` (gate advancement into the final stage only when `gate_closing: true`; default off = today). When passed, set `booking.confirmed=True` (giving the dead `:1318` path its first, intentional writer) and record `call_outcome` on the bridge snapshot.
6. **Callback policy**: in the objection shortcut (`conversation.py:1061-1083`), when `callback_policy.enabled` and utterance matches deferral+time, parse a time preference **without** booking a calendar slot (never `_record_calendar_booking`), store via `set_externally('callback_time', …)`, reply from `callback_policy.responses[lang]`, skip the strike when `counts_as_decline: false`. Wire `decline_threshold` at `:1068` (all 16 agents carry 2 — verified — so zero change).
7. **Scheduling config**: render offer-count/style from `call_flow.scheduling` (defaults = today's exact strings); `day_synonyms` + `timezone` generalize `_extract_time_preference` (`:1748-1858`) so "Monday 4 PM"-style slot labels finally match.

### 4.4 State manager (L4)

1. Mirror all new `SlotDefinition` fields onto `SlotSpec`; `build_state_manager` copies **all** fields (today it drops `summary_known/unknown`, `state_manager.py:632-636`). Precompile `extract_patterns`/`enum_synonyms` in `__init__` (<1 ms hot path preserved).
2. **Type-driven extraction** runs first for slots with non-empty `type` (enum matching latest-mention-wins like `_match_location`; money generalizing `_BUDGET_RE`; number; boolean; phone reusing `memory.py`'s pattern; date/time defer to LLM gap-filler). Empty `type` = legacy name-keyed dispatch **byte-identical** (Pramukh's slots carry no `type`).
3. **Dependency gates**: `detect_missing_slots` computes `blocked` (required, unfilled, `depends_on` unmet) excluded from `missing` — never asked early, still filled opportunistically when volunteered. Cycle validation at sanitize/load time; blocked counts as incomplete for stage advancement.
4. **Merge policies**: `free` = current latest-wins; `correction_gated` = overwrite non-None only with a correction marker (generalized `_NEGATED_VALUE_RE` + actually/nahi/change lexicon), else keep + audit the rejected overwrite; `confirm` = route through `mark_pending` with a generalized confirmation loop in `conversation.py` (affirm→`set_externally`, negate→`discard_pending`, N-turn auto-accept). **The literal slot `name` is hard-excluded** — the existing `_name_pending` machinery stays untouched.
5. **Read-back**: formatter per `confirm_readback` (`digit_groups_2` → "98 76 54 32 10"); `build_prompt_context` emits the read-back instruction for filled-but-unconfirmed slots. Phone becomes expressible as a first-class optional slot; the never-read-back prompt line stays verbatim unless the agent defines a phone slot with `confirm_readback` (D5). `CallState.customer_phone` write-through syncs with the phone slot so the two stores can't diverge.
6. **Ask fatigue**: `asked_count` incremented at question-selection time; at `max_asks` (when >0) the slot moves to `skipped` — excluded from `missing`, prompt says "stop asking; proceed", counts as satisfied for advancement. Default 0 = unlimited (Pramukh unchanged); Copilot writes 2 for new agents.
7. Keep `_REAL_ESTATE_SLOTS`/`_MEETING_SLOTS` fallbacks frozen (load-bearing for the 15 existing slotless agents); deprecate only after all agents carry explicit slots. `to_dict()` gains only additive keys (`skipped`, `blocked`) — never rename `filled/missing/pending` (snapshot consumers).

### 4.5 Voice/language (L6) and scripts (L7)

- Wire `languages.default`/`greeting_language` into `ConversationRouter` construction (bridge `:273` and `run.py`) — **flagged live change** for en-default agents (greeting synthesis flips hi-IN→en-IN, the intended fix): audit all agents' `default` first, per-agent sign-off before deploy.
- Per-language voice on mid-call switch: `SarvamTTS.set_voice(model, speaker, pace)` called at the pipeline's `set_language` site with the agent's `tts_voices[lang]`; absent = today. Fix the preview-path pace drop in `run.py:99-110`.
- Wrong-number close, objection playbook deterministic matching, canned phrases, closing_responses wiring, outbound script injection — all gated on field presence (absent = today's exact behavior). Greeting shortcut stays behind `enable_greeting_shortcut: false` (Pramukh HAS localized greeting entries; a default-on shortcut would change its live hi/gu "hello" turns).
- `max_response_length` wiring: pass `llm_config.max_response_length` to `_validate_response`; bump the `from_dict` default 150→400 **and** backfill Copilot-written YAMLs carrying 300 → 400 in the same change (zero live truncation change; the 300s were never in effect).
- `identity_responses` KeyError fix + generated fallback (Copilot default is `{}` — today "who is calling?" crashes copilot-built agents).

### 4.6 Post-call (L10) and telephony (L9)

- `calls` table migrations: `outcome TEXT`, `outcome_kind TEXT` (append-only MIGRATIONS in `shared/db/database.py`). Bridge computes a deterministic outcome at teardown only when `post_call.outcomes` is non-empty (else NULL = today, including Pramukh until backfilled).
- Agent-aware scoring: `_analyze_transcript` loads the call's agent spec; when `post_call.scoring` is configured, inject identity/stage goals/outcome taxonomy/criteria/labels + `collected_data` (its first reader); else the existing prompt byte-for-byte. `auto_run` default false = zero new LLM spend.
- Overview KPI change (`server.py:453-456`) is **explicitly flagged** as a semantics change (disposition OR outcome_kind='success'); ship behind review, deduped per contact.
- **Callback auto-dial fix**: `_dial_automation_entry` (`server.py:3300-3348`) must resolve identity from the automation row's `owner` and prefer the original call's `agent_slug` over the globally-active agent; add `call_links.context_note` (nullable) carrying call_aim/past-context into a "WHY YOU ARE CALLING BACK" prompt block. Today the callback is fulfilled by the wrong agent with amnesia.
- Gate the unconditional Pramukh STT tables (`post_processor.py:301-411`) behind `stt_corrections.use_builtin_tables` (default true); Copilot sets false for new agents and auto-generates `project_name_corrections` from catalog aliases.

---

## 5. Surgical Edit Engine

### 5.1 Write choke point — `agent_definition/store.py` (D14)

`read_agent(slug) -> (dict, rev_sha)` and `write_agent(slug, new_dict, actor, base_rev=None, reason='') `:
(a) read current + sha256; (b) `base_rev` mismatch → `ConflictError` (HTTP 409 with current definition); (c) snapshot the **pre-write YAML text** into Postgres `agent_revisions` (new table: `id, slug, owner, actor, reason, sha256, yaml_text, created_at`; retention 50/slug) — D11; (d) stamp `revision+=1`, `updated_at`, `updated_by`; (e) validate via `from_dict`; (f) atomic tmp+`os.replace` write; (g) `agent_registry.invalidate(slug)`. **All four writers route through it**: Builder PUT (+`If-Match`), PUT /raw, all Copilot tools, `yaml_filler.apply_fields`. Owner checks: `write_agent` takes the resolved owner and re-verifies `_slug_owner` — every new endpoint below inherits the same guard.

### 5.2 Diff engine — `agent_definition/diff.py`

`compute_diff(old, new) -> [{path, op, old, new}]` with list-index paths (`call_flow.stages[3].goal`); `render_human(diff)`; `apply_ops(definition, ops)` executing `[{op: set|append|remove_at|move, path, value?, to?}]` server-side.

### 5.3 Two-phase Copilot edits (additive tools)

- `propose_update(name, changes|ops, reason)` → builds candidate, normalizes/validates, returns human diff **without writing**; pending change stored in the session (persisted).
- `apply_update(change_id)` → server-guarded like the existing create/activate gates: allowed only when proposed in a prior turn OR the triggering user message contains an explicit imperative/confirmation. Commits via `write_agent(base_rev=stored)` so an interleaved Builder write 409s instead of being clobbered. `discard_update(change_id)`.
- `replace_mentions` gains `dry_run` (default true **only together with** the prompt update — flipping it alone breaks the current one-shot flow): returns per-path match list + count; commit on confirmation.
- **Stage ops** (the critic's "planned by none" gap — binding here): `update_stage(name, stage_id, fields)`, `add_stage(name, stage, after_id)`, `remove_stage(name, stage_id)`, `reorder_stages(name, ordered_ids)` — each mutates only the matching stage dict against the **on-disk** YAML (never rebuilt from the LLM's memory), renumbers `order` ints on reorder, returns a scoped diff. Mirror as `PATCH /api/agents/{name}/ops` for the Builder UI.
- **Slot ops** analogous: `update_slot`, `add_slot`, `remove_slot` (same path-op machinery).
- Keep `update_agent` as an immediate-apply alias during transition (in-flight sessions + battle-tested truthfulness prompt assume same-turn results); switch the prompt to propose/apply in one deploy.

### 5.4 Provable scoping (the "nothing else changed" guarantee)

After `os.replace`, `write_agent` re-reads and asserts every changed path ∈ (requested paths/ops ∪ server-managed: `revision, updated_at, updated_by`). Unexpected collateral → auto-restore snapshot + loud error naming the paths. To make this pass: in the Copilot **update** path, run `_normalize_definition` on the *changes subtree* only; the whole-doc passes (`_enforce_invariants`, placeholder canonicalization on untouched fields, calendar coercion) become validation-time **checks that fail the proposal** instead of silent mutations. (Full normalization stays ON for `create`.)

### 5.5 Rollback + revision surfaces

`list_revisions(name)` / `rollback_agent(name, rev_id)` Copilot tools + `GET /api/agents/{name}/revisions`, `POST /api/agents/{name}/rollback/{rev_id}` (owner-scoped). Rollback validates the snapshot, writes via `write_agent` (itself snapshotting — rollback is undoable). Every mutating result gains `{effective: 'next_call', revision: N}` and the Copilot quotes it ("applies from the next call — revision 7").

### 5.6 Concurrency plumbing (frontend)

`agents.ts` carries `rev` from GET, sends `base_rev` on PUT (structured and raw tabs); 409 → "This agent changed since you loaded it (edited by Copilot)" reload-and-reapply UX. Absent `base_rev` = today's last-write-wins (old clients keep working).

---

## 6. Interview Engine (Copilot v2 core)

### 6.1 Data-driven blueprint — `dashboard/copilot_interview.py`

`InterviewLayer(id, title, compulsory, spec_paths, question_hint, followup_hint, allows_upload, done(draft) -> (bool, missing))` and `INTERVIEW_LAYERS` (~12 layers): `business_identity`, `call_objective` (→ `call_direction`, `call_flow.call_goal` — D1 naming), `persona_voice`, `languages`, `brand_trust`, `call_flow` (stages incl. outbound confirm-person/good-time openers), `data_capture` (**slots — the layer the current prompt hides**), `knowledge_catalog` (satisfiable via upload), `objections_branching` (→ `response_bank.objection_playbook` + `stages[].branches` — D2/D3 paths), `scheduling_followup` (calendar, `follow_up.whatsapp_promise`, phone read-back), `guardrails_compliance`, `greeting_close` (→ `opening_greeting` + `closing_responses` — D9 path). `interview_progress(draft, skipped)` is a pure function — completeness is **server-computed, never LLM-trusted**.

**Blueprint `done()` checks reference the exact AgentSpec v2 paths from §3** (the critic's E1↔L5 path-mismatch is resolved by D1/D2: `call_flow.call_goal`, `stages[].branches`).

**Per-language coverage is enforced**: `data_capture.done()` requires a `question` entry for **every** code in `languages.supported` (reject en-only questions on a hi/gu agent); `greeting_close`/`objections` likewise check per-language response coverage.

### 6.2 Session state + tools

- Session gains `interview: {draft: {}, skipped: [], provenance: {}, phase: idle|interviewing|built}` (lazy `setdefault` — old session files unaffected).
- New tool **`update_draft(changes)`**: normalize → deep-merge into the session draft → return `{notes, quality, progress, next_layer_id + question_hint}`. Registered non-mutating (never trips the truthfulness backstop or `agents_changed`). **Provenance**: every path written by `update_draft` in a turn is stamped `confirmed` (came from user words) vs `derived` (proposed by the Copilot and accepted) vs later `defaulted` at create — stored in `interview.provenance` so future edits know what the owner actually said.
- New tool **`skip_layer(layer_id, user_words)`** — the only per-layer bypass, auditable with the user's own quoted words.
- **Domain-adaptive proposals** (the "building process, not a dump" requirement): after `business_identity`+`call_objective` complete, the prompt instructs the Copilot to *propose* a tailored slot set, stage skeleton, and likely objections for that business type ("For a property business I suggest collecting: location, configuration, budget… shall I use these?") — accepted proposals land via `update_draft` with `derived` provenance; the user's confirmation upgrades them.
- **Hardened create gate** (extends `agent_copilot_api.py:1413-1418`): for new slugs, compute `interview_progress`; any compulsory layer neither complete nor skipped → refuse, naming the missing layers and next questions. Dump-and-done becomes mechanically impossible; "just build it" becomes explicit per-layer `skip_layer` calls.
- **Create consumes the draft**: `merged = deep_merge(default_skeleton, session_draft, tool_call_definition)` — everything captured during the interview lands in the YAML even if the final tool call is thin and chat history was trimmed (fixes the 48-message loss).
- **Quality moves pre-create**: `update_draft` results include `_quality_warnings` on the merged preview, so thin content is challenged during the interview, not after the write.

### 6.3 Dynamic system prompt (D12)

`_system_prompt(session)`: when interviewing, replace the static checklist region (`:1559-1566`) with a compact INTERVIEW STATE render (~350 tokens hard cap: one line per layer ✓/✗/skipped + "NEXT: ask exactly this ONE question: …" + "on an info dump: mine EVERYTHING into update_draft first, then ask the next missing layer" + "offer a document upload at the knowledge layer"). The DEFINITION FIELDS docs get **one consolidated rewrite** covering all AgentSpec v2 sections (including, finally, `slots`). One owner tracks total prompt size (Groq TPM shared with live calls).

### 6.4 Document upload — a real mechanism, not an offer

(a) `POST /api/copilot/upload` (multipart, owner-scoped via the existing tenancy dep) → `dashboard/copilot_uploads/<sid>/<doc_id>`; (b) chat body gains optional `attachments: [doc_id]`; (c) new tool `ingest_document(doc_id)` reusing the extract-document logic factored out of `agent_builder_api.py:398-520`, merging extracted facts/catalog items into the draft via the `update_draft` path; (d) `knowledge_catalog.done()` accepts ≥80-char portfolio text OR one ingested doc; (e) frontend composer gets an attach button (§7). `yaml_filler`'s field spec extends to `brand.*`, `knowledge_base.catalog`, `pricing_policy`, `match_rules`, `guardrails.*` so brochures can fill Layers 2/3/8.

### 6.5 Master compulsory question list (the AGENT_PROFILE, as an interview)

Aggregated question → spec-field map (the interview asks these one at a time, in language the owner speaks, skipping answered ones; full per-layer hints live in `INTERVIEW_LAYERS`):

| Layer | Compulsory questions | Lands in |
|---|---|---|
| Business | What does the business do; what do customers call about? | `identity.company_name`, `identity.role_description`, seeds knowledge |
| Objective | The ONE thing a successful call accomplishes? Inbound or outbound? | `call_flow.call_goal`, `call_direction` |
| Persona | Agent name; warm/formal; energetic/calm; mirror energy?; reply length; admit being AI or deflect?; vocabulary register per language | `identity.agent_name`, `personality.*`, `languages.register_notes` |
| Brand | Exact spoken company name; credibility markers; top 2-3 trust proofs; regions; contact + WhatsApp numbers; tagline | `brand.*` |
| Languages/Voice | Which of hi/en/gu (expectation-setter: adding a 4th is engineering); opening language; male/female + specific speaker; pace; bilingual greeting? | `languages.*`, `tts_voices` |
| Call flow | Walk the ideal call step by step (echoed back as named, reorderable stages); what must be true to move past each stage; interest-building before the ask; when is the call COMPLETE? | `call_flow.stages[]` (role/goal/advance_when), `completed_when` |
| Data capture | What must be collected from every caller; order + dependencies (budget after city+config?); type/options per field with hi/gu phrasings; exact question wording per language; re-ask wording; change-mind policy per field; which values read back (phone in 2-digit groups?); how many asks before giving up | `slots[]` — every field of §3.1 |
| Catalog | Full product list — per item: name, location/variant, configurations, price range, availability, positioning; need→product match rules; volunteer prices or on request or defer; state-freely vs defer facts; lock onto one product once interest shown?; **upload brochures/price sheets** | `knowledge_base.catalog`, `match_rules`, `pricing_policy`, `fact_policy`, `product_lock` (+ `ingest_document`) |
| Objections & branches | Top 5 pushbacks + best answer to each (one at a time); after the recommendation — what happens on (a) other options (b) rejection (c) interest? Rejection: capture number for WhatsApp follow-up or close politely? | `response_bank.objection_playbook`, `stages[].branches` (+ actions) |
| Scheduling/follow-up | Bookable slots; exactly-two-options or flexible; callback policy ("call me tomorrow" — capture time + auto-dial?); WhatsApp media promise + assets | `calendar.*`, `call_flow.scheduling`, `callback_policy`, `follow_up.whatsapp_promise` |
| Guardrails | What must the agent NEVER do / ALWAYS do; sector disclaimers (RERA etc.); outbound consent/DND phrases; escalate-to-human when?; what to say when it doesn't know | `guardrails.*` |
| Scripts | Exact opening line (drafted, incl. `{contact_name}` for outbound); identity handshake + good-time wording (defaults offered, editable); wrong-number close; closing line; canned trivial-phrase responses | `response_bank.opening_greeting`, `outbound.*`, `closing_responses`, `canned_phrases` |
| Post-call | Which fields make a call "successful"; outcome taxonomy; scoring criteria + labels; lead summary shape | `post_call.*` |

---

## 7. Frontend (CopilotChat v2)

1. **Interview progress rail**: renders `interview.progress` from the chat response — per-layer ✓/✗/skipped chips, current question highlighted. (API contract additive; old bundles ignore it.)
2. **Live spec preview pane**: normalized draft rendered as friendly sections (who it is / how it speaks / call flow / what it collects / what it knows / rules), updating per turn; after build, shows the real agent with revision number.
3. **Diff cards**: `propose_update` results render as before→after cards with Apply/Discard buttons (wired to `apply_update`/`discard_update`) — confirmation becomes a click, not a typed "yes".
4. **Upload button** in the composer → `/api/copilot/upload` → attaches `doc_id` to the next message.
5. **Stage editor** (Build page): visual list of stages with drag-reorder/insert/remove/edit calling `PATCH /api/agents/{name}/ops` — the same surgical ops the Copilot uses, so both paths converge on `store.py`.
6. **Revision history panel** (Build page) + conflict UX on 409.

---

## 8. The reference flow — your 7-step outbound example in AgentSpec v2

Proof the schema expresses the requirement (abridged; this also becomes the Copilot's outbound skeleton template):

```yaml
call_direction: outbound
call_flow:
  call_goal: Qualify the contact and book a confirmed site visit; on rejection, capture WhatsApp follow-up.
  scheduling: {offer_count: 2, style: exact_n}
  callback_policy: {enabled: true, capture_slot: callback_time, counts_as_decline: false}
  completed_when: {required_slots: [name, phone], require_appointment: false}
  stages:
    - {id: OPENING, order: 0, role: confirm_person, goal: 'Confirm you reached {contact_name}; ask if it is a good time.',
       advance_when: {intent: affirmation, min_turns: 1}}          # steps 1-2 — editable lines live in outbound:
    - {id: DISCOVER_LOCATION, order: 1, role: discovery, collect: [location],
       goal: 'Ask their preferred location. ONE question.', advance_when: {all_collect_filled: true}}
    - {id: DISCOVER_CONFIG, order: 2, role: discovery, collect: [bhk],
       goal: 'Ask the configuration they need (1/2/3/4 BHK).', advance_when: {all_collect_filled: true}}
    - {id: DISCOVER_BUDGET, order: 3, role: discovery, collect: [budget],
       goal: 'Ask their comfortable budget, softly.', advance_when: {all_collect_filled: true}}
    - {id: RECOMMEND, order: 4, role: recommend,
       goal: 'Recommend exactly ONE property matching stored location+config+budget (MATCH RULES). Two personal reasons.',
       branches:
         - {when_intent: question,    goto: ALTERNATIVES,       max_takes: 2}   # 7a other options
         - {when_intent: negation,    goto: HANDLE_REJECTION,   max_takes: 1}   # 7b rejection
         - {when_intent: affirmation, goto: NEXT_STEP,          max_takes: 1}}  # 7c interest
    - {id: ALTERNATIVES, order: 5, role: pitch, goal: 'Offer ONE different matching property.',
       branches: [{when_intent: affirmation, goto: NEXT_STEP}, {when_intent: negation, goto: HANDLE_REJECTION}]}
    - {id: HANDLE_REJECTION, order: 6, role: pitch,
       goal: 'Acknowledge warmly; one trust proof; offer WhatsApp details for later.',
       branches: [{when_intent: affirmation, goto: NEXT_STEP,
                   actions: [capture_phone, promise_whatsapp_media]},
                  {when_intent: negation, goto: CLOSING}]}
    - {id: NEXT_STEP, order: 7, role: scheduling,
       goal: 'Capture + read back phone; promise WhatsApp photos/videos of the chosen project; offer exactly TWO visit slots.',
       advance_when: {slots: [phone]}, branches: [{when_intent: affirmation, goto: CLOSING, actions: [book_visit]}]}
    - {id: CLOSING, order: 8, role: closing, goal: 'Warm close by name; restate what happens next.', turns_until_advance: 99}
outbound:
  identity_confirm_line: {hi: 'नमस्ते, क्या मेरी बात {contact_name} जी से हो रही है?'}
  good_time_check_line:  {hi: 'क्या अभी दो मिनट बात करना ठीक रहेगा?'}
  wrong_person_response: {hi: 'माफ़ कीजिए! क्या {contact_name} जी से बात हो सकती है?'}
  wrong_number_close:    {hi: 'माफ़ कीजिए, लगता है गलत नंबर लग गया। आपका दिन शुभ रहे!'}
slots:
  - {name: location, type: enum, enum_values: [Surat, Vapi, Silvassa], required: true, priority: 20, question: {...hi/en/gu...}}
  - {name: bhk, type: enum, enum_values: [1BHK, 2BHK, 3BHK, 4BHK], required: true, priority: 30, depends_on: [location], question: {...}}
  - {name: budget, type: money, required: true, priority: 40, depends_on: [location, bhk], merge_policy: correction_gated, max_asks: 2, question: {...}}
  - {name: phone, type: phone, required: false, priority: 90, confirm_readback: digit_groups_2, merge_policy: confirm, question: {...}}
follow_up:
  whatsapp_promise: {enabled: true, assets: [photos, videos], promise_line: {hi: 'मैं आपको WhatsApp पर photos और videos भेज दूँगा।'}}
```

---

## 9. Multi-tenant + storage doctrine (explicit, per the critic)

- **YAML stays the runtime source of truth** (registry + watchfiles hot reload + shared bind mount). The PG `agents` table stays an index (`shared/db/schema.sql:20-21`). **Revisions live in PG** (`agent_revisions`, §5.1). New PG migrations are append-only in `shared/db/database.py`. Never touch the `lescall-pg` container.
- **Owner guards on every new surface**: `/revisions`, `/rollback`, `/ops`, `/outcomes`, `/api/copilot/upload`, `followups/whatsapp-pending` all resolve owner via the existing deps and assert `_slug_owner`/`_visible_slugs`; `write_agent`'s `actor` is `{owner}:{copilot|builder|docfill|rollback}`, not free text.
- Sarvam key pool doctrine untouched (file-based, admin-only).

---

## 10. What we deliberately do NOT change

- The dead-config **activations** that would alter live tenant prompts ship **gated with a per-agent audit**: wiring `extra_*_rules` activates text that angelobot/kaapdfabrics/every Copilot agent carry but have never run — audit each YAML and get sign-off before the aliasing lands (D4 makes the render single-path; the audit decides per agent whether the text stays).
- Pramukh's `pronouns: she`/male-voice mismatch: flagged to you as a separate opt-in fix, never bundled.
- `calendar.type: webhook` (real calendar integration) and actual WhatsApp **sending** are out of scope; the promise/fulfillment queue (§4.4) is the integration point for later.
- The `_LANG_STRINGS` hi/en/gu universe stays: adding a 4th language remains an engineering task; the interview sets that expectation verbatim.

---

## 11. Rollout phases (each gated by the golden harness)

| Phase | Content | Gate before next |
|---|---|---|
| **P0** | Golden-prompt harness + all-agent load sweep + stage-trace test; atomic `to_yaml`; kill Pramukh fallback (neutral fallback agent); load-failure visibility | Harness green on unmodified tree; test call to Pramukh DID |
| **P1** | AgentSpec v2 schema (all sections, `.get()` parsing, constructors de-strictified) + sanitizer registration + `revision/updated_at/updated_by`; deploy runtime+dashboard containers together | Load sweep green; Pramukh `from_yaml` before/after asdict-diff = 3 new defaults only |
| **P2** | `prompt_blocks.py` refactor (D13) — byte-identity, no parameterization yet | Harness byte-identical |
| **P3** | Engine wiring behind defaults: role shim, gates, branches+actions, playbook, scheduling, callback, guardrails, brand, catalog, outbound, calendar, voice-switch, state-manager v2, decline wiring, post_call, KeyError fixes | Harness byte-identical; scripted stage traces identical; 7-step fixture agent passes its own trace test |
| **P4** | Edit engine: `store.py` + `agent_revisions` PG migration + diff engine + propose/apply/stage-ops/slot-ops/rollback tools + If-Match on Builder routes + `yaml_filler` reroute | Concurrency test (interleaved writes 409); scoped-diff proof test; rollback round-trip |
| **P5** | Interview engine: blueprint + `update_draft`/`skip_layer`/`ingest_document` + hardened create gate + draft-consuming create + dynamic prompt + upload endpoint | Scripted interview e2e (dump mid-way, one skip, one upload, create refused until layers close); old session replay OK |
| **P6** | Frontend: progress rail, spec preview, diff cards, upload button, stage editor, revision panel, 409 UX | Manual smoke on agent.lescall |
| **P7** | Pramukh explicit backfill (additive, off-peak, one section per commit: personality/brand/playbook/roles/collect/post_call — `prompt_guidance` = literal current lines) + live test call; audit `extra_*_rules` per agent and land the guardrails alias; flip new-agent defaults (slots always emitted, `max_asks: 2`, `use_builtin_tables: false`, DND phrases); language-default wiring after per-agent audit | Harness re-baselined deliberately; one recorded Pramukh call reviewed end-to-end |
| **P8** | Deprecations: neutral builtin objection block for never-backfilled agents (flagged change), fallback slot-set retirement, `update_agent`→propose/apply prompt switch, Overview KPI change | Per-change sign-off |

**Deploy mechanics** (per platform doctrine): dashboard + runtime are separate docker containers over one repo mount — schema changes redeploy **both** in one step; Pramukh YAML edits hot-reload instantly via watchfiles, so backfills happen off-peak, validated in a scratch copy first, written atomically.

---

## 12. Back-compat defaults ledger (the contract)

Every new field, its default, and why that default is provably today's behavior:

| Field | Default | Today-equivalence |
|---|---|---|
| `identity.role_line` | `''` | exact current f-string at `conversation.py:2345` |
| `personality.traits/energy_mirroring/ai_disclosure/verbosity_max_sentences` | `[]`/`true`/`deflect`/`3` | current hardcoded lines `:2399-2400, 2346` |
| `brand.*` | all empty | resolvers fall back to identity/kb fields rendered by the exact current BRAND lines `:2351-2353` |
| `languages.greeting_language/bilingual_greeting`; `tts.gender` | `''`/`false`/`''` | greeting falls back to `default`; gender informational |
| `call_flow.call_goal/completed_when/scheduling/callback_policy` | `''`/`{}`/`{offer_count:2, style:exact_n}`/`{enabled:false}` | no MISSION block; no gate (validator is dead code today); exact "TWO options" strings `:2328,2453,2456`; 2-strike path untouched |
| `stages[].role/label/collect/advance_when/branches` | `''`/`''`/`[]`/`{}`/`[]` | role derived from legacy id; turn-count-only advancement; linear flow |
| `slots[].type/enum_*/extract_patterns/depends_on/merge_policy/confirm_readback/max_asks/reask_question/summary_*` | `''`/empty/`[]`/`[]`/`free`/`''`/`0`/`{}`/`''` | legacy name-keyed extractors; no gating; latest-wins; never read back; unlimited asks |
| `knowledge_base.catalog/match_rules/pricing_policy/fact_policy/product_lock` | `[]`/`[]`/`{}`/`{}`/`{}` | `portfolio_prompt_text` verbatim; no injection; exact `:2450` string; no lines; set-once lock |
| `stt_corrections.use_builtin_tables` | `true` | Pramukh tables keep running for existing agents |
| `response_bank.objection_playbook/closing_responses/canned_phrases/ai_disclosure_responses/enable_greeting_shortcut` | `[]`/`{}`/`[]`/`{}`/`false` | current 4 hardcoded prompt lines; current farewell chain; no shortcut changes |
| `outbound.*` | all empty | exact current direction-block prose `:2282-2313` |
| `guardrails.*` | `use_builtin_rules: true`, rest empty, `dnd_phrases: []` | FIRM RULES byte-for-byte; no DND behavior change |
| `follow_up.*` | `enabled: false` | prose-in-stage-goals keeps driving Pramukh |
| `calendar.day_synonyms/timezone/booking_confirmation_line` | `{}`/`Asia/Kolkata`/`{}` | hardcoded day map + IST behavior unchanged |
| `post_call.*` | all empty/false | NULL outcomes; existing scoring prompt byte-for-byte; KPI unchanged until flagged change |
| top-level `revision/updated_at/updated_by` | `0`/`''`/`''` | never read by runtime |
| `llm_config.max_response_length` from_dict default | 150 → **400** | 400 is the current effective constant; Copilot YAMLs carrying 300 backfilled to 400 in the same change |

**With every default in place, an untouched `agents/pramukh/agent.yaml` must parse to a spec whose system prompt, stage trace, extraction behavior, and spoken deterministic responses are byte-identical to today — enforced by the P0 harness at every phase.**

---

## 13. Open decisions for you

1. **Overview KPI semantics** (§4.6): OK to count `outcome_kind='success'` alongside the disposition, or keep KPI untouched and add a separate "outcomes" widget?
2. **DND**: should "stop calling"/"remove my number" end the call immediately for *new* agents (recommended) — Pramukh keeps its current 2-strike behavior either way.
3. **Pramukh `ai_disclosure`**: your doctrine says "always admit"; the engine's current behavior is deflect-by-prompt. Backfill Pramukh to `admit` (a deliberate live change), or leave as-is?
4. **Groq vs stronger model for the interview**: the interview engine is model-proof (server-side completeness), but interview *quality* (follow-up depth, proposal quality) on llama-3.3-70b is the weak link. Option: point `COPILOT_LLM_*` at a stronger endpoint for the interview only.
