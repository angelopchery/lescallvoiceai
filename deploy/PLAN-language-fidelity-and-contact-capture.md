# PLAN — Language Fidelity + Contact Capture (emails) — platform-wide

**Status: IMPLEMENTED 2026-07-30 (same day) — RUNBOOK §20.** All of Part A
(A1–A6) and Part B (B1–B6) are live. Notable deltas from the plan text:
B6 is engine-level slot provisioning in `build_state_manager` (slots named
"email" are email-typed even when authored `type: text`) instead of a YAML
migration — agents' files stay as their authors wrote them; phone slots
keep their authored merge policy (a forced confirm gate could stall live
outbound flows); contact confirmations require a BARE yes (≤3 words) so
"okay tell me more" can't confirm an email; and the A4 work also fixed a
latent bug — under streaming STT the forced-language script-retry never
re-uploaded audio (it only flushed the already-consumed stream) and now
retries via the HTTP rescue client. Tests:
`tests/test_language_fidelity.py`, `tests/test_contact_capture.py`.

*Original plan (authored 2026-07-30 from live-call evidence) follows.*

Owner report (2026-07-30, after test call `918076562864_20260730_120613`):

1. *"Agent providing output in a different language despite STT taking the correct
   input that too in the correct language"* — agent replied in **Gujarati** to a
   caller speaking pure Hindi; also mid-call en↔hi churn and misdetections in
   recent calls.
2. *"The emails are not being stored properly and due to a little delay in the
   conversations, gaps during emails being spelt etc they are not captured
   precisely."*

Scope requirement: fixes must land in the **shared engine** so they apply to the
LesCall agent, every existing agent, and every future Copilot-built agent.

---

## 0. TL;DR — symptom → root cause → fix

| # | Symptom (from the call) | Root cause | Fix |
|---|---|---|---|
| 1 | Hindi caller, agent replies in Gujarati (twice) | `_STRONG_GU_DEVA_RE` false-positive: virama/matra defeats `(?<!\w)` — "अच्छे" matches marker "छे" | **A1** |
| 2 | One bad turn flips the whole call's language | Unlocked switch on a single word/signal; no evidence threshold | **A2** |
| 3 | "alvin anthony" (a name) flips call to English | Names/emails counted as language evidence; 2-word ASCII switch | **A3** |
| 4 | 29×/day junk-script STT (pa/bn/ml/kn) on short Hindi; 3–4 s retry latency; mishears (', angie?', 'hi there') | Streaming STT runs `language_code="unknown"` for the entire call | **A4** |
| 5 | Both pre-merge AND post-merge replies spoken back-to-back (the two Gujarati lines; double "Sure, am I speaking with Angelo?") | Cont-merge abort can't reach a response already dequeued by the TTS player; repair-replay races the fresh reply | **A5** |
| 6 | Hindi text spoken under `lang=gu` stamp (wrong accent+pace) | Same marker regex feeds `tts_language_for` | **A1** (same fix) |
| 7 | "ben at gmail dot com" → stored `bell@gmail.com`, forever | No email type/validator; background LLM extractor gap-fills a guess; `fill_only` makes first value immortal | **B1, B4** |
| 8 | Caller's correction "ben@gmail.com" acknowledged in speech but state never updated; agent re-asserts bell@ from state | Slot no longer "missing" → extractor never re-runs; no pending/confirm state on email | **B3, B4** |
| 9 | Spelled letters shredded: 'एल दब्यू वाय एल' / 'c o n y bn @' / 'email dot' as 3 separate turns, agent answers each | No capture mode: 550 ms merge window, VAD splits, barge-in mid-spelling, no forced-en, Whisper transliterates letter names to Devanagari | **B2** |
| 10 | Good letter run 'K L W Y N' thrown away | Mixed-script validation rejects the whole transcript because of one junk token ('ਇਹ।') | **A4c** |
| 11 | LLM freelances read-back ("bell@gmail.com", "ben@gmail.com") regardless of state | No deterministic read-back; confirm machinery exists but unprovisioned | **B3** |
| 12 | All of the above will recur on new Copilot agents | Copilot emits `type: text, merge: free, readback: ''` for every slot | **B5, B6** |

---

## 1. Evidence

Canonical call: `recordings/918076562864_20260730_120613/` (conn
`5b3eba18-a790-4549-b6a9-5fd71068c227`, agent `maya_lescall_sdr-outbound`,
12:06–12:10 server time, logs `phase14_runtime/logs/latest/ws_audio_receiver.log`
lines ~7936–12300). All quotes below are from that log unless noted.

