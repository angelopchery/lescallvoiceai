# PLAN — Input priority: the user's words outrank the model's authorship

**Status:** IMPLEMENTED & DEPLOYED 2026-07-15 (115/120 backend tests; the 5 failures are the
parallel roles-flattening session's pre-existing edit-engine breakage, untouched by this diff)
**Scope:** Copilot only (`dashboard/agent_copilot_api.py`). No engine/schema/agent changes.

## 0. The requirement

"Any form of information that has been provided to the copilot as input must always be
preserved, edited and prioritized. The LLM must only and only use its own free power when
there is no input provided from the user side."

## 1. The regime

Every draft leaf carries a SERVER-DECIDED provenance rank:

| rank | source | meaning |
|---|---|---|
| 3 | `confirmed` | the user typed it |
| 2 | `derived` | it came from a document/dump the user supplied |
| 1 | `proposal` | the model proposed it and the user accepted |
| 0 | `authored` | the model's own invention |

**Rule:** a write may only land on a path that already holds a value if its rank >= the
stored rank. Gaps are never protected — authorship stays free where the user said nothing.

## 2. Verification, not trust

`source='user'` is a HINT, not authority. Spoken CONTENT (strings >= 25 chars, incl. those
buried in `call_flow.stages[].goal` and `objection_playbook[].answer`) must trace back to the
user's own words via **phrase-level (3-gram) overlap >= 0.35** against the corpus of everything
the user supplied (typed messages + ingested document text). Word-level overlap was rejected:
against a long brief nearly every common word appears somewhere, which waved invented copy
through. Short scalars are structure (enums/flags/voices/ids) and keep the claimed rank —
they never appear in prose and are not what a caller hears.

## 3. Enforcement points

- `update_draft`: rank → `_strip_outranked` → drop outranked leaves (noted), all-outranked
  raises "their input stands, do NOT retry the same content", authored leaves are flagged
  back to the model as ITS copy so it can re-record the user's wording.
- `_merge_extracted_into_draft` (document + paste miner): document = `derived`, beats authored,
  never beats typed.
- `create_agent`: definition sections the interview answered are dropped; draft merges last.
- `_enforce_invariants`: platform defaults must not contradict the user's VOICE choice —
  a male voice flips the default `she` pronouns and the feminine hi/gu fallback lines
  (only where the value still IS the default; user-written lines are never re-worded).
- System prompt HOW YOU WORK §2: "THE USER'S INPUT OUTRANKS YOU — ALWAYS … your own
  authorship is for GAPS ONLY."

## 4. Tests

`test_input_priority.py` (12): verification beats the claim, structural values exempt,
document text counts as the user's, authored cannot overwrite, all-outranked fails loudly,
gaps still fillable, the user can always change their own mind, document vs typed, corpus
excludes the model's own prose, gender defaults yield to the voice (both directions) and
never re-word user lines.
