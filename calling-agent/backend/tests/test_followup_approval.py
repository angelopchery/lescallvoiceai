"""The approval gate, asserted against the real query and a real database.

test_autodial_guardrails.py pins `blocked_reason`, which is a pure function
over a dict. That is the SECOND line of defence. The one that decides who the
executor even looks at is the SQL in `_DUE_SQL`, and a pure-function test
cannot see it: on 2026-07-29 every guardrail in that file was correct and a
customer was still called, because nothing upstream had asked a human.

So these tests insert real rows and call `due_candidates()` for real. If the
approval predicate is ever dropped from the query, exactly these fail.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
# intel_api does `from auth import current_owner`, so the dashboard directory
# has to be importable as a top-level path, exactly as it is in the container
# (working_dir=/home/angelo/lescall/dashboard).
if str(_REPO / "dashboard") not in sys.path:
    sys.path.insert(0, str(_REPO / "dashboard"))

# Reuse the queue suite's throwaway-database machinery: it builds a
# lescall_test DB from the legacy schema plus every versioned migration, so
# 0011 is exercised from scratch on each session.
from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401,E402

OWNER = "gate-test@example.com"


def _insert(**over) -> int:
    """One follow-up that every guardrail except approval would let through:
    due an hour ago, first attempt, dialable, contact not DND."""
    from shared.db.database import get_db

    fields = {
        "owner": OWNER,
        "decision": "yes",
        "mode": "auto",
        "status": "scheduled",
        "reason": "gate test",
        "auto_scheduled": 0,          # customer-stated time -> skips the grace
        "priority": "medium",
    }
    fields.update(over)
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with get_db() as db:
        row = db.execute(
            f"""INSERT INTO followups ({cols}, scheduled_at)
                VALUES ({marks}, now() - interval '1 hour')
                RETURNING id""",
            tuple(fields.values()),
        ).fetchone()
    return int(row["id"])


def _phone_on(fid: int, phone: str | None = None) -> None:
    """due_candidates only returns rows with a dialable number (via the calls
    or contacts join), so give the row a contact to resolve one.

    The number is derived from the follow-up id because contacts carry a
    UNIQUE (owner, phone) — several rows in one test need several contacts.
    """
    from shared.db.database import get_db

    phone = phone or f"+9199999{fid:05d}"
    with get_db() as db:
        c = db.execute(
            "INSERT INTO contacts (owner, name, phone) VALUES (?,?,?) RETURNING id",
            (OWNER, "Gate Test", phone),
        ).fetchone()
        db.execute("UPDATE followups SET contact_id=? WHERE id=?",
                   (int(c["id"]), fid))


@pytest.fixture(autouse=True)
def _clean():
    from shared.db.database import get_db

    with get_db() as db:
        db.execute("DELETE FROM followups WHERE owner=?", (OWNER,))
        db.execute("DELETE FROM contacts WHERE owner=?", (OWNER,))
    yield


def _due_ids() -> set[int]:
    from shared.intelligence import autodial

    return {int(r["id"]) for r in autodial.due_candidates(500)}


# ── the invariant ───────────────────────────────────────────────────────────

def test_a_new_followup_is_not_diallable():
    """The default is the safe one. A row the pipeline just wrote — decision
    yes, mode auto, past due — is invisible to the dialer until a person acts.
    This is the incident, inverted into an assertion."""
    fid = _insert()
    _phone_on(fid)
    assert fid not in _due_ids()


def test_only_the_approved_row_is_returned():
    pending = _insert()
    approved = _insert(approval_state="approved")
    declined = _insert(approval_state="declined")
    for f in (pending, approved, declined):
        _phone_on(f)
    due = _due_ids()
    assert approved in due
    assert pending not in due
    assert declined not in due


def test_expired_approval_is_excluded_by_the_query():
    """Not just by blocked_reason — the row must not even be a candidate."""
    from shared.db.database import get_db

    fid = _insert(approval_state="approved")
    _phone_on(fid)
    assert fid in _due_ids()
    with get_db() as db:
        db.execute(
            "UPDATE followups SET approval_expires_at = now() - interval '1 minute'"
            " WHERE id=?", (fid,))
    assert fid not in _due_ids()


def test_mode_auto_alone_grants_nothing():
    """The regression that caused the incident: commit b693a8c flipped the
    per-agent auto_execute default ON, every new row became mode='auto', and
    that was sufficient to dial. It must never be sufficient again."""
    fid = _insert(mode="auto", approval_state="pending")
    _phone_on(fid)
    assert fid not in _due_ids()


def test_the_patch_endpoint_cannot_approve():
    """Approval has exactly one writer. If `approval_state` ever leaks into
    _FU_PATCHABLE, any client could grant itself permission to call."""
    import intel_api

    assert "approval_state" not in intel_api._FU_PATCHABLE
    assert "approved_by" not in intel_api._FU_PATCHABLE
    assert "approval_expires_at" not in intel_api._FU_PATCHABLE


# ── the approve/decline endpoints ───────────────────────────────────────────

def test_approve_stamps_who_and_when_then_dials():
    import intel_api
    from shared.db.database import get_db

    fid = _insert()
    _phone_on(fid)
    res = intel_api.followup_approve(fid, {"mode": "auto"}, owner=OWNER)
    assert res["ok"] and res["approved"] == [fid]
    with get_db() as db:
        row = db.execute(
            "SELECT approval_state, approved_by, approved_at, approval_expires_at,"
            "       audit FROM followups WHERE id=?", (fid,)).fetchone()
    assert row["approval_state"] == "approved"
    assert row["approved_by"] == OWNER      # the audit answer to "who allowed this"
    assert row["approved_at"] is not None
    assert row["approval_expires_at"] is not None
    assert "approved" in row["audit"]
    assert fid in _due_ids()


def test_approve_is_owner_scoped():
    from fastapi import HTTPException

    import intel_api

    fid = _insert()
    _phone_on(fid)
    with pytest.raises(HTTPException) as exc:
        intel_api.followup_approve(fid, {"mode": "auto"}, owner="someone@else.com")
    assert exc.value.status_code == 404
    assert fid not in _due_ids()


def test_approve_refuses_a_dnd_contact():
    """A customer's "stop calling me" outranks an operator's click, even a
    deliberate one — the endpoint drops the row rather than trusting the UI."""
    import intel_api
    from shared.db.database import get_db

    fid = _insert()
    _phone_on(fid)
    with get_db() as db:
        db.execute(
            "UPDATE contacts SET disposition='rejected'"
            " WHERE id=(SELECT contact_id FROM followups WHERE id=?)", (fid,))
    res = intel_api.followups_approve_bulk({"ids": [fid]}, owner=OWNER)
    assert res["count"] == 0 and res["skipped"] == 1
    assert fid not in _due_ids()


def test_decline_then_reapprove_round_trip():
    """Declining must not be a one-way door: the review queue is the only
    screen that can approve, so a declined row has to remain reachable."""
    import intel_api

    fid = _insert()
    _phone_on(fid)
    intel_api.followup_decline(fid, {"note": "not now"}, owner=OWNER)
    assert fid not in _due_ids()
    assert fid not in {i["id"] for i in
                       intel_api.followup_review_queue(owner=OWNER, limit=200)["items"]}
    shown = intel_api.followup_review_queue(owner=OWNER, limit=200, include_declined=1)["items"]
    assert fid in {i["id"] for i in shown}
    intel_api.followup_approve(fid, {"mode": "auto"}, owner=OWNER)
    assert fid in _due_ids()


# ── the review queue and its evidence ───────────────────────────────────────

def test_review_queue_excludes_decision_no():
    """Surfacing rows the AI already judged pointless invites rubber-stamping."""
    import intel_api

    no = _insert(decision="no")
    _phone_on(no)
    assert no not in {i["id"] for i in
                      intel_api.followup_review_queue(owner=OWNER, limit=200)["items"]}


def test_evidence_marks_an_unbacked_followup_as_inferred():
    """The heart of the review screen. Fifteen of the twenty rows armed on the
    incident day had no commitment on the source call, yet nine of those were
    labelled promised_by='agent_promised'. The UI must be told that the claim
    is unsupported, not shown the claim."""
    import intel_api
    from shared.db.database import get_db

    with get_db() as db:
        call = db.execute(
            """INSERT INTO calls (owner, direction, phone, transcript, started_at)
               VALUES (?, 'outbound', '+919999900001',
                       '[00:01.000]\nAgent: hello there\n\n[00:03.000]\nCaller: hi\n',
                       now()) RETURNING id""", (OWNER,)).fetchone()
    fid = _insert(promised_by="agent_promised", call_id=int(call["id"]))
    _phone_on(fid)
    item = next(i for i in intel_api.followup_review_queue(owner=OWNER, limit=200)["items"]
                if i["id"] == fid)
    assert item["evidence"]["strength"] == "inferred"
    assert item["evidence"]["commitments"] == []
    # …and the operator still gets the words that were actually said.
    assert item["evidence"]["transcript_tail"]


def test_evidence_quotes_a_real_commitment():
    import intel_api
    from shared.db.database import get_db

    quote = "I'll call you back at 4pm tomorrow."
    with get_db() as db:
        call = db.execute(
            """INSERT INTO calls (owner, direction, phone, transcript, started_at)
               VALUES (?, 'outbound', '+919999900001', '[00:01.000]\nAgent: x\n', now())
               RETURNING id""", (OWNER,)).fetchone()
        db.execute(
            """INSERT INTO commitments (call_id, owner, made_by, kind, description, quote)
               VALUES (?, ?, 'agent', 'callback', 'Agent will call back', ?)""",
            (int(call["id"]), OWNER, quote))
    fid = _insert(call_id=int(call["id"]))
    _phone_on(fid)
    item = next(i for i in intel_api.followup_review_queue(owner=OWNER, limit=200)["items"]
                if i["id"] == fid)
    assert item["evidence"]["strength"] == "quoted"
    assert item["evidence"]["commitments"][0]["quote"] == quote
    assert item["evidence"]["commitments"][0]["made_by"] == "agent"
