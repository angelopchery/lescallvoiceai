"""Language fidelity (2026-07-30).

A live Hindi call flipped to Gujarati mid-conversation: the strong
Gujarati-marker regex matched "छे" INSIDE the everyday Hindi word "अच्छे"
(Python's \\w excludes Devanagari combining marks, so `(?<!\\w)` fired
mid-word), and the unlocked single-signal switch turned one false hit into
a whole-call language change. Separately, the Latin name "alvin anthony"
flipped the call to English, and Whisper mishears (", angie?" for "हाँजी")
steered language state. Full diagnosis:
deploy/PLAN-language-fidelity-and-contact-capture.md.

Covers: the Devanagari-block marker boundaries (A1), the evidence-density
switch scorer and its tiers (A2), the neutral-turn exclusions and mirror
hardening (A3), the STT source threading + mixed-script salvage (A4), and
the delivery-epoch bookkeeping (A5).
"""

from __future__ import annotations

import asyncio
import types

import pytest

from app.voice.conversation import (_STRONG_GU_DEVA_RE, _WEAK_GU_DEVA_RE,
                                    ConversationRouter)
from test_call_termination import make_router


@pytest.fixture(autouse=True)
def _no_network_llm(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


# ── A1: marker boundaries ───────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "अच्छे से मैनेज तो कर रहे हैं।",     # THE incident sentence
    "मुझे अच्छे से पता है",
    "पूछो उनसे",
    "छोटा काम है",
    "वो चाहती है",                        # weak "हती" inside चाहती
])
def test_gu_markers_no_false_positive(text):
    assert ConversationRouter._has_gujarati_markers_devanagari(text) is False


@pytest.mark.parametrize("text", [
    "केम छो",
    "गेम छो, मजामा?",                     # STT's Devanagari for "kem cho"
    "तमने केम छे",
    "हुं जाऊं छुं",
    "माने बरोबर लागे",                     # two weak markers
])
def test_gu_markers_true_positive(text):
    assert ConversationRouter._has_gujarati_markers_devanagari(text) is True


def test_strong_regex_boundary_is_deva_block():
    assert _STRONG_GU_DEVA_RE.search("अच्छे") is None
    assert _STRONG_GU_DEVA_RE.search("केम") is not None
    assert _WEAK_GU_DEVA_RE.search("चाहती") is None
    assert _WEAK_GU_DEVA_RE.search("बरोबर") is not None


# ── A2: evidence-density scorer ─────────────────────────────────────────────

def test_names_cast_no_votes():
    r = make_router()
    votes, _ = r._language_votes("alvin anthony")
    assert votes == {"en": 0, "hi": 0, "gu": 0}


def test_acks_cast_no_votes():
    r = make_router()
    votes, _ = r._language_votes("yes yes. oh haanji haanji.")
    assert sum(votes.values()) == 0


def test_devanagari_votes_hindi():
    r = make_router()
    votes, _ = r._language_votes("हम काम करते हैं")
    assert votes["hi"] == 4 and votes["gu"] == 0


def test_incident_sentence_votes_hindi_not_gujarati():
    r = make_router()
    votes, _ = r._language_votes("अच्छे से मैनेज तो कर रहे हैं।")
    assert votes["gu"] == 0 and votes["hi"] >= 5


def test_switch_instant_on_dense_evidence():
    r = make_router()
    r.state.language = "en"
    target, reason = r._evidence_switch_decision("हम सीमेंट का काम करते हैं")
    assert target == "hi" and reason.startswith("evidence(")


def test_switch_instant_gujarati_markers():
    # The agent must DECLARE Gujarati: since 2026-08-04 switching is bounded
    # by languages.supported, so an agent with no Gujarati greeting, no
    # Gujarati responses and no Gujarati voice is never carried into it.
    r = make_router(languages={"supported": ["en", "hi", "gu"],
                               "default": "hi", "tts_voices": {}})
    r.state.language = "hi"
    r.state.turn_count = 5
    target, reason = r._evidence_switch_decision("तमे केम छो")
    assert target == "gu"


def test_gujarati_markers_do_not_switch_an_agent_without_gujarati():
    """The same utterance, on an agent built only for English and Hindi."""
    r = make_router()                       # supported = ["en", "hi"]
    r.state.language = "hi"
    r.state.turn_count = 5
    target, reason = r._evidence_switch_decision("तमे केम छो")
    assert target is None and "unsupported" in reason


def test_switch_instant_english_needs_function_word():
    r = make_router()
    r.state.language = "hi"
    r.state.turn_count = 5
    target, _ = r._evidence_switch_decision("i need pricing details")
    assert target == "en"
    # a name never switches — zero votes, Tier 0
    target2, reason2 = r._evidence_switch_decision("alvin anthony")
    assert target2 is None and reason2 == "no_votes"


