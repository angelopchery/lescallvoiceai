"""Sarvam key supply: rotation under load, verified burial, resurrection.

The 2026-09-08 outage (RUNBOOK §34): two calls to a real contact connected,
played SILENCE for 25s, billed, and were recorded as "no caller speech" —
while 6 of the 28 Sarvam keys were perfectly funded and every one of them
sat on the dead list.

Three independent defects made that possible, one test class each:

1. rotate() popped a key per 402. One dead key produces a BURST of 402s
   (the STT router alone fires four requests per segment), so live keys
   were promoted, never used, and thrown away.
2. The watchdog buried a key on INFERENCE — "the active key changed, so the
   old one must be spent" — which is exactly wrong during such a burst.
3. Burial was permanent: a refilled or wrongly-buried key never came back.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "calling-agent" / "backend"))


# ---------------------------------------------------------------------------
# 1. rotate() — one dead key costs the pool exactly one key
# ---------------------------------------------------------------------------
@pytest.fixture()
def rotator(tmp_path, monkeypatch):
    """key_rotator wired to throwaway pool/.env files."""
    import app.voice.key_rotator as kr
    importlib.reload(kr)
    pool_f = tmp_path / "api_key_pool.json"
    env_f = tmp_path / ".env"
    pool_f.write_text(json.dumps({"pool": ["K1", "K2", "K3"]}))
    env_f.write_text("SARVAM_API_KEY=K0\n")
    monkeypatch.setattr(kr, "_POOL_FILE", pool_f)
    monkeypatch.setattr(kr, "_ENV_FILE", env_f)
    monkeypatch.setenv("SARVAM_API_KEY", "K0")
    return kr, pool_f


def test_burst_of_402s_on_one_key_consumes_one_pool_key(rotator):
    kr, pool_f = rotator
    # Four in-flight requests all fail on the SAME active key K0 — the exact
    # shape of one segment's primary + retry + race + race-retry.
    first = kr.rotate("K0")
    rest = [kr.rotate("K0") for _ in range(3)]

    assert first == "K1"
    assert rest == ["K1", "K1", "K1"], "peers must adopt the new key, not pop more"
    assert json.loads(pool_f.read_text())["pool"] == ["K2", "K3"]


def test_402_on_a_leased_backup_key_never_touches_the_pool(rotator):
    """Per-call leasing hands calls a POOL key. A 402 on one is handled by
    mark_exhausted; it must not promote-and-discard a different key."""
    kr, pool_f = rotator
    assert kr.rotate("K2") == "K0", "active key is returned unchanged"
    assert json.loads(pool_f.read_text())["pool"] == ["K1", "K2", "K3"]


def test_rotate_still_advances_when_the_active_key_is_the_failing_one(rotator):
    kr, pool_f = rotator
    assert kr.rotate("K0") == "K1"
    assert kr.rotate("K1") == "K2"
    assert json.loads(pool_f.read_text())["pool"] == ["K3"]


def test_rotate_without_a_failed_key_keeps_legacy_behaviour(rotator):
    kr, _ = rotator
    assert kr.rotate() == "K1"


def test_rotate_returns_none_on_an_empty_pool(rotator, tmp_path):
    kr, pool_f = rotator
    pool_f.write_text(json.dumps({"pool": []}))
    assert kr.rotate("K0") is None


# ---------------------------------------------------------------------------
# 2 + 3. watchdog — verified burial and resurrection
# ---------------------------------------------------------------------------
@pytest.fixture()
def watchdog(tmp_path, monkeypatch):
    import sarvam_key_watchdog as wd
    importlib.reload(wd)
    dead_f = tmp_path / "sarvam_dead_keys.json"
    dead_f.write_text(json.dumps({"dead": []}))
    monkeypatch.setattr(wd, "DEAD_FILE", dead_f)
    monkeypatch.setattr(wd, "VERIFY_BEFORE_BURY", True)
    return wd, dead_f


def test_a_key_that_still_has_credits_is_never_buried(watchdog, monkeypatch):
    wd, dead_f = watchdog
    monkeypatch.setattr(wd, "probe_key", lambda k: "live")
    dead: set[str] = set()

    assert wd.bury("K9", dead, "in-process rotation") is False
    assert dead == set()
    assert json.loads(dead_f.read_text())["dead"] == []


def test_a_key_that_fails_its_probe_is_buried(watchdog):
    wd, dead_f = watchdog
    import sarvam_key_watchdog as _wd
    _wd.probe_key = lambda k: "dead"
    dead: set[str] = set()

    assert wd.bury("K9", dead, "402 reported in runtime log") is True
    assert dead == {"K9"}
    assert json.loads(dead_f.read_text())["dead"] == ["K9"]


def test_an_unreachable_api_cannot_empty_the_key_pool(watchdog, monkeypatch):
    """A 5xx, a rate limit or a network blip returns "unknown". Burying on
    that would let one Sarvam outage bury every key we own."""
    wd, _ = watchdog
    monkeypatch.setattr(wd, "probe_key", lambda k: "unknown")
    dead: set[str] = set()

    assert wd.bury("K9", dead, "in-process rotation") is False
    assert dead == set()


def test_resurrection_revives_only_the_keys_that_answer(watchdog, monkeypatch):
    wd, dead_f = watchdog
    funded = {"K2", "K5"}
    monkeypatch.setattr(wd, "probe_key",
                        lambda k: "live" if k in funded else "dead")
    master = ["K1", "K2", "K3", "K4", "K5"]
    dead = set(master)

    revived = wd.resurrect(master, dead)

    assert revived == 2
    assert dead == {"K1", "K3", "K4"}
    assert json.loads(dead_f.read_text())["dead"] == ["K1", "K3", "K4"]


def test_resurrection_leaves_an_unreachable_provider_alone(watchdog, monkeypatch):
    wd, _ = watchdog
    monkeypatch.setattr(wd, "probe_key", lambda k: "unknown")
    dead = {"K1", "K2"}
    assert wd.resurrect(["K1", "K2"], dead) == 0
    assert dead == {"K1", "K2"}


def test_probe_reads_402_as_dead_and_200_as_live(watchdog, monkeypatch):
    wd, _ = watchdog

    class _Resp:
        def __init__(self, code): self.status_code = code

    codes = {"LIVE": 200, "SPENT": 402, "BROKEN": 503}
    fake = type("H", (), {"post": staticmethod(
        lambda url, **kw: _Resp(codes[kw["headers"]["api-subscription-key"]]))})
    monkeypatch.setitem(sys.modules, "httpx", fake)

    assert wd.probe_key("LIVE") == "live"
    assert wd.probe_key("SPENT") == "dead"
    assert wd.probe_key("BROKEN") == "unknown"
    assert wd.probe_key("") == "unknown"


# ---------------------------------------------------------------------------
# 4. the sentinel probe must SEE a 402
# ---------------------------------------------------------------------------
def test_health_probe_reports_402_as_no_credits():
    """It scored 402 as "ok" for eight days of mute calls."""
    sys.path.insert(0, str(REPO))
    from shared.voice_health import _timed_post

    assert _timed_post(lambda: 402)[0] == "no-credits"
    assert _timed_post(lambda: 200)[0] == "ok"
    assert _timed_post(lambda: 401)[0] == "auth-error"
    assert _timed_post(lambda: 503)[0] == "server-error"


# ---------------------------------------------------------------------------
# 5. call teardown must not blame the caller for OUR silence
# ---------------------------------------------------------------------------
class _FakeTTS:
    def __init__(self, spoke=0, mute=0, blocked=False):
        self.spoke_utterances = spoke
        self.mute_utterances = mute
        self.credit_blocked = blocked


@pytest.fixture(scope="module")
def bridge():
    """conversation_call_bridge, with its live-reload watcher shut down.

    Importing the bridge starts a watchfiles thread; leaving it running makes
    the interpreter abort at exit (SIGABRT), which would fail the whole run
    even with every test green.
    """
    sys.path.insert(0, str(REPO))
    import conversation_call_bridge as ccb
    yield ccb
    try:
        # The bridge imports the registry as `backend.app.…`; importing it as
        # `app.…` yields a DIFFERENT module object with its own singleton and
        # would stop a watcher nobody started.
        from backend.app.agent_definition.registry import agent_registry
        agent_registry.stop_watcher()
    except Exception:
        pass


def _session_with(ccb, tts, turns=()):
    """A bare object carrying just what the two teardown methods read."""
    from types import SimpleNamespace

    s = SimpleNamespace(conn_id="test", transcript_tail=list(turns),
                        pipeline=SimpleNamespace(tts=tts))
    s._agent_was_mute = ccb.TelephonySession._agent_was_mute.__get__(s)
    s._compute_answer_state = ccb.TelephonySession._compute_answer_state.__get__(s)
    return s


def test_a_call_where_tts_never_spoke_is_not_the_callers_fault(bridge):
    s = _session_with(bridge, _FakeTTS(spoke=0, mute=2))
    assert s._compute_answer_state(25.6) == ("empty", "agent_tts_unavailable")


def test_credit_blocked_tts_counts_as_mute_even_with_no_mute_turns(bridge):
    s = _session_with(bridge, _FakeTTS(spoke=0, mute=0, blocked=True))
    assert s._compute_answer_state(25.6) == ("empty", "agent_tts_unavailable")


def test_a_genuinely_silent_callee_still_reads_as_no_caller_speech(bridge):
    s = _session_with(bridge, _FakeTTS(spoke=3, mute=0))
    assert s._compute_answer_state(25.6) == ("empty", "no_caller_speech")


def test_one_audible_turn_is_enough_to_clear_the_agent(bridge):
    """A mid-call TTS hiccup must never re-label a genuinely silent callee."""
    s = _session_with(bridge, _FakeTTS(spoke=1, mute=4))
    assert s._compute_answer_state(25.6) == ("empty", "no_caller_speech")


def test_a_real_conversation_is_untouched_by_the_mute_check(bridge):
    turns = [{"speaker": "Caller",
              "text": "Yes I am interested, please send me the details today"}]
    s = _session_with(bridge, _FakeTTS(spoke=4, mute=0), turns)
    assert s._compute_answer_state(120.0) == ("answered", None)


# ---------------------------------------------------------------------------
# 4. retire() — the dead key is never handed back to the retry
# ---------------------------------------------------------------------------
def test_retire_rotates_first_so_the_lease_drops_the_dead_active_key(rotator):
    """Call 1072 (2026-10-12): mark_exhausted() ran BEFORE rotate(), refused
    to blacklist what was still the active key, and the per-call lease gave
    the retry the same dead key — the caller's first answer was lost."""
    kr, _ = rotator
    get = kr.per_call_key_provider("call-1072")
    assert get() == "K0"
    assert kr.retire("K0") == "K1"
    assert get() != "K0"
    assert "K0" not in {kr.lease_key(f"c{i}") for i in range(6)}
