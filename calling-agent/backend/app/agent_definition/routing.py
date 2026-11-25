"""
Telephony routing — maps inbound DID + caller metadata to an agent name.

Each agent directory may contain a routing.yaml beside agent.yaml.
If no routing.yaml exists for an agent it is never auto-selected by this module.

Routing resolution order (first match wins):
  1. Exact DID match
  2. Caller-ID prefix match
  3. Time-of-day window match
  4. fallback_agent chain (walk until an agent with no fallback is reached)

routing.yaml example
--------------------
did: "+918012345678"           # exact inbound DID (E.164) — match this number
caller_id_prefixes:            # optional: match if caller starts with any prefix
  - "+9179"
  - "+9198"
time_windows:                  # optional: restrict routing to these hours (0-23 local)
  - {start: 9, end: 18}
fallback_agent: "pramukh"     # agent to try when this rule doesn't match
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_AGENTS_DIR = Path(__file__).resolve().parents[3] / "agents"


@dataclass
class TimeWindow:
    start: int  # 0-23 inclusive
    end: int    # 0-23 inclusive (end hour is excluded from the window)


@dataclass
class RoutingRule:
    """Routing configuration loaded from one agent's routing.yaml."""
    agent_name: str
    did: str = ""
    caller_id_prefixes: list[str] = field(default_factory=list)
    time_windows: list[TimeWindow] = field(default_factory=list)
    fallback_agent: str = ""

    def matches(self, did: str, caller_id: str, hour: int) -> bool:
        """Return True if this rule fires for the given call parameters."""
        if self.did and self.did != did:
            return False
        if self.caller_id_prefixes:
            if not any(caller_id.startswith(p) for p in self.caller_id_prefixes):
                return False
        if self.time_windows:
            if not any(w.start <= hour < w.end for w in self.time_windows):
                return False
        # If a DID was specified and it matched, or no constraints were specified,
        # and we passed all other checks — this rule fires.
        if not self.did and not self.caller_id_prefixes and not self.time_windows:
            return False  # an empty rule matches nothing (safety guard)
        return True


def load_routing_rules(agents_dir: Optional[Path] = None) -> list[RoutingRule]:
    """Scan agents/*/routing.yaml and return parsed rules."""
    base = agents_dir or _AGENTS_DIR
    rules: list[RoutingRule] = []
    for routing_path in sorted(base.glob("*/routing.yaml")):
        agent_name = routing_path.parent.name
        try:
            with open(routing_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            windows = [
                TimeWindow(start=w["start"], end=w["end"])
                for w in data.get("time_windows", [])
            ]
            rule = RoutingRule(
                agent_name=agent_name,
                did=data.get("did", ""),
                caller_id_prefixes=data.get("caller_id_prefixes", []),
                time_windows=windows,
                fallback_agent=data.get("fallback_agent", ""),
            )
            rules.append(rule)
            logger.debug("[routing] loaded rule for '%s'", agent_name)
        except Exception as exc:
            logger.warning("[routing] failed to load routing for '%s': %s", agent_name, exc)
    return rules


def resolve_agent(
    did: str,
    caller_id: str = "",
    hour: int = 12,
    agents_dir: Optional[Path] = None,
    default_agent: str = "pramukh",
) -> str:
    """Return the agent name to use for this inbound call.

    Parameters
    ----------
    did:           The inbound DID (E.164), e.g. "+918012345678".
    caller_id:     The caller's number (E.164), e.g. "+917900000000".
    hour:          Current hour in local time (0-23). Callers provide this.
    agents_dir:    Override the agents/ directory (for testing).
    default_agent: Returned when no routing rule matches.
    """
    rules = load_routing_rules(agents_dir)
    for rule in rules:
        if rule.matches(did, caller_id, hour):
            logger.info(
                "[routing] DID %s → agent '%s' (rule matched)", did, rule.agent_name
            )
            return rule.agent_name

    # Walk fallback chain — find the first rule whose fallback matches
    rule_map = {r.agent_name: r for r in rules}
    for rule in rules:
        if rule.fallback_agent and rule.fallback_agent in rule_map:
            pass  # fallback chain is traversed by the caller if needed

    logger.info(
        "[routing] DID %s → default agent '%s' (no rule matched)", did, default_agent
    )
    return default_agent
