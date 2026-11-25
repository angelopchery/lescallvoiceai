# PLAN — Two-stage model VAD + endpointer + call-session state machine

Status: **IMPLEMENTED & LIVE 2026-07-30** (VAD_ENGINE=fsmn in the runtime
compose env; rollback = delete that line + `up -d runtime`). Delivered:
`vad_stack.py` (WebRTC pre-gate + FSMN worker thread, frame path 65µs
p50), `audio_events.py` (DSP classifier: DTMF/RINGBACK/BUSY/FAX/BEEP/
MUSIC/SPEECH/SILENCE/NOISE), `endpointer.py` (fast/default/hold semantic
profiles), `call_session.py` (shadow FSM, IVR/voicemail interpretation
for outbound). Model vendored at data/models/fsmn_vad (Apache-2.0).
funasr_onnx installed --no-deps in Dockerfile.app (stale numpy cap;
runtime-verified on 2.4.4). Pre-speech ring deepened to 400ms on this
engine (model onset lag). E2E-verified through the live ingress:
FSMN events, endpoint profiles, full session lifecycle
CALL_INIT→…→USER_INTERRUPTED→…→CALL_ENDED in one call. · 2026-07-30
Replaces the energy-threshold VAD with WebRTC-VAD (pre-filter) →
FSMN-VAD (primary), splits endpoint detection out of VAD, and introduces
an explicit call-session state machine every component reads instead of
guessing from scattered flags.

## Current state (verified in code this week, not docs)

- **VAD is energy-only** (`vad.py`): mean-absolute amplitude per 20ms
  frame vs a threshold self-calibrated ONCE from the first 500ms of the
  call — during which the pipeline is structurally deaf. No
  re-calibration, no drift tracking; telephony thresholds hardcoded
  (min 260). A quiet caller sits near the floor for the whole call.
- **Endpointing is fused into the VAD loop** as a constants zoo:
  400ms hard flush (`HARD_FLUSH_SILENCE_FRAMES=20`), 550ms merge window,
  800ms post-response merge, 2000ms continuation window, 1200+300ms
  TurnManager backstop. The TurnManager's "semantic" path now gets real
  words (streaming STT segment finals) but end-of-turn is still decided
  purely by silence duration.
- **Session state is scattered booleans**: `_tts_playing`,
  `_greeting_done`, `_vad_mute_until`, `playback_busy_until`,
  `_continuation_likely_until`, `hangup_pending`, filler grace windows —
  each consumer re-derives "what is the call doing" independently.
- **Barge-in** = energy streak over `2×threshold` while `_tts_playing`,
  because the agent's own TTS echoes back through mod_audio_stream and
  energy margin is the only echo discriminator we have.
- **Streaming STT (LIVE)** is fed on the energy VAD's speech frames;
  Sarvam's server-side VAD segments it independently.
- `webrtcvad` already sits unused in `calling-agent/venv`.
- Hardware reality: **2-core / 3.8GB VM** shared with FreeSWITCH,
  Postgres, and four containers. The frame path runs every 20ms; the
  whole VAD stack gets a hard CPU budget.

## Target architecture

```
16k PCM frames (20ms)
   │
   ▼
[C1] WebRTC-VAD pre-filter          mode 0-1 (PERMISSIVE), hysteresis
   │   drops only long-idle silence; NEVER a business decision
   ▼
[C2] FSMN-VAD primary (onnx)        100ms hops, prob smoothing,
   │   speech_start / speech_end / prob events                soft-speech
   │                                                          profile
   ├──────────────► [C6] ASR gate: feed SarvamStreamingSTT only
   │                     while FSMN says speech (or interruption
   │                     candidate during AI_SPEAKING)
   ▼
[C3] EndpointController             SEPARATE from VAD: fuses FSMN
   │   speech_end + silence ms + streaming-STT segment finals
   │   (semantic completeness, dangling-clause, digit-dictation
   │   guard) → "turn is over, run the LLM"
   ▼
[C5] CallSessionState               explicit enum + transition table;
       AI_SPEAKING / USER_INTERRUPTED / THINKING / … — every
       consumer (barge-in, echo mute, watchdogs, ASR gate,
       greeting gate) READS state instead of re-deriving it
```

## Component specs

### C1 — WebRTC-VAD pre-filter
`webrtcvad` (already installed), 20ms frames @16k, **aggressiveness 0-1
only**. Its sole job: skip FSMN inference during long idle stretches
(caller listening to the agent, dead air). Rules that make it safe:
- It can only *gate FSMN invocation*, never end a turn, never drop a
  frame from `_audio_buffer`/pre-speech ring, never touch barge-in.
