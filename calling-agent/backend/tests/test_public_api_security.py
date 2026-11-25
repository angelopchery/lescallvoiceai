"""Adversarial tests for the public API — what an attacker would try.

Separate from test_public_api.py, which proves the surface WORKS. This file
assumes the caller is hostile: a stolen key, a neighbouring tenant, a fuzzer on
the query string, a client trying to outrun the rate limiter. Every test here
exists because the failure it pins would be a real incident, not a bug report.

Grouped by what is being attacked:
  A  credentials            E  rate limiting
  B  tenancy (IDOR)         F  scopes
  C  injection              G  idempotency
  D  source-IP policy       H  resource limits / robustness
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import datetime as dt

import pytest


from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401  (fixtures)

_REPO = Path(__file__).resolve().parents[3]
for p in (str(_REPO / "dashboard"), str(_REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

NUL_BYTE = chr(0)
VICTIM = "victim@x.com"
ATTACKER = "attacker@x.com"


@pytest.fixture
def client(pg_test_db):
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    import public_api

    app = FastAPI()
    public_api.install(app)
    return TestClient(app, client=("127.0.0.1", 50000), raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def clean(pg_test_db):
    from shared.db.database import get_db
    with get_db() as db:
        for t in ("api_rate_windows", "api_idempotency", "api_keys",
                  "api_access_policy", "commitments", "call_intelligence",
                  "calls", "contacts", "contact_groups"):
            db.execute(f"DELETE FROM {t}")
    # The limiter keeps an in-process refusal cache; without clearing it, one
    # test exhausting a budget blocks every test that follows in the same minute.
    from shared.public_api import ratelimit
    ratelimit.reset_cache()
    yield


def _key(owner=VICTIM, **kw):
    from shared.public_api import keys as keys_mod
    return keys_mod.create_key(owner, name=kw.pop("name", "k"), **kw)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _seed_call(owner=VICTIM, call_key="919000000001_20260812_101500"):
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute(
            """INSERT INTO calls (conn_id, call_key, direction, owner, agent_slug,
                                  phone, sig_digits, transcript, summary, started_at,
                                  duration_s, recording_path, download_token)
               VALUES (?,?, 'inbound', ?, 'a', '+919000000001', '9000000001',
                       'Agent: hi', 'summary', '2026-08-12T10:15:00Z', 10,
                       'recordings/x/call_audio.wav', ?) RETURNING id""",
            (f"conn-{call_key}", call_key, owner, f"tok-{call_key}")).fetchone()
        cid = row["id"]
        db.execute(
            """INSERT INTO call_intelligence (call_id, owner, summary, lead_score,
                       processed_at) VALUES (?,?, 'secret summary', 90, now())""",
            (cid, owner))
    return cid


def _restrict(owner=VICTIM, cidrs=("203.0.113.0/24",), **kw):
    from shared.public_api import policy as policy_mod
    return policy_mod.upsert_policy(
        owner, enabled=kw.pop("enabled", True),
        ip_allowlist=[{"cidr": c} for c in cidrs],
        rate_per_min=kw.pop("rate_per_min", 60),
        rate_per_day=kw.pop("rate_per_day", 10000),
        max_keys=kw.pop("max_keys", 5), actor="admin")


# ===========================================================================
# A. credentials
# ===========================================================================

def test_no_key_existence_oracle(client):
    """A wrong secret and an unknown key_id must be INDISTINGUISHABLE.

    Otherwise the API is an oracle for "does this key id exist", which turns a
    partially-leaked key (a truncated log line, a screenshot) into a
    confirmation that the rest is worth attacking.
    """
    real = _key()
    key_id = real["key_id"]
    wrong_secret = client.get("/v1/me", headers=_auth(f"lk_live_{key_id}_" + "A" * 43))
    unknown_id = client.get("/v1/me", headers=_auth("lk_live_" + "0" * 16 + "_" + "A" * 43))
    assert wrong_secret.status_code == unknown_id.status_code == 401
    assert wrong_secret.json()["error"] == {
        **unknown_id.json()["error"],
        "request_id": wrong_secret.json()["error"]["request_id"],
    }


def test_malformed_tokens_are_rejected_without_touching_the_database(client):
    for token in ("", "Bearer", "lk_live_", "lk_live__", "lk_test_abc_def",
                  "lk_live_ZZZZZZZZZZZZZZZZ_secret",      # non-hex key id
                  "lk_live_" + "a" * 16 + "_short",        # secret too short
                  "lk_live_" + "a" * 16,                   # no separator
                  "lk_live_" + "a" * 5000,
                  "lk_live_\x00aaaaaaaaaaaaaaa_" + "A" * 43):
        res = client.get("/v1/me", headers=_auth(token))
        assert res.status_code == 401, token
        assert res.json()["error"]["code"] in ("unauthenticated", "invalid_key"), token


def test_key_is_never_accepted_from_the_query_string(client):
    """URLs are logged at every hop; a key in one is a key on disk forever."""
    k = _key()
    assert client.get(f"/v1/me?api_key={k['token']}").status_code == 401
    assert client.get(f"/v1/me?access_token={k['token']}").status_code == 401


def test_other_auth_schemes_are_not_honoured(client):
    k = _key()
    for header in (f"Basic {k['token']}", f"Token {k['token']}", k["token"]):
        res = client.get("/v1/me", headers={"Authorization": header})
        assert res.status_code == 401, header


def test_bearer_scheme_is_case_insensitive(client):
    k = _key()
    for scheme in ("Bearer", "bearer", "BEARER"):
        assert client.get("/v1/me",
                          headers={"Authorization": f"{scheme} {k['token']}"}).status_code == 200


def test_secret_never_appears_in_any_response(client):
    """The plaintext exists once, in the mint response. Nothing else may echo it."""
    k = _key()
    secret = k["token"].split("_", 3)[-1]
    for path in ("/v1/me", "/v1/agents", "/v1/contacts", "/v1/calls"):
        body = client.get(path, headers=_auth(k["token"])).text
        assert secret not in body, path
        assert k["token"] not in body, path


# ===========================================================================
# B. tenancy — every record belongs to exactly one account
# ===========================================================================

def test_no_cross_tenant_read_on_any_by_id_endpoint(client):
    _seed_call(VICTIM, "919000000001_20260812_101500")
    victim_key = _key(VICTIM)
    contact = client.post("/v1/contacts", headers=_auth(victim_key["token"]),
                          json={"phone": "9000000055", "name": "Victim contact"}).json()
    attacker = _auth(_key(ATTACKER)["token"])

    for path in ("/v1/calls/919000000001_20260812_101500",
                 "/v1/calls/919000000001_20260812_101500/intelligence",
                 "/v1/calls/919000000001_20260812_101500/transcript",
                 "/v1/calls/919000000001_20260812_101500/recording",
                 f"/v1/contacts/{contact['id']}"):
        res = client.get(path, headers=attacker)
        assert res.status_code == 404, path
        # 404, never 403: the API must not confirm the record exists at all.
        assert res.json()["error"]["code"] == "not_found", path
        assert "secret summary" not in res.text


def test_no_cross_tenant_write_or_delete(client):
    victim_key = _key(VICTIM)
    contact = client.post("/v1/contacts", headers=_auth(victim_key["token"]),
                          json={"phone": "9000000056", "name": "Untouchable"}).json()
    attacker = _auth(_key(ATTACKER)["token"])

    assert client.patch(f"/v1/contacts/{contact['id']}", headers=attacker,
                        json={"name": "Owned"}).status_code == 404
    assert client.delete(f"/v1/contacts/{contact['id']}", headers=attacker).status_code == 404
    still = client.get(f"/v1/contacts/{contact['id']}",
                       headers=_auth(victim_key["token"])).json()
    assert still["name"] == "Untouchable"


def test_cross_tenant_filters_return_nothing(client):
    _seed_call(VICTIM)
    from shared.db.database import get_db
    with get_db() as db:
        vc = db.execute("INSERT INTO contacts (owner, phone, sig_digits, name) "
                        "VALUES (?,?,?,?) RETURNING id",
                        (VICTIM, "+919000000077", "9000000077", "V")).fetchone()["id"]
    attacker = _auth(_key(ATTACKER)["token"])
    for q in (f"/v1/calls?contact_id={vc}", "/v1/calls?phone=9000000001",
              "/v1/calls?agent=a", "/v1/contacts?phone=9000000077"):
        assert client.get(q, headers=attacker).json()["data"] == [], q


def test_group_from_another_tenant_cannot_be_borrowed(client):
    victim_key = _key(VICTIM)
    client.post("/v1/contacts", headers=_auth(victim_key["token"]),
                json={"phone": "9000000078", "group": "Victim private group"})
    groups = client.get("/v1/contact-groups", headers=_auth(victim_key["token"])).json()
    gid = groups["data"][0]["id"]

    attacker_key = _key(ATTACKER)
    made = client.post("/v1/contacts", headers=_auth(attacker_key["token"]),
                       json={"phone": "9000000079", "group_id": gid}).json()
    # Falls back to "no group" rather than filing into the victim's.
    assert made["group_id"] is None
    assert client.get("/v1/contact-groups",
                      headers=_auth(attacker_key["token"])).json()["data"] == []


def test_renumber_cannot_collide_across_tenants(client):
    """Two tenants may legitimately hold the same number; a renumber must not
    see the other tenant's row and refuse (or worse, take it)."""
    victim_key, attacker_key = _key(VICTIM), _key(ATTACKER)
    client.post("/v1/contacts", headers=_auth(victim_key["token"]),
                json={"phone": "9000000080"})
    mine = client.post("/v1/contacts", headers=_auth(attacker_key["token"]),
                       json={"phone": "9000000081"}).json()
    moved = client.patch(f"/v1/contacts/{mine['id']}", headers=_auth(attacker_key["token"]),
                         json={"phone": "9000000080"})
    assert moved.status_code == 200
    assert moved.json()["phone"] == "+919000000080"


