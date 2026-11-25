"""What may and may not become a slot value, and what counts as a decline.

Found auditing the Bhavna Patel agent (jai_mistry-outbound) on 2026-09-18 by
driving the real engine with the things real prospects say. Four defects, none
of them specific to that agent:

1. "हाँ जी बताओ" — yes, go on — was stored as the ANSWER to the question the
   opening had just asked (customer_channel='haan ji batao'). The 2026-09-16
   bare-acknowledgement guard only covered the json extractor, which production
   does not run (LLM_TOOLS_MODE=on), and knew no Hindi go-ahead verb.
2. The tool roster exposed bare slot names, so the model filled whatever was
   empty: "mostly references" → customer_channel AND business_type.
3. Placeholders ('unscheduled', 'Pending') were stored as values and satisfied
   slot gates.
4. "फॉलो अप करने का टाइम नहीं मिलता" — the prospect naming the exact pain the
   business sells against — was classified as a deferral and answered with
   "no problem, what time would be convenient for you?".

Offline: no LLM calls.
"""

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice import conversation as conv
from app.voice.conversation import ConversationRouter, detect_intent
from app.voice.llm_tools import (
    ToolProposal,
    build_tool_declarations,
    clean_slot_args,
    confirm_line,
)


def _router(monkeypatch) -> ConversationRouter:
    monkeypatch.setenv("LLM_TOOLS_MODE", "on")
    d = {
        "agent_id": "t_hygiene", "version": "1.0.0",
        "identity": {"agent_name": "Asha", "company_name": "TestCo"},
        "languages": {"supported": ["en", "hi"], "default": "hi",
                      "tts_voices": {}},
        "call_flow": {"stages": [
            {"id": "S1", "order": 0, "goal": "one", "turns_until_advance": 8},
            {"id": "S2", "order": 1, "goal": "two", "turns_until_advance": 99},
        ]},
        "response_bank": {"opening_greeting": "hi"},
        "slots": [
            {"name": "customer_channel", "type": "text", "label": "Channel",
             "question": {"hi": "Naye customers aapko kaise milte hain?",
                          "en": "How do new customers find you?"}},
            {"name": "business_type", "type": "text",
             "label": "Their business",
             "question": {"en": "What kind of business do you run?"}},
            {"name": "lead_satisfaction", "type": "text",
             "question": {"en": "Are you happy with your leads?"}},
            {"name": "consultation_time", "type": "text", "required": False,
             "question": {"en": "Which day and time suits you?"}},
        ],
    }
    r = ConversationRouter(agent_def=AgentDefinition.from_dict(d),
                           default_language="hi")
    r.state.turn_count = 3
    return r


def _record(router, args):
    router._pending_tool_proposals = [
        ToolProposal(name="record_customer_details", args=args)]
    router._execute_tool_proposals(had_text=True)
    return {k: v for k, v in router.state_mgr.state.items() if v is not None}


# ── 1. a go-ahead is not an answer ──────────────────────────────────────────

@pytest.mark.parametrize("said", [
    "हाँ जी बताओ", "हां बोलिए", "haan ji batao", "Haan boliye",
    "जी बोलिए", "बताओ बताओ", "हाँ तो बताइए", "hello haan bolo",
    # with the adverbs that ride along — the model even adds its own ("abhi")
    "हाँ जल्दी बताओ थोड़ा", "abhi jaldi batao thoda", "ज़रा जल्दी बोलिए",
])
def test_go_ahead_is_never_stored_as_a_value(monkeypatch, said):
    r = _router(monkeypatch)
    assert _record(r, {"customer_channel": said}) == {}


@pytest.mark.parametrize("said", ["हाँ जी बताओ", "haan boliye", "जी बोलिए"])
def test_go_ahead_turn_counts_as_acknowledgement_only(said):
    # The json extractor's guard reads this; it knew no Hindi go-ahead verb.
    assert conv._is_ack_only(said)


@pytest.mark.parametrize("value", ["No", "नहीं", "Yes", "हाँ", "nahi"])
def test_yes_and_no_remain_real_values(monkeypatch, value):
    """'No' IS the answer to 'are you happy with your leads?' — the go-ahead
    rule must not swallow it (that would empty every yes/no text slot)."""
    r = _router(monkeypatch)
    assert _record(r, {"lead_satisfaction": value}) == {
        "lead_satisfaction": value}


@pytest.mark.parametrize("value", [
    "abhi", "अभी", "kya", "thoda", "jaldi",                 # fillers alone
    "unko bolo ki mujhe call karein",                        # a real message
    "Monday ko batao", "जल्दी डिलीवरी",
])
def test_fillers_alone_and_real_sentences_are_kept(monkeypatch, value):
    """The filler list only counts next to a go-ahead verb and nothing else —
    on their own, or inside a sentence carrying real words, values survive."""
    r = _router(monkeypatch)
    assert _record(r, {"business_type": value}) == {"business_type": value}


