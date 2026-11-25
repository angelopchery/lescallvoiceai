"""Adaptive TTS speech rate (2026-07-28).

Two documented Bulbul behaviours skew perceived speed away from the
configured pace: long lightly-punctuated sentences are delivered rushed
("too fast"), and Hindi runs audibly slower than English at the same pace
value ("too slow in Hindi"). _effective_pace() corrects both per request:
base pace × language factor × long-sentence damping, clamped to 0.5–2.0.
"""
from __future__ import annotations

import app.voice.tts_sarvam as tts_mod
from app.voice.tts_sarvam import SarvamTTS, _LANG_CODE_MAP, _effective_pace
from app.agent_definition.voice_catalog import PLATFORM_LANGUAGES

SHORT = "Sure, I can help with that."
# One 150+ char sentence with no terminal punctuation until the end — the
# shape Bulbul rushes through.
RUN_ON = ("So as I was saying about the project it has two and three bhk "
          "options with a clubhouse and a garden and parking and it is very "
          "close to the highway near Ambli road")
# Similar total length but split into short sentences — paces fine, no damp.
MULTI = ("We have two options. Both are near Ambli road. There is parking. "
         "There is also a clubhouse. The garden is very large. "
         "I think you will really like it. Shall we go ahead?")


class _DummySink:
    async def start(self):
        pass

    async def write_frame(self, pcm: bytes):
        pass


def test_language_factor_boosts_hindi(monkeypatch):
    monkeypatch.setattr(tts_mod, "TTS_ADAPTIVE_PACE", True)
    monkeypatch.setattr(tts_mod, "_LANG_PACE_FACTORS", {"hi": 1.08, "en": 1.0})
    assert _effective_pace(1.15, "hi", SHORT) == round(1.15 * 1.08, 3)
    assert _effective_pace(1.15, "en", SHORT) == 1.15


def test_unknown_language_uses_base(monkeypatch):
    monkeypatch.setattr(tts_mod, "TTS_ADAPTIVE_PACE", True)
    assert _effective_pace(1.15, "xx", SHORT) == 1.15


def test_long_sentence_damped(monkeypatch):
    monkeypatch.setattr(tts_mod, "TTS_ADAPTIVE_PACE", True)
    monkeypatch.setattr(tts_mod, "_LANG_PACE_FACTORS", {"en": 1.0})
    assert len(RUN_ON) >= tts_mod._LONG_SENT_CHARS
    assert _effective_pace(1.15, "en", RUN_ON) == round(
        1.15 * tts_mod._LONG_SENT_FACTOR, 3)


def test_short_sentences_not_damped_even_when_chunk_is_long(monkeypatch):
    monkeypatch.setattr(tts_mod, "TTS_ADAPTIVE_PACE", True)
    monkeypatch.setattr(tts_mod, "_LANG_PACE_FACTORS", {"en": 1.0})
    assert len(MULTI) >= tts_mod._LONG_SENT_CHARS  # long chunk...
    assert _effective_pace(1.15, "en", MULTI) == 1.15  # ...but no run-on


def test_clamped_to_bulbul_v3_range(monkeypatch):
    monkeypatch.setattr(tts_mod, "TTS_ADAPTIVE_PACE", True)
    monkeypatch.setattr(tts_mod, "_LANG_PACE_FACTORS", {"hi": 1.5})
    assert _effective_pace(1.9, "hi", SHORT) == 2.0
    monkeypatch.setattr(tts_mod, "_LANG_PACE_FACTORS", {"hi": 0.5})
    assert _effective_pace(0.6, "hi", SHORT) == 0.5


def test_disable_flag_restores_raw_base(monkeypatch):
    monkeypatch.setattr(tts_mod, "TTS_ADAPTIVE_PACE", False)
    assert _effective_pace(1.15, "hi", RUN_ON) == 1.15


def test_falsy_base_defaults_to_natural(monkeypatch):
    monkeypatch.setattr(tts_mod, "TTS_ADAPTIVE_PACE", False)
    assert _effective_pace(0.0, "en", SHORT) == 1.0


def test_lang_code_map_covers_platform_roster():
    # A platform language missing here silently synthesizes as en-IN —
    # mispronounced Indic through the English frontend sounds slow/halting.
    missing = [l for l in PLATFORM_LANGUAGES if l not in _LANG_CODE_MAP]
    assert missing == []


def test_cache_key_includes_pace_config(monkeypatch):
    tts = SarvamTTS(api_key="test", audio_sink=_DummySink())
    monkeypatch.setattr(tts_mod, "TTS_CANNED_CACHE", True)
    monkeypatch.setattr(tts_mod, "_PACE_CFG_TAG", "cfg-a")
    path_a = tts._cache_path("Hello there")
    monkeypatch.setattr(tts_mod, "_PACE_CFG_TAG", "cfg-b")
    path_b = tts._cache_path("Hello there")
    assert path_a != path_b  # retuned knobs must miss the old-pace cache


def test_stream_rate_mismatch_warns(caplog):
    tts = SarvamTTS(api_key="test", audio_sink=_DummySink(), sample_rate=8000)
    header = (b"RIFF" + (36).to_bytes(4, "little") + b"WAVE"
              + b"fmt " + (16).to_bytes(4, "little")
              + (1).to_bytes(2, "little") + (1).to_bytes(2, "little")
              + (24000).to_bytes(4, "little")  # Sarvam's 24k default
              + (48000).to_bytes(4, "little")
              + (2).to_bytes(2, "little") + (16).to_bytes(2, "little"))
    with caplog.at_level("WARNING"):
        tts._check_stream_rate(header + b"\x00\x00" * 100)
    assert any("24000" in r.message for r in caplog.records)


def test_stream_rate_silent_on_raw_pcm(caplog):
    tts = SarvamTTS(api_key="test", audio_sink=_DummySink(), sample_rate=8000)
    with caplog.at_level("WARNING"):
        tts._check_stream_rate(b"\x12\x34" * 512)   # headerless linear16
    assert not any("Hz" in r.message for r in caplog.records)
