"""
Registry freshness tests (F8).

The watcher thread is best-effort and can die; get() must therefore detect a
changed agent.yaml by itself via a per-hit os.stat() check (gated by
AGENT_CACHE_STAT_CHECK, default ON) — no watcher involved anywhere here.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.agent_definition.registry import AgentRegistry

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "seven_step_agent.yaml"

OLD_GREETING = "नमस्ते, क्या मेरी बात {contact_name} जी से हो रही है?"
NEW_GREETING = "Hello ji, updated greeting for the freshness test!"


@pytest.fixture()
def agents_dir(tmp_path, monkeypatch) -> Path:
    """A tmp agents/ dir holding one valid agent ('tiny'), stat check ON."""
    monkeypatch.delenv("AGENT_CACHE_STAT_CHECK", raising=False)
    d = tmp_path / "agents"
    (d / "tiny").mkdir(parents=True)
    shutil.copyfile(FIXTURE, d / "tiny" / "agent.yaml")
    return d


def _rewrite_greeting(agents_dir: Path, new: str) -> None:
    p = agents_dir / "tiny" / "agent.yaml"
    text = p.read_text(encoding="utf-8")
    assert OLD_GREETING in text, "fixture drifted — update OLD_GREETING"
    p.write_text(text.replace(OLD_GREETING, new), encoding="utf-8")


def test_stat_reload_without_watcher(agents_dir):
    reg = AgentRegistry()
    a1 = reg.get("tiny", agents_dir=agents_dir)
    assert a1.response_bank.opening_greeting == OLD_GREETING

    meta1 = reg.meta("tiny")
    assert meta1 is not None
    for key in ("mtime_ns", "size", "sha256", "path", "revision"):
        assert key in meta1
    assert meta1["path"].endswith("agent.yaml")
    assert len(meta1["sha256"]) == 64

    # Rewrite the yaml on disk — NO watcher is running on this registry.
    _rewrite_greeting(agents_dir, NEW_GREETING)

    a2 = reg.get("tiny", agents_dir=agents_dir)
    assert a2.response_bank.opening_greeting == NEW_GREETING
    meta2 = reg.meta("tiny")
    assert meta2["sha256"] != meta1["sha256"]
    assert (meta2["mtime_ns"], meta2["size"]) != (meta1["mtime_ns"], meta1["size"])


def test_stat_check_disabled_serves_stale(agents_dir, monkeypatch):
    reg = AgentRegistry()
    a1 = reg.get("tiny", agents_dir=agents_dir)
    assert a1.response_bank.opening_greeting == OLD_GREETING

    monkeypatch.setenv("AGENT_CACHE_STAT_CHECK", "false")
    _rewrite_greeting(agents_dir, NEW_GREETING)

    a2 = reg.get("tiny", agents_dir=agents_dir)
    assert a2 is a1, "stat check disabled — the cached object must be served"
    assert a2.response_bank.opening_greeting == OLD_GREETING

    # Re-enable: the very next get() picks the change up.
    monkeypatch.delenv("AGENT_CACHE_STAT_CHECK")
    a3 = reg.get("tiny", agents_dir=agents_dir)
    assert a3.response_bank.opening_greeting == NEW_GREETING


def test_deleted_yaml_serves_cached_without_raising(agents_dir):
    reg = AgentRegistry()
    a1 = reg.get("tiny", agents_dir=agents_dir)

    (agents_dir / "tiny" / "agent.yaml").unlink()

    a2 = reg.get("tiny", agents_dir=agents_dir)  # must NOT raise
    assert a2 is a1
    # Meta stays as captured at load time.
    assert reg.meta("tiny") is not None


def test_broken_rewrite_keeps_previous_definition(agents_dir):
    """A yaml that turns unparseable mid-flight serves the last good def."""
    reg = AgentRegistry()
    a1 = reg.get("tiny", agents_dir=agents_dir)
    (agents_dir / "tiny" / "agent.yaml").write_text(
        "identity: [unclosed", encoding="utf-8")
    a2 = reg.get("tiny", agents_dir=agents_dir)  # must NOT raise
    assert a2 is a1
    assert reg.load_failure_count() == 1


def test_health_reports_sanely(agents_dir):
    reg = AgentRegistry()
    h0 = reg.health()
    assert h0 == {"watcher_alive": False, "cached": {}, "load_failures": 0}

    reg.get("tiny", agents_dir=agents_dir)
    h1 = reg.health()
    assert h1["watcher_alive"] is False  # never started here
    assert set(h1["cached"]) == {"tiny"}
    assert h1["cached"]["tiny"]["sha256"] == reg.meta("tiny")["sha256"]
    assert h1["cached"]["tiny"]["revision"] == reg.meta("tiny")["revision"]
    assert h1["load_failures"] == 0

    # A failed load for a missing agent is counted, not hidden.
    with pytest.raises(Exception):
        reg.get("no_such_agent", agents_dir=agents_dir)
    assert reg.health()["load_failures"] == 1
    assert reg.last_load_error("no_such_agent")


def test_meta_absent_for_unknown_and_put_entries(agents_dir):
    from app.agent_definition.fallback_agent import get_neutral_fallback_agent

    reg = AgentRegistry()
    assert reg.meta("tiny") is None  # not loaded yet

    reg.put("handmade", get_neutral_fallback_agent())
    assert reg.meta("handmade") is None  # no backing file — exempt from stat
    # And get() on a put() entry must not stat-crash.
    assert reg.get("handmade", agents_dir=agents_dir) is not None


def test_stat_reload_sticks_to_original_dir(agents_dir):
    """A cache hit WITHOUT agents_dir must stat/reload the file the entry was
    originally loaded from — not re-resolve against the default agents dir."""
    reg = AgentRegistry()
    reg.get("tiny", agents_dir=agents_dir)
    _rewrite_greeting(agents_dir, NEW_GREETING)
    a2 = reg.get("tiny")  # no agents_dir on the hit
    assert a2.response_bank.opening_greeting == NEW_GREETING
    assert reg.meta("tiny")["path"] == str(agents_dir / "tiny" / "agent.yaml")


def test_invalidate_drops_meta(agents_dir):
    reg = AgentRegistry()
    reg.get("tiny", agents_dir=agents_dir)
    assert reg.meta("tiny") is not None
    reg.invalidate("tiny")
    assert reg.meta("tiny") is None
    assert reg.health()["cached"] == {}
