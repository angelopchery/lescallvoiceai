"""
End-to-end surgical-edit proof through the COPILOT tool layer (plan P4 gate):
propose → diff → apply (confirmation-gated) → nothing-else-changed → rollback.

Imports the dashboard module against a temp agents dir + temp sqlite DB.
"""
from __future__ import annotations

import asyncio
import importlib
import shutil
import sys
from pathlib import Path

import pytest

from conftest import AGENTS_DIR, REPO_ROOT


@pytest.fixture()
def copilot(tmp_path, monkeypatch):
    monkeypatch.setenv("LESCALL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    dash = str(REPO_ROOT / "dashboard")
    if dash not in sys.path:
        sys.path.insert(0, dash)
    import agent_builder_api as builder
    import agent_copilot_api as cop
    importlib.reload(builder) if getattr(builder, "_TEST_RELOADED", False) else None
    # Point BOTH modules at a temp agents dir with a copy of pramukh.
    agents_dir = tmp_path / "agents"
    (agents_dir / "pramukh").mkdir(parents=True)
    shutil.copy(AGENTS_DIR / "pramukh" / "agent.yaml",
                agents_dir / "pramukh" / "agent.yaml")
    monkeypatch.setattr(builder, "_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(cop, "_AGENTS_DIR", agents_dir)
    # An agent on disk is also CLAIMED in the agents index — without the row,
    # the tenant boundary in _execute_tool rejects every edit with a 404.
    # (This used to pass only because the caller resolved as an admin, which
    # bypassed the check entirely; roles are flat now, so the fixture has to
    # model the real thing.)
    builder._claim_slug("pramukh", builder._ctx_owner(), "Pramukh")
    from backend.app.agent_definition.registry import agent_registry
    agent_registry.invalidate_all()
    return cop, agents_dir


def _run(coro):
    return asyncio.run(coro)


def test_propose_apply_rollback_scoped(copilot):
    cop, agents_dir = copilot
    session: dict = {"messages": [], "transcript": [], "meta": {}}
    guard = {"user_turns": 3, "created_this_turn": False,
             "session": session, "user_message": "change the tagline"}

    before = (agents_dir / "pramukh" / "agent.yaml").read_text(encoding="utf-8")

    # 1. Propose a single-field change → diff, no write.
    res, _s, ok, mutated = _run(cop._execute_tool("propose_update", {
        "name": "pramukh",
        "changes": {"identity": {"company_tagline": "Surgically edited tagline"}},
        "reason": "test edit",
    }, guard))
    assert ok and not mutated
    assert res["status"] == "proposed"
    assert "company_tagline" in res["diff"]
    change_id = res["change_id"]
    assert (agents_dir / "pramukh" / "agent.yaml").read_text(encoding="utf-8") == before

    # 2. Apply with explicit confirmation in the user message.
    guard["user_message"] = "yes, apply it"
    res2, _s, ok2, mutated2 = _run(cop._execute_tool("apply_update",
                                                     {"change_id": change_id}, guard))
    assert ok2 and mutated2
    # One more than whatever the copied agent file carried. This asserted a
    # literal 1 — true only while the live pramukh YAML had never been
    # written; the 2026-09-11 §42 migration stamped it revision 1 and the
    # test has failed on every run since.
    import yaml as _y
    assert res2["revision"] == int(_y.safe_load(before).get("revision") or 0) + 1

    # 3. Prove scoping: ONLY the tagline (+ server-managed fields) changed.
    from backend.app.agent_definition import store, diff as diffmod
    import yaml as _yaml
    import dataclasses as _dc
    from backend.app.agent_definition.schema import AgentDefinition
    after_dict, _ = store.read_agent("pramukh", agents_dir)
    canon_before = _dc.asdict(AgentDefinition.from_dict(_yaml.safe_load(before)))
    canon_after = _dc.asdict(AgentDefinition.from_dict(after_dict))
    changed = {d["path"] for d in diffmod.compute_diff(canon_before, canon_after)}
    assert changed == {"identity.company_tagline", "revision", "updated_at", "updated_by"}

    # 4. Roll it back — byte-level content restored (minus audit stamps).
    revs = store.list_revisions("pramukh")
    res3, _s, ok3, _m = _run(cop._execute_tool(
        "rollback_agent", {"name": "pramukh", "rev_id": revs[0]["id"]}, guard))
    assert ok3
    restored, _ = store.read_agent("pramukh", agents_dir)
    assert restored["identity"]["company_tagline"] == \
        _yaml.safe_load(before)["identity"]["company_tagline"]


def test_apply_requires_confirmation(copilot):
    cop, _ = copilot
    session: dict = {"messages": [], "transcript": [], "meta": {}}
    guard = {"user_turns": 3, "session": session,
             "user_message": "change the greeting please"}
    res, _s, ok, _m = _run(cop._execute_tool("propose_update", {
        "name": "pramukh",
        "changes": {"identity": {"company_tagline": "x" * 20}},
    }, guard))
    # Same turn, no confirmation words → refused.
    with pytest.raises(ValueError, match="confirmation"):
        _run(cop._execute_tool("apply_update", {"change_id": res["change_id"]}, guard))


def test_stage_ops_scoped(copilot):
    cop, agents_dir = copilot
    guard = {"user_turns": 3, "session": {}, "user_message": "swap stages"}
    res, _s, ok, mutated = _run(cop._execute_tool("update_stage", {
        "name": "pramukh", "stage_id": "DISCOVERY",
        "fields": {"turns_until_advance": 9},
    }, guard))
    assert ok and mutated and res["status"] == "updated"
    from backend.app.agent_definition import store
    data, _ = store.read_agent("pramukh", agents_dir)
    disc = next(s for s in data["call_flow"]["stages"] if s["id"] == "DISCOVERY")
    assert disc["turns_until_advance"] == 9

    # reorder round-trip
    ids = [s["id"] for s in data["call_flow"]["stages"]]
    new_order = [ids[1], ids[0]] + ids[2:]
    res2, _s, ok2, _m = _run(cop._execute_tool("reorder_stages", {
        "name": "pramukh", "ordered_ids": new_order}, guard))
    assert ok2
    data2, _ = store.read_agent("pramukh", agents_dir)
    assert [s["id"] for s in data2["call_flow"]["stages"]] == new_order
    assert [s["order"] for s in data2["call_flow"]["stages"]] == list(range(len(ids)))


def test_slot_ops(copilot):
    cop, agents_dir = copilot
    guard = {"user_turns": 3, "session": {}, "user_message": "add a slot"}
    res, _s, ok, _m = _run(cop._execute_tool("add_slot", {
        "name": "pramukh",
        "slot": {"name": "referral_source", "type": "text", "required": False,
                 "priority": 85, "question": {"en": "How did you hear about us?"}},
    }, guard))
    assert ok
    from backend.app.agent_definition import store
    data, _ = store.read_agent("pramukh", agents_dir)
    assert any(s["name"] == "referral_source" for s in data["slots"])
    res2, _s, ok2, _m = _run(cop._execute_tool("remove_slot", {
        "name": "pramukh", "slot_name": "referral_source"}, guard))
    assert ok2
    data2, _ = store.read_agent("pramukh", agents_dir)
    assert not any(s["name"] == "referral_source" for s in data2["slots"])


def test_replace_mentions_dry_run(copilot):
    cop, agents_dir = copilot
    guard = {"user_turns": 3, "session": {}, "user_message": "rename"}
    before = (agents_dir / "pramukh" / "agent.yaml").read_text(encoding="utf-8")
    res, _s, ok, mutated = _run(cop._execute_tool("replace_mentions", {
        "name": "pramukh", "find": "Vishal", "replace": "Vikram", "dry_run": True,
    }, guard))
    assert ok and not mutated
    assert res["status"] == "dry_run" and res["would_replace"] > 0
    assert (agents_dir / "pramukh" / "agent.yaml").read_text(encoding="utf-8") == before
