"""Copilot quick controls — voice + speaking speed by click (RUNBOOK §67).

Runs the real endpoints against a temp agents dir holding the frozen Ravi
twins (never a live agent YAML) and a temp sessions dir.
"""
from __future__ import annotations

import asyncio
import shutil
import sys

import pytest
import yaml
from fastapi import HTTPException

from conftest import REPO_ROOT

FIX = REPO_ROOT / "calling-agent" / "backend" / "tests" / "fixtures"


class _Req:
    def __init__(self, body=None):
        self._body = body or {}

    async def json(self):
        return self._body


@pytest.fixture()
def cop(tmp_path, monkeypatch):
    monkeypatch.setenv("LESCALL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    dash = str(REPO_ROOT / "dashboard")
    if dash not in sys.path:
        sys.path.insert(0, dash)
    import agent_builder_api as builder
    import agent_copilot_api as cop
    agents_dir = tmp_path / "agents"
    for d in ("inbound", "outbound"):
        (agents_dir / f"ravi_q-{d}").mkdir(parents=True)
        shutil.copy(FIX / f"ravi_{d}_2026_09_24.yaml",
                    agents_dir / f"ravi_q-{d}" / "agent.yaml")
        builder._claim_slug(f"ravi_q-{d}", builder._ctx_owner(), "Ravi")
    monkeypatch.setattr(builder, "_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(cop, "_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(cop, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(cop, "_SESSIONS", {})
    from backend.app.agent_definition.registry import agent_registry
    agent_registry.invalidate_all()
    return cop, agents_dir


def _run(c):
    return asyncio.run(c)


def _voices(agents_dir, slug):
    d = yaml.safe_load((agents_dir / slug / "agent.yaml").read_text())
    return d["languages"]["tts_voices"]


def test_roster_lists_only_v3_voices_with_gender(cop):
    c, _ = cop
    out = _run(c.copilot_quick_controls("s1", _Req()))
    roster = {v["speaker"]: v for v in out["roster"]}
    assert "priya" in roster and roster["priya"]["gender"] == "female"
    assert "shubh" in roster and roster["shubh"]["gender"] == "male"
    assert "anushka" not in roster                 # v2-only
    assert out["target"]["kind"] == "none"
    assert out["pace_range"]["min"] == 0.5 and out["pace_range"]["max"] == 2.0


def test_agent_voice_change_writes_both_twins_and_flips_pronouns(cop):
    c, agents_dir = cop
    c._load_session("s2")["meta"]["agent"] = "ravi_q-outbound"
    st = _run(c.copilot_quick_controls("s2", _Req()))
    assert st["target"] == {"kind": "agent", "slug": "ravi_q-outbound",
                            "twin": "ravi_q-inbound", "display_name": "Ravi"}
    assert st["state"]["pronouns"] == "he/him"
    res = _run(c.copilot_quick_controls_apply("s2", _Req({"speaker": "priya"})))
    assert res["state"]["speaker"] == "priya"
    assert res["state"]["pronouns"] == "she/her"
    assert res["twin_write"]["applied"]
    for slug in ("ravi_q-outbound", "ravi_q-inbound"):
        tv = _voices(agents_dir, slug)
        assert {v["speaker"] for v in tv.values()} == {"priya"}
        # pace is the voice's own — untouched by a voice change
        assert {v["pace"] for v in tv.values()} == {1.1}
    from backend.app.agent_definition.schema import AgentDefinition
    a = AgentDefinition.from_yaml(agents_dir / "ravi_q-outbound" / "agent.yaml")
    assert a.personality.pronouns == "she"
    # the chat's model is told
    assert "voice = priya" in c._load_session("s2")["messages"][-1]["content"]


def test_speed_per_language_and_all(cop):
    c, agents_dir = cop
    c._load_session("s3")["meta"]["agent"] = "ravi_q-outbound"
    _run(c.copilot_quick_controls_apply(
        "s3", _Req({"pace": {"hi": 0.93}, "both_directions": False})))
    tv = _voices(agents_dir, "ravi_q-outbound")
    assert tv["hi"]["pace"] == 0.95               # snapped to 0.05
    assert tv["en"]["pace"] == 1.1                # other languages untouched
    assert _voices(agents_dir, "ravi_q-inbound")["hi"]["pace"] == 1.1
    res = _run(c.copilot_quick_controls_apply("s3", _Req({"pace": 1.25})))
    assert set(res["state"]["pace"].values()) == {1.25}


@pytest.mark.parametrize("body", [{"speaker": "nobody"}, {"speaker": "anushka"},
                                  {"pace": 3}, {"pace": "fast"}, {}])
def test_bad_input_is_refused(cop, body):
    c, _ = cop
    c._load_session("s4")["meta"]["agent"] = "ravi_q-outbound"
    with pytest.raises(HTTPException) as e:
        _run(c.copilot_quick_controls_apply("s4", _Req(body)))
    assert e.value.status_code == 400


def test_nothing_to_edit_yet(cop):
    c, _ = cop
    with pytest.raises(HTTPException) as e:
        _run(c.copilot_quick_controls_apply("s5", _Req({"speaker": "priya"})))
    assert e.value.status_code == 409


def test_draft_is_edited_before_the_agent_exists(cop):
    c, agents_dir = cop
    s = c._load_session("s6")
    s["interview"] = {"phase": "interviewing", "skipped": [], "provenance": {},
                      "draft": {"identity": {"agent_name": "Meera"},
                                "languages": {"supported": ["hi", "en"],
                                              "default": "hi"}}}
    res = _run(c.copilot_quick_controls_apply(
        "s6", _Req({"speaker": "kavya", "pace": 1.0})))
    assert res["target"]["kind"] == "draft"
    tv = s["interview"]["draft"]["languages"]["tts_voices"]
    assert tv["hi"]["speaker"] == "kavya" and tv["en"]["pace"] == 1.0
    assert res["state"]["pronouns"] == "she/her"
    assert "interview" in res                      # rail refresh rides along
