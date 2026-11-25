"""Agent-scoped LLM context cache (RUNBOOK §62). Offline — no network, no LLM.

What must hold for the cache to be both cheap and safe:
* the AGENT band is byte-identical for every caller of an agent, and nothing
  about the caller (name, phone, style variant) is ever inside it;
* two agents never share a cache, and an agent edit gets a new one;
* a cached request carries no system instruction and no tools (the provider
  refuses them), with the per-call context first and the per-turn context last;
* a cache failure never costs a turn — the same turn goes out uncached;
* the pre-warm at call start has no side effect on the slot ask budget.
"""
from __future__ import annotations

import asyncio
import os
import time
import types
from pathlib import Path

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice import context_cache as cc
from app.voice.conversation import ConversationRouter

FIXTURE = Path(__file__).parent / "fixtures" / "viraj_outbound_2026_09_26.yaml"
GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai/"


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch, tmp_path):
    reg = cc.ContextCacheRegistry(state_path=tmp_path / "ctx.json")

    async def _no_network(*a, **k):      # never reach the provider in tests
        return None
    monkeypatch.setattr(reg, "_create", _no_network)
    monkeypatch.setattr(reg, "_refresh", _no_network)
    monkeypatch.setattr(cc, "REGISTRY", reg)
    monkeypatch.setattr(cc, "ENABLED", True)
    monkeypatch.delenv("LLM_CONTEXT_CACHE_DISABLED_AGENTS", raising=False)
    yield reg


def _router(name="Alwyn", phone="917042340227", slug="viraj-outbound",
            base_url=GEMINI) -> ConversationRouter:
    r = ConversationRouter(agent_def=AgentDefinition.from_yaml(FIXTURE),
                           default_language="hi")
    r.agent_slug = slug
    r._base_url = base_url
    r._api_key = "test-key"
    r.set_call_context(direction="outbound", contact_name=name,
                       contact_phone=phone)
    return r


def _ready_entry(reg, key, ns="viraj-outbound", tokens=4800):
    e = cc.CacheEntry(key=key, namespace=ns, model="m", name="cachedContents/abc",
                      tokens=tokens, expire_ts=time.time() + 1800)
    reg._entries[key] = e
    return e


# ── 1. bands ────────────────────────────────────────────────────────────────

def test_the_bands_rebuild_the_prompt_byte_for_byte():
    r = _router()
    full = r._build_system_prompt()
    f, agent, call, turn = r._prompt_bands
    assert f == full
    assert full == "\n".join(x for x in (agent, call, turn) if x)


def test_the_agent_band_is_identical_for_every_caller_and_never_names_them():
    a, b = _router("Alwyn", "917042340227"), _router("Priya", "919999999999")
    a._build_system_prompt(); b._build_system_prompt()
    assert a._prompt_bands[1] == b._prompt_bands[1]
    assert "Alwyn" not in a._prompt_bands[1] and "7042340227" not in a._prompt_bands[1]
    assert "Alwyn" in a._prompt_bands[2]
    # The per-call style variant is never in the shared band either.
    assert "━━ THIS CALL ━━" not in a._prompt_bands[1]


def test_the_agent_band_is_per_language():
    r = _router()
    assert "\n".join(r._agent_band_parts("hi")) != "\n".join(r._agent_band_parts("en"))


# ── 2. isolation ────────────────────────────────────────────────────────────

def test_two_agents_never_share_a_key_even_with_identical_prompts():
    k1 = cc.make_key("viraj-outbound", "m", "same band", None)
    k2 = cc.make_key("viraj-inbound", "m", "same band", None)
    assert k1 != k2


def test_an_agent_edit_or_a_new_tool_roster_gets_a_new_key():
    base = cc.make_key("a", "m", "band v1", None)
    assert cc.make_key("a", "m", "band v2", None) != base
    assert cc.make_key("a", "m", "band v1", [{"function": {"name": "x"}}]) != base
    assert cc.make_key("a", "other-model", "band v1", None) != base


def test_the_namespace_is_the_slug_else_the_agent_id():
    r = _router(slug="viraj-outbound")
    assert r._context_cache_namespace() == "viraj-outbound"
    r.agent_slug = ""
    assert r._context_cache_namespace() == r._def.agent_id


