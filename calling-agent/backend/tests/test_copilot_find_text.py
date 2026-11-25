"""find_text — the read-only sweep-verify tool for banned-phrase edits.

The copilot must never claim a phrase is gone without a 0-hit find_text
result; these tests pin the tool's path reporting, read-only guarantee, and
the replace_mentions → find_text verification loop end-to-end.
"""
from __future__ import annotations

import asyncio
import shutil
import sys

import pytest
import yaml

from conftest import AGENTS_DIR, REPO_ROOT


@pytest.fixture()
def copilot(tmp_path, monkeypatch):
    monkeypatch.setenv("LESCALL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    dash = str(REPO_ROOT / "dashboard")
    if dash not in sys.path:
        sys.path.insert(0, dash)
    import agent_builder_api as builder
    import agent_copilot_api as cop
    agents_dir = tmp_path / "agents"
    (agents_dir / "pramukh").mkdir(parents=True)
    shutil.copy(AGENTS_DIR / "pramukh" / "agent.yaml",
                agents_dir / "pramukh" / "agent.yaml")
    monkeypatch.setattr(builder, "_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(cop, "_AGENTS_DIR", agents_dir)
    builder._claim_slug("pramukh", builder._ctx_owner(), "Pramukh")
    from app.agent_definition.registry import agent_registry
    agent_registry.invalidate_all()
    return cop, agents_dir


def _run(coro):
    return asyncio.run(coro)


GUARD = {"user_turns": 1, "created_this_turn": False,
         "session": {"messages": []}, "user_message": "check"}


def test_find_text_reports_paths_and_snippets(copilot):
    cop, agents_dir = copilot
    tagline = yaml.safe_load(
        (agents_dir / "pramukh" / "agent.yaml").read_text(
            encoding="utf-8"))["identity"]["company_tagline"]
    assert tagline, "fixture assumption: pramukh has a company_tagline"
    res, _s, ok, mutated = _run(cop._execute_tool(
        "find_text", {"name": "pramukh", "find": tagline}, GUARD))
    assert ok and mutated is False
    assert res["status"] == "ok"
    assert res["matches"] >= 1
    paths = [h["path"] for h in res["hits"]]
    assert "identity.company_tagline" in paths
    hit = next(h for h in res["hits"] if h["path"] == "identity.company_tagline")
    assert tagline in hit["snippet"]


def test_find_text_is_case_insensitive_and_keys_list_items(copilot):
    cop, agents_dir = copilot
    stage0 = yaml.safe_load(
        (agents_dir / "pramukh" / "agent.yaml").read_text(
            encoding="utf-8"))["call_flow"]["stages"][0]
    # A word from stage 0's goal, upper-cased — must still hit, and the path
    # must be keyed by the stage id, not a bare index.
    word = next(w for w in str(stage0["goal"]).split() if len(w) > 5)
    res, _s, ok, _m = _run(cop._execute_tool(
        "find_text", {"name": "pramukh", "find": word.upper()}, GUARD))
    assert ok and res["matches"] >= 1
    assert any(f"call_flow.stages[{stage0['id']}]" in h["path"]
               for h in res["hits"])


def test_find_text_no_match_is_ok_and_readonly(copilot):
    cop, agents_dir = copilot
    before = (agents_dir / "pramukh" / "agent.yaml").read_bytes()
    res, summary, ok, mutated = _run(cop._execute_tool(
        "find_text", {"name": "pramukh", "find": "zzz-not-present-anywhere"},
        GUARD))
    assert ok and mutated is False
    assert res["matches"] == 0 and res["hits"] == []
    assert "does not appear" in summary
    assert (agents_dir / "pramukh" / "agent.yaml").read_bytes() == before


def test_replace_then_find_zero_hits(copilot):
    """The sweep-verify loop: replace a phrase everywhere, then prove the old
    wording is gone and the new wording is present."""
    cop, agents_dir = copilot
    tagline = yaml.safe_load(
        (agents_dir / "pramukh" / "agent.yaml").read_text(
            encoding="utf-8"))["identity"]["company_tagline"]
    res, _s, ok, mutated = _run(cop._execute_tool("replace_mentions", {
        "name": "pramukh", "find": tagline,
        "replace": "A fresh replacement tagline"}, GUARD))
    assert ok and mutated
    assert res["replaced"] >= 1
    old = _run(cop._execute_tool(
        "find_text", {"name": "pramukh", "find": tagline}, GUARD))[0]
    assert old["matches"] == 0
    new = _run(cop._execute_tool(
        "find_text", {"name": "pramukh", "find": "A fresh replacement tagline"},
        GUARD))[0]
    assert new["matches"] >= 1
