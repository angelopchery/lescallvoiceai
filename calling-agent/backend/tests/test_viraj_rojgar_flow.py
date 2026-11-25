"""Viraj — भारत रोजगार सेवा follow-up caller (2026-09-24, RUNBOOK §56).

Call 934, the agent's first test call: the tool recorded got_job = "नहीं" on
turn 1 and the flow still asked the caller which company he had joined, his
designation, the city, whether he had joined, his joining date and whether
the offer letter had come — six questions for a man who had said he was
unemployed. Then "is there anything else I can help with?" six times and
"shall I consider the call ended?" three times.

Engine mechanism under test: a CHECK_STATUS branch on the VALUE of got_job,
whose `retire_slots:` action retires the other half's questions for this
caller — they leave `missing`, count as satisfied, read "not applicable" in
the record and refuse later writes. Plus the two closing-turn fixes the same
call exposed: an affirmation wrapped in politeness is still a yes, and seven
polite Latin tokens are not evidence that the call switched to English.

Frozen snapshot only — never the live YAML (owner-editable, gitignored).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.agent_definition.schema import AgentDefinition
from app.agent_definition.preflight import preflight_agent
from app.agent_definition.spoken_surfaces import resolve_spoken_surfaces
from app.voice.conversation import ConversationRouter, _language_content_words

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "viraj_outbound_2026_09_24.yaml"

PLACED = ["job_source", "company_name", "designation", "job_location",
          "joining_status", "joining_date", "offer_letter"]
SEEKER = ["company_contacted", "current_status", "still_searching", "preferred_role",
          "qualification", "expected_salary", "preferred_location",
          "relocate_outside_lucknow"]

SARKARI = ("अभ्यर्थी", "पद ", "योग्यता", "अपेक्षित", "नियुक्ति", "माध्यम")


def _agent() -> AgentDefinition:
    return AgentDefinition.from_yaml(FIXTURE)


def _router(stage: str = "CHECK_STATUS") -> ConversationRouter:
    r = ConversationRouter(agent_def=_agent(), default_language="hi")
    r.set_call_context(direction="outbound", contact_name="Alwyn",
                       contact_phone="917042340227")
    r.state.stage = stage
    return r


# ---------------------------------------------------------------------------
# 1. The value of got_job decides the flow, and retires the other half
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("answer", ["नहीं", "अभी ढूंढ रहा हूँ", "interview de raha hoon",
                                    "No", "बेरोजगार हूँ"])
def test_no_job_branches_to_not_employed_and_retires_the_placed_questions(answer):
    r = _router()
    r.state_mgr.merge_state({"got_job": answer}, source="tool")
    assert r.state_mgr.state["got_job"] == "No"
    assert r._maybe_branch("") is True
    assert r.state.stage == "NOT_EMPLOYED"
    gaps = r.state_mgr.detect_missing_slots()
    assert not set(PLACED) & set(gaps["missing"])
    assert set(PLACED) <= r.state_mgr.retired
    # The seeker questions are what is asked next, company_contacted first.
    assert gaps["missing"][0] == "company_contacted"


@pytest.mark.parametrize("answer", ["हाँ", "मिल गई", "Yes", "job lag gayi", "joined"])
def test_got_job_branches_to_job_details_and_retires_the_seeker_questions(answer):
    r = _router()
    r.state_mgr.merge_state({"got_job": answer}, source="tool")
    assert r.state_mgr.state["got_job"] == "Yes"
    assert r._maybe_branch("") is True
    assert r.state.stage == "JOB_DETAILS"
    gaps = r.state_mgr.detect_missing_slots()
    assert not set(SEEKER) & set(gaps["missing"])
    assert set(SEEKER) <= r.state_mgr.retired
    assert gaps["missing"][0] == "job_source"


def test_a_retired_slot_refuses_the_write_call_934_stored():
    """company_name='होना है private limited' for an unemployed caller."""
    r = _router()
    r.state_mgr.merge_state({"got_job": "नहीं"}, source="tool")
    r._maybe_branch("")
    r.state_mgr.merge_state({"company_name": "होना है private limited",
                             "designation": "डेटा साइंस",
                             "joining_status": "अभी बाकी है"}, source="tool")
    for n in ("company_name", "designation", "joining_status"):
        assert r.state_mgr.state.get(n) is None
    rejected = [a for a in r.state_mgr.audit_log if "retired" in (a.utterance or "")]
    assert len(rejected) >= 3


def test_the_record_says_not_applicable_never_not_yet_discussed():
    r = _router()
    r.state_mgr.merge_state({"got_job": "नहीं"}, source="tool")
    r._maybe_branch("")
    summary = r.state_mgr.running_summary()
    assert "not applicable" in summary
    assert "Company Name not yet discussed" not in summary
    d = r.state_mgr.to_dict()
    assert set(PLACED) <= set(d["retired"])
    assert not set(PLACED) & set(d["missing"])


def test_the_prompt_never_lists_a_retired_question_as_a_later_part_of_the_call():
    r = _router()
    r.state_mgr.merge_state({"got_job": "नहीं"}, source="tool")
    r._maybe_branch("")
    prompt = r._build_system_prompt()
    state = prompt.split("CONVERSATION STATE", 1)[1].split("━━ RIGHT NOW", 1)[0]
    later = next((l for l in state.splitlines() if "LATER part of the call" in l), "")
    for label in ("Company they've been hired by", "Their work or role at the company",
                  "Already joined, or joining still pending",
                  "Offer or appointment letter received (Yes/No)"):
        assert f"✗ {label}" not in state, label          # not an open fact
        assert label not in later, label                 # not a later question
        assert f"{label}: not applicable" in state, label  # the record says why
    assert "company ने आपसे contact" in prompt


def test_not_employed_escapes_to_followup_offer_never_into_job_details():
    r = _router()
    r.state_mgr.merge_state({"got_job": "नहीं"}, source="tool")
    r._maybe_branch("")
    r.state_mgr.merge_state({"company_contacted": "नहीं", "still_searching": "हाँ",
                             "preferred_role": "DevOps", "qualification": "BSc",
                             "expected_salary": "20000", "preferred_location": "Delhi",
                             "relocate_outside_lucknow": "हाँ"}, source="tool")
    r.state.discovery_turns = 1
    r._maybe_advance_stage()
    assert r.state.stage == "FOLLOWUP_OFFER"


def test_the_branch_fires_once_and_a_second_answer_does_not_reroute():
    r = _router()
    r.state_mgr.merge_state({"got_job": "नहीं"}, source="tool")
    assert r._maybe_branch("") is True
    r.state.stage = "NOT_EMPLOYED"
    assert r._maybe_branch("") is False   # NOT_EMPLOYED has no branches


# ---------------------------------------------------------------------------
# 2. The spoken surfaces: two short beats, everyday Hindi, no sarkari words
# ---------------------------------------------------------------------------

def test_preflight_renders_every_stage_in_both_languages():
    assert preflight_agent(_agent(), languages=["hi", "en"]) == "ok"


def test_two_beat_opening_resolves_with_the_real_engine():
    s = resolve_spoken_surfaces(_agent(), "outbound")
    hi = s["openings"]["hi"]
    assert hi["line"] == "हेलो?"
    assert "{contact_name}" in hi["second_line"] and "office" not in hi["second_line"]
    assert s["shadowed"] == []
    r = _router("WARM_WELCOME")
    assert r.connect_intro_line() == "नमस्ते! क्या मेरी बात Alwyn जी से हो रही है?"


def test_no_sarkari_hindi_in_any_spoken_slot_question():
    a = _agent()
    for slot in a.slots:
        q = slot.question.get("hi", "")
        for w in SARKARI:
            assert w not in q, f"{slot.name}: {w!r} in {q!r}"


def test_no_helpdesk_reflexes_in_the_closing_goal():
    a = _agent()
    closing = next(s for s in a.call_flow.stages if s.id == "CLOSING")
    assert "never ask whether there's anything else" in closing.goal
    assert "never ask whether you may end the call" in closing.goal


def test_the_placed_and_seeker_halves_never_share_a_stage():
    a = _agent()
    by_id = {s.id: s for s in a.call_flow.stages}
    assert set(by_id["JOB_DETAILS"].collect) == set(PLACED)
    assert set(by_id["NOT_EMPLOYED"].collect) == set(SEEKER) - {"current_status"}
    assert by_id["NOT_EMPLOYED"].advance_when.get("goto") == "FOLLOWUP_OFFER"


# ---------------------------------------------------------------------------
# 3. Closing-turn fixes the same call exposed (platform, every agent)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "ji ji ji okay sir thank you",     # the live answer
    "haan ji thank you sir",
    "okay okay thank you",
    "जी हाँ, धन्यवाद सर",
    "yes sir",
])
def test_an_affirmation_wrapped_in_politeness_is_consent_to_close(text):
    assert _router()._is_close_confirm_yes(text) is True


@pytest.mark.parametrize("text", [
    "thank you",                          # politeness alone is not an answer
    "okay thank you but one question",
    "no thank you",
    "thank you sir, ek sawal hai",
])
def test_politeness_without_an_affirmation_keeps_the_call_alive(text):
    assert _router()._is_close_confirm_yes(text) is False


def test_polite_latin_tokens_are_not_english_evidence():
    assert _language_content_words("ji ji ji okay sir thank you") == 0
    assert _language_content_words("okay sir") == 0
    assert _language_content_words("yes we can talk in english") == 5
    assert _language_content_words("delhi") == 1


def test_the_call_row_language_is_the_dominant_one_not_the_last_flip():
    r = _router()
    for i in range(6):
        r.memory.add_user(f"हाँ जी {i}", language="hi")
    r.memory.add_user("ji ji ji okay sir thank you", language="en")
    r.state.language = "en"
    assert r.dominant_language() == "hi"
    assert r._closing_language() == "hi"     # policy: dominant


def test_retire_slots_is_idempotent_and_ignores_unknown_names():
    r = _router()
    first = r.state_mgr.retire_slots(["company_name", "no_such_slot", ""], reason="t")
    assert first == ["company_name"]
    assert r.state_mgr.retire_slots(["company_name"]) == []
    assert "no_such_slot" not in r.state_mgr.retired
