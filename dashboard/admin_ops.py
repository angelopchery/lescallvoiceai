"""Admin console — platform operations surfaces.

What lives here is everything an operator currently needs SSH, `psql` or
`docker ps` for. Split from admin_ui.py (tenants + money) so the two stay
readable.

Deliberate constraint: this module adds NO new credentials to the admin
container. It reads ports by connecting to them, state from files the repo
mount already exposes, and everything else from the database. In particular it
does NOT take FS_ESL_PASSWORD — the admin plane can already change plans and
issue invoices, and giving it the ability to originate calls as well would
widen the blast radius of a leaked ADMIN_TOKEN for a liveness number that port
8021 plus recent call activity already answers.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import socket
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from admin_ui import (IST, _check_csrf, _csrf_field, _flash, e, money, num,
                      page, pill, when, yn)

logger = logging.getLogger("lescall.admin.ops")

ops = APIRouter(prefix="/admin")

REPO = Path("/home/angelo/lescall")
LIVE_STATE = REPO / "phase14_runtime" / "live_calls_state.json"

# host:port -> what breaks if it is down
PORTS = [
    (8080, "API / dashboard", "tenants cannot log in; no dials can be placed"),
    (8081, "SPA container", "the web app does not load"),
    (9000, "socket_server (ESL)", "FreeSWITCH cannot hand calls to the AI"),
    (9001, "media WebSocket", "calls connect but carry no audio"),
    (8021, "FreeSWITCH ESL", "no outbound calls can be originated"),
    (5432, "Postgres", "everything stops"),
]


def _tenant_link(owner) -> str:
    """Owner cell. A separate function because building an anchor inside an
    f-string needs escaped quotes, which Python refuses in an expression part."""
    if not owner:
        return '<span class="muted">—</span>'
    return f'<a href="/admin/tenant/{e(owner)}">{e(owner)}</a>'


def _listening(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except Exception:
        return False


def _disk(path: str = "/") -> dict:
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        return {"total": total, "free": free,
                "pct": round((total - free) / total * 100) if total else 0}
    except Exception:
        return {}


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}PB"


def _live_calls() -> dict:
    try:
        data = json.loads(LIVE_STATE.read_text(encoding="utf-8")) or {}
        age = dt.datetime.now().timestamp() - LIVE_STATE.stat().st_mtime
        return {"calls": data.get("calls") or [], "age_s": int(age)}
    except Exception as exc:
        return {"calls": [], "error": repr(exc)[:120], "age_s": None}


# ---------------------------------------------------------------------------
# platform health — the incident page
# ---------------------------------------------------------------------------

@ops.get("/health", response_class=HTMLResponse)
def health_page(request: Request):
    from admin_app import audit, require_token
    require_token(request)
    audit(request, "ui.health")
    from shared.db.database import get_db

    port_rows = ""
    down = 0
    for port, name, impact in PORTS:
        up = _listening(port)
        down += 0 if up else 1
        port_rows += (
            f"<tr><td><b>{e(name)}</b><div class='small muted'>:{port}</div></td>"
            f"<td>{pill('up', 'mint') if up else pill('DOWN', 'rose')}</td>"
            f"<td class='small muted'>{'' if up else e(impact)}</td></tr>")

    disk = _disk()
    live = _live_calls()

    # Freshness signals: is anything actually still working?
    with get_db() as db:
        last_call = db.execute(
            "SELECT MAX(started_at) AS t FROM calls").fetchone()
        last_ledger = db.execute(
            "SELECT MAX(updated_at) AS t FROM usage_ledger").fetchone()
        last_intel = db.execute(
            "SELECT MAX(created_at) AS t FROM intel_jobs").fetchone()
        unrec = db.execute(
            "SELECT COUNT(*) AS n FROM usage_ledger WHERE status<>'reconciled'"
        ).fetchone()
        open_calls = db.execute(
            "SELECT COUNT(*) AS n FROM usage_ledger WHERE status='open'").fetchone()
        failopen = db.execute(
            "SELECT COUNT(*) AS n FROM usage_ledger "
            "WHERE entitlement_check IN ('error','unattributed')").fetchone()
        stuck = db.execute(
            "SELECT COUNT(*) AS n FROM billing_events WHERE processed_at IS NULL"
        ).fetchone()
        jobs = [dict(r) for r in db.execute(
            "SELECT status, COUNT(*) AS n FROM intel_jobs GROUP BY status").fetchall()]
        pending_appr = db.execute(
            "SELECT COUNT(*) AS n FROM followups WHERE approval_state='pending' "
            "AND status='scheduled'").fetchone()

    from shared.billing import razorpay_client as rz
    from shared.billing.entitlements import enforcement_enabled, fail_open_state
    fo = fail_open_state()

    def stale(v, hours: int) -> str:
        t = _as_dt(v)
        if not t:
            return pill("never", "rose")
        age = (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600
        return (pill(f"{when(v, '%d %b %H:%M')}", "mint") if age < hours
                else pill(f"{when(v, '%d %b %H:%M')} — stale", "amber"))

    disk_tone = "rose" if disk.get("pct", 0) >= 90 else (
        "amber" if disk.get("pct", 0) >= 80 else "mint")

    job_bits = " · ".join(f"{r['status']}={r['n']}" for r in jobs) or "none"
    integrity = []
    if int(unrec["n"] or 0):
        integrity.append(f"{unrec['n']} unreconciled ledger row(s)")
    if int(failopen["n"] or 0):
        integrity.append(f"{failopen['n']} call(s) admitted without a verified "
                         f"entitlement")
    if int(stuck["n"] or 0):
        integrity.append(f"{stuck['n']} unprocessed gateway event(s)")
    if fo:
        integrity.append(f"fail-open active for {len(fo)} owner(s)")

    return page("Health", f"""
