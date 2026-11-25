"""Regression gate for the 2026-08-12 Pramukh outbound call review.

The reviewed call did two things the owner rejected:

  1. It captured location → configuration → budget and then asked
     "shall we set a time for a site visit?" without ever naming a project.
  2. Asked three times about properties in Vapi — a city whose projects are
     in its own catalogue — it answered "I'll check with the team" and
     steered back to booking the Surat visit.

Both had concrete causes, all pinned here:

  * WARM_WELCOME's advance_when waited on `finalized_property`, a slot only
    fillable at the END of the call, so the greeting stage owned the caller's
    whole qualification and the recommend stage never ran;
  * `project_recommended` had no writer for the agent's OWN speech, so every
    consumer of "has anything been recommended yet" read None forever;
  * `known_projects` carried "3 BHK", which latched matched_entity to a
    configuration and told the model not to ask which one they wanted;
  * no catalogue item carried `location`, so under STRICT knowledge grounding
    a by-city question looked like a thing the agent had never been told;
  * every match rule keyed on `budget` / `location` while the slots are named
    `budget_range` / `preferred_location`, so the whole rule set was dead.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import AGENTS_DIR

from app.agent_definition.catalog import evaluate_match_rules
from app.agent_definition.schema import AgentDefinition
from app.voice.conversation import ConversationRouter

SLUG = "outbound_lead_qualifier-outbound"
# A FROZEN snapshot, not the live YAML: agent files are owner-editable and
# gitignored, and this suite went red the morning an owner rebuilt theirs.
# Snapshot taken 2026-09-24 after the city-memory rebuild (RUNBOOK §54).
YAML = Path(__file__).parent / "fixtures" / "prakhar_outbound_2026_09_24.yaml"


@pytest.fixture(autouse=True)
def _no_network_llm(stub_llm):
    """Nothing here may reach the real model."""


def _router() -> ConversationRouter:
    agent = AgentDefinition.from_yaml(YAML)
    r = ConversationRouter(agent_def=agent, default_language="en")
    r._available_calendar_slots = lambda: list(r._def.calendar.mock_slots or [])
    r.set_call_context(direction="outbound", contact_name="Angelo P Chery",
                       contact_phone="9876543210")
    return r


def _qualify(r: ConversationRouter) -> None:
    """The reviewed call's facts, as the extractors would have stored them."""
    r.memory.call_state.domain_fields["location"] = "Surat"
    r.memory.call_state.domain_fields["property_type"] = "3BHK"
    r.memory.call_state.domain_fields["budget"] = "4.10 to 5 crore"
    r.state_mgr.set_externally("preferred_location", "Surat", source="system")
    r.state_mgr.set_externally("purpose", "Self-use", source="system")
    r.state_mgr.set_externally("configuration_required", "3 BHK", source="system")
    r.state_mgr.set_externally("purchase_timeline", "Immediate", source="system")
    r.state_mgr.set_externally("budget_range", "₹4.10 to 5 crore", source="system")


# ---------------------------------------------------------------------------
# 1. The stage machine actually reaches the recommend stage
# ---------------------------------------------------------------------------

def test_greeting_stage_releases_the_call():
    """WARM_WELCOME must not hold the caller through their own qualification.

    With a CRM-known contact the name slot is seeded before the greeting, so
    all_collect_filled is true on the first pass. The old config could only
    escape via max_turns: 6 — six caller turns, the entire reviewed call.
    """
    r = _router()
    r.state.stage = r._post_greeting_stage()     # what process_stream does
    assert r.state.stage == "WARM_WELCOME"
    r.state.discovery_turns = 1
    r._maybe_advance_stage()
    assert r.state.stage != "WARM_WELCOME"


def test_flow_reaches_recommend_before_any_scheduling_stage():
    """Replay the reviewed call's turns and land on the recommendation.

    The caller confirmed identity, said it was a good time, gave city / size /
    purpose on one turn and timeline / budget on the next. In the reviewed
    call the agent was STILL in WARM_WELCOME at that point and asked for a
    site visit; here the same turns must end on RECOMMEND_THE_FIT, having
    passed no scheduling stage.
    """
    r = _router()
    order = [s.id for s in r._def.call_flow.stages]
    assert order.index("RECOMMEND_THE_FIT") < order.index("INVITE_SITE_VISIT")

    r.state.stage = r._post_greeting_stage()
    assert r.state.stage == "WARM_WELCOME"
    turns = [
        # ("intent", {slot: value}) — the stage machine trails the caller by
        # one turn: they answer the question the PREVIOUS stage asked.
        ("affirmation", {}),                                  # "yes, you are"
        ("affirmation", {}),                                  # "haan, bolo"
        (None, {"preferred_location": "Surat", "purpose": "Self-use",
                "configuration_required": "3 BHK"}),
        (None, {"purchase_timeline": "Immediate",
                "budget_range": "₹4.10 to 5 crore"}),
    ]
    seen = [r.state.stage]
    for intent, slots in turns:
        if intent:
            r.state.last_intent = intent
        for slot, val in slots.items():
            r.state_mgr.set_externally(slot, val, source="system")
        r.state.discovery_turns += 1
        r._maybe_advance_stage()
        seen.append(r.state.stage)

    assert r.state.stage == "RECOMMEND_THE_FIT", f"walked {seen}"
    assert "INVITE_SITE_VISIT" not in seen
    assert "CONFIRM_AND_CLOSE" not in seen


# ---------------------------------------------------------------------------
# 2. The recommend-before-schedule gate
# ---------------------------------------------------------------------------

def test_gate_shut_before_the_caller_has_said_anything():
    r = _router()
    assert r._recommendation_gate_open() is False
    assert "RECOMMEND BEFORE YOU SCHEDULE" not in r._build_system_prompt()


