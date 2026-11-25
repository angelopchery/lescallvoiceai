"""━━ RIGHT NOW ━━ — temporal grounding in the system prompt (D19).

Until this band existed the prompt carried business hours, possession dates
and a slot list, but never said what day it was. Live-call evidence: across
the 515 recorded calls the agent named a relative day ("tomorrow", "kal",
"Monday") in 451 turns and a real date in 21, and it described a "Possession
December 2025" catalog line as "near-final stage" eight months after that
date had passed.

These tests pin the three things that make the band worth its ~230 tokens a
turn: the clock is IST (the host runs UTC), the seven-day horizon resolves
every weekday name a caller can say, and the band renders BELOW the prompt
cache's static-prefix marker so it never invalidates the cached prefix.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import AGENTS_DIR

from app.agent_definition.schema import AgentDefinition
from app.voice import prompt_blocks as pb
from app.voice.conversation import ConversationRouter, _now_ist

IST = timezone(timedelta(hours=5, minutes=30))
PRAMUKH_YAML = AGENTS_DIR / "pramukh" / "agent.yaml"
# Carries scheduling.days + scheduling.window, so it exercises the
# office-open branch that pramukh's slotless config skips.
# Frozen snapshot (2026-09-24), never the live, owner-editable YAML.
QUALIFIER_YAML = Path(__file__).parent / "fixtures" / "prakhar_outbound_2026_09_24.yaml"


def _agent(path: Path) -> AgentDefinition:
    return AgentDefinition.from_yaml(path)


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=IST)


# ---------------------------------------------------------------------------
# The clock itself
# ---------------------------------------------------------------------------

def test_now_is_ist_not_host_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host runs UTC; a caller in India must not be told it is 09:00
    when their clock says 14:30."""
    monkeypatch.delenv("PROMPT_NOW_OVERRIDE", raising=False)
    assert _now_ist().utcoffset() == timedelta(hours=5, minutes=30)


def test_override_freezes_the_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPT_NOW_OVERRIDE", "2026-08-12T15:22:00")
    now = _now_ist()
    assert (now.year, now.month, now.day, now.hour, now.minute) == (
        2026, 8, 12, 15, 22)


