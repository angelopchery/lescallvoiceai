"""Whole-turn re-transcription (RUNBOOK §69).

Call 1032 (2026-10-07, Pramukh Aristo): the caller's sentences reached the
agent as separately-transcribed one-second pieces — "की बात कर सर।" + "कहां
रहने के वास्ते फिर हो?" for "किसकी बात कर रहा हूँ? कहाँ रहने की बात कर रहा
हूँ?", and "अभी जो" + "okay" + "तो वो जिन में और कुछ।" for "मैं अभी तो सोच ही
नहीं रहा कुछ।" (the agent replied "so you want to move ASAP?"). Half the
pieces came back EMPTY, and an empty piece's words were simply lost.
A turn built from ≥2 pieces is now transcribed once, as joined audio.
"""
from __future__ import annotations

import asyncio
import types
from collections import deque

import numpy as np

from app.voice.pipeline import Transcript, VoicePipeline


class _STT:
    def __init__(self, text="मैं अभी तो सोच ही नहीं रहा कुछ।"):
        self.text, self.calls = text, []

    async def transcribe(self, audio, sample_rate, language, default_language=None, **kw):
        self.calls.append((len(audio), language, default_language))
        await asyncio.sleep(0)
        return types.SimpleNamespace(text=self.text, confidence=None, source="primary")


def _pipe(stt=None, last_tts_end=0.0, busy=0.0):
    ns = types.SimpleNamespace(
        stt=stt or _STT(), _stt_lang_pin="hi-IN", _stt_accepts_hint=False,
        conversation=types.SimpleNamespace(
            state=types.SimpleNamespace(language="hi"),
            _is_bare_hello_text=lambda t: t.strip(" ?.!").lower() in ("hello", "हेलो")),
        _last_tts_end=last_tts_end,
        sink=types.SimpleNamespace(playback_busy_until=busy))
    for name in ("_retx_remember", "_retx_pieces", "_retx_language",
                 "_retx_start", "_retx_prefetch", "_retx_apply",
                 "_retx_close_window",
                 "_bare_hello", "_is_agent_echo_text"):
        setattr(ns, name, types.MethodType(getattr(VoicePipeline, name), ns))
    ns._is_agent_echo_text = lambda t: False
    return ns


def _seg(start, end, shadow=False):
    pcm = (np.ones(int(16000 * (end - start)), dtype=np.int16) * 500).tobytes()
    return types.SimpleNamespace(audio=pcm, duration_ms=(end - start) * 1000,
                                 timestamp=end + 0.2, speech_start_ts=start,
                                 speech_end_ts=end, shadow=shadow)


def _tr(text, start, end):
    return Transcript(text=text, duration_ms=(end - start) * 1000,
                      timestamp=end + 0.3, speech_start_ts=start, speech_end_ts=end)


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c) \
        if not asyncio.get_event_loop().is_closed() else asyncio.run(c)


def test_three_garbled_pieces_become_one_sentence():
    p = _pipe()
    p._retx_remember(_seg(100.0, 100.6), "अभी जो")
    p._retx_remember(_seg(100.8, 101.1), "okay")
    p._retx_remember(_seg(101.3, 102.6), "तो वो जिन में और कुछ।")
    out = asyncio.run(p._retx_apply(_tr("अभी जो okay तो वो जिन में और कुछ।", 100.0, 102.6)))
    assert out.text == "मैं अभी तो सोच ही नहीं रहा कुछ।"
    assert len(p.stt.calls) == 1 and p.stt.calls[0][1] == "hi-IN"


def test_an_empty_piece_is_not_lost():
    p = _pipe(_STT("हाँ, मुझे 3 BHK चाहिए Vapi में"))
    p._retx_remember(_seg(50.0, 51.0), "")          # Sarvam returned nothing
    p._retx_remember(_seg(51.3, 52.0), "Vapi में")
    out = asyncio.run(p._retx_apply(_tr("Vapi में", 51.3, 52.0)))
    assert out.text == "हाँ, मुझे 3 BHK चाहिए Vapi में"


def test_single_piece_turn_is_untouched():
    p = _pipe()
    p._retx_remember(_seg(10.0, 11.5), "हाँ जी बताओ।")
    t = _tr("हाँ जी बताओ।", 10.0, 11.5)
    assert asyncio.run(p._retx_apply(t)) is t
    assert p.stt.calls == []


def test_previous_turn_audio_is_never_rejoined():
    p = _pipe()
    p._retx_remember(_seg(10.0, 11.0), "पहला")
    p._retx_remember(_seg(11.2, 12.0), "टर्न")
    asyncio.run(p._retx_apply(_tr("पहला टर्न", 10.0, 12.0)))
    p.stt.calls.clear()
    p._retx_remember(_seg(20.0, 21.0), "नया जवाब")
    t = _tr("नया जवाब", 20.0, 21.0)
    assert asyncio.run(p._retx_apply(t)) is t and p.stt.calls == []


def test_empty_piece_over_our_own_playback_is_echo_not_caller():
    p = _pipe(busy=30.5)                 # our audio on the wire until 30.5
    p._retx_remember(_seg(29.0, 30.0), "")       # echo of the agent
    p._retx_remember(_seg(31.0, 32.0), "हाँ")
    t = _tr("हाँ", 31.0, 32.0)
    assert asyncio.run(p._retx_apply(t)) is t


def test_bare_hello_passes_through():
    p = _pipe()
    p._retx_remember(_seg(5.0, 5.5), "")
    p._retx_remember(_seg(6.0, 6.5), "hello")
    t = _tr("hello", 6.0, 6.5)
    assert asyncio.run(p._retx_apply(t)) is t


def test_empty_or_much_shorter_joined_text_keeps_fragments():
    for joined in ("", "हाँ"):
        p = _pipe(_STT(joined))
        p._retx_remember(_seg(1.0, 2.0), "मुझे Vapi में घर")
        p._retx_remember(_seg(2.2, 3.5), "चाहिए तीन BHK का")
        t = _tr("मुझे Vapi में घर चाहिए तीन BHK का", 1.0, 3.5)
        assert asyncio.run(p._retx_apply(t)).text == t.text


def test_prefetch_is_reused_not_repeated():
    p = _pipe()
    p._retx_remember(_seg(1.0, 2.0), "a")
    p._retx_remember(_seg(2.2, 3.0), "b")
    t = _tr("a b", 1.0, 3.0)

    async def go():
        p._retx_prefetch(t)
        await asyncio.sleep(0)
        return await p._retx_apply(t)
    asyncio.run(go())
    assert len(p.stt.calls) == 1
