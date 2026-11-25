"""Campaign engine: do-not-call, hard caps, calling hours (2026-07-29).

The campaign runner is the only path that dials a LIST of people from one
click. When the follow-up approval gate was built (test_followup_approval.py),
an audit found this path had none of the same limits: it ignored
disposition='rejected' entirely, accepted max_calls:0 as "unlimited" and
delay_seconds:0 as "as fast as the trunk allows", and would happily start at
04:00. These tests pin each of those shut.

`_campaign_plan` and `_campaign_limits` are pure, so most of this needs no
database and no HTTP; the two refusal paths run through the real endpoint.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
# server.py does `import auth`, exactly as it does in the container
# (working_dir=/home/angelo/lescall/dashboard).
if str(_REPO / "dashboard") not in sys.path:
    sys.path.insert(0, str(_REPO / "dashboard"))

import server  # noqa: E402


def _c(cid: str, *, status: str = "not_called",
       disposition: str = "not_classified") -> dict:
    return {"id": cid, "db_id": int(cid), "phone": f"+91999990{cid:>04}",
            "name": f"C{cid}", "call_status": status, "disposition": disposition}


# ── do-not-call ─────────────────────────────────────────────────────────────

def test_rejected_contacts_are_never_queued():
    """The gap that mattered: 'rejected' is how an in-call "stop calling me"
    is recorded (test_dnd.py), and the campaign dialled straight past it."""
    contacts = [_c("1"), _c("2", disposition="rejected"), _c("3")]
    to_call, dnd, capped = server._campaign_plan(contacts, "not_called", 100)
    assert [c["id"] for c in to_call] == ["1", "3"]
    assert dnd == 1 and capped is False


def test_dnd_is_applied_before_the_cap():
    """Otherwise a list of opted-out contacts eats the whole budget and the
    people who wanted the call never get one."""
    contacts = ([_c(str(i), disposition="rejected") for i in range(1, 6)]
                + [_c(str(i)) for i in range(6, 9)])
    to_call, dnd, _ = server._campaign_plan(contacts, "not_called", 3)
    assert len(to_call) == 3
    assert all(c["disposition"] != "rejected" for c in to_call)
    assert dnd == 5


def test_rejected_as_an_audience_resolves_to_nobody():
    """'rejected' is a legal value of the audience picker's sibling filter, so
    it can arrive here. It must select nobody rather than exactly the people
    who opted out."""
    contacts = [_c("1", disposition="rejected"), _c("2", disposition="rejected")]
    to_call, dnd, _ = server._campaign_plan(contacts, "rejected", 100)
    assert to_call == [] and dnd == 2


# ── caps and pacing ─────────────────────────────────────────────────────────

def test_max_calls_zero_is_the_ceiling_not_unlimited():
    delay, max_calls = server._campaign_limits({"max_calls": 0})
    assert max_calls == server.CAMPAIGN_MAX_CALLS
    assert delay >= server.CAMPAIGN_MIN_DELAY_S


def test_an_oversized_request_is_clamped():
    _, max_calls = server._campaign_limits({"max_calls": 10_000})
    assert max_calls == server.CAMPAIGN_MAX_CALLS


def test_a_smaller_request_is_honoured():
    _, max_calls = server._campaign_limits({"max_calls": 5})
    assert max_calls == 5


@pytest.mark.parametrize("sent", [0, None, "", 0.5, -20])
def test_delay_never_drops_below_the_floor(sent):
    delay, _ = server._campaign_limits({"delay_seconds": sent})
    assert delay >= server.CAMPAIGN_MIN_DELAY_S


def test_a_longer_delay_is_honoured():
    delay, _ = server._campaign_limits({"delay_seconds": 30})
    assert delay == 30


def test_the_plan_truncates_to_the_cap():
    contacts = [_c(str(i)) for i in range(1, 12)]
    to_call, _, capped = server._campaign_plan(contacts, "all", 4)
    assert len(to_call) == 4 and capped is True


# ── audience resolution ─────────────────────────────────────────────────────

def test_disposition_audiences_resolve_against_disposition():
    """'to_contact_later' is a disposition, not a call_status. Matching only
    call_status made this audience silently empty while the UI counted dozens
    — and the empty start then reported success."""
    contacts = [_c("1", disposition="to_contact_later"),
                _c("2", disposition="interested")]
    to_call, _, _ = server._campaign_plan(contacts, "to_contact_later", 100)
    assert [c["id"] for c in to_call] == ["1"]


def test_call_status_audiences_still_resolve_against_call_status():
    contacts = [_c("1", status="no_answer"), _c("2", status="not_called")]
    to_call, _, _ = server._campaign_plan(contacts, "no_answer", 100)
    assert [c["id"] for c in to_call] == ["1"]


def test_all_means_everyone_except_do_not_call():
    contacts = [_c("1", status="answered"), _c("2", status="busy"),
                _c("3", disposition="rejected")]
    to_call, dnd, _ = server._campaign_plan(contacts, "all", 100)
    assert len(to_call) == 2 and dnd == 1


# ── calling hours, through the real endpoint ────────────────────────────────

class _Req:
    """Minimal stand-in: _acting_owner falls back to DEFAULT_OWNER when auth
    can't read the request, which is exactly what we want here."""
    client = None

    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body


