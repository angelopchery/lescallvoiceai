"""Regression gate for the 2026-09-08 Jai Mistry "pitch" call (RUNBOOK §36).

A live call showed the scripted script fidelity breaking down in three
distinct ways, once the 2026-09-08 stage-wiring fix (RUNBOOK §35) was
already live:

1. A caller's paraphrase of a scripted objection ("tell me about your
   agency" for the authored "what do you do") got an improvised pitch
   instead of the owner's exact verbatim line — even though that exact
   line was sitting in the prompt's OBJECTION HANDLING section the whole
   time. Root cause: `_match_objection_playbook()` is only ever consulted
   from inside the `intent == "objection"` branch, but a neutral FAQ
   question ("what do you do", "are you an AI", "how did you get my
   number") never classifies as "objection" — detect_intent correctly
   calls it "question"/"identity_question"/"general". These playbook
   entries could never fire deterministically for the very callers they
   were written for.

2. Even when a verbatim line WAS spoken correctly, the model then padded
   it with an invented follow-up ("...Is there anything else I can help
   you with?"), leading a real call into a repeat-and-loop.

3. CALL MISSION's blanket "every turn must move toward this mission"
   competed with a closing-stage goal, and won — the model asked one more
   qualifying question ("what's your biggest challenge bringing leads?")
   instead of using the actual scripted acknowledgment + close it had just
   been shown.

Fixes, all platform-wide (not Jai-Mistry-specific):
  * A new intent-independent FAQ shortcut checks the objection playbook
    whenever intent != "objection" — genuine declines (NOT_INTERESTED,
    "busy", …) are untouched, still routed through the existing
    decline-counting path.
  * `script_fidelity_block()` (prompt_blocks.py) — rendered every turn,
    immediately before CURRENT GOAL — tells the model a quoted "verbatim"/
    "say ONLY" line is a literal contract, and forbids padding it with an
    unscripted lead-in or follow-up.
  * `mission_block()` now explicitly defers to a closing/specific-line
    CURRENT GOAL instead of treating "keep qualifying" as always correct.
"""
from __future__ import annotations

import asyncio

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice import prompt_blocks as pb
from app.voice.conversation import ConversationRouter


@pytest.fixture(autouse=True)
def _no_network_llm(stub_llm):
    """Any turn that reaches the LLM gets the fixed stub reply — proves
    which turns are (and are not) handled deterministically."""


def _router(**over) -> ConversationRouter:
    d = {
        "agent_id": "t_faq",
        "version": "1.0.0",
        "identity": {"agent_name": "Asha", "company_name": "TestCo"},
        "languages": {"supported": ["en"], "default": "en", "tts_voices": {}},
        "call_flow": {
            "call_goal": "Qualify the caller's needs before booking a visit.",
            "stages": [
                {"id": "S1", "order": 0, "goal": "Ask their name.",
                 "turns_until_advance": 8},
                {"id": "S2", "order": 1, "goal": "Continue.",
                 "turns_until_advance": 8},
            ],
        },
        "response_bank": {
            "opening_greeting": "hi",
            "objection_playbook": [
                {"id": "WHAT_DO_YOU_DO",
                 "patterns": ["what do you do", "tell me about your company"],
                 "answer": {"en": "We help builders find qualified buyers."}},
            ],
        },
    }
    d.update(over)
    agent = AgentDefinition.from_dict(d)
    r = ConversationRouter(agent_def=agent, default_language="en")
    r.state.stage = "S2"  # past GREETING, where the objection gate is live
    return r


def drive(router: ConversationRouter, text: str) -> str:
    async def _one():
        out = []
        async for res in router.process_stream(text):
            out.append(res.text)
        return " ".join(t for t in out if t)

    return asyncio.run(_one())


# ---------------------------------------------------------------------------
# 1. The FAQ shortcut fires on a paraphrase, independent of intent
# ---------------------------------------------------------------------------

def test_a_paraphrased_faq_question_gets_the_verbatim_answer():
    r = _router()
    resp = drive(r, "so tell me about your company, what's the deal")

    assert resp == "We help builders find qualified buyers."


def test_the_exact_authored_trigger_still_works():
    r = _router()
    resp = drive(r, "What do you do exactly?")

    assert resp == "We help builders find qualified buyers."


# ---------------------------------------------------------------------------
# 2. A genuine objection is completely unaffected — same path, same
#    decline-counting, as before this fix.
# ---------------------------------------------------------------------------

def test_a_genuine_objection_still_increments_decline_count():
    r = _router()
    assert r.state.decline_count == 0

    drive(r, "I'm not interested, please stop calling")

    assert r.state.decline_count == 1


def test_a_genuine_objection_does_not_get_a_faq_answer():
    """No playbook entry matches "not interested" here — must fall back to
    the platform's hardcoded objection line, never the stub LLM reply."""
    r = _router()
    resp = drive(r, "I'm not interested right now, thanks")

    assert "I understand" in resp
    assert resp != "Understood, I have made a note of that."  # the stub reply


def test_an_objection_that_also_matches_a_playbook_pattern_still_counts():
    """Edge case: if a decline-shaped phrase happens to ALSO match a
    playbook trigger, it must still be classified as objection=True and
    counted — the FAQ shortcut only ever engages when intent != objection,
    so there is no path where a real decline goes uncounted."""
    r = _router(response_bank={
        "opening_greeting": "hi",
        "objection_playbook": [
            {"id": "NOT_INTERESTED", "patterns": ["not interested"],
             "answer": {"en": "No problem, thanks for your time."}},
        ],
    })
    drive(r, "not interested, thanks")

    assert r.state.decline_count == 1


# ---------------------------------------------------------------------------
# 3. SCRIPT FIDELITY block — always rendered, tells the model quoted lines
#    are literal and forbids padding them.
# ---------------------------------------------------------------------------

def test_script_fidelity_block_demands_word_for_word_reproduction():
    lines = "\n".join(pb.script_fidelity_block())

    assert "WORD FOR WORD" in lines
    assert "Say ONLY that line" in lines
    assert "matches on MEANING, not exact wording" in lines


def test_script_fidelity_block_is_in_the_live_system_prompt():
    r = _router()
    prompt = r._build_system_prompt()

    assert "━━ SCRIPT FIDELITY ━━" in prompt
    # Must appear AFTER (i.e. closer to the caller's turn than) OBJECTION
    # HANDLING and CURRENT GOAL's own header, per the recency reasoning in
    # RUNBOOK §36.
    assert prompt.index("━━ SCRIPT FIDELITY ━━") < prompt.index("━━ CURRENT GOAL ━━")


# ---------------------------------------------------------------------------
# 4. CALL MISSION defers to a closing/specific-line CURRENT GOAL
# ---------------------------------------------------------------------------

def test_call_mission_defers_to_current_goal_when_closing():
    lines = "\n".join(pb.mission_block(
        AgentDefinition.from_dict({
            "agent_id": "t", "version": "1.0.0",
            "identity": {"agent_name": "A", "company_name": "C"},
            "languages": {"supported": ["en"], "default": "en", "tts_voices": {}},
            "call_flow": {"call_goal": "Qualify the caller.",
                          "stages": [{"id": "S1", "order": 0, "goal": "g"}]},
            "response_bank": {"opening_greeting": "hi"},
        })
    ))

    assert "UNLESS CURRENT GOAL" in lines
    assert "one more thing to ask" in lines
