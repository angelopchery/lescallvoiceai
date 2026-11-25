"""
F3 — _maybe_advance_stage v2 semantics (declarative advance_when + explicit
confirm_person role).

Pins today's behaviour:
- advance_when {intent, max_turns}: intent match advances immediately; the
  max_turns OR-escape advances on turn N when the intent never fires;
- {intent} alone with a non-matching intent NEVER advances (advance_when
  replaces the legacy turn-count pacing entirely);
- bare {max_turns: N} does NOT advance before turn N (vacuous-True bug fixed);
- confirm_person: EXPLICIT role + affirmation + >=1 turn advances; a derived
  confirm_person role (legacy OPENING id, role='') must NOT trigger; a
  non-affirmation intent must NOT trigger.

No network, no LLM — _maybe_advance_stage called directly on a synthetic agent.
"""
from __future__ import annotations

from app.agent_definition.schema import AgentDefinition
from app.voice.conversation import ConversationRouter


def make_router(stages) -> ConversationRouter:
    agent = AgentDefinition.from_dict({
        "agent_id": "t_stage",
        "version": "1.0.0",
        "identity": {"agent_name": "Asha", "company_name": "TestCo"},
        "languages": {"supported": ["en", "hi"], "default": "en",
                      "tts_voices": {}},
        "call_flow": {"stages": stages},
        "response_bank": {"opening_greeting": "hi"},
    })
    router = ConversationRouter(agent_def=agent, default_language="en")
    router._available_calendar_slots = lambda: []
    return router


def three_stages(advance_when, first=None):
    s1 = {"id": "S1", "order": 0, "goal": "one", "turns_until_advance": 8,
          "advance_when": advance_when}
    if first:
        s1.update(first)
    return [
        s1,
        {"id": "S2", "order": 1, "goal": "two", "turns_until_advance": 8},
        {"id": "S3", "order": 2, "goal": "three", "turns_until_advance": 99},
    ]


# ── (a) intent + max_turns: intent match advances immediately ───────────────

def test_intent_match_advances_immediately():
    router = make_router(three_stages({"intent": "affirmation", "max_turns": 3}))
    router.state.stage = "S1"
    router.state.last_intent = "affirmation"
    router.state.discovery_turns = 1
    router._maybe_advance_stage()
    assert router.state.stage == "S2"
    assert router.state.discovery_turns == 0  # reset on advance


# ── (b) intent never fires → max_turns escape on turn 3 ─────────────────────

def test_max_turns_escape_when_intent_never_fires():
    router = make_router(three_stages({"intent": "affirmation", "max_turns": 3}))
    router.state.stage = "S1"
    router.state.last_intent = "general"
    for turn in (1, 2):
        router.state.discovery_turns = turn
        router._maybe_advance_stage()
        assert router.state.stage == "S1", f"advanced too early at turn {turn}"
    router.state.discovery_turns = 3
    router._maybe_advance_stage()
    assert router.state.stage == "S2"


# ── (c) intent-only condition with no escape NEVER advances ─────────────────

def test_intent_only_never_advances_without_match():
    router = make_router(three_stages({"intent": "affirmation"}))
    router.state.stage = "S1"
    router.state.last_intent = "general"
    for turn in range(1, 21):
        router.state.discovery_turns = turn
        router._maybe_advance_stage()
        assert router.state.stage == "S1", f"advanced at turn {turn}"


# ── (d) bare max_turns: no vacuous-True advance on turn 1 ───────────────────

def test_bare_max_turns_waits_for_turn_n():
    router = make_router(three_stages({"max_turns": 3}))
    router.state.stage = "S1"
    router.state.last_intent = "general"
    router.state.discovery_turns = 1
    router._maybe_advance_stage()
    assert router.state.stage == "S1"  # the vacuous-True bug would advance here
    router.state.discovery_turns = 3
    router._maybe_advance_stage()
    assert router.state.stage == "S2"


# ── (e) confirm_person role gate ────────────────────────────────────────────

def confirm_stages(role):
    return [
        {"id": "OPENING", "order": 0, "goal": "confirm", "role": role,
         "turns_until_advance": 8, "advance_when": {}},
        {"id": "S2", "order": 1, "goal": "two", "turns_until_advance": 8},
        {"id": "S3", "order": 2, "goal": "three", "turns_until_advance": 99},
    ]


def test_explicit_confirm_person_advances_on_affirmation():
    router = make_router(confirm_stages("confirm_person"))
    router.state.stage = "OPENING"
    router.state.last_intent = "affirmation"
    router.state.discovery_turns = 1
    router._maybe_advance_stage()
    assert router.state.stage == "S2"


def test_derived_confirm_person_role_does_not_trigger():
    # OPENING derives role=confirm_person via the legacy shim, but only an
    # EXPLICIT StageDefinition.role may trigger the affirmation advance.
    router = make_router(confirm_stages(""))
    router.state.stage = "OPENING"
    router.state.last_intent = "affirmation"
    router.state.discovery_turns = 1
    router._maybe_advance_stage()
    assert router.state.stage == "OPENING"


def test_confirm_person_requires_affirmation_intent():
    router = make_router(confirm_stages("confirm_person"))
    router.state.stage = "OPENING"
    router.state.last_intent = "question"
    router.state.discovery_turns = 1
    router._maybe_advance_stage()
    assert router.state.stage == "OPENING"
