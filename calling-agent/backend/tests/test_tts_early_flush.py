"""Early first-flush of TTS audio (2026-07-23 latency work).

The caller used to wait for part 1's ENTIRE Sarvam stream before hearing
anything (median +806ms dead air). speak() now flushes the first ~0.9-1.6s
of PCM as its own WAV, cutting at an inter-word pause so the bridge's 100ms
inter-file pacing gap never lands mid-word.
"""
from __future__ import annotations

import asyncio

from app.voice.tts_sarvam import (
    SarvamTTS, _EARLY_FLUSH_MIN_S, _EARLY_FLUSH_MAX_S,
    _EARLY_FLUSH_SILENCE_RMS, _tail_is_pause,
)


def test_tail_is_pause_detects_silence():
    sr = 8000
    silence = b"\x00\x00" * int(sr * 0.05)          # 50ms of digital silence
    assert _tail_is_pause(silence, sr) is True


def test_tail_is_pause_rejects_speech():
    import math
    sr = 8000
    # 50ms of a loud 400Hz tone (well above the RMS threshold)
    samples = bytearray()
    for i in range(int(sr * 0.05)):
        v = int(20000 * math.sin(2 * math.pi * 400 * i / sr))
        samples += v.to_bytes(2, "little", signed=True)
    assert _tail_is_pause(bytes(samples), sr) is False


def test_tail_is_pause_needs_full_window():
    # A buffer shorter than the scan window can't be judged — no flush.
    assert _tail_is_pause(b"\x00\x00" * 10, 8000) is False


class _FlushRecordingSink:
    """AudioSink lookalike that records flush sizes."""

    def __init__(self):
        self.buf = bytearray()
        self.flush_sizes: list[int] = []
        self.last_flush_duration = 0.0

    async def start(self):
        pass

    async def write_frame(self, pcm: bytes):
        self.buf += pcm

    async def flush(self):
        if self.buf:
            self.flush_sizes.append(len(self.buf))
            self.last_flush_duration = len(self.buf) / (2 * 8000)
            self.buf.clear()


def _tts_with_fake_stream(chunks_per_part):
    tts = SarvamTTS(api_key="test", sample_rate=8000,
                    audio_sink=_FlushRecordingSink())

    async def fake_stream(part):
        for c in chunks_per_part:
            yield type("C", (), {"audio": c})()

    tts._stream_audio = fake_stream
    return tts


def test_early_flush_fires_at_pause_after_min_buffer():
    sr = 8000
    # 1.0s of "speech" (constant amplitude) then a silent chunk = pause
    speech = (b"\x10\x27" * int(sr * 1.0))          # ~10000 amplitude
    pause = b"\x00\x00" * int(sr * 0.1)
    tts = _tts_with_fake_stream([speech, pause, speech])
    asyncio.run(tts.speak("hello there my friend, this is a long reply"))
    sink = tts._sink
    # 3 flushes: early (speech+pause), end-of-part, final (empty → skipped)
    assert len(sink.flush_sizes) >= 2
    # the first flush must be the early one: min buffer ≤ size ≤ cap + chunk
    first_s = sink.flush_sizes[0] / (2 * sr)
    assert _EARLY_FLUSH_MIN_S <= first_s <= _EARLY_FLUSH_MAX_S + 0.2


def test_early_flush_hard_cap_without_pause():
    sr = 8000
    # continuous loud speech, no pause anywhere: cap must force the flush
    chunk = (b"\x10\x27" * int(sr * 0.2))           # 200ms chunks
    tts = _tts_with_fake_stream([chunk] * 12)       # 2.4s total
    asyncio.run(tts.speak("one long unpunctuated run of words"))
    sink = tts._sink
    first_s = sink.flush_sizes[0] / (2 * sr)
    assert first_s <= _EARLY_FLUSH_MAX_S + 0.25     # cap honored (±1 chunk)


def test_no_early_flush_below_min_buffer():
    sr = 8000
    # a short utterance: total audio < min threshold → single flush at end
    chunk = (b"\x10\x27" * int(sr * 0.3))           # 300ms
    tts = _tts_with_fake_stream([chunk])
    asyncio.run(tts.speak("okay!"))
    sink = tts._sink
    assert len(sink.flush_sizes) == 1


def test_early_flush_disabled_while_capturing_cache():
    sr = 8000
    speech = (b"\x10\x27" * int(sr * 1.2))
    pause = b"\x00\x00" * int(sr * 0.1)
    tts = _tts_with_fake_stream([speech, pause])
    tts._capture_buf = bytearray()                  # cache-capture mode
    asyncio.run(tts.speak("greeting being cached right now"))
    sink = tts._sink
    assert len(sink.flush_sizes) == 1               # single whole-part flush
