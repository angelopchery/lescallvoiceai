"""Interakt (interakt.ai) — a Meta Business Solution Provider.

The second way a tenant's WhatsApp can be wired up (RUNBOOK §80). Pramukh
Group's WhatsApp Business number lives on Interakt: one API key instead of a
WABA id + phone number id + system-user token, templates created and approved
in Interakt's dashboard (there is no public "list templates" endpoint, so the
tenant registers each one here by hand), media passed as a PUBLIC URL rather
than an uploaded media id, and delivery receipts through Interakt's webhook
(HMAC-signed with a secret the tenant chooses).

Everything that talks to Interakt is in this module. Failures are raised as
graph.GraphError so the sender's one retry/fail path serves both providers:
`transient` decides whether a later attempt can succeed, `http_status` 401
means the key itself was rejected.

API shape (docs: interakt.shop/resource-center/how-to-send-whatsapp-templates-
using-apis-webhooks/):

    POST https://api.interakt.ai/v1/public/message/
    Authorization: Basic <api key>          (the key IS the Basic credential)
    {"countryCode": "+91", "phoneNumber": "9876543210", "type": "Template",
     "callbackData": "...", "template": {"name": ..., "languageCode": "en",
     "headerValues": [<media url> | <header text var>], "fileName": "x.pdf",
     "bodyValues": ["v1", "v2"], "buttonValues": {"0": ["suffix"]}}}
    → {"result": true, "message": "Message created successfully", "id": "..."}
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

import httpx

from shared.whatsapp import templates
from shared.whatsapp.graph import GraphError

BASE = "https://api.interakt.ai/v1/public"
_TIMEOUT = httpx.Timeout(20.0, connect=5.0)

# Interakt's webhook event → our send status (store.apply_status vocabulary).
STATUS_EVENTS = {
    "message_api_sent": "sent", "message_api_delivered": "delivered",
    "message_api_read": "read", "message_api_failed": "failed",
    "message_campaign_sent": "sent", "message_campaign_delivered": "delivered",
    "message_campaign_read": "read", "message_campaign_failed": "failed",
}
INBOUND_EVENT = "message_received"

_KEY_RE = re.compile(r"^[A-Za-z0-9+/=_-]{16,512}$")


def normalize_key(raw: str) -> str:
    """The key as Interakt shows it, whether pasted bare or as the whole
    `Basic …` header value. '' when it cannot be a key."""
    key = (raw or "").strip()
    if key.lower().startswith("basic "):
        key = key[6:].strip()
    return key if _KEY_RE.match(key) else ""


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Basic {api_key}", "Content-Type": "application/json",
            "Accept": "application/json"}


def _error_from(resp: httpx.Response) -> GraphError:
    msg = f"HTTP {resp.status_code}"
    try:
        body = resp.json() or {}
        detail = body.get("message") or body.get("detail") or body.get("error")
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, ensure_ascii=False)
        if detail:
            msg = f"{detail} (HTTP {resp.status_code})"
    except ValueError:
        text = (resp.text or "").strip()
        if text:
            msg = f"{text[:200]} (HTTP {resp.status_code})"
    transient = resp.status_code == 429 or resp.status_code >= 500
    return GraphError(msg, code=resp.status_code, http_status=resp.status_code,
                      transient=transient)


async def _post(path: str, api_key: str, payload: dict[str, Any],
                params: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(f"{BASE}/{path.lstrip('/')}", headers=_headers(api_key),
                                     json=payload, params=params)
    except httpx.HTTPError as exc:
        raise GraphError(f"could not reach Interakt: {exc!r}", transient=True) from exc
    if resp.status_code >= 400:
        raise _error_from(resp)
    try:
        body = resp.json() or {}
    except ValueError:
        body = {}
    if isinstance(body, dict) and body.get("result") is False:
        raise GraphError(str(body.get("message") or "Interakt refused the request"),
                         code=resp.status_code, http_status=resp.status_code, transient=False)
    return body if isinstance(body, dict) else {}


async def verify_key(api_key: str) -> dict[str, Any]:
    """Prove the key is accepted without touching anything: the Get Users API
    filtered to a date in the future returns an empty page for a valid key and
    401 for an invalid one. (The only GET-free, side-effect-free call the
    public API offers.)"""
    return await _post(
        "apis/users/", api_key,
        {"filters": [{"trait": "created_at_utc", "op": "gt", "val": "2099-01-01T00:00:00.000Z"}]},
        params={"offset": "0", "limit": "1"})


def split_phone(e164: str) -> tuple[str, str]:
    """"+919876543210" → ("+91", "9876543210"). Interakt wants the country code
    and the national number apart, the national part without a leading zero.
    Indian numbers are 10 digits after +91; anything else keeps the last ten
    digits as the national number, which is right for every country whose
    national plan is 10 digits (US, Canada, Australia mobiles…)."""
    digits = re.sub(r"\D", "", e164 or "")
    if not digits:
        return "", ""
    if len(digits) == 12 and digits.startswith("91"):
        return "+91", digits[2:]
    if len(digits) == 10:
        return "+91", digits
    if len(digits) > 10:
        return "+" + digits[:-10], digits[-10:]
    return "+91", digits.lstrip("0")


def build_template(tpl: dict, params: dict[str, str], *, media_url: str = "",
                   file_name: str = "") -> dict[str, Any]:
    """Interakt's `template` object for one send, from a Meta-shaped template
    and the rendered slot values.

    Interakt fills variables POSITIONALLY — bodyValues in the order the
    variables appear in the body text — whether the template was written with
    {{1}} or with names, so order of appearance is the one rule."""
    comps = templates._components(tpl)
    out: dict[str, Any] = {"name": tpl.get("name"), "languageCode": tpl.get("language") or "en"}
    header_values: list[str] = []
    body_values: list[str] = []
    button_values: dict[str, list[str]] = {}
    for comp in comps:
        ctype = (comp.get("type") or "").upper()
        if ctype == "HEADER":
            fmt = (comp.get("format") or "TEXT").upper()
            if fmt in templates.MEDIA_FORMATS:
                if not media_url:
                    raise ValueError("this template needs an attachment and the "
                                     "playbook has none")
                header_values = [media_url]
                if fmt == "DOCUMENT" and file_name:
                    out["fileName"] = file_name
            elif fmt == "TEXT":
                header_values = [params.get(f"header.{v}", "")
                                 for v in templates._vars(comp.get("text") or "")]
        elif ctype == "BODY":
            body_values = [params.get(f"body.{v}", "")
                           for v in templates._vars(comp.get("text") or "")]
        elif ctype == "BUTTONS":
            for i, b in enumerate(comp.get("buttons") or []):
                if (b.get("type") or "").upper() == "URL" and templates._vars(b.get("url") or ""):
                    button_values[str(i)] = [params.get(f"button.{i}", "")]
    if header_values:
        out["headerValues"] = header_values
    out["bodyValues"] = body_values
    if button_values:
        out["buttonValues"] = button_values
    return out


async def send_template(api_key: str, *, to: str, template: dict[str, Any],
                        callback_data: str = "") -> str:
    """Send one template message; returns the message id Interakt assigned
    (what its webhooks later call data.message.id)."""
    country, national = split_phone(to)
    if not national:
        raise GraphError(f"not a sendable number: {to!r}", transient=False)
    body: dict[str, Any] = {"countryCode": country, "phoneNumber": national,
                            "type": "Template", "template": template}
    if callback_data:
        body["callbackData"] = callback_data[:512]
    res = await _post("message/", api_key, body)
    mid = res.get("id")
    if not mid:
        raise GraphError("Interakt accepted the message but returned no message id",
                         transient=False)
    return str(mid)


# ---------------------------------------------------------------------------
# webhook
# ---------------------------------------------------------------------------

def signature_ok(raw_body: bytes, header: str | None, secret: str) -> bool:
    """`Interakt-Signature: sha256=<hex HMAC-SHA256 of the raw body>`, keyed
    by the secret the tenant typed into Interakt's Developer Settings."""
    if not secret or not header:
        return False
    given = header.strip()
    if given.lower().startswith("sha256="):
        given = given[7:].strip()
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given.lower())


