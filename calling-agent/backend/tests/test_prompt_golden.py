"""
Golden-prompt harness — THE regression gate for every Copilot/AgentSpec phase.

Three guarantees, enforced on every PR:

1. **Prompt byte-identity**: the live Pramukh agent's rendered system prompt,
   for a fixed CallState at each of its 6 stages × {hi,en,gu} × {inbound,
   outbound}, matches the snapshot fixtures byte-for-byte.
2. **Load sweep**: every agents/<slug>/agent.yaml parses via
   AgentDefinition.from_yaml without raising.
3. **Stage-advance trace**: a scripted advancement trace through Pramukh's
   thresholds (1/8/6/99/99/99) is unchanged.

Regenerate fixtures ONLY for a deliberate, reviewed change:
    GOLDEN_REGEN=1 ../venv/bin/python -m pytest tests/test_prompt_golden.py
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import AGENTS_DIR

from app.agent_definition.schema import AgentDefinition
from app.voice.conversation import ConversationRouter

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "golden_prompts"
PRAMUKH_YAML = AGENTS_DIR / "pramukh" / "agent.yaml"
PRAMUKH_AGENT_YAML = AGENTS_DIR / "pramukh_agent" / "agent.yaml"

REGEN = os.environ.get("GOLDEN_REGEN", "") == "1"

# The ━━ RIGHT NOW ━━ band renders the live IST clock, so an un-pinned prompt
# changes every minute and no byte-identity fixture could ever hold. Freeze it
# for the whole module — a Wednesday inside the Mon-Fri 10:00-18:00 window, so
# the fixtures exercise the open-hours branch. Changing this instant means
# regenerating every fixture.
FROZEN_NOW = "2026-08-12T15:22:00"

STAGES = ["GREETING", "DISCOVERY", "PROJECT_MATCH",
          "INVITE_VISIT", "CONFIRMATION", "CLOSING"]
# The Copilot-built production sibling (DID-routed, takes live outbound
# calls). It has NO other prompt protection — the load sweep alone cannot
# catch a rendered-prompt regression, which is how an engine change could
# alter live behaviour with CI green (2026-07-15 audit).
PA_STAGES = ["CONFIRM_IDENTITY", "ASK_LOCATION_PREFERENCES",
             "ASK_PROPERTY_SIZE_PREFERENCES", "ASK_BUDGET_PREFERENCES",
             "RECOMMEND_PROPERTIES", "SUGGEST_SITE_VISIT",
             "SCHEDULE_SITE_VISIT", "NO_SITE_VISIT", "CLOSING"]
LANGS = ["hi", "en", "gu"]
DIRECTIONS = ["inbound", "outbound"]


@pytest.fixture(autouse=True)
def _freeze_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ━━ RIGHT NOW ━━ so the rendered prompt is reproducible."""
    monkeypatch.setenv("PROMPT_NOW_OVERRIDE", FROZEN_NOW)


def _make_router(stage: str, lang: str, direction: str,
                 yaml_path: Path = PRAMUKH_YAML) -> ConversationRouter:
    """Router over the live Pramukh YAML with a FIXED, deterministic CallState."""
    agent = AgentDefinition.from_yaml(yaml_path)
    router = ConversationRouter(agent_def=agent, default_language=lang)

    # Deterministic calendar: bypass bookings.json so live bookings can never
    # flip the fixture. The prompt still renders the full LIVE AVAILABILITY
    # block from the YAML's mock_slots.
    router._available_calendar_slots = (
        lambda: list(router._def.calendar.mock_slots or []))

    # Fixed mid-call state, identical across runs.
    router.state.stage = stage
    router.state.language = lang
    router.state.turn_count = 4
    router.memory.call_state.customer_name = "Ramesh"
    router.memory.call_state.customer_phone = "9876543210"
    router.state_mgr.set_externally("name", "Ramesh", source="system")
    router.state_mgr.set_externally("location", "Vapi", source="system")

    router.call_direction = direction
    if direction == "outbound":
        router.outbound_contact_name = "Ramesh Bhai"
        router.outbound_contact_phone = "9876543210"
    return router


