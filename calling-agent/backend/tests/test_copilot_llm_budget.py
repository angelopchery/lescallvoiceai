"""
Copilot LLM request-budget tests (Groq TPM 413 incident, 2026-07-15).

A 42-message interview session produced an 18,775-token request: over the
fallback model's 12k TPM ceiling, and 2 tool-loop iterations busted even the
primary's 30k. _fit_messages must shrink any history to a per-model budget
while never dropping the system prompt or the current turn.
"""
from __future__ import annotations

import sys

import pytest

from conftest import REPO_ROOT


@pytest.fixture()
def cop():
    dash = str(REPO_ROOT / "dashboard")
    if dash not in sys.path:
        sys.path.insert(0, dash)
    import agent_copilot_api as cop
    return cop


def _fake_history(turns: int, tool_chars: int = 3000) -> list[dict]:
    msgs: list[dict] = []
    for i in range(turns):
        msgs.append({"role": "user", "content": f"answer {i}: " + "detail " * 60})
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": f"c{i}", "type": "function",
                                     "function": {"name": "update_draft", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * tool_chars})
        msgs.append({"role": "assistant", "content": "Noted. Next question: " + "words " * 40})
    return msgs


def test_under_budget_untouched(cop):
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"}]
    assert cop._fit_messages(msgs, 50_000) == msgs


def test_over_budget_fits_and_preserves_invariants(cop):
    system = {"role": "system", "content": "S" * 16_000}   # realistic prompt size
    history = _fake_history(12)                            # ~ the failing session, bigger
    current = {"role": "user", "content": "my newest answer"}
    msgs = [system] + history + [current]
    assert cop._approx_tokens(msgs) > 9_500                # genuinely over

    fitted = cop._fit_messages(msgs, 9_500)
    assert cop._approx_tokens(fitted) <= 9_500
    assert fitted[0] is system or fitted[0]["role"] == "system"
    assert fitted[-1]["content"] == "my newest answer"     # current turn survives
    # no orphaned tool results / tool-calling assistants at the front
    assert fitted[1]["role"] not in ("tool",)
    assert not (fitted[1]["role"] == "assistant" and fitted[1].get("tool_calls"))


def test_current_turn_never_dropped_even_if_over(cop):
    # A single huge current turn can't fit 2k tokens — fitting must keep it
    # (the API call may still fail upstream, but we never send a turn-less chat).
    msgs = [{"role": "system", "content": "S" * 4_000},
            {"role": "user", "content": "U" * 40_000}]
    fitted = cop._fit_messages(msgs, 2_000)
    assert [m["role"] for m in fitted] == ["system", "user"]


def test_tool_use_failed_walks_the_chain(cop, monkeypatch):
    """400 tool_use_failed on the primary (flaky small model emitting broken
    JSON) must fall through to the fallback model, not 502 the user."""
    import asyncio
    import httpx
    from openai import BadRequestError

    def _bad_request():
        req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        resp = httpx.Response(400, request=req)
        return BadRequestError(
            "Error code: 400 - {'code': 'tool_use_failed', 'failed_generation': '...'}",
            response=resp, body={"error": {"code": "tool_use_failed"}})

    calls: list[str] = []

    class _Completions:
        async def create(self, **kw):
            calls.append(kw["model"])
            if kw["model"] == "flaky-primary":
                raise _bad_request()
            class _Msg:  # minimal choices[0].message shape
                content = "ok"
                tool_calls = None
            class _Choice:
                message = _Msg()
            class _Completion:
                choices = [_Choice()]
            return _Completion()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    monkeypatch.setenv("LLM_FALLBACK_MODEL", "good-fallback")
    # COPILOT_-prefixed overrides win in _env(); clear any from the real .env
    monkeypatch.delenv("COPILOT_LLM_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    result = asyncio.run(cop._chat_completion(_Client(), "flaky-primary", msgs))
    assert result.content == "ok"
    # 3 nudged tries on the primary, then the fallback succeeds
    assert calls == ["flaky-primary"] * 3 + ["good-fallback"]


def test_budget_math_fits_both_groq_ceilings(cop):
    # primary: half of 30k TPM minus output+margin; fallback: 12k TPM ceiling
    primary_budget = max(8000, 30_000 // 2 - 3_000 - 500)
    fb_budget = max(7000, 12_000 - 2_000 - 500)
    assert primary_budget + 3_000 <= 15_000    # two iterations fit one minute
    assert fb_budget + 2_000 <= 12_000         # a fitted request fits the 413 ceiling


def test_gemini_thought_signature_round_trip(cop):
    """Gemini 3 (OpenAI-compat) 400s when assistant functionCall history
    lacks extra_content.google.thought_signature; other providers may reject
    the unknown field. The adapter must add/strip per provider without
    mutating the stored session history."""
    class _GeminiCli:
        base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
    class _GroqCli:
        base_url = "https://api.groq.com/openai/v1"

    real_sig = {"google": {"thought_signature": "REAL_SIG_FROM_MODEL"}}
    history = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "update_draft", "arguments": "{}"}}]},   # legacy, no sig
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c2", "type": "function",
             "function": {"name": "update_draft", "arguments": "{}"},
             "extra_content": dict(real_sig)}]},                            # preserved sig
        {"role": "tool", "tool_call_id": "c2", "content": "ok"},
    ]
    import copy as _copy
    snapshot = _copy.deepcopy(history)

    gem = cop._adapt_tool_calls_for_provider(history, _GeminiCli())
    sigs = [tc["extra_content"]["google"]["thought_signature"]
            for m in gem for tc in (m.get("tool_calls") or [])]
    assert sigs == ["context_engineering_is_the_way_to_go", "REAL_SIG_FROM_MODEL"]

    groq = cop._adapt_tool_calls_for_provider(history, _GroqCli())
    assert all("extra_content" not in tc
               for m in groq for tc in (m.get("tool_calls") or []))

    assert history == snapshot  # session store never mutated


