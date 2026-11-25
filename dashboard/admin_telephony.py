"""Admin console — the telephony plane.

The operator's controls over which account owns which number, which of that
account's agents answers it, and whether it is in service at all. The tenant's
own Telephony panel can do none of this: it can only ever touch resources the
tenant already owns, and it is refused outright on an unpaid plan.

Everything here goes through `shared/telephony_admin.py` — the same functions
the JSON API in `admin_app.py` calls — so the two surfaces cannot drift. In
particular both enforce the two rules that make an admin override safe:

  * the receiving account must be entitled to telephony (`telephony_allowed`),
    because a number on an unentitled account answers calls the billing layer
    then refuses to meter;
  * a number may only be bound to agents in ITS OWN account, because the
    inbound resolver trusts the binding and does not re-check ownership.

Path is `/admin/telephony`, deliberately not under `/admin/api` — that prefix
is the bearer-only JSON namespace and a cookie must never drive it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from admin_ui import (_check_csrf, _csrf_field, _flash, e, num, page, pill, when)

logger = logging.getLogger("lescall.admin.telephony")

telephony = APIRouter(prefix="/admin/telephony")


def _back(owner: str, kind: str, text: str) -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse(
        f"/admin/telephony/{quote(owner)}?flash={kind}&msg={quote(text)}",
        status_code=303)


def _guard(request: Request, csrf: str, reason: str = "", min_len: int = 0) -> str:
    from admin_app import require_token
    require_token(request, allow_cookie_write=True)
    _check_csrf(request, csrf)
    reason = (reason or "").strip()
    if min_len and len(reason) < min_len:
        from fastapi import HTTPException
        raise HTTPException(status_code=400,
                            detail=f"a reason of at least {min_len} characters is required")
    return reason[:500]


def _state_pill(row: dict) -> str:
    st = (row.get("config_status") or "pending").lower()
    if st == "applied":
        return pill("in service", "mint")
    if st == "disconnected":
        return pill("out of service", "rose")
    if st == "error":
        return pill("apply failed", "rose")
    return pill("not connected", "amber")


# ---------------------------------------------------------------------------
# platform overview
# ---------------------------------------------------------------------------

@telephony.get("", response_class=HTMLResponse)
@telephony.get("/", response_class=HTMLResponse)
def overview(request: Request):
    from admin_app import audit, require_token
    require_token(request)
    audit(request, "ui.telephony.overview")
    from shared.db.database import get_db
    from shared.telephony_admin import subscription_state

    with get_db() as db:
        numbers = [dict(r) for r in db.execute(
            "SELECT p.*, t.name AS trunk_name, t.owner AS trunk_owner, "
            "       t.config_status AS trunk_status "
            "FROM phone_numbers p LEFT JOIN trunks t ON t.id = p.trunk_id "
            "ORDER BY p.owner, p.id").fetchall()]
        orphan_trunks = [dict(r) for r in db.execute(
            "SELECT t.id, t.name, t.owner, t.config_status FROM trunks t "
            "WHERE NOT EXISTS (SELECT 1 FROM phone_numbers p WHERE p.trunk_id = t.id) "
            "ORDER BY t.id").fetchall()]

    rows = []
    for n in numbers:
        state = subscription_state(n["owner"] or "")
        mismatch = (n.get("trunk_owner") or "") and n["trunk_owner"] != n["owner"]
        rows.append(
            f"<tr><td><b>{e(n['number'])}</b></td>"
            f"<td><a href='/admin/telephony/{e(n['owner'] or '')}'>{e(n['owner'] or '—')}</a>"
            + ("" if state["ok"] else f"<div class=small>{pill('not entitled', 'rose')}</div>")
            + f"</td><td class=small><code>{e(n['inbound_agent_slug'] or '—')}</code></td>"
            f"<td class=small><code>{e(n['outbound_agent_slug'] or '—')}</code></td>"
            f"<td class=small><code>{e(n.get('trunk_name') or '—')}</code>"
            + (f"<div class=small>{pill('owned by ' + str(n['trunk_owner']), 'rose')}</div>"
               if mismatch else "")
            + f"</td><td>{_state_pill(n)}</td>"
            f"<td class=small>{when(n.get('last_applied_at'), '%d %b %Y')}</td>"
            f"<td class=small><a href='/admin/telephony/{e(n['owner'] or '')}'>manage</a></td></tr>")

    orphan_rows = "".join(
        f"<tr><td><code>{e(t['name'])}</code></td><td>{e(t['owner'] or '—')}</td>"
        f"<td>{pill(t['config_status'] or '—')}</td>"
        f"<td class=small><a href='/admin/telephony/{e(t['owner'] or '')}'>manage</a></td></tr>"
        for t in orphan_trunks)

    body = f"""
