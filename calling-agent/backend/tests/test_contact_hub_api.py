"""Customer Intelligence Hub API (/api/intel/contacts/*).

Calls the endpoint functions directly (owner passed explicitly — the
current_owner dependency is FastAPI-resolved only over HTTP) against the
lescall_test DB. Proves: hub payload assembly, owner scoping + grant access,
timeline pagination/filters, and the per-contact call list shape.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401  (fixtures)
from tests.test_intel_extract import (  # noqa: F401
    TRANSCRIPT, _insert_call, _llm_output,
)

_REPO = Path(__file__).resolve().parents[3]
for p in (str(_REPO / "dashboard"), str(_REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

import intel_api  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def _prepare_contact_with_history():
    """Seed contact 7's world: analyzed call + memory + digest + timeline."""
    from shared.db.database import get_db
    from shared.intelligence import extract, memory, pipeline, timeline

    with get_db() as db:
        db.execute(
            """INSERT INTO contacts (id, owner, phone, sig_digits, name)
               VALUES (7, 't@x.com', '+919876543210', '9876543210', 'Ramesh')
               ON CONFLICT (id) DO UPDATE SET owner='t@x.com', name='Ramesh'""")
    call_id = _insert_call(transcript=TRANSCRIPT + "\nCaller: haan pakka.")
    call = pipeline._load_call(call_id)
    rec = extract.parse_and_validate(_llm_output(), call)
    pipeline.store_extraction(call, rec, model="test")
    asyncio.run(memory.stage_memory(call))
    asyncio.run(memory.stage_digest(call))
    asyncio.run(timeline.stage_timeline(call))
    return call_id


def test_contact_hub_payload(pg_test_db):
    call_id = _prepare_contact_with_history()
    hub = intel_api.contact_hub(7, owner="t@x.com")
    assert hub["access"] == "owner"
    assert hub["contact"]["name"] == "Ramesh"
    assert hub["stats"]["calls"] >= 1
    assert hub["memory_meta"]["digest"]  # precomputed digest present
    assert isinstance(hub["memory"], dict) and hub["memory"].get("preferences")
    assert hub["scores"]["trend"] and hub["scores"]["latest"]["lead_score"] is not None
    assert any(c["call_id"] == call_id for c in
               [dict(x) for x in hub["open_commitments"]]) or hub["open_commitments"] is not None


def test_contact_hub_owner_scoping(pg_test_db):
    _prepare_contact_with_history()
    with pytest.raises(HTTPException) as exc:
        intel_api.contact_hub(7, owner="stranger@x.com")
    assert exc.value.status_code == 404


def test_contact_hub_grant_access(pg_test_db):
    from shared.db.database import get_db

    _prepare_contact_with_history()
    with get_db() as db:
        db.execute(
            """INSERT INTO contact_grants (contact_id, owner, grantee_email, access)
               VALUES (7, 't@x.com', 'colleague@x.com', 'read')
               ON CONFLICT (contact_id, grantee_email) DO NOTHING""")
    hub = intel_api.contact_hub(7, owner="colleague@x.com")
    assert hub["access"] == "read"
    assert hub["memory_meta"]["digest"]      # same context path as the owner
    assert hub["grants"] == []               # grant roster is owner-only


def test_contact_timeline_pagination_and_filter(pg_test_db):
    _prepare_contact_with_history()
    page = intel_api.contact_timeline(7, owner="t@x.com", before=0, kinds="", limit=2)
    assert len(page["items"]) == 2
    assert page["next_before"] == page["items"][-1]["id"]
    older = intel_api.contact_timeline(7, owner="t@x.com",
                                       before=page["next_before"], kinds="", limit=50)
    assert all(r["id"] < page["next_before"] for r in older["items"])
    only_calls = intel_api.contact_timeline(7, owner="t@x.com", before=0,
                                            kinds="call.inbound,call.outbound",
                                            limit=50)
    assert only_calls["items"]
    assert all(r["kind"].startswith("call.") for r in only_calls["items"])
    assert all(isinstance(r["payload"], dict) for r in only_calls["items"])


def test_grant_lifecycle_owner_only(pg_test_db):
    from shared.db.database import get_db

    _prepare_contact_with_history()
    # a grantee cannot grant further access (owner-only)
    with get_db() as db:
        db.execute(
            """INSERT INTO contact_grants (contact_id, owner, grantee_email, access)
               VALUES (7, 't@x.com', 'reader@x.com', 'read')
               ON CONFLICT (contact_id, grantee_email) DO NOTHING""")
    with pytest.raises(HTTPException) as exc:
        intel_api.contact_grant_add(
            7, {"grantee_email": "friend@x.com", "access": "read"},
            owner="reader@x.com")
    assert exc.value.status_code == 403

    # the owner grants, upgrades, then revokes
    r = intel_api.contact_grant_add(
        7, {"grantee_email": "colleague2@x.com", "access": "read"}, owner="t@x.com")
    intel_api.contact_grant_add(
        7, {"grantee_email": "colleague2@x.com", "access": "edit"}, owner="t@x.com")
    hub = intel_api.contact_hub(7, owner="colleague2@x.com")
    assert hub["access"] == "edit"
    intel_api.contact_grant_revoke(7, r["id"], owner="t@x.com")
    with pytest.raises(HTTPException):
        intel_api.contact_hub(7, owner="colleague2@x.com")


def test_primary_agent_patch_owner_only(pg_test_db):
    from shared.db.database import get_db

    _prepare_contact_with_history()
    intel_api.contact_hub_patch(7, {"primary_agent_slug": "vishal_v2"},
                                owner="t@x.com")
    with get_db() as db:
        row = db.execute(
            "SELECT primary_agent_slug FROM contacts WHERE id=7").fetchone()
    assert row["primary_agent_slug"] == "vishal_v2"
    with get_db() as db:
        db.execute(
            """INSERT INTO contact_grants (contact_id, owner, grantee_email, access)
               VALUES (7, 't@x.com', 'editor@x.com', 'edit')
               ON CONFLICT (contact_id, grantee_email) DO UPDATE SET access='edit'""")
    with pytest.raises(HTTPException) as exc:
        intel_api.contact_hub_patch(7, {"primary_agent_slug": "hijack"},
                                    owner="editor@x.com")
    assert exc.value.status_code == 403


def test_contact_calls_list_shape(pg_test_db):
    call_id = _prepare_contact_with_history()
    out = intel_api.contact_calls(7, owner="t@x.com", page=1, limit=10)
    assert out["total"] >= 1
    row = next(r for r in out["items"] if r["call_id"] == call_id)
    assert row["lead_score"] is not None
    assert isinstance(row["tags"], list)