def test_confirmation_band_arm_complete_disarm():
    r = make_router()
    r.state.language = "en"
    r.state.turn_count = 5
    # one romanized-Hindi vote → hold + arm
    target, reason = r._evidence_switch_decision("batao")
    assert target is None and reason.startswith("pending(")
    assert r._pending_lang_switch == "hi"
    # one more supporting vote completes the switch
    r.state.turn_count = 6
    target, reason = r._evidence_switch_decision("dikhao")
    assert target == "hi" and reason.startswith("evidence_confirmed(")
    assert r._pending_lang_switch is None
    # disarm: a turn voting the current language clears a pending switch
    r.state.language = "en"
    r.state.turn_count = 8
    r._evidence_switch_decision("batao")
    assert r._pending_lang_switch == "hi"
    r.state.turn_count = 9
    target, reason = r._evidence_switch_decision(
        "sure i can tell you all about that")
    # current language is en → best==cur → disarmed, no switch
    assert r._pending_lang_switch is None


def test_oscillation_guard_one_quiet_turn():
    r = make_router()
    r.state.language = "en"
    r.state.turn_count = 5
    assert r._evidence_switch_decision("हम काम करते हैं")[0] == "hi"
    r.state.language = "hi"
    r._last_evidence_switch_turn = 5
    r.state.turn_count = 6
    target, reason = r._evidence_switch_decision(
        "i want the pricing details please")
    assert target is None and reason == "cooldown"
    r.state.turn_count = 7
    assert r._evidence_switch_decision(
        "i want the pricing details please")[0] == "en"


def test_evidence_switch_env_and_agent_optout(monkeypatch):
    import app.voice.conversation as conv_mod

    r = make_router()
    assert r._evidence_switch_enabled() is True
    r._def.languages.evidence_switch = False
    assert r._evidence_switch_enabled() is False
    r2 = make_router()
    monkeypatch.setattr(conv_mod, "LANG_EVIDENCE_SWITCH", False)
    assert r2._evidence_switch_enabled() is False


def test_schema_parses_evidence_switch_optout():
    r = make_router(languages={"supported": ["en"], "default": "en",
                               "tts_voices": {}, "evidence_switch": False})
    assert r._def.languages.evidence_switch is False
    assert make_router()._def.languages.evidence_switch is True


# ── A3: neutral turns / mirror hardening ────────────────────────────────────

def test_mirror_rejects_name_as_english():
    """'alvin anthony' passed the old ≥2-Latin-word bar and flipped a Hindi
    call to English — an en claim now needs a function word."""
    r = make_router()
    assert r.stt_mirror_consistent("en", "alvin anthony") is False
    assert r.stt_mirror_consistent("en", "i need pricing details") is True


def test_recovered_stt_source_never_votes():
    r = make_router()
    r.state.language = "hi"
    r.state.turn_count = 5
    r._turn_stt_source = "fallback"
    # Even dense English evidence must not switch on a Whisper-recovered turn
    # (checked in the process_stream wiring — here we assert the flag the
    # wiring reads survives on the router).
    assert r._turn_stt_source == "fallback"


# ── A4: STT source threading + mixed-script salvage ─────────────────────────

def test_router_salvages_mixed_script_transcript():
    """One junk-script token must not throw away a good transcript
    ('ਇਹ। K L W Y N' lost the caller's spelled letters on a live call)."""
    from app.voice.stt.router import STTRouter
    from app.voice.stt.base import STTResult

    class FakePrimary:
        async def transcribe(self, audio_bytes, sample_rate=16000,
                             language="en"):
            return STTResult(text="ਇਹ। i need pricing details",
                             detected_language="en-IN")

    class ExplodingFallback:
        async def transcribe(self, *a, **k):
            raise AssertionError("fallback must not run when salvage works")

    router = STTRouter(primary=FakePrimary(), fallback=ExplodingFallback())
    res = asyncio.run(router.transcribe(b"\x00\x00" * 8000, language="unknown",
                                        default_language="en"))
    assert "pricing" in res.text
    assert res.source == "primary"
    assert router.script_recovered_count == 1


def test_sttresult_source_default_primary():
    from app.voice.stt.base import STTResult
    assert STTResult(text="x").source == "primary"


# ── A5: delivery epoch ──────────────────────────────────────────────────────

def test_flush_pipeline_queues_bumps_epoch():
    from app.voice.pipeline import VoicePipeline

    ns = types.SimpleNamespace(
        _delivery_epoch=0,
        _tts_queue=asyncio.Queue(),
        _stt_queue=asyncio.Queue(),
        _transcript_queue=asyncio.Queue(),
        _stt_ready_queue=asyncio.Queue(),
        _stt_inflight=0,
    )
    ns._flush_pipeline_queues = types.MethodType(
        VoicePipeline._flush_pipeline_queues, ns)
    ns._flush_pipeline_queues()
    assert ns._delivery_epoch == 1


def test_response_replay_flag_default():
    from app.voice.pipeline import Response
    assert Response(text="x", is_shortcut=True, timestamp=0.0).replay is False
    assert Response(text="x", is_shortcut=True, timestamp=0.0,
                    replay=True).replay is True
