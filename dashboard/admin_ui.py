"""Server-rendered admin console — tenant list, tenant detail, plan controls.

Kept separate from `admin_app.py` so the JSON API and the HTML surface stay
readable side by side. Both are mounted on the same app and share
`require_token`.

No React, no build step, no external assets: one CSS block inlined below. This
runs behind an SSH tunnel on a box with no CDN access, and an admin page that
needs a bundler to render is an admin page you cannot use during an incident.

CSRF: browser POSTs carry a token derived from ADMIN_TOKEN that an attacker
page can neither read (cross-origin responses are opaque) nor compute. That,
plus SameSite=strict on the session cookie and an Origin check, is what makes
it safe to expose mutations to the browser at all — see `_check_csrf`.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger("lescall.admin.ui")

ui = APIRouter(prefix="/admin")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

def e(v) -> str:
    return html.escape("" if v is None else str(v))


def money(paise) -> str:
    if paise is None:
        return "—"
    return f"₹{int(paise) / 100:,.2f}"


def when(v, fmt: str = "%d %b %Y %H:%M") -> str:
    """Timestamps arrive as strings from this codebase's DB layer."""
    if not v:
        return "—"
    try:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return e(v)
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(IST).strftime(fmt) + " IST"


def num(v) -> str:
    return "—" if v is None else f"{int(v):,}"


def yn(v) -> str:
    return ('<span class="y">yes</span>' if v else '<span class="n">no</span>')


def pill(text: str, tone: str = "") -> str:
    return f'<span class="pill {tone}">{e(text)}</span>'


PLAN_TONE = {"free": "", "pilot": "sky", "basic": "mint",
             "growth": "violet", "enterprise": "amber"}

CSS = """
*{box-sizing:border-box}
body{font:14px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
     margin:0;background:#f6f7f9;color:#16181d}
a{color:#0b6bcb;text-decoration:none}a:hover{text-decoration:underline}
header{background:#16181d;color:#fff;padding:.7rem 1.25rem;display:flex;
       align-items:center;gap:1rem;flex-wrap:wrap;position:sticky;top:0;z-index:5}
header .brand{font-weight:700;letter-spacing:.02em}
header nav{margin-left:auto;display:flex;gap:1rem;align-items:center}
header a{color:#c9d4e4}
main{max-width:78rem;margin:0 auto;padding:1.25rem}
h1{font-size:1.3rem;margin:0 0 .2rem}
h2{font-size:1rem;margin:0 0 .7rem;padding-bottom:.4rem;border-bottom:1px solid #e6e8ec}
.sub{color:#697086;font-size:.85rem;margin:0 0 1.1rem}
.card{background:#fff;border:1px solid #e6e8ec;border-radius:12px;padding:1.1rem;
      margin-bottom:1.1rem}
.grid{display:grid;gap:1.1rem}
@media(min-width:60rem){.grid.two{grid-template-columns:1fr 1fr}
                        .grid.side{grid-template-columns:2fr 1fr}}
table{border-collapse:collapse;width:100%;font-size:.88rem}
th,td{text-align:left;padding:.45rem .6rem;border-bottom:1px solid #eef0f3;
      vertical-align:top}
th{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:#697086;
   font-weight:600}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:#fafbfc}
.num{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:.1rem .45rem;border-radius:99px;font-size:.72rem;
      font-weight:600;background:#eef0f3;color:#414755}
.pill.mint{background:#d9f5e6;color:#0b6b45}.pill.sky{background:#dbeafe;color:#1e4fa3}
.pill.violet{background:#ede9fe;color:#5b3ec8}.pill.amber{background:#fef3c7;color:#8a5b00}
.pill.rose{background:#fee2e2;color:#9b1c1c}
.y{color:#0b6b45;font-weight:600}.n{color:#a0a6b4}
dl{display:grid;grid-template-columns:auto 1fr;gap:.3rem .9rem;margin:0;font-size:.88rem}
dt{color:#697086}dd{margin:0;font-weight:500}
form.act{margin:0}
label{display:block;font-size:.78rem;color:#697086;margin:.55rem 0 .2rem}
input,select,textarea{width:100%;padding:.45rem .55rem;border:1px solid #ccd2dc;
                      border-radius:7px;font:inherit;font-size:.88rem;background:#fff}
button{margin-top:.7rem;padding:.5rem .9rem;border:0;border-radius:7px;font:inherit;
       font-weight:600;font-size:.88rem;background:#0b6bcb;color:#fff;cursor:pointer}
button.danger{background:#b42318}button.ghost{background:#eef0f3;color:#16181d}
button:hover{filter:brightness(1.07)}
.flash{padding:.65rem .9rem;border-radius:9px;margin-bottom:1rem;font-size:.9rem}
.flash.ok{background:#d9f5e6;color:#0b6b45}.flash.err{background:#fee2e2;color:#9b1c1c}
.flash.warn{background:#fef3c7;color:#8a5b00}
.muted{color:#697086}.small{font-size:.8rem}
.bar{height:6px;background:#eef0f3;border-radius:99px;overflow:hidden;margin-top:.3rem}
.bar>i{display:block;height:100%;background:#0b6bcb}
.bar>i.warn{background:#d97706}.bar>i.over{background:#b42318}
code{background:#eef0f3;padding:.08rem .3rem;border-radius:4px;font-size:.82rem}
.searchrow{display:flex;gap:.6rem;align-items:flex-end;margin-bottom:.9rem}
.searchrow input{max-width:22rem}.searchrow button{margin-top:0}
"""


def page(title: str, body: str, flash: tuple[str, str] | None = None) -> str:
    msg = ""
    if flash:
        kind, text = flash
        msg = f'<div class="flash {e(kind)}">{e(text)}</div>'
    return f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{e(title)} · LesCall admin</title>
<style>{CSS}</style>
<header>
  <span class="brand">LesCall admin</span>
  <nav>
    <a href="/admin">Tenants</a>
    <a href="/admin/calls">Calls</a>
    <a href="/admin/agents">Agents</a>
    <a href="/admin/telephony">Telephony</a>
    <a href="/admin/invoices">Outstanding</a>
    <a href="/admin/payments">Payments</a>
    <a href="/admin/health">Health</a>
    <a href="/admin/integration">Integration API</a>
    <a href="/admin/keys">Keys</a>
    <a href="/admin/audit">Audit</a>
    <a href="/admin/logout">Sign out</a>
  </nav>
