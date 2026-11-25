"""
F4 + F15 — stage-collect discovery gate, filler governors/ack pools, closing
language policy, and objection_patterns_mode call-site semantics.

Pins today's behaviour:
- a stage that DECLARES collect slots gets the strong 'Ask ONLY ONE question
  next' directive at any index; without collect (index >= 2) the weave-in
  directive renders instead;
- _filler_allowed: 0/0 defaults are ungoverned; filler_max_per_call and
  filler_min_gap_turns suppress correctly;
- _pick_filler: filler_ack_extended recognises Devanagari affirmations →
  approval pool; flag off → thinking pool;
- _closing_language: policy '' = state.language even against a hi-majority
  call; 'dominant' = majority caller language; no turns = state.language;
- objection_patterns_mode 'extend' merges the universal deferral set with the
  agent's custom patterns; mode absent (replace) disables the universal set.

No network, no LLM — helpers called directly; detect_intent is exercised via
the exact call-site merge logic from conversation.py's process() intake.
"""
from __future__ import annotations

from app.agent_definition.schema import AgentDefinition
from app.voice.conversation import (
    ConversationRouter,
    detect_intent,
    _DEFAULT_OBJECTION_PATTERNS,
)


def make_router(default_language="en", **over) -> ConversationRouter:
    d = {
        "agent_id": "t_filler",
        "version": "1.0.0",
        "identity": {"agent_name": "Asha", "company_name": "TestCo"},
        "languages": {"supported": ["en", "hi"], "default": "en",
                      "tts_voices": {}},
        "call_flow": {"stages": [
            {"id": "S1", "order": 0, "goal": "one", "turns_until_advance": 8},
            {"id": "S2", "order": 1, "goal": "two", "turns_until_advance": 99},
        ]},
        "response_bank": {"opening_greeting": "hi"},
    }
    d.update(over)
    agent = AgentDefinition.from_dict(d)
    router = ConversationRouter(agent_def=agent, default_language=default_language)
    router._available_calendar_slots = lambda: []  # deterministic, no I/O
    return router


# ── F4(a) — stage-collect discovery gate ────────────────────────────────────

THREE_SLOTS = [
    {"name": "s1", "priority": 1, "required": True, "question": {"en": "s1?"}},
    {"name": "s2", "priority": 2, "required": True, "question": {"en": "s2?"}},
    {"name": "s3", "priority": 3, "required": True, "question": {"en": "s3?"}},
]


def _collect_router(collect):
    return make_router(
        slots=THREE_SLOTS,
        call_flow={"stages": [
            {"id": "A", "order": 0, "goal": "a", "turns_until_advance": 8},
            {"id": "B", "order": 1, "goal": "b", "turns_until_advance": 8},
            {"id": "C", "order": 2, "goal": "c", "turns_until_advance": 8,
             "collect": collect},
        ]},
    )


def test_collect_stage_gets_strong_one_question_directive():
    router = _collect_router(["s1", "s2"])
    router.state.stage = "C"  # index 2 — beyond the position-based gate
    prompt = router._build_system_prompt()
    assert "Ask ONLY ONE question next" in prompt
    assert "Weave in AT MOST one" not in prompt


def test_stage_without_collect_gets_weave_in_directive():
    router = _collect_router([])
    router.state.stage = "C"
    prompt = router._build_system_prompt()
    assert "Weave in AT MOST one" in prompt
    assert "Ask ONLY ONE question next" not in prompt


# ── F15(b) — _filler_allowed governors ──────────────────────────────────────

def test_filler_defaults_are_ungoverned():
    router = make_router()  # no llm_config overrides → 0/0
    router._fillers_played = 5
    router.state.turn_count = 10
    router._last_filler_turn = 10
    assert router._filler_allowed() is True


def test_filler_max_per_call_caps():
    router = make_router(llm_config={"filler_max_per_call": 1})
    router.state.turn_count = 5   # past the greeting-turn suppression
    assert router._filler_allowed() is True
    router._fillers_played = 1
    assert router._filler_allowed() is False


# ── stage suppression (2026-07-23): no filler before the agent has even
#    introduced itself, none glued onto the goodbye ────────────────────────

def test_filler_suppressed_on_first_llm_turn():
    router = make_router()
    router.state.turn_count = 0
    assert router._filler_allowed() is False
    router.state.turn_count = 1
    assert router._filler_allowed() is False
    router.state.turn_count = 2
    assert router._filler_allowed() is True


def test_filler_suppressed_while_closing():
    router = make_router()
    router.state.turn_count = 8
    router.state.is_closing = True
    assert router._filler_allowed() is False


# ── platform floor: per-agent filler_timeout_s can never undercut the
#    measured-LLM-latency floor (fillers below it ADD delay) ───────────────

def test_filler_timeout_floored(monkeypatch):
    monkeypatch.delenv("FILLER_TIMEOUT_FLOOR_S", raising=False)
    router = make_router(llm_config={"filler_timeout_s": 0.6})
    assert router._effective_filler_timeout() == 1.5