<h1>Platform health</h1>
<p class="sub">{'All listeners up' if not down else f'{down} listener(s) DOWN'} ·
   {len(live['calls'])} call(s) live ·
   <a href="/admin">tenants</a></p>

<div class="grid two"><div>
<div class="card">
  <h2>Listeners</h2>
  <table><tbody>{port_rows}</tbody></table>
</div>

<div class="card">
  <h2>Live calls</h2>
  {_live_table(live)}
</div>
</div><div>

<div class="card">
  <h2>Freshness</h2>
  <dl>
    <dt>Last call</dt><dd>{stale(last_call['t'], 48)}</dd>
    <dt>Metering</dt><dd>{stale(last_ledger['t'], 6)}</dd>
    <dt>Intel worker</dt><dd>{stale(last_intel['t'], 48)}</dd>
    <dt>Calls open now</dt><dd>{num(open_calls['n'])}</dd>
    <dt>Intel jobs</dt><dd class="small">{e(job_bits)}</dd>
    <dt>Follow-ups awaiting approval</dt>
      <dd>{num(pending_appr['n'])}
        {pill('needs a human', 'amber') if int(pending_appr['n'] or 0) else ''}</dd>
  </dl>
</div>

<div class="card">
  <h2>Disk</h2>
  <dl>
    <dt>Root filesystem</dt>
    <dd>{pill(str(disk.get('pct', '?')) + '% used', disk_tone)}
        <div class="small muted">{_human(disk.get('free', 0))} free of
        {_human(disk.get('total', 0))}</div></dd>
  </dl>
  <p class="small muted">FreeSWITCH recordings and logs are the usual cause of
     growth — the VM filled to 95% once before (RUNBOOK).</p>
</div>

<div class="card">
  <h2>Billing integrity</h2>
  <dl>
    <dt>Enforcement</dt><dd>{pill('ON', 'mint') if enforcement_enabled(refresh=True) else pill('OFF', 'rose')}</dd>
    <dt>Gateway</dt><dd>{yn(rz.configured())} <span class="small muted">({e(rz.mode())})</span></dd>
  </dl>
  {'<p class="small" style="color:#9b1c1c;margin-top:.6rem">⚠ ' + '<br>⚠ '.join(e(i) for i in integrity) + '</p>'
   if integrity else '<p class="small" style="color:#0b6b45;margin-top:.6rem">No integrity issues.</p>'}
  <p class="small muted"><a href="/admin/events">gateway events</a> ·
     <a href="/admin/invoices">outstanding</a></p>