</header>
<main>{msg}{body}</main>"""


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def _inv_status(i: dict) -> str:
    st = i.get("status")
    if st == "void":
        return pill("cancelled", "rose")
    if st == "due":
        from shared.billing.entitlements import as_dt, now_utc
        due = as_dt(i.get("due_date"))
        if due and due < now_utc():
            return pill(f"overdue {(now_utc() - due).days}d", "rose")
        return pill("payment due", "amber")
    if st == "refunded":
        return pill("refunded", "amber")
    if st == "captured":
        return pill("paid", "mint")
    return pill(st or "—")


def _inv_actions(owner: str, i: dict, csrf: str) -> str:
    """Row controls. Only manual invoices are settleable or voidable here —
    a gateway payment is reversed by refunding it at the gateway."""
    if i.get("source") != "manual" or i.get("status") == "void":
        return '<span class="muted">—</span>'
    bits = []
    if i.get("status") == "due":
        from shared.billing.invoices import METHODS
        opts = "".join(f'<option value="{e(k)}">{e(v)}</option>'
                       for k, v in METHODS.items() if k != "credit")
        bits.append(f"""<form class=act method=post
          action="/admin/invoice/{i['id']}/paid" style="margin-bottom:.4rem">
          {csrf}<select name=method style="width:auto">{opts}</select>
          <input name=reference placeholder="ref" style="width:7rem">
          <button type=submit style="margin-top:.3rem">Mark paid</button></form>""")
    bits.append(f"""<form class=act method=post action="/admin/invoice/{i['id']}/void">
      {csrf}<input name=reason placeholder="reason to cancel" minlength=10 required
      style="width:9rem"><button type=submit class=danger
      style="margin-top:.3rem">Void</button></form>""")
    return "".join(bits)


def _csrf_field(request: Request) -> str:
    from admin_app import csrf_token
    return f'<input type=hidden name=csrf value="{e(csrf_token())}">'


def _check_csrf(request: Request, csrf: str) -> None:
    """Reject a browser POST that did not come from our own page.

    Three independent barriers, because this endpoint is reachable at
    127.0.0.1 whenever the operator's tunnel is open — which means any website
    they happen to be visiting can attempt a request to it:

      1. SameSite=strict on the session cookie: a cross-site POST arrives with
         no cookie at all, so it is simply unauthenticated.
      2. This token, derived from ADMIN_TOKEN. An attacker page cannot read it
         (cross-origin responses are opaque) and cannot compute it.
      3. An Origin check, which catches anything that forges its way past both.
    """
    from admin_app import csrf_token
    import secrets as _s
    if not csrf or not _s.compare_digest(csrf, csrf_token()):
        logger.warning("[admin] CSRF token rejected for %s", request.url.path)
        raise HTTPException(status_code=403, detail="invalid CSRF token")
    origin = request.headers.get("origin") or ""
    if origin:
        host = request.headers.get("host") or ""
        if not (origin.endswith(f"//{host}") or origin.endswith(f"//{host}/")):
            logger.warning("[admin] cross-origin POST refused: origin=%r host=%r",
                           origin, host)
            raise HTTPException(status_code=403, detail="cross-origin request refused")


def _back(owner: str, kind: str, text: str) -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse(
        f"/admin/tenant/{quote(owner)}?flash={kind}&msg={quote(text)}",
        status_code=303)


# ---------------------------------------------------------------------------
# tenant list
# ---------------------------------------------------------------------------

@ui.get("", response_class=HTMLResponse)
@ui.get("/", response_class=HTMLResponse)
def tenants_page(request: Request, q: str = ""):
    from admin_app import audit, require_token
    require_token(request)
    audit(request, "ui.tenants")
    from shared.billing.entitlements import enforcement_enabled
    from shared.db.database import get_db

    with get_db() as db:
        rows = [dict(r) for r in db.execute("""
            SELECT u.email AS owner, u.name, u.company, u.last_login,
                   e.plan_code, e.status, e.minutes_limit, e.period_end,
                   e.max_agent_families, e.telephony,
                   (SELECT COUNT(*) FROM agents a
                     WHERE a.owner=u.email AND a.archived_at IS NULL) AS agents,
                   (SELECT COUNT(*) FROM calls c WHERE c.owner=u.email) AS calls,
                   (SELECT COUNT(*) FROM phone_numbers p WHERE p.owner=u.email) AS numbers
            FROM users u LEFT JOIN entitlements e ON e.owner=u.email
            ORDER BY u.email""").fetchall()]

    needle = (q or "").strip().lower()
    if needle:
        rows = [r for r in rows if needle in (r["owner"] or "").lower()
                or needle in (r["name"] or "").lower()
                or needle in (r["company"] or "").lower()
                or needle == (r["plan_code"] or "")]

    body = []
    for r in rows:
        plan = r["plan_code"] or "free"
        st = r["status"] or "ok"
        body.append(f"""<tr>
  <td><a href="/admin/tenant/{e(r['owner'])}"><b>{e(r['owner'])}</b></a>
      <div class="small muted">{e(r['name'] or '')}{' · ' + e(r['company']) if r['company'] else ''}</div></td>
  <td>{pill(plan, PLAN_TONE.get(plan, ''))}</td>
  <td>{pill(st, 'rose' if st == 'locked' else ('amber' if st == 'grace' else 'mint'))}</td>
  <td class="num">{'unlimited' if r['minutes_limit'] is None else num(r['minutes_limit'])}</td>
  <td class="num">{num(r['agents'])}{'' if r['max_agent_families'] is None else ' / ' + num(r['max_agent_families'])}</td>
  <td class="num">{num(r['calls'])}</td>
  <td class="num">{num(r['numbers'])}</td>
  <td>{yn(r['telephony'])}</td>
  <td class="small muted">{when(r['last_login'], '%d %b %H:%M')}</td>
</tr>""")

    enf = enforcement_enabled(refresh=True)
    enf_pill = pill("ON", "mint") if enf else pill("OFF", "rose")
    flash = _flash(request)
    return page("Tenants", f"""
<h1>Tenants</h1>
<p class="sub">{len(rows)} account{'s' if len(rows) != 1 else ''} ·
   quota enforcement {enf_pill} ·
   <a href="/admin/settings">platform settings</a></p>
<div class="card">
  <form class="searchrow" method=get action="/admin">
    <div style="flex:1"><label>Search by email, name, company or plan</label>
      <input name=q value="{e(q)}" placeholder="e.g. gmail.com, or: growth" autofocus></div>
    <button type=submit>Search</button>
    {'<a href="/admin"><button type=button class=ghost>Clear</button></a>' if q else ''}
  </form>
  <table>
    <thead><tr><th>Tenant</th><th>Plan</th><th>Status</th><th class=num>Minutes</th>
      <th class=num>Agents</th><th class=num>Calls</th><th class=num>Numbers</th>
      <th>Telephony</th><th>Last login</th></tr></thead>
    <tbody>{''.join(body) or '<tr><td colspan=9 class=muted>No tenants match.</td></tr>'}</tbody>
  </table>