- 400ms hangover: FSMN keeps running for 20 frames after webrtc's last
  speech frame.
- While a turn is OPEN (speech accumulated, endpoint not fired) or state
  is AI_SPEAKING (interruption watch), FSMN runs on EVERY frame
  regardless of webrtc — the pre-filter only saves CPU in confirmed
  idle. Missing soft speech at stage 1 is therefore impossible while
  anything is at stake; the worst case is ~100ms extra onset latency
  from deep idle, recovered by the pre-speech ring buffer.

### C2 — FSMN-VAD primary (the decision maker)
`funasr_onnx.Fsmn_vad_online`, quantized ONNX (~1MB), model files
vendored in-repo (`data/models/fsmn_vad/`: `model_quant.onnx`,
`config.yaml`, `am.mvn`) — no hub download at boot, no torch.
- Buffer 20ms frames into **100ms hops** (VAD granularity; endpoint
  decisions are ≥250ms so 100ms resolution costs nothing).
- Emits `speech_prob`, `speech_start`, `speech_end` with configurable
  onset (2 hops) / hangover (3 hops) smoothing.
- **Soft-speech ("telephony-soft") profile**: lowered speech threshold +
  longer min-silence, tuned offline (see eval harness) — the model
  replaces the amplitude floor entirely, which is the whole point: a
  quiet caller is a *model* problem, not a loudness problem.
- Kills the 500ms calibration deafness: FSMN needs no calibration.
- `last_energy` stays computed (2 lines) — energy is retained ONLY as
  the echo discriminator for barge-in, never for speech decisions.
- Adapter exposes the current `EnergyVAD` surface (`is_speech`,
  `barge_in_threshold` equivalents) so `pipeline.py`'s diff is minimal.

### C3 — EndpointController (separate from VAD)
New `app/voice/endpointer.py`. Inputs per 100ms tick: FSMN state,
silence-since-speech-end, streaming-STT segment finals for the open
turn, conversation context (what the agent just asked). Decision
profiles instead of one constant:
- **fast-complete** (~250-300ms silence): last segment final ends with
  terminal punctuation / is a complete short answer to a closed
  question ("हाँ ठीक है।").
- **default** (~400ms): today's behavior, unchanged risk.
- **hold** (~700-900ms): dangling clause (`_DANGLING_END` reused), open
  arithmetic ("मेरा नंबर है नौ आठ..."), digit-dictation in progress
  (digits in the last final → caller is reading a number in bursts —
  the known phone-number trap), or FSMN prob hovering mid-band (breath
  pause, not end).
The 550ms merge window and the TurnManager both fold into this one
component; continuation-merge (rollback) stays as the safety net for
the endpointer's mistakes. Every endpoint decision logs its profile +
inputs (one line), and lands in `call_turns` for offline tuning.

### C5 — Call-session state machine
New `app/voice/call_session.py`: enum + guarded transitions + timestamped
event log, emitted to EventHub and stamped on `call_turns`.

Platform-adapted states (inbound answers immediately; pre-answer audio
does not exist until FreeSWITCH answers — see constraint below):

```
CALL_INIT → (outbound only: RINGING → CALLER_TUNE | IVR |
             WAITING_FOR_HUMAN | VOICEMAIL) →
LIVE_CONVERSATION ⇄ AI_SPEAKING ⇄ USER_INTERRUPTED → THINKING →
CALL_ENDED
```

- Phase A wires the machine up and DERIVES it from today's flags
  (shadow: log-only). Phase B flips consumers one at a time to read it:
  barge-in guard (`AI_SPEAKING`), echo mute, greeting gate, silence
  watchdogs (`LIVE_CONVERSATION` only), ASR gate, hangup arming.
- **Constraint stated up front**: mod_audio_stream forks media only
  after answer, so true RINGING is invisible today. Post-answer
  CALLER_TUNE / IVR / VOICEMAIL detection (music vs speech, beep
  detection, long-monologue-at-start heuristics) is the outbound
  phase (P5) and needs the event classifier; do not block the core
  migration on it.

### C6 — ASR gating
`SarvamStreamingSTT.feed_nowait` fires only while C2 says speech-active
(+ hangover), and during AI_SPEAKING only once the interruption
candidate is confirmed — cuts Sarvam load and stops feeding it echo.
Barge-in becomes: `state == AI_SPEAKING` AND FSMN speech ≥ N hops AND
the retained energy-margin echo guard (a model VAD happily classifies
our own TTS echo as speech — energy margin + `playback_busy_until` stay
as the discriminators until real AEC exists).

## Soft-speech doctrine (explicit, per the requirement)

