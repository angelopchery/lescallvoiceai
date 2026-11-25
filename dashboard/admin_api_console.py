"""Admin console — the Integration API plane.

The operator's controls for the customer-facing `/v1` API: which client IP
addresses may reach it per tenant, how hard those addresses may push, and which
keys currently exist.

Why the IP allowlist lives HERE and not in the tenant's own Settings page: it
is the boundary a customer's security team asks us for, and a boundary the
account being protected can edit is not a boundary. A tenant may narrow it
further on their own keys; only this page can widen it.

Path is `/admin/integration`, not `/admin/api` — `/admin/api/*` is the admin
JSON API namespace, which is deliberately bearer-header-only (a browser cookie
can never drive it). Keeping the HTML page out of that prefix keeps that rule
intact.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from admin_ui import (PLAN_TONE as PLAN_TONE_LOCAL, _check_csrf, _csrf_field,
                      _flash, e, num, page, pill, when)

logger = logging.getLogger("lescall.admin.integration")

api_console = APIRouter(prefix="/admin/integration")


def _back(owner: str, kind: str, text: str) -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse(
        f"/admin/integration/{quote(owner)}?flash={kind}&msg={quote(text)}",
        status_code=303)


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------

@api_console.get("", response_class=HTMLResponse)
@api_console.get("/", response_class=HTMLResponse)
def integration_index(request: Request):
    from admin_app import audit, require_token
    require_token(request)
    audit(request, "ui.integration")
    from shared.db.database import get_db
    from shared.public_api import policy as policy_mod

    try:
        with get_db() as db:
            rows = [dict(r) for r in db.execute("""
                SELECT u.email AS owner, u.company,
                       p.enabled, p.ip_allowlist, p.rate_per_min, p.rate_per_day,
                       p.max_keys, p.updated_at,
                       (SELECT COUNT(*) FROM api_keys k
                         WHERE k.owner=u.email AND k.status='active') AS active_keys,
                       (SELECT MAX(k.last_used_at) FROM api_keys k
                         WHERE k.owner=u.email) AS last_used,
                       e.plan_code, e.api_access,
                       (SELECT COALESCE(SUM(k.request_count),0) FROM api_keys k
                         WHERE k.owner=u.email) AS requests
                FROM users u
                LEFT JOIN api_access_policy p ON p.owner = u.email
                LEFT JOIN entitlements e ON e.owner = u.email
                ORDER BY (SELECT COUNT(*) FROM api_keys k
                           WHERE k.owner=u.email AND k.status='active') DESC,
                         u.email""").fetchall()]
    except Exception as exc:
        logger.error("[admin] integration index failed: %r", exc)
        return page("Integration API", f"""<h1>Integration API</h1>
<div class="card"><p class="muted">Could not read the API tables — has migration
0019_public_api been applied? <code>{e(exc)}</code></p></div>""")

    body = []
    for r in rows:
        allow = policy_mod.normalize_allowlist(r.get("ip_allowlist") or [])
        if r.get("enabled") is None:
            state = pill("defaults", "")
        elif int(r["enabled"] or 0):
            state = pill("enabled", "mint")
        else:
            state = pill("disabled", "rose")
        limits = (f"{num(r['rate_per_min'])}/min · {num(r['rate_per_day'])}/day"
                  if r.get("rate_per_min") else
                  f"{policy_mod.DEFAULTS['rate_per_min']}/min · "
                  f"{policy_mod.DEFAULTS['rate_per_day']}/day")
        body.append(f"""<tr>
  <td><a href="/admin/integration/{e(r['owner'])}"><b>{e(r['owner'])}</b></a>
      <div class="small muted">{e(r.get('company') or '')}</div></td>
  <td>{state}</td>
  <td>{(pill(r.get('plan_code') or 'free',
             'mint' if r.get('api_access') else 'rose'))}</td>
  <td class="num">{num(r['active_keys'])}</td>
  <td>{(pill(f"{len(allow)} IP rule" + ("s" if len(allow) != 1 else ""), "sky")
        if allow else '<span class="muted">any IP</span>')}</td>
  <td class="small">{e(limits)}</td>
  <td class="num">{num(r['requests'])}</td>
  <td class="small muted">{when(r['last_used'], '%d %b %H:%M')}</td>
</tr>""")

    active = sum(1 for r in rows if (r.get("active_keys") or 0) > 0)
    restricted = sum(1 for r in rows
                     if policy_mod.normalize_allowlist(r.get("ip_allowlist") or []))
    return page("Integration API", f"""
