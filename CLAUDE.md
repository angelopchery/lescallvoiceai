# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This System Does

LesCall (Dhron AI) is a multi-tenant platform for AI voice agents that make and answer real phone calls for Indian businesses in English / Hindi / Gujarati. Calls arrive or leave via FreeSWITCH → WebSocket → the AI pipeline; every tenant agent is pure data (`calling-agent/agents/<slug>/agent.yaml`, built by the Copilot) and every call comes back as searchable intelligence. The original Pramukh Group real-estate agent (6-stage flow `GREETING → DISCOVERY → PROJECT_MATCH → INVITE_VISIT → CONFIRMATION → CLOSING`) is one tenant family, not the product. **Platform map: `docs/PLATFORM.md`** (architecture, surfaces, tunables, testing tools, measured numbers, known gaps) — read it before a broad inspection; this file holds the operating doctrine.

## Running the System

### Restart the telephony runtime — IT IS A CONTAINER
```bash
docker restart lescall-runtime     # THE way to apply voice code changes
```
`lescall-runtime` runs `deploy/runtime_entry.sh`, which starts BOTH
`socket_server.py` (port 9000 ESL + 9004 playback) and `ws_audio_receiver.py`
(port 9001 media WS + AI bridge) on the **host network namespace**, from a
bind mount of this repo — so a plain restart picks up new `.py` files. If
either process dies the container exits and Docker restarts the pair.
Its logs go to `phase14_runtime/logs/docker/`, and its entrypoint points
`phase14_runtime/logs/latest` → `docker`.

⚠ **Do NOT run `phase14_runtime/start_all.sh` on this host.** It starts a
SECOND, competing `socket_server` + `ws_audio_receiver` pair on the same
ports; the container then cannot bind, exits, and enters a Docker restart
loop while the unmanaged host pair serves calls (hit 2026-08-04). `stop_all.sh`
+ `check_runtime.sh` have the same blind spot — `check_runtime.sh` reads
`logs/latest/*.pid`, which the container never writes, so it reports
`[FAIL] ws_audio_receiver PID not found` even when the runtime is perfectly
healthy. Verify with `docker ps` and `ss -ltnp | grep -E ':900[014]'` instead.
The scripts remain useful only on a host where the container is not running.

### Monitor
```bash
tail -f phase14_runtime/logs/docker/ws_audio_receiver.log
tail -f phase14_runtime/logs/docker/socket_server.log
docker ps --format '{{.Names}}\t{{.Status}}'
```
Benign log noise from the healthchecks, on both ports every ~20s:
`InvalidMessage: did not receive a valid HTTP request` (WS 9001) and an ESL
`EOF during handshake at +0.001s` followed by an empty
`CASE R2 — Both dialplan and ESL recordings FAIL` summary block (9000).
Neither is a call and neither indicates a fault.

### Restart the dashboard / Copilot API
```bash
docker restart lescall-api         # applies dashboard/*.py AND calling-agent/.env
```

### AI Pipeline Only (CLI mic, no telephony)
```bash
cd calling-agent
source venv/bin/activate
cd backend
python run.py              # mic input → speaker output
python run.py --web        # + local web console at http://127.0.0.1:8000
python run.py --test       # single-turn test without mic
```

### Environment
Copy `calling-agent/.env` and set:
```
SARVAM_API_KEY=sk_...          # STT + TTS (required)
OPENAI_API_KEY=sk-...          # Whisper fallback STT only
ENABLE_BARGE_IN=true           # interrupt TTS mid-playback
TELEPHONY_MODE=false           # set true when running via FreeSWITCH
ENABLE_CALL_AUDIT=true         # record WAV + transcripts to ./recordings/
```

Document-RAG knobs (all optional; defaults are measured — see
`app/documents/config.py` and RUNBOOK §28):
```
RAG_RETRIEVAL_TOP_K=20         # candidates before re-ranking
RAG_FINAL_TOP_K=3              # ceiling on chunks in the prompt (weak ones are pruned)
RAG_RERANK_ENABLED=true        # BM25 + vector, fused with RRF
RAG_MMR_LAMBDA=1.0             # 1.0 = MMR off; measured worse at every grid point
RAG_ABSTENTION_THRESHOLD=0.25  # NOT the abstention mechanism — see config.py
RAG_SEMANTIC_CACHE_ENABLED=true
RAG_CHUNK_WORDS=250            # changing this needs a re-index to take effect
RAG_EMBED_PROVIDER=gemini      # gemini-embedding-2 on LLM_API_KEY since §57; `openai` = the §28 setup
RAG_EMBED_MODEL=gemini-embedding-2
RAG_EMBED_DIM=768              # changing provider/model/dim → every index reports `stale`
RAG_RELATIVE_FLOOR=0.85        # provider-dependent default (0.6 for openai) — swept, see config.py
DOC_EXTRACT_MODEL=             # document → agent fields; defaults to COPILOT_LLM_MODEL on LLM_BASE_URL
DOC_UPLOAD_MAX_MB=20           # nginx caps the request at 25m
```
Smart-filter + export knobs (`shared/intelligence/config.py`,
`shared/export_store.py`; RUNBOOK §31):
```
INTEL_FILTER_VERIFY_ENABLED=true       # off → the old similarity-only filter
INTEL_FILTER_LLM_MODEL=gemini-3.5-flash-lite   # smallest model on the key
INTEL_FILTER_MAX_CANDIDATES=300        # calls the judge may read per filter
INTEL_FILTER_CONCURRENCY=16            # judgements in flight
INTEL_FILTER_BUDGET_S=120              # whole-pass wall clock, then it STOPS
INTEL_FILTER_MIN_CONFIDENCE=0.55
INTEL_SEARCH_ARM_LIMIT=150             # calls ONE retrieval arm may contribute
INTEL_SEARCH_CHUNK_POOL=4000           # chunks the vector arm reads per scan
GCS_EXPORTS_BUCKET=                    # defaults to GCS_RECORDINGS_BUCKET
GCS_EXPORTS_PREFIX=exports
```

`run.py` validates `SARVAM_API_KEY` at startup and exits immediately if missing.

### Dependencies
```bash
cd calling-agent
pip install -r requirements.txt
```

---

## Architecture

### Process Topology (telephony mode)

```
FreeSWITCH
    │ ESL (port 9000)
    ▼
socket_server.py          ← ESL controller; handles call events, triggers
    │                       playback via validated execute command
    │ WebSocket (port 9001)
    ▼
ws_audio_receiver.py      ← receives L16 PCM16 @ 8 kHz from mod_audio_stream (PCMU branch unused),
    │                       upsamples 8kHz→16kHz, calls call_bridge
    │
    ▼
conversation_call_bridge.py  ← creates one TelephonySession per call;
    │                          wires TelephonySource/Sink + VoicePipeline
    ▼
calling-agent/backend/app/voice/pipeline.py  ← full AI pipeline (see below)
    │
    └── TTS audio → TelephonySink → TCP port 9004 → socket_server.py → FreeSWITCH
```

### AI Pipeline (pipeline.py)

Six independent async tasks (audio/VAD, STT submitter, STT worker, conversation, TTS, silence watchdog) connected by bounded queues:

```
[AudioSource]
    → vad_queue   → [VAD task]        WebRTC pre-gate → FSMN neural VAD (`VAD_ENGINE=fsmn`, `vad_stack.py`) + semantic endpointer; energy VAD is the fallback
    → stt_queue   → [STT worker]      Sarvam primary / Whisper fallback
    → transcript_queue → [ConversationRouter]  deterministic shortcuts first, then LLM
    → tts_queue   → [TTS player]      Sarvam Bulbul HTTP stream
    → [AudioSink]                     speaker (local) or TelephonySink (telephony)
```

- **AudioSource/Sink** are swappable: `LocalMicSource`/`LocalSpeakerSink` for CLI mode; `TelephonySource`/`TelephonySink` for call mode.
- Queue bounds prevent backpressure from propagating upward — a slow TTS never blocks audio capture.
- `ENABLE_BARGE_IN=true` allows the user to interrupt TTS; a cooldown prevents false triggers.