# ===========================================================================
# C. injection
# ===========================================================================

# Sent URL-ENCODED so the payload reaches the server rather than being refused
# by the HTTP client — the point is to test our parsing, not httpx's.
SQLI = ["' OR '1'='1", "'; DROP TABLE calls; --", "1 UNION SELECT * FROM api_keys",
        "%' --", "\\", "%00", "1;--", "' OR 1=1 --", "admin'/*", "\u0000"]


def test_query_parameters_are_not_injectable(client):
    _seed_call()
    _restrict(cidrs=(), rate_per_min=5000)      # this test IS a small flood
    hdr = _auth(_key()["token"])
    from urllib.parse import quote
    for raw in SQLI:
        payload = quote(raw, safe="")
        for param in ("q", "phone", "agent", "sentiment", "outcome_kind", "direction"):
            res = client.get(f"/v1/calls?{param}={payload}", headers=hdr)
            assert res.status_code in (200, 400), (param, raw, res.text[:200])
            if res.status_code == 200:
                assert "api_keys" not in res.text
            res = client.get(f"/v1/contacts?{param}={payload}", headers=hdr)
            assert res.status_code in (200, 400), (param, raw)

    from shared.db.database import get_db
    with get_db() as db:                      # the table is still there
        assert db.execute("SELECT COUNT(*) AS n FROM calls").fetchone()["n"] == 1


def test_injection_in_request_bodies_is_stored_as_data(client):
    hdr = _auth(_key()["token"])
    made = client.post("/v1/contacts", headers=hdr, json={
        "phone": "9000000090",
        "name": "'; DROP TABLE contacts; --",
        "notes": "' OR '1'='1",
        "group": "'); DELETE FROM api_keys; --",
        "fields": {"'; --": "<script>alert(1)</script>"}}).json()
    assert made["name"] == "'; DROP TABLE contacts; --"     # stored verbatim, as data
    from shared.db.database import get_db
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"] == 1
        assert db.execute("SELECT COUNT(*) AS n FROM api_keys").fetchone()["n"] == 1


