# PLAN — Copilot draft-mapping fix (update_draft rejects valid answers)

**Status:** IMPLEMENTED & DEPLOYED 2026-07-15 (F1-F5; 78/78 backend tests incl. golden harness; lescall-api restarted; incident payload replayed successfully in the live container)
**Scope:** Copilot only (`dashboard/agent_copilot_api.py`, `dashboard/copilot_interview.py`). No agent YAML, engine, or schema changes.

## 0. The incident

During an interview, the user answered the *Objective* question with a perfectly good
call goal. The LLM called `update_draft(changes={"call_objective": "..."})` — using the
**interview layer id** as a field name. `_normalize_definition` dropped the unknown key,
`changes` became empty, and `update_draft` raised
`"None of that mapped to real settings — nothing was recorded"` (agent_copilot_api.py:2286-2288).
The LLM then blamed the user: *"The call objective is not a real setting. Can you rephrase…"*.

## 1. Root causes (all in Copilot)

| # | Cause | Where |
|---|-------|-------|
| R1 | Layer ids (`call_objective`, `data_capture`, …) look like field names, but `update_draft` only accepts spec paths (`call_flow.call_goal`, `slots`, …). The vocabulary the server shows the LLM is not the vocabulary it accepts back. | copilot_interview.py layer ids vs `_normalize_definition` |
| R2 | `update_draft` is all-or-nothing: zero surviving keys → hard error whose text contains no corrective mapping, so the LLM cannot self-repair. | agent_copilot_api.py:2286-2288 |
| R3 | `render_interview_state()` never tells the LLM *where* to record the current layer's answers, even though every `InterviewLayer` carries `spec_paths`. | copilot_interview.py:330-360 |
| R4 | No prompt rule that tool-mapping errors are the LLM's to fix silently — so it surfaced the error to the user and asked them to rephrase a fine answer. | `_system_prompt` HOW YOU WORK §1 |

## 2. Fixes

### F1 — Deterministic alias rewrite (server-side, before normalize)
New `_alias_rewrite(changes: dict, notes: list[str]) -> dict` applied at the top of
`update_draft` (and `create_agent`/`validate_agent`, same choke point) that renames
known-wrong top-level keys to canonical paths **only when unambiguous**:

| Alias keys (top-level) | Canonical destination |
|---|---|
| `call_objective`, `objective`, `call_goal`, `goal`, `mission` (string value) | `call_flow.call_goal` |
| `voice`, `tts_voice` (string/dict) | `languages.tts_voices` (string → `{default_lang_or_"en-IN": {speaker: v}}` handled by existing voice normalizer) |
| `tone`, `energy`, `style` (string) | `personality.<same key>` |
| `company`, `business_name` | `identity.company_name` |
| `agent_name` (top-level string) | `identity.agent_name` |
| `greeting`, `opening_line` | `response_bank.opening_greeting` |
| `closing`, `closing_line` (dict per lang) | `response_bank.closing_responses` |
| `objections`, `objection_responses` (list of {id/answer} dicts) | `response_bank.objection_playbook` |
| `knowledge`, `catalog_text`, `products` (string) | `knowledge_base.portfolio_prompt_text` |
| `stages` (list, top-level) | `call_flow.stages` |
| Every INTERVIEW layer id whose value is a dict | deep-merge into that layer's primary section (`business_identity`→`identity`, `persona_voice`→`personality`, `guardrails_compliance`→`guardrails`, `brand_trust`→`brand`, `scheduling_followup`→`calendar`, `data_capture`→`{slots: v}` if list) |
| Anything ambiguous | leave for normalize to drop (F2 handles messaging) |

Each rewrite appends a note: `aliased 'call_objective' → call_flow.call_goal`.
The map lives as one module-level dict next to `_normalize_definition` — a single
place to grow as new misfires are observed in session logs.

### F2 — Self-correcting failure contract
Rework the `update_draft` empty-changes branch:

- **Partial survive** → success (already the case), but `notes` MUST list every dropped
  key **with its nearest valid path** (difflib `get_close_matches` against a canonical
  path list derived from `_default_definition` + the DEFINITION FIELDS doc).
- **Nothing survives** → still an error (nothing silently lost), but the message becomes
  machine-repairable:
  `Nothing recorded. 'call_objective' is not a spec field — the CURRENT topic
  ({layer.title}) records under: call_flow.call_goal, call_direction. Retry
  update_draft NOW with those paths. The user's answer was fine — do NOT ask them
  to rephrase.`
  (current `next_layer`'s `spec_paths` injected server-side; suggestions from the
  close-match list for keys that don't belong to the current layer).

### F3 — Spec paths in the INTERVIEW STATE block
`render_interview_state()` NEXT section gains one line:
`Record answers under: call_flow.call_goal, call_direction` (the layer's `spec_paths`).
~10 tokens/turn; eliminates the guesswork that caused R1.

### F4 — Prompt rule (one sentence in HOW YOU WORK §1)
"Tool errors from update_draft/create_agent are YOURS to fix by correcting field paths
and retrying — never tell the user their answer 'isn't a real setting' and never ask
them to rephrase because of a mapping problem."

### F5 — Regression tests (backend suite)
1. Replay the incident: `update_draft(changes={"call_objective": "…"})` → succeeds via
   F1, lands in `call_flow.call_goal`, provenance recorded, `call_objective` layer done.
2. Mixed valid/invalid keys → valid merged, notes carry close-match suggestion.
3. Fully unknown keys → error text contains current layer `spec_paths` and the
   "do NOT ask them to rephrase" instruction.
4. Alias map never shadows a REAL spec key (assert alias keys ∩ canonical top-level
   keys = ∅).

## 3. Deploy
`docker restart lescall-api` (bind mount — no image build). No runtime/SPA impact.
Golden-prompt harness unaffected (no engine files touched) but run it anyway per doctrine.

## 4. Non-changes
- Layer ids stay as-is (they're in session files and the SPA progress rail).
- `_normalize_definition`'s drop-unknown behavior stays — F1 runs *before* it; unknown
  junk is still never written to a draft or YAML.
- No change to `skip_layer`, create gate, or engine.