def parse_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """One webhook payload → {kind, ...} or None when it is nothing we track.

    status:  {"kind": "status", "message_id", "status", "error_code", "error"}
    inbound: {"kind": "inbound", "message_id", "phone", "body", "content_type"}
    """
    kind = str(payload.get("type") or "")
    data = payload.get("data") or {}
    msg = data.get("message") or {}
    customer = data.get("customer") or {}
    mid = str(msg.get("id") or "")
    if kind in STATUS_EVENTS:
        if not mid:
            return None
        return {"kind": "status", "message_id": mid, "status": STATUS_EVENTS[kind],
                "error_code": str(msg.get("channel_error_code") or "") or None,
                "error": str(msg.get("channel_failure_reason") or "") or None}
    if kind == INBOUND_EVENT:
        digits = re.sub(r"\D", "", str(customer.get("channel_phone_number") or ""))
        if not mid or not digits:
            return None
        ctype = str(msg.get("message_content_type") or "Text")
        text = msg.get("message")
        if isinstance(text, (dict, list)):
            text = json.dumps(text, ensure_ascii=False)
        text = str(text or "")
        if ctype.lower() != "text":
            text = f"[{ctype.lower()}] {text}".strip()
        return {"kind": "inbound", "message_id": mid, "phone": "+" + digits,
                "body": text, "content_type": ctype.lower()}
    return None