</div>
</div></div>""", _flash(request))


def _as_dt(v):
    if not v:
        return None
    try:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _live_table(live: dict) -> str:
    if live.get("error"):
        return f'<p class="small muted">Live state unreadable: {e(live["error"])}</p>'
    if not live["calls"]:
        age = live.get("age_s")
        return ('<p class="muted">No calls in progress.</p>'
                + (f'<p class="small muted">snapshot {age}s old</p>'
                   if age is not None else ""))
    rows = "".join(
        f"<tr><td>{e(c.get('caller') or c.get('phone') or '—')}</td>"
        f"<td><code>{e(c.get('agent_slug') or '—')}</code></td>"
        f"<td class=small>{e(c.get('owner') or '—')}</td>"
        f"<td class=small muted>{e(str(c.get('duration') or c.get('started_at') or ''))[:19]}</td></tr>"
        for c in live["calls"])
    return (f"<table><thead><tr><th>Caller</th><th>Agent</th><th>Tenant</th>"
            f"<th>Since</th></tr></thead><tbody>{rows}</tbody></table>"
            f"<p class='small muted'>snapshot {live.get('age_s')}s old</p>")


# ---------------------------------------------------------------------------
# Sarvam key pool — platform infrastructure, admin-only by doctrine
# ---------------------------------------------------------------------------

def _key_files() -> dict:
    """Read the key pool's file-based state.

    Deliberately files, not the database: the pool is PLATFORM infrastructure
    shared by every tenant, never per-tenant data, and the rotation scripts on
    the host read the same files. Keeping it out of the DB is what stops it
    ever being exposed through a tenant API by accident.
    """
    out = {}
    for name in ("api_key_pool", "api_key_state", "sarvam_key_status",
                 "sarvam_dead_keys", "sarvam_credits", "sarvam_key_master"):
        out[name] = _read_key_json(name)
    return out


def _read_key_json(name: str):
    try:
        return json.loads(
            (REPO / "dashboard" / f"{name}.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_key_json(name: str, data) -> None:
    """Atomic, owner-only write. These files hold LIVE API keys, so the mode is
    set on the temp file BEFORE the rename — the key is never world-readable,
    not even for the instant in between. Identical contract to
    sarvam_key_watchdog._write_json, which writes the same files from the
    other side."""
    path = REPO / "dashboard" / f"{name}.json"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _active_key() -> str:
    """The key the pipeline is using right now. calling-agent/.env is the
    single source of truth for "current" — both rotation paths (the in-process
    key_rotator and the watchdog service) write it there and re-read it per
    request."""
    try:
        m = re.search(r"^SARVAM_API_KEY=(.*)$",
                      (REPO / "calling-agent" / ".env").read_text(encoding="utf-8"),
                      re.MULTILINE)
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


def _unwrap(data, key: str) -> list:
    """These JSON stores are inconsistent: some are a bare list, others wrap it
    under a key ({"dead": [...]}, {"pool": [...]}). Reading the wrapper as the
    list itself reported 1 exhausted key when there were 11 — a number an
    operator would act on."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for candidate in (key, "keys", "pool", "dead"):
            if isinstance(data.get(candidate), list):
                return data[candidate]
    return []


def _mask(key: str) -> str:
    key = str(key or "")
    return f"{key[:8]}…{key[-4:]}" if len(key) > 14 else (key or "—")


