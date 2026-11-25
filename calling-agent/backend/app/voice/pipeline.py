"""
Pipeline orchestrator — connects all layers via async queues.

Architecture:
    [AudioLayer] → vad_queue → [VADLayer] → stt_queue → [STTWorker]
        → transcript_queue → [ConversationEngine] → tts_queue → [TTSPlayer]

Each layer is an independent async task. Queues are bounded.
Failure in one layer does not crash others.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import time
from collections import deque
import dataclasses
from dataclasses import dataclass

import numpy as np

from .audio_input import SAMPLE_RATE, SAMPLES_PER_FRAME
from .vad import VoiceActivityDetector
from .stt.router import STTRouter
from .tts_stream import StreamingTTS, MockStreamingTTS
from .tts_sarvam import SarvamTTS
from .turn_manager import TurnManager, TurnManagerConfig
from .conversation import ConversationRouter
from .memory import ConversationMemory
from .event_hub import EventHub
from . import machine_line as _machine_line

from ..audio_transport.base import AudioSource, AudioSink
from ..audio_transport.local_mic_source import LocalMicSource
from ..audio_transport.local_speaker_sink import LocalSpeakerSink

logger = logging.getLogger(__name__)

# ENV-based control for barge-in and telephony defaults
ENABLE_BARGE_IN = os.environ.get("ENABLE_BARGE_IN", "true").lower() == "true"

# VAD engine (deploy/PLAN-vad-stack.md): "energy" = legacy threshold VAD,
# "fsmn" = WebRTC pre-filter → FSMN-VAD model stack + EndpointController +
# audio-event classifier + shadow session state machine. Rollback is this
# one env var.
VAD_ENGINE = os.environ.get("VAD_ENGINE", "energy").strip().lower()
TELEPHONY_MODE = os.environ.get("TELEPHONY_MODE", "false").lower() == "true"


@dataclass
class AudioSegment:
    """A complete speech segment ready for STT."""
    audio: bytes
    duration_ms: float
    timestamp: float
    # Monotonic timestamps of the caller's actual speech onset/end for this
    # segment (continuation-merge timing anchors; 0.0 = unknown).
    speech_start_ts: float = 0.0
    speech_end_ts: float = 0.0
    # True when speech_start_ts is a RECONSTRUCTION (end − buffered speech)
    # rather than the measured onset. Turn-taking may use it; the ASR-replay
    # corpus may not (see the plausibility gate in _flush_segment).
    span_estimated: bool = False
    # True when part of this audio was captured over agent playback (shadow
    # ring) — such transcripts pass the agent-echo text guard before use.
    shadow: bool = False
    # Milliseconds of frames the VAD stack called REAL speech inside this
    # segment (0.0 = not measured). duration_ms counts the whole buffered
    # span, hangover and pre-roll included — a 860 ms segment can hold 300 ms
    # of voice, and "the caller clearly spoke" must be judged on this.
    speech_ms: float = 0.0


@dataclass
class Transcript:
    """STT result ready for conversation engine."""
    text: str
    duration_ms: float
    timestamp: float
    stt_latency_ms: float = 0.0
    # Carried through from AudioSegment (continuation-merge timing anchors).
    speech_start_ts: float = 0.0
    speech_end_ts: float = 0.0
    span_estimated: bool = False
    # Derived STT confidence (0..1) from the router; None = unknown.
    confidence: "float | None" = None
    # Which STT path produced the text ("primary" | "primary-script-retry" |
    # "fallback"). Recovered paths are mishear-prone: they never vote on the
    # conversation language (A3, 2026-07-30).
    stt_source: str = "primary"


def _spoken_numbers_text(text: str, enabled: bool) -> str:
    """The TTS text with figures in English when the agent opted in
    (`languages.tts_numbers_english`, RUNBOOK §82); the text itself
    otherwise. The transcript keeps the figures as written — only what
    Bulbul is handed changes."""
    if not enabled or not text:
        return text
    from .spoken_numbers import english_numbers
    out = english_numbers(text)
    if out != text:
        logger.info("[TTS:NUMBERS] %r -> %r", text[:70], out[:90])
    return out


@dataclass
class Response:
    """Engine response ready for TTS."""
    text: str
    is_shortcut: bool
    timestamp: float
    # True only for filler utterances ("एक सेकंड…").  Used by the TTS layer to
    # grant the filler — and the real response that follows it — a longer
    # barge-in guard so the filler's own echo can't cancel the real response.
    is_filler: bool = False
    # True for utterances that exist because the caller missed something
    # (hello replays, the connect intro, watchdog reprompts, the greeting).
    # The connect delivery guard protects these from hello barge-ins at any
    # call age, not just inside CONNECT_WINDOW_S.
    protected: bool = False
    # Short language code ("en"/"hi"/"gu") stamped when the text was chosen,
    # so the accent is decided with the words — not re-read from mutable
    # conversation state at dequeue time (a mid-queue language flip once spoke
    # a fully-English reply as hi-IN). None → derived from the text itself in
    # _resolve_tts_language.
    language: str | None = None
    # A5: replay of the agent's previous utterance (early-"hello?" repair /
    # repeat request). A replay exists to fill a silence — if a NEWER
    # response started playing after this replay was created, the silence is
    # gone and the replay must be dropped, not double-spoken.
    replay: bool = False
    # The connect intro spoken on the caller's first sustained speech
    # (_maybe_connect_onset). Its playback end separates the pickup hello it
    # answered from the caller's reaction to it — see _onset_play_end_ts.
    onset_intro: bool = False


QUEUE_SIZE_AUDIO = 40
# Raised 3 → 6 (2026-08-03): during a slow-Sarvam stretch the queue filled
# behind an in-flight request and NEW caller speech was dropped ("STT queue
# full"). Late segments are recoverable (the stale-drop and merge layers
# sort them out); lost audio is not.
QUEUE_SIZE_STT = 6
QUEUE_SIZE_TRANSCRIPT = 3
# How many segments beyond the one being processed may transcribe AHEAD in
# parallel (the STT submitter/worker split). 1 → up to ~3 provider requests
# in flight worst-case. Transcripts are still DELIVERED strictly in order.
STT_PREFETCH_AHEAD = max(1, int(os.environ.get("STT_PREFETCH_AHEAD", "1")))
# Raised from 3 to 8: streamed LLM responses produce multiple sentence-level
# chunks in quick succession. A queue of 3 caused drops when the TTS layer was
# busy playing an earlier chunk while new ones arrived.
QUEUE_SIZE_TTS = 8

MAX_SPEECH_SECONDS = 25
MAX_BUFFER_BYTES = SAMPLE_RATE * 2 * MAX_SPEECH_SECONDS
# 300ms minimum: short inbound responses like "haan", "yes", "ha" are
# valid single-word answers that must reach STT. 300ms is enough audio to
# distinguish real speech from a click/breath without missing short replies.
MIN_SPEECH_MS = 300.0
# ── Real-speech floor (2026-08-04 VAD audit) ──────────────────────────────
# MIN_SPEECH_MS above is measured on the PADDED buffer: the 400ms pre-speech
# ring and the 200ms hysteresis hold are both counted into _speech_duration_ms
# because both are prepended/appended to the segment. One single 20ms frame of
# model-confirmed speech therefore scored ~620ms and cleared the 300ms floor —
# the gate was arithmetically incapable of rejecting anything. In the
# 2026-08-04 audit calls 9 of 23 (39%) live STT submissions carried no caller
# speech at all, and an 80ms blip padded to 980ms came back as "₹1 lakh." in a
# sales call about budget.
#
# This floor is measured on raw_speech frames ONLY — the model's own verdict,
# with no amplitude term — so the "a quiet caller is a model problem, not a
# loudness problem" doctrine of vad_stack.py still holds.
MIN_REAL_SPEECH_MS = float(os.environ.get("MIN_REAL_SPEECH_MS", "200"))
# Connect window keeps a lower bar for the same reason CONNECT_MIN_SPEECH_MS
# exists: a clipped opening "hello" must still reach STT.
CONNECT_MIN_REAL_SPEECH_MS = float(
    os.environ.get("CONNECT_MIN_REAL_SPEECH_MS", "120"))
# Companion guard for the case the ms floor alone cannot see: FSMN reporting
# speech in a buffer that carries no signal. In the 2026-08-04 replay a 760ms
# segment at RMS 15 was credited with exactly 200ms of "speech", cleared the
# floor, and cost a paid Sarvam call that returned "".
#
# This is a RATIO against the caller's own line noise, never an absolute level
# — a quiet caller is still far above their own floor, so vad_stack.py's "a
# quiet caller is a model problem, not a loudness problem" doctrine holds. It
# fails open: with too little ambient measured to form an estimate, the segment
# passes.
#
# The threshold is deliberately far below real speech. A first attempt at 2.0×
# rejected a genuine caller utterance ("d v h k", 800ms of model-confirmed
# speech) that measured 1.8× in the callsim replay, while other real speech in
# the SAME call measured 2.0-8.4× — i.e. 2.0 sat inside the real-speech
# distribution. Deafness costs far more than a wasted STT call, so this now
# asserts only that speech rose ABOVE the ambient at all.
MIN_SPEECH_SNR = float(os.environ.get("MIN_SPEECH_SNR", "1.2"))
SNR_MIN_NOISE_FRAMES = int(os.environ.get("SNR_MIN_NOISE_FRAMES", "10"))
# ── How the two terms are MEASURED (2026-08-17 deafness audit) ─────────────
# The first version of this guard compared mean(frames the model labelled
# speech) against mean(frames it did not). That is only valid if the model's
# labels line up with the audio, and on this stack they do not:
# FsmnVadStack.is_speech() returns `_speech_open`, state written by a worker
# thread consuming 100ms hops, so the labelled window lands 200-280ms LATE
# (measured by replaying recordings/*/caller_audio.wav through the live
# stack), and FSMN_MAX_END_SIL_MS holds it open past the end on top of that.
# For an utterance under ~400ms — i.e. every bare "hello" — the labelled
# window therefore sits mostly on the silence AFTER the voice.
#
# On call ffbdc517 that inverted the ratio completely: the caller's burst
# (mean-abs ~3000) landed in the NOISE bucket because the model had not
# opened yet, the DTX comfort fill after it (constant |x|=8) landed in the
# SPEECH bucket, and the guard computed speech=7.4 / noise=261 = 0.03x on
# audio whose true SNR was ~500x. All 20 discards across 5 calls that day
# were this arm; on two of them the caller was never heard at all.
#
# Both terms are now measured from a rolling window of raw frame energy and
# never consult a label:
#   floor  — a PERCENTILE of recent frame energy. A burst is a small slice of
#            any multi-second window, so the estimate cannot be dragged up by
#            the speech it is supposed to be measured against.
#   speech — the mean of the loudest SNR_SPEECH_TOP_FRAMES frames INSIDE the
#            segment. Peak-ish rather than mean, so a late label window full
#            of comfort noise can no longer drag it down to the floor; still
#            an average over 100ms, so a single click cannot fake it.
SNR_AMBIENT_WINDOW_FRAMES = int(
    os.environ.get("SNR_AMBIENT_WINDOW_FRAMES", "250"))   # 5s
SNR_AMBIENT_PCTL = float(os.environ.get("SNR_AMBIENT_PCTL", "10"))
SNR_SPEECH_TOP_FRAMES = int(os.environ.get("SNR_SPEECH_TOP_FRAMES", "5"))
# Absolute backstop: a segment this loud is unambiguously somebody talking,
# so it is never energy-judged no matter what the floor estimate says. Scaled
# off the barge-in threshold because that is already the stack's "this is
# definitely not echo or line noise" level. 0 disables.
SNR_LOUD_EXEMPT_FACTOR = float(os.environ.get("SNR_LOUD_EXEMPT_FACTOR", "1.0"))
# Consecutive SNR drops that each carried real model-confirmed speech before
# the call is called out as probably-deaf. One is line noise; a run is the
# 2026-08-17 signature.
SNR_DROP_STREAK_WARN = int(os.environ.get("SNR_DROP_STREAK_WARN", "2"))


def _fmt_energy(v: float | None) -> str:
    """Energy for a log line — 'n/a' rather than a misleading 0."""
    return "n/a" if v is None else f"{v:.0f}"
# ...and it polices SHORT segments only. Hallucination risk concentrates in
# brief blips (the 80ms "₹1 lakh."), and that is also where a false drop costs
# least. Anything with this much model-confirmed speech is taken on the model's
# word regardless of level — the "d v h k" class is never energy-judged again.
SNR_ENFORCE_BELOW_MS = float(os.environ.get("SNR_ENFORCE_BELOW_MS", "600"))
# (removed 2026-07-23: BARGE_IN_COOLDOWN_MS was dead — the live guard is
# BARGE_IN_MIN_ELAPSED_MS below; audits kept grabbing the wrong knob)
# 5 sustained frames (100ms) before barge-in fires. 3 frames (60ms) was
# too hair-trigger: brief noise or the tail of a TTS echo could satisfy it
# and cut the agent mid-sentence. 5 frames still catches a genuine
# interrupt within the first spoken syllable (~100-150ms).
BARGE_IN_MIN_FRAMES = 5

# Barge-in grace for the utterance that immediately FOLLOWS a filler
# ("एक सेकंड…", "One moment…").  A filler's own audio echoes back through the
# telephony round-trip (~700ms injection + the filler's playback duration) and
# lands *during* the real response that plays right after it.  Those echo frames
# are loud and fresh, so they trip barge-in and cancel the real response — the
# caller hears only the filler, then silence.  The normal 250ms guard is far too
# short to skip that echo.  For the post-filler utterance only (and the filler
# itself) we require this much elapsed time before barge-in can fire, which
# covers the filler-echo window.  Every other utterance keeps the 250ms guard,
# so normal barge-in responsiveness is unchanged.
BARGE_IN_MIN_ELAPSED_MS = 250.0
FILLER_ECHO_GRACE_MS = 1500.0

# Echo cooldown after TTS ends naturally: prevents residual TTS audio from
# being re-captured by the microphone and sent to STT as "user speech".
# In telephony mode the TTS speaker and caller mic are on separate paths.
# In local-mic mode: reduced from 1500ms to 600ms. 1500ms was swallowing the
# caller's first 3-4 words when they responded immediately after the agent.
# Real acoustic echo decays within 100-300ms in headphone/normal setups.
# The echo gate now also exits early if sustained real speech is detected
# (see ECHO_EARLY_EXIT_FRAMES below), providing a second layer of protection.
TTS_ECHO_COOLDOWN_MS = float(os.environ.get(
    "TTS_ECHO_COOLDOWN_MS", "400" if TELEPHONY_MODE else "600"))

# ── Playback shadow capture (2026-08-03 fleet audit, defect #1) ──────────────
# While TTS is playing, frames that don't reach the barge-in bar used to be
# DISCARDED — a caller answering at normal volume during agent speech was
# simply never heard (18/31 fleet agents lost whole turns; heads of
# straddling utterances were cut: "my name is rahul sharma" → "rahul
# sharma."). Instead, speech-energy frames during playback are kept in a
# bounded shadow ring and joined to the caller's turn when one of three
# things confirms it was real speech: a barge-in fires, speech continues
# into the clear window after playback, or the ring alone holds a
# substantial utterance at playback end. Echo protection moves from
# time-domain (mute everything) to text-domain: a shadow-tainted transcript
# that matches what the agent itself was saying is discarded as echo.
ENABLE_PLAYBACK_SHADOW = os.environ.get(
    "ENABLE_PLAYBACK_SHADOW", "true").lower() == "true"
# Ring cap: keep at most this much audio (oldest dropped first).
SHADOW_MAX_MS = float(os.environ.get("SHADOW_MAX_MS", "6000"))
# A frame enters the ring when energy exceeds vad.threshold × this factor.
SHADOW_ENERGY_FACTOR = float(os.environ.get("SHADOW_ENERGY_FACTOR", "0.8"))
# Standalone submission floor: at playback end, ring speech shorter than
# this is discarded (echo blips), longer is seeded as a caller segment.
SHADOW_MIN_SPEECH_MS = float(os.environ.get("SHADOW_MIN_SPEECH_MS", "350"))
# After playback ends, a new speech onset within this window may claim the
# ring as its (playback-era) head.
SHADOW_JOIN_WINDOW_MS = float(os.environ.get("SHADOW_JOIN_WINDOW_MS", "1500"))
# Echo-text guard: shadow transcript vs the agent's own utterance text.
SHADOW_ECHO_TEXT_RATIO = float(os.environ.get("SHADOW_ECHO_TEXT_RATIO", "0.75"))

# Minimum consecutive loud frames during the echo cooldown window that signal
# real caller speech rather than echo. Echo is short-lived (1-3 frames);
# a human speaking produces sustained energy well above this threshold.
ECHO_EARLY_EXIT_FRAMES = int(os.environ.get("ECHO_EARLY_EXIT_FRAMES", "5"))
# ...and those frames must actually be LOUD (2026-08-04 VAD audit). The exit
# used to count bare raw_speech, i.e. the FSMN verdict — but vad_stack.py's own
# contract says the model "happily classifies our own TTS echo as speech" and
# names frame energy as the ONLY echo discriminator in the stack. The exit
# therefore ran on the one signal that cannot tell echo from caller: in the
# audit calls it broke out of a 5.4s echo window on 4 frames and shipped a
# 700ms segment at RMS 8 (digital silence) to Sarvam. Frames that are speech to
# the model but sit below this energy bar no longer open the gate.
# Expressed as a multiple of vad.threshold (260 on the fsmn engine); real
# caller speech in the audit calls measured 737-3655 RMS, echo measured 8-89.
ECHO_EXIT_ENERGY_FACTOR = float(
    os.environ.get("ECHO_EXIT_ENERGY_FACTOR", "1.0"))

# Greeting-window softening: the caller's post-greeting "hello" is the most
# predictable sustained-speech event of the call, so while the greeting is the
# utterance being guarded (not _greeting_done yet) the echo gate exits earlier
# and the mute tail is shorter. All later windows keep the defaults above.
GREETING_ECHO_EARLY_EXIT_FRAMES = int(os.environ.get(
    "GREETING_ECHO_EARLY_EXIT_FRAMES", "3"))
GREETING_ECHO_TAIL_MS = float(os.environ.get("GREETING_ECHO_TAIL_MS", "100"))

# Onset lowered 3→2 frames: catch speech sooner so the very first word after
# a pause isn't clipped. Hold kept at 10 for noise immunity.
SPEECH_ONSET_MIN_FRAMES = 2
SPEECH_HOLD_MIN_FRAMES = 10

# Hard-flush silence gate (frames of consecutive silence after hold expires).
# LATENCY: this is dead air the caller hears after they stop talking before the
# agent even begins to think. 560 ms (28 frames) still tolerates a breath
# pause; any mid-sentence split it does cause is re-merged by
# TRANSCRIPT_MERGE_WINDOW_MS below (which now only waits when a follow-on can
# actually exist), and the continuation-merge feature (when enabled) recovers
# genuinely long pauses. With ENABLE_CONTINUATION_MERGE live, 20 (400 ms) is
# the intended tier-b value.
# (History: 30→45→55 crept up to fight splits, then 35; env-tunable now.)
HARD_FLUSH_SILENCE_FRAMES = int(os.environ.get("HARD_FLUSH_SILENCE_FRAMES", "28"))

# TurnManager backstop threshold (see __init__); hard-flush usually wins.
TURN_SILENCE_THRESHOLD_MS = float(os.environ.get("TURN_SILENCE_THRESHOLD_MS", "1200"))

# Debounce window (ms) after the first transcript arrives before firing the LLM.
# Catches follow-on STT segments from the same split utterance so they can be
# merged into one turn rather than generating two separate agent responses.
TRANSCRIPT_MERGE_WINDOW_MS = float(os.environ.get("TRANSCRIPT_MERGE_WINDOW_MS", "550"))
# Skip the merge wait entirely when no follow-on segment can exist (nothing
# queued, nothing in STT, caller silent) — removes a fixed ~550ms per turn.
TRANSCRIPT_MERGE_SKIP_WHEN_IDLE = os.environ.get(
    "TRANSCRIPT_MERGE_SKIP_WHEN_IDLE", "true").lower() == "true"
# ...but never fast-path a FRAGMENT (2026-08-04 VAD audit). The idle test is a
# point-in-time sample of _prev_speech, and FSMN confirms speech 100-300ms
# AFTER true onset — so a caller who has already resumed still reads as idle.
# In call 02819104 the window was skipped at 06:23:58.896, FSMN reported the
# resumed speech 196ms later, and CONT-MERGE merged 'area.' + 'stanley.' 1.3s
# after the LLM call for 'area.' alone had already been dispatched: the caller
# got two separate answers to one sentence.
# Timing alone cannot separate "finished" from "mid-pause" (both land ~240ms
# after speech end), but WORD COUNT can: every split fragment observed in the
# audit calls was 1-3 words ('area.', 'stanley.', 'yes.', 'a normal.'), and no
# full sentence was ever the head of a split. So short transcripts pay the
# merge wait and long ones keep the zero-latency fast path.
MERGE_NO_FASTPATH_MAX_WORDS = int(
    os.environ.get("MERGE_NO_FASTPATH_MAX_WORDS", "3"))
# ...and only while the caller's silence is still shorter than the model's
# confirm lag; past this they really have stopped.
MERGE_NO_FASTPATH_QUIET_MS = float(
    os.environ.get("MERGE_NO_FASTPATH_QUIET_MS", "700"))

# Post-response continuation window (ms). If TTS completed cleanly and a short
# fragment (≤ 4 words) arrives within this window, it is likely a trailing
# clause of the previous caller turn ("for that", "yes, and also..."). Prepend
# the previous turn's text so the LLM has full context.
POST_RESPONSE_MERGE_MS = 800.0

# ── Continuation merge (pause-tolerant turns) ─────────────────────────────
# A caller who pauses mid-utterance gets a fast reply to the partial input;
# if they resume speaking within CONTINUATION_WINDOW_MS of their own speech
# end, the reply is cut, the router rolls the turn back, and ONE merged
# utterance is re-run (see conversation.begin_turn_transaction). OFF by
# default until validated on live calls; when OFF, behavior is identical to
# before, including the legacy Case-B fragment merge.
ENABLE_CONTINUATION_MERGE = os.environ.get(
    "ENABLE_CONTINUATION_MERGE", "false").lower() == "true"
CONTINUATION_WINDOW_MS = float(os.environ.get("CONTINUATION_WINDOW_MS", "2000"))
# Owner request 2026-08-03: slow/paused callers resume well past 2s and were
# getting a second, separate answer. Gaps in (CONTINUATION_WINDOW_MS,
# CONTINUATION_LONG_WINDOW_MS] also merge — but ONLY while the superseded
# reply is still undelivered (LLM generating, queued for TTS, currently
# playing, or barged into). After a reply has fully played, a caller
# utterance past the base window is a genuine new turn (their answer to the
# agent's question), never a continuation.
CONTINUATION_LONG_WINDOW_MS = float(
    os.environ.get("CONTINUATION_LONG_WINDOW_MS", "6000"))
# Relaxed hearing during the bounded post-endpoint window: barge-in energy
# factor (vs the normal 2.0× echo-reject threshold) and sustained-frame
# requirements. Outside the window today's constants apply unchanged.
CONTINUATION_BARGE_FACTOR = float(os.environ.get("CONTINUATION_BARGE_FACTOR", "1.3"))
CONTINUATION_BARGE_FRAMES = int(os.environ.get("CONTINUATION_BARGE_FRAMES", "5"))
CONTINUATION_ECHO_EXIT_FRAMES = int(os.environ.get("CONTINUATION_ECHO_EXIT_FRAMES", "4"))
# Cap on consecutive rollback-merges per chain (background-noise guard).
# 3 → 5 (2026-08-03): long fragmented monologues hit the cap mid-thought.
CONTINUATION_MAX_MERGES = int(os.environ.get("CONTINUATION_MAX_MERGES", "5"))

# ── Connect window (repeated-"hello" loop counter) ──────────────────────────
# In the first CONNECT_WINDOW_S of a call the caller's patience is ~2s and a
# "hello?" is the most likely utterance. Three protections apply, each with
# its own kill switch and a per-agent opt-out via call_flow.connect:
#   delivery guard — a barge-in against a reply the caller has NOT heard yet
#     (nothing flushed, or < CONNECT_MIN_PLAYED_S played) is deferred until
#     STT shows the interruption was more than a bare hello. Replayed /
#     intro utterances (Response.protected) get the guard at any call age.
#   force-language — short first segments skip lang=unknown (bare "hello"
#     routinely misdetects as Odia/Kannada, doubling STT latency exactly on
#     the loop's critical turns).
#   lower min-speech — a clipped 240ms "hello" must still reach STT.
CONNECT_DELIVERY_GUARD = os.environ.get(
    "CONNECT_DELIVERY_GUARD",
    "true" if TELEPHONY_MODE else "false").lower() == "true"
CONNECT_WINDOW_S = float(os.environ.get("CONNECT_WINDOW_S", "12"))
# Speech ending this long after the onset intro finished playing is the
# caller's answer to it, not the pickup hello it answered (RUNBOOK §65).
ONSET_REACTION_GRACE_S = float(os.environ.get("ONSET_REACTION_GRACE_S", "0.2"))
CONNECT_MIN_PLAYED_S = float(os.environ.get("CONNECT_MIN_PLAYED_S", "1.0"))
# RUNBOOK §70 (call 1033): the delivery guard used to protect only replies in
# the first CONNECT_WINDOW_S of the call. Two later replies were killed 0.15 s
# and 0.43 s into playback by echo/line noise (relaxed continuation-window
# threshold), the caller heard nothing, and sat through 7-10 s of dead air
# until they said hello. A barge-in against a reply the caller has not yet
# heard (< CONNECT_MIN_PLAYED_S audible) now ALWAYS waits for the STT verdict:
# real words kill the reply, a hello or nothing at all lets it play on.
BARGE_GUARD_ALL_REPLIES = os.environ.get(
    "BARGE_GUARD_ALL_REPLIES", "true").lower() == "true"
CONNECT_MIN_SPEECH_MS = float(os.environ.get("CONNECT_MIN_SPEECH_MS", "200"))
CONNECT_FORCE_LANG = os.environ.get(
    "CONNECT_FORCE_LANG", "true").lower() == "true"
CONNECT_SHORT_SEG_MS = float(os.environ.get("CONNECT_SHORT_SEG_MS", "800"))
# ── Connect onset trigger (2026-08-17) ──────────────────────────────────────
# The three protections above all still run DOWNSTREAM of a transcript, and a
# transcript is exactly what the caller could not produce on ffbdc517: five
# hellos in a row were discarded before STT and the agent sat mute for 14s
# while its answer was ready the whole time (it went out in 1ms once a
# transcript finally landed).
#
# Inside the connect window the second beat does not depend on WHAT the caller
# said — their first sustained speech is the whole cue. So fire it on VAD
# onset: no endpoint wait, no STT round trip, nothing that can silently drop
# the turn. The segment still goes to STT normally; the conversation absorbs
# the hello that arrives behind it (CONNECT_ONSET_ABSORB_S) so one caller
# utterance never gets two agent lines.
CONNECT_ONSET_TRIGGER = os.environ.get(
    "CONNECT_ONSET_TRIGGER",
    "true" if TELEPHONY_MODE else "false").lower() == "true"
# Model-confirmed speech before the intro fires. 140ms is ~7 frames — long
# enough that a click or a single echo frame cannot trip it, short enough to
# land inside the caller's first syllable.
CONNECT_ONSET_MS = float(os.environ.get("CONNECT_ONSET_MS", "140"))
# Empty-transcript streak damper: speak "sorry, couldn't hear you" only on
# the SECOND consecutive empty (first stays silent, later ones defer to the
# dead-air watchdog). Four sorries in a row once fed an echo→empty→sorry
# self-loop.
EMPTY_STREAK_DAMPER = os.environ.get(
    "EMPTY_STREAK_DAMPER", "true").lower() == "true"
# TTS accent consistency (PLAN-tts-accent-consistency.md).
# TTS_LANG_FROM_TEXT: the target_language_code sent to Sarvam is derived from
# the text actually being spoken (script/marker detection) instead of raw
# conversation state — English replies were going out hi-IN ("rural accent").
# LANG_MIRROR_GUARDED: Sarvam's per-segment language detection updates
# conversation state only when the transcript survives validation AND the
# claim is consistent with the transcript's own script/markers — a bare
# "hello" misdetected as Odia, or Indian-accented English tagged hi-IN, no
# longer flips the agent's language mid-call.
TTS_LANG_FROM_TEXT = os.environ.get(
    "TTS_LANG_FROM_TEXT", "true").lower() == "true"
LANG_MIRROR_GUARDED = os.environ.get(
    "LANG_MIRROR_GUARDED", "true").lower() == "true"
# A4a (2026-07-30): once the conversation language has been stable for
# STT_LANG_PIN_TURNS accepted transcripts, pin the streaming STT to it —
# per-segment auto-detect rendered short Hindi in Punjabi/Bengali/Kannada
# script 29×/day, each costing a 2-4s retry/fallback chain and often an
# English mishear. Unpins the moment conversation state moves off the
# pinned language (explicit request or evidence switch), so genuine
# switching is never locked out. false = always auto-detect (legacy).
STT_LANG_PIN = os.environ.get("STT_LANG_PIN", "true").lower() == "true"
STT_LANG_PIN_TURNS = int(os.environ.get("STT_LANG_PIN_TURNS", "2"))
# A pin is only ever as good as the evidence behind it, and it is
# SELF-SEALING: the pinned language shapes the transcript, which re-confirms
# the pin. Two guards (2026-08-04): only caller evidence counts toward
# STT_LANG_PIN_TURNS, and every STT_LANG_PIN_RECHECK_TURNS the pin is dropped
# for one auto-detect turn so a wrong pin always has a way out. 0 = never
# recheck (pre-08-04 behaviour).
STT_LANG_PIN_RECHECK_TURNS = int(
    os.environ.get("STT_LANG_PIN_RECHECK_TURNS", "8"))
# lang_source values that represent something the CALLER actually said.
# 'default' / 'stt_or_default_first_turn' / 'guard' / 'pending(...)' are the
# agent's YAML default or a fallback, and must never earn a pin.
_LANG_EVIDENCE_SOURCES = ("script", "script_gu_markers", "pattern", "llm",
                          "evidence", "evidence_confirmed", "stt_mirror")


def _lang_source_is_evidence(src: str) -> bool:
    head = str(src or "").split("(", 1)[0].strip()
    return head in _LANG_EVIDENCE_SOURCES
# B2 (2026-07-30): spelled contact capture. While the conversation is
# capturing an email/phone the merge window stretches so letter-by-letter
# spelling with natural pauses aggregates into one turn instead of three
# separately-answered fragments.
CAPTURE_MODE = os.environ.get("CAPTURE_MODE", "true").lower() == "true"
CAPTURE_MERGE_WINDOW_MS = float(os.environ.get(
    "CAPTURE_MERGE_WINDOW_MS", "2000"))
# H5 (2026-07-31): a transcript ending in a continuation word ("हम बेसिकली
# जो।") must not take the idle-skip fast path — hold the merge window so
# the caller's resumed speech joins the same turn instead of racing the LLM.
MERGE_HOLD_CONTINUATION = os.environ.get(
    "MERGE_HOLD_CONTINUATION", "true").lower() == "true"
MERGE_HOLD_CONTINUATION_MS = float(os.environ.get(
    "MERGE_HOLD_CONTINUATION_MS", "1200"))
# Absolute ceiling on merge-window re-arming while caller input is in
# flight (speaking / STT round-trip running). Bounds the fleet-audit fix
# that stops the engine answering the head of a sentence whose tail is
# still on its way.
MERGE_HOLD_MAX_TOTAL_S = float(os.environ.get(
    "MERGE_HOLD_MAX_TOTAL_S", "6.0"))

# ── Pause tolerance: read the SPEAKER, not just the clause ─────────────────
# The idle fast-path above concludes a turn with ZERO hold whenever the
# caller has gone quiet and nothing is in flight. That is right for a caller
# answering questions ("Vapi", "3 BHK", "50 lakh") and wrong for a caller
# telling you something: a screening candidate describing their background
# pauses for breath after a grammatically complete clause and carries
# straight on. Measured 2026-09-16 on the shubhangi calls — 45 engine
# replies, 33 idle skips, and 27 of those turns produced a continuation the
# engine had to merge (17) or silently swallow (10 "stale fragment
# absorbed, not answered": the caller said something and got no answer at
# all, because the reply to the FIRST half was already on the wire).
#
# `_ends_with_continuation_cue` only catches a dangling conjunction. These
# two signals catch the rest, and both are properties of the SPEAKER:
#
#   narrative — this segment alone ran longer than anyone spends answering a
#               question. Someone mid-story pauses; someone answering does
#               not need to.
#   splitter  — this caller has ALREADY resumed through a pause on this
#               call. One is a habit, not an accident, and the cost of
#               believing them is one hold; the cost of not believing them
#               is interrupting every remaining turn.
#
# Neither fires on a caller who never splits, so the short-answer flows keep
# today's latency exactly. TURN_PAUSE_TOLERANCE=false restores the old
# behaviour wholesale.
TURN_PAUSE_TOLERANCE = os.environ.get(
    "TURN_PAUSE_TOLERANCE", "true").lower() == "true"
TURN_NARRATIVE_SPEECH_MS = float(os.environ.get(
    "TURN_NARRATIVE_SPEECH_MS", "2500"))
TURN_SPLIT_MEMORY = os.environ.get(
    "TURN_SPLIT_MEMORY", "true").lower() == "true"
# How long to hold the merge window for a pause we expect to be spoken
# through. Sits between MERGE_HOLD_CONTINUATION_MS (1200ms, for an explicit
# dangling cue — a stronger signal) and the 550ms default.
TURN_CONTINUATION_HOLD_MS = float(os.environ.get(
    "TURN_CONTINUATION_HOLD_MS", "900"))
# Pieces one utterance may be reassembled from inside a single merge window
# when the caller is speaking through pauses. The window re-opens after each
# one, so this is a ceiling on reassembly, not on waiting —
# MERGE_HOLD_MAX_TOTAL_S still bounds the wall clock.
TURN_MAX_MERGED_FRAGMENTS = int(os.environ.get(
    "TURN_MAX_MERGED_FRAGMENTS", "4"))

# ── Per-agent turn-taking (call_flow.turn_taking, RUNBOOK §78) ─────────────
# A back-channel is an acknowledgement spoken while OUR audio still has at
# least this long to play: nobody answers a question a full second before it
# ends, so the margin separates "हाँ" mid-pitch from "हाँ" to a yes/no.
BACKCHANNEL_MIN_REMAINING_S = float(os.environ.get(
    "BACKCHANNEL_MIN_REMAINING_S", "1.0"))
# Caller speech (not a back-channel) that lands while our audio is still
# playing after synthesis finished stops it — only when enough is left for
# the stop to be audible at all.
TAIL_CUT_MIN_REMAINING_S = float(os.environ.get("TAIL_CUT_MIN_REMAINING_S", "0.8"))

# ── Whole-turn re-transcription (RUNBOOK §69) ───────────────────────────────
# The VAD cuts a caller's sentence at every ~300 ms pause and each piece is
# transcribed ALONE. Sarvam needs context: a one-second piece comes back
# garbled ("की बात कर सर।" for "किसकी बात कर रहा हूँ?") or EMPTY — and an
# empty piece's words are simply gone. The merge window then glues the
# separately-mangled texts together. Measured 2026-10-07 (call 1032): 35 STT
# requests, ~half empty; re-transcribing the same caller audio in whole
# sentences recovered every utterance the agent misread.
# When a turn is assembled from ≥2 pieces (empty pieces count), the pieces'
# audio is joined and transcribed ONCE, and that text is what the engine
# reads. The request starts speculatively as each piece arrives, so on most
# turns it is already back by the time the merge window closes.
TURN_RETRANSCRIBE = os.environ.get("TURN_RETRANSCRIBE", "true").lower() == "true"
TURN_RETRANSCRIBE_TIMEOUT_S = float(os.environ.get(
    "TURN_RETRANSCRIBE_TIMEOUT_S", "1.6"))
TURN_RETRANSCRIBE_LOOKBACK_S = float(os.environ.get(
    "TURN_RETRANSCRIBE_LOOKBACK_S", "10.0"))
TURN_RETRANSCRIBE_MAX_S = float(os.environ.get(
    "TURN_RETRANSCRIBE_MAX_S", "20.0"))
# Stale-fragment suppression (post-deploy fleet 2026-08-03: double replies
# 12→25 once the deafness fix started surfacing previously-lost tails). A
# fragment whose speech BEGAN before our delivered reply started playing is
# the tail of the turn we already answered — not a reaction to the reply.
# Absorb it into the next turn's context instead of answering it again.
# A barged (cut) reply never suppresses: the caller interrupted on purpose.
STALE_FRAGMENT_SUPPRESS = os.environ.get(
    "STALE_FRAGMENT_SUPPRESS", "true").lower() == "true"
# A "stale fragment" is the TAIL of a turn the delivered reply already
# answered. Speech longer than this is not a tail — call 909 (2026-09-24,
# RUNBOOK §55.5) absorbed a 6.1 s, fifteen-word question ("tell me a project
# name… which projects are available") and the caller never got an answer.
STALE_FRAGMENT_MAX_MS = float(os.environ.get("STALE_FRAGMENT_MAX_MS", "3000"))
# RUNBOOK §81: the enum-answer re-read without the STT language lock waits
# at most this long; past it the locked transcript stands.
ENUM_RECHECK_TIMEOUT_S = float(os.environ.get("ENUM_RECHECK_TIMEOUT_S", "1.5"))

# Words a finished thought does not end on — dangling conjunctions,
# postpositions and discourse connectors in the three call languages
# (Sarvam appends daṇḍa/period even mid-thought, so punctuation is
# stripped before the check).
_CONTINUATION_CUE_WORDS = frozenset({
    # Hindi / Hinglish
    "जो", "कि", "तो", "और", "या", "पर", "लेकिन", "यानी", "मतलब",
    "बेसिकली", "से", "का", "की", "के", "में", "वाला", "वाले",
    # English
    "and", "but", "so", "or", "because", "like", "basically", "mean",
    "umm", "uhh",
    # Gujarati + romanized
    "અને", "પણ", "કે", "એટલે", "ane", "etle",
})


# Digit dictation tail (phone/pincode/budget read-outs): callers pause
# BETWEEN digit groups — the same continuation hold applies (fleet-audit
# defect #3: the endpointer's semantic profiles can't see text on the batch
# path, so the merge layer is where number bursts get stitched).
_DIGIT_TAIL_CUE = re.compile(r"[\d०-९૦-૯][\d०-९૦-૯\s\-]{1,}\s*[.।॥]?\s*$")


def _ends_with_continuation_cue(text: str) -> bool:
    stripped = (text or "").strip()
    words = stripped.split()
    if not words:
        return False
    if _DIGIT_TAIL_CUE.search(stripped):
        return True
    last = words[-1].strip(".।॥?!,;:\"'").lower()
    return last in _CONTINUATION_CUE_WORDS


# Single-word segments below the duration threshold pass only when they are
# an affirmation/negation or a greeting (H6: "हेलो" — the spelling Sarvam
# actually produces — was missing, so the caller's are-you-alive probe was
# skipped and the dead-air watchdog fired instead).
_SINGLE_WORD_AFFIRMATIONS = frozenset({
    "yes", "yeah", "yep", "ok", "okay", "sure",
    "haan", "ha", "ji", "na", "nahi", "no",
    "nope", "theek", "barobar", "aavjo",
    "हां", "हाँ", "जी", "ठीक", "हा", "ना", "नहीं",
    "હા", "જી", "ઠીક", "ના", "નહીં",
})
_SINGLE_WORD_GREETINGS = frozenset({
    "hello", "hallo", "helo", "hi", "hey", "yello",
    "namaste", "namaskar", "salaam", "salam",
    "kemcho", "kem", "bolo", "boliye", "haanji", "hanji",
    "नमस्ते", "नमस्कार", "हैलो", "हलो", "हेलो", "हॅलो",
    "हैल्लो", "हल्लो", "बोलो", "बोलिए",
    "નમસ્તે", "નમસ્કાર", "હેલો", "હલો", "કેમ", "બોલો",
})


def _is_self_contained_greeting(text: str) -> bool:
    """True for a turn that is nothing but greeting words ("hello", "हेलो").

    The merge window exists to reassemble an utterance ASR split in two, and a
    short transcript arriving while the caller has only just gone quiet is
    normally the head of such a split. A bare greeting is the exception: it is
    a COMPLETE turn — the caller checking the line is alive — and holding it
    buys nothing while adding the very silence that makes them say it again.
    Every other short fragment keeps the full hold.
    """
    toks = re.findall(r"[^\s.,!?।॥]+", (text or "").lower())
    if not toks or len(toks) > 3:
        return False
    if all(t in _SINGLE_WORD_GREETINGS for t in toks):
        return True
    # Two-word spellings of a single greeting ("kem cho"). Joined here rather
    # than added to _SINGLE_WORD_GREETINGS, which the VAD single-word gate also
    # reads — "cho" on its own is not a greeting.
    return "".join(toks) in _SINGLE_WORD_GREETINGS


# Fallback phrases played when STT returns empty on substantial speech,
# or when TTS receives an empty string or fails to play (feature 2 & 5).
_STT_FALLBACK_PHRASES: dict[str, str] = {
    "hi": "माफ़ कीजिए, आपकी आवाज़ स्पष्ट नहीं आई। कृपया दोबारा बताइए।",
    "gu": "માફ કરશો, તમારો અવાજ સ્પષ્ટ સંભળાયો નથી. કૃપા કરીને ફરી કહેશો?",
    "en": "Sorry, I couldn't hear you clearly. Could you please repeat that?",
}
# Minimum speech duration that triggers a language-fallback response on empty STT
_STT_FALLBACK_MIN_SPEECH_MS = 800.0

# Dead-air repair (platform rule, every agent): when the agent has finished
# speaking and the caller produces NO voice activity for this long, the most
# likely explanations on a phone line are "they answered and STT/VAD missed
# it" or "they never heard us" — so the agent says it couldn't hear them and
# re-asks (ConversationRouter.generate_silence_reprompt). Capped so a caller
# who is deliberately silent isn't nagged forever. Defaults ON in telephony;
# CLI/dev mic sessions idle at a desk, so it's opt-in there.
SILENCE_REPROMPT_ENABLED = os.environ.get(
    "SILENCE_REPROMPT_ENABLED",
    "true" if TELEPHONY_MODE else "false").lower() == "true"
SILENCE_REPROMPT_S = float(os.environ.get("SILENCE_REPROMPT_S", "6.0"))
SILENCE_REPROMPT_MAX = int(os.environ.get("SILENCE_REPROMPT_MAX", "2"))

# Silence hangup (platform rule, every agent; per-agent override via
# call_flow.termination.silence_hangup): once every repair reprompt is spent
# and the caller still shows no voice activity for a further grace window,
# say ONE goodbye and end the call — a walked-away caller used to leave the
# channel (and the billing meter) open until the far end gave up. Same
# telephony-only default as the reprompts themselves.
SILENCE_HANGUP_ENABLED = os.environ.get(
    "SILENCE_HANGUP_ENABLED",
    "true" if TELEPHONY_MODE else "false").lower() == "true"
SILENCE_HANGUP_GRACE_S = float(os.environ.get("SILENCE_HANGUP_GRACE_S", "10.0"))

# ── Dead media detection (2026-08-04, call 02819104) ────────────────────────
# A live telephone leg always dithers: µ-law silence decodes to 0, ±8, ±16, ±24
# — never to one repeated value. A stream that flatlines to a SINGLE constant
# sample is a dead media path, not a quiet caller, and the two need opposite
# responses: re-ask a quiet caller, close on a dead line.
#
# In call 02819104 every sample from second 64 to the end was exactly -1, for
# 61 of the call's 125 seconds. -1 is not even reachable from a µ-law decode,
# and the line floor before the cut was ±8, so the carrier leg went flat
# upstream of us. VAD correctly reported no speech — but nothing distinguished
# it from silence, so the agent re-asked twice and delivered a farewell into a
# dead socket over the next 53 seconds, billing TTS the whole way.
DEAD_MEDIA_ENABLED = os.environ.get(
    "DEAD_MEDIA_ENABLED", "true" if TELEPHONY_MODE else "false").lower() == "true"
# Long enough that a codec hiccup or a brief comfort-noise gap can't trip it,
# short enough to save the 53s of wasted spend.
DEAD_MEDIA_S = float(os.environ.get("DEAD_MEDIA_S", "5.0"))

# ── Mailbox beep as audio evidence (RUNBOOK §83) ─────────────────────────────
# The audio-event classifier confirms a record cue (one frequency, 0.3-1.2 s,
# then it stops — audio_events.BEEP_*). It ends the call ONLY on an outbound
# leg where no PERSON has spoken yet (every caller line so far was a
# machine's, or STT rendered none — a Tamil or Marathi greeting is rejected
# as an unsupported script and never reaches the text gate) and only after
# at least MACHINE_BEEP_MIN_SPEECH_MS of caller-side speech was heard before
# it: a greeting played, then the tone. A beep after a human line is a sound
# in their room. Telephony-only like the other close rules.
MACHINE_BEEP_CLOSE_ENABLED = os.environ.get(
    "MACHINE_BEEP_CLOSE_ENABLED",
    "true" if TELEPHONY_MODE else "false").lower() == "true"
MACHINE_BEEP_MIN_SPEECH_MS = float(
    os.environ.get("MACHINE_BEEP_MIN_SPEECH_MS", "1500"))


def _machine_holds_line(pipeline, now: float) -> bool:
    """A call screener or hold announcement has the line (RUNBOOK §63):
    silence is the machine ringing the person, not a caller who missed our
    question — the dead-air repair and silence hangup stand down."""
    fn = getattr(getattr(pipeline, "conversation", None),
                 "awaiting_machine_handoff", None)
    if not callable(fn):
        return False
    try:
        return bool(fn(now))
    except Exception:
        return False


class VoicePipeline:
    """
    Production voice pipeline with independent async layers.

    Each layer runs as a separate task communicating via bounded queues.
    The audio loop never blocks on downstream processing.
    """

    def __init__(
        self,
        audio_source: AudioSource | None = None,
        audio_sink: AudioSink | None = None,
        vad: VoiceActivityDetector | None = None,
        stt: STTRouter | None = None,
        tts: StreamingTTS | None = None,
        conversation: ConversationRouter | None = None,
        event_hub: EventHub | None = None,
        mic_enabled: bool = True,
        skip_greeting: bool = False,
    ) -> None:
        self.audio = audio_source or LocalMicSource(sample_rate=SAMPLE_RATE)
        self.sink = audio_sink or LocalSpeakerSink(sample_rate=SAMPLE_RATE)
        
        # Phase 17: Adjust VAD for telephony mode
        self._session = None
        self._endpointer = None
        if vad is None and VAD_ENGINE == "fsmn":
            try:
                from .vad_stack import FsmnVadStack
                from .audio_events import AudioEventClassifier
                from .call_session import CallSession
                from .endpointer import EndpointController
                self._session = CallSession(
                    direction=getattr(conversation, "call_direction",
                                      "inbound") or "inbound",
                    event_hub=event_hub)
                classifier = AudioEventClassifier(
                    on_label=self._session.on_audio_label,
                    on_beep=self._session.on_beep)
                vad = FsmnVadStack(
                    minimum_threshold=260.0 if TELEPHONY_MODE else 150.0,
                    minimum_rms_floor=60.0 if TELEPHONY_MODE else 50.0,
                    event_sink=classifier)
                self._endpointer = EndpointController()
            except Exception:
                logger.exception(
                    "[VAD-STACK] init failed — falling back to energy VAD")
                vad = None
                self._session = None
                self._endpointer = None
        if vad is None:
            vad = VoiceActivityDetector(
                minimum_threshold=260.0 if TELEPHONY_MODE else 150.0,
                minimum_rms_floor=60.0 if TELEPHONY_MODE else 50.0,
                # Absolute ceiling for threshold to prevent deafness
                maximum_threshold=3000.0 if TELEPHONY_MODE else 1500.0,
            )
        self.vad = vad
        
        self.stt = stt or STTRouter()
        if conversation is None:
            # CLI/dev default only — telephony always passes a conversation.
            from ..agent_definition import get_pramukh_agent
            _cli_def = get_pramukh_agent()
            conversation = ConversationRouter(
                agent_def=_cli_def,
                default_language=_cli_def.languages.default or "hi")
        self.conversation = conversation
        self._hub = event_hub

        # TTS uses the sink
        self.tts = tts or SarvamTTS(audio_sink=self.sink)
        if tts and hasattr(tts, 'set_sink'):
            tts.set_sink(self.sink)

        # Inter-layer queues
        self._stt_queue: asyncio.Queue[AudioSegment] = asyncio.Queue(maxsize=QUEUE_SIZE_STT)
        self._transcript_queue: asyncio.Queue[Transcript] = asyncio.Queue(maxsize=QUEUE_SIZE_TRANSCRIPT)
        self._tts_queue: asyncio.Queue[Response] = asyncio.Queue(maxsize=QUEUE_SIZE_TTS)
        # STT prefetch hand-off: (segment, transcribe_task, submit_ts) tuples.
        # The submitter STARTS transcriptions as segments arrive (bounded by
        # the queue size); the worker consumes them strictly in order, so one
        # slow provider request no longer serializes the segments behind it.
        self._stt_ready_queue: asyncio.Queue = asyncio.Queue(
            maxsize=STT_PREFETCH_AHEAD)

        # State
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._current_generation_task: asyncio.Task | None = None
        self._tts_playing = False
        # A5 delivery epoch (2026-07-30): every supersede event (cont-merge
        # rollback, barge-in drain, deferred-barge kill) bumps this. The TTS
        # player snapshots it at dequeue and re-checks before synthesis — a
        # Response dequeued a moment before its turn was superseded used to
        # synthesize and play anyway (both pre- and post-merge replies were
        # spoken back-to-back on a live call).
        self._delivery_epoch: int = 0
        self._last_playback_start_ts: float = 0.0
        # A4a streaming-STT language pin state.
        self._stt_lang_pin: str | None = None      # BCP-47 or None (=unknown)
        self._stt_lang_last: str | None = None
        self._stt_lang_stable: int = 0
        self._stt_lang_pinned_turns: int = 0
        # Whisper-hint gate (2026-08-03): the conversation language starts as
        # the AGENT's default — hinting Whisper with it before the CALLER has
        # confirmed a language makes Whisper transliterate/translate (Hindi
        # speech under an 'en' hint comes back as Latin junk), which then
        # starves the script-based language evidence: the call sticks in the
        # wrong language. Until established, the fallback runs auto-detect.
        self._lang_initial: str | None = None
        self._lang_caller_established: bool = False
        # Lazily probed: does self.stt.transcribe accept fallback_hint?
        # (Router does; bare providers and older test mocks may not.)
        self._stt_accepts_hint: bool | None = None
        # Live "is the caller currently speaking" flag — read-only signal for
        # external monitoring (dashboard live-call snapshot); never consulted
        # by any pipeline control-flow branch.
        self._user_speaking: bool = False
        self._tts_start_time = 0.0
        self._tts_end_time = 0.0
        self._vad_mute_until = 0.0
        self._barge_in_occurred = False
        self._mic_enabled = mic_enabled
        # Turn-level trace records (one dict per caller turn), enriched as
        # the turn's stages complete and batch-persisted to the call_turns
        # table by the bridge at teardown. Capped so a marathon call can't
        # grow memory unbounded.
        self.turn_traces: list[dict] = []
        # Turn accumulator: tracks full caller text across segments within one
        # turn. Preserved when barge-in fires so the next STT fragment is merged
        # with prior context. Cleared when TTS completes a clean (non-barge-in)
        # response, meaning the caller has been fully answered.
        self._turn_accumulator: str = ""
        # Last fully-processed caller text and the time TTS finished for it.
        # Used to detect short trailing fragments ("for that") that arrive after
        # a clean TTS completion and should be merged with the previous turn.
        self._last_user_text: str = ""
        self._last_tts_end: float = 0.0
        # Connect delivery guard state: per-utterance sequence number,
        # protected flag, when this utterance's audio first reached the
        # sink's playback clock, and the deferred-barge latch (see
        # _connect_barge_defer / the deferred verdict in _conversation_layer).
        self._utt_seq: int = 0
        self._utt_protected: bool = False
        self._utt_first_flush_ts: float = 0.0
        self._bargein_deferred: bool = False
        self._bargein_deferred_utt: int = -1
        self._bargein_deferred_ts: float = 0.0
        # Continuation-merge state (see ENABLE_CONTINUATION_MERGE):
        # window during which resumed caller speech is a probable
        # continuation, timing anchors, and the per-chain merge counter.
        self._continuation_likely_until: float = 0.0
        self._cur_speech_start_ts: float = 0.0
        self._turn_txn_speech_end: float = 0.0
        self._merge_chain_count: int = 0
        # When the last reply started playing, and whether the caller let it
        # finish. `_barge_in_occurred` is consumed by the TTS finally block,
        # so it cannot answer "was the last reply cut?" one turn later —
        # these two can, and the continuation gate needs that answer.
        self._last_reply_tts_start: float = 0.0
        self._last_reply_completed_clean: bool = False
        # The agent line recorded for the reply currently in flight. Cleared
        # the moment any of it reaches the wire; retracted if the reply is
        # superseded first (see _retract_unheard_agent_line).
        self._unheard_agent_line: str = ""
        # How many times THIS caller has carried on through a pause we had
        # already treated as the end of their turn. See TURN_SPLIT_MEMORY.
        self._caller_split_turns: int = 0
        # Number of segments currently being transcribed (merge-window skip
        # signal: a follow-on transcript can only arrive if audio is queued,
        # in flight through STT, or the caller is speaking right now).
        self._stt_inflight: int = 0
        # True while the ACTIVE echo-mute window follows the greeting.
        self._greeting_echo_window: bool = False
        # Issue 4: skip greeting on reconnect — caller already heard it
        self._skip_greeting: bool = skip_greeting
        # Warmup gate — set after all provider warmups complete (feature 6)
        self._warmup_done: asyncio.Event = asyncio.Event()
        # Greeting guard — False until the opening TTS utterance has fully
        # played.  STT fallback phrases and barge-in are suppressed until this
        # is True so startup noise / TTS echo never produce spurious output.
        # When skip_greeting=True we mark it done immediately so barge-in
        # and STT fallback are active from the start.
        self._greeting_done: bool = skip_greeting
        # Counts consecutive STT failures (empty transcript while user spoke).
        # Reset to 0 on any successful transcript.  When it hits the limit the
        # pipeline plays the agent's graceful-close phrase and stops queuing
        # more fallbacks — preventing an infinite "sorry I couldn't hear you" loop.
        self._stt_consecutive_fallbacks: int = 0
        # Dead-air repair state (see _silence_watchdog): last time the VAD saw
        # caller speech, consecutive reprompts without a real caller turn, and
        # when the last reprompt fired.
        self._last_voice_activity: float = 0.0
        # ...and the last time the caller was actually HEARD: a segment that
        # survived the admission gates and reached STT. _last_voice_activity
        # is bumped by every raw VAD frame INCLUDING ones whose segment is
        # then discarded, so on 2026-08-17 it read "caller is talking" for 14
        # straight seconds while every word was being thrown away, and the
        # dead-air reprompt never came due. Repair anchors on this one; the
        # raw signal stays where it belongs (barge-in, echo, merge timing).
        self._last_heard_activity: float = 0.0
        # Connect onset trigger: once per call (see CONNECT_ONSET_TRIGGER).
        self._connect_onset_fired: bool = False
        # When the onset intro finished PLAYING (monotonic; 0 = none yet) and
        # whether the last delivered reply was that intro. Speech that ends
        # after this instant is the caller answering the intro, never the
        # pickup hello it was spoken over (RUNBOOK §65).
        self._onset_play_end_ts: float = 0.0
        self._last_reply_was_onset: bool = False
        self._reprompt_count: int = 0
        self._last_reprompt_ts: float = 0.0
        # Call-termination watchdog state: one-shot latches for the silence
        # hangup (re-armed if the caller comes back) and the max-duration
        # close (never re-armed — it's a hard cap), plus the call-age anchor.
        self._silence_hangup_fired: bool = False
        # §83: a machine has the line — the agent's audio was cut once (the
        # abort is idempotent), caller-side speech heard so far on the call,
        # and the last confirmed beep the watchdog already judged.
        self._machine_abort_done: bool = False
        self._speech_total_ms: float = 0.0
        self._beep_consumed_mono: float = 0.0
        # Dead-media latch: when the caller channel flatlined, and whether the
        # close already fired. Re-arms if real audio comes back.
        self._dead_media_since: float = 0.0
        self._dead_media_fired: bool = False
        self._maxdur_fired: bool = False
        self._call_start_ts: float = time.monotonic()

        # VAD + buffering state
        self._audio_buffer = bytearray()
        self._speech_duration_ms = 0.0
        # Model-confirmed speech inside the buffer, EXCLUDING the pre-speech
        # ring and the hysteresis hold that _speech_duration_ms counts. This is
        # what MIN_REAL_SPEECH_MS gates on — see the constant for why the
        # padded number could never reject a segment.
        self._raw_speech_ms = 0.0
        # Rolling raw frame energy for the SNR guard (see MIN_SPEECH_SNR and
        # SNR_AMBIENT_WINDOW_FRAMES). Label-free by design: every frame goes
        # in, and both terms are derived from the window at flush time.
        self._energy_window: deque[float] = deque(
            maxlen=SNR_AMBIENT_WINDOW_FRAMES)
        # Frames observed since the last segment boundary. Caps how far back
        # the segment slice may reach, so a stale _speech_duration_ms can
        # never pull in audio from before the previous flush.
        self._frames_since_segment = 0
        self._prev_speech = False
        self._speech_frame_streak = 0
        # Tracks whether the previously-started TTS utterance was a filler, so
        # the real response that immediately follows it gets a longer barge-in
        # guard (see FILLER_ECHO_GRACE_MS).  Set per-utterance in the TTS layer.
        self._last_was_filler = False
        self._barge_in_min_elapsed_ms = BARGE_IN_MIN_ELAPSED_MS
        self._frame_count = 0
        self._speech_onset_frames = 0  # debounce: frames since speech started
        self._silence_frames = 0       # debounce: frames since last speech
        self._pre_speech_buffer: list[bytes] = []  # look-back to recover onset clip
        self._echo_gate_speech_frames: int = 0    # sustained frames during echo cooldown
        self._echo_preserve_logged: bool = False  # once-per-window log latch
        # Playback shadow capture (fleet-audit defect #1) — see constants.
        self._shadow_frames: list[bytes] = []
        self._shadow_speech_ms: float = 0.0
        self._shadow_started_ts: float = 0.0
        self._shadow_until: float = 0.0          # claim deadline after TTS end
        self._shadow_seeded: bool = False        # buffer holds shadow audio
        self._segment_has_shadow: bool = False
        self._current_tts_text: str = ""         # for the echo-text guard
        self._prev_tts_text: str = ""

        # Diagnostic counters — logged at session end to trace audio path
        self._vad_frames_seen: int = 0
        self._vad_speech_frames: int = 0
        self._segments_created: int = 0
        self._segments_sent_to_stt: int = 0
        # Caller speech we could not keep up with. Any non-zero value is an
        # alarm condition (voice_health already has the hook) — it means the
        # agent answered without hearing part of what was said.
        self._stt_queue_drops: int = 0
        # Segments the real-speech floor rejected — paid STT calls not made.
        self._segments_dropped_no_speech: int = 0
        # Subset of the above dropped by the SNR arm while carrying real
        # model-confirmed speech — the deafness signature, counted separately
        # so the fleet rate is measurable instead of buried (2026-08-17).
        self._segments_dropped_snr: int = 0
        self._snr_drop_streak: int = 0
        # Tail of the previous fragment of the turn in progress, for the
        # endpointer's semantic profiles (see _endpoint_hint_text).
        self._last_fragment_text: str = ""
        self._last_fragment_ts: float = 0.0

        # Phase 17: Hysteresis tuning
        self._speech_onset_min_frames =  SPEECH_ONSET_MIN_FRAMES
        self._speech_hold_min_frames = SPEECH_HOLD_MIN_FRAMES
       

        logger.info(
            "[PIPELINE] BARGE_IN=%s TELEPHONY=%s echo_cooldown=%.0fms "
            "hard_flush=%d_frames min_speech=%.0fms",
            ENABLE_BARGE_IN, TELEPHONY_MODE, TTS_ECHO_COOLDOWN_MS,
            HARD_FLUSH_SILENCE_FRAMES, MIN_SPEECH_MS,
        )
        if TELEPHONY_MODE:
            logger.info(
                "[VAD] TELEPHONY_MODE=True: onset=%d, hold=%d, thr_min=%.1f, rms_floor=%.1f",
                self._speech_onset_min_frames, self._speech_hold_min_frames,
                self.vad._minimum_threshold, self.vad._minimum_rms_floor,
            )

        # Turn manager — tuned for phone call cadence.
        # Longer silence threshold (1400 ms) gives callers time to finish their
        # thought before the agent jumps in.  Grace window 300 ms absorbs
        # trailing syllables.  min_words raised so single-frame noise doesn't
        # trigger a turn.
        self._turn = TurnManager(TurnManagerConfig(
            silence_threshold_ms=TURN_SILENCE_THRESHOLD_MS,
            grace_window_ms=300.0,
            min_words_short=2,
            min_words_long=3,
        ))

    async def start(self) -> None:
        self._running = True
        logger.info("=" * 60)
        logger.info("[PIPELINE] Starting voice pipeline")
        logger.info("=" * 60)

        # Await all provider warmups before starting the pipeline layers so the
        # first live caller turn never pays TLS/API handshake cost (feature 6).
        # Issue 3: only log "queued" for warmups that will actually run
        # (guarded by class-level _warmup_done_flag inside each provider).
        warmup_coros = []
        primary_stt = getattr(self.stt, "_primary", None)
        if primary_stt is not None and hasattr(primary_stt, "warmup"):
            if not getattr(primary_stt.__class__, "_warmup_done_flag", False):
                logger.info("[WARMUP] STT primary warmup queued")
            warmup_coros.append(primary_stt.warmup())
        if hasattr(self.tts, "warmup"):
            if not getattr(self.tts.__class__, "_warmup_done_flag", False):
                logger.info("[WARMUP] TTS warmup queued")
            warmup_coros.append(self.tts.warmup())
        if hasattr(self.conversation, "warmup"):
            if not getattr(self.conversation.__class__, "_warmup_done_flag", False):
                logger.info("[WARMUP] LLM warmup queued")
            warmup_coros.append(self.conversation.warmup())

        # CRITICAL LATENCY FIX: warm up in the BACKGROUND. The greeting is canned
        # TTS and does NOT need the LLM — but LLM warmup is a real ~4-5s call to
        # a US endpoint. Awaiting it here used to delay the caller's first word by
        # ~10s. Now the greeting plays immediately while warmup runs concurrently
        # (it finishes long before the caller's first reply needs STT/LLM).
        if warmup_coros:
            async def _bg_warmup():
                try:
                    results = await asyncio.wait_for(
                        asyncio.gather(*warmup_coros, return_exceptions=True),
                        timeout=8.0,
                    )
                    for r in results:
                        if isinstance(r, Exception):
                            logger.warning("[WARMUP] Error: %s", r)
                except asyncio.TimeoutError:
                    logger.warning("[WARMUP] Timed out — proceeding anyway")
                finally:
                    self._warmup_done.set()
                    logger.info("[WARMUP] Background warmup complete")
            asyncio.create_task(_bg_warmup())
        else:
            self._warmup_done.set()

        self._tasks = [
            asyncio.create_task(self._audio_vad_layer(), name="audio_vad"),
            asyncio.create_task(self._stt_submitter(), name="stt_submitter"),
            asyncio.create_task(self._stt_worker(), name="stt_worker"),
            asyncio.create_task(self._conversation_layer(), name="conversation"),
            asyncio.create_task(self._tts_layer(), name="tts"),
            asyncio.create_task(self._silence_watchdog(), name="silence_watchdog"),
        ]

        # Issue 4: skip greeting on reconnect — caller already heard it.
        if self._skip_greeting:
            logger.info("[PIPELINE] Greeting suppressed — already greeted this call (reconnect)")
            return

        # Wait for the first inbound audio frame before playing the greeting.
        # With mod_audio_stream the WebSocket connects when the dialplan executes
        # the application — which may be before FreeSWITCH sends CHANNEL_ANSWER.
        # Waiting for the first real audio frame guarantees the call is live.
        if hasattr(self.audio, '_pushed_frames'):
            logger.info("[PIPELINE] Waiting for first audio frame to confirm call is established...")
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if getattr(self.audio, '_pushed_frames', 0) > 0:
                    logger.info("[PIPELINE] First audio frame received — call established, queuing greeting")
                    break
                await asyncio.sleep(0.025)
            else:
                logger.warning("[PIPELINE] No audio in 5s — playing greeting anyway (call may not be answered yet)")

        # Agent speaks first — bilingual greeting
        opening = await self.conversation.generate_opening()
        try:
            self._tts_queue.put_nowait(Response(
                text=opening.text,
                is_shortcut=True,
                timestamp=time.monotonic(),
                protected=True,
            ))
            logger.info("[PIPELINE] Agent opening: %r", opening.text)
            if self._hub is not None:
                self._hub.emit_nowait({
                    "type": "agent",
                    "text": opening.text,
                    "llm_latency_ms": 0,
                    "first_chunk_ms": 0,
                    "total_latency_ms": 0,
                })
        except asyncio.QueueFull:
            logger.warning("[PIPELINE] Could not enqueue opening greeting")

    async def stop(self) -> None:
        self._running = False
        # Finalize any pending turn transaction BEFORE teardown so deferred
        # side effects (calendar booking) flush and the bridge's persistence
        # reads fully-committed state.
        if hasattr(self.conversation, "commit_pending_turn"):
            try:
                self.conversation.commit_pending_turn()
            except Exception:
                logger.exception("[PIPELINE] commit_pending_turn at stop failed")
        await self.audio.stop()
        if self._session is not None:
            self._session.set("CALL_ENDED")
        if hasattr(self.vad, "close"):
            try:
                self.vad.close()
            except Exception:
                pass
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("[PIPELINE] All layers stopped")

    async def run_until_shutdown(self) -> None:
        """Run pipeline until Ctrl+C or signal."""
        await self.start()

        shutdown = asyncio.Event()

        def _signal():
            logger.info("[PIPELINE] Shutdown signal received")
            shutdown.set()

        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGINT, _signal)
            loop.add_signal_handler(signal.SIGTERM, _signal)
        except NotImplementedError:
            pass

        try:
            await shutdown.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await self.stop()

    # -----------------------------------------------------------------------
    # Layer 1: Audio + VAD + Turn Detection
    # -----------------------------------------------------------------------

    async def _audio_vad_layer(self) -> None:
        """
        Captures audio, runs VAD, buffers speech, detects turns.
        When a turn completes, enqueues an AudioSegment for STT.
        NEVER blocks on downstream — uses put_nowait with overflow handling.
        """
        try:
            async for frame in self.audio.frames():
                if not self._running:
                    break
                self._frame_count += 1
                await self._process_vad_frame(frame)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[AUDIO] Layer crashed")
        finally:
            logger.info(
                "[AUDIO] Layer stopped: frames=%d vad_seen=%d vad_speech=%d "
                "segments_created=%d segments_sent_to_stt=%d "
                "dropped_no_real_speech=%d dropped_as_line_noise=%d",
                self._frame_count, self._vad_frames_seen, self._vad_speech_frames,
                self._segments_created, self._segments_sent_to_stt,
                self._segments_dropped_no_speech, self._segments_dropped_snr,
            )

    @property
    def mic_enabled(self) -> bool:
        return self._mic_enabled

    def set_mic_enabled(self, enabled: bool) -> None:
        """Toggle mic gating. When disabled, frames are discarded before VAD —
        no calibration progress, no speech detection, no barge-in."""
        if enabled == self._mic_enabled:
            return
        self._mic_enabled = enabled
        logger.info("[MIC] %s", "enabled" if enabled else "disabled")
        if not enabled:
            # Drop any in-flight speech buffer so a partial utterance doesn't
            # get committed when the mic comes back on.
            self._reset_vad_state()
            self._speech_frame_streak = 0
        if self._hub is not None:
            self._hub.emit_nowait({"type": "mic_state", "enabled": enabled})

    async def _process_vad_frame(self, frame: bytes) -> None:
        # Mic gate: skip speech detection / barge-in while disabled, but keep
        # advancing VAD calibration on disabled-period ambient. Otherwise the
        # first ~600ms of speech after the user enables mic is silently
        # consumed by the calibration window. is_speech() returns False during
        # calibration, so this never triggers downstream processing.
        # Media liveness runs BEFORE the mic gate and before every early
        # return below: in the live incident the channel died while the agent
        # was mid-playback, which is exactly when the rest of this method
        # short-circuits.
        if DEAD_MEDIA_ENABLED:
            self._track_media_liveness(frame)

        if not self._mic_enabled:
            if not self.vad.calibrated:
                self.vad.is_speech(frame)
            self._user_speaking = False
            return

        self._vad_frames_seen += 1
        raw_speech = self.vad.is_speech(frame)
        # SNR guard bookkeeping — BEFORE every early return below (playback
        # mute, echo cooldown), so the floor estimate sees the whole call and
        # a segment that claimed shadow audio can still find its own frames.
        self._energy_window.append(self.vad.last_energy)
        self._frames_since_segment += 1
        if raw_speech:
            self._vad_speech_frames += 1
        self._user_speaking = bool(raw_speech) and not self._tts_playing

        # === FULL MUTE while TTS is playing — only allow barge-in ===
        # Barge-in uses a stricter threshold than turn-detection so TTS
        # echo / ambient noise can't masquerade as a sustained interrupt.
        if self._tts_playing:
            # Shadow capture: keep what the caller says OVER agent audio.
            # Runs before every return in this block so no path can discard
            # speech-energy frames anymore. Greeting included — capture never
            # interrupts anything, it only prevents loss.
            if ENABLE_PLAYBACK_SHADOW:
                self._shadow_capture(frame)
            # Issue 4: never allow barge-in during the opening greeting.
            # The greeting echo (via mod_audio_stream) is often loud enough to
            # trip the barge-in threshold, cutting the greeting short and leaving
            # no echo-cooldown window.  Once _greeting_done is True, barge-in
            # is subject to the normal ENABLE_BARGE_IN flag.
            if not ENABLE_BARGE_IN or not self._greeting_done:
                if not self._greeting_done:
                    logger.debug("[BARGE-IN] Suppressed — greeting not yet complete")
                return
                    
            # Continuation-likely window: right after a fast endpoint the
            # caller may still be finishing their sentence — hear a soft
            # continuation that the 2× echo-reject threshold would drop.
            # Bounded to CONTINUATION_WINDOW_MS past their speech end; outside
            # it, today's constants apply unchanged.
            in_cont_window = (
                ENABLE_CONTINUATION_MERGE
                and self._greeting_done
                and time.monotonic() < self._continuation_likely_until
            )
            if in_cont_window:
                barge_threshold = max(
                    self.vad.threshold * CONTINUATION_BARGE_FACTOR, 300.0)
            else:
                barge_threshold = self.vad.barge_in_threshold
            # B2: while capturing an email the agent's prompts are short and
            # the caller often talks over them mid-spelling — demand a much
            # louder interrupt so the capture prompt actually finishes.
            # C4: spelling-context turns only — an armed-but-digressing turn
            # (booking, Zoom) keeps normal barge sensitivity.
            if (CAPTURE_MODE and self.conversation is not None
                    and getattr(self.conversation, "_capture_slot", None)
                    and getattr(self.conversation, "capture_expecting_input",
                                lambda: True)()):
                barge_threshold *= 2.0
            loud = self.vad.last_energy > barge_threshold

            if loud:
                self._speech_frame_streak += 1
            else:
                self._speech_frame_streak = 0

            elapsed = (time.monotonic() - self._tts_start_time) * 1000

            # Incremental TTS flushing means injected audio can already be
            # playing on the call while speak() is still synthesizing later
            # parts — its echo returns via mod_audio_stream DURING
            # _tts_playing. While the sink reports playback in flight,
            # require a longer sustained-speech streak so echo can't cancel
            # the rest of the utterance. A real interrupter clears the bar in
            # ~200ms; LocalSpeakerSink has no playback_busy_until, so CLI
            # mode is unaffected.
            min_frames = BARGE_IN_MIN_FRAMES
            if in_cont_window:
                min_frames = CONTINUATION_BARGE_FRAMES
            elif time.monotonic() < getattr(self.sink, 'playback_busy_until', 0.0):
                min_frames = BARGE_IN_MIN_FRAMES * 2

            if (
                self._speech_frame_streak >= min_frames
                and elapsed > self._barge_in_min_elapsed_ms
            ):
                # Connect delivery guard: never let a burst of energy kill a
                # reply the caller has not heard yet (the repeated-"hello"
                # loop: each hello killed the answer before its first WAV
                # was flushed, so the caller heard only dead air). Latch the
                # deferral; the STT verdict in _conversation_layer kills the
                # playback iff the interruption was more than a bare hello.
                _defer_now = time.monotonic()
                if self._connect_barge_defer(_defer_now):
                    if not self._bargein_deferred:
                        self._bargein_deferred = True
                        self._bargein_deferred_utt = self._utt_seq
                        self._bargein_deferred_ts = _defer_now
                        logger.info(
                            "[BARGE-IN] Deferred (delivery window) "
                            "frames=%d energy=%.0f played=%.2fs — "
                            "awaiting STT verdict",
                            self._speech_frame_streak,
                            self.vad.last_energy,
                            max(0.0, _defer_now - self._utt_first_flush_ts)
                            if self._utt_first_flush_ts > 0.0 else 0.0,
                        )
                    # RUNBOOK §78 (opt-in): the guard exists for a 300 ms
                    # "hello?"; a caller still producing sustained speech
                    # this long after the deferral latched is interrupting,
                    # and the STT verdict may never come (call b5452b23:
                    # 1.3 s of speech sat in the echo gate for 10 s, came
                    # back empty, and the reply played over the caller).
                    if not self._deferred_barge_override_due(_defer_now):
                        self._speech_frame_streak = 0
                        return
                    logger.info(
                        "[BARGE-IN] Deferred kill overridden — caller speech "
                        "sustained %.0fms past the deferral",
                        (_defer_now - self._bargein_deferred_ts) * 1000.0)
                    self._bargein_deferred = False

                logger.info(
                    "[BARGE-IN] Interrupting TTS "
                    "frames=%d energy=%.0f threshold=%.0f cont_window=%s",
                    self._speech_frame_streak,
                    self.vad.last_energy,
                    barge_threshold,
                    in_cont_window,
                )

                # Media-layer abort FIRST: WAVs already injected into
                # FreeSWITCH keep playing no matter what we cancel locally.
                # interrupt_playback() sends uuid_break to the channel, then
                # drains the sink's queued WAVs (LocalSpeakerSink has no such
                # method — CLI mode is unaffected).
                interrupt = getattr(self.sink, "interrupt_playback", None)
                if interrupt is not None:
                    await interrupt()

                await self.tts.stop()

                self._tts_playing = False
                self._barge_in_occurred = True
                if self._session is not None:
                    self._session.set("USER_INTERRUPTED", "barge-in")
                if self.turn_traces:
                    self.turn_traces[-1]["barged_in"] = True
                self._vad_mute_until = 0.0
                self._speech_frame_streak = 0
                # The extended post-filler barge-in guard must not outlive the
                # filler/response pair it protects: without this reset, the
                # NEXT unrelated utterance after a barge-in inherited the
                # 1500ms FILLER_ECHO_GRACE window and was deaf to
                # interruption for 1.5s on a turn that had no filler.
                self._last_was_filler = False

                self._flush_pipeline_queues()
                if (
                    self._current_generation_task
                    and not self._current_generation_task.done()
                    ):
                    self._current_generation_task.cancel()
                    
                # Fleet-audit fix: the buffer holds caller speech captured
                # BEFORE our reply started playing (appends pause during
                # playback) — the head of the very utterance that is now
                # barging in. The old reset wiped it; preserve it through
                # the reset, then reattach the playback-era shadow after it.
                _pre_barge_audio = bytes(self._audio_buffer)
                _pre_barge_ms = self._speech_duration_ms
                _pre_barge_real_ms = self._raw_speech_ms
                _pre_barge_start = self._cur_speech_start_ts
                self._reset_vad_state()
                if _pre_barge_ms >= 200.0:
                    self._audio_buffer.extend(_pre_barge_audio)
                    self._speech_duration_ms = _pre_barge_ms
                    self._raw_speech_ms = _pre_barge_real_ms
                    self._cur_speech_start_ts = _pre_barge_start
                    self._prev_speech = True
                    logger.info(
                        "[SHADOW] Preserved %.0fms pre-playback caller audio "
                        "through barge-in reset", _pre_barge_ms)
                self._claim_shadow("barge-in")

            else:
                return
            
            # Original logic disabled below:
            # if not ENABLE_BARGE_IN:
            #     return
            #
            # loud = self.vad.last_energy > self.vad.barge_in_threshold
            # if loud:
            #     self._speech_frame_streak += 1
            # else:
            #     self._speech_frame_streak = 0
            #
            # elapsed = (time.monotonic() - self._tts_start_time) * 1000
            # if (self._speech_frame_streak >= BARGE_IN_MIN_FRAMES
            #         and elapsed > BARGE_IN_COOLDOWN_MS):
            #     logger.info("[BARGE-IN] Sustained loud speech (%d frames, energy=%.0f, thr=%.0f) — interrupting TTS",
            #                 self._speech_frame_streak, self.vad.last_energy, self.vad.barge_in_threshold)
            #     await self.tts.stop()
            #     self._tts_playing = False
            #     self._barge_in_occurred = True
            #     self._vad_mute_until = 0.0
            #     self._speech_frame_streak = 0
            #     self._flush_pipeline_queues()
            #     self._reset_vad_state()
            # return
            # PHASE17_DEBUG_DISABLE_BARGE_IN_END

        # === ECHO COOLDOWN after TTS ends naturally ===
        # During this window audio is likely TTS residue. Discard it unless
        # sustained real speech is detected, in which case exit early.
        if time.monotonic() < self._vad_mute_until:
            # raw_speech ALONE cannot open this gate. vad_stack.py's contract:
            # the model classifies our own TTS echo as speech, and frame energy
            # is the only echo discriminator in the stack — so counting the
            # model verdict here was counting the one signal that cannot tell
            # the agent from the caller. Require both.
            _echo_exit_floor = self.vad.threshold * ECHO_EXIT_ENERGY_FACTOR
            if raw_speech and self.vad.last_energy >= _echo_exit_floor:
                self._echo_gate_speech_frames = getattr(self, '_echo_gate_speech_frames', 0) + 1
            else:
                self._echo_gate_speech_frames = 0

            # Post-greeting window exits earliest ("hello" is the most
            # predictable speech event of the call); continuation-likely
            # windows exit slightly early; everything else keeps the default.
            exit_frames = ECHO_EARLY_EXIT_FRAMES
            if self._greeting_echo_window:
                exit_frames = GREETING_ECHO_EARLY_EXIT_FRAMES
            elif (ENABLE_CONTINUATION_MERGE
                    and time.monotonic() < self._continuation_likely_until):
                exit_frames = CONTINUATION_ECHO_EXIT_FRAMES

            if self._echo_gate_speech_frames >= exit_frames:
                # Sustained loud speech during cooldown — this is a real caller,
                # not echo. Exit the mute window early and let speech through.
                logger.info(
                    "[ECHO-GATE] Early exit: %d sustained speech frames detected "
                    "(energy=%.0f ≥ %.0f, %.0fms cooldown remaining) — "
                    "treating as real caller speech",
                    self._echo_gate_speech_frames,
                    self.vad.last_energy, _echo_exit_floor,
                    (self._vad_mute_until - time.monotonic()) * 1000,
                )
                self._vad_mute_until = 0.0
                self._echo_gate_speech_frames = 0
                # Fall through to normal VAD processing below
            else:
                if self._audio_buffer:
                    # Fleet-audit fix: a non-empty buffer entering this gate
                    # was captured BEFORE the agent spoke (appends never run
                    # during playback or this gate) — it is the caller's
                    # unfinished segment, not TTS residue. Wiping it here is
                    # how the tail of a split utterance used to vanish
                    # entirely. Preserve substantial speech; let it flush
                    # after the gate.
                    if (self._speech_duration_ms >= 300.0
                            or self._shadow_seeded):
                        # Once per cooldown window, not once per 20ms frame:
                        # this line alone was 222 of the 1298 log lines in an
                        # 86-second call.
                        if not self._echo_preserve_logged:
                            self._echo_preserve_logged = True
                            logger.info(
                                "[ECHO-GATE] Preserving %.0fms pre-TTS caller "
                                "segment through cooldown",
                                self._speech_duration_ms)
                    else:
                        logger.debug(
                            "[ECHO-GATE] Discarding %d bytes of post-TTS audio "
                            "(%.0fms cooldown remaining, echo_frames=%d)",
                            len(self._audio_buffer),
                            (self._vad_mute_until - time.monotonic()) * 1000,
                            self._echo_gate_speech_frames,
                        )
                        self._audio_buffer.clear()
                        self._speech_duration_ms = 0.0
                        self._raw_speech_ms = 0.0
                        self._reset_segment_energy()
                        self._segment_has_shadow = False
                        self._turn.flush()
                self._prev_speech = False
                self._speech_onset_frames = 0
                self._silence_frames = 0
                return

        # === Normal VAD processing ===
        if raw_speech:
            self._speech_onset_frames += 1
            self._silence_frames = 0
            self._last_voice_activity = time.monotonic()
            # Model-confirmed speech, counted here and ONLY here so it is
            # independent of both padding sources: the onset debounce (which
            # suppresses `speech` for the first frame) and the hysteresis hold
            # (which keeps `speech` true through 200ms of trailing silence).
            self._raw_speech_ms += 20.0
            self._speech_total_ms += 20.0
            self._maybe_connect_onset()
        else:
            self._silence_frames += 1
            self._speech_onset_frames = 0
            # Look-back ring so onset clip is recoverable. Energy engine:
            # 2 frames (decision is instant). FSMN engine: 20 frames
            # (400ms) — the model confirms speech ~100-300ms after true
            # onset, and the ring is what recovers those first syllables
            # into the STT buffer.
            self._pre_speech_buffer.append(frame)
            _ring_max = (20 if self._endpointer is not None
                         else SPEECH_ONSET_MIN_FRAMES)
            if len(self._pre_speech_buffer) > _ring_max:
                self._pre_speech_buffer.pop(0)

        if raw_speech and self._speech_onset_frames < self._speech_onset_min_frames and not self._prev_speech:
            speech = False
        elif not raw_speech and self._silence_frames < self._speech_hold_min_frames and self._prev_speech:
            speech = True
        else:
            speech = raw_speech

        if speech:
            if not self._prev_speech:
                logger.info("[VAD] Speech start")
                # Continuation-merge anchor: when the caller actually began
                # speaking (accounting for the recovered pre-onset frames).
                #
                # ONLY on the first onset of a segment. Speech resuming after
                # a within-segment hangover gap re-entered this branch with
                # `_pre_speech_buffer` already cleared, so the anchor jumped
                # to `now` — which is how the audit transcript ended up
                # carrying a 0.08s span and a 16.30s span for segments the
                # log records at 980ms and 940ms. The spans feed the ASR
                # replay corpus, so a wrong one silently poisons WER.
                if not self._audio_buffer:
                    self._cur_speech_start_ts = (
                        time.monotonic() - 0.02 * len(self._pre_speech_buffer)
                    )
                # Speech resuming shortly after playback: the utterance may
                # have BEGUN over the agent's audio — reattach that head
                # from the shadow ring (chronologically before the pre-ring).
                if (ENABLE_PLAYBACK_SHADOW and self._shadow_frames
                        and time.monotonic() < self._shadow_until):
                    self._claim_shadow("post-playback onset")
                # Prepend buffered pre-onset frames to recover clipped onset
                if self._pre_speech_buffer:
                    for pre_frame in self._pre_speech_buffer:
                        self._audio_buffer.extend(pre_frame)
                        self._speech_duration_ms += 20.0
                    self._pre_speech_buffer.clear()
            self._turn.mark_speech()
            self._audio_buffer.extend(frame)
            self._speech_duration_ms += 20.0
            if self._session is not None:
                # Monologue-length tick (outbound IVR/voicemail heuristics).
                self._session.on_speech_run(self._speech_duration_ms)

            if len(self._audio_buffer) > MAX_BUFFER_BYTES:
                del self._audio_buffer[:len(self._audio_buffer) - MAX_BUFFER_BYTES]

            # No mid-turn transcript exists on the batch path — the POST only
            # happens after the endpoint fires. Feed the TurnManager proxy
            # tokens so its length heuristics still advance; its sentence-end
            # heuristic cannot match on proxies, which is the pre-streaming
            # behaviour.
            proxy_words = max(1, int(self._speech_duration_ms / 300))
            self._turn.update_text(" ".join(["x"] * proxy_words))
        else:
            if self._prev_speech:
                logger.info("[VAD] Speech end (%.0fms)", self._speech_duration_ms)

        self._prev_speech = speech

        # TurnManager OR hard telephony silence flush
        should_flush = self._turn.should_trigger()

        # Hard flush: after enough accumulated speech, a long consecutive
        # silence window triggers a flush even if TurnManager hasn't fired.
        # Legacy energy engine: fixed HARD_FLUSH_SILENCE_FRAMES. FSMN engine
        # (VAD_ENGINE=fsmn): the model already absorbed FSMN_MAX_END_SIL_MS
        # of pause tolerance before is_speech flipped false, so the
        # EndpointController decides with small semantic-aware residuals
        # (fast on complete sentences, hold on dangling/digit dictation).
        if (
            not speech
            and self._speech_duration_ms >= self._min_speech_ms(time.monotonic())
        ):
            if self._endpointer is not None:
                # The batch path has no transcript for the utterance in
                # progress, so this used to pass "" — which made
                # EndpointController structurally incapable of ever leaving its
                # "default" profile (both hold and fast_complete are gated on
                # `if text:`). Every [ENDPOINT] line in production read
                # profile=default, and _DIGIT_TAIL — the rule written so budget
                # and phone-number dictation isn't chopped between bursts — had
                # never once executed on a live call.
                #
                # What IS available is the text of the PREVIOUS fragment of this
                # same turn: during a continuation chain ("one lakh" ⟶ pause ⟶
                # "twenty thousand") the head has already come back from STT
                # before this endpoint decision runs. Its tail is exactly the
                # signal the profiles want. Outside the continuation window
                # there is no chain and "" is still the honest answer.
                _flush_due = self._endpointer.should_flush(
                    self._silence_frames,
                    self._speech_duration_ms,
                    self._endpoint_hint_text(),
                    False,
                )
            else:
                _flush_due = self._silence_frames >= HARD_FLUSH_SILENCE_FRAMES
            if _flush_due:
                logger.info(
                    "[TURN] Hard-flush: duration=%.0fms silence_frames=%d",
                    self._speech_duration_ms, self._silence_frames,
                )
                should_flush = True

        if should_flush:
            logger.info(
                "[TURN] Triggered flush speech_ms=%.0f silence_frames=%d buffer=%d",
                self._speech_duration_ms,
                self._silence_frames,
                len(self._audio_buffer),
            )

            _now_gate = time.monotonic()
            _too_short = self._speech_duration_ms < self._min_speech_ms(_now_gate)
            # The floor that actually bites: padded duration clears 300ms on a
            # single speech frame, real speech does not. Everything below this
            # is a click, a breath, or residual echo — shipping it costs a paid
            # Sarvam call, a turn of latency, and (worse) a hallucinated
            # transcript the LLM then answers.
            _no_speech = self._raw_speech_ms < self._min_real_speech_ms(_now_gate)
            _snr = self._segment_snr()
            # Measured once: the ratio alone is what made the 2026-08-17
            # inversion invisible in the log, so both terms travel with it.
            _speech_e = _fmt_energy(self._segment_speech_energy())
            _floor_e = _fmt_energy(self._ambient_energy())
            # Fails open three times over: _segment_snr() returns None when
            # there is too little to judge, a segment carrying substantial
            # speech is never judged on energy at all, and neither is one
            # that is loud in absolute terms.
            _dead_line = (
                _snr is not None
                and self._raw_speech_ms < SNR_ENFORCE_BELOW_MS
                and _snr < MIN_SPEECH_SNR
                and not self._segment_is_loud())
            if _too_short or _no_speech or _dead_line:
                logger.info(
                    "[TURN] Discarded: padded=%.0fms real_speech=%.0fms "
                    "snr=%s (speech=%s floor=%s) (floors %.0f/%.0f/%.1f) — %s",
                    self._speech_duration_ms, self._raw_speech_ms,
                    f"{_snr:.1f}x" if _snr is not None else "n/a",
                    _speech_e, _floor_e,
                    self._min_speech_ms(_now_gate),
                    self._min_real_speech_ms(_now_gate), MIN_SPEECH_SNR,
                    "too short" if _too_short
                    else ("no real speech" if _no_speech
                          else "no signal above line noise"),
                )
                if not _too_short:
                    self._segments_dropped_no_speech += 1
                # A drop on the SNR arm carrying real model-confirmed speech
                # is the 2026-08-17 deafness signature. One is ordinary line
                # noise; a streak means the caller is talking and we are
                # throwing it away — say so while the call is still live,
                # instead of only in the end-of-session counter.
                if _dead_line and not _too_short and not _no_speech:
                    self._segments_dropped_snr += 1
                    self._snr_drop_streak += 1
                    if self._snr_drop_streak >= SNR_DROP_STREAK_WARN:
                        logger.warning(
                            "[TURN] %d consecutive segments dropped as line "
                            "noise while carrying real speech (last: "
                            "%.0fms real, snr=%.2fx, speech=%s floor=%s) — "
                            "the caller may be inaudible to this call",
                            self._snr_drop_streak, self._raw_speech_ms,
                            _snr if _snr is not None else -1.0,
                            _speech_e, _floor_e)
                else:
                    self._snr_drop_streak = 0
                self._audio_buffer.clear()
                self._speech_duration_ms = 0.0
                self._raw_speech_ms = 0.0
                self._reset_segment_energy()
                self._segment_has_shadow = False
                self._shadow_seeded = False
                self._turn.flush()
                return

            _now_flush = time.monotonic()
            _speech_end_ts = _now_flush - self._silence_frames * 0.02
            # A span must be consistent with the audio it labels. When the
            # ONSET anchor is missing or the arithmetic disagrees with the
            # buffered duration by more than a second, that anchor is not
            # corpus-grade — a corpus row with a bad span is worse than a
            # corpus row with none.
            #
            # But only the ONSET is ever in doubt. `_speech_end_ts` is derived
            # from the flush moment minus the counted silence, so it is sound
            # whatever the onset did, and zeroing it too is what broke the
            # anti-double-reply machinery: every continuation gate
            # (`_turn_txn_speech_end`, the CONT-MERGE gap, stale-fragment
            # suppression) requires a non-zero span, and the callers who
            # trigger the gate — the ones who pause inside a segment, so
            # wall-clock ≫ counted speech — are exactly the callers those
            # gates exist for. One dropped span also POISONED the next turns:
            # `_turn_txn_speech_end` kept a stale value, so the following
            # utterance measured a ~10s gap and was answered separately.
            # Live 2026-09-09 05:43:21 — 5.70s measured vs 0.86s buffered,
            # then two full pitches back to back.
            #
            # So: keep the end, and reconstruct the onset as the tightest
            # honest lower bound (end − buffered speech). Flag it so the
            # corpus keeps publishing nothing rather than a guess.
            _span_estimated = False
            if self._cur_speech_start_ts <= 0.0 or abs(
                    (_speech_end_ts - self._cur_speech_start_ts) * 1000.0
                    - self._speech_duration_ms) > 1000.0:
                if self._cur_speech_start_ts > 0.0:
                    logger.warning(
                        "[VAD] Implausible speech span "
                        "(%.2fs measured vs %.2fs buffered) — onset "
                        "reconstructed for turn-taking, withheld from corpus",
                        _speech_end_ts - self._cur_speech_start_ts,
                        self._speech_duration_ms / 1000.0)
                self._cur_speech_start_ts = max(
                    0.0, _speech_end_ts - self._speech_duration_ms / 1000.0)
                _span_estimated = True
            segment = AudioSegment(
                audio=bytes(self._audio_buffer),
                duration_ms=self._speech_duration_ms,
                timestamp=_now_flush,
                speech_start_ts=self._cur_speech_start_ts,
                speech_end_ts=_speech_end_ts,
                span_estimated=_span_estimated,
                shadow=self._segment_has_shadow,
            )
            self._segment_has_shadow = False
            self._shadow_seeded = False
            self._segments_created += 1

            # Arm the continuation-likely window: for CONTINUATION_WINDOW_MS
            # past the caller's speech end, resumed speech is treated as a
            # probable continuation (relaxed hearing above; merge decision in
            # the conversation layer).
            #
            # NOT widened for splitter callers, deliberately. Inside this
            # window the barge-in bar drops to ~1.3x the VAD floor and the
            # sustained-speech requirement halves, so stretching it to 6s
            # would give the agent's OWN echo three times as long to cancel
            # its own reply. Measured 2026-09-16: barge-in fired on all 9
            # real interruptions across today's calls — the caller resuming
            # is heard. What was broken is the agent STARTING during the
            # pause, which the merge hold above now prevents.
            if ENABLE_CONTINUATION_MERGE:
                self._continuation_likely_until = (
                    _speech_end_ts + CONTINUATION_WINDOW_MS / 1000.0
                )

            _real_ms = self._raw_speech_ms
            segment.speech_ms = float(_real_ms or 0.0)
            self._audio_buffer.clear()
            self._speech_duration_ms = 0.0
            self._raw_speech_ms = 0.0
            self._reset_segment_energy()

            self._turn.flush()

            try:
                self._stt_queue.put_nowait(segment)
                self._segments_sent_to_stt += 1
                self._last_heard_activity = time.monotonic()
                self._snr_drop_streak = 0

                logger.info(
                    "[TURN] Enqueued audio segment (%.0fms, %.0fms real speech "
                    "= %.0f%%, snr=%s)",
                    segment.duration_ms, _real_ms,
                    100.0 * _real_ms / max(segment.duration_ms, 1.0),
                    f"{_snr:.1f}x" if _snr is not None else "n/a",
                )

            except asyncio.QueueFull:
                # 47 segments of real caller speech were discarded this way
                # on 2026-08-03 (two bursts, during the Sarvam degradation).
                # Dropping the NEWEST arrival is the worst choice available:
                # the caller's most recent words are the ones the agent is
                # about to answer, while the oldest queued segment is the
                # most likely to be stale by the time it is transcribed.
                _evicted = None
                try:
                    _evicted = self._stt_queue.get_nowait()
                    self._stt_queue.task_done()
                except asyncio.QueueEmpty:
                    pass
                try:
                    self._stt_queue.put_nowait(segment)
                    self._segments_sent_to_stt += 1
                    logger.warning(
                        "[TURN] STT queue full — evicted the oldest queued "
                        "segment (%.0fms) to keep the newest (%.0fms)",
                        getattr(_evicted, "duration_ms", 0.0),
                        segment.duration_ms)
                except asyncio.QueueFull:
                    logger.error("[TURN] STT queue full — dropping segment "
                                 "(%.0fms) after eviction failed",
                                 segment.duration_ms)
                self._stt_queue_drops += 1

    def _reset_vad_state(self) -> None:
        self._audio_buffer.clear()
        self._speech_duration_ms = 0.0
        self._raw_speech_ms = 0.0
        self._reset_segment_energy()
        self._prev_speech = False
        self._speech_onset_frames = 0
        self._silence_frames = 0
        self._speech_frame_streak = 0
        self._pre_speech_buffer.clear()
        self._segment_has_shadow = False
        self._shadow_seeded = False
        self._turn.flush()

    # ── Playback shadow capture (fleet-audit defect #1) ──────────────────

    def _shadow_capture(self, frame: bytes) -> None:
        """Ring frames spoken over agent playback instead of dropping them.

        Entry condition is speech-level energy; once the ring is non-empty,
        quieter gap frames are kept too so a claimed utterance stays
        contiguous. Bounded to SHADOW_MAX_MS (oldest first out)."""
        energetic = self.vad.last_energy > self.vad.threshold * SHADOW_ENERGY_FACTOR
        if not energetic and not self._shadow_frames:
            return
        if not self._shadow_frames:
            self._shadow_started_ts = time.monotonic()
        self._shadow_frames.append(frame)
        if energetic:
            self._shadow_speech_ms += 20.0
        max_frames = int(SHADOW_MAX_MS / 20)
        while len(self._shadow_frames) > max_frames:
            dropped = self._shadow_frames.pop(0)
            self._shadow_started_ts += 0.02
            # Approximation: assume dropped frames carried speech at ring
            # density; exact bookkeeping isn't worth per-frame energy storage.
            if self._shadow_speech_ms > 0:
                self._shadow_speech_ms = max(
                    0.0, self._shadow_speech_ms - 20.0)

    def _drop_shadow(self, reason: str) -> None:
        if self._shadow_frames:
            logger.info("[SHADOW] Dropped %.0fms ring (%.0fms speech) — %s",
                        len(self._shadow_frames) * 20.0,
                        self._shadow_speech_ms, reason)
        self._shadow_frames.clear()
        self._shadow_speech_ms = 0.0
        self._shadow_started_ts = 0.0

    def _claim_shadow(self, reason: str) -> bool:
        """Move the shadow ring to the FRONT of the live segment buffer so
        speech that began over playback keeps its head. Returns True if
        anything was claimed."""
        if not self._shadow_frames or self._shadow_speech_ms < 100.0:
            self._drop_shadow(f"{reason}: below floor")
            return False
        # Chronology: anything already buffered was captured BEFORE playback
        # started (pre-TTS caller speech) — the shadow follows it; the
        # pre-speech ring / live frames are appended after by the caller.
        self._audio_buffer.extend(b"".join(self._shadow_frames))
        added_ms = len(self._shadow_frames) * 20.0
        self._speech_duration_ms += added_ms
        # Only the energetic portion of the ring counts as real speech — the
        # rest are the quiet gap frames the ring keeps for contiguity.
        self._raw_speech_ms += self._shadow_speech_ms
        # True onset is the shadow's first frame when that is earlier than
        # whatever anchor the normal path just computed.
        if self._shadow_started_ts > 0.0 and (
                self._cur_speech_start_ts <= 0.0
                or self._shadow_started_ts < self._cur_speech_start_ts):
            self._cur_speech_start_ts = self._shadow_started_ts
        self._segment_has_shadow = True
        self._shadow_seeded = True
        self._prev_speech = True
        logger.info(
            "[SHADOW] Claimed %.0fms of playback-era audio (%.0fms speech) "
            "into segment — %s", added_ms, self._shadow_speech_ms, reason)
        self._shadow_frames.clear()
        self._shadow_speech_ms = 0.0
        self._shadow_started_ts = 0.0
        return True

    def _is_agent_echo_text(self, text: str) -> bool:
        """True when `text` reads as an echo of the agent's own utterance
        (current or previous): most of its tokens appear in what the agent
        just said. Guards only shadow-tainted transcripts."""
        import re as _re

        def _toks(s: str) -> set:
            # Split on whitespace + punctuation, NOT on \W: Python's \w does
            # not cover Devanagari/Gujarati vowel signs, so "कौन" was shredded
            # into "क"/"न" and dropped — Hindi overlap was measured on
            # consonant fragments (§70).
            return {t for t in _re.sub(r"[.,!?;:।॥\"'“”‘’()\[\]—–\-…]", " ",
                                       (s or "").lower()).split()
                    if len(t) > 1}

        spoken = _toks(text)
        if len(spoken) < 2:
            return False
        # §70 (call 1033): "कौन बात कर रहे हैं?" shares 4 of 5 words with the
        # agent's "…कहाँ से बात कर रहे हैं?" and was discarded as echo — the
        # caller's question was never heard. An echo reproduces the agent's
        # words; a caller adds their own. Short transcripts must be covered
        # COMPLETELY (one word the agent never said = the caller said it);
        # longer ones keep the ratio.
        allowed_missing = (0 if len(spoken) <= 6
                           else int(len(spoken) * (1.0 - SHADOW_ECHO_TEXT_RATIO)))
        for agent_text in (self._current_tts_text, self._prev_tts_text):
            agent = _toks(agent_text)
            if not agent:
                continue
            if len(spoken - agent) <= allowed_missing:
                return True
        return False

    @staticmethod
    def _turn_end_anchor(transcript: "Transcript") -> float:
        """When this turn's caller speech ended — the anchor every later
        continuation gap is measured against.

        Falls back to *now* when the segment carries no end timestamp. Keeping
        the PREVIOUS turn's value there is the one option that must never
        happen: the next utterance would then measure its gap against speech
        that ended turns ago, read as a brand-new turn, and be answered
        separately. That is how one dropped span turned into two full replies
        in a row (2026-09-09 05:43). The caller fell silent a moment before
        this transcript reached us, so *now* is far closer to the truth than a
        stale anchor, and never further from it.
        """
        if transcript.speech_end_ts > 0.0:
            return transcript.speech_end_ts
        return time.monotonic()

    def _retract_unheard_agent_line(self, reason: str) -> None:
        """Take back the agent line for a reply the caller never heard.

        The engine records an agent utterance the moment generation finishes.
        A continuation merge can cut that reply microseconds later — the
        caller was still speaking, nothing was ever injected into the channel
        — and the merged reply is what they actually hear. Both lines used to
        land on the transcript, so a call where the agent asked ONE question
        read back as it asking six (live 2026-09-16, call 849: the caller's
        whole self-introduction went uninterrupted, and the audit showed five
        cancelled "what is your current location?" lines that no one heard).

        Callers must check `_reply_still_inaudible()` BEFORE the cut mutates
        playback state; this only performs the retraction.
        """
        line, self._unheard_agent_line = self._unheard_agent_line, ""
        if not line or self._hub is None:
            return
        logger.info("[ENGINE] Retracting unheard agent line (%s): %r",
                    reason, line[:60])
        self._hub.emit_nowait({"type": "agent_retract", "text": line})

    def _reply_still_inaudible(self) -> bool:
        """True while the reply to the pending turn cannot have been heard.

        A reply that is still generating, still queued for TTS, or playing
        with nothing yet written to the playback clock has reached no ear —
        cutting it costs the caller nothing.

        This is what separates a CONTINUATION from a genuine new turn without
        any arithmetic on timestamps. The gap windows exist to tell "the
        caller resumed their own sentence" from "the caller is answering the
        line we just spoke"; while nothing has been spoken there is no line
        to answer, so the second reading is impossible and the gap does not
        need to be trusted. Precisely the case the old gate got wrong: a
        segment whose span was dropped measured a ~10s gap against a stale
        anchor and was answered separately, and the caller heard two full
        pitches back to back (2026-09-09 05:43).
        """
        if (self._current_generation_task is not None
                and not self._current_generation_task.done()):
            return True
        if not self._tts_queue.empty():
            return True
        if self._tts_playing:
            # Telephony: audio is audible only once the sink has written it
            # to the playback clock. CLI (LocalSpeakerSink) exposes no such
            # clock — there, "playing" is the best evidence we have and a
            # playing reply counts as heard.
            busy_until = getattr(self.sink, "playback_busy_until", None)
            if busy_until is None:
                return False
            return busy_until <= time.monotonic()
        return False

    def _maybe_commit_pending_turn(self) -> None:
        """Lazily commit the pending turn transaction once the continuation
        window has clearly expired and no caller input is in flight.

        The in-flight guard matters: committing flushes deferred side effects
        (calendar booking), which must not happen while a just-spoken
        continuation is still on its way through STT.
        """
        if not ENABLE_CONTINUATION_MERGE:
            return
        conv = self.conversation
        if not hasattr(conv, "has_pending_turn") or not conv.has_pending_turn():
            return
        if self._turn_txn_speech_end <= 0.0:
            return
        expired = (
            time.monotonic() - self._turn_txn_speech_end
            > (CONTINUATION_WINDOW_MS + 1000.0) / 1000.0
        )
        quiescent = (
            not self._prev_speech
            and self._stt_queue.empty()
            and self._stt_inflight == 0
            and self._transcript_queue.empty()
        )
        if expired and quiescent:
            conv.commit_pending_turn()
            self._merge_chain_count = 0

    def _flush_pipeline_queues(self) -> None:
        self._delivery_epoch += 1   # A5: anything dequeued earlier is stale
        drained = 0
        for q in (self._tts_queue, self._stt_queue, self._transcript_queue):
            while not q.empty():
                try:
                    q.get_nowait()
                    drained += 1
                except asyncio.QueueEmpty:
                    break
        # Prefetched transcriptions belong to flushed audio — cancel them and
        # release their inflight slots (the worker never sees these pairs).
        while not self._stt_ready_queue.empty():
            try:
                _, _task, _ = self._stt_ready_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            _task.cancel()
            self._stt_inflight -= 1
            drained += 1
        if drained:
            logger.info("[BARGE-IN] Drained %d items from pipeline queues", drained)

    # -----------------------------------------------------------------------
    # Layer 2: STT Worker
    # -----------------------------------------------------------------------

    def _update_stt_lang_pin(self) -> None:
        """A4a: pin the STT language once the conversation language has been
        stable for STT_LANG_PIN_TURNS accepted transcripts; release the pin
        the moment conversation state moves off it (explicit request or
        evidence switch flips state → one auto-detect turn → re-pin). The
        pin is read in _stt_worker and passed as the batch request's
        language_code."""
        if not STT_LANG_PIN or self.conversation is None:
            return
        _state = getattr(self.conversation, "state", None)
        cur = getattr(_state, "language", None)
        if cur not in ("en", "hi", "gu"):
            return
        # A pin must be earned by the CALLER, never by the agent's YAML
        # default. Measured 07-31..08-04: roughly half of all pins fired with
        # no prior Sarvam detection in that call, and under a hi-IN pin 160
        # of 166 turns came back in Indic script — including English speech
        # transliterated into Devanagari ("व्हाट थिंग्स डू यू हैव...") and a
        # mangled phone number. Seeded-default turns no longer count.
        _src = str(getattr(_state, "lang_source", "") or "")
        if not _lang_source_is_evidence(_src):
            logger.debug("[STT] lang pin: turn not counted (source=%r)", _src)
            return
        if cur == self._stt_lang_last:
            self._stt_lang_stable += 1
        else:
            self._stt_lang_last = cur
            self._stt_lang_stable = 1
        # Periodic release: a pin that is wrong is otherwise self-sealing —
        # the pinned language shapes the transcript, which re-confirms the
        # pin. One free auto-detect turn every STT_LANG_PIN_RECHECK_TURNS
        # gives a wrong pin a way back out.
        if self._stt_lang_pin is not None:
            self._stt_lang_pinned_turns += 1
            if (STT_LANG_PIN_RECHECK_TURNS
                    and self._stt_lang_pinned_turns >= STT_LANG_PIN_RECHECK_TURNS):
                logger.info("[STT] Language pin %s released for one "
                            "auto-detect turn (%d turns pinned)",
                            self._stt_lang_pin, self._stt_lang_pinned_turns)
                self._stt_lang_pin = None
                self._stt_lang_pinned_turns = 0
                self._stt_lang_stable = 0
                return
        mapped = {"en": "en-IN", "hi": "hi-IN", "gu": "gu-IN"}[cur]
        if self._stt_lang_pin and self._stt_lang_pin != mapped:
            self._stt_lang_pin = None
            self._stt_lang_pinned_turns = 0
            logger.info("[STT] Language pin released (state now %s)", cur)
        elif (self._stt_lang_pin is None
                and self._stt_lang_stable >= STT_LANG_PIN_TURNS):
            self._stt_lang_pin = mapped
            self._stt_lang_pinned_turns = 0
            logger.info("[STT] Language pinned to %s (stable %d evidence "
                        "turns)", mapped, self._stt_lang_stable)

    async def _stt_submitter(self) -> None:
        """
        Layer 2a: pulls audio segments and STARTS each transcription
        immediately (bounded look-ahead, STT_PREFETCH_AHEAD), so one slow
        provider round-trip no longer serializes the segments behind it —
        the 08-03 slow-Sarvam stretch turned 2.5s requests into 8-20s queue
        tails and dropped segments. The worker consumes the (segment, task)
        pairs strictly IN ORDER: validation, language mirror, merge and
        engine semantics stay exactly sequential.
        """
        while self._running:
            try:
                segment = await asyncio.wait_for(
                    self._stt_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            self._stt_inflight += 1
            try:
                latency_start = time.monotonic()
                # Pass the locked conversation language to Sarvam to prevent
                # auto-detect misfires (Bengali/Punjabi/Malayalam/Marathi on
                # Gujarati/Hindi audio). "unknown" is only used pre-lock (first
                # turn / GREETING stage) to allow free language detection.
                # English is also passed explicitly when locked — Sarvam handles
                # English-IN well and it prevents misclassification on accented
                # English with Indian words.
                # Default: auto-detect every turn (prevents a lock feedback loop
                # and enables dynamic language switching). BUT an agent may pin STT
                # to one language via languages.stt_forced_language (e.g. "hi-IN")
                # when it is single-language and auto-detect misfires (Hindi tagged
                # as Punjabi/Tamil/Malayalam).
                _stt_lang = "unknown"
                _forced_lang = ""
                if self.conversation and hasattr(self.conversation, "_def"):
                    _forced_lang = getattr(
                        self.conversation._def.languages, "stt_forced_language", ""
                    ) or ""
                if _forced_lang:
                    _stt_lang = _forced_lang
                    logger.debug("[STT-WORKER] stt_lang forced to %s by agent config", _stt_lang)
                elif (
                    CONNECT_FORCE_LANG
                    and segment.duration_ms < CONNECT_SHORT_SEG_MS
                    and (time.monotonic() - self._call_start_ts)
                    < CONNECT_WINDOW_S
                ):
                    # Connect window: a short first segment is almost always
                    # "hello" — with lang=unknown Sarvam routinely labels it
                    # Odia/Kannada, forcing a retry round-trip that doubles
                    # STT latency exactly where dead air feeds the hello
                    # loop. Anchor to the agent's default language; real
                    # sentences (≥ CONNECT_SHORT_SEG_MS) keep auto-detect.
                    _default_short = "en"
                    if self.conversation and hasattr(self.conversation, "_def"):
                        _default_short = (
                            getattr(self.conversation._def.languages,
                                    "default", "") or "en")
                    _stt_lang = {"en": "en-IN", "hi": "hi-IN",
                                 "gu": "gu-IN"}.get(_default_short, "unknown")
                    if _stt_lang != "unknown":
                        logger.info(
                            "[STT-WORKER] Connect window: forcing lang=%s "
                            "for %.0fms segment", _stt_lang,
                            segment.duration_ms)
                elif (CAPTURE_MODE and self.conversation is not None
                        and getattr(self.conversation, "_capture_slot", None)
                        and getattr(self.conversation,
                                    "capture_expecting_input",
                                    lambda: True)()):
                    # B2: spelled emails/letters are Latin — en-IN stops the
                    # letter-name transliteration ("एल दब्यू वाय एल") and the
                    # junk-script roulette mid-capture. C4: only while the
                    # engine actually expects spelled input — a digression
                    # turn mid-capture is normal conversation in the
                    # caller's language.
                    _stt_lang = "en-IN"
                    logger.info(
                        "[STT-WORKER] Capture mode: forcing lang=en-IN")
                elif STT_LANG_PIN and self._stt_lang_pin:
                    _stt_lang = self._stt_lang_pin
                    logger.debug("[STT-WORKER] stt_lang pinned to %s", _stt_lang)
                else:
                    logger.debug("[STT-WORKER] stt_lang=unknown (auto-detect every turn)")
                _seg_samples = np.frombuffer(segment.audio, dtype=np.int16).astype(np.float32)
                _seg_rms = float(np.sqrt(np.mean(_seg_samples ** 2))) if len(_seg_samples) else 0.0
                # Full-segment RMS cannot tell noise from a quiet one-word
                # answer padded with 620ms of pre-roll and hold — which is
                # exactly the question anyone tuning an amplitude floor has
                # to answer, and the logs have never carried the number that
                # settles it. peak_rms is the loudest 100ms WINDOW inside the
                # segment: measurement only, nothing is dropped on it.
                _peak_rms = _seg_rms
                _win = int(0.100 * 16_000)
                if len(_seg_samples) > _win:
                    _sq = np.cumsum(np.concatenate(
                        ([0.0], (_seg_samples ** 2).astype(np.float64))))
                    _win_ms = (_sq[_win:] - _sq[:-_win]) / _win
                    _peak_rms = float(np.sqrt(_win_ms.max()))
                logger.info(
                    "[STT-WORKER] Submitting segment: %.0fms audio (%d bytes) "
                    "queued %.0fms ago lang=%s rms=%.0f peak_rms=%.0f",
                    segment.duration_ms,
                    len(segment.audio),
                    (time.monotonic() - segment.timestamp) * 1000,
                    _stt_lang,
                    _seg_rms,
                    _peak_rms,
                )
                # F16: best-known conversation language for the STT router's
                # script-misdetection retry (genuine Hindi transcribed as
                # Gurmukhi etc.). Before any caller turn establishes a language
                # this is the language the agent itself opened in — an
                # approximate anchor, but the retry only fires on transcripts
                # in scripts we don't support at all, where the alternative is
                # dropping the caller's first response outright (2026-07-24
                # test call: 540ms "hello" → ml-IN conf 1.00 → dropped → dead-
                # air reprompt → hang-up).
                _retry_lang = None
                if self.conversation is not None:
                    _conv_state = getattr(self.conversation, "state", None)
                    if _conv_state is not None:
                        _retry_lang = getattr(_conv_state, "language", None) or None
                # Whisper hint (2026-08-03): only once the CALLER has
                # established the language — "" (auto-detect) before that.
                # See _lang_caller_established in __init__ for the trap this
                # avoids (agent-default hints transliterate Hindi to Latin).
                _fb_hint = ""
                if _retry_lang:
                    if self._lang_initial is None:
                        self._lang_initial = _retry_lang
                    if (self._lang_caller_established
                            or _retry_lang != self._lang_initial):
                        self._lang_caller_established = True
                        _fb_hint = _retry_lang
                if self._stt_accepts_hint is None:
                    import inspect
                    try:
                        self._stt_accepts_hint = ("fallback_hint" in
                            inspect.signature(self.stt.transcribe).parameters)
                    except (TypeError, ValueError):
                        self._stt_accepts_hint = False
                _hint_kw = ({"fallback_hint": _fb_hint}
                            if self._stt_accepts_hint else {})
                _stt_task = asyncio.create_task(self.stt.transcribe(
                    segment.audio, sample_rate=SAMPLE_RATE, language=_stt_lang,
                    default_language=_retry_lang, **_hint_kw,
                ))
            except Exception:
                logger.exception("[STT] Submit failed")
                self._stt_inflight -= 1
                continue
            try:
                await self._stt_ready_queue.put(
                    (segment, _stt_task, latency_start))
            except asyncio.CancelledError:
                _stt_task.cancel()
                self._stt_inflight -= 1
                break

        logger.info("[STT] Submitter stopped")

    async def _stt_worker(self) -> None:
        """
        Consumes prefetched (segment, transcription-task) pairs IN ORDER and
        runs every post-transcription step (validation, mirror, enqueue).
        The companion _stt_submitter overlaps the provider round-trips.
        """
        while self._running:
            try:
                segment, _stt_task, latency_start = await asyncio.wait_for(
                    self._stt_ready_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                try:
                    stt_result = await _stt_task
                except asyncio.CancelledError:
                    if _stt_task.cancelled():
                        # Segment flushed (barge-in/shutdown) — skip it; the
                        # worker itself is not being cancelled.
                        continue
                    raise
                transcript_text = stt_result.text
                stt_latency = (time.monotonic() - latency_start) * 1000
                _retx_entry = self._retx_remember(segment, transcript_text)

                # Update conversation language from Sarvam's native detection
                # result. Guarded by default (LANG_MIRROR_GUARDED): the write
                # is deferred until the transcript survives validation, and
                # only lands when the claim is consistent with the
                # transcript's own script/markers — acoustic misdetection
                # (bare "hello" → anything, Indian-accented English → hi-IN)
                # used to flip the agent's accent mid-call before the
                # transcript was even accepted. LANG_MIRROR_GUARDED=false
                # restores the legacy immediate unconditional write.
                _pending_mirror: str | None = None
                if (
                    stt_result.detected_language
                    and self.conversation
                    and transcript_text
                ):
                    _bcp47_to_short = {"en-IN": "en", "hi-IN": "hi", "gu-IN": "gu"}
                    short = _bcp47_to_short.get(stt_result.detected_language)
                    # RUNBOOK §73: the mirror is bounded by the agent's own
                    # languages exactly like every conversation-side switch.
                    if short and hasattr(self.conversation, "language_allowed") \
                            and not self.conversation.language_allowed(short):
                        logger.info("[STT] Language mirror %s refused — agent "
                                    "does not speak it", short)
                        short = None
                    if short:
                        if LANG_MIRROR_GUARDED:
                            _pending_mirror = short
                        else:
                            logger.info(
                                "[STT] Language auto-detected by Sarvam: %s → %s",
                                stt_result.detected_language, short,
                            )
                            self.conversation.state.language = short
                            # Do NOT lock — allow every utterance to update language dynamically

                # Duration-aware plausibility: natural speech maxes at ~4 words/sec.
                # A transcript with far more words than the audio can contain is a
                # hallucination (STT filling silence with fluent-sounding text).
                # Threshold: 250 ms/word (4 wps) with a floor of 4 words for short segments.
                # On the batch path a transcript covers exactly the segment it
                # was POSTed for, so the segment duration IS the audio it can
                # legitimately account for.
                if transcript_text and transcript_text.strip():
                    _effective_ms = segment.duration_ms
                    _word_count = len(transcript_text.split())
                    _max_plausible = max(4, int(_effective_ms / 250))
                    if _word_count > _max_plausible * 2:
                        logger.warning(
                            "[STT] Rejecting implausible transcript: %d words in %.0fms "
                            "(max_plausible=%d) — likely hallucination: %r",
                            _word_count, _effective_ms, _max_plausible,
                            transcript_text[:80],
                        )
                        transcript_text = ""
                    elif _word_count > _max_plausible:
                        logger.warning(
                            "[STT] Suspicious word count: %d words in %.0fms "
                            "(max_plausible=%d): %r",
                            _word_count, _effective_ms, _max_plausible,
                            transcript_text[:80],
                        )

                # Guard against very short segments producing a single ambiguous word
                # that could spuriously trigger location/name/budget extraction.
                # Segments < 700ms yielding a single word are more likely noise than
                # meaningful input — unless the segment is clearly an affirmation/negation
                # (yes/no/haan/ji etc.) or a greeting reply, which must pass through
                # for flow logic to work.
                if (
                    transcript_text
                    and len(transcript_text.split()) == 1
                    and segment.duration_ms < 700.0
                ):
                    # Sarvam punctuates single words ('Hello.'), so trailing marks
                    # must come off before the allowlist lookup — otherwise even
                    # 'Yes.' falls through and the turn is lost.
                    _single = transcript_text.strip().lower().strip(".,!?;:।॥'\"")
                    if (_single not in _SINGLE_WORD_AFFIRMATIONS
                            and _single not in _SINGLE_WORD_GREETINGS):
                        logger.info(
                            "[STT-WORKER] Single-word fragment %r from %.0fms segment — "
                            "below duration threshold, skipping",
                            transcript_text, segment.duration_ms,
                        )
                        continue

                if not transcript_text or len(transcript_text.strip()) < 2:
                    # Unsupported-language recovery: play a language-appropriate
                    # "please repeat" phrase when the user clearly spoke but STT
                    # produced nothing usable.
                    #
                    # ALL four guards must pass before queuing the fallback
                    # (Issues 1 & 2):
                    #   1. Duration ≥ threshold — actual user speech, not a click
                    #   2. _greeting_done     — greeting has finished playing
                    #   3. not _tts_playing   — agent is not currently speaking
                    #   4. _vad_mute_until elapsed — not inside echo-cooldown window
                    if self._segment_speech_ms(segment) >= _STT_FALLBACK_MIN_SPEECH_MS:
                        if not self._greeting_done:
                            logger.info(
                                "[STT] Skipping fallback — greeting not yet complete "
                                "(startup/warmup audio, %.0fms)", segment.duration_ms,
                            )
                        elif self._tts_playing:
                            logger.info(
                                "[STT] Skipping fallback — agent is currently speaking "
                                "(%.0fms segment)", segment.duration_ms,
                            )
                        elif time.monotonic() < self._vad_mute_until:
                            logger.info(
                                "[STT] Skipping fallback — inside TTS echo-cooldown window "
                                "(%.0fms segment, %.0fms remaining)",
                                segment.duration_ms,
                                (self._vad_mute_until - time.monotonic()) * 1000,
                            )
                        else:
                            lang = self.conversation.state.language if self.conversation else "en"
                            self._stt_consecutive_fallbacks += 1
                            _MAX_STT_FALLBACKS = 4
                            if self._stt_consecutive_fallbacks > _MAX_STT_FALLBACKS:
                                # Break the infinite loop: too many consecutive failures.
                                # Play a graceful-close phrase and stop queuing more fallbacks.
                                logger.warning(
                                    "[STT] %d consecutive STT failures — playing graceful close to break loop",
                                    self._stt_consecutive_fallbacks,
                                )
                                close_text = None
                                if self.conversation and hasattr(self.conversation, '_def'):
                                    rb = self.conversation._def.response_bank
                                    close_text = (
                                        rb.graceful_close.get(lang)
                                        or rb.lang_fallbacks.get(lang)
                                        or rb.response_fallback
                                    )
                                if not close_text:
                                    close_text = {
                                        "hi": "क्षमा कीजिए, मुझे आपकी आवाज़ सुनने में परेशानी हो रही है। कृपया बाद में कॉल करें।",
                                        "gu": "માફ કરશો, મને સાંભળવામાં તકલીફ છે. કૃપા કરીને પછીથી કૉલ કરો.",
                                        "en": "I'm sorry, I'm having trouble hearing you. Please try calling again shortly.",
                                    }.get(lang, "I'm sorry, I'm having trouble hearing you. Please try calling again.")
                                try:
                                    self._tts_queue.put_nowait(Response(
                                        text=close_text,
                                        is_shortcut=True,
                                        timestamp=time.monotonic(),
                                        language=(lang if lang in
                                                  ("en", "hi", "gu") else "en"),
                                    ))
                                except asyncio.QueueFull:
                                    pass
                            elif not self._empty_fallback_speaks(
                                    self._stt_consecutive_fallbacks):
                                # Damper: only the SECOND consecutive empty
                                # speaks. The first stays silent (one garbled
                                # segment is normal), later ones defer to the
                                # dead-air watchdog — a spoken "sorry" per
                                # empty once fed an echo→empty→sorry loop.
                                logger.info(
                                    "[STT] Unrecognized speech (%.0fms, lang=%s, "
                                    "attempt %d/%d) — staying silent (empty-streak damper)",
                                    segment.duration_ms, lang,
                                    self._stt_consecutive_fallbacks, _MAX_STT_FALLBACKS,
                                )
                            else:
                                fallback = self._stt_fallback_phrase(lang)
                                logger.info(
                                    "[STT] Unrecognized speech (%.0fms, lang=%s, attempt %d/%d) — queuing fallback",
                                    segment.duration_ms, lang,
                                    self._stt_consecutive_fallbacks, _MAX_STT_FALLBACKS,
                                )
                                try:
                                    self._tts_queue.put_nowait(Response(
                                        text=fallback,
                                        is_shortcut=True,
                                        timestamp=time.monotonic(),
                                        language=(lang if lang in
                                                  ("en", "hi", "gu") else "en"),
                                    ))
                                    # The caller hears this sentence, so it is
                                    # part of the call. The identical phrase
                                    # spoken by the dead-air watchdog is
                                    # recorded (_queue_watchdog_line); without
                                    # this emit the same words were in the
                                    # transcript or not depending purely on
                                    # which path produced them. Unlike a filler
                                    # it carries meaning — it explains a gap.
                                    self._emit_agent_line(fallback)
                                except asyncio.QueueFull:
                                    logger.warning("[STT] TTS queue full — cannot play language-fallback phrase")
                    else:
                        logger.info("[STT] Empty transcript (%.0fms) — below fallback threshold, skipping",
                                    segment.duration_ms)
                    continue

                self._stt_consecutive_fallbacks = 0  # successful transcript — reset failure streak
                # Deferred language mirror: the transcript survived every
                # rejection guard — apply Sarvam's detection only if it is
                # plausible for this text (script/markers agree; a lone
                # "okay" is not an English switch).
                # A3 (2026-07-30): recovered transcripts (script-retry /
                # Whisper) are mishear-prone — a Whisper ", angie?" for
                # "हाँजी?" must never flip the conversation language.
                # A machine's voice (call screener, voicemail, network
                # announcement — RUNBOOK §63) says nothing about the language
                # of the person we called: an iPhone screener pinned a Hindi
                # call to English before the customer said a word.
                _machine_kind = _machine_line.classify(transcript_text)
                if _machine_kind and _pending_mirror:
                    logger.info("[STT] Language mirror skipped (%s line)",
                                _machine_kind)
                    _pending_mirror = None
                if getattr(stt_result, "source", "primary") != "primary":
                    if _pending_mirror:
                        logger.info(
                            "[STT] Language mirror skipped (source=%s)",
                            getattr(stt_result, "source", "?"))
                    _pending_mirror = None
                if _pending_mirror and self.conversation:
                    if self.conversation.stt_mirror_consistent(
                            _pending_mirror, transcript_text):
                        # A consistent caller transcript confirmed a language
                        # — the Whisper hint may follow it from now on.
                        self._lang_caller_established = True
                        logger.info(
                            "[STT] Language auto-detected by Sarvam: %s → %s (guarded)",
                            stt_result.detected_language, _pending_mirror)
                        self.conversation.state.language = _pending_mirror
                        # Provenance, so the pin can tell caller evidence
                        # from the agent's YAML default.
                        self.conversation.state.lang_source = "stt_mirror"
                    else:
                        logger.info(
                            "[STT] Language mirror suppressed: %r claim "
                            "inconsistent with transcript %r",
                            _pending_mirror, transcript_text[:40])
                logger.info("[STT] %r (%.0fms latency)", transcript_text, stt_latency)

                # Echo-text guard: shadow-tainted audio may be the acoustic
                # echo of OUR OWN utterance. Echo transcribes to the agent's
                # words; a real caller's don't. Text-domain check, so a
                # clean line (no echo) costs nothing.
                if segment.shadow and transcript_text.strip() and \
                        self._is_agent_echo_text(transcript_text):
                    logger.info(
                        "[SHADOW] [ECHO-TEXT] Discarding transcript that "
                        "matches the agent's own speech: %r",
                        transcript_text[:60])
                    if _retx_entry is not None:
                        _retx_entry["echo"] = True
                    continue

                if not _machine_kind:
                    self._update_stt_lang_pin()

                if self._hub is not None:
                    self._hub.emit_nowait({
                        "type": "user",
                        "text": transcript_text.strip(),
                        "stt_latency_ms": round(stt_latency),
                        # Exact live VAD boundaries (monotonic) — the bridge
                        # converts to recording-relative time so the audit
                        # transcript self-documents each turn's audio span
                        # (corpus cutting + ASR-bench alignment).
                        # Reconstructed onsets are for turn-taking only —
                        # the corpus gets 0.0 rather than a guess.
                        "speech_start_mono": (
                            0.0 if segment.span_estimated
                            else segment.speech_start_ts),
                        "speech_end_mono": (
                            0.0 if segment.span_estimated
                            else segment.speech_end_ts),
                    })

                transcript = Transcript(
                    text=transcript_text.strip(),
                    duration_ms=segment.duration_ms,
                    timestamp=time.monotonic(),
                    stt_latency_ms=stt_latency,
                    speech_start_ts=segment.speech_start_ts,
                    speech_end_ts=segment.speech_end_ts,
                    span_estimated=segment.span_estimated,
                    confidence=getattr(stt_result, "confidence", None),
                    stt_source=getattr(stt_result, "source", "primary"),
                )

                # Feeds the endpointer's semantic profiles on the NEXT endpoint
                # decision, so a turn split mid-dictation ("one lakh" ⟶ pause)
                # holds instead of flushing on acoustics alone.
                self._last_fragment_text = transcript.text
                self._last_fragment_ts = transcript.timestamp

                try:
                    self._transcript_queue.put_nowait(transcript)
                except asyncio.QueueFull:
                    logger.warning("[STT] Transcript queue full — dropping")

            except Exception:
                logger.exception("[STT] Transcription failed")
            finally:
                self._stt_inflight -= 1

        logger.info("[STT] Worker stopped")

    # -----------------------------------------------------------------------
    # Layer 3: Conversation Engine
    # -----------------------------------------------------------------------

    # ── Whole-turn re-transcription (§69) ───────────────────────────────
    def _retx_remember(self, segment, text: str):
        """Keep this segment's audio for the turn-level pass. Returns the
        entry so a later guard can mark it (echo)."""
        if not TURN_RETRANSCRIBE:
            return None
        try:
            buf = self.__dict__.setdefault("_retx_segments", deque(maxlen=48))
            seq = self.__dict__.get("_retx_seq", 0) + 1
            self._retx_seq = seq
            end = segment.speech_end_ts or segment.timestamp
            start = segment.speech_start_ts or (end - segment.duration_ms / 1000.0)
            entry = {"seq": seq, "start": start, "end": end,
                     "audio": segment.audio, "text": (text or "").strip(),
                     "shadow": bool(segment.shadow), "echo": False,
                     # Did this audio start while (or just after) our own
                     # voice was on the wire? playback_busy_until is the real
                     # end of playback on a phone line; _last_tts_end is only
                     # when synthesis finished.
                     # Entirely inside our own playback (+0.3 s tail): an
                     # EMPTY piece like that is far more likely our echo.
                     "over_agent": end <= max(
                         self._last_tts_end,
                         float(getattr(self.sink, "playback_busy_until", 0.0)
                               or 0.0)) + 0.3,
                     "ms": float(segment.duration_ms)}
            buf.append(entry)
            return entry
        except Exception:
            logger.debug("[STT-TURN] remember failed", exc_info=True)
            return None

    def _retx_pieces(self, transcript) -> list:
        """The caller-audio pieces that make up `transcript`'s turn."""
        buf = self.__dict__.get("_retx_segments") or ()
        floor = self.__dict__.get("_retx_floor", 0.0)
        end = transcript.speech_end_ts or transcript.timestamp
        lo = max(floor, end - TURN_RETRANSCRIBE_LOOKBACK_S)
        out = []
        for e in buf:
            # Every piece that came back BEFORE this turn is committed belongs
            # to it — including trailing pieces the merge window waited for
            # and Sarvam returned empty (§70, call 1033: "हाँ" + a "Vapi में"
            # that came back empty after it, then dropped).
            if e["echo"] or e["end"] <= lo:
                continue
            if not e["text"] and (e["shadow"] or e["over_agent"]):
                # An EMPTY piece over or just after our own playback is far
                # more likely our echo than the caller — never join it.
                continue
            out.append(e)
        # Newest pieces win the length budget.
        total, keep = 0.0, []
        for e in reversed(out):
            total += e["ms"] / 1000.0
            if total > TURN_RETRANSCRIBE_MAX_S:
                break
            keep.append(e)
        return list(reversed(keep))

    def _retx_language(self) -> tuple:
        conv_lang = None
        if self.conversation is not None:
            conv_lang = getattr(getattr(self.conversation, "state", None),
                                "language", None) or None
        lang = self._stt_lang_pin or "unknown"
        return lang, conv_lang

    def _retx_start(self, pieces: list):
        key = tuple(e["seq"] for e in pieces)
        tasks = self.__dict__.setdefault("_retx_tasks", {})
        if key in tasks:
            return tasks[key]
        gap = np.zeros(int(SAMPLE_RATE * 0.12), dtype=np.int16).tobytes()
        audio = gap.join(e["audio"] for e in pieces)
        lang, conv_lang = self._retx_language()
        kw = {}
        if self._stt_accepts_hint:
            kw["fallback_hint"] = ""
        task = asyncio.create_task(self.stt.transcribe(
            audio, sample_rate=SAMPLE_RATE, language=lang,
            default_language=conv_lang, **kw))
        tasks[key] = task
        # Bounded: only the last few speculative passes are worth keeping.
        if len(tasks) > 6:
            for k in list(tasks)[:-6]:
                t = tasks.pop(k)
                if not t.done():
                    t.cancel()
        return task

    def _retx_prefetch(self, transcript) -> None:
        """Start the joined-audio pass speculatively (merge window open)."""
        if not TURN_RETRANSCRIBE:
            return
        try:
            pieces = self._retx_pieces(transcript)
            if len(pieces) >= 2:
                self._retx_start(pieces)
        except Exception:
            logger.debug("[STT-TURN] prefetch failed", exc_info=True)

    async def _enum_recheck(self, transcript):
        """RUNBOOK §81 — one re-transcription of this turn's audio without
        the agent's STT language lock, when the router says the turn answers
        an enum question and names none of its values. The re-read replaces
        the text only if IT names a value; everything else keeps the
        original. Opt-in per agent (`languages.stt_enum_recheck`)."""
        conv = self.conversation
        fn = getattr(conv, "stt_recheck_slot", None) if conv else None
        if fn is None:
            return transcript
        slot = fn(transcript.text)
        if not slot:
            return transcript
        pieces = self._retx_pieces(transcript)
        if not pieces:
            return transcript
        gap = np.zeros(int(SAMPLE_RATE * 0.12), dtype=np.int16).tobytes()
        audio = gap.join(e["audio"] for e in pieces)
        kw = {"fallback_hint": ""} if self._stt_accepts_hint else {}
        t0 = time.monotonic()
        try:
            res = await asyncio.wait_for(
                self.stt.transcribe(audio, sample_rate=SAMPLE_RATE,
                                    language="unknown",
                                    default_language=None, **kw),
                timeout=ENUM_RECHECK_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.info("[STT-RECHECK] %s: re-read not back in %.1fs — "
                        "keeping %r", slot, ENUM_RECHECK_TIMEOUT_S,
                        transcript.text[:40])
            return transcript
        new = (getattr(res, "text", "") or "").strip()
        waited = (time.monotonic() - t0) * 1000.0
        old = transcript.text.strip()
        if not new or new == old or not conv.stt_recheck_accepts(slot, new):
            logger.info("[STT-RECHECK] %s: %r re-read as %r in +%.0fms — "
                        "no value, keeping it", slot, old[:40], new[:40],
                        waited)
            return transcript
        logger.info("[STT-RECHECK] %s: %r → %r (+%.0fms, no language lock)",
                    slot, old[:40], new[:60], waited)
        hub = getattr(self, "_hub", None)
        if hub is not None:
            starts = [e["start"] for e in pieces if e["start"]]
            hub.emit_nowait({
                "type": "user_retranscribed",
                "pieces": [old],
                "text": new,
                "speech_start_mono": min(starts) if starts else 0.0,
                "speech_end_mono": float(transcript.speech_end_ts or 0.0),
            })
        return dataclasses.replace(transcript, text=new)

    async def _retx_apply(self, transcript):
        """Replace a multi-piece turn's glued text with ONE transcription of
        its joined audio. Single-piece turns and bare hellos pass through."""
        end = transcript.speech_end_ts or transcript.timestamp
        try:
            pieces = self._retx_pieces(transcript)
            if len(pieces) < 2 or self._bare_hello(transcript.text):
                return transcript
            t0 = time.monotonic()
            task = self._retx_start(pieces)
            try:
                res = await asyncio.wait_for(asyncio.shield(task),
                                             timeout=TURN_RETRANSCRIBE_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.info("[STT-TURN] joined pass not back in %.1fs — using "
                            "the fragment text", TURN_RETRANSCRIBE_TIMEOUT_S)
                return transcript
            new = (getattr(res, "text", "") or "").strip()
            old = transcript.text.strip()
            waited = (time.monotonic() - t0) * 1000.0
            if not new:
                logger.info("[STT-TURN] joined pass empty — keeping %r", old[:60])
                return transcript
            if (len(old.split()) >= 4
                    and len(new.split()) < 0.4 * len(old.split())):
                logger.info("[STT-TURN] joined pass much shorter (%r vs %r) "
                            "— keeping the fragments", new[:40], old[:40])
                return transcript
            try:
                if self._is_agent_echo_text(new):
                    logger.info("[STT-TURN] joined pass reads as our own "
                                "speech — keeping %r", old[:60])
                    return transcript
            except Exception:
                pass
            starts = [e["start"] for e in pieces if e["start"]]
            if new != old:
                logger.info(
                    "[STT-TURN] %d pieces (%d empty) re-transcribed whole in "
                    "+%.0fms: %r → %r", len(pieces),
                    sum(1 for e in pieces if not e["text"]), waited,
                    old[:60], new[:80])
                # The audit transcript holds the pieces as they were heard
                # one by one; the engine acts on THIS text. The owner read
                # "…परिष्टों में इंटरेस्ट है।" on call b5452b23 and concluded
                # "4 BHK" was never heard — the joined pass had it.
                hub = getattr(self, "_hub", None)
                if hub is not None:
                    hub.emit_nowait({
                        "type": "user_retranscribed",
                        "pieces": [e["text"] for e in pieces if e["text"]],
                        "text": new,
                        "speech_start_mono": min(starts) if starts else 0.0,
                        "speech_end_mono": float(transcript.speech_end_ts
                                                 or 0.0),
                    })
            return Transcript(
                text=new,
                duration_ms=sum(e["ms"] for e in pieces),
                timestamp=transcript.timestamp,
                stt_latency_ms=transcript.stt_latency_ms,
                speech_start_ts=(min(starts) if starts
                                 else transcript.speech_start_ts),
                speech_end_ts=transcript.speech_end_ts,
                span_estimated=transcript.span_estimated,
                confidence=getattr(res, "confidence", None),
                stt_source=getattr(res, "source", "primary"),
            )
        finally:
            self._retx_close_window(transcript)

    def _retx_close_window(self, transcript) -> None:
        """This turn's pieces are spent: the next turn's window starts after
        them, and speculative passes over them are dropped."""
        end = transcript.speech_end_ts or transcript.timestamp
        _ends = [e["end"] for e in (self.__dict__.get("_retx_segments") or ())]
        self._retx_floor = max(self.__dict__.get("_retx_floor", 0.0), end,
                               max(_ends) if _ends else 0.0)
        self.__dict__.get("_retx_tasks", {}).clear()

    def _pipeline_quiet(self, now: float) -> bool:
        """Shared precondition for every dead-air decision: the agent has
        spoken, nothing is playing or in flight anywhere in the pipeline,
        and estimated wire playback has drained."""
        if not self._greeting_done or self._tts_playing:
            return False
        gen = self._current_generation_task
        if gen is not None and not gen.done():
            return False
        if (self._stt_inflight or self._user_speaking or self._prev_speech
                or not self._stt_queue.empty()
                or not self._transcript_queue.empty()
                or not self._tts_queue.empty()):
            return False
        busy_until = getattr(self.sink, 'playback_busy_until', 0.0)
        return now >= busy_until + 0.5

    def _silence_anchor(self) -> float:
        """Timestamp of the last activity in either direction — silence is
        measured from here.

        Uses _last_heard_activity, NOT _last_voice_activity: audio we
        discarded is not activity, it is the failure this watchdog exists to
        repair. Speech genuinely in progress still cannot trip a reprompt —
        _pipeline_quiet() gates on _prev_speech / _user_speaking / in-flight
        STT independently of this anchor."""
        busy_until = getattr(self.sink, 'playback_busy_until', 0.0)
        return max(self._tts_end_time, busy_until,
                   self._last_heard_activity, self._last_reprompt_ts)

    def _termination_cfg(self) -> dict:
        """The active agent's call_flow.termination ({} when absent — e.g.
        mock conversations in tests)."""
        fn = getattr(self.conversation, "_termination_cfg", None)
        if callable(fn):
            try:
                return fn() or {}
            except Exception:
                return {}
        return {}

    def _connect_cfg(self) -> dict:
        """The active agent's call_flow.connect ({} when absent)."""
        fn = getattr(self.conversation, "_connect_cfg", None)
        if callable(fn):
            try:
                return fn() or {}
            except Exception:
                return {}
        return {}

    # ── Per-agent turn-taking (call_flow.turn_taking, RUNBOOK §78) ─────────

    def _tt_cfg(self) -> dict:
        """The active agent's call_flow.turn_taking ({} when absent)."""
        fn = getattr(self.conversation, "_turn_taking_cfg", None)
        if callable(fn):
            try:
                return fn() or {}
            except Exception:
                return {}
        return {}

    def _tt_num(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self._tt_cfg().get(key, default) or 0.0)
        except (TypeError, ValueError):
            return float(default)

    def _tt_flag(self, key: str) -> bool:
        return bool(self._tt_cfg().get(key, False))

    def _pause_hold_s(self) -> float:
        """How long a mid-thought pause is held: the platform hold, or the
        agent's own `pause_hold_ms` when that is longer."""
        return max(TURN_CONTINUATION_HOLD_MS,
                   self._tt_num("pause_hold_ms")) / 1000.0

    def _agent_audio_remaining(self, at_ts: float) -> float:
        """Seconds of OUR audio still to play at `at_ts` — the sink's playback
        clock, which outlives `_tts_playing` (synthesis finishes long before
        FreeSWITCH has played the WAVs; a 25 s pitch reads "tts done" at
        5 s)."""
        busy = getattr(self.sink, "playback_busy_until", None)
        if not busy:
            return 0.0
        return float(busy) - float(at_ts)

    def _bare_ack(self, text: str) -> bool:
        """Delegate to the router's bare-acknowledgement check."""
        fn = getattr(self.conversation, "_is_bare_ack_text", None)
        if callable(fn):
            try:
                return bool(fn(text))
            except Exception:
                return False
        return False

    def _is_backchannel(self, transcript: "Transcript") -> bool:
        """A bare acknowledgement the caller spoke while our line still had
        BACKCHANNEL_MIN_REMAINING_S of audio to go — a listener's "हाँ", not
        a turn (opt-in: call_flow.turn_taking.backchannel_guard)."""
        if not self._tt_flag("backchannel_guard"):
            return False
        start = float(getattr(transcript, "speech_start_ts", 0.0) or 0.0)
        if start <= 0.0 or not self._bare_ack(transcript.text):
            return False
        if start < float(getattr(self, "_last_reply_tts_start", 0.0) or 0.0):
            return False
        return self._agent_audio_remaining(start) >= BACKCHANNEL_MIN_REMAINING_S

    def _deferred_barge_override_due(self, now: float) -> bool:
        """A deferred barge-in (delivery guard) is carried out once the
        caller has kept speaking `sustained_barge_ms` past the deferral
        (opt-in; 0 = never, today's behaviour)."""
        sustain_s = self._tt_num("sustained_barge_ms") / 1000.0
        return sustain_s > 0.0 and (now - self._bargein_deferred_ts) >= sustain_s

    def _tail_cut_due(self, transcript: "Transcript") -> bool:
        """Real caller speech while our audio is still playing after
        synthesis ended (opt-in: call_flow.turn_taking.tail_barge_in)."""
        if not self._tt_flag("tail_barge_in") or self._tts_playing:
            return False
        if self._bare_ack(transcript.text) or self._bare_hello(transcript.text):
            return False
        # A pickup greeting over the identity line is not an interruption
        # (call 64629614: "हेलो नमस्ते" cut the onset intro at 2.4 s left).
        if (self._greeting_only(transcript.text)
                or getattr(self, "_last_reply_was_onset", False)
                or time.monotonic() - self._call_start_ts < CONNECT_WINDOW_S):
            return False
        return (self._agent_audio_remaining(time.monotonic())
                >= TAIL_CUT_MIN_REMAINING_S)

    def _greeting_only(self, text: str) -> bool:
        """Delegate to the router's greeting-only check (hello, namaste…)."""
        fn = getattr(self.conversation, "_is_greeting_only_text", None)
        if callable(fn):
            try:
                return bool(fn(text))
            except Exception:
                return False
        return False

    def _bare_hello(self, text: str) -> bool:
        """Delegate to the router's script-aware bare-hello check."""
        fn = getattr(self.conversation, "_is_bare_hello_text", None)
        if callable(fn):
            try:
                return bool(fn(text))
            except Exception:
                return False
        return False

    def _min_speech_ms(self, now: float) -> float:
        """Effective STT floor: lowered inside the connect window so a
        clipped 240ms "hello" still reaches STT instead of being dropped
        into dead air (the watchdog then parrots the greeting)."""
        if now - self._call_start_ts < CONNECT_WINDOW_S:
            return min(CONNECT_MIN_SPEECH_MS, MIN_SPEECH_MS)
        return MIN_SPEECH_MS

    def _track_media_liveness(self, frame: bytes) -> None:
        """Flag a caller channel that has flatlined to one constant sample.

        Zero variance is the discriminator, not level: a quiet caller still
        carries dither, a dead stream does not. See DEAD_MEDIA_ENABLED."""
        a = np.frombuffer(frame, dtype=np.int16)
        if a.size and int(a.max()) == int(a.min()):
            if self._dead_media_since == 0.0:
                self._dead_media_since = time.monotonic()
            return
        if self._dead_media_since > 0.0:
            flat_s = time.monotonic() - self._dead_media_since
            if flat_s >= 1.0:
                logger.info("[MEDIA] caller channel recovered after %.1fs flat",
                            flat_s)
        self._dead_media_since = 0.0
        self._dead_media_fired = False

    def _dead_media_due(self, now: float) -> bool:
        """The caller channel has been flat long enough to call it dead."""
        if not DEAD_MEDIA_ENABLED or self._dead_media_fired:
            return False
        if self._dead_media_since <= 0.0:
            return False
        if now - self._dead_media_since < DEAD_MEDIA_S:
            return False
        conv = self.conversation
        if conv is None or not hasattr(conv, "arm_dead_media_close"):
            return False
        if getattr(conv, "hangup_pending", False):
            return False  # a close is already draining
        return True

    def _segment_speech_energy(self) -> float | None:
        """Loudest SNR_SPEECH_TOP_FRAMES frames of the current segment,
        averaged. None when the segment holds no frames yet.

        The slice is the most recent `_speech_duration_ms / 20` entries of the
        energy window: `_speech_duration_ms` advances by exactly 20ms for
        every frame appended to `_audio_buffer` (pre-speech ring, live speech
        frames and any claimed shadow alike), so that count IS the segment's
        frame count. `_frames_since_segment` caps it so a stale duration can
        never reach back past the previous flush."""
        n = min(int(self._speech_duration_ms / 20.0),
                self._frames_since_segment, len(self._energy_window))
        if n <= 0:
            return None
        seg = list(self._energy_window)[-n:]
        seg.sort(reverse=True)
        top = seg[:max(1, SNR_SPEECH_TOP_FRAMES)]
        return sum(top) / len(top)

    def _ambient_energy(self) -> float | None:
        """The caller's line floor: a low percentile of recent frame energy.

        Deliberately label-free — see SNR_AMBIENT_WINDOW_FRAMES for why the
        old "frames the model didn't call speech" definition inverted the
        guard. A percentile is immune to the speech it is measured against
        because speech is a small fraction of a multi-second window."""
        if len(self._energy_window) < SNR_MIN_NOISE_FRAMES:
            return None
        vals = sorted(self._energy_window)
        idx = int(len(vals) * SNR_AMBIENT_PCTL / 100.0)
        return vals[min(idx, len(vals) - 1)]

    def _segment_snr(self) -> float | None:
        """Segment speech energy over the caller's own line floor. None when
        there is not enough to judge — the caller then always gets the
        benefit of the doubt."""
        speech = self._segment_speech_energy()
        noise = self._ambient_energy()
        if speech is None or noise is None:
            return None
        if noise <= 0.0:
            # Digitally silent floor (a DTX line between bursts): any real
            # speech is infinitely above it, but so is a stuck-at-1 dead
            # line — fall back to "is there anything at all here", the one
            # place an absolute is unavoidable.
            return float("inf") if speech >= 2.0 else 0.0
        return speech / noise

    def _segment_is_loud(self) -> bool:
        """Absolute backstop: this much energy is somebody talking, whatever
        the floor estimate says. Guards against a poisoned or pathological
        floor silently deafening the call (see SNR_LOUD_EXEMPT_FACTOR)."""
        if SNR_LOUD_EXEMPT_FACTOR <= 0.0:
            return False
        speech = self._segment_speech_energy()
        if speech is None:
            return False
        return speech >= self.vad.barge_in_threshold * SNR_LOUD_EXEMPT_FACTOR

    def _reset_segment_energy(self) -> None:
        """Close the current segment's slice of the energy window. The window
        itself is rolling and call-level — resetting the FLOOR estimate per
        segment is what let a single burst dominate it."""
        self._frames_since_segment = 0

    def _min_real_speech_ms(self, now: float) -> float:
        """Model-confirmed speech a segment must carry to be worth a paid STT
        call. Same connect-window relaxation as _min_speech_ms."""
        if now - self._call_start_ts < CONNECT_WINDOW_S:
            return min(CONNECT_MIN_REAL_SPEECH_MS, MIN_REAL_SPEECH_MS)
        return MIN_REAL_SPEECH_MS

    def _note_caller_split(self, why: str) -> None:
        """Record that the caller carried on through a pause we had closed.

        Read by `_pause_may_continue`: the FIRST split is the evidence that
        this caller speaks in fragments, and from then on their pauses get
        the benefit of the doubt for the rest of the call.
        """
        if not TURN_SPLIT_MEMORY:
            return
        # The pickup is not evidence of how this caller speaks (RUNBOOK §65):
        # a "hello" spoken over the connect intro followed by "हाँ जी" is two
        # answers to two lines, not one sentence with a pause. Counting it
        # armed a 900ms hold on every later turn of 20/46 Ravi calls.
        if self._in_pickup_phase():
            logger.info("[TURN-TAKING] split during pickup ignored (%s)", why)
            return
        self._caller_split_turns += 1
        if self._caller_split_turns == 1:
            logger.info(
                "[TURN-TAKING] caller resumed through a pause (%s) — their "
                "pauses now get a %.0fms hold for the rest of this call",
                why, TURN_CONTINUATION_HOLD_MS)

    def _in_pickup_phase(self) -> bool:
        """Before the caller's first real turn, inside the connect window."""
        if time.monotonic() - self._call_start_ts >= CONNECT_WINDOW_S:
            return False
        state = getattr(self.conversation, "state", None)
        return int(getattr(state, "turn_count", 1) or 0) == 0

    def _onset_reaction(self, transcript: "Transcript") -> bool:
        """Did this speech run on past the end of the onset intro — i.e. is
        it (at least partly) the caller answering it?

        End, not start: the pipeline merges a pickup "hello" with the "जी हां"
        that follows it (call 994), and the merged transcript keeps the
        hello's onset. The pickup hello itself ends while the intro is still
        playing — it began 140ms before it."""
        if self._onset_play_end_ts <= 0.0:
            return False
        end = float(getattr(transcript, "speech_end_ts", 0.0) or 0.0)
        return end > self._onset_play_end_ts + ONSET_REACTION_GRACE_S

    def _pause_may_continue(self, transcript: "Transcript") -> str:
        """Why this pause is probably mid-thought — "" when it is not.

        Consulted before the idle fast-path concludes a turn with no hold at
        all. Deliberately cheap and speaker-shaped: no semantics beyond what
        `_ends_with_continuation_cue` already covers, because the cases this
        exists for ("yeah, so basically I work as an artificial intelligence
        engineer." → 2.5s pause → "so I've had experience working on AI…")
        are complete sentences by every grammatical test.
        """
        # The agent asked for every pause to be held (RUNBOOK §78).
        if self._tt_num("pause_hold_ms") > 0.0:
            return "agent"
        if not TURN_PAUSE_TOLERANCE:
            return ""
        if transcript.duration_ms >= TURN_NARRATIVE_SPEECH_MS:
            return "narrative"
        if TURN_SPLIT_MEMORY and self._caller_split_turns >= 1:
            return "splitter"
        return ""

    def _endpoint_hint_text(self) -> str:
        """Text the EndpointController may reason over for the utterance
        currently in progress.

        Only the previous fragment of the SAME turn qualifies: within the
        continuation window its tail predicts whether more is coming
        (dangling conjunction, digit burst mid-dictation). Past that window it
        belongs to a finished turn and would make the endpointer hold on a
        stale signal, so return "" and let it decide on acoustics.
        """
        if not self._last_fragment_text:
            return ""
        window_s = CONTINUATION_WINDOW_MS / 1000.0
        if time.monotonic() - self._last_fragment_ts > window_s:
            return ""
        return self._last_fragment_text

    def _tts_numbers_english(self) -> bool:
        """`languages.tts_numbers_english` of the live agent (RUNBOOK §82);
        False with no agent (CLI mode) or any shape surprise."""
        try:
            return bool(self.conversation._def.languages.tts_numbers_english)
        except Exception:
            return False

    def _resolve_tts_language(self, response: "Response", text: str) -> tuple[str, str]:
        """(language, source) the TTS speaks this utterance in. A language
        stamped on the Response (decided with the words) always wins; else
        the text's own script/markers decide (TTS_LANG_FROM_TEXT, per-agent
        opt-out languages.tts_lang_from_text); conversation state is the
        fallback and the pre-fix behaviour."""
        conv = self.conversation
        state_lang = (getattr(conv.state, "language", None) or "en") if conv else "en"
        if response.language:
            return response.language, "stamp"
        if not TTS_LANG_FROM_TEXT:
            return state_lang, "state"
        if conv is not None and getattr(
                conv._def.languages, "tts_lang_from_text", True) is False:
            return state_lang, "state"
        fn = getattr(conv, "tts_language_for", None) if conv else None
        if callable(fn):
            try:
                return fn(text), "text"
            except Exception:
                return state_lang, "state"
        return state_lang, "state"

    @staticmethod
    def _segment_speech_ms(segment: "AudioSegment") -> float:
        """How much of this segment was actually voice: the VAD's real-speech
        count when it was measured, else the whole buffered span."""
        real = float(getattr(segment, "speech_ms", 0.0) or 0.0)
        return real if real > 0.0 else float(segment.duration_ms)

    def _stt_fallback_phrase(self, lang: str) -> str:
        """The "couldn't hear you, say that again" line, in the agent's OWN
        words when it wrote one (response_bank.lang_fallbacks), else the
        platform default. Call 971: an agent whose register forbids sarkari
        Hindi said the platform's "कृपया दोबारा बताइए" while its own line
        read "एक बार फिर बताइए?"."""
        lang = (lang or "en")[:2]
        if lang not in ("en", "hi", "gu"):
            lang = "en"
        try:
            rb = self.conversation._def.response_bank if self.conversation else None
            own = (rb.lang_fallbacks or {}).get(lang) if rb is not None else ""
            if isinstance(own, str) and len(own.strip()) >= 8:
                return own.strip()
        except Exception:
            pass
        return _STT_FALLBACK_PHRASES.get(lang, _STT_FALLBACK_PHRASES["en"])

    @staticmethod
    def _empty_fallback_speaks(streak: int) -> bool:
        """Empty-transcript damper decision: with EMPTY_STREAK_DAMPER on,
        only the SECOND consecutive empty speaks the "couldn't hear you"
        line — one garbled segment is normal, and a sorry per empty once
        fed an echo→empty→sorry self-loop."""
        return (not EMPTY_STREAK_DAMPER) or streak == 2

    def _connect_barge_defer(self, now: float) -> bool:
        """Should this barge-in be DEFERRED instead of killing playback?

        True while the currently-playing utterance is one the caller cannot
        have meaningfully heard — nothing flushed to the playback clock yet,
        or less than CONNECT_MIN_PLAYED_S audible — during the connect
        window (or any time for Response.protected utterances). The kill is
        not lost: the transcript verdict in _conversation_layer performs it
        the moment STT shows real content. A bare "hello?" releases the
        deferral and playback simply continues.
        """
        if not CONNECT_DELIVERY_GUARD:
            return False
        if self._connect_cfg().get("delivery_guard") is False:
            return False
        if not self._tts_playing:
            return False
        if not (self._utt_protected
                or BARGE_GUARD_ALL_REPLIES
                or now - self._call_start_ts < CONNECT_WINDOW_S):
            return False
        busy_until = getattr(self.sink, "playback_busy_until", 0.0)
        if self._utt_first_flush_ts <= 0.0:
            if busy_until > now:
                # First audio of this utterance just reached the clock.
                self._utt_first_flush_ts = now
            return True
        return (now - self._utt_first_flush_ts) < CONNECT_MIN_PLAYED_S

    def _silence_reprompt_due(self, now: float) -> bool:
        """Pure decision: is the call in genuine dead air the agent should
        repair? True only when the agent has spoken, nothing is playing or
        in flight anywhere in the pipeline, the caller has produced no voice
        activity for SILENCE_REPROMPT_S, and the repair cap isn't spent."""
        if self._reprompt_count >= SILENCE_REPROMPT_MAX:
            return False
        if _machine_holds_line(self, now):
            return False
        # Call is wrapping up (farewell/outcome decided) — silence is normal.
        if getattr(self.conversation, "call_outcome", ""):
            return False
        # A close is already decided — the goodbye may be mid-flight or spoken
        # and the caller has nothing left to answer. Asking "is it alright if
        # we close the call now?" AFTER the farewell is the worst repair the
        # agent can make, and it is what live call 6ad891a4 (2026-08-25) did.
        # _silence_hangup_due has carried this guard since 2026-07-29; the
        # reprompt arm never got it.
        if (getattr(self.conversation, "hangup_pending", False)
                or getattr(getattr(self.conversation, "state", None),
                           "is_closing", False)):
            return False
        if not self._pipeline_quiet(now):
            return False
        anchor = self._silence_anchor()
        return anchor > 0.0 and now - anchor >= SILENCE_REPROMPT_S

    def _silence_hangup_due(self, now: float) -> bool:
        """Silence hangup (call_flow.termination.silence_hangup): every
        repair reprompt is spent — or the call already reached a wrap-up
        outcome where reprompts don't fire — and the line has stayed dead
        for a further grace window. The caller always gets the full repair
        cycle's benefit of the doubt before the platform closes."""
        if not SILENCE_HANGUP_ENABLED or self._silence_hangup_fired:
            return False
        sh = self._termination_cfg().get("silence_hangup")
        sh = sh if isinstance(sh, dict) else {}
        if not bool(sh.get("enabled", True)):
            return False
        conv = self.conversation
        if not hasattr(conv, "generate_silence_goodbye"):
            return False
        if getattr(conv, "hangup_pending", False):
            return False  # a close is already draining — don't double-fire
        if _machine_holds_line(self, now):
            return False
        cap_spent = (not SILENCE_REPROMPT_ENABLED
                     or self._reprompt_count >= SILENCE_REPROMPT_MAX
                     or bool(getattr(conv, "call_outcome", "")))
        if not cap_spent:
            return False
        if not self._pipeline_quiet(now):
            return False
        try:
            grace = float(sh.get("grace_s", SILENCE_HANGUP_GRACE_S))
        except (TypeError, ValueError):
            grace = SILENCE_HANGUP_GRACE_S
        anchor = self._silence_anchor()
        return anchor > 0.0 and now - anchor >= SILENCE_REPROMPT_S + max(grace, 0.0)

    def _max_duration_due(self, now: float) -> bool:
        """Hard duration cap (call_flow.termination.max_call_minutes > 0):
        the call has outlived the business's ceiling. Fires once, and only
        at a quiet moment — the wrap-up line never talks over anyone."""
        if self._maxdur_fired:
            return False
        try:
            cap_min = float(self._termination_cfg().get("max_call_minutes") or 0)
        except (TypeError, ValueError):
            cap_min = 0.0
        if cap_min <= 0:
            return False
        conv = self.conversation
        if not hasattr(conv, "generate_timeout_goodbye"):
            return False
        if getattr(conv, "hangup_pending", False):
            return False
        if now - self._call_start_ts < cap_min * 60.0:
            return False
        return self._pipeline_quiet(now)

    async def _abort_agent_speech(self, why: str) -> None:
        """A machine has the line (RUNBOOK §83): stop whatever the agent is
        saying NOW — the opening greeting, the onset intro, a reply to a
        garbled hold fragment — and generate nothing more. Same media-layer
        order as barge-in (uuid_break at FreeSWITCH first, then the local
        queues), minus every barge-in guard: nothing we are saying is for
        anyone. Idempotent; the bridge hangs up on hangup_immediate."""
        if self._machine_abort_done:
            return
        self._machine_abort_done = True
        logger.info("[MACHINE] aborting agent speech — %s", why)
        gen = self._current_generation_task
        if (gen is not None and not gen.done()
                and gen is not asyncio.current_task()):
            gen.cancel()
        self._flush_pipeline_queues()
        interrupt = getattr(self.sink, "interrupt_playback", None)
        if interrupt is not None:
            try:
                await interrupt()
            except Exception:
                logger.exception("[MACHINE] playback interrupt failed")
        try:
            await self.tts.stop()
        except Exception:
            logger.exception("[MACHINE] tts stop failed")
        self._tts_playing = False

    def _machine_beep_close_due(self, now: float) -> bool:
        """Pure decision: did the classifier confirm a NEW mailbox beep that
        the engine allows to end this call (outbound, nobody has spoken,
        a greeting's worth of audio preceded it)? Each beep is judged once."""
        if not MACHINE_BEEP_CLOSE_ENABLED or self._session is None:
            return False
        beep = getattr(self._session, "last_beep", None)
        if not beep:
            return False
        at = float(beep.get("at_mono") or 0.0)
        if at <= self._beep_consumed_mono:
            return False
        self._beep_consumed_mono = at
        conv = self.conversation
        allowed = getattr(conv, "machine_audio_close_allowed", None)
        if not callable(allowed) or not allowed():
            logger.info("[MACHINE] beep %.0fms @%.0fHz ignored — a person has "
                        "spoken, or not an outbound leg",
                        beep.get("duration_ms", 0.0), beep.get("freq_hz", 0.0))
            return False
        if self._speech_total_ms < MACHINE_BEEP_MIN_SPEECH_MS:
            logger.info("[MACHINE] beep %.0fms @%.0fHz ignored — only %.0fms "
                        "of caller audio before it (no greeting played)",
                        beep.get("duration_ms", 0.0), beep.get("freq_hz", 0.0),
                        self._speech_total_ms)
            return False
        logger.info("[MACHINE] mailbox beep confirmed %.0fms @%.0fHz after "
                    "%.0fms of machine audio, no person yet — closing",
                    beep.get("duration_ms", 0.0), beep.get("freq_hz", 0.0),
                    self._speech_total_ms)
        return True

    async def _machine_close_tick(self, now: float) -> bool:
        """Watchdog arm for RUNBOOK §83. True when this tick ended the call
        (or found it already ending), so the repair cycle below stands down."""
        conv = self.conversation
        if conv is None:
            return False
        if getattr(conv, "hangup_immediate", False):
            if not self._machine_abort_done:
                await self._abort_agent_speech("machine close armed")
            return True
        expired = getattr(conv, "machine_wait_expired", None)
        kind = expired(now) if callable(expired) else None
        if kind:
            logger.info("[MACHINE] %s episode ended with no person on the "
                        "line — closing", kind)
            try:
                conv.arm_machine_timeout_close(kind)
            except Exception:
                logger.exception("[MACHINE] timeout close failed")
                return False
            await self._abort_agent_speech(f"{kind} timeout")
            return True
        if self._machine_beep_close_due(now):
            try:
                conv.arm_machine_audio_close("beep")
            except Exception:
                logger.exception("[MACHINE] beep close failed")
                return False
            await self._abort_agent_speech("mailbox beep")
            return True
        return False

    async def _silence_watchdog(self) -> None:
        """Dead-air repair + call termination (platform rules): re-ask a
        quiet caller (at most SILENCE_REPROMPT_MAX times per silence), then
        close the call if they never come back; also enforces the per-agent
        max-call-duration cap."""
        while self._running:
            try:
                await asyncio.sleep(0.5)
                now = time.monotonic()

                # A machine has the line (RUNBOOK §83): a hold / screener
                # episode that ran its course, a confirmed mailbox beep, or
                # an immediate close the engine armed — cut the audio, end.
                if await self._machine_close_tick(now):
                    continue

                # Dead media outranks everything: with the channel flat there
                # is nobody to re-ask and nobody to say goodbye to, so the
                # repair cycle below would only bill TTS into a dead socket.
                if self._dead_media_due(now):
                    self._dead_media_fired = True
                    logger.warning(
                        "[HANGUP] dead media — caller channel flat (constant "
                        "sample) for %.1fs; closing without further TTS",
                        now - self._dead_media_since)
                    try:
                        self.conversation.arm_dead_media_close()
                    except Exception:
                        logger.exception("[HANGUP] dead-media close failed")
                    continue

                # Hard duration cap — checked first: it outranks repair.
                if self._max_duration_due(now):
                    self._maxdur_fired = True
                    try:
                        text = self.conversation.generate_timeout_goodbye()
                    except Exception:
                        logger.exception("[HANGUP] max-duration goodbye failed")
                        continue
                    logger.info("[HANGUP] max_call_minutes reached after "
                                "%.1f min — wrap-up line queued",
                                (now - self._call_start_ts) / 60.0)
                    self._queue_watchdog_line(text, now)
                    continue

                # Silence hangup — the repair cycle is spent and the line is
                # still dead; close it with one goodbye.
                if self._silence_hangup_due(now):
                    self._silence_hangup_fired = True
                    try:
                        text = self.conversation.generate_silence_goodbye()
                    except Exception:
                        logger.exception("[HANGUP] silence goodbye failed")
                        continue
                    anchor = self._silence_anchor()
                    logger.info("[HANGUP] silence hangup after %.1fs dead air "
                                "— goodbye queued",
                                now - anchor if anchor else 0.0)
                    self._queue_watchdog_line(text, now)
                    continue

                if not SILENCE_REPROMPT_ENABLED:
                    continue
                if not self._silence_reprompt_due(now):
                    continue
                text = ""
                if hasattr(self.conversation, "generate_silence_reprompt"):
                    try:
                        text = self.conversation.generate_silence_reprompt() or ""
                    except Exception:
                        logger.exception("[REPAIR] reprompt build failed")
                if not text:
                    lang = (getattr(self.conversation.state, "language", "en")
                            or "en")[:2]
                    text = self._stt_fallback_phrase(lang)
                self._reprompt_count += 1
                self._last_reprompt_ts = now
                anchor = max(self._tts_end_time, self._last_heard_activity)
                logger.info("[REPAIR] Dead air %.1fs — reprompt %d/%d: %r",
                            now - anchor if anchor else 0.0,
                            self._reprompt_count, SILENCE_REPROMPT_MAX,
                            text[:80])
                self._queue_watchdog_line(text, now)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[REPAIR] silence watchdog error")

    def _emit_agent_line(self, text: str) -> None:
        """Record one agent utterance on the transcript.

        Any path that makes the agent SAY something the caller can hear must
        call this, or the sentence exists on the call and nowhere else — the
        audit transcript, the stored call record and the intelligence pipeline
        all read the same "agent" events. Fillers are the one deliberate
        exception (see the engine loop) because they carry no content and used
        to fuse onto the real reply in summaries and embeddings."""
        if not text or self._hub is None:
            return
        self._hub.emit_nowait({
            "type": "agent", "text": text,
            "llm_latency_ms": 0, "first_chunk_ms": 0,
            "total_latency_ms": 0,
        })

    def _maybe_connect_onset(self) -> None:
        """Speak the connect intro on the caller's first sustained speech.

        Called from the VAD frame path, so it must stay cheap and must never
        raise into audio processing. Everything about WHETHER there is an
        intro to speak belongs to the conversation (connect_onset_line);
        this decides only that the caller has started talking."""
        if self._connect_onset_fired or not CONNECT_ONSET_TRIGGER:
            return
        if self._raw_speech_ms < CONNECT_ONSET_MS:
            return
        now = time.monotonic()
        if now - self._call_start_ts >= CONNECT_WINDOW_S:
            # One-shot latch either way: past the window this call never
            # re-checks, so the frame path stops paying for it.
            self._connect_onset_fired = True
            return
        if self._tts_playing or self.conversation is None:
            return
        try:
            text = self.conversation.connect_onset_line()
        except Exception:
            logger.exception("[CONNECT] onset line failed")
            self._connect_onset_fired = True
            return
        if not text:
            return
        self._connect_onset_fired = True
        logger.info(
            "[CONNECT] Onset intro at %.0fms of speech (%.1fs into call) — "
            "not waiting for a transcript: %r",
            self._raw_speech_ms, now - self._call_start_ts, text[:60])
        try:
            self.conversation.note_connect_intro_spoken(text)
        except Exception:
            logger.exception("[CONNECT] onset bookkeeping failed")
        self._tts_queue.put_nowait(Response(
            text=text, is_shortcut=True, timestamp=now, protected=True,
            onset_intro=True))
        self._emit_agent_line(text)

    def _queue_watchdog_line(self, text: str, now: float) -> None:
        """Speak a watchdog-originated line (reprompt / silence goodbye /
        max-duration wrap-up) through the normal TTS path."""
        if not text:
            return
        self._tts_queue.put_nowait(Response(
            text=text, is_shortcut=True, timestamp=now, protected=True))
        self._emit_agent_line(text)

    def _lang_conformance_check(self, stamp: str | None, text: str) -> None:
        """A6 telemetry (warning only, never behavioral): the language the
        response text actually carries vs the language the LLM was directed
        to answer in. A mismatch is either legitimate code-mix or the exact
        wrong-language-reply class the 2026-07-30 incident belonged to —
        one grep (`RESPONSE-MISMATCH`) now finds every case."""
        try:
            directed = getattr(
                getattr(self.conversation, "state", None), "language", None)
            if (stamp and directed and stamp != directed
                    and directed in ("en", "hi", "gu")):
                logger.warning(
                    "[LANG] RESPONSE-MISMATCH directed=%s got=%s text=%r",
                    directed, stamp, text[:60])
        except Exception:
            pass

    async def _conversation_layer(self) -> None:
        """
        Consumes transcripts, routes through conversation engine.
        Uses streaming: sends sentence-level chunks to TTS as they arrive.
        """
        while self._running:
            try:
                transcript = await asyncio.wait_for(
                    self._transcript_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                # Idle tick: a pending turn transaction whose continuation
                # window has clearly expired can now be committed safely.
                self._maybe_commit_pending_turn()
                continue
            except asyncio.CancelledError:
                break

            self._retx_prefetch(transcript)
            # A real caller turn arrived — the dead-air repair cycle resets,
            # and a caller who came back after the silence goodbye gets the
            # full repair + close cycle again before any future silence close.
            self._reprompt_count = 0
            self._silence_hangup_fired = False

            # Deferred-barge verdict (connect delivery guard): the energy
            # burst that would have killed playback is now text. A bare
            # "hello?" means the caller just couldn't hear us — keep
            # playing. Anything else is a real interruption — kill the
            # playback now, exactly as the immediate barge path would have.
            if self._bargein_deferred:
                self._bargein_deferred = False
                _is_hello_defer = self._bare_hello(transcript.text)
                if (not _is_hello_defer
                        and self._tts_playing
                        and self._utt_seq == self._bargein_deferred_utt):
                    logger.info(
                        "[BARGE-IN] Deferred kill — caller said %r "
                        "(not a hello)", transcript.text[:40])
                    self._delivery_epoch += 1   # A5: stale anything dequeued
                    interrupt = getattr(self.sink, "interrupt_playback", None)
                    if interrupt is not None:
                        await interrupt()
                    await self.tts.stop()
                    self._tts_playing = False
                    self._speech_frame_streak = 0
                else:
                    logger.info(
                        "[BARGE-IN] Deferred barge released — %s",
                        "bare hello, playback continues" if _is_hello_defer
                        else "utterance already finished")

            # Debounce: wait a short window for a follow-on segment from the same
            # utterance. VAD may split one sentence at a natural pause and fire
            # two STT results in quick succession. Merging them prevents two
            # separate LLM calls and two back-to-back agent responses.
            # LATENCY: a follow-on can only exist if another transcript is
            # queued, a segment is in STT, or the caller is speaking right
            # now — when none of those hold, the wait is pure dead air and is
            # skipped (was an unconditional ~550ms tax on every turn).
            # B2: while an email/phone capture is armed, the window
            # stretches so letter-by-letter spelling with natural pauses
            # aggregates into ONE turn — three separately-answered
            # fragments is how a live capture attempt fell apart. The
            # idle skip is also disabled: mid-spelling the pipeline IS
            # idle between fragments; that is exactly the pause to absorb.
            _capture_active = (
                CAPTURE_MODE and self.conversation is not None
                and getattr(self.conversation, "_capture_slot", None)
                is not None
                and getattr(self.conversation, "capture_expecting_input",
                            lambda: True)())
            # H5: an utterance ending mid-thought ("हम बेसिकली जो।") predicts
            # a resumed continuation — never fast-path it, and widen the
            # window so the continuation merges instead of racing the LLM.
            _dangling = (
                MERGE_HOLD_CONTINUATION
                and not _capture_active
                and _ends_with_continuation_cue(transcript.text))
            merge_wait_s = (
                CAPTURE_MERGE_WINDOW_MS if _capture_active
                else TRANSCRIPT_MERGE_WINDOW_MS) / 1000.0
            if _dangling:
                merge_wait_s = max(merge_wait_s,
                                   MERGE_HOLD_CONTINUATION_MS / 1000.0)
            # This caller pauses mid-thought — either they are mid-narrative
            # or they have already done it once on this call. Hold the window
            # so their next breath lands in the SAME turn (see
            # TURN_PAUSE_TOLERANCE).
            # A bare "hello?" is exempt for the same reason it is exempt from
            # the fragment hold below: the caller is checking the line is
            # alive, and holding buys nothing while adding the very silence
            # they are probing. Without this carve-out a splitter caller's
            # are-you-there probe would pay the full hold.
            _continues = (
                "" if (_capture_active
                       or _is_self_contained_greeting(transcript.text))
                else self._pause_may_continue(transcript))
            if _continues:
                merge_wait_s = max(merge_wait_s, self._pause_hold_s())
            # A short transcript that arrived while the caller has only just
            # gone quiet is the head of a split, not a finished turn. The idle
            # test below cannot see this: _prev_speech is a point-in-time
            # sample and FSMN confirms resumed speech 100-300ms late, so the
            # caller is already talking when we sample "idle". Word count is
            # the signal that survives the lag — see MERGE_NO_FASTPATH_MAX_WORDS.
            _quiet_ms = (time.monotonic() - self._last_voice_activity) * 1000.0
            _fragment = (
                len(transcript.text.split()) <= MERGE_NO_FASTPATH_MAX_WORDS
                and _quiet_ms < MERGE_NO_FASTPATH_QUIET_MS
                # A bare "hello" is a finished turn, not the head of a split;
                # the caller is checking we are still there. Paying the hold
                # here is what produced the dead air they were probing for.
                # The idle guards below still apply, so this only fast-paths
                # when nothing else is in flight and the caller is not talking.
                and not _is_self_contained_greeting(transcript.text))
            if (
                TRANSCRIPT_MERGE_SKIP_WHEN_IDLE
                and not _capture_active
                and not _dangling
                and not _continues
                and not _fragment
                and self._transcript_queue.empty()
                and self._stt_queue.empty()
                and self._stt_inflight == 0
                and not self._prev_speech
            ):
                merge_wait_s = 0.0
                logger.info("[ENGINE] Merge window skipped (idle)")
            elif _continues and not _dangling:
                logger.info(
                    "[ENGINE] Merge window held %.0fms (%s — this caller "
                    "speaks through pauses; %.1fs segment, %d split(s) so "
                    "far)", merge_wait_s * 1000.0, _continues,
                    transcript.duration_ms / 1000.0, self._caller_split_turns)
            elif _dangling:
                logger.info(
                    "[ENGINE] Merge window held %.0fms (dangling ending %r)",
                    merge_wait_s * 1000.0, transcript.text[-24:])
            elif _fragment:
                logger.info(
                    "[ENGINE] Merge window held %.0fms (fragment %r, caller "
                    "quiet only %.0fms — split likely)",
                    merge_wait_s * 1000.0, transcript.text[:24], _quiet_ms)
            elif merge_wait_s > 0.0:
                logger.info(
                    "[ENGINE] Merge window %.0fms%s (queued=%d stt_q=%d inflight=%d speaking=%s)",
                    merge_wait_s * 1000.0,
                    " [capture]" if _capture_active else "",
                    self._transcript_queue.qsize(), self._stt_queue.qsize(),
                    self._stt_inflight, self._prev_speech,
                )
            if merge_wait_s > 0.0:
                # In capture mode every merged fragment re-opens the window
                # (up to 8) so a full spelled email lands as one turn. A
                # caller who speaks through pauses gets the same treatment in
                # miniature: their utterance arrives in three or four pieces,
                # and stopping after ONE means the rest races the LLM again.
                # Still bounded by MERGE_HOLD_MAX_TOTAL_S, and each extra
                # piece only costs time if the caller is actually still
                # talking — a finished caller times the window out once.
                if _capture_active:
                    _max_follow_ons = 8
                elif _continues or _dangling:
                    _max_follow_ons = int(self._tt_num("max_merged_fragments")
                                          or TURN_MAX_MERGED_FRAGMENTS)
                else:
                    _max_follow_ons = 1
                _hold_started = time.monotonic()
                _follow_ons_taken = 0
                _merged_now = False
                while _follow_ons_taken < _max_follow_ons:
                    try:
                        follow_on = await asyncio.wait_for(
                            self._transcript_queue.get(),
                            timeout=merge_wait_s,
                        )
                    except asyncio.TimeoutError:
                        # Fleet-audit fix (defect #3): the window used to
                        # expire while the caller was LITERALLY STILL
                        # SPEAKING the continuation (or its segment was in
                        # STT flight) — the engine answered the head of the
                        # sentence and the tail became an orphan double-
                        # reply. Never conclude the turn while caller input
                        # is demonstrably in flight; re-arm, bounded.
                        _in_flight = (
                            self._prev_speech
                            or self._stt_inflight > 0
                            or not self._stt_queue.empty()
                        )
                        if _in_flight and (time.monotonic() - _hold_started
                                           < MERGE_HOLD_MAX_TOTAL_S):
                            logger.info(
                                "[ENGINE] Merge window re-armed — caller "
                                "input still in flight (speaking=%s "
                                "stt_inflight=%d)",
                                self._prev_speech, self._stt_inflight)
                            continue
                        break  # No follow-on within window — proceed
                    time_gap = follow_on.timestamp - transcript.timestamp
                    _gap_cap = 3.0 if not _capture_active else (
                        CAPTURE_MERGE_WINDOW_MS / 1000.0 + 1.0)
                    # The hold above may re-arm for up to MERGE_HOLD_MAX_TOTAL_S
                    # (6s) precisely BECAUSE the caller is demonstrably still
                    # speaking — so a follow-on can legitimately arrive well
                    # past a fixed 3s cap and still be the continuation we
                    # deliberately waited for. With the two numbers
                    # independent, every follow-on landing between 3s and 6s
                    # of holding was discarded: the engine waited for the
                    # caller's answer and then threw it away, answering the
                    # 3-char fragment instead and re-asking the same question.
                    # Measured live 2026-09-08 (RUNBOOK §37): "हा." held 4.0s,
                    # the real answer arrived and was dropped, and the agent
                    # asked the same question a third time. The cap must
                    # therefore cover the whole time we were willing to wait.
                    _gap_cap = max(_gap_cap,
                                   time.monotonic() - _hold_started + 0.5)
                    if (time_gap < _gap_cap and self._bare_hello(transcript.text)
                            and self._bare_hello(follow_on.text)):
                        # Two bare hellos are ONE repair signal, not a
                        # sentence — merging them builds the 'hello hello
                        # hello' garbage that poisons slots and the LLM.
                        logger.info(
                            "[ENGINE] Dropping repeated hello follow-on "
                            "(%.1fs gap)", time_gap)
                    elif time_gap < _gap_cap:
                        # Close in time — continuation of the same utterance; merge.
                        merged_text = transcript.text.rstrip(" .,") + " " + follow_on.text.lstrip()
                        logger.info(
                            "[ENGINE] Merging split segments (%.1fs gap): %r + %r",
                            time_gap, transcript.text[:35], follow_on.text[:35],
                        )
                        self._note_caller_split("merged inside the window")
                        _merged_now = True
                        transcript = Transcript(
                            text=merged_text,
                            duration_ms=transcript.duration_ms + follow_on.duration_ms,
                            timestamp=follow_on.timestamp,
                            stt_latency_ms=follow_on.stt_latency_ms,
                            # First fragment's onset, last fragment's end.
                            speech_start_ts=transcript.speech_start_ts,
                            speech_end_ts=follow_on.speech_end_ts,
                            span_estimated=(transcript.span_estimated
                                            or follow_on.span_estimated),
                            # Any recovered fragment taints the merged turn.
                            stt_source=(transcript.stt_source
                                        if transcript.stt_source != "primary"
                                        else follow_on.stt_source),
                        )
                    else:
                        # Gap too large — from a genuinely separate prior turn;
                        # discard. (The text logged is the FOLLOW-ON being
                        # dropped, not the turn being answered — the old
                        # "Dropping old segment" wording read as the opposite
                        # and cost real time during the §37 investigation.)
                        logger.info(
                            "[ENGINE] Dropping follow-on as stale (%.1fs gap "
                            "> cap %.1fs): %r",
                            time_gap, _gap_cap, follow_on.text[:40],
                        )
                    _follow_ons_taken += 1
                    if _merged_now:
                        _merged_now = False
                        self._retx_prefetch(transcript)

            # Drain any remaining queued transcripts.
            # Transcripts that arrived recently (within 3 s) are very likely
            # continuations of the same utterance — the VAD split a long
            # sentence at a natural pause and two STT results landed in quick
            # succession. Merge them into the current turn so the LLM sees the
            # full sentence. Older transcripts are genuinely stale (leftover
            # from a previous turn while the agent was speaking) and are dropped.
            _drain_now = time.monotonic()
            while not self._transcript_queue.empty():
                try:
                    candidate = self._transcript_queue.get_nowait()
                    age_s = _drain_now - candidate.timestamp
                    if (age_s <= 3.0 and self._bare_hello(transcript.text)
                            and self._bare_hello(candidate.text)):
                        logger.info(
                            "[ENGINE] Dropping repeated hello continuation "
                            "(%.1fs old)", age_s)
                    elif age_s <= 3.0:
                        merged_text = transcript.text.rstrip(" .,") + " " + candidate.text.lstrip()
                        transcript = Transcript(
                            text=merged_text,
                            duration_ms=transcript.duration_ms + candidate.duration_ms,
                            timestamp=candidate.timestamp,
                            stt_latency_ms=candidate.stt_latency_ms,
                            speech_start_ts=transcript.speech_start_ts,
                            speech_end_ts=candidate.speech_end_ts,
                            span_estimated=(transcript.span_estimated
                                            or candidate.span_estimated),
                            stt_source=(transcript.stt_source
                                        if transcript.stt_source != "primary"
                                        else candidate.stt_source),
                        )
                        logger.info(
                            "[ENGINE] Merging late continuation (%.1fs): %r + %r",
                            age_s, transcript.text[:40], candidate.text[:40],
                        )
                        self._note_caller_split("late continuation")
                    else:
                        logger.info(
                            "[ENGINE] Discarding stale transcript (%.1fs old): %r",
                            age_s, candidate.text[:50],
                        )
                except asyncio.QueueEmpty:
                    break

            # Enum answer re-check (§81): a locked-language transcript of a
            # short answer that names none of the asked slot's values is
            # re-read once without the lock. First — the whole-turn pass
            # below moves the piece window past this turn.
            _rechecked = False
            try:
                _rc = await self._enum_recheck(transcript)
                _rechecked = _rc is not transcript
                transcript = _rc
            except Exception:
                logger.exception("[STT-RECHECK] failed — using the transcript")
            if _rechecked:
                self._retx_close_window(transcript)

            # Whole-turn re-transcription (§69): one STT pass over every
            # piece of this turn's audio, replacing the glued fragment texts.
            if TURN_RETRANSCRIBE and not _rechecked:
                try:
                    transcript = await self._retx_apply(transcript)
                except Exception:
                    logger.exception("[STT-TURN] re-transcription failed — "
                                     "using the fragment text")

            # ── Continuation merge (pause-tolerant turns) ────────────────────
            # The caller paused mid-utterance, got a fast reply to the partial
            # input, and resumed within the continuation window of their OWN
            # speech end: cut the reply, roll the router back to before the
            # partial turn, and re-run ONE merged utterance.
            now = time.monotonic()
            effective_text = transcript.text
            merged_from_rollback = False
            # A bare "hello?" is a repair signal, never a continuation of the
            # caller's own sentence and never context worth prepending — it
            # must reach the router clean so the replay / instant-intro
            # branches can recognize it (a merged 'hello hello hello hello
            # hello' is >4 tokens and stops matching entirely).
            _hello_turn = self._bare_hello(transcript.text)

            # Onset intro answered (RUNBOOK §65): speech that ran on past the
            # end of the intro's playback is the caller's reply to it. The
            # router must not absorb it as the pickup hello — "हेलो। हां जी
            # हां जी।" is a yes to "क्या मेरी बात … से हो रही है?", and
            # absorbing it cost calls 994/998/1008 6-8 s of silence and a
            # re-asked identity question.
            _onset_answer = (self._last_reply_was_onset
                             and self._onset_reaction(transcript))
            if _onset_answer:
                _close = getattr(self.conversation,
                                 "end_connect_onset_absorb", None)
                if callable(_close):
                    _close()

            # Per-agent turn-taking (RUNBOOK §78). A bare "हाँ" while our
            # line still has a second or more to play is a listener's
            # acknowledgement — on call b5452b23 it was taken as a turn,
            # advanced the stage and queued the next question behind the
            # pitch. Real speech in that tail stops the audio instead of
            # letting the reply queue behind it.
            if not _hello_turn and self._is_backchannel(transcript):
                logger.info("[ENGINE] Back-channel absorbed — agent still "
                            "speaking %.1fs more: %r",
                            self._agent_audio_remaining(
                                transcript.speech_start_ts),
                            transcript.text[:40])
                continue
            if not _hello_turn and self._tail_cut_due(transcript):
                interrupt = getattr(self.sink, "interrupt_playback", None)
                if interrupt is not None:
                    logger.info("[BARGE-IN] Tail cut — caller spoke %r with "
                                "%.1fs of our audio still playing",
                                transcript.text[:40],
                                self._agent_audio_remaining(time.monotonic()))
                    await interrupt()
                    # The caller did not hear the end of that reply.
                    self._last_reply_completed_clean = False

            # The pickup greeting the onset intro was spoken over: that intro
            # already answered it. It is neither a turn nor context for the
            # next one — prepended, "नमस्कार" turned the caller's "हाँ" into
            # a greeting and the yes was dropped (call 998).
            if (self._last_reply_was_onset and not _onset_answer
                    and transcript.speech_start_ts > 0.0
                    and transcript.speech_start_ts
                    < getattr(self, "_last_reply_tts_start", 0.0)
                    and self._greeting_only(transcript.text)):
                logger.info("[CONNECT] Pickup greeting spoken over the onset "
                            "intro — already answered: %r",
                            transcript.text[:40])
                continue

            # Stale-fragment suppression: this speech BEGAN before the reply
            # we have since fully delivered started playing — it is the tail
            # of the turn that reply answered, not a reaction to it. Absorb
            # into the accumulator (the next real turn carries it as context,
            # slots included) and do not speak a second answer.
            if (STALE_FRAGMENT_SUPPRESS and self._greeting_done
                    and not _hello_turn and not _capture_active
                    and not _onset_answer
                    and transcript.speech_start_ts > 0.0
                    and float(transcript.duration_ms or 0.0) <= STALE_FRAGMENT_MAX_MS
                    and getattr(self, "_last_reply_tts_start", 0.0) > 0.0
                    and transcript.speech_start_ts
                    < self._last_reply_tts_start
                    and getattr(self, "_last_reply_completed_clean", False)):
                self._turn_accumulator = (
                    (self._turn_accumulator + " " + transcript.text).strip())
                # If a reply is playing right now, its clean completion must
                # NOT wipe this: nothing has answered these words yet (call
                # 909 — absorbed at +0.9 s into a 10 s reply, cleared when
                # that reply finished, then "I couldn't hear you").
                self._absorbed_during_reply = bool(self._tts_playing)
                logger.info(
                    "[ENGINE] Stale fragment absorbed, not answered "
                    "(spoke %.1fs before delivered reply): %r",
                    self._last_reply_tts_start - transcript.speech_start_ts,
                    transcript.text[:60])
                # The clearest split there is: they kept talking and we had
                # already answered the first half. Never let it happen twice
                # on the same call without widening the hold.
                self._note_caller_split("tail arrived after the reply")
                continue

            if ENABLE_CONTINUATION_MERGE and self._greeting_done:
                conv = self.conversation
                if (hasattr(conv, "has_pending_turn") and conv.has_pending_turn()):
                    gap_ms = (
                        transcript.speech_start_ts - self._turn_txn_speech_end
                    ) * 1000.0
                    # Long-pause merges (gap > base window) are allowed only
                    # while the superseded reply has NOT been fully delivered:
                    # still generating, queued, playing, or cut by a barge-in.
                    # Evaluated BEFORE the cut below mutates any of these.
                    _reply_undelivered = (
                        (self._current_generation_task is not None
                         and not self._current_generation_task.done())
                        or not self._tts_queue.empty()
                        or self._tts_playing
                        or self._barge_in_occurred
                        # A reply the caller CUT is undelivered for as long as
                        # it matters. `_barge_in_occurred` is cleared by the
                        # TTS finally block the instant playback unwinds —
                        # typically seconds before the continuation's
                        # transcript arrives — so on its own it never survives
                        # to be read here.
                        or not self._last_reply_completed_clean
                    )
                    # Nothing of the reply has reached the caller: this
                    # transcript cannot be a response to it, so it is the rest
                    # of the utterance we already started answering. Merge on
                    # that fact alone — no gap arithmetic, which is exactly
                    # what a dropped or reconstructed span makes unreliable.
                    #
                    # Excluded: a reply the caller CUT was one they were
                    # hearing, and the barge-in path already owns that case
                    # (it keeps the accumulator so the next transcript carries
                    # the pre-interruption words). Cancelling a task does not
                    # mark it done immediately, so without the guard the
                    # moments right after a barge-in read as "still
                    # generating" and would merge here instead.
                    _inaudible = (not self._barge_in_occurred
                                  and self._reply_still_inaudible())
                    _gap_ok = (
                        transcript.speech_start_ts > 0.0
                        and self._turn_txn_speech_end > 0.0
                        and 0.0 <= gap_ms
                        and (gap_ms <= CONTINUATION_WINDOW_MS
                             or (_reply_undelivered
                                 and gap_ms <= CONTINUATION_LONG_WINDOW_MS))
                    )
                    if (
                        not _hello_turn
                        and (_gap_ok or _inaudible)
                        and self._merge_chain_count < CONTINUATION_MAX_MERGES
                    ):
                        # Read "did any of it reach the caller?" BEFORE the
                        # cut below clears the playback state that answers it.
                        # An inaudible reply must also come off the
                        # transcript, or the call reads back as the agent
                        # repeating a question it only ever asked once.
                        if self._reply_still_inaudible():
                            self._retract_unheard_agent_line(
                                "superseded by continuation merge")
                        # Cut the agent's superseded reply wherever it is:
                        # still generating, queued for TTS, or playing.
                        # A5: the epoch bump also catches the reply the TTS
                        # player has DEQUEUED but not yet started synthesizing
                        # — the 300ms window the queue drain below can't reach.
                        self._delivery_epoch += 1
                        # Synthesis may be over while FreeSWITCH is still
                        # playing the WAVs (RUNBOOK §78): call 64629614 heard
                        # the budget question twice, 3 s apart, because the
                        # superseded reply had "tts done" and was left to
                        # play out before the merged one.
                        _still_audible = (
                            self._agent_audio_remaining(time.monotonic()) > 0.3)
                        if self._tts_playing or _still_audible:
                            # WAVs already injected into FreeSWITCH keep
                            # playing whatever we cancel locally, so the
                            # media layer is aborted FIRST — same order as
                            # the barge-in path. Without it a superseded
                            # reply could still be heard alongside the
                            # merged one, which is the very thing this
                            # rollback exists to prevent.
                            interrupt = getattr(
                                self.sink, "interrupt_playback", None)
                            if interrupt is not None:
                                await interrupt()
                            if self._tts_playing:
                                await self.tts.stop()
                                self._tts_playing = False
                        if (
                            self._current_generation_task
                            and not self._current_generation_task.done()
                        ):
                            self._current_generation_task.cancel()
                        while not self._tts_queue.empty():
                            try:
                                self._tts_queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                        prev_text = conv.rollback_pending_turn()
                        if prev_text:
                            effective_text = (
                                prev_text.rstrip(" .,") + " "
                                + transcript.text.lstrip()
                            )
                            merged_from_rollback = True
                            self._merge_chain_count += 1
                            # The rollback owns the merge — no Case-A prepend,
                            # and the superseded reply must not anchor the
                            # accumulator-keeping barge-in path.
                            self._turn_accumulator = ""
                            self._barge_in_occurred = False
                            self._note_caller_split("rollback merge")
                            logger.info(
                                "[CONT-MERGE] gap=%.0fms chain=%d via=%s: "
                                "%r + %r → %r",
                                gap_ms, self._merge_chain_count,
                                "gap" if _gap_ok else "reply-inaudible",
                                prev_text[:40], transcript.text[:40],
                                effective_text[:80],
                            )
                            if self._hub is not None:
                                self._hub.emit_nowait({
                                    "type": "turn_merged",
                                    "merged_text": effective_text,
                                    "gap_ms": round(gap_ms),
                                })
                    else:
                        # Genuinely new turn — the previous one is final.
                        conv.commit_pending_turn()
                        self._merge_chain_count = 0

            # ── Turn accumulator (Change 3) ──────────────────────────────────
            # Determine the effective text the LLM will receive this turn.
            #
            # Case A — barge-in context: if the caller interrupted the agent
            # mid-response, _turn_accumulator holds what they said before the
            # interruption. Prepend it so the LLM sees the complete caller turn,
            # not just the barge-in fragment.
            #
            # Case B — post-response continuation (legacy, subsumed by the
            # continuation merge when that feature is ON): if TTS finished
            # cleanly a moment ago and the new transcript is very short
            # (≤ 4 words), it is likely a trailing clause ("for that",
            # "yes, and...") that arrived just after the VAD split. Prepend
            # the previous turn's text.
            if merged_from_rollback:
                pass  # effective_text already merged with full state rollback
            elif _hello_turn:
                # Keep the hello clean — no prior-fragment prepend, no
                # post-response merge. The router's repair branches need the
                # bare form, and 'prior sentence + hello' is noise anyway.
                pass
            elif self._turn_accumulator:
                effective_text = (
                    self._turn_accumulator.rstrip(" .,") + " " + transcript.text.lstrip()
                )
                logger.info(
                    "[ENGINE] Prepending barge-in context to transcript: %r + %r → %r",
                    self._turn_accumulator[:40], transcript.text[:40], effective_text[:60],
                )
            elif (
                not ENABLE_CONTINUATION_MERGE
                and self._last_user_text
                and (now - self._last_tts_end) < POST_RESPONSE_MERGE_MS / 1000.0
                and len(transcript.text.split()) <= 4
            ):
                effective_text = (
                    self._last_user_text.rstrip(" .,") + " " + transcript.text.lstrip()
                )
                logger.info(
                    "[ENGINE] Merging post-response fragment %r with prior turn %r → %r",
                    transcript.text[:35], self._last_user_text[:35], effective_text[:60],
                )
            # Save for potential next barge-in — but never a bare hello:
            # stored hellos become the 'hello hello …' prepend chains.
            if not _hello_turn:
                self._turn_accumulator = effective_text
                self._last_user_text = effective_text
            # Anchor for the next continuation-gap measurement: when the
            # caller's speech for THIS turn actually ended.
            self._turn_txn_speech_end = self._turn_end_anchor(transcript)
            if transcript.speech_end_ts > 0.0:
                # Single-grep latency instrument: total dead air between the
                # caller falling silent and the engine starting (endpoint
                # gate + STT + merge window + queue time).
                logger.info(
                    "[LATENCY] speech_end→engine %.0fms (stt=%.0fms)",
                    (time.monotonic() - transcript.speech_end_ts) * 1000.0,
                    transcript.stt_latency_ms,
                )
                # Companion instrument: arm the sink so its FIRST flush of
                # this turn logs speech_end→first_audio — the number the
                # whole latency effort optimizes, previously reconstructible
                # only by hand-joining log timestamps.
                sink = getattr(self.tts, "_sink", None)
                if sink is not None and hasattr(sink, "flush"):
                    sink.turn_speech_end_ts = transcript.speech_end_ts

            # The turn (merged or not) is now the engine's. Its text must not
            # leak into the NEXT utterance's endpoint decision — a prior turn
            # ending in digits would otherwise hold the following turn open for
            # the _HOLD residual on a signal that no longer applies.
            self._last_fragment_text = ""
            self._last_fragment_ts = 0.0

            # Turn-level trace record. The numbers were always computed —
            # they just died as log lines. Enriched below as the LLM stages
            # complete, by the sink when the first audio flushes, and by the
            # barge-in handler; the bridge batch-persists to call_turns.
            sink = getattr(self.tts, "_sink", None)
            state = getattr(self.conversation, "state", None)
            trace = {
                "turn_index": len(self.turn_traces),
                "ts": time.time(),
                "stage": (str(getattr(state, "conversation_stage", "")
                              or getattr(state, "stage", "") or "")
                          or None),
                "language": getattr(state, "language", None) or None,
                "user_chars": len(effective_text),
                "stt_latency_ms": round(transcript.stt_latency_ms, 1),
                "stt_confidence": getattr(transcript, "confidence", None),
                "speech_end_to_engine_ms": (
                    round((time.monotonic() - transcript.speech_end_ts)
                          * 1000.0, 1)
                    if transcript.speech_end_ts > 0.0 else None),
            }
            if self._session is not None:
                # A machine's line is not an exchange (RUNBOOK §83): the
                # session's monologue / beep interpretation stays armed.
                if _machine_line.classify(effective_text) is None:
                    self._session.mark_exchange()
                trace["session_state"] = self._session.state
            if self._endpointer is not None:
                trace["endpoint_profile"] = self._endpointer.last_profile
            if len(self.turn_traces) < 500:
                self.turn_traces.append(trace)
            if sink is not None and hasattr(sink, "flush"):
                sink.turn_trace_ref = trace

            # A3: the router's language-evidence tiering needs to know when
            # a turn came from a mishear-prone recovery path.
            if self.conversation is not None:
                try:
                    self.conversation._turn_stt_source = transcript.stt_source
                except Exception:
                    pass

            try:
                async def _run_generation():

                    latency_start = time.monotonic()

                    first_result = None
                    first_chunk_ms = 0.0

                    full_text_parts: list[str] = []
                    # Collect LLM (non-shortcut) sentence chunks to combine
                    # into a single TTS call, preventing per-sentence gaps.
                    llm_parts: list[str] = []

                    async for result in self.conversation.process_stream(
                        effective_text
                    ):

                        if first_result is None:

                            first_result = result

                            first_chunk_ms = (
                                time.monotonic() - latency_start
                            ) * 1000

                        # Fillers play on the call but are NOT part of what the
                        # agent said: excluding them here keeps them out of the
                        # stored transcript, and therefore out of the
                        # intelligence pipeline's extraction and embedding
                        # input (audit: every stored filler was fused onto the
                        # real reply — "One moment please... Hi, hope you're…" —
                        # polluting summaries and vectors). The LLM's own
                        # memory already excluded them.
                        if result.intent != "filler":
                            full_text_parts.append(result.text)

                        if result.is_shortcut:
                            # Fillers, shortcuts, language-switch confirmations
                            # go to TTS immediately for low latency.
                            try:
                                self._tts_queue.put_nowait(
                                    Response(
                                        text=result.text,
                                        is_shortcut=True,
                                        timestamp=time.monotonic(),
                                        is_filler=(result.intent == "filler"),
                                        # Replays / connect intros exist
                                        # because the caller missed them —
                                        # shield their delivery from hello
                                        # barge-ins at any call age.
                                        protected=(result.intent in
                                                   ("repeat", "connect_intro")),
                                        replay=(result.intent == "repeat"),
                                        # Fillers are romanized ("Ek minute…")
                                        # and picked BY the state language —
                                        # stamp it so text-derivation can't
                                        # misroute them to en-IN.
                                        language=(
                                            self.conversation.state.language
                                            if (result.intent == "filler"
                                                and self.conversation
                                                and self.conversation.state
                                                .language in ("en", "hi", "gu"))
                                            else None),
                                    )
                                )
                            except asyncio.QueueFull:
                                logger.warning(
                                    "[ENGINE] TTS queue full -- dropping shortcut chunk"
                                )
                        else:
                            # First LLM sentence → TTS immediately to cut
                            # perceived latency. Remaining sentences are buffered
                            # and sent as one follow-up request once the LLM
                            # finishes, avoiding per-sentence TTS gaps.
                            #
                            # Exception: if the first chunk ends on a dangling
                            # conjunction ("but", "and", "however", …), it is an
                            # incomplete thought mid-stream. Sending it alone
                            # produces sentences like "I understand but" that
                            # confuse callers. Buffer it and merge with the next
                            # chunk so the caller hears a complete sentence.
                            _DANGLING_END = re.compile(
                                r'\b(but|and|or|yet|however|also|so|although|'
                                r'though|because|since|if|when|while|after|'
                                r'before|as|that|which|who|whom)\s*[,.]?\s*$',
                                re.IGNORECASE,
                            )
                            if not llm_parts:
                                if _DANGLING_END.search(result.text.rstrip()):
                                    logger.debug(
                                        "[ENGINE] First sentence ends on conjunction — buffering: %r",
                                        result.text[:60],
                                    )
                                    llm_parts.append(result.text)
                                else:
                                    try:
                                        # Accent decided WITH the words: a
                                        # state flip while this waits in the
                                        # queue once spoke an English reply
                                        # as hi-IN.
                                        _stamp = (self.conversation
                                                  .tts_language_for(result.text))
                                        self._lang_conformance_check(
                                            _stamp, result.text)
                                        self._tts_queue.put_nowait(
                                            Response(
                                                text=result.text,
                                                is_shortcut=False,
                                                timestamp=time.monotonic(),
                                                language=_stamp,
                                            )
                                        )
                                        logger.debug(
                                            "[ENGINE] First LLM sentence → TTS immediately: %r",
                                            result.text[:60],
                                        )
                                    except asyncio.QueueFull:
                                        logger.warning(
                                            "[ENGINE] TTS queue full — buffering first sentence"
                                        )
                                        llm_parts.append(result.text)
                            else:
                                llm_parts.append(result.text)

                    # Send remaining LLM sentences (2nd sentence onwards) as
                    # one combined TTS request to avoid inter-sentence gaps.
                    if llm_parts:
                        combined_text = " ".join(llm_parts)
                        try:
                            _stamp = (self.conversation
                                      .tts_language_for(combined_text))
                            self._lang_conformance_check(_stamp, combined_text)
                            self._tts_queue.put_nowait(
                                Response(
                                    text=combined_text,
                                    is_shortcut=False,
                                    timestamp=time.monotonic(),
                                    language=_stamp,
                                )
                            )
                        except asyncio.QueueFull:
                            logger.warning(
                                "[ENGINE] TTS queue full -- dropping combined LLM response"
                            )

                    engine_latency = (
                        time.monotonic() - latency_start
                    ) * 1000

                    total_latency = (
                        transcript.stt_latency_ms
                        + engine_latency
                    )

                    if first_result:

                        full_text = " ".join(full_text_parts)

                        trace["llm_first_chunk_ms"] = round(first_chunk_ms, 1)
                        trace["llm_total_ms"] = round(engine_latency, 1)
                        trace["agent_chars"] = len(full_text)
                        trace["intent"] = getattr(first_result, "intent",
                                                  None) or None
                        trace["is_filler"] = (
                            getattr(first_result, "intent", "") == "filler")
                        # Which uploaded document backed what the agent just
                        # said. The router resolved this before generating, so
                        # by here it describes THIS turn. Persisted with the
                        # turn so "why did the agent say that?" is answerable
                        # from the call record months later, rather than only
                        # from a rotating trace file.
                        _cites = getattr(self.conversation, "_rag_citations", None)
                        if _cites:
                            trace["rag_sources"] = [
                                {"label": c.get("label"),
                                 "document_id": c.get("document_id"),
                                 "chunk_id": c.get("chunk_id"),
                                 "score": c.get("relevance_score")}
                                for c in _cites]
                        _rag_tid = getattr(self.conversation, "_rag_trace_id", "")
                        if _rag_tid:
                            trace["rag_trace_id"] = _rag_tid

                        logger.info(
                            "[ENGINE] %r -> %r (%.0fms, first_chunk=%.0fms)",
                            transcript.text,
                            full_text,
                            engine_latency,
                            first_chunk_ms,
                        )

                        if self._hub is not None:
                            # Provisional until the caller hears some of it:
                            # a continuation merge can still cut this reply
                            # before a frame reaches the wire, and then it
                            # must come back OFF the transcript (see
                            # _retract_unheard_agent_line).
                            self._unheard_agent_line = full_text
                            self._hub.emit_nowait({
                                "type": "agent",
                                "text": full_text,
                                "llm_latency_ms": round(engine_latency),
                                "first_chunk_ms": round(first_chunk_ms),
                                "total_latency_ms": round(total_latency),
                            })

                if self._session is not None:
                    self._session.set("THINKING", "llm turn")
                self._current_generation_task = asyncio.create_task(
                    _run_generation()
                )

                await self._current_generation_task

                # §83: the gate just closed on a machine — stop the opening /
                # onset intro that is still playing into it, right now.
                if getattr(self.conversation, "hangup_immediate", False):
                    await self._abort_agent_speech("machine on the line")

            except asyncio.CancelledError:

                logger.info("[ENGINE] Generation cancelled")

            except Exception:

                logger.exception("[ENGINE] Processing failed")

                try:
                    self._tts_queue.put_nowait(Response(
                        text="Sorry, could you say that again?",
                        is_shortcut=True,
                        timestamp=time.monotonic(),
                        language="en",
                    ))
                except asyncio.QueueFull:
                    pass

        logger.info("[ENGINE] Layer stopped")

    # -----------------------------------------------------------------------
    # Layer 4: TTS Playback
    # -----------------------------------------------------------------------

    async def _tts_layer(self) -> None:
        """
        Consumes responses, plays via TTS.
        Tracks playback state for barge-in detection.

        Ensures only one utterance plays at a time: waits for speak()
        to fully complete before starting the next. Stale responses
        (queued >3s ago) are dropped to prevent pile-up.
        """
        while self._running:
            try:
                response = await asyncio.wait_for(
                    self._tts_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            # A5: snapshot the delivery epoch the moment the Response leaves
            # the queue — a supersede event (cont-merge rollback, barge-in)
            # after this point must still be able to kill it.
            _epoch_at_dequeue = self._delivery_epoch

            # Drop genuinely-orphaned responses. Cap is intentionally generous:
            # a multi-sentence streamed response can spend tens of seconds in
            # the queue waiting for earlier chunks to finish speaking. Barge-in
            # already drains this queue via _flush_pipeline_queues(), so this
            # cap only fires on genuinely stuck pipelines.
            age_ms = (time.monotonic() - response.timestamp) * 1000
            if age_ms > 8000:
                logger.warning("[TTS] Dropping stale response (%.0fms old): %r",
                               age_ms, response.text[:50])
                continue

            # A5: a replay exists to fill a silence. If a newer response
            # started playing after this replay was created, the silence is
            # gone — speaking it now double-answers the caller (live case:
            # "Sure, am I speaking with Angelo?" played twice back-to-back).
            if response.replay and self._last_playback_start_ts > response.timestamp:
                logger.info(
                    "[TTS] Dropped replay — a newer response already played: %r",
                    response.text[:50])
                continue

            if self._tts_playing:
                logger.info("[TTS] Interrupting previous playback")

                try:
                    await self.tts.stop()
                except Exception:
                    logger.exception("[TTS] stop() failed")

                self._tts_playing = False

                await asyncio.sleep(0.05)

            try:
                # Silence protection (feature 5): never send blank text to TTS
                if not response.text.strip():
                    lang = self.conversation.state.language if self.conversation else "en"
                    fallback = self._stt_fallback_phrase(lang)
                    logger.warning("[TTS] Empty response text — substituting fallback phrase (lang=%s)", lang)
                    response = Response(
                        text=fallback,
                        is_shortcut=True,
                        timestamp=response.timestamp,
                        language=lang if lang in ("en", "hi", "gu") else "en",
                    )
                    # The engine emitted the (empty) original, which the audit
                    # writer discards — so the substitute is what the caller
                    # actually hears and the only thing worth recording.
                    self._emit_agent_line(fallback)

                # Preprocess text to avoid gibberish: normalize punctuation that
                # can confuse Sarvam TTS (em-dashes produce audio glitches).
                # Figures first (RUNBOOK §82, per-agent opt-in): a "50–55"
                # range must become "fifty to fifty five" BEFORE the dash
                # below turns it into "fifty, fifty five".
                tts_text = _spoken_numbers_text(
                    response.text, self._tts_numbers_english())
                tts_text = tts_text.replace("—", ", ").replace("–", ", ")
                tts_text = tts_text.replace("…", ".").replace("…", ".")
                tts_text = tts_text.replace("•", "").replace("·", "")
                # Collapse multiple spaces/newlines that may appear in LLM output
                tts_text = re.sub(r'\s+', ' ', tts_text).strip()

                # A5: last gate before synthesis. Covers the dequeue→synth
                # window (including the stop()/sleep awaits above) where a
                # cont-merge rollback used to lose the race and both the
                # superseded and the merged reply were spoken.
                if self._delivery_epoch != _epoch_at_dequeue:
                    logger.info(
                        "[TTS] Dropped stale response (epoch %d < %d): %r",
                        _epoch_at_dequeue, self._delivery_epoch,
                        tts_text[:50])
                    continue

                logger.info("[TTS] Playing: %r (queued %.0fms ago)", tts_text, age_ms)

                self._tts_playing = True
                if self._session is not None:
                    self._session.set("AI_SPEAKING")
                self._tts_start_time = time.monotonic()
                self._last_playback_start_ts = self._tts_start_time
                # Connect delivery guard bookkeeping: new utterance, nothing
                # of it flushed yet, protected status from the Response.
                self._utt_seq += 1
                self._utt_protected = response.protected
                self._utt_first_flush_ts = 0.0
                # Barge-in guard for THIS utterance.  If this utterance is a
                # filler, or it is the real response that immediately follows a
                # filler, use the longer FILLER_ECHO_GRACE_MS so the filler's
                # own telephony echo (which lands during the next utterance)
                # cannot trip barge-in and cancel the real response.  Every
                # other utterance keeps the normal 250ms guard — full barge-in
                # responsiveness is preserved.
                prev_was_filler = self._last_was_filler
                self._last_was_filler = response.is_filler
                if response.is_filler or prev_was_filler:
                    self._barge_in_min_elapsed_ms = FILLER_ECHO_GRACE_MS
                else:
                    self._barge_in_min_elapsed_ms = BARGE_IN_MIN_ELAPSED_MS
                # Reset the barge-in streak at every utterance boundary so loud
                # frames accumulated from the PREVIOUS utterance's echo cannot
                # trip barge-in on this one.  This is what was cutting off the
                # real response right after a filler ("एक सेकंड…"): the filler's
                # echo built the streak to ~10, then the response started and
                # tripped barge-in the instant it passed the 250ms guard.  A
                # genuine interrupter simply rebuilds the streak (~60ms).
                self._speech_frame_streak = 0

                if hasattr(self.tts, 'set_language') and self.conversation:
                    _lang, _lang_src = self._resolve_tts_language(
                        response, tts_text)
                    self.tts.set_language(_lang)
                    # v2: per-language voice on mid-call language switch —
                    # only when the agent defines a voice for that language
                    # (absent = today's single-voice behaviour).
                    try:
                        _vcfg = self.conversation._def.languages.tts_voices.get(_lang)
                        if _vcfg is not None and hasattr(self.tts, 'set_voice'):
                            self.tts.set_voice(model=_vcfg.model,
                                               speaker=_vcfg.speaker,
                                               pace=getattr(_vcfg, 'pace', None))
                    except Exception:
                        pass
                    # One audit line per utterance: the accent decision used
                    # to be reconstructable only by cross-referencing three
                    # log families.
                    logger.info(
                        "[TTS] lang=%s src=%s speaker=%s",
                        _lang, _lang_src,
                        getattr(self.tts, "_speaker", "?"))

                # Canned lines (greeting, fillers, response-bank phrases) go
                # through the disk cache: repeat plays cost zero synthesis
                # time and zero Sarvam credits. LLM output is never cached.
                # Echo-text guard bookkeeping + shadow hygiene: a ring left
                # unclaimed from the PREVIOUS playback window must not leak
                # under this new utterance's audio.
                self._prev_tts_text = self._current_tts_text
                self._current_tts_text = tts_text
                if ENABLE_PLAYBACK_SHADOW and self._shadow_frames:
                    self._drop_shadow("stale — new utterance starting")
                if response.is_shortcut and hasattr(self.tts, "speak_cached"):
                    await self.tts.speak_cached(tts_text)
                else:
                    await self.tts.speak(tts_text)

            except asyncio.CancelledError:
                logger.info("[TTS] Interrupted")
            except Exception:
                logger.exception("[TTS] Playback failed — recovering")
                # Reset TTS state so next utterance can play
                try:
                    await self.tts.stop()
                except Exception:
                    pass
                # Silence protection (feature 5): play fallback phrase to avoid dead air
                lang = self.conversation.state.language if self.conversation else "en"
                fallback = self._stt_fallback_phrase(lang)
                logger.info("[TTS] Playing silence-protection fallback phrase (lang=%s)", lang)
                try:
                    await self.tts.speak(fallback)
                    # Recorded only on success: this path bypasses the TTS
                    # queue entirely, so nothing else would ever attribute
                    # these words to the agent.
                    self._emit_agent_line(fallback)
                except Exception:
                    logger.warning("[TTS] Fallback phrase also failed — accepting silence")
            finally:
                self._tts_playing = False
                if self._session is not None:
                    self._session.set("LIVE_CONVERSATION", "tts done")
                self._tts_end_time = time.monotonic()
                # This utterance is over — a still-latched deferral must not
                # leak onto the next one.
                self._bargein_deferred = False

                # Issue 4 / Issue 1: mark greeting as done after the very first
                # TTS utterance finishes.  From this point barge-in and STT
                # fallback phrases are permitted.
                was_greeting = not self._greeting_done
                if not self._greeting_done:
                    self._greeting_done = True
                    logger.info("[PIPELINE] Greeting complete — barge-in and STT fallback now active")

                # Stale-fragment anchors: when this reply started playing and
                # whether the caller let it finish (a cut reply must never
                # suppress the interrupting speech). A FILLER ("one second…")
                # answers nothing and must not become the anchor: on call 909
                # the filler that preceded a reply counted as "the reply we
                # delivered", so the caller's next question, begun 6 s before
                # it, was absorbed as that turn's tail.
                if not response.is_filler:
                    self._last_reply_tts_start = self._tts_start_time
                    self._last_reply_completed_clean = not self._barge_in_occurred
                    self._last_reply_was_onset = bool(response.onset_intro)
                if response.onset_intro:
                    # Synthesis ends long before the caller has heard the
                    # line; the sink knows when playback really ends.
                    self._onset_play_end_ts = max(
                        time.monotonic(),
                        float(getattr(self.sink, "playback_busy_until", 0.0)
                              or 0.0))
                # Playback ran to its end (or the caller cut in, which means
                # they HEARD it): this utterance is no longer retractable.
                # Without clearing, a later merge would take a line off the
                # transcript that the caller did hear.
                self._unheard_agent_line = ""
                if self._barge_in_occurred:
                    self._barge_in_occurred = False
                    # Accumulator intentionally kept: the next transcript will be
                    # merged with what the caller said before the interruption.
                else:
                    # Clean completion — caller was fully answered. Clear the
                    # turn accumulator so the next turn starts fresh. Record
                    # when TTS ended for post-response continuation detection.
                    # Unless a fragment was absorbed WHILE this reply played:
                    # those words were spoken before the reply and answered by
                    # nothing — they ride into the next turn as context.
                    if getattr(self, "_absorbed_during_reply", False):
                        self._absorbed_during_reply = False
                        logger.info("[ENGINE] Keeping absorbed fragment for the "
                                    "next turn: %r", self._turn_accumulator[:60])
                    else:
                        self._turn_accumulator = ""
                    self._last_tts_end = time.monotonic()
                    # Issue 3: In telephony mode the echo of TTS audio returns
                    # via mod_audio_stream with a delay of:
                    #   injection_overhead + audio_playback_duration
                    # The old 400 ms window expired long before the echo arrived,
                    # so agent speech leaked into STT.  Use the actual audio
                    # duration from TelephonySink (if available) to cover the
                    # full echo window.
                    now = time.monotonic()
                    busy_until = getattr(self.sink, 'playback_busy_until', 0.0)
                    audio_dur = getattr(self.sink, 'last_flush_duration', 0.0)
                    # Post-greeting mute gets a shorter echo tail and an
                    # earlier speech exit (see GREETING_ECHO_* constants).
                    self._greeting_echo_window = was_greeting
                    tail_s = (GREETING_ECHO_TAIL_MS / 1000.0
                              if was_greeting else 0.2)
                    if busy_until > 0.0:
                        # With incremental flushing, earlier parts of the
                        # utterance already played on the call WHILE speak()
                        # was still synthesizing later parts.  Anchoring the
                        # full utterance duration at speak()-return (old
                        # formula) therefore muted the pipeline for many
                        # seconds AFTER playback actually ended, swallowing
                        # the caller's reply.  The sink's flush-anchored
                        # playback clock already accounts for audio that has
                        # played; mute only until it says playback ends,
                        # plus a short echo tail.
                        mute_s = max(
                            max(busy_until - now, 0.0) + tail_s,
                            TTS_ECHO_COOLDOWN_MS / 1000.0,
                        )
                        logger.info(
                            "[ECHO-MUTE] Window %.1fs (playback ends in %.1fs "
                            "+ %.1fs tail%s)",
                            mute_s, max(busy_until - now, 0.0), tail_s,
                            ", greeting" if was_greeting else "",
                        )
                    elif audio_dur > 0.0:
                        # Sink tracks flush duration but not a playback
                        # clock: fall back to duration-anchored window.
                        mute_s = 0.7 + audio_dur + 0.2
                        logger.info(
                            "[ECHO-MUTE] Extended window %.1fs "
                            "(audio=%.1fs + 0.7s injection + 0.2s buffer)",
                            mute_s, audio_dur,
                        )
                    else:
                        mute_s = TTS_ECHO_COOLDOWN_MS / 1000.0
                    self._vad_mute_until = now + mute_s
                    self._echo_preserve_logged = False

                # Shadow disposition at utterance end (fleet defect #1):
                # a substantial ring = the caller spoke a whole utterance
                # over our audio — seed it as a live segment so the normal
                # silence path flushes it to STT (echo-gate preserves seeded
                # buffers). A thin ring may still be claimed by an onset
                # within the join window; after that it is dropped.
                if ENABLE_PLAYBACK_SHADOW:
                    self._shadow_until = (
                        self._vad_mute_until + SHADOW_JOIN_WINDOW_MS / 1000.0)
                    if self._shadow_speech_ms >= SHADOW_MIN_SPEECH_MS:
                        self._claim_shadow("utterance spoken over playback")

        logger.info("[TTS] Layer stopped")
