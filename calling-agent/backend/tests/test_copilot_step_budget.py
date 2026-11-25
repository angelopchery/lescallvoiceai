"""The step budget must not punish the Copilot for verifying its own work.

`_MAX_TOOL_ITERATIONS = 8` counted every LLM round, read or write. The system
prompt simultaneously ORDERS verification (find_text before and after a ban
sweep, preview_opening after a greeting edit) — so the mandated checks were
exactly what got cut when a turn ran long. A "change both directions" request
spends get_agent ×2 + update_agent ×2 before verifying anything, which is how
the observed capped turns looked.

Measured on the real corpus before the change: 299 tool-using turns, round
distribution {1:182, 2:37, 3:35, 4:23, 5:3, 6:7, 7:3, 8:9} — 3.0% hit the
cap, and every inspected truncation had landed all its writes and lost only
verification calls.

Also pinned here: the cap reply must be plain English. It used to join the
raw internal summaries, which is how a user read
"Done so far: remove slot email in LesCall; remove slot email in LesCall;
update stage MEETING_BOOKING: collect, goal in LesCall; …".
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from conftest import REPO_ROOT


@pytest.fixture()
def cop(tmp_path, monkeypatch):
    monkeypatch.setenv("LESCALL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    dash = str(REPO_ROOT / "dashboard")
    if dash not in sys.path:
        sys.path.insert(0, dash)
    import agent_copilot_api as cop_mod
    return cop_mod


# ── the budget rules ────────────────────────────────────────────────────────

def test_reads_are_free_and_writes_are_not(cop):
    assert "get_agent" in cop._READ_ONLY_TOOLS
    assert "preview_opening" in cop._READ_ONLY_TOOLS
    assert "find_text" in cop._READ_ONLY_TOOLS
    assert "list_controls" in cop._READ_ONLY_TOOLS
    assert not (cop._READ_ONLY_TOOLS & cop._MUTATING_TOOLS)


def test_a_read_loop_still_terminates(cop):
    """Free reads need their own brake or the ceiling is the only stop."""
    assert cop._MAX_TOOL_ROUNDS > cop._MAX_TOOL_ITERATIONS
    assert cop._MAX_IDENTICAL_READS >= 1


# ── the cap reply ───────────────────────────────────────────────────────────

def test_cap_reply_is_plain_english_and_deduped(cop, monkeypatch):
    """Two twins editing the same display name produced doubled clauses."""
    monkeypatch.setattr(cop, "_digest_agent_name", lambda s: "LesCall")
    actions = [
        {"tool": "update_stage", "ok": True, "agent": "lescall-inbound",
         "summary": "update stage MEETING_BOOKING: collect, goal in LesCall"},
        {"tool": "update_stage", "ok": True, "agent": "lescall-outbound",
         "summary": "update stage MEETING_BOOKING: collect, goal in LesCall"},
        {"tool": "update_agent", "ok": True, "agent": "lescall-inbound",
         "summary": "Updated LesCall — rules & compliance"},
    ]
    digest = cop._actions_digest(actions)
    assert "update stage" not in digest        # no raw internal summary
    assert "MEETING_BOOKING" not in digest     # no config keys
    assert digest.count("call flow") == 1      # deduped
    # …but the user is still told BOTH directions moved.
    assert "inbound and outbound" in digest


def test_digest_is_silent_about_direction_for_a_single_agent(cop, monkeypatch):
    monkeypatch.setattr(cop, "_digest_agent_name", lambda s: "LesCall")
    digest = cop._actions_digest([
        {"tool": "update_agent", "ok": True, "agent": "lescall-inbound",
         "summary": "Updated LesCall"}])
    assert "inbound and outbound" not in digest


def test_remaining_hint_reports_a_pending_change(cop, monkeypatch):
    monkeypatch.setattr(cop, "_agent_display_name", lambda s: "LesCall")
    hint = cop._remaining_hint({"pending_changes": {"lescall-inbound": {}}}, [])
    assert "waiting for your go-ahead" in hint


def test_remaining_hint_invents_nothing(cop):
    """An empty string is the honest answer when the server cannot tell."""
    assert cop._remaining_hint({}, []) == ""


def test_remaining_hint_flags_an_unedited_twin(cop, monkeypatch, tmp_path):
    import agent_builder_api as builder
    monkeypatch.setattr(builder, "counterpart_of",
                        lambda s: "demo-outbound" if s.endswith("-inbound")
                        else "demo-inbound")
    (tmp_path / "demo-outbound").mkdir()
    (tmp_path / "demo-outbound" / "agent.yaml").write_text("agent_id: demo\n")
    monkeypatch.setattr(cop, "_AGENTS_DIR", tmp_path)
    hint = cop._remaining_hint({}, [
        {"tool": "update_agent", "ok": True, "agent": "demo-inbound",
         "summary": "Updated Demo"}])
    assert "outbound" in hint


# ── the twin mirror ─────────────────────────────────────────────────────────

def test_direction_specific_paths_are_refused_on_the_twin(cop):
    """Mirroring a greeting or an outbound.* field would erase exactly what
    makes the two directions different."""
    for p in ("outbound.opening_greeting",
              "response_bank.opening_greeting", "call_direction"):
        assert p.startswith(cop._DIRECTION_SPECIFIC_PREFIXES) or \
            p == "call_direction", p
