"""F1 — an amenity is never presented as built and in use without data
(owner review of the Green County agent, 2026-10-12; RUNBOOK §76).

The Pramukh sheet lists 27 amenities for Green County with no status, and the
project is "under construction and ready to move, both". `amenities` renders
as a flat "Amenities:" line, and the model reads a list as an inventory — "you
have an indoor swimming pool and a Dolby theatre" — as if every one were open
today. `catalog.amenity_status_clause` adds one built-in clause to such an
item's line, and ONLY to such an item: amenities present AND availability or
status saying under construction. Every other item renders byte-for-byte as
before — the literals below are what the renderer produced before the clause
existed.

Offline: no LLM, no live agent YAML. The owner's sheet (deploy/) is the source
object the Pramukh agents are generated from, and is read here only to pin
the clause's measured blast radius.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.agent_definition.catalog import (
    _AMENITY_STATUS_CLAUSE,
    _render_item,
    amenity_status_clause,
    render_catalog_prompt,
    render_item_details,
)
from app.agent_definition.schema import AgentDefinition


SHEET = Path(__file__).resolve().parents[3] / "deploy" / "pramukh_vapi_projects.yaml"

UNDER_CONSTRUCTION = {
    "id": "gc", "name": "Pramukh Green County", "location": "Chharwada, Vapi",
    "configurations": ["3 BHK", "4 BHK"],
    "availability": "under construction and ready to move, both",
    "amenities": ["indoor swimming pool", "Dolby theatre", "gym"],
    "not_published": ["RERA number"],
}


def _kb(catalog, primary=None):
    return SimpleNamespace(catalog=catalog, portfolio_prompt_text="",
                           primary_project=primary, locations=["Vapi"],
                           match_rules=[], known_projects={})


# ── the conditional case ─────────────────────────────────────────────────────

def test_under_construction_item_with_amenities_carries_the_clause():
    assert amenity_status_clause(UNDER_CONSTRUCTION) == _AMENITY_STATUS_CLAUSE
    details = render_item_details(UNDER_CONSTRUCTION)
    assert details == (
        "Amenities: indoor swimming pool, Dolby theatre, gym. "
        "NOT published — never guess: RERA number. " + _AMENITY_STATUS_CLAUSE)


def test_the_clause_is_the_last_thing_on_the_items_line():
    line = _render_item(UNDER_CONSTRUCTION)
    assert line.endswith(_AMENITY_STATUS_CLAUSE)
    assert line.count(_AMENITY_STATUS_CLAUSE) == 1


@pytest.mark.parametrize("field,value", [
    ("availability", "Under Construction"),
    ("availability", "under-construction, possession 2027"),
    ("status", "under construction"),
    ("status", "Under-construction; sample flat ready"),
])
def test_either_availability_or_status_can_state_the_condition(field, value):
    item = {"name": "P", "amenities": ["gym"], field: value}
    assert amenity_status_clause(item) == _AMENITY_STATUS_CLAUSE


def test_the_clause_reaches_the_knowledge_band_once_per_item():
    band = render_catalog_prompt(_kb([UNDER_CONSTRUCTION]))
    assert band.count(_AMENITY_STATUS_CLAUSE) == 1


def test_primary_project_rendering_keeps_the_clause():
    band = render_catalog_prompt(_kb(
        [UNDER_CONSTRUCTION, {"id": "o", "name": "Other", "amenities": ["gym"],
                              "availability": "Ready to move"}],
        primary={"id": "gc", "mode": "exclusive", "reason": "the pitch"}))
    first, rest = band.split("\n▸ Every project below", 1)
    assert _AMENITY_STATUS_CLAUSE in first         # on the primary's line
    assert _AMENITY_STATUS_CLAUSE not in rest      # not on the ready one


# ── unaffected items are byte-identical to before ────────────────────────────

@pytest.mark.parametrize("item,expected", [
    # amenities, but ready — no clause
    ({"name": "Ready", "availability": "Ready to move",
      "amenities": ["Gym", "Pool"]},
     "▸ Ready — Ready to move Amenities: Gym, Pool."),
    # under construction, but no amenities — no clause
    ({"name": "Bare", "availability": "under construction",
      "not_published": ["RERA number"]},
     "▸ Bare — under construction NOT published — never guess: RERA number."),
    # amenities and nothing said about construction — no clause
    ({"name": "Quiet", "amenities": ["Gym"], "status": "launched"},
     "▸ Quiet Status: launched. Amenities: Gym."),
    # a pre-§55 item with none of the detail keys — untouched
    ({"name": "Legacy", "location": "Vapi", "configurations": ["2 BHK"],
      "price_range": "₹34-40 Lakh"},
     "▸ Legacy — in Vapi, 2 BHK, ₹34-40 Lakh"),
    # an empty amenities list is no amenities
    ({"name": "Empty", "availability": "under construction", "amenities": []},
     "▸ Empty — under construction"),
])
def test_items_outside_the_condition_render_exactly_as_before(item, expected):
    assert amenity_status_clause(item) == ""
    assert _render_item(item) == expected


def test_a_catalogue_without_the_condition_is_byte_identical():
    catalog = [
        {"id": "a", "name": "A", "location": "Vapi", "availability": "Ready",
         "amenities": ["Gym"]},
        {"id": "b", "name": "B", "location": "Surat", "price_range": "₹70 Lakh"},
    ]
    band = render_catalog_prompt(_kb(catalog))
    assert band == ("▸ A — in Vapi, Ready Amenities: Gym.\n"
                    "▸ B — in Surat, ₹70 Lakh")
    assert _AMENITY_STATUS_CLAUSE not in band


def test_schema_roundtrip_keeps_the_condition_fields():
    """A catalogue item is an open dict; the two fields the clause reads
    survive AgentDefinition.from_dict."""
    d = {
        "agent_id": "t_amen", "version": "1.0.0",
        "identity": {"agent_name": "Dhaval", "company_name": "Pramukh"},
        "languages": {"supported": ["en"], "default": "en", "tts_voices": {}},
        "call_flow": {"stages": [{"id": "S", "order": 0, "goal": "g",
                                  "turns_until_advance": 9}]},
        "knowledge_base": {"catalog": [UNDER_CONSTRUCTION],
                           "portfolio_prompt_text": ""},
        "response_bank": {"opening_greeting": "hi"},
    }
    agent = AgentDefinition.from_dict(d)
    assert _AMENITY_STATUS_CLAUSE in render_catalog_prompt(agent.knowledge_base)


# ── measured blast radius on the owner's sheet ───────────────────────────────

def test_blast_radius_on_the_pramukh_sheet_is_the_four_unfinished_projects():
    """Engine-level means every agent carrying the shared catalogue is
    affected: the clause fires for exactly the sheet projects that are
    under construction AND list amenities. Green County is the one the
    review was about; the other three ride along by the same rule. If this
    set changes, the owner edited a project's availability — re-check that
    the clause is still wanted there."""
    sheet = yaml.safe_load(SHEET.read_text(encoding="utf-8"))
    fired = sorted(p["item"]["name"] for p in sheet["projects"]
                   if amenity_status_clause(p["item"]))
    assert fired == ["Pramukh Blue Stone", "Pramukh Green County",
                     "Pramukh Silver Stone", "Pramukh Vedanta"]
    assert "Pramukh Green County" in fired
