"""Intelligence-pipeline stage `whatsapp`: a finished call → WhatsApp sends.

Runs after `notify`, so the extractor's commitments (one of the three consent
signals) already exist. Decision only — nothing here talks to Meta; a send is
a row, and the sender loop in lescall-api delivers it.

Gates, in order:
  1. The tenant has an ENABLED playbook naming this call's agent.
  2. The call is fresh (config.FRESH_WINDOW_S). The sweeper backfills every
     newly shipped stage over history; without this, deploying the feature
     would message months of past callers at once.
  3. The call produced a consent signal (consent.detect). A refusal is
     recorded too — it outranks an older "yes" for that number.
  4. There is a number to send to.

Each (playbook, call) yields at most one send — a database constraint, not
this code's diligence. Approval mode decides the starting status:
auto → queued, manual → pending_approval. A send whose variables cannot all
be filled is held for a person even under auto, with the reason on the row.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from shared.db.database import get_db
from shared.phone import significant_digits, to_e164
from shared.whatsapp import config, consent as consent_mod, placeholders, store, templates

log = logging.getLogger("whatsapp.stage")

_PROJECT_KEYS = ("selected_project", "project", "project_name", "interested_project",
                 "project_preferences", "product", "selected_product")
_VISIT_DATE_KEYS = ("site_visit_date", "visit_date", "appointment_date", "meeting_date")
_VISIT_TIME_KEYS = ("site_visit_time", "visit_time", "appointment_time", "meeting_time")
_NAME_KEYS = ("name", "customer_name", "caller_name", "full_name", "contact_name")


def _is_fresh(call_id: int) -> bool:
    with get_db() as db:
        row = db.execute(
            "SELECT 1 FROM calls WHERE id=? AND created_at > now() - make_interval(secs => ?)",
            (call_id, config.FRESH_WINDOW_S)).fetchone()
    return bool(row)


def _commitments(call_id: int) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute("SELECT kind, quote, made_by FROM commitments WHERE call_id=?",
                          (call_id,)).fetchall()
    return [dict(r) for r in rows]


def _booking(conn_id: str | None) -> dict[str, Any] | None:
    if not conn_id:
        return None
    with get_db() as db:
        row = db.execute(
            "SELECT slot_day, slot_time, appointment_time FROM bookings WHERE conn_id=?"
            " ORDER BY id DESC LIMIT 1", (conn_id,)).fetchone()
    return dict(row) if row else None


def _product_interest(call_id: int) -> str:
    with get_db() as db:
        row = db.execute("SELECT product_interest FROM call_intelligence WHERE call_id=?",
                         (call_id,)).fetchone()
    if not row:
        return ""
    try:
        items = json.loads(row["product_interest"] or "[]")
    except (TypeError, ValueError):
        return ""
    for it in items if isinstance(items, list) else []:
        if isinstance(it, str) and it.strip():
            return it.strip()
    return ""


def _state(call: dict[str, Any]) -> dict[str, Any]:
    try:
        cd = json.loads(call.get("collected_data") or "{}") or {}
    except (TypeError, ValueError):
        return {}
    st = cd.get("state") or {}
    return st if isinstance(st, dict) else {}


def _first(state: dict, keys: tuple[str, ...], *, name_like: bool = False) -> str:
    for k in keys:
        v = state.get(k)
        if v in (None, "") or isinstance(v, (dict, list)):
            continue
        v = str(v).strip()
        if name_like:
            v = placeholders.plausible_name(v)
        elif v.lower() in ("yes", "no", "haan", "nahi", "हाँ", "नहीं", "none"):
            v = ""
        if v:
            return v
    return ""


def _agreed_share_project(call: dict[str, Any]) -> str:
    """RUNBOOK §72: the FIRST project the caller agreed to receive on
    WhatsApp, as the live-call share tracker recorded it
    (collected_data.flags.whatsapp_share_projects). '' for every other call."""
    try:
        cd = json.loads(call.get("collected_data") or "{}") or {}
    except (TypeError, ValueError):
        return ""
    names = (cd.get("flags") or {}).get("whatsapp_share_projects")
    if isinstance(names, list):
        for n in names:
            if isinstance(n, str) and n.strip():
                return n.strip()
    return ""


def _primary_project_name(agent: dict | None) -> str:
    kb = (agent or {}).get("knowledge_base") or {}
    pid = ((kb.get("primary_project") or {}).get("id") or "").strip()
    if not pid:
        return ""
    cat = kb.get("catalog")
    if isinstance(cat, list):
        for item in cat:
            if isinstance(item, dict) and item.get("id") == pid:
                return str(item.get("name") or pid)
    return pid


def build_context(*, call: dict[str, Any], contact: dict[str, Any] | None,
                  agent: dict[str, Any] | None, language: str,
                  link: str = "") -> placeholders.Context:
    state = _state(call)
    identity = (agent or {}).get("identity") or {}
    name = placeholders.plausible_name((contact or {}).get("name")) \
        or _first(state, _NAME_KEYS, name_like=True)
    project = _agreed_share_project(call) or _first(state, _PROJECT_KEYS) \
        or _primary_project_name(agent) \
        or (_product_interest(call["id"]) if call.get("id") else "")
    booking = _booking(call.get("conn_id"))
    visit_date = (booking or {}).get("slot_day") or _first(state, _VISIT_DATE_KEYS)
    visit_time = (booking or {}).get("slot_time") or _first(state, _VISIT_TIME_KEYS)
    return placeholders.Context(
        contact_name=name,
        gender=(contact or {}).get("gender") or "",
        language=language,
        agent_name=str(identity.get("agent_name") or ""),
        company_name=str(identity.get("company_name") or ""),
        project_name=project,
        visit_date=str(visit_date or ""),
        visit_time=str(visit_time or ""),
        link=link,
        slots=state,
    )


def _norm_project(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def resolve_media(pb: dict[str, Any], project: str) -> int | None:
    """The attachment for THIS send: the playbook's per-project file when the
    project the caller agreed to matches one (case/punctuation-blind, and
    "Vedanta" matches "Pramukh Vedanta"), else the playbook's default file.
    None when the playbook has nothing to attach yet."""
    media_map = pb.get("media_map") or {}
    want = _norm_project(project)
    if want and media_map:
        for key, mid in media_map.items():
            k = _norm_project(key)
            if k and (k == want or k in want or want in k):
                return int(mid)
    return int(pb["media_id"]) if pb.get("media_id") else None


def pick_language(owner: str, template_name: str, call_language: str | None) -> tuple[str, dict | None]:
    """The template variant for this call: the call's dominant language when
    an APPROVED variant exists, then English, then any approved one."""
    variants = [t for t in store.templates_named(owner, template_name)
                if (t.get("status") or "").upper() == "APPROVED"]
    want = (call_language or "en").lower()[:2]
    for pref in (want, "en"):
        for t in variants:
            if (t["language"] or "").lower()[:2] == pref:
                return t["language"], t
    if variants:
        return variants[0]["language"], variants[0]
    # Nothing approved (yet): keep the call's language; the sender re-resolves
    # the template when it is time to send and holds the send until then.
    anyv = store.templates_named(owner, template_name)
    for t in anyv:
        if (t["language"] or "").lower()[:2] == want:
            return t["language"], t
    return (anyv[0]["language"], anyv[0]) if anyv else (want, None)


def render_params(pb: dict[str, Any], tpl: dict | None, ctx: placeholders.Context,
                  link_token: str = "") -> tuple[dict[str, str], list[str]]:
    """Slot key → value for this send, plus the keys that came out empty."""
    variables = pb.get("variables") or {}
    keys = templates.text_slot_keys(tpl) if tpl else list(variables.keys())
    ours = templates.link_button_is_ours(tpl, config.public_base_url()) if tpl else {}
    params: dict[str, str] = {}
    missing: list[str] = []
    for key in keys:
        spec = variables.get(key) or {}
        if ours.get(key) and link_token:
            params[key] = link_token
            continue
        value = placeholders.render(str(spec.get("value") or ""), ctx)
        if not value:
            value = placeholders.render(str(spec.get("fallback") or ""), ctx)
        value = placeholders.meta_safe(value)
        params[key] = value
        if not value:
            missing.append(key)
    return params, missing


async def stage_whatsapp(call: dict[str, Any]) -> None:
    owner = call.get("owner") or ""
    slug = call.get("agent_slug") or ""
    if not owner or not slug:
        return
    playbooks = [p for p in store.enabled_playbooks(owner) if slug in (p["agent_slugs"] or [])]
    if not playbooks or not _is_fresh(call["id"]):
        return

    decision = consent_mod.detect(call, _commitments(call["id"]))
    if decision is None:
        return

    dialled = to_e164(call.get("phone") or "")
    contact = None
    if call.get("contact_id"):
        contact = store.contact_by_id(owner, int(call["contact_id"]))
    if contact is None:
        contact = store.contact_by_phone(owner, significant_digits(call.get("phone") or ""))
    contact_id = contact["id"] if contact else None

    consent_phone = decision.given_phone or dialled
    if not consent_phone:
        log.info("wa stage: call %s has consent but no usable number", call["id"])
        return
    consent_id = store.record_consent(
        owner, phone=consent_phone, contact_id=contact_id,
        state="granted" if decision.granted else "revoked",
        source=decision.source or "call_slot", call_id=call["id"],
        quote=decision.quote or decision.declined_reason)
    _timeline(owner, contact_id, "consent.granted" if decision.granted else "consent.revoked",
              "wa_consents", consent_id,
              {"channel": "whatsapp", "quote": (decision.quote or "")[:200],
               "reason": decision.declined_reason or None})
    if not decision.granted:
        # A "no" today withdraws anything still waiting from an earlier "yes".
        store.cancel_open_sends_for_phone(owner, consent_phone,
                                          "caller declined WhatsApp on a later call")
        return

    from shared.intelligence.pipeline import _agent_yaml

    agent = _agent_yaml(slug)
    acct = store.get_account(owner)
    for pb in playbooks:
        try:
            _create_send(owner, call, pb, acct, agent, contact, contact_id, consent_id,
                         dialled, decision)
        except Exception as exc:  # one broken playbook must not block the others
            log.error("wa stage: playbook %s on call %s failed: %r", pb["id"], call["id"], exc)


def _create_send(owner, call, pb, acct, agent, contact, contact_id, consent_id,
                 dialled, decision) -> None:
    to = decision.given_phone if (pb["recipient"] == "given" and decision.given_phone) else dialled
    to = to or decision.given_phone
    if not to or not pb.get("template_name"):
        return
    if pb.get("require_booking") and not _booking(call.get("conn_id")):
        return

    language, tpl = pick_language(owner, pb["template_name"], call.get("language"))
    link_token = store.new_link_token() if pb.get("link_url") else ""
    link = f"{config.public_base_url()}/r/{link_token}" if link_token else ""
    ctx = build_context(call=call, contact=contact, agent=agent, language=language, link=link)
    params, missing = render_params(pb, tpl, ctx, link_token)

    status, reason = "queued", None
    if pb["approval_mode"] != "auto":
        status = "pending_approval"
    if missing:
        status = "pending_approval"
        reason = "fill in before sending: " + ", ".join(missing)

    media_id = resolve_media(pb, ctx.project_name) if (tpl and templates.media_format(tpl)) else None
    send_id = store.create_send(
        owner, playbook_id=pb["id"], call_id=call["id"], contact_id=contact_id,
        consent_id=consent_id, to_phone=to, template_name=pb["template_name"],
        language=language, params=params, status=status, reason=reason,
        delay_min=int(acct.get("send_delay_min") or 0),
        ttl_h=int(acct.get("approval_ttl_h") or 48),
        project=ctx.project_name, media_id=media_id)
    if send_id is None:
        return  # already created on an earlier run of this stage
    if link_token:
        store.create_link(owner, send_id, placeholders.render_url(pb["link_url"], ctx),
                          token=link_token)
    _timeline(owner, contact_id,
              "whatsapp.awaiting_approval" if status == "pending_approval" else "whatsapp.queued",
              "wa_sends", send_id,
              {"playbook": pb["name"], "template": pb["template_name"], "to": to})
    if status == "pending_approval":
        from shared.intelligence.notify import _emit

        who = (contact or {}).get("name") or to
        _emit(owner, "whatsapp.approval", "info",
              f"WhatsApp to {who} is waiting for approval",
              f"{pb['name']} — {reason or 'approve or skip it'}",
              "/integrations/whatsapp?tab=approvals", f"wa:send:{send_id}")


def _timeline(owner: str, contact_id: int | None, kind: str, ref_table: str,
              ref_id: int | None, payload: dict) -> None:
    if not contact_id or not ref_id:
        return
    from shared.intelligence import timeline

    timeline.emit(contact_id, owner, kind, ref_table=ref_table, ref_id=ref_id,
                  payload=payload)
