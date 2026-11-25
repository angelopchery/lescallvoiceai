"""A caller's city outranks every other fact — and a closed slot stays closed.

Found on the Prakhar (Pramukh Group) agent, calls 903 and 904 of 2026-09-24.
The caller said Chala, in Vapi, and a budget of 40-50 lakh; the agent agreed a
visit at Pramukh Vedanta in Chala and then said "Based on what you've shared, I
think Pramukh Aranya II in Surat could be a genuinely good fit." Four platform
defects lined up behind that one sentence:

1. match rules are a union and each fires on its own condition — the budget
   rule pointed at Surat and nothing asked which city the caller was in;
2. the tool wrote preferred_location='Chala', and 'Chala' != 'Vapi', so the
   Vapi rule never fired at all — the same write path stored 'KBS' as a
   configuration and 'Bhagwati' as the chosen project;
3. caller_name='caller' was stored as a name, so the agent stopped asking;
4. book_appointment was passed 2025-09-25 for "tomorrow" four times and every
   attempt was refused as already past, so no booking ever reached the stage
   machine and it wandered back into the recommend stage.

Offline: no LLM calls, no live agent YAML.
"""

from __future__ import annotations

import pytest

from app.agent_definition.catalog import (
    caller_city,
    evaluate_match_rules,
    scope_hits_to_city,
)
from app.agent_definition.schema import AgentDefinition
from app.voice import prompt_blocks as pb
from app.voice.conversation import ConversationRouter, _drop_non_values
from app.voice.llm_tools import clean_slot_args, is_placeholder_value
from app.voice.state_manager import build_state_manager


# ---------------------------------------------------------------------------
# A small Pramukh-shaped agent: two cities, one project per tier.
# ---------------------------------------------------------------------------

CATALOG = [
    {"id": "aura", "name": "Pramukh Aura", "location": "Chala, Vapi",
     "configurations": ["2 BHK", "3 BHK"], "price_range": "₹34-40 Lakh"},
    {"id": "sahaj", "name": "Pramukh Sahaj", "location": "Chala, Vapi",
     "configurations": ["1 BHK", "2 BHK"], "price_range": "₹22-29 Lakh"},
    {"id": "aranya-ii", "name": "Pramukh Aranya II", "location": "Godadara, Surat",
     "configurations": ["3 BHK"], "price_range": "₹70 Lakh onwards"},
    {"id": "revanta", "name": "Pramukh Revanta", "location": "Vesu, Surat",
     "configurations": ["3 BHK"], "price_range": "₹1.11 Cr onwards"},
    {"id": "orbit", "name": "Orbit Series",
     "location": "Vesu Canal Road, Surat and Vapi",
     "configurations": ["Shops"]},
    {"id": "mystery", "name": "Somewhere Heights", "configurations": ["2 BHK"]},
]

# Deliberately the OLD, location-blind shape: budget rules that name a city
# without saying so.
RULES = [
    {"when": {"preferred_location": "Vapi"}, "recommend": ["sahaj", "aura"],
     "reason": "Everything in Vapi"},
    {"when": {"budget_range": "<=70L"}, "recommend": ["aranya-ii"],
     "reason": "Mid-range option in Surat"},
    {"when": {"budget_range": ">=1.1 Cr"}, "recommend": ["revanta"],
     "reason": "Premium Surat"},
]


def _agent_dict(**over) -> dict:
    d = {
        "agent_id": "t_city", "version": "1.0.0",
        "identity": {"agent_name": "Prakhar", "company_name": "Pramukh Group"},
        "languages": {"supported": ["en", "hi"], "default": "en",
                      "tts_voices": {}},
        "call_flow": {
            "recommend_before_schedule": "on",
            "stages": [
                {"id": "WELCOME", "order": 0, "goal": "welcome",
                 "role": "greeting", "collect": ["caller_name"],
                 "advance_when": {"all_collect_filled": True, "max_turns": 2}},
                {"id": "DISCOVER", "order": 1, "goal": "discover",
                 "role": "discovery",
                 "collect": ["preferred_location", "configuration_required"],
                 "advance_when": {"all_collect_filled": True, "max_turns": 4}},
                {"id": "RECOMMEND", "order": 2, "goal": "recommend",
                 "role": "recommend", "collect": ["finalized_property"],
                 "advance_when": {"all_collect_filled": True, "max_turns": 3}},
                {"id": "CLOSE", "order": 3, "goal": "close", "role": "closing",
                 "turns_until_advance": 99},
            ],
        },
        "knowledge_base": {
            "known_projects": {}, "locations": ["Surat", "Vapi"],
            "portfolio_prompt_text": "", "catalog": CATALOG,
            "match_rules": RULES,
        },
        "response_bank": {"opening_greeting": "Hello"},
        "slots": [
            {"name": "caller_name", "type": "text", "label": "Name",
             "priority": 1, "question": {"en": "Who am I speaking with?"}},
            {"name": "preferred_location", "type": "location", "priority": 2,
             "label": "Preferred city",
             "enum_values": ["Surat", "Vapi"],
             "enum_synonyms": {"Vapi": ["chala", "वापी"],
                               "Surat": ["vesu", "godadara"]},
             "question": {"en": "Which city — Surat or Vapi?"}},
            {"name": "configuration_required", "type": "enum", "priority": 3,
             "label": "Configuration",
             "enum_values": ["1 BHK", "2 BHK", "3 BHK"],
             "enum_synonyms": {"2 BHK": ["2bhk", "two bhk", "do bhk"],
                               "3 BHK": ["3bhk", "three bhk", "teen bhk"]},
             "question": {"en": "What size — 2 BHK or 3 BHK?"}},
            {"name": "budget_range", "type": "money", "priority": 4,
             "label": "Budget", "question": {"en": "What range?"}},
            # Closed vocabulary WITHOUT typed extraction: the LLM decides when
            # the caller has settled on one; the vocabulary decides what may be
            # stored.
            {"name": "finalized_property", "type": "text", "priority": 5,
             "label": "Project to visit",
             "enum_values": ["Pramukh Aura", "Pramukh Sahaj",
                             "Pramukh Aranya II", "Pramukh Revanta"],
             "enum_synonyms": {"Pramukh Aura": ["aura"],
                               "Pramukh Sahaj": ["sahaj"],
                               "Pramukh Aranya II": ["aranya ii", "aranya 2"],
                               "Pramukh Revanta": ["revanta"]},
             "question": {"en": "Which one would you like to see?"}},
        ],
    }
    d.update(over)
    return d


