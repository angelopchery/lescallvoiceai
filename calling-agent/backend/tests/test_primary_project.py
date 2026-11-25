"""One project is THE pitch; every other one is information (RUNBOOK §55).

Built for Vikrant (Pramukh Group, 2026-09-24): the owner wanted an agent that
actively pitches ONLY Pramukh Swarit — 4 & 5 BHK bungalows in New Chala,
Vapi — keeps every other Pramukh project in its knowledge base, answers any
question about any of them, and never volunteers one. The match rules could
not express that: they are a union of "when X, recommend Y" and the caller's
city outranks them (§54), so a caller in Surat would have been offered Surat
projects and a caller who said nothing would have been offered nothing.

`knowledge_base.primary_project = {id, mode, reason}` is the platform answer:
  * the primary is ALWAYS the first candidate, before any slot is filled;
  * `exclusive` (default) makes it the ONLY candidate — the rules stay as
    data, their hits never reach the band;
  * `lead` lets the scoped rule hits follow it, minus itself;
  * the knowledge band renders it FIRST, with its detail keys (RERA, address,
    amenities…), and labels the rest information-only;
  * a caller in another city is told where the primary is and asked whether
    that could work — never told the primary "is not an option";
  * no primary → every band is byte-identical to before.

Offline: no LLM calls, no live agent YAML.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent_definition.catalog import (
    evaluate_match_rules,
    primary_city,
    primary_project,
    render_catalog_prompt,
    render_item_details,
)
from app.agent_definition.controls import unknown_paths
from app.agent_definition.schema import AgentDefinition
from app.voice import prompt_blocks as pb


SWARIT = {
    "id": "pramukh-swarit", "name": "Pramukh Swarit",
    "aliases": ["Swarit"], "location": "New Chala, Vapi",
    "configurations": ["4 BHK", "5 BHK"], "price_range": "Price on request",
    "availability": "Ready to move",
    "positioning_tag": "flagship luxury bungalows in Vapi",
    "project_type": "Bungalows",
    "rera": "PR/GJ/VALSAD/PARDI/Others/RAA10449/140722",
    "address": "Opposite Pramukh Sahaj, New Chala, Vapi",
    "amenities": ["Swimming Pool", "Gym", "Zen Garden"],
    "contact": {"Vapi office": "+91 74062 58000"},
    "not_published": ["price", "floor plans"],
    # data-only keys — must never reach the prompt
    "media": {"video": "https://example.invalid/swarit.mp4"},
    "contact_email": "inquiry@example.invalid",
    "faq": [{"q": "RERA?", "a": "yes"}],
}

CATALOG = [
    SWARIT,
    {"id": "sahaj", "name": "Pramukh Sahaj", "location": "Chala, Vapi",
     "configurations": ["1 BHK", "2 BHK"], "price_range": "₹22-29 Lakh"},
    {"id": "aura", "name": "Pramukh Aura", "location": "Chala, Vapi",
     "configurations": ["2 BHK", "3 BHK"], "price_range": "₹34-40 Lakh"},
    {"id": "aranya-ii", "name": "Pramukh Aranya II", "location": "Godadara, Surat",
     "configurations": ["3 BHK"], "price_range": "₹70 Lakh onwards"},
]

RULES = [
    {"when": {"preferred_location": "Vapi"}, "recommend": ["pramukh-swarit", "sahaj", "aura"],
     "reason": "everything in Vapi"},
    {"when": {"preferred_location": "Surat"}, "recommend": ["aranya-ii"],
     "reason": "the Surat entry point"},
    {"when": {"budget_range": "<=30L"}, "recommend": ["sahaj"],
     "reason": "the value option"},
]


def _agent_dict(primary=None, **over) -> dict:
    kb = {"known_projects": {}, "locations": ["Surat", "Vapi"],
          "portfolio_prompt_text": "", "catalog": CATALOG, "match_rules": RULES}
    if primary is not None:
        kb["primary_project"] = primary
    d = {
        "agent_id": "t_primary", "version": "1.0.0",
        "identity": {"agent_name": "Vikrant", "company_name": "Pramukh Group"},
        "languages": {"supported": ["en", "hi"], "default": "en", "tts_voices": {}},
        "call_flow": {
            "recommend_before_schedule": "on",
            "stages": [
                {"id": "WELCOME", "order": 0, "goal": "welcome", "role": "greeting",
                 "collect": ["caller_name"],
                 "advance_when": {"all_collect_filled": True, "max_turns": 2}},
                {"id": "PRESENT", "order": 1, "goal": "present", "role": "recommend",
                 "collect": ["finalized_property"],
                 "advance_when": {"all_collect_filled": True, "max_turns": 3}},
                {"id": "CLOSE", "order": 2, "goal": "close", "role": "closing",
                 "turns_until_advance": 99},
            ],
        },
        "knowledge_base": kb,
        "response_bank": {"opening_greeting": "Hello"},
        "slots": [
            {"name": "caller_name", "type": "text", "label": "Name", "priority": 1,
             "question": {"en": "Who am I speaking with?"}},
            {"name": "preferred_location", "type": "location", "priority": 2,
             "label": "Preferred city", "enum_values": ["Surat", "Vapi"],
             "enum_synonyms": {"Vapi": ["chala"], "Surat": ["vesu"]},
             "question": {"en": "Which city?"}},
            {"name": "budget_range", "type": "money", "priority": 3,
             "label": "Budget", "question": {"en": "What range?"}},
            {"name": "finalized_property", "type": "text", "priority": 4,
             "label": "Project to visit",
             "enum_values": ["Pramukh Swarit", "Pramukh Sahaj", "Pramukh Aura",
                             "Pramukh Aranya II"],
             "enum_synonyms": {"Pramukh Swarit": ["swarit"]},
             "question": {"en": "Shall we see Swarit?"}},
        ],
    }
    d.update(over)
    return d


def _agent(primary=None, **over) -> AgentDefinition:
    return AgentDefinition.from_dict(_agent_dict(primary, **over))


EXCLUSIVE = {"id": "pramukh-swarit", "mode": "exclusive",
             "reason": "our flagship bungalows in Vapi"}


# ---------------------------------------------------------------------------
# 1. Schema + control surface: the field is real
# ---------------------------------------------------------------------------

def test_primary_project_survives_from_dict():
    kb = _agent(EXCLUSIVE).knowledge_base
    assert kb.primary_project == EXCLUSIVE
    pp = primary_project(kb)
    assert pp and pp["id"] == "pramukh-swarit" and pp["mode"] == "exclusive"
    assert pp["name"] == "Pramukh Swarit" and pp["item"] is CATALOG[0]


def test_primary_project_is_a_known_control():
    """The Copilot fence must not refuse the key it is told to write."""
    assert unknown_paths(["knowledge_base.primary_project",
                          "knowledge_base.primary_project.id",
                          "knowledge_base.primary_project.mode",
                          "knowledge_base.primary_project.reason"]) == []
    bad = unknown_paths(["knowledge_base.primary_project.priority"])
    assert bad and "no setting called 'priority'" in bad[0][1]


def test_unknown_id_is_no_primary():
    """A primary the catalogue cannot render must not demote the rest."""
    kb = _agent({"id": "nope"}).knowledge_base
    assert primary_project(kb) is None
    assert evaluate_match_rules(kb, {"preferred_location": "Surat"}) == [
        {"recommend": ["aranya-ii"], "reason": "the Surat entry point"}]
    assert "PRIMARY PROJECT" not in render_catalog_prompt(kb)


def test_bad_mode_reads_as_exclusive():
    kb = _agent({"id": "pramukh-swarit", "mode": "whatever"}).knowledge_base
    assert primary_project(kb)["mode"] == "exclusive"


def test_primary_city_resolves_through_the_item_location():
    assert primary_city(_agent(EXCLUSIVE).knowledge_base) == "Vapi"
    assert primary_city(_agent().knowledge_base) is None


# ---------------------------------------------------------------------------
# 2. Recommendation candidates
# ---------------------------------------------------------------------------

def test_no_primary_is_byte_identical():
    kb = _agent().knowledge_base
    hits = evaluate_match_rules(kb, {"preferred_location": "Vapi"})
    assert hits == [{"recommend": ["pramukh-swarit", "sahaj", "aura"],
                     "reason": "everything in Vapi"}]
    assert all("primary" not in h for h in hits)


def test_primary_is_recommended_before_any_slot_is_filled():
    """'Recommends Swarit by default' — nothing known, still one candidate."""
    kb = _agent(EXCLUSIVE).knowledge_base
    assert evaluate_match_rules(kb, {}) == [
        {"recommend": ["pramukh-swarit"], "reason": "our flagship bungalows in Vapi",
         "primary": True}]


def test_exclusive_drops_every_other_hit():
    kb = _agent(EXCLUSIVE).knowledge_base
    for slots in ({"preferred_location": "Vapi"},
                  {"preferred_location": "Surat"},
                  {"budget_range": "25 lakh"},
                  {"preferred_location": "Vapi", "budget_range": "25 lakh"}):
        hits = evaluate_match_rules(kb, slots)
        assert [h["recommend"] for h in hits] == [["pramukh-swarit"]], slots


def test_lead_keeps_scoped_hits_after_the_primary():
    kb = _agent({**EXCLUSIVE, "mode": "lead"}).knowledge_base
    hits = evaluate_match_rules(kb, {"preferred_location": "Vapi", "budget_range": "25 lakh"})
    assert hits[0]["primary"] is True
    assert hits[0]["recommend"] == ["pramukh-swarit"]
    # the Vapi rule minus the primary itself, then the budget rule
    assert [h["recommend"] for h in hits[1:]] == [["sahaj", "aura"], ["sahaj"]]


def test_lead_still_scopes_the_rest_to_the_callers_city():
    """§54 survives: in lead mode a Surat caller never sees Vapi rule hits —
    only the primary (named with its city) and the Surat rule."""
    kb = _agent({**EXCLUSIVE, "mode": "lead"}).knowledge_base
    hits = evaluate_match_rules(kb, {"preferred_location": "Surat", "budget_range": "25 lakh"})
    assert [h["recommend"] for h in hits] == [["pramukh-swarit"], ["aranya-ii"]]


def test_empty_reason_gets_a_default_clause():
    kb = _agent({"id": "pramukh-swarit"}).knowledge_base
    assert evaluate_match_rules(kb, {})[0]["reason"] == "the primary project — lead with it"


# ---------------------------------------------------------------------------
# 3. Knowledge band
# ---------------------------------------------------------------------------

def test_knowledge_band_renders_the_primary_first_with_its_details():
    text = render_catalog_prompt(_agent(EXCLUSIVE).knowledge_base)
    lines = text.split("\n")
    assert lines[0].startswith("★ PRIMARY PROJECT — the one you pitch: Pramukh Swarit")
    assert "(our flagship bungalows in Vapi)" in lines[0]
    assert lines[1].startswith("▸ Pramukh Swarit — in New Chala, Vapi, 4 BHK/5 BHK, Price on request")
    for needle in ("Type: Bungalows.", "RERA: PR/GJ/VALSAD/PARDI/Others/RAA10449/140722.",
                   "Address: Opposite Pramukh Sahaj, New Chala, Vapi.",
                   "Amenities: Swimming Pool, Gym, Zen Garden.",
                   "Contact: Vapi office +91 74062 58000.",
                   "NOT published — never guess: price, floor plans."):
        assert needle in lines[1], needle
    assert "INFORMATION ONLY" in lines[2]
    assert "back to Pramukh Swarit" in lines[2]
    # every other project is still there, exactly as before, and only once
    assert text.count("▸ Pramukh Sahaj — in Chala, Vapi, 1 BHK/2 BHK, ₹22-29 Lakh") == 1
    assert text.count("▸ Pramukh Swarit") == 1
    assert "Pramukh Aranya II" in text


def test_data_only_keys_never_reach_the_prompt():
    text = render_catalog_prompt(_agent(EXCLUSIVE).knowledge_base)
    assert "example.invalid" not in text
    assert "swarit.mp4" not in text
    assert "faq" not in text.lower()


def test_lead_mode_labels_the_rest_as_followers():
    text = render_catalog_prompt(_agent({**EXCLUSIVE, "mode": "lead"}).knowledge_base)
    assert "may follow Pramukh Swarit" in text
    assert "INFORMATION ONLY" not in text


def test_knowledge_band_without_primary_is_unchanged():
    kb = _agent().knowledge_base
    text = render_catalog_prompt(kb)
    assert "PRIMARY" not in text
    assert text.split("\n")[0].startswith("▸ Pramukh Swarit — in New Chala, Vapi")


def test_item_details_are_empty_for_a_plain_item():
    """Every pre-§55 catalogue renders byte-for-byte: no detail keys, no text."""
    assert render_item_details(CATALOG[1]) == ""
    assert render_item_details({}) == ""


# ---------------------------------------------------------------------------
# 4. Per-turn bands
# ---------------------------------------------------------------------------

def _band(agent, slots, role="recommend"):
    return "\n".join(pb.match_rules_block(agent, slots, role))


def test_match_rules_band_names_the_primary_with_nothing_known():
    text = _band(_agent(EXCLUSIVE), {})
    assert "▸ Recommend: Pramukh Swarit — our flagship bungalows in Vapi" in text
    assert "PRIMARY PROJECT: Pramukh Swarit is the project you pitch" in text
    assert "information only" in text
    assert "Pramukh Sahaj" not in text


def test_match_rules_band_tells_a_surat_caller_where_swarit_is():
    text = _band(_agent(EXCLUSIVE), {"preferred_location": "Surat"})
    assert "The caller has said Surat; Pramukh Swarit is in Vapi." in text
    assert "ask whether Vapi could work for them" in text
    assert "do not pitch Surat projects on your own" in text
    # the §54 line that would call the primary "NOT an option" is gone
    assert "is NOT an option" not in text
    assert "Pramukh Aranya II" not in text


def test_match_rules_band_for_a_vapi_caller():
    text = _band(_agent(EXCLUSIVE), {"preferred_location": "Chala"})
    assert "The caller's city is Vapi — Pramukh Swarit is right there." in text


def test_match_rules_band_without_primary_is_unchanged():
    text = _band(_agent(), {"preferred_location": "Surat"})
    assert "▸ Recommend: Pramukh Aranya II — the Surat entry point" in text
    assert "a project anywhere else is NOT an option" in text
    assert "PRIMARY" not in text


def test_full_band_is_still_stage_gated_but_the_reminder_is_not():
    """Live call 908 (§55.4): the caller said "a flat in Vapi" in the greeting
    stage, the full band was gated off, and the agent listed the two Vapi
    apartment projects — never the primary. Early stages now carry the
    short ━━ PRIMARY PROJECT ━━ band; the full MATCH RULES band stays gated."""
    early = "\n".join(pb.match_rules_block(_agent(EXCLUSIVE), {}, "greeting"))
    assert "━━ PRIMARY PROJECT ━━" in early
    assert "Pramukh Swarit is the one project this call is about" in early
    assert "(4 BHK/5 BHK, Bungalows, in New Chala, Vapi)" in early
    assert "Pramukh Swarit is the FIRST project you name, every time" in early
    assert "even when they ask for a flat, an apartment" in early
    assert "Do NOT name any other project before Pramukh Swarit" in early
    assert "▸ Recommend:" not in early and "MATCH RULES" not in early
    forced = "\n".join(pb.match_rules_block(_agent(EXCLUSIVE), {}, "greeting", force=True))
    assert "MATCH RULES" in forced and "▸ Recommend: Pramukh Swarit" in forced
    # no primary → early stages render nothing, exactly as before
    assert pb.match_rules_block(_agent(), {}, "greeting") == []
    assert pb.match_rules_block(_agent(), {}, "discovery") == []


def test_reminder_band_tells_a_surat_caller_where_the_primary_is():
    text = "\n".join(pb.match_rules_block(_agent(EXCLUSIVE), {"preferred_location": "Surat"}, "discovery"))
    assert "They said Surat; Pramukh Swarit is in Vapi" in text
    vapi = "\n".join(pb.match_rules_block(_agent(EXCLUSIVE), {"preferred_location": "Chala"}, "discovery"))
    assert "They said" not in vapi


def test_by_location_line_leads_with_the_primary():
    """'answered from the list above' read as 'list what is in Vapi' (call
    908). With a primary the by-location answer leads with it."""
    text = "\n".join(pb.portfolio_block(_agent(EXCLUSIVE), "en"))
    assert ("A caller asking about any of them is answered from the list above, "
            "not deferred — and the answer LEADS with Pramukh Swarit (it is in "
            "New Chala, Vapi)") in text
    plain = "\n".join(pb.portfolio_block(_agent(), "en"))
    assert "must be answered from the list above, not deferred." in plain
    assert "LEADS with" not in plain


def test_strict_grounding_names_the_primary_first():
    strict = {"honesty": {"knowledge_grounding": "strict"}}
    text = "\n".join(pb.knowledge_grounding_block(_agent(EXCLUSIVE, guardrails=strict)))
    assert "Where Pramukh Swarit answers the question" in text
    assert "FIRST and BY NAME" in text
    assert "never a list you volunteer" in text
    plain = "\n".join(pb.knowledge_grounding_block(_agent(guardrails=strict)))
    assert "Pramukh Swarit" not in plain
    assert pb.knowledge_grounding_block(_agent(EXCLUSIVE)) == []


def test_recommend_first_gate_names_the_primary():
    text = "\n".join(pb.recommend_first_block(_agent(EXCLUSIVE), {}))
    assert "First recommend Pramukh Swarit BY NAME" in text
    assert "ONE or TWO" not in text
    plain = "\n".join(pb.recommend_first_block(_agent(), {}))
    assert "ONE or TWO specific options BY NAME" in plain


# ---------------------------------------------------------------------------
# 5. The live Vikrant agent, frozen (never the owner's YAML)
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"
RAVI = {d: FIXTURES / f"ravi_{d}_2026_09_24.yaml" for d in ("inbound", "outbound")}


@pytest.mark.parametrize("direction", sorted(RAVI))
def test_ravi_snapshot_pitches_only_swarit(direction):
    agent = AgentDefinition.from_yaml(RAVI[direction])
    kb = agent.knowledge_base
    assert agent.identity.agent_name == "Ravi"
    # Owner: primary language Hindi; Gujarati and English stay supported.
    assert agent.languages.default == "hi"
    assert set(agent.languages.supported) == {"hi", "en", "gu"}
    assert primary_project(kb)["name"] == "Pramukh Swarit"
    assert primary_city(kb) == "Vapi"
    # Every Prakhar project is still in the knowledge base
    names = {str(i.get("name")) for i in kb.catalog}
    for kept in ("Pramukh Sahaj", "Pramukh Aura", "Pramukh Vedanta", "Pramukh Revanta",
                 "Pramukh Aranya II", "Pramukh Sangath", "Orbit Series", "Agastya"):
        assert kept in names, kept
    assert len(kb.catalog) == 16
    # ...and nothing but Swarit is ever a candidate
    for slots in ({}, {"preferred_location": "Surat"},
                  {"preferred_location": "Vapi", "configuration_required": "2 BHK"}):
        assert [h["recommend"] for h in evaluate_match_rules(kb, slots)] == [["pramukh-swarit"]]
    text = render_catalog_prompt(kb)
    assert text.startswith("★ PRIMARY PROJECT — the one you pitch: Pramukh Swarit")
    assert "RERA: PR/GJ/VALSAD/PARDI/Others/RAA10449/140722" in text
    assert "₹2.50 Cr – ₹3.50 Cr" in text.split("\n")[1]
    assert "5,010 sq ft" in text and "46 bungalows" in text
    # the owner's brief (2026-09-24) is in the spoken knowledge…
    assert "Ideal for: business owners and industrialists" in text
    assert "toddler-friendly spaces" in text
    # …and its data-only parts are not
    assert "qualification_questions" not in text and "conversion_goals" not in text
    slots = {s.name for s in agent.slots}
    for wanted in ("calling_from", "family_size", "amenity_priorities",
                   "site_visit_interest", "whatsapp_same_number"):
        assert wanted in slots, wanted


@pytest.mark.parametrize("direction", sorted(RAVI))
def test_ravi_snapshot_has_no_prakhar_left(direction):
    raw = RAVI[direction].read_text(encoding="utf-8")
    for old in ("Prakhar", "प्रखर", "પ્રખર", "Vikrant", "विक्रांत", "વિક્રાંત"):
        assert old not in raw, old
    for new in ("Ravi", "रवि", "રવિ"):
        assert new in raw, new


@pytest.mark.parametrize("direction", sorted(RAVI))
def test_ravi_snapshot_preflights_in_every_language(direction):
    from app.agent_definition.preflight import preflight_agent
    agent = AgentDefinition.from_yaml(RAVI[direction])
    # 'ok' or 'preflight_failed:<stage>' — a string, by contract.
    assert preflight_agent(agent, languages=["en", "hi", "gu"]) == "ok"


def test_ravi_inbound_second_beat_introduces_vikrant():
    """The moment an inbound caller first responds they hear the intro line."""
    from app.agent_definition.spoken_surfaces import resolve_spoken_surfaces
    agent = AgentDefinition.from_yaml(RAVI["inbound"])
    openings = resolve_spoken_surfaces(agent, "inbound")["openings"]
    assert "Ravi" in openings["en"]["second_line"]
    assert "रवि" in openings["hi"]["second_line"]
    assert "રવિ" in openings["gu"]["second_line"]
    assert "Prakhar" not in str(openings)


def test_ravi_outbound_introduces_vikrant_after_identity_check():
    """Outbound's second beat is the identity check (no name by design); the
    introduction is the good-time line that follows it."""
    from app.agent_definition.spoken_surfaces import resolve_spoken_surfaces
    agent = AgentDefinition.from_yaml(RAVI["outbound"])
    openings = resolve_spoken_surfaces(agent, "outbound")["openings"]
    assert "Prakhar" not in str(openings)
    assert all("{contact_name}" in openings[l]["second_line"] for l in ("en", "hi", "gu"))
    line = agent.outbound.good_time_check_line
    assert "Ravi" in line["en"]
    assert "रवि" in line["hi"]
    assert "રવિ" in line["gu"]