@ops.get("/keys", response_class=HTMLResponse)
def keys_page(request: Request):
    from admin_app import audit, require_token
    require_token(request)
    audit(request, "ui.keys")

    f = _key_files()
    status = f.get("sarvam_key_status") or {}
    pool = _unwrap(f.get("api_key_pool"), "pool")
    dead = _unwrap(f.get("sarvam_dead_keys"), "dead")
    master = _unwrap(f.get("sarvam_key_master"), "keys")

    # ONE table over the master list, because that is the list an operator just
    # added to. The pool table alone could never show a freshly added key: the
    # pool is "master - dead - ACTIVE", and a new key usually becomes the active
    # one, so it was excluded from the only visible list by construction and
    # read as "my key did not go in" (asked 2026-08-31, twice).
    active = _active_key()
    dead_set = set(dead)

    # The headline number must include the key CURRENTLY SERVING CALLS. The
    # pool is "master - dead - active", so it excludes that key by
    # construction; reporting it as "keys remaining: 0 usable" told an operator
    # the platform was dry while a funded key was happily running every call
    # (asked 2026-08-31). Standby and working are different numbers, so the
    # page now shows both and never calls the standby count "remaining".
    standby = len(pool)
    in_use_ok = bool(active) and active not in dead_set
    working = standby + (1 if in_use_ok else 0)
    tone = "rose" if working == 0 else ("amber" if working <= 3 else "mint")

    def _state(k: str) -> tuple[str, str, int]:
        if k and k == active:
            return ("in use", "mint", 0)
        if k in dead_set:
            return ("exhausted", "rose", 2)
        return ("usable", "sky", 1)

    all_keys = list(master)
    if active and active not in all_keys:
        # .env edited by hand, or a key retired off the master list. Show it
        # rather than rendering a table that omits the key serving calls.
        all_keys.append(active)
    all_keys.sort(key=lambda k: _state(k)[2])   # stable: master order within a state

    key_rows = "".join(
        f"<tr><td><code>{e(_mask(k))}</code></td><td>{pill(label, tone)}</td>"
        f"<td class=small muted>{'not on the master list' if k not in master else ''}</td></tr>"
        for k, (label, tone, _) in ((k, _state(k)) for k in all_keys))
    csrf = _csrf_field(request)

    return page("API keys", f"""
<h1>Sarvam API keys</h1>
<p class="sub">Speech-to-text and text-to-speech for every call on the
   platform. One key serves calls at a time; the rest stand by. When the key in
   use runs out and no standby is left, <b>every tenant's calls go silent</b>.</p>

{'<div class="flash err">No working key. Calls are failing right now — add a credit-loaded key below.</div>' if working == 0 else ''}
{f'<div class="flash warn">Running on the last key. {e(_mask(active))} is serving every call and there is no standby: when its credit runs out, calls stop. Add another key.</div>' if working == 1 and in_use_ok else ''}

<div class="grid two"><div>
<div class="card">
  <h2>Status</h2>
  <dl>
    <dt>In use</dt><dd><code>{e(_mask(active) or '—')}</code>
        {'' if in_use_ok else pill('marked exhausted', 'rose')}</dd>
    <dt>Working keys</dt><dd>{pill(f'{working}', tone)}
        <span class="small muted">{1 if in_use_ok else 0} in use + {standby} standby</span></dd>
    <dt>Exhausted</dt><dd>{num(len(dead))} of {num(len(master))} on the master list</dd>
    <dt>Last change</dt><dd class="small">{e(status.get('updated') or '—')}</dd>
  </dl>
  <p class="small muted">Rotation is automatic on <b>HTTP 402</b> (out of
     credits): the key is retired and a standby takes over mid-call, no
     restart. This page is for adding keys BEFORE the standby count reaches
     zero. Sarvam publishes no balance API, so "exhausted" means the key
     actually 402'd on a live call — it is never predicted.</p>
</div>

<div class="card">
  <h2>Add keys</h2>
  <form class="act" method=post action="/admin/keys/add">
    {csrf}
    <label>One key per line (<code>sk_…</code>)</label>
    <textarea name=keys rows=5 required placeholder="sk_xxxxxxxx&#10;sk_yyyyyyyy"></textarea>
    <label>Reason (audited, min 10 chars)</label>
    <input name=reason required minlength=10 placeholder="e.g. topped up 5 keys, invoice #123">
    <button type=submit>Add keys</button>
  </form>
  <p class="small muted">Keys go on the <b>master list</b> — the only durable
     store. Standby is recomputed from it immediately, so a new key is live on
     the next call; nothing restarts. Re-adding a key that is currently
     exhausted restores it, for when you have topped its credit up on Sarvam.
     A key already in use stays in use — that is a no-op, not a failure.</p>
</div>
</div><div>

<div class="card">
  <h2>All keys ({len(all_keys)})</h2>
  <p class="small muted"><b>in use</b> is the single key serving calls right
     now. <b>usable</b> is the standby pool the pipeline rotates into on a 402
     — derived every 3s as <code>master − exhausted − in&nbsp;use</code>, so a
     key you add appears here only if it is not already the one in use.
     <b>exhausted</b> keys stay listed: re-adding one restores it, for when you
     have topped its credit up.</p>
  <table><thead><tr><th>Key</th><th>State</th><th></th></tr></thead>
    <tbody>{key_rows or '<tr><td class=muted>No keys at all — calls cannot run.</td></tr>'}</tbody></table>
</div>
</div></div>""", _flash(request))


