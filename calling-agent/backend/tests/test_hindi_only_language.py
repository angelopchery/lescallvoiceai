"""A Hindi-only agent never answers in English (RUNBOOK §73, 2026-10-12).

Call 1059 (Prashant / Pramukh Aristo, arisha@): asked "कितने BHK?", the
caller said "3 bhk" and the agent replied "How soon are you looking to
move…" in English. The agent was built Hindi-only (RUNBOOK §70: supported
[hi], evidence_switch false, auto_switch false) — which routed every turn
through the LEGACY switcher, the one path that never checked
languages.supported or languages.auto_switch:

    [LANG] "3 bhk" -> en (src=default, conf=0.50, locked=False)
    [LANG] Switching to English (ascii multi-word, src=default, words=2, min=2)

Two fixes, both platform-wide: every automatic switch is bounded by the
agent's build after BOTH branches, and digits / unit words ("3 bhk",
"90 lakh") never count as English. Offline: no LLM calls.
"""
from __future__ import annotations

import asyncio

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice import conversation as conv
from app.voice.conversation import ConversationRouter


@pytest.fixture(autouse=True)
def _offline(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


def lang_router(supported=("hi",), default="hi", evidence=False, auto=False):
    d = {
        "agent_id": "t_hi", "version": "1.0.0",
        "identity": {"agent_name": "Prashant", "company_name": "TestCo"},
        "languages": {"supported": list(supported), "default": default,
                      "tts_voices": {}, "evidence_switch": evidence,
                      "auto_switch": auto, "allow_code_mixing": True},
        "call_flow": {"stages": [
            {"id": "S1", "order": 0, "goal": "one", "turns_until_advance": 8},
            {"id": "S2", "order": 1, "goal": "two", "turns_until_advance": 99}]},
        "response_bank": {"opening_greeting": "hi"},
    }
    agent = AgentDefinition.from_dict(d)
    r = ConversationRouter(agent_def=agent, default_language=default)
    r.state.language = default
    r.state.turn_count = 4
    r.last_agent_response = "आप किस size का सोच रहे हैं — कितने BHK?"
    return r


def drive(r, text):
    async def _one():
        return [res async for res in r.process_stream(text)]
    return asyncio.run(_one())


def said(results) -> str:
    return " ".join(getattr(x, "text", "") or "" for x in results).strip()


# ── the live failure ────────────────────────────────────────────────────────

def test_3_bhk_does_not_flip_a_hindi_only_call():
    r = lang_router()
    drive(r, "3 bhk")
    assert r.state.language == "hi"


def test_a_full_english_sentence_cannot_leave_a_hindi_only_agent():
    r = lang_router()
    drive(r, "how soon are you looking to move into the new flat")
    assert r.state.language == "hi"


def test_the_legacy_switcher_still_follows_english_where_the_agent_speaks_it():
    r = lang_router(supported=("hi", "en"), evidence=False, auto=True)
    drive(r, "i would like to see the price list for the project please")
    assert r.state.language == "en"


def test_auto_switch_off_pins_the_legacy_switcher_too():
    r = lang_router(supported=("hi", "en"), evidence=False, auto=False)
    drive(r, "i would like to see the price list for the project please")
    assert r.state.language == "hi"


def test_a_language_seeded_from_outside_the_build_is_pinned_back():
    r = lang_router()
    r.state.language = "en"                  # a pre-§73 mirror / seed
    drive(r, "ठीक है")
    assert r.state.language == "hi"


# ── what counts as English ──────────────────────────────────────────────────

@pytest.mark.parametrize("text,count", [
    ("3 bhk", 0), ("90 lakh", 0), ("2 bhk ready possession", 0),
    ("1200 sq ft", 0), ("ok sir", 0), ("i want to buy a house", 6),
])
def test_digits_and_unit_words_never_count_as_english(text, count):
    assert conv._language_content_words(text) == count


def test_legacy_switch_with_only_unit_words_is_suppressed_even_for_bilingual_agents():
    r = lang_router(supported=("hi", "en"), evidence=False, auto=True)
    drive(r, "3 bhk")
    assert r.state.language == "hi"


# ── the other doors ─────────────────────────────────────────────────────────

def test_pipeline_mirror_is_bounded_by_the_same_set():
    r = lang_router()
    assert r.language_allowed("hi") and not r.language_allowed("en")
    assert not r.language_allowed("en-IN")
    r2 = lang_router(supported=("hi", "en", "gu"))
    assert r2.language_allowed("en") and r2.language_allowed("gu")


def test_an_explicit_request_for_english_is_refused_on_a_hindi_only_agent():
    r = lang_router()
    drive(r, "please talk in english")
    assert r.state.language == "hi"


def test_gratitude_is_answered_in_the_calls_language():
    r = lang_router()
    out = said(drive(r, "thank you"))
    assert "You're welcome" not in out and "Happy to help" not in out
    assert any("ऀ" <= ch <= "ॿ" for ch in out), out


def test_gratitude_defaults_exist_for_every_language():
    assert set(conv._GRATITUDE_DEFAULTS) == {"en", "hi", "gu"}