</div>""", flash)


def _flash(request: Request) -> tuple[str, str] | None:
    kind = request.query_params.get("flash")
    msg = request.query_params.get("msg")
    if kind and msg:
        return ("ok" if kind == "ok" else "err", msg[:300])
    return None


# ---------------------------------------------------------------------------
# tenant detail
# ---------------------------------------------------------------------------

@ui.get("/tenant/{owner}", response_class=HTMLResponse)
def tenant_page(owner: str, request: Request):
    from admin_app import audit, require_token
    require_token(request)
    audit(request, "ui.tenant", owner)

    from shared.billing.entitlements import usage_summary
    from shared.db.database import get_db

    usage = usage_summary(owner)
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE email=?", (owner,)).fetchone()
        ent = db.execute("SELECT * FROM entitlements WHERE owner=?", (owner,)).fetchone()
        agents = [dict(r) for r in db.execute(
            "SELECT slug, display_name, archived_at FROM agents WHERE owner=? "
            "ORDER BY archived_at NULLS FIRST, slug", (owner,)).fetchall()]
        numbers = [dict(r) for r in db.execute(
            "SELECT number, inbound_agent_slug, outbound_agent_slug, config_status "
            "FROM phone_numbers WHERE owner=?", (owner,)).fetchall()]
        trunks = [dict(r) for r in db.execute(
            "SELECT name, provider, config_status FROM trunks WHERE owner=?",
            (owner,)).fetchall()]
        subs = [dict(r) for r in db.execute(
            "SELECT * FROM subscriptions WHERE owner=? ORDER BY id DESC LIMIT 10",
            (owner,)).fetchall()]
        pays = [dict(r) for r in db.execute(
            "SELECT * FROM payments WHERE owner=? ORDER BY id DESC LIMIT 10",
            (owner,)).fetchall()]
        invoices_list = [dict(r) for r in db.execute(
            "SELECT * FROM payments WHERE owner=? ORDER BY id DESC LIMIT 25",
            (owner,)).fetchall()]
        due_row = db.execute(
            "SELECT COALESCE(SUM(amount_paise),0) AS due FROM payments "
            "WHERE owner=? AND status='due'", (owner,)).fetchone()
        attempts = [dict(r) for r in db.execute(
            "SELECT * FROM payment_attempts WHERE owner=? ORDER BY id DESC LIMIT 8",
            (owner,)).fetchall()]
        claim = db.execute("SELECT * FROM pilot_claims WHERE owner=?", (owner,)).fetchone()
        calls = [dict(r) for r in db.execute(
            "SELECT started_at, direction, phone, duration_s, agent_slug, outcome "
            "FROM calls WHERE owner=? ORDER BY id DESC LIMIT 10", (owner,)).fetchall()]
        audit_rows = [dict(r) for r in db.execute(
            "SELECT at, action, target FROM audit_events WHERE owner=? "
            "ORDER BY id DESC LIMIT 10", (owner,)).fetchall()]
        plans = [dict(r) for r in db.execute(
            "SELECT code, name FROM plans WHERE active=1 ORDER BY sort").fetchall()]

    from shared.billing.credits import list_grants, live_balance
    grants = list_grants(owner)
    reward = live_balance(owner)

    if user is None and ent is None:
        raise HTTPException(status_code=404, detail="no such tenant")
    user = dict(user) if user else {"email": owner}
    ent = dict(ent) if ent else {}

    used, limit = usage["minutes_used"], usage["minutes_limit"]
    pct = 0 if not limit else min(100, round(used / limit * 100))
    bar_cls = "over" if limit and used >= limit else ("warn" if limit and pct >= 80 else "")
    plan = usage["plan_code"] or "free"
    csrf = _csrf_field(request)

    # ---- overview -----------------------------------------------------
    overview = f"""
<div class="card">
  <h2>Account</h2>
  <dl>
    <dt>Email</dt><dd>{e(owner)}</dd>
    <dt>Name</dt><dd>{e(user.get('name') or '—')}</dd>
    <dt>Company</dt><dd>{e(user.get('company') or '—')}</dd>
    <dt>Phone</dt><dd>{e(user.get('phone') or '—')}</dd>
    <dt>Signed up</dt><dd>{when(user.get('created_at'))}</dd>
    <dt>Last login</dt><dd>{when(user.get('last_login'))}</dd>
    <dt>Pilot used</dt><dd>{yn(claim and not dict(claim).get('released_at'))}
      {('<span class="small muted"> · claimed ' + when(dict(claim)['claimed_at']) + '</span>') if claim else ''}</dd>
  </dl>
</div>

<div class="card">
  <h2>Entitlement</h2>
  <dl>
    <dt>Plan</dt><dd>{pill(plan, PLAN_TONE.get(plan, ''))}
      {pill(usage['status'], 'rose' if usage['status'] == 'locked' else ('amber' if usage['status'] == 'grace' else 'mint'))}</dd>
    <dt>Period</dt><dd>{when(usage['period_start'], '%d %b')} → {when(usage['period_end'], '%d %b %Y')}</dd>
    <dt>Minutes</dt><dd>{num(used)} / {'unlimited' if limit is None else num(limit)}
      {f'<div class="bar"><i class="{bar_cls}" style="width:{pct}%"></i></div>' if limit else ''}
      {f'<div class="small muted">{num(usage["minutes_included"])} on plan + '
       f'{num(reward["minutes_remaining"])} rewarded</div>' if reward["minutes_remaining"] else ''}</dd>
    <dt>Agents</dt><dd>{num(usage['agents_used'] if 'agents_used' in usage else len([a for a in agents if not a['archived_at']]))} /
      {'unlimited' if usage['max_agent_families'] is None else num(usage['max_agent_families'])}</dd>
    <dt>Concurrent</dt><dd>{'unlimited' if usage['max_concurrent_calls'] is None else num(usage['max_concurrent_calls'])}</dd>
    <dt>Copilot/day</dt><dd>{'unlimited' if usage['copilot_turns_per_day'] is None else num(usage['copilot_turns_per_day'])}</dd>
    <dt>Overrides</dt><dd><code>{e(ent.get('overrides') or '—')}</code></dd>
    <dt>Updated</dt><dd>{when(ent.get('updated_at'))} by {e(ent.get('updated_by') or '—')}</dd>
  </dl>
  <h2 style="margin-top:1rem">Capabilities</h2>
  <dl>
    {''.join(f'<dt>{k.replace("_", " ").title()}</dt><dd>{yn(v)}</dd>'
             for k, v in usage['features'].items())}
    <dt>Telephony</dt><dd>{yn(ent.get('telephony'))}</dd>
  </dl>