def _agent(**over) -> AgentDefinition:
    return AgentDefinition.from_dict(_agent_dict(**over))


@pytest.fixture(autouse=True)
def _offline(stub_llm):
    """Nothing here may reach the real model."""


# ---------------------------------------------------------------------------
# 1. Match rules are scoped to the caller's city
# ---------------------------------------------------------------------------

def test_the_budget_rule_alone_used_to_point_at_the_other_city():
    """The exact call-904 state, evaluated WITHOUT scoping: Surat wins."""
    kb = _agent().knowledge_base
    from app.agent_definition.catalog import _raw_match_hits
    raw = _raw_match_hits(kb, {"preferred_location": "Vapi",
                               "budget_range": "50 Lakhs"})
    assert [h["recommend"] for h in raw] == [["sahaj", "aura"], ["aranya-ii"]]


def test_a_vapi_caller_is_never_offered_surat():
    kb = _agent().knowledge_base
    hits = evaluate_match_rules(kb, {"preferred_location": "Vapi",
                                     "budget_range": "50 Lakhs"})
    assert [h["recommend"] for h in hits] == [["sahaj", "aura"]]


def test_a_locality_resolves_to_its_city_through_the_catalogue():
    kb = _agent().knowledge_base
    assert caller_city(kb, {"preferred_location": "Chala"}) == "Vapi"
    assert caller_city(kb, {"preferred_location": "vesu, surat"}) == "Surat"
    assert caller_city(kb, {"preferred_location": "Godadara"}) == "Surat"
    # A place we do not build in, or no place at all, scopes nothing.
    assert caller_city(kb, {"preferred_location": "Ahmedabad"}) is None
    assert caller_city(kb, {"budget_range": "50 Lakhs"}) is None
    # Only PLACE-named slots are consulted — a name that happens to be a city
    # is not a location.
    assert caller_city(kb, {"caller_name": "Surat Patel"}) is None


def test_scoping_keeps_items_in_both_cities_and_items_with_no_location():
    kb = _agent().knowledge_base
    hits = [{"recommend": ["orbit", "mystery", "revanta"], "reason": ""}]
    assert scope_hits_to_city(kb, "Vapi", hits) == [
        {"recommend": ["orbit", "mystery"], "reason": ""}]
    # Unknown id → nothing known about it → kept.
    assert scope_hits_to_city(kb, "Vapi", [{"recommend": ["nope"], "reason": ""}]) \
        == [{"recommend": ["nope"], "reason": ""}]
    # No city → untouched.
    assert scope_hits_to_city(kb, None, hits) == hits


def test_prompt_names_the_city_and_only_its_projects():
    agent = _agent()
    body = "\n".join(pb.match_rules_block(
        agent, {"preferred_location": "Vapi", "budget_range": "50 Lakhs"},
        "recommend"))
    assert "Pramukh Sahaj, Pramukh Aura" in body
    assert "Aranya" not in body
    assert "The caller's city is Vapi" in body


def test_prompt_says_so_when_the_city_removes_every_candidate():
    """Silence here is the bug: with no band the model reads the whole
    catalogue and offers the other city anyway."""
    agent = _agent()
    # The slot holds the LOCALITY (the pre-canonicalization shape of call
    # 904): 'Chala' equals no rule's "Vapi", so only the premium budget rule
    # fires — and it points at Surat.
    state = {"preferred_location": "Chala", "budget_range": "1.2 Cr"}
    assert [h["recommend"] for h in evaluate_match_rules(
        agent.knowledge_base, state)] == []
    body = "\n".join(pb.match_rules_block(agent, state, "recommend"))
    assert "The caller wants Vapi" in body
    assert "NEVER offer a project in another city" in body
    assert "Revanta" not in body
    # ...but when nothing fired at all, the band stays absent as before.
    agent.knowledge_base.match_rules = []
    assert pb.match_rules_block(agent, {"preferred_location": "Vapi"},
                                "recommend") == []


