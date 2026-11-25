"""Meta webhook: delivery receipts, customer replies, STOP, template reviews.

The endpoint is unauthenticated by design — the X-Hub-Signature-256 HMAC over
the raw body (keyed by the platform's app secret) is its credential, exactly
like the Razorpay webhook. With no secret configured it refuses everything:
an unverifiable webhook that is accepted is a way for anyone to mark messages
"read", invent replies, or opt real customers out.

Every accepted payload is logged raw (wa_webhook_events, deduped by body
hash) before it is processed, so a processing bug can be fixed and replayed.

Interakt's webhook (RUNBOOK §80) lands on a per-tenant URL
(/api/whatsapp/webhook/interakt/<token>) and is signed with the secret the
tenant typed into Interakt — `Interakt-Signature: sha256=<hex HMAC>`. Same
rule: no secret on file, nothing is accepted. ingest_interakt() maps its
events onto the same send statuses and the same inbound/STOP handling.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from typing import Any

from shared.phone import significant_digits
from shared.whatsapp import config, store

log = logging.getLogger("whatsapp.webhook")

# A whole message that is one of these is an opt-out. Whole message only:
# "please don't stop sending the brochure" is not a STOP.
STOP_WORDS = {
    "stop", "stop all", "unsubscribe", "opt out", "optout", "remove me",
    "don't message", "dont message", "no more messages", "band", "band karo",
    "mat bhejo", "बंद", "बंद करो", "मत भेजो", "बंद कीजिए", "બંધ", "બંધ કરો",
}


def signature_ok(raw_body: bytes, header: str | None) -> bool:
    secret = config.app_secret()
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):].strip())


def verify_challenge(mode: str | None, token: str | None, challenge: str | None) -> str | None:
    """Meta's subscription handshake: echo the challenge iff the token matches."""
    expected = config.verify_token()
    if mode == "subscribe" and expected and token and hmac.compare_digest(expected, token):
        return challenge or ""
    return None


def is_stop(text: str) -> bool:
    t = re.sub(r"[\s.!।,]+", " ", (text or "")).strip().lower()
    return t in STOP_WORDS


def _e164(wa_id: str) -> str:
    digits = re.sub(r"\D", "", wa_id or "")
    return "+" + digits if digits else ""


def _message_text(msg: dict[str, Any]) -> str:
    kind = msg.get("type") or ""
    if kind == "text":
        return (msg.get("text") or {}).get("body") or ""
    if kind == "button":
        return (msg.get("button") or {}).get("text") or ""
    if kind == "interactive":
        inter = msg.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return reply.get("title") or ""
    if kind in ("image", "document", "video", "audio", "sticker"):
        caption = (msg.get(kind) or {}).get("caption") or ""
        return f"[{kind}] {caption}".strip()
    if kind == "location":
        loc = msg.get("location") or {}
        return f"[location] {loc.get('name') or ''} {loc.get('address') or ''}".strip()
    return f"[{kind or 'message'}]"


def ingest(raw_body: bytes) -> dict[str, int]:
    """Log then process one verified payload. Never raises."""
    key = hashlib.sha256(raw_body).hexdigest()
    event_id = store.record_webhook_event(key, raw_body.decode("utf-8", "replace"))
    if event_id is None:
        return {"duplicate": 1}
    try:
        payload = json.loads(raw_body or b"{}")
        stats = process(payload)
        store.finish_webhook_event(event_id, None)
        return stats
    except Exception as exc:
        log.error("wa webhook processing failed: %r", exc, exc_info=True)
        store.finish_webhook_event(event_id, repr(exc)[:500])
        return {"error": 1}


def process(payload: dict[str, Any]) -> dict[str, int]:
    stats = {"statuses": 0, "messages": 0, "stops": 0, "templates": 0}
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            field = change.get("field")
            value = change.get("value") or {}
            if field == "messages":
                pnid = (value.get("metadata") or {}).get("phone_number_id") or ""
                acct = store.account_by_phone_number_id(pnid) if pnid else None
                if not acct:
                    log.info("wa webhook: phone_number_id %s is not connected here", pnid)
                    continue
                for st in value.get("statuses") or []:
                    _status(acct["owner"], st)
                    stats["statuses"] += 1
                for msg in value.get("messages") or []:
                    if _message(acct["owner"], msg):
                        stats["stops"] += 1
                    stats["messages"] += 1
            elif field == "message_template_status_update":
                tid = str(value.get("message_template_id") or "")
                event = (value.get("event") or "").upper()
                if tid and event:
                    reason = value.get("reason")
                    store.set_template_status(tid, event,
                                              None if reason in (None, "NONE") else str(reason))
                    stats["templates"] += 1
    return stats