def test_an_agent_can_be_opted_out(monkeypatch):
    r = _router()
    assert r._context_cache_allowed()
    monkeypatch.setenv("LLM_CONTEXT_CACHE_DISABLED_AGENTS", "x, viraj-outbound")
    assert not r._context_cache_allowed()


def test_no_cache_off_gemini():
    assert not _router(base_url="https://api.openai.com/v1")._context_cache_allowed()


# ── 3. the request body ─────────────────────────────────────────────────────

def test_cached_messages_put_the_call_first_and_the_turn_last():
    hist = [{"role": "assistant", "content": "हेलो?"},
            {"role": "user", "content": "जी"}]
    msgs = cc.build_cached_messages("CALLER=Alwyn", "STAGE=X", hist)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "user"]
    assert msgs[0]["content"].startswith(cc.CALL_HEADER) and "Alwyn" in msgs[0]["content"]
    assert msgs[-1]["content"].startswith(cc.CONTEXT_HEADER) and "STAGE=X" in msgs[-1]["content"]
    assert not any(m["role"] == "system" for m in msgs)


def test_a_system_message_in_history_disables_the_cache_for_that_turn():
    assert cc.build_cached_messages("c", "t", [{"role": "system", "content": "x"}]) is None


def test_the_plan_is_none_until_the_cache_is_ready(_fresh_registry):
    r = _router()
    system = r._build_system_prompt()
    assert r._context_cache_plan(system, "FINAL", [], None, False) is None
    assert _fresh_registry.stats.misses == 1
    key = cc.make_key("viraj-outbound", r._model, r._prompt_bands[1], None)
    _ready_entry(_fresh_registry, key)
    plan = r._context_cache_plan(system, "FINAL", [{"role": "user", "content": "जी"}],
                                 None, False)
    assert plan is not None and plan["key"] == key
    assert plan["messages"][-1]["content"].endswith("FINAL")
    assert "Alwyn" in plan["messages"][0]["content"]
    # never for a text-only re-ask, never for a stale prompt
    assert r._context_cache_plan(system, "F", [], None, True) is None
    assert r._context_cache_plan(system + "x", "F", [], None, False) is None


def test_an_entry_near_expiry_is_not_used(_fresh_registry):
    e = _ready_entry(_fresh_registry, "k")
    e.expire_ts = time.time() + cc.SAFETY_MARGIN_S - 1
    assert _fresh_registry.lookup("k") is None