@ops.post("/keys/add")
def keys_add(request: Request, csrf: str = Form(""), keys: str = Form(""),
             reason: str = Form("")):
    from admin_app import audit, require_token
    require_token(request, allow_cookie_write=True)
    _check_csrf(request, csrf)
    reason = (reason or "").strip()
    if len(reason) < 10:
        return _ops_back("/admin/keys", "err",
                         "A reason of at least 10 characters is required.")

    candidates = [k.strip() for k in (keys or "").splitlines() if k.strip()]
    good = [k for k in candidates if k.startswith("sk_") and len(k) >= 20]
    bad = [k for k in candidates if k not in good]
    if not good:
        return _ops_back("/admin/keys", "err",
                         "No usable keys found — they should look like sk_… "
                         "and be at least 20 characters.")

    # A key is added to the MASTER list, never to the pool.
    #
    # api_key_pool.json is DERIVED, not stored: sarvam-key-watchdog.service
    # rewrites it every 3 seconds to exactly "master - dead - active". This
    # used to append to the pool, so the form reported "Added 1 key(s)" and the
    # watchdog erased it before the operator could refresh the page - three
    # keys were added and lost that way on 2026-08-31, while 22 of 25 keys were
    # already exhausted and live calls were rotating every few minutes.
    master = _unwrap(_read_key_json("sarvam_key_master"), "keys")
    dead = set(_unwrap(_read_key_json("sarvam_dead_keys"), "dead"))
    active = _active_key()

    added = [k for k in good if k not in master]
    # Re-adding an exhausted key is the "I topped this one up" case, and it is
    # the ONLY way back out of the dead list - leave it there and the key can
    # never be used again however much credit it holds. Reported, not silent:
    # if it was not actually topped up the pipeline retires it on the next 402.
    revived = [k for k in good if k in dead]

    _write_key_json("sarvam_key_master", {"keys": master + added})
    if revived:
        _write_key_json("sarvam_dead_keys", {"dead": sorted(dead - set(good))})
        dead -= set(good)

    # Reconcile the pool here too, by the watchdog's own rule, so a new key is
    # usable on the very next call even if the watchdog service is down.
    pool = [k for k in master + added if k and k != active and k not in dead]
    _write_key_json("api_key_pool", {"pool": pool})

    audit(request, "keys.add", f"{len(added)} key(s)",
          {"reason": reason, "added": len(added), "rejected": len(bad),
           "revived": len(revived), "pool": len(pool),
           "masked": [_mask(k) for k in added]})
    logger.warning("[admin] %d Sarvam key(s) added to the master list "
                   "(%d revived, pool now %d) :: %s",
                   len(added), len(revived), len(pool), reason)
    # Report the outcome PER KEY. "Added 0 key(s). 0 usable in the pool. 1
    # already present." was the true summary of pasting a perfectly good key
    # that happened to already be the active one, and it read as a failure
    # (2026-08-31). A no-op has to say which key and why.
    parts = []
    if added:
        parts.append(f"Added {len(added)} key(s).")
    if revived:
        parts.append(f"Restored {len(revived)} key(s) from the exhausted list — "
                     "retired again on the next 402 if the credit was not "
                     "actually topped up.")
    unchanged = [k for k in good if k not in added and k not in revived]
    if active in unchanged:
        parts.append(f"{_mask(active)} is already the key in use — nothing to change.")
    others = [k for k in unchanged if k != active]
    if others:
        parts.append(f"{len(others)} key(s) already on the master list and on standby.")
    if bad:
        parts.append(f"{len(bad)} line(s) ignored (not sk_… or too short).")
    parts.append(f"{len(pool)} key(s) now on standby.")
    return _ops_back("/admin/keys", "ok", " ".join(parts))


