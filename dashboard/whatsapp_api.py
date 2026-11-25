"""Integrations → WhatsApp: the tenant API behind /integrations/whatsapp.

Three kinds of route live here, and each is admitted differently by
auth.install_login_gate:

  /api/integrations/whatsapp/*   session + tenancy (listed in
                                 _TENANT_API_PREFIXES); every handler scopes
                                 by Depends(current_owner)
  /api/whatsapp/webhook          OPEN by exact path (_OPEN_EXACT). Its
                                 credential is the X-Hub-Signature-256 HMAC;
                                 with no app secret configured it refuses all
  /api/whatsapp/webhook/interakt/<token>
                                 OPEN by prefix. Interakt's webhook for ONE
                                 tenant (the token names the account); its
                                 credential is the Interakt-Signature HMAC
                                 keyed by that tenant's own secret
  /r/<token>                     OPEN by prefix (_OPEN_PREFIXES). A tracked
                                 link in a customer's message: it records the
                                 click and redirects. The token is the whole
                                 capability — it names one URL, nothing else
  /r/f/<token>                   OPEN by the same prefix. Serves ONE uploaded
                                 attachment (the brochure) so a BSP can fetch
                                 it by URL. Same rule: token = one file

The send pipeline itself is shared/whatsapp (stage → sender); this module is
configuration, review and the webhook door.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# WA_* secrets (token key, app secret, verify token, kill switch) live in
# calling-agent/.env with the platform's other keys. Loaded here directly, as
# intel_api does, rather than trusting some other router to have done it first.
try:
    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / "calling-agent" / ".env")
except Exception:  # pragma: no cover
    pass

from auth import current_owner  # noqa: E402
from shared.db.database import get_db  # noqa: E402
from shared.whatsapp import (  # noqa: E402
    config, crypto, graph, interakt, placeholders, stage, store, templates, webhook,
)
from shared.whatsapp.consent import SOURCE_LABELS  # noqa: E402

log = logging.getLogger("dashboard.whatsapp")

router = APIRouter()
_P = "/api/integrations/whatsapp"

WEBHOOK_MAX_BYTES = 1024 * 1024


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _owner_slugs(owner: str) -> set[str]:
    with get_db() as db:
        rows = db.execute("SELECT slug FROM agents WHERE owner=?", (owner,)).fetchall()
    return {r["slug"] for r in rows}


async def _token(owner: str) -> str:
    blob = await asyncio.to_thread(store.account_token_blob, owner)
    if not blob:
        raise HTTPException(409, "Connect a WhatsApp Business number first.")
    try:
        return crypto.decrypt(blob)
    except Exception as exc:
        raise HTTPException(409, f"The stored access token cannot be read ({exc!s}). "
                                 "Reconnect the number.") from exc


def _graph_http(exc: graph.GraphError) -> HTTPException:
    return HTTPException(502, f"Meta refused the request: {exc.as_text()}")


def _account_view(owner: str) -> dict[str, Any]:
    acct = store.get_account(owner)
    acct.pop("has_token", None)
    acct["provider"] = (acct.get("provider") or "meta").lower()
    acct["webhook_url"] = (config.interakt_webhook_url(acct["webhook_token"])
                           if acct["provider"] == "interakt" and acct.get("webhook_token") else None)
    return acct


def _provider(owner: str) -> str:
    return (store.get_account(owner).get("provider") or "meta").lower()


def _agent_project(slug: str) -> str:
    from shared.intelligence.pipeline import _agent_yaml

    return stage._primary_project_name(_agent_yaml(slug))


# ---------------------------------------------------------------------------
# overview + connection
# ---------------------------------------------------------------------------

@router.get(f"{_P}/overview")
def overview(owner: str = Depends(current_owner)) -> dict:
    return {
        "account": _account_view(owner),
        "platform": config.platform_status(),
        "counts": store.counts(owner),
        "playbooks": len(store.list_playbooks(owner)),
        "templates": len(store.list_templates(owner)),
    }


@router.get(f"{_P}/pending-count")
def pending_count(owner: str = Depends(current_owner)) -> dict:
    return {"pending": store.pending_count(owner)}


class ConnectBody(BaseModel):
    waba_id: str = Field(min_length=5, max_length=40)
    phone_number_id: str = Field(min_length=5, max_length=40)
    access_token: str = Field(min_length=20, max_length=1024)


async def _sync_templates(owner: str, waba_id: str, token: str) -> int:
    rows = await graph.list_templates(waba_id, token)
    return await asyncio.to_thread(store.replace_templates, owner, rows)


@router.post(f"{_P}/connect")
async def connect(body: ConnectBody, owner: str = Depends(current_owner)) -> dict:
    waba = body.waba_id.strip()
    pnid = body.phone_number_id.strip()
    if not (waba.isdigit() and pnid.isdigit()):
        raise HTTPException(422, "The WhatsApp Business Account ID and Phone number ID "
                                 "are numbers — copy them from Meta's WhatsApp → API Setup page.")
    if await asyncio.to_thread(store.phone_number_id_taken, pnid, owner):
        raise HTTPException(409, "That WhatsApp number is already connected to another account.")
    try:
        enc = crypto.encrypt(body.access_token.strip())
    except crypto.TokenKeyMissing as exc:
        raise HTTPException(503, str(exc)) from exc
    token = body.access_token.strip()
    try:
        info = await graph.phone_number_info(pnid, token)
    except graph.GraphError as exc:
        raise HTTPException(422, f"Meta did not accept these details: {exc.as_text()}") from exc
    subscribe_error = None
    try:
        await graph.subscribe_app(waba, token)
    except graph.GraphError as exc:
        # Sending still works; replies and delivery receipts will not arrive.
        subscribe_error = exc.as_text()
    await asyncio.to_thread(
        store.save_connection, owner, waba_id=waba, phone_number_id=pnid,
        display_phone=info.get("display_phone_number") or "",
        verified_name=info.get("verified_name") or "", token_enc=enc,
        quality_rating=info.get("quality_rating"),
        messaging_limit=info.get("messaging_limit_tier"))
    synced, sync_error = 0, None
    try:
        synced = await _sync_templates(owner, waba, token)
    except graph.GraphError as exc:
        sync_error = exc.as_text()
    if subscribe_error:
        await asyncio.to_thread(
            store.set_account_health, owner,
            error=f"Connected, but webhooks could not be subscribed ({subscribe_error}) — "
                  "delivery receipts and replies will not arrive.")
    return {"account": _account_view(owner), "templates_synced": synced,
            "template_sync_error": sync_error, "webhook_error": subscribe_error}


class InteraktConnectBody(BaseModel):
    api_key: str = Field(min_length=16, max_length=600)
    display_phone: str = Field(default="", max_length=40)
    webhook_secret: str = Field(default="", max_length=200)


@router.post(f"{_P}/connect/interakt")
async def connect_interakt(body: InteraktConnectBody,
                           owner: str = Depends(current_owner)) -> dict:
    """Connect through Interakt (RUNBOOK §80): the key from Interakt →
    Settings → Developer Settings is verified with a side-effect-free call,
    then stored encrypted. Templates are registered by hand afterwards —
    Interakt has no API that lists them."""
    key = interakt.normalize_key(body.api_key)
    if not key:
        raise HTTPException(422, "That does not look like an Interakt API key — copy the "
                                 "Secret Key from Interakt → Settings → Developer Settings.")
    try:
        enc = crypto.encrypt(key)
        secret_enc = crypto.encrypt(body.webhook_secret.strip()) if body.webhook_secret.strip() else None
    except crypto.TokenKeyMissing as exc:
        raise HTTPException(503, str(exc)) from exc
    try:
        await interakt.verify_key(key)
    except graph.GraphError as exc:
        if exc.http_status in (401, 403):
            raise HTTPException(422, "Interakt rejected this API key. Check it is the account's "
                                     "Secret Key and that public APIs are enabled on the plan.") from exc
        raise HTTPException(502, f"Interakt could not be reached to verify the key: "
                                 f"{exc.as_text()}") from exc
    await asyncio.to_thread(
        store.save_interakt_connection, owner, token_enc=enc,
        display_phone=body.display_phone.strip(), webhook_secret_enc=secret_enc)
    return {"account": _account_view(owner),
            "templates": len(store.list_templates(owner))}


class WebhookSecretBody(BaseModel):
    secret: str = Field(default="", max_length=200)


@router.post(f"{_P}/webhook-secret")
def set_webhook_secret(body: WebhookSecretBody, owner: str = Depends(current_owner)) -> dict:
    """The HMAC secret the tenant typed into Interakt's webhook settings.
    Empty clears it — and with it, every receipt is refused until a new one
    is set (an unverifiable webhook is not accepted)."""
    if _provider(owner) != "interakt":
        raise HTTPException(409, "Webhook secrets apply to accounts connected through Interakt.")
    secret = body.secret.strip()
    try:
        enc = crypto.encrypt(secret) if secret else None
    except crypto.TokenKeyMissing as exc:
        raise HTTPException(503, str(exc)) from exc
    store.set_webhook_secret(owner, enc)
    return {"account": _account_view(owner)}


@router.post(f"{_P}/disconnect")
def disconnect(owner: str = Depends(current_owner)) -> dict:
    store.disconnect(owner)
    return {"account": _account_view(owner)}


@router.post(f"{_P}/refresh")
async def refresh(owner: str = Depends(current_owner)) -> dict:
    acct = await asyncio.to_thread(store.get_account, owner)
    token = await _token(owner)
    if (acct.get("provider") or "meta") == "interakt":
        try:
            await interakt.verify_key(token)
        except graph.GraphError as exc:
            await asyncio.to_thread(store.set_account_health, owner, error=exc.as_text())
            raise HTTPException(502, f"Interakt refused the stored key: {exc.as_text()}") from exc
        await asyncio.to_thread(store.set_account_health, owner, error=None)
        return {"account": _account_view(owner),
                "templates_synced": len(store.list_templates(owner))}
    try:
        info = await graph.phone_number_info(acct["phone_number_id"], token)
        synced = await _sync_templates(owner, acct["waba_id"], token)
    except graph.GraphError as exc:
        await asyncio.to_thread(store.set_account_health, owner, error=exc.as_text())
        raise _graph_http(exc) from exc
    await asyncio.to_thread(store.set_account_health, owner,
                            quality_rating=info.get("quality_rating"),
                            messaging_limit=info.get("messaging_limit_tier"), error=None)
    return {"account": _account_view(owner), "templates_synced": synced}


class SettingsBody(BaseModel):
    default_approval: str = Field(pattern="^(manual|auto)$")
    send_window_start: int = Field(ge=0, le=23)
    send_window_end: int = Field(ge=1, le=24)
    send_delay_min: int = Field(ge=0, le=1440)
    daily_cap: int = Field(ge=1, le=100000)
    approval_ttl_h: int = Field(ge=1, le=168)


@router.patch(f"{_P}/settings")
def update_settings(body: SettingsBody, owner: str = Depends(current_owner)) -> dict:
    if body.send_window_end <= body.send_window_start:
        raise HTTPException(422, "Sending hours must end after they start.")
    store.update_settings(owner, **body.model_dump())
    return {"account": _account_view(owner)}


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------

def _template_view(t: dict) -> dict:
    return {**t, "slots": templates.slots(t), "preview": templates.preview(t, {}),
            "media": templates.media_format(t)}


@router.get(f"{_P}/templates")
def list_templates(owner: str = Depends(current_owner)) -> dict:
    return {"templates": [_template_view(t) for t in store.list_templates(owner)]}


@router.post(f"{_P}/templates/sync")
async def sync_templates(owner: str = Depends(current_owner)) -> dict:
    acct = await asyncio.to_thread(store.get_account, owner)
    token = await _token(owner)
    if (acct.get("provider") or "meta") == "interakt":
        raise HTTPException(409, "Interakt has no API that lists templates. Add each approved "
                                 "template here with the same name and text it has in Interakt.")
    try:
        n = await _sync_templates(owner, acct["waba_id"], token)
    except graph.GraphError as exc:
        raise _graph_http(exc) from exc
    return {"templates_synced": n}


class TemplateBody(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    language: str = Field(pattern=r"^[a-z]{2}(_[A-Z]{2})?$")
    category: str = Field(pattern="^(UTILITY|MARKETING)$")
    body: str = Field(min_length=1, max_length=1024)
    footer: str = Field(default="", max_length=60)
    header: str = Field(default="none", pattern="^(none|document|image|video)$")
    header_media_id: int | None = None
    button_text: str = Field(default="", max_length=25)
    examples: dict[str, str] = Field(default_factory=dict)


@router.post(f"{_P}/templates")
async def create_template(body: TemplateBody, owner: str = Depends(current_owner)) -> dict:
    problems = templates.validate_new_template(body.name, body.body)
    if problems:
        raise HTTPException(422, " ".join(problems))
    acct = await asyncio.to_thread(store.get_account, owner)
    token = await _token(owner)
    if (acct.get("provider") or "meta") == "interakt":
        raise HTTPException(409, "Templates for an Interakt account are created and approved in "
                                 "Interakt → Templates; then add the approved one here.")
    handle = ""
    header_format = "" if body.header == "none" else body.header.upper()
    if header_format:
        if not body.header_media_id:
            raise HTTPException(422, "Pick the example file Meta will review for the header.")
        m = await asyncio.to_thread(store.get_media, owner, body.header_media_id)
        if not m:
            raise HTTPException(404, "That file is not in this account's library.")
        try:
            handle = await graph.upload_header_example(token, Path(m["path"]), m["mime"])
        except graph.GraphError as exc:
            raise _graph_http(exc) from exc
    payload = templates.create_payload(
        name=body.name, language=body.language, category=body.category, body=body.body,
        examples={k: v[:200] for k, v in body.examples.items()}, footer=body.footer,
        header_format=header_format, header_handle=handle,
        button_text=body.button_text,
        button_url_base=(config.public_base_url() + "/r") if body.button_text.strip() else "")
    try:
        res = await graph.create_template(acct["waba_id"], token, payload)
    except graph.GraphError as exc:
        raise _graph_http(exc) from exc
    row = {**payload, "id": res.get("id"), "status": res.get("status") or "PENDING",
           "category": res.get("category") or body.category}
    await asyncio.to_thread(store.upsert_template, owner, row)
    return {"template": _template_view({**row, "components": payload["components"]}),
            "category_changed": (res.get("category") or body.category).upper()
            != body.category}


class RegisterTemplateBody(BaseModel):
    """A template approved in the BSP's dashboard, written here exactly as it
    is there so the message can be previewed, its variables mapped, and the
    send built. `{{1}}`/`{{2}}` as Interakt shows them, or our `{greeting}`."""
    name: str = Field(min_length=1, max_length=512)
    language: str = Field(pattern=r"^[a-z]{2}(_[A-Z]{2})?$")
    category: str = Field(default="UTILITY", pattern="^(UTILITY|MARKETING|AUTHENTICATION)$")
    header: str = Field(default="none", pattern="^(none|text|document|image|video)$")
    header_text: str = Field(default="", max_length=60)
    body: str = Field(min_length=1, max_length=1024)
    footer: str = Field(default="", max_length=60)
    button_text: str = Field(default="", max_length=25)
    button_url: str = Field(default="", max_length=2000)


def _registered_row(body: RegisterTemplateBody) -> dict[str, Any]:
    problems = templates.validate_registered_template(body.name, body.body)
    if problems:
        raise HTTPException(422, " ".join(problems))
    url = body.button_url.strip()
    if body.button_text.strip() and url and not re.match(r"^https?://[^\s]+$", url):
        raise HTTPException(422, "The button link must be a full web address starting with https://")
    comps, fmt = templates.manual_components(
        header="" if body.header == "none" else body.header, header_text=body.header_text,
        body=body.body, footer=body.footer, button_text=body.button_text, button_url=url)
    return {"name": body.name.strip(), "language": body.language, "category": body.category,
            "status": "APPROVED", "parameter_format": fmt, "id": "", "components": comps,
            "rejected_reason": None}


@router.post(f"{_P}/templates/register")
def register_template(body: RegisterTemplateBody, owner: str = Depends(current_owner)) -> dict:
    if _provider(owner) != "interakt":
        raise HTTPException(409, "Registering a template by hand is for accounts connected "
                                 "through Interakt; a Meta account syncs its templates.")
    row = _registered_row(body)
    if store.template_name_taken(owner, row["name"], row["language"], None):
        raise HTTPException(409, f"A template named “{row['name']}” in that language is already here.")
    tid = store.upsert_template(owner, row, source="manual")
    return {"template": _template_view(store.get_template(owner, tid))}


@router.put(f"{_P}/templates/{{template_id}}")
def update_template(template_id: int, body: RegisterTemplateBody,
                    owner: str = Depends(current_owner)) -> dict:
    cur = store.get_template(owner, template_id)
    if not cur:
        raise HTTPException(404, "Template not found")
    if (cur.get("source") or "meta") != "manual":
        raise HTTPException(409, "This template is Meta's copy — edit it in WhatsApp Manager and sync.")
    row = _registered_row(body)
    if store.template_name_taken(owner, row["name"], row["language"], template_id):
        raise HTTPException(409, f"A template named “{row['name']}” in that language is already here.")
    store.update_manual_template(owner, template_id, row)
    return {"template": _template_view(store.get_template(owner, template_id))}


@router.delete(f"{_P}/templates/{{template_id}}")
def delete_template(template_id: int, owner: str = Depends(current_owner)) -> dict:
    cur = store.get_template(owner, template_id)
    if not cur:
        raise HTTPException(404, "Template not found")
    if (cur.get("source") or "meta") != "manual":
        raise HTTPException(409, "This template is Meta's copy — delete it in WhatsApp Manager and sync.")
    used = [p["name"] for p in store.list_playbooks(owner) if p.get("template_name") == cur["name"]]
    if used:
        raise HTTPException(409, f"Playbook(s) still use it: {', '.join(used)}. Pick another "
                                 "template there first.")
    store.delete_manual_template(owner, template_id)
    return {"ok": True}


@router.get(f"{_P}/projects")
def agent_projects(agents: str = Query(default=""), owner: str = Depends(current_owner)) -> dict:
    """The projects the chosen agents pitch (knowledge_base.primary_project),
    so a playbook can attach one brochure per project (RUNBOOK §80)."""
    slugs = _owner_slugs(owner)
    wanted = [a for a in dict.fromkeys(x.strip() for x in agents.split(",")) if a and a in slugs]
    out: dict[str, list[str]] = {}
    for slug in wanted:
        name = _agent_project(slug)
        if name:
            out.setdefault(name, []).append(slug)
    return {"projects": [{"name": n, "agents": a} for n, a in out.items()]}


# ---------------------------------------------------------------------------
# media library
# ---------------------------------------------------------------------------

def _sniff(data: bytes) -> str:
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[4:8] == b"ftyp":
        return "video/mp4"
    return ""


@router.get(f"{_P}/media")
def list_media(owner: str = Depends(current_owner)) -> dict:
    return {"media": store.list_media(owner)}


@router.delete(f"{_P}/media/{{media_id}}")
def delete_media(media_id: int, owner: str = Depends(current_owner)) -> dict:
    row = store.delete_media(owner, media_id)
    if not row:
        raise HTTPException(404, "That file is not in this account's library.")
    try:
        Path(row["path"]).unlink(missing_ok=True)
    except OSError:
        pass
    return {"ok": True}


@router.post(f"{_P}/media")
async def upload_media(file: UploadFile = File(...),
                       owner: str = Depends(current_owner)) -> dict:
    data = await file.read(config.MEDIA_MAX_BYTES + 1)
    if len(data) > config.MEDIA_MAX_BYTES:
        raise HTTPException(413, f"The file is larger than "
                                 f"{config.MEDIA_MAX_BYTES // (1024 * 1024)} MB.")
    if not data:
        raise HTTPException(422, "The file is empty.")
    # The bytes decide the type, not the name or the browser's claim.
    mime = _sniff(data)
    if mime not in config.MEDIA_TYPES:
        raise HTTPException(415, "WhatsApp attachments here can be a PDF, a JPEG or PNG "
                                 "image, or an MP4 video.")
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(file.filename or "file").name)[:120] or "file"
    folder = config.MEDIA_DIR / hashlib.sha256(owner.encode()).hexdigest()[:16]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}_{name}"
    await asyncio.to_thread(path.write_bytes, data)
    row = await asyncio.to_thread(store.insert_media, owner, filename=name, mime=mime,
                                  size_bytes=len(data), path=str(path))
    return {"media": row}


# ---------------------------------------------------------------------------
# playbooks
# ---------------------------------------------------------------------------

class VariableSpec(BaseModel):
    value: str = Field(default="", max_length=500)
    fallback: str = Field(default="", max_length=200)


class PlaybookBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = False
    agent_slugs: list[str] = Field(default_factory=list, max_length=50)
    require_booking: bool = False
    recipient: str = Field(default="dialled", pattern="^(dialled|given)$")
    template_name: str | None = Field(default=None, max_length=512)
    variables: dict[str, VariableSpec] = Field(default_factory=dict)
    media_id: int | None = None
    # project name → media id: the brochure of THE project the caller agreed
    # to, when one playbook covers several projects' agents. media_id stays
    # the default for a project not listed here.
    media_map: dict[str, int] = Field(default_factory=dict)
    link_url: str | None = Field(default=None, max_length=2000)
    approval_mode: str = Field(default="manual", pattern="^(manual|auto)$")


def _validate_playbook(owner: str, body: PlaybookBody) -> dict[str, Any]:
    slugs = _owner_slugs(owner)
    foreign = [s for s in body.agent_slugs if s not in slugs]
    if foreign:
        raise HTTPException(404, f"Agent not found: {', '.join(foreign)}")
    if body.enabled and not body.agent_slugs:
        raise HTTPException(422, "Pick at least one agent before switching this playbook on.")
    if body.enabled and not body.template_name:
        raise HTTPException(422, "Pick a message template before switching this playbook on.")
    if body.media_id is not None and not store.get_media(owner, body.media_id):
        raise HTTPException(404, "That attachment is not in this account's library.")
    media_map: dict[str, int] = {}
    if len(body.media_map) > 50:
        raise HTTPException(422, "Too many per-project attachments.")
    for project, mid in body.media_map.items():
        name = " ".join(str(project).split())[:80]
        if not name or not mid:
            continue
        if not store.get_media(owner, int(mid)):
            raise HTTPException(404, f"The attachment for {name} is not in this account's library.")
        media_map[name] = int(mid)
    link = (body.link_url or "").strip() or None
    if link and not re.match(r"^https?://[^\s]+$", link):
        raise HTTPException(422, "The link must be a full web address starting with https://")
    variables = {}
    for key, spec in body.variables.items():
        if not re.fullmatch(r"(header|body|button)\.[A-Za-z0-9_]{1,40}", key):
            raise HTTPException(422, f"Unknown template slot “{key}”.")
        variables[key] = {"value": spec.value, "fallback": spec.fallback}
    return {"name": body.name.strip(), "enabled": body.enabled,
            "agent_slugs": list(dict.fromkeys(body.agent_slugs)),
            "require_booking": body.require_booking, "recipient": body.recipient,
            "template_name": (body.template_name or "").strip() or None,
            "variables": variables, "media_id": body.media_id, "media_map": media_map,
            "link_url": link, "approval_mode": body.approval_mode}


@router.get(f"{_P}/playbooks")
def list_playbooks(owner: str = Depends(current_owner)) -> dict:
    return {"playbooks": store.list_playbooks(owner),
            "tokens": list(placeholders.TOKENS)}


@router.post(f"{_P}/playbooks")
def create_playbook(body: PlaybookBody, owner: str = Depends(current_owner)) -> dict:
    return {"playbook": store.save_playbook(owner, _validate_playbook(owner, body))}


@router.put(f"{_P}/playbooks/{{playbook_id}}")
def update_playbook(playbook_id: int, body: PlaybookBody,
                    owner: str = Depends(current_owner)) -> dict:
    if not store.get_playbook(owner, playbook_id):
        raise HTTPException(404, "Playbook not found")
    return {"playbook": store.save_playbook(owner, _validate_playbook(owner, body), playbook_id)}


@router.delete(f"{_P}/playbooks/{{playbook_id}}")
def delete_playbook(playbook_id: int, owner: str = Depends(current_owner)) -> dict:
    if not store.delete_playbook(owner, playbook_id):
        raise HTTPException(404, "Playbook not found")
    return {"ok": True}


class PreviewBody(PlaybookBody):
    call_id: int | None = None


def _preview_call(owner: str, slugs: list[str], call_id: int | None) -> dict | None:
    with get_db() as db:
        if call_id is not None:
            row = db.execute("SELECT * FROM calls WHERE id=? AND owner=?",
                             (call_id, owner)).fetchone()
        elif slugs:
            row = db.execute(
                """SELECT * FROM calls WHERE owner=? AND agent_slug = ANY(?)
                      AND COALESCE(duration_s, 0) > 20
                    ORDER BY id DESC LIMIT 1""", (owner, slugs)).fetchone()
        else:
            row = None
    return dict(row) if row else None


@router.post(f"{_P}/playbooks/preview")
def preview_playbook(body: PreviewBody, owner: str = Depends(current_owner)) -> dict:
    """The message this playbook would send, rendered against a REAL call of
    the chosen agent (the latest one, or the one asked for) — or against a
    sample contact when the agent has no calls yet."""
    pb = _validate_playbook(owner, PlaybookBody(**body.model_dump(exclude={"call_id"})))
    call = _preview_call(owner, pb["agent_slugs"], body.call_id)
    sample = call is None
    if sample:
        call = {"id": None, "owner": owner, "agent_slug": (pb["agent_slugs"] or [None])[0],
                "phone": "", "collected_data": "{}", "language": "en-IN", "conn_id": None}
    from shared.intelligence.pipeline import _agent_yaml
    from shared.phone import significant_digits

    agent = _agent_yaml(call.get("agent_slug"))
    contact = None
    if not sample:
        if call.get("contact_id"):
            contact = store.contact_by_id(owner, int(call["contact_id"]))
        contact = contact or store.contact_by_phone(owner, significant_digits(call.get("phone") or ""))
    else:
        contact = {"name": "Rakesh Shah", "gender": "male"}
    tpl = None
    language = (call.get("language") or "en")[:2]
    if pb["template_name"]:
        language, tpl = stage.pick_language(owner, pb["template_name"], call.get("language"))
    link = f"{config.public_base_url()}/r/aB3dE5fG7h" if pb["link_url"] else ""
    ctx = stage.build_context(call=call, contact=contact, agent=agent, language=language,
                              link=link)
    params, missing = stage.render_params(pb, tpl, ctx, "aB3dE5fG7h" if link else "")
    media_id = stage.resolve_media(pb, ctx.project_name) if (tpl and templates.media_format(tpl)) else None
    media = store.get_media(owner, media_id) if media_id else None
    return {
        "sample": sample,
        "project": ctx.project_name or None,
        "attachment": (media or {}).get("filename"),
        "needs_attachment": bool(tpl and templates.media_format(tpl)) and media is None,
        "call": None if sample else {"id": call["id"], "call_key": call.get("call_key"),
                                     "phone": call.get("phone"),
                                     "contact_name": (contact or {}).get("name")},
        "language": language,
        "params": params,
        "missing": missing,
        "preview": templates.preview(tpl, params) if tpl else None,
        "link_target": placeholders.render_url(pb["link_url"] or "", ctx) or None,
        "template_found": tpl is not None,
    }


# ---------------------------------------------------------------------------
# sends: approvals + activity
# ---------------------------------------------------------------------------

def _send_view(owner: str, s: dict) -> dict:
    t = next((t for t in store.templates_named(owner, s["template_name"])
              if t["language"] == s.get("language")), None)
    return {**s,
            "consent_label": SOURCE_LABELS.get(s.get("consent_source") or "", ""),
            "preview": templates.preview(t, s.get("params") or {}) if t else None,
            "slots": templates.slots(t) if t else []}


@router.get(f"{_P}/sends")
def list_sends(owner: str = Depends(current_owner),
               status: str = Query(default=""),
               playbook_id: int | None = Query(default=None),
               page: int = Query(default=1, ge=1),
               page_size: int = Query(default=25, ge=1, le=100)) -> dict:
    statuses = [s for s in status.split(",") if s in store.SEND_STATUSES] or None
    rows, total = store.list_sends(owner, statuses=statuses, playbook_id=playbook_id,
                                   limit=page_size, offset=(page - 1) * page_size)
    return {"sends": [_send_view(owner, s) for s in rows], "total": total,
            "page": page, "pages": max(1, -(-total // page_size))}


@router.get(f"{_P}/calls/{{call_key}}")
def sends_for_call(call_key: str, owner: str = Depends(current_owner)) -> dict:
    with get_db() as db:
        row = db.execute("SELECT id FROM calls WHERE owner=? AND call_key=?",
                         (owner, call_key)).fetchone()
    if not row:
        raise HTTPException(404, "Call not found")
    return {"sends": [_send_view(owner, s) for s in store.sends_for_call(owner, row["id"])]}


class ParamsBody(BaseModel):
    params: dict[str, str] = Field(default_factory=dict)


def _clean_params(owner: str, send: dict, params: dict[str, str]) -> dict[str, str]:
    allowed = set(send.get("params") or {})
    out = dict(send.get("params") or {})
    for k, v in params.items():
        if k not in allowed:
            raise HTTPException(422, f"Unknown template slot “{k}”.")
        out[k] = placeholders.meta_safe(v)
    return out


@router.patch(f"{_P}/sends/{{send_id}}")
def edit_send(send_id: int, body: ParamsBody, owner: str = Depends(current_owner)) -> dict:
    s = store.get_send(owner, send_id)
    if not s:
        raise HTTPException(404, "Message not found")
    if not store.update_send_params(owner, send_id, _clean_params(owner, s, body.params)):
        raise HTTPException(409, "This message has already been sent, skipped or expired.")
    return {"send": _send_view(owner, store.get_send(owner, send_id))}


def _approve_one(owner: str, send_id: int, params: dict[str, str] | None) -> dict:
    s = store.get_send(owner, send_id)
    if not s:
        raise HTTPException(404, "Message not found")
    if params:
        store.update_send_params(owner, send_id, _clean_params(owner, s, params))
        s = store.get_send(owner, send_id)
    empty = [k for k, v in (s.get("params") or {}).items() if not (v or "").strip()]
    if empty:
        raise HTTPException(422, "Fill in " + ", ".join(empty) + " before approving — "
                                 "WhatsApp refuses a message with an empty field.")
    ttl = int(store.get_account(owner).get("approval_ttl_h") or 48)
    if not store.approve_send(owner, send_id, owner, ttl):
        raise HTTPException(409, "This message is no longer waiting for approval "
                                 "(it was sent, skipped or expired).")
    return store.get_send(owner, send_id)


@router.post(f"{_P}/sends/{{send_id}}/approve")
def approve_send(send_id: int, body: ParamsBody | None = None,
                 owner: str = Depends(current_owner)) -> dict:
    return {"send": _send_view(owner, _approve_one(owner, send_id,
                                                   body.params if body else None))}


class BulkBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)


@router.post(f"{_P}/sends/approve")
def approve_many(body: BulkBody, owner: str = Depends(current_owner)) -> dict:
    ok, failed = [], []
    for sid in body.ids:
        try:
            _approve_one(owner, sid, None)
            ok.append(sid)
        except HTTPException as exc:
            failed.append({"id": sid, "reason": exc.detail})
    return {"approved": ok, "failed": failed}


@router.post(f"{_P}/sends/{{send_id}}/skip")
def skip_send(send_id: int, owner: str = Depends(current_owner)) -> dict:
    if not store.get_send(owner, send_id):
        raise HTTPException(404, "Message not found")
    if not store.skip_send(owner, send_id, owner):
        raise HTTPException(409, "This message is no longer waiting to be sent.")
    return {"ok": True}


@router.get(f"{_P}/replies")
def list_replies(owner: str = Depends(current_owner),
                 page: int = Query(default=1, ge=1),
                 page_size: int = Query(default=25, ge=1, le=100)) -> dict:
    rows, total = store.list_inbound(owner, page_size, (page - 1) * page_size)
    return {"replies": rows, "total": total, "page": page,
            "pages": max(1, -(-total // page_size))}


@router.get(f"{_P}/contacts/{{contact_id}}/consent")
def contact_consent(contact_id: int, owner: str = Depends(current_owner)) -> dict:
    if not store.contact_by_id(owner, contact_id):
        raise HTTPException(404, "Contact not found")
    rows = store.consents_for_contact(owner, contact_id)
    return {"consents": [{**r, "label": SOURCE_LABELS.get(r["source"], r["source"])}
                         for r in rows],
            "current": rows[0]["state"] if rows else None}


# ---------------------------------------------------------------------------
# open routes: Meta webhook + tracked links
# ---------------------------------------------------------------------------

@router.get("/api/whatsapp/webhook")
def webhook_verify(request: Request):
    q = request.query_params
    challenge = webhook.verify_challenge(q.get("hub.mode"), q.get("hub.verify_token"),
                                         q.get("hub.challenge"))
    if challenge is None:
        return PlainTextResponse("forbidden", status_code=403)
    return PlainTextResponse(challenge)


@router.post("/api/whatsapp/webhook")
async def webhook_receive(request: Request):
    if not config.app_secret():
        return JSONResponse({"detail": "webhook not configured"}, status_code=503)
    raw = await request.body()
    if len(raw) > WEBHOOK_MAX_BYTES:
        return JSONResponse({"detail": "too large"}, status_code=413)
    if not webhook.signature_ok(raw, request.headers.get("x-hub-signature-256")):
        return JSONResponse({"detail": "bad signature"}, status_code=401)
    stats = await asyncio.to_thread(webhook.ingest, raw)
    return {"ok": True, **stats}


@router.get("/r/{token}")
def follow_link(token: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", token or ""):
        return PlainTextResponse("This link is not valid.", status_code=404)
    hit = store.follow_link(token)
    if not hit or not re.match(r"^https?://", hit["url"] or ""):
        return PlainTextResponse("This link is not valid.", status_code=404)
    if hit["clicks"] == 1 and hit.get("send_id"):
        s = store.get_send(hit["owner"], int(hit["send_id"]))
        if s and s.get("contact_id"):
            from shared.intelligence import timeline

            timeline.emit(int(s["contact_id"]), hit["owner"], "whatsapp.clicked",
                          ref_table="wa_sends", ref_id=int(s["id"]),
                          payload={"url": hit["url"][:200]})
    return RedirectResponse(hit["url"], status_code=302)


@router.get("/r/f/{token}")
def serve_media(token: str):
    """One uploaded attachment, by its unguessable token — what a BSP fetches
    when a template's header is a document/image (RUNBOOK §80)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,48}", token or ""):
        return PlainTextResponse("This link is not valid.", status_code=404)
    m = store.media_by_public_token(token)
    if not m or not Path(m["path"]).is_file():
        return PlainTextResponse("This link is not valid.", status_code=404)
    safe_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", m["filename"] or "file")
    return FileResponse(m["path"], media_type=m["mime"], filename=safe_name,
                        content_disposition_type="inline",
                        headers={"Cache-Control": "private, max-age=3600"})