# ---------------------------------------------------------------------------
# Token accounting (2026-09-12)
#
# Before this, _chat_completion() read completion.choices[0].message and threw
# completion.usage away — the platform had no idea what the copilot cost. The
# turn counter that stood in for it counts SEND PRESSES, and one press runs a
# tool loop of up to _MAX_TOOL_ROUNDS full requests.
# ---------------------------------------------------------------------------

def _usage_client(calls: list, usage_per_call=(1000, 200, 1500)):
    """A client that answers once with no tool calls and reports usage."""
    class _Usage:
        prompt_tokens, completion_tokens, total_tokens = usage_per_call

    class _Completions:
        async def create(self, **kw):
            calls.append(kw["model"])
            class _Msg:
                content = "ok"
                tool_calls = None
            class _Choice:
                message = _Msg()
            class _Completion:
                choices = [_Choice()]
                usage = _Usage()
            return _Completion()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    return _Client()


def test_every_provider_round_is_charged(cop, monkeypatch):
    """Including the nudged retries and the fallback walk. Those are real
    requests against the shared model quota; a budget that only counted the
    successful one would let a tenant provoking malformed tool calls spend
    three times what it records."""
    import asyncio
    charged: list[tuple] = []
    monkeypatch.setattr(cop, "_charge_tokens",
                        lambda owner, usage: (charged.append(
                            (owner, getattr(usage, "total_tokens", 0))), 1500)[1])
    calls: list[str] = []
    spend: dict = {}
    asyncio.run(cop._chat_completion(
        _usage_client(calls), "m",
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        owner="who@example.com", spend=spend))
    assert charged == [("who@example.com", 1500)]
    assert spend["tokens"] == 1500


def test_charge_is_skipped_when_the_provider_reports_nothing(cop):
    """A missing usage block costs a count, never a reply."""
    assert cop._charge_tokens("who@example.com", None) == 0
    assert cop._charge_tokens(None, object()) == 0


def test_charge_trusts_the_providers_total(cop, monkeypatch):
    """Gemini folds reasoning tokens into total_tokens without showing them in
    completion_tokens. Summing the visible parts under-counts the expensive
    half of every request."""
    seen: dict = {}
    import shared.billing.entitlements as ent
    monkeypatch.setattr(ent, "record_copilot_tokens",
                        lambda owner, **kw: seen.update(kw))

    class _Usage:
        prompt_tokens, completion_tokens, total_tokens = 1000, 100, 9000

    assert cop._charge_tokens("who@example.com", _Usage()) == 9000
    assert seen["total_tokens"] == 9000


def test_remaining_tokens_is_none_when_nothing_caps_the_tenant(cop, monkeypatch):
    """None means "do not run the mid-turn guard", and it is also what every
    failure degrades to — a budget that cannot be read must not become a wall
    in the middle of someone's work."""
    import shared.billing.entitlements as ent
    monkeypatch.setattr(ent, "effective_limits",
                        lambda o: {"copilot_tokens_per_day": None})
    assert cop._remaining_tokens("who@example.com") is None

    def _boom(_):
        raise RuntimeError("db down")
    monkeypatch.setattr(ent, "effective_limits", _boom)
    assert cop._remaining_tokens("who@example.com") is None
    assert cop._remaining_tokens(None) is None


def test_remaining_tokens_subtracts_todays_spend(cop, monkeypatch):
    import shared.billing.entitlements as ent
    monkeypatch.setattr(ent, "effective_limits",
                        lambda o: {"copilot_tokens_per_day": 150_000})
    monkeypatch.setattr(ent, "copilot_tokens_used", lambda o: 120_000)
    assert cop._remaining_tokens("who@example.com") == 30_000
    monkeypatch.setattr(ent, "copilot_tokens_used", lambda o: 900_000)
    assert cop._remaining_tokens("who@example.com") == 0   # never negative
