"""
Call-start preflight tests (F6).

Incident B: a deterministic prompt-build TypeError was swallowed per-turn into
"Sorry, could you repeat that?" — three dead calls, no alert. preflight_agent
renders every stage × language prompt on throwaway routers BEFORE the greeting
so the bridge can swap to the neutral fallback agent instead.

The bridge (conversation_call_bridge._build_agent_stack) branches on the exact
return contract tested here: 'ok' keeps the loaded agent; anything else takes
the SAME degradation path as a load failure (agent_def=None →
get_neutral_fallback_agent()). The bridge module itself is not imported — a
live watchfiles thread crashes the interpreter at exit (pre-existing), so the
preflight logic lives engine-side and is tested directly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import app.voice.prompt_blocks as pb
from app.agent_definition.fallback_agent import get_neutral_fallback_agent
from app.agent_definition.preflight import preflight_agent
from app.agent_definition.schema import AgentDefinition

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "seven_step_agent.yaml"


def _agent() -> AgentDefinition:
    return AgentDefinition.from_yaml(FIXTURE)


def test_healthy_agent_is_ok():
    assert preflight_agent(_agent()) == "ok"


def test_prompt_build_failure_returns_stage_marker(monkeypatch):
    """Incident-B shape: a prompt-block helper raising TypeError per call."""
    def _boom(agent_def):
        raise TypeError("sequence item 3: expected str instance, NoneType found")

    monkeypatch.setattr(pb, "identity_block", _boom)
    health = preflight_agent(_agent(), slug="broken")
    # First stage of the first supported language fails the render.
    assert health == "preflight_failed:OPENING"
    # ...and it is exactly the non-'ok' marker the bridge's fallback-swap
    # branch keys on.
    assert health != "ok"
    assert health.startswith("preflight_failed:")


def test_failure_names_the_broken_stage(monkeypatch):
    """A stage-goal bug is attributed to THAT stage, not the first one."""
    real = pb.match_rules_block

    def _boom_on_recommend(agent_def, state, stage_role, **kw):
        if stage_role == "recommend":
            raise TypeError("boom in recommend")
        return real(agent_def, state, stage_role, **kw)

    monkeypatch.setattr(pb, "match_rules_block", _boom_on_recommend)
    assert preflight_agent(_agent()) == "preflight_failed:RECOMMEND"


def test_languages_override_and_fallback():
    agent = _agent()
    # Explicit language list is honored (a single healthy language → ok).
    assert preflight_agent(agent, languages=["en"]) == "ok"


def test_neutral_fallback_agent_passes_preflight():
    """The swap TARGET must itself always preflight clean — otherwise a
    degraded call would have nowhere left to go."""
    assert preflight_agent(get_neutral_fallback_agent()) == "ok"


def test_preflight_never_raises(monkeypatch):
    """Even a constructor-level failure comes back as a marker, not a raise."""
    def _boom(agent_def):
        raise RuntimeError("total prompt meltdown")

    monkeypatch.setattr(pb, "personality_block", _boom)
    health = preflight_agent(_agent())
    assert health.startswith("preflight_failed:")


def test_preflight_uses_throwaway_routers_only(monkeypatch):
    """preflight must not touch any pre-existing router/session state — it
    builds its own routers from the definition alone."""
    from app.voice.conversation import ConversationRouter

    agent = _agent()
    live = ConversationRouter(agent_def=agent, default_language="hi")
    live.state.stage = "OPENING"
    live.state.turn_count = 7
    before = (live.state.stage, live.state.turn_count, live.state.language)

    assert preflight_agent(agent) == "ok"

    assert (live.state.stage, live.state.turn_count,
            live.state.language) == before