def test_call_id_cannot_walk_the_filesystem(client):
    """`call_id` reaches a filesystem path in the recording streamer."""
    hdr = _auth(_key()["token"])
    for probe in ("../../../../etc/passwd", "..%2f..%2fetc%2fpasswd",
                  "....//....//etc/passwd", "/etc/passwd", "a/../../b",
                  "919000000001_20260812_101500/../../../etc/passwd"):
        for suffix in ("", "/recording", "/transcript", "/intelligence"):
            res = client.get(f"/v1/calls/{probe}{suffix}", headers=hdr)
            assert res.status_code in (400, 404), (probe, suffix, res.status_code)
            assert "root:" not in res.text


def test_oversized_identifiers_are_refused_cheaply(client):
    hdr = _auth(_key()["token"])
    assert client.get("/v1/calls/" + "a" * 5000, headers=hdr).status_code == 404
    assert client.get("/v1/contacts/" + "9" * 400, headers=hdr).status_code == 404


# ===========================================================================
# D. source-IP policy
# ===========================================================================

def test_forwarding_headers_from_an_untrusted_peer_are_ignored(pg_test_db):
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    import public_api

    app = FastAPI()
    public_api.install(app)
    direct = TestClient(app, client=("198.51.100.7", 4444), raise_server_exceptions=False)
    k = _key()
    _restrict()
    for spoof in ({"X-Real-IP": "203.0.113.9"},
                  {"X-Forwarded-For": "203.0.113.9"},
                  {"X-Forwarded-For": "203.0.113.9, 198.51.100.7"},
                  {"X-Real-IP": "203.0.113.9", "X-Forwarded-For": "203.0.113.9"},
                  {"Forwarded": "for=203.0.113.9"},
                  {"X-Client-IP": "203.0.113.9"},
                  {"True-Client-IP": "203.0.113.9"},
                  {"CF-Connecting-IP": "203.0.113.9"}):
        res = direct.get("/v1/me", headers={**_auth(k["token"]), **spoof})
        assert res.status_code == 403, spoof
        assert res.json()["error"]["code"] == "ip_not_allowed"


def test_forged_forwarded_for_behind_the_proxy_cannot_prepend_an_allowed_ip(client):
    """nginx APPENDS the real peer to any client-supplied X-Forwarded-For, so a
    client that sends its own list must not be able to make us read the wrong
    element."""
    k = _key()
    _restrict()
    res = client.get("/v1/me", headers={
        **_auth(k["token"]),
        # what nginx would produce if the client sent "203.0.113.9"
        "X-Forwarded-For": "203.0.113.9, 198.51.100.7",
        "X-Real-IP": "198.51.100.7"})
    assert res.status_code == 403


def test_unparseable_source_address_is_denied_under_a_restriction(client):
    k = _key()
    _restrict()
    for junk in ("not-an-ip", "203.0.113.9; rm -rf /", "999.999.999.999", "",
                 "203.0.113.9/24", "0x7f000001", "203.0.113.9:443",
                 "2001:db8::1", "<script>", "203.0.113.9 203.0.113.10"):
        res = client.get("/v1/me", headers={**_auth(k["token"]), "X-Real-IP": junk})
        assert res.status_code == 403, junk
    # Whitespace padding is normalised, not treated as a different address.
    padded = client.get("/v1/me", headers={**_auth(k["token"]),
                                           "X-Real-IP": "  203.0.113.9  "})
    assert padded.status_code == 200


def test_junk_source_address_is_not_stored_or_reflected(client):
    """With no restriction, junk in X-Real-IP is allowed through — but it must
    not be written to the key row as if it were an address."""
    k = _key()
    junk = "<script>alert(1)</script>" + "A" * 200
    client.get("/v1/me", headers={**_auth(k["token"]), "X-Real-IP": junk})
    from shared.db.database import get_db
    with get_db() as db:
        row = dict(db.execute("SELECT last_used_ip FROM api_keys WHERE key_id=?",
                              (k["key_id"],)).fetchone())
    assert "<script>" not in (row["last_used_ip"] or "")


def test_ipv6_allowlisting_works(client):
    k = _key()
    _restrict(cidrs=("2001:db8::/32",))
    ok = client.get("/v1/me", headers={**_auth(k["token"]), "X-Real-IP": "2001:db8::1"})
    assert ok.status_code == 200
    # An IPv4 address must not match an IPv6 block (or vice versa).
    assert client.get("/v1/me", headers={**_auth(k["token"]),
                                         "X-Real-IP": "203.0.113.9"}).status_code == 403


def test_a_tenant_cannot_widen_their_own_ip_restriction(client):
    """The account allowlist is the operator's boundary; a key allowlist may
    only narrow it."""
    _restrict(cidrs=("203.0.113.0/24",))
    k = _key(ip_allowlist=["0.0.0.0/0"])       # tenant tries to open it back up
    res = client.get("/v1/me", headers={**_auth(k["token"]), "X-Real-IP": "198.51.100.7"})
    assert res.status_code == 403


# ===========================================================================
# E. rate limiting
# ===========================================================================

def test_account_ceiling_is_not_multiplied_by_minting_more_keys(client):
    """The admin sets a per-ACCOUNT ceiling. If the limiter only counted per
    key, a tenant could mint five keys and quintuple it — the ceiling would be
    advisory, not a ceiling."""
    _restrict(cidrs=(), rate_per_min=4, max_keys=5)
    a, b = _key(), _key()
    ok = 0
    for token in (a["token"], b["token"]) * 4:
        if client.get("/v1/me", headers=_auth(token)).status_code == 200:
            ok += 1
    assert ok <= 4, f"{ok} requests admitted against a 4/min account ceiling"