def test_filler_timeout_agent_value_above_floor_wins(monkeypatch):
    monkeypatch.delenv("FILLER_TIMEOUT_FLOOR_S", raising=False)
    router = make_router(llm_config={"filler_timeout_s": 2.5})
    assert router._effective_filler_timeout() == 2.5


def test_filler_timeout_floor_env_override(monkeypatch):
    monkeypatch.setenv("FILLER_TIMEOUT_FLOOR_S", "0.8")
    router = make_router(llm_config={"filler_timeout_s": 0.6})
    assert router._effective_filler_timeout() == 0.8


def test_filler_min_gap_turns():
    router = make_router(llm_config={"filler_min_gap_turns": 2})
    router.state.turn_count = 10
    router._last_filler_turn = 9   # gap 1 < 2
    assert router._filler_allowed() is False
    router._last_filler_turn = 8   # gap 2 >= 2
    assert router._filler_allowed() is True


# ── F15(c) — _pick_filler ack pools ─────────────────────────────────────────

APPROVAL_HI = {"Haanji!", "Zaroor!", "Bilkul!", "Ji haan!", "Haan, bilkul!"}
THINKING_HI_BUILTIN = {"Ek minute...", "Batati hoon...",
                       "Ek second, dekh leti hoon...", "Haan, check karti hoon..."}


def test_devanagari_affirmation_with_flag_hits_approval_pool():
    """filler_ack_extended routes Devanagari affirmations to the APPROVAL
    pool. Python's \\w strips combining vowel signs/virama ('हाँ' → 'ह'), so
    the flag's tokenizer preserves the Devanagari/Gujarati blocks — without
    that, the extended token set could never match and every Devanagari
    approval got 'एक मिनट…' (the Kaapd verbal tic)."""
    router = make_router(default_language="hi",
                         llm_config={"filler_ack_extended": True})
    router.memory.add_user("हाँ बिल्कुल")
    filler = router._pick_filler()
    assert filler in APPROVAL_HI
    assert filler not in THINKING_HI_BUILTIN


def test_devanagari_affirmation_hits_thinking_pool_without_flag():
    router = make_router(default_language="hi")  # filler_ack_extended defaults False
    router.memory.add_user("हाँ बिल्कुल")
    filler = router._pick_filler()
    assert filler in THINKING_HI_BUILTIN
    assert filler not in APPROVAL_HI


def test_romanized_affirmation_hits_approval_pool():
    """Control: the approval pool itself works — romanized affirmations
    survive the word cleaning and match the base token set (flag-independent)."""
    router = make_router(default_language="hi")
    router.memory.add_user("haan bilkul")
    filler = router._pick_filler()
    assert filler in APPROVAL_HI
    assert filler not in THINKING_HI_BUILTIN


# ── F15(d) — _closing_language policy ───────────────────────────────────────

def test_closing_language_default_policy_uses_state_language():
    router = make_router()
    for i in range(5):
        router.memory.add_user(f"turn {i}", language="hi")
    router.state.language = "en"
    assert router._closing_language() == "en"  # today's pinned behaviour


def test_closing_language_dominant_policy_uses_majority():
    router = make_router(languages={
        "supported": ["en", "hi"], "default": "en", "tts_voices": {},
        "closing_language_policy": "dominant"})
    for i in range(5):
        router.memory.add_user(f"turn {i}", language="hi")
    router.state.language = "en"
    assert router._closing_language() == "hi"


def test_closing_language_dominant_no_turns_falls_back():
    router = make_router(languages={
        "supported": ["en", "hi"], "default": "en", "tts_voices": {},
        "closing_language_policy": "dominant"})
    router.state.language = "en"
    assert router._closing_language() == "en"


# ── F15(e) — objection_patterns_mode call-site semantics ────────────────────

def _call_site_intent(router: ConversationRouter, text: str) -> str:
    """The exact objection-pattern merge from conversation.py's process()."""
    _kb = router._def.knowledge_base
    _obj_patterns = _kb.objection_patterns
    if (_obj_patterns and str(getattr(
            _kb, "objection_patterns_mode", "replace")) == "extend"):
        _obj_patterns = list(_DEFAULT_OBJECTION_PATTERNS) + list(_obj_patterns)
    return detect_intent(
        text,
        router.state.stage,
        location_aliases=frozenset(_kb.location_aliases),
        objection_patterns=_obj_patterns,
        time_patterns=_kb.time_patterns,
    )


def test_extend_mode_keeps_universal_and_adds_custom():
    router = make_router(knowledge_base={
        "objection_patterns": ["polyester doesn't sell"],
        "objection_patterns_mode": "extend"})
    assert _call_site_intent(router, "not interested") == "objection"
    assert _call_site_intent(router, "polyester doesn't sell today") == "objection"


def test_replace_mode_disables_universal_set():
    router = make_router(knowledge_base={
        "objection_patterns": ["polyester doesn't sell"]})  # mode absent = replace
    assert _call_site_intent(router, "not interested") != "objection"
    # the custom pattern itself still classifies
    assert _call_site_intent(router, "polyester doesn't sell today") == "objection"
