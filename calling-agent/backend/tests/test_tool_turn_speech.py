"""A tool turn must never leave the caller in silence (2026-08-04).

The prompt trailer tells the model "ALWAYS also reply with your normal 1-2
spoken sentences alongside any tool call". When the model ignores that, the
only thing the caller hears is a canned CONFIRM_LINE — and when the tool
filled nothing (the model named a property the agent does not have), there
is no canned line either, so the caller hears nothing at all.

Measured on this fleet's own transcripts: 0 canned lines in 1,206 agent
turns over 07-20..07-30, then 7.9% (08-01) / 10.3% (08-03) / 13.6% (08-04)
of agent turns; and 0 in 517 turns → 16.8% within maya_lescall_sdr alone,
an agent whose slot roster and tool wiring never changed across the step.

This suite pins the three layers of the repair:
  1. re-ask the model with the tools withdrawn — the caller gets real words
  2. the canned confirmation, if the re-ask does not land
  3. a spoken acknowledgement, if there is no canned line either
plus the junk-argument gate that produced "Done — I have noted today at now."
"""

from __future__ import annotations

import asyncio
import types

import pytest

from app.voice import conversation as conv
from app.voice.conversation import ConversationRouter
from app.voice.llm_tools import ToolProposal
from test_call_termination import drive, make_router