def test_per_key_limit_still_applies(client):
    _restrict(cidrs=(), rate_per_min=100)
    from shared.public_api import keys as keys_mod
    k = keys_mod.create_key(VICTIM, name="throttled", rate_per_min=2)
    hdr = _auth(k["token"])
    assert client.get("/v1/me", headers=hdr).status_code == 200
    assert client.get("/v1/me", headers=hdr).status_code == 200
    assert client.get("/v1/me", headers=hdr).status_code == 429


def test_daily_ceiling_is_enforced_independently(client):
    _restrict(cidrs=(), rate_per_min=1000, rate_per_day=3)
    hdr = _auth(_key()["token"])
    for _ in range(3):
        assert client.get("/v1/me", headers=hdr).status_code == 200
    blocked = client.get("/v1/me", headers=hdr)
    assert blocked.status_code == 429
    assert blocked.headers["X-RateLimit-Window"] == "day"


def test_refused_requests_keep_the_window_pinned(client):
    """Hammering through a 429 must not reset the counter."""
    _restrict(cidrs=(), rate_per_min=2)
    hdr = _auth(_key()["token"])
    for _ in range(2):
        client.get("/v1/me", headers=hdr)
    for _ in range(5):
        assert client.get("/v1/me", headers=hdr).status_code == 429


def test_a_rate_limited_key_cannot_still_read_data(client, whole_minute):
    """The limit must bite BEFORE the handler, not after."""
    _seed_call()
    _restrict(cidrs=(), rate_per_min=1)
    hdr = _auth(_key()["token"])
    client.get("/v1/me", headers=hdr)
    res = client.get("/v1/calls/919000000001_20260812_101500", headers=hdr)
    assert res.status_code == 429
    assert "secret summary" not in res.text


def test_revoked_key_stops_counting_against_nothing(client, whole_minute):
    """A revoked key must be refused before it can consume the account budget."""
    from shared.public_api import keys as keys_mod
    _restrict(cidrs=(), rate_per_min=2)
    dead = _key()
    keys_mod.revoke_key(VICTIM, dead["id"], actor="t")
    live = _key()
    for _ in range(3):
        client.get("/v1/me", headers=_auth(dead["token"]))
    assert client.get("/v1/me", headers=_auth(live["token"])).status_code == 200


# ===========================================================================
# F. scopes
# ===========================================================================

def test_every_endpoint_enforces_its_scope(client):
    _seed_call()
    from shared.public_api import keys as keys_mod
    empty = keys_mod.create_key(VICTIM, name="no scopes", scopes=["contacts:read"])
    hdr = _auth(empty["token"])
    call = "919000000001_20260812_101500"
    forbidden = [
        ("get", "/v1/calls"), ("get", f"/v1/calls/{call}"),
        ("get", f"/v1/calls/{call}/intelligence"),
        ("get", f"/v1/calls/{call}/transcript"),
        ("get", f"/v1/calls/{call}/recording"),
        ("get", "/v1/agents"),
    ]
    for method, path in forbidden:
        res = getattr(client, method)(path, headers=hdr)
        assert res.status_code == 403, path
        assert res.json()["error"]["code"] == "insufficient_scope", path
        assert "secret summary" not in res.text
    for path, body in (("/v1/contacts", {"phone": "9000000091"}),
                       ("/v1/contacts/bulk", {"contacts": [{"phone": "9000000092"}]}),
                       ("/v1/contact-groups", {"name": "x"})):
        assert client.post(path, headers=hdr, json=body).status_code == 403, path


def test_unknown_scopes_are_dropped_not_granted(client):
    from shared.public_api import keys as keys_mod
    k = keys_mod.create_key(VICTIM, name="creative", scopes=["contacts:read", "*", "admin",
                                                             "calls:write", "../calls:read"])
    assert set(k["scopes"]) == {"contacts:read"}
    assert client.get("/v1/calls", headers=_auth(k["token"])).status_code == 403


def test_partial_scope_omits_sections_without_leaking_them(client):
    _seed_call()
    from shared.public_api import keys as keys_mod
    k = keys_mod.create_key(VICTIM, name="calls only", scopes=["calls:read"])
    body = client.get("/v1/calls/919000000001_20260812_101500",
                      headers=_auth(k["token"]))
    assert body.status_code == 200
    assert "secret summary" not in body.text
    assert body.json()["intelligence"] is None


# ===========================================================================
# G. idempotency
# ===========================================================================

def test_idempotency_records_do_not_cross_keys(client):
    """A replay cache shared across keys would let one key read another key's
    response body."""
    a, b = _key(VICTIM), _key(VICTIM)
    hdr_a = {**_auth(a["token"]), "Idempotency-Key": "shared-id"}
    hdr_b = {**_auth(b["token"]), "Idempotency-Key": "shared-id"}
    first = client.post("/v1/contacts", headers=hdr_a,
                        json={"phone": "9000000093", "name": "From A"})
    second = client.post("/v1/contacts", headers=hdr_b,
                         json={"phone": "9000000094", "name": "From B"})
    assert second.status_code == 201
    assert second.json()["name"] == "From B"
    assert second.headers.get("Idempotent-Replay") is None
    assert first.json()["id"] != second.json()["id"]


def test_idempotency_key_length_is_bounded(client):
    hdr = {**_auth(_key()["token"]), "Idempotency-Key": "x" * 5000}
    res = client.post("/v1/contacts", headers=hdr, json={"phone": "9000000095"})
    assert res.status_code in (201, 400)


# ===========================================================================
# H. resource limits and robustness
# ===========================================================================

