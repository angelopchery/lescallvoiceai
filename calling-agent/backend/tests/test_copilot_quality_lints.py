"""Cross-surface contradiction lints + surgical-tool quality surfacing.

Regression source (2026-07-28): a live call spoke a banned "twenty-minute
demo … Tuesday at 10 AM" AFTER four update_stage edits — call_flow.call_goal
(rendered every turn as CALL MISSION) and the exact_n scheduling default kept
ordering the old behaviour, and update_stage returned no quality warnings so
the copilot claimed success. These tests pin the three new lints, prove they
never nag the production pramukh agents, and prove the surgical tools now
surface quality + the mission string.
"""
from __future__ import annotations

import asyncio
import dataclasses
import shutil
import sys

import pytest
import yaml

from conftest import AGENTS_DIR, REPO_ROOT

from app.agent_definition.schema import AgentDefinition


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


def _run(coro):
    return asyncio.run(coro)


# Stable marker substrings, one per lint.
MARK_CALLER_PICKS = "let the CALLER propose the time"
MARK_FIRST_BEAT = "only the FIRST beat"
MARK_NO_TIMES = "no times to offer"
MARK_DEAD_END_RULES = "no way to act"
MARK_TINY_CAP = "barely covers a greeting"
MARK_NO_CLOSE_CONFIRM = "hang up on the first thing it hears"
ALL_MARKS = (MARK_CALLER_PICKS, MARK_FIRST_BEAT, MARK_NO_TIMES,
             MARK_DEAD_END_RULES, MARK_TINY_CAP)


def maya_shape(**over) -> dict:
    """The exact definition shape that produced the live incident."""
    d = {
        "call_direction": "outbound",
        "languages": {"supported": ["en"], "default": "en"},
        "response_bank": {"opening_greeting": "Hello"},
        "calendar": {"type": "mock", "mock_slots": []},
        "call_flow": {
            "call_goal": "Qualify businesses and schedule a twenty-minute "
                         "online demonstration meeting with a specialist.",
            "scheduling": {},
            "stages": [
                {"id": "GREET", "order": 0,
                 "goal": "Once the caller responds, confirm their identity. "
                         "Once confirmed, introduce yourself from the company. "
                         "Politely ask if this is a good time to talk.",
                 "advance_when": {"intent": "affirmation", "max_turns": 3,
                                  "min_turns": 1}},
                {"id": "BOOK", "order": 1,
                 "goal": "Do NOT suggest any specific times or dates. "
                         "Instead, ask them when they would be free for a "
                         "follow-up later.",
                 "advance_when": {"max_turns": 5}},
            ],
        },
    }
    d.update(over)
    return d


def _warned(cop, merged, mark) -> bool:
    return any(mark in w for w in cop._quality_warnings(
        merged, include_wiring_lints=False))


# ── lint (i): caller-picks goals vs exact_n scheduling ──────────────────────

def test_lint_caller_picks_vs_exact_n(cop):
    assert _warned(cop, maya_shape(), MARK_CALLER_PICKS)
    fixed = maya_shape()
    fixed["call_flow"]["scheduling"] = {"style": "flexible"}
    assert not _warned(cop, fixed, MARK_CALLER_PICKS)


# ── lint (ii): unreachable beats on intent-advanced stages ──────────────────

def test_lint_unreachable_beats(cop):
    assert _warned(cop, maya_shape(), MARK_FIRST_BEAT)
    fixed = maya_shape()
    fixed["call_flow"]["stages"][0]["advance_when"]["min_turns"] = 2
    assert not _warned(cop, fixed, MARK_FIRST_BEAT)
    single_beat = maya_shape()
    single_beat["call_flow"]["stages"][0]["goal"] = \
        "Confirm you are speaking with the named contact."
    assert not _warned(cop, single_beat, MARK_FIRST_BEAT)


# ── lint (iii): booking language + empty calendar + exact_n ─────────────────

def test_lint_invented_times_empty_calendar(cop):
    assert _warned(cop, maya_shape(), MARK_NO_TIMES)
    with_slots = maya_shape(
        calendar={"type": "mock",
                  "mock_slots": [{"day": "Tomorrow", "time": "11:00 AM"}]})
    assert not _warned(cop, with_slots, MARK_NO_TIMES)
    flexible = maya_shape()
    flexible["call_flow"]["scheduling"] = {"style": "flexible"}
    assert not _warned(cop, flexible, MARK_NO_TIMES)


# ── production-nag guard: pramukh agents trigger NONE of the new lints ──────

