"""
Endpoint detection — SEPARATE from VAD (deploy/PLAN-vad-stack.md C3/C4).

VAD answers "is the caller making speech sounds"; this answers "has the
customer finished their thought, should the LLM start". With
VAD_ENGINE=fsmn the model has already absorbed FSMN_MAX_END_SIL_MS of
pause tolerance before it closes a segment, so the residuals here are
small; the semantic signals come from streaming STT's mid-turn segment
finals (real text, live since 2026-07-29).

Profiles, decided fresh every silence frame:
    fast_complete  — the transcript so far reads finished (terminal
                     punctuation, ≥2 words): endpoint after a short
                     residual. A finished "हाँ ठीक है।" should not wait
                     out a pause budget sized for ditherers.
    default        — no semantic signal either way.
    hold           — the caller is mid-thought: dangling conjunction,
                     digit dictation in bursts (the phone-number trap),
                     or no text yet from a still-open stream. Wait
                     longer before cutting.

Every decision logs its profile + inputs; the same numbers land in
call_turns via the pipeline's turn trace for offline tuning.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# Residual silence AFTER the FSMN segment-close (which already waited
# FSMN_MAX_END_SIL_MS ≈ 280ms), in 20ms frames.
_FAST_FRAMES = int(os.environ.get("EP_FAST_RESIDUAL_FRAMES", "2"))    # +40ms
_DEFAULT_FRAMES = int(os.environ.get("EP_DEFAULT_RESIDUAL_FRAMES", "7"))  # +140ms
_HOLD_FRAMES = int(os.environ.get("EP_HOLD_RESIDUAL_FRAMES", "26"))   # +520ms

_TERMINAL = re.compile(r"[.!?।]\s*$")
_DANGLING = re.compile(
    r"\b(but|and|or|yet|however|also|so|although|though|because|since|if|"
    r"when|while|after|before|aur|lekin|par|ki|to|matlab|"
    r"और|लेकिन|पर|कि|तो|मतलब|अने|પણ|કે|તો)\s*[,.]?\s*$",
    re.IGNORECASE)
# Digit dictation: a number burst at the tail means the caller is reading
# something out (phone, budget, pincode) and pauses BETWEEN groups.
_DIGIT_TAIL = re.compile(r"[\d०-९૦-૯][\d०-९૦-૯\s\-]{1,}\s*$")


class EndpointController:
    """should_flush() is consulted by the pipeline's VAD loop on every
    silence frame once minimum speech has accumulated (fsmn engine only —
    the legacy energy path keeps its fixed HARD_FLUSH_SILENCE_FRAMES)."""

    def __init__(self) -> None:
        self.last_profile = "default"

    def should_flush(
        self,
        silence_frames: int,
        speech_ms: float,
        turn_text: str,
        stream_active: bool,
    ) -> bool:
        text = (turn_text or "").strip()
        profile = "default"
        need = _DEFAULT_FRAMES

        # Empty text is NOT a hold signal: Sarvam's segment final usually
        # lags our local endpoint by 100-300ms, so most turns decide on
        # acoustics alone (default profile). Semantics only ever make the
        # decision faster (complete sentence) or slower (dangling/digits).
        if text:
            if _DANGLING.search(text) or _DIGIT_TAIL.search(text):
                profile, need = "hold", _HOLD_FRAMES
            elif _TERMINAL.search(text) and len(text.split()) >= 2:
                profile, need = "fast_complete", _FAST_FRAMES

        self.last_profile = profile
        if silence_frames >= need:
            logger.info(
                "[ENDPOINT] flush profile=%s silence=%d/%d speech=%.0fms "
                "text_tail=%r", profile, silence_frames, need, speech_ms,
                text[-40:])
            return True
        return False
