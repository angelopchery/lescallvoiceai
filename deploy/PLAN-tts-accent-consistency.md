# PLAN — TTS accent consistency (agent speaks the accent of its reply language)

**Date:** 2026-07-30 · **Status:** IMPLEMENTED 2026-07-30 (P0-P6; RUNBOOK §19
is the operational reference; P4's optional per-chunk damping freeze and the
P5 audition of existing agents' voices deliberately not done)
**Symptom (user report):** the agent's accent changes mid-call, and English replies
often come out in a heavy Hindi / "rural Indian" accent. The agent must speak in
the accent of the language it is actually speaking.

---

## 1. How Sarvam bulbul:v3 actually controls accent (docs research)

- `target_language_code` (en-IN / hi-IN / gu-IN …) primarily drives the
  **pre-TTS text-normalization frontend**, but Sarvam's own consistency guide
  names **"sending text in one language with a different target_language_code"
  as the most common cause of mispronunciation and unnatural prosody**. English
  text sent as `hi-IN` = English words through the Hindi frontend — exactly the
  reported heavy accent.
- bulbul:v3 is Indian-voiced by design: `en-IN` produces *Indian-accented
  English* (natural, urban). There is no "neutral/US" accent option.
- **Speakers are not equal across languages.** Sarvam ranks speakers per
  language by Critical Error Rate. For **en-IN the recommended speakers are
  `ratan` (M) and `ishita` (F)** — `shubh` (our platform default) is
  recommended for **hi/te/kn/od/ml, not English**. Top overall: `mani`
  (0.00% CER), `priya` (0.13%), `ishita` (0.13%).
- Consistency requires **pinning every parameter on every request** (model,
  speaker, target_language_code, pace). Punctuation and chunk length audibly
  change rhythm; short per-clause chunks vary prosody between chunks.
- `pitch`/`loudness` are bulbul:v2-only. Code-mixed (Hinglish) text is spoken
  natively — no need to split scripts.

## 2. What our pipeline actually does (code trace, verified in logs)

The TTS request (`tts_sarvam.py:571-582`) sends
`target_language_code = _LANG_CODE_MAP[self._language]`, speaker, model, pace.
`self._language` is set **per dequeued utterance** in `_tts_layer`
(`pipeline.py:2229-2231`) from **`conversation.state.language`** — *never from
the text being spoken*. Nothing in the repo inspects the reply's script before
synthesis.

`state.language` has **two writers**:

1. `conversation.py` guarded path (`:1435-1556`) — script detection on the
   **caller's** text, language lock with decay, 2-vs-4-word English-switch
   thresholds, Gujarati-marker suppression.
2. `pipeline.py:1172-1185` — **unguarded**: raw per-segment Sarvam STT
   detection, applied **every turn**, with the comment "Do NOT lock". It runs
   **before** transcript validation, so a transcript later rejected as
   hallucination still flips the accent of the next reply. Several flips show
   in logs as a bare `[STT] Language auto-detected` with **no `[LANG]` line**.

And the **initial value is hardcoded**: `ConversationState.language = "hi"`
(`conversation.py:115`), `ConversationRouter.__init__(default_language="hi")`
(`:688,729`). The bridge (`conversation_call_bridge.py:544`) never passes
`default_language`, and reads `agent_def.languages.default` **only to pick the
speaker** (`:532`). **The agent's configured default language is dead for
language purposes** — every telephony call starts in `hi`.

## 3. Measured evidence (production logs, Jul 25-30)

| Fact | Number |
|---|---|
| Calls whose FIRST audio was `hi-IN` (Jul 27-30) | **87 of 88** — incl. en-only agents |
| `rent_a_boyfriend-outbound` (`supported:[en]`, en-only voice map) greetings sent `hi-IN` | **100%** (pace 1.24 = 1.15×hi-factor) |
| Calls with ≥2 TTS language codes (Jul 27-29) | 10/33, 15/25, 10/19 per day |
| Mid-call accent switch events (Jul 29) | **21** across 9 calls; worst call: **10 transitions** in 54 requests |
| Clearly-English text synthesized `hi-IN` (5 days, live requests) | **28** |
| Text-script vs code mismatch rate (live requests) | 4.3-22.2% per day |
| `'Hello'` greeting replayed from **Hindi-accented cached PCM** | **62 replays**; no en-IN variant exists on disk |
| Pure-English canned lines cached under `hi-IN` | **13** of 42 resolvable cache entries |

Reconstructed call proof points:

- **`bcd9f9ea` (Jul 30):** maya outbound (`default: en`) greeting
  *"Hello! I'm calling from LesCall by DhronAI…"* → `(hi-IN, shubh,
  pace=1.08)`; two turns later the same call speaks `en-IN` — the audible
  accent change is the state flipping from the hardcoded `hi` to detected `en`.
- **`0b19394d` (Jul 29):** the queue race — an English reply was already
  logged `[TTS] Playing` when the unguarded mirror flipped state to `hi`;
  `set_language` runs at dequeue, so *"I completely understand."* went out
  `(hi-IN, pace=1.08)`.
- **`b217ca93` (Jul 29):** the same Hindi sentence synthesized twice 1.3s
  apart — once `en-IN`, once `hi-IN` — two different accents back-to-back.
- **`a75e21ff` (Jul 29):** a **Gujarati carrier hold-announcement** was
  transcribed as caller speech and switched an English-default agent to
  `gu-IN` — three languages in 30 seconds.
- **Same call, two cached accents:** `b217ca93` played the `hi-IN`-cached
  `'Hello'` and the `en-IN`-cached "Sorry, I couldn't hear you…" side by side.

Also verified: the **speaker never changes mid-call** (accent churn is purely
language-code + pace), the **cache key is correct** (model|speaker|pace|lang|
sr|paceCfg|text — poisoned entries were *created* under wrong state, not
collided), and **all parts of one utterance share one code** (pace, however,
varies per chunk: 1.00/0.93/1.08 via long-sentence damping + hi factor).

## 4. Root causes, ranked

1. **Hardcoded `"hi"` session start; `languages.default` never seeds the
   conversation** → every call opens Hindi-accented regardless of config.
2. **TTS language mirrors conversation state, never the reply text** → any
   state/text divergence = wrong accent (28 clearly-English `hi-IN` requests;
   Hindi text under `en-IN` too).
3. **Unguarded per-segment STT mirror (`pipeline.py:1184`)** → accent flips on
   every misdetection (Indian-accented English → `hi-IN` is routine; bare
   "hello" documented misdetector), before transcript validation, bypassing
   every guard in `conversation.py`; also creates the dequeue race.
4. **Poisoned canned-audio cache** → wrong-accent PCM persisted and replayed
   indefinitely (62× Hindi-accented `'Hello'`), and mixed accents within one
   call from cache.
5. **Carrier IVR/hold audio counts as caller speech** for language mirroring.
6. **Pace entanglement** — the `hi` ×1.08 factor rides the same mislabel, so
   wrong-accent English is also 8% fast; per-chunk pace varies within a reply.
7. **Speaker choice (secondary)** — `shubh` is not Sarvam-recommended for
   English (`ratan`/`ishita` are); Copilot writes one speaker (`simran`) for
   every language. Real, but the measured incidents are language-code driven.

Related bug found in passing (out of scope here, record only): stage-goal
fragments and unresolved placeholders spliced into spoken callback greetings
("Humari kal baat hui thi Identify the caller, then …").

---

## 5. Plan (platform-wide — one engine, all agents, current and future)

### P0 — Seed the session language from the agent (trivial, immediate win)
Pass `default_language=agent_def.languages.default` when constructing
`ConversationRouter` in `conversation_call_bridge.py:544`, `run.py:123`, and
the `pipeline.py:311` fallback. Openings/greetings then synthesize under the
agent's own language from turn zero. (`preflight.py`/`boot_check.py` already
pass it.) Kills root cause 1 → the "87 of 88 calls open hi-IN" stat goes to 0.

