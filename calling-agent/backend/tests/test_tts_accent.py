"""TTS accent consistency (2026-07-30).

Live calls spoke English with a heavy Hindi accent: every session started
at the hardcoded "hi" seed (the agent's languages.default was never
applied), the TTS target_language_code followed mutable conversation state
instead of the text being spoken, and Sarvam's per-segment STT detection
rewrote that state unguarded — before transcript validation. Full
diagnosis: deploy/PLAN-tts-accent-consistency.md.

Covers: the session-language seed (P0), the text-derived TTS language
(P1: tts_language_for + the pipeline resolver precedence stamp > text >
state), the guarded STT language mirror (P2: stt_mirror_consistent), the
per-agent/env opt-outs, and the per-language speaker recommendations (P5).
"""

from __future__ import annotations

import types

import pytest

from app.voice.conversation import ConversationRouter
from test_call_termination import make_router


@pytest.fixture(autouse=True)
def _no_network_llm(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


# ── P0: session language seeded from the agent default ─────────────────────

def test_seed_from_agent_default():
    base = make_router()
    for code in ("en", "hi", "gu"):
        r = ConversationRouter(agent_def=base._def, default_language=code)
        assert r.state.language == code


def test_seed_clamps_non_conversational_codes():
    """mr/ta/… keep the legacy hi seed — fillers/responses only exist for
    en/hi/gu."""
    base = make_router()
    r = ConversationRouter(agent_def=base._def, default_language="mr")
    assert r.state.language == "hi"


# ── P1: the text decides the accent ─────────────────────────────────────────

def test_english_text_never_hindi_labelled():
    """THE incident: English reply while conversation state says Hindi."""
    r = make_router()
    r.state.language = "hi"
    assert r.tts_language_for("I completely understand.") == "en"


def test_devanagari_is_hindi_even_when_state_english():
    r = make_router()
    r.state.language = "en"
    assert r.tts_language_for("हाँ, मुझे आपकी आवाज़ आ रही है।") == "hi"


def test_gujarati_script_wins():
    r = make_router()
    r.state.language = "en"
    assert r.tts_language_for("ચોક્કસ, હું રાહ જોઉં છું.") == "gu"


def test_code_mixed_devanagari_latin_stays_hindi():
    """Sarvam speaks embedded English natively under hi-IN; Devanagari
    under en-IN does not work."""
    r = make_router()
    r.state.language = "en"
    assert r.tts_language_for(
        "आपका appointment Saturday को confirm है।") == "hi"


def test_gujarati_written_in_devanagari():
    r = make_router()
    assert r.tts_language_for("गेम छो, मजामा?") == "gu"


def test_romanized_hindi_follows_state():
    r = make_router()
    r.state.language = "hi"
    assert r.tts_language_for("haan theek hai, batao") == "hi"
    r.state.language = "en"
    assert r.tts_language_for("haan theek hai, batao") == "en"


def test_empty_text_falls_back_to_state():
    r = make_router()
    r.state.language = "gu"
    assert r.tts_language_for("") == "gu"


# ── P1: pipeline resolver precedence (stamp > text > state) ─────────────────

def _pl_ns(conv):
    from app.voice.pipeline import VoicePipeline

    ns = types.SimpleNamespace(conversation=conv)
    ns._resolve_tts_language = types.MethodType(
        VoicePipeline._resolve_tts_language, ns)
    return ns


def _resp(text, language=None):
    from app.voice.pipeline import Response

    return Response(text=text, is_shortcut=False, timestamp=0.0,
                    language=language)


def test_resolver_stamp_wins():
    ns = _pl_ns(make_router())
    assert ns._resolve_tts_language(
        _resp("x", language="gu"), "x") == ("gu", "stamp")


def test_resolver_derives_from_text():
    conv = make_router()
    conv.state.language = "hi"
    ns = _pl_ns(conv)
    assert ns._resolve_tts_language(
        _resp("Sure, I can help."), "Sure, I can help.") == ("en", "text")


def test_resolver_env_optout(monkeypatch):
    import app.voice.pipeline as pl

    monkeypatch.setattr(pl, "TTS_LANG_FROM_TEXT", False)
    conv = make_router()
    conv.state.language = "hi"
    ns = _pl_ns(conv)
    assert ns._resolve_tts_language(
        _resp("Sure."), "Sure.") == ("hi", "state")


def test_resolver_agent_optout():
    conv = make_router()
    conv.state.language = "hi"
    conv._def.languages.tts_lang_from_text = False
    ns = _pl_ns(conv)
    assert ns._resolve_tts_language(
        _resp("Sure."), "Sure.") == ("hi", "state")


def test_schema_parses_agent_optout():
    r = make_router(languages={"supported": ["en"], "default": "en",
                               "tts_voices": {},
                               "tts_lang_from_text": False})
    assert r._def.languages.tts_lang_from_text is False
    assert make_router()._def.languages.tts_lang_from_text is True


# ── P2: guarded STT language mirror ─────────────────────────────────────────

def test_mirror_rejects_hindi_claim_on_english_text():
    """Indian-accented English tagged hi-IN must not flip the agent."""
    r = make_router()
    assert r.stt_mirror_consistent("hi", "I completely understand.") is False


def test_mirror_accepts_real_hindi():
    r = make_router()
    assert r.stt_mirror_consistent("hi", "हाँ ठीक है") is True
    assert r.stt_mirror_consistent("hi", "haan theek hai") is True


def test_mirror_bare_hello_never_flips():
    r = make_router()
    for claim in ("en", "hi", "gu"):
        assert r.stt_mirror_consistent(claim, "hello") is False


def test_mirror_english_needs_two_words():
    """A lone 'okay' from a Hindi speaker is not an English switch."""
    r = make_router()
    assert r.stt_mirror_consistent("en", "okay") is False
    assert r.stt_mirror_consistent("en", "i need pricing details") is True
    assert r.stt_mirror_consistent("en", "जरूरत नहीं है") is False


def test_mirror_gujarati():
    r = make_router()
    assert r.stt_mirror_consistent("gu", "કેમ છો") is True
    assert r.stt_mirror_consistent("gu", "kem cho majama") is True
    assert r.stt_mirror_consistent("gu", "hello there") is False


def test_mirror_empty_text_never_flips():
    r = make_router()
    assert r.stt_mirror_consistent("hi", "") is False


# ── P5: per-language speaker recommendations ────────────────────────────────

def test_recommended_speakers_valid_for_v3():
    from app.agent_definition.voice_catalog import (
        RECOMMENDED_SPEAKERS, SPEAKER_CATALOG, recommended_speaker)

    assert recommended_speaker("en", "female") == "ishita"
    assert recommended_speaker("en", "male") == "ratan"
    assert recommended_speaker("hi", "male") == "shubh"
    assert recommended_speaker("??", "female") == "priya"
    for spk in RECOMMENDED_SPEAKERS.values():
        assert "bulbul:v3" in SPEAKER_CATALOG[spk]["models"], spk
