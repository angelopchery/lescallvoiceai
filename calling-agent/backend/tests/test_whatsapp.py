"""Integrations → WhatsApp (shared/whatsapp, dashboard/whatsapp_api.py).

Pure units first (placeholders, templates, consent, webhook helpers, send
window), then the database paths against lescall_test: the pipeline stage
that turns a finished call into a send, the sender's re-checks at the moment
of sending, the webhook, and the tenant API's owner scoping.

Meta is never contacted: graph.* is monkeypatched wherever a send happens.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401  (fixtures)

_REPO = Path(__file__).resolve().parents[3]
for p in (str(_REPO / "dashboard"), str(_REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

from shared.whatsapp import consent, placeholders, sender, templates, webhook  # noqa: E402

OWNER = "wa-owner@x.com"
OTHER = "wa-other@x.com"


# ===========================================================================
# placeholders
# ===========================================================================

def _ctx(**kw):
    base = dict(contact_name="Rakesh Shah", gender="male", language="en",
                agent_name="Ravi", company_name="Pramukh", project_name="Swarit")
    base.update(kw)
    return placeholders.Context(**base)


def test_greeting_uses_title_only_when_gender_is_known():
    assert placeholders.render("Hi {greeting},", _ctx()) == "Hi Mr. Rakesh Shah,"
    assert placeholders.render("Hi {greeting},", _ctx(gender="")) == "Hi Rakesh Shah,"
    assert placeholders.render("Hi {greeting},", _ctx(gender="other")) == "Hi Rakesh Shah,"


def test_greeting_without_a_name_is_an_honorific_or_nothing():
    assert placeholders.render("{greeting}", _ctx(contact_name="")) == "Sir"
    assert placeholders.render("{greeting}", _ctx(contact_name="", gender="")) == ""


def test_gender_words_follow_the_template_language():
    assert placeholders.render("{title} {contact_name}", _ctx(language="hi")) == "श्री Rakesh Shah"
    assert placeholders.render("{honorific}", _ctx(gender="female", language="gu_IN")) == "મેડમ"


def test_a_removed_word_leaves_no_seam_and_unknown_tokens_vanish():
    assert placeholders.render("Hello {honorific}, thanks!", _ctx(gender="")) == "Hello, thanks!"
    assert placeholders.render("Budget {budget} ok", _ctx()) == "Budget ok"


def test_slot_tokens_read_the_call_state():
    ctx = _ctx(slots={"company_name": "Acme Tools", "nested": {"x": 1}})
    assert placeholders.render("{slot:company_name}", ctx) == "Acme Tools"
    assert placeholders.render("[{slot:nested}]", ctx) == "[]"


def test_url_values_are_encoded():
    url = placeholders.render_url(
        "https://docs.google.com/forms/d/e/X/viewform?entry.1={contact_name}&entry.2={slot:co}",
        _ctx(contact_name="Rakesh & Sons", slots={"co": "A/B"}))
    assert url.endswith("entry.1=Rakesh%20%26%20Sons&entry.2=A%2FB")


def test_meta_safe_strips_what_meta_rejects():
    assert placeholders.meta_safe("a\nb\tc     d") == "a b c d"


def test_plausible_name_rejects_answers_and_numbers():
    assert placeholders.plausible_name("haan") == ""
    assert placeholders.plausible_name("98765") == ""
    assert placeholders.plausible_name(" Priya  Mehta ") == "Priya Mehta"


def test_gender_tables_match_the_voice_engine():
    """The stage cannot import the engine; the copy must not drift."""
    backend = _REPO / "calling-agent" / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    from app.voice import address

    assert placeholders.HONORIFIC_WORDS == address.HONORIFIC_WORDS
    assert placeholders.TITLE_WORDS == address.TITLE_WORDS


# ===========================================================================
# templates
# ===========================================================================

NAMED_TPL = {
    "name": "brochure", "language": "en", "parameter_format": "NAMED", "status": "APPROVED",
    "components": [
        {"type": "HEADER", "format": "DOCUMENT"},
        {"type": "BODY", "text": "Hi {{greeting}}, here is the {{project_name}} brochure.",
         "example": {"body_text_named_params": [
             {"param_name": "greeting", "example": "Rakesh"},
             {"param_name": "project_name", "example": "Swarit"}]}},
        {"type": "FOOTER", "text": "Reply STOP to opt out"},
        {"type": "BUTTONS", "buttons": [
            {"type": "URL", "text": "Open", "url": "https://agent.lescall.com/r/{{1}}"},
            {"type": "QUICK_REPLY", "text": "Call me"}]},
    ],
}
POSITIONAL_TPL = {
    "name": "form", "language": "hi", "parameter_format": "POSITIONAL", "status": "APPROVED",
    "components": [{"type": "BODY", "text": "नमस्ते {{2}}, {{1}} फ़ॉर्म"}],
}


def test_slots_cover_header_body_and_dynamic_buttons_only():
    keys = [s["key"] for s in templates.slots(NAMED_TPL)]
    assert keys == ["header.media", "body.greeting", "body.project_name", "button.0"]
    assert templates.media_format(NAMED_TPL) == "document"
    assert templates.link_button_is_ours(NAMED_TPL, "https://agent.lescall.com") == {"button.0": True}


def test_named_components_payload():
    comps = templates.build_components(
        NAMED_TPL, {"body.greeting": "Rakesh", "body.project_name": "Swarit",
                    "button.0": "tok123"},
        {"id": "MID", "filename": "Swarit.pdf"})
    assert comps[0] == {"type": "header", "parameters": [
        {"type": "document", "document": {"id": "MID", "filename": "Swarit.pdf"}}]}
    assert comps[1]["parameters"] == [
        {"type": "text", "text": "Rakesh", "parameter_name": "greeting"},
        {"type": "text", "text": "Swarit", "parameter_name": "project_name"}]
    assert comps[2] == {"type": "button", "sub_type": "url", "index": "0",
                        "parameters": [{"type": "text", "text": "tok123"}]}


def test_positional_parameters_are_sent_in_number_order():
    comps = templates.build_components(POSITIONAL_TPL, {"body.1": "A", "body.2": "B"})
    assert [p["text"] for p in comps[0]["parameters"]] == ["A", "B"]
    assert "parameter_name" not in comps[0]["parameters"][0]


def test_a_media_template_without_media_is_refused():
    with pytest.raises(ValueError):
        templates.build_components(NAMED_TPL, {})


def test_preview_fills_known_values_and_shows_missing_ones():
    pv = templates.preview(NAMED_TPL, {"body.greeting": "Rakesh", "button.0": "t"})
    assert pv["body"] == "Hi Rakesh, here is the {{project_name}} brochure."
    assert pv["header_media"] == "document"
    assert pv["buttons"][0]["url"] == "https://agent.lescall.com/r/t"


def test_authoring_converts_placeholders_to_named_params():
    body, names = templates.author_body_to_meta("Hi {greeting}, the {project_name} PDF is here.")
    assert body == "Hi {{greeting}}, the {{project_name}} PDF is here."
    assert names == ["greeting", "project_name"]
    payload = templates.create_payload(
        name="x", language="en", category="utility", body="Hi {greeting}, here.",
        examples={}, button_text="Open form", button_url_base="https://agent.lescall.com/r")
    assert payload["category"] == "UTILITY" and payload["parameter_format"] == "NAMED"
    assert payload["components"][-1]["buttons"][0]["url"] == "https://agent.lescall.com/r/{{1}}"


@pytest.mark.parametrize("body,ok", [
    ("Hi {greeting}, here is your brochure.", True),
    ("{greeting}, here is your brochure.", False),       # starts with a variable
    ("Here is your brochure {project_name}", False),     # ends with a variable
    ("Hi {a}{b} there", False),                          # adjacent variables
])
def test_new_template_rules(body, ok):
    assert (templates.validate_new_template("brochure_v1", body) == []) is ok


# ===========================================================================
# consent
# ===========================================================================

def _call(state=None, flags=None, transcript="", audit=None):
    cd = {"state": state or {}, "audit_log": audit or []}
    if flags is not None:
        cd["flags"] = flags
    return {"collected_data": json.dumps(cd), "transcript": transcript}


def test_promise_flag_is_consent():
    c = consent.detect(_call(flags={"whatsapp_promised": True}))
    assert c.granted and c.source == "call_promise"


def test_whatsapp_slot_yes_is_consent_with_the_callers_words():
    c = consent.detect(_call(
        state={"whatsapp_number": "हाँ"},
        audit=[{"field": "whatsapp_number", "utterance": "हाँ जी, यही नंबर है"}]))
    assert c.granted and c.source == "call_slot" and c.quote == "हाँ जी, यही नंबर है"


def test_a_number_given_for_whatsapp_becomes_the_destination():
    c = consent.detect(_call(state={"whatsapp_number": "98765 43210"}))
    assert c.granted and c.given_phone == "+919876543210"


def test_no_on_the_slot_beats_an_agent_promise():
    c = consent.detect(_call(state={"whatsapp_number": "nahi"},
                             flags={"whatsapp_promised": True}))
    assert c.granted is False


def test_dnd_request_is_a_refusal():
    c = consent.detect(_call(flags={"dnd_requested": True, "whatsapp_promised": True}))
    assert c.granted is False


def test_commitment_counts_only_when_its_quote_is_in_the_transcript():
    t = "Agent: मैं आपके इसी नंबर पर Swarit की location WhatsApp कर देता हूँ\nCaller: ठीक है"
    real = [{"kind": "whatsapp", "quote": "मैं आपके इसी नंबर पर Swarit की location WhatsApp कर देता हूँ"}]
    fake = [{"kind": "whatsapp", "quote": "I will definitely send the brochure on WhatsApp"}]
    assert consent.detect(_call(transcript=t), real).source == "call_commitment"
    assert consent.detect(_call(transcript=t), fake) is None


def test_whatsapp_never_mentioned_is_none():
    assert consent.detect(_call(state={"budget": "50 lakh"})) is None


# ===========================================================================
# webhook helpers + send window
# ===========================================================================

def test_signature_check(monkeypatch):
    monkeypatch.setenv("WA_APP_SECRET", "s3cret")
    body = b'{"entry":[]}'
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert webhook.signature_ok(body, good)
    assert not webhook.signature_ok(body + b" ", good)
    assert not webhook.signature_ok(body, None)
    monkeypatch.setenv("WA_APP_SECRET", "")
    assert not webhook.signature_ok(body, good), "no secret must mean no webhook"


def test_verify_handshake(monkeypatch):
    monkeypatch.setenv("WA_VERIFY_TOKEN", "vt")
    assert webhook.verify_challenge("subscribe", "vt", "123") == "123"
    assert webhook.verify_challenge("subscribe", "nope", "123") is None
    monkeypatch.setenv("WA_VERIFY_TOKEN", "")
    assert webhook.verify_challenge("subscribe", "", "123") is None


@pytest.mark.parametrize("text,stop", [
    ("STOP", True), ("  stop. ", True), ("बंद करो", True), ("Unsubscribe", True),
    ("please don't stop sending", False), ("cancel my visit", False), ("ok", False),
])
def test_stop_is_a_whole_message(text, stop):
    assert webhook.is_stop(text) is stop


def test_send_window():
    ist = sender.IST
    inside = dt.datetime(2026, 9, 29, 12, 0, tzinfo=ist)
    early = dt.datetime(2026, 9, 29, 7, 30, tzinfo=ist)
    late = dt.datetime(2026, 9, 29, 21, 5, tzinfo=ist)
    assert sender.next_window_start(inside, 9, 20) is None
    assert sender.next_window_start(early, 9, 20) == dt.datetime(2026, 9, 29, 9, 0, tzinfo=ist)
    assert sender.next_window_start(late, 9, 20) == dt.datetime(2026, 9, 30, 9, 0, tzinfo=ist)


# ===========================================================================
# database paths
# ===========================================================================

@pytest.fixture()
def wa_db(pg_test_db):
    from shared.db.database import get_db

    with get_db() as db:
        for t in ("wa_webhook_events", "wa_inbound", "wa_links", "wa_sends", "wa_consents",
                  "wa_playbooks", "wa_media", "wa_templates", "wa_accounts"):
            db.execute(f"DELETE FROM {t}")
        db.execute("DELETE FROM notifications WHERE owner = ANY(?)", ([OWNER, OTHER],))
        db.execute("DELETE FROM calls WHERE owner = ANY(?)", ([OWNER, OTHER],))
        db.execute("DELETE FROM contacts WHERE owner = ANY(?)", ([OWNER, OTHER],))
        db.execute("DELETE FROM agents WHERE slug IN ('ravi-inbound', 'nisha-outbound')")
        db.execute("INSERT INTO agents (slug, owner) VALUES ('ravi-inbound', ?)", (OWNER,))
        db.execute("INSERT INTO agents (slug, owner) VALUES ('nisha-outbound', ?)", (OTHER,))
        db.execute(
            """INSERT INTO contacts (owner, phone, sig_digits, name, gender)
               VALUES (?, '+919876543210', '9876543210', 'Rakesh Shah', 'male')""", (OWNER,))
    yield


def _insert_call(*, owner=OWNER, slug="ravi-inbound", phone="+919876543210", cd=None,
                 age_min=5, language="hi-IN", transcript="") -> int:
    from shared.db.database import get_db

    with get_db() as db:
        row = db.execute(
            """INSERT INTO calls (conn_id, call_key, owner, agent_slug, phone, sig_digits,
                      language, collected_data, transcript, duration_s, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 60,
                       now() - make_interval(mins => ?::int)) RETURNING id""",
            (f"conn-{age_min}-{phone}-{slug}-{id(cd)}", f"key-{age_min}-{id(cd)}-{slug}",
             owner, slug, phone, phone[-10:], language,
             json.dumps(cd or {"state": {"whatsapp_number": "हाँ"}}, ensure_ascii=False),
             transcript, age_min)).fetchone()
    return row["id"]


def _playbook(approval="manual", **kw):
    from shared.whatsapp import store

    pb = {"name": "Brochure", "enabled": True, "agent_slugs": ["ravi-inbound"],
          "require_booking": False, "recipient": "dialled", "template_name": "brochure",
          "variables": {"body.greeting": {"value": "{greeting}", "fallback": "there"},
                        "body.project_name": {"value": "{project_name}", "fallback": ""}},
          "media_id": None, "link_url": None, "approval_mode": approval}
    pb.update(kw)
    return store.save_playbook(OWNER, pb)


def _approved_template(owner=OWNER, language="hi"):
    from shared.whatsapp import store

    tpl = dict(NAMED_TPL, language=language, id="T1")
    tpl["components"] = [c for c in NAMED_TPL["components"] if c["type"] == "BODY"]
    store.upsert_template(owner, tpl)


def _run_stage(call_id: int) -> None:
    from shared.intelligence import pipeline
    from shared.whatsapp.stage import stage_whatsapp

    asyncio.run(stage_whatsapp(pipeline._load_call(call_id)))


def _sends(owner=OWNER):
    from shared.whatsapp import store

    return store.list_sends(owner, statuses=None, playbook_id=None, limit=50, offset=0)[0]


def test_consent_on_a_fresh_call_creates_one_send_awaiting_approval(wa_db):
    from shared.db.database import get_db

    _approved_template()
    _playbook()
    call_id = _insert_call(cd={"state": {"whatsapp_number": "हाँ", "project": "Swarit"}})
    _run_stage(call_id)
    _run_stage(call_id)  # the pipeline re-runs stages; still one send
    sends = _sends()
    assert len(sends) == 1
    s = sends[0]
    assert s["status"] == "pending_approval"
    assert s["to_phone"] == "+919876543210" and s["language"] == "hi"
    assert s["params"] == {"body.greeting": "श्री Rakesh Shah", "body.project_name": "Swarit"}
    with get_db() as db:
        n = db.execute("SELECT count(*) AS n FROM notifications WHERE owner=? AND kind=?",
                       (OWNER, "whatsapp.approval")).fetchone()["n"]
        c = db.execute("SELECT state, source FROM wa_consents WHERE call_id=?",
                       (call_id,)).fetchone()
    assert n == 1
    assert dict(c) == {"state": "granted", "source": "call_slot"}


def test_auto_approval_queues_directly(wa_db):
    _approved_template()
    _playbook("auto")
    _run_stage(_insert_call(cd={"state": {"whatsapp_number": "yes", "project": "Swarit"}}))
    assert _sends()[0]["status"] == "queued"


def test_an_empty_variable_holds_the_send_even_under_auto(wa_db):
    _approved_template()
    _playbook("auto")  # project_name has no fallback and the call names no project
    _run_stage(_insert_call())
    s = _sends()[0]
    assert s["status"] == "pending_approval"
    assert "body.project_name" in (s["reason"] or "")


def test_an_old_call_never_sends(wa_db):
    _approved_template()
    _playbook("auto")
    _run_stage(_insert_call(age_min=180))
    assert _sends() == []


def test_a_playbook_only_covers_its_own_agents(wa_db):
    _approved_template()
    _playbook("auto", agent_slugs=["some-other-agent"])
    _run_stage(_insert_call())
    assert _sends() == []


def test_a_later_no_withdraws_a_waiting_yes(wa_db):
    from shared.whatsapp import store

    _approved_template()
    _playbook("manual")
    _run_stage(_insert_call(cd={"state": {"whatsapp_number": "haan", "project": "Swarit"}}))
    assert _sends()[0]["status"] == "pending_approval"
    _run_stage(_insert_call(cd={"state": {"whatsapp_number": "nahi"}}, age_min=1))
    assert _sends()[0]["status"] == "blocked"
    assert store.latest_consent(OWNER, "+919876543210")["state"] == "revoked"


def test_a_tracked_link_is_minted_per_send(wa_db):
    from shared.db.database import get_db

    _approved_template()
    _playbook("auto", link_url="https://docs.google.com/forms/x?entry.1={contact_name}",
              variables={"body.greeting": {"value": "{greeting}"},
                         "body.project_name": {"value": "{link}"}})
    _run_stage(_insert_call())
    s = _sends()[0]
    assert s["params"]["body.project_name"].startswith("https://agent.lescall.com/r/")
    with get_db() as db:
        link = db.execute("SELECT url FROM wa_links WHERE send_id=?", (s["id"],)).fetchone()
    assert link["url"] == "https://docs.google.com/forms/x?entry.1=Rakesh%20Shah"


# --------------------------------------------------------------------------- sender

@pytest.fixture()
def connected(wa_db, monkeypatch):
    from cryptography.fernet import Fernet
    from shared.whatsapp import crypto, store

    monkeypatch.setenv("WA_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("WA_SEND_ENABLED", "true")
    monkeypatch.delenv("WA_CANARY_PHONES", raising=False)
    store.save_connection(OWNER, waba_id="111", phone_number_id="222",
                          display_phone="+91 90000 00000", verified_name="Pramukh",
                          token_enc=crypto.encrypt("tok"), quality_rating="GREEN",
                          messaging_limit=None)
    store.update_settings(OWNER, default_approval="manual", send_window_start=0,
                          send_window_end=24, send_delay_min=0, daily_cap=100,
                          approval_ttl_h=48)
    sent = []

    async def fake_send(pnid, token, *, to, name, language, components):
        sent.append({"to": to, "name": name, "language": language, "components": components})
        return f"wamid.{len(sent)}"

    from shared.whatsapp import graph

    monkeypatch.setattr(graph, "send_template", fake_send)
    return sent


def _queued_send():
    _approved_template()
    _playbook("auto")
    _run_stage(_insert_call(cd={"state": {"whatsapp_number": "yes", "project": "Swarit"}}))
    return _sends()[0]


def test_sender_delivers_a_queued_send(connected):
    s = _queued_send()
    assert asyncio.run(sender.run_once()) == 1
    assert connected[0]["to"] == "+919876543210" and connected[0]["name"] == "brochure"
    after = _sends()[0]
    assert after["status"] == "sent" and after["wamid"] == "wamid.1"
    assert asyncio.run(sender.run_once()) == 0, "a sent message is never sent again"
    assert s["id"] == after["id"]


def test_kill_switch_claims_nothing(connected, monkeypatch):
    _queued_send()
    monkeypatch.setenv("WA_SEND_ENABLED", "false")
    assert asyncio.run(sender.run_once()) == 0
    assert _sends()[0]["status"] == "queued" and connected == []


def test_canary_blocks_everyone_else(connected, monkeypatch):
    _queued_send()
    monkeypatch.setenv("WA_CANARY_PHONES", "+91 90000 11111")
    asyncio.run(sender.run_once())
    assert _sends()[0]["status"] == "blocked" and connected == []


def test_an_opt_out_after_the_call_blocks_the_send(connected):
    from shared.whatsapp import store

    _queued_send()
    store.record_consent(OWNER, phone="+919876543210", contact_id=None, state="revoked",
                         source="reply_stop", call_id=None, quote="STOP")
    asyncio.run(sender.run_once())
    assert _sends()[0]["status"] == "blocked" and connected == []


def test_outside_sending_hours_waits_without_spending_an_attempt(connected):
    from shared.whatsapp import store

    _queued_send()
    now_h = dt.datetime.now(sender.IST).hour
    start = (now_h + 2) % 24
    store.update_settings(OWNER, default_approval="manual", send_window_start=min(start, 22),
                          send_window_end=min(start, 22) + 1, send_delay_min=0,
                          daily_cap=100, approval_ttl_h=48)
    asyncio.run(sender.run_once())
    s = _sends()[0]
    assert s["status"] == "queued" and s["attempts"] == 0 and "sending hours" in s["reason"]


def test_transient_meta_error_retries_permanent_one_fails(connected, monkeypatch):
    from shared.whatsapp import graph

    _queued_send()

    async def flaky(*a, **k):
        raise graph.GraphError("throttled", code=130429, transient=True)

    monkeypatch.setattr(graph, "send_template", flaky)
    asyncio.run(sender.run_once())
    s = _sends()[0]
    assert s["status"] == "queued" and s["attempts"] == 1

    async def dead(*a, **k):
        raise graph.GraphError("not a WhatsApp user", code=131026, transient=False)

    from shared.db.database import get_db

    with get_db() as db:
        db.execute("UPDATE wa_sends SET not_before=now() - interval '1 minute'")
    monkeypatch.setattr(graph, "send_template", dead)
    asyncio.run(sender.run_once())
    s = _sends()[0]
    assert s["status"] == "failed" and s["error_code"] == "131026"


def test_unapproved_send_expires(wa_db):
    from shared.db.database import get_db
    from shared.whatsapp import store

    _approved_template()
    _playbook("manual")
    _run_stage(_insert_call(cd={"state": {"whatsapp_number": "yes", "project": "Swarit"}}))
    with get_db() as db:
        db.execute("UPDATE wa_sends SET expires_at = now() - interval '1 minute'")
    assert store.expire_due() == 1
    s = _sends()[0]
    assert s["status"] == "expired"
    assert not store.approve_send(OWNER, s["id"], OWNER, 48)


# --------------------------------------------------------------------------- webhook

def _wh(value: dict, field="messages") -> dict:
    return {"entry": [{"changes": [{"field": field, "value": value}]}]}


def test_status_receipts_only_move_forward(connected):
    from shared.whatsapp import store

    _queued_send()
    asyncio.run(sender.run_once())
    meta = {"metadata": {"phone_number_id": "222"}}
    webhook.process(_wh({**meta, "statuses": [
        {"id": "wamid.1", "status": "read", "pricing": {"category": "utility"}}]}))
    webhook.process(_wh({**meta, "statuses": [{"id": "wamid.1", "status": "delivered"}]}))
    s = store.get_send(OWNER, _sends()[0]["id"])
    assert s["status"] == "read" and s["pricing_category"] == "utility"


def test_stop_reply_revokes_and_cancels(wa_db, connected):
    from shared.whatsapp import store

    _approved_template()
    _playbook("manual")
    _run_stage(_insert_call(cd={"state": {"whatsapp_number": "yes", "project": "Swarit"}}))
    stats = webhook.process(_wh({"metadata": {"phone_number_id": "222"}, "messages": [
        {"from": "919876543210", "id": "wamid.in1", "type": "text", "text": {"body": "STOP"}}]}))
    assert stats["stops"] == 1
    assert store.latest_consent(OWNER, "+919876543210")["state"] == "revoked"
    assert _sends()[0]["status"] == "blocked"


def test_a_reply_for_an_unknown_number_id_is_ignored(wa_db):
    stats = webhook.process(_wh({"metadata": {"phone_number_id": "999"}, "messages": [
        {"from": "919876543210", "id": "wamid.x", "type": "text", "text": {"body": "hi"}}]}))
    assert stats["messages"] == 0


def test_template_review_updates_status(wa_db):
    from shared.whatsapp import store

    _approved_template()
    webhook.process(_wh({"event": "REJECTED", "message_template_id": "T1",
                         "reason": "INCORRECT_CATEGORY"},
                        field="message_template_status_update"))
    t = store.templates_named(OWNER, "brochure")[0]
    assert t["status"] == "REJECTED" and t["rejected_reason"] == "INCORRECT_CATEGORY"


# --------------------------------------------------------------------------- tenant API

def test_api_is_owner_scoped(wa_db):
    import whatsapp_api
    from fastapi import HTTPException

    _approved_template()
    _playbook("manual")
    _run_stage(_insert_call(cd={"state": {"whatsapp_number": "yes", "project": "Swarit"}}))
    sid = _sends()[0]["id"]
    assert whatsapp_api.list_sends(owner=OTHER, status="", playbook_id=None,
                                   page=1, page_size=25)["total"] == 0
    for fn in (lambda: whatsapp_api.approve_send(sid, None, owner=OTHER),
               lambda: whatsapp_api.skip_send(sid, owner=OTHER)):
        with pytest.raises(HTTPException) as exc:
            fn()
        assert exc.value.status_code == 404
    out = whatsapp_api.approve_send(sid, None, owner=OWNER)
    assert out["send"]["status"] == "queued"


def test_a_playbook_cannot_name_another_tenants_agent(wa_db):
    import whatsapp_api
    from fastapi import HTTPException

    body = whatsapp_api.PlaybookBody(name="x", agent_slugs=["nisha-outbound"])
    with pytest.raises(HTTPException) as exc:
        whatsapp_api.create_playbook(body, owner=OWNER)
    assert exc.value.status_code == 404


def test_approving_refuses_an_empty_field(wa_db):
    import whatsapp_api
    from fastapi import HTTPException

    _approved_template()
    _playbook("manual")
    _run_stage(_insert_call())  # no project → body.project_name empty
    sid = _sends()[0]["id"]
    with pytest.raises(HTTPException) as exc:
        whatsapp_api.approve_send(sid, None, owner=OWNER)
    assert exc.value.status_code == 422
    out = whatsapp_api.approve_send(
        sid, whatsapp_api.ParamsBody(params={"body.project_name": "Swarit"}), owner=OWNER)
    assert out["send"]["status"] == "queued"
    assert out["send"]["params"]["body.project_name"] == "Swarit"


def test_preview_renders_against_the_latest_real_call(wa_db):
    import whatsapp_api

    _approved_template()
    _insert_call(cd={"state": {"whatsapp_number": "yes", "project": "Swarit"}})
    body = whatsapp_api.PreviewBody(
        name="x", agent_slugs=["ravi-inbound"], template_name="brochure",
        variables={"body.greeting": {"value": "{greeting}"},
                   "body.project_name": {"value": "{project_name}"}})
    out = whatsapp_api.preview_playbook(body, owner=OWNER)
    assert out["sample"] is False
    assert out["preview"]["body"] == "Hi श्री Rakesh Shah, here is the Swarit brochure."


def test_webhook_and_link_routes_over_http(wa_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from shared.whatsapp import store
    import whatsapp_api

    app = FastAPI()
    app.include_router(whatsapp_api.router)
    client = TestClient(app)

    monkeypatch.setenv("WA_APP_SECRET", "")
    assert client.post("/api/whatsapp/webhook", content=b"{}").status_code == 503
    monkeypatch.setenv("WA_APP_SECRET", "abc")
    assert client.post("/api/whatsapp/webhook", content=b"{}",
                       headers={"x-hub-signature-256": "sha256=00"}).status_code == 401
    body = b'{"entry":[]}'
    sig = "sha256=" + hmac.new(b"abc", body, hashlib.sha256).hexdigest()
    r = client.post("/api/whatsapp/webhook", content=body, headers={"x-hub-signature-256": sig})
    assert r.status_code == 200
    r = client.post("/api/whatsapp/webhook", content=body, headers={"x-hub-signature-256": sig})
    assert r.json().get("duplicate") == 1

    token = store.create_link(OWNER, 0, "https://docs.google.com/forms/abc")
    r = client.get(f"/r/{token}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://docs.google.com/forms/abc"
    assert client.get("/r/nope!!", follow_redirects=False).status_code == 404
