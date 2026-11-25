"""
Pramukh Group agent loader.

Phase 1: loads from agents/pramukh/agent.yaml.
The YAML file is the single source of truth — edit it to change agent behaviour.
"""

from __future__ import annotations

from pathlib import Path

from .schema import AgentDefinition

_YAML_PATH = Path(__file__).resolve().parents[3] / "agents" / "pramukh" / "agent.yaml"

_PRAMUKH_AGENT: AgentDefinition | None = None


def get_pramukh_agent() -> AgentDefinition:
    """Return the shared Pramukh Group agent definition (lazy singleton)."""
    global _PRAMUKH_AGENT
    if _PRAMUKH_AGENT is None:
        _PRAMUKH_AGENT = AgentDefinition.from_yaml(_YAML_PATH)
    return _PRAMUKH_AGENT