def test_real_answers_pass_through(monkeypatch):
    r = _router(monkeypatch)
    got = _record(r, {"customer_channel": "रेफरेंस से",
                      "business_type": "Furniture showroom"})
    assert got == {"customer_channel": "रेफरेंस से",
                   "business_type": "Furniture showroom"}


# ── 3. a placeholder is not an answer ───────────────────────────────────────

@pytest.mark.parametrize("junk", [
    "unscheduled", "Pending", "pending confirmation", "Not specified",
    "TBD", "not mentioned", "N/A", "none", "unknown", "Not provided.",
])
def test_placeholders_never_fill_a_slot(monkeypatch, junk):
    r = _router(monkeypatch)
    assert _record(r, {"consultation_time": junk}) == {}
    assert clean_slot_args({"consultation_time": junk}) == {}


def test_placeholder_leaves_the_slot_open_for_the_real_answer(monkeypatch):
    r = _router(monkeypatch)
    _record(r, {"consultation_time": "unscheduled"})
    assert _record(r, {"consultation_time": "Monday 4 PM"}) == {
        "consultation_time": "Monday 4 PM"}


# ── 2. the tool roster says what each slot means ────────────────────────────

def test_tool_declarations_carry_slot_meaning(monkeypatch):
    r = _router(monkeypatch)
    hints = r._tool_slot_hints()
    assert "business" in hints["business_type"].lower()
    decls = build_tool_declarations(list(r.state_mgr.slots), hints)
    props = next(d for d in decls if d["function"]["name"]
                 == "record_customer_details")["function"]["parameters"][
                     "properties"]
    assert "kind of business" in props["business_type"]["description"]
    # Hindi call → the Hindi question is the hint, as for the json extractor.
    assert "kaise milte" in props["customer_channel"]["description"]


def test_tool_declarations_without_hints_are_unchanged():
    decls = build_tool_declarations(["budget", "bhk"])
    props = decls[0]["function"]["parameters"]["properties"]
    assert props == {"budget": {"type": "string"}, "bhk": {"type": "string"}}


def test_slot_hint_is_bounded():
    decls = build_tool_declarations(["a"], {"a": "x" * 5000})
    desc = decls[0]["function"]["parameters"]["properties"]["a"]["description"]
    assert len(desc) <= 160


# ── 4. "no time to <do something>" is a pain point, not a deferral ──────────

@pytest.mark.parametrize("said", [
    "सबसे बड़ा चैलेंज यह है कि फॉलो अप करने का टाइम नहीं मिलता",
    "पोस्ट डालने के लिए समय नहीं मिलता हमें",
    "follow up karne ka time nahi milta",
    "honestly we have no time to follow up with leads",
    "we have no time for marketing",
])
def test_activity_time_complaint_is_not_a_decline(said):
    assert detect_intent(said, stage="DISCOVERY") != "objection"


@pytest.mark.parametrize("said", [
    "मेरे पास टाइम नहीं है",
    "अभी टाइम नहीं है बाद में बात करते हैं",
    "बात करने का टाइम नहीं है अभी",          # the activity IS the call
    "I have no time to talk right now",
    "no time for this",
    "no time",
    "मुझे इंटरेस्ट नहीं है",
    "not interested",
    "abhi busy hoon call later",
    # a real decline in the same breath as a pain point still counts
    "फॉलो अप करने का टाइम नहीं मिलता और वैसे भी मुझे इंटरेस्ट नहीं है",
])
def test_real_declines_still_count(said):
    assert detect_intent(said, stage="DISCOVERY") == "objection"


# ── booking sanity ──────────────────────────────────────────────────────────

def test_past_dated_booking_is_refused(monkeypatch):
    """A date in the past is not a booking — unless it is only the YEAR that
    is wrong. 2025-09-19 on a call made 2026-09-18 is the model's
    training-era year on tomorrow's date (call 904 passed 2025-09-25 four
    times for "tomorrow, 3 o'clock" and every attempt was refused, so the
    caller was told the visit was fixed while nothing was stored). Such a
    date is read as this year's; a date genuinely weeks in the past stays
    refused."""
    monkeypatch.setenv("PROMPT_NOW_OVERRIDE", "2026-09-18T13:00:00")
    r = _router(monkeypatch)
    r._write_calendar_booking = lambda *_a, **_k: None
    r._pending_tool_proposals = [ToolProposal(
        name="book_appointment", args={"date": "2026-09-10", "time": "16:00"})]
    r._execute_tool_proposals(had_text=True)
    assert not r.memory.call_state.appointment_time

    r._pending_tool_proposals = [ToolProposal(
        name="book_appointment", args={"date": "2025-09-19", "time": "16:00"})]
    r._execute_tool_proposals(had_text=True)
    assert r.memory.call_state.appointment_date == "2026-09-19"
    assert r.memory.call_state.appointment_time == "16:00"