<h1>Integration API</h1>
<p class="sub">{active} account{'s' if active != 1 else ''} with live keys ·
   {restricted} restricted to specific client IPs ·
   default ceiling {policy_mod.DEFAULTS['rate_per_min']}/min,
   {policy_mod.DEFAULTS['rate_per_day']}/day</p>
<div class="card">
  <p class="small muted">The API is a <b>Growth</b> capability. A plan pill shown
     in red means that account\'s entitlement does not include it — grant it to an
     individual tenant with an <code>api_access</code> override rather than by
     moving their plan. Tenants mint their own keys in Settings → API access.
     What is set here is the boundary they cannot cross: which source addresses
     may reach <code>/v1</code> at all, and how hard each may push. An account
     with no row uses the platform defaults.</p>
  <table>
    <thead><tr><th>Tenant</th><th>API</th><th>Plan</th><th class=num>Live keys</th>
      <th>Source IPs</th><th>Rate ceiling</th><th class=num>Requests</th>
      <th>Last used</th></tr></thead>
    <tbody>{''.join(body) or '<tr><td colspan=8 class=muted>No accounts.</td></tr>'}</tbody>
  </table>
</div>""", _flash(request))


# ---------------------------------------------------------------------------
# per-tenant
# ---------------------------------------------------------------------------

@api_console.get("/{owner}", response_class=HTMLResponse)
def integration_tenant(owner: str, request: Request):
    from admin_app import audit, require_token
    require_token(request)
    audit(request, "ui.integration.tenant", owner)
    from shared.public_api import keys as keys_mod
    from shared.public_api import policy as policy_mod
    from shared.public_api import ratelimit

    pol = policy_mod.get_policy(owner, use_cache=False)
    try:
        from shared.billing.entitlements import enforcement_enabled, get_entitlements
        ent = get_entitlements(owner)
        plan_ok = bool(ent.get("api_access"))
        plan_code = ent.get("plan_code") or "free"
        enforcing = enforcement_enabled()
    except Exception as exc:
        logger.warning("[admin] entitlement read failed for %s: %r", owner, exc)
        plan_ok, plan_code, enforcing = True, "?", False
    try:
        keys = keys_mod.list_keys(owner)
    except Exception as exc:
        logger.error("[admin] key list failed for %s: %r", owner, exc)
        keys = []
    active_ids = [k["key_id"] for k in keys if k["status"] == "active"]
    history = ratelimit.daily_history(owner, days=14)
    csrf = _csrf_field(request)

    # The addresses this account has actually been seen calling from. Shown
    # BESIDE the allowlist form because the failure mode of this page is an
    # operator listing the one IP they were told about and silently cutting off
    # the tenant's staging box, their developer's laptop, and anything else
    # already integrated. You cannot avoid that by being careful; you avoid it
    # by being able to see what is really connecting.
    seen: dict[str, dict] = {}
    for k in keys:
        addr = (k.get("last_used_ip") or "").strip()
        if not addr:
            continue
        row = seen.setdefault(addr, {"ip": addr, "keys": [], "last": k.get("last_used_at")})
        row["keys"].append(k["name"])
        if (k.get("last_used_at") or "") > (row["last"] or ""):
            row["last"] = k.get("last_used_at")
    covered = {a: policy_mod.match_ip(a, pol.get("ip_allowlist"))[0] for a in seen}
    seen_rows = "".join(f"""<tr>
  <td><code>{e(v['ip'])}</code></td>
  <td class="small muted">{e(', '.join(sorted(set(v['keys'])))[:60])}</td>
  <td class="small muted">{when(v['last'], '%d %b %H:%M')}</td>
  <td>{(pill('allowed', 'mint') if covered[a] else pill('would be BLOCKED', 'rose'))}</td>
</tr>""" for a, v in sorted(seen.items(), key=lambda kv: kv[1]["last"] or "", reverse=True))

    key_rows = []
    for k in keys:
        tone = "mint" if k["status"] == "active" and not k["expired"] else "rose"
        label = "expired" if k["expired"] else k["status"]
        used = k["usage"]
        revoke = ("" if k["status"] != "active" else f"""
      <form class=act method=post
            action="/admin/integration/{e(owner)}/keys/{k['id']}/revoke">
        {csrf}<input name=reason placeholder="why revoke this key" minlength=10
        required style="width:11rem">
        <button type=submit class=danger style="margin-top:.3rem">Revoke</button>
      </form>""")
        key_rows.append(f"""<tr>
  <td><b>{e(k['name'])}</b><div class="small muted"><code>{e(k['masked'])}</code></div></td>
  <td>{pill(label, tone)}</td>
  <td class="small">{e(', '.join(k['scopes']) or '—')}</td>
  <td class="small">{(e(', '.join(x['cidr'] for x in k['ip_allowlist']))
                      if k['ip_allowlist'] else '<span class="muted">account default</span>')}</td>
  <td class="num">{num(k['request_count'])}<div class="small muted">
      {num(used.get('day', 0))} today</div></td>
  <td class="small muted">{when(k['last_used_at'], '%d %b %H:%M')}
      <div class="small muted">{e(k['last_used_ip'] or '')}</div></td>
  <td>{revoke}</td>