@router.post("/api/whatsapp/webhook/interakt/{token}")
async def interakt_webhook_receive(token: str, request: Request):
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,48}", token or ""):
        return JSONResponse({"detail": "not found"}, status_code=404)
    acct = await asyncio.to_thread(store.account_by_webhook_token, token)
    if not acct:
        return JSONResponse({"detail": "not found"}, status_code=404)
    raw = await request.body()
    if len(raw) > WEBHOOK_MAX_BYTES:
        return JSONResponse({"detail": "too large"}, status_code=413)
    blob = await asyncio.to_thread(store.account_webhook_secret_blob, acct["owner"])
    if not blob:
        return JSONResponse({"detail": "webhook secret not configured"}, status_code=503)
    try:
        secret = crypto.decrypt(blob)
    except Exception:
        return JSONResponse({"detail": "webhook secret unreadable"}, status_code=503)
    if not interakt.signature_ok(raw, request.headers.get("interakt-signature"), secret):
        return JSONResponse({"detail": "bad signature"}, status_code=401)
    stats = await asyncio.to_thread(webhook.ingest_interakt, acct["owner"], raw)
    return {"ok": True, **stats}


# ---------------------------------------------------------------------------
# sender loop
# ---------------------------------------------------------------------------

def start_sender() -> None:
    from shared.whatsapp import sender

    log.info("WhatsApp sender ON (sending %s)",
             "ENABLED" if config.send_enabled() else "switched off by WA_SEND_ENABLED")
    asyncio.get_event_loop().create_task(sender.loop())
