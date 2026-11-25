"""Call 1033 (2026-10-07, Prashant / Pramukh Aristo) — RUNBOOK §70.

* two replies were cut 0.15 s / 0.43 s into playback by echo or line noise;
  the caller heard nothing and sat in dead air until they said hello;
* "कौन बात कर रहे हैं?" was discarded as the agent's own echo;
* "Vapi में" came back as "अभी मैं" / "बाबी में" / "I like it.", and a
  trailing "Vapi में" piece that returned empty was dropped from the turn.
"""
from __future__ import annotations

import asyncio
import types
from pathlib import Path

import numpy as np
import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice import pipeline as pl
from app.voice.conversation import ConversationRouter
from app.voice.pipeline import Transcript, VoicePipeline

FIX = (Path(__file__).parent / "fixtures"
       / "pramukh_aristo_prashant_outbound_2026_10_07.yaml")


# ── barge-in against a reply the caller has not heard ───────────────────────
def _guard(now, call_start, first_flush, busy_until):
    ns = types.SimpleNamespace(
        _tts_playing=True, _utt_protected=False, _call_start_ts=call_start,
        _utt_first_flush_ts=first_flush,
        sink=types.SimpleNamespace(playback_busy_until=busy_until),
        _connect_cfg=lambda: {})
    return VoicePipeline._connect_barge_defer(ns, now)


def test_mid_call_unheard_reply_is_protected(monkeypatch):
    monkeypatch.setattr(pl, "CONNECT_DELIVERY_GUARD", True)   # on in telephony
    # 60 s into the call (past the connect window), 0.15 s audible.
    assert pl.BARGE_GUARD_ALL_REPLIES
    assert _guard(now=1060.15, call_start=1000.0, first_flush=1060.0,
                  busy_until=1063.0)


def test_heard_reply_can_still_be_interrupted(monkeypatch):
    monkeypatch.setattr(pl, "CONNECT_DELIVERY_GUARD", True)
    assert not _guard(now=1062.0, call_start=1000.0, first_flush=1060.0,
                      busy_until=1063.0)


# ── echo-text guard ─────────────────────────────────────────────────────────
def _echo(text, agent):
    ns = types.SimpleNamespace(_current_tts_text=agent, _prev_tts_text="")
    return VoicePipeline._is_agent_echo_text(ns, text)


def test_callers_own_question_is_not_echo():
    agent = "और श्री Angelo, आप कहाँ से बात कर रहे हैं — Vapi में ही हैं?"
    assert not _echo("कौन बात कर रहे हैं?", agent)


def test_true_echo_is_still_caught():
    agent = "और श्री Angelo, आप कहाँ से बात कर रहे हैं — Vapi में ही हैं?"
    assert _echo("आप कहाँ से बात कर रहे हैं", agent)


# ── trailing empty piece belongs to the turn ────────────────────────────────
class _STT:
    async def transcribe(self, audio, sample_rate, language, default_language=None, **kw):
        return types.SimpleNamespace(text="हाँ, Vapi में", confidence=None, source="primary")


def _seg(start, end):
    pcm = (np.ones(int(16000 * (end - start)), dtype=np.int16) * 500).tobytes()
    return types.SimpleNamespace(audio=pcm, duration_ms=(end - start) * 1000,
                                 timestamp=end + 0.2, speech_start_ts=start,
                                 speech_end_ts=end, shadow=False)


def test_trailing_empty_piece_is_joined():
    ns = types.SimpleNamespace(
        stt=_STT(), _stt_lang_pin="hi-IN", _stt_accepts_hint=False,
        conversation=types.SimpleNamespace(state=types.SimpleNamespace(language="hi"),
                                           _is_bare_hello_text=lambda t: False),
        _last_tts_end=0.0, sink=types.SimpleNamespace(playback_busy_until=0.0))
    for n in ("_retx_remember", "_retx_pieces", "_retx_language", "_retx_start",
              "_retx_prefetch", "_retx_apply", "_bare_hello",
              "_retx_close_window"):
        setattr(ns, n, types.MethodType(getattr(VoicePipeline, n), ns))
    ns._is_agent_echo_text = lambda t: False
    ns._retx_remember(_seg(100.0, 100.5), "हाँ।")
    ns._retx_remember(_seg(101.4, 102.3), "")          # "Vapi में" → empty
    t = Transcript(text="हाँ।", duration_ms=500, timestamp=100.8,
                   speech_start_ts=100.0, speech_end_ts=100.5)
    assert asyncio.run(ns._retx_apply(t)).text == "हाँ, Vapi में"


# ── "Vapi में" sound-alikes, only after a place question ────────────────────
@pytest.fixture()
def router():
    return ConversationRouter(agent_def=AgentDefinition.from_yaml(FIX),
                              default_language="hi")


@pytest.mark.parametrize("heard,fixed", [
    ("अभी मैं", "Vapi में"), ("अभी मैं।", "Vapi में।"), ("बाबी में", "Vapi में"),
    ("abhi mai", "Vapi mai"), ("Vapi में", "Vapi में"),
])
def test_city_soundalike_after_place_question(router, heard, fixed):
    router._last_asked_slot = "calling_from"
    assert router._contextual_city_fix(heard) == fixed


@pytest.mark.parametrize("text,slot", [
    ("अभी मैं", "purpose"),                         # not a place question
    ("अभी मैं घर देख रहा हूँ", "calling_from"),       # a real sentence
])
def test_no_correction_outside_context(router, text, slot):
    router._last_asked_slot = slot
    assert router._contextual_city_fix(text) == text