</div>"""

    # ---- actions ------------------------------------------------------
    plan_opts = "".join(
        f'<option value="{e(p["code"])}"{" selected" if p["code"] == plan else ""}>'
        f'{e(p["name"])} ({e(p["code"])})</option>' for p in plans)
    locked = usage["status"] == "locked"
    actions = f"""
<div class="card">
  <h2>Change plan</h2>
  <form class="act" method=post action="/admin/tenant/{e(owner)}/plan">
    {csrf}
    <label>Plan</label><select name=plan_code>{plan_opts}</select>
    <label>Expires after (days) — blank = no expiry</label>
    <input name=period_days type=number min=1 max=3650 placeholder="e.g. 7 for a Pilot run">
    <label>Reason (audited, min 10 chars)</label>
    <input name=reason required minlength=10 placeholder="why this change">
    <button type=submit>Apply plan</button>
  </form>
  <p class="small muted" style="margin-top:.6rem">
    Applied as an override, so it survives entitlement rebuilds and outranks
    any gateway subscription. Existing agents and numbers are never removed by
    a downgrade — only new creates are refused once over cap.</p>
</div>

<div class="card">
  <h2>Account actions</h2>
  <form class="act" method=post action="/admin/tenant/{e(owner)}/{'unlock' if locked else 'lock'}">
    {csrf}
    <label>Reason (audited, min 10 chars)</label>
    <input name=reason required minlength=10
           placeholder="{'why restore access' if locked else 'why suspend'}">
    <button type=submit class="{'ghost' if locked else 'danger'}">
      {'Unlock account' if locked else 'Lock account'}</button>
  </form>
  <hr style="border:0;border-top:1px solid #eef0f3;margin:1rem 0">
  <form class="act" method=post action="/admin/tenant/{e(owner)}/pilot-reset">
    {csrf}
    <label>Reason — re-grants the once-ever Pilot (min 10 chars)</label>
    <input name=reason required minlength=10 placeholder="why re-grant the trial">
    <button type=submit class=ghost>Reset Pilot claim</button>
  </form>
  <hr style="border:0;border-top:1px solid #eef0f3;margin:1rem 0">
  <form class="act" method=post action="/admin/tenant/{e(owner)}/reconcile">
    {csrf}
    <button type=submit class=ghost>Re-check payments with gateway</button>
  </form>
</div>

<div class="card">
  <h2>Telephony</h2>
  <dl>
    <dt>Numbers</dt><dd>{num(len(numbers))}
      {pill('in service: ' + str(sum(1 for n in numbers if n['config_status'] == 'applied')), 'mint')
        if numbers else ''}</dd>
    <dt>Trunks</dt><dd>{num(len(trunks))}</dd>
    <dt>Entitled</dt><dd>{yn(ent.get('telephony'))}</dd>
  </dl>
  <p class="small muted" style="margin:.6rem 0 0">
    <a href="/admin/telephony/{e(owner)}">Add, connect, rebind or move this
    account's numbers →</a></p>
</div>"""

    # ---- reward credits -----------------------------------------------
    #
    # Free minutes an operator hands out: onboarding sweeteners, an apology for
    # an outage, a negotiated extension. Runs on the existing top-up rail
    # (shared/billing/credits.py) so it lands in `minutes_limit` and every gate
    # and meter picks it up with no other code change.
    grant_rows = "".join(
        f"<tr><td class=num>{num(g['minutes'])}</td>"
        f"<td class=num>{num(g['minutes_remaining'])}</td>"
        f"<td>{when(g['period_end'], '%d %b %Y')}</td>"
        f"<td>{pill(g['status'], {'active': 'mint', 'spent': 'amber', 'revoked': 'rose', 'expired': ''}.get(g['status'], ''))}</td>"
        f"<td class=small muted>{e(g['note'] or '—')}<div>{e(g['granted_by'] or '')} · "
        f"{when(g['created_at'], '%d %b %Y')}</div></td>"
        f"<td class=small>" + (
            f"""<form class=act method=post action="/admin/tenant/{e(owner)}/credits/{g['id']}/revoke">
              {csrf}<input name=reason placeholder="reason" minlength=10 required style="width:8rem">
              <button type=submit class=danger style="margin-top:.3rem">Revoke</button></form>"""
            if g["status"] == "active" else '<span class="muted">—</span>')
        + "</td></tr>"
        for g in grants)

    credits_card = f"""
<div class="card">
  <h2>Reward minutes</h2>
  <p class="small muted" style="margin:-.3rem 0 .8rem">
    Free minutes on top of whatever the plan includes. Live on this account's
    very next call — nothing to restart, no invoice, no payment record.
    <b>Validity is a pool, not an allowance</b>: 1,000 minutes for 3 months means
    1,000 minutes to spend at any point in those 3 months, not 1,000 every month.
    Each closed month draws down whatever the account spent beyond its own plan.</p>
  {f'''<div class="flash ok" style="margin:0 0 .8rem">
      <b>{num(reward["minutes_remaining"])} reward minutes</b> left of
      {num(reward["minutes_granted"])} granted
      {(" · first expiry " + when(reward["expires_at"], "%d %b %Y")) if reward["expires_at"] else ""}
    </div>''' if reward["grants"] else ''}
  <form class="act" method=post action="/admin/tenant/{e(owner)}/credits">
    {csrf}
    <div class="grid two">
      <div><label>Credit minutes</label>
        <input name=minutes type=number min=1 max=100000 required placeholder="e.g. 1000"></div>
      <div><label>Valid for (months)</label>
        <input name=valid_months type=number min=1 max=60 value=1 required></div>
    </div>
    <label>Reason — shown in the credit history and audited (min 10 chars)</label>
    <input name=reason required minlength=10
           placeholder="e.g. goodwill credit for the 12 Aug outage">
    <button type=submit>Grant credit</button>
  </form>
  {f'''<table style="margin-top:1rem">
    <thead><tr><th class=num>Granted</th><th class=num>Left</th><th>Expires</th>
      <th>State</th><th>Reason</th><th></th></tr></thead>
    <tbody>{grant_rows}</tbody></table>''' if grants else
   '<p class="small muted" style="margin-top:.8rem">No credits granted to this account yet.</p>'}
</div>"""

    # ---- invoicing ----------------------------------------------------
    from shared.billing.invoices import METHODS
    method_opts = "".join(f'<option value="{e(k)}">{e(v)}</option>'
                          for k, v in METHODS.items())
    today = dt.datetime.now(IST).strftime("%Y-%m-%d")
    plan_price_opts = "".join(
        f'<option value="{e(p["code"])}" data-price="{int(p.get("price_paise") or 0)}"'
        f'{" selected" if p["code"] == plan else ""}>{e(p["name"])}</option>'
        for p in plans)
    invoice_form = f"""