### 1.1 The Gujarati flip (Issue 1, primary)

```
12:08:14.319 [STT] 'अच्छे से मैनेज तो कर रहे हैं।'          ← Sarvam: hi-IN, correct
12:08:14.871 [LANG] "अच्छे से मैनेज तो कर रहे हैं।" -> gu (src=script_gu_markers, conf=0.90, locked=False)
12:08:14.871 [LANG] Switching to Gujarati (src=script_gu_markers)
12:08:16.136 [ENGINE] '...' -> 'ચોક્કસ, એ જાણીને આનંદ થયો...'   ← LLM obeyed the gu directive
```

STT was **correct** (hi-IN, Devanagari). The conversation-side detector
`detect_language_v3` re-classified it as Gujarati because of a marker regex bug
(§2 RC1). The LLM then did exactly what it was told. This is not an accent bug
and not an STT bug — it is post-STT language classification.

Empirical regex verification (run 2026-07-30 against the live pattern):

| text | `_STRONG_GU_DEVA_RE` match | correct? |
|---|---|---|
| अच्छे से मैनेज तो कर रहे हैं। | **'छे'** | ✗ false positive (THE incident) |
| पूछो उनसे | **'छो'** | ✗ false positive |
| मुझे अच्छे से पता है | **'छे'** | ✗ false positive |
| छोटा काम है | – | ✓ (docstring's own counter-example passes) |
| केम छो / गेम छो, मजामा? | 'केम' / 'छो' | ✓ genuine Gujarati |

Why: the pattern is `(?<!\w)(?:छे|छो|...)(?!\w)`. In Python `re`, combining
marks (virama ्, matras ा ी ो े ू) are **not** `\w`, so "अच्छे" (अ च ् छ े)
has a phantom "word boundary" between ् and छ → the lookbehind passes
mid-word. This is the same Indic-matras gotcha recorded in RUNBOOK §13 —
this regex predates that lesson.

Same-day secondary effect: the agent's own **Hindi** reply at 12:08:00
('ठीक है ... अच्छे से मैनेज कर पा रहे हैं?') was stamped `lang=gu` by
`tts_language_for` (same marker function) and spoken as gu-IN pace 1.00 —
wrong accent + wrong pace on correct-language text. One regex, three consumers
(`detect_language_v3`, `tts_language_for`, `stt_mirror_consistent`).

### 1.2 Mid-call language churn (Issue 1, secondary)

```
12:09:26.570 [LANG] "alvin anthony" -> en ... Response language: en (prev=en… mid-Hindi call)
             → agent answers a Hindi conversation in English for one turn
12:09:44.590 [STT] Script-retry recovered 'hi there' (caller was speaking Hindi)
12:06:27.631 [STT:Whisper] ', angie?'  (caller said "हाँजी?")
```

`locked=False` on **every** [LANG] line of the call — the language lock never
engages in practice (it was designed around a Sarvam `language_probability`
that does not exist; see memory/RUNBOOK §19). So every turn re-litigates the
language with single-turn evidence: a Latin-script *name* ("alvin anthony",
2 words ≥ the 2-word unlocked threshold at conversation.py:1541) flips the
call to English; a Whisper mishear flips it back. Language-neutral content
(names, emails, spelled letters) is being used as language evidence.

### 1.3 STT script roulette (Issue 1, contributing)

Streaming STT opens once per call with `language_code="unknown"`
(sarvam_stream.py:259–265) and never narrows, even after the call has
stabilized in one language. Today's log alone: **29 script misdetections**
(pa-IN ×8, en ×8, hi ×6, bn ×4, ml ×2, kn ×1) — short Hindi utterances
("हाँ", "हाँजी") coming back as 'ਹਾਂ।', 'শেষ।', 'ಹ್ಞೂ.', 'ശരി'. Each costs a
2 s script-retry, often + a Whisper fallback (measured 3.6–4.3 s
speech→transcript), and the recovery text is frequently an *English mishear*
that then feeds the churn in §1.2. The connect-window force
(pipeline.py:1155) already proves per-segment language forcing works.

### 1.4 Double responses (Issue 1, delivery layer)

```
12:08:16.136 [TTS] Playing: 'ચોક્કસ, એ જાણીને આનંદ થયો...'   ← reply to fragment 1 dequeued
12:08:16.137 [TTS] Playback interrupted                      ← cont-merge abort fires 1 ms later
12:08:16.137 [TURN-TXN] Rolled back provisional turn ...
12:08:16.137 [CONT-MERGE] ... 'अच्छे से...' + 'दिक्कत नहीं...'
12:08:16.459 [TTS:Sarvam] Streaming started for 'ચોક્કસ...'  ← superseded reply synthesizes ANYWAY
   → transcript records BOTH Gujarati replies, 1.1 s apart (02:02.246, 02:03.355)
```

The abort path (pipeline.py:1885–1899) stops playback, cancels generation, and
drains `_tts_queue` — but the superseded Response had already been **dequeued**
by the TTS player task, which carries on into synthesis with no epoch check.
Same shape at 12:06:29/30: the hello repair-replay and the fresh LLM reply to
', angie?' both played ("Sure, am I speaking with Angelo?" ×2). This is the
"cont-merge double-response" known-open item, now with a precise trace.

### 1.5 Email capture (Issue 2) — full trace

```
12:09:13.743 [STT] 'bell www.gmail.com'      ← caller said "ben at gmail dot com"; ITN artifact www.
12:09:14.557 agent: '...आपने बताया "bell@gmail.com", क्या यह सही है?'   ← LLM invented the @ form
12:09:15.045 [STATE] email: None → 'bell@gmail.com' (source=llm turn=14)  ← background extractor gap-fill
12:09:41.008 [STT] 'ben @gmail.com मैंने बोला।'   ← caller corrects
12:09:42.378 agent says "ben@gmail.com" in speech — but NO [STATE] email update ever fires
12:10:02.416 agent: 'शुक्रिया, तो ईमेल एड्रेस bell@gmail.com है...'   ← re-asserts the wrong value FROM STATE
```

Spelling attempt shredding:

```
12:09:57.260 [STT] rejected 'ਇਹ। K L W Y N' — one junk pa-IN token kills a GOOD letter run
12:10:01.040 [STT:Whisper] 'एल दब्यू वाय एल'   ← 4.3 s later, letter names transliterated to Devanagari
12:10:01.661 [STT] 'c o n y bn @'              ← separate turn
12:10:02.177 [STT] 'email dot'                 ← separate turn
   → two agent replies race (one re-asserting bell@, one asking to spell again)
```

Mechanism (all confirmed in code):

- The `email` slot on this agent (and every Copilot agent) is
  `type: text, merge_policy: free, confirm_readback: '', max_asks: 0` — none
  of the v2 safety machinery is engaged, and **no `email` type exists** in
  state_manager.py (types: text|number|money|enum|boolean|phone|date|time|location).
- The only writer is the background LLM extractor
  (conversation.py:2560 `_llm_extract_slots`) with `fill_only=True`
  (state_manager.py:527): the **first** value — here a hallucinated guess —
  fills the slot, and because the slot is then no longer "missing"
  (conversation.py:2551), the extractor never runs for it again. Corrections
  have no path into state. The no-re-ask injection then makes the LLM treat
  the wrong value as confirmed ground truth.
- Bonus evidence the extractor writes junk generally:
  `[STATE] preferred_time: None → 'after office'` (from a discovery answer),
  `[STATE] name: None → 'Ben'` (from the email correction).
- The agent asked the caller to "धीरे-धीरे बोलिए" (speak slowly) — which
  *increases* VAD fragmentation. The system requests behavior it cannot ingest.

---

## 2. Root causes (ranked)

### Issue 1 — wrong output language

- **RC1 (critical, one-line):** `_STRONG_GU_DEVA_RE` (conversation.py:98–100)
  uses `(?<!\w)`/`(?!\w)` boundaries that Devanagari combining marks defeat.
  Weak tier (conversation.py:3826) is worse: bare substring `m in text`.
  Consumers: `detect_language_v3` :3751, `tts_language_for` :4153,
  `stt_mirror_consistent` :4174.
- **RC2:** With the lock effectively never engaging, `detect_language_v3`
  switches the response language on **single-turn** evidence
  (gu markers :1515–1520; 2-word ASCII → en :1535–1551). No hysteresis.
- **RC3:** Language-neutral utterances (proper names, emails, spelled letters,
  filler mishears) are scored as language evidence.
- **RC4:** Streaming STT never narrows from `unknown` (sarvam_stream.py:262
  comment documents the choice) → per-segment script roulette on short
  utterances → latency + mishears; mixed-script transcripts are rejected
  wholesale even when one segment is clean.
- **RC5:** Superseded replies are not reliably killed (no epoch check in the
  TTS player after dequeue) → double-speak on cont-merge and repair-replay.

### Issue 2 — email capture

- **RC6:** No `email` slot type: no validator, no normalizer, no extract
  pattern. LLM guess → state, unvalidated.
- **RC7:** `fill_only` + "missing-only" extraction = first-write-wins forever;
  no pending/confirm lifecycle on contact slots (name has one; email doesn't).
- **RC8:** No capture mode in the audio path: 550 ms
  `TRANSCRIPT_MERGE_WINDOW_MS` (pipeline.py:186) vs. multi-second spelling
  pauses; barge-in + echo-mute active during spelling; STT stays `unknown`
  (letter names get transliterated to Devanagari by the Whisper fallback);
  fragments are answered individually by the LLM.
- **RC9:** Read-back and confirmation are freelanced by the LLM instead of the
  deterministic v2 confirm loop (conversation.py:1992–2010) + `format_readback`
  (state_manager.py:97) that already exist.
- **RC10:** Copilot `_default_definition`/slot generation emits unprovisioned
  slots (`text/free/''/0`), so every new agent re-inherits RC6–RC9. Existing
  agent YAMLs are the same.

---

## 3. Part A — Language fidelity (Issue 1)

### A1 — Fix the Gujarati-marker matching (RC1) — *smallest, highest-value*

`conversation.py`:

- Replace `(?<!\w)` / `(?!\w)` in `_STRONG_GU_DEVA_RE` with Devanagari-block
  boundaries: `(?<![ऀ-ॿ])` and `(?![ऀ-ॿ])`. A marker then
  only matches at a *real* word edge (space/punct/BOS), never after a matra or
  virama. Verified: kills अच्छे/पूछो false positives, keeps केम छो / गेम छो.
- Compile the weak-tier marker list into one regex with the same boundaries
  (replace the `m in text` substring loop at :3826).
- `tts_language_for` and `stt_mirror_consistent` inherit the fix automatically
  (they call the same function) — re-run `test_tts_accent.py` unchanged.

### A2 — Evidence-density language switching (RC2) — one scorer, all pairs

*(Design revised 2026-07-30 after owner review: word evidence within the
utterance IS the switching mechanism; a second turn is only a fallback for
the genuinely ambiguous band — blanket two-turn hysteresis rejected because
the agent's held-language reply discourages the confirming turn.)*

Replaces the `single_strong` marker fast path (conversation.py:1478) AND the
2/4-ASCII-word English switch (:1535–1551) with one uniform per-turn scorer
covering every pair en/hi/gu in both directions.

**Per-turn scoring** (pure Python, <1 ms). Tokenize the transcript; each word
casts one vote:

| word shape | votes for |
|---|---|
| Gujarati-script word | gu |
| Devanagari word matching a strong gu marker (A1-fixed regex) | gu |
| any other Devanagari word | hi |
| Latin word on the English function/content list ("i","is","can","the","want","need","please",…) | en |
| Latin word in `_HINDI_PATTERNS` / `_GUJARATI_PATTERNS` (romanized) | hi / gu |
| everything else — names, digits, spelled letters, emails, unknown Latin tokens | **neutral, no vote** |

`density(L) = votes(L) / total_votes`, neutral words excluded from the
denominator.

**Decision per turn:**

- **Tier 0 — the turn doesn't vote** (definition in A3): capture mode /
  pending contact slot, Whisper- or script-retry-recovered transcripts, or
  zero voting words ("alvin anthony" → all neutral). Language untouched.
- **Tier 1 — instant switch, no waiting:** either
  (a) explicit request — `language_switch` intent, ANY supported target
  language, any phrasing/script, both directions (existing
  `_extract_requested_language` path; verify reachability for every pair,
  not just the Gujarati-centric branches); or
  (b) `density(target) ≥ 0.6` AND `votes(target) ≥ 2`; for en additionally
  ≥1 vote must be a *function* word (blocks name/brand strings). Covers the
  real cases in one turn: "केम छो" (2/2), a full Gujarati-script sentence,
  "i need pricing details".
- **Tier 2 — confirmation band (rare, the only place a second turn exists):**
  exactly one vote for the target, or density in [0.3, 0.6): hold the
  current language and arm `pending_switch(L)`; the next voting turn with
  ≥1 vote for L completes the switch; a turn voting the current language
  disarms it.
- **Otherwise stay.** Conflicting evidence ("yes yes. oh haanji haanji." —
  2 en vs 2 hi) stays: code-mixed filler is not a switch.

**Oscillation guard:** after any non-explicit switch, non-explicit switching
is suspended for 1 turn (explicit requests always honored) — bounds
worst-case churn to once per 2 turns.

**Verified against the incident call:** "अच्छे से मैनेज तो कर रहे हैं।"
scores gu density 0 after A1 — and even with the buggy regex, 1 gu vote vs
~6 hi votes = density 0.14 → stays Hindi. A1 and A2 each independently
prevent the incident. Interplay with A4: under pinned hi-IN STT, genuine
Gujarati speech still arrives as marker-dense Devanagari → Tier 1 fires.
Known residual risk (accepted): a whole-utterance STT hallucination in a
wrong script can pass Tier 1 once; A4's pinning + salvage shrink its
frequency, and the oscillation guard limits the damage to one turn.

### A3 — Language-neutral turns don't vote (RC3)

Skip language re-evaluation (freeze `state.language`, still record history as
`None`/skip) when:

- a contact-type slot is pending/being captured (B2's capture mode, name
  pending included) — "alvin anthony", "ben @gmail.com" turns;
- the utterance is ≤2 words AND matches a proper-noun/ack shape (no
  language-pattern hits on either side) — "yes", "okay", "alvin anthony";
- the transcript came from the Whisper fallback or script-retry recovery
  (tag the TranscriptItem with `stt_source`) — mishears like "hi there",
  ", angie?" must not steer language. STT-source tagging is a small pipeline
  change: pass `source` through `_transcribe()` → TranscriptItem.

### A4 — Tame the STT script roulette (RC4)

`app/voice/stt/sarvam_stream.py` + pipeline STT worker:

- **a) Language pinning after stabilization.** Once the conversation language
  has been stable for N=2 turns (state.language unchanged and consistent with
  transcripts), submit segments with the mapped BCP-47 code instead of
  `unknown` (docs confirm `language_code` is a supported streaming param; the
  connect-window force already does this for en-IN). Re-open/re-negotiate to
  `unknown` for 2 turns after: an explicit switch request, or 2 consecutive
  script-inconsistent results (escape hatch so a genuine mid-call switch is
  never locked out). Note saaras codemix handles embedded English under
  hi-IN — pinning hi does NOT break Hinglish.
  - Implementation note: the current design holds ONE `unknown` WS for the
    whole call (reconnect risk documented at :260–263). Prefer per-segment
    `language_code` on submit if the SDK supports it; otherwise reconnect at a
    turn boundary (never mid-turn), reusing the warm-reconnect path the
    stream already has for errors.
- **b) Segment-level salvage on mixed-script finalize.** Before rejecting a
  multi-segment transcript for script misdetection, drop only the
  junk-script *tokens/segments* and re-validate the remainder:
  'ਇਹ। K L W Y N' → 'K L W Y N' (usable!), 'অ. ਹਾਂਜੀ। जी बताओ।' → 'जी बताओ।'.
  Saves the 2 s retry + 2 s Whisper round-trip in the common case.
- **c) Whisper fallback language hint.** Pass the stabilized conversation
  language as `language` to the Whisper call so "हाँजी?" stops coming back as
  ', angie?'. (Whisper API accepts ISO-639-1.)

