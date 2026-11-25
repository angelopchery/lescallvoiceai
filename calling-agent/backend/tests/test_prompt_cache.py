"""Prompt-cache guards and LLM cost accounting.

THE GUARD THAT MATTERS
----------------------
`test_static_prefix_is_identical_across_stages` is the whole point of this
file. Provider prompt caching serves a request only up to the first byte that
differs from the previous one, so the money depends on a property no other
test in the repo checks: that the first ~86% of the system prompt is
byte-identical from turn to turn.

That property is trivially destroyed. A timestamp in the identity block, a
`set` iterated without `sorted()`, a stage name promoted a few lines up — any
of these keeps every existing test green (the prompt is still *correct*) while
silently taking the cacheable prefix to zero and re-billing ~2,800 input tokens
on every turn of every call. The golden fixtures pin what the prompt SAYS;
this pins what makes it CHEAP.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.voice import prompt_cache  # noqa: E402
from shared import ai_pricing  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "golden_prompts"

# Measured on the live endpoint 2026-08-11: the rendered Pramukh prompt is
# 3,236 input tokens and 86% of it is stable across the six stages. The floor
# is set well under that so ordinary prompt edits don't trip it — it fires on
# a structural regression (a dynamic value promoted into the static band),
# not on wording changes.
MIN_STABLE_PREFIX_RATIO = 0.60


def _fixture_groups() -> dict[tuple[str, str, str], dict[str, str]]:
    groups: dict[tuple[str, str, str], dict[str, str]] = {}
    for path in FIXTURE_DIR.glob("*__*__*__*.txt"):
        agent, stage, lang, direction = path.stem.split("__")
        groups.setdefault((agent, lang, direction), {})[stage] = (
            path.read_text(encoding="utf-8"))
    return groups


def _longest_common_prefix(strings: list[str]) -> str:
    if not strings:
        return ""
    lo, hi = min(strings), max(strings)
    for i, ch in enumerate(lo):
        if ch != hi[i]:
            return lo[:i]
    return lo


@pytest.mark.parametrize("group", sorted(_fixture_groups()))
def test_static_prefix_is_identical_across_stages(group):
    """The cacheable band must not vary with conversation stage.

    Stage is the fastest-moving thing in a call. If two stages of the same
    agent+language+direction disagree anywhere inside the static band, then no
    two consecutive turns share a prefix and prompt caching cannot work at all.
    """
    prompts = _fixture_groups()[group]
    if len(prompts) < 2:
        pytest.skip("single-stage agent — nothing to compare")

    statics = {stage: prompt_cache.split_prompt(p)[0]
               for stage, p in prompts.items()}
    distinct = set(statics.values())
    assert len(distinct) == 1, (
        f"{group}: the static band differs across stages "
        f"{sorted(statics)} — prompt caching is dead. Something that varies "
        f"per turn was moved above the '{prompt_cache.STATIC_PREFIX_MARKER}' "
        f"marker in _build_system_prompt."
    )

    static = distinct.pop()
    full = next(iter(prompts.values()))
    ratio = len(static) / len(full)
    assert ratio >= MIN_STABLE_PREFIX_RATIO, (
        f"{group}: only {ratio:.0%} of the prompt is cacheable "
        f"(floor {MIN_STABLE_PREFIX_RATIO:.0%}). Per-turn content moved up "
        f"into the static band."
    )


@pytest.mark.parametrize("group", sorted(_fixture_groups()))
def test_static_prefix_clears_provider_minimum(group):
    """A stable prefix under the provider's floor is billed fresh anyway."""
    prompts = _fixture_groups()[group]
    static = prompt_cache.split_prompt(next(iter(prompts.values())))[0]
    tokens = prompt_cache.estimate_tokens(static)
    assert tokens >= prompt_cache.MIN_CACHEABLE_TOKENS, (
        f"{group}: static prefix is ~{tokens} tokens, under the "
        f"{prompt_cache.MIN_CACHEABLE_TOKENS}-token provider minimum — it "
        f"would never be cached even on a paid tier."
    )


def test_fingerprint_tracks_the_static_band_only():
    """Same prefix + different per-turn state must fingerprint identically."""
    prefix = "AGENT RULES\n" * 200
    a = prefix + prompt_cache.STATIC_PREFIX_MARKER + "\nStage: DISCOVERY | Turn: 2"
    b = prefix + prompt_cache.STATIC_PREFIX_MARKER + "\nStage: CLOSING | Turn: 9"
    assert prompt_cache.prefix_fingerprint(a) == prompt_cache.prefix_fingerprint(b)

    changed = "AGENT RULES v2\n" + prefix
    c = changed + prompt_cache.STATIC_PREFIX_MARKER + "\nStage: DISCOVERY | Turn: 2"
    assert prompt_cache.prefix_fingerprint(c) != prompt_cache.prefix_fingerprint(a)