<div class="card">
  <h2>Issue an invoice</h2>
  <p class="small muted" style="margin:-.3rem 0 .6rem">
    For money collected outside the payment gateway. Produces a real GST tax
    invoice, numbered in the same series, visible to the customer in their own
    Billing page. Choosing <b>Not yet received</b> issues it on credit — the
    plan activates now and the balance shows as outstanding.</p>
  <form class="act" method=post action="/admin/tenant/{e(owner)}/invoice">
    {csrf}
    <label>Plan being invoiced</label>
    <select name=plan_code id=inv_plan>{plan_price_opts}</select>
    <label>Amount before GST (₹)</label>
    <input name=amount_rupees id=inv_amount type=number step="0.01" min="1" required>
    <div class="small muted" id=inv_calc style="margin-top:.3rem"></div>
    <label>Payment method</label>
    <select name=method>{method_opts}</select>
    <label>Invoice date</label>
    <input name=issued_on type=date value="{today}" max="{today}" required>
    <label>Billing period (months)</label>
    <input name=period_months type=number min=1 max=60 value=1 required>
    <label>Reference — UTR, cheque no. (optional)</label>
    <input name=reference placeholder="e.g. UTR SBIN0123456789">
    <label>Note for the invoice / audit (min 10 chars)</label>
    <input name=reason required minlength=10 placeholder="e.g. NEFT received against Growth annual">
    <button type=submit>Issue invoice</button>
  </form>
  <script>
  (function(){{
    var sel=document.getElementById('inv_plan'), amt=document.getElementById('inv_amount'),
        out=document.getElementById('inv_calc');
    function fill(){{
      var p=sel.options[sel.selectedIndex].dataset.price|0;
      if(!amt.value) amt.value=(p/100).toFixed(2);
      calc();
    }}
    function calc(){{
      var base=Math.round(parseFloat(amt.value||0)*100);
      if(!base){{out.textContent='';return;}}
      var tax=Math.round(base*0.18);
      out.textContent='GST 18%: ₹'+(tax/100).toLocaleString('en-IN')
        +'  ·  Total payable: ₹'+((base+tax)/100).toLocaleString('en-IN');
    }}
    sel.addEventListener('change',function(){{amt.value='';fill();}});
    amt.addEventListener('input',calc); fill();
  }})();
  </script>
</div>"""

    inv_rows = "".join(
        f"<tr><td class=small><code>{e(i['receipt_number'] or '—')}</code>"
        + (f"<div class=small muted>CN {e(i['credit_note_number'])}</div>" if i.get('credit_note_number') else "")
        + f"</td><td>{when(i.get('issued_at') or i['created_at'], '%d %b %Y')}</td>"
        f"<td class=num>{money(i['amount_paise'])}</td>"
        f"<td>{_inv_status(i)}</td>"
        f"<td class=small>{e(METHODS.get(i.get('method') or '', i.get('method') or '—'))}"
        + (f"<div class=muted>{e(i['payment_reference'])}</div>" if i.get('payment_reference') else "")
        + f"</td><td class=small>{_inv_actions(owner, i, csrf)}</td></tr>"
        for i in invoices_list)

    # ---- tables -------------------------------------------------------
    def table(title, head, rows_html, empty):
        return f"""<div class="card"><h2>{title}</h2><table>
<thead><tr>{''.join(f'<th>{h}</th>' for h in head)}</tr></thead>
<tbody>{rows_html or f'<tr><td colspan={len(head)} class=muted>{empty}</td></tr>'}</tbody>
</table></div>"""

    agents_html = "".join(
        f"<tr><td><code>{e(a['slug'])}</code></td><td>{e(a['display_name'] or '—')}</td>"
        f"<td>{pill('archived', 'rose') if a['archived_at'] else pill('active', 'mint')}</td></tr>"
        for a in agents)
    tel_html = "".join(
        f"<tr><td>{e(n['number'])}</td><td><code>{e(n['inbound_agent_slug'] or '—')}</code></td>"
        f"<td><code>{e(n['outbound_agent_slug'] or '—')}</code></td>"
        f"<td>{pill(n['config_status'] or '—')}</td></tr>" for n in numbers)
    trunk_html = "".join(
        f"<tr><td>{e(t['name'])}</td><td>{e(t['provider'])}</td>"
        f"<td>{pill(t['config_status'] or '—')}</td></tr>" for t in trunks)
    subs_html = "".join(
        f"<tr><td>{e(s['plan_code'])}</td><td>{e(s['kind'])}</td>"
        f"<td>{pill(s['status'])}</td><td>{when(s['current_end'], '%d %b %Y')}</td>"
        f"<td class=small><code>{e(s['razorpay_subscription_id'] or s['razorpay_order_id'] or '—')}</code></td></tr>"
        for s in subs)
    pays_html = "".join(
        f"<tr><td>{when(p['created_at'], '%d %b %Y')}</td><td>{e(p['purpose'])}</td>"
        f"<td class=num>{money(p['amount_paise'])}</td><td>{pill(p['status'], 'mint' if p['status'] == 'captured' else 'rose')}</td>"
        f"<td class=small><code>{e(p['receipt_number'] or '—')}</code></td></tr>" for p in pays)
    att_html = "".join(
        f"<tr><td>{when(a['created_at'], '%d %b %H:%M')}</td><td>{e(a['plan_code'] or a['kind'])}</td>"
        f"<td class=num>{money(a['amount_paise'])}</td><td>{pill(a['status'], 'mint' if a['status'] == 'captured' else 'rose')}</td>"
        f"<td class=small muted>{e((a['failure_reason'] or '')[:70])}</td></tr>" for a in attempts)
    calls_html = "".join(
        f"<tr><td>{when(c['started_at'], '%d %b %H:%M')}</td><td>{e(c['direction'] or '—')}</td>"
        f"<td>{e(c['phone'] or '—')}</td><td class=num>{num(c['duration_s'])}s</td>"
        f"<td><code>{e(c['agent_slug'] or '—')}</code></td><td>{e(c['outcome'] or '—')}</td></tr>"
        for c in calls)
    audit_html = "".join(
        f"<tr><td>{when(a['at'], '%d %b %H:%M')}</td><td><code>{e(a['action'])}</code></td>"
        f"<td class=small muted>{e(a['target'] or '')}</td></tr>" for a in audit_rows)

    body = f"""
<h1>{e(owner)}</h1>
<p class="sub"><a href="/admin">← all tenants</a></p>
<div class="grid side">
  <div>{overview}{credits_card}</div>
  <div>{actions}</div>