### A5 — Single-delivery invariant: a superseded reply must never sound (RC5)

`pipeline.py` TTS layer:

- Add a monotonically-increasing **delivery epoch**. Every abort path
  (cont-merge :1885–1899, barge-in, repair-replay supersede) increments it.
  The TTS player captures the epoch when it dequeues a Response and re-checks
  **immediately before starting synthesis** and **before each flush to the
  sink**; stale epoch → drop silently. This closes the 300 ms dequeue→synth
  window the 12:08:16 trace exposed.
- Repair-replay ("hello?" → replay last utterance) must go through the same
  epoch: if a fresh reply for a merged/parallel turn is already in flight,
  replay is skipped (delivery guard extension — the 12:06:29/30 double).
- Telemetry: log `[TTS] dropped stale response (epoch X < Y)` — grep-able
  acceptance signal.

### A6 — Output-language conformance telemetry (cheap safety net)

After `_validate_response`, compare the response's dominant script against the
directed response language; on mismatch log
`[LANG] RESPONSE-MISMATCH directed=hi got=gu` (warning only, no behavior).
This turns any future regression of this class into a one-line grep instead of
a caller-reported incident. (The 12:08 incident would have printed it twice.)

---

## 4. Part B — Deterministic contact capture (Issue 2)

Design principle: **the LLM asks; the engine captures.** Free conversation
stays LLM-driven, but once the dialogue needs a verbatim artifact (email,
phone), capture/validation/read-back/confirmation run deterministically in the
engine, and the LLM only receives the confirmed result.

