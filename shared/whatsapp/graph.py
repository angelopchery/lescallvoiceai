"""Meta Graph API client — the only module that talks to Meta.

Every failure becomes a GraphError carrying Meta's numeric code and whether
retrying could help. The split matters to the sender: "recipient is not on
WhatsApp" retried eight times is eight wasted attempts and a quality signal
against the number; "rate limited" given up on is a customer who never gets
their brochure.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

import httpx

from shared.whatsapp import config

# Codes where a later attempt can succeed. Everything else is permanent for
# THIS message (bad number, template problem, token revoked, account issue).
TRANSIENT_CODES = {
    1, 2, 4, 17, 341,          # Graph: unknown/temporary/app or user rate limit
    80007,                     # WABA rate limit
    130429,                    # throughput reached
    131000,                    # something went wrong
    131016,                    # service unavailable
    131048,                    # spam rate limit
    131056,                    # pair rate limit
    133004,                    # server temporarily unavailable
}

_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


class GraphError(Exception):
    def __init__(self, message: str, code: int | None = None,
                 http_status: int | None = None, transient: bool = False):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.transient = transient

    def as_text(self) -> str:
        return f"{self.code}: {self}" if self.code else str(self)


def _url(path: str) -> str:
    return f"{config.GRAPH_BASE}/{config.api_version()}/{path.lstrip('/')}"


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    code = None
    msg = f"HTTP {resp.status_code}"
    try:
        err = (resp.json() or {}).get("error") or {}
        code = err.get("code")
        detail = (err.get("error_data") or {}).get("details") or err.get("error_user_msg")
        msg = err.get("message") or msg
        if detail and detail not in msg:
            msg = f"{msg} — {detail}"
    except ValueError:
        pass
    transient = resp.status_code >= 500 or resp.status_code == 429 or code in TRANSIENT_CODES
    raise GraphError(msg, code=code, http_status=resp.status_code, transient=transient)


async def _request(method: str, path: str, token: str, **kw) -> dict[str, Any]:
    headers = kw.pop("headers", {}) or {}
    headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.request(method, _url(path), headers=headers, **kw)
    except httpx.HTTPError as exc:
        raise GraphError(f"could not reach Meta: {exc!r}", transient=True) from exc
    _raise_for(resp)
    try:
        return resp.json()
    except ValueError:
        return {}


async def phone_number_info(phone_number_id: str, token: str) -> dict[str, Any]:
    info = await _request(
        "GET", phone_number_id, token,
        params={"fields": "display_phone_number,verified_name,quality_rating"})
    # The messaging tier is a newer field; an older API version refusing it
    # must not fail the whole connect.
    try:
        tier = await _request("GET", phone_number_id, token,
                              params={"fields": "messaging_limit_tier"})
        info["messaging_limit_tier"] = tier.get("messaging_limit_tier")
    except GraphError:
        pass
    return info


async def subscribe_app(waba_id: str, token: str) -> None:
    """Point this WABA's webhooks at our app. Idempotent on Meta's side."""
    await _request("POST", f"{waba_id}/subscribed_apps", token)


async def list_templates(waba_id: str, token: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    params = {"fields": "id,name,language,status,category,components,"
                        "parameter_format,rejected_reason",
              "limit": "100"}
    path = f"{waba_id}/message_templates"
    for _ in range(20):  # 2,000 templates is far past any real WABA
        page = await _request("GET", path, token, params=params)
        out.extend(page.get("data") or [])
        after = ((page.get("paging") or {}).get("cursors") or {}).get("after")
        if not after or not (page.get("paging") or {}).get("next"):
            break
        params = {**params, "after": after}
    return out


async def create_template(waba_id: str, token: str, payload: dict) -> dict[str, Any]:
    return await _request("POST", f"{waba_id}/message_templates", token, json=payload)


async def upload_media(phone_number_id: str, token: str, path: Path,
                       mime: str) -> str:
    """Upload a file for sending; returns Meta's media id (valid ~30 days)."""
    data = path.read_bytes()
    res = await _request(
        "POST", f"{phone_number_id}/media", token,
        data={"messaging_product": "whatsapp", "type": mime},
        files={"file": (path.name, data, mime)})
    mid = res.get("id")
    if not mid:
        raise GraphError("Meta accepted the upload but returned no media id")
    return str(mid)


async def upload_header_example(token: str, path: Path, mime: str) -> str:
    """Resumable upload → the `header_handle` a media-header template needs
    as its example at creation time. Keyed by the platform's Meta app id."""
    app = config.app_id()
    if not app:
        raise GraphError("Creating a template with an attachment header needs the "
                         "platform's Meta app id (WA_APP_ID), which is not configured.")
    data = path.read_bytes()
    session = await _request(
        "POST", f"{app}/uploads", token,
        params={"file_name": path.name, "file_length": str(len(data)),
                "file_type": mime or mimetypes.guess_type(path.name)[0] or ""})
    sid = session.get("id")
    if not sid:
        raise GraphError("Meta did not open an upload session")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(_url(sid), content=data, headers={
                "Authorization": f"OAuth {token}", "file_offset": "0"})
    except httpx.HTTPError as exc:
        raise GraphError(f"could not reach Meta: {exc!r}", transient=True) from exc
    _raise_for(resp)
    handle = (resp.json() or {}).get("h")
    if not handle:
        raise GraphError("Meta returned no file handle for the header example")
    return handle


async def send_template(phone_number_id: str, token: str, *, to: str, name: str,
                        language: str, components: list[dict]) -> str:
    """Send one template message; returns the wamid Meta assigned."""
    body = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to.lstrip("+"),
        "type": "template",
        "template": {"name": name, "language": {"code": language},
                     "components": components},
    }
    res = await _request("POST", f"{phone_number_id}/messages", token, json=body)
    msgs = res.get("messages") or []
    wamid = msgs[0].get("id") if msgs else None
    if not wamid:
        raise GraphError("Meta accepted the message but returned no message id",
                         transient=False)
    return str(wamid)