def test_wrong_year_is_corrected_but_a_real_past_date_is_not(monkeypatch):
    monkeypatch.setenv("PROMPT_NOW_OVERRIDE", "2026-09-24T10:00:00")
    assert conv._roll_forward_wrong_year("2025-09-25") == "2026-09-25"
    assert conv._roll_forward_wrong_year("2024-10-05") == "2026-10-05"
    # Genuinely past: shifting the year lands outside the booking window.
    assert conv._roll_forward_wrong_year("2026-09-10") == "2026-09-10"
    assert conv._roll_forward_wrong_year("2025-03-01") == "2025-03-01"
    # Not ISO, or not past: untouched.
    assert conv._roll_forward_wrong_year("tomorrow") == "tomorrow"
    assert conv._roll_forward_wrong_year("2026-09-24") == "2026-09-24"
    assert conv._roll_forward_wrong_year("2026-11-02") == "2026-11-02"


def test_relative_dates_are_not_mistaken_for_past(monkeypatch):
    monkeypatch.setenv("PROMPT_NOW_OVERRIDE", "2026-09-18T13:00:00")
    assert not conv._is_past_iso_date("tomorrow")
    assert not conv._is_past_iso_date("Monday")
    assert not conv._is_past_iso_date("2026-09-18")      # today is bookable
    assert conv._is_past_iso_date("2026-09-17")


def test_canned_booking_confirmation_is_speakable():
    line = confirm_line("book_appointment", "hi",
                        {"date": "2026-09-19", "time": "16:00"})
    assert "2026" not in line and "16:00" not in line
    assert "19 September" in line and "4 PM" in line
    # already-speakable values pass through untouched
    assert "Tuesday" in confirm_line("book_appointment", "en",
                                     {"date": "Tuesday", "time": "3pm"})


# ── a tool-written date/time is held to the same bar as any other ───────────

def _time_router(monkeypatch):
    monkeypatch.setenv("LLM_TOOLS_MODE", "on")
    d = {
        "agent_id": "t_time", "version": "1.0.0",
        "identity": {"agent_name": "Asha", "company_name": "TestCo"},
        "languages": {"supported": ["en", "hi"], "default": "hi",
                      "tts_voices": {}},
        "call_flow": {"stages": [
            {"id": "S1", "order": 0, "goal": "one", "turns_until_advance": 8}]},
        "response_bank": {"opening_greeting": "hi"},
        "slots": [{"name": "consultation_time", "type": "time",
                   "required": False,
                   "question": {"en": "Which day and time suits you?"}}],
    }
    r = ConversationRouter(agent_def=AgentDefinition.from_dict(d),
                           default_language="hi")
    r.state.turn_count = 3
    return r


@pytest.mark.parametrize("junk", [
    "Sure, we can do a call", "हाँ ठीक है, कर सकते हैं", "after discussion",
])
def test_tool_cannot_store_a_non_time_in_a_time_slot(monkeypatch, junk):
    """Simulated 2026-09-18: asked for a day and time, the caller said "Sure,
    we can do a call"; the record tool stored that sentence, and fill-only
    then refused "Monday 11 am" one turn later."""
    r = _time_router(monkeypatch)
    assert _record(r, {"consultation_time": junk}) == {}
    assert _record(r, {"consultation_time": "Monday 11 am"}) == {
        "consultation_time": "Monday 11 am"}


@pytest.mark.parametrize("value", [
    "Monday 11 am", "Saturday, 19 September 2026 at 4 PM", "tomorrow evening",
    "कल शाम चार बजे", "परसों सुबह", "११ बजे", "કાલે સાંજે",
])
def test_real_times_in_any_script_are_stored(monkeypatch, value):
    r = _time_router(monkeypatch)
    assert _record(r, {"consultation_time": value}) == {
        "consultation_time": value}


# ── "parso" is written out, not left for the model to count ─────────────────

def test_clock_band_resolves_parso(monkeypatch):
    from datetime import datetime
    from app.voice import prompt_blocks as pb
    r = _time_router(monkeypatch)
    friday = datetime(2026, 9, 18, 13, 0)
    band = "\n".join(pb.now_block(r._def, friday))
    assert "day after tomorrow Sun 20 Sep" in band
    assert "परसों" in band and "(Sun 20 Sep)" in band
    assert "tomorrow (Sat 19 Sep)" in band or "is tomorrow (Sat 19 Sep)" in band
