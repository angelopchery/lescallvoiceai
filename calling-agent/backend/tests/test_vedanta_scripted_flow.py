"""The owner's scripted sales flow for the Vedanta test agent (RUNBOOK §75,
2026-10-12) — the four engine capabilities it needed, all opt-in:

  * `ask_gated` slots are written only as the answer to their own question
    (or on an explicit correction) — call 1052 flipped decision_factor six
    times from questions about the project ("लोकेशन बता सकते हो");
  * a `when_intent: no_more_questions` branch moves the flow on (to the
    invitation) when the caller says "no, nothing else" to the stage's
    "any other question?" — instead of closing the call;
  * `termination.pre_close_question` is asked before every goodbye and its
    "और किसी चीज़ में मदद" wording counts as a close permission, so the
    caller's "नहीं" ends the call on `closing_responses` with
    `confirm_before_end: false`;
  * a budget-fit alternative must offer the caller's configuration.

Offline: no LLM calls (stub_llm).
"""
from __future__ import annotations

import asyncio
import types

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice import pitch_switch as ps
from app.voice import prompt_blocks as pb
from app.voice.conversation import ConversationRouter


@pytest.fixture(autouse=True)
def _offline(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


Q_DOUBTS = ("क्या आपके पास इस property के बारे में और कोई सवाल या doubt है, "
            "जिसमें मैं आपकी मदद कर सकता हूँ?")
L_PRECLOSE = ("ठीक है जी, धन्यवाद। जाने से पहले क्या मैं आपकी और किसी चीज़ में "
              "मदद कर सकता हूँ?")
L_BYE = "Thank you, आपका दिन शुभ हो।"


def _slot(name, prio, enum, syn, ask_gated=False, policy="free"):
    return {"name": name, "priority": prio, "required": True, "label": name,
            "question": {"hi": f"{name}?"}, "type": "enum",
            "enum_values": enum, "enum_synonyms": syn, "extract_patterns": [],
            "depends_on": [], "merge_policy": policy, "confirm_readback": "",
            "max_asks": 2, "reask_question": {}, "summary_known": "",
            "summary_unknown": "", "ask_gated": ask_gated}


def make_router(stages, slots=(), termination=None, closing=None,
                stage=None):
    d = {
        "agent_id": "t_vedanta", "version": "1.0.0",
        "identity": {"agent_name": "Ravi", "company_name": "Pramukh Vedanta"},
        "languages": {"supported": ["hi"], "default": "hi", "tts_voices": {},
                      "evidence_switch": False, "auto_switch": False},
        "call_flow": {"stages": stages,
                      "termination": termination or {}},
        "slots": list(slots),
        "response_bank": {"opening_greeting": "hi",
                          "closing_responses": closing or {}},
    }
    agent = AgentDefinition.from_dict(d)
    r = ConversationRouter(agent_def=agent, default_language="hi")
    r.state.language = "hi"
    r.state.turn_count = 4
    if stage:
        r.state.stage = stage
    return r


def drive(r, text):
    async def _one():
        return [res async for res in r.process_stream(text)]
    return asyncio.run(_one())


def said(results) -> str:
    return " ".join(getattr(x, "text", "") or "" for x in results).strip()


# ── ask-gated slots ─────────────────────────────────────────────────────────

FACTOR_SYN = {"Location": ["location", "लोकेशन"],
              "Amenities": ["amenities", "एमेनिटीज़"]}
BHK_SYN = {"3 BHK": ["3bhk", "3 bhk"], "4 BHK": ["4bhk", "4 bhk"]}


def gated_router():
    stages = [{"id": "S1", "order": 0, "goal": "one",
               "collect": ["configuration_required", "decision_factor"],
               "advance_when": {"all_collect_filled": True, "max_turns": 9},
               "turns_until_advance": 9}]
    slots = [_slot("configuration_required", 1, ["3 BHK", "4 BHK"], BHK_SYN),
             _slot("decision_factor", 2,
                   ["Location", "Amenities"], FACTOR_SYN, ask_gated=True)]
    return make_router(stages, slots)


def test_a_question_about_the_project_never_fills_an_ask_gated_slot():
    r = gated_router()
    sm = r.state_mgr
    sm.observe_user("उससे पहले आप मेरे को लोकेशन बता सकते हो Vedanta की?")
    assert sm.state.get("decision_factor") is None
    # ...while an un-gated slot keeps today's behaviour: volunteered = taken.
    sm.observe_user("हमें 3 bhk चाहिए, लोकेशन बताओ")
    assert sm.state["configuration_required"] == "3 BHK"
    assert sm.state.get("decision_factor") is None


def test_the_answer_to_its_own_question_is_taken_and_then_held():
    r = gated_router()
    sm = r.state_mgr
    sm.observe_user("लोकेशन", asked="decision_factor")
    assert sm.state["decision_factor"] == "Location"
    # A later QUESTION about amenities is not a change of mind (call 1052).
    sm.observe_user("थोड़ा एमेनिटीज़ के बारे में बता सकते हो?")
    assert sm.state["decision_factor"] == "Location"
    # An explicit correction is.
    sm.observe_user("नहीं, मेरे लिए amenities ज़्यादा ज़रूरी हैं")
    assert sm.state["decision_factor"] == "Amenities"


def test_the_llm_gap_filler_cannot_write_an_ask_gated_slot_unasked():
    r = gated_router()
    sm = r.state_mgr
    sm.observe_user("मैं निवेश के लिए देख रहा था")      # turn 1, not asked
    sm.merge_state({"decision_factor": "Location"}, source="llm",
                   utterance="मैं निवेश के लिए देख रहा था", fill_only=True)
    assert sm.state.get("decision_factor") is None
    # The slot the agent actually asked ("answer" source) always lands.
    sm.merge_state({"decision_factor": "Amenities"}, source="answer",
                   utterance="amenities")
    assert sm.state["decision_factor"] == "Amenities"


def test_the_directive_of_the_previous_turn_counts_as_being_asked():
    """The prompt directive ("Ask ONLY ONE question next, about: X") notes
    the ask; the caller's next turn answers it — no utterance hint needed."""
    r = gated_router()
    sm = r.state_mgr
    sm.observe_user("4 bhk")                 # turn 1
    sm.note_asked("decision_factor")         # the directive asked it
    sm.observe_user("location")              # turn 2: the answer
    assert sm.state["decision_factor"] == "Location"


def test_the_next_question_is_not_the_current_one():
    """Becoming the head of the missing list is not being asked: the size
    arriving must not turn a 'location बताओ' in the same breath into the
    deciding factor."""
    r = gated_router()
    sm = r.state_mgr
    sm.observe_user("हमें 3 bhk चाहिए, लोकेशन बताओ")
    assert sm.state["configuration_required"] == "3 BHK"
    assert sm.state.get("decision_factor") is None
    sm.observe_user("location")              # still not asked → not taken
    assert sm.state.get("decision_factor") is None


# ── "no, nothing else" moves on instead of closing ─────────────────────────

def flow_stages(with_branch=True):
    br = ([{"when_intent": "no_more_questions", "goto": "INVITE",
            "max_takes": 1, "actions": []}] if with_branch else [])
    return [
        {"id": "KNOW_MORE", "order": 0, "role": "recommend", "goal": "q&a",
         "collect": [], "advance_when": {"max_turns": 10},
         "turns_until_advance": 10, "branches": br},
        {"id": "INVITE", "order": 1, "role": "scheduling", "goal": "invite",
         "collect": [], "advance_when": {"appointment_captured": True,
                                         "max_turns": 4},
         "turns_until_advance": 4},
        {"id": "CLOSE", "order": 2, "role": "confirmation", "goal": "close",
         "collect": [], "advance_when": {}, "turns_until_advance": 99},
    ]


def test_done_reply_takes_the_branch_and_the_call_stays_open():
    r = make_router(flow_stages(), stage="KNOW_MORE")
    r.last_agent_response = Q_DOUBTS
    drive(r, "नहीं, बस इतना ही")
    assert r.state.stage == "INVITE"
    assert r.hangup_pending is False
    assert r.state.is_closing is False
    assert r._close_confirm_pending is False


def test_without_the_branch_the_same_reply_closes_as_before():
    r = make_router(flow_stages(with_branch=False), stage="KNOW_MORE")
    r.last_agent_response = Q_DOUBTS
    drive(r, "नहीं, बस इतना ही")
    assert r.state.stage == "KNOW_MORE"
    assert r._close_confirm_pending is True        # the close-confirm gate


def test_a_real_question_is_not_a_done_reply():
    r = make_router(flow_stages(), stage="KNOW_MORE")
    r.last_agent_response = Q_DOUBTS
    drive(r, "नहीं, लेकिन parking कितनी मिलेगी?")
    assert r.state.stage == "KNOW_MORE"


def test_done_reply_one_line_after_the_doubts_question_still_branches():
    """An OBJECTION HANDLING answer the model closed with its own question
    came between the doubts question and the caller's "नहीं, बस इतना ही"
    (sim 2026-10-12) — that "no" is still the stage's done-reply."""
    r = make_router(flow_stages(), stage="KNOW_MORE")
    r.memory.add_assistant(Q_DOUBTS, language="hi")
    own_q = "क्या आप available option की complete cost sheet देखना चाहेंगे?"
    r.memory.add_assistant(own_q, language="hi")
    r.last_agent_response = own_q
    drive(r, "नहीं, बस इतना ही")
    assert r.state.stage == "INVITE"
    assert r.hangup_pending is False


def test_a_tool_booking_advances_the_waiting_stage_on_the_next_turn():
    """book_appointment writes CallState after the reply; the stage gated on
    appointment_captured must move on the very next caller turn, so that
    turn's reply is the read-back, not a second invitation."""
    r = make_router(flow_stages(), stage="INVITE")
    cs = r.memory.call_state
    cs.appointment_date, cs.appointment_time = "2026-10-17", "17:00"
    drive(r, "हाँ ठीक है")
    assert r.state.stage == "CLOSE"
    assert r.memory.booking.appointment_time == "17:00"


def test_the_branch_fires_once():
    r = make_router(flow_stages(), stage="KNOW_MORE")
    r.last_agent_response = Q_DOUBTS
    drive(r, "नहीं बस")
    assert r.state.stage == "INVITE"
    r.state.stage = "KNOW_MORE"
    r.last_agent_response = Q_DOUBTS
    drive(r, "नहीं बस")
    assert r.state.stage == "KNOW_MORE"
    assert r._close_confirm_pending is True


# ── the pre-close question and the two-step close ──────────────────────────

def close_router():
    stages = [{"id": "CLOSE", "order": 0, "role": "confirmation",
               "goal": "close", "collect": [], "advance_when": {},
               "turns_until_advance": 99}]
    return make_router(stages, termination={
        "confirm_before_end": False,
        "pre_close_question": {"hi": L_PRECLOSE, "en": "Anything else?"}},
        closing={"hi": L_BYE, "en": "Thank you, have a good day."},
        stage="CLOSE")


def test_the_owners_pre_close_wording_is_a_close_permission():
    rx = ConversationRouter._CLOSE_PERMISSION_RE
    assert rx.search(L_PRECLOSE)
    assert rx.search("kya main aapki aur kisi cheez mein madad kar sakta hoon?")
    # ...and the model's own paraphrase of it (sim 2026-10-12).
    assert rx.search("क्या इस सिलसिले में किसी और चीज़ में मेरी मदद चाहिए?")
    # ...but "some other day?" is a scheduling question, not a close.
    assert not rx.search("क्या आप और किसी दिन आ सकते हैं?")


def test_no_to_the_pre_close_question_ends_on_the_closing_line():
    r = close_router()
    r.last_agent_response = L_PRECLOSE
    out = said(drive(r, "नहीं"))
    assert r.hangup_pending is True
    assert r.state.is_closing is True
    assert "आपका दिन शुभ हो" in out
    assert "कॉल कट" not in out                 # no "shall I cut the call?"


def test_pre_close_question_resolves_per_language_and_is_empty_elsewhere():
    r = close_router()
    assert r._pre_close_question() == L_PRECLOSE
    assert r._close_question_asked() is False
    r.memory.add_assistant(L_PRECLOSE, language="hi")
    assert r._close_question_asked() is True
    plain = make_router(flow_stages())
    assert plain._pre_close_question() == ""


def test_visit_band_asks_the_pre_close_line_before_any_goodbye():
    lines = ["ठीक है, Saturday 17 October को शाम 5 बजे confirm।"]
    band = "\n".join(pb.visit_confirmed_block(
        "2026-10-17", "17:00", lines, close_question=L_PRECLOSE))
    assert "VISIT ALREADY CONFIRMED" in band
    assert L_PRECLOSE in band
    assert "[END_CALL]" not in band
    # Once it has been asked, the normal goodbye bullet is back.
    band2 = "\n".join(pb.visit_confirmed_block(
        "2026-10-17", "17:00", lines, close_question=L_PRECLOSE,
        close_asked=True))
    assert L_PRECLOSE not in band2 and "[END_CALL]" in band2
    # No pre-close line (every other agent): byte-identical to before.
    assert pb.visit_confirmed_block("2026-10-17", "17:00", lines) == \
        pb.visit_confirmed_block("2026-10-17", "17:00", lines,
                                 close_question="")


def test_with_a_pre_close_line_only_that_line_closes_the_call():
    """The doubts loop's "और कोई सवाल?" is conversation; a "नहीं बस" to it
    must not hang up (confirm_before_end is off on this agent)."""
    r = close_router()
    r.last_agent_response = Q_DOUBTS
    drive(r, "नहीं बस")
    assert r.hangup_pending is False and r.state.is_closing is False
    # The model's paraphrase of the pre-close line still counts.
    r.last_agent_response = "ठीक है जी। जाने से पहले, क्या मैं आपकी और किसी चीज़ में मदद कर सकता हूँ?"
    drive(r, "नहीं बस")
    assert r.hangup_pending is True


# ── the scripted stage's floor: ask_verbatim / after_answer ────────────────

Q_BHK = "Sure Sir. आप specifically 3 BHK consider कर रहे हैं या 4 BHK?"


def scripted_router(script, stage_id="S1", collect=("configuration_required",)):
    stages = [{"id": stage_id, "order": 0, "role": "discovery", "goal": "g",
               "collect": list(collect),
               "advance_when": {"all_collect_filled": True, "max_turns": 9},
               "turns_until_advance": 9, "script": script},
              {"id": "S2", "order": 1, "role": "scheduling", "goal": "g2",
               "collect": [], "advance_when": {"max_turns": 9},
               "turns_until_advance": 9}]
    slot = _slot("configuration_required", 1, ["3 BHK", "4 BHK"], BHK_SYN)
    slot["question"] = {"hi": Q_BHK, "en": "3 or 4 BHK?"}
    return make_router(stages, [slot], stage=stage_id)


def test_ask_verbatim_speaks_the_question_the_model_left_out(stub_llm):
    r = scripted_router({"ask_verbatim": True})
    out = said(drive(r, "investment के लिए"))
    assert stub_llm in out
    assert "specifically 3 BHK consider" in out      # appended, word for word
    assert r.last_agent_response.endswith("4 BHK?")  # part of the stored line


def test_ask_verbatim_is_silent_when_the_reply_asked_it():
    r = scripted_router({"ask_verbatim": True})
    r.state_mgr.note_asked("configuration_required")
    paraphrase = "अच्छा Angelo, आप specifically 3 BHK देख रहे हैं या 4 BHK?"
    assert r._script_followup(paraphrase) == ""
    # ...and when the slot is already filled, nothing is asked.
    r.state_mgr.set_externally("configuration_required", "3 BHK")
    assert r._script_followup("ठीक है।") == ""


def test_a_stage_without_a_script_appends_nothing(stub_llm):
    r = scripted_router({})
    out = said(drive(r, "investment के लिए"))
    assert out.strip() == stub_llm.strip()


def test_after_answer_line_follows_every_reply_unless_already_asked():
    line = {"hi": Q_DOUBTS}
    r = scripted_router({"after_answer": line, "unless": ["क्या आप Pramukh "
                         "Vedanta के बारे में थोड़ा और जानना चाहेंगे?"]},
                        collect=())
    assert r._script_followup("floor की height 10 से 11 फ़ीट है।") == Q_DOUBTS
    assert r._script_followup("…फ़ीट है। " + Q_DOUBTS) == ""
    assert r._script_followup("ठीक है। कोई और सवाल हो तो बताइए?") == ""   # permission
    assert r._script_followup("क्या आप Pramukh Vedanta के बारे में थोड़ा और "
                              "जानना चाहेंगे, या किसी और project में?") == ""


def test_the_floor_never_speaks_after_a_goodbye(stub_llm):
    r = scripted_router({"after_answer": {"hi": Q_DOUBTS}}, collect=())
    r.state.is_closing = True
    out = said(drive(r, "ठीक है"))
    assert Q_DOUBTS not in out


# ── a budget-fit alternative must offer the caller's BHK ───────────────────

def _kb():
    return types.SimpleNamespace(catalog=[
        {"id": "a", "name": "Pramukh A", "configurations": ["3 BHK", "4 BHK"],
         "budget_lakhs": [90, 100]},
        {"id": "b", "name": "Pramukh B", "configurations": ["1.5 BHK", "2 BHK"],
         "budget_lakhs": [90, 100]},
        {"id": "c", "name": "Pramukh C", "budget_lakhs": [90, 100]},
        {"id": "d", "name": "Pramukh D", "configurations": ["Ultra 4 BHK"],
         "budget_lakhs": [90, 100]},
    ])


def test_alternatives_are_filtered_by_the_callers_configuration():
    ids = [i["id"] for i in ps.alternatives(_kb(), (95, 95), set(), limit=9,
                                            config="3 BHK")]
    assert ids == ["a", "c"]                   # c: no list → never hidden
    ids4 = [i["id"] for i in ps.alternatives(_kb(), (95, 95), set(), limit=9,
                                             config="4bhk")]
    assert ids4 == ["a", "c", "d"]             # Ultra 4 BHK is a 4 BHK
    # No configuration known → exactly today's list.
    assert [i["id"] for i in ps.alternatives(_kb(), (95, 95), set(), limit=9)] \
        == ["a", "b", "c", "d"]


@pytest.mark.parametrize("cfgs,want,ok", [
    (["3 BHK"], "3 BHK", True), (["3 BHK"], "4 BHK", False),
    (["Ultra 4 BHK"], "4 BHK", True), (["4 BHK"], "Ultra 4 BHK", True),
    ([], "3 BHK", True), (["2 BHK"], "", True),
])
def test_offers_config(cfgs, want, ok):
    assert ps.offers_config({"configurations": cfgs}, want) is ok