</tr>""")

    hist = "".join(f"<tr><td>{e(h['day'])}</td><td class=num>{num(h['hits'])}</td></tr>"
                   for h in history)

    checked = "checked" if pol.get("enabled") else ""
    allow_text = policy_mod.format_allowlist_text(pol.get("ip_allowlist"))
    source = ("platform defaults (no row yet)" if pol.get("provisional")
              else f"set by {e(pol.get('updated_by') or '—')} {when(pol.get('updated_at'))}")

    return page(f"API · {owner}", f"""
<h1>Integration API — {e(owner)}</h1>
<p class="sub"><a href="/admin/integration">← all accounts</a> ·
   <a href="/admin/tenant/{e(owner)}">tenant detail</a> · {source}</p>
<div class="card" style="border-left:3px solid {'#0b6b45' if plan_ok else '#b42318'}">
  <b>Plan entitlement:</b> {pill(plan_code, PLAN_TONE_LOCAL.get(plan_code, ''))}
  {'includes' if plan_ok else '<b>does NOT include</b>'} the integration API
  {'' if enforcing else ' — <b>but platform billing enforcement is OFF</b>, so'
   ' every account can use the API regardless of plan today.'}
  <div class="small muted" style="margin-top:.35rem">
    The API is a Growth capability (<code>entitlements.api_access</code>). To grant
    it to this tenant alone, set an <code>api_access</code> override on their
    entitlement rather than moving their plan — the policy below controls WHERE
    they may call from, not WHETHER their plan allows it.
  </div>
</div>

<div class="grid side">
 <div>
  <div class="card">
   <h2>Access policy</h2>
   <p class="small muted">The <b>IP allowlist is the boundary</b>: leave it empty
      and any address with a valid key may call <code>/v1</code>; fill it in and
      every other address is refused before the key is even checked. One rule
      per line:</p>
   <p class="small"><code>cidr | label | rate_per_min | rate_per_day</code></p>
   <p class="small muted">Everything after the address is optional. A per-line
      rate <b>raises</b> the ceiling for that one machine — give a client's
      production CRM server room without opening it up for everything else. The
      most specific matching rule wins, so a <code>/32</code> beats the
      <code>/16</code> it sits inside.</p>
   <p class="small" style="color:#8a5b00">
      <b>List every source that legitimately calls</b>, not just the one you were
      told about — a staging box, an office VPN, a developer testing from a
      laptop. Anything you leave out starts getting <code>403</code> the moment
      you save. The panel on the right shows what is really connecting today.</p>
   <form method=post action="/admin/integration/{e(owner)}/policy">
     {csrf}
     <label><input type=checkbox name=enabled value=1 {checked}
       style="width:auto;margin-right:.4rem">API access enabled for this account</label>
     <label>Allowed client IPs (blank = any)</label>
     <textarea name=ip_allowlist rows=6
       placeholder="203.0.113.9/32 | Pramukh CRM prod | 300 | 100000
