"""Deliver queued WhatsApp sends. Runs as a loop inside lescall-api.

Every guard is re-checked HERE, at the moment of sending, not only when the
send was created — a lot can change in the minutes or hours between a call
ending and its message leaving:

  kill switch     WA_SEND_ENABLED off → nothing is claimed at all
  connection      tenant disconnected → held, retried later
  canary          WA_CANARY_PHONES set and this number not on it → blocked
  consent         the newest consent for this number is a revocation
                  (STOP reply, or a later call saying no) → blocked
  send window     outside the tenant's hours (IST) → deferred to the next opening
  daily cap       reached → deferred to tomorrow's window
  template        not APPROVED (yet) → held
  attachment      template needs a file the playbook does not have yet → held,
                  and picked up the moment one is attached (a brochure uploaded
                  an hour after the call still reaches the caller)

A deferral never counts as an attempt; only a real request to the provider
does. Expiry (store.expire_due) is what finally ends a send that can never
leave.

Two providers, one path (RUNBOOK §80): `wa_accounts.provider` says whether the
message goes to Meta's Graph API (media uploaded first, components JSON) or to
Interakt (media as a public /r/f/ URL, positional values). Everything before
that line is identical.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

from shared.whatsapp import config, crypto, graph, interakt, store, templates

log = logging.getLogger("whatsapp.sender")
IST = ZoneInfo("Asia/Kolkata")


def next_window_start(now: dt.datetime, start_h: int, end_h: int) -> dt.datetime | None:
    """None when `now` is inside the window, else when it next opens."""
    local = now.astimezone(IST)
    if start_h <= local.hour < end_h:
        return None
    opening = local.replace(hour=start_h, minute=0, second=0, microsecond=0)
    if local.hour >= end_h:
        opening += dt.timedelta(days=1)
    return opening


def _tomorrow_opening(now: dt.datetime, start_h: int) -> dt.datetime:
    local = now.astimezone(IST)
    return (local + dt.timedelta(days=1)).replace(hour=start_h, minute=0, second=0,
                                                  microsecond=0)


async def run_once() -> int:
    """One pass. Returns how many messages actually left."""
    await asyncio.to_thread(store.expire_due)
    await asyncio.to_thread(store.release_sending)
    if not config.send_enabled():
        return 0
    due = await asyncio.to_thread(store.claim_due, config.SENDER_BATCH)
    sent = 0
    for s in due:
        try:
            if await deliver(s):
                sent += 1
        except Exception as exc:  # never leave a row stuck in 'sending'
            log.error("wa send %s crashed: %r", s["id"], exc, exc_info=True)
            await asyncio.to_thread(
                store.defer_send, s["id"],
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10),
                f"internal error, retrying: {exc!r}"[:300])
    return sent


def _defer(s: dict, minutes: float | None = None, *, until: dt.datetime | None = None,
           reason: str) -> bool:
    when = until or dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes or 15)
    store.defer_send(s["id"], when, reason)
    return False


async def deliver(s: dict) -> bool:
    owner = s["owner"]
    acct = await asyncio.to_thread(store.get_account, owner)
    now = dt.datetime.now(dt.timezone.utc)

    if acct.get("status") != "connected" or not acct.get("has_token"):
        return await asyncio.to_thread(
            _defer, s, 30, reason="WhatsApp is not connected for this account")

    canary = config.canary_phones()
    if canary and s["to_phone"][-10:] not in canary:
        await asyncio.to_thread(
            store.mark_final, s["id"], "blocked",
            "canary mode: only the platform's test numbers receive messages")
        return False

    latest = await asyncio.to_thread(store.latest_consent, owner, s["to_phone"])
    if latest and latest["state"] == "revoked":
        await asyncio.to_thread(store.mark_final, s["id"], "blocked",
                                "this person opted out of WhatsApp messages")
        return False

    opening = next_window_start(now, int(acct["send_window_start"]),
                                int(acct["send_window_end"]))
    if opening is not None:
        return await asyncio.to_thread(
            _defer, s, until=opening,
            reason=f"waiting for sending hours ({acct['send_window_start']:02d}:00–"
                   f"{acct['send_window_end']:02d}:00 IST)")

    if await asyncio.to_thread(store.sent_today, owner) >= int(acct["daily_cap"]):
        return await asyncio.to_thread(
            _defer, s, until=_tomorrow_opening(now, int(acct["send_window_start"])),
            reason=f"daily limit of {acct['daily_cap']} messages reached")

    provider = (acct.get("provider") or "meta").lower()
    label = "Interakt" if provider == "interakt" else "Meta"
    variants = await asyncio.to_thread(store.templates_named, owner, s["template_name"])
    tpl = next((t for t in variants if t["language"] == s["language"]
                and (t.get("status") or "").upper() == "APPROVED"), None)
    if tpl is None:
        return await asyncio.to_thread(
            _defer, s, 30,
            reason=f"template “{s['template_name']}” ({s['language']}) is not approved "
                   f"by {label} yet")

    token = crypto.decrypt(await asyncio.to_thread(store.account_token_blob, owner))
    try:
        media = None
        if templates.media_format(tpl):
            media = await _attachment(owner, s, acct, token, provider)
            if media is None:
                what = f" for {s['project']}" if s.get("project") else ""
                return await asyncio.to_thread(
                    _defer, s, 30,
                    reason=f"waiting for the attachment{what}: upload the brochure to the "
                           "playbook and this message goes out on its own")
        if provider == "interakt":
            payload = interakt.build_template(
                tpl, s["params"] or {}, media_url=(media or {}).get("link") or "",
                file_name=(media or {}).get("filename") or "")
            wamid = await interakt.send_template(
                token, to=s["to_phone"], template=payload, callback_data=f"wa_send:{s['id']}")
        else:
            components = templates.build_components(tpl, s["params"] or {}, media)
            wamid = await graph.send_template(
                acct["phone_number_id"], token, to=s["to_phone"], name=s["template_name"],
                language=s["language"], components=components)
    except graph.GraphError as exc:
        return await asyncio.to_thread(_failed, s, exc, label)
    except ValueError as exc:
        await asyncio.to_thread(store.mark_final, s["id"], "failed", str(exc))
        return False

    await asyncio.to_thread(store.mark_sent, s["id"], wamid)
    if s.get("contact_id"):
        from shared.intelligence import timeline

        await asyncio.to_thread(
            timeline.emit, int(s["contact_id"]), owner, "whatsapp.sent",
            ref_table="wa_sends", ref_id=int(s["id"]),
            payload={"template": s["template_name"], "to": s["to_phone"]})
    log.info("wa send %s → %s (%s) sent as %s", s["id"], s["to_phone"],
             s["template_name"], wamid)
    return True


def _failed(s: dict, exc: graph.GraphError, label: str = "Meta") -> bool:
    attempts = int(s.get("attempts") or 1)
    if exc.transient and attempts < config.MAX_ATTEMPTS:
        store.defer_send(s["id"],
                         dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=2 ** attempts),
                         f"{label}: {exc.as_text()} — retrying"[:300], undo_attempt=False)
        return False
    # Meta 190 = token expired/revoked; Interakt 401 = API key rejected. Either
    # way every send from this account will fail until someone reconnects.
    if exc.code == 190 or exc.http_status == 401:
        store.set_account_health(
            s["owner"], error=f"{label} rejected the account's credential: {exc}. "
                              "Reconnect in Setup.")
    store.mark_final(s["id"], "failed", f"{label}: {exc}"[:500],
                     error_code=str(exc.code) if exc.code else None)
    if s.get("contact_id"):
        from shared.intelligence import timeline

        timeline.emit(int(s["contact_id"]), s["owner"], "whatsapp.failed",
                      ref_table="wa_sends", ref_id=int(s["id"]),
                      payload={"template": s["template_name"], "error": str(exc)[:200]})
    return False


async def _attachment(owner: str, s: dict, acct: dict, token: str,
                      provider: str) -> dict | None:
    """The file this send carries, resolved NOW: the one the stage picked, or
    — when the playbook had nothing at that moment — whatever the playbook has
    since been given for this send's project. None = still nothing."""
    from shared.whatsapp.stage import resolve_media

    media_id = s.get("media_id")
    if not media_id:
        pb = await asyncio.to_thread(store.get_playbook, owner, s["playbook_id"])
        media_id = resolve_media(pb, s.get("project") or "") if pb else None
        if media_id:
            await asyncio.to_thread(store.set_send_media, s["id"], media_id)
    if not media_id:
        return None
    m = await asyncio.to_thread(store.get_media, owner, int(media_id))
    if not m or not Path(m["path"]).exists():
        return None
    if provider == "interakt":
        tok = m.get("public_token") or await asyncio.to_thread(store.ensure_media_token, m["id"])
        return {"link": config.media_public_url(tok), "filename": m["filename"]}
    if not (m.get("meta_media_id") and m.get("meta_fresh")):
        mid = await graph.upload_media(acct["phone_number_id"], token, Path(m["path"]), m["mime"])
        await asyncio.to_thread(store.set_media_meta_id, m["id"], mid)
        m["meta_media_id"] = mid
    return {"id": m["meta_media_id"], "filename": m["filename"]}


async def loop() -> None:
    try:
        n = await asyncio.to_thread(store.release_sending)
        if n:
            log.warning("wa sender: re-queued %d send(s) stranded by a restart", n)
    except Exception as exc:
        log.error("wa sender: stranded-row sweep failed: %r", exc)
    while True:
        try:
            n = await run_once()
            if n:
                log.info("wa sender: %d message(s) sent", n)
        except Exception as exc:
            log.error("wa sender pass failed: %r", exc)
        await asyncio.sleep(config.SENDER_POLL_S)