def _fixture_path(stage: str, lang: str, direction: str,
                  slug: str = "pramukh") -> Path:
    return FIXTURE_DIR / f"{slug}__{stage}__{lang}__{direction}.txt"


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("stage", STAGES)
def test_pramukh_prompt_golden(stage: str, lang: str, direction: str) -> None:
    router = _make_router(stage, lang, direction)
    prompt = router._build_system_prompt()
    path = _fixture_path(stage, lang, direction)
    if REGEN or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prompt, encoding="utf-8")
        if not REGEN:
            pytest.skip(f"fixture created: {path.name} — rerun to enforce")
        return
    expected = path.read_text(encoding="utf-8")
    assert prompt == expected, (
        f"System prompt for stage={stage} lang={lang} direction={direction} "
        f"changed. If this is DELIBERATE, regenerate with GOLDEN_REGEN=1 and "
        f"review the fixture diff."
    )


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("stage", PA_STAGES)
def test_pramukh_agent_prompt_golden(stage: str, lang: str,
                                     direction: str) -> None:
    router = _make_router(stage, lang, direction,
                          yaml_path=PRAMUKH_AGENT_YAML)
    prompt = router._build_system_prompt()
    path = _fixture_path(stage, lang, direction, slug="pramukh_agent")
    if REGEN or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prompt, encoding="utf-8")
        if not REGEN:
            pytest.skip(f"fixture created: {path.name} — rerun to enforce")
        return
    expected = path.read_text(encoding="utf-8")
    assert prompt == expected, (
        f"pramukh_agent prompt for stage={stage} lang={lang} "
        f"direction={direction} changed. If DELIBERATE, regenerate with "
        f"GOLDEN_REGEN=1 and review the fixture diff."
    )


def test_all_agents_load_sweep() -> None:
    """Every agent YAML on disk must parse — no TypeError/KeyError on load."""
    yamls = sorted(AGENTS_DIR.glob("*/agent.yaml"))
    assert yamls, f"no agents found under {AGENTS_DIR}"
    failures: list[str] = []
    for path in yamls:
        try:
            AgentDefinition.from_yaml(path)
        except Exception as exc:  # noqa: BLE001 — report all, fail once
            failures.append(f"{path.parent.name}: {exc!r}")
    assert not failures, "agent load sweep failed:\n" + "\n".join(failures)


def test_pramukh_stage_trace() -> None:
    """Scripted turn-count advancement through Pramukh's thresholds.

    GREETING(1) → DISCOVERY(8) → PROJECT_MATCH(6) → INVITE_VISIT(99)...
    Discovery slots stay unfilled so early-advance never fires; each simulated
    turn increments discovery_turns then runs _maybe_advance_stage, exactly as
    the live turn loop does.
    """
    agent = AgentDefinition.from_yaml(PRAMUKH_YAML)
    router = ConversationRouter(agent_def=agent, default_language="hi")
    router.state.stage = "DISCOVERY"

    trace: list[str] = []
    for _turn in range(20):
        router.state.discovery_turns += 1
        router._maybe_advance_stage()
        trace.append(router.state.stage)

    expected = (["DISCOVERY"] * 7 + ["PROJECT_MATCH"] * 6
                + ["INVITE_VISIT"] * 7)
    assert trace == expected, f"stage trace changed: {trace}"


def test_pramukh_early_advance_on_filled_slots() -> None:
    """All required discovery slots filled → DISCOVERY advances immediately."""
    agent = AgentDefinition.from_yaml(PRAMUKH_YAML)
    router = ConversationRouter(agent_def=agent, default_language="hi")
    router.state.stage = "DISCOVERY"
    for name, spec in router.state_mgr.slots.items():
        if spec.required:
            router.state_mgr.set_externally(name, "test-value", source="system")
    router._maybe_advance_stage()
    assert router.state.stage == "PROJECT_MATCH"


def test_pramukh_appointment_advance() -> None:
    """appointment_time set → advance exactly one stage, once."""
    agent = AgentDefinition.from_yaml(PRAMUKH_YAML)
    router = ConversationRouter(agent_def=agent, default_language="hi")
    router.state.stage = "INVITE_VISIT"
    router.memory.booking.appointment_time = "Tomorrow at 11:00 AM"
    router._maybe_advance_stage()
    assert router.state.stage == "CONFIRMATION"
    router._maybe_advance_stage()  # 99-turn threshold: must NOT advance again
    assert router.state.stage == "CONFIRMATION"


def test_neutral_fallback_agent_builds_router() -> None:
    """The neutral fallback agent must always construct a working router."""
    from app.agent_definition.fallback_agent import get_neutral_fallback_agent
    agent = get_neutral_fallback_agent()
    router = ConversationRouter(agent_def=agent, default_language="hi")
    prompt = router._build_system_prompt()
    assert "Pramukh" not in prompt
    assert "Vishal" not in prompt
    # KeyError-prone response paths must all resolve for the fallback agent.
    rb = agent.response_bank
    for lang in ("en", "hi", "gu"):
        assert rb.identity_responses[lang]
        assert rb.decline_farewell[lang]
        assert rb.timeout_fallbacks[lang]


def test_router_requires_explicit_agent_def() -> None:
    with pytest.raises(ValueError):
        ConversationRouter()
