"""Public integration API (/v1) — keys, IP policy, rate limits, and export.

Driven through a real ASGI TestClient rather than by calling handlers, because
half of what this surface promises IS the HTTP layer: the Authorization header,
the rate-limit headers, the status codes, the error envelope. A test that calls
the function directly proves none of that.

The client connects from 127.0.0.1 (the nginx hop) and sets `X-Real-IP` per
request, which is exactly how a request reaches this app in production — so the
IP-allowlist tests exercise the real trusted-proxy path, not a stub.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401  (fixtures)

_REPO = Path(__file__).resolve().parents[3]
for p in (str(_REPO / "dashboard"), str(_REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

OWNER = "api-tenant@x.com"
OTHER = "someone-else@x.com"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client(pg_test_db):
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    import public_api

    app = FastAPI()
    public_api.install(app)
    # The peer is loopback, i.e. nginx: X-Real-IP is then honoured, which is the
    # production path for every real request.
    return TestClient(app, client=("127.0.0.1", 50000), raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def clean_api_tables(pg_test_db):
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("DELETE FROM api_rate_windows")
        db.execute("DELETE FROM api_idempotency")
        db.execute("DELETE FROM api_keys")
        db.execute("DELETE FROM api_access_policy")
        db.execute("DELETE FROM commitments")
        db.execute("DELETE FROM call_intelligence")
        db.execute("DELETE FROM calls")
        db.execute("DELETE FROM contacts")
        db.execute("DELETE FROM contact_groups")
    # The limiter keeps an in-process refusal cache; without clearing it, one
    # test exhausting a budget blocks every test that follows in the same minute.
    from shared.public_api import ratelimit
    ratelimit.reset_cache()
    yield


def _key(owner=OWNER, **kw):
    from shared.public_api import keys as keys_mod
    return keys_mod.create_key(owner, name=kw.pop("name", "test key"), **kw)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _seed_call(owner=OWNER, *, call_key="9198765000_20260812_101500",
               transcript="[00:01.000]\nAgent: Namaste.\n\n[00:04.000]\nCaller: Haan ji.",
               analyzed=True, summary="Caller asked about 3BHK in Surat."):
    """One call, optionally with an intelligence record — the shape the export
    endpoints read."""
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute(
            """INSERT INTO calls (conn_id, call_key, direction, owner, agent_slug,
                                  phone, sig_digits, language, transcript, summary,
                                  started_at, ended_at, duration_s, outcome_kind,
                                  recording_path, recording_url, download_token,
                                  collected_data)
               VALUES (?,?, 'inbound', ?, 'pramukh', '+919198765000', '9198765000',
                       'hi-IN', ?, ?, '2026-08-12T10:15:00Z', '2026-08-12T10:17:03Z',
                       123.4, 'visit_booked', 'recordings/x/call_audio.wav',
                       'https://agent.lescall.com/api/recordings/dl/tok123', ?, ?)
               RETURNING id""",
            (f"conn-{call_key}", call_key, owner, transcript, summary,
             f"tok-{call_key}", json.dumps({"budget": "45L"}))).fetchone()
        call_id = row["id"]
        if analyzed:
            db.execute(
                """INSERT INTO call_intelligence (call_id, owner, agent_slug, summary,
                       outcome_summary, customer_intent, sentiment, urgency,
                       lead_score, opportunity_score, risk_score,
                       conversion_probability, topics, objections, entities,
                       processed_at, model)
                   VALUES (?,?, 'pramukh', ?, 'Visit booked', 'buy_flat', 'positive',
                           'high', 82, 74, 12, 0.71, ?, ?, ?, '2026-08-12T10:25:00Z',
                           'test-model')""",
                (call_id, owner, summary,
                 json.dumps([{"text": "3BHK availability"}]),
                 json.dumps([{"text": "price"}]),
                 json.dumps({"names": ["Ramesh"], "amounts": ["45L"]})))
            db.execute(
                """INSERT INTO commitments (call_id, owner, made_by, kind, description)
                   VALUES (?,?, 'agent', 'visit', 'Site visit on Saturday 11am')""",
                (call_id, owner))
    return call_id


# ---------------------------------------------------------------------------
# keys
# ---------------------------------------------------------------------------

def test_token_roundtrip_and_storage(pg_test_db):
    from shared.db.database import get_db
    from shared.public_api import keys as keys_mod

    key = _key()
    token = key["token"]
    assert token.startswith("lk_live_")
    parsed = keys_mod.parse_token(token)
    assert parsed and parsed[0] == key["key_id"]

    with get_db() as db:
        row = dict(db.execute("SELECT * FROM api_keys WHERE key_id=?",
                              (key["key_id"],)).fetchone())
    # The secret must exist NOWHERE in the row — a dump must not be replayable.
    assert parsed[1] not in json.dumps(row, default=str)
    assert row["secret_hash"] and row["secret_hash"] != parsed[1]


def test_bad_and_missing_credentials(client):
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers=_auth("garbage")).json()["error"]["code"] == "invalid_key"
    key = _key()
    tampered = key["token"][:-3] + "aaa"
    body = client.get("/v1/me", headers=_auth(tampered)).json()
    assert body["error"]["code"] == "invalid_key"


def test_x_api_key_header_is_accepted(client):
    """CRM HTTP connectors that cannot set Authorization must still work — the
    alternative is customers putting the key in a query string."""
    key = _key()
    res = client.get("/v1/me", headers={"X-API-Key": key["token"]})
    assert res.status_code == 200
    assert res.json()["account"] == OWNER


def test_revoked_and_expired_keys_are_refused(client):
    import datetime as dt
    from shared.public_api import keys as keys_mod

    live = _key()
    keys_mod.revoke_key(OWNER, live["id"], actor=OWNER)
    assert client.get("/v1/me", headers=_auth(live["token"])).json()["error"]["code"] == "key_revoked"

    past = _key(expires_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1))
    assert client.get("/v1/me", headers=_auth(past["token"])).json()["error"]["code"] == "key_expired"


def test_max_keys_is_enforced(pg_test_db):
    from shared.public_api import policy as policy_mod
    from shared.public_api.errors import ApiError

    policy_mod.upsert_policy(OWNER, enabled=True, ip_allowlist=[], rate_per_min=60,
                             rate_per_day=1000, max_keys=2, actor="admin")
    _key(), _key()
    with pytest.raises(ApiError) as exc:
        _key()
    assert exc.value.status == 409


def test_scopes_gate_endpoints(client):
    key = _key(scopes=["calls:read"])
    _seed_call()
    assert client.get("/v1/calls", headers=_auth(key["token"])).status_code == 200
    res = client.get("/v1/contacts", headers=_auth(key["token"]))
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "insufficient_scope"
    assert res.json()["error"]["details"]["required_scope"] == "contacts:read"


# ---------------------------------------------------------------------------
# IP policy — the operator's boundary
# ---------------------------------------------------------------------------

def test_ip_allowlist_blocks_unlisted_source(client):
    from shared.public_api import policy as policy_mod
    key = _key()
    policy_mod.upsert_policy(OWNER, enabled=True,
                             ip_allowlist=[{"cidr": "203.0.113.0/24", "label": "CRM"}],
                             rate_per_min=60, rate_per_day=1000, max_keys=5,
                             actor="admin")
    blocked = client.get("/v1/me", headers={**_auth(key["token"]), "X-Real-IP": "198.51.100.7"})
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "ip_not_allowed"

    ok = client.get("/v1/me", headers={**_auth(key["token"]), "X-Real-IP": "203.0.113.9"})
    assert ok.status_code == 200
    assert ok.json()["client_ip"] == "203.0.113.9"
    assert ok.json()["ip_restricted"] is True


def test_ip_check_precedes_key_validity(client):
    """An unlisted address must not learn whether the key it presented was
    real, revoked or expired."""
    from shared.public_api import keys as keys_mod, policy as policy_mod
    key = _key()
    keys_mod.revoke_key(OWNER, key["id"], actor=OWNER)
    policy_mod.upsert_policy(OWNER, enabled=True, ip_allowlist=[{"cidr": "203.0.113.9/32"}],
                             rate_per_min=60, rate_per_day=1000, max_keys=5, actor="admin")
    res = client.get("/v1/me", headers={**_auth(key["token"]), "X-Real-IP": "198.51.100.7"})
    assert res.json()["error"]["code"] == "ip_not_allowed"   # NOT key_revoked


def test_most_specific_ip_rule_wins():
    from shared.public_api import policy as policy_mod
    entries = [{"cidr": "203.0.113.0/24", "rate_per_min": 60},
               {"cidr": "203.0.113.9/32", "label": "prod", "rate_per_min": 600}]
    allowed, entry = policy_mod.match_ip("203.0.113.9", entries)
    assert allowed and entry["rate_per_min"] == 600
    allowed, entry = policy_mod.match_ip("203.0.113.10", entries)
    assert allowed and entry["rate_per_min"] == 60
    assert policy_mod.match_ip("198.51.100.1", entries)[0] is False
    # An empty list is "no restriction", not "deny everything".
    assert policy_mod.match_ip("198.51.100.1", [])[0] is True


def test_per_key_allowlist_can_only_narrow(client):
    from shared.public_api import policy as policy_mod
    policy_mod.upsert_policy(OWNER, enabled=True, ip_allowlist=[{"cidr": "203.0.113.0/24"}],
                             rate_per_min=60, rate_per_day=1000, max_keys=5, actor="admin")
    key = _key(ip_allowlist=["203.0.113.9/32"])
    hdr = _auth(key["token"])
    assert client.get("/v1/me", headers={**hdr, "X-Real-IP": "203.0.113.9"}).status_code == 200
    # Inside the ACCOUNT allowlist but outside the key's own narrowing.
    narrowed = client.get("/v1/me", headers={**hdr, "X-Real-IP": "203.0.113.50"})
    assert narrowed.status_code == 403


def test_forged_forwarding_headers_are_ignored_from_untrusted_peer(pg_test_db):
    """The allowlist would be decorative if a caller could name their own IP."""
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from shared.public_api import policy as policy_mod
    import public_api

    app = FastAPI()
    public_api.install(app)
    direct = TestClient(app, client=("198.51.100.7", 4444), raise_server_exceptions=False)
    key = _key()
    policy_mod.upsert_policy(OWNER, enabled=True, ip_allowlist=[{"cidr": "203.0.113.9/32"}],
                             rate_per_min=60, rate_per_day=1000, max_keys=5, actor="admin")
    res = direct.get("/v1/me", headers={**_auth(key["token"]), "X-Real-IP": "203.0.113.9"})
    assert res.status_code == 403


def test_disabled_account_is_refused(client):
    from shared.public_api import policy as policy_mod
    key = _key()
    policy_mod.upsert_policy(OWNER, enabled=False, ip_allowlist=[], rate_per_min=60,
                             rate_per_day=1000, max_keys=5, actor="admin")
    assert client.get("/v1/me", headers=_auth(key["token"])).json()["error"]["code"] == "api_disabled"


def test_allowlist_text_roundtrip():
    from shared.public_api import policy as policy_mod
    text = "203.0.113.9/32 | Pramukh CRM prod | 300 | 100000\n# a comment\n10.0.0.0/8"
    entries, errors = policy_mod.parse_allowlist_text(text)
    assert not errors and len(entries) == 2
    assert entries[0]["rate_per_min"] == 300 and entries[0]["label"] == "Pramukh CRM prod"
    again, errors2 = policy_mod.parse_allowlist_text(
        policy_mod.format_allowlist_text(entries))
    assert not errors2 and again == entries
    _, bad = policy_mod.parse_allowlist_text("not-an-ip")
    assert bad and "not an IP" in bad[0]


# ---------------------------------------------------------------------------
# rate limiting
# ---------------------------------------------------------------------------

def test_rate_limit_blocks_and_reports(client, whole_minute):
    from shared.public_api import policy as policy_mod
    policy_mod.upsert_policy(OWNER, enabled=True, ip_allowlist=[], rate_per_min=3,
                             rate_per_day=1000, max_keys=5, actor="admin")
    key = _key()
    hdr = _auth(key["token"])
    for i in range(3):
        res = client.get("/v1/me", headers=hdr)
        assert res.status_code == 200, i
        assert res.headers["X-RateLimit-Limit"] == "3"
    blocked = client.get("/v1/me", headers=hdr)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert int(blocked.headers["Retry-After"]) >= 1
    assert blocked.headers["X-RateLimit-Remaining"] == "0"


def test_rate_limit_survives_a_restart(client):
    """The counter is in Postgres, so a fresh process does not hand out a fresh
    budget — that is the whole reason it is not an in-memory dict."""
    from shared.public_api import policy as policy_mod, ratelimit
    policy_mod.upsert_policy(OWNER, enabled=True, ip_allowlist=[], rate_per_min=2,
                             rate_per_day=1000, max_keys=5, actor="admin")
    key = _key()
    client.get("/v1/me", headers=_auth(key["token"]))
    counts = ratelimit.usage([key["key_id"]])
    assert counts[key["key_id"]]["minute"] == 1
    assert counts[key["key_id"]]["day"] == 1


def test_per_ip_rule_raises_the_limit_for_one_machine(client):
    from shared.public_api import policy as policy_mod
    policy_mod.upsert_policy(
        OWNER, enabled=True,
        ip_allowlist=[{"cidr": "203.0.113.0/24"},
                      {"cidr": "203.0.113.9/32", "label": "prod", "rate_per_min": 500}],
        rate_per_min=5, rate_per_day=100000, max_keys=5, actor="admin")
    key = _key()
    hot = client.get("/v1/me", headers={**_auth(key["token"]), "X-Real-IP": "203.0.113.9"})
    assert hot.json()["rate_limits"]["per_minute"] == 500
    assert hot.json()["rate_limits"]["applied_from"] == "ip_rule"
    cold = client.get("/v1/me", headers={**_auth(key["token"]), "X-Real-IP": "203.0.113.20"})
    assert cold.json()["rate_limits"]["per_minute"] == 5


def test_key_ceiling_can_only_lower():
    from shared.public_api import policy as policy_mod
    pol = {"rate_per_min": 100, "rate_per_day": 1000}
    assert policy_mod.effective_limits(pol, {"rate_per_min": 10}, None)["per_min"] == 10
    # A key asking for MORE than the account policy does not get it.
    assert policy_mod.effective_limits(pol, {"rate_per_min": 999}, None)["per_min"] == 100


# ---------------------------------------------------------------------------
# contacts in
# ---------------------------------------------------------------------------

def test_contact_push_creates_then_upserts(client):
    key = _key()
    hdr = _auth(key["token"])
    res = client.post("/v1/contacts", headers=hdr, json={
        "phone": "9876543210", "name": "Ramesh Patel", "group": "Website leads",
        "fields": {"crm_id": "LD-88421", "budget": "45L"}})
    assert res.status_code == 201, res.text
    created = res.json()
    assert created["created"] is True
    assert created["phone"] == "+919876543210"     # national -> E.164
    assert created["group_name"] == "Website leads"
    assert created["fields"]["crm_id"] == "LD-88421"

    # Same person, different formatting, extra field: one contact, merged fields,
    # and the name we already knew is NOT blanked by an omitted field.
    again = client.post("/v1/contacts", headers=hdr, json={
        "phone": "+91 98765 43210", "fields": {"stage": "qualified"}})
    assert again.status_code == 200
    body = again.json()
    assert body["created"] is False and body["id"] == created["id"]
    assert body["name"] == "Ramesh Patel"
    assert body["fields"] == {"crm_id": "LD-88421", "budget": "45L", "stage": "qualified"}


def test_contact_push_rejects_unusable_number(client):
    key = _key()
    res = client.post("/v1/contacts", headers=_auth(key["token"]), json={"phone": "123"})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "invalid_request"


def test_contact_upsert_false_conflicts(client):
    key = _key()
    hdr = _auth(key["token"])
    client.post("/v1/contacts", headers=hdr, json={"phone": "9876543211", "name": "A"})
    res = client.post("/v1/contacts", headers=hdr,
                      json={"phone": "9876543211", "name": "B", "upsert": False})
    assert res.status_code == 409 and res.json()["error"]["code"] == "conflict"


def test_bulk_push_is_partial_not_all_or_nothing(client):
    key = _key()
    res = client.post("/v1/contacts/bulk", headers=_auth(key["token"]), json={
        "group": "Import batch 7",
        "contacts": [{"phone": "9876500001", "name": "One"},
                     {"phone": "junk"},
                     {"phone": "9876500002", "name": "Two"},
                     {"phone": "9876500001", "name": "One again"}]})
    assert res.status_code == 200
    body = res.json()
    assert body["created"] == 2 and body["failed"] == 1 and body["updated"] == 1
    assert body["results"][1]["status"] == "failed"
    assert body["results"][2]["status"] == "created"
    listed = client.get("/v1/contacts", headers=_auth(key["token"])).json()
    assert {c["group_name"] for c in listed["data"]} == {"Import batch 7"}


def test_bulk_rejects_oversize_batch(client):
    key = _key()
    res = client.post("/v1/contacts/bulk", headers=_auth(key["token"]),
                      json={"contacts": [{"phone": "9876500001"}] * 501})
    assert res.status_code == 400 and "500" in res.json()["error"]["message"]


def test_idempotency_replays_and_detects_mismatch(client):
    key = _key()
    hdr = {**_auth(key["token"]), "Idempotency-Key": "crm-req-001"}
    first = client.post("/v1/contacts", headers=hdr, json={"phone": "9876500009", "name": "Once"})
    assert first.status_code == 201
    repeat = client.post("/v1/contacts", headers=hdr, json={"phone": "9876500009", "name": "Once"})
    assert repeat.status_code == 201
    assert repeat.headers.get("Idempotent-Replay") == "true"
    assert repeat.json()["id"] == first.json()["id"]

    mismatch = client.post("/v1/contacts", headers=hdr,
                           json={"phone": "9876500010", "name": "Different"})
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "idempotency_mismatch"


def test_contacts_are_owner_scoped(client):
    key = _key()
    other = _key(owner=OTHER)
    mine = client.post("/v1/contacts", headers=_auth(key["token"]),
                       json={"phone": "9876500055", "name": "Mine"}).json()
    res = client.get(f"/v1/contacts/{mine['id']}", headers=_auth(other["token"]))
    assert res.status_code == 404          # not 403: existence is not confirmed


def test_contact_delete(client):
    key = _key()
    hdr = _auth(key["token"])
    made = client.post("/v1/contacts", headers=hdr, json={"phone": "9876500077"}).json()
    assert client.delete(f"/v1/contacts/{made['id']}", headers=hdr).status_code == 200
    assert client.get(f"/v1/contacts/{made['id']}", headers=hdr).status_code == 404


# ---------------------------------------------------------------------------
# calls out
# ---------------------------------------------------------------------------

def test_call_list_shape_and_scores(client):
    _seed_call()
    key = _key()
    body = client.get("/v1/calls", headers=_auth(key["token"])).json()
    assert body["object"] == "list" and len(body["data"]) == 1
    call = body["data"][0]
    assert call["call_id"] == "9198765000_20260812_101500"
    assert call["direction"] == "inbound" and call["agent"] == "pramukh"
    assert call["duration_seconds"] == pytest.approx(123.4)
    assert call["started_at"].endswith("Z") and "T" in call["started_at"]
    assert call["analyzed"] is True and call["scores"]["lead_score"] == 82
    assert call["answer_state"] == "answered"      # NULL is reported, not leaked
    assert call["links"]["transcript"].endswith("/transcript")


def test_call_list_keyset_pagination_is_stable(client):
    for i in range(5):
        _seed_call(call_key=f"9198765{i:03d}_20260812_1015{i:02d}", analyzed=False)
    key = _key()
    hdr = _auth(key["token"])
    page1 = client.get("/v1/calls?order=asc&limit=2", headers=hdr).json()
    assert page1["has_more"] is True and len(page1["data"]) == 2
    # A call landing between pages must not shift the window.
    _seed_call(call_key="9198765999_20260812_999999", analyzed=False)
    page2 = client.get(f"/v1/calls?order=asc&limit=2&cursor={page1['next_cursor']}",
                       headers=hdr).json()
    ids = [c["call_id"] for c in page1["data"]] + [c["call_id"] for c in page2["data"]]
    assert len(ids) == len(set(ids))


def test_call_filters(client):
    _seed_call(call_key="9198765001_20260801_100000")
    _seed_call(call_key="9198765002_20260815_100000", analyzed=False)
    key = _key()
    hdr = _auth(key["token"])
    assert len(client.get("/v1/calls?analyzed=true", headers=hdr).json()["data"]) == 1
    assert len(client.get("/v1/calls?min_lead_score=90", headers=hdr).json()["data"]) == 0
    assert len(client.get("/v1/calls?min_lead_score=50", headers=hdr).json()["data"]) == 1
    assert len(client.get("/v1/calls?sentiment=positive", headers=hdr).json()["data"]) == 1
    assert len(client.get("/v1/calls?direction=outbound", headers=hdr).json()["data"]) == 0
    assert len(client.get("/v1/calls?agent=pramukh", headers=hdr).json()["data"]) == 2
    bad = client.get("/v1/calls?since=yesterday", headers=hdr)
    assert bad.status_code == 400


def test_call_detail_is_the_full_export_record(client):
    _seed_call()
    key = _key()
    body = client.get("/v1/calls/9198765000_20260812_101500",
                      headers=_auth(key["token"])).json()
    assert body["call"]["call_id"] == "9198765000_20260812_101500"
    assert body["intelligence"]["summary"].startswith("Caller asked")
    assert body["intelligence"]["scores"]["lead_score"] == 82
    # JSON columns come back as structures, not strings containing JSON.
    assert body["intelligence"]["topics"] == [{"text": "3BHK availability"}]
    assert body["intelligence"]["entities"]["names"] == ["Ramesh"]
    assert body["commitments"][0]["kind"] == "visit"
    assert body["transcript"]["turns"][0]["speaker"]
    assert body["recording"]["available"] is True
    assert body["recording"]["download_url"].startswith("https://")
    assert body["collected_data"] == {"budget": "45L"}
    assert "omitted_sections" not in body
    # The pipeline's internal checkpoint map is not a customer-facing field.
    assert "stages" not in body["intelligence"]


def test_call_detail_omits_out_of_scope_sections_instead_of_failing(client):
    _seed_call()
    key = _key(scopes=["calls:read"])
    body = client.get("/v1/calls/9198765000_20260812_101500",
                      headers=_auth(key["token"])).json()
    assert body["call"]["call_id"]
    assert body["intelligence"] is None and "transcript" not in body
    assert set(body["omitted_sections"]) == {"intelligence:read", "transcripts:read",
                                             "recordings:read"}
    assert body["recording"]["scope_required"] == "recordings:read"


def test_unanalyzed_call_says_pending_not_error(client):
    _seed_call(call_key="9198765003_20260812_101500", analyzed=False)
    key = _key()
    res = client.get("/v1/calls/9198765003_20260812_101500/intelligence",
                     headers=_auth(key["token"]))
    assert res.status_code == 200
    assert res.json()["analyzed"] is False and res.json()["intelligence"] is None


def test_transcript_json_and_text(client):
    _seed_call()
    key = _key()
    hdr = _auth(key["token"])
    js = client.get("/v1/calls/9198765000_20260812_101500/transcript", headers=hdr).json()
    assert js["turns"] and js["turns"][0]["text"]
    txt = client.get("/v1/calls/9198765000_20260812_101500/transcript?format=text",
                     headers=hdr)
    assert txt.headers["content-type"].startswith("text/plain")
    assert "Namaste" in txt.text


def test_calls_are_owner_scoped(client):
    _seed_call(owner=OWNER)
    other = _key(owner=OTHER)
    res = client.get("/v1/calls/9198765000_20260812_101500", headers=_auth(other["token"]))
    assert res.status_code == 404
    assert client.get("/v1/calls", headers=_auth(other["token"])).json()["data"] == []


def test_recording_404_when_absent(client):
    from shared.db.database import get_db
    _seed_call(call_key="9198765004_20260812_101500", analyzed=False)
    with get_db() as db:
        db.execute("UPDATE calls SET recording_path=NULL, recording_url=NULL "
                   " WHERE call_key='9198765004_20260812_101500'")
    key = _key()
    res = client.get("/v1/calls/9198765004_20260812_101500/recording",
                     headers=_auth(key["token"]))
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

def test_service_root_and_spec_need_no_key(client):
    root = client.get("/v1")
    assert root.status_code == 200 and "documentation" in root.json()
    spec = client.get("/v1/openapi.json")
    assert spec.status_code == 200
    doc = spec.json()
    assert doc["openapi"].startswith("3.1")
    assert "/v1/calls/{call_id}" in doc["paths"]


def test_every_error_uses_one_envelope(client):
    """A CRM branches on `code`; if any path returns a different shape, that
    branch silently stops working."""
    key = _key(scopes=["calls:read"])
    for res in (client.get("/v1/me"),
                client.get("/v1/me", headers=_auth("nope")),
                client.get("/v1/contacts", headers=_auth(key["token"])),
                client.get("/v1/calls/does-not-exist", headers=_auth(key["token"]))):
        body = res.json()
        assert set(body) == {"error"}
        assert {"code", "message"} <= set(body["error"])
        assert res.headers.get("X-Request-Id", "").startswith("req_")


def test_patch_updates_without_blanking_and_can_renumber(client):
    key = _key()
    hdr = _auth(key["token"])
    made = client.post("/v1/contacts", headers=hdr, json={
        "phone": "9876511111", "name": "Renumber Me", "notes": "operator note",
        "fields": {"crm_id": "X1"}}).json()

    # A partial payload leaves untouched fields alone.
    patched = client.patch(f"/v1/contacts/{made['id']}", headers=hdr,
                           json={"fields": {"stage": "won"}}).json()
    assert patched["name"] == "Renumber Me" and patched["notes"] == "operator note"
    assert patched["fields"] == {"crm_id": "X1", "stage": "won"}

    # A new number moves THIS row rather than forking a second person.
    moved = client.patch(f"/v1/contacts/{made['id']}", headers=hdr,
                         json={"phone": "9876522222"}).json()
    assert moved["id"] == made["id"] and moved["phone"] == "+919876522222"
    assert len(client.get("/v1/contacts", headers=hdr).json()["data"]) == 1

    # Renumbering onto somebody else's number is a conflict, not a silent merge.
    other = client.post("/v1/contacts", headers=hdr, json={"phone": "9876533333"}).json()
    clash = client.patch(f"/v1/contacts/{other['id']}", headers=hdr,
                         json={"phone": "9876522222"})
    assert clash.status_code == 409


def test_renumber_keeps_calls_attached(client):
    from shared.db.database import get_db
    key = _key()
    hdr = _auth(key["token"])
    made = client.post("/v1/contacts", headers=hdr, json={"phone": "9876544444"}).json()
    call_id = _seed_call(call_key="9198765777_20260812_101500", analyzed=False)
    with get_db() as db:
        db.execute("UPDATE calls SET contact_id=? WHERE id=?", (int(made["id"]), call_id))

    client.patch(f"/v1/contacts/{made['id']}", headers=hdr, json={"phone": "9876555555"})
    listed = client.get(f"/v1/calls?contact_id={made['id']}", headers=hdr).json()
    assert len(listed["data"]) == 1
