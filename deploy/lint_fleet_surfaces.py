#!/usr/bin/env python3
"""Fleet lint: which agents carry shadowed/dormant greeting fields?

For every agent on disk, resolves what the next call ACTUALLY opens with
(via the engine-driven spoken_surfaces resolver) and reports any greeting
field that is not what callers hear. Read-only; exit code 0 always (this is
a report, not a gate) unless --strict, where shadowed (not dormant)
surfaces exit 1.

Run from anywhere:
    calling-agent/venv/bin/python deploy/lint_fleet_surfaces.py [--strict]

Born from the 2026-08-01 Jethalal incident — an edit to
response_bank.opening_greeting on an outbound agent was applied, reported
done, and never spoken (outbound calls open with outbound.opening_greeting).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "calling-agent" / "backend"))

from app.agent_definition.registry import AgentRegistry          # noqa: E402
from app.agent_definition.spoken_surfaces import (               # noqa: E402
    resolve_spoken_surfaces,
)

AGENTS_DIR = REPO / "calling-agent" / "agents"


def main() -> int:
    strict = "--strict" in sys.argv[1:]
    reg = AgentRegistry()
    shadowed = 0
    dormant = 0
    for yaml_path in sorted(AGENTS_DIR.glob("*/agent.yaml")):
        slug = yaml_path.parent.name
        try:
            agent = reg.get(slug)
            sr = resolve_spoken_surfaces(agent)
        except Exception as exc:  # noqa: BLE001 — report and continue
            print(f"⚠ {slug}: resolve failed: {exc}")
            continue
        if not sr["shadowed"]:
            continue
        dl = sr["default_language"]
        opening = (sr["openings"].get(dl) or {})
        print(f"\n{slug}  ({sr['direction']}, default {dl})")
        print(f"  opens with: {opening.get('line', '')!r}"
              f"  [from {opening.get('source', '?')}]")
        for s in sr["shadowed"]:
            kind = "DORMANT" if "dormant" in s["note"] else "SHADOWED"
            if kind == "SHADOWED":
                shadowed += 1
            else:
                dormant += 1
            print(f"  {kind}: {s['path']} — {s['note']}")
    print(f"\n{shadowed} shadowed surface(s), {dormant} dormant surface(s) "
          f"across the fleet.")
    return 1 if strict and shadowed else 0


if __name__ == "__main__":
    sys.exit(main())