def test_tools_convert_to_function_declarations_without_rejected_keys():
    fds = cc.to_function_declarations([{"type": "function", "function": {
        "name": "end_call", "description": "d",
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": {"why": {"type": "string", "default": "x"}}}}}])
    assert fds == [{"name": "end_call", "description": "d", "parameters": {
        "type": "object", "properties": {"why": {"type": "string"}}}}]


# ── 4. a cache failure never costs a turn ───────────────────────────────────

class _Err(Exception):
    def __init__(self, msg, status):
        super().__init__(msg)
        self.status_code = status


class _FakeCompletions:
    def __init__(self, fail_cached=None):
        self.calls = []
        self.fail_cached = fail_cached

    async def create(self, **kw):
        self.calls.append(kw)
        if "extra_body" in kw and self.fail_cached:
            raise self.fail_cached
        return "STREAM"


def _client(fail=None):
    comp = _FakeCompletions(fail)
    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=comp)), comp


def _plan(reg):
    e = _ready_entry(reg, "k1")
    return {"key": "k1", "entry": e, "messages": [{"role": "user", "content": "ctx"}]}


def test_a_cached_turn_sends_no_tools_and_meters_the_cache(_fresh_registry):
    r = _router()
    client, comp = _client()
    tools = {"tools": [{"function": {"name": "end_call"}}]}
    out = asyncio.run(r._primary_stream_create(
        client, [{"role": "system", "content": "S"}], tools, {}, _plan(_fresh_registry)))
    assert out == "STREAM" and len(comp.calls) == 1
    call = comp.calls[0]
    assert "tools" not in call
    assert call["extra_body"]["extra_body"]["google"]["cached_content"] == "cachedContents/abc"
    assert r._turn_cache_tokens == 4800
    assert _fresh_registry.stats.hits == 1


def test_a_refused_cache_is_killed_and_the_same_turn_goes_uncached(_fresh_registry):
    r = _router()
    client, comp = _client(_Err("CachedContent not found (or permission denied)", 403))
    tools = {"tools": [{"function": {"name": "end_call"}}]}
    msgs = [{"role": "system", "content": "S"}]
    out = asyncio.run(r._primary_stream_create(client, msgs, tools, {}, _plan(_fresh_registry)))
    assert out == "STREAM" and len(comp.calls) == 2
    assert comp.calls[1]["messages"] == msgs and comp.calls[1]["tools"]
    assert r._turn_cache_tokens == 0
    assert _fresh_registry.lookup("k1") is None


def test_an_ordinary_failure_propagates_to_the_fallback_logic(_fresh_registry):
    r = _router()
    client, comp = _client(_Err("429 Resource exhausted", 429))
    with pytest.raises(_Err):
        asyncio.run(r._primary_stream_create(client, [], {}, {}, _plan(_fresh_registry)))
    assert _fresh_registry.lookup("k1") is not None


def test_metering_reports_the_cached_tokens_for_the_primary_model(monkeypatch):
    r = _router()
    seen = {}

    class _T:
        async def record_llm(self, call_id, model, i, o, cached_input_tokens=0, **kw):
            seen["cached"] = cached_input_tokens

    import app.voice.conversation as conv
    monkeypatch.setattr(conv, "_credit_tracker", lambda: _T())
    r._turn_cache_tokens = 4800
    usage = types.SimpleNamespace(prompt_tokens=7000, completion_tokens=40,
                                  prompt_tokens_details=None)

    async def go():
        r._record_llm_usage(usage, r._model, "S")
        await asyncio.sleep(0)
    asyncio.run(go())
    assert seen["cached"] == 4800
    seen.clear()

    async def go_fb():
        r._record_llm_usage(usage, "some-fallback", "S")
        await asyncio.sleep(0)
    asyncio.run(go_fb())
    assert seen["cached"] == 0


# ── 5. lifecycle ────────────────────────────────────────────────────────────

def test_prewarm_never_spends_an_ask(monkeypatch, _fresh_registry):
    started = []
    monkeypatch.setattr(_fresh_registry, "ensure",
                        lambda **kw: started.append(kw) or None)
    r = _router()                       # set_call_context → prewarm
    assert started and started[0]["namespace"] == "viraj-outbound"
    assert "Alwyn" not in started[0]["agent_band"]
    assert r.state_mgr.asked_count == {}


def test_a_tiny_band_is_never_cached(_fresh_registry):
    async def go():
        return _fresh_registry.ensure(key="k", namespace="a", model="m",
                                      agent_band="short", tools=None,
                                      api_key="x", base_url=GEMINI)
    assert asyncio.run(go()) is None
    assert "k" in _fresh_registry._negative


def test_ensure_schedules_creation_once_per_key(_fresh_registry):
    band = "agent band " * 800

    async def go():
        t1 = _fresh_registry.ensure(key="k", namespace="a", model="m",
                                    agent_band=band, tools=None,
                                    api_key="x", base_url=GEMINI)
        t2 = _fresh_registry.ensure(key="k", namespace="a", model="m",
                                    agent_band=band, tools=None,
                                    api_key="x", base_url=GEMINI)
        await t1
        return t1, t2
    t1, t2 = asyncio.run(go())
    assert t1 is not None and t2 is None          # single flight


def test_ensure_outside_an_event_loop_is_a_quiet_no_op(_fresh_registry):
    assert _fresh_registry.ensure(key="k", namespace="a", model="m",
                                  agent_band="x" * 9000, tools=None,
                                  api_key="x", base_url=GEMINI) is None


def test_live_entries_survive_a_restart_and_dead_ones_do_not(tmp_path):
    path = tmp_path / "state.json"
    reg = cc.ContextCacheRegistry(state_path=path)
    reg._loaded = True
    _ready_entry(reg, "live")
    dead = _ready_entry(reg, "gone"); dead.expire_ts = time.time() - 10
    reg._save()
    again = cc.ContextCacheRegistry(state_path=path)
    assert again.lookup("live") is not None
    assert again.lookup("gone") is None


def test_cache_errors_are_told_apart_from_quota_errors():
    assert cc.is_cache_error(_Err("CachedContent not found", 404))
    assert cc.is_cache_error(_Err("Cached content expired", 400))
    assert not cc.is_cache_error(_Err("Resource has been exhausted", 429))
    assert not cc.is_cache_error(_Err("Invalid argument: max_tokens", 400))
