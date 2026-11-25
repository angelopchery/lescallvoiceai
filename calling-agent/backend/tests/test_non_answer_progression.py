"""A reply that is not an answer must not move the call on (RUNBOOK §69).

Call 1032 (2026-10-07, Prashant / Pramukh Aristo). Asked where he was calling
from, the caller said "आप कौन बात कर रहे हो?"; asked the purpose, "कौन था
सर?" (heard for "kaun sa, sir?"); asked the family size, "किसकी बात कर रहा
हूँ? कहाँ रहने की बात कर रहा हूँ?". Each slot had one ask, each reply spent
it, all three were skipped, and the agent answered "समझ गया" and asked the
next question every time.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice import prompt_blocks as pb
from app.voice.conversation import ConversationRouter

FIX = (Path(__file__).parent / "fixtures"
       / "pramukh_aristo_prashant_outbound_2026_10_07.yaml")


@pytest.fixture()
def router():
    r = ConversationRouter(agent_def=AgentDefinition.from_yaml(FIX),
                           default_language="hi")
    r.state.stage = "LET_THEM_TALK"
    return r


@pytest.mark.parametrize("text,kind", [
    ("आप कौन बात कर रहे हो?", "question"),
    ("कौन था सर?", "confused"),
    ("किसकी बात कर रहा हूँ? कहाँ रहने की बात कर रहा हूँ?", "confused"),
    ("samjha nahi, phir se boliye", "confused"),
    ("which project are you talking about", "confused"),
    ("हेलो", "hello"),
    ("Vapi से ही बोल रहा हूँ", ""),
    ("हम चार लोग हैं", ""),
    ("मैं अभी तो सोच ही नहीं रहा कुछ।", ""),
])
def test_non_answer_kind(router, text, kind):
    assert router._non_answer_kind(text) == kind


def _ask_then_reply(router, slot, reply):
    sm = router.state_mgr
    sm.note_asked(slot)                  # the agent asks (its one ask)
    kind = router._non_answer_kind(reply)
    sm.observe_user(reply, asked=slot, non_answer=bool(kind) or "?" in reply)
    sm.observe_user("…", asked=None)     # one more caller turn: settle point
    return sm


@pytest.mark.parametrize("slot,reply", [
    ("calling_from", "आप कौन बात कर रहे हो?"),
    ("purpose", "कौन था सर?"),
    ("family_size", "किसकी बात कर रहा हूँ? कहाँ रहने की बात कर रहा हूँ?"),
])
def test_a_question_back_does_not_skip_the_question(router, slot, reply):
    sm = _ask_then_reply(router, slot, reply)
    assert slot not in sm.skipped


def test_a_real_non_answer_still_settles(router):
    sm = _ask_then_reply(router, "purpose", "हाँ ठीक है")
    assert "purpose" in sm.skipped


def test_grace_is_bounded(router):
    sm = router.state_mgr
    for _ in range(3):
        sm.note_asked("purpose")
        sm.observe_user("कौन था सर?", asked="purpose", non_answer=True)
    sm.observe_user("…")
    sm.observe_user("…")
    assert "purpose" in sm.skipped


def test_band_tells_the_model_it_was_not_answered():
    q = "आप अभी कहाँ से बात कर रहे हैं — Vapi में ही हैं, या किसी और शहर से?"
    band = "\n".join(pb.last_question_block(q, non_answer="confused"))
    assert "did NOT answer" in band and "समझ गया" in band
    assert "move on to something else" not in band
    plain = "\n".join(pb.last_question_block(q))
    assert "never pretend you understood" in plain
