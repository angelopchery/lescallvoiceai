# PLAN — Repeated "hello"s at call connect / agent long pause

**Date:** 2026-07-30 · **Status:** LIVE 2026-07-30 (all phases; RUNBOOK §18
is the operational reference). **P0 + the Copilot opening guards were
corrected the same day**: the owner wants openings to STAY a short "Hello"
(greet → wait → engine speaks the identity intro on the caller's response),
so the opening rewrites were reverted and the bare-opening lint/enricher
composition removed — loop prevention is engine-side only (P1.2/P2/P3).
See RUNBOOK §18 "Doctrine correction".
**Symptom (user report):** right after connect, the caller says "hello" repeatedly;
the agent takes a long pause and "keeps waiting" through the hellos. Most calls
show this at connect.

---

## 1. What the logs actually show

All evidence from `phase14_runtime/logs/latest/ws_audio_receiver.log.1` +
`socket_server.log.1` (2026-07-29) and `recordings/<call>/transcript.txt`.

### Headline numbers (full day, n=73 turns)

| Metric | Value |
|---|---|
| speech_end → first agent audio | **p50 2.47s · p75 2.73s · p90 3.26s · max 9.6s** |
| Calls opening with a bare `Hello` | **14 / 19** (both maya SDR agents: `opening_greeting: Hello`) |
| Barge-in interrupts | 23 |
| STT script misdetections (bare "hello" → Odia/Kannada, forcing a 2nd Sarvam round-trip) | 33 |
| Bare `'Hello'` transcripts | 14 |

A human on a phone re-says "hello?" after ~1.5–2s of silence. Our **median**
reply gap is 2.5s. The loop is structural, not bad luck.

### Case A — the canonical loop (`bba4e198…`, 919328225597, inbound, 12:14)

```
00.57  agent opening plays: "Hello" (0.43s)  ← the ENTIRE opening
02.21  caller: "hello" #1  (they heard 0.4s of audio then silence)
03.17  STT done (236ms) → engine    04.39  LLM TTFT 1163ms → response ready
04.74  TTS first chunk to sink — but NO wav flushed to FS yet
04.89  caller: "hello" #2 → BARGE-IN (frames=5, 100ms) kills the response
       socket_server confirms: NO PLAYBACK_SEND between 00.60 and 06.90
       → the caller NEVER heard response #1
06.20  hello #2 transcribed (528ms — Odia misdetect + forced-en retry)
06.20  [REPAIR] replay #1 → TTS re-synthesises (292ms) → plays 06.92
07.05  caller: "hello" #3 (already in flight before audio could reach them)
       → BARGE-IN at threshold 338 (cont_window=True) kills replay after 130ms
08.43  hello #3 → CONT-MERGE chain: 'hello hello hello' → [REPAIR] replay #2
09.13  caller: "hello" #4 → BARGE-IN kills replay #2
10.33  hello #4 (Kannada misdetect + retry) → replay cap spent → LLM
       [STAGE] → BUSINESS_DISCOVERY (max_turns 2 reached — caller said NOTHING)
       [STATE] name: None → 'hello hello hello' (LLM gap-fill!)
11.92  "Right, it's nice to have you on the line…" finally plays — first
       sentence the caller ever hears, 9.7s after their first hello
18.92  caller hangs up
```

**The loop:** hello → 2.5s dead air → agent reply starts → next hello (already
in flight) barges in and kills the reply *before any audio was flushed* → more
dead air → hello… Each party keeps resetting the other.

### Case B — opening never lands (`dcdba4cb…`, 918980906619, inbound, 12:25)

Opening "Hello" (0.43s) plays at 00.3s. Caller replies with a **240ms** "hello"
→ **below `MIN_SPEECH_MS=300` → never sent to STT** (`segments_sent_to_stt=0`).
Dead-air watchdog fires at 6.2s and replays… "Hello" again (last utterance).
Caller hears: "Hello" … 12s silence … "Hello". Hangs up at 14s.

### Case C — the "sorry" echo loop (`b217ca93…`, 919725244180, outbound, 11:04)

Caller's 2nd utterance barges in the agent's question; then VAD captures four
rhythmic 0.8–1.1s "speech" segments, each starting ~1.5s after each
"Sorry, I couldn't hear you clearly" playback — every one transcribes **empty**
at Sarvam, and every empty immediately queues another "sorry" (attempt 1/4 …
3/4). The segments fall **inside** the 3.2s `[ECHO-MUTE]` window yet still
reach STT (echo-gate early exit suspect). Caller eventually asks
"हेलो, आवाज़ आ रही है आपको?" — 50s of nothing intelligible either way.

### Case D — control (`918076562864`, outbound, 07:31): caller "hello" at 2.7s,
agent answers at 3.8s, playback survives, call flows fine for 90s. When the
reply lands inside ~1.5s and isn't barged, there is no loop.

---

## 2. Root causes (ranked)

1. **Bare `Hello` openings** (`agents/maya_lescall_sdr-*/agent.yaml:
   opening_greeting: Hello`). A 0.4s "Hello" then silence is a *prompt for the
   caller to say hello back and wait*. It's also easily missed during the
   answer transition. 14/19 calls that day. The hello-replay repair
   deliberately refuses to replay a bare-hello opening
   (`conversation.py:1607`), so the first caller hello always pays full
   LLM latency.
2. **A "hello" can kill a reply the caller never heard.** Barge-in needs only
   5 frames (100ms) of energy and 250ms elapsed (`BARGE_IN_MIN_ELAPSED_MS`).
   Before the sink's first per-sentence flush, `playback_busy_until` is 0 so
   the 2× frame guard (`pipeline.py:685`) never applies. Case A response #1
   and both replays died this way.
3. **The continuation-merge canary makes it worse.** For 2s after every caller
   utterance (`CONTINUATION_WINDOW_MS`), barge threshold drops to VAD×1.3
   (338 vs 520) with the same 5 frames — serial hellos arrive precisely in
   that window, so replays are killed *more* easily (Case A hellos #3/#4,
   `cont_window=True` in the log).
4. **2.5s median dead air feeds the caller's timeout.** Breakdown at median:
   ~200–400ms flush hangover + 240–680ms STT (doubled to ~900ms by script
   misdetect on bare hellos — 33×/day) + 950–1500ms LLM TTFT + 250–340ms TTS
   first chunk + per-sentence sink flush.
5. **Short hellos get dropped, then the watchdog parrots.** `MIN_SPEECH_MS=300`
   discards a real 240ms "hello" (Case B); `generate_silence_reprompt` at
   turn_count==0 replays the last utterance verbatim — which for these agents
   is the bare "Hello".
6. **Compounding damage once the loop runs:** CONT-MERGE + barge-in prepend
   build `'hello hello hello hello'`; that garbage is (a) fed to the LLM,
   (b) **gap-filled into `CallState.name`**, (c) counted as stage turns so the
   stage machine advanced past identity while the caller had communicated
   nothing.
7. **Empty-transcript → instant "sorry" loop** (Case C): every clean-empty
   during the early call answers with a spoken fallback, which produces echo,
   which produces another empty. No streak damper; echo-gate lets mid-window
   segments through.

---

## 3. The plan

### Scope — platform-wide by construction

This is a **platform fix, not a maya fix**. Agents on this system are data
(YAML under `agents/<slug>/`); there is exactly ONE engine —
`pipeline.py` (VAD/STT/TTS/barge-in/watchdog), `conversation.py`
(ConversationRouter), `stt/` — shared by every agent, every tenant, inbound
and outbound. Phases 1.2 and 2–6 change that shared engine, so they apply
automatically to **all existing agents and every agent Copilot creates in
the future**, with zero per-agent configuration. The only per-agent items
are Phase 0 (repairing the three YAMLs that carry the bad opening today)
and the Copilot-side guards in 1.1 that stop the bad pattern from ever
being authored again. Where behaviour should be business-tunable, it gets a
`call_flow.*` knob with the platform default ON (same doctrine as
`call_flow.termination`) — Copilot can tune it per agent, but no agent has
to opt in to be protected.

### Phase 0 — config-only, deployable today (no code)

**0.1 Rewrite the bare openings (repo sweep, 2026-07-30).** A sweep of all
27 `agents/*/agent.yaml` found exactly three with `opening_greeting: Hello`:
`maya_lescall_sdr-inbound`, `maya_lescall_sdr-outbound`, and
`outbound_lead_qualifier-inbound`. Every other agent already opens with a
full identity line. Rewrite all three, e.g. "Hello! This is Maya calling
from LesCall by DhronAI — am I speaking with {contact_name}?" (outbound) /
"Hello, thanks for calling LesCall! This is Maya — how can I help you
today?" (inbound). This alone removes the trigger from 14/19 affected calls,
makes the opening replayable by the hello-repair rule, and gives the
dead-air watchdog something worth repeating.

### Phase 1 — stop inviting the loop (Copilot authoring layer, all agents)

**1.1 Make a bare opening impossible to author.** Three hooks in
`dashboard/agent_copilot_api.py`, mirroring how termination was made
doctrine:
  - **Create-time default** in `_enrich_new_agent` (line ~2108, CREATE-ONLY
    per the call-fidelity doctrine — never in `_normalize_definition`): when
    a new agent's `opening_greeting` is missing, < ~15 chars, or
    bare-hello-like, compose one from `identity.agent_name` +
    `identity.company_name` (+ `{contact_name}` for outbound). Every future
    Copilot-created agent starts safe by default.
  - **Quality lint** in `_quality_warnings`: warn on any create/edit when
    `opening_greeting` is bare/near-bare — "the opening must identify the
    agent/company; a bare Hello triggers a hello ping-pong at connect."
    Same lint for stage-0 goals that instruct the agent to open with just a
    greeting word. Add to `ALL_MARKS` in `test_copilot_quality_lints.py` so
    the production-nag guard proves no healthy agent gets nagged.
  - **System-prompt doctrine**: teach the Copilot the opening rule
    explicitly (the same pattern as "CALL-ENDING REQUIREMENTS GO IN
    call_flow.termination") so it repairs bare openings when asked to edit
    an agent, instead of preserving them.

**1.2 Instant intro on the first bare hello (kill the LLM tax on turn 1) —
engine, all agents.** In `ConversationRouter.process_stream`, before LLM
routing: if we are in the greeting stage, `turn_count == 0..1`, and
`_is_bare_hello_text(text)` — reply deterministically with the full
configured intro, 0ms think time. Source order: `opening_greeting` if it is
a full line → stage-0 opening question → **identity-composed fallback**
("Hello, this is {agent_name} from {company_name}…") so the fast path works
even for an agent whose config is still bad — it must never depend on
per-agent data being clean. This replaces the current path where the first
hello pays 1.2–1.5s TTFT to have the LLM improvise a greeting.
Pre-synthesise this line into the TTS PCM cache at call setup (the greeting
warmup already exists — extend it to cover the intro line) so the whole
turn is STT + ~0ms.

### Phase 2 — a hello must not kill an unheard reply (pipeline)

**2.1 Delivery-window barge-in guard.** Until the response's first WAV has
been flushed AND ≥ ~1.0s of it has actually played
(`sink.playback_busy_until` anchored), require sustained speech to barge:
`min_frames` ≈ 3–4× (600–800ms of continuous energy) instead of 5 frames.
A real interruption ("stop calling me", a full sentence) still cuts within
~1s; a 400–600ms "hello?" no longer can. Applies only in the first ~10s of
the call (or greeting/first-response turns) to keep normal barge-in snappy.
Knobs: `DELIVERY_GUARD_S`, `DELIVERY_BARGE_FRAMES`.

**2.2 Same immunity for hello-replays.** A replay exists precisely because
the caller heard nothing; letting the next in-flight hello kill it at
threshold 338 within 130ms (Case A) defeats it. Replayed utterances get the
2.1 guard for their first ~1.5s regardless of call age.

**2.3 Barge-in undo for bare hellos.** If barge-in DID fire and the STT
result comes back as a bare early-hello (we know within ~500ms), re-enqueue
the interrupted utterance from the start (PCM-cached, does not decrement the
replay cap — it's a resume, not a new replay). This converges even when the
guard misses.

**2.4 Continuation-window carve-out.** While `ENABLE_CONTINUATION_MERGE` is
on, do not apply the lowered continuation barge threshold during the delivery
window of a replay/first response — the two features currently fight
(root cause 3).

### Phase 3 — shrink the dead air on hello turns (STT layer)

**3.1 Force-language for early short segments.** For segments < ~800ms in the
first ~15s with no locked language, send the agent's default language instead
of `lang=unknown`. Kills the Odia/Kannada misdetect → forced-en retry that
doubles STT latency on exactly these turns (33×/day). Fall back to today's
behaviour once a real sentence arrives.

**3.2 Greeting-window min-speech.** During the first ~10s, lower the STT
floor to ~200ms (`MIN_SPEECH_MS` stays 300 elsewhere) so a clipped "hello"
(Case B's 240ms) still reaches STT instead of being silently dropped.

### Phase 4 — stop the compounding damage (conversation layer)

**4.1 Bare hellos never merge, never fill slots.** Exclude bare-hello texts
from CONT-MERGE chains and barge-in context prepending (`'hello hello'`
carries no information); reject hello-like values in name gap-fill /
`[QA] Field answered` (Case A stored `name='hello hello hello'`).

**4.2 Repair turns don't burn stage turns.** Hello-replay and dead-air
reprompt turns must not increment the stage-machine turn counters
(Case A advanced to BUSINESS_DISCOVERY on `max_turns 2` off the back of four
hellos). Same principle already exists for reprompts' `last_agent_response`
handling — extend it to stage accounting.

**4.3 Dead-air reprompt never parrots a bare hello.** In
`generate_silence_reprompt`, when the last utterance is bare-hello-ish,
substitute the full intro line (per 1.2) instead of replaying "Hello".

### Phase 5 — empty-transcript / echo loop (Case C)

**5.1 Empty-streak damper.** First clean-empty in a turn: stay silent and
keep listening (no spoken "sorry" — today it speaks on attempt 1). Second
consecutive: one "sorry, couldn't hear you". Beyond: silence + rely on the
dead-air watchdog. Prevents the sorry→echo→empty→sorry self-feed.

**5.2 Echo-gate audit (investigation).** In Case C, segments born *inside*
the 3.2s `[ECHO-MUTE]` window reached STT. Verify `_vad_mute_until` vs the
segment path: suspects are the echo-gate early-exit thresholds
(`ECHO_EARLY_EXIT_FRAMES=5`, `CONTINUATION_ECHO_EXIT_FRAMES=4` — 80–100ms of
echo energy defeats the gate) and the busy_until anchor when playback was
just barged. Outcome: either raise exit frames within the window or gate
segment *submission* (not just capture) during the mute window.

### Per-agent tunability (Copilot control, defaults ON)

Phases 2–5 are platform defaults for every agent. For businesses that need
different behaviour, expose a small `call_flow.connect` block (same shape
discipline as `call_flow.termination`): `hello_fastpath` (default true),
`delivery_guard` (default true), `replay_cap` (default 2). Copilot schema
docs + `_normalize_definition` shape-validation + lints follow the
termination template exactly. No agent has to set any of this to be
protected — the block exists only to opt OUT or tune.

### Phase 6 — observability + rollout

- Per-call counters in the session summary + `call_turns`: hello-replay count,
  barge-ins during delivery window, empty-transcript streaks, time-to-first-
  substantive-response. Alert/report when a call logs ≥2 early hellos.
- Every behavioural change behind an env knob, default matching today except
  the new guards; canary exactly like cont-merge (RUNBOOK §11 pattern —
  remember: compose restart ≠ env apply).
- Tests: unit tests per gate (barge-in delivery guard is a pure decision —
  test like `_silence_hangup_due`); replay-immunity + undo through the
  `test_call_repair.py` harness; lint tests in `test_copilot_quality_lints.py`
  with the production-nag guard; golden prompts unaffected (no prompt-text
  changes).

### Acceptance criteria

1. Synthetic replay of Case A's timing (hello every ~2.2s) ends with the full
   intro audible by ~3s and **zero** killed-before-flush responses.
2. A 240ms "hello" reaches STT during the greeting window (Case B).
3. No transcript ever shows ≥3 consecutive caller hellos with zero completed
   agent playbacks between them (checkable from PLAYBACK_SEND vs BARGE-IN in
   logs).
4. `CallState.name` can never be set to a hello-derived string.
5. Two consecutive empty transcripts produce at most one spoken "sorry".

### Priority / effort

| Phase | Impact | Effort | Risk |
|---|---|---|---|
| 0 (3 bare-opening YAMLs) | High — removes trigger in most affected calls | trivial | none |
| 1 (authoring guards + instant intro) | High — removes 1.5s LLM tax on turn 1, prevents recurrence | S | low |
| 2 (delivery guard) | High — breaks the kill loop | M | medium (barge-in feel — canary) |
| 3 (STT fast lane) | Medium — ~0.3–0.7s off hello turns | S | low |
| 4 (pollution) | Medium — correctness | S | low |
| 5 (echo/empty) | Medium — fixes Case C class | M | medium (needs 5.2 audit first) |