@pytest.fixture(autouse=True)
def _offline(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


def tool_router(**over) -> ConversationRouter:
    # A real slot roster, so record_customer_details actually fills something
    # and the canned CONFIRM_LINE path is exercised rather than skipped.
    over.setdefault("slots", [{"name": "name", "priority": 1},
                              {"name": "budget", "priority": 2}])
    r = make_router(**over)
    r._tools_mode = "on"
    r.state.turn_count = 3          # past the phantom-booking guard
    return r


def _arm(router, name: str, args: dict) -> None:
    """Make the next turn behave as if the model streamed this tool call
    and no text — the exact shape the Gemini compat shim produces."""
    real = ConversationRouter._call_llm_stream

    async def _texless(self, text, force_text=False):
        if force_text:
            async for chunk in real(self, text, force_text=True):
                yield chunk
            return
        self._pending_tool_proposals = [ToolProposal(name=name, args=args)]
        return
        yield  # pragma: no cover — makes this an async generator

    router._call_llm_stream = types.MethodType(_texless, router)


# ── layer 1: the re-ask ─────────────────────────────────────────────────────

def test_textless_tool_turn_reasks_for_words(stub_llm):
    r = tool_router()
    _arm(r, "record_customer_details", {"name": "Ramesh"})
    resp = drive(r, "my name is Ramesh")
    assert stub_llm in resp
    assert "Noted, thank you." not in resp


def test_reask_is_not_used_when_the_model_already_spoke():
    """A turn that carried text pays no extra round trip."""
    r = tool_router()
    calls: list[bool] = []
    real = ConversationRouter._call_llm_stream

    async def _with_text(self, text, force_text=False):
        calls.append(force_text)
        if force_text:
            async for c in real(self, text, force_text=True):
                yield c
            return
        self._pending_tool_proposals = [
            ToolProposal(name="record_customer_details", args={"name": "R"})]
        yield "Lovely, Ramesh."

    r._call_llm_stream = types.MethodType(_with_text, r)
    resp = drive(r, "my name is Ramesh")
    assert "Lovely, Ramesh." in resp
    assert calls == [False]          # no re-ask


def test_reask_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(conv, "_TOOL_TEXT_REISSUE", False)
    r = tool_router()
    # budget, not name: the deterministic extractors would have filled `name`
    # from the utterance already, and a fill-only merge then changes nothing.
    _arm(r, "record_customer_details", {"budget": "50 lakh"})
    resp = drive(r, "yes that works for me")
    assert "Noted, thank you." in resp     # today's behaviour, unchanged


def test_reask_timeout_falls_back_to_the_canned_line(monkeypatch):
    monkeypatch.setattr(conv, "_TOOL_TEXT_REISSUE_TIMEOUT_S", 0.01)
    r = tool_router()
    _arm(r, "record_customer_details", {"budget": "50 lakh"})

    real = r._call_llm_stream

    async def _slow(text, force_text=False):
        if force_text:
            await asyncio.sleep(5)
            yield "too late"
            return
        async for c in real(text, force_text=force_text):
            yield c

    r._call_llm_stream = _slow
    resp = drive(r, "yes that works for me")
    assert "Noted, thank you." in resp


def test_reask_never_rearms_the_hangup():
    r = tool_router()
    _arm(r, "record_customer_details", {"name": "Ramesh"})
    r._llm_end_call_seen = False
    drive(r, "my name is Ramesh")
    assert r.hangup_pending is False


# ── layer 3: zero-fill must not be dead air ─────────────────────────────────

def test_zero_fill_tool_still_speaks(monkeypatch):
    """The 08-03 signature: record_customer_details naming a property the
    agent does not have → 0 slots filled → no canned line → dead air."""
    monkeypatch.setattr(conv, "_TOOL_TEXT_REISSUE", False)
    r = tool_router()
    _arm(r, "record_customer_details", {"current_insurance_status": "none"})
    resp = drive(r, "I don't have insurance right now")
    assert resp.strip()               # the whole point: not silence


def test_zero_fill_prefers_the_agents_own_wording():
    r = tool_router(response_bank={
        "opening_greeting": "hi",
        "tool_ack_fallbacks": {"en": "Understood — anything else?"}})
    r._tools_mode = "on"
    r.state.turn_count = 3
    _arm(r, "record_customer_details", {"nope": "x"})

    async def _empty(text, force_text=False):
        if force_text:
            return
            yield  # pragma: no cover
        r._pending_tool_proposals = [
            ToolProposal(name="record_customer_details", args={"nope": "x"})]
        return
        yield  # pragma: no cover

    r._call_llm_stream = _empty
    assert "Understood — anything else?" in drive(r, "nothing useful")


# ── junk temporal arguments ─────────────────────────────────────────────────

@pytest.mark.parametrize("date,time_,ok", [
    ("today", "now", False),
    ("tomorrow", "asap", False),
    ("2026-08-06", "", False),
    ("tomorrow", "3pm", True),
    ("Monday", "15:30", True),
    ("tomorrow", "4 o'clock", True),
    ("कल", "शाम 5 बजे", True),
])
def test_bookable_when(date, time_, ok):
    assert conv._is_bookable_when(date, time_) is ok


def test_junk_booking_is_not_confirmed():
    """Live 08-03: [TOOLS] book_appointment executed today at now →
    the caller heard "Done — I have noted today at now."."""
    r = tool_router()
    _arm(r, "book_appointment", {"date": "today", "time": "now"})
    resp = drive(r, "sure, sounds good")
    assert "today at now" not in resp
    assert r.memory.call_state.appointment_time in (None, "")


def test_real_booking_still_confirms(monkeypatch):
    monkeypatch.setattr(conv, "_TOOL_TEXT_REISSUE", False)
    r = tool_router()
    _arm(r, "book_appointment", {"date": "tomorrow", "time": "4pm"})
    resp = drive(r, "tomorrow at four works")
    assert "tomorrow" in resp and "4pm" in resp
    assert r.memory.call_state.appointment_time == "4pm"


def test_real_booking_is_recorded_even_when_the_reask_speaks(stub_llm):
    """The action commits on the first pass; the re-ask only buys words."""
    r = tool_router()
    _arm(r, "book_appointment", {"date": "tomorrow", "time": "4pm"})
    resp = drive(r, "tomorrow at four works")
    assert stub_llm in resp
    assert r.memory.call_state.appointment_time == "4pm"


# ── dead-air repair no longer replays an acknowledgement ────────────────────

def test_reprompt_does_not_replay_an_acknowledgement():
    """Live 08-03: "Sorry, I couldn't hear you. Noted, thank you."."""
    r = make_router()
    r.state.turn_count = 3
    r.memory.add_user("I want health insurance")
    r.memory.add_assistant("And what cover amount are you looking for?")
    r.memory.add_user("hmm")
    r._store_last_response("Noted, thank you.")
    out = r.generate_silence_reprompt()
    assert "Noted, thank you." not in out
    assert "cover amount" in out          # the real question came back


def test_reprompt_still_replays_a_question():
    r = make_router()
    r.state.turn_count = 3
    r._store_last_response("What is your budget?")
    assert "What is your budget?" in r.generate_silence_reprompt()


def test_reprompt_with_nothing_to_re_ask_asks_plainly():
    r = make_router()
    r.state.turn_count = 3
    r._store_last_response("Noted, thank you.")
    out = r.generate_silence_reprompt()
    assert out and "Noted, thank you." not in out


# ── layer 4: the re-ask must not disarm a close ─────────────────────────────
# Live call 6ad891a4 (2026-08-25, shubhangi-outbound). The caller said "yes,
# yes — let's close the call here"; the model answered with the `end_call`
# TOOL and no words, which is precisely what triggers the re-ask. The re-ask
# then cleared _llm_end_call_seen on its way out, so the close handler that
# runs immediately after saw nothing to do. The agent spoke its farewell and
# stayed on the line, asked to close a fourth time, and dead-air repair asked
# a fifth. `end_call` is ALWAYS a text-less tool turn — this defect dropped
# every tool-issued close on the fleet, not just this one.

def test_end_call_tool_survives_the_text_reask():
    r = tool_router(termination={"confirm_before_end": False})
    _arm(r, "end_call", {"reason": "caller agreed to close"})
    resp = drive(r, "yes, yes let's close the call here")
    assert resp.strip()                      # the caller still hears a goodbye
    assert r.hangup_pending is True          # ...and the call actually ends


def test_end_call_tool_reaches_the_close_confirm_gate():
    """With the default gate on, the tool close still has to be CONFIRMED —
    the fix restores the signal, it does not skip the platform's gate."""
    r = tool_router()
    _arm(r, "end_call", {"reason": "caller agreed to close"})
    resp = drive(r, "yes, yes let's close the call here")
    assert r._close_confirm_question() in resp
    assert r.hangup_pending is False         # deferred until the caller says yes


def test_reask_marker_alone_still_cannot_arm_a_close():
    """The original guard survives: when the FIRST pass never asked to close,
    an [END_CALL] the retry invents is still discarded."""
    r = tool_router()
    real = ConversationRouter._call_llm_stream

    async def _stream(self, text, force_text=False):
        if force_text:
            # What the real stream does with a marker in the model's text.
            yield self._strip_end_call_marker("All noted. [END_CALL]")
            return
        self._pending_tool_proposals = [
            ToolProposal(name="record_customer_details", args={"budget": "50 lakh"})]
        return
        yield  # pragma: no cover — makes this an async generator

    r._call_llm_stream = types.MethodType(_stream, r)
    resp = drive(r, "fifty lakh is my budget")
    assert "END_CALL" not in resp            # never spoken
    assert r.hangup_pending is False         # and never closes



# ── F4: a spoken booking claim the gates refused ─────────────────────────────

def _speak_then_book(r, text, args):
    async def _fake(self, text_in, force_text=False):
        if force_text:
            return
            yield  # pragma: no cover
        yield text
        self._pending_tool_proposals = [
            ToolProposal(name="book_appointment", args=args)]
    r._call_llm_stream = types.MethodType(_fake, r)


@pytest.mark.parametrize("args,reason", [
    ({"date": "today", "time": "now"}, "unusable"),
    ({"date": "2020-01-01", "time": "4pm"}, "unusable"),
])
def test_refused_booking_after_spoken_claim_arms_not_booked_band(args, reason):
    r = tool_router()
    _speak_then_book(r, "Perfect, your visit is booked.", args)
    drive(r, "yes tomorrow four")
    assert r.memory.call_state.appointment_time in (None, "")
    assert r._booking_unconfirmed_reason() == reason
    assert any("NOT BOOKED" in l for l in r._booking_unconfirmed_band())


def test_closing_refusal_says_do_not_offer_booking():
    r = tool_router()
    r.state.is_closing = True
    _speak_then_book(r, "Booked!", {"date": "tomorrow", "time": "4pm"})
    drive(r, "ok")
    assert r._booking_unconfirmed_reason() == "refused"


def test_executed_booking_never_arms_the_band():
    r = tool_router()
    _speak_then_book(r, "Done, tomorrow at 4pm.",
                     {"date": "tomorrow", "time": "4pm"})
    drive(r, "tomorrow at four works")
    assert r.memory.call_state.appointment_time == "4pm"
    assert r._booking_unconfirmed_reason() == ""
    assert r._booking_unconfirmed_band() == []


def test_band_expires_after_the_next_turn():
    r = tool_router()
    _speak_then_book(r, "Booked.", {"date": "today", "time": "now"})
    drive(r, "yes")
    r.state.turn_count += 2
    assert r._booking_unconfirmed_reason() == ""


def test_reask_after_refusal_does_not_claim_recorded(monkeypatch):
    """The text re-ask must not be told 'already been recorded' when the
    booking was refused."""
    r = tool_router()
    r._booking_refused = (r.state.turn_count, "unusable")
    assert r._booking_unconfirmed_reason() == "unusable"
    import inspect
    src = inspect.getsource(ConversationRouter._call_llm_stream)
    i_new = src.index("No booking was recorded")
    i_old = src.index("already been recorded")
    assert i_new < i_old     # refusal branch is tested first


# ── F4, the re-ask PROMPT itself (not the branch order) ──────────────────────

def _recording_client(seen: list, reply: str):
    """An offline client that records the messages it was sent."""

    class _Stream:
        def __aiter__(self):
            async def gen():
                yield types.SimpleNamespace(choices=[types.SimpleNamespace(
                    delta=types.SimpleNamespace(content=reply))])
            return gen()

    class _Client:
        def __init__(self):
            async def _create(**kw):
                seen.append(kw.get("messages") or [])
                return _Stream()
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=_create))

    return _Client


