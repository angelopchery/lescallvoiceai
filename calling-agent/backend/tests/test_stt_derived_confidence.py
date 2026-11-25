"""Derived STT confidence: the words-per-second hallucination guard.

On the batch path a transcript covers exactly the segment it was POSTed
for, so the local buffer IS the audio it can account for. The
`covered_seconds` escape hatch that let a stream-stitched transcript score
against server-side audio was removed with the WebSocket STT provider on
2026-07-31 (RUNBOOK §24)."""

from app.voice.stt.router import STTRouter

SR = 16000


def _audio(sec: float) -> bytes:
    return b"\x00" * int(2 * SR * sec)


TEXT = "शाम चार बजे साइट विजिट बुक कीजिए"  # 7 words


def test_impossible_word_rate_scores_low():
    # 7 words in 464ms is 15 wps — no human says that; score it down.
    score = STTRouter._derive_confidence(TEXT, _audio(0.46), SR, 800, None)
    assert score < 0.4


def test_normal_word_rate_scores_high():
    # The same 7 words over 4s of audio is ordinary speech.
    score = STTRouter._derive_confidence(TEXT, _audio(4.0), SR, 800, None)
    assert score >= 0.9