### STT Layer (`app/voice/stt/`)

- **`stt/router.py` — STTRouter**: always tries Sarvam first. If the primary is silent past 0.6s a SECOND Sarvam request races it (`STT_PRIMARY_RETRY_HEDGE_S` — Sarvam hangs are per-request; the duplicate usually lands in ~200ms and keeps the turn on saarika); past 1.0s a Whisper hedge starts in parallel (`STT_HEDGE_DELAY_S`). Whisper only gets a language hint once the caller has established the conversation language (`fallback_hint` from the pipeline) — before that it auto-detects, so Hindi speech comes back in Devanagari, not romanized junk. On HTTP 429, exponential backoff (60s → 300s max) and routes to Whisper. On clean empty transcript (no speech), returns `""` immediately — Whisper is **not** called. The pipeline's `_stt_submitter` starts transcriptions as segments arrive (`STT_PREFETCH_AHEAD`) while the worker consumes results strictly in order — one slow request never serializes the segments behind it.
- **`stt/sarvam.py` — SarvamSTT**: multipart WAV POST to `api.sarvam.ai/speech-to-text`, model `saarika:v2.5` (owner's choice — do not switch to saaras without asking; `SARVAM_STT_MODEL`/`SARVAM_STT_MODE` env to experiment). PCM audio is amplitude-normalized before upload (telephony audio is often quiet). Uses a persistent shared `httpx.AsyncClient`; per-phase timeout caps (`SARVAM_CONNECT_TIMEOUT_S`=3s / `SARVAM_READ_TIMEOUT_S`=5s) keep a hung Sarvam request from stalling the STT worker, and a timeout resets the client so the retry gets a fresh connection.
- **`stt/router.py` — `validate_transcript()`**: rejects hallucinations (YouTube phrases + Devanagari equivalents), unsupported scripts (Korean, Tamil, Malayalam, etc.), Marathi (shares Devanagari with Hindi — caught by specific word markers), filler words, repetition, single-word noise tokens, and function-word-only fragments (quality score < 0.3). Returns `""` to reject; caller skips Whisper fallback on clean empty results.
- **Language locking** (`pipeline.py`): After Sarvam returns `language_probability ≥ 0.75`, the detected BCP-47 code (e.g. `hi-IN`) is locked in `_stt_locked_language` and passed on every subsequent STT call, preventing mid-call language bounce.
- **Audio normalization** (`stt/base.py`): PCM amplitude is boosted before upload to compensate for quiet telephone callers.

### Conversation Engine (`app/voice/conversation.py`)

`ConversationRouter` is the production agent. Key design points:

- **Stage machine**: `CallState.conversation_stage` drives which goal the LLM is given. Stages advance only after explicit conditions are met (e.g. interest confirmed before moving to booking).
- **`CallState`** (in `memory.py`) is the canonical session object — `customer_name`, `customer_phone`, `selected_project`, `selected_location`, `appointment_date/time`, `language`, `conversation_stage`. It is injected into every LLM system prompt so the agent never re-asks confirmed fields.
- **Deterministic shortcuts**: common phrases (hello, bye, thanks) return a canned response instantly, bypassing the LLM. These live **inside `conversation.py`'s `process_stream`**, not in `shortcuts.py` — that module is imported only by `main_loop.py` (CLI mic mode) and has never been on the telephony path (`git log -S"check_shortcut"` on `conversation.py` / `pipeline.py` / `conversation_call_bridge.py` is empty for all history). Debug the branches in `process_stream`.
- **The two opening beats**: a call opens with `response_bank.opening_greeting` (or `outbound.opening_greeting`), and the moment the caller first responds the agent speaks a *second* line — `call_flow.connect.intro_line` when the business set one, otherwise composed from `identity.agent_name` + `identity.company_name`. `spoken_surfaces.resolve_spoken_surfaces()` resolves both by driving the real engine; never infer either from the YAML by eye.
- **Spoken placeholders** (`_substitute_tokens`, RUNBOOK §41): four tokens are
  resolved in **every** authored spoken line, not just the greeting —
  `{contact_name}`, `{current_time}`, `{honorific}` ("Sir"/"Ma'am", standalone)
  and `{title}` ("Mr."/"Mrs.", before a name); Hindi/Gujarati forms are
  automatic. The two gendered ones come from `contacts.gender` and resolve to
  **nothing at all** when that contact has none — the token and the seam it
  leaves are removed, because ~99% of contacts have no gender on file and it
  must never be guessed. Substitution lives in **`_lang_response`**, THE funnel
  for every authored line (closings, canned phrases, objection answers…);
  `_personalize` = substitute + `strip_unresolved_tokens` for surfaces that hand
  a finished line to TTS. **Never strip inside `_lang_response`** — objection
  answers resolve `{trust_proofs}` afterwards. Adding a token means updating the
  engine regex, `agent_copilot_api._LIVE_TOKENS` + `_canonicalize_placeholders`,
  and `spoken_surfaces` (whose preview drives the real engine and would
  otherwise resolve the token away). The model is ALSO told both address forms
  in the prompt band, so its own improvised sentences use them all call.
- **Gender is DATA, for every agent** (`app/voice/address.py`, RUNBOOK §42):
  every line the caller hears passes `_finalize_spoken` — Sir/Ma'am/Mr./Mrs.
  held to `contacts.gender` (a literal "Sir" in a script and a model-improvised
  one included), and **removed entirely when there is no gender**; the agent's
  own first person ("kar raha/rahi hoon") follows its **TTS voice**, never a
  pronouns field (`from_dict` derives it; the Builder field is read-only).
  Choke points: the `process_stream` wrapper, `generate_opening`, the
  watchdog `_raw` wrappers, `memory.spoken_filter`, and `_render_author_line`
  for the stage goal / quoted outbound lines. `{contact_name}` with no name
  drops an identity-question clause — it is never "sir/ma'am". Use
  script-aware boundaries (`_NB`/`_NA`), never `\b`, around Indic text, and
  keep every regender rule anchored on a first-person-only form.
- **Project lock**: first confirmed Pramukh project is stored in `CallState.selected_project` and never overridden — prevents the agent from being steered to a different project mid-call.
- **One project is THE pitch: `knowledge_base.primary_project`** (`{id, mode:
  exclusive|lead, reason}`, RUNBOOK §55). Built for Ravi (Pramukh, the
  Swarit-only agent; born Vikrant the same day): the match rules are a union that the caller's city
  outranks (§54), so they cannot say "pitch Swarit to everyone, keep the
  other 15 projects as answers". With a primary set, `evaluate_match_rules`
  returns it FIRST before any slot is filled; `exclusive` makes it the ONLY
  candidate (the rules stay as data for `lead`/rollback), the knowledge band
  renders it under `★ PRIMARY PROJECT` with its detail keys (`rera`,
  `address`, `amenities`, `layout`, `not_published`… — `catalog._DETAIL_KEYS`;
  `media`/`faq`/`source`/`contact_email` are data-only) and labels the rest
  INFORMATION ONLY, and a caller in another city is told where the primary is
  and asked whether that could work — never that it "is NOT an option". No
  primary → every band is byte-identical. Every NON-recommend stage carries a
  short `━━ PRIMARY PROJECT ━━` band (`primary_reminder_block`), and the
  by-location and STRICT-grounding lines lead with the primary — call 908
  (§55.4) showed the model listing Aura/Sahaj for "a flat in Vapi" when the
  rules sat only in the static band. `deploy/pramukh_swarit_knowledge.yaml`
  is the source object (site scrape + owner brief); the site publishes no
  price, sizes or floor plans, and the agent must never guess them.
- **A confirmed visit is read back ONCE, a name is not a reflex, and a
  closing turn must be able to end the call** (RUNBOOK §55.5, call 909):
  `visit_confirmed_block` / `name_fatigue_block` are per-turn guards next to
  CURRENT GOAL (the static guideline was ignored); `build_tool_declarations
  (include_book=False)` once a visit is fixed or the call is closing, because a
  refused tool-only turn costs a text re-ask whose `[END_CALL]` was ignored —
  it is honoured now when `state.is_closing`. A FILLER never anchors the
  stale-fragment rule, an absorbed fragment survives the playing reply's
  completion, and speech over `STALE_FRAGMENT_MAX_MS` is a new turn.
- **The caller's answer to the onset intro is never the pickup hello**
  (RUNBOOK §65, Ravi calls 994/998/1008). Speech that ENDS after the onset
  intro finished playing closes the absorb window (`end_connect_onset_absorb`);
  a pickup greeting spoken over the intro is dropped before it can be merged or
  prepended; pickup-phase splits never arm the 900 ms splitter hold. हां/जी are
  hello-filler only when nothing was asked. A plain yes to the identity
  question speaks `outbound.good_time_check_line` with no LLM
  (`_good_time_fastpath_line`, opt-out `call_flow.connect.good_time_fastpath`).
- **A "no" to "am I speaking with X?" is RE-CONFIRMED once, and nothing
  about the offer follows an unconfirmed identity** (RUNBOOK §73, calls 966,
  1062). Outbound only: a bare no or a wrong-number phrase →
  `outbound.wrong_number_reconfirm_line` (built-in per language; the Pramukh
  builder authors the owner's Hindi wording) → the caller's yes / ack /
  repeated no → `wrong_number_close` + hangup; "it's me" → the good-time
  line; "he's not here / my husband's number" → `wrong_person_response` and a
  per-turn `━━ IDENTITY NOT CONFIRMED ━━` band that forbids the pitch. The
  verdict order is me → wrong-number phrase → someone-else → ack. Rollback:
  `WRONG_NUMBER_RECONFIRM=false`.
- **Every automatic language switch is bounded by `languages.supported`,
  on EVERY path** (§73, call 1059). A Hindi-only agent (`evidence_switch:
  false`) runs the LEGACY switcher, which never checked the bound or
  `auto_switch` — "3 bhk" counted as two English words and the reply came
  in English. The clamp sits after both branches; the pipeline's STT mirror
  and the "repeat in X" path are bounded too; digits and unit words (bhk,
  lakh, sq ft) never count as English; gratitude has hi/gu built-ins.
- **A declined site visit stands for the rest of the call** (§73, six of
  seven test calls re-invited). `_detect_visit_decline` (strong forms
  anywhere; "not now / later / bare no" only after an invitation; "tell me
  first" = one turn of `━━ ANSWER FIRST ━━`; a concrete alternative time is a
  negotiation, not a decline) writes `site_visit_interest`, downgrades the
  objection strike, and arms the per-turn `━━ VISIT DECLINED ━━` band
  (details on WhatsApp, never a second invitation). The Pramukh builder adds
  branches so the INVITE stage exits on the value. Dead-air repair never
  re-asks a question the caller already answered. Corpus-sweep caller lines
  before widening a pattern (19 hits in 10,830 lines, all genuine).
- **An action-only branch (no `goto`) runs its actions and stays** (§73):
  `purpose = Investment` → `retire_slots:family_size`, so an investor is
  never asked who will live there. `correction_gated` now works in Hindi —
  `_CORRECTION_MARKER_RE` used `\b` around `नहीं`, which never matched.
- **A scripted flow is followed line by line through four opt-in hooks**
  (RUNBOOK §75, the Vedanta test agent, `deploy/build_pramukh_vedanta_test.py`):
  `slots[].ask_gated` writes a slot only as the answer to its own question
  (the utterance hint or the previous turn's directive — never the
  head-of-missing fallback) or on a correction marker, because "location"
  / "amenities" / "abhi" are words callers say in QUESTIONS (call 1052
  flipped `decision_factor` six times); a stage branch `when_intent:
  no_more_questions` moves the flow on when the caller says "no, nothing
  else" to that stage's "any other question?" instead of closing the call;
  `termination.pre_close_question` is the owner's "anything else before I
  go?" line, asked before every goodbye (the VISIT ALREADY CONFIRMED band
  carries it) and recognised as a close permission; a budget-fit
  alternative must offer the caller's BHK (`pitch_switch.offers_config`).
  `_DONE_REPLY_RE` used `\b` after "नहीं", so a Devanagari "no" to "और
  कुछ?" never closed deterministically — fixed. Scripted lines are quoted
  in stage goals and OBJECTION HANDLING guidance, native script with
  English words in Latin, "Sir" left to the gender filter. The model still
  paraphrases and drops lines, so `stages[].script` is the FLOOR: `ask_verbatim`
  speaks the directive's slot question after a reply that did not ask it
  (content-word overlap, never a repeat of a paraphrase), `after_answer`
  follows every reply of the stage unless already asked / `unless` lines.
  An authored pre-close line is the ONLY close permission for that agent.
  `_reconcile_ask_budget` and the `asked=` hint trust the QA tracker (the
  slot's own question text) over the slot-name/label keyword scan, which
  credited "…investment के लिए?" to investment_horizon and un-noted the
  purpose ask. A tool booking is mirrored into `memory.booking` before the
  stage-advance check (it used to land one turn late).
- **Turn-taking is per-agent data, and "tts done" is not "finished
  speaking"** (RUNBOOK §78, Vedanta test call b5452b23). `_tts_playing`
  drops when SYNTHESIS ends; FreeSWITCH plays the WAVs for seconds more
  (a 25 s pitch reads "tts done" at 5 s). The sink's `playback_busy_until`
  is the only clock that knows — read it (`_agent_audio_remaining`) before
  deciding whether the caller interrupted or answered. `call_flow.
  turn_taking` (all opt-in, every other agent byte-identical):
  `pause_hold_ms` holds EVERY caller pause (the platform holds only
  narrative/splitter callers — `_pause_may_continue` → "agent");
  `backchannel_guard` drops a bare "हाँ/जी/हम्म/okay" spoken with ≥1 s of
  our audio still to play (it was a TURN: the stage advanced on it and the
  next question queued behind the pitch); `tail_barge_in` stops still-
  playing audio on real speech after synthesis ended; `sustained_barge_ms`
  carries out a delivery-guard-deferred barge-in once the caller has kept
  talking that long (1.3 s of speech sat in the echo gate for 10 s, came
  back empty, and the agent said "couldn't hear you"). Two platform bugs
  fixed with it: `_validate_response` did not know the daṇḍa, so every
  Hindi reply over `max_response_length` (400) was sliced mid-word with a
  spoken "..." — set the cap per agent when its scripted lines are long
  (Vedanta: 647 chars → 760); and `_note_ask_signals` credited any slot
  with "time" in its NAME on a clock word ("कुछ समय पहले" in the good-time
  line → enum `purchase_timeline` "asked" → "हाँ, अभी बात कर सकते हैं"
  wrote it Immediate through the ask gate). The audit transcript now
  carries the joined re-transcription of a split turn (`user_retranscribed`
  → `replace_last_events`), not the pieces — the owner read the pieces and
  concluded "4 BHK" was never heard. A goal line cannot stop the model
  answering its own question on a bare "हम्म": on an `ask_verbatim` stage
  `_scripted_filler_reask` repeats the question verbatim with no model
  turn. `_CORRECTION_MARKER_RE` must know the Devanagari "चेंज" — a
  correction-gated slot refused "बजट चेंज करके 70 लाख" until it did.
- **Moving the visit is not refusing it, and "because" is not "why"**
  (RUNBOOK §79, call 64629614). "कल की मत करो, कोई और date fix कर लो"
  matched the objection word "मत करो" twice; the second strike closed the
  call with `guardrails.escalation.handoff_message` (the Ravi base agent's
  "sales manager will call you back within two hours") while the caller
  was still talking. `_is_visit_negotiation` (a visit on the table + a
  time word or a move-it cue, minus call-level declines / strong refusals /
  callback requests) turns that intent into `general` with the per-turn
  `━━ VISIT DAY BEING NEGOTIATED ━━` band. `detect_intent`'s question-word
  scan is a substring test — blank "क्योंकि" before it, or "इन्वेस्टमेंट
  वाली क्योंकि…" is a question and its answer is refused. The model answers
  about the wrong project when two are in play: `_project_focus_band`
  names the project the caller said this turn, else the one(s) the agent's
  own last line named. A "call me in 10 minutes" closes the call by design
  and becomes a FOLLOW-UP suggestion for a person — the platform never
  places it.
- **"Are you an AI?" is always answered yes — only when asked**
  (`app/voice/ai_disclosure.py`, RUNBOOK §66): a router branch ahead of the
  identity shortcut and the FAQ playbook; an owner's playbook answer that
  admits it is kept, one that denies it is overridden. Corpus-sweep caller
  lines before widening a pattern (0 false positives in 9,992).
- **Language detection**: set from the caller's first substantive response and mirrored for the rest of the call. `SarvamSTT` and `SarvamTTS` both accept a `language` field (`en-IN`, `hi-IN`, `gu-IN`).
- **LLM**: OpenAI-compatible client pointed at the configured endpoint (see `.env`). Responses are capped at 1-2 sentences for spoken delivery.

### TTS Layer

- **`tts_sarvam.py` — SarvamTTS**: HTTP streaming to `api.sarvam.ai/text-to-speech/stream`, model `bulbul:v3`, speaker `shubh`. Audio chunks are written to the `AudioSink` as they arrive (true streaming, no buffer-all-first).
- **`tts_stream.py` — StreamingTTS**: abstract base class. `MockStreamingTTS` available for testing.
- **Figures in English are per-agent DATA** (`languages.tts_numbers_english`,
  `app/voice/spoken_numbers.py`, RUNBOOK §82): Bulbul read "₹50-55 लाख" digit
  by digit in Hindi and voiced the hyphen. With the flag, only what is written
  in DIGITS on the text handed to TTS becomes English words ("fifty to fifty
  five लाख", "two point one five crore"); number words and the transcript are
  untouched. It runs in `pipeline._spoken_numbers_text` BEFORE the "–" → ", "
  rewrite. `deploy/set_pramukh_tts_numbers.py` switches the Pramukh families on.

### Document RAG (`app/documents/`)

Grounds live calls in tenant-uploaded documents. `retrieval.retrieve_async()` is
the entry point (`conversation._call_llm_stream` awaits it); it returns evidence
**plus a decision**, never a bare string.

```
turn → filler guard → cache (exact tier answers with no API call)
     → embed (worker thread, hard deadline) ‖ BM25 → RRF fuse → prune weak
     → abstention gate → labelled context + structured citations → trace
```

- **Never raises into a call.** No credentials, a provider timeout, a stale or
  unreadable index: each degrades to the lexical arm with `degraded` set.
- **Citations are data, not dialogue** — `[S1]` labels flow to the turn record
  and the API; the prompt forbids speaking them (`RAG_SPOKEN_CITATIONS=false`).
- **Changing `RAG_EMBED_MODEL` or `RAG_CHUNK_WORDS` requires a re-index.**
  Indexes record how they were built and a mismatch is reported, never silently
  reinterpreted. `POST /api/agents/{slug}/documents/reindex`.
- **Measure before changing anything here**: `rag_eval/` holds the golden set,
  the runner, a parameter sweep and a real-caller-turn replay. `tests/test_rag_*`
  is the regression gate. RUNBOOK §28 has the numbers.
- **Both document stages run on the platform's Gemini key, not OpenAI**
  (RUNBOOK §57). `OPENAI_API_KEY` ran dry on 2026-09-11 and for two weeks
  every Builder upload "embedded" nothing while the tab toasted success, and
  every Copilot attachment failed with 429. Embeddings: `gemini-embedding-2`
  via the NATIVE `batchEmbedContents` (`GeminiEmbeddingProvider`) — the
  OpenAI-compat route returns `usage: null` and cannot pass a task type.
  Field extraction: `yaml_filler.resolve_llm()` → `LLM_BASE_URL` with
  `COPILOT_LLM_MODEL`, the agent-BUILDING quota bucket, never the live-call
  one. A failed embed leaves `embed_error.json` beside the chunks and
  `index_status` reports the reason; the upload reply carries `embedded`.
- **One list decides what a document picker offers**: `extractor.SUPPORTED`
  / `ACCEPT` / `unsupported_reason()` (mirrored as `DOC_ACCEPT` in
  `frontend/src/api/agents.ts`). Every refusal names the way out (".doc →
  save as .docx or PDF"). `.docx` reads tables in body order;
  `Document.paragraphs` alone drops every cell. Parsing and extraction run in
  `asyncio.to_thread` — inline, a scanned PDF freezes the whole API (the
  2026-07-20 incident). The design-system `Button` is always `type="button"`:
  a `<form onSubmit>` upload never fires; drive it from `onClick`.

### Verified smart filter + call exports (`shared/intelligence/`)

The Intelligence page's natural-language filter is **retrieve, then decide**.
Hybrid retrieval answers "which calls are ABOUT this?"; a filter asks "which
calls SATISFY this?" — and the two have nearly identical embeddings, which is
why *"whoever said yes to relocate to Delhi"* used to return the Vapi calls
instead of the correct empty result. RUNBOOK §31.

- **`verify.py` is the precision gate.** A planner call turns the phrase into
  an explicit requirement checklist; then ONE judge call per candidate decides
  membership. Three gates, in order: every stated check true → confidence ≥
  `FILTER_MIN_CONFIDENCE` → **the quoted evidence actually occurs in that
  call's transcript** (`extract.quote_in_transcript`). The third is what makes
  an empty result trustworthy — a model can assert a match, it cannot
  fabricate words into a transcript we already hold.
- **Entities and stance are strict; TENSE is not.** "whoever relocated to
  Vapi" means the callers who *agreed* to relocate to Vapi — a phone call
  captures commitments, not completed moves. Requiring completion produced 0
  rows on a corpus that clearly had 3. The planner prompt carries worked
  examples for exactly this; do not "tighten" it back.
- **Retrieval is now candidate generation**, widened by the planner's
  paraphrases + keywords (`search_calls(probes=…, keywords=…)`). A relevant
  call the arms never surfaced is the one failure the judge cannot repair.
  Those extra arms share a voting BUDGET in `rrf_fuse` (`_shared`), not a
  per-arm weight — the model picks the arm count, and six flat-weighted
  keyword arms out-voted the query itself.
- **`verified=false` is never a silent fallback.** When the judge cannot run,
  the response says so and the UI labels the rows unverified.
- Verdicts cache in `intel_filter_verdicts`, keyed by question × that call's
  CONTENT — **never by the planner's requirement text**, which an LLM rewords
  between runs (that made the cache hit 0% on a repeated filter). The plan
  itself is cached per `(owner, question)` in `intel_filter_plans` (0024) so
  the checklist is stable. Bump `FILTER_RUBRIC_VERSION` when the prompt
  changes meaning; it retires both.
- **Exports (`selection_export.py` + `shared/export_store.py`)**: ticked rows →
  a multi-sheet .xlsx (Numbers / Contacts / Calls / Insights / Transcripts /
  Evidence / Commitments / Legend) uploaded to GCS. **No signed URLs** — org
  policy blocks SA key creation, so the bucket stays private and our 256-bit
  token is the credential, exactly like call recordings (`/api/exports/dl/…`,
  allowlisted in `auth._OPEN_PREFIXES`). A failed upload is NOT a failed
  export: the file is served locally and the row says `storage='local'`.
- **A query only skips the judge if it carries DIGITS** (phone / call_key /
  conn_id). The first predicate was "no spaces, 8+ chars", which made
  `relocation`, `complaints` and `interested` bypass verification silently —
  see RUNBOOK §31.6b, which lists all 6 post-ship defects.
- **Exports read back from the bucket recorded ON THE ROW**, and retention
  re-confirms the object before deleting the local copy: this VM's SA has
  object-level access only and cannot see bucket lifecycle rules, so a
  delete-after-N-days rule is invisible to us.
- **Recall is set by `ARM_LIMIT`, not by the candidate pool** (RUNBOOK §32).
  It was 40; every arm ranks the same strong calls, so six arms capped at 40
  were barely wider than one, and *"people willing to relocate to vapi"* — a
  98-call answer — reached the judge as 103 candidates and left 29 true
  matches unseen. Widening the pool alone changed nothing. Measured: 150
  restores 100% recall. **Precision was never the problem — false positives
  measured 0 across six filters.**
- **The result list PAGES; it is not one screen.** The dashboard asked for 50
  matches and rendered them as a single page, so 50 was a hard ceiling on what
  could be seen, ticked, exported or reported no matter what the server
  matched. Export and Report can only send TICKED ids, so that cap silently
  shortened every workbook built from a large filter.
- **`pool_full` means `matched` is a floor, not a total.** When retrieval fills
  its own candidate pool the API says so and the UI must repeat it. A ceiling
  that can be hit silently is the bug; a bigger number alone is not a fix.
- **A small answer is usually the DATA, not the filter.** "Who knows DevOps"
  returned 6 because the agent only ever offers DevOps as one of four roles —
  it never asks whether the candidate knows it, and only 6 callers said the
  word themselves. Split transcripts by speaker before assuming a search bug.
- **A quote may span one speaker's consecutive segments, never a change of
  speaker** (`extract.speech_runs`, RUNBOOK §53.5). The transcript has one line
  per VAD segment with a timestamp line between, and 28.7% of caller lines are
  continuations — so a caller who paused ("i expected" / "4 lakh per annum")
  could never be quoted whole, and the gate threw out a call that passed every
  check. Do not "simplify" it to stripping all scaffolding: that lets the
  agent's question be glued onto the caller's "yes".
- **A `QUOTE_NOT_FOUND` rejection is never cached.** It is a failure of one
  model response, not a fact about the call; cached, it excluded that call from
  that filter forever. `verified` is computed from "the model answered"
  (`judged`), not from "something was cacheable" (`fresh`).
- **The answer moves with the WORDING, and that is the data** (§53.6): the same
  intent returned 44 / 43 / 20 / 30 as "prior AI experience" and "4-7 LPA" were
  re-phrased ("LPA" does not say current or expected CTC). Read the printed
  requirement checklist before suspecting the judge or retrieval.
- **A bare `docker exec lescall-api python -` has no LLM endpoint.** `.env` is
  loaded as an import side effect of `intel_api`, and `config` reads the
  environment at import. Import `intel_api` FIRST and probe once — otherwise
  every judge call fails instantly and a retry loop looks like rate limiting.
- Tests: `test_intel_verify.py` (28) + `test_intel_export.py` (15)
  + `test_intel_filter_api.py` (20) + 9 in `test_intel_search.py`.

### Public Integration API (`/v1`)

The customer-facing HTTP surface a CRM/ERP integrates against: push contacts in,
pull every call's intelligence summary, transcript and recording out. RUNBOOK §30.

- **It is NOT under `/api/`** — that prefix is the dashboard's session-auth
  allowlist (`auth._TENANT_API_PREFIXES`), where a bypass applies to every
  future sibling route by prefix. `/v1` has its own credential and its own gate,
  listed once in `auth._OPEN_PREFIXES`, plus its own nginx location.
- **`shared/public_api/keys.py::authenticate()` is the whole admission chain** —
  key → **account IP allowlist** → status/expiry → per-key allowlist → rate
  limit. No endpoint may re-implement any part of it. The IP check comes BEFORE
  key validity on purpose, so an unlisted address learns nothing about the key.
- **Client IP is `X-Real-IP` only when the peer is loopback** (our nginx, which
  overwrites it). Otherwise the socket peer. Trusting the header from any peer
  would make the allowlist decorative.
- **The IP allowlist and rate ceilings are ADMIN-owned** (`/admin/integration`,
  `dashboard/admin_api_console.py`). Tenants mint keys in Settings → API access
  and may only NARROW — `policy.effective_limits` enforces that direction.
- **Rate-limit state is in Postgres** (`api_rate_windows`), so a restart does not
  hand out fresh budgets; it fails OPEN on a DB error, with nginx's
  `limit_req zone=lescall_api` as the floor.
- `call_id` in the API is `call_key`, never the integer PK. Sync is keyset
  pagination: `?order=asc&cursor=<last id>`.
- Contact writes go through `shared/contacts.py`, shared with the dashboard, so
  API and CSV upload normalize numbers and dedupe identically.
- **Rate limits are per ACCOUNT, not per key** — every request counts against
  `key:<id>` AND `acct:<owner>` (migration 0020). Counting only per key made the
  operator's ceiling multipliable by minting keys. `policy.account_ceiling()` is
  the aggregate budget.
- **All untrusted text goes through `shared/public_api/sanitize.py`** at exactly
  two boundaries (`_json_body`, `_q`). A NUL byte in any field reaches Postgres
  otherwise and 500s — a DoS any caller could trigger.
- **Every failure under /v1 is one envelope**, including 404/405/500, enforced by
  a `/v1`-scoped middleware in `public_api.install()`. Non-`/v1` traffic is
  untouched — this is a shared app.
- **The API surface is `/developers` + `/developers/reference`, NOT a Settings
  tab** — it was one and it read as an afterthought. Settings is account profile
  only, with signposts. `/api-docs` redirects to the reference.
- PDF is the browser's print pipeline via the `@media print` block in
  `index.css` (scoped to `body.printing-doc`, set only by the reference page).
- **Never surface `/v1/openapi.json` as a link in the product UI** — a naked
  file link reads as unfinished. The endpoint stays live for machines.
- **The API is a GROWTH capability** — `entitlements.api_access` (migration 0021),
  enforced by `feature_allowed()` in `keys.authenticate` (402 `plan_required`) and
  at key creation. A COLUMN, not `plan_code == 'growth'`, so the admin plane can
  grant it per tenant via an override. Inert while billing enforcement is off.
- Tests: `tests/test_public_api.py` (38 functional) + `test_public_api_security.py`
  (56 adversarial — key oracles, IDOR, injection, IP spoofing, limit evasion).
  RUNBOOK §30.11 lists the 9 findings they caught.

### WhatsApp follow-ups (`shared/whatsapp/`, Integrations → WhatsApp)

A caller who agrees on the call gets the template the agent's PLAYBOOK names
(brochure / tracked form link), auto-sent or held for approval. RUNBOOK §64.

- **Pipeline stage `whatsapp` runs last and only for calls < 1 h old** — the
  sweeper backfills every new stage over history; the freshness gate is what
  stops that from messaging months of past callers. Never loosen it.
- **Consent is `wa_consents`, never `contacts.disposition`** (`rejected` also
  means "declined a ring"). Newest row per phone wins; STOP revokes; a "no" on a
  later call cancels anything still waiting. A commitment counts only if its
  quote is in the transcript.
- **The sender re-checks everything at send time** (kill switch
  `WA_SEND_ENABLED`, canary, consent, IST window, daily cap, template approved).
  A deferral never spends an attempt. One send per `(playbook, call)` is a DB
  constraint.
- **Meta refuses an empty variable** and gender words resolve to nothing without
  `contacts.gender` — use `{greeting}` + a fallback; a send with an empty field
  waits for a person even under auto-approval.
- Tokens are Fernet-encrypted with `WA_TOKEN_KEY` (calling-agent/.env) — losing
  it disconnects every tenant. `/r/<token>`, `/r/f/<token>` (one attachment by
  token), `/api/whatsapp/webhook` and `/api/whatsapp/webhook/interakt/<token>`
  are the only open routes (auth `_OPEN_PREFIXES` / `_OPEN_EXACT` + nginx).
- **Two providers on one account row** (`wa_accounts.provider`, RUNBOOK §80):
  `meta` is the tenant's own Cloud API account; `interakt` is a Meta BSP key
  (Pramukh). Interakt has NO list-templates API — templates are REGISTERED by
  hand exactly as approved there (`templates.manual_components`, `source=
  'manual'`, never deleted by a Meta sync); media goes as a public `/r/f/` URL;
  values are POSITIONAL in order of appearance. `sender.deliver` is the one
  path for both. A playbook carries one brochure PER PROJECT (`media_map`,
  `stage.resolve_media`); a send with no file yet is HELD and re-resolved every
  pass, never failed. `WA_SEND_ENABLED=true` since 2026-10-13.
- Tests: `tests/test_whatsapp.py` (59) + `test_whatsapp_interakt.py` (21).
  Meta and Interakt are always monkeypatched.

### Memory (`app/voice/memory.py`)

Three-layer memory injected into each LLM call:
1. **`ConversationMemory`** — sliding window of last 5 turns.
2. **`ContextMemory`** — extracted `user_name`, `intent`, `topic`, `sentiment`.
3. **`CallState`** — structured booking fields; the ground truth for what has been confirmed.

Phone numbers are extracted and stored internally; they are never read back verbatim to the caller.

### Web Console (`app/web/`)

Optional `--web` flag adds a FastAPI server (port 8000). `EventHub` (`event_hub.py`) is an async pub/sub broadcaster — pipeline calls `emit_nowait()` (non-blocking); each WebSocket client has its own bounded queue. A slow client drops messages but never blocks the pipeline.

### Call Audit (`call_audit.py`)

When `ENABLE_CALL_AUDIT=true`, each call produces a WAV recording and transcript in `./recordings/<conn_id>/`. Managed by `audit_manager` (singleton imported by `ws_audio_receiver.py`).

---

## Key Invariants

- **`SARVAM_API_KEY`** env var name is `SARVAM_API_KEY` (not `SARVAMAI_API_KEY` — a common past mistake).
- **A phone number that arrives as a spreadsheet NUMBER is not text**
  (RUNBOOK §58). `str(9128692865.0)` is `"9128692865.0"`; stripping non-digits
  gives an 11-digit number the carrier rejects as `UNALLOCATED_NUMBER`, and
  the UI used to say "declined" — twenty candidates "declined" in two minutes
  and none was ever rung. `shared.phone.numeric_text()` undoes float and
  scientific-notation formatting inside `digits_only`, so every write path
  and the dial itself agree; the xlsx reader prints integral floats as
  integers. `invalid_number` is its own call status: the carrier could not
  route the number. A "declined" that comes back in under two seconds for a
  whole list is a number problem, and the `CAMPAIGN … -> status (carrier
  words)` log line now says so.
- The greeting fires only after the first audio frame is received (`signal_call_active()` gate), not at WebSocket open, to avoid burning TTS credits on abandoned connections.
- `socket_server.py` runs under system Python; `ws_audio_receiver.py` (and everything under `calling-agent/`) runs under `calling-agent/venv`. Do not mix them.
- All audio throughout the AI pipeline is 16-bit PCM at 16 kHz. FreeSWITCH mod_audio_stream delivers L16 PCM at 8 kHz (`TELEPHONY_INPUT_CODEC=l16`; a PCMU branch exists, unused); `ws_audio_receiver.py` resamples to 16 kHz before forwarding.
- **An outbound leg does NOT know who it called from the channel alone** — the
  caller id is OUR origination number and `Caller-Destination-Number` is the
  dialplan extension (`outbound_ai_connect`), because the destination lives in
  the dial string. The dialled number rides as the `lescall_dest` channel
  variable → WS path segment 6 → `TelephonySession(dialed_number=…)`.
  **Only an exact phone match may supply contact identity** (`identity_dial`);
  a time-only dial match may LABEL direction and nothing else. Conflating the
  two greeted 44 real people by a stranger's name and handed one customer's
  memory to another (RUNBOOK §33). Deploy `lescall-api` BEFORE
  `lescall-runtime` when touching this path.
- **The TRIAL FLOOR is the one set of limits `enforcement_enabled()` cannot
  switch off** (`shared/billing/entitlements.py`, RUNBOOK §44). Global billing
  enforcement is OFF and staying off — flipping it starts refusing calls on
  minute quotas and locking accounts platform-wide. But every cap used to open
  with `if not enforcement_enabled(): return Decision.ok()`, so a `free`
  account had no ceiling on anything. `free` tenants are now held to **1 agent
  family, 20 copilot messages/day, 150k copilot tokens/day, 25k tokens per
  message** regardless of that switch. Pilot is a PAID trial and is NOT in
  `TRIAL_PLANS`. The floor only ever TIGHTENS (`_floor()` is a `min`; NULL
  means unlimited, i.e. the LOOSEST value — inverting that hands a trial
  account an unlimited budget and reads like a typo). `effective_limits()` is
  the single source both the gates and the meters read: if a gate and a meter
  ever disagree, the gate is not the bug, the second read is.
  - The agent cap has TWO gates and both must bind: `_assert_agent_quota()`
    before any filesystem write, and `_billing_claim_guard()` inside the
    `INSERT`'s transaction under `pg_advisory_xact_lock`. Count-then-write is
    TOCTOU; guarding only the pre-check leaves the cap one race from bypassable.
  - Any UI deciding whether a cap applies reads **`enforcement || trial`**.
    Reading `enforcement` alone hides a limit that IS being enforced, and the
    user meets it as an unexplained 402 after filling in the whole form.
- **Copilot tokens are metered, and a "turn" is not a unit of cost.** One chat
  message runs a server-side tool loop of up to `_MAX_TOOL_ROUNDS` (20) full
  LLM requests, so the old turns/day counter priced twenty polite messages and
  twenty adversarial ones identically. `_charge_tokens()` is called **inside
  `_attempt()`**, so nudged retries and fallback-chain walks are charged too —
  they are real requests against a quota SHARED with live phone calls. Trust
  the provider's `total_tokens` over `prompt + completion`: Gemini folds
  reasoning tokens into the total without showing them (~29%). A new LLM
  surface wires up `check_copilot_budget()`, never one half of it.
- **SQL: `tests/test_sql_safety.py` is a gate, and it fails closed.** Building
  SQL from anything the scanner cannot prove constant fails the test. Fix it
  with a bind parameter; if it is an identifier, map the input through a
  module-level whitelist and interpolate the LOOKUP RESULT; only then add a
  `_REVIEWED` entry with a written reason. Never widen the safe-shape rules to
  make one call pass.
- **`shared/db/database.py` is the one choke point for every DB write**, HTTP
  or not. The qmark translator is literal-aware — it must never again rewrite a
  `?` inside a quoted string, which silently shifts every bind value after it.
  NUL bytes are stripped from `str` parameters there because Postgres text
  cannot hold one and psycopg raises `DataError` — a 500 any caller can trigger
  is a DoS. `%` is NOT escaped there; callers write `%%` themselves.
- **TTS audio is checked against its TEXT before it is cached** (RUNBOOK §45.1).
  Bulbul occasionally appends a hallucinated clause to a short line, and the
  canned-audio cache is keyed on the resolved `{contact_name}` — so one bad
  synthesis replayed "Am I speaking to Piyush Gupta **or**" to that contact on
  every call, with no API call left to correct it.
  `_expected_speech_seconds()` is a calibrated floor model (worst legitimate
  ratio measured 0.95 over 88 live syntheses; the two real hallucinations
  measured 1.27 and 1.44) and `TTS_DURATION_MAX_RATIO` (1.2) is the gate. It
  refuses the cache WRITE and evicts on READ, so entries written before it
  self-heal. A false positive costs one live synthesis; a false negative is
  permanent. It is also the only working wrong-sample-rate detector —
  `_check_stream_rate` reads a RIFF header the `linear16` stream never sends.
- **A goodbye only ends a call when it ENDS the caller's sentence**
  (`_farewell_is_terminal`, RUNBOOK §45.2). "bye" is a homophone of by/buy/bhai
  and STT mishears real words into it: "Signify, Philips Lighting" arrived as
  "bye philips lightning" and the caller was offered a hang-up mid-answer.
  Position, not vocabulary, is what separates every genuine goodbye in the
  corpus. Per-agent `farewell_patterns` stay exempt — they are deliberately
  unambiguous phrases.
- **Reporting a received call is not requesting one** (`_RECEIVED_CALL_RE`,
  RUNBOOK §45.3). "I got a call from this number 20 minutes back" carries a
  duration AND the word "call", which is all the weak callback branch needed —
  two inbound callers asking about a role were told "I'll call you back in
  about 20 minutes" and hung up on. The explicit forms are still read first and
  unconditionally. `am`/`pm` need a preceding digit, or `\bam\b` matches
  "ma'am".
- **An agent line the caller never heard must come OFF the transcript**
  (`_retract_unheard_agent_line`, RUNBOOK §45.4). The line is recorded when
  generation finishes; a continuation merge can cut the reply microseconds
  later. `calls.transcript` is read from the audit file, so an un-retracted
  line reaches the DB row, Intelligence, exports and reports — one question
  read back as six on a call where the caller heard it once. Check
  `_reply_still_inaudible()` BEFORE the cut clears the state that answers it.
- **A stage asks its OWN questions** (`build_prompt_context(stage_slots=…)`,
  RUNBOOK §45.5). Global slot priority made the agent ask a later stage's
  question early; that stage then arrived and its script asked it again.
  Stages with no `collect` (every legacy agent) keep global priority
  byte-for-byte. `━━ YOU ALREADY ASKED THIS ━━` renders AFTER `CURRENT GOAL`
  on purpose — a script quoting the same question must not win.
- **A pause is not the end of a turn — read the SPEAKER** (RUNBOOK §46).
  `TRANSCRIPT_MERGE_SKIP_WHEN_IDLE` concludes a turn with ZERO hold when the
  caller is quiet and nothing is in flight. Right for someone answering
  questions, wrong for someone telling you something: measured 2026-09-16,
  33 of 45 turns took that path and 27 produced a continuation — 17 merged,
  10 **silently swallowed** (the caller spoke and got no reply at all).
  `_ends_with_continuation_cue` cannot help; those fragments are complete
  sentences. `_pause_may_continue` adds two speaker-shaped signals —
  a narrative-length segment (`TURN_NARRATIVE_SPEECH_MS`) and a caller who
  has already resumed through a pause on this call (`_note_caller_split`,
  recorded at ALL FOUR merge paths) — and holds `TURN_CONTINUATION_HOLD_MS`,
  reassembling up to `TURN_MAX_MERGED_FRAGMENTS` pieces into ONE turn. A
  caller who never splits never trips it, so short-answer flows keep their
  latency. Do NOT widen the relaxed barge-in window to match: inside it the
  barge bar drops to ~1.3x the VAD floor, and 6s of that is 6s for the
  agent's own echo to cancel its own reply.
- **A branch VALUE can retire the questions that no longer apply**
  (`branches[].actions: ["retire_slots:a,b,c"]`, `StateManager.retire_slots`,
  RUNBOOK §56). `depends_on` gates a slot on another being FILLED, not on its
  value, so "got a job / still looking" could not be expressed: Viraj (भारत
  रोजगार सेवा, call 934) recorded `got_job=नहीं` on turn 1 and still asked an
  unemployed caller which company he had joined, his designation, joining
  date and offer letter — and the extractor then STORED a company for him.
  Retired slots leave `missing`, count as satisfied for the stage gate, are
  omitted from the facts list, read "not applicable" in the summary and the
  record (`collected_data.retired`), and REFUSE later writes. Order the
  stages so adjacency after the branching stage is the harmless half and
  `goto` the other half past it. The same call fixed two closing-turn
  classes for every agent: "ji ji ji okay sir thank you" is a yes to "shall
  I cut the call?" (`_POLITE_TOKENS`), and polite Latin tokens are not
  evidence of English (`_language_content_words`), so a 20-turn Hindi call
  no longer flips to English on its last word — and `calls.language` is now
  the call's DOMINANT language (`dominant_language()`), never the last flip.
  The generic ENDING THE CALL band's example line ("Is there anything else I
  can help you with?") is the origin of that reflex on an outbound survey;
  `termination.end_conditions` is the exception that lets a survey close
  itself — write them.
- **An everyday word is a slot value only as the answer to its own question,
  and an ask budget settles on the CALLER's turn** (RUNBOOK §61, call 971).
  `state_manager._EVERYDAY_ALIASES` ("ठीक", "okay", "अच्छा", "हाँ"…) fill an
  enum/boolean only when `_is_being_asked(slot)`; a city or a project is taken
  wherever it is volunteered. `note_asked` at `max_asks` puts the slot in
  `awaiting` (not askable, still an extractor target via `missing + awaiting`)
  and it becomes `skipped` one caller turn later — moving it to `skipped` at
  the choice lost "नहीं, अभी तक नहीं आई" and "रोजगार की मदद चाहूंगा". A
  caller's QUESTION never writes a yes/no (`_drop_yes_no_from_question_turn`,
  both extractor doors). A greeting-only agent line ("हेलो, नमस्ते।") is
  never replayed (`_is_greeting_only_text`; the identity line follows), and an
  outbound first stage speaks before an intent can advance it. Never `\b` or
  `isalnum()` as a word edge near Indic text — use `_word_boundary_ok`/`_NB`/`_NA`.
- **A machine on the line is not the customer, and a hello is not an
  answer** (`app/voice/machine_line.py`, RUNBOOK §63, Craft Idea call
  2026-09-28). iPhone Call Screening, voicemail, network and hold
  announcements are classified BEFORE anything reads the turn
  (`_machine_line_gate`): a screener gets one identity line then silence, a
  mailbox is closed in silence (or gets `call_flow.connect.voicemail`), and
  none of it reaches memory, slots, QA, language or the stage. Patterns are
  anchored on machine phrasing and were validated on every caller line in
  `recordings/` — re-run that sweep before widening one. A bare mid-call
  "hello" is a presence check (`_presence_check_reply`): "I can hear you" +
  the SAME question, never the next one; a question or a bare "okay" never
  marks an open question answered in the QA band.
- **A machine on an OUTBOUND line ends the call NOW, and a hold is bounded
  as an EPISODE** (RUNBOOK §83; call 917265832771 = 690 s of a hold
  announcement, 17 pitches into it). `_arm_machine_close` sets
  `hangup_immediate`: the pipeline cuts the opening / onset intro
  (`_abort_agent_speech`) and the bridge hangs up with no 3 s drain; only an
  authored `connect.voicemail` message speaks first
  (`MACHINE_CLOSE_IMMEDIATE` is the kill switch). A repeated hold line re-arms
  the 60 s gap timer, never `_machine_wait_started` — `MACHINE_HOLD_MAX_S`
  (90, per-agent `connect.hold_max_s`) / `MACHINE_SCREENER_MAX_S` end the
  episode through `machine_wait_expired()` in the watchdog, in silence, not
  with two "couldn't hear you". Hold fragments are `is_hold_residue`; a
  person is back on a greeting / ack / return cue / ≥4 words
  (`_looks_like_hold_pickup`). A CONFIRMED mailbox beep (`audio_events`
  `BEEP_*`: 3–12 hops at one frequency, then it stops) closes only when
  `machine_audio_close_allowed()` — outbound, no PERSON has spoken, ≥1.5 s of
  caller audio before it. Corpus-sweep every caller line before widening a
  pattern: "you have reached" and "get back to you" are things people said.
- **A twin's call direction is its SLUG** (`store.assert_direction_matches_slug`,
  RUNBOOK §60). `x-inbound` answers, `x-outbound` places calls; `write_agent`
  and `write_agent_dir` refuse a `call_direction` that contradicts the suffix
  (and fill a missing one), so the Copilot, the Builder's Call Type selector,
  rollback and share import all hit one gate, and `list_agents` derives
  `counterpart` from the slug. Viraj's inbound twin carried `outbound` for two
  days because `_quality_warnings` crashed AFTER the write on a nested object
  Gemini had sent as JSON text, the model was told "DID NOT HAPPEN", re-created,
  was pointed at the wrong twin, and edited it. Post-write advisories go
  through `_safe_quality`; `_decode_stringified_objects` turns JSON text back
  into objects in `_normalize_definition`; a crashed tool logs its traceback.
- **An agent's gender comes from its TTS VOICE, not its `pronouns` field.**
  `schema.from_dict` overwrites `identity.pronouns` / `personality.pronouns`
  from `speaker_gender()` at load time, so changing a persona's sex means
  changing the speaker (`simran` → `shubh`); editing the pronouns alone is
  silently discarded. Renaming a persona never renames the SLUG — that is an
  identifier with calls, revisions, campaigns and DID bindings hanging off
  it (RUNBOOK §46.5).
- **`qa_field_signals` must be what the agent SAYS, not what the field is
  called.** Copilot auto-filled them with the slot label ("joining timeline"),
  so `QATracker` — the second anti-repetition layer — matched nothing on any
  builder-built agent. The engine now widens them with each slot's own
  `question` text at load time; fix the source, not one agent's YAML.
- **A go-ahead or a placeholder is not a slot value** (`_drop_non_values`,
  RUNBOOK §50). "हाँ जी बताओ" was stored as `customer_channel`; `'unscheduled'`
  satisfied a `when_slot` gate and closed a call mid-invitation. The guard
  stands at ALL THREE doors an LLM value comes through (main-call tool, shadow
  tool, json extractor) — production runs `LLM_TOOLS_MODE=on`, so a guard in the
  json branch alone protects nothing. Yes/no stay values on purpose. The tool
  roster carries each slot's MEANING (`_tool_slot_hints`); a bare name makes the
  model fill whatever is empty. A tool-written date/time passes the same
  plausibility check as any other source.
- **"No time TO DO something" is a pain point, not a decline**
  (`_strip_activity_time_complaints`). "फॉलो अप करने का टाइम नहीं मिलता" is the
  most useful sentence a discovery call can hear; it was answered with the
  canned deferral. When the activity is the call itself ("बात करने का टाइम
  नहीं") it is still a deferral.
- **Simulating an agent: set `LLM_TOOLS_MODE=on` and pace it.** The flag lives
  only in docker-compose for `lescall-runtime`; without it you measure the
  `shadow` extraction path production never runs (the dashboard Try-agent sim
  has this gap today). The LLM key allows 15 req/min and is SHARED with live
  calls; a turn costs 2–3. Check `ss -tn | grep ':9001 .*ESTAB'` first.
- **Speaking speed belongs to the VOICE, not to `pace`** (RUNBOOK §51). Same
  line, same `pace: 1.0`: neha 10.6 s, simran 7.0 s, shubh 5.9 s. Changing a
  speaker silently changes the speed — re-measure (one fixed line per speaker x
  pace, compare seconds) instead of carrying the number over.
- **Authored Hindi is NATIVE SCRIPT with English words in Latin** ("आपके
  business में नए customers…"). Sarvam: romanized input "significantly degrades
  output quality". The model mirrors the script of the lines it sees.
- **Never give the model a list of acknowledgements or a quotable example
  line** — it says them verbatim ("Samajh gayi, Alwyn." every turn; a persona
  example copied word for word). Describe the behaviour. The built-in rule that
  ORDERED an opening acknowledgment each turn is gone for the same reason.
- **Per-call variety is opt-in data**: `opening_variants`, `personality.
  style_variants` (one per call, PER-CALL band), and
  `llm_config.record_details_on_main_call: false` (no record tool on the spoken
  path: −1.2 s median on 77% of turns; the background extractor is then the
  writer, including for an optional slot on the turn it was just asked). All
  default to today's behaviour; previews set `vary_per_call = False`.
- **Engine tests never load a live agent YAML.** Agent files are gitignored and
  owner-editable through the Copilot; 13 tests went red the morning an owner
  rebuilt `jai_mistry-outbound`. Freeze a snapshot under `tests/fixtures/`.
  And never run two pytest processes at once — they share one test database.

---

## Frontend design system (`frontend/`)

React 18 + TS + Vite + Tailwind **v3** + shadcn/ui, redesigned 2026-07-22
(full record: `deploy/PLAN-ux-redesign.md`). If you touch a page, match these —
they are the whole point of the redesign, and re-introducing the old patterns
silently undoes it.

- **shadcn is on Tailwind v3** → add components with `npx shadcn@2 add <c>`
  (the 3.x CLI targets v4 and will corrupt `tailwind.config.ts`). Primitives
  live in `src/components/ui/`.
- **Token namespacing is load-bearing.** shadcn's `--border`/`--accent` are
  renamed `--ui-border`/`--ui-accent` because the app's `:root` already owns
  those names (brand emerald). Dark `--ui-border`/`--input` carry baked-in
  alpha → never use opacity modifiers (`border-border/50`) on them.
- **Type scale is closed**: `text-2xs` 11 · `xs` 12 · `sm` 13 (body default) ·
  `base` 14 · `lg` 16 · `xl` 18 · `2xl` 20 · `3xl` 24, plus a display tier
  `4xl` 30 / `5xl` 40 **for metric values only**. Never write `text-[Npx]`.
- **Radius law**: controls `rounded-md`, cards `rounded-2xl`, pills
  `rounded-full`.
- **One confirm idiom**: `useConfirm()` / `usePrompt()` from
  `components/confirm.tsx` (awaitable). No `window.confirm`/`prompt`/`alert`.
- **Chart colour comes from `lib/chartColors.ts`** (`CHART`, `TONE`,
  `scoreColor`) — never hardcode a hex. This is what fixed the old
  Negative-vs-Hot collision.
- **Loading/empty/error go through `AsyncState`** (`variant="rows|cards|text"`,
  `rows`, or a `skeleton` override). Empty labels state what would put
  something there, per filter — not "Nothing here".
- **Icon-only buttons are `IconButton`** with a specific `label` (it becomes
  both `aria-label` and tooltip): name the row's subject, don't ship four
  identical "Edit"s.
- **Shared composites before bespoke markup**: `components/kit.tsx`
  (PageHeader/Table/Pagination/SearchInput/Segmented/BarList),
  `components/insight.tsx` (ScoreCell/Chips/FilterChips/CommitmentCard/
  TimelineList/StatTile), `components/analyticsCharts.tsx`.
- **List state belongs in the URL** via `useUrlParam`/`useUrlNumber`
  (`lib/hooks.ts`); debounce text search with `useDebounced`.
- **State that must survive opening a row does NOT belong in `useState`**
  (RUNBOOK §53). `AppShell` keys `<main>` on the pathname, so list → detail is
  a real unmount. The Intelligence AI filter lived in component state and was
  destroyed on every row click — one 25 s judged pass per call inspected. Rule:
  WHICH view is active goes in the URL (`?ai=`); an EXPENSIVE result, the ticked
  selection and the row order go in `lib/intelBrowse.ts` (memory +
  `sessionStorage`, every access guarded). A detail page's breadcrumb returns to
  the list URL it came from, never a bare path.
- **Next/Previous on a detail page is `components/CallPager.tsx`**: steps
  `replace` history (one Back returns to the list), a server-paged list is
  extended by fetching the neighbouring page with the same predicates, and
  "returning from a call" is a recorded flag — `useNavigationType()` is `POP`
  for a refresh too. `frontend/e2e/intel_browse.e2e.mjs` is the gate; it counts
  `/intel/search` hits because the regression is a cost, not a pixel.
- **All page I/O** goes through `api/client.ts` (`api`, `sse`) and
  `api/mutate.ts` (`post/patch/put/del/postForm`). Zero raw `fetch` in pages.
- **Routes are lazy** in `App.tsx` — only Overview + shell are eager. Adding a
  static page import back re-inflates the entry bundle (it was 740 kB; it is
  385 kB).
- **IA**: `/` is the Overview dashboard; the Copilot chat is `/copilot`.
- **Copilot quick controls** (`components/VoiceSpeedControls.tsx`, RUNBOOK §67):
  voice + speed by click, via `/api/copilot/sessions/{sid}/quick-controls`
  (agent on disk → both twins through the store; else the interview draft).
  New click-to-set agent settings belong in that endpoint + sheet, and must
  leave a model-context note so the chat does not undo them.

Deploy the SPA with
`docker compose -f deploy/docker-compose.services.yml up -d --build spa`
(bind-mounted containers serve stale code until rebuilt).