### B1 — `email` slot type: validator + spoken-form normalizer (RC6)

`state_manager.py`:

- New `type: "email"` with `_SLOT_EMAIL_RE` validation and a
  **spoken-form normalizer** applied before validation:
  - operator words (multilingual): "at"/"at the rate"/"एट"/"@" → `@`;
    "dot"/"डॉट"/"." → `.`; "underscore", "dash/hyphen", "capital …";
  - strip ASR/ITN artifacts: leading `www.` glued by Sarvam ITN
    ("gmail dot com" → "www.gmail.com" — seen live), trailing punctuation,
    spaces inside the local part;
  - letter-name maps: English ("double u"→w, "why"→y…) AND Devanagari
    letter-name transliterations from the Whisper path
    (एल→l, दब्यू→w, वाय→y, बी→b, सी→c …) — the live call produced both;
  - domain dictionary snap: edit-distance ≤1 on
    {gmail, yahoo, outlook, hotmail, rediffmail, icloud, proton} + TLD fixes
    (".con"→".com", "male"→"mail").
- `merge_state` gains **validator gating**: a typed slot only accepts values
  that pass its validator, from ANY source. `source="llm"` writes to
  email/phone-typed slots are rejected outright (the deterministic assembler
  is the only writer) — kills the "bell@gmail.com from a guess" class.
  Applied generally: type `time`/`date` values must parse — also blocks
  `preferred_time='after office'`.

