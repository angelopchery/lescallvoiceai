"""WhatsApp through Interakt (shared/whatsapp/interakt.py, RUNBOOK §80).

Pure units first (key normalisation, phone split, the positional payload,
webhook signature + event parsing, hand-registered templates), then the
database paths: a registered template + a playbook with one brochure per
project → a send that carries the project and the file; the sender holding a
send until a brochure is attached and then delivering it through Interakt
with the file as a /r/f/ URL; the per-tenant webhook; owner scoping.

Interakt is never contacted: interakt.send_template / verify_key are
monkeypatched wherever a send or a connect happens.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401  (fixtures)
from tests.test_whatsapp import (  # noqa: F401  (fixtures + helpers)
    OTHER, OWNER, _insert_call, _run_stage, _sends, wa_db,
)

_REPO = Path(__file__).resolve().parents[3]
for p in (str(_REPO / "dashboard"), str(_REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

from shared.whatsapp import interakt, sender, templates, webhook  # noqa: E402
from shared.whatsapp.graph import GraphError  # noqa: E402

KEY = "ekhCX3lLeVViRnN6NS1LRkxVSXFoV0tWV1dScWVORDJJaEdaVkptSllvbzo="


# ===========================================================================
# pure units
# ===========================================================================

def test_key_is_accepted_bare_or_as_the_header_value():
    assert interakt.normalize_key(f"  Basic {KEY} ") == KEY
    assert interakt.normalize_key(KEY) == KEY
    assert interakt.normalize_key("not a key!") == ""
    assert interakt.normalize_key("") == ""


def test_phone_is_split_into_country_code_and_national_number():
    assert interakt.split_phone("+919876543210") == ("+91", "9876543210")
    assert interakt.split_phone("9876543210") == ("+91", "9876543210")
    assert interakt.split_phone("+12025550123") == ("+1", "2025550123")
    assert interakt.split_phone("") == ("", "")


DOC_TPL = {
    "name": "pramukh_brochure", "language": "en", "status": "APPROVED",
    "parameter_format": "POSITIONAL",
    "components": [
        {"type": "HEADER", "format": "DOCUMENT"},
        {"type": "BODY", "text": "Hi {{1}}, here is the {{2}} brochure from {{3}}."},
        {"type": "FOOTER", "text": "Reply STOP to opt out"},
        {"type": "BUTTONS", "buttons": [
            {"type": "URL", "text": "Open", "url": "https://agent.lescall.com/r/{{1}}"}]},
    ],
}


def test_payload_fills_values_in_order_of_appearance_with_the_file_as_a_url():
    out = interakt.build_template(
        DOC_TPL, {"body.1": "Mr. Rakesh Shah", "body.2": "Pramukh Vedanta", "body.3": "Ravi",
                  "button.0": "aB3dE5fG7h"},
        media_url="https://agent.lescall.com/r/f/tok123", file_name="Vedanta.pdf")
    assert out == {
        "name": "pramukh_brochure", "languageCode": "en",
        "headerValues": ["https://agent.lescall.com/r/f/tok123"], "fileName": "Vedanta.pdf",
        "bodyValues": ["Mr. Rakesh Shah", "Pramukh Vedanta", "Ravi"],
        "buttonValues": {"0": ["aB3dE5fG7h"]},
    }


def test_payload_refuses_a_media_template_without_a_file():
    with pytest.raises(ValueError):
        interakt.build_template(DOC_TPL, {"body.1": "x", "body.2": "y", "body.3": "z"})


def test_named_variables_are_still_sent_positionally_by_appearance():
    tpl = {"name": "t", "language": "hi", "components": [
        {"type": "BODY", "text": "{{project_name}} के लिए धन्यवाद {{greeting}}"}]}
    out = interakt.build_template(tpl, {"body.greeting": "श्री शाह", "body.project_name": "Vedanta"})
    assert out["bodyValues"] == ["Vedanta", "श्री शाह"] and "headerValues" not in out


def test_webhook_signature_is_hmac_sha256_of_the_raw_body():
    body = b'{"type":"message_api_sent"}'
    sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert interakt.signature_ok(body, "sha256=" + sig, "s3cret")
    assert interakt.signature_ok(body, sig.upper(), "s3cret")
    assert not interakt.signature_ok(body, "sha256=" + sig, "other")
    assert not interakt.signature_ok(body, None, "s3cret")
    assert not interakt.signature_ok(body, "sha256=" + sig, "")


def test_webhook_events_map_onto_send_statuses_and_inbound_messages():
    failed = interakt.parse_event({"type": "message_api_failed", "data": {"message": {
        "id": "m1", "channel_error_code": "1013",
        "channel_failure_reason": "Recipient is not a valid WhatsApp user"}}})
    assert failed == {"kind": "status", "message_id": "m1", "status": "failed",
                      "error_code": "1013", "error": "Recipient is not a valid WhatsApp user"}
    read = interakt.parse_event({"type": "message_api_read", "data": {"message": {"id": "m2"}}})
    assert read["status"] == "read" and read["error_code"] is None
    inbound = interakt.parse_event({"type": "message_received", "data": {
        "customer": {"channel_phone_number": "919876543210"},
        "message": {"id": "in1", "message_content_type": "Text", "message": "STOP"}}})
    assert inbound == {"kind": "inbound", "message_id": "in1", "phone": "+919876543210",
                       "body": "STOP", "content_type": "text"}
    assert interakt.parse_event({"type": "something_else", "data": {}}) is None


def test_a_registered_template_behaves_like_a_synced_one():
    comps, fmt = templates.manual_components(
        header="document", body="Hi {{1}}, the {{2}} brochure.", footer="Reply STOP",
        button_text="Call us", button_url="https://pramukh.com/contact")
    assert fmt == "POSITIONAL"
    tpl = {"components": comps, "parameter_format": fmt}
    assert [s["key"] for s in templates.slots(tpl)] == ["header.media", "body.1", "body.2"]
    assert templates.media_format(tpl) == "document"
    pv = templates.preview(tpl, {"body.1": "Rakesh", "body.2": "Vedanta"})
    assert pv["body"] == "Hi Rakesh, the Vedanta brochure." and pv["header_media"] == "document"
    assert pv["buttons"] == [{"type": "URL", "text": "Call us", "url": "https://pramukh.com/contact"}]

    comps, fmt = templates.manual_components(body="Hi {greeting}, about {project_name}.")
    assert fmt == "NAMED"
    assert comps[0]["text"] == "Hi {{greeting}}, about {{project_name}}."


def test_registered_template_validation():
    assert templates.validate_registered_template("Pramukh Brochure", "x")
    assert templates.validate_registered_template("pramukh_brochure", "")
    assert templates.validate_registered_template("pramukh_brochure", "Hi {{1}}") == []


# ===========================================================================
# database paths
# ===========================================================================

@pytest.fixture()
def interakt_connected(wa_db, monkeypatch, tmp_path):
    from cryptography.fernet import Fernet
    from shared.whatsapp import crypto, store

    monkeypatch.setenv("WA_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("WA_SEND_ENABLED", "true")
    monkeypatch.delenv("WA_CANARY_PHONES", raising=False)
    store.save_interakt_connection(OWNER, token_enc=crypto.encrypt(KEY),
                                   display_phone="+91 90000 00000",
                                   webhook_secret_enc=crypto.encrypt("whsec"))
    store.update_settings(OWNER, default_approval="auto", send_window_start=0,
                          send_window_end=24, send_delay_min=0, daily_cap=100,
                          approval_ttl_h=48)
    sent = []

    async def fake_send(api_key, *, to, template, callback_data=""):
        sent.append({"key": api_key, "to": to, "template": template, "callback": callback_data})
        return f"ik.{len(sent)}"

    monkeypatch.setattr(interakt, "send_template", fake_send)
    return sent


def _register(owner=OWNER, name="pramukh_brochure", language="en", header="document"):
    import whatsapp_api

    body = whatsapp_api.RegisterTemplateBody(
        name=name, language=language, category="UTILITY", header=header,
        body="Hi {{1}}, here is the {{2}} brochure from {{3}}.", footer="Reply STOP to opt out")
    return whatsapp_api.register_template(body, owner=owner)["template"]


def _media(tmp_path, name="Vedanta.pdf"):
    from shared.whatsapp import store

    f = tmp_path / name
    f.write_bytes(b"%PDF-1.4 test")
    return store.insert_media(OWNER, filename=name, mime="application/pdf",
                              size_bytes=f.stat().st_size, path=str(f))


def _brochure_playbook(**kw):
    from shared.whatsapp import store

    pb = {"name": "Pramukh brochure", "enabled": True, "agent_slugs": ["ravi-inbound"],
          "require_booking": False, "recipient": "dialled", "template_name": "pramukh_brochure",
          "variables": {"body.1": {"value": "{greeting}", "fallback": "there"},
                        "body.2": {"value": "{project_name}", "fallback": "Pramukh"},
                        "body.3": {"value": "{agent_name}", "fallback": "Pramukh Group"}},
          "media_id": None, "media_map": {}, "link_url": None, "approval_mode": "auto"}
    pb.update(kw)
    return store.save_playbook(OWNER, pb)


def _consent_call(project="Pramukh Vedanta", **kw):
    return _insert_call(cd={"flags": {"whatsapp_promised": True,
                                      "whatsapp_share_projects": [project]},
                            "state": {}}, **kw)


def _due():
    from shared.db.database import get_db

    with get_db() as db:
        db.execute("UPDATE wa_sends SET not_before=now() - interval '1 minute'")


def test_connect_stores_the_key_encrypted_and_mints_a_webhook_url(interakt_connected):
    from shared.whatsapp import crypto, store

    acct = store.get_account(OWNER)
    assert acct["provider"] == "interakt" and acct["status"] == "connected"
    assert acct["has_token"] and acct["has_webhook_secret"] and acct["webhook_token"]
    assert crypto.decrypt(store.account_token_blob(OWNER)) == KEY
    assert store.account_token_blob(OWNER) != KEY
    assert store.account_by_webhook_token(acct["webhook_token"])["owner"] == OWNER
    # reconnecting keeps the webhook URL the tenant already pasted into Interakt
    store.save_interakt_connection(OWNER, token_enc=crypto.encrypt("k2"), display_phone="",
                                   webhook_secret_enc=None)
    after = store.get_account(OWNER)
    assert after["webhook_token"] == acct["webhook_token"] and after["has_webhook_secret"]


def test_a_send_carries_the_project_and_the_brochure_of_that_project(interakt_connected, tmp_path):
    _register()
    vedanta = _media(tmp_path, "Vedanta.pdf")
    aristo = _media(tmp_path, "Aristo.pdf")
    _brochure_playbook(media_map={"Pramukh Vedanta": vedanta["id"], "Pramukh Aristo": aristo["id"]})
    _run_stage(_consent_call("Pramukh Vedanta"))
    s = _sends()[0]
    assert s["status"] == "queued" and s["project"] == "Pramukh Vedanta"
    assert s["media_id"] == vedanta["id"] and s["media_filename"] == "Vedanta.pdf"
    assert s["params"] == {"body.1": "Mr. Rakesh Shah", "body.2": "Pramukh Vedanta",
                           "body.3": "Pramukh Group"}

    assert asyncio.run(sender.run_once()) == 1
    sent = interakt_connected[0]
    assert sent["key"] == KEY and sent["to"] == "+919876543210"
    assert sent["callback"] == f"wa_send:{s['id']}"
    tpl = sent["template"]
    assert tpl["name"] == "pramukh_brochure" and tpl["languageCode"] == "en"
    assert tpl["headerValues"][0].startswith("https://agent.lescall.com/r/f/")
    assert tpl["fileName"] == "Vedanta.pdf"
    assert tpl["bodyValues"] == ["Mr. Rakesh Shah", "Pramukh Vedanta", "Pramukh Group"]
    after = _sends()[0]
    assert after["status"] == "sent" and after["wamid"] == "ik.1"


def test_project_match_is_forgiving_and_falls_back_to_the_default_file(tmp_path, wa_db):
    from shared.whatsapp.stage import resolve_media

    pb = {"media_id": 7, "media_map": {"Vedanta": 1, "pramukh silver stone": 2}}
    assert resolve_media(pb, "Pramukh Vedanta") == 1
    assert resolve_media(pb, "Silver Stone") == 2
    assert resolve_media(pb, "Pramukh Swarit") == 7
    assert resolve_media({"media_id": None, "media_map": {}}, "Pramukh Swarit") is None


def test_a_send_waits_for_the_brochure_and_leaves_once_it_is_attached(interakt_connected, tmp_path):
    from shared.whatsapp import store

    _register()
    pb = _brochure_playbook()  # no file yet
    _run_stage(_consent_call("Pramukh Vedanta"))
    s = _sends()[0]
    assert s["status"] == "queued" and s["media_id"] is None

    assert asyncio.run(sender.run_once()) == 0
    s = _sends()[0]
    assert s["status"] == "queued" and s["attempts"] == 0
    assert "attachment for Pramukh Vedanta" in s["reason"]
    assert interakt_connected == []

    m = _media(tmp_path)
    store.save_playbook(OWNER, {**pb, "media_map": {"Pramukh Vedanta": m["id"]}}, pb["id"])
    _due()
    assert asyncio.run(sender.run_once()) == 1
    s = _sends()[0]
    assert s["status"] == "sent" and s["media_id"] == m["id"]


def test_a_rejected_key_fails_the_send_and_flags_the_account(interakt_connected, tmp_path, monkeypatch):
    from shared.whatsapp import store

    _register()
    _brochure_playbook(media_id=_media(tmp_path)["id"])
    _run_stage(_consent_call())

    async def unauthorized(*a, **k):
        raise GraphError("Unauthorized Access (HTTP 401)", code=401, http_status=401,
                         transient=False)

    monkeypatch.setattr(interakt, "send_template", unauthorized)
    asyncio.run(sender.run_once())
    s = _sends()[0]
    assert s["status"] == "failed" and s["error_code"] == "401" and "Interakt" in s["reason"]
    acct = store.get_account(OWNER)
    assert acct["status"] == "error" and "Interakt" in acct["last_error"]


def test_rate_limit_retries(interakt_connected, tmp_path, monkeypatch):
    _register()
    _brochure_playbook(media_id=_media(tmp_path)["id"])
    _run_stage(_consent_call())

    async def throttled(*a, **k):
        raise GraphError("Rate limit exceeded for this resource. (HTTP 429)", code=429,
                         http_status=429, transient=True)

    monkeypatch.setattr(interakt, "send_template", throttled)
    asyncio.run(sender.run_once())
    s = _sends()[0]
    assert s["status"] == "queued" and s["attempts"] == 1 and "retrying" in s["reason"]


def test_meta_sync_never_deletes_a_registered_template(interakt_connected):
    from shared.whatsapp import store

    _register()
    store.replace_templates(OWNER, [])
    assert [t["name"] for t in store.list_templates(OWNER)] == ["pramukh_brochure"]


def test_registered_templates_are_owner_scoped_and_renames_follow_playbooks(interakt_connected):
    import whatsapp_api
    from fastapi import HTTPException
    from shared.whatsapp import store

    t = _register()
    _brochure_playbook()
    body = whatsapp_api.RegisterTemplateBody(name="vedanta_brochure", language="en",
                                             header="document", body="Hi {{1}}, {{2}} by {{3}}.")
    with pytest.raises(HTTPException) as exc:
        whatsapp_api.update_template(t["id"], body, owner=OTHER)
    assert exc.value.status_code == 404
    out = whatsapp_api.update_template(t["id"], body, owner=OWNER)
    assert out["template"]["name"] == "vedanta_brochure"
    assert store.list_playbooks(OWNER)[0]["template_name"] == "vedanta_brochure"
    with pytest.raises(HTTPException) as exc:
        whatsapp_api.delete_template(t["id"], owner=OWNER)  # a playbook still uses it
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        whatsapp_api.register_template(whatsapp_api.RegisterTemplateBody(
            name="x", language="en", body="hi {{1}} there"), owner=OTHER)  # not on Interakt
    assert exc.value.status_code == 409


def test_meta_only_routes_refuse_an_interakt_account(interakt_connected):
    import whatsapp_api
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(whatsapp_api.sync_templates(owner=OWNER))
    assert exc.value.status_code == 409


def test_webhook_and_media_routes_over_http(interakt_connected, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from shared.whatsapp import store
    import whatsapp_api

    app = FastAPI()
    app.include_router(whatsapp_api.router)
    client = TestClient(app)

    _register()
    m = _media(tmp_path)
    _brochure_playbook(media_id=m["id"])
    _run_stage(_consent_call())
    asyncio.run(sender.run_once())
    url = interakt_connected[0]["template"]["headerValues"][0]
    path = url.replace("https://agent.lescall.com", "")
    r = client.get(path)
    assert r.status_code == 200 and r.content == b"%PDF-1.4 test"
    assert r.headers["content-type"].startswith("application/pdf")
    assert client.get("/r/f/not-a-real-token-xx").status_code == 404

    token = store.get_account(OWNER)["webhook_token"]
    hook = f"/api/whatsapp/webhook/interakt/{token}"
    body = json.dumps({"type": "message_api_read", "data": {"message": {"id": "ik.1"}}}).encode()
    good = "sha256=" + hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    assert client.post(hook, content=body).status_code == 401
    assert client.post(hook, content=body, headers={"Interakt-Signature": "sha256=00"}).status_code == 401
    assert client.post("/api/whatsapp/webhook/interakt/unknown-token-0000000",
                       content=body, headers={"Interakt-Signature": good}).status_code == 404
    r = client.post(hook, content=body, headers={"Interakt-Signature": good})
    assert r.status_code == 200 and r.json()["statuses"] == 1
    assert _sends()[0]["status"] == "read"
    assert client.post(hook, content=body, headers={"Interakt-Signature": good}).json().get("duplicate") == 1

    stop = json.dumps({"type": "message_received", "data": {
        "customer": {"channel_phone_number": "919876543210"},
        "message": {"id": "in1", "message_content_type": "Text", "message": "STOP"}}}).encode()
    r = client.post(hook, content=stop,
                    headers={"Interakt-Signature": "sha256=" + hmac.new(b"whsec", stop, hashlib.sha256).hexdigest()})
    assert r.status_code == 200 and r.json()["stops"] == 1
    assert store.latest_consent(OWNER, "+919876543210")["state"] == "revoked"

    store.set_webhook_secret(OWNER, None)
    assert client.post(hook, content=body, headers={"Interakt-Signature": good}).status_code == 503


def test_preview_names_the_project_and_its_brochure(interakt_connected, tmp_path):
    import whatsapp_api

    _register()
    m = _media(tmp_path)
    _consent_call("Pramukh Vedanta")
    body = whatsapp_api.PreviewBody(
        name="x", agent_slugs=["ravi-inbound"], template_name="pramukh_brochure",
        media_map={"Pramukh Vedanta": m["id"]},
        variables={"body.1": {"value": "{greeting}"}, "body.2": {"value": "{project_name}"},
                   "body.3": {"value": "{agent_name}", "fallback": "Pramukh Group"}})
    out = whatsapp_api.preview_playbook(body, owner=OWNER)
    assert out["project"] == "Pramukh Vedanta" and out["attachment"] == "Vedanta.pdf"
    assert out["needs_attachment"] is False
    assert out["preview"]["body"] == "Hi Mr. Rakesh Shah, here is the Pramukh Vedanta brochure from Pramukh Group."


def test_projects_endpoint_groups_agents_by_primary_project(interakt_connected, monkeypatch):
    import whatsapp_api

    monkeypatch.setattr(whatsapp_api, "_agent_project",
                        lambda slug: "Pramukh Vedanta" if slug == "ravi-inbound" else "")
    out = whatsapp_api.agent_projects(agents="ravi-inbound,nisha-outbound,ghost", owner=OWNER)
    assert out == {"projects": [{"name": "Pramukh Vedanta", "agents": ["ravi-inbound"]}]}