@pytest.mark.parametrize("slug", ["pramukh", "pramukh_agent"])
@pytest.mark.parametrize("wiring", [True, False])
def test_production_agents_trigger_no_new_lints(cop, slug, wiring):
    raw = yaml.safe_load(
        (AGENTS_DIR / slug / "agent.yaml").read_text(encoding="utf-8"))
    merged = dataclasses.asdict(AgentDefinition.from_dict(raw))
    warnings = cop._quality_warnings(merged, include_wiring_lints=wiring)
    for mark in ALL_MARKS:
        assert not any(mark in w for w in warnings), (slug, mark, warnings)


# ── termination lints + normalization (call_flow.termination, 2026-07-29) ──

def test_lint_end_conditions_with_llm_end_call_off(cop):
    d = maya_shape()
    d["call_flow"]["termination"] = {
        "end_conditions": ["once booked, end the call"],
        "llm_end_call": False,
    }
    assert _warned(cop, d, MARK_DEAD_END_RULES)
    d["call_flow"]["termination"]["llm_end_call"] = True
    assert not _warned(cop, d, MARK_DEAD_END_RULES)


def test_lint_tiny_duration_cap(cop):
    d = maya_shape()
    d["call_flow"]["termination"] = {"max_call_minutes": 1}
    assert _warned(cop, d, MARK_TINY_CAP)
    d["call_flow"]["termination"]["max_call_minutes"] = 10
    assert not _warned(cop, d, MARK_TINY_CAP)


def test_lint_close_confirm_disabled(cop):
    """Turning the misheard-goodbye guard off is a real risk — flag it.
    Leaving it unset (the default, on) must stay silent."""
    d = maya_shape()
    assert not _warned(cop, d, MARK_NO_CLOSE_CONFIRM)
    d["call_flow"]["termination"] = {"confirm_before_end": False}
    assert _warned(cop, d, MARK_NO_CLOSE_CONFIRM)
    d["call_flow"]["termination"]["confirm_before_end"] = True
    assert not _warned(cop, d, MARK_NO_CLOSE_CONFIRM)


def test_normalize_close_confirm_coerced_to_bool(cop):
    notes: list[str] = []
    d = maya_shape()
    d["call_flow"]["termination"] = {"confirm_before_end": "no"}
    out = cop._normalize_definition(d, notes)
    assert out["call_flow"]["termination"]["confirm_before_end"] is True


def test_system_prompt_teaches_close_confirm(cop):
    """Copilot must know the confirm step exists platform-wide, so it never
    writes it into prompt text or claims agents hang up instantly."""
    p = cop._system_prompt()
    assert "confirm_before_end" in p
    assert "mishear" in p.lower()


def test_normalize_termination_shapes(cop):
    notes: list[str] = []
    d = maya_shape()
    d["call_flow"]["termination"] = {
        "end_conditions": "once booked, end the call",   # str → [str]
        "farewell_patterns": ["ok", "chalo rakhte hain"],  # "ok" is unsafe
        "max_call_minutes": "not-a-number",
        "silence_hangup": "yes please",                  # bad shape → dropped
    }
    out = cop._normalize_definition(d, notes)
    term = out["call_flow"]["termination"]
    assert term["end_conditions"] == ["once booked, end the call"]
    assert term["farewell_patterns"] == ["chalo rakhte hain"]
    assert "max_call_minutes" not in term
    assert "silence_hangup" not in term
    assert any("too short" in n for n in notes)


def test_normalize_termination_bad_root_shape(cop):
    notes: list[str] = []
    d = maya_shape()
    d["call_flow"]["termination"] = ["end", "the", "call"]
    out = cop._normalize_definition(d, notes)
    assert "termination" not in out["call_flow"]
    assert any("termination" in n for n in notes)


def test_no_lint_for_short_opening(cop):
    """'Hello' then wait is the intended style — it must never be nagged.
    (A bare-opening lint shipped and was reverted 2026-07-30: it pushed
    users into monologue openings.)"""
    assert not any(
        "opening" in w.lower()
        for w in cop._quality_warnings(maya_shape(),
                                       include_wiring_lints=False))


def test_enrich_never_touches_short_opening(cop):
    """Regression pin for the 2026-07-30 incident: a provided short opening
    ('Hello') must survive enrichment verbatim — the engine, not the script,
    delivers the identity intro when the caller responds."""
    notes: list[str] = []
    d = maya_shape(identity={"agent_name": "Maya", "company_name": "Acme"})
    cop._enrich_new_agent(d, notes)
    assert d["response_bank"]["opening_greeting"] == "Hello"
    assert not any("opening" in n for n in notes)


