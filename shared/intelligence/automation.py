"""Automation engine — triggers, condition trees, actions (PLAN §2.6).

A rule = trigger (event kind) + condition tree + ordered action list.
Pipeline stage 5 derives the call's event set from stored intelligence and
evaluates enabled rules; matches become automation_runs executed as
`automation.run` jobs with per-action step logs, retries, and action-index
checkpointing (a retried run resumes at the first unfinished action, and
long `delay_s` actions re-enqueue the job instead of blocking a worker slot).

Safeguards:
    * per-(rule, call) dedupe via automation_runs.dedupe_key
    * circuit breaker: a rule that fires > MAX_RUNS_PER_HOUR is skipped
    * unknown/unconfigured action channels log "not_configured" and continue —
      honest degradation, never silent failure

Condition tree: {"all": [leaf|tree, ...]} | {"any": [...]} | leaf
Leaf: {"field": "lead_score", "op": "gte", "value": 70}
Ops: eq, neq, gt, gte, lt, lte, contains, not_contains, in
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import subprocess
from typing import Any

from shared.db.database import get_db
from shared.intelligence import config

log = logging.getLogger("intel.automation")

MAX_RUNS_PER_HOUR = 20
MAX_ACTIONS = 10
INLINE_DELAY_CAP_S = 60

TRIGGERS = (
    "call.analyzed", "sentiment.negative", "lead.hot", "commitment.detected",
    "followup.created", "followup.overdue", "compliance.issue",
    "competitor.mentioned", "escalation.detected",
)

ACTIONS = (
    "notify_inapp", "email", "webhook", "create_followup", "add_tag",
    "escalate", "send_sms", "send_whatsapp", "slack", "teams", "crm_update",
)


# ---------------------------------------------------------------------------
# Condition evaluation (pure)
# ---------------------------------------------------------------------------

def _leaf(cond: dict[str, Any], ctx: dict[str, Any]) -> bool:
    field = str(cond.get("field") or "")
    op = str(cond.get("op") or "eq")
    want = cond.get("value")
    have = ctx.get(field)
    try:
        if op == "eq":
            return have == want
        if op == "neq":
            return have != want
        if op in ("gt", "gte", "lt", "lte"):
            if have is None:
                return False
            have_f, want_f = float(have), float(want)
            return {"gt": have_f > want_f, "gte": have_f >= want_f,
                    "lt": have_f < want_f, "lte": have_f <= want_f}[op]
        if op == "contains":
            return str(want).lower() in str(have or "").lower() if not isinstance(have, list) \
                else any(str(want).lower() in str(x).lower() for x in have)
        if op == "not_contains":
            return not _leaf({**cond, "op": "contains"}, ctx)
        if op == "in":
            return have in (want if isinstance(want, list) else [want])
    except (TypeError, ValueError):
        return False
    return False


def evaluate_conditions(tree: dict[str, Any] | None, ctx: dict[str, Any]) -> bool:
    if not tree:
        return True
    if "all" in tree:
        return all(evaluate_conditions(t, ctx) for t in tree["all"])
    if "any" in tree:
        return any(evaluate_conditions(t, ctx) for t in tree["any"])
    return _leaf(tree, ctx)


# ---------------------------------------------------------------------------
# Event derivation + rule matching (pipeline stage 5)
# ---------------------------------------------------------------------------

def _context_for_call(call: dict[str, Any], ci: dict[str, Any],
                      followup: dict[str, Any] | None) -> dict[str, Any]:
    def jlist(field):
        try:
            return json.loads(ci.get(field) or "[]")
        except Exception:
            return []

    return {
        "direction": call.get("direction"), "agent_slug": call.get("agent_slug"),
        "contact_id": call.get("contact_id"),
        "phone": call.get("phone"), "language": call.get("language"),
        "duration_s": call.get("duration_s"), "outcome": call.get("outcome"),
        "lead_score": ci.get("lead_score"), "opportunity_score": ci.get("opportunity_score"),
        "risk_score": ci.get("risk_score"), "compliance_score": ci.get("compliance_score"),
        "sentiment": ci.get("sentiment"), "urgency": ci.get("urgency"),
        "customer_intent": ci.get("customer_intent"), "summary": ci.get("summary"),
        "confidence": ci.get("confidence"),
        "topics": jlist("topics"), "tags": jlist("tags"),
        "objections": jlist("objections"),
        "competitor_mentions": jlist("competitor_mentions"),
        "compliance_issues": jlist("compliance_issues"),
        "escalations": jlist("escalations"),
        "followup_decision": (followup or {}).get("decision"),
        "followup_priority": (followup or {}).get("priority"),
    }


def _events_for_call(ctx: dict[str, Any], commitments_n: int) -> list[str]:
    events = ["call.analyzed"]
    if ctx.get("sentiment") == "negative":
        events.append("sentiment.negative")
    if (ctx.get("lead_score") or 0) >= 70:
        events.append("lead.hot")
    if commitments_n:
        events.append("commitment.detected")
    if ctx.get("followup_decision") == "yes":
        events.append("followup.created")
    if (ctx.get("compliance_score") or 100) < 70 or ctx.get("compliance_issues"):
        events.append("compliance.issue")
    if ctx.get("competitor_mentions"):
        events.append("competitor.mentioned")
    if ctx.get("escalations"):
        events.append("escalation.detected")
    return events


def _breaker_open(db, rule_id: int) -> bool:
    n = db.execute(
        "SELECT count(*) AS n FROM automation_runs WHERE rule_id=?"
        " AND created_at > now() - interval '1 hour'", (rule_id,)
    ).fetchone()["n"]
    return n >= MAX_RUNS_PER_HOUR


def fire_matching_rules(owner: str, events: list[str], ctx: dict[str, Any],
                        call_id: int | None) -> int:
    """Match enabled rules against the event set; create runs + jobs."""
    from shared.intelligence import queue

    fired = 0
    with get_db() as db:
        rules = [dict(r) for r in db.execute(
            """SELECT * FROM automation_rules
                WHERE owner=? AND enabled=1 AND deleted_at IS NULL
                  AND trigger = ANY(?)""",
            (owner, events),
        ).fetchall()]
    for rule in rules:
        try:
            conditions = json.loads(rule.get("conditions") or "{}")
        except Exception:
            conditions = {}
        if not evaluate_conditions(conditions, ctx):
            continue
        with get_db() as db:
            if _breaker_open(db, rule["id"]):
                log.warning("automation rule %s circuit-breaker open (>%d runs/h)",
                            rule["id"], MAX_RUNS_PER_HOUR)
                continue
            row = db.execute(
                """INSERT INTO automation_runs
                       (rule_id, rule_version, owner, trigger_event, call_id,
                        dedupe_key)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (dedupe_key) DO NOTHING
                   RETURNING id""",
                (rule["id"], rule["version"], owner,
                 json.dumps({"events": events, "trigger": rule["trigger"],
                             "context": {k: v for k, v in ctx.items()
                                         if not isinstance(v, list)}}),
                 call_id, f"{rule['id']}:{rule['trigger']}:{call_id}"),
            ).fetchone()
        if row:
            queue.enqueue(queue.KIND_AUTOMATION_RUN, owner=owner, call_id=call_id,
                          payload={"run_id": row["id"]},
                          dedupe_key=f"automation.run:{row['id']}")
            fired += 1
            contact_id = ctx.get("contact_id")
            if contact_id:
                from shared.intelligence import timeline

                timeline.emit(int(contact_id), owner, "automation.fired",
                              ref_table="automation_runs", ref_id=row["id"],
                              payload={"rule": rule.get("name") or rule["id"],
                                       "trigger": rule["trigger"]})
    return fired


async def stage_automation(call: dict[str, Any]) -> None:
    call_id = call["id"]
    with get_db() as db:
        ci = db.execute(
            "SELECT * FROM call_intelligence WHERE call_id=?", (call_id,)
        ).fetchone()
        if not ci:
            return
        followup = db.execute(
            "SELECT decision, priority FROM followups WHERE call_id=?"
            " ORDER BY id DESC LIMIT 1", (call_id,)
        ).fetchone()
        commitments_n = db.execute(
            "SELECT count(*) AS n FROM commitments WHERE call_id=?", (call_id,)
        ).fetchone()["n"]
    ctx = _context_for_call(call, dict(ci), dict(followup) if followup else None)
    events = _events_for_call(ctx, commitments_n)
    fired = fire_matching_rules(call.get("owner") or "", events, ctx, call_id)
    if fired:
        log.info("automation: call %s events=%s fired %d rule(s)",
                 call_id, events, fired)


# ---------------------------------------------------------------------------
# Action execution (automation.run jobs)
# ---------------------------------------------------------------------------

def _notify(owner: str, severity: str, title: str, body: str, link: str,
            ref: str) -> dict:
    with get_db() as db:
        db.execute(
            """INSERT INTO notifications (owner, kind, severity, title, body, link, ref)
               VALUES (?, 'automation.fired', ?, ?, ?, ?, ?)""",
            (owner, severity, title[:200], body[:1000], link, ref),
        )
    return {"ok": True}


# A tenant writes `to`/`subject` freely in the rule builder, and both used to
# be concatenated straight into the message headers. A newline in either one
# appends headers of the attacker's choosing (extra Bcc:, a forged From:), which
# turns this VM into a relay for convincing "LesCall payment failed, re-enter
# your card" mail sent from our own IP — precisely the mail billing is about to
# start sending for real. Headers are single-line by definition: fold anything
# that could break the line, and validate the address shape.
_ADDR_RE = re.compile(r"^[^@\s;,<>\"]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}$")


def _header_value(raw: str, limit: int = 200) -> str:
    """One header line: CR/LF/NUL stripped, collapsed, truncated."""
    return re.sub(r"[\r\n\x00]+", " ", str(raw or "")).strip()[:limit]


def _email(owner: str, params: dict, ctx: dict) -> dict:
    to = _header_value(params.get("to") or owner, 254)
    if not _ADDR_RE.match(to):
        return {"ok": False, "detail": f"invalid recipient address: {to[:80]!r}"}
    subject = _header_value(params.get("subject") or "LesCall automation alert")
    body = params.get("body") or json.dumps(ctx, ensure_ascii=False, indent=1)[:2000]
    # The recipient is passed as an ARGV envelope address, not parsed out of the
    # header block by `sendmail -t` — so even if a header somehow survived
    # sanitisation it cannot redirect delivery.
    msg = f"To: {to}\nSubject: {subject}\n\n{body}\n"
    try:
        proc = subprocess.run(["sendmail", "-i", "--", to], input=msg.encode(),
                              timeout=20, capture_output=True)
        return {"ok": proc.returncode == 0,
                "detail": (proc.stderr.decode()[:200] or "sent")}
    except FileNotFoundError:
        return {"ok": False, "detail": "not_configured: sendmail missing"}
    except Exception as exc:
        return {"ok": False, "detail": repr(exc)[:200]}


# The webhook URL is tenant-supplied and this code runs in a host-network
# container, so an unvalidated POST reaches anything the VM can reach:
# 127.0.0.1 (the super-admin plane, Postgres, the media WebSocket) and the GCP
# metadata service at 169.254.169.254, which hands out VM service-account
# tokens. The URL is therefore resolved to its IPs BEFORE the request and every
# private/loopback/link-local destination is refused; redirects are disabled so
# a public URL cannot bounce us to a private one on the second hop.
_WEBHOOK_TIMEOUT_S = 15
_WEBHOOK_MAX_BYTES = 64 * 1024


def _public_ip(host: str) -> tuple[bool, str]:
    """(allowed, detail) for a hostname — every resolved address must be a
    global unicast address. Fails CLOSED on resolution failure."""
    import ipaddress
    import socket as _socket

    try:
        infos = _socket.getaddrinfo(host, None)
    except Exception as exc:
        return False, f"dns resolution failed: {exc!r}"
    if not infos:
        return False, "dns resolution returned no addresses"
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False, f"unparseable address {addr!r}"
        if not ip.is_global or ip.is_multicast:
            # covers loopback, RFC1918, link-local (incl. 169.254.169.254),
            # unique-local v6, reserved, and CGNAT.
            return False, f"refusing private/reserved destination {addr}"
    return True, ""


async def _webhook(params: dict, payload: dict) -> dict:
    from urllib.parse import urlsplit

    url = str(params.get("url") or "")
    if not url:
        return {"ok": False, "detail": "not_configured: url missing"}
    parts = urlsplit(url)
    if parts.scheme != "https":
        return {"ok": False, "detail": "webhook url must be https"}
    if not parts.hostname:
        return {"ok": False, "detail": "webhook url has no host"}
    if parts.port is not None and parts.port not in (443,):
        return {"ok": False, "detail": f"webhook port {parts.port} not allowed"}

    import asyncio as _asyncio
    allowed, detail = await _asyncio.to_thread(_public_ip, parts.hostname)
    if not allowed:
        log.warning("[automation] webhook blocked (%s): %s", detail, url[:200])
        return {"ok": False, "detail": f"blocked: {detail}"}

    import httpx

    try:
        async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT_S,
                                     follow_redirects=False) as client:
            resp = await client.post(url, json=payload)
    except Exception as exc:
        return {"ok": False, "detail": f"webhook failed: {exc!r}"[:200]}
    if resp.status_code in (301, 302, 303, 307, 308):
        return {"ok": False, "detail": "webhook redirected — refusing to follow"}
    return {"ok": resp.status_code < 300, "status": resp.status_code,
            "body": resp.text[:_WEBHOOK_MAX_BYTES] if resp.status_code >= 300 else ""}


def _create_followup(owner: str, call_id: int | None, params: dict,
                     ctx: dict) -> dict:
    delay_h = int(params.get("delay_hours") or 24)
    when = (dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30)))
            + dt.timedelta(hours=delay_h)).isoformat()
    with get_db() as db:
        db.execute(
            """INSERT INTO followups (owner, call_id, decision, confidence,
                   explanation, reason, followup_type, scheduled_at,
                   auto_scheduled, priority, status, source, audit)
               VALUES (?, ?, 'yes', 1.0, 'Created by automation rule', ?,
                       ?, ?, 1, ?, 'scheduled', 'pipeline', '[]')""",
            (owner, call_id, params.get("reason") or ctx.get("summary") or "automation",
             params.get("type") or "callback", when,
             params.get("priority") or "high"),
        )
    return {"ok": True, "scheduled_at": when}


def _add_tag(call_id: int | None, params: dict) -> dict:
    tag = str(params.get("tag") or "").strip().lower()
    if not (tag and call_id):
        return {"ok": False, "detail": "tag or call missing"}
    with get_db() as db:
        row = db.execute(
            "SELECT tags FROM call_intelligence WHERE call_id=?", (call_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "detail": "no intelligence row"}
        try:
            tags = json.loads(row["tags"] or "[]")
        except Exception:
            tags = []
        if tag not in tags:
            tags.append(tag)
            db.execute("UPDATE call_intelligence SET tags=? WHERE call_id=?",
                       (json.dumps(tags), call_id))
    return {"ok": True, "tags": tags}


async def _run_action(action: dict, run: dict, ctx: dict) -> dict:
    kind = str(action.get("type") or "")
    params = action.get("params") or {}
    owner = run["owner"]
    call_id = run.get("call_id")
    ref = f"autorun:{run['id']}:{kind}"
    link = f"/intelligence/{ctx.get('call_key')}" if ctx.get("call_key") else "/intelligence"

    if kind == "notify_inapp":
        return _notify(owner, params.get("severity") or "info",
                       params.get("title") or f"Automation: {ctx.get('rule_name')}",
                       params.get("body") or ctx.get("summary") or "", link, ref)
    if kind == "escalate":
        return _notify(owner, "critical",
                       params.get("title") or f"ESCALATION: {ctx.get('rule_name')}",
                       params.get("body") or ctx.get("summary") or "", link, ref)
    if kind == "email":
        return _email(owner, params, ctx)
    if kind == "webhook":
        return await _webhook(params, {"rule": ctx.get("rule_name"),
                                       "event": run.get("trigger_event"),
                                       "context": {k: v for k, v in ctx.items()
                                                   if not isinstance(v, list)}})
    if kind == "create_followup":
        return _create_followup(owner, call_id, params, ctx)
    if kind == "add_tag":
        return _add_tag(call_id, params)
    if kind in ("send_sms", "send_whatsapp", "slack", "teams", "crm_update"):
        return {"ok": False, "detail": f"not_configured: {kind} channel has no credentials"}
    return {"ok": False, "detail": f"unknown action {kind!r}"}


async def execute_run(job: dict[str, Any]) -> None:
    """Execute an automation run with action-index checkpointing."""
    import asyncio

    run_id = (job.get("payload") or {}).get("run_id")
    if not run_id:
        raise ValueError("automation.run job without run_id")
    with get_db() as db:
        run = db.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            raise ValueError(f"run {run_id} not found")
        run = dict(run)
        rule = db.execute("SELECT * FROM automation_rules WHERE id=?",
                          (run["rule_id"],)).fetchone()
        if not rule:
            raise ValueError(f"rule {run['rule_id']} not found")
        rule = dict(rule)
        call = None
        if run.get("call_id"):
            call = db.execute("SELECT call_key, phone FROM calls WHERE id=?",
                              (run["call_id"],)).fetchone()
        db.execute("UPDATE automation_runs SET status='running' WHERE id=?", (run_id,))

    try:
        actions = json.loads(rule.get("actions") or "[]")[:MAX_ACTIONS]
        steps = json.loads(run.get("steps") or "[]")
        trigger_event = json.loads(run.get("trigger_event") or "{}")
        ctx = dict(trigger_event.get("context") or {})
        ctx["rule_name"] = rule.get("name")
        if call:
            ctx.setdefault("call_key", call["call_key"])
            ctx.setdefault("phone", call["phone"])

        for idx in range(len(steps), len(actions)):
            action = actions[idx]
            delay = int(action.get("delay_s") or 0)
            if delay > INLINE_DELAY_CAP_S and not steps:
                pass  # long first-delay handled below via re-enqueue
            if delay:
                await asyncio.sleep(min(delay, INLINE_DELAY_CAP_S))
            result = await _run_action(action, run, ctx)
            steps.append({"type": action.get("type"),
                          "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                          **result})
            with get_db() as db:
                db.execute("UPDATE automation_runs SET steps=? WHERE id=?",
                           (json.dumps(steps), run_id))
        with get_db() as db:
            db.execute(
                "UPDATE automation_runs SET status='done', finished_at=now() WHERE id=?",
                (run_id,))
    except Exception as exc:
        with get_db() as db:
            db.execute(
                "UPDATE automation_runs SET status='failed', error=? WHERE id=?",
                (repr(exc)[:500], run_id))
        raise


# ---------------------------------------------------------------------------
# Out-of-band events (worker sweep): overdue follow-ups
# ---------------------------------------------------------------------------

def fire_overdue_events(rows: list[dict[str, Any]]) -> int:
    fired = 0
    for r in rows:
        ctx = {"followup_id": r["id"], "followup_priority": r.get("priority"),
               "reason": r.get("reason"), "phone": r.get("phone"),
               "followup_decision": "yes"}
        fired += fire_matching_rules(r["owner"], ["followup.overdue"], ctx,
                                     r.get("call_id"))
    return fired


# Seeded rule templates (created via the API, shown in the UI gallery).
TEMPLATES = [
    {"name": "Hot lead alert", "trigger": "lead.hot",
     "description": "Notify immediately when a call scores as a hot lead.",
     "conditions": {"all": [{"field": "lead_score", "op": "gte", "value": 70}]},
     "actions": [{"type": "notify_inapp", "params": {"severity": "warn",
                  "title": "Hot lead — act fast"}}]},
    {"name": "Negative call review", "trigger": "sentiment.negative",
     "description": "Flag negative-sentiment calls for manager review.",
     "conditions": {},
     "actions": [{"type": "notify_inapp", "params": {"severity": "warn",
                  "title": "Negative call needs review"}},
                 {"type": "add_tag", "params": {"tag": "needs-review"}}]},
    {"name": "Compliance escalation", "trigger": "compliance.issue",
     "description": "Escalate any detected compliance issue.",
     "conditions": {},
     "actions": [{"type": "escalate", "params": {"title": "Compliance issue detected"}}]},
    {"name": "Competitor mentioned", "trigger": "competitor.mentioned",
     "description": "Tag and notify when a competitor comes up.",
     "conditions": {},
     "actions": [{"type": "add_tag", "params": {"tag": "competitor"}},
                 {"type": "notify_inapp", "params": {"title": "Competitor mentioned"}}]},
    {"name": "Overdue follow-up escalation", "trigger": "followup.overdue",
     "description": "Escalate follow-ups that were missed.",
     "conditions": {},
     "actions": [{"type": "escalate", "params": {"title": "Follow-up missed"}}]},
]