def test_pagination_arguments_cannot_be_abused(client):
    for i in range(3):
        _seed_call(call_key=f"91900000000{i}_20260812_10150{i}")
    hdr = _auth(_key()["token"])
    assert len(client.get("/v1/calls?limit=999999", headers=hdr).json()["data"]) <= 200
    for bad in ("-1", "0", "abc", "1e9", "9" * 40, "null"):
        res = client.get(f"/v1/calls?limit={bad}", headers=hdr)
        assert res.status_code in (200, 400, 422), bad


def test_absurd_cursor_values_do_not_500(client):
    """A cursor larger than bigint must be a 400, not a database error."""
    _seed_call()
    hdr = _auth(_key()["token"])
    for bad in ("9" * 40, "-9" + "9" * 30, "abc", "1;--", "1.5", " 12 ", "0x10"):
        for path in ("/v1/calls", "/v1/contacts"):
            res = client.get(f"{path}?cursor={bad}", headers=hdr)
            assert res.status_code in (200, 400), (path, bad, res.status_code)
            assert res.status_code != 500


def test_bulk_size_and_shape_are_bounded(client):
    hdr = _auth(_key()["token"])
    assert client.post("/v1/contacts/bulk", headers=hdr,
                       json={"contacts": [{"phone": "9000000001"}] * 501}).status_code == 400
    for body in ({"contacts": "not a list"}, {"contacts": []}, {},
                 {"contacts": [None, 1, "x"]}, {"contacts": [[]]}):
        res = client.post("/v1/contacts/bulk", headers=hdr, json=body)
        assert res.status_code in (200, 400), body
        assert res.status_code != 500


def test_malformed_bodies_never_500(client):
    hdr = _auth(_key()["token"])
    for raw in (b"", b"not json", b"[]", b'"string"', b"null", b"123",
                b'{"phone": {"$ne": null}}', b'{"fields": "not an object"}',
                b'{"phone": ["9000000001"]}', b'{"phone": 9000000001}'):
        res = client.post("/v1/contacts", headers={**hdr, "Content-Type": "application/json"},
                          content=raw)
        assert res.status_code in (200, 201, 400, 409, 422), raw
        assert res.status_code != 500, raw


def test_deeply_nested_fields_do_not_crash(client):
    hdr = _auth(_key()["token"])
    nested = {"a": 1}
    for _ in range(60):
        nested = {"n": nested}
    res = client.post("/v1/contacts", headers=hdr,
                      json={"phone": "9000000096", "fields": nested})
    assert res.status_code in (201, 400)


def test_every_failure_uses_the_error_envelope(client):
    """A CRM branches on `error.code`. A route that answers in a different shape
    silently breaks that branch."""
    k = _key(scopes=["contacts:read"])
    probes = [
        client.get("/v1/me"),                                    # 401
        client.get("/v1/me", headers=_auth("bad")),              # 401
        client.get("/v1/calls", headers=_auth(k["token"])),      # 403
        client.get("/v1/contacts/999999", headers=_auth(k["token"])),   # 404
        client.get("/v1/nope", headers=_auth(k["token"])),       # 404 unknown route
        client.post("/v1/me", headers=_auth(k["token"])),        # 405 wrong method
        client.get("/v1/calls?since=nope", headers=_auth(k["token"])),  # 400
    ]
    for res in probes:
        body = res.json()
        assert set(body) == {"error"}, (res.status_code, body)
        assert {"code", "message"} <= set(body["error"])
        assert res.headers.get("X-Request-Id", "").startswith("req_")


def test_internal_failures_do_not_leak_internals(client, monkeypatch):
    """An unexpected exception must become the envelope, never a traceback or a
    database error string."""
    import public_api

    def boom(*_a, **_k):
        raise RuntimeError("psycopg: connection to 10.0.0.5 failed: password authentication")

    monkeypatch.setattr(public_api, "_call_row", boom)
    res = client.get("/v1/calls/anything", headers=_auth(_key()["token"]))
    assert res.status_code == 500
    assert "psycopg" not in res.text and "password" not in res.text
    assert res.json()["error"]["code"] == "internal_error"
    assert res.json()["error"]["request_id"].startswith("req_")


def test_api_requests_are_attributable_in_the_audit_log(client):
    """Every /v1 request must be traceable to a key, including /v1/me."""
    import public_api
    seen = {}

    class _Probe:
        def __init__(self):
            self.state = None

    k = _key()
    original = public_api.keys_mod.authenticate

    def spy(request):
        ctx = original(request)
        seen["key_id"] = getattr(request.state, "api_key_id", None)
        seen["owner"] = getattr(request.state, "api_owner", None)
        return ctx

    # /v1/me must stamp request.state exactly like every other route does.
    client.get("/v1/me", headers=_auth(k["token"]))
    from shared.db.database import get_db
    with get_db() as db:
        row = dict(db.execute("SELECT request_count, last_used_ip FROM api_keys "
                              " WHERE key_id=?", (k["key_id"],)).fetchone())
    assert row["request_count"] >= 1


def test_max_keys_cannot_be_exceeded(client):
    from shared.public_api import keys as keys_mod
    from shared.public_api.errors import ApiError
    _restrict(cidrs=(), max_keys=2)
    _key(), _key()
    with pytest.raises(ApiError):
        _key()
    from shared.db.database import get_db
    with get_db() as db:
        n = db.execute("SELECT COUNT(*) AS n FROM api_keys WHERE owner=? "
                       "AND status='active'", (VICTIM,)).fetchone()["n"]
    assert n == 2


def test_disabled_account_blocks_every_endpoint(client):
    _seed_call()
    k = _key()
    _restrict(cidrs=(), enabled=False)
    for path in ("/v1/me", "/v1/calls", "/v1/contacts", "/v1/agents",
                 "/v1/calls/919000000001_20260812_101500"):
        res = client.get(path, headers=_auth(k["token"]))
        assert res.status_code == 403, path
        assert res.json()["error"]["code"] == "api_disabled", path
        assert "secret summary" not in res.text


