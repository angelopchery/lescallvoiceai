"""
Call-session state machine (deploy/PLAN-vad-stack.md C5).

One explicit, timestamped answer to "what is this call doing right now",
instead of every component re-deriving it from scattered booleans
(_tts_playing, _greeting_done, mute windows, busy-until horizons).

SHADOW BY DOCTRINE (phase A): the pipeline PUSHES transitions from the
places that already own the signals, the machine logs them, stamps the
EventHub, and interprets classifier labels — but no existing consumer
reads it yet. Consumers flip one at a time in phase B, each its own
commit, so a state-derivation bug can never take down barge-in and the
watchdogs in the same change.

States (media axis — orthogonal to the sales conversation_stage):

    CALL_INIT          media up, greeting not yet played
    AI_SPEAKING        agent audio in flight (greeting or reply)
    LIVE_CONVERSATION  floor is the caller's / idle listening
    USER_INTERRUPTED   barge-in fired, reply being cut
    THINKING           endpoint fired, LLM generating
    IVR_SUSPECT        outbound: unbroken machine-like monologue
    VOICEMAIL_SUSPECT  outbound: monologue then BEEP
    MUSIC              caller tune / hold music (classifier)
    CALL_ENDED         teardown

IVR vs VOICEMAIL vs plain speech are interpretations of HUMAN_SPEECH
patterns over time, not acoustics — that logic lives here, fed by
AudioEventClassifier labels.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

CALL_INIT = "CALL_INIT"
AI_SPEAKING = "AI_SPEAKING"
LIVE_CONVERSATION = "LIVE_CONVERSATION"
USER_INTERRUPTED = "USER_INTERRUPTED"
THINKING = "THINKING"
IVR_SUSPECT = "IVR_SUSPECT"
VOICEMAIL_SUSPECT = "VOICEMAIL_SUSPECT"
MUSIC = "MUSIC"
CALL_ENDED = "CALL_ENDED"

# Outbound: unbroken speech this long before the callee ever heard us
# respond → machine greeting (IVR menu or voicemail preamble).
_MONOLOGUE_SUSPECT_MS = 8000.0


class CallSession:
    def __init__(self, conn_id: str = "", direction: str = "inbound",
                 event_hub=None) -> None:
        self.conn_id = conn_id
        self.direction = direction
        self._hub = event_hub
        self.state = CALL_INIT
        self.since = time.monotonic()
        self.history: list[tuple[str, float]] = [(CALL_INIT, self.since)]
        # classifier-pattern bookkeeping
        self._speech_run_ms = 0.0
        self._had_exchange = False   # caller and agent have both spoken
        # Confirmed mailbox beeps (audio_events.on_beep, RUNBOOK §83). The
        # pipeline watchdog reads last_beep; whether it ENDS the call is the
        # engine's decision (outbound leg, nobody has spoken yet).
        self.beep_count = 0
        self.last_beep: dict | None = None

    # ── transitions pushed by the pipeline ───────────────────────────

    def set(self, state: str, why: str = "") -> None:
        if state == self.state or self.state == CALL_ENDED:
            return
        prev, self.state, self.since = self.state, state, time.monotonic()
        self.history.append((state, self.since))
        if len(self.history) > 200:
            del self.history[:100]
        logger.info("[SESSION] %s → %s%s", prev, state,
                    f" ({why})" if why else "")
        if self._hub is not None:
            try:
                self._hub.emit_nowait({"type": "session_state",
                                       "state": state, "prev": prev})
            except Exception:
                pass

    def mark_exchange(self) -> None:
        """A real caller turn reached the engine — this is a conversation,
        not a machine monologue."""
        self._had_exchange = True
        if self.state in (IVR_SUSPECT, MUSIC):
            self.set(LIVE_CONVERSATION, "caller turn landed")

    # ── classifier feed (from the FSMN worker thread) ────────────────

    def on_audio_label(self, label: str, prev: str, at_ms: float) -> None:
        """Interpret acoustic labels into session semantics. Log-only for
        inbound; the outbound campaign layer will act on VOICEMAIL_SUSPECT
        in its own phase."""
        if label == "HUMAN_SPEECH":
            self._speech_run_ms = 0.0
        elif prev == "HUMAN_SPEECH":
            pass
        if label == "MUSIC" and not self._had_exchange \
                and self.state in (CALL_INIT, LIVE_CONVERSATION):
            kind = ("caller-tune" if self.direction == "outbound"
                    else "hold-music")
            self.set(MUSIC, kind)
        elif label == "BEEP" and self.state == IVR_SUSPECT:
            self.set(VOICEMAIL_SUSPECT, "beep after monologue")
        elif label in ("RINGBACK", "BUSY", "FAX", "DTMF"):
            logger.info("[SESSION] tone observed: %s (state=%s)",
                        label, self.state)

    def on_beep(self, duration_ms: float, freq_hz: float, at_ms: float) -> None:
        """A confirmed single-frequency tone burst that STOPPED — the shape of
        a mailbox's record cue. Called from the FSMN worker thread: record it
        and nothing else; the pipeline watchdog decides on the event loop."""
        self.beep_count += 1
        self.last_beep = {
            "at_ms": at_ms, "at_mono": time.monotonic(),
            "duration_ms": duration_ms, "freq_hz": freq_hz,
            "state": self.state, "had_exchange": self._had_exchange,
        }
        logger.info("[SESSION] beep %.0fms @%.0fHz (state=%s, exchange=%s)",
                    duration_ms, freq_hz, self.state, self._had_exchange)
        if (self.direction == "outbound" and not self._had_exchange
                and self.state == IVR_SUSPECT):
            self.set(VOICEMAIL_SUSPECT, "beep after monologue")

    def on_speech_run(self, run_ms: float) -> None:
        """Continuous FSMN-speech length, ticked by the pipeline. An
        unbroken machine-length monologue before any exchange, on an
        outbound leg, is an IVR/voicemail greeting."""
        self._speech_run_ms = run_ms
        if (self.direction == "outbound" and not self._had_exchange
                and run_ms >= _MONOLOGUE_SUSPECT_MS
                and self.state in (CALL_INIT, LIVE_CONVERSATION)):
            self.set(IVR_SUSPECT, f"monologue {run_ms:.0f}ms, no exchange")