def _status(owner: str, st: dict[str, Any]) -> None:
    wamid = st.get("id") or ""
    status = (st.get("status") or "").lower()
    if not wamid or status not in ("sent", "delivered", "read", "failed"):
        return
    err = (st.get("errors") or [{}])[0] if st.get("errors") else {}
    detail = ((err.get("error_data") or {}).get("details") or err.get("message")
              or err.get("title") or "")
    _apply_status(owner, wamid, status,
                  error_code=str(err.get("code")) if err.get("code") else None,
                  error=detail or None,
                  pricing_category=(st.get("pricing") or {}).get("category"))


def _apply_status(owner: str, wamid: str, status: str, *, error_code: str | None,
                  error: str | None, pricing_category: str | None) -> bool:
    row = store.apply_status(wamid, status, error_code=error_code, error=error,
                             pricing_category=pricing_category)
    if not row or row["owner"] != owner:
        return False
    if row.get("contact_id") and status in ("read", "failed"):
        from shared.intelligence import timeline

        timeline.emit(int(row["contact_id"]), owner, f"whatsapp.{status}",
                      ref_table="wa_sends", ref_id=int(row["id"]),
                      payload={"error": (error or "")[:200]} if status == "failed" else None)
    return True


def _message(owner: str, msg: dict[str, Any]) -> bool:
    """Store one inbound Meta message. Returns True when it was an opt-out."""
    phone = _e164(msg.get("from") or "")
    wamid = msg.get("id") or ""
    if not phone or not wamid:
        return False
    return _inbound(owner, phone=phone, wamid=wamid, kind=msg.get("type") or "",
                    body=_message_text(msg))


def _inbound(owner: str, *, phone: str, wamid: str, kind: str, body: str) -> bool:
    """Store one inbound message from either provider. True when it was a STOP."""
    stop = is_stop(body)
    contact = store.contact_by_phone(owner, significant_digits(phone))
    contact_id = contact["id"] if contact else None
    inbound_id = store.insert_inbound(owner, phone=phone, contact_id=contact_id,
                                      wamid=wamid, kind=kind,
                                      body=body[:4000], is_stop=stop)
    if inbound_id is None:
        return False  # the provider re-delivered a message we already have

    from shared.intelligence import timeline
    from shared.intelligence.notify import _emit

    who = (contact or {}).get("name") or phone
    if stop:
        cid = store.record_consent(owner, phone=phone, contact_id=contact_id,
                                   state="revoked", source="reply_stop", call_id=None,
                                   quote=body[:200])
        store.cancel_open_sends_for_phone(owner, phone, "this person replied STOP")
        if contact_id:
            timeline.emit(contact_id, owner, "consent.revoked", ref_table="wa_consents",
                          ref_id=cid, payload={"channel": "whatsapp", "quote": body[:200]})
        _emit(owner, "whatsapp.optout", "warn", f"{who} opted out of WhatsApp",
              f"They replied “{body[:80]}”. Nothing more will be sent to them.",
              "/integrations/whatsapp?tab=activity", f"wa:in:{inbound_id}")
        return True

    if contact_id:
        timeline.emit(contact_id, owner, "whatsapp.replied", ref_table="wa_inbound",
                      ref_id=inbound_id, payload={"body": body[:200]})
    _emit(owner, "whatsapp.reply", "info", f"{who} replied on WhatsApp",
          body[:200], "/integrations/whatsapp?tab=activity", f"wa:in:{inbound_id}")
    return False


# ---------------------------------------------------------------------------
# Interakt
# ---------------------------------------------------------------------------

def ingest_interakt(owner: str, raw_body: bytes) -> dict[str, int]:
    """Log then process one verified Interakt payload for `owner`. Never raises."""
    key = "interakt:" + hashlib.sha256(raw_body).hexdigest()
    event_id = store.record_webhook_event(key, raw_body.decode("utf-8", "replace"))
    if event_id is None:
        return {"duplicate": 1}
    try:
        payload = json.loads(raw_body or b"{}")
        stats = process_interakt(owner, payload if isinstance(payload, dict) else {})
        store.finish_webhook_event(event_id, None)
        return stats
    except Exception as exc:
        log.error("interakt webhook processing failed: %r", exc, exc_info=True)
        store.finish_webhook_event(event_id, repr(exc)[:500])
        return {"error": 1}


def process_interakt(owner: str, payload: dict[str, Any]) -> dict[str, int]:
    from shared.whatsapp import interakt

    stats = {"statuses": 0, "messages": 0, "stops": 0}
    ev = interakt.parse_event(payload)
    if not ev:
        return stats
    if ev["kind"] == "status":
        if _apply_status(owner, ev["message_id"], ev["status"], error_code=ev["error_code"],
                         error=ev["error"], pricing_category=None):
            stats["statuses"] += 1
        return stats
    if _inbound(owner, phone=ev["phone"], wamid=ev["message_id"], kind=ev["content_type"],
                body=ev["body"]):
        stats["stops"] += 1
    stats["messages"] += 1
    return stats
