# PLAN — Native LLM tool calling for live calls

Status: **IMPLEMENTED 2026-07-29** (same day). P0 probe passed: TTFT flat
(median 1142ms with 5 tools vs 1165ms without), streamed tool_calls work
through the compat shim BUT arrive with `index=None` (handled) and the
model returns tool calls INSTEAD of text (so shadow mode moved to the
background extractor, and "on" mode speaks deterministic confirmations
on tool-only turns). Machinery: `app/voice/llm_tools.py` +
`ConversationRouter._execute_tool_proposals` / `_shadow_tool_extract`.
Modes: `llm_config.enable_tools` = off | **shadow (default)** | on;
env `LLM_TOOLS_MODE` is the global kill switch. Flip agents to "on"
after the P1 agreement metric looks clean (grep `[TOOLS] shadow`).

## Current state (verified in code, not docs)

- **Zero tool/function calling anywhere.** Repo-wide grep for `tools=`,
  `tool_calls`, `tool_choice` over `calling-agent/backend/app/` → no hits.
  The only structured output is `response_format=json_object` in the
  background slot extractor (`conversation.py:2349`).
- State extraction is **regex-first** (`state_manager.observe_user`, <1ms)
  plus a **background LLM JSON extraction** fired only when regex found
  nothing (`_spawn_slot_extractor`, `max_tokens=120`, 2.5s cap, fill-only
  merge). Failure mode: silent — a phrasing the regex never anticipated
  and the extractor misses simply extracts nothing.
- Business actions (calendar write, callback capture, DND flag, hangup,
  WhatsApp promise) are triggered by **deterministic code** inspecting
  state/intents — never chosen by the model with validated arguments.
- Stage transitions are **fully deterministic** (`_maybe_advance_stage`,
  `advance_when`, branches) — this is a strength and is NOT up for
  replacement.

## Why migrate

Tool calling puts the decision to act and the structured arguments in the
same model call that is already reasoning about the conversation:
"book them for Tuesday 3pm" becomes `book_appointment(date, time)` with
schema validation and a visible, retryable failure — instead of an
after-the-fact regex race that fails silently. The stage machine stays as
the **gate**: a tool call is a *proposal*; code decides whether it takes
effect.

## Target architecture

```
LLM (streaming, tools declared)
  ├─ text deltas  → sentence chunker → TTS   (unchanged, latency-first)
  └─ tool_calls   → ToolProposalBuffer → end-of-stream:
                      state_mgr validation  (slot types, fill-only rules)
                      stage-machine gate    (is this action legal now?)
                      turn-transaction      (defer while txn open — same
                                             rule as today's calendar write)
                      → execute | reject+log
```

Rules that keep today's guarantees:

1. **Stages advance only in code.** No `advance_stage` tool, ever.
2. **Tools never overwrite confirmed slots** — same `fill_only` semantics
   as the background extractor.
3. **Text streams first.** Tool execution happens at end-of-stream; a tool
   needed *before* answering (calendar availability) stays prompt-injected
   as today (`calendar_block`) so no serial tool round-trip is added to
   the spoken path.
4. **Continuation-merge safe**: proposals buffer inside the existing turn
   transaction and are discarded on rollback (calendar writes already
   defer this way — reuse that mechanism).

## Tool roster (initial)

| Tool | Kind | Replaces |
|---|---|---|
| `record_customer_details(name?, budget?, location?, bhk?, timeline?)` | write-proposal | regex extractors + background JSON call |
| `book_appointment(date, time)` | write-proposal | `_extract_time_preference` + booking heuristics |
| `request_callback(when, reason)` | write-proposal | `_capture_callback_flags` |
| `flag_do_not_call()` | write-proposal | DND regex |
| `end_call(reason)` | write-proposal (still goes through hangup-permission flow) | farewell intent detection |

Read-only lookups (projects, documents) stay prompt-injected/RAG — they are
needed *before* the answer, so a tool round-trip would add latency.

## Migration phases

- **P0 — capability probe (½ day).** Measure gemini-3.1-flash-lite via the
  OpenAI-compat endpoint: does declaring ~5 tools change TTFT? Do streamed
  `tool_calls` deltas arrive correctly through the compat layer? Abort
  criteria: >100ms TTFT regression or broken tool-call streaming
  (fall back to plan B: tools on the *fallback* extraction call only).
- **P1 — shadow mode (1-2 days).** Declare tools, EXECUTE NOTHING: log
  proposals alongside what regex/extractor produced, on real calls.
  Metric: agreement rate + proposals regex missed (that's the payoff) +
  hallucinated proposals (that's the risk). Uses `call_turns` +
  `credit_log` for cost tracking.
- **P2 — gated execution (2-3 days).** Turn on validator-gated execution
  for `record_customer_details` only (lowest blast radius — fill-only
  slots). Regex stays live as the fast path; extractor demoted to
  tool-miss fallback. Per-agent YAML flag `llm_config.enable_tools`.
- **P3 — action tools (2-3 days).** `book_appointment`,
  `request_callback`, `flag_do_not_call`, `end_call` behind the same
  gates. Retire the background slot extractor once shadow agreement >95%
  over ≥50 calls.

## Risks

- **Gemini OpenAI-compat quirks**: tool-call streaming through the compat
  shim is the biggest unknown → P0 exists to kill this cheaply. The
  single-systemInstruction gotcha (see `conversation.py:3079`) already
  shows the shim is not fully faithful.
- **Latency**: tools add prompt tokens (~200-400 for the roster above).
  Mitigation: minimal schemas, no descriptions beyond one line, P0 gate.
- **max_tokens interplay**: thinking + tools eat into `max_tokens=160`
  (the `LLM_REASONING_EFFORT` lesson) — P0 must verify with
  reasoning_effort=none.
- **Double-write with regex fast path**: fill-only merge makes this
  idempotent, same as today's extractor race.

## Explicit non-goals

- Replacing the deterministic stage machine or `advance_when`.
- Tool-driven RAG / calendar reads on the spoken path.
- Any change to shortcut/canned bypasses (they skip the LLM entirely).
