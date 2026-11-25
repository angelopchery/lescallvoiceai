"""The copilot "Actions taken" digest — one plain-English line per turn.

Regression source (2026-08-01): the chip list under each copilot reply showed
raw tool names (`update_draft`, `mine_dump`) and internal refusal/error text
("REFUSED — call_flow.stages is replaced WHOLE…"), which non-technical users
could not read. `_actions_digest` replaces it; these tests pin that the line
stays jargon-free: no tool names, no config keys, no error internals.
"""
from __future__ import annotations

import sys

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


def _a(tool, ok=True, agent="", summary=""):
    return {"tool": tool, "summary": summary, "ok": ok, "agent": agent}


def test_single_update_is_one_plain_sentence(cop):
    # Agent doesn't exist on disk → the slug is prettified, never shown raw.
    line = cop._actions_digest([_a("update_agent", agent="nosuch_agent")])
    assert line == "Updated Nosuch Agent."


def test_related_edits_collapse_to_one_phrase(cop):
    line = cop._actions_digest([
        _a("update_stage", agent="x"), _a("update_stage", agent="x"),
        _a("add_stage", agent="x"), _a("update_slot", agent="x"),
    ])
    # Four stage edits + a slot edit → exactly two phrases, no repetition.
    assert line.lower().count("call flow") == 1
    assert "questions" in line
    assert "update_stage" not in line


def test_reads_only_say_nothing_was_changed(cop):
    line = cop._actions_digest([
        _a("list_agents"), _a("get_agent", agent="x"), _a("find_text", agent="x"),
    ])
    assert "nothing was changed" in line.lower()
    assert "get_agent" not in line and "find_text" not in line


def test_interview_notes_collapse(cop):
    line = cop._actions_digest([
        _a("update_draft"), _a("update_draft"), _a("mine_dump"), _a("skip_layer"),
    ])
    assert "noted" in line.lower()
    assert "update_draft" not in line and "mine_dump" not in line


def test_reads_vanish_when_a_change_landed(cop):
    line = cop._actions_digest([
        _a("get_agent", agent="x"),
        _a("update_agent", agent="x"),
    ])
    assert "checked" not in line.lower()
    assert "updated" in line.lower()


def test_unrecovered_failure_is_plain_no_error_text(cop):
    line = cop._actions_digest([
        _a("update_agent", ok=False, agent="x",
           summary="Couldn't update the agent: REFUSED — call_flow.stages is replaced WHOLE"),
    ])
    assert "didn't go through" in line.lower()
    assert "REFUSED" not in line and "call_flow" not in line


def test_confirmation_hold_is_not_a_failure(cop):
    line = cop._actions_digest([
        _a("propose_update", agent="x"),
        _a("apply_update", ok=False, agent="x",
           summary="Held until the user confirms — say 'build it'"),
    ])
    assert "go-ahead" in line.lower()
    assert "didn't go through" not in line.lower()


def test_mixed_success_and_failure_mentions_both(cop):
    line = cop._actions_digest([
        _a("update_agent", agent="x"),
        _a("activate_agent", ok=False, agent="x", summary="Couldn't activate: boom"),
    ])
    assert "updated" in line.lower()
    assert "one other change didn't go through" in line.lower()
    assert "boom" not in line


def test_every_mutating_tool_has_a_phrase(cop):
    # A mutating tool without a digest phrase would fall through to the
    # "Checked …" read bucket and tell the user nothing changed when it did.
    for tool in cop._MUTATING_TOOLS:
        assert tool in cop._DIGEST_DONE, tool


def test_empty_actions_empty_digest(cop):
    assert cop._actions_digest([]) == ""


# ── agent_links: Try/Open/Activate buttons only for agents that exist ───────
# Regression (2026-08-01): the pre-consent create_agent refusal carried the
# requested slug, so the chat showed "Try jethalal" buttons for an agent that
# had not been built yet — every one of them dead.

def test_agent_links_only_for_agents_on_disk(cop, tmp_path, monkeypatch):
    (tmp_path / "built_agent").mkdir()
    (tmp_path / "built_agent" / "agent.yaml").write_text("identity: {}\n")
    monkeypatch.setattr(cop, "_AGENTS_DIR", tmp_path)
    # Tenant visibility is builder-context-dependent; this test pins the
    # exists-on-disk gate, so let every slug through the visibility check.
    monkeypatch.setattr(cop, "_visible_slug", lambda s: True)
    links = cop._agent_links_for([
        _a("create_agent", ok=False, agent="ghost_draft",
           summary="Couldn't create the agent: Held until the user says yes"),
        _a("update_agent", agent="built_agent"),
        _a("update_agent", agent="built_agent"),   # deduped
        _a("update_draft"),                        # no agent at all
    ])
    assert links == ["built_agent"]


def test_agent_links_empty_when_nothing_exists(cop, tmp_path, monkeypatch):
    monkeypatch.setattr(cop, "_AGENTS_DIR", tmp_path)
    assert cop._agent_links_for([_a("create_agent", ok=False, agent="ghost")]) == []