def test_enrich_new_agent_keeps_full_opening(cop):
    notes: list[str] = []
    d = maya_shape(
        identity={"agent_name": "Maya", "company_name": "Acme"},
        response_bank={"opening_greeting":
                       "Namaste! Acme se Maya bol rahi hoon."})
    cop._enrich_new_agent(d, notes)
    assert d["response_bank"]["opening_greeting"] == \
        "Namaste! Acme se Maya bol rahi hoon."


def test_enrich_fills_missing_opening_with_short_hello(cop):
    notes: list[str] = []
    d = maya_shape(response_bank={})
    cop._enrich_new_agent(d, notes)
    assert d["response_bank"]["opening_greeting"] == "Hello!"
    assert any("opening_greeting was empty" in n for n in notes)


def test_default_voices_use_recommended_speaker(cop):
    """New agents get the Sarvam-recommended speaker for their default
    language (the old hardcoded simran spoke English below tier), and ONE
    speaker across all languages."""
    d = cop._default_definition(
        "acme", {"languages": {"supported": ["en"], "default": "en"}})
    assert all(v["speaker"] == "ishita"
               for v in d["languages"]["tts_voices"].values())
    d2 = cop._default_definition(
        "acme", {"languages": {"supported": ["hi", "en"], "default": "hi"}})
    voices = d2["languages"]["tts_voices"]
    assert set(voices) == {"hi", "en"}
    assert all(v["speaker"] == "priya" for v in voices.values())


def test_normalize_missing_voice_inherits_existing_speaker(cop):
    """A missing per-language voice entry inherits the agent's existing
    speaker — never a different voice mid-call."""
    d = maya_shape(languages={
        "supported": ["hi", "en"], "default": "hi",
        "tts_voices": {"hi": {"model": "bulbul:v3", "speaker": "shubh",
                              "pace": 1.0}}})
    cop._enforce_invariants(d, [])
    assert d["languages"]["tts_voices"]["en"]["speaker"] == "shubh"


def test_normalize_unvoiced_agent_gets_recommended(cop):
    d = maya_shape()          # languages: en only, no tts_voices at all
    cop._enforce_invariants(d, [])
    assert d["languages"]["tts_voices"]["en"]["speaker"] == "ishita"


def test_normalize_connect_shapes(cop):
    notes: list[str] = []
    d = maya_shape()
    d["call_flow"]["connect"] = {"hello_fastpath": 0, "delivery_guard": 1}
    out = cop._normalize_definition(d, notes)
    conn = out["call_flow"]["connect"]
    assert conn["hello_fastpath"] is False
    assert conn["delivery_guard"] is True

    d2 = maya_shape()
    d2["call_flow"]["connect"] = "yes please"
    notes2: list[str] = []
    out2 = cop._normalize_definition(d2, notes2)
    assert "connect" not in out2["call_flow"]
    assert any("connect" in n for n in notes2)


def test_system_prompt_teaches_openings(cop):
    p = cop._system_prompt()
    for pin in ("OPENING STYLE — SHORT GREETING, THEN WAIT",
                "NEVER rewrite a user's opening",
                "call_flow.connect", "hello_fastpath", "delivery_guard"):
        assert pin in p, pin
    # The reverted doctrine must stay gone: it ordered the Copilot to
    # rewrite short openings into monologues.
    assert "OPENING LINES MUST IDENTIFY THE AGENT" not in p


def test_system_prompt_teaches_termination(cop):
    """The Copilot must be told termination requests are engine wiring —
    the old doctrine ordered it to refuse them."""
    p = cop._system_prompt()
    for pin in ("CALL-ENDING REQUIREMENTS GO IN call_flow.termination",
                "end_conditions", "farewell_patterns", "max_call_minutes",
                "silence_hangup"):
        assert pin in p, pin


# ── surgical tools now surface quality + the mission ────────────────────────

def test_update_stage_result_has_quality_and_call_goal(cop_with_agents):
    cop, agents_dir = cop_with_agents
    guard = {"user_turns": 1, "created_this_turn": False,
             "session": {"messages": []}, "user_message": "edit stage"}
    stage_id = yaml.safe_load(
        (agents_dir / "pramukh" / "agent.yaml").read_text(
            encoding="utf-8"))["call_flow"]["stages"][0]["id"]
    res, _s, ok, mutated = _run(cop._execute_tool("update_stage", {
        "name": "pramukh", "stage_id": stage_id,
        "fields": {"goal": "Greet the caller warmly, confirm how you can "
                           "help them today, and steer toward their need."},
    }, guard))
    assert ok and mutated
    assert isinstance(res.get("quality"), list)
    expected_goal = yaml.safe_load(
        (AGENTS_DIR / "pramukh" / "agent.yaml").read_text(
            encoding="utf-8"))["call_flow"].get("call_goal") or ""
    assert res.get("call_goal") == expected_goal[:300]
    assert "CALL MISSION" in res.get("call_goal_note", "")


