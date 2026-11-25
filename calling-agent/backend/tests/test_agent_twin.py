"""Inbound/outbound twin agents (agent_builder_api).

A twin is NOT a copy with call_direction flipped: who speaks first changes the
script. These lock the four things adapt_direction() rewrites — greeting, first
stage goal, the name slot, cold-call objections — plus the slug algebra that
pairs two records into one family.

Pure-function tests only: no filesystem, no DB, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
for p in (str(_REPO / "dashboard"), str(_REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent_builder_api import (  # noqa: E402
    adapt_direction, agent_family, counterpart_of, twin_slug,
)


def _spec(direction: str = "inbound") -> dict:
    return {
        "agent_id": "acme-v1",
        "call_direction": direction,
        "identity": {"agent_name": "Asha", "company_name": "Acme Homes"},
        "call_flow": {"stages": [{"id": "S1", "order": 0, "goal": "ORIGINAL GOAL"},
                                 {"id": "S2", "order": 1, "goal": "second stage"}]},
        "response_bank": {"opening_greeting": "ORIGINAL GREETING",
                          "objection_responses": {}},
        "slots": [
            {"name": "caller_name", "question": {"en": "who's calling?"},
             "reask_question": {"en": "your name?"}, "max_asks": 2},
            {"name": "preferred_location", "question": {}, "max_asks": 0},
        ],
        "knowledge_base": {"locations": ["Surat"]},
    }


def _slot(spec: dict, name: str) -> dict:
    return next(s for s in spec["slots"] if s["name"] == name)


# ── slug algebra ────────────────────────────────────────────────────────────

def test_family_groups_a_pair_and_leaves_standalone_alone():
    assert agent_family("acme-inbound") == "acme"
    assert agent_family("acme-outbound") == "acme"
    # A pre-twin slug is its own family — it must never be grouped with an
    # unrelated agent that merely shares a prefix.
    assert agent_family("pramukh_agent") == "pramukh_agent"


def test_twin_slug_is_idempotent():
    """Deriving a twin from a twin must not stack suffixes."""
    assert twin_slug("acme", "outbound") == "acme-outbound"
    assert twin_slug("acme-inbound", "outbound") == "acme-outbound"
    assert twin_slug("acme-outbound", "outbound") == "acme-outbound"


def test_counterpart_round_trips_and_is_none_for_standalone():
    assert counterpart_of("acme-inbound") == "acme-outbound"
    assert counterpart_of("acme-outbound") == "acme-inbound"
    assert counterpart_of("pramukh_agent") is None


# ── direction adaptation ────────────────────────────────────────────────────

def test_adapt_does_not_mutate_the_source():
    src = _spec("inbound")
    adapt_direction(src, "outbound")
    assert src["call_direction"] == "inbound"
    assert src["response_bank"]["opening_greeting"] == "ORIGINAL GREETING"
    assert _slot(src, "caller_name")["max_asks"] == 2


def test_outbound_opens_the_call_and_names_the_contact():
    out = adapt_direction(_spec("inbound"), "outbound")
    assert out["call_direction"] == "outbound"
    greeting = out["response_bank"]["opening_greeting"]
    # It speaks first, so the greeting carries the contact placeholder the
    # bridge substitutes from call_link.json.
    assert "{contact_name}" in greeting
    assert "{current_time}" in greeting
    assert "{contact_name}" in out["call_flow"]["stages"][0]["goal"]


def test_inbound_answers_and_asks_who_is_calling():
    inb = adapt_direction(_spec("outbound"), "inbound")
    assert inb["call_direction"] == "inbound"
    # Answering a ringing phone: no contact record, so no placeholder to fill.
    assert "{contact_name}" not in inb["response_bank"]["opening_greeting"]
    assert "who is calling" in inb["call_flow"]["stages"][0]["goal"].lower()


def test_outbound_never_asks_for_a_name_it_already_has():
    out = adapt_direction(_spec("inbound"), "outbound")
    name = _slot(out, "caller_name")
    assert name["max_asks"] == 0
    assert name["question"] == {} and name["reask_question"] == {}


def test_inbound_restores_the_name_question():
    """Twinning an outbound agent back to inbound has to re-arm the ask, or the
    inbound half never learns who it is talking to."""
    out = adapt_direction(_spec("inbound"), "outbound")
    back = adapt_direction(out, "inbound")
    name = _slot(back, "caller_name")
    assert name["max_asks"] == 2
    assert name["question"].get("en")
    assert name["reask_question"].get("en")


def test_cold_call_objections_only_on_outbound():
    out = adapt_direction(_spec("inbound"), "outbound")
    assert out["response_bank"]["objection_responses"].get("en")
    # They read as nonsense when the customer dialled us.
    inb = adapt_direction(out, "inbound")
    assert inb["response_bank"]["objection_responses"] == {}


def test_author_written_objections_are_not_overwritten():
    src = _spec("inbound")
    src["response_bank"]["objection_responses"] = {"en": ["MINE"]}
    out = adapt_direction(src, "outbound")
    assert out["response_bank"]["objection_responses"] == {"en": ["MINE"]}


def test_direction_neutral_content_survives():
    """Everything that isn't about who dialled must carry over untouched."""
    out = adapt_direction(_spec("inbound"), "outbound")
    assert out["knowledge_base"] == {"locations": ["Surat"]}
    assert out["identity"]["company_name"] == "Acme Homes"
    assert out["agent_id"] == "acme-v1"
    assert out["call_flow"]["stages"][1]["goal"] == "second stage"
    assert _slot(out, "preferred_location") == {
        "name": "preferred_location", "question": {}, "max_asks": 0}


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
def test_adaptation_is_idempotent(direction):
    once = adapt_direction(_spec("inbound"), direction)
    assert adapt_direction(once, direction) == once


def test_company_name_lands_in_the_outbound_goal():
    out = adapt_direction(_spec("inbound"), "outbound")
    assert "Acme Homes" in out["call_flow"]["stages"][0]["goal"]


def test_missing_optional_sections_do_not_crash():
    """Agents built by older versions may have no slots / no response_bank."""
    bare = {"agent_id": "x", "identity": {}, "call_flow": {"stages": []}}
    out = adapt_direction(bare, "outbound")
    assert out["call_direction"] == "outbound"
    assert out["response_bank"]["opening_greeting"]
