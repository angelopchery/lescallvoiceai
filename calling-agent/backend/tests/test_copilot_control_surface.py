"""The Copilot must not file a request into a field that merely sounds right.

Two structural holes, both found on 2026-08-04:

1. `AgentDefinition.from_dict` builds every field by explicit reads, so a key
   the schema does not know is silently DROPPED. `write_agent`'s scoping
   proof only asserts that nothing changed OUTSIDE the declared paths — never
   that anything changed INSIDE them. So a write naming an invented setting
   diffs to nothing, bumps the revision, and comes back "updated".
2. The untyped free-dict subtrees (call_flow.connect, guardrails.honesty, …)
   pass through verbatim, so an invented key there PERSISTS to the YAML and
   still changes nothing on any call.

The unknown-key guard ships in warn mode (a too-tight allowlist would
edit-lock working agents, which is worse than the failure it prevents); the
empty-effective-diff refusal is provable and ships strict.
"""

from __future__ import annotations

import asyncio
import pathlib
import shutil
import sys

import pytest
import yaml

from conftest import AGENTS_DIR, REPO_ROOT

from app.agent_definition import controls


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def cop(tmp_path, monkeypatch):
    monkeypatch.setenv("LESCALL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    dash = str(REPO_ROOT / "dashboard")
    if dash not in sys.path:
        sys.path.insert(0, dash)
    import agent_copilot_api as cop_mod
    return cop_mod


@pytest.fixture()
def cop_with_agents(tmp_path, monkeypatch, cop):
    import agent_builder_api as builder
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


# ── the module itself ───────────────────────────────────────────────────────

def test_schema_paths_are_derived_not_listed():
    paths = controls.schema_paths()
    for real in ("identity.agent_name", "response_bank.opening_greeting",
                 "outbound.opening_greeting", "call_flow.stages.goal",
                 "call_flow.connect", "languages.default"):
        assert real in paths, real


def test_invented_top_level_key_is_unknown():
    bad = controls.unknown_paths(["call_flow.whisper_mode"])
    assert bad and "not a setting" in bad[0][1]


def test_invented_key_inside_a_free_dict_is_unknown():
    """The case store.py structurally cannot catch — `connect` is a free
    dict, so the key persists to YAML and still does nothing."""
    bad = controls.unknown_paths(["call_flow.connect.whisper"])
    assert bad and "no setting called 'whisper'" in bad[0][1]


def test_the_new_second_beat_control_is_known():
    assert controls.unknown_paths(["call_flow.connect.intro_line"]) == []


def test_language_keyed_dicts_are_open():
    """A `ta` key is as legitimate as `en` — never refuse inside these."""
    assert controls.unknown_paths(
        ["response_bank.objection_responses.ta",
         "response_bank.opening_greeting",
         "languages.tts_voices.gu.speaker"]) == []


def test_stage_and_catalog_interiors_are_open():
    """Free dicts inside lists: refusing here would edit-lock live agents."""
    assert controls.unknown_paths([
        "call_flow.stages[0].advance_when.all_collect_filled",
        "call_flow.stages[2].branches[0].when_intent",
        "knowledge_base.catalog[0].availability"]) == []


def test_nearest_controls_names_real_fields():
    hits = controls.nearest_controls("opening_greeting")
    assert "response_bank.opening_greeting" in hits


def _leaf_paths(node, prefix=""):
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            yield p
            yield from _leaf_paths(v, p)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _leaf_paths(v, f"{prefix}[{i}]")


def test_every_live_agent_key_is_known_or_provably_dead():
    """The fence that keeps the allowlist honest as the schema grows.

    Exactly one agent is expected to fail: serene_health carries
    `llm_config.temperature` / `timeout_seconds`, legacy keys that
    from_dict genuinely drops. If this list grows, either the schema gained
    a field the allowlist has not learned, or something wrote an invented
    key into a live agent — both are worth failing for.
    """
    known_dead = {"serene_health"}
    offenders = {}
    for d in sorted(pathlib.Path(AGENTS_DIR).iterdir()):
        f = d / "agent.yaml"
        if not f.exists():
            continue
        doc = yaml.safe_load(f.read_text()) or {}
        bad = controls.unknown_paths(list(_leaf_paths(doc)))
        if bad:
            offenders[d.name] = sorted({r for _, r in bad})[:4]
    assert set(offenders) <= known_dead, offenders


# ── the write-time guards ───────────────────────────────────────────────────

def test_noop_rewrite_is_refused(cop_with_agents):
    """Writing the agent's own current value back must not report success."""
    cop, agents_dir = cop_with_agents
    current, _ = cop._read_full_definition("pramukh")
    same = (current.get("response_bank") or {}).get("opening_greeting")
    with pytest.raises(Exception) as exc:
        _run(cop._execute_tool(
            "update_agent",
            {"name": "pramukh",
             "changes": {"response_bank": {"opening_greeting": same}}},
            {"user_message": "keep it the same"}))
    assert "already" in str(exc.value).lower()


def test_unknown_key_write_is_refused_in_strict_mode(cop_with_agents,
                                                     monkeypatch):
    cop, _ = cop_with_agents
    monkeypatch.setenv("COPILOT_UNKNOWN_PATH_GUARD", "strict")
    with pytest.raises(Exception) as exc:
        _run(cop._execute_tool(
            "update_agent",
            {"name": "pramukh",
             "changes": {"call_flow": {"connect": {"whisper_mode": True}}}},
            {"user_message": "make it whisper"}))
    msg = str(exc.value)
    assert "NOT A REAL SETTING" in msg
    assert "closest real controls" in msg


def test_unknown_key_write_warns_by_default(cop_with_agents, monkeypatch):
    """Default is warn: the write lands, and the model is told to relay it."""
    cop, _ = cop_with_agents
    monkeypatch.delenv("COPILOT_UNKNOWN_PATH_GUARD", raising=False)
    res, _summary, ok, _mut = _run(cop._execute_tool(
        "update_agent",
        {"name": "pramukh",
         "changes": {"call_flow": {"connect": {"whisper_mode": True,
                                               "hello_fastpath": False}}}},
        {"user_message": "make it whisper"}))
    assert ok
    assert any("NOT A REAL SETTING" in n for n in res["notes"])


def test_known_connect_key_is_allowed(cop_with_agents):
    """Ties F1 to the guard: the new second-beat control must write."""
    cop, _ = cop_with_agents
    res, _s, ok, _m = _run(cop._execute_tool(
        "update_agent",
        {"name": "pramukh",
         "changes": {"call_flow": {"connect": {
             "intro_line": {"en": "This is Vishal from Pramukh Group."}}}}},
        {"user_message": "after they say hello, say: This is Vishal from "
                         "Pramukh Group."}))
    assert ok and res["status"] == "updated"
    after, _ = cop._read_full_definition("pramukh")
    assert (after["call_flow"]["connect"]["intro_line"]["en"]
            == "This is Vishal from Pramukh Group.")


def test_list_controls_names_real_settings(cop_with_agents):
    cop, _ = cop_with_agents
    res, _s, ok, mut = _run(cop._execute_tool(
        "list_controls", {"topic": "opening_greeting"}, {}))
    assert ok and not mut
    assert "response_bank.opening_greeting" in res["controls"]


def test_list_controls_is_free(cop):
    """It must not spend a step, or the model still guesses instead."""
    assert "list_controls" in cop._READ_ONLY_TOOLS
