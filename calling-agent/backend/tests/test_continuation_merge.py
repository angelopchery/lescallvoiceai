"""Continuation merge — turn transactions (snapshot / rollback / commit).

Proves the router side of the pause-tolerant turn feature: a provisional
turn's full mutation surface rolls back cleanly, the merged re-run produces
exactly one user turn, calendar bookings defer until commit (never duplicated,
never written for a superseded turn), closing turns are never supersedable,
and the kill switch keeps everything inert.

The pipeline-side trigger (gap timing, barge-in relaxation) is exercised by
live-call validation; these tests cover every state transition it drives.
"""

from __future__ import annotations

import asyncio
import copy

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice.conversation import ConversationRouter


@pytest.fixture(autouse=True)
def _no_network_llm(stub_llm):
    """These assert on router state, never on model wording — so the turns that
    fall through to the LLM run offline. Six of them were opening real network
    connections, which made them flaky for reasons unrelated to what they test.
    """


def make_router(enabled=True, **over) -> ConversationRouter:
    d = {
        "agent_id": "t_contmerge",
        "version": "1.0.0",
        "identity": {"agent_name": "Asha", "company_name": "TestCo"},
        "languages": {"supported": ["en", "hi"], "default": "en", "tts_voices": {}},
        "call_flow": {
            "stages": [
                {"id": "S1", "order": 0, "goal": "one", "turns_until_advance": 8},
                {"id": "S2", "order": 1, "goal": "two", "turns_until_advance": 99},
            ],
        },
        "calendar": {"mock_slots": []},
        "response_bank": {"opening_greeting": "hi"},
    }
    d.update(over)
    agent = AgentDefinition.from_dict(d)
    router = ConversationRouter(agent_def=agent, default_language="en")
    router._available_calendar_slots = lambda: []
    router._continuation_enabled = enabled

    async def _fake_llm(text):
        yield "Okay, noted."

    router._call_llm_stream = _fake_llm
    return router


def drive(router: ConversationRouter, text: str) -> str:
    async def _one():
        out = []
        async for res in router.process_stream(text):
            out.append(res.text)
        return " ".join(t for t in out if t)

    return asyncio.run(_one())


def observable_state(r: ConversationRouter) -> dict:
    """Everything a turn can mutate, in comparable form."""
    return {
        "turns": [(t.role, t.text) for t in r.memory.turns],
        "conv_state": copy.deepcopy(r.state.__dict__),
        "sm_state": copy.deepcopy(r.state_mgr.state),
        "audit_len": len(r.state_mgr.audit_log),
        "sm_pending": set(r.state_mgr._pending_fields),
        "sm_turn_index": r.state_mgr.turn_index,
        "extra_flags": copy.deepcopy(r.state_mgr.extra_flags),
        "qa_asked": dict(r.memory.qa_tracker._asked),
        "qa_answered": dict(r.memory.qa_tracker._answered),
        "last_agent_response": r.last_agent_response,
        "booking": copy.deepcopy(r.memory.booking.__dict__),
        "call_state": copy.deepcopy(r.memory.call_state.__dict__),
        "deferred_bookings": list(r._deferred_bookings),
    }


def test_rollback_restores_full_surface():
    r = make_router()
    baseline = observable_state(r)

    drive(r, "i want a three bedroom flat")
    assert r.has_pending_turn()
    # The turn visibly mutated state before rollback.
    assert observable_state(r) != baseline

    prev = r.rollback_pending_turn()
    assert prev == "i want a three bedroom flat"
    assert observable_state(r) == baseline
    assert not r.has_pending_turn()


def test_merged_rerun_is_single_turn():
    r = make_router()
    drive(r, "i want a three bedroom flat")
    prev = r.rollback_pending_turn()
    merged = prev + " in the city center"

    drive(r, merged)
    user_turns = [t for t in r.memory.turns if t.role == "user"]
    assert len(user_turns) == 1
    assert user_turns[0].text == merged
    assert r.state.turn_count == 1
    # The merged re-run opened its own transaction (chained pauses compose).
    assert r.has_pending_turn()


def test_dynamic_conv_state_attr_removed_on_rollback():
    r = make_router()
    stage_before = r.state.stage
    drive(r, "i want a flat")
    # Simulates stage machinery setting a dynamic attribute mid-turn.
    r.state.appointment_advanced = True
    r.state.stage = "S2"
    r.rollback_pending_turn()
    assert not hasattr(r.state, "appointment_advanced")
    assert r.state.stage == stage_before