def _ops_back(path: str, kind: str, text: str) -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse(f"{path}?flash={kind}&msg={quote(text)}",
                            status_code=303)


# ---------------------------------------------------------------------------
# cross-tenant call browser
# ---------------------------------------------------------------------------

@ops.get("/calls", response_class=HTMLResponse)
def calls_page(request: Request, q: str = "", owner: str = ""):
    from admin_app import audit, require_token
    require_token(request)
    audit(request, "ui.calls", owner or q)
    from shared.db.database import get_db

    where, params = [], []
    if owner:
        where.append("c.owner = ?")
        params.append(owner)
    if q.strip():
        where.append("(c.phone LIKE ? OR c.call_key LIKE ? OR c.conn_id LIKE ? "
                     "OR c.agent_slug LIKE ?)")
        params += [f"%{q.strip()}%"] * 4
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            f"SELECT c.id, c.started_at, c.owner, c.direction, c.phone, "
            f"c.duration_s, c.agent_slug, c.outcome, c.agent_health, "
            f"c.call_key, u.billable, u.entitlement_check, u.billsec_final "
            f"FROM calls c LEFT JOIN usage_ledger u ON u.conn_id = c.conn_id "
            f"{clause} ORDER BY c.id DESC LIMIT 200", tuple(params)).fetchall()]

    body = "".join(
        f"<tr><td class=small>{when(r['started_at'], '%d %b %H:%M')}</td>"
        f"<td class=small>{_tenant_link(r['owner'])}</td>"
        f"<td>{e(r['direction'] or '—')}</td><td>{e(r['phone'] or '—')}</td>"
        f"<td class=num>{num(r['duration_s'])}s</td>"
        f"<td><code class=small>{e(r['agent_slug'] or '—')}</code></td>"
        f"<td>{_health_pill(r['agent_health'])}</td>"
        f"<td class=small>{e(r['outcome'] or '—')}</td>"
        f"<td>{_billable_pill(r)}</td></tr>" for r in rows)

    return page("Calls", f"""
<h1>Calls</h1>
<p class="sub">Newest 200 across every tenant.{' Filtered by ' + e(owner) if owner else ''}</p>
<div class="card">
  <form class="searchrow" method=get action="/admin/calls">
    <div style="flex:1"><label>Search phone, call id, conn id or agent</label>
      <input name=q value="{e(q)}" placeholder="e.g. 9898042452 or hardik"></div>
    <button type=submit>Search</button>
    {'<a href="/admin/calls"><button type=button class=ghost>Clear</button></a>' if (q or owner) else ''}
  </form>
  <table><thead><tr><th>Started</th><th>Tenant</th><th>Dir</th><th>Phone</th>
    <th class=num>Dur</th><th>Agent</th><th>Health</th><th>Outcome</th>
    <th>Billing</th></tr></thead>
  <tbody>{body or '<tr><td colspan=9 class=muted>No calls match.</td></tr>'}</tbody></table>
</div>""", _flash(request))


def _health_pill(h) -> str:
    if not h or h == "ok":
        return pill("ok", "mint")
    if h == "unrouted":
        return pill("no agent bound", "rose")
    return pill(str(h)[:24], "amber")


def _billable_pill(r: dict) -> str:
    if r.get("billable") is None:
        return '<span class="muted small">no ledger</span>'
    if not r.get("billable"):
        return pill("not billed", "")
    chk = r.get("entitlement_check")
    if chk in ("error", "unattributed"):
        return pill(str(chk), "rose")
    return pill("billed", "mint")


# ---------------------------------------------------------------------------
# agents across tenants
# ---------------------------------------------------------------------------

