"""F2 — Green County's unit-wise and floor/tower availability are NOT
published (owner review 2026-10-12; RUNBOOK §76).

The sheet says only "under construction and ready to move, both"; it has no
per-unit inventory, so "the 3 BHK is available" could only ever be improvised.
The fix is data: two items on the Green County entry's `not_published` list
in deploy/pramukh_vapi_projects.yaml — the source object every Pramukh agent
is generated from (build_pramukh_projects.py copies each item verbatim into
knowledge_base.catalog, and catalog._DETAIL_KEYS renders `not_published` as
"NOT published — never guess: …").

tests/fixtures/pramukh_vapi_projects_2026_10_10.yaml is the sheet as it stood
before this change, so the diff is pinned: Green County gains exactly those
two items and no other project moves.

Offline: no LLM, no live agent YAML.
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from app.agent_definition.catalog import _render_item, render_item_details

HERE = Path(__file__).resolve().parent
SHEET = HERE.parents[2] / "deploy" / "pramukh_vapi_projects.yaml"
BEFORE = HERE / "fixtures" / "pramukh_vapi_projects_2026_10_10.yaml"

NEW_ITEMS = ["unit-wise availability", "floor and tower availability"]


def _projects(path: Path) -> dict[str, dict]:
    sheet = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {p["key"]: p for p in sheet["projects"]}


def test_green_county_not_published_gained_exactly_the_two_items():
    now, before = _projects(SHEET), _projects(BEFORE)
    gc_now, gc_before = now["green_county"], before["green_county"]
    assert gc_now["item"]["not_published"] == (
        gc_before["item"]["not_published"] + NEW_ITEMS)
    # Nothing else on the entry moved.
    expected = copy.deepcopy(gc_before)
    expected["item"]["not_published"] = gc_now["item"]["not_published"]
    assert gc_now == expected


def test_every_other_project_is_unchanged():
    now, before = _projects(SHEET), _projects(BEFORE)
    assert set(now) == set(before)
    for key in now:
        if key == "green_county":
            continue
        assert now[key] == before[key], key
        assert not (set(NEW_ITEMS) & set(now[key]["item"].get("not_published") or [])), key


def test_the_generated_green_county_entry_says_both_are_not_published():
    """What the builder copies into every agent's catalogue is the sheet item
    itself, and this is the sentence the knowledge band renders from it."""
    item = _projects(SHEET)["green_county"]["item"]
    details = render_item_details(item)
    assert ("NOT published — never guess: possession date for the "
            "under-construction buildings, RERA number, unit-wise "
            "availability, floor and tower availability." in details)
    line = _render_item(item)
    assert line.startswith("▸ Pramukh Green County — in Chharwada, Vapi, "
                           "3 BHK/4 BHK, about ₹78–85 lakh (3 BHK); about ₹1.12 Cr "
                           "(4 BHK), under construction and ready to move, both.")


def test_other_projects_render_exactly_as_before():
    now, before = _projects(SHEET), _projects(BEFORE)
    for key in now:
        if key == "green_county":
            continue
        assert _render_item(now[key]["item"]) == _render_item(before[key]["item"]), key