### P1 — Speak-language follows the TEXT being spoken (the core fix)
New helper on the router (pure function, testable):
`tts_language_for(text) -> "en"|"hi"|"gu"`:
- Devanagari-dominant → `hi`; Gujarati-script-dominant → `gu` (dominance by
  script-character share, mirroring `detect_language_v3`'s existing regexes).
- Latin script: if romanized-Hinglish markers (existing marker sets) → follow
  `state.language`; else → `en`.
- Mixed script (code-mixed Hinglish): dominant script wins; Sarvam handles
  embedded English words natively under `hi-IN`.
`_tts_layer` calls `set_language(tts_language_for(tts_text))` instead of raw
`state.language`. **Stamp the language on the `Response` at enqueue time**
(new `Response.language` field) so the dequeue race (call `0b19394d`) is dead:
language decided when the text is decided, not when the queue drains.
This wholesale fixes the mismatch family: English closings/fallback lines
under `hi` state, Hindi replies under `en` state, `connect_intro_line`
replays, `_lang_response`/`_localized` English fallbacks, the hardcoded
English crash line — whatever text is chosen, it is spoken in its own accent.
Env knob `TTS_LANG_FROM_TEXT` (default **true**), per-agent opt-out
`languages.tts_lang_from_text: false`.

### P2 — One guarded writer for `state.language`
Remove the raw write at `pipeline.py:1184`; route STT-detected language into
the router's guarded path (`conversation.py:1435-1556`) instead — after
transcript validation, honoring the lock, thresholds, and Gujarati-marker
suppression. Add the existing conversation-side hysteresis to what the mirror
feeds (a single low-confidence detection never flips state; `language_probability`
gate ≥ 0.75, same bar as the STT lock). IVR/hold announcements (root cause 5)
stop flipping accents because rejected/implausible transcripts no longer write
state. Env knob `LANG_MIRROR_GUARDED` (default **true**).

### P3 — Cache hygiene (one-time + preventive)
- One-time: purge `data/tts_cache/` after P0/P1 deploy so every canned line
  re-synthesizes under its correct code (130 files, self-rebuilding, zero risk
  — cost is one synth per canned line).
- Preventive: `Cache HIT` log line gains the language code + speaker (today it
  logs neither, making cached accents invisible in audits).
- The key formula itself is already correct — no change.

### P4 — Pace follows the same language as the text
No new code beyond P1: `_effective_pace` already keys off `self._language`, so
once the language matches the text, the ×1.08 hi factor stops applying to
English. Optional polish (decide at implementation): freeze the long-sentence
damping per utterance instead of per chunk so rhythm doesn't wobble within one
reply (chunks currently swing 1.00/0.93/1.08).

### P5 — Voice quality for English (config, needs the owner's ear)
- Recommend auditioning `ratan` (M) / `ishita` (F) for English-default agents
  (Sarvam's en-IN CER picks; `shubh` is a Hindi-tier voice). The existing
  voice-preview flow in Copilot chat can play samples.
- Keep **one speaker per agent across languages** (per-language speaker maps
  exist, but switching speakers mid-call changes the *person*, which is worse
  than an accent shift). Prefer speakers strong across the agent's whole
  language set: `priya`/`ishita` (F) span en+hi+gu; male en+hi has no single
  Sarvam-recommended pick — audition `mani` (best overall CER) vs `ratan`.
- Copilot: when generating `tts_voices`, choose the speaker from the agent's
  language set + gender using a small recommendation table in
  `voice_catalog.py` (instead of hardcoded `simran` everywhere); catalog gains
  per-language quality tiers. Existing agents untouched — config only.

### P6 — Observability + tests + acceptance
- One audit line per utterance: `[TTS] lang=<code> src=text|state|seed
  speaker=<s> pace=<p>` (today the language decision is reconstructable only
  by cross-referencing three log families).
- Tests (patterns already in the repo — pure functions + SimpleNamespace):
  `tts_language_for` unit matrix (en/hi/gu/mixed/romanized); seeding test
  (agent default ≠ hardcoded hi); guarded-mirror tests (low-confidence and
  rejected transcripts don't flip state; IVR-style Gujarati burst doesn't
  flip an en agent); Response.language stamping test; cache-key regression.
- Acceptance criteria: (a) 0 clearly-English lines synthesized `hi-IN` over a
  full day of calls; (b) ≤1 TTS language transition per call unless the caller
  genuinely switched; (c) every call's first audio matches
  `languages.default`; (d) no `Cache HIT` of a code that mismatches its text.

### Rollback
Every behavioural phase has an independent kill switch: P0 is a constructor
arg (revert = one line); P1 `TTS_LANG_FROM_TEXT=false` reverts to
state-driven; P2 `LANG_MIRROR_GUARDED=false` restores the raw mirror; P3-P5
are config/log-only. Compose env changes need `up -d` (restart alone does not
re-read env).

### Scope
All engine changes live in the one shared pipeline/router — every agent,
current and future, inherits them. Copilot changes (P5) only affect newly
generated voice maps. No agent YAML edits required except optional speaker
auditions.

### Suggested order & effort
P0 (minutes) → P3 purge alongside → P1 (core, ~half day incl. tests) →
P2 (careful, touches STT worker; test-heavy) → P4/P6 (small) → P5 (config +
Copilot table, owner auditions voices). P0+P1+P3 alone eliminate every
measured incident class except mid-call flips from misdetection (P2).