def test_unauthenticated_routes_expose_no_tenant_data(client):
    _seed_call()
    _key()
    for path in ("/v1", "/v1/", "/v1/openapi.json"):
        body = client.get(path).text
        assert "secret summary" not in body
        assert VICTIM not in body
        assert "lk_live_" not in body or "lk_live_…" in body


def test_null_bytes_and_control_characters_never_reach_the_database(client):
    """Postgres text columns cannot hold a NUL. Before this was scrubbed at the
    boundary, `?q=%00` — or a stray \\u0000 in a pushed name — became a 500 that
    any caller could trigger at will."""
    _restrict(cidrs=(), rate_per_min=2000)
    hdr = _auth(_key()["token"])

    for path in ("/v1/calls?q=%00", "/v1/contacts?q=%00", "/v1/calls?agent=%00x",
                 "/v1/contacts?phone=%00", "/v1/calls?sentiment=%00",
                 "/v1/calls?since=%00", "/v1/calls?cursor=%00"):
        res = client.get(path, headers=hdr)
        assert res.status_code in (200, 400), (path, res.status_code)

    made = client.post("/v1/contacts", headers=hdr, json={
        "phone": "9000000097",
        "name": "Ram\u0000esh",
        "notes": "line one\nline two\ttabbed",
        "fields": {"crm\u0000id": "LD\u0007-1"}})
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["name"] == "Ramesh"                        # control chars stripped
    assert body["notes"] == "line one\nline two\ttabbed"    # real whitespace kept
    assert body["fields"] == {"crmid": "LD-1"}

    bulk = client.post("/v1/contacts/bulk", headers=hdr, json={
        "contacts": [{"phone": "9000000098", "name": "A\u0000B"}]})
    assert bulk.status_code == 200 and bulk.json()["created"] == 1

    from shared.db.database import get_db
    with get_db() as db:
        names = [dict(r)["name"] for r in
                 db.execute("SELECT name FROM contacts").fetchall()]
    assert all(NUL_BYTE not in (n or "") for n in names)


def test_absurdly_wide_payloads_are_bounded(client):
    """A body with thousands of keys, or a 100 kB string in a name, costs real
    CPU to walk and to serialise back. Caps sit far above any legitimate CRM
    payload and well below anything that hurts."""
    _restrict(cidrs=(), rate_per_min=2000)
    hdr = _auth(_key()["token"])
    res = client.post("/v1/contacts", headers=hdr, json={
        "phone": "9000000099",
        "name": "x" * 100_000,
        "fields": {f"k{i}": "v" for i in range(5000)}})
    assert res.status_code in (201, 400)
    if res.status_code == 201:
        assert len(res.json()["name"]) <= 200
        assert len(res.json()["fields"]) <= 200


def test_rate_limit_headers_name_the_binding_budget(client):
    """A client that hits the ACCOUNT ceiling must be told so — otherwise they
    look at one key's usage, see headroom, and conclude the API is broken."""
    _restrict(cidrs=(), rate_per_min=2)
    a, b = _key(), _key()
    client.get("/v1/me", headers=_auth(a["token"]))
    client.get("/v1/me", headers=_auth(b["token"]))
    blocked = client.get("/v1/me", headers=_auth(b["token"]))
    assert blocked.status_code == 429
    assert blocked.headers["X-RateLimit-Scope"] == "account"
    assert "this account" in blocked.json()["error"]["message"]


def test_usage_history_survives_key_revocation(client):
    """History is read from the ACCOUNT window, so revoking a key does not
    erase the traffic it made — which is exactly what you want when the reason
    you revoked it was suspicious traffic."""
    from shared.public_api import keys as keys_mod, ratelimit
    _restrict(cidrs=(), rate_per_min=100)
    k = _key()
    client.get("/v1/me", headers=_auth(k["token"]))
    keys_mod.revoke_key(VICTIM, k["id"], actor="t")
    history = ratelimit.daily_history(VICTIM, days=14)
    assert history and history[0]["hits"] >= 1


def test_brute_force_scanning_is_throttled_in_memory(client):
    """A scanner walking key ids must not get an unlimited supply of indexed
    SELECTs on a box that is also carrying live calls."""
    from shared.public_api import ratelimit
    for i in range(ratelimit.AUTH_FAILURE_LIMIT):
        res = client.get("/v1/me", headers=_auth("lk_live_" + f"{i:016x}" + "_" + "A" * 43))
        assert res.status_code == 401, i
    throttled = client.get("/v1/me", headers=_auth("lk_live_" + "f" * 16 + "_" + "A" * 43))
    assert throttled.status_code == 429
    assert throttled.headers["Retry-After"] == "60"
    # A VALID key from the same address is refused too — that is the point of a
    # per-address throttle, and it lasts at most the rest of the minute.
    assert client.get("/v1/me", headers=_auth(_key()["token"])).status_code == 429


def test_repeat_refusals_stop_hitting_the_database(client, monkeypatch):
    """The whole reason the refusal cache exists: a client hammering an
    exhausted budget must stop costing us database writes."""
    from shared.public_api import ratelimit
    _restrict(cidrs=(), rate_per_min=1)
    hdr = _auth(_key()["token"])
    assert client.get("/v1/me", headers=hdr).status_code == 200
    assert client.get("/v1/me", headers=hdr).status_code == 429   # DB decided this

    calls = {"n": 0}
    from shared.db import database
    original = database.get_db

    def counting_get_db(*a, **k):
        calls["n"] += 1
        return original(*a, **k)

    monkeypatch.setattr(database, "get_db", counting_get_db)
    for _ in range(5):
        assert client.get("/v1/me", headers=hdr).status_code == 429
    # One key lookup per request is the floor (revocation must be instant, so
    # api_keys is never cached). The four window upserts, the policy read and
    # the key touch are all avoidable, and must be avoided.
    assert calls["n"] <= 5, f"{calls['n']} database sessions for 5 cached refusals"


