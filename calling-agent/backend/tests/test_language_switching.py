"""Follow the caller into their language — but only into languages this
agent was built for (2026-08-04).

Live call b6c5d398, Prakhar / Pramukh group, languages ["hi","en","gu"]:

    11:29:46 [LANG] "haan, baat kar sakte hain." -> hi (src=pattern, conf=1.00)
    11:29:46 [LANG] No switch (pending(votes=1/1,density=1.00), staying en)
    11:29:46 [LANG] Response language: en (detected=hi, prev=en)
    11:29:59 [LANG] "mujhe vapi mein property chahiye thi." -> hi
    11:29:59 [LANG] Evidence switch en → hi (evidence(votes=3/3,density=1.00))

The detector was right the FIRST time, at full confidence, with nothing in
the utterance voting for another language — and the two-vote bar held the
switch anyway, so a Hindi speaker was answered in English and had to repeat
himself before the agent followed.

Separately, nothing anywhere bounded the switcher to the agent's own
`languages.supported`: a call could be carried into a language with no
greeting, no canned responses and no TTS voice configured for it.
"""

from __future__ import annotations

import pytest

from app.voice.conversation import ConversationRouter
from test_call_termination import drive, make_router


@pytest.fixture(autouse=True)
def _offline(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


def lang_router(supported=("hi", "en", "gu"), default="en", **over):
    over.setdefault("languages", {"supported": list(supported),
                                  "default": default, "tts_voices": {}})
    r = make_router(**over)
    r.state.language = default
    r.state.turn_count = 4          # past the evidence-switch cooldown
    return r


def decide(router, text):
    return router._evidence_switch_decision(text)


# ── the live failure ────────────────────────────────────────────────────────

def test_unanimous_hindi_sentence_switches_on_the_first_turn():
    r = lang_router()
    target, reason = decide(r, "haan, baat kar sakte hain.")
    assert target == "hi", reason
    assert "unanimous" in reason or "evidence(" in reason


def test_the_second_hindi_sentence_still_switches():
    r = lang_router()
    assert decide(r, "mujhe vapi mein property chahiye thi.")[0] == "hi"


def test_the_caller_never_has_to_repeat_themselves():
    """End to end: one Hindi sentence, and the reply language is Hindi."""
    r = lang_router()
    drive(r, "haan, baat kar sakte hain.")
    assert r.state.language == "hi"


# ── bounded by the agent's own languages ────────────────────────────────────

def test_an_english_only_agent_is_never_carried_into_hindi():
    r = lang_router(supported=("en",), default="en")
    target, reason = decide(r, "mujhe vapi mein property chahiye thi.")
    assert target is None
    assert "unsupported" in reason


def test_a_hindi_english_agent_still_follows_hindi():
    r = lang_router(supported=("en", "hi"), default="en")
    assert decide(r, "mujhe vapi mein property chahiye thi.")[0] == "hi"


def test_switchable_set_is_exactly_the_agents_languages():
    """The schema already clamps `default` into `supported`, so the
    switchable set is simply what the business declared."""
    assert lang_router(supported=("hi", "en", "gu"))._switchable_languages() \
        == frozenset({"hi", "en", "gu"})
    assert lang_router(supported=("en",))._switchable_languages() \
        == frozenset({"en"})


def test_auto_switch_off_pins_the_agent_to_its_default():
    r = lang_router()
    r._def.languages.auto_switch = False
    target, reason = decide(r, "mujhe vapi mein property chahiye thi.")
    assert target is None and reason == "auto_switch_off"


def test_auto_switch_is_on_by_default():
    assert lang_router()._auto_switch_enabled() is True


# ── the guards that must survive ────────────────────────────────────────────

def test_a_single_word_does_not_flip_the_call():
    """Unanimous evidence still needs a real sentence behind it."""
    r = lang_router()
    assert decide(r, "mujhe")[0] is None


def test_bare_acknowledgements_never_vote():
    r = lang_router()
    for ack in ("haanji", "yes.", "okay", "hello", "theek"):
        assert decide(r, ack)[0] is None, ack


def test_an_english_sentence_does_not_switch_a_hindi_call():
    r = lang_router(default="hi")
    r.state.language = "hi"
    assert decide(r, "yes I want to know about the property")[0] == "en"


def test_a_name_does_not_read_as_an_english_switch():
    r = lang_router(default="hi")
    r.state.language = "hi"
    assert decide(r, "alvin anthony")[0] is None


def test_split_evidence_still_needs_confirmation():
    """Density below 1.0 keeps the old two-turn confirmation band — only
    UNANIMOUS evidence was promoted to a single turn."""
    r = lang_router()
    target, reason = decide(r, "mujhe kem")      # one hi vote, one gu vote
    assert target is None
    assert "pending" in reason


def test_an_english_utterance_on_an_english_call_is_a_no_op():
    r = lang_router()
    assert decide(r, "I want mujhe property") == (None, "current")


def test_the_cooldown_still_holds_after_a_switch():
    r = lang_router()
    r._last_evidence_switch_turn = r.state.turn_count
    assert decide(r, "mujhe vapi mein property chahiye thi.")[1] == "cooldown"


# ── explicit requests are bounded too ───────────────────────────────────────

def test_explicit_request_for_an_unsupported_language_is_refused():
    r = lang_router(supported=("en",), default="en",
                    response_bank={"opening_greeting": "hi",
                                   "lang_unsupported_responses": {
                                       "en": "Sorry, I can only help in English."}})
    r.state.language = "en"
    r.state.turn_count = 4
    resp = drive(r, "can you speak in hindi")
    assert "only help in English" in resp
    assert r.state.language == "en"


def test_explicit_request_for_a_supported_language_still_works():
    r = lang_router()
    drive(r, "can you speak in hindi")
    assert r.state.language == "hi"