def test_booking_defers_until_commit(monkeypatch):
    writes = []
    import app.agent_definition.calendar_store as cal

    monkeypatch.setattr(
        cal, "add_booking", lambda *a, **k: writes.append(k) or True
    )

    r = make_router()
    r.conn_id = "test-conn"
    r._agent_folder = lambda: "t_contmerge"  # test agent has no disk folder
    # Turn transaction open → booking write must defer.
    r.begin_turn_transaction("tuesday works")
    r._record_calendar_booking("Tuesday at 5 PM")
    assert writes == []
    assert r._deferred_bookings == ["Tuesday at 5 PM"]

    # Superseded turn → its deferred booking evaporates.
    r.rollback_pending_turn()
    assert r._deferred_bookings == []
    r.commit_pending_turn()
    assert writes == []

    # Committed turn → exactly one write, with the (merged) time.
    r.begin_turn_transaction("tuesday works in the evening")
    r._record_calendar_booking("Tuesday at 6 PM")
    r.commit_pending_turn()
    assert len(writes) == 1
    assert writes[0]["appointment_time"] == "Tuesday at 6 PM"


def test_booking_immediate_when_disabled(monkeypatch):
    writes = []
    import app.agent_definition.calendar_store as cal

    monkeypatch.setattr(
        cal, "add_booking", lambda *a, **k: writes.append(k) or True
    )
    r = make_router(enabled=False)
    r.conn_id = "test-conn"
    r._agent_folder = lambda: "t_contmerge"
    r.begin_turn_transaction("hello")  # no-op with the flag off
    assert not r.has_pending_turn()
    r._record_calendar_booking("Tuesday at 5 PM")
    assert len(writes) == 1


def test_rollback_cancels_slot_extractor():
    async def _scenario():
        r = make_router()
        r.begin_turn_transaction("some text")

        async def _hang():
            await asyncio.sleep(30)

        task = asyncio.create_task(_hang())
        r._slot_extract_task = task
        r.rollback_pending_turn()
        await asyncio.sleep(0)  # let cancellation propagate
        return task.cancelled()

    assert asyncio.run(_scenario())


def test_closing_turn_commits_immediately():
    r = make_router()
    drive(r, "thank you bye")
    # The close-confirm question (2026-07-31) is deliberately NOT committed —
    # if "bye" was the front of a longer sentence, continuation-merge should
    # still be able to roll it back. The confirmed close is the final turn.
    drive(r, "yes")
    assert r.state.is_closing is True
    assert not r.has_pending_turn()
    assert r.rollback_pending_turn() is None


def test_kill_switch_off_is_inert():
    r = make_router(enabled=False)
    drive(r, "i want a three bedroom flat")
    assert not r.has_pending_turn()
    assert r.rollback_pending_turn() is None
    # commit with nothing pending is a safe no-op
    r.commit_pending_turn()


def test_chained_merges_compose():
    r = make_router()
    drive(r, "i want a flat")
    p1 = r.rollback_pending_turn()

    m1 = p1 + " with three bedrooms"
    drive(r, m1)
    p2 = r.rollback_pending_turn()
    assert p2 == m1

    m2 = p2 + " in the city center"
    drive(r, m2)
    user_turns = [t for t in r.memory.turns if t.role == "user"]
    assert len(user_turns) == 1
    assert "flat" in user_turns[0].text
    assert "three bedrooms" in user_turns[0].text
    assert "city center" in user_turns[0].text


def test_begin_commits_previous_pending(monkeypatch):
    writes = []
    import app.agent_definition.calendar_store as cal

    monkeypatch.setattr(
        cal, "add_booking", lambda *a, **k: writes.append(k) or True
    )
    r = make_router()
    r.conn_id = "test-conn"
    r._agent_folder = lambda: "t_contmerge"  # test agent has no disk folder
    r.begin_turn_transaction("turn one")
    r._record_calendar_booking("Monday at 4 PM")
    assert writes == []
    # A new distinct turn starting = the previous one is final.
    r.begin_turn_transaction("turn two")
    assert len(writes) == 1
    assert r.has_pending_turn()
    assert r._pending_turn.user_text == "turn two"


def test_rollback_never_replays_a_filler():
    """Pre-canary invariant (2026-07-23): filler audio is a side effect that
    already happened — rolling back the turn must NOT rewind the filler
    governors, or the merged re-run plays a second 'one moment please'
    back-to-back and played fillers stop counting toward the per-call cap."""
    r = make_router()
    r.state.turn_count = 5
    r.begin_turn_transaction("first half of the sentence")
    # the provisional turn played a filler
    r._fillers_played += 1
    r._filler_counter += 1
    r._last_filler_turn = r.state.turn_count
    played, counter, last = r._fillers_played, r._filler_counter, r._last_filler_turn
    assert r.rollback_pending_turn() is not None
    assert r._fillers_played == played        # audio happened; stays counted
    assert r._filler_counter == counter
    assert r._last_filler_turn == last
