"""Caller identity is not revised by mishearing (2026-08-06).

Regression cover for the live call where an outbound contact known as
"Harshal" was addressed as "Ravi" for the rest of the call and hung up.

Chain that produced it:
  * ASR returned "हाँ मैं रवि रेडी हूँ" — re-transcribing the original audio
    four ways (hi-IN, auto, mr-IN, en-IN) reproduces "रवि" every time, so this
    is not a tunable ASR defect;
  * the agent's own `customer_name` COLLECT slot had never been seeded from
    the contacts row, so it counted as MISSING and the background extractor
    went looking for a name;
  * it read "मैं रवि" as "I am Ravi" and wrote it (source="llm", old=None);
  * build_prompt_context then re-published it as "✓ ground truth — never
    re-ask", and the agent switched names mid-call.

The fix seeds identity slots from the known contact before the greeting and
locks them: only an answer to a name question the agent asked, or an explicit
request to be called something else, may change them.
"""

from __future__ import annotations

import pytest

from app.voice.state_manager import (
    ConversationStateManager, SlotSpec, is_identity_slot, wants_name_change,
)


def _mgr(*slot_names: str) -> ConversationStateManager:
    return ConversationStateManager(
        [SlotSpec(name=n, priority=i + 1) for i, n in enumerate(slot_names)])


# --------------------------------------------------------------- slot naming

@pytest.mark.parametrize("name", [
    "name", "customer_name", "contact_name", "caller_name", "full_name",
    "client_name", "lead_name", "customer_full_name", "your_name",
])
def test_identity_slots_are_recognised(name):
    """Existing agents and anything the Copilot writes later are covered by
    the canonical name — no new YAML flag to remember."""
    assert is_identity_slot(name)


@pytest.mark.parametrize("name", [
    "company_name", "project_name", "policy_name", "product_name", "city",
    "insurance_type", "nominee_relation", "brand_name",
])
def test_non_identity_slots_are_untouched(name):
    """Deliberately narrow: a 'name' that is not the CALLER's must keep
    behaving exactly as it does today."""
    assert not is_identity_slot(name)


# ------------------------------------------------------------ the live bug

def test_misheard_name_cannot_replace_a_known_contact():
    """The exact utterance from the call, through the exact writer."""
    m = _mgr("customer_name", "city")
    m.seed_identity("Harshal")

    m.merge_state({"customer_name": "Ravi"}, source="llm",
                  utterance="हाँ मैं रवि रेडी हूँ अब बात कर सकते हो")

    assert m.state["customer_name"] == "Harshal"
    assert any("REJECTED" in (e.utterance or "") for e in m.audit_log)


def test_seeding_removes_the_name_from_missing():
    """The extractor only ever targets missing slots — this is the first and
    cheapest wall, before the lock is even consulted."""
    m = _mgr("customer_name", "city")
    m.seed_identity("Harshal")
    assert "customer_name" not in m.detect_missing_slots()["missing"]
    assert "city" in m.detect_missing_slots()["missing"]


def test_regex_extraction_is_blocked_too():
    m = _mgr("customer_name")
    m.seed_identity("Harshal")
    m.merge_state({"customer_name": "Ravi"}, source="regex",
                  utterance="मैं रवि रेडी हूँ")
    assert m.state["customer_name"] == "Harshal"


def test_prompt_tells_the_model_not_to_switch_names():
    """merge_state refusing to STORE the name is not enough — the model will
    still address the caller by whatever it inferred this turn."""
    m = _mgr("customer_name")
    m.seed_identity("Harshal")
    ctx = m.build_prompt_context()
    assert "Harshal" in ctx
    assert "explicitly" in ctx.lower()


# ------------------------------------------------- explicit change still works

@pytest.mark.parametrize("utterance", [
    "actually my name is Ravi",
    "no, my name is not Harshal",
    "sorry, the name is wrong",
    "please call me Ravi",
    "mera naam Ravi hai",
    "मेरा नाम रवि है",
    "મારું નામ રવિ છે",
])
def test_explicit_change_requests_are_recognised(utterance):
    assert wants_name_change(utterance)


@pytest.mark.parametrize("utterance", [
    "हाँ मैं रवि रेडी हूँ अब बात कर सकते हो",   # the live mis-hearing
    "yes I am ready now",
    "haan bilkul, boliye",
    "I am interested in health insurance",
    "मैं चंडीगढ़ में रहता हूँ",
])
def test_ordinary_speech_does_not_unlock_the_name(utterance):
    assert not wants_name_change(utterance)


def test_caller_may_still_correct_their_name():
    m = _mgr("customer_name")
    m.seed_identity("Harshal")
    m.merge_state({"customer_name": "Ravi"}, source="llm",
                  utterance="actually my name is Ravi, not Harshal")
    assert m.state["customer_name"] == "Ravi"


def test_answering_a_name_question_still_works():
    """When the agent actually asks, the caller's answer is authoritative."""
    m = _mgr("customer_name")
    m.seed_identity("Harshal")
    m.merge_state({"customer_name": "Ravi"}, source="answer",
                  utterance="Ravi")
    assert m.state["customer_name"] == "Ravi"


def test_confirmation_path_still_writes_through():
    m = _mgr("customer_name")
    m.seed_identity("Harshal")
    m.set_externally("customer_name", "Ravi", source="system")
    assert m.state["customer_name"] == "Ravi"


# ------------------------------------------------- unknown callers unchanged

def test_unknown_caller_behaviour_is_unchanged():
    """Nothing is locked that was never known — inbound strangers keep
    today's capture behaviour exactly."""
    m = _mgr("customer_name")
    assert m.identity_locked == set()
    m.merge_state({"customer_name": "Ravi"}, source="llm",
                  utterance="मैं रवि बोल रहा हूँ")
    assert m.state["customer_name"] == "Ravi"


def test_seed_never_overwrites_something_the_caller_already_said():
    m = _mgr("customer_name")
    m.merge_state({"customer_name": "Rajesh"}, source="answer", utterance="Rajesh")
    m.seed_identity("Harshal")
    assert m.state["customer_name"] == "Rajesh"


def test_agent_without_a_name_slot_is_a_no_op():
    m = _mgr("city", "insurance_type")
    assert m.seed_identity("Harshal") == []
    assert m.identity_locked == set()


def test_blank_seed_is_ignored():
    m = _mgr("customer_name")
    assert m.seed_identity("   ") == []
    assert m.identity_locked == set()
    assert m.state["customer_name"] is None
