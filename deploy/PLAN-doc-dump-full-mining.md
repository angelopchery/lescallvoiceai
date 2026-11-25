# PLAN — Full-coverage mining of documents & pasted dumps in the Copilot interview

**Status:** IMPLEMENTED & DEPLOYED 2026-07-15 (P1-P5; 80/80 backend tests incl. golden harness; lescall-api restarted)
**Scope:** Copilot only (`dashboard/agent_copilot_api.py`, `calling-agent/backend/app/documents/yaml_filler.py`). No engine/schema changes.

## 0. The requirement

When the user attaches a document or pastes a large text containing content for
multiple interview topics (objections, greetings, call flow, guardrails, …) —
at the start or mid-interview — the Copilot must absorb ALL of it and never
re-ask about topics the content already answers.

## 1. Current gaps

| Path | Behavior today |
|---|---|
| Attached document (`ingest_document`) | Fixed extraction schema covers identity/brand/guardrails/knowledge/greeting/direction/slots-calendar. **Not extracted:** call flow stages, call goal, objection *responses* (only patterns), closing lines, persona, languages, data-capture slots → those topics are re-asked. 14k-char truncation. Hardcoded gpt-4o-mini, no retry. |
| Pasted dump | No server-side extraction at all — mining depends on the interview LLM obeying a prompt instruction. Weak fallback models mine 1–2 sections and drop the rest. |
| Both | Extracted (`derived`) data can overwrite interview-confirmed answers on re-ingest. |

## 2. Fixes

- **P1 — Widen the extractor** (`yaml_filler._PROMPT`) to every interview layer:
  `call_goal`, `stages [{id, goal}]`, `objection_playbook [{id, patterns, answer:{lang}}]`,
  `objection_responses {lang:[str]}`, `closing_responses {lang:str}`,
  `personality {tone, energy, style, traits, ai_disclosure}`,
  `languages {supported, default}`, `slots [{name, required, question:{lang}}]`.
  Map all of them into the draft in `ingest_document` (shared helper).
  Everything passes `_normalize_definition` → all existing guards apply.
  Rules: `derived` never overwrites a `confirmed` provenance path; a document
  never replaces an existing ≥2-stage flow.
- **P2 — Deterministic mining for pastes:** in the chat route, when an
  interview is live (draft not built) and the user message is large
  (≥1500 chars), run the same extractor on the message BEFORE the LLM turn and
  merge via the shared helper; surface what was absorbed as a system note so
  the model acknowledges instead of re-asking. Extraction failure degrades to
  today's prompt-driven mining (never blocks the turn).
- **P3 — Extractor robustness:** chunk long texts (~12k chars/chunk, capped)
  and merge per-chunk results (lists extend+dedupe, dicts update, scalar
  first-wins); one retry on JSON parse failure; model via `DOC_EXTRACT_MODEL`
  env (default gpt-4o-mini).
- **P4 — Honest gap report:** ingest/mine results name the topics the content
  CLOSED and the ones still open; the next question targets only genuinely
  missing topics (done() checks make no-re-asking structural).
- **P5 — Regression tests:** all-section fixture document closes every
  matching layer; confirmed-beats-derived; stage-flow protection; paste
  auto-mine helper; chunk merge.

## 3. Deploy

`docker restart lescall-api` (bind mount). No runtime/SPA impact.
Run the golden harness per doctrine.