def test_update_slot_result_has_quality(cop_with_agents):
    cop, _agents_dir = cop_with_agents
    guard = {"user_turns": 1, "created_this_turn": False,
             "session": {"messages": []}, "user_message": "edit slot"}
    res, _s, ok, mutated = _run(cop._execute_tool("update_slot", {
        "name": "pramukh", "slot_name": "name",
        "fields": {"max_asks": 2},
    }, guard))
    assert ok and mutated
    assert isinstance(res.get("quality"), list)


# ── the system prompt teaches the injection model ───────────────────────────

def test_system_prompt_teaches_injection_model(cop):
    p = cop._system_prompt()
    for pin in ("CALL MISSION", "never_rules", '"style": "flexible"',
                "find_text", "BANNED-PHRASE SWEEP",
                "TOP of the NEXT stage's goal"):
        assert pin in p, pin


def test_system_prompt_pace_report_then_ask(cop):
    """Speed complaints: report current pace + the 0.5–2.0 scale FIRST, then
    ask the user's preference — never silently adjust."""
    p = cop._system_prompt()
    assert "SPEED COMPLAINTS" in p
    assert "0.5–2.0 scale" in p
    assert "spoken_surfaces.pace" in p
    assert "ask what they'd prefer" in p


def test_get_agent_digest_has_pace(cop_with_agents):
    cop, agents_dir = cop_with_agents
    guard = {"user_turns": 1, "created_this_turn": False,
             "session": {"messages": []}, "user_message": "look"}
    res, _s, ok, mutated = _run(cop._execute_tool(
        "get_agent", {"name": "pramukh"}, guard))
    assert ok and mutated is False
    digest = res.get("spoken_surfaces") or {}
    pace = digest.get("pace") or {}
    # The digest reports the EFFECTIVE pace — the canonical definition fills
    # the 1.0 default when the raw YAML omits the key.
    raw = yaml.safe_load((agents_dir / "pramukh" / "agent.yaml").read_text(
        encoding="utf-8"))
    canonical = dataclasses.asdict(AgentDefinition.from_dict(raw))
    expected = canonical["languages"]["tts_voices"]
    assert set(pace) == set(expected) and pace
    for lg, v in expected.items():
        assert pace[lg] == v.get("pace"), lg


# ---------------------------------------------------------------------------
# RUNBOOK §37: the self-transition lint must fire on the path agents are
# actually edited through.
#
# The §35 lint was written for exactly one phrasing — "advance to WRAP_UP" in
# a stage goal with no branches — and then gated behind include_wiring_lints,
# which is False for update_agent and update_stage. On 2026-09-08 a Copilot
# goal rewrite put that phrasing straight back into the agent the lint had
# been written for, and the tool result came back "quality": [].
# ---------------------------------------------------------------------------

def _stage_flow(agency_goal: str, branches: list | None = None) -> dict:
    return {
        "call_direction": "outbound",
        "languages": {"supported": ["hi", "en"], "default": "hi",
                      "tts_voices": {"hi": {}, "en": {}}},
        "knowledge_base": {"portfolio_prompt_text": "x" * 120},
        "response_bank": {"opening_greeting": "Hello, am I speaking with X?",
                          "objection_responses": {"hi": ["ok"]}},
        "outbound": {"identity_confirm_line": {"hi": "hi"}},
        "call_flow": {"stages": [
            {"id": "INTRO", "order": 0, "goal": "Confirm who you are speaking to. " * 3,
             "branches": [], "advance_when": {"max_turns": 3}},
            {"id": "AGENCY_CHALLENGES", "order": 1, "goal": agency_goal,
             "branches": branches or [], "advance_when": {"max_turns": 6}},
            {"id": "WRAP_UP", "order": 2, "goal": "Close the call politely. " * 3,
             "branches": [], "advance_when": {"max_turns": 2}},
        ]},
    }


def _lint_text(cop, warnings: list) -> str:
    return " ".join(warnings)


