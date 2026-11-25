"""Agent-initiated hangup + close-permission confirm (platform rule, all
agents, 2026-07-27).

The supreme rule: the agent never ends a call the caller hasn't agreed to
end. hangup_pending arms ONLY on caller-consented closes — the caller's own
goodbye, or a "no, that's all" reply to the agent's permission-to-close
question — plus the involuntary terminal closes (wrong number, DND/decline
threshold). A new caller utterance after the goodbye cancels it.

The bridge side (conversation_call_bridge._maybe_agent_hangup) polls this
flag after the farewell audio drains; socket_server queues an event-locked
FreeSWITCH hangup so the goodbye is never cut mid-sentence.
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice.conversation import ConversationRouter


@pytest.fixture(autouse=True)
def _no_network_llm(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


def make_router(**over) -> ConversationRouter:
    d = {
        "agent_id": "t_hangup",
        "version": "1.0.0",
        "identity": {"agent_name": "Asha", "company_name": "TestCo"},
        "languages": {"supported": ["en", "hi"], "default": "en", "tts_voices": {}},
        "call_flow": {
            "stages": [
                {"id": "S1", "order": 0, "goal": "one", "turns_until_advance": 8},
                {"id": "S2", "order": 1, "goal": "two", "turns_until_advance": 99},
            ],
        },
        "response_bank": {"opening_greeting": "hi"},
    }
    d.update(over)
    agent = AgentDefinition.from_dict(d)
    return ConversationRouter(agent_def=agent, default_language="en")


def drive(router: ConversationRouter, text: str) -> str:
    async def _one():
        out = []
        async for res in router.process_stream(text):
            out.append(res.text)
        return " ".join(t for t in out if t)

    return asyncio.run(_one())


def test_caller_goodbye_arms_hangup():
    """Two turns now: the goodbye is confirmed before the hangup arms
    (close-confirm gate, 2026-07-31)."""
    r = make_router()
    drive(r, "okay thank you, goodbye")
    assert r.hangup_pending is False       # confirmation asked, not closed
    drive(r, "yes")
    assert r.state.is_closing is True
    assert r.hangup_pending is True


def test_close_permission_confirm_arms_hangup():
    """Agent asked 'anything else?' → caller 'no, that's all' → confirm → close."""
    r = make_router()
    r.last_agent_response = "Great! Is there anything else I can help you with?"
    drive(r, "no that's all")
    assert r.hangup_pending is False
    drive(r, "yes please")
    assert r.state.is_closing is True
    assert r.hangup_pending is True


def test_close_permission_confirm_hinglish():
    r = make_router()
    r.last_agent_response = "Theek hai! Aur kuch madad chahiye aapko?"
    drive(r, "nahi bas")
    assert r.hangup_pending is False
    drive(r, "haan ji")
    assert r.hangup_pending is True


def test_plain_no_without_permission_question_does_not_close():
    """A bare 'no' mid-conversation is an answer, not a goodbye."""
    r = make_router()
    r.last_agent_response = "Would you like the premium plan?"
    drive(r, "no")
    assert r.state.is_closing is False
    assert r.hangup_pending is False


def test_no_but_a_question_keeps_the_call_alive():
    r = make_router()
    r.last_agent_response = "Is there anything else I can help you with?"
    drive(r, "no but what is the price")
    assert r.hangup_pending is False


def test_caller_speaking_again_cancels_hangup():
    """'Wait, one more thing' after the goodbye must be answered, not cut."""
    r = make_router()
    drive(r, "okay bye")
    drive(r, "yes")                        # confirm the close
    assert r.hangup_pending is True
    drive(r, "wait actually one more question about the location")
    assert r.hangup_pending is False


def test_wrong_number_close_arms_hangup():
    r = make_router(call_direction="outbound",
                    outbound={"wrong_number_patterns": ["wrong number"],
                              "wrong_number_close": {"en": "So sorry! Bye."}})
    r.call_direction = "outbound"
    # RUNBOOK §73: re-confirmed once, then the caller's yes closes it.
    drive(r, "you have the wrong number")
    assert r.state.is_closing is False
    drive(r, "yes")
    assert r.state.is_closing is True
    assert r.hangup_pending is True


# ── Confirmed callback = consented close (platform rule, 2026-07-27) ──
# "Call me after 10 minutes" is the caller asking for the call to end NOW
# and resume later — after confirming the time the agent says goodbye and
# the platform hangs up. This is what the Copilot could never deliver via
# prompt guardrails: no script text can cut a call, only this engine path.

def test_confirmed_relative_callback_closes_the_call():
    r = make_router()
    resp = drive(r, "can you call me after 10 minutes")
    assert "10 minute" in resp
    assert r.state.is_closing is True
    assert r.hangup_pending is True


def test_confirmed_day_time_callback_closes_the_call():
    r = make_router()
    drive(r, "call me tomorrow at 5 pm")
    assert r.hangup_pending is True


def test_clarify_question_turn_does_not_close():
    """The pin-down QUESTION keeps the call open — only the confirmed
    answer closes it."""
    r = make_router()
    q = drive(r, "call me later")
    assert "?" in q
    assert r.hangup_pending is False
    a = drive(r, "ten minutes")
    assert "?" not in a
    assert r.hangup_pending is True


def test_busy_brushoff_pins_down_then_closes():
    """'Sorry not free right now' must not burn decline strikes or keep
    pitching — it asks when to call back, then closes on the answer."""
    r = make_router()
    q = drive(r, "sorry not free right now")
    assert "?" in q and "no problem" in q.lower()
    assert r.state.decline_count == 0
    assert r.hangup_pending is False
    drive(r, "call me after 15 minutes")
    assert r.state_mgr.extra_flags.get("callback_offset_min") == 15
    assert r.hangup_pending is True