def _start(monkeypatch, *, in_window: bool, contacts: list[dict]) -> dict:
    from shared.intelligence import autodial

    monkeypatch.setattr(autodial, "within_window", lambda *a, **k: in_window)
    monkeypatch.setattr(server, "_load_outbound", lambda owner: contacts)
    res = asyncio.run(server.campaign_start(_Req({"call_status_filter": "all"})))
    import json as _json
    return {"status": res.status_code, **_json.loads(bytes(res.body))}


def test_a_campaign_cannot_start_outside_calling_hours(monkeypatch):
    out = _start(monkeypatch, in_window=False, contacts=[_c("1")])
    assert out["status"] == 400 and out["ok"] is False
    assert "calling hours" in out["reason"]


def test_an_all_rejected_audience_fails_loudly(monkeypatch):
    """It used to return ok:false with HTTP 200, which the SPA toasted as
    'Campaign started'. Silence about a refusal is how you get surprised."""
    out = _start(monkeypatch, in_window=True,
                 contacts=[_c("1", disposition="rejected")])
    assert out["status"] == 400 and out["dnd_skipped"] == 1
    assert "Rejected" in out["reason"]


def test_an_empty_audience_fails_loudly(monkeypatch):
    out = _start(monkeypatch, in_window=True, contacts=[])
    assert out["status"] == 400 and out["ok"] is False


# ── the mid-run re-check ────────────────────────────────────────────────────

def test_dnd_recheck_fails_closed(monkeypatch):
    """If we can't establish that someone is callable, we don't call them."""
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("shared.db.database.get_db", _boom)
    assert server._contact_is_dnd(7, "t@x.com") is True
    assert server._contact_is_dnd(None, "t@x.com") is True


def test_the_run_loop_still_rechecks_hours_and_dnd():
    """Both re-checks live inside the dial loop, not just at start: a run that
    began at 18:55 must stop at 19:00, and someone who opts out on call 3 must
    not be dialled on call 40 of the same run.

    Read from `_campaign_launch`, which is where the loop lives. `campaign_start`
    is a thin HTTP wrapper over it so that the scheduler runs the SAME loop —
    the guardrails asserted here are exactly what a scheduled campaign inherits
    by not having its own copy.
    """
    import inspect

    src = inspect.getsource(server._campaign_launch)
    body = src.split("async def _run()", 1)[1]
    assert "within_window" in body
    assert "_contact_is_dnd" in body


def test_the_start_endpoint_is_only_a_wrapper_over_the_shared_launcher():
    """If Start ever grows its own planning again, the scheduler stops sharing
    its guardrails — which is the whole reason the body was extracted."""
    import inspect

    src = inspect.getsource(server.campaign_start)
    assert "_campaign_launch" in src
    assert "async def _run()" not in src


def test_a_scheduled_campaign_uses_the_same_launcher():
    import inspect

    assert "_campaign_launch" in inspect.getsource(server._fire_schedule)
    assert "_place_call" in inspect.getsource(server._fire_schedule)