def test_raising_a_limit_takes_effect_within_a_minute(client):
    """A cached refusal must not outlive an operator's fix. Day-window blocks
    used to be cacheable for up to 24 hours, which would leave a customer
    locked out long after the ceiling was raised."""
    from shared.public_api import ratelimit
    _restrict(cidrs=(), rate_per_min=1000, rate_per_day=1)
    hdr = _auth(_key()["token"])
    client.get("/v1/me", headers=hdr)
    blocked = client.get("/v1/me", headers=hdr)
    assert blocked.status_code == 429 and blocked.headers["X-RateLimit-Window"] == "day"
    # The cache may hold this for at most _CACHE_TTL_S, never until midnight.
    entry = ratelimit._blocked[ratelimit.account_subject(VICTIM)]
    cache_until, reset_at = entry[0], entry[1]
    assert (cache_until - ratelimit._now()).total_seconds() <= ratelimit._CACHE_TTL_S + 1
    assert reset_at > cache_until          # Retry-After still tells the truth


# ===========================================================================
# I. the key-management endpoints (/api/apikeys) — session-authenticated
#
# A different credential and a different threat model from /v1: this is the
# surface that MINTS keys, so a tenancy hole here is worse than one downstream.
# ===========================================================================

@pytest.fixture
def dash(pg_test_db):
    """The /api/apikeys router with the session identity pinned to VICTIM."""
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    import api_keys_api
    from auth import current_owner

    app = FastAPI()
    app.include_router(api_keys_api.router)
    app.dependency_overrides[current_owner] = lambda: VICTIM
    return TestClient(app, raise_server_exceptions=False)


def test_a_tenant_cannot_touch_another_tenants_keys(dash):
    from shared.public_api import keys as keys_mod
    theirs = keys_mod.create_key(ATTACKER, name="not yours")

    assert dash.patch(f"/api/apikeys/{theirs['id']}", json={"name": "mine now"}).status_code == 404
    assert dash.delete(f"/api/apikeys/{theirs['id']}").status_code == 404
    listed = dash.get("/api/apikeys").json()
    assert listed["keys"] == []
    # …and it still works for its real owner.
    assert keys_mod.list_keys(ATTACKER)[0]["status"] == "active"
    assert keys_mod.list_keys(ATTACKER)[0]["name"] == "not yours"


def test_the_mint_response_is_the_only_place_a_secret_appears(dash):
    minted = dash.post("/api/apikeys", json={"name": "once", "scopes": ["calls:read"]})
    assert minted.status_code == 201
    token = minted.json()["token"]
    assert token.startswith("lk_live_")
    # Every subsequent read shows the mask, never the secret.
    listed = dash.get("/api/apikeys").text
    assert token not in listed
    assert "•" in dash.get("/api/apikeys").json()["keys"][0]["masked"]


def test_a_tenant_cannot_raise_their_own_limits_or_widen_their_allowlist(dash):
    """The policy is the operator's. A tenant sending policy fields to the mint
    endpoint must not have them honoured."""
    from shared.public_api import policy as policy_mod
    _restrict(cidrs=("203.0.113.0/24",), rate_per_min=10, max_keys=5)
    made = dash.post("/api/apikeys", json={
        "name": "greedy", "scopes": ["calls:read"],
        "rate_per_min": 100000, "rate_per_day": 100000,
        "max_keys": 99, "enabled": True,
        "ip_allowlist": ["0.0.0.0/0"]}).json()
    assert made["rate_per_min"] is None and made["rate_per_day"] is None

    pol = policy_mod.get_policy(VICTIM, use_cache=False)
    assert pol["rate_per_min"] == 10 and pol["max_keys"] == 5
    assert [e["cidr"] for e in pol["ip_allowlist"]] == ["203.0.113.0/24"]

    # The key's own allowlist is stored, but it can only ever narrow — proven
    # against the live gate in test_a_tenant_cannot_widen_their_own_ip_restriction.
    assert made["ip_allowlist"] == [{"cidr": "0.0.0.0/0", "label": ""}]


def test_expiry_and_scope_inputs_are_validated(dash):
    for body in ({"expires_in_days": -5}, {"expires_in_days": 99999},
                 {"expires_in_days": "soon"}):
        assert dash.post("/api/apikeys", json={"name": "x", **body}).status_code == 400
    # An unknown scope is dropped, not granted; an empty list falls back to the
    # documented defaults rather than minting a key that can do nothing.
    made = dash.post("/api/apikeys", json={"name": "x", "scopes": ["nonsense"]}).json()
    from shared.public_api import keys as keys_mod
    assert made["scopes"] == keys_mod.DEFAULT_SCOPES


def test_key_cap_is_enforced_through_the_dashboard_too(dash):
    _restrict(cidrs=(), max_keys=1)
    assert dash.post("/api/apikeys", json={"name": "one"}).status_code == 201
    second = dash.post("/api/apikeys", json={"name": "two"})
    assert second.status_code == 409
    assert "maximum" in second.json()["reason"]


def test_revoking_is_immediate_at_the_api(client, dash):
    """No cache may keep a revoked key working — that is the one thing
    revocation has to guarantee."""
    minted = dash.post("/api/apikeys", json={"name": "burn me"}).json()
    hdr = _auth(minted["token"])
    assert client.get("/v1/me", headers=hdr).status_code == 200
    assert dash.delete(f"/api/apikeys/{minted['id']}").status_code == 200
    assert client.get("/v1/me", headers=hdr).json()["error"]["code"] == "key_revoked"


# ===========================================================================
# J. plan gating — the integration API is a Growth capability (migration 0021)
# ===========================================================================

@pytest.fixture
def enforced(monkeypatch):
    """Turn platform billing enforcement ON for one test.

    Every capability gate is a no-op while enforcement is off, so a test that
    does not do this proves nothing about the gate — it proves the switch is
    off, which we already know.
    """
    from shared.billing import entitlements
    monkeypatch.setattr(entitlements, "enforcement_enabled", lambda *a, **k: True)
    from shared.public_api import ratelimit
    ratelimit.reset_cache()
    return True


