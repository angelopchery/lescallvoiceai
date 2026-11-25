"""The authored good-time beat is never rolled back (RUNBOOK §77).

Call 1061: the engine spoke `outbound.good_time_check_line` the moment the
caller said "हां जी"; a "ठीक है?" fragment landed while it played; the
continuation merge rolled the provisional turn back — the line left history,
`_good_time_spoken` was restored to False, the merged "हां जी। ठीक है?" no
longer qualified for the fast path — and the model re-introduced itself
from the INTRO stage goal. The beat now commits its turn, exactly as closing
turns do, so the fragment is simply the next turn.

Offline: stub LLM; the identity harness from test_identity_reconfirm.
"""
from __future__ import annotations

import asyncio

import pytest

from app.voice.conversation import ConversationRouter
from tests.test_identity_reconfirm import make_router


@pytest.fixture(autouse=True)
def _offline(stub_llm):
    """Model turns run against the offline stub."""


def drive(router: ConversationRouter, text: str) -> str:
    async def _one():
        out = []
        async for res in router.process_stream(text):
            out.append(res.text)
        return " ".join(t for t in out if t)
    return asyncio.run(_one())


def _turns(r):
    return [(t.role, t.text) for t in r.memory.turns]


def test_good_time_fast_path_commits_its_turn_and_survives_a_fragment():
    r = make_router()
    r._continuation_enabled = True
    reply = drive(r, "हां जी")
    assert "दो मिनट बात" in reply and r._good_time_spoken is True
    # §77: committed — nothing for a continuation fragment to supersede.
    assert r.has_pending_turn() is False
    assert r.rollback_pending_turn() is None
    before = _turns(r)
    assert any(role == "assistant" and "दो मिनट बात" in text for role, text in before)
    # The fragment that used to roll the beat back is now the next turn.
    reply2 = drive(r, "ठीक है?")
    assert r._good_time_spoken is True
    assert _turns(r)[:len(before)] == before
    assert "दो मिनट बात" not in reply2


def test_an_ordinary_turn_is_still_supersedable():
    r = make_router()
    r._continuation_enabled = True
    drive(r, "हां जी")
    drive(r, "मुझे एक 3 bhk चाहिए था")
    assert r.has_pending_turn() is True


def test_its_me_after_a_recheck_commits_too():
    r = make_router()
    r._continuation_enabled = True
    drive(r, "नहीं")                      # re-confirmation asked
    assert r._wrong_number_pending == 1
    reply = drive(r, "मैं ही हूँ")          # it is them: the good-time beat
    assert "दो मिनट बात" in reply and r._good_time_spoken is True
    assert r.has_pending_turn() is False