### B2 — Engine capture mode (RC8)

New pipeline+conversation state, entered deterministically when the agent's
just-spoken question asks for a pending contact-type slot (detectable: stage
`collect` contains a missing email/phone slot AND the outgoing response
matches the slot's question/ask registry — same mechanism `mark_pending` uses
for name). While in capture mode (bounded: exits on completed capture,
confirmation, or 45 s):

- **Endpointing:** effective merge window for caller speech ⇒
  `CAPTURE_MERGE_WINDOW_MS` (default 2000 ms) instead of 550 ms; fragments
  accumulate in a capture buffer instead of dispatching to the router
  per-fragment. The agent stays silent between fragments (no LLM call per
  fragment — the 3-fragments→2-racing-replies failure disappears).
- **STT:** force `en-IN` per segment (emails/letters are Latin; prevents both
  the Devanagari letter-name path and junk-script roulette); suppress the
  Whisper fallback's language auto-detect the same way (A4c).
- **Barge-in:** raise the barge-in threshold/disable interrupt during the
  agent's short capture prompts (the caller talking over "जी, बताइए" is
  expected); echo-mute unchanged (agent prompts are short).
- **Assembly:** on buffer settle (silence > window) run the B1 normalizer over
  the joined fragments → candidate. Valid → write slot via `merge_policy:
  confirm` (pending) and proceed to read-back (B3). Invalid → one deterministic
  re-ask with targeted phrasing (see B3 language note), asking the caller to
  say it **"in one go"** — never "slowly" (evidence §1.5: slow = fragmented).
