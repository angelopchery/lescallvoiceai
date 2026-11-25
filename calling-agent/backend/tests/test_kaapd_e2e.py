"""
Kaapd end-to-end acceptance test — THE regression floor for the 2026-07-15
call-fidelity remediation (deploy/PLAN-call-fidelity.md, F0.3).

Replays the incident pipeline: the Kaapd brief's extracted fields →
document-merge into the interview draft → create_agent through the REAL
create path (enrichment + engine boot-check) → the emitted YAML loaded by the
REAL engine → a scripted 6-turn conversation mirroring live call cf6b0e23.

Every assertion below is a symptom from the incident transcript:
- the call never left CONFIRM_PERSON          → must leave it by turn 2
- discovery questions never reached a prompt  → PITCH must render the ask
- real-estate objections/rules were spoken    → zero real-estate leakage
- the user's objection scripts were dropped   → they must render
- six phantom 10:00 AM slots steered booking  → no calendar band
- the model invented products                 → strict grounding band renders
"""
from __future__ import annotations

import asyncio
import sys

import pytest

from conftest import REPO_ROOT


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def cop(tmp_path, monkeypatch):
    monkeypatch.setenv("LESCALL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    dash = str(REPO_ROOT / "dashboard")
    if dash not in sys.path:
        sys.path.insert(0, dash)
    import agent_builder_api as builder
    import agent_copilot_api as cop
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    monkeypatch.setattr(builder, "_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(cop, "_AGENTS_DIR", agents_dir)
    return cop, agents_dir


# What the upgraded extractor yields from deploy/kaapd_test_brief.txt —
# deterministic recorded output (the brief's own facts, nothing invented).
_KAAPD_FIELDS = {
    "agent_name": "Karan",
    "company_name": "Kaapd",
    "role_description": "outbound fabric-sales agent calling retailers",
    "call_direction": "outbound",
    "contact": "9876543210",
    "call_goal": "A retailer agreeing to receive samples and a follow-up.",
    "opening_greeting": "नमस्ते जी! क्या मेरी बात दुकान के मालिक से हो रही है? "
                        "मैं कापड़, सूरत से करण बोल रहा हूँ।",
    "closing_responses": {"en": "Thank you for your time — I'll send the "
                                "samples and our latest catalogue across."},
    "stages": [
        {"id": "CONFIRM_PERSON", "goal": "Karan confirms he has the right "
         "person by asking whether he is speaking with the owner of the shop, "
         "using the contact's name where available."},
        {"id": "INTRODUCE_SELF", "goal": "Karan introduces himself as Karan "
         "from Kaapd and checks if it's a good time to talk before anything "
         "else."},
        {"id": "PITCH", "goal": "If it's a good time, Karan gives a crisp "
         "line on who we are and then asks about the fabrics they currently "
         "stock, the monthly quantity they move, and the city they operate "
         "from, in that order."},
        {"id": "MATCH_AND_OFFER", "goal": "After gathering all three answers, "
         "Karan matches their needs to what we do and offers to send a "
         "sample kit."},
        {"id": "CLOSING", "goal": "Karan thanks them and politely ends the "
         "call."},
    ],
    "slots": [
        {"name": "current_stock", "required": True,
         "question": {"hi": "आपके पास अभी कौन से कपड़े हैं?",
                      "en": "What fabrics do you currently stock?"}},
        {"name": "monthly_quantity", "required": True,
         "question": {"hi": "महीने में कितना माल उठता है?",
                      "en": "What quantity do you move monthly?"}},
        {"name": "operating_city", "required": True,
         "question": {"hi": "आप किस शहर से काम करते हैं?",
                      "en": "Which city do you operate from?"}},
    ],
    "objection_playbook": [
        {"id": "already_have_suppliers",
         "patterns": ["already have suppliers", "supplier fix hai"],
         "answer": {"en": "That's perfectly fine — most of our long-term "
                          "partners started while keeping their existing "
                          "suppliers. One sample kit costs nothing to "
                          "compare."}},
        {"id": "rates_too_high",
         "patterns": ["rates too high", "rate zyada hai"],
         "answer": {"hi": "Rates depend on the construction and quantity — "
                          "manufacturers quote better than traders, and "
                          "you'd be buying at the source."}},
    ],
    # The brief names ONE product family and no itemized list — the
    # known_projects→catalog backstop must still produce a catalog item.
    "known_projects": {"polyester knitted fabrics":
                       ["Polyester Knitted Fabrics", "Standard"]},
    "portfolio_prompt_text": "Kaapd is a polyester knitted fabrics "
        "manufacturer in Surat. Knitting, dyeing and digital printing all "
        "happen in-house, which is why fresh fabric launches go out every "
        "single month and leading fashion brands across India have stayed "
        "long-term.",
    "locations": ["Surat"],
    "languages": {"supported": ["hi", "en"], "default": "hi"},
    "personality": {"tone": "friendly", "energy": "medium",
                    "ai_disclosure": "admit"},
    "guardrails": {"never_rules": [
        "pretends to make cotton or natural fibres",
        "quotes prices or rate cards on the phone"]},
    "whatsapp_promise": {"assets": ["catalogue PDF", "launch updates"]},
    "callback_policy": {"capture_exact_time": True},
}


def _build_kaapd(cop_mod, agents_dir):
    session = {"messages": [
        {"role": "user", "content": "build the Kaapd agent from this brief"}],
        "transcript": [], "meta": {}}
    guard = {"user_turns": 3, "created_this_turn": False,
             "session": session, "user_message": "brief attached — use defaults and build the agent",
             "sid": "kaapd1"}
    _ch, notes, progress, _closed = cop_mod._merge_extracted_into_draft(
        session, dict(_KAAPD_FIELDS))
    # Close whatever the document could not answer, exactly like a user
    # saying "use defaults for the rest".
    iv = cop_mod._interview_state(session)
    progress = cop_mod.interview_progress(iv["draft"], iv["skipped"])
    for open_id in list(progress["open_layers"]):
        guard.pop("skips_this_turn", None)  # each skip on its own turn
        _run(cop_mod._execute_tool("skip_layer", {
            "layer_id": open_id,
            "user_words": "skip that, use defaults"}, guard))
    res, _s, ok, _m = _run(cop_mod._execute_tool("create_agent", {
        "name": "kaapd_e2e", "definition": {}}, guard))
    assert ok and res["status"] == "created"
    return agents_dir / res["name"] / "agent.yaml", res


def _router_for(yaml_path, lang="hi"):
    from app.agent_definition.schema import AgentDefinition
    from app.voice.conversation import ConversationRouter
    agent = AgentDefinition.from_yaml(yaml_path)
    router = ConversationRouter(agent_def=agent, default_language=lang)
    router._available_calendar_slots = (
        lambda: list(router._def.calendar.mock_slots or []))
    router.call_direction = "outbound"
    router.outbound_contact_name = "Angelo P"
    router.outbound_contact_phone = "9876543210"
    router.state.language = lang
    return router, agent


def test_kaapd_brief_builds_an_agent_that_runs_its_own_flow(cop):
    cop_mod, agents_dir = cop
    yaml_path, res = _build_kaapd(cop_mod, agents_dir)
    router, agent = _router_for(yaml_path)

    # ── The incident's stage trace, corrected ────────────────────────────
    # Live call cf6b0e23: GREETING → CONFIRM_PERSON on turn 1, then STUCK
    # for all six turns. Turn 1 was an affirmation ("बताओ हां जी वही बात हो
    # रही है") — the built agent must leave CONFIRM_PERSON on it.
    router.state.stage = agent.call_flow.stages[0].id
    assert router.state.stage == "CONFIRM_PERSON"
    router.state.discovery_turns = 1
    router.state.last_intent = "affirmation"
    router._maybe_advance_stage()
    assert router.state.stage != "CONFIRM_PERSON", (
        "the built agent still cannot leave its confirm stage on a yes — "
        "the incident's stage stall is NOT fixed")

    # And even a caller who never clearly affirms cannot be stranded:
    router2, _ = _router_for(yaml_path)
    router2.state.stage = "CONFIRM_PERSON"
    router2.state.last_intent = "general"
    for turn in range(1, 5):
        router2.state.discovery_turns = turn
        router2._maybe_advance_stage()
        if router2.state.stage != "CONFIRM_PERSON":
            break
    assert router2.state.stage != "CONFIRM_PERSON", (
        "max_turns escape missing — a shy caller strands the flow")

    # ── PITCH must drive the user's three discovery questions ───────────
    router3, _ = _router_for(yaml_path)
    router3.state.stage = "PITCH"
    prompt = router3._build_system_prompt()
    assert "Ask ONLY ONE question next" in prompt, (
        "the strong ask-directive is missing at PITCH — the user's "
        "discovery questions do not drive the call")
    assert "आपके पास अभी कौन से कपड़े हैं" in prompt

    pitch = next(s for s in agent.call_flow.stages if s.id == "PITCH")
    assert set(pitch.collect) == {"current_stock", "monthly_quantity",
                                  "operating_city"}
    assert pitch.advance_when.get("all_collect_filled") is True
    assert pitch.advance_when.get("max_turns")

    # ── Zero real-estate leakage ─────────────────────────────────────────
    for stage in agent.call_flow.stages:
        router3.state.stage = stage.id
        p = router3._build_system_prompt()
        for phrase in ("site visit", "zero stalls", "RERA", "builders",
                       "property pitches", "whole portfolio"):
            assert phrase not in p, (
                f"real-estate leakage {phrase!r} in stage {stage.id} — the "
                "generic rules pack / objection fallback is not in effect")

    # ── The user's objection scripts reach the prompt ────────────────────
    router3.state.stage = "PITCH"
    p = router3._build_system_prompt()
    assert "already have suppliers" in p
    assert "sample kit costs nothing to compare" in p

    # ── Strict grounding renders; phantom calendar does not ─────────────
    assert "KNOWLEDGE GROUNDING (STRICT)" in p
    assert "ONLY source of truth" in p
    assert "APPOINTMENT CALENDAR" not in p, (
        "a calendar band rendered for a flow that never schedules")

    # ── The backstop produced a catalog from the single product family ──
    assert agent.knowledge_base.catalog, "known_projects→catalog backstop failed"
    assert agent.knowledge_base.catalog[0]["name"] == "Polyester Knitted Fabrics"
    assert "Polyester Knitted Fabrics" in p

    # ── Verbatim scripts still land ──────────────────────────────────────
    opening = _run(router3.generate_opening())
    assert "मैं कापड़, सूरत से करण बोल रहा हूँ" in opening.text

    # ── The engine boot-check certified it at create ─────────────────────
    assert not [n for n in res["notes"] if "Engine check failed" in n]


def test_kaapd_slots_capture_and_advance_out_of_pitch(cop):
    """A caller who answers the three questions moves PITCH forward — the
    declarative all_collect_filled gate is live for the built agent."""
    cop_mod, agents_dir = cop
    yaml_path, _res = _build_kaapd(cop_mod, agents_dir)
    router, agent = _router_for(yaml_path)
    router.state.stage = "PITCH"
    for name in ("current_stock", "monthly_quantity", "operating_city"):
        router.state_mgr.set_externally(name, "answered", source="system")
    router.state.discovery_turns = 1
    router._maybe_advance_stage()
    assert router.state.stage == "MATCH_AND_OFFER"


def test_kaapd_incident_b_shape_is_refused_at_create(cop, monkeypatch):
    """A definition whose prompt build crashes must be refused at create with
    an actionable error — never ship and answer live calls with a canned
    fallback loop (Incident B)."""
    cop_mod, agents_dir = cop
    import backend.app.voice.prompt_blocks as pb

    _orig = pb.firm_rules_block

    def _boom(agent_def, lang="en"):
        raise TypeError("'bool' object is not iterable")
    monkeypatch.setattr(pb, "firm_rules_block", _boom)
    try:
        session = {"messages": [{"role": "user", "content": "brief"}],
                   "transcript": [], "meta": {}}
        guard = {"user_turns": 3, "created_this_turn": False,
                 "session": session, "user_message": "skip the rest, go ahead, create it", "sid": "s2"}
        cop_mod._merge_extracted_into_draft(session, dict(_KAAPD_FIELDS))
        iv = cop_mod._interview_state(session)
        progress = cop_mod.interview_progress(iv["draft"], iv["skipped"])
        for open_id in list(progress["open_layers"]):
            guard.pop("skips_this_turn", None)  # each skip on its own turn
            _run(cop_mod._execute_tool("skip_layer", {
                "layer_id": open_id, "user_words": "skip"}, guard))
        with pytest.raises(ValueError, match="crash on a live call"):
            _run(cop_mod._execute_tool("create_agent", {
                "name": "kaapd_broken", "definition": {}}, guard))
        for _slug in ("kaapd_broken", "kaapd_broken-inbound", "kaapd_broken-outbound"):
            assert not (agents_dir / _slug).exists(), (
                "a definition that crashes the engine was WRITTEN anyway")
    finally:
        monkeypatch.setattr(pb, "firm_rules_block", _orig)