def test_reask_prompt_after_a_refused_booking_says_nothing_was_recorded(monkeypatch):
    """Tool-only turn, booking refused by the gates: the sentence the model is
    asked for must not be built on 'the details have already been recorded'."""
    seen: list = []
    monkeypatch.setattr(ConversationRouter, "_get_client",
                        lambda self: _recording_client(
                            seen, "Which day and time would suit you?")())
    r = tool_router()
    _arm(r, "book_appointment", {"date": "today", "time": "now"})
    resp = drive(r, "yes let's fix it")
    assert seen, "the text re-ask never reached the model"
    system = seen[-1][0]
    assert system["role"] == "system"
    assert "No booking was recorded" in system["content"]
    assert "do not say a visit or appointment is booked" in system["content"]
    assert "already been recorded" not in system["content"]
    assert "Which day and time would suit you?" in resp
    assert r.memory.call_state.appointment_time in (None, "")


def test_reask_prompt_after_an_executed_booking_is_unchanged(monkeypatch):
    seen: list = []
    monkeypatch.setattr(ConversationRouter, "_get_client",
                        lambda self: _recording_client(
                            seen, "Lovely, see you tomorrow at four.")())
    r = tool_router()
    _arm(r, "book_appointment", {"date": "tomorrow", "time": "4pm"})
    drive(r, "tomorrow at four works")
    assert seen
    system = seen[-1][0]["content"]
    assert "already been recorded" in system
    assert "No booking was recorded" not in system
    assert r.memory.call_state.appointment_time == "4pm"
    assert r._booking_unconfirmed_band() == []