</div>
<div class="grid two">
  <div>
    {table('Agents', ['Slug', 'Name', 'State'], agents_html, 'No agents.')}
    {table('Numbers', ['Number', 'Inbound', 'Outbound', 'State'], tel_html, 'No numbers registered.')}
    {table('Trunks', ['Name', 'Provider', 'State'], trunk_html, 'No trunks.')}
  </div>
  <div>
    {table('Subscriptions', ['Plan', 'Kind', 'Status', 'Ends', 'Gateway id'], subs_html, 'No purchases.')}
    {table('Payments', ['Date', 'For', 'Amount', 'Status', 'Invoice'], pays_html, 'No payments.')}
    {table('Payment attempts', ['When', 'Plan', 'Amount', 'Status', 'Failure'], att_html, 'No attempts.')}
  </div>
</div>
{invoice_form}
{table('Invoices &amp; receipts' + (f' &mdash; <span class="pill amber">{money(dict(due_row)["due"])} outstanding</span>' if dict(due_row)['due'] else ''),
       ['Number', 'Issued', 'Amount', 'Status', 'Method', ''], inv_rows, 'No invoices yet.')}
{table('Recent calls', ['Started', 'Dir', 'Phone', 'Dur', 'Agent', 'Outcome'], calls_html, 'No calls yet.')}
{table('Recent activity', ['When', 'Action', 'Target'], audit_html, 'Nothing recorded.')}"""
    return page(owner, body, _flash(request))


# ---------------------------------------------------------------------------
# mutations (browser forms)
# ---------------------------------------------------------------------------

def _guard(request: Request, csrf: str, reason: str = "", min_len: int = 10) -> str:
    """Authorize + validate one console form submission.

    `allow_cookie_write=True` is the ONLY place a cookie may authorize a state
    change, and it is immediately paired with `_check_csrf`. The JSON API never
    passes it, so `/admin/api/*` stays bearer-header-only.
    """
    from admin_app import require_token
    require_token(request, allow_cookie_write=True)
    _check_csrf(request, csrf)
    reason = (reason or "").strip()
    if min_len and len(reason) < min_len:
        raise HTTPException(status_code=400,
                            detail=f"a reason of at least {min_len} characters is required")
    return reason[:500]


@ui.post("/tenant/{owner}/plan")
def ui_set_plan(owner: str, request: Request, csrf: str = Form(""),
                plan_code: str = Form(...), reason: str = Form(""),
                period_days: str = Form("")):
    reason = _guard(request, csrf, reason)
    from admin_app import audit
    from shared.billing.entitlements import now_utc, resolve_entitlements
    from shared.db.database import get_db

    overrides: dict = {"plan_code": plan_code}
    if (period_days or "").strip():
        try:
            days = int(period_days)
        except ValueError:
            return _back(owner, "err", "Expiry must be a whole number of days.")
        if not 1 <= days <= 3650:
            return _back(owner, "err", "Expiry must be between 1 and 3650 days.")
        start = now_utc()
        overrides["period_start"] = start.isoformat()
        overrides["period_end"] = (start + dt.timedelta(days=days)).isoformat()

    with get_db() as db:
        if not db.execute("SELECT 1 FROM plans WHERE code=?", (plan_code,)).fetchone():
            return _back(owner, "err", f"Unknown plan '{plan_code}'.")
        before = db.execute("SELECT plan_code, overrides FROM entitlements WHERE owner=?",
                            (owner,)).fetchone()
        db.execute(
            "INSERT INTO entitlements (owner, plan_code, overrides, updated_by, updated_at) "
            "VALUES (?,?,?, 'admin-ui', CURRENT_TIMESTAMP) "
            "ON CONFLICT (owner) DO UPDATE SET overrides=excluded.overrides, "
            "updated_by='admin-ui', updated_at=CURRENT_TIMESTAMP",
            (owner, plan_code, json.dumps(overrides)))

    ent = resolve_entitlements(owner, actor="admin-ui")
    audit(request, "tenant.plan", owner,
          {"reason": reason, "plan_code": plan_code, "overrides": overrides,
           "before": dict(before) if before else None})
    tail = f" until {overrides['period_end'][:10]}" if "period_end" in overrides else ""
    return _back(owner, "ok", f"Plan set to {ent['plan_code']}{tail}.")


@ui.post("/tenant/{owner}/lock")
def ui_lock(owner: str, request: Request, csrf: str = Form(""), reason: str = Form("")):
    reason = _guard(request, csrf, reason)
    from admin_app import audit
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("UPDATE entitlements SET status='locked', lock_reason=?, "
                   "updated_by='admin-ui', updated_at=CURRENT_TIMESTAMP WHERE owner=?",
                   (reason, owner))
    audit(request, "tenant.lock", owner, {"reason": reason})
    return _back(owner, "ok", "Account locked — calls and agent creation refused.")


@ui.post("/tenant/{owner}/unlock")
def ui_unlock(owner: str, request: Request, csrf: str = Form(""), reason: str = Form("")):
    reason = _guard(request, csrf, reason)
    from admin_app import audit
    from shared.billing.entitlements import resolve_entitlements
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("UPDATE entitlements SET status='ok', lock_reason=NULL, "
                   "updated_by='admin-ui', updated_at=CURRENT_TIMESTAMP WHERE owner=?",
                   (owner,))
    resolve_entitlements(owner, actor="admin-ui")
    audit(request, "tenant.unlock", owner, {"reason": reason})
    return _back(owner, "ok", "Account unlocked.")


@ui.post("/tenant/{owner}/pilot-reset")
def ui_pilot_reset(owner: str, request: Request, csrf: str = Form(""),
                   reason: str = Form("")):
    reason = _guard(request, csrf, reason)
    from admin_app import audit
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("UPDATE pilot_claims SET released_by='admin-ui', "
                   "released_at=CURRENT_TIMESTAMP, release_reason=? WHERE owner=?",
                   (reason, owner))
    audit(request, "tenant.pilot_reset", owner, {"reason": reason})
    return _back(owner, "ok", "Pilot claim released — they can buy the trial again.")


@ui.post("/tenant/{owner}/credits")
def ui_grant_credit(owner: str, request: Request, csrf: str = Form(""),
                    minutes: str = Form(""), valid_months: str = Form("1"),
                    reason: str = Form("")):
    """Reward a tenant with free minutes for a number of months.

    No payment row and no invoice: this is not money changing hands, and
    inventing a ₹0 receipt would put a fake sale in the GST series.
    """
    reason = _guard(request, csrf, reason)
    from admin_app import audit
    from shared.billing.credits import CreditError, grant_minutes
    try:
        grant = grant_minutes(owner, minutes, valid_months,
                              actor="admin-ui", note=reason)
    except CreditError as exc:
        return _back(owner, "err", str(exc))
    audit(request, "tenant.credit.grant", owner,
          {"reason": reason, "grant": grant})
    return _back(owner, "ok",
                 f"{int(grant['minutes']):,} minutes credited, valid until "
                 f"{str(grant['period_end'])[:10]}. Live on their next call.")


@ui.post("/tenant/{owner}/credits/{grant_id}/revoke")
def ui_revoke_credit(owner: str, grant_id: int, request: Request,
                     csrf: str = Form(""), reason: str = Form("")):
    reason = _guard(request, csrf, reason)
    from admin_app import audit
    from shared.billing.credits import CreditError, revoke_grant
    try:
        grant = revoke_grant(grant_id, actor="admin-ui", reason=reason)
    except CreditError as exc:
        return _back(owner, "err", str(exc))
    audit(request, "tenant.credit.revoke", owner,
          {"reason": reason, "grant_id": grant_id, "grant": grant})
    return _back(owner, "ok",
                 f"Credit withdrawn — {int(grant['minutes_remaining']):,} unused "
                 f"minutes removed from their allowance. Minutes already spent stand.")


@ui.post("/tenant/{owner}/reconcile")
def ui_reconcile(owner: str, request: Request, csrf: str = Form("")):
    _guard(request, csrf, min_len=0)
    from admin_app import audit
    from shared.billing.service import reconcile_owner
    result = reconcile_owner(owner, actor="admin-ui")
    audit(request, "tenant.reconcile", owner, result)
    if result.get("recovered"):
        return _back(owner, "ok", f"Recovered {result['recovered']} payment(s).")
    return _back(owner, "ok",
                 f"Checked {result.get('checked', 0)} attempt(s); nothing to recover."
                 + (f" ({result['notes'][0]})" if result.get("notes") else ""))


# ---------------------------------------------------------------------------
# platform settings + global views
# ---------------------------------------------------------------------------

@ui.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    from admin_app import audit, require_token
    require_token(request)
    audit(request, "ui.settings")
    from shared.billing import razorpay_client as rz
    from shared.billing import receipts
    from shared.billing.entitlements import enforcement_enabled
    enf = enforcement_enabled(refresh=True)
    ok, detail = receipts.platform_ready()
    csrf = _csrf_field(request)
    return page("Settings", f"""
