"""
Surgical edit engine tests (plan §11 P4 gate): concurrency 409, revision
snapshots, scoped-diff proof with auto-restore, rollback round-trip.

Runs against a TEMP agents dir and a TEMP sqlite DB — never the live tree.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from conftest import AGENTS_DIR


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LESCALL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    agents_dir = tmp_path / "agents"
    (agents_dir / "pramukh").mkdir(parents=True)
    shutil.copy(AGENTS_DIR / "pramukh" / "agent.yaml",
                agents_dir / "pramukh" / "agent.yaml")
    return agents_dir


def test_write_agent_round_trip_and_revisions(env):
    from app.agent_definition import store

    data, rev = store.read_agent("pramukh", env)
    # One more than the copied file carried — NOT a literal 1. The fixture is
    # a copy of the LIVE pramukh YAML, which has carried revision 1 since the
    # 2026-09-11 §42 migration; the literal has failed on every run since.
    before = int(data.get("revision") or 0)
    data["identity"]["company_tagline"] = "New tagline for testing"
    result = store.write_agent(
        "pramukh", data, actor="tester:copilot", base_rev=rev,
        reason="tagline edit", agents_dir=env,
        expected_paths={"identity.company_tagline"})
    assert result["revision"] == before + 1
    assert result["effective"] == "next_call"

    data2, rev2 = store.read_agent("pramukh", env)
    assert data2["identity"]["company_tagline"] == "New tagline for testing"
    assert data2["updated_by"] == "tester:copilot"
    assert rev2 != rev

    revs = store.list_revisions("pramukh")
    assert len(revs) == 1
    assert revs[0]["reason"] == "tagline edit"


def test_concurrency_conflict(env):
    from app.agent_definition import store

    data, rev = store.read_agent("pramukh", env)
    # Interleaved write (another session)
    other = dict(data)
    other["identity"] = dict(data["identity"], contact="+91 11111 11111")
    store.write_agent("pramukh", other, actor="other:builder",
                      base_rev=rev, agents_dir=env)
    # Stale write with the old rev must 409, and the file must be untouched.
    mine = dict(data)
    mine["identity"] = dict(data["identity"], contact="+91 22222 22222")
    with pytest.raises(store.ConflictError) as exc:
        store.write_agent("pramukh", mine, actor="me:copilot",
                          base_rev=rev, agents_dir=env)
    assert exc.value.current_rev
    data3, _ = store.read_agent("pramukh", env)
    assert data3["identity"]["contact"] == "+91 11111 11111"


def test_scoped_diff_proof_auto_restore(env):
    from app.agent_definition import store

    data, rev = store.read_agent("pramukh", env)
    before_text = (env / "pramukh" / "agent.yaml").read_text(encoding="utf-8")
    # Claim we're editing the tagline, but ALSO sneak in a greeting change.
    data["identity"]["company_tagline"] = "scoped"
    data["response_bank"]["opening_greeting"] = "COLLATERAL DAMAGE"
    with pytest.raises(store.ScopeError) as exc:
        store.write_agent("pramukh", data, actor="tester:copilot",
                          base_rev=rev, agents_dir=env,
                          expected_paths={"identity.company_tagline"})
    assert any("opening_greeting" in p for p in exc.value.unexpected_paths)
    # Auto-restore: the file is byte-identical to before the attempt.
    after_text = (env / "pramukh" / "agent.yaml").read_text(encoding="utf-8")
    assert after_text == before_text


def test_rollback_round_trip(env):
    from app.agent_definition import store

    original, rev = store.read_agent("pramukh", env)
    edited = dict(original)
    edited["identity"] = dict(original["identity"], agent_name="Someone Else")
    store.write_agent("pramukh", edited, actor="tester:copilot",
                      base_rev=rev, agents_dir=env, reason="rename")
    revs = store.list_revisions("pramukh")
    snap_id = revs[0]["id"]  # pre-rename snapshot

    result = store.rollback_agent("pramukh", snap_id, actor="tester:rollback",
                                  agents_dir=env)
    assert result["rolled_back_to"] == snap_id
    data, _ = store.read_agent("pramukh", env)
    assert data["identity"]["agent_name"] == original["identity"]["agent_name"]
    # Rollback is itself undoable: it produced another snapshot.
    assert len(store.list_revisions("pramukh")) == 2


def test_apply_ops_and_diff():
    from app.agent_definition.diff import apply_ops, compute_diff, render_human

    doc = {"call_flow": {"stages": [
        {"id": "A", "order": 0, "goal": "a"},
        {"id": "B", "order": 1, "goal": "b"},
        {"id": "C", "order": 2, "goal": "c"},
    ]}}
    out = apply_ops(doc, [
        {"op": "set", "path": "call_flow.stages[1].goal", "value": "b2"},
        {"op": "move", "path": "call_flow.stages[2]", "to": 0},
        {"op": "append", "path": "call_flow.stages",
         "value": {"id": "D", "order": 3, "goal": "d"}},
    ])
    assert doc["call_flow"]["stages"][1]["goal"] == "b"  # input not mutated
    assert [s["id"] for s in out["call_flow"]["stages"]] == ["C", "A", "B", "D"]
    assert out["call_flow"]["stages"][2]["goal"] == "b2"
    d = compute_diff(doc, out)
    assert d and render_human(d)
    with pytest.raises(ValueError):
        apply_ops(doc, [{"op": "set", "path": "call_flow.stages[9].goal", "value": "x"}])


def test_owner_mismatch(env):
    from app.agent_definition import store
    from shared.db.database import get_db

    with get_db() as db:
        db.execute("INSERT INTO agents (slug, owner) VALUES (?,?)",
                   ("pramukh", "someone@else.com"))
    data, rev = store.read_agent("pramukh", env)
    with pytest.raises(store.OwnerMismatch):
        store.write_agent("pramukh", data, actor="me:copilot", base_rev=rev,
                          owner="me@tenant.com", agents_dir=env)
    # Admin bypasses
    store.write_agent("pramukh", data, actor="admin:builder", base_rev=rev,
                      owner="admin@x.com", is_admin=True, agents_dir=env)
