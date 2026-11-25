"""The dashboard's own contact write endpoints (/api/outbound/contacts).

Calls the endpoint functions directly against the lescall_test DB, the way
test_contact_hub_api.py does — the owner dependency is FastAPI-resolved only
over HTTP.

What these protect (2026-09-09)
-------------------------------
A number typed into the Add-contact form was stored exactly as typed. Every
OTHER write path normalized to +E.164 — the CSV import through
`_format_phone_digits`, the public API through `shared.contacts.upsert_contact`
— so a bare 10-digit Indian number could only enter the phone book by hand, and
then it dialled without a country code and the carrier answered SIP 404. The
operator was told "declined", which is also what a human pressing reject looks
like.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401  (fixtures)

_REPO = Path(__file__).resolve().parents[3]
for p in (str(_REPO / "dashboard"), str(_REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

import server  # noqa: E402


OWNER = "tech@dhronai.com"


class _Req:
    """The two things these endpoints touch on a Request."""

    def __init__(self, body: dict):
        self._body = body
        self.client = None
        self.query_params = {}

    async def json(self):
        return self._body


@pytest.fixture()
def clean_book():
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("DELETE FROM contacts WHERE owner=?", (OWNER,))
    yield
    with get_db() as db:
        db.execute("DELETE FROM contacts WHERE owner=?", (OWNER,))


def _add(**body):
    return json.loads(asyncio.run(server.outbound_add(_Req(body))).body)


def _stored(phone_like: str) -> dict:
    from shared.phone import significant_digits
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM contacts WHERE owner=? AND sig_digits=?",
            (OWNER, significant_digits(phone_like))).fetchone()
    return dict(row) if row else {}


def test_a_hand_typed_ten_digit_number_is_stored_dialable(clean_book):
    """The exact row that failed: SHAHANAZ, "7359800826", four 404s."""
    assert _add(phone="7359800826", name="SHAHANAZ")["ok"] is True
    assert _stored("7359800826")["phone"] == "+917359800826"


def test_a_number_typed_with_spaces_or_a_leading_zero_is_stored_dialable(clean_book):
    for typed in ("073598 00826", "+91 73598-00826", "91 7359800826"):
        assert _add(phone=typed, name="x")["ok"] is True
        assert _stored(typed)["phone"] == "+917359800826", typed
        from shared.db.database import get_db
        with get_db() as db:
            db.execute("DELETE FROM contacts WHERE owner=?", (OWNER,))


def test_an_unusable_number_is_refused_not_stored(clean_book):
    res = asyncio.run(server.outbound_add(_Req({"phone": "12345", "name": "x"})))
    assert res.status_code == 400
    assert not _stored("12345")


def test_editing_a_number_normalizes_it_too(clean_book):
    """The correction path matters as much as create — fixing a typo is the
    single most common reason to edit a contact, and the corrected number is
    what gets dialled."""
    handle = _add(phone="+919876543210", name="Ramesh")["contact"]["id"]
    out = asyncio.run(server.outbound_update(handle, _Req({"phone": "7359800826"})))
    assert json.loads(out.body)["ok"] is True
    assert _stored("7359800826")["phone"] == "+917359800826"


def test_editing_a_number_still_writes_it_through(clean_book):
    """The Contacts page sent every field EXCEPT phone on edit, so a corrected
    number was silently dropped while the UI reported success. The server side
    has to keep honouring it."""
    handle = _add(phone="+919876543210", name="Ramesh")["contact"]["id"]
    asyncio.run(server.outbound_update(handle, _Req({"phone": "+919000000001"})))
    assert _stored("9000000001")["name"] == "Ramesh"
    assert not _stored("9876543210")


def test_renumbering_onto_another_contact_is_still_a_conflict(clean_book):
    """Normalization must not weaken the dedupe rule it feeds."""
    _add(phone="+919876543210", name="A")
    handle = _add(phone="+919000000002", name="B")["contact"]["id"]
    out = asyncio.run(server.outbound_update(handle, _Req({"phone": "9876543210"})))
    assert out.status_code == 409
    assert _stored("9000000002")["name"] == "B"


def test_the_same_person_typed_three_ways_is_one_contact(clean_book):
    assert _add(phone="9876543210", name="A")["ok"] is True
    for again in ("09876543210", "+91 98765 43210"):
        res = asyncio.run(server.outbound_add(_Req({"phone": again, "name": "A"})))
        assert res.status_code == 409, again


# ── scheduled calls, at the API boundary ────────────────────────────────────
#
# The point of these: a scheduled call must go through the SAME guardrails the
# Call button does. `_place_call` is the shared body; these prove the scheduler
# actually calls it and honours its refusals rather than dialling around them.

def _schedule_body(handle, when, kind="call", **kw):
    return {"kind": kind, "contact_id": handle, "when": when.isoformat(), **kw}


def _soon():
    import datetime as dt
    from shared.intelligence import autodial
    probe = autodial.now_local().replace(hour=11, minute=0, second=0, microsecond=0)
    while probe <= autodial.now_local() or not autodial.within_window(probe):
        probe += dt.timedelta(days=1)
    return probe


@pytest.fixture()
def clean_schedules():
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("DELETE FROM scheduled_calls WHERE owner=?", (OWNER,))
    yield
    with get_db() as db:
        db.execute("DELETE FROM scheduled_calls WHERE owner=?", (OWNER,))


def test_scheduling_a_call_stores_it_pending(clean_book, clean_schedules):
    handle = _add(phone="+919876543210", name="Ramesh")["contact"]["id"]
    out = json.loads(asyncio.run(
        server.schedule_create(_Req(_schedule_body(handle, _soon())))).body)
    assert out["ok"] is True
    assert out["schedule"]["status"] == "pending"
    assert out["schedule"]["contact_handle"] == handle


def test_scheduling_an_unknown_contact_is_refused(clean_book, clean_schedules):
    res = asyncio.run(server.schedule_create(_Req(_schedule_body("nope", _soon()))))
    assert res.status_code == 404


def test_a_do_not_call_contact_cannot_be_scheduled(clean_book, clean_schedules):
    """Refused at the form, not silently skipped at the moment it should ring.
    A campaign already refuses to dial 'rejected'; scheduling must not be the
    way around that."""
    handle = _add(phone="+919876543210", name="Ramesh")["contact"]["id"]
    asyncio.run(server.outbound_update(handle, _Req({"disposition": "rejected"})))
    res = asyncio.run(server.schedule_create(_Req(_schedule_body(handle, _soon()))))
    assert res.status_code == 400
    assert "Rejected" in json.loads(res.body)["reason"]


def test_scheduling_outside_calling_hours_is_refused(clean_book, clean_schedules):
    import datetime as dt
    handle = _add(phone="+919876543210", name="Ramesh")["contact"]["id"]
    late = _soon().replace(hour=22)
    res = asyncio.run(server.schedule_create(_Req(_schedule_body(handle, late))))
    assert res.status_code == 400
    assert "calling hours" in json.loads(res.body)["reason"]


def test_scheduling_a_campaign_with_an_empty_audience_is_refused(clean_book, clean_schedules):
    """Refused now rather than firing tomorrow morning and dialling nobody."""
    res = asyncio.run(server.schedule_create(_Req({
        "kind": "campaign", "when": _soon().isoformat(),
        "audience": {"call_status_filter": "not_called"}})))
    assert res.status_code == 400
    assert "nothing to dial" in json.loads(res.body)["reason"]


def test_scheduling_a_campaign_reports_who_it_would_dial(clean_book, clean_schedules):
    _add(phone="+919876543210", name="Ramesh")
    _add(phone="+919876543211", name="Suresh")
    out = json.loads(asyncio.run(server.schedule_create(_Req({
        "kind": "campaign", "when": _soon().isoformat(), "label": "Morning list",
        "audience": {"call_status_filter": "not_called"}}))).body)
    assert out["ok"] is True
    assert out["schedule"]["will_dial"] == 2


def test_a_schedule_can_be_cancelled_then_not_again(clean_book, clean_schedules):
    handle = _add(phone="+919876543210", name="Ramesh")["contact"]["id"]
    sid = json.loads(asyncio.run(
        server.schedule_create(_Req(_schedule_body(handle, _soon())))).body)["schedule"]["id"]
    assert json.loads(asyncio.run(server.schedule_cancel(sid, _Req({}))).body)["ok"] is True
    assert asyncio.run(server.schedule_cancel(sid, _Req({}))).status_code == 409


def test_the_list_names_the_contact_and_flags_a_deleted_one(clean_book, clean_schedules):
    handle = _add(phone="+919876543210", name="Ramesh")["contact"]["id"]
    asyncio.run(server.schedule_create(_Req(_schedule_body(handle, _soon()))))
    listed = json.loads(asyncio.run(server.schedule_list(_Req({}))).body)["schedules"]
    assert listed[0]["contact_name"] == "Ramesh"
    assert listed[0]["contact_missing"] is False

    asyncio.run(server.outbound_delete(handle, _Req({})))
    listed = json.loads(asyncio.run(server.schedule_list(_Req({}))).body)["schedules"]
    assert listed[0]["contact_missing"] is True


def test_a_refused_dial_fails_the_schedule_instead_of_retrying(clean_book, clean_schedules,
                                                               monkeypatch):
    """The scheduler must honour a guardrail refusal — record it and stop, not
    loop. A retry here re-dials a real person with nobody watching."""
    from shared import scheduling
    handle = _add(phone="+919876543210", name="Ramesh")["contact"]["id"]
    sid = json.loads(asyncio.run(
        server.schedule_create(_Req(_schedule_body(handle, _soon())))).body)["schedule"]["id"]

    async def _refuse(*a, **k):
        raise server.DialRefused({"ok": False, "reason": "no minutes left",
                                  "code": "quota_minutes"}, 402)
    monkeypatch.setattr(server, "_place_call", _refuse)
    asyncio.run(server._fire_schedule(
        {"id": sid, "owner": OWNER, "kind": "call", "contact_handle": handle}))

    got = scheduling.get(OWNER, sid)
    assert got["status"] == "failed"
    assert got["last_error"] == "no minutes left"


def test_a_scheduled_call_goes_through_the_shared_dial_path(clean_book, clean_schedules,
                                                            monkeypatch):
    """`_place_call` is where the duplicate guard, the voice-capability gate and
    the billing admission live. The scheduler must not have its own copy."""
    from shared import scheduling
    seen = {}
    handle = _add(phone="+919876543210", name="Ramesh")["contact"]["id"]
    sid = json.loads(asyncio.run(
        server.schedule_create(_Req(_schedule_body(handle, _soon())))).body)["schedule"]["id"]

    async def _spy(owner, contact_id, *, origin, force=False, actor_ip=None):
        seen.update(owner=owner, contact_id=contact_id, origin=origin, force=force)
        return {"ok": True, "phone": "+919876543210", "status": "dialing"}
    monkeypatch.setattr(server, "_place_call", _spy)
    asyncio.run(server._fire_schedule(
        {"id": sid, "owner": OWNER, "kind": "call", "contact_handle": handle}))

    assert seen == {"owner": OWNER, "contact_id": handle,
                    "origin": "schedule", "force": True}
    assert scheduling.get(OWNER, sid)["status"] == "done"