1. Stage 1 is permissive by construction and only gates confirmed idle.
2. Stage 2's threshold is tuned on OUR audio: the eval harness runs the
   whole stack over the **392 recorded real calls on disk**
   (`recordings/*/caller_audio.wav`, 16k caller-side), mining
   low-energy regions the current VAD scored as silence and checking
   what FSMN finds there (sampled + listened-to, not just counted).
3. Gate to ship: on the eval set, missed-speech ≤ current energy VAD's
   (strictly better expected) and false-trigger rate on hold
   music/noise not worse than 1.2× current, p95 stack cost < 2ms/hop.
4. Live shadow mode (`VAD_SHADOW=true`) runs FSMN alongside energy on
   real calls, logging per-turn disagreement before any flip.
5. Fallback documented: if FSMN (zh-CN-trained) underperforms on soft
   Indic telephony speech in the eval, Silero-VAD (onnx, multilingual)
   slots into the same C2 adapter — the architecture doesn't change.

## How FSMN-VAD is obtained/imported

**Runtime (production VM) — no torch, no hub downloads:**
```bash
calling-agent/venv/bin/pip install funasr_onnx onnxruntime  # ORT CPU wheel
# model vendored in-repo (one-time, ~1-2MB total):
#   data/models/fsmn_vad/{model_quant.onnx, config.yaml, am.mvn}
```
```python
from funasr_onnx import Fsmn_vad_online
vad = Fsmn_vad_online("data/models/fsmn_vad", quantize=True)
# per 100ms hop: segments = vad(chunk, is_final=False)  (cache internal)
```

**Model files, two acquisition routes (one-time, dev side):**
- Route A (preferred): download the pre-exported ONNX from Hugging Face
  `funasr/fsmn-vad-onnx` (or ModelScope
  `iic/speech_fsmn_vad_zh-cn-16k-common-onnx`) and commit the three
  files. No torch anywhere.
- Route B: full export on a dev box —
  `pip install funasr torch --index-url .../cpu` then
  `AutoModel(model="fsmn-vad").export(type="onnx", quantize=True)` —
  only if we want to re-export/re-quantize ourselves.
- Checklist item at download: record the model card license
  (FunASR code is MIT; confirm the FSMN-VAD model card allows
  commercial use — expected yes, verify and note the revision).

## Phases

- **P0 — acquisition + offline eval harness** (no runtime change):
  vendor model, `tools/vad_eval.py` over the recordings corpus,
  CPU benchmark on THIS VM, threshold tuning, ship-gate report.
- **P1 — two-stage VAD behind `VAD_ENGINE=fsmn`** (default `energy`):
  `vad_stack.py` adapter, pre-speech ring integration, shadow mode on
  live calls; energy path untouched and default.
- **P2 — EndpointController**: absorb hard-flush/merge-window/
  TurnManager behind `ENDPOINTER=v2` flag; per-decision logging into
  call_turns; continuation-merge kept as net.
- **P3 — state machine**: shadow (derived+logged) → consumers flipped
  one at a time, each its own commit.
- **P4 — flip defaults** after shadow numbers are clean; energy VAD
  code stays for one release as `VAD_ENGINE=energy` rollback.
- **P5 (outbound, separate approval)** — event classifier for
  CALLER_TUNE / IVR / VOICEMAIL states + campaign-aware handling
  (needs music/speech discrimination; candidates: FSMN prob patterns +
  spectral heuristics for ringback/DTMF/beep, or SenseVoice-small AED
  if CPU allows — it likely doesn't on 2 cores; benchmark first).

## Risks

- **Echo-as-speech is the #1 risk**: FSMN will detect our own TTS echo
  as speech where the energy threshold accidentally filtered it. The
  energy-margin + playback-busy guards are RETAINED for barge-in, and
  P1 shadow mode measures exactly this before anything flips. True AEC
  (speexdsp/WebRTC-APM with the TTS reference signal) is the durable
  fix and is explicitly out of scope here.
- zh-CN-trained model on Indic telephony: gated by the P0 eval on our
  own recordings; Silero fallback pre-planned.
- CPU on the shared 2-core VM: quantized ONNX + 100ms hops + idle
  gating; hard gate p95 < 2ms/hop measured on this machine, else stop.
- webrtc pre-filter can never kill soft speech by construction (only
  gates FSMN during confirmed idle, full-rate during open turns).
- Pre-answer states (RINGING) are unreachable without an early-media
  fork from FreeSWITCH — scoped out; states begin at answer.

## Non-goals

- Replacing Sarvam's server-side VAD (it stays; C2/C3 decide locally).
- AEC, diarization, or any change to the sales-stage machine
  (`conversation_stage` is a different axis and untouched).
- The IVR-navigation behavior itself (P5 detects; acting on IVR menus
  is its own future plan).