def test_live_prompt_for_the_call_904_state(monkeypatch):
    """The whole router: Chala + 50 lakh → the Vapi options, never Surat."""
    monkeypatch.setenv("LLM_TOOLS_MODE", "on")
    r = ConversationRouter(agent_def=_agent(), default_language="en")
    r.state_mgr.merge_state({"preferred_location": "Chala"}, source="tool")
    r.state_mgr.merge_state({"budget_range": "50 Lakhs"}, source="regex")
    prompt = r._build_system_prompt()
    assert "━━ MATCH RULES" in prompt
    assert "Pramukh Aranya II" not in prompt.split("━━ MATCH RULES")[1]
    assert "The caller's city is Vapi" in prompt


# ---------------------------------------------------------------------------
# 2. A slot with a vocabulary stores the canonical value or nothing
# ---------------------------------------------------------------------------

def _sm():
    return build_state_manager(_agent(), language="en")


def test_a_locality_written_by_the_tool_is_stored_as_its_city():
    sm = _sm()
    sm.merge_state({"preferred_location": "Chala"}, source="tool")
    assert sm.state["preferred_location"] == "Vapi"
    sm2 = _sm()
    sm2.merge_state({"preferred_location": "somewhere in chala"}, source="llm")
    assert sm2.state["preferred_location"] == "Vapi"
    sm3 = _sm()
    sm3.merge_state({"preferred_location": "वापी"}, source="llm")
    assert sm3.state["preferred_location"] == "Vapi"


def test_noise_is_rejected_not_stored():
    sm = _sm()
    sm.merge_state({"configuration_required": "KBS",
                    "finalized_property": "Bhagwati",
                    "preferred_location": "Ahmedabad"}, source="tool")
    assert sm.state["configuration_required"] is None
    assert sm.state["finalized_property"] is None
    assert sm.state["preferred_location"] is None
    rejected = [e for e in sm.audit_log if "REJECTED" in (e.utterance or "")]
    assert len(rejected) == 3


def test_values_canonicalize_and_the_longest_alias_wins():
    sm = _sm()
    sm.merge_state({"configuration_required": "3bhk"}, source="tool")
    assert sm.state["configuration_required"] == "3 BHK"
    sm.merge_state({"finalized_property": "Aranya 2 please"}, source="llm")
    assert sm.state["finalized_property"] == "Pramukh Aranya II"
    # Case / spacing / punctuation do not matter.
    sm2 = _sm()
    sm2.merge_state({"finalized_property": "  REVANTA. "}, source="tool")
    assert sm2.state["finalized_property"] == "Pramukh Revanta"


def test_an_open_slot_is_untouched_by_the_vocabulary_gate():
    sm = _sm()
    sm.merge_state({"caller_name": "Baki"}, source="answer")
    assert sm.state["caller_name"] == "Baki"
    sm.merge_state({"budget_range": "50 Lakhs"}, source="regex")
    assert sm.state["budget_range"] == "50 Lakhs"


def test_a_same_city_rewrite_is_not_an_audit_event():
    sm = _sm()
    sm.merge_state({"preferred_location": "Vapi"}, source="answer")
    n = len(sm.audit_log)
    sm.merge_state({"preferred_location": "chala"}, source="tool")
    assert sm.state["preferred_location"] == "Vapi"
    assert len(sm.audit_log) == n


def test_the_typed_extractor_still_reads_a_locality_from_speech():
    sm = _sm()
    sm.observe_user("i was saying somewhere in chala")
    assert sm.state["preferred_location"] == "Vapi"
    sm.observe_user("3 bhk, mere ko 3 bhk chahiye")
    assert sm.state["configuration_required"] == "3 BHK"
    # type: text + vocabulary → no eager fill on a mere mention...
    assert sm.state["finalized_property"] is None
    # ...but a tool write of that mention canonicalizes.
    sm.merge_state({"finalized_property": "aura"}, source="tool")
    assert sm.state["finalized_property"] == "Pramukh Aura"


# ---------------------------------------------------------------------------
# 3. A role word is not a name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["caller", "Caller", "the caller",
                                   "Customer", "sir", "Ma'am", "user"])
def test_role_words_are_placeholders(value):
    assert is_placeholder_value(value)
    assert clean_slot_args({"caller_name": value}) == {}
    assert _drop_non_values({"caller_name": value}, "tool") == {}


def test_real_names_are_not_placeholders():
    for v in ("Baki", "Angelo P Chery", "Alwym", "Surat Patel"):
        assert not is_placeholder_value(v)
        assert clean_slot_args({"caller_name": v}) == {"caller_name": v}