<h1>Telephony</h1>
<p class="sub">Every number on the platform, who owns it, and whether it is
  answering calls. Pick an account to add, move, connect or rebind a number.</p>
<div class="card">
  <h2>Numbers ({len(numbers)})</h2>
  <table>
    <thead><tr><th>Number</th><th>Account</th><th>Inbound agent</th>
      <th>Outbound agent</th><th>Trunk</th><th>State</th><th>Applied</th><th></th></tr></thead>
    <tbody>{''.join(rows) or '<tr><td colspan=8 class=muted>No numbers registered.</td></tr>'}</tbody>
  </table>
</div>
<div class="card">
  <h2>Trunks with no number</h2>
  <p class="small muted" style="margin:-.3rem 0 .6rem">A carrier connection nobody
    is using. Usually a half-finished setup, or a number that was removed and left
    its trunk behind.</p>
  <table>
    <thead><tr><th>Trunk</th><th>Account</th><th>State</th><th></th></tr></thead>
    <tbody>{orphan_rows or '<tr><td colspan=4 class=muted>None.</td></tr>'}</tbody>
  </table>
</div>"""
    return page("Telephony", body, _flash(request))


# ---------------------------------------------------------------------------
# per-account control surface
# ---------------------------------------------------------------------------

@telephony.get("/{owner}", response_class=HTMLResponse)
def tenant_telephony(owner: str, request: Request):
    from admin_app import audit, require_token
    require_token(request)
    audit(request, "ui.telephony.tenant", owner)
    from shared.db.database import get_db
    from shared.telephony_admin import (subscription_state, tenant_agents,
                                        tenant_numbers, tenant_trunks)

    owner = (owner or "").strip().lower()
    state = subscription_state(owner)
    numbers = tenant_numbers(owner)
    trunks = tenant_trunks(owner)
    agents = tenant_agents(owner)
    csrf = _csrf_field(request)

    with get_db() as db:
        others = [r["email"] for r in db.execute(
            "SELECT email FROM users WHERE lower(email) <> ? ORDER BY email",
            (owner,)).fetchall()]

    def agent_select(name: str, current: str, want: str) -> str:
        opts = [f'<option value=""{" selected" if not current else ""}>— none —</option>']
        for a in agents:
            sel = " selected" if a["slug"] == current else ""
            tail = f" · built for {a['direction']}" if a["direction"] and a["direction"] != want else ""
            opts.append(f'<option value="{e(a["slug"])}"{sel}>{e(a["display_name"])} '
                        f'({e(a["slug"])}){e(tail)}</option>')
        # A binding that points at an agent this account no longer owns must stay
        # visible, or "save" would silently drop it without the operator seeing why.
        if current and not any(a["slug"] == current for a in agents):
            opts.append(f'<option value="{e(current)}" selected>{e(current)} '
                        f'— NOT in this account</option>')
        return f'<select name="{name}" style="width:auto;min-width:15rem">{"".join(opts)}</select>'

    gate = ""
    if not state["ok"]:
        gate = f"""<div class="flash warn" style="margin-bottom:1.1rem">
          <b>No telephony entitlement.</b> {e(state['reason'])}
          <div class="small" style="margin-top:.3rem">Existing numbers are shown and can
          be disconnected or moved away, but nothing can be added or connected until this
          is resolved. <a href="/admin/tenant/{e(owner)}">Change the plan →</a></div></div>"""

    # ---- per-number cards ---------------------------------------------
    cards = []
    for n in numbers:
        connected = (n.get("config_status") or "") == "applied"
        owner_opts = "".join(f'<option value="{e(o)}">{e(o)}</option>' for o in others)
        cards.append(f"""