<h1>Platform settings</h1>
<p class="sub"><a href="/admin">← all tenants</a></p>
<div class="grid two"><div>
<div class="card">
  <h2>Quota enforcement</h2>
  <p>Currently {pill('ON', 'mint') if enf else pill('OFF', 'rose')}.</p>
  <p class="small muted">When OFF, minute quotas, agent caps and Copilot budgets
     are not applied — every tenant behaves as unlimited. The telephony gate is
     independent and stays active either way.</p>
  <form class="act" method=post action="/admin/settings/enforcement">
    {csrf}
    <input type=hidden name=enabled value="{'0' if enf else '1'}">
    <label>Reason (audited, min 15 chars)</label>
    <input name=reason required minlength=15
           placeholder="why you are turning it {'off' if enf else 'on'}">
    <button type=submit style="background:{'#b42318' if enf else '#0b6bcb'}">
      Turn enforcement {'OFF' if enf else 'ON'}</button>
  </form>
</div></div><div>
<div class="card">
  <h2>Payment gateway</h2>
  <dl>
    <dt>Configured</dt><dd>{yn(rz.configured())}</dd>
    <dt>Webhook</dt><dd>{yn(rz.webhook_configured())}</dd>
    <dt>Mode</dt><dd>{e(rz.mode())}</dd>
  </dl>
  <p class="small muted">Set in <code>/etc/lescall/billing.env</code>, then
     <code>docker compose up -d api workers</code>.</p>
</div>
<div class="card">
  <h2>GST invoicing</h2>
  <dl><dt>Ready</dt><dd>{yn(ok)}</dd></dl>
  {f'<p class="small muted">{e(detail)}</p>' if not ok else ''}
</div>
</div></div>""", _flash(request))


@ui.post("/settings/enforcement")
def ui_enforcement(request: Request, csrf: str = Form(""), enabled: str = Form("0"),
                   reason: str = Form("")):
    reason = _guard(request, csrf, reason, min_len=15)
    from admin_app import audit
    from shared.billing.entitlements import set_enforcement
    on = enabled == "1"
    set_enforcement(on, actor=f"admin-ui: {reason}")
    audit(request, "enforcement", str(on), {"reason": reason})
    logger.critical("[admin] BILLING ENFORCEMENT set to %s :: %s", on, reason)
    from urllib.parse import quote
    return RedirectResponse(
        f"/admin/settings?flash=ok&msg={quote('Enforcement is now ' + ('ON' if on else 'OFF') + '.')}",
        status_code=303)


@ui.get("/payments", response_class=HTMLResponse)
def payments_page(request: Request):
    from admin_app import audit, require_token
    require_token(request)
    audit(request, "ui.payments")
    from shared.db.database import get_db
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM payments ORDER BY id DESC LIMIT 200").fetchall()]
        tot = db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount_paise),0) AS gross, "
            "COALESCE(SUM(refunded_paise),0) AS ref FROM payments "
            "WHERE status='captured'").fetchone()
    body = "".join(
        f"<tr><td>{when(p['created_at'], '%d %b %Y %H:%M')}</td>"
        f"<td><a href=\"/admin/tenant/{e(p['owner'])}\">{e(p['owner'])}</a></td>"
        f"<td>{e(p['purpose'])}</td><td class=num>{money(p['amount_paise'])}</td>"
        f"<td>{pill(p['status'], 'mint' if p['status'] == 'captured' else 'rose')}</td>"
        f"<td class=small><code>{e(p['receipt_number'] or '—')}</code></td></tr>"
        for p in rows)
    t = dict(tot or {})
    return page("Payments", f"""
<h1>Payments</h1>
<p class="sub">{num(t.get('n'))} captured · {money(t.get('gross'))} gross ·
   {money(t.get('ref'))} refunded</p>
<div class="card"><table>
<thead><tr><th>Date</th><th>Tenant</th><th>For</th><th class=num>Amount</th>
  <th>Status</th><th>Invoice</th></tr></thead>
<tbody>{body or '<tr><td colspan=6 class=muted>No payments recorded yet.</td></tr>'}</tbody>
</table></div>""", _flash(request))


@ui.get("/events", response_class=HTMLResponse)
def events_page(request: Request):
    from admin_app import audit, require_token
    require_token(request)
    audit(request, "ui.events")
    from shared.db.database import get_db
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT id, event_id, event_type, owner, processed_at, attempts, error, "
            "received_at FROM billing_events ORDER BY id DESC LIMIT 200").fetchall()]
    body = "".join(
        f"<tr><td>{when(r['received_at'], '%d %b %H:%M')}</td>"
        f"<td><code>{e(r['event_type'])}</code></td><td>{e(r['owner'] or '—')}</td>"
        f"<td>{pill('done', 'mint') if r['processed_at'] else pill('pending', 'rose')}</td>"
        f"<td class=num>{num(r['attempts'])}</td>"
        f"<td class=small muted>{e((r['error'] or '')[:80])}</td></tr>" for r in rows)
    return page("Events", f"""