def test_override_with_explicit_offset_converts_to_ist(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPT_NOW_OVERRIDE", "2026-08-12T10:00:00+00:00")
    assert _now_ist().hour == 15  # 10:00 UTC == 15:30 IST


def test_garbage_override_falls_back_to_the_real_clock(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo in the env must never pin a live agent to a wrong date."""
    monkeypatch.setenv("PROMPT_NOW_OVERRIDE", "not-a-date")
    delta = abs(_now_ist() - datetime.now(IST))
    assert delta < timedelta(seconds=5)


# ---------------------------------------------------------------------------
# The rendered band
# ---------------------------------------------------------------------------

def test_band_states_todays_weekday_date_and_time() -> None:
    lines = pb.now_block(_agent(PRAMUKH_YAML), _at("2026-08-12T15:22:00"))
    assert "It is 3:22 PM on Wednesday, 12 August 2026." in lines


@pytest.mark.parametrize("iso,expected", [
    ("2026-08-12T00:05:00", "12:05 AM"),   # midnight is 12 AM, not 0 AM
    ("2026-08-12T12:00:00", "12:00 PM"),   # noon is 12 PM, not 0 PM
    ("2026-08-12T09:07:00", "9:07 AM"),
    ("2026-08-12T23:59:00", "11:59 PM"),
])
def test_twelve_hour_clock_edges(iso: str, expected: str) -> None:
    body = "\n".join(pb.now_block(_agent(PRAMUKH_YAML), _at(iso)))
    assert f"It is {expected} on " in body


def test_seven_day_horizon_names_every_weekday_a_caller_can_say() -> None:
    """"Monday" must be resolvable without the model doing calendar
    arithmetic — that arithmetic is exactly what it gets wrong."""
    body = "\n".join(pb.now_block(_agent(PRAMUKH_YAML), _at("2026-08-12T15:22:00")))
    horizon = next(ln for ln in body.split("\n") if "next seven days" in ln)
    for day in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
        assert day in horizon, f"{day} missing from the horizon: {horizon}"
    assert "today Wed 12 Aug" in horizon
    assert "tomorrow Thu 13 Aug" in horizon


@pytest.mark.parametrize("iso,expected", [
    ("2026-08-12T12:00:00", "Monday the 17th"),   # midweek → this coming Mon
    ("2026-08-17T12:00:00", "Monday the 24th"),   # ON a Monday → NEXT Monday
    ("2026-08-31T12:00:00", "Monday the 7th"),    # rolls into September
    ("2026-12-31T12:00:00", "Monday the 4th"),    # rolls into 2027
    ("2026-08-16T12:00:00", "Monday the 17th"),   # Sunday → tomorrow
    ("2026-08-01T12:00:00", "Monday the 3rd"),    # ordinal: 3rd, not 3th
])
def test_worked_example_never_points_at_today_or_a_bad_ordinal(
        iso: str, expected: str) -> None:
    body = "\n".join(pb.now_block(_agent(PRAMUKH_YAML), _at(iso)))
    assert f'"{expected}"' in body


def test_band_forbids_past_days_and_stale_prompt_dates() -> None:
    body = "\n".join(pb.now_block(_agent(PRAMUKH_YAML), _at("2026-08-12T15:22:00")))
    assert "already past" in body
    assert "ALREADY HAPPENED" in body


# ---------------------------------------------------------------------------
# Office open/closed — only when the YAML actually says
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("iso,state", [
    ("2026-08-12T10:00:00", "OPEN"),     # window opens at 10:00, inclusive
    ("2026-08-12T17:59:00", "OPEN"),
    ("2026-08-12T09:59:00", "CLOSED"),
    ("2026-08-12T18:00:00", "CLOSED"),   # window closes at 18:00, exclusive
    ("2026-08-15T12:00:00", "CLOSED"),   # Saturday
    ("2026-08-16T12:00:00", "CLOSED"),   # Sunday
])
def test_office_open_state_tracks_the_configured_window(
        iso: str, state: str) -> None:
    # The window is pinned here, not read from the owner's live YAML: Prakhar
    # moved to seven-day 10:00-19:00 hours on 2026-09-24 and this test is
    # about the OPEN/CLOSED arithmetic, not about one business's hours.
    agent = _agent(QUALIFIER_YAML)
    agent.call_flow.scheduling = {
        "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        "window": "10:00-18:00",
    }
    body = "\n".join(pb.now_block(agent, _at(iso)))
    assert f"the office is {state}" in body


def test_no_office_line_when_the_agent_declares_no_hours() -> None:
    """Silence beats a guess: an unconfigured window must not render as a
    confident OPEN, which the model would state to the caller as fact."""
    body = "\n".join(pb.now_block(_agent(PRAMUKH_YAML), _at("2026-08-12T15:22:00")))
    assert "the office is" not in body


def test_unparseable_window_renders_no_office_line() -> None:
    agent = _agent(QUALIFIER_YAML)
    agent.call_flow.scheduling = {"days": ["Monday"], "window": "whenever"}
    body = "\n".join(pb.now_block(agent, _at("2026-08-17T12:00:00")))
    assert "the office is" not in body


# ---------------------------------------------------------------------------
# Placement — the band must not cost a cache hit
# ---------------------------------------------------------------------------

def test_band_renders_below_the_static_prefix_marker(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The band changes every turn. Above ━━ CALL STATE ━━ it would
    invalidate the cached prefix on every turn of every call."""
    from app.voice import prompt_cache

    monkeypatch.setenv("PROMPT_NOW_OVERRIDE", "2026-08-12T15:22:00")
    router = ConversationRouter(agent_def=_agent(PRAMUKH_YAML),
                                default_language="en")
    prompt = router._build_system_prompt()
    assert prompt.index(prompt_cache.STATIC_PREFIX_MARKER) < \
        prompt.index("━━ RIGHT NOW ━━")


def test_static_prefix_is_byte_stable_as_the_clock_moves(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Two turns a minute apart must share an identical cacheable prefix."""
    from app.voice import prompt_cache

    def _prefix(iso: str) -> str:
        monkeypatch.setenv("PROMPT_NOW_OVERRIDE", iso)
        router = ConversationRouter(agent_def=_agent(PRAMUKH_YAML),
                                    default_language="en")
        p = router._build_system_prompt()
        return p[:p.index(prompt_cache.STATIC_PREFIX_MARKER)]

    assert _prefix("2026-08-12T15:22:00") == _prefix("2026-08-12T15:23:00")


def test_the_greeting_word_and_the_band_agree_on_the_day(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Both read _now_ist(); a caller must never hear "good morning" from a
    prompt whose RIGHT NOW band says it is the afternoon."""
    from app.voice.conversation import _time_of_day_word

    monkeypatch.setenv("PROMPT_NOW_OVERRIDE", "2026-08-12T15:22:00")
    assert _time_of_day_word("") == "afternoon"
    body = "\n".join(pb.now_block(_agent(PRAMUKH_YAML), _now_ist()))
    assert "3:22 PM" in body


def test_band_is_present_in_a_live_rendered_prompt(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPT_NOW_OVERRIDE", "2026-08-12T15:22:00")
    router = ConversationRouter(agent_def=_agent(QUALIFIER_YAML),
                                default_language="en")
    router.call_direction = "outbound"
    prompt = router._build_system_prompt()
    assert "━━ RIGHT NOW ━━" in prompt
    # The whole point: a real, unambiguous date the model can reason from.
    assert re.search(r"It is [\d:]+ [AP]M on \w+day, \d+ \w+ 2026\.", prompt)