<div class="card">
  <h2>{e(n['number'])} &nbsp;{_state_pill(n)}</h2>
  <dl style="margin-bottom:.8rem">
    <dt>Trunk</dt><dd><code>{e(n.get('trunk_name') or '— none —')}</code>
      {'' if (n.get('trunk_owner') or owner) == owner else pill('owned by ' + str(n.get('trunk_owner')), 'rose')}</dd>
    <dt>Direction</dt><dd>{e(n.get('direction') or 'both')}</dd>
    <dt>Last applied</dt><dd>{when(n.get('last_applied_at'))}</dd>
  </dl>

  <form class="act" method=post action="/admin/telephony/{e(owner)}/number/{n['id']}/bind">
    {csrf}
    <label>Inbound agent — answers calls TO this number</label>
    {agent_select('inbound_agent_slug', n.get('inbound_agent_slug') or '', 'inbound')}
    <label>Outbound agent — speaks on calls FROM this number</label>
    {agent_select('outbound_agent_slug', n.get('outbound_agent_slug') or '', 'outbound')}
    <label>Direction</label>
    <select name=direction style="width:auto">
      {''.join(f'<option value="{d}"{" selected" if (n.get("direction") or "both") == d else ""}>{d}</option>' for d in ('both', 'inbound', 'outbound'))}
    </select>
    <button type=submit>Save agent binding</button>
  </form>
  <p class="small muted" style="margin:.5rem 0 0">Only agents in this account are
    listed — the inbound resolver trusts the binding, so pointing a number at
    another tenant's agent would have it answer in that tenant's name.</p>

  <hr style="border:0;border-top:1px solid #eef0f3;margin:1rem 0">
  <div style="display:flex;gap:.6rem;flex-wrap:wrap;align-items:flex-start">
    <form class="act" method=post
          action="/admin/telephony/{e(owner)}/number/{n['id']}/{'disconnect' if connected else 'connect'}">
      {csrf}<button type=submit class="{'ghost' if connected else ''}" style="margin-top:0">
        {'Disconnect' if connected else 'Connect'}</button></form>
    <form class="act" method=post action="/admin/telephony/{e(owner)}/number/{n['id']}/remove">
      {csrf}<input name=reason placeholder="reason to remove" minlength=10 required
        style="width:12rem;display:inline-block">
      <button type=submit class=danger style="margin-top:0">Remove</button></form>
  </div>
  <p class="small muted" style="margin:.5rem 0 0">
    <b>Disconnect</b> deletes the DID's FreeSWITCH dialplan so it stops answering,
    and keeps the row, the trunk and the bindings — reconnect is one click.
    <b>Remove</b> deletes both.</p>

  <hr style="border:0;border-top:1px solid #eef0f3;margin:1rem 0">
  <form class="act" method=post action="/admin/telephony/{e(owner)}/number/{n['id']}/test-call">
    {csrf}
    <label>Prove it works — ring a phone FROM this number</label>
    <input name=destination placeholder="919876543210 (with country code)"
           style="width:16rem;display:inline-block">
    <button type=submit class=ghost style="margin-top:.5rem">Place test call</button>
  </form>
  <p class="small muted" style="margin:.5rem 0 0">
    Dials out over this number's own trunk with its outbound agent live, so it
    proves the whole path — caller ID, carrier, gateway, agent. <b>This is a real
    call</b>: it costs money and counts against {e(owner)}'s quota. It is also the
    only way to tell "connected" apart from "the carrier is actually sending us
    this number" — Connect cannot know the difference.</p>

  <hr style="border:0;border-top:1px solid #eef0f3;margin:1rem 0">
  <form class="act" method=post action="/admin/telephony/{e(owner)}/number/{n['id']}/transfer">
    {csrf}
    <label>Move this number to another account</label>
    <select name=new_owner style="width:auto;min-width:18rem"><option value="">— choose —</option>{owner_opts}</select>
    <label style="margin-top:.5rem"><input type=checkbox name=move_trunk value=1 checked
      style="width:auto;margin-right:.4rem">Move its trunk too (required for outbound calls)</label>
    <label><input type=checkbox name=move_agents value=1 checked
      style="width:auto;margin-right:.4rem">Move the agents bound to it</label>
    <label>Reason (audited, min 10 chars)</label>
    <input name=reason required minlength=10 placeholder="e.g. account handover requested by the customer">
    <button type=submit class=ghost>Transfer number</button>
  </form>