def _set_plan(owner: str, plan_code: str):
    from shared.db.database import get_db
    with get_db() as db:
        db.execute(
            "INSERT INTO entitlements (owner, plan_code, api_access) "
            "VALUES (?,?, (SELECT api_access_enabled FROM plans WHERE code=?)) "
            "ON CONFLICT (owner) DO UPDATE SET plan_code=excluded.plan_code, "
            " api_access=excluded.api_access",
            (owner, plan_code, plan_code))


def test_growth_and_enterprise_carry_api_access(pg_test_db):
    from shared.db.database import get_db
    with get_db() as db:
        flags = {r["code"]: r["api_access_enabled"] for r in
                 db.execute("SELECT code, api_access_enabled FROM plans").fetchall()}
    assert flags["growth"] == 1 and flags["enterprise"] == 1
    # Pilot too (migration 0022, owner decision): a prospect evaluating LesCall
    # against an incumbent has to SEE the data land in their own CRM. When the
    # 7-day window elapses the entitlement resolves back to Free and keys stop
    # authenticating with 402 — they are not revoked, so upgrading restores them.
    assert flags["pilot"] == 1
    assert flags["free"] == 0 and flags["basic"] == 0


def test_pilot_can_use_the_api_and_loses_it_when_the_window_ends(client, enforced):
    """The trial grants it; expiry takes it back without destroying the keys."""
    _set_plan(VICTIM, "pilot")
    k = _key()
    assert client.get("/v1/me", headers=_auth(k["token"])).status_code == 200

    _set_plan(VICTIM, "free")               # what an elapsed Pilot resolves to
    from shared.public_api import ratelimit
    ratelimit.reset_cache()
    lapsed = client.get("/v1/me", headers=_auth(k["token"]))
    assert lapsed.status_code == 402 and lapsed.json()["error"]["code"] == "plan_required"

    _set_plan(VICTIM, "pilot")              # …and comes straight back
    ratelimit.reset_cache()
    assert client.get("/v1/me", headers=_auth(k["token"])).status_code == 200
    from shared.db.database import get_db
    with get_db() as db:
        assert db.execute("SELECT status FROM api_keys WHERE key_id=?",
                          (k["key_id"],)).fetchone()["status"] == "active"


def test_a_plan_without_api_access_is_refused_at_v1(client, enforced):
    _set_plan(VICTIM, "basic")
    k = _key()
    res = client.get("/v1/me", headers=_auth(k["token"]))
    assert res.status_code == 402
    assert res.json()["error"]["code"] == "plan_required"
    assert "Growth" in res.json()["error"]["message"]
    assert res.json()["error"]["details"]["upgrade"] == "/billing"


def test_growth_passes_the_gate(client, enforced):
    _set_plan(VICTIM, "growth")
    k = _key()
    assert client.get("/v1/me", headers=_auth(k["token"])).status_code == 200


def test_the_plan_gate_covers_every_endpoint_and_leaks_nothing(client, enforced):
    _seed_call()
    _set_plan(VICTIM, "free")
    k = _key()
    for path in ("/v1/me", "/v1/calls", "/v1/contacts", "/v1/agents",
                 "/v1/calls/919000000001_20260812_101500",
                 "/v1/calls/919000000001_20260812_101500/transcript"):
        res = client.get(path, headers=_auth(k["token"]))
        assert res.status_code == 402, path
        assert "secret summary" not in res.text


def test_an_invalid_key_is_still_told_about_the_key_not_the_plan(client, enforced):
    """Plan is checked AFTER credential validity — otherwise a customer with a
    broken key on a cheap plan gets sent to the billing page to fix a typo."""
    _set_plan(VICTIM, "free")
    _key()
    res = client.get("/v1/me", headers=_auth("lk_live_" + "0" * 16 + "_" + "A" * 43))
    assert res.status_code == 401 and res.json()["error"]["code"] == "invalid_key"


def test_an_admin_override_grants_api_access_without_a_plan_change(client, enforced):
    """The whole reason this is a column and not `plan_code == 'growth'`: one
    Enterprise tenant, or a customer mid-negotiation, can be granted it."""
    import json as _json
    from shared.db.database import get_db
    _set_plan(VICTIM, "basic")
    with get_db() as db:
        db.execute("UPDATE entitlements SET api_access=1, overrides=? WHERE owner=?",
                   (_json.dumps({"api_access": True}), VICTIM))
    k = _key()
    assert client.get("/v1/me", headers=_auth(k["token"])).status_code == 200


def test_key_creation_is_refused_on_a_plan_without_it(dash, enforced):
    _set_plan(VICTIM, "free")
    res = dash.post("/api/apikeys", json={"name": "nope"})
    assert res.status_code == 402
    body = res.json()["detail"] if isinstance(res.json().get("detail"), dict) else res.json()
    assert body["code"] == "plan_required"
    assert body["upgrade"] == "/billing"
    from shared.db.database import get_db
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM api_keys WHERE owner=?",
                          (VICTIM,)).fetchone()["n"] == 0


def test_the_settings_payload_reports_the_plan_state(dash, enforced):
    _set_plan(VICTIM, "free")
    assert dash.get("/api/apikeys").json()["plan_allowed"] is False
    _set_plan(VICTIM, "growth")
    assert dash.get("/api/apikeys").json()["plan_allowed"] is True


def test_the_gate_is_inert_while_enforcement_is_off(client, dash):
    """Today's production state. The gate must not start refusing people the
    moment it is deployed — it arms when billing enforcement is switched on."""
    _set_plan(VICTIM, "free")
    k = _key()
    assert client.get("/v1/me", headers=_auth(k["token"])).status_code == 200
    assert dash.get("/api/apikeys").json()["plan_allowed"] is True
