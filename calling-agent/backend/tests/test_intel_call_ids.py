"""/api/intel/calls/ids — the whole matching set, not the page on screen.

Export and Report can only ever send TICKED call ids. The Intelligence list is
server-paged at 50 (`_PAGE_LIMIT`) and, for a STRUCTURED filter, the page was
the only thing a user could tick: "select all" meant "select all fifty". A
filter matching 246 calls exported as 50 with nothing saying so — the same
silent shortening the AI filter shed on 2026-08-29, still fully in place on the
ordinary list because that fix only ever covered client-side NL results.

So the property under test is not "the endpoint returns ids", it is "the count
it returns is unbounded by the page size". A regression here is invisible in
the UI: the workbook still builds, it is just short.

Endpoint functions are called directly with an explicit owner, as elsewhere in
this suite — current_owner is FastAPI-resolved only over HTTP.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401  (fixtures)

_REPO = Path(__file__).resolve().parents[3]
for p in (str(_REPO / "dashboard"), str(_REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

import intel_api  # noqa: E402

# The test DB is not truncated between tests in a module, so each test owns a
# distinct tenant and asserts only over that tenant — otherwise the counts here
# would depend on execution order.
_seq = itertools.count(1)
_owners = (f"ids-{i}@x.com" for i in itertools.count(1))


def _seed(n: int, owner: str, agent: str = "pramukh_agent",
          started: str = "2026-07-18T10:00:00Z") -> list[int]:
    """n bare calls — no call_intelligence, deliberately.

    An export is a data dump: a call with no analysis still has a number, a
    duration and a transcript worth exporting, so the ids endpoint must not
    quietly restrict to analysed calls the way a report does.
    """
    from shared.db.database import get_db

    ids = []
    with get_db() as db:
        for _ in range(n):
            k = next(_seq)
            row = db.execute(
                """INSERT INTO calls (conn_id, call_key, direction, owner,
                                      agent_slug, transcript, started_at,
                                      duration_s)
                   VALUES (?, ?, 'outbound', ?, ?, 'hello', ?, 60)
                   RETURNING id""",
                (f"conn-ids-{k}", f"key-ids-{k}", owner, agent, started),
            ).fetchone()
            ids.append(int(row["id"]))
    return ids


def test_returns_every_match_not_one_page(pg_test_db):
    """The defect: 120 matching calls must not come back as 50."""
    owner = next(_owners)
    seeded = _seed(120, owner)
    res = intel_api.intel_call_ids(owner=owner)
    assert res["count"] == len(seeded) == 120
    assert set(res["call_ids"]) == set(seeded)
    assert res["capped"] is False
    # The page size must not be able to bound this, whatever it is set to.
    assert res["count"] > intel_api._PAGE_LIMIT


def test_ids_agree_with_the_total_the_list_reports(pg_test_db):
    """`total` drives the button label ("Select all N"); N must be reachable.

    If the list's total and the ids set ever disagree, the button promises a
    number it cannot deliver and the export is short by the difference — the
    original bug wearing a different mask.
    """
    owner = next(_owners)
    _seed(73, owner)
    listing = intel_api.intel_calls(owner=owner, page=1, limit=50, days=0,
                                    min_lead=0)
    res = intel_api.intel_call_ids(owner=owner)
    assert listing["total"] == res["count"] == 73
    assert len(listing["items"]) == 50  # the page really is capped
    assert len(res["call_ids"]) == 73   # the selection is not


def test_owner_scoped(pg_test_db):
    owner, other = next(_owners), next(_owners)
    mine = _seed(5, owner)
    _seed(9, other)
    res = intel_api.intel_call_ids(owner=owner)
    assert set(res["call_ids"]) == set(mine)
    assert res["count"] == 5


def test_honours_the_same_filters_as_the_list(pg_test_db):
    """Selection and list must resolve the SAME predicates, or "all matches"
    would export calls the user never saw."""
    owner = next(_owners)
    _seed(4, owner, agent="agent_a")
    _seed(6, owner, agent="agent_b")

    a = intel_api.intel_call_ids(owner=owner, agent="agent_a")
    b = intel_api.intel_call_ids(owner=owner, agent="agent_b")
    assert a["count"] == 4 and b["count"] == 6
    assert not (set(a["call_ids"]) & set(b["call_ids"]))

    listing = intel_api.intel_calls(owner=owner, page=1, limit=50, days=0,
                                    min_lead=0, agent="agent_a")
    assert listing["total"] == a["count"]


def test_days_window_matches_the_list(pg_test_db):
    owner = next(_owners)
    _seed(3, owner, started="2026-07-18T10:00:00Z")            # old
    recent = _seed(2, owner, started="2099-01-01T10:00:00Z")   # any window

    res = intel_api.intel_call_ids(owner=owner, days=30)
    assert set(res["call_ids"]) == set(recent)
    assert intel_api.intel_calls(
        owner=owner, page=1, limit=50, days=30,
        min_lead=0)["total"] == res["count"]


def test_unanalyzed_calls_are_included(pg_test_db):
    """Seeded rows have no call_intelligence; an export still wants them."""
    owner = next(_owners)
    seeded = _seed(3, owner)
    res = intel_api.intel_call_ids(owner=owner)
    assert set(res["call_ids"]) == set(seeded)


def test_capped_reports_a_floor_not_a_total(pg_test_db, monkeypatch):
    """A ceiling that can be hit silently is the bug; the flag is the fix."""
    owner = next(_owners)
    monkeypatch.setattr(intel_api, "_EXPORT_MAX_CALLS", 10)
    _seed(14, owner)
    res = intel_api.intel_call_ids(owner=owner)
    assert res["capped"] is True
    assert res["count"] == 10 == len(res["call_ids"])
    assert res["max"] == 10


def test_route_is_declared_before_the_call_key_route():
    """`/calls/ids` must not be swallowed as a call_key.

    FastAPI matches in declaration order, so /calls/{call_key} appearing first
    would make this endpoint unreachable over HTTP while every direct-call test
    above still passed.
    """
    paths = [r.path for r in intel_api.router.routes]
    assert "/api/intel/calls/ids" in paths, paths
    assert (paths.index("/api/intel/calls/ids")
            < paths.index("/api/intel/calls/{call_key}"))