</div>""")

    # ---- add a number -------------------------------------------------
    trunk_opts = "".join(
        f'<option value="{t["id"]}">{e(t["name"])} · {e(t["sip_domain"])}</option>'
        for t in trunks)
    add_number = f"""
<div class="card">
  <h2>Add a number to this account</h2>
  {'<p class="small muted">This account has no carrier trunk yet — create one below first.</p>'
     if not trunks else ''}
  <form class="act" method=post action="/admin/telephony/{e(owner)}/numbers">
    {csrf}
    <label>Number (as the carrier presents it)</label>
    <input name=number placeholder="+91 80 3133 9140" required>
    <label>Carrier trunk</label>
    <select name=trunk_id required>{trunk_opts or '<option value="">— no trunk on this account —</option>'}</select>
    <label>Inbound agent</label>{agent_select('inbound_agent_slug', '', 'inbound')}
    <label>Outbound agent</label>{agent_select('outbound_agent_slug', '', 'outbound')}
    <label>Direction</label>
    <select name=direction style="width:auto">
      <option value=both selected>both</option><option value=inbound>inbound</option>
      <option value=outbound>outbound</option></select>
    <label>Label</label>
    <input name=label placeholder="e.g. main line">
    <label>Reason (audited, min 10 chars)</label>
    <input name=reason required minlength=10 placeholder="why this account gets this number">
    <button type=submit{' disabled' if not trunks or not state['ok'] else ''}>Add number</button>
  </form>
  <p class="small muted" style="margin-top:.6rem">Added out of service. Press
    <b>Connect</b> on its card to write the FreeSWITCH config and put it live.</p>
</div>"""

    # ---- trunks -------------------------------------------------------
    trunk_rows = "".join(
        f"<tr><td><code>{e(t['name'])}</code></td><td class=small>{e(t['sip_domain'])}</td>"
        f"<td class=small>{e(t['caller_id'] or '—')}</td><td>{pill(t['config_status'] or '—')}</td>"
        f"<td class=small><form class=act method=post "
        f"action=\"/admin/telephony/{e(owner)}/trunk/{t['id']}/delete\">{csrf}"
        f"<input name=reason placeholder=reason minlength=10 required style='width:8rem'>"
        f"<button type=submit class=danger style='margin-top:.3rem'>Delete</button>"
        f"</form></td></tr>" for t in trunks)
    trunk_card = f"""