- Language of capture prompts: the *conversation* language (state), from a
  canned per-language template set — capture-mode turns are exempt from
  language re-evaluation (A3), so spelling can't flip the call language.

### B3 — Deterministic read-back + confirmation, capped (RC9)

Wire the existing machinery, don't rebuild it:

- Provision contact slots `merge_policy: confirm` → value lands in
  `_pending_fields` (state_manager.py:543–551), unusable as ground truth until
  the caller's explicit yes (generic confirm loop conversation.py:1992).
- New `confirm_readback: "spelled"` mode in `format_readback`
  (state_manager.py:97): local part spelled character-by-character with
  separator pauses, domain spoken whole —
  `"b. e. n. — at — gmail. dot com."` Bulbul v3 has **no SSML** (docs
  confirmed); punctuation-separated characters + optional pace damp
  (existing `TTS_PACE_*` machinery) is the documented workaround. Read-back
  text is **template-generated from the slot value** — the LLM never
  paraphrases it; inject the confirmed value into the LLM context only after
  confirmation.
- **Caps + fallback (per-slot, schema-driven):** `max_asks: 2` (existing
  field). On exhaustion: mark the slot `unresolved` (never a low-confidence
  guess), say the per-agent fallback line ("No problem — I'll note it and
  we'll sort the email over WhatsApp/SMS follow-up"), flag
  `collected_data.email_unresolved=true` so the intel pipeline surfaces it for
  human follow-up. The generic confirm loop's **2-turn auto-accept
  (conversation.py:2007–2010) must be disabled for contact-typed slots** —
  auto-accepting an unconfirmed email is how wrong values become permanent.
- Corrections: `merge_policy: confirm` + validator-gated writes make a later
  valid email (with correction markers per `_CORRECTION_MARKER_RE`, or while
  pending) re-enter pending→read-back→confirm. The bell@/ben@ deadlock cannot
  recur: nothing is ground truth until confirmed, and confirmed values remain
  correctable via the correction-marker path.

### B4 — Fix extractor immortality (RC7)

`conversation.py` `_llm_extract_slots` / `_maybe_extract_slots`:

- Contact-typed slots (email/phone) are **excluded from LLM extraction
  entirely** (deterministic-only writers). For remaining slots, keep
  `fill_only=True` but add: when the current utterance carries a correction
  marker AND names/patterns a filled slot, allow a validator-passing overwrite
  through the normal (non-fill-only) merge path with an audit entry.
- `_scrub_hello_values`-style guard extended: never let the extractor write a
  value that appears **only** in the agent's own last utterance (the LLM
  echoing the agent's read-back back into state).

### B5 — Copilot: provision the machinery for every NEW agent (RC10)

`dashboard/agent_copilot_api.py`:

- Slot generation/`_enforce_invariants`: any slot named or typed
  email/phone/contact gets `type` set correctly, `merge_policy: confirm`,
  `confirm_readback: spelled` (email) / `digit_groups_2` (phone),
  `max_asks: 2`, and a per-language `reask_question` filled from templates.
  (Fill loop lives in `_enforce_invariants`, in place — NOT
  `_normalize_definition`; see memory note.)
- System-prompt doctrine (same pattern as the openings doctrine): "The engine
  captures emails/phones — write stage goals so the agent asks ONCE and never
  instructs the caller to 'speak slowly'; never promise the agent will
  read back letter-by-letter (the engine does it)."
- Cross-surface lint: stage `collect` includes a contact slot ⇒ that slot must
  be provisioned (type+confirm+readback) — mirrors the existing sched-rule
  lints.

### B6 — Existing agents: one-time migration (RC10)

Additive enrichment pass over `calling-agent/agents/*/agent.yaml` upgrading
contact-type slots to the B5 provisioning (script under `deploy/` or a
registry boot-enrichment following the established **enricher CREATE-ONLY /
boot-check gate** pathway — enrichment never in `_normalize_definition`).
Registry stat-reload picks changes up without restart. Engine defaults are
written so an **unprovisioned** legacy slot still gets validator gating and
capture mode when `type`/name indicates email or phone (belt-and-braces for
agents that miss migration).

---

## 5. Env / schema knobs (all default-on, each independently revertible)

| Knob | Scope | Default | Purpose |
|---|---|---|---|
| `LANG_EVIDENCE_SWITCH` | env | `true` | A2 evidence-density scorer (false = today's single-signal switch) |
| `STT_LANG_PIN` | env | `true` | A4a streaming language pinning after stabilization |
| `STT_SEGMENT_SALVAGE` | env | `true` | A4b mixed-script partial salvage |
| `TTS_DELIVERY_EPOCH` | env | `true` | A5 stale-response drop |
| `CAPTURE_MODE` | env | `true` | B2 engine capture mode master switch |
| `CAPTURE_MERGE_WINDOW_MS` | env | `2000` | B2 spelling endpoint window |
| `languages.evidence_switch` | agent | inherit | per-agent A2 opt-out |
| `slots[].merge_policy/confirm_readback/max_asks` | agent | via B5/B6 | per-slot capture behavior |

Latency budget notes (owner's standing concern): A1/A2/A3 are pure-Python
decision changes (<1 ms). A4 **reduces** latency (removes 2–4 s
retry/fallback chains). A5 removes double audio (perceived latency win). B2
adds intentional wait *only* inside capture mode (the pause is the feature);
normal turns are untouched — capture mode is entered by slot-ask detection,
never on ordinary turns.

---

## 6. Test plan

New `tests/test_language_fidelity.py`:

- RC1 regex matrix (the §1.1 table verbatim — अच्छे/पूछो false-positive pins,
  केम छो/गेम छो true-positive pins) against strong AND weak tiers.
- A2 scorer matrix: incident sentence stays hi (even with 1 buggy marker
  vote → density 0.14); "केम छो" → instant gu; full Gujarati-script
  sentence → instant gu; "i need pricing details" → instant en;
  "alvin anthony" → zero votes, no switch; "yes yes. oh haanji haanji." →
  conflict, stays; Tier-2: single-marker turn holds, +1 supporting vote next
  turn switches, current-language vote disarms; explicit request switches
  instantly for every pair en↔hi↔gu; oscillation guard suspends
  non-explicit switching for 1 turn.
- A3: "alvin anthony" (2 words) during pending-name/capture → language
  unchanged; Whisper-tagged transcript never votes.
- A5: superseded Response dequeued-but-not-synthesized is dropped
  (SimpleNamespace pipeline harness per `test_tts_accent.py` pattern).

New `tests/test_contact_capture.py`:

- B1 normalizer corpus — seed with the live call's actual ASR outputs:
  `"bell www.gmail.com"`, `"ben @gmail.com मैंने बोला।"`,
  `"एल दब्यू वाय एल"`, `"c o n y bn @"` + `"email dot"`, plus clean forms.
- Validator gating: `source="llm"` write to email slot rejected;
  `preferred_time='after office'` rejected for a time-typed slot.
- Confirm lifecycle: pending → yes → confirmed; pending → correction →
  re-pending with new value; max_asks exhaustion → unresolved flag, never a
  guess; **no auto-accept** for contact slots.
- format_readback "spelled" mode output shape.
- Copilot: generated agent's email slot is fully provisioned; lint fires on
  unprovisioned contact slot in `collect`.

Suite invariants: full backend suite (689 at last count) stays green;
`stub_llm` for anything touching the router; `test_kaapd_e2e.py` remains the
acceptance floor.

---

## 7. Rollout order

1. **A1** (regex) — one-line class of fix, immediately deployable alone; kills
   the reported Gujarati incident.
2. **A2 + A3** (evidence-density scorer + neutral turns) — conversation.py only.
3. **A5** (delivery epoch) — closes double-speak; independent of A2.
4. **A4** (STT pinning/salvage/Whisper hint) — biggest latency win; needs the
   most care (streaming reconnect semantics) → own canary call.
5. **B1 + B4** (email type, validator gating, extractor exclusion) — state
   layer only, safe before capture mode exists.
6. **B2 + B3** (capture mode + read-back/confirm) — the big UX change; canary
   on the LesCall agent with a scripted email test call before enabling
   platform-wide.
7. **B5 + B6** (Copilot provisioning + migration) — after B2/B3 verified live.

Each step: deploy via compose `up -d` (restart ≠ env apply), verify with a
test call + the new grep-able log lines (`[LANG] RESPONSE-MISMATCH`,
`[TTS] dropped stale`, `[CAPTURE]`), check
`ss -tn state established '( sport = :9001 )'` before restarting runtime.

---

## 8. Explicitly out of scope (documented, not forgotten)

- SMS/WhatsApp link as email-capture alternative — no messaging integration
  exists on the platform today; listed as the natural next step after B3's
  `email_unresolved` flag creates the demand signal.
- Sarvam Pronunciation Dictionary v2 (STT) — docs suggest REST-only; worth a
  support ticket for streaming availability, not load-bearing for this plan.
- DTMF capture — inapplicable to emails; phone numbers already have
  digit-groups readback.
- IVR/hold-music turns polluting the language path (RUNBOOK §19 out-of-scope
  item) — A3's neutral-turn rule reduces exposure but full IVR discrimination
  stays separate.
- Fixing Sarvam's ITN ("gmail dot com"→"www.gmail.com") upstream — B1
  normalizes it locally instead.

---

## 9. Open questions for the owner

1. **B3 fallback line** wording + whether an unresolved email should trigger an
   automatic follow-up task in the intel pipeline (it will be flagged either
   way).
2. **A4a pin aggressiveness:** pin after 2 stable turns (recommended) or only
   after an explicit caller language choice? Pinning earlier saves more
   latency but relies on the A4a escape hatch for genuine switchers.
3. Should capture mode also cover **names** (the existing name flow already
   has pending/confirm but not the endpointing/STT treatment)? Recommended:
   yes, later, once email capture proves out.