<h1>Gateway events</h1>
<p class="sub">Newest 200. Anything stuck in <b>pending</b> for more than 15
   minutes is alarmed by the sync worker and replayed from its stored payload.</p>
<div class="card"><table>
<thead><tr><th>Received</th><th>Type</th><th>Tenant</th><th>State</th>
  <th class=num>Tries</th><th>Error</th></tr></thead>
<tbody>{body or '<tr><td colspan=6 class=muted>No webhook events yet — the gateway is not configured.</td></tr>'}</tbody>
</table></div>""", _flash(request))


# ---------------------------------------------------------------------------
# manual invoices
# ---------------------------------------------------------------------------

@ui.post("/tenant/{owner}/invoice")
def ui_issue_invoice(owner: str, request: Request, csrf: str = Form(""),
                     plan_code: str = Form(...), amount_rupees: str = Form(...),
                     method: str = Form(...), issued_on: str = Form(""),
                     period_months: str = Form("1"), reference: str = Form(""),
                     reason: str = Form("")):
    """Issue a manual tax invoice for money collected outside the gateway."""
    reason = _guard(request, csrf, reason)
    from admin_app import audit
    from shared.billing import invoices

    try:
        base_paise = int(round(float(amount_rupees) * 100))
    except (TypeError, ValueError):
        return _back(owner, "err", "Amount must be a number.")
    issued = None
    if issued_on.strip():
        try:
            # The operator picks a DATE; anchor it at midday IST so the
            # financial-year check can never be flipped by a timezone edge.
            d = dt.datetime.strptime(issued_on.strip(), "%Y-%m-%d")
            issued = d.replace(hour=12, tzinfo=IST).astimezone(dt.timezone.utc)
        except ValueError:
            return _back(owner, "err", "Invoice date must be YYYY-MM-DD.")

    try:
        inv = invoices.issue(
            owner=owner, plan_code=plan_code, base_paise=base_paise,
            method=method, issued_at=issued,
            period_months=int(period_months or 1),
            reference=reference.strip(), note=reason, actor="admin-ui")
    except invoices.InvoiceError as exc:
        return _back(owner, "err", str(exc))
    except Exception as exc:
        logger.exception("[admin] invoice issue failed for %s", owner)
        return _back(owner, "err", f"Could not issue the invoice: {exc}")

    audit(request, "invoice.issue", owner,
          {"reason": reason, "number": inv.get("receipt_number"),
           "amount_paise": inv.get("amount_paise"), "method": method,
           "plan_code": plan_code})
    settled = "recorded as paid" if inv.get("status") == "captured" else "issued on credit"
    return _back(owner, "ok",
                 f"Invoice {inv.get('receipt_number')} {settled} "
                 f"({money(inv.get('amount_paise'))}). {plan_code.title()} is active.")


@ui.post("/invoice/{payment_id}/paid")
def ui_mark_paid(payment_id: int, request: Request, csrf: str = Form(""),
                 method: str = Form(...), reference: str = Form("")):
    _guard(request, csrf, min_len=0)
    from admin_app import audit
    from shared.billing import invoices
    try:
        inv = invoices.mark_paid(payment_id, method=method,
                                 reference=reference.strip(), actor="admin-ui")
    except invoices.InvoiceError as exc:
        return _back(_owner_of(payment_id), "err", str(exc))
    audit(request, "invoice.paid", inv.get("owner", ""),
          {"number": inv.get("receipt_number"), "method": method,
           "reference": reference})
    return _back(inv.get("owner", ""), "ok",
                 f"Invoice {inv.get('receipt_number')} marked paid.")


@ui.post("/invoice/{payment_id}/void")
def ui_void_invoice(payment_id: int, request: Request, csrf: str = Form(""),
                    reason: str = Form("")):
    reason = _guard(request, csrf, reason)
    from admin_app import audit
    from shared.billing import invoices
    try:
        inv = invoices.void(payment_id, reason=reason, actor="admin-ui")
    except invoices.InvoiceError as exc:
        return _back(_owner_of(payment_id), "err", str(exc))
    audit(request, "invoice.void", inv.get("owner", ""),
          {"reason": reason, "number": inv.get("receipt_number"),
           "credit_note": inv.get("credit_note_number")})
    return _back(inv.get("owner", ""), "ok",
                 f"Invoice {inv.get('receipt_number')} cancelled — "
                 f"credit note {inv.get('credit_note_number')} issued.")


def _owner_of(payment_id: int) -> str:
    from shared.db.database import get_db
    try:
        with get_db() as db:
            row = db.execute("SELECT owner FROM payments WHERE id=?",
                             (payment_id,)).fetchone()
        return (row["owner"] or "") if row else ""
    except Exception:
        return ""


@ui.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request):
    """Collections view — what is owed, oldest first."""
    from admin_app import audit, require_token
    require_token(request)
    audit(request, "ui.invoices")
    from shared.billing import invoices as inv_mod

    due = inv_mod.outstanding()
    total = sum(int(i["amount_paise"] or 0) for i in due)
    def _row(i: dict) -> str:
        state = (pill(f"{i['days_overdue']}d overdue", "rose") if i["overdue"]
                 else pill("due", "amber"))
        link = f'<a href="/admin/tenant/{e(i["owner"])}">{e(i["owner"])}</a>'
        return (f"<tr><td class=small><code>{e(i['receipt_number'])}</code></td>"
                f"<td>{link}</td>"
                f"<td>{when(i.get('issued_at'), '%d %b %Y')}</td>"
                f"<td>{when(i.get('due_date'), '%d %b %Y')}</td>"
                f"<td class=num>{money(i['amount_paise'])}</td>"
                f"<td>{state}</td></tr>")

    rows = "".join(_row(i) for i in due)
    return page("Outstanding", f"""
<h1>Outstanding invoices</h1>
<p class="sub">{len(due)} unpaid · {money(total)} owed ·
   <a href="/admin/payments">all payments</a></p>
<div class="card"><table>
<thead><tr><th>Number</th><th>Tenant</th><th>Issued</th><th>Due</th>
  <th class=num>Amount</th><th>State</th></tr></thead>
<tbody>{rows or '<tr><td colspan=6 class=muted>Nothing outstanding.</td></tr>'}</tbody>
</table></div>""", _flash(request))