<div class="card">
  <h2>Carrier trunks</h2>
  <table>
    <thead><tr><th>Name</th><th>SIP domain</th><th>Caller ID</th><th>State</th><th></th></tr></thead>
    <tbody>{trunk_rows or '<tr><td colspan=5 class=muted>No trunk on this account.</td></tr>'}</tbody>
  </table>
  <hr style="border:0;border-top:1px solid #eef0f3;margin:1rem 0">
  <form class="act" method=post action="/admin/telephony/{e(owner)}/trunks">
    {csrf}
    <label>Short name — the account prefix is added automatically</label>
    <input name=name placeholder="plivo-339140" required pattern="[A-Za-z0-9_-]{{1,64}}">
    <label>SIP domain</label>
    <input name=sip_domain placeholder="86116159722292477.zt.plivo.com" required>
    <label>Caller ID</label>
    <input name=caller_id placeholder="+91 80 3133 9140">
    <label>Carrier signalling IPs (comma separated CIDRs)</label>
    <input name=provider_ips placeholder="103.21.244.0/22">
    <label>Reason (audited, min 10 chars)</label>
    <input name=reason required minlength=10 placeholder="why this account needs a trunk">
    <button type=submit{' disabled' if not state['ok'] else ''}>Create trunk</button>
  </form>
</div>"""

    # ---- what the console CANNOT do ------------------------------------
    #
    # Adding a DID is not self-contained and no form here can make it so. This
    # console owns the platform half (trunk row → gateway XML, number row →
    # dialplan XML, agent bindings); steps 1-3 below happen in the carrier's
    # own console. Miss any of them and Connect still reports `applied` —
    # FreeSWITCH is correctly configured to answer a call that never arrives.
    # Saying so here is the difference between "ready" and "working".
    import os
    sip_ip = os.environ.get("PUBLIC_SIP_IP", "35.207.225.237")
    sip_port = os.environ.get("PUBLIC_SIP_PORT", "5060")
    domains = sorted({t["sip_domain"] for t in trunks if t.get("sip_domain")})
    carrier = f"""
<div class="card">
  <h2>Before a NEW number will work</h2>
  <p class="small muted" style="margin:-.3rem 0 .7rem">
    This page only configures our side. A number that has never been used
    before also needs three things done in the carrier's own console — and
    until they are, <b>Connect still reports “in service”</b>: FreeSWITCH is
    correctly set up to answer a call that will never arrive.</p>
  <ol class="small" style="margin:0 0 .8rem 1.1rem;padding:0">
    <li>The number is <b>owned</b> in the carrier account (Plivo, Twilio…).</li>
    <li>It is <b>attached to the trunk</b> whose SIP domain is listed below —
        not just to the account.</li>
    <li>That trunk's <b>origination URI points here</b>:
        <code>sip:{e(sip_ip)}:{e(sip_port)}</code> over UDP.</li>
  </ol>
  <dl>
    <dt>Send SIP to</dt><dd><code>sip:{e(sip_ip)}:{e(sip_port)}</code> · udp</dd>
    <dt>This account's SIP domain{'s' if len(domains) != 1 else ''}</dt>
    <dd>{'<br>'.join(f'<code>{e(d)}</code>' for d in domains) or '<span class="muted">— no trunk yet —</span>'}</dd>
    <dt>Media (RTP)</dt><dd>flows directly between the carrier and this server</dd>
  </dl>
  <p class="small muted" style="margin:.6rem 0 0">
    Outbound caller ID is authorised by the <b>carrier</b>, not by us — we egress
    through this account's own gateway, so the carrier rejects any caller ID the
    account does not own. There is no “verify this number” step here by design.</p>
</div>"""

    body = f"""
<h1>{e(owner)} · telephony</h1>
<p class="sub"><a href="/admin/telephony">← all numbers</a> ·
  <a href="/admin/tenant/{e(owner)}">account &amp; billing</a></p>
{gate}
<div class="grid side">
  <div>{''.join(cards) or '<div class="card"><h2>No numbers</h2><p class="muted small">This account has no number yet. Add one on the right.</p></div>'}</div>
  <div>{add_number}{carrier}{trunk_card}</div>
