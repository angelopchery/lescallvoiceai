# PLAN — Call hearing + capture hardening (platform-wide)

**Status: IMPLEMENTED 2026-07-31 (same day) — RUNBOOK §21. Full suite 800 green
(43 new in `tests/test_hearing_capture_hardening.py`); deployed with 0 active
calls. Deltas from spec: none — every fix landed as written; the generic
confirm loop (conversation.py:2215) additionally got the C3 read-back-context
gate (it was a second, independent discard path the spec's C3 implied).**

Authored 2026-07-31 from call `918076562864_20260731_052423`
(conn `bf023e24`, agent `maya_lescall_sdr-outbound` rev 42, hi, 8m00s).
Follow-up to `PLAN-language-fidelity-and-contact-capture.md` (RUNBOOK §20): that deploy's
language layer worked exactly as designed on this call (zero wrong-language turns, zero
accent flips, LLM email writes blocked). What failed is one layer *below* it (streaming-STT
segment accounting silently discarding real caller speech) and one layer *above* it (the
capture flow arming too late and fighting the LLM for ownership of the email conversation).

Every fix below is **engine-level** (`pipeline.py`, `conversation.py`, `state_manager.py`,
`sarvam_stream.py`, `contact_capture.py`) so it applies to the LesCall agent, every other
existing agent, and every future Copilot-built agent with **zero agent-YAML changes**.

---

## 1. Incident inventory (what the owner heard) → root causes

| # | Symptom on the call | Root cause(s) |
|---|---|---|
| I1 | "LesCall kaun?" transcribed as "plus call count"; agent ignored the question | Sarvam mishear of the brand word pre-pin (pin engaged the following turn). Accepted-risk; mitigated by RC-free pin now engaging at turn 3. No code fix — noted §7. |
| I2 | "Ham basically ji (pause) **Cleaning and helping** services…" — the words after the pause vanished | RC1 (instant dispatch on a dangling utterance) + RC2 (barge-in `reset_turn()` throws away fed caller audio) |
| I3 | ~35 s "couldn't hear you" spiral though the caller was talking the whole time | RC3 (plausibility check uses local segment ms against stream-accumulated text) + RC4 (double-flush consumes the episode, second finalize counts as "unrecognized") + RC5 (real speech dropped as "stale inter-turn segment") + RC6 ("हेलो" missing from the single-word allowlist) |
| I4 | "आंदलो। डॉट।" → agent reply died after 254 ms; caller's continued spelling ("मनन एट द रेट।") vanished; 7 s of mutual silence → "हेलो" | RC2 again (generation bump discarded the spelled payload) + RC7 (capture never armed, so the engine answered mid-spelling instead of holding) |
| I5 | Email took 2 min of readback ping-pong; agent read back values that were never in state ("angelo.ann@", "angelo.pa.knaan@"), asked to spell "धीरे-धीरे" | RC7 (capture armed only after a regex fill made the slot pending — every earlier spelling turn went to the LLM) + RC8 (LLM/engine split-brain: LLM speaks its own assemblies, cannot write state, engine's pending value diverges) + RC9 (normalizer gaps: "एट द रेट", Devanagari letter-name runs before an @-token dropped) |
| I6 | "हाँ सही है सही है।" did not confirm; final "नहीं नहीं।" (about *"any more questions?"*) **discarded the email** and spoke the unresolved line after the LLM had claimed it was noted | RC10 (bare-confirm is `≤3 words` — an emphatic Hindi yes is 5) + RC11 (bare yes/no interpreted as confirm/reject with **no context check** that the last agent turn was a readback) |
| I7 | DB: `name: "Panj Loa"` (a spelling fragment), `meeting_platform: null` despite "ज़ूम चलेगा", `preferred_time: "Now"` despite the booked "आज शाम 4 बजे" | RC12 (LLM extractor runs on spelling turns → name pollution) + RC13 (echo guard is script-blind: Latin "Zoom" in agent turn vs Devanagari "ज़ूम" in caller turn → legit answer dropped) + RC14 (fill-only merge makes the first value immortal even when the caller directly answers the agent's follow-up question) |
| I8 | 2 s response tax on every turn after the email section (Zoom, 4-baje…) | RC15 (capture merge window applies to every armed turn, not just spelling-context turns; capture stayed armed because pending never resolved) |
| I9 | Dead-air watchdog replayed the entire 166-char two-sentence question verbatim | RC16 (replay is whole-utterance; should condense to the question) |

## 2. Root causes with log anchors

- **RC1 — instant dispatch on dangling utterance.** `हम बेसिकली जो।` flushed and
  `Merge window skipped (idle)` at 05:25:33.622; LLM answer left at 33.623 while VAD logged
  Speech-start at 33.727. The idle-skip optimization ignores that the transcript *ends in a
  continuation word* (`जो`). `ENABLE_CONTINUATION_MERGE=true` did not save it: the barge fired
  at 35.607, outside `_continuation_likely_until` (armed from speech-end 33.607), so
  `cont_window=False`.
- **RC2 — barge-in flush destroys fed caller audio.** `_flush_pipeline_queues()`
  (pipeline.py:1180) calls `_streaming_stt.reset_turn()`, bumping the generation; every
  segment the server later emits for audio fed *before* the barge trigger is discarded as
  old-gen. Log: `[TURN] Flushed: 'x x x x'` at 05:25:35.609 then `segment #12 gen=11` —
  gen 10 ("cleaning and helping…") never surfaced. Same at 05:28:47: `'मनन एट द रेट।'`
  arrived as gen 31, finalize ran at gen 32 → `'' (segments=0)`. The echo-gate had already
  vetted these frames as real caller speech (`[ECHO-GATE] Early exit … treating as real
  caller speech`), so the discard throws away *vetted* input.
- **RC3 — plausibility check is streaming-blind.** pipeline.py:1308-1329 scores
  words against `segment.duration_ms` (one VAD chunk). Streaming finalize returns the whole
  speech *episode* (server-stitched across VAD chunks). 05:26:25.042: `Rejecting implausible
  transcript: 14 words in 1440ms` — real answer destroyed. 05:31:52.951: same, 16 words in
  660 ms — the caller's availability answer destroyed. `SarvamStreamingSTT.last_covered_seconds`
  exists **for exactly this** (router already uses it for derived confidence at
  stt/router.py:718) — the pipeline check never consults it.
- **RC4 — double-flush "unrecognized" spiral.** The first VAD flush's finalize consumed both
  server segments; the second flush's finalize returned `''` after a full 2 s timeout and
  **counted toward the unrecognized-speech streak** → "आवाज़ स्पष्ट नहीं आई" at 05:26:27
  while the caller had been perfectly understood (and rejected by RC3).
- **RC5 — real speech dropped as stale.** sarvam_stream.py:356-362: a data segment arriving
  when `_fed_since_finalize` is false is discarded. 05:26:25.611:
  `dropping stale inter-turn segment 'अभी तो दिक्कत नहीं है कोई बिल्कुल भी।'` — a real
  caller sentence, thrown away while finalize #2 sat waiting 2 s for exactly that text.
- **RC6 — "हेलो" not in the allowlist.** pipeline.py `_greetings` has `हैलो`/`हलो` but not
  `हेलो` (the spelling Sarvam actually produces). 05:26:38.968: `Single-word fragment 'हेलो।'
  … skipping` — the caller's are-you-alive probe ignored, dead-air watchdog fired 6 s later.
- **RC7 — capture arms too late.** `_refresh_capture_slot` arms on *pending* or on
  `asked_count ≥ 1`; `note_asked` only runs in `build_prompt_context`'s stage-discovery
  branch (state_manager.py:763). This agent's email ask came from the stage goal via the
  LLM, so `asked_count` stayed 0. Capture armed at 05:29:36 — **five spelling turns after
  the ask** — and released again at 05:29:40 when the rejection cleared pending
  (`[CAPTURE] released`).
- **RC8 — split-brain readbacks.** The LLM spoke `angelo.ann@`, `angelo.pa.knaan@`,
  `anjali.panan@`, `angelo.panan@` and "मैंने नोट कर लिया" — none of which it can write
  (`[TOOLS] record_customer_details … → 0 filled`). State pending meanwhile held first
  `knaan@gmail.com`, then `anjali.panan@gmail.com`. The caller confirmed the LLM's spoken
  value; state held a different one; the end-of-call `नहीं नहीं` (RC11) then discarded even that.
- **RC9 — normalizer gaps** (contact_capture.py): no multi-word `एट द रेट` / `at the rate`
  operator; Devanagari letter-name coverage partial (`ए एन जी ई एल ओ` run before an @-token
  contributed nothing — result `knaan@gmail.com` instead of `angelo.pa.knaan@gmail.com`);
  pre-@ spelled material is dropped when any token already contains `@`.
- **RC10 — bare-confirm too strict.** `_bare_confirm = len(text.split()) <= 3`
  (conversation.py:4535). `हाँ सही है सही है।` = 5 tokens → fell to the LLM.
- **RC11 — no readback context for bare yes/no.** Any bare negation while pending rejects
  (conversation.py:4550) — even 2 minutes and 6 turns after the last readback, even when the
  agent's question was "कोई और सवाल?". 05:31:56: `email rejected by caller — re-asking` →
  cap → `unresolved` → stored **nothing**.
- **RC12 — extractor runs on spelling turns.** 05:29:06: `name: None → 'Panj Loa'
  (source=llm turn=18)` — the caller was spelling "angelo", not introducing themself.
- **RC13 — script-blind echo guard.** conversation.py:2789-2799 drops an extracted value
  present in the agent's last utterance but "not" in the caller's — compared as raw
  lowercase strings across scripts. Caller: `ज़ूम चलेगा` (Devanagari); agent: "जैसे कि Zoom
  या Google Meet?" (Latin) → "Zoom" dropped → `meeting_platform: null`.
- **RC14 — fill-only immortality vs direct answers.** `preferred_time='Now'` (turn 13);
  agent later asked "आज कितने बजे फ्री रहेंगे?"; caller: "आज। 4 बजे।" — no correction marker,
  slot filled → update refused; booking recorded at "Now".
- **RC15 — capture merge-window tax.** `Merge window 2000ms [capture]` on the Zoom, booking
  and closing turns (05:30:54 → 05:31:54) — 2 s added to five turns that had nothing to do
  with spelling, because "armed" is conflated with "expecting spelled input *now*".
- **RC16 — verbatim watchdog replay.** 05:26:44: reprompt replayed the full 2-sentence
  question (166 chars ≈ 13 s of TTS) after an apology, compounding the caller's impression
  the agent is broken.

## 3. Fixes — Part H (hearing: pipeline + stream)

**H1. Plausibility uses covered audio, not the local chunk** (pipeline.py:1308).
`effective_ms = max(segment.duration_ms, stt_covered_ms)` where `stt_covered_ms` comes from
`self.stt.last_covered_seconds * 1000` (STTRouter grows a `last_covered_seconds` property
delegating to the active provider; batch providers report 0.0 → unchanged behavior).
Keeps the hallucination guard fully intact for batch mode and for streams that cover little audio.

**H2. Barge-in keeps fed caller audio** (pipeline.py:1180-1195). Remove the
`reset_turn()` call from `_flush_pipeline_queues` — the generation bump there discards
vetted caller speech; the superseded *turn*'s segments were already consumed by its own
finalize (which bumps the gen itself, sarvam_stream.py:435). The too-short-discard site
(pipeline.py:1101) **keeps** its `reset_turn()` — that drop is intentional.
Knob: `BARGE_KEEP_STREAM_AUDIO` (default true; false restores the old flush).

**H3. Stash late segments instead of dropping** (sarvam_stream.py:356). A non-empty data
segment arriving with `_fed_since_finalize` false is stashed as `(_monotonic, text)` in
`_late_segment` instead of discarded. `_finalize()` consults the stash first: a stashed
segment younger than 2.5 s is returned (and cleared) without waiting out the flush timeout.
Older stash entries expire. Echo residue ('' segments) still dropped. This turns the RC5
loss AND most RC4 empty-finalizes into instant wins (no 2 s timeout, real text returned).

**H4. Consumed-episode empties don't count as unrecognized** (pipeline.py STT worker).
When a finalize returns `''` and the previous *accepted* transcript finished < 3 s ago with
over-coverage (`last_covered_seconds` > its own segment's duration), skip silently — do not
bump the unrecognized-speech streak, do not queue the "couldn't hear you" repair.

**H5. Continuation-hold on dangling endings** (conversation/pipeline merge window). Before
the idle-skip, strip trailing daṇḍa/punctuation and check the last token against a
continuation-cue set (hi: जो कि तो और या पर लेकिन यानी मतलब बेसिकली से का की के में; en: and,
but, so, or, because, like, basically, mean; gu: અને, પણ, કે, એટલે + romanized ane/pan/ke/etle).
If it matches, hold the merge window (existing `CONTINUATION_WINDOW_MS`-scale wait ~1200 ms)
so the resumed speech merges into one turn instead of racing the LLM.
Knob: `MERGE_HOLD_CONTINUATION` (default true).

**H6. Allowlist "हेलो"** (+ variants हैल्लो / हल्लो / हॅलो) in `_greetings`
(pipeline.py:1356) so a bare hello always reaches the repair logic.

**H7. Watchdog replays the question, not the essay** (repair layer). When the dead-air
reprompt's replay text exceeds ~90 chars, replay only the final sentence (last
sentence-split on `।?.!`), keeping the apology prefix. Applies to the same §13 replay path,
all agents.

## 4. Fixes — Part C (capture ownership + state)

**C1. Arm at ask time, deterministically** (conversation.py). After every agent response is
committed (`_store_last_response`), scan it for an email-ask: response mentions an
email keyword (`email`, `ईमेल`, `ઈમેલ`, `e-mail`, `मेल`) AND (contains `?` or an ask verb
बता/बताइए/भेज/share/give/spell). If the agent has an email-typed slot that is missing →
set `_capture_asked_slot = name` (does NOT touch `asked_count`/max_asks — fatigue budget
still belongs to the scheduler). `_refresh_capture_slot` arms on pending OR asked_count≥1
OR `_capture_asked_slot`. Release only on: slot confirmed, slot unresolved-capped, or
call stage moves past with slot skipped — **not** when a rejection clears pending.
Knob: `CAPTURE_ASK_ARM` (default true).

**C2. Engine owns every email-ish turn while armed** (conversation.py `_handle_capture_turn`):
- Payload/partial classification unchanged, plus: a turn *containing* any email evidence
  (an `@`, an operator word, a letter-name run, or ≥2 single-letter tokens) mixed with other
  speech is still a payload/partial turn — normalize the evidence portion.
- Progress queries while pending ("नोट कर लिया?", "कितना नोट किया?", "did you note") →
  deterministic status readback template (re-reads pending spelled), never the LLM.
- The LLM never speaks an email: while armed, `_validate_response` strips/replaces any
  email-regex match in an LLM response — if a pending value exists, substitute the engine's
  spelled readback template; otherwise drop the sentence containing the address. Also inject
  one system-prompt line while armed: *"Email capture is engine-owned: never state, read
  back, or confirm an email address; never ask the caller to spell it slowly."* This covers
  **existing** agents at runtime (the 07-30 Copilot doctrine only reached newly built ones).

**C3. Confirm/reject requires readback context** (conversation.py):
- `_bare_confirm`: every stripped token ∈ affirmation vocab (हाँ, हां, जी, सही, है, ठीक, हैं,
  yes, yeah, ok, okay, correct, right, sahi, theek, barobar, હા, બરાબર) — OR ≤3 tokens as
  today. Mirror rule for negation (नहीं, नो, no, नहि, ना, गलत, wrong, nahi).
- Bare yes/no resolves the pending slot **only when the engine's last utterance was a
  readback/confirm question for that slot** (track `_capture_awaiting_confirm`, set on
  yielding readback/status templates, cleared on any other agent output). Otherwise a bare
  yes/no routes normally (it's answering whatever the agent actually asked).
- Once a slot is caller-confirmed, later negations never discard it (corrections with a new
  payload still re-enter readback).

**C4. Capture window only in spelling context** (pipeline conversation-layer gating). The
2000 ms merge window, follow-on loop, forced en-IN STT and doubled barge threshold apply
only when `awaiting_payload or awaiting_confirm` (i.e. the engine's last utterance was the
ask, a re-ask, or a readback). Armed-but-digressing turns get normal timing. Kills RC15.

**C5. Extraction hygiene while capturing** (conversation.py):
- `_spawn_slot_extractor`: return immediately while capture is armed **and**
  awaiting payload/confirm (spelling turns are noise; RC12).
- Echo guard (RC13): apply only when the caller's turn is Latin-script; if the turn contains
  Devanagari/Gujarati codepoints, skip the guard (cross-script comparison is meaningless).
- Name protection: `name` writes from source=llm are rejected while capture is armed
  (state_manager merge gate) — a spelling fragment can never become the caller's name.

**C6. Direct answers may update filled slots** (conversation.py + state_manager). Track
`_last_asked_slot` from the same response-scan as C1 generalized to all slots (slot display
name/question keywords + `?`). When the very next caller turn extracts a value for exactly
that slot, merge with overwrite allowed (`source="answer"`), non-contact slots only.
Fixes RC14 ("आज। 4 बजे।" updates `preferred_time`), keeps fill-only immortality everywhere else.
Knob: `SLOT_ANSWER_OVERWRITE` (default true).

**C7. Normalizer upgrades** (contact_capture.py, all pure-function + corpus-tested):
- Multi-word operators: `एट द रेट`, `at the rate`, `एट दी रेट`, `एट रेट` → `@` (substituted
  before tokenization).
- Full A–Z Devanagari letter-name map (ए बी सी डी ई एफ जी एच आई जे के एल एम एन ओ पी क्यू आर
  एस टी यू वी डब्ल्यू/दब्यू एक्स वाई जेड/ज़ेड + common ASR variants).
- Pre-@ assembly: when a token already contains `@`, everything before it that normalizes to
  letters/dots/digits joins the local part (`angelo . pa knaan@gmail.com` →
  `angelo.pa.knaan@gmail.com`), instead of keeping only the @-token.
- Corpus additions from this call: `पंज लो आ। डॉट। अनन एट द रेट। gmail.com`,
  `नहीं नहीं नहीं ए एन जी ई एल ओ। डॉट। पी ए। knaan@gmail.com`, `मनन एट द रेट।` (partial),
  `आंदलो। डॉट।` (partial), `5 लोड। पी ए एन। एन ए एन।` (partial).

## 5. Knobs

| Knob | Default | Restores |
|---|---|---|
| `BARGE_KEEP_STREAM_AUDIO` | true | false → old reset_turn at barge flush |
| `MERGE_HOLD_CONTINUATION` | true | false → idle-skip regardless of ending |
| `CAPTURE_ASK_ARM` | true | false → arm only on pending/asked_count (07-30 behavior) |
| `SLOT_ANSWER_OVERWRITE` | true | false → strict fill-only |
| (H1/H3/H4 ride existing `ENABLE_STREAMING_STT`; C2–C5 ride existing `CAPTURE_MODE`) | | |

## 6. Test plan (all offline, stub_llm)

- **Stream accounting** (fake stream provider): finalize-consumed double-flush → second
  finalize returns stash, no unrecognized bump; late-segment stash returned within TTL,
  expired stash dropped; covered-seconds plausibility passes 14-words/1440 ms when
  covered=4.5 s and still rejects 14-words/1440 ms when covered=0 (batch).
- **Barge keep-audio**: `_flush_pipeline_queues` no longer calls reset_turn (flag on), does
  when flag off; epoch bump unchanged.
- **Continuation hold**: `हम बेसिकली जो।` → hold; `हम काम करते हैं।` → no hold; en/gu cues.
- **हेलो allowlist**: 600 ms `हेलो।` passes the single-word gate.
- **Capture arming**: agent response "क्या आप मुझे अपना ईमेल एड्रेस बता सकते हैं?" arms
  without note_asked; rejection keeps it armed; confirm/cap releases.
- **Ownership**: armed + LLM response containing `foo@bar.com` → address never in yield;
  progress query → status readback; "हाँ सही है सही है।" confirms pending after readback;
  bare "नहीं नहीं" with NO preceding readback routes normally and does NOT discard;
  post-confirmation "नहीं नहीं" never unsets.
- **Extraction hygiene**: no name/llm writes during payload turns; Devanagari caller turn
  skips echo guard (Zoom fills); "आज। 4 बजे।" after a time-question overwrites 'Now'.
- **Normalizer corpus** (§C7 additions) end-to-end through `drive()`: the live call's exact
  sequence converges to `angelo.pa.knaan@gmail.com`-class assemblies with spelled readback.
- Full regression: existing 749 tests stay green.

## 7. Accepted risks / out of scope

- Brand-word mishears pre-pin ("plus call count") — acoustic; pin now active by turn 3;
  revisit only if it recurs post-H fixes.
- No auto-transliteration of Devanagari *word* tokens into email local parts (आंदलो →
  "angelo" is a guess; the spelled path is the correct recovery).
- Sarvam streaming server-side stitching granularity — we compensate client-side; a
  `speech_to_text_streaming` per-segment timestamps API would obsolete H3/H4 if Sarvam ships it.
- The `opening handshake failed` ERROR every 20 s is the docker healthcheck probing :9001 —
  cosmetic, pre-existing, not the Sarvam stream (verified by cadence and by stream
  reconnect logs succeeding).

## 8. Rollout

1. Part H (hearing) — deployable alone; kills I2/I3/I4-audio-loss on every agent.
2. Part C (capture/state) — deployable second; kills I5–I8.
3. Restart runtime with 0 established :9001 connections; verify markers:
   `[STT:Stream] late-segment stash`, `[CAPTURE] armed (ask)`, `[CAPTURE] awaiting-confirm`,
   absence of `Rejecting implausible` on multi-chunk streams, `[STATE] … source=answer`.
4. RUNBOOK §21; commit on `Bettercall`.