def test_prompt_without_marker_claims_no_cacheable_prefix():
    static, dynamic = prompt_cache.split_prompt("no state block here")
    assert static == ""
    assert dynamic == "no state block here"
    assert prompt_cache.prefix_fingerprint("no state block here") == "none"


def test_token_estimator_matches_measured_api_count():
    """±10% of the 3,666 tokens the live API reported for this exact file.

    Re-measured 2026-09-08 after the SCRIPT FIDELITY block (RUNBOOK §36)
    added ~430 tokens to every prompt (was 3,236 tokens / 2,900-3,560
    bounds). Pins the Latin/Indic split. A regression to a naive len/4
    would understate the Hindi and Gujarati prompts by roughly a third.
    """
    prompt = (FIXTURE_DIR / "pramukh__DISCOVERY__en__inbound.txt").read_text(
        encoding="utf-8")
    assert 3300 <= prompt_cache.estimate_tokens(prompt) <= 4040


# --- cost accounting -------------------------------------------------------


def test_cached_tokens_are_a_subset_not_an_addition():
    """Cached input is billed at the cache rate INSTEAD of the input rate."""
    full = ai_pricing.llm_cost("gemini-3.5-flash", 3236, 45, cached_input_tokens=0)
    cached = ai_pricing.llm_cost("gemini-3.5-flash", 3236, 45,
                                 cached_input_tokens=2800)
    assert cached < full
    # 2,800 of 3,236 tokens at 1/10th the rate.
    assert cached == pytest.approx(full * 0.29, rel=0.1)


def test_cached_tokens_cannot_exceed_input_tokens():
    """A provider over-reporting cache must not produce a negative bill."""
    cost = ai_pricing.llm_cost("gemini-3.5-flash", 100, 10,
                               cached_input_tokens=99_999)
    assert cost is not None and cost > 0


def test_tier_variants_are_not_priced_as_their_base_model():
    """`-lite` is a different model, not a release suffix of `-flash`."""
    lite = ai_pricing.llm_rate("gemini-3.5-flash-lite")
    assert lite is not None and lite != ai_pricing.llm_rate("gemini-3.5-flash")
    assert lite["in"] == 0.30 and lite["out"] == 2.50
    assert ai_pricing.llm_rate("gemini-3.5-flash-preview-09-2026") == \
        ai_pricing.llm_rate("gemini-3.5-flash")


def test_unpriced_model_returns_none_not_zero():
    """Unknown pricing must be visible, never silently free."""
    assert ai_pricing.llm_cost("some-model-nobody-priced", 10_000, 1_000) is None


def test_cost_is_positive_and_ordered_by_tier():
    flash = ai_pricing.llm_cost("gemini-3.5-flash", 10_000, 500)
    lite = ai_pricing.llm_cost("gemini-3.1-flash-lite", 10_000, 500)
    assert flash > lite > 0


# --- tracker integration ---------------------------------------------------


def test_record_llm_accumulates_into_the_call_ledger(tmp_path, monkeypatch):
    from app.voice import credit_tracker
    monkeypatch.setattr(credit_tracker, "_CREDIT_LOG_DIR", tmp_path / "credit_log")
    tracker = credit_tracker.SarvamCreditTracker()
    tracker.start_call("call-1")

    async def _drive():
        await tracker.record_llm("call-1", "gemini-3.5-flash", 3236, 45,
                                 cached_input_tokens=0)
        await tracker.record_llm("call-1", "gemini-3.5-flash", 3300, 60,
                                 cached_input_tokens=2800)

    asyncio.run(_drive())
    summary = tracker.get_call_summary("call-1")
    assert summary["llm_requests"] == 2
    assert summary["llm_input_tokens"] == 6536
    assert summary["llm_cached_input_tokens"] == 2800
    assert summary["llm_models"] == ["gemini-3.5-flash"]
    # The cache saving is a subtraction of two measured numbers.
    assert summary["est_llm_cost"] < summary["est_llm_cost_uncached"]
    # est_total_cost is rounded to 6dp; the subtotal it sums is not.
    assert summary["est_total_cost"] == pytest.approx(
        summary["est_llm_cost"], abs=1e-6)