</div>"""
    return page(f"{owner} · telephony", body, _flash(request))


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------

def _run(owner: str, request: Request, action: str, fn, meta: dict):
    """Call one telephony_admin function, audit it, and flash the outcome."""
    from admin_app import audit
    from shared.telephony_admin import TelephonyAdminError
    try:
        result = fn()
    except TelephonyAdminError as exc:
        audit(request, f"telephony.{action}.refused", owner, {**meta, "error": str(exc)})
        return _back(owner, "err", str(exc))
    except Exception as exc:                        # pragma: no cover - defensive
        logger.exception("[admin-tel] %s failed", action)
        audit(request, f"telephony.{action}.failed", owner, {**meta, "error": repr(exc)})
        return _back(owner, "err", f"{action} failed: {exc!r}"[:280])
    audit(request, f"telephony.{action}", owner, {**meta, "result": result})
    return result


@telephony.post("/{owner}/number/{number_id}/bind")
def ui_bind(owner: str, number_id: int, request: Request, csrf: str = Form(""),
            inbound_agent_slug: str = Form(""), outbound_agent_slug: str = Form(""),
            direction: str = Form("both")):
    _guard(request, csrf)
    from shared.telephony_admin import bind_number
    out = _run(owner, request, "bind", lambda: bind_number(
        number_id, inbound_agent_slug=inbound_agent_slug,
        outbound_agent_slug=outbound_agent_slug, direction=direction,
        actor="admin-ui"), {"number_id": number_id})
    if isinstance(out, RedirectResponse):
        return out
    warn = (" " + " ".join(out["warnings"])) if out.get("warnings") else ""
    return _back(owner, "ok", f"Binding saved — live on the next call.{warn}")


@telephony.post("/{owner}/number/{number_id}/connect")
def ui_connect(owner: str, number_id: int, request: Request, csrf: str = Form("")):
    _guard(request, csrf)
    from shared.telephony_admin import connect_number
    out = _run(owner, request, "connect", lambda: connect_number(number_id, "admin-ui"),
               {"number_id": number_id})
    if isinstance(out, RedirectResponse):
        return out
    if out.get("status") != "applied":
        return _back(owner, "err", "Config written but FreeSWITCH did not reload — "
                                   "the number is NOT in service. Check the FS logs.")
    return _back(owner, "ok", f"{out['number']} is in service.")


@telephony.post("/{owner}/number/{number_id}/disconnect")
def ui_disconnect(owner: str, number_id: int, request: Request, csrf: str = Form("")):
    _guard(request, csrf)
    from shared.telephony_admin import disconnect_number
    out = _run(owner, request, "disconnect",
               lambda: disconnect_number(number_id, "admin-ui"), {"number_id": number_id})
    if isinstance(out, RedirectResponse):
        return out
    return _back(owner, "ok", f"{out['number']} is out of service. Its setup is kept — "
                              f"press Connect to restore it.")


@telephony.post("/{owner}/number/{number_id}/test-call")
def ui_test_call(owner: str, number_id: int, request: Request, csrf: str = Form(""),
                 destination: str = Form("")):
    _guard(request, csrf)
    from shared.telephony_admin import test_call
    out = _run(owner, request, "number.test_call",
               lambda: test_call(number_id, destination, actor="admin-ui"),
               {"number_id": number_id, "destination": destination})
    if isinstance(out, RedirectResponse):
        return out
    if not out["ok"]:
        return _back(owner, "err",
                     f"FreeSWITCH refused the call: {out['response']}. That usually "
                     f"means the carrier is not routing this number to us yet.")
    return _back(owner, "ok",
                 f"Ringing {out['destination']} from {out['number']} with "
                 f"{out['agent_slug']} — answer it to hear the agent.")


@telephony.post("/{owner}/number/{number_id}/remove")
def ui_remove(owner: str, number_id: int, request: Request, csrf: str = Form(""),
              reason: str = Form("")):
    reason = _guard(request, csrf, reason, min_len=10)
    from shared.telephony_admin import remove_number
    out = _run(owner, request, "remove", lambda: remove_number(number_id, "admin-ui"),
               {"number_id": number_id, "reason": reason})
    if isinstance(out, RedirectResponse):
        return out
    return _back(owner, "ok", f"{out['number']} removed, dialplan deleted.")


@telephony.post("/{owner}/number/{number_id}/transfer")
def ui_transfer(owner: str, number_id: int, request: Request, csrf: str = Form(""),
                new_owner: str = Form(""), reason: str = Form(""),
                move_trunk: str = Form(""), move_agents: str = Form("")):
    reason = _guard(request, csrf, reason, min_len=10)
    if not (new_owner or "").strip():
        return _back(owner, "err", "Choose the account to move the number to.")
    from shared.telephony_admin import transfer_number
    out = _run(owner, request, "transfer", lambda: transfer_number(
        number_id, new_owner, move_trunk=bool(move_trunk), move_agents=bool(move_agents),
        actor="admin-ui", reason=reason),
        {"number_id": number_id, "to": new_owner, "reason": reason})
    if isinstance(out, RedirectResponse):
        return out
    bits = [f"{out['number']} moved to {out['to']}"]
    if out.get("trunk"):
        bits.append(f"trunk {out['trunk']['was']} → {out['trunk']['now']}")
    if out.get("agents"):
        bits.append("agents " + ", ".join(out["agents"]))
    return _back(new_owner.strip().lower(), "ok", "; ".join(bits) + ".")


@telephony.post("/{owner}/numbers")
def ui_add_number(owner: str, request: Request, csrf: str = Form(""),
                  number: str = Form(""), trunk_id: str = Form(""),
                  inbound_agent_slug: str = Form(""), outbound_agent_slug: str = Form(""),
                  direction: str = Form("both"), label: str = Form(""),
                  reason: str = Form("")):
    reason = _guard(request, csrf, reason, min_len=10)
    try:
        tid = int(trunk_id)
    except (TypeError, ValueError):
        return _back(owner, "err", "Pick the carrier trunk this number arrives on.")
    from shared.telephony_admin import create_number
    out = _run(owner, request, "number.add", lambda: create_number(
        owner, number, tid, inbound_agent_slug=inbound_agent_slug,
        outbound_agent_slug=outbound_agent_slug, direction=direction, label=label,
        actor="admin-ui"), {"number": number, "reason": reason})
    if isinstance(out, RedirectResponse):
        return out
    warn = (" " + " ".join(out["warnings"])) if out.get("warnings") else ""
    return _back(owner, "ok", f"{out['number']['number']} added — press Connect to "
                              f"put it in service.{warn}")


@telephony.post("/{owner}/trunks")
def ui_add_trunk(owner: str, request: Request, csrf: str = Form(""),
                 name: str = Form(""), sip_domain: str = Form(""),
                 caller_id: str = Form(""), provider_ips: str = Form(""),
                 reason: str = Form("")):
    reason = _guard(request, csrf, reason, min_len=10)
    from shared.telephony_admin import create_trunk
    out = _run(owner, request, "trunk.add", lambda: create_trunk(
        owner, name, sip_domain, caller_id=caller_id, provider_ips=provider_ips,
        actor="admin-ui"), {"name": name, "reason": reason})
    if isinstance(out, RedirectResponse):
        return out
    return _back(owner, "ok", f"Trunk {out['trunk']['name']} created.")


@telephony.post("/{owner}/trunk/{trunk_id}/delete")
def ui_delete_trunk(owner: str, trunk_id: int, request: Request, csrf: str = Form(""),
                    reason: str = Form("")):
    reason = _guard(request, csrf, reason, min_len=10)
    from shared.telephony_admin import delete_trunk
    out = _run(owner, request, "trunk.delete", lambda: delete_trunk(trunk_id, "admin-ui"),
               {"trunk_id": trunk_id, "reason": reason})
    if isinstance(out, RedirectResponse):
        return out
    return _back(owner, "ok", f"Trunk {out['name']} deleted and its gateway retired.")
