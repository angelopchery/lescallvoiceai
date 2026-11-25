"""Which queries the AI judge is allowed to skip.

`/api/intel/search` bypasses verification for one case only: a pasted
identifier, where there is no membership question to decide and the index
already answered it exactly.

Getting that predicate wrong is silent in both directions, which is why it has
its own suite. The first cut was "no spaces and 8+ characters", so
"relocation", "complaints", "interested" and "possession" were all read as
identifiers — every one-word English query skipped the judge and fell back to
the loose similarity matching the feature exists to replace, with nothing in
the response to say so. The same predicate refused "+91 98765 43210" because
it has spaces, so a pasted phone number was sent to the judge and spent a
hundred model calls confirming a primary key.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[3] / "dashboard"
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))


@pytest.fixture(scope="module")
def looks():
    from intel_api import _looks_like_identifier

    return _looks_like_identifier


@pytest.mark.parametrize("term", [
    "919876500001",                          # phone, as stored
    "+91 98765 43210",                       # phone, as a human types it
    "+91-98765-43210",
    "(079) 4000 1234",
    "cba6b876-63ad-46ad-913a-ca8f740baee1",  # conn_id (UUID)
    "919876500001_20260819_103000",          # call_key
])
def test_a_pasted_identifier_skips_the_judge(looks, term):
    assert looks(term) is True, f"{term!r} is a lookup, not a description"


@pytest.mark.parametrize("term", [
    "relocation", "complaints", "interested", "possession", "commitment",
    "escalation", "negotiation",              # 8+ letters, no digits
    "vapi", "3bhk", "2 BHK",
    "whoever relocated to vapi",
    "who said yes to relocating",
])
def test_a_description_is_never_mistaken_for_an_identifier(looks, term):
    assert looks(term) is False, (
        f"{term!r} was read as an identifier, so it silently skipped "
        "verification and fell back to similarity matching")


def test_an_empty_query_is_not_an_identifier(looks):
    assert looks("") is False
    assert looks("   ") is False


def test_a_short_number_is_a_description_not_a_phone(looks):
    """'3bhk' and '2024' are things people search FOR, not numbers to dial."""
    assert looks("2024") is False
    assert looks("3bhk") is False


# ===========================================================================
# Multiple agents in one filter (2026-08-29)
#
# The Agent control was a native <select>, so it could hold exactly one slug:
# "how did shubhangi AND maya do" meant running the page twice and adding the
# numbers by hand. `?agent=` now takes a comma-separated set. It stays the SAME
# parameter deliberately — a single slug is a one-item list, so every existing
# link, bookmark and API caller keeps working.
# ===========================================================================

@pytest.fixture(scope="module")
def agent_list():
    from intel_api import _agent_list

    return _agent_list


@pytest.mark.parametrize("raw,want", [
    ("", []),                                    # no filter at all
    ("shubhangi-outbound", ["shubhangi-outbound"]),          # the old form
    ("a,b", ["a", "b"]),
    (" a , b ", ["a", "b"]),                     # humans put spaces after commas
    ("a,,b", ["a", "b"]),                        # a trailing comma is not an agent
    (",", []),
    ("   ", []),
])
def test_agent_csv_parses_to_a_set(agent_list, raw, want):
    assert agent_list(raw) == want


def test_a_single_agent_still_parses_as_one(agent_list):
    """The compatibility guarantee, stated on its own: the old single-agent
    query string must keep meaning exactly what it meant."""
    assert agent_list("maya_lescall_sdr-outbound") == ["maya_lescall_sdr-outbound"]


def test_the_filter_clause_uses_ANY_for_one_agent_and_for_many(agent_list):
    """One code path for both, so a single-agent filter cannot drift away from
    a multi-agent one."""
    from intel_api import _call_filters

    one_where, one_params = _call_filters("o@x", agent="solo")
    many_where, many_params = _call_filters("o@x", agent="a,b,c")
    assert any("agent_slug = ANY(?)" in c for c in one_where)
    assert any("agent_slug = ANY(?)" in c for c in many_where)
    assert ["solo"] in one_params and ["a", "b", "c"] in many_params

    none_where, _ = _call_filters("o@x", agent="")
    assert not any("agent_slug" in c for c in none_where), (
        "an empty agent filter must not narrow anything")