def test_unpriced_model_is_named_rather_than_costed(tmp_path, monkeypatch):
    """Total stays a number, but the reader is told it is a floor."""
    from app.voice import credit_tracker
    monkeypatch.setattr(credit_tracker, "_CREDIT_LOG_DIR", tmp_path / "credit_log")
    tracker = credit_tracker.SarvamCreditTracker()
    tracker.start_call("call-2")
    asyncio.run(tracker.record_llm("call-2", "gemini-3.6-flash", 5000, 200))
    summary = tracker.get_call_summary("call-2")
    assert summary["llm_input_tokens"] == 5000
    assert summary["est_llm_cost"] is None
    assert summary["llm_unpriced_models"] == ["gemini-3.6-flash"]


def _live_router(stage="DISCOVERY", lang="en"):
    """A router over the real Pramukh YAML, driven like the golden harness."""
    from conftest import AGENTS_DIR
    from app.agent_definition.schema import AgentDefinition
    from app.voice.conversation import ConversationRouter

    agent = AgentDefinition.from_yaml(AGENTS_DIR / "pramukh" / "agent.yaml")
    router = ConversationRouter(agent_def=agent, default_language=lang)
    router._available_calendar_slots = (
        lambda: list(router._def.calendar.mock_slots or []))
    router.state.stage = stage
    router.state.language = lang
    router.state.turn_count = 4
    router.call_direction = "inbound"
    return router


def test_pending_name_does_not_break_the_static_prefix():
    """REGRESSION — per-turn state must not render above the cache boundary.

    `name_confirm_block` sat above the ━━ CALL STATE ━━ marker. It is empty in
    every golden fixture (fixed CallState, no pending name), so the whole
    fixture suite passed while a live call broke its own prefix on the turn
    after the caller said their name. Only live state exposes this class of
    bug — hence a router, not a fixture.
    """
    a = _live_router()
    before = prompt_cache.prefix_fingerprint(a._build_system_prompt())

    b = _live_router()
    b._name_pending = "Rohan"
    after_prompt = b._build_system_prompt()

    assert prompt_cache.prefix_fingerprint(after_prompt) == before, (
        "a pending name changed the cacheable prefix — per-turn content is "
        "rendering above prompt_cache.STATIC_PREFIX_MARKER"
    )
    # The instruction must still reach the model, just below the boundary.
    assert "Rohan" in after_prompt
    assert "Rohan" in prompt_cache.split_prompt(after_prompt)[1]


def test_turn_count_and_stage_stay_out_of_the_prefix():
    """The two fastest-moving values must never appear in the cached band."""
    r = _live_router(stage="CLOSING")
    r.state.turn_count = 17
    static = prompt_cache.split_prompt(r._build_system_prompt())[0]
    assert "Turn: 17" not in static
    assert "Stage: CLOSING" not in static


class _Err(Exception):
    """Stand-in for an SDK APIStatusError — same duck type the check reads."""

    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


def test_quota_error_is_not_mistaken_for_a_parameter_rejection():
    """REGRESSION — the first live call after deploy recorded zero requests.

    A 429 disabled usage telemetry for the whole process on the reasoning that
    the provider had "rejected stream_options". It had not: the free tier's
    20-requests-per-day cap was spent. A quota, timeout or 5xx failure says
    nothing about parameter support and must leave telemetry armed.
    """
    from app.voice.conversation import _is_param_rejection
    quota = _Err(429, "RESOURCE_EXHAUSTED: Quota exceeded for metric "
                      "generate_content_free_tier_requests, limit: 20")
    assert _is_param_rejection(quota) is False
    assert _is_param_rejection(_Err(500, "internal error")) is False
    assert _is_param_rejection(_Err(503, "overloaded")) is False
    assert _is_param_rejection(_Err(401, "invalid api key")) is False
    assert _is_param_rejection(TimeoutError("read timeout")) is False


def test_genuine_parameter_rejection_is_detected():
    """A 400 that names the parameter is the ONLY case that disarms telemetry."""
    from app.voice.conversation import _is_param_rejection
    assert _is_param_rejection(
        _Err(400, "Unrecognized request argument supplied: stream_options"))
    assert _is_param_rejection(
        _Err(422, "include_usage is not supported by this model"))
    # A 400 about something else is a real request error, not a param issue.
    assert _is_param_rejection(_Err(400, "messages must not be empty")) is False


def test_tracker_survives_unknown_call_id(tmp_path, monkeypatch):
    """A turn recorded against a call that already ended must not raise."""
    from app.voice import credit_tracker
    monkeypatch.setattr(credit_tracker, "_CREDIT_LOG_DIR", tmp_path / "credit_log")
    tracker = credit_tracker.SarvamCreditTracker()
    asyncio.run(tracker.record_llm("never-started", "gemini-3.5-flash", 100, 10))
    assert tracker.get_session_summary()["llm_requests"] == 1