10.20.0.0/16 | office VPN">{e(allow_text)}</textarea>
     <div class="grid two">
       <div><label>Requests per minute</label>
         <input name=rate_per_min type=number min=1 max={policy_mod.MAX_PER_MIN}
           value="{int(pol.get('rate_per_min') or policy_mod.DEFAULTS['rate_per_min'])}"></div>
       <div><label>Requests per day</label>
         <input name=rate_per_day type=number min=1 max={policy_mod.MAX_PER_DAY}
           value="{int(pol.get('rate_per_day') or policy_mod.DEFAULTS['rate_per_day'])}"></div>
     </div>
     <label>Maximum live keys</label>
     <input name=max_keys type=number min=0 max={policy_mod.MAX_KEYS}
       value="{int(pol.get('max_keys') or policy_mod.DEFAULTS['max_keys'])}">
     <label>Note (shown to the tenant on their Settings page)</label>
     <input name=note maxlength=500 value="{e(pol.get('note') or '')}"
       placeholder="e.g. Restricted to Pramukh's CRM server on request, 12 Aug">
     <button type=submit>Save policy</button>
   </form>
  </div>

  <div class="card">
   <h2>Keys ({len(active_ids)} live of {int(pol.get('max_keys') or 0)})</h2>
   <p class="small muted">Keys are minted by the tenant in Settings → API access;
      secrets are stored only as hashes and cannot be read back from here.
      Revoking is immediate and permanent — the row is kept so the record of
      what that credential could do, and when it was last used, survives.</p>
   <table>
     <thead><tr><th>Key</th><th>State</th><th>Scopes</th><th>Key IP limit</th>
       <th class=num>Requests</th><th>Last used</th><th></th></tr></thead>
     <tbody>{''.join(key_rows) or '<tr><td colspan=7 class=muted>No keys minted yet.</td></tr>'}</tbody>
   </table>
  </div>
 </div>

 <div>
  <div class="card">
   <h2>Seen calling</h2>
   <p class="small muted">Addresses this account's keys have actually used.
      Check these against the allowlist before you save it.</p>
   <table>
     <thead><tr><th>Address</th><th>Key</th><th>Last seen</th><th>Under this policy</th></tr></thead>
     <tbody>{seen_rows or '<tr><td colspan=4 class=muted>No API traffic from this account yet.</td></tr>'}</tbody>
   </table>
  </div>

  <div class="card">
   <h2>Usage · 14 days</h2>
  <table><thead><tr><th>Day</th><th class=num>Requests</th></tr></thead>
   <tbody>{hist or '<tr><td colspan=2 class=muted>No API traffic yet.</td></tr>'}</tbody></table>
  </div>
 </div>
</div>""", _flash(request))


@api_console.post("/{owner}/policy")
def save_policy(owner: str, request: Request, csrf: str = Form(""),
                enabled: str = Form(""), ip_allowlist: str = Form(""),
                rate_per_min: int = Form(60), rate_per_day: int = Form(20000),
                max_keys: int = Form(5), note: str = Form("")):
    from admin_app import audit, require_token
    require_token(request, allow_cookie_write=True)
    _check_csrf(request, csrf)
    from shared.public_api import policy as policy_mod

    entries, errors = policy_mod.parse_allowlist_text(ip_allowlist)
    if errors:
        # Refuse the WHOLE save on a bad line. Silently dropping it would leave
        # an operator believing they had restricted an account that is still
        # wide open — the one failure mode this page must not have.
        return _back(owner, "err", "; ".join(errors[:3]))
    try:
        policy_mod.upsert_policy(owner, enabled=bool(enabled), ip_allowlist=entries,
                                 rate_per_min=rate_per_min, rate_per_day=rate_per_day,
                                 max_keys=max_keys, note=note, actor="admin")
    except Exception as exc:
        logger.error("[admin] policy save failed for %s: %r", owner, exc)
        return _back(owner, "err", f"could not save: {exc}")
    audit(request, "integration.policy", owner)
    try:
        from shared.audit import record
        record(owner, "api_policy_update", owner,
               {"enabled": bool(enabled), "ip_rules": len(entries),
                "rate_per_min": rate_per_min, "rate_per_day": rate_per_day,
                "max_keys": max_keys, "by": "admin"})
    except Exception:
        pass
    where = (f"restricted to {len(entries)} IP rule(s)" if entries
             else "open to any IP")
    return _back(owner, "ok", f"Policy saved — {where}, "
                              f"{rate_per_min}/min, {rate_per_day}/day.")


@api_console.post("/{owner}/keys/{key_pk}/revoke")
def revoke_tenant_key(owner: str, key_pk: int, request: Request,
                      csrf: str = Form(""), reason: str = Form("")):
    from admin_app import audit, require_token
    from admin_ui import _guard
    reason = _guard(request, csrf, reason, min_len=10)
    from shared.public_api import keys as keys_mod
    try:
        key = keys_mod.revoke_key(owner, key_pk, actor="admin")
    except Exception as exc:
        return _back(owner, "err", f"could not revoke: {exc}")
    audit(request, "integration.key_revoke", f"{owner}:{key['key_id']}")
    try:
        from shared.audit import record
        record(owner, "api_key_revoke", key["key_id"],
               {"name": key["name"], "by": "admin", "reason": reason})
    except Exception:
        pass
    return _back(owner, "ok", f"Key '{key['name']}' revoked.")