# ── an hour the caller never said is not a booking (call 1072, 2026-10-12) ──
# "संडे को आ जाऊँगी" became book_appointment(2026-10-18, 11:00); the caller
# was told "any time 10 to 7" and then read back "11 बजे".

def test_booking_with_an_hour_the_caller_never_said_is_refused():
    r = tool_router()
    _arm(r, "book_appointment", {"date": "2026-10-18", "time": "11:00"})
    drive(r, "हाँ ठीक है संडे को आ जाऊँगी")
    assert r.memory.call_state.appointment_time in (None, "")
    assert r._booking_unconfirmed_reason() == "unusable"


@pytest.mark.parametrize("said,tm", [
    ("संडे ग्यारह बजे आ जाऊँगी", "11:00"),
    ("sunday 11 baje", "11:00"),
    ("कल शाम ४ बजे", "4pm"),
    ("tomorrow at four works", "16:00"),
    ("રવિવારે અગિયાર વાગ્યે", "11:00"),
])
def test_booking_with_the_callers_own_hour_is_kept(said, tm):
    r = tool_router()
    _arm(r, "book_appointment", {"date": "2026-10-18", "time": tm})
    drive(r, said)
    assert r.memory.call_state.appointment_time == tm


def test_an_hour_the_agent_offered_and_the_caller_accepted_is_kept():
    r = tool_router()
    r.memory.add_assistant("रविवार 11 बजे ठीक रहेगा?")
    _arm(r, "book_appointment", {"date": "2026-10-18", "time": "11:00"})
    drive(r, "हाँ जी")
    assert r.memory.call_state.appointment_time == "11:00"


def test_hour_matching_is_script_aware():
    assert conv._hour_said_in("ग्यारह बजे", {11, 23})
    assert conv._hour_said_in("११ बजे", {11, 23})
    assert not conv._hour_said_in("1100 sq ft nahi, 11 nahi", set())
    assert not conv._hour_said_in("ग्यारहवीं मंज़िल", {11})     # no word edge
    assert conv._hours_of("4pm") == {4, 16} and conv._hours_of("now") == set()


def test_one_calendar_row_per_call_and_always_with_its_date():
    """Call 1072 wrote "2026-10-18 at 11:00" from the tool and, one turn
    later, a bare "11:00" from the CallState→booking sync."""
    r = tool_router()
    rows: list[str] = []
    r._write_calendar_booking = rows.append
    _arm(r, "book_appointment", {"date": "2026-10-18", "time": "11:00"})
    drive(r, "संडे ग्यारह बजे आ जाऊँगी")
    r._call_llm_stream = ConversationRouter._call_llm_stream.__get__(r)
    drive(r, "ठीक है")
    drive(r, "हाँ")
    assert rows == ["2026-10-18 at 11:00"]