@ops.get("/agents", response_class=HTMLResponse)
def agents_page(request: Request):
    from admin_app import audit, require_token
    require_token(request)
    audit(request, "ui.agents")
    from shared.db.database import get_db

    agents_dir = REPO / "calling-agent" / "agents"
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT a.slug, a.owner, a.display_name, a.archived_at, a.is_active, "
            "(SELECT COUNT(*) FROM calls c WHERE c.agent_slug=a.slug) AS calls "
            "FROM agents a ORDER BY a.owner NULLS FIRST, a.slug").fetchall()]

    body = ""
    orphans = 0
    for r in rows:
        on_disk = (agents_dir / (r["slug"] or "") / "agent.yaml").exists()
        if not on_disk and not r["archived_at"]:
            orphans += 1
        state = (pill("archived", "") if r["archived_at"]
                 else (pill("live", "mint") if on_disk else pill("no files", "rose")))
        owner_cell = (_tenant_link(r["owner"]) if r["owner"]
                      else pill("UNCLAIMED", "rose"))
        flag = pill("active", "sky") if r["is_active"] else ""
        name = e(r["display_name"] or "")
        body += (
            f"<tr><td><code>{e(r['slug'])}</code>"
            f"<div class='small muted'>{name}</div></td>"
            f"<td class=small>{owner_cell}</td>"
            f"<td>{state}</td><td>{flag}</td>"
            f"<td class=num>{num(r['calls'])}</td></tr>")

    return page("Agents", f"""
<h1>Agents</h1>
<p class="sub">{len(rows)} across every tenant
   {'· <b style="color:#9b1c1c">' + str(orphans) + ' with no files on disk</b>' if orphans else ''}</p>
<div class="card">
  <p class="small muted">An agent with an owned row but no files consumes a
     plan slot and 404s on archive — the create-rollback path used to leave
     these behind. "Active" is the legacy global fallback flag; it is scoped to
     the founding tenant at routing time so another tenant's agent can never
     answer an unrouted DID.</p>
  <table><thead><tr><th>Slug</th><th>Owner</th><th>State</th><th>Legacy flag</th>
    <th class=num>Calls</th></tr></thead>
  <tbody>{body or '<tr><td colspan=5 class=muted>No agents.</td></tr>'}</tbody></table>
</div>""", _flash(request))


# ---------------------------------------------------------------------------
# audit trail
# ---------------------------------------------------------------------------

@ops.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, q: str = "", limit: int = 200):
    from admin_app import audit, require_token
    require_token(request)
    audit(request, "ui.audit")
    from shared.db.database import get_db

    where, params = [], []
    if q.strip():
        where.append("(owner LIKE ? OR action LIKE ? OR target LIKE ?)")
        params += [f"%{q.strip()}%"] * 3
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            f"SELECT at, owner, action, target, ip FROM audit_events {clause} "
            f"ORDER BY id DESC LIMIT ?", (*params, min(int(limit), 1000))).fetchall()]

    body = "".join(
        f"<tr><td class=small>{when(r['at'], '%d %b %H:%M:%S')}</td>"
        f"<td class=small>{e(r['owner'] or '—')}</td>"
        f"<td><code class=small>{e(r['action'])}</code></td>"
        f"<td class=small>{e((r['target'] or '')[:60])}</td>"
        f"<td class='small muted'>{e(r['ip'] or '')}</td></tr>" for r in rows)

    return page("Audit", f"""
<h1>Audit trail</h1>
<p class="sub">Newest {len(rows)}. Admin actions are prefixed <code>admin.</code>
   and include reads, so a leaked token leaves a trail.</p>
<div class="card">
  <form class="searchrow" method=get action="/admin/audit">
    <div style="flex:1"><label>Search owner, action or target</label>
      <input name=q value="{e(q)}" placeholder="e.g. admin.tenant.plan"></div>
    <button type=submit>Search</button>
    {'<a href="/admin/audit"><button type=button class=ghost>Clear</button></a>' if q else ''}
  </form>
  <table><thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th>
    <th>Source</th></tr></thead>
  <tbody>{body or '<tr><td colspan=5 class=muted>Nothing recorded.</td></tr>'}</tbody></table>
</div>""", _flash(request))