def test_gate_opens_once_qualified_and_forbids_the_visit_ask():
    r = _router()
    _qualify(r)
    assert r._recommendation_gate_open() is True
    prompt = r._build_system_prompt()
    assert "━━ RECOMMEND BEFORE YOU SCHEDULE ━━" in prompt
    assert "Do NOT propose, ask about, or hint at a visit" in prompt


def test_gate_closes_when_the_agent_names_a_project():
    r = _router()
    _qualify(r)
    r._store_last_response(
        "Based on what you've shared, Pramukh Revanta in Vesu could be a good fit.")
    assert r.state.project_recommended == "Pramukh Revanta"
    assert r._recommendation_gate_open() is False
    assert "RECOMMEND BEFORE YOU SCHEDULE" not in r._build_system_prompt()


def test_gate_ignores_the_agent_merely_saying_a_configuration():
    """"3 BHK" in the agent's own reply is not a recommendation."""
    r = _router()
    _qualify(r)
    r._store_last_response("Got it — a 3 BHK. And what budget did you have in mind?")
    assert r.state.project_recommended is None
    assert r._recommendation_gate_open() is True


def test_gate_is_off_for_agents_without_a_catalogue():
    r = _router()
    _qualify(r)
    r._def.knowledge_base.catalog = []
    assert r._recommendation_gate_open() is False


def test_gate_is_owner_disableable():
    r = _router()
    _qualify(r)
    r._def.call_flow.recommend_before_schedule = "off"
    assert r._recommendation_gate_open() is False


# ---------------------------------------------------------------------------
# 3. A configuration is not a project
# ---------------------------------------------------------------------------

def test_bhk_never_latches_as_the_matched_project():
    r = _router()
    assert r._extract_project_from_text("3 bhk, mere ko 3 bhk property chahiye") is None
    r._extract_data_from_input("3 bhk, mere ko 3 bhk property chahiye", "statement")
    assert "matched_entity" not in r.memory.call_state.domain_fields
    assert "DO NOT ask which one they want" not in r._build_system_prompt()


def test_a_real_project_name_still_latches():
    r = _router()
    assert r._extract_project_from_text("tell me about Revanta") == (
        "Pramukh Revanta", "Vesu, Surat")


# ---------------------------------------------------------------------------
# 4. A by-city question is answerable
# ---------------------------------------------------------------------------

def test_every_catalogue_item_carries_a_location():
    agent = AgentDefinition.from_yaml(YAML)
    missing = [i.get("name") for i in agent.knowledge_base.catalog
               if not str(i.get("location") or "").strip()]
    assert missing == []


def test_vapi_projects_are_named_with_their_city_in_the_prompt():
    r = _router()
    prompt = r._build_system_prompt()
    for line in prompt.splitlines():
        if line.startswith("▸ Pramukh Aura"):
            assert "Vapi" in line
            assert "₹34 Lakh" in line
            break
    else:                                        # pragma: no cover - failure path
        pytest.fail("Pramukh Aura is not in the rendered knowledge base")
    assert "Every location covered above: Surat, Vapi, Silvassa, Umbergaon" in prompt


def test_strict_grounding_tells_the_agent_to_answer_what_it_was_given():
    r = _router()
    prompt = r._build_system_prompt()
    assert "ANSWER IT with those exact details FIRST" in prompt
    assert "Only when it is genuinely NOT covered above, deflect" in prompt


def test_agent_is_no_longer_gagged_on_prices():
    r = _router()
    prompt = r._build_system_prompt()
    assert "NEVER give specific pricing" not in prompt
    assert "NEVER quote prices" not in prompt


# ---------------------------------------------------------------------------
# 5. Match rules key on the slot names the agent actually has
# ---------------------------------------------------------------------------

def test_match_rules_fire_on_the_real_slot_names():
    agent = AgentDefinition.from_yaml(YAML)
    hits = evaluate_match_rules(agent.knowledge_base,
                                {"preferred_location": "Vapi"})
    names = {n for h in hits for n in h["recommend"]}
    assert {"pramukh-sahaj", "pramukh-aura"} <= names


def test_match_rules_tolerate_the_owner_writing_the_short_key():
    """`when: {budget: ...}` against a slot named `budget_range` used to be a
    silent no-op — every rule of the live agent was dead this way."""
    agent = AgentDefinition.from_yaml(YAML)
    kb = agent.knowledge_base
    kb.match_rules = [{"when": {"budget": ">=1.1 Cr"},
                       "recommend": ["pramukh-revanta"], "reason": "premium"}]
    assert evaluate_match_rules(kb, {"budget_range": "₹4.10 to 5 crore"})


def test_top_of_portfolio_reaches_a_multi_crore_caller():
    r = _router()
    _qualify(r)
    prompt = r._build_system_prompt()
    assert "━━ MATCH RULES (deterministic — follow these) ━━" in prompt
    assert "Agastya" in prompt


def test_match_rules_survive_the_first_project_being_named():
    """A ₹4-5 Cr caller shown a ₹1.11 Cr project must still see the top tier.

    The candidate list used to render only in a pitch/recommend stage, so the
    single most useful block went dark exactly when the model started
    recommending from a discovery stage.
    """
    r = _router()
    _qualify(r)
    r._store_last_response("Pramukh Revanta starts at ₹1.11 Cr.")
    assert r._recommendation_gate_open() is False
    prompt = r._build_system_prompt()
    assert "━━ MATCH RULES (deterministic — follow these) ━━" in prompt
    assert "Agastya" in prompt


def test_match_rules_stay_dark_before_the_caller_says_anything():
    r = _router()
    assert "━━ MATCH RULES" not in r._build_system_prompt()