def test_self_transition_lint_fires_on_an_update_for_the_touched_stage(cop):
    """The live rev-213 regression: a goal rewrite that ends 'and advance to
    WRAP_UP' on a stage with no branches."""
    before = _stage_flow("Ask about their agency challenges and listen. " * 3)
    after = _stage_flow("Ask about their agency challenges and listen. " * 3
                        + "and advance to WRAP_UP.")

    touched = cop._changed_stage_ids(before, after)
    assert touched == {"AGENCY_CHALLENGES"}

    w = cop._quality_warnings(after, include_wiring_lints=False,
                              touched_stages=touched)
    assert "AGENCY_CHALLENGES" in _lint_text(cop, w)
    assert "transition itself" in _lint_text(cop, w)


def test_the_same_lint_catches_terminate_the_call(cop):
    """WRAP_UP's rev-214 goal ended 'Then terminate the call immediately.' The
    model does not end calls — the platform does, after asking the caller."""
    before = _stage_flow("Ask about their agency challenges and listen. " * 3)
    after = _stage_flow("Ask about their agency challenges and listen. " * 3
                        + "Then terminate the call immediately.")
    w = cop._quality_warnings(after, include_wiring_lints=False,
                              touched_stages=cop._changed_stage_ids(before, after))
    assert "AGENCY_CHALLENGES" in _lint_text(cop, w)


def test_a_stage_that_really_branches_there_is_not_flagged(cop):
    """Wording that describes what the ENGINE does is fine once the stage has
    a real, immediate way to reach the destination."""
    goal = ("Ask about their agency challenges and listen. " * 3
            + "and advance to WRAP_UP.")
    after = _stage_flow(goal, branches=[
        {"when_slot": {"name": "marketing_challenges"}, "goto": "WRAP_UP"}])
    w = cop._quality_warnings(
        after, include_wiring_lints=False,
        touched_stages=cop._changed_stage_ids(_stage_flow("a " * 30), after))
    assert "transition itself" not in _lint_text(cop, w)


def test_untouched_stages_are_never_nagged_about(cop):
    """The reason wiring lints are suppressed on updates in the first place:
    an edit to one stage must not report someone else's pre-existing wiring."""
    d = _stage_flow("Ask about their agency challenges. " * 3
                    + "and advance to WRAP_UP.")
    w = cop._quality_warnings(d, include_wiring_lints=False,
                              touched_stages=set())
    assert "transition itself" not in _lint_text(cop, w)


def test_copilot_prompt_names_the_edit_do_not_regenerate_rule(cop):
    """§37: three Copilot edits each dropped a constraint the user had not
    mentioned, including the rule stopping invented lead numbers."""
    p = cop._system_prompt()
    assert "EDIT A FIELD, DO NOT REGENERATE IT" in p
    assert "IMPROVISED, NOT CONFIGURED" in p
    assert "NEVER write \"then terminate the call\"" in p


def test_latin_only_decline_patterns_are_flagged_only_when_they_replace(cop):
    """knowledge_base.objection_patterns decides whether a decline is HEARD.
    'extend' merges the platform defaults, which now carry both scripts, so
    such an agent is covered; a 'replace' list in Latin only is blind."""
    base = {
        "languages": {"supported": ["hi", "en"], "default": "hi",
                      "tts_voices": {"hi": {}, "en": {}}},
        "knowledge_base": {"portfolio_prompt_text": "x" * 120,
                           "objection_patterns": ["not interested", "abhi nahi"]},
        "response_bank": {"opening_greeting": "Hello, am I speaking with X?",
                          "objection_responses": {"hi": ["ok"]}},
        "call_flow": {"stages": [
            {"id": "A", "order": 0, "goal": "Do the first thing well. " * 4},
            {"id": "B", "order": 1, "goal": "Do the second thing well. " * 4},
        ]},
    }
    import copy
    replaced = copy.deepcopy(base)
    replaced["knowledge_base"]["objection_patterns_mode"] = "replace"
    assert any("objection_patterns is Latin-script only" in x
               for x in cop._quality_warnings(replaced))

    extended = copy.deepcopy(base)
    extended["knowledge_base"]["objection_patterns_mode"] = "extend"
    assert not any("objection_patterns is Latin-script only" in x
                   for x in cop._quality_warnings(extended))


def test_a_goal_describing_platform_termination_is_not_flagged(cop):
    """The §37 WRAP_UP goal says 'the platform ends the call itself … never
    ask whether to cut the call yourself'. Flagging that would push authors to
    delete the sentence that stops the model hanging up."""
    d = _stage_flow(
        "Close the call politely and thank them. " * 3
        + "Say nothing after it — the platform ends the call itself, and it "
          "will ask the caller's permission first. Never ask whether to cut "
          "the call yourself.")
    w = cop._quality_warnings(
        d, include_wiring_lints=False,
        touched_stages={"AGENCY_CHALLENGES"})
    assert not any("transition itself" in x for x in w)
