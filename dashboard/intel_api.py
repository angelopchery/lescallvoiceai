"""Intelligence layer API (plan: deploy/PLAN-intelligence-layer.md §2.11).

Mounted into dashboard/server.py exactly like telephony_api (APIRouter,
try/except include). Every endpoint is owner-scoped via the current_owner
dependency; the /api/intel prefix is registered in auth._TENANT_API_PREFIXES.

Endpoints are plain `def` (FastAPI threadpool) because the DB layer is sync —
only the semantic-search path is async (its embedding call is async httpx and
its scans run via asyncio.to_thread inside search_calls).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

logger = logging.getLogger("dashboard.intel")

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# The semantic-search arm embeds the query via INTEL_EMBED_/LLM_ keys living
# in calling-agent/.env. The worker loads that file explicitly; here it used
# to arrive only as a side effect of agent_builder_api's load_dotenv running
# first — a fragile coupling (if the builder failed to mount, search silently
# degraded to lexical-only). Load it directly, never overriding real env.
try:
    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / "calling-agent" / ".env")
except Exception:  # pragma: no cover - key absence just degrades search
    pass

from auth import current_owner  # noqa: E402
from contact_names import label_rows as _label_contacts  # noqa: E402
from shared.db.database import get_db  # noqa: E402
from shared.intelligence import queue as intel_queue  # noqa: E402
from shared.intelligence import search as intel_search  # noqa: E402
from shared.intelligence.embed import parse_turns  # noqa: E402
from shared.intelligence.memory import digest_for_contact  # noqa: E402

def require_feature(feature: str):
    """E12 — a per-tier capability gate, enforced AT THE API.

    Hiding a nav item is decoration, not enforcement: the endpoints stay
    reachable with a session and a URL. These dependencies are what actually
    separates the tiers.

    Note what is NOT gated: post-call PROCESSING still runs for every tenant
    on every call. It feeds follow-ups and platform analytics, and it means an
    upgrade retroactively reveals the customer's own history instead of
    leaving a hole for the months they were on a cheaper plan.
    """
    def _dep(request: Request, owner: str = Depends(current_owner)) -> str:
        if any(request.url.path.startswith(p) for p in _CROSS_TIER_PATHS):
            return owner
        try:
            from shared.billing.entitlements import feature_allowed
            from shared.billing.errors import BillingRefusal, CODE_PLAN_REQUIRED
        except Exception:
            return owner
        if not feature_allowed(owner, feature):
            raise BillingRefusal(
                _FEATURE_COPY.get(feature, "This feature is not included in "
                                            "your plan."),
                CODE_PLAN_REQUIRED, feature=feature)
        return owner
    return _dep


# Endpoints under /api/intel that every tier must keep, regardless of plan.
# The notification bell is the obvious one and it is not optional: billing
# itself delivers "payment failed" and "trial ending" through it, so gating it
# behind the Intelligence tier would silence exactly the messages a
# non-Intelligence tenant most needs to see.
_CROSS_TIER_PATHS = (
    "/api/intel/notifications",
)


_FEATURE_COPY = {
    "intelligence": "The Intelligence dashboard is included in Growth. "
                    "Upgrade to see what is happening inside your calls.",
    "transcripts": "Call transcripts are included in Growth. Upgrade to "
                   "search every conversation.",
    "summaries": "AI call summaries are included in Growth.",
    "conversation_analytics": "Conversation analytics are included in Growth.",
    "advanced_analytics": "Advanced analytics are included in Growth.",
    "recordings": "Call recordings are included in every paid plan.",
    "autocall": "Automated follow-up calls are included in Growth.",
}

# `intelligence` gates the whole prefix — it is the Intelligence dashboard.
# Individual routes below add narrower flags on top where the tiers differ.
router = APIRouter(prefix="/api/intel",
                   dependencies=[Depends(require_feature("intelligence"))])

_PAGE_LIMIT = 50


def _j(val: Any, default: str = "[]") -> Any:
    try:
        return json.loads(val or default)
    except Exception:
        return json.loads(default)


def _now_audit(by: str, action: str, detail: str = "") -> dict[str, str]:
    import datetime as dt

    return {"at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "by": by, "action": action, "detail": detail}


def _timeline_emit(contact_id: int, owner: str, kind: str, *,
                   ref_table: str | None = None, ref_id: int | None = None,
                   payload: dict | None = None) -> None:
    """Best-effort contact_events append — never fails the API call."""
    try:
        from shared.intelligence import timeline

        timeline.emit(contact_id, owner, kind, ref_table=ref_table,
                      ref_id=ref_id, payload=payload)
    except Exception as exc:
        logger.warning("timeline emit %s failed: %r", kind, exc)


# ---------------------------------------------------------------------------
# Unified intelligence dashboard rows
# ---------------------------------------------------------------------------

_SORTABLE = {"date": "c.started_at", "lead": "ci.lead_score",
             "risk": "ci.risk_score", "opportunity": "ci.opportunity_score",
             "duration": "c.duration_s"}


def _agent_list(agent: str) -> list[str]:
    """`?agent=` accepts ONE slug or a comma-separated set.

    The Intelligence filter can now scope to several agents at once, and a CSV
    keeps that on the existing parameter: a single slug is a one-item list, so
    every saved link, bookmark and API caller written against the old
    single-agent form keeps working unchanged.
    """
    return [a.strip() for a in (agent or "").split(",") if a.strip()]


def _like_escape(term: str) -> str:
    """Neutralize LIKE wildcards so a literal % or _ typed by the user matches
    itself. Pairs with the ESCAPE '\\' clause on every predicate below."""
    return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _search_clause(term: str) -> tuple[str, list[Any]]:
    """Free-text call filter used by /calls.

    Covers everything a user can plausibly paste into one box:

        contact name   — ct.name (the reason a name used to return far fewer
                         rows than the same contact's phone: the old clause
                         only looked at ci.summary, so a name matched by
                         accident when the LLM happened to write it)
        phone          — literal substring AND digits-only against sig_digits,
                         so "+91 70423-40227" and "7042340227" both hit
        call id        — call_key / conn_id, so the ID copied from Recordings
                         resolves here instead of returning nothing
        intelligence   — summary / customer_intent free text
        extracted sets — topics / objections / product_interest / tags, so a
                         topic clicked in Conversation Analytics lands on the
                         calls it came from (these are TEXT columns holding JSON
                         string arrays; a substring match over the raw array is
                         exactly the containment test we want)

    The contacts join must exist in BOTH the row query and the count query for
    this — see intel_calls.
    """
    from shared.phone import digits_only, significant_digits

    like = f"%{_like_escape(term)}%"
    cols = ["c.phone", "c.call_key", "c.conn_id", "ct.name",
            "ci.summary", "ci.customer_intent",
            "ci.topics", "ci.objections", "ci.product_interest", "ci.tags"]
    parts = [f"{col} ILIKE ? ESCAPE '\\'" for col in cols]
    params: list[Any] = [like] * len(cols)

    # A number typed with spaces/dashes/+91 never matches c.phone verbatim;
    # compare on the canonical routing key instead. Bounded at 15 digits (E.164
    # max) and letter-free so a pasted call_key — which is phone+timestamp, ~25
    # digits — does not get its tail mistaken for a phone number.
    digits = digits_only(term)
    if 4 <= len(digits) <= 15 and not any(ch.isalpha() for ch in term):
        parts.append("c.sig_digits LIKE ? ESCAPE '\\'")
        params.append(f"%{significant_digits(digits)}%")

    return "(" + " OR ".join(parts) + ")", params


def _call_filters(owner: str, *, search: str = "", sentiment: str = "",
                  decision: str = "", agent: str = "", days: int = 0,
                  min_lead: int = 0, call_direction: str = "",
                  analyzed_only: bool = False) -> tuple[list[str], list[Any]]:
    """The shared WHERE for "which calls?" → (clauses, params).

    One definition, used by the Intelligence list AND by filter-built reports,
    so a report generated from a filter covers exactly the calls that filter
    showed. Assumes the caller's FROM already joins call_intelligence ci and
    the latest followup as f (both LEFT, so an unanalyzed call still lists).
    """
    where = ["c.owner = ?"]
    params: list[Any] = [owner]
    if search.strip():
        clause, sparams = _search_clause(search.strip())
        where.append(clause)
        params += sparams
    if sentiment:
        where.append("ci.sentiment = ?")
        params.append(sentiment)
    agents = _agent_list(agent)
    if agents:
        # ANY() rather than "=" so one slug and twelve take the same path; a
        # single-agent filter is just a one-element set.
        where.append("c.agent_slug = ANY(?)")
        params.append(agents)
    if call_direction:
        where.append("c.direction = ?")
        params.append(call_direction)
    if days:
        # calls.started_at is a legacy naive TIMESTAMP (stored as UTC) — the
        # cast is required; comparing against ::text raises UndefinedFunction
        # and 500s the whole list (the "no calls shown" incident, 2026-07-21).
        where.append("c.started_at::timestamptz > now() - make_interval(days => ?)")
        params.append(days)
    if min_lead:
        where.append("ci.lead_score >= ?")
        params.append(min_lead)
    if decision:
        where.append("f.decision = ?")
        params.append(decision)
    if analyzed_only:
        # A report synthesizes STORED intelligence, so a call with none in it
        # contributes nothing but does consume one of the 1000 slots.
        where.append("ci.call_id IS NOT NULL")
    return where, params


@router.get("/calls")
def intel_calls(owner: str = Depends(current_owner),
                page: int = Query(1, ge=1), limit: int = Query(_PAGE_LIMIT, le=200),
                search: str = "", sentiment: str = "", decision: str = "",
                agent: str = "", days: int = Query(0, ge=0),
                min_lead: int = Query(0, ge=0, le=100),
                sort: str = "date", order: str = "desc"):
    where, params = _call_filters(
        owner, search=search, sentiment=sentiment, decision=decision,
        agent=agent, days=days, min_lead=min_lead)

    sort_col = _SORTABLE.get(sort, "c.started_at")
    direction = "ASC" if order == "asc" else "DESC"
    offset = (page - 1) * limit

    sql = f"""
        SELECT c.id AS call_id, c.call_key, c.phone, c.direction, c.agent_slug,
               c.started_at, c.duration_s, c.outcome, c.contact_id, c.language,
               ct.name AS contact_name,
               ci.summary, ci.customer_intent, ci.sentiment, ci.urgency,
               ci.lead_score, ci.opportunity_score, ci.risk_score,
               ci.compliance_score, ci.confidence, ci.low_confidence,
               ci.tags, ci.processed_at, ci.model,
               f.id AS followup_id, f.decision AS followup_decision,
               f.status AS followup_status, f.scheduled_at AS followup_at,
               f.priority AS followup_priority
          FROM calls c
          LEFT JOIN call_intelligence ci ON ci.call_id = c.id
          LEFT JOIN LATERAL (
                SELECT * FROM followups f2
                 WHERE f2.call_id = c.id AND f2.deleted_at IS NULL
                 ORDER BY f2.id DESC LIMIT 1
          ) f ON true
          LEFT JOIN contacts ct ON ct.id = c.contact_id
         WHERE {' AND '.join(where)}
         ORDER BY {sort_col} {direction} NULLS LAST
         LIMIT ? OFFSET ?"""
    with get_db() as db:
        rows = [dict(r) for r in db.execute(sql, (*params, limit, offset)).fetchall()]
        total = db.execute(
            # The contacts join mirrors the row query on purpose: _search_clause
            # references ct.name, so dropping it here would 500 the whole list.
            f"""SELECT count(*) AS n FROM calls c
                 LEFT JOIN call_intelligence ci ON ci.call_id = c.id
                 LEFT JOIN LATERAL (
                       SELECT * FROM followups f2
                        WHERE f2.call_id = c.id AND f2.deleted_at IS NULL
                        ORDER BY f2.id DESC LIMIT 1
                 ) f ON true
                 LEFT JOIN contacts ct ON ct.id = c.contact_id
                WHERE {' AND '.join(where)}""",
            tuple(params),
        ).fetchone()["n"]
    for r in rows:
        r["tags"] = _j(r.get("tags"))
    # The contact_id join only fires for calls that were already bound to a
    # contact when they happened; the phone book fills in the rest, so saving
    # a number today renames every past call from it too.
    _label_contacts(owner, rows)
    return {"items": rows, "total": total, "page": page, "limit": limit}


# ---------------------------------------------------------------------------
# Hybrid semantic search (Call History fuzzy filter)
# ---------------------------------------------------------------------------

def _identifier_arm(owner: str, term: str, limit: int,
                    agents: list[str] | None = None) -> list[int]:
    """Exact-ish lookup that the embedding arms structurally cannot do.

    call_embeddings holds transcript chunks — a call ID, a phone number or a
    contact name is metadata that never appears in that text, so a paste of any
    of them scored zero and the box looked broken. These hits are prepended
    ahead of the fused semantic results.
    """
    clause, params = _search_clause(term)
    agent_sql, agent_params = "", []
    if agents:
        agent_sql, agent_params = " AND c.agent_slug = ANY(?)", [list(agents)]
    with get_db() as db:
        rows = db.execute(
            f"""SELECT c.id FROM calls c
                  LEFT JOIN call_intelligence ci ON ci.call_id = c.id
                  LEFT JOIN contacts ct ON ct.id = c.contact_id
                 WHERE c.owner = ? AND {clause}{agent_sql}
                 ORDER BY c.started_at DESC NULLS LAST LIMIT ?""",
            (owner, *params, *agent_params, limit),
        ).fetchall()
    return [r["id"] for r in rows]


# Everything this platform calls an identifier carries DIGITS: a phone number,
# a call_key ("<digits>_<YYYYMMDD>_<HHMMSS>"), a conn_id (a UUID). That is the
# whole test, and it has to be, because the first cut — "no spaces and 8+
# characters" — matched "relocation", "complaints", "interested" and
# "possession", so every one-word English query silently SKIPPED the AI judge
# and fell back to the loose similarity matching this feature exists to
# replace. It also failed the opposite way: "+91 98765 43210" has spaces, so a
# pasted phone number was sent to the judge and spent 100 model calls
# confirming a primary key.
_PHONE_PUNCT = re.compile(r"^[0-9 +().\-]+$")
_IDLIKE_RE = re.compile(r"^(?=.*\d)[A-Za-z0-9_.\-]{8,}$")


def _looks_like_identifier(term: str) -> bool:
    """A pasted phone number / call key / conn id is an unambiguous lookup, not
    a description of a conversation. Those bypass the AI judge: there is no
    membership question to decide, and paying a model to re-confirm a primary
    key would be slow AND less correct than the index that found it."""
    t = (term or "").strip()
    if not t:
        return False
    # A phone number may be typed with spaces, +91 and dashes.
    if _PHONE_PUNCT.match(t) and sum(c.isdigit() for c in t) >= 6:
        return True
    # A call_key or conn_id: one token, and it contains a digit.
    return " " not in t and bool(_IDLIKE_RE.match(t))


@router.get("/search")
async def intel_search_calls(request: Request, q: str = "",
                             limit: int = Query(25, le=500),
                             agent: str = "",
                             verify: int = 1,
                             owner: str = Depends(current_owner)):
    """Natural-language call filter: retrieve, then DECIDE.

    Retrieval alone answers "which calls are about this?", which is not what a
    filter is asked. "whoever relocated to Vapi" and "whoever agreed to
    relocate to Delhi" embed almost identically, so the Delhi query used to
    return the Vapi calls — plausible rows instead of the correct empty answer.

    So the hybrid arms now generate CANDIDATES (up to FILTER_MAX_CANDIDATES,
    widened with the planner's paraphrases so a code-mixed transcript is not
    missed), and a small model decides membership per call against an explicit
    requirement checklist, defended by a verbatim quote that must actually
    occur in that call's transcript (shared/intelligence/verify.py).

    `verify=0` returns the raw retrieval ranking — kept for debugging and for
    the export path's "show me what was considered", never as a silent
    fallback: when the judge cannot run, the response says verified=false and
    the UI labels the rows unverified rather than passing them off as filtered.
    """
    import asyncio

    from shared.intelligence import config as intel_config, verify as intel_verify

    q = (q or "").strip()
    if not q:
        return {"items": [], "degraded": False, "query": q}

    # A constraint typed INSIDE the sentence ("...using agent shubhangi") is
    # lifted out and applied as a real filter; only the remaining phrase is
    # embedded. The dropdown wins when both name an agent — it is the explicit
    # control, and silently overriding it would make the UI lie.
    intent = await asyncio.to_thread(intel_search.parse_query_intent, owner, q)
    picked = _agent_list(agent)
    agents = picked or intent["agents"]
    sem_q = intent["query"] if not picked else q
    applied = {"agent": ", ".join(picked) or intent["agent_label"], "query": sem_q}

    identifier = _looks_like_identifier(sem_q)
    want_verify = (bool(verify) and not identifier
                   and intel_config.FILTER_VERIFY_ENABLED
                   and bool(intel_config.FILTER_LLM_API_KEY))

    client = None
    criterion: dict | None = None
    try:
        if want_verify:
            # One client for the planner and every judgement in this request.
            client = intel_verify._client()
            criterion = await intel_verify.plan_query(sem_q, client, owner)

        pool = intel_config.FILTER_MAX_CANDIDATES if want_verify else limit
        result = await intel_search.search_calls(
            owner, sem_q, limit=pool, agents=agents,
            probes=(criterion or {}).get("paraphrases"),
            keywords=(criterion or {}).get("keywords"))
        exact = await asyncio.to_thread(_identifier_arm, owner, sem_q, pool, agents)

        # Identifier hits first (a pasted call ID / phone / name is an
        # unambiguous intent), then semantic hits not already covered.
        seen = set(exact)
        ordered: list[tuple[int, dict | None]] = [(cid, None) for cid in exact]
        for res in result["results"]:
            if res["call_id"] in seen:
                continue
            seen.add(res["call_id"])
            ordered.append((res["call_id"], res))

        verdicts: dict[int, dict] = {}
        vinfo: dict[str, Any] = {}
        if want_verify and ordered:
            vinfo = await intel_verify.verify_calls(
                owner, sem_q, [cid for cid, _ in ordered],
                criterion=criterion, client=client)
            verdicts = vinfo.get("verdicts") or {}
    finally:
        if client is not None:
            await client.close()

    candidates = len(ordered)
    # No candidates at all is a checked, honest zero — not a broken verifier.
    # Reporting verified=False here made the UI warn "these are similarity
    # matches" about an empty list it had never claimed anything about.
    verified = (bool(vinfo.get("verified")) or not ordered) if want_verify else False
    if verified:
        # Judged membership REPLACES similarity ranking: the strongest evidence
        # first, retrieval order only breaking ties. A call that failed the
        # checklist is not a weaker match, it is not a match.
        rank = {cid: i for i, (cid, _) in enumerate(ordered)}
        ordered = [(cid, res) for cid, res in ordered
                   if verdicts.get(cid, {}).get("match")]
        ordered.sort(key=lambda cr: (-(verdicts[cr[0]].get("confidence") or 0.0),
                                     rank[cr[0]]))
    ordered = ordered[:limit]

    meta = {
        "degraded": result["degraded"], "query": q, "applied": applied,
        "verified": verified,
        "verification": {
            "enabled": want_verify,
            "model": intel_config.FILTER_LLM_MODEL if want_verify else None,
            "candidates": candidates,
            "examined": vinfo.get("examined", 0),
            "cached": vinfo.get("cached", 0),
            "matched": sum(1 for v in verdicts.values() if v.get("match")),
            "returned": len(ordered),
            # Retrieval filled its own candidate pool, so there may be further
            # matches it never offered the judge. "70 matched" reads as a
            # complete count and on 2026-08-29 it was not one: the true answer
            # to "people willing to relocate to vapi" was 98 and the arms
            # surfaced 103 candidates of which 69 matched. The pool is much
            # wider now, but a ceiling that can be hit silently is the bug —
            # say so when it is hit.
            "pool_full": candidates >= intel_config.FILTER_MAX_CANDIDATES,
            "truncated": bool(vinfo.get("truncated")),
            "identifier_lookup": identifier,
            "criterion": {
                "restated": (criterion or {}).get("restated"),
                "requirements": [r["text"] for r in (criterion or {}).get("requirements") or []],
                "excludes": (criterion or {}).get("excludes") or [],
                "planned": bool((criterion or {}).get("planned")),
            } if criterion else None,
        },
    }

    ids = [cid for cid, _ in ordered]
    if not ids:
        return {"items": [], **meta}

    def _hydrate():
        # Column list mirrors intel_calls (including the follow-up LATERAL):
        # search results render in the SAME frontend table, and the slimmer
        # list this used to select made every AI-filtered row show a blank
        # Opp score and Follow-up "no" — even for calls with one scheduled.
        with get_db() as db:
            rows = [dict(r) for r in db.execute(
                """SELECT c.id AS call_id, c.call_key, c.phone, c.direction,
                          c.agent_slug, c.started_at, c.duration_s, c.contact_id,
                          c.language, c.outcome,
                          ct.name AS contact_name,
                          ci.summary, ci.customer_intent, ci.sentiment,
                          ci.urgency, ci.lead_score, ci.opportunity_score,
                          ci.risk_score, ci.compliance_score, ci.confidence,
                          ci.low_confidence, ci.tags, ci.processed_at,
                          f.id AS followup_id, f.decision AS followup_decision,
                          f.status AS followup_status,
                          f.scheduled_at AS followup_at,
                          f.priority AS followup_priority
                     FROM calls c
                     LEFT JOIN call_intelligence ci ON ci.call_id = c.id
                     LEFT JOIN LATERAL (
                           SELECT * FROM followups f2
                            WHERE f2.call_id = c.id AND f2.deleted_at IS NULL
                            ORDER BY f2.id DESC LIMIT 1
                     ) f ON true
                     LEFT JOIN contacts ct ON ct.id = c.contact_id
                    WHERE c.owner = ? AND c.id = ANY(?)""",
                (owner, ids),
            ).fetchall()]
        return {r["call_id"]: r for r in rows}

    by_id = await asyncio.to_thread(_hydrate)
    items = []
    for cid, res in ordered:
        row = by_id.get(cid)
        if not row:
            continue
        row["tags"] = _j(row.get("tags"))
        row["snippet"] = (res["snippet"][:400] if res
                          else (row.get("summary") or "")[:400])
        row["score"] = round(res["rrf"], 5) if res else None
        row["match"] = "semantic" if res else "exact"
        v = verdicts.get(cid)
        if v:
            # The words that put this row in the result. Shown in the UI and
            # carried into the Excel export, so a filtered list can be audited
            # afterwards instead of being trusted.
            row["match"] = "verified"
            row["match_confidence"] = round(v.get("confidence") or 0.0, 3)
            row["match_evidence"] = v.get("evidence") or ""
            row["match_reason"] = v.get("reason") or ""
            if v.get("evidence"):
                row["snippet"] = v["evidence"][:400]
        items.append(row)
    _label_contacts(owner, items)
    return {"items": items, **meta}


# ---------------------------------------------------------------------------
# Conversation-intelligence workspace (single call)
# ---------------------------------------------------------------------------


@router.get("/calls/ids")
def intel_call_ids(owner: str = Depends(current_owner),
                   search: str = "", sentiment: str = "", decision: str = "",
                   agent: str = "",
                   days: Annotated[int, Query(ge=0)] = 0,
                   min_lead: Annotated[int, Query(ge=0, le=100)] = 0,
                   sort: str = "date", order: str = "desc"):
    """Every call id the current list filter matches — the whole set, not a page.

    The Intelligence list is server-paged at 50, and Export/Report can only
    send TICKED ids, so "select all" could never mean more than one screen:
    a 246-call filter exported as 50 unless the user paged through and ticked
    each page in turn. Returning ids only (not the ~30-column row the list
    renders) is what makes the whole set cheap enough to hand over at once.

    Declared BEFORE /calls/{call_key} — FastAPI matches in declaration order
    and the path-param route would otherwise swallow "ids" as a call key.

    Capped at _EXPORT_MAX_CALLS, the larger of the two consumer ceilings;
    `capped` tells the caller the selection is a floor, not the total, so the
    UI can say so rather than silently shortening a workbook.

    `days`/`min_lead` are Annotated rather than `= Query(0, ...)` so the
    default is a real int. The tests call endpoint functions directly (the
    current_owner dependency is FastAPI-resolved only over HTTP), and an
    unresolved `Query` object is TRUTHY — with the bare form the day-window
    predicate switched itself on and the object reached psycopg as a bind
    parameter. Same validation over HTTP, callable in a test.
    """
    where, params = _call_filters(
        owner, search=search, sentiment=sentiment, decision=decision,
        agent=agent, days=days, min_lead=min_lead)
    sort_col = _SORTABLE.get(sort, "c.started_at")
    direction = "ASC" if order == "asc" else "DESC"
    with get_db() as db:
        rows = db.execute(
            f"""SELECT c.id AS call_id
                  FROM calls c
                  LEFT JOIN call_intelligence ci ON ci.call_id = c.id
                  LEFT JOIN LATERAL (
                        SELECT * FROM followups f2
                         WHERE f2.call_id = c.id AND f2.deleted_at IS NULL
                         ORDER BY f2.id DESC LIMIT 1
                  ) f ON true
                  LEFT JOIN contacts ct ON ct.id = c.contact_id
                 WHERE {' AND '.join(where)}
                 ORDER BY {sort_col} {direction} NULLS LAST
                 LIMIT ?""",
            (*params, _EXPORT_MAX_CALLS + 1)).fetchall()
    ids = [int(r["call_id"]) for r in rows]
    capped = len(ids) > _EXPORT_MAX_CALLS
    return {"ok": True, "call_ids": ids[:_EXPORT_MAX_CALLS],
            "count": min(len(ids), _EXPORT_MAX_CALLS), "capped": capped,
            "max": _EXPORT_MAX_CALLS}


def _call_by_key(db, owner: str, call_key: str) -> dict[str, Any]:
    row = db.execute(
        "SELECT * FROM calls WHERE owner=? AND (call_key=? OR conn_id=?)",
        (owner, call_key, call_key),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="call not found")
    return dict(row)


@router.get("/calls/{call_key}")
def intel_call_detail(call_key: str, owner: str = Depends(current_owner)):
    with get_db() as db:
        call = _call_by_key(db, owner, call_key)
        ci = db.execute(
            "SELECT * FROM call_intelligence WHERE call_id=?", (call["id"],)
        ).fetchone()
        commitments = [dict(r) for r in db.execute(
            "SELECT * FROM commitments WHERE call_id=? ORDER BY id", (call["id"],)
        ).fetchall()]
        items = [dict(r) for r in db.execute(
            "SELECT * FROM action_items WHERE call_id=? ORDER BY id", (call["id"],)
        ).fetchall()]
        followup = db.execute(
            "SELECT * FROM followups WHERE call_id=? AND deleted_at IS NULL"
            " ORDER BY id DESC LIMIT 1", (call["id"],)
        ).fetchone()

    intelligence = None
    if ci:
        intelligence = dict(ci)
        for f in ("sentiment_journey", "topics", "pain_points", "objections",
                  "buying_signals", "competitor_mentions", "product_interest",
                  "budget_discussion", "timeline_discussion", "decision_makers",
                  "compliance_issues", "escalations", "risk_indicators", "tags",
                  "key_moments"):
            intelligence[f] = _j(intelligence.get(f))
        for f in ("agent_performance", "recommendations", "entities", "stages"):
            intelligence[f] = _j(intelligence.get(f), "{}")
        intelligence["stages"].pop("_follow_up_judgment", None)

    turns = [{"timestamp": ts, "speaker": sp, "text": tx}
             for ts, sp, tx in parse_turns(call.get("transcript") or "")]
    fu = dict(followup) if followup else None
    if fu:
        fu["notes"] = _j(fu.get("notes"))
        fu["audit"] = _j(fu.get("audit"))

    memory = None
    if call.get("contact_id"):
        with get_db() as db:
            mrow = db.execute(
                "SELECT memory, calls_count, last_call_at FROM customer_memory"
                " WHERE contact_id=?", (call["contact_id"],)
            ).fetchone()
        if mrow:
            memory = {"calls_count": mrow["calls_count"],
                      "last_call_at": mrow["last_call_at"],
                      **_j(mrow["memory"], "{}")}

    # Who this call was with, by name — the workspace headlines the person and
    # keeps the number underneath, same as every list that links here.
    call_out = {k: call.get(k) for k in
                ("id", "call_key", "conn_id", "phone", "direction", "agent_slug",
                 "language", "started_at", "ended_at", "duration_s", "outcome",
                 "outcome_kind", "contact_id", "recording_url")}
    call_out["contact_name"] = ""
    if call.get("contact_id"):
        with get_db() as db:
            crow = db.execute("SELECT name FROM contacts WHERE id=?",
                              (call["contact_id"],)).fetchone()
        call_out["contact_name"] = (crow["name"] if crow else "") or ""
    _label_contacts(owner, [call_out])

    return {
        "call": call_out,
        "audio_url": f"/api/calls/recordings/{call.get('call_key')}/audio"
                     if call.get("call_key") else None,
        "intelligence": intelligence,
        "transcript": turns,
        "commitments": commitments,
        "action_items": items,
        "followup": fu,
        "memory": memory,
    }


@router.post("/calls/{call_key}/reanalyze")
def intel_reanalyze(call_key: str, owner: str = Depends(current_owner)):
    with get_db() as db:
        call = _call_by_key(db, owner, call_key)
        db.execute("UPDATE call_intelligence SET stages='{}' WHERE call_id=?",
                   (call["id"],))
        job = db.execute(
            "SELECT id FROM intel_jobs WHERE dedupe_key IN (?, ?)",
            (f"call.process:{call.get('conn_id')}", f"call.process:{call.get('call_key')}"),
        ).fetchone()
    if job:
        intel_queue.requeue(job["id"])
    else:
        intel_queue.enqueue(intel_queue.KIND_CALL_PROCESS, owner=owner,
                            call_id=call["id"],
                            dedupe_key=f"call.process:{call.get('conn_id') or call['id']}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------

_FU_STATUSES = ("pending", "scheduled", "completed", "cancelled",
                "waiting_customer", "escalated", "missed", "archived")

# Bucket definitions are ORDERED and evaluated first-match-wins, exactly like a
# CASE. Both the list filter and the tab counts are derived from this one list
# so they cannot drift.
#
# They used to be written twice — an if/elif chain for the list and a separate
# CASE for the counts — and had silently diverged: the list required
# `scheduled_at > now()` while the CASE did not, so a pending follow-up with no
# scheduled date was counted under Upcoming but appeared in no bucket at all
# (9 counted vs 6 listed on prod). Undated follow-ups now live in `upcoming`,
# where they are actually reachable and can be given a date.
#
# The last entry is the catch-all (TRUE), reproducing the old CASE's ELSE: any
# status outside the ladder (waiting_customer, escalated) lands there instead of
# being counted-but-unlistable.
_FU_BUCKETS: list[tuple[str, str]] = [
    ("today", "f.status IN ('pending','scheduled') "
              "AND f.scheduled_at::date = now()::date"),
    ("overdue", "f.status = 'missed'"),
    ("review", "f.decision = 'unsure' AND f.status = 'pending'"),
    ("upcoming", "f.status IN ('pending','scheduled') "
                 "AND (f.scheduled_at IS NULL "
                 "     OR f.scheduled_at::date > now()::date)"),
    ("done", "f.status IN ('completed','cancelled')"),
    ("archived", "TRUE"),
]


def _bucket_filter(name: str) -> list[str]:
    """First-match-wins predicate for one bucket: its own condition AND the
    negation of every bucket ahead of it. This is what makes the listed rows
    identical to the counted rows."""
    clauses: list[str] = []
    for key, pred in _FU_BUCKETS:
        if key == name:
            clauses.append(f"COALESCE({pred}, FALSE)")
            return clauses
        # COALESCE is load-bearing: with scheduled_at NULL the predicate is NULL,
        # and bare `NOT NULL` is NULL — which a WHERE treats as false and drops
        # the row. A CASE skips a NULL WHEN and moves on, so without this the
        # counts and the list disagree again (the exact bug this list replaced).
        clauses.append(f"NOT COALESCE({pred}, FALSE)")
    return []  # unknown bucket -> no extra filtering


_FU_COUNT_CASE = "CASE\n" + "\n".join(
    f"  WHEN {pred} THEN '{key}'" for key, pred in _FU_BUCKETS) + "\nEND"


@router.get("/followups")
def followups_list(owner: str = Depends(current_owner), bucket: str = "",
                   status: str = "", decision: str = "",
                   page: int = Query(1, ge=1), limit: int = Query(_PAGE_LIMIT, le=200)):
    where = ["f.owner = ?", "f.deleted_at IS NULL"]
    params: list[Any] = [owner]
    where += _bucket_filter(bucket)
    if status:
        where.append("f.status = ?")
        params.append(status)
    if decision:
        where.append("f.decision = ?")
        params.append(decision)

    offset = (page - 1) * limit
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            # contact_handle is the string id the outbound dialler keys on
            # (legacy_id, else "db<pk>" — see _contact_to_dict in server.py).
            # Without it a follow-up row cannot be turned into an actual call:
            # followups.contact_id is the integer PK, and POST
            # /api/outbound/dial/{contact_id} takes the string handle.
            f"""SELECT f.*, c.call_key, c.phone, ct.name AS contact_name,
                       COALESCE(ct.legacy_id, 'db' || ct.id::text) AS contact_handle,
                       COALESCE(ct.phone, c.phone) AS dial_phone
                  FROM followups f
                  LEFT JOIN calls c ON c.id = f.call_id
                  LEFT JOIN contacts ct ON ct.id = f.contact_id
                 WHERE {' AND '.join(where)}
                 ORDER BY COALESCE(f.scheduled_at, f.created_at) ASC
                 LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()]
        total = db.execute(
            f"SELECT count(*) AS n FROM followups f WHERE {' AND '.join(where)}",
            tuple(params),
        ).fetchone()["n"]
        counts = {r["k"]: r["n"] for r in db.execute(
            f"""SELECT {_FU_COUNT_CASE} AS k, count(*) AS n
                  FROM followups f
                 WHERE f.owner=? AND f.deleted_at IS NULL GROUP BY 1""",
            (owner,),
        ).fetchall()}
    for r in rows:
        r["notes"] = _j(r.get("notes"))
        r["audit"] = _j(r.get("audit"))
    _label_contacts(owner, rows, phone_key="dial_phone")
    return {"items": rows, "total": total, "counts": counts,
            "page": page, "limit": limit}


@router.post("/followups")
def followup_create(body: dict, owner: str = Depends(current_owner)):
    if not body.get("reason") and not body.get("summary"):
        raise HTTPException(status_code=422, detail="reason or summary required")
    mode = body.get("mode") or "manual"
    if mode not in ("manual", "auto"):
        raise HTTPException(status_code=422, detail="bad mode")
    with get_db() as db:
        row = db.execute(
            """INSERT INTO followups
                   (owner, call_id, contact_id, decision, confidence, explanation,
                    reason, summary, followup_type, promised_by, scheduled_at,
                    priority, status, assigned_to, source, mode, audit)
               VALUES (?, ?, ?, 'yes', 1.0, 'Created manually', ?, ?, ?, NULL,
                       ?, ?, 'scheduled', ?, 'manual', ?, ?)
               RETURNING id""",
            (owner, body.get("call_id"), body.get("contact_id"),
             body.get("reason"), body.get("summary"),
             body.get("followup_type") or "callback",
             body.get("scheduled_at"), body.get("priority") or "medium",
             body.get("assigned_to"), mode,
             json.dumps([_now_audit(owner, "created", "manual")])),
        ).fetchone()
    if body.get("contact_id"):
        _timeline_emit(int(body["contact_id"]), owner, "followup.created",
                       ref_table="followups", ref_id=row["id"],
                       payload={"followup_type": body.get("followup_type") or "callback",
                                "scheduled_at": body.get("scheduled_at"),
                                "priority": body.get("priority") or "medium",
                                "source": "manual"})
    return {"ok": True, "id": row["id"]}


_FU_PATCHABLE = ("status", "scheduled_at", "priority", "assigned_to",
                 "followup_type", "reason", "summary", "mode", "max_attempts")


@router.patch("/followups/{fid}")
def followup_patch(fid: int, body: dict, owner: str = Depends(current_owner)):
    sets, params = [], []
    for field in _FU_PATCHABLE:
        if field in body:
            if field == "status" and body["status"] not in _FU_STATUSES:
                raise HTTPException(status_code=422, detail="bad status")
            # mode flips are the human override on auto-dialling; validated
            # here so a typo can never mean "the platform calls people".
            if field == "mode" and body["mode"] not in ("manual", "auto"):
                raise HTTPException(status_code=422, detail="bad mode")
            if field == "max_attempts":
                try:
                    body["max_attempts"] = (
                        None if body["max_attempts"] in (None, "")
                        else max(0, min(10, int(body["max_attempts"]))))
                except (TypeError, ValueError):
                    raise HTTPException(status_code=422,
                                        detail="max_attempts must be 0-10")
            sets.append(f"{field}=?")
            params.append(body[field])
    # Re-arming a follow-up for auto-dialling clears a previous permanent
    # block — otherwise the toggle is a no-op the user can't see past.
    if body.get("mode") == "auto":
        sets.append("autodial_blocked_at=NULL")
        sets.append("last_attempt_error=NULL")
    note = (body.get("note") or "").strip()
    if not sets and not note:
        raise HTTPException(status_code=422, detail="nothing to update")

    with get_db() as db:
        row = db.execute(
            "SELECT notes, audit, status, contact_id, followup_type FROM followups"
            " WHERE id=? AND owner=? AND deleted_at IS NULL", (fid, owner),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="follow-up not found")
        audit = _j(row["audit"])
        audit.append(_now_audit(owner, "update",
                                ", ".join(f"{k}={body[k]}" for k in body
                                          if k in _FU_PATCHABLE)))
        sets.append("audit=?")
        params.append(json.dumps(audit[-50:]))
        if note:
            notes = _j(row["notes"])
            notes.append({**_now_audit(owner, "note"), "text": note[:1000]})
            sets.append("notes=?")
            params.append(json.dumps(notes[-100:]))
        if body.get("status") == "completed":
            sets.append("completed_at=now()")
        sets.append("updated_at=now()")
        db.execute(
            f"UPDATE followups SET {', '.join(sets)} WHERE id=? AND owner=?",
            (*params, fid, owner),
        )
    if (body.get("status") == "completed" and row["status"] != "completed"
            and row["contact_id"]):
        _timeline_emit(row["contact_id"], owner, "followup.completed",
                       ref_table="followups", ref_id=fid,
                       payload={"followup_type": row["followup_type"]})
    return {"ok": True}


@router.delete("/followups/{fid}")
def followup_delete(fid: int, owner: str = Depends(current_owner)):
    with get_db() as db:
        cur = db.execute(
            "UPDATE followups SET deleted_at=now() WHERE id=? AND owner=?",
            (fid, owner),
        )
    if not getattr(cur, "rowcount", 0):
        raise HTTPException(status_code=404, detail="follow-up not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Follow-up call approval — the human-in-the-loop gate
#
# The auto-dialer may only ring a number a named person signed off on. See
# migration 0011 and shared/intelligence/autodial.py for the incident this
# came from; the short version is that the AI's own "yes, follow up" verdict
# turned out to be worth very little. Of the 20 rows armed for auto-dialling
# on 2026-07-29, fifteen had no commitment on the source call at all, and nine
# of those were nevertheless labelled promised_by='agent_promised' — including
# calls that failed on OUR side ("call failed due to technical audio issues",
# confidence 1.0). So the review UI must not merely restate the AI's reason:
# it has to show the human what was actually SAID, and admit when nothing was.
# ---------------------------------------------------------------------------

# How long an approval stays good. Matches AUTODIAL_MAX_AGE_H so an approval
# never outlives the row's own staleness guard: consent to call someone today
# is not consent to call them next week.
_APPROVAL_TTL_H = 72

# Rows a human can be asked to authorise. 'no' is excluded — the AI saw no
# reason to call, and surfacing those invites rubber-stamping.
_APPROVABLE_DECISIONS = ("yes", "unsure")


def _autodial_max_attempts() -> int:
    """How many times one approval may ring the same person before the row
    needs a human again. Surfaced to the UI so the approve dialog can state it
    rather than let the operator assume "approve" means exactly one call."""
    try:
        from shared.intelligence import config as intel_config

        return int(intel_config.AUTODIAL_MAX_ATTEMPTS)
    except Exception:
        return 3


def _evidence_for(db, rows: list[dict]) -> dict[int, dict]:
    """Per-follow-up proof of why a call is being proposed.

    Two tiers, and the difference between them is the entire point:

      quoted    a commitment was extracted from the call with the verbatim
                words that created the obligation, and who said them.
      inferred  no commitment exists. The follow-up is the model's own idea.

    Deliberately joined on call_id, NOT on followups.commitment_id: that FK is
    NULL on every row in production (all 89 commitments carry a quote, yet not
    one follow-up points at one), so trusting it would have shown "no evidence"
    for calls that genuinely do have a recorded promise.
    """
    call_ids = sorted({int(r["call_id"]) for r in rows if r.get("call_id")})
    if not call_ids:
        return {}
    marks = ",".join("?" for _ in call_ids)
    commits: dict[int, list[dict]] = {}
    for c in db.execute(
        f"""SELECT call_id, made_by, kind, description, quote, deadline_text,
                   confidence
              FROM commitments
             WHERE call_id IN ({marks})
             ORDER BY made_by, id""",
        tuple(call_ids),
    ).fetchall():
        commits.setdefault(int(c["call_id"]), []).append({
            "made_by": c["made_by"], "kind": c["kind"],
            "description": c["description"], "quote": c["quote"],
            "deadline_text": c["deadline_text"],
            "confidence": c["confidence"],
        })

    # Tail of the transcript as context. A promise, if there is one, is made
    # near the end; `right(...)` keeps a long call from bloating the response.
    tails: dict[int, list[dict]] = {}
    for t in db.execute(
        f"""SELECT id, right(transcript, 4000) AS tail FROM calls
             WHERE id IN ({marks})""",
        tuple(call_ids),
    ).fetchall():
        turns = parse_turns(t["tail"] or "")[-8:]
        tails[int(t["id"])] = [
            {"at": at, "speaker": who, "text": text[:400]}
            for at, who, text in turns
        ]

    out: dict[int, dict] = {}
    for r in rows:
        cid = int(r["call_id"]) if r.get("call_id") else 0
        found = commits.get(cid, [])
        out[int(r["id"])] = {
            "strength": "quoted" if found else "inferred",
            "commitments": found,
            "transcript_tail": tails.get(cid, []),
        }
    return out


@router.get("/followups/review-queue")
def followup_review_queue(owner: str = Depends(current_owner),
                          limit: int = Query(100, le=200),
                          include_declined: int = 0):
    """Everyone with a follow-up call awaiting a human decision, with proof.

    This is what backs the "Review & approve calls" dialog. Nothing here has
    been called and nothing here can be called until it comes back through
    /approve.

    include_declined=1 also returns rows someone already refused. Without it a
    decline is a one-way door: the row leaves this queue, and the queue is the
    only screen that can approve. Changing your mind has to be possible.
    """
    states = ("pending", "declined") if include_declined else ("pending",)
    state_marks = ",".join("?" for _ in states)
    marks = ",".join("?" for _ in _APPROVABLE_DECISIONS)
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            f"""SELECT f.id, f.call_id, f.contact_id, f.decision, f.confidence,
                       f.reason, f.explanation, f.summary, f.followup_type,
                       f.promised_by, f.scheduled_at, f.auto_scheduled,
                       f.priority, f.status, f.mode, f.attempts,
                       f.approval_state, f.created_at, f.declined_at, f.declined_by,
                       c.call_key, c.started_at AS source_call_at,
                       ct.name AS contact_name,
                       COALESCE(ct.legacy_id, 'db' || ct.id::text) AS contact_handle,
                       COALESCE(ct.phone, c.phone) AS dial_phone,
                       (ct.disposition = 'rejected') AS dnd,
                       ci.customer_intent
                  FROM followups f
                  LEFT JOIN calls c ON c.id = f.call_id
                  LEFT JOIN contacts ct ON ct.id = f.contact_id
                  LEFT JOIN call_intelligence ci ON ci.call_id = f.call_id
                 WHERE f.owner = ?
                   AND f.deleted_at IS NULL
                   AND f.approval_state IN ({state_marks})
                   AND f.decision IN ({marks})
                   AND f.status IN ('pending', 'scheduled', 'missed')
                 ORDER BY f.approval_state = 'pending' DESC,
                          f.priority = 'urgent' DESC,
                          COALESCE(f.scheduled_at, f.created_at) ASC
                 LIMIT ?""",
            (owner, *states, *_APPROVABLE_DECISIONS, limit),
        ).fetchall()]
        evidence = _evidence_for(db, rows)
    for r in rows:
        r["evidence"] = evidence.get(int(r["id"]),
                                     {"strength": "inferred", "commitments": [],
                                      "transcript_tail": []})
    _label_contacts(owner, rows, phone_key="dial_phone")
    # `total` counts only what still needs a decision, so the header badge
    # means the same thing whether or not declined rows are being shown.
    return {"items": rows,
            "total": sum(1 for r in rows if r["approval_state"] == "pending"),
            "shown": len(rows),
            "max_attempts_per_approval": _autodial_max_attempts(),
            "approval_ttl_hours": _APPROVAL_TTL_H}


def _approve_rows(db, owner: str, ids: list[int], *, mode: str,
                  ttl_h: int) -> list[int]:
    """Stamp approval on rows this owner actually owns. Returns the ids hit.

    A DND contact is never approvable, whatever the UI sent — the customer's
    own "stop calling me" outranks an operator's click.
    """
    done: list[int] = []
    for fid in ids:
        row = db.execute(
            """SELECT f.id, f.audit, (ct.disposition = 'rejected') AS dnd
                 FROM followups f
                 LEFT JOIN contacts ct ON ct.id = f.contact_id
                WHERE f.id=? AND f.owner=? AND f.deleted_at IS NULL""",
            (fid, owner),
        ).fetchone()
        if not row or row["dnd"]:
            continue
        audit = _j(row["audit"])
        audit.append(_now_audit(
            owner, "approved",
            f"approved for {'automatic' if mode == 'auto' else 'manual'} "
            f"calling, valid {ttl_h}h"))
        # autodial_blocked_at is deliberately NOT cleared here. If the dialer
        # permanently stopped this row (retry cap, no number), approving the
        # CONTENT of the call should not silently restart the attempts; that
        # stays an explicit re-arm through PATCH mode=auto.
        db.execute(
            """UPDATE followups
                  SET approval_state='approved',
                      approved_at=now(),
                      approved_by=?,
                      approval_expires_at=now() + (? || ' hours')::interval,
                      declined_at=NULL, declined_by=NULL,
                      mode=?,
                      audit=?,
                      updated_at=now()
                WHERE id=? AND owner=?""",
            (owner, str(int(ttl_h)), mode, json.dumps(audit[-50:]), fid, owner),
        )
        done.append(int(fid))
    return done


@router.post("/followups/{fid}/approve")
def followup_approve(fid: int, body: dict | None = None,
                     owner: str = Depends(current_owner)):
    """Authorise this one call. mode='auto' lets the dialer place it."""
    body = body or {}
    mode = body.get("mode") or "auto"
    if mode not in ("manual", "auto"):
        raise HTTPException(status_code=422, detail="bad mode")
    try:
        ttl_h = max(1, min(720, int(body.get("expires_in_h") or _APPROVAL_TTL_H)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="bad expires_in_h")
    with get_db() as db:
        done = _approve_rows(db, owner, [fid], mode=mode, ttl_h=ttl_h)
    if not done:
        raise HTTPException(
            status_code=404,
            detail="follow-up not found, or the contact asked not to be called")
    return {"ok": True, "approved": done, "mode": mode, "expires_in_h": ttl_h}


@router.post("/followups/approve")
def followups_approve_bulk(body: dict, owner: str = Depends(current_owner)):
    """Approve a reviewed batch. The UI sends only ids the human ticked."""
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=422, detail="ids required")
    try:
        ids = [int(i) for i in ids][:200]
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="ids must be integers")
    mode = body.get("mode") or "auto"
    if mode not in ("manual", "auto"):
        raise HTTPException(status_code=422, detail="bad mode")
    try:
        ttl_h = max(1, min(720, int(body.get("expires_in_h") or _APPROVAL_TTL_H)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="bad expires_in_h")
    with get_db() as db:
        done = _approve_rows(db, owner, ids, mode=mode, ttl_h=ttl_h)
    # skipped = not owned, deleted, or DND. Reported so a bulk approve that
    # quietly dropped rows doesn't read as a full success.
    return {"ok": True, "approved": done, "count": len(done),
            "skipped": len(ids) - len(done), "mode": mode}


@router.post("/followups/{fid}/decline")
def followup_decline(fid: int, body: dict | None = None,
                     owner: str = Depends(current_owner)):
    """Refuse this call. The row stays on the list and stays callable by hand;
    it simply loses the right to dial itself."""
    body = body or {}
    note = (body.get("note") or "").strip()
    with get_db() as db:
        row = db.execute(
            "SELECT audit, notes FROM followups"
            " WHERE id=? AND owner=? AND deleted_at IS NULL", (fid, owner),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="follow-up not found")
        audit = _j(row["audit"])
        audit.append(_now_audit(owner, "declined", note[:200] or "not approved"))
        sets = ["approval_state='declined'", "declined_at=now()",
                "declined_by=?", "approved_at=NULL", "approved_by=NULL",
                "approval_expires_at=NULL", "mode='manual'", "audit=?",
                "updated_at=now()"]
        params: list[Any] = [owner, json.dumps(audit[-50:])]
        if note:
            notes = _j(row["notes"])
            notes.append({**_now_audit(owner, "note"), "text": note[:1000]})
            sets.append("notes=?")
            params.append(json.dumps(notes[-100:]))
        db.execute(
            f"UPDATE followups SET {', '.join(sets)} WHERE id=? AND owner=?",
            (*params, fid, owner),
        )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Action items
# ---------------------------------------------------------------------------

@router.patch("/action-items/{aid}")
def action_item_patch(aid: int, body: dict, owner: str = Depends(current_owner)):
    sets, params = [], []
    for field in ("status", "priority", "assigned_to", "due_at", "description"):
        if field in body:
            sets.append(f"{field}=?")
            params.append(body[field])
    if not sets:
        raise HTTPException(status_code=422, detail="nothing to update")
    sets.append("updated_at=now()")
    with get_db() as db:
        cur = db.execute(
            f"UPDATE action_items SET {', '.join(sets)} WHERE id=? AND owner=?",
            (*params, aid, owner),
        )
    if not getattr(cur, "rowcount", 0):
        raise HTTPException(status_code=404, detail="action item not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@router.get("/reports")
def reports_list(owner: str = Depends(current_owner),
                 page: int = Query(1, ge=1), limit: int = Query(_PAGE_LIMIT, le=200)):
    offset = (page - 1) * limit
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            """SELECT id, title, query_prompt, call_count, status, model, error,
                      created_at, completed_at
                 FROM reports WHERE owner=? AND deleted_at IS NULL
                ORDER BY id DESC LIMIT ? OFFSET ?""",
            (owner, limit, offset),
        ).fetchall()]
        total = db.execute(
            "SELECT count(*) AS n FROM reports WHERE owner=? AND deleted_at IS NULL",
            (owner,),
        ).fetchone()["n"]
    return {"items": rows, "total": total, "page": page, "limit": limit}


_REPORT_MAX_CALLS = 1000


def _calls_matching_filters(owner: str, filters: dict,
                            analyzed_only: bool = True) -> list[int]:
    """Call ids for a filter-built report, newest first.

    Reports could only ever be built by hand-ticking rows on the Intelligence
    page, which does not scale past one screen and makes the obvious ask —
    "report on last month's outbound calls" — a manual selection exercise. The
    filter vocabulary here is deliberately the SAME one the Intelligence list
    uses, via _call_filters, so "what the filter showed" and "what the report
    covers" cannot drift apart.
    """
    try:
        days = max(0, min(int(filters.get("days") or 0), 3650))
        min_lead = max(0, min(int(filters.get("min_lead") or 0), 100))
        cap = int(filters.get("limit") or 200)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="filters must be numeric where numeric")
    cap = max(1, min(cap, _REPORT_MAX_CALLS))
    where, params = _call_filters(
        owner,
        search=str(filters.get("search") or ""),
        sentiment=str(filters.get("sentiment") or ""),
        decision=str(filters.get("decision") or ""),
        agent=str(filters.get("agent") or ""),
        call_direction=str(filters.get("direction") or ""),
        days=days, min_lead=min_lead, analyzed_only=analyzed_only)
    with get_db() as db:
        rows = db.execute(
            f"""SELECT c.id FROM calls c
                  LEFT JOIN call_intelligence ci ON ci.call_id = c.id
                  LEFT JOIN LATERAL (
                        SELECT * FROM followups f2
                         WHERE f2.call_id = c.id AND f2.deleted_at IS NULL
                         ORDER BY f2.id DESC LIMIT 1
                  ) f ON true
                  LEFT JOIN contacts ct ON ct.id = c.contact_id
                 WHERE {' AND '.join(where)}
                 ORDER BY c.started_at DESC NULLS LAST
                 LIMIT ?""",
            (*params, cap)).fetchall()
    return [int(r["id"]) for r in rows]


@router.post("/reports/preview")
def report_preview(body: dict, owner: str = Depends(current_owner)):
    """How many analyzed calls a filter would put in a report.

    The builder shows this before you commit: generating a report is a
    background LLM job, so "0 calls match" has to be visible at the point of
    choosing, not discovered as a failed report ten minutes later.
    """
    ids = _calls_matching_filters(owner, body.get("filters") or {})
    return {"ok": True, "call_count": len(ids),
            "capped": len(ids) >= max(1, min(
                int((body.get("filters") or {}).get("limit") or 200),
                _REPORT_MAX_CALLS))}


@router.post("/reports")
def report_create(body: dict, owner: str = Depends(current_owner)):
    raw = body.get("call_ids") or []
    # No explicit selection → build the set from the filter instead. Both
    # paths converge on the same ownership check below.
    if not raw and isinstance(body.get("filters"), dict) and body["filters"]:
        raw = _calls_matching_filters(owner, body["filters"])
        if not raw:
            raise HTTPException(
                status_code=422,
                detail="No analyzed calls match that filter — widen the date "
                       "range or clear a filter.")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=422, detail="call_ids required")
    try:
        # De-duplicate before the ownership check. count(*) over `id = ANY(...)`
        # counts matching ROWS, so a selection containing the same call twice
        # made owned < len(call_ids) and the request 403'd with "calls you do
        # not own" — about the caller's own calls. Non-numeric input used to
        # reach int() unguarded and 500; it is a 422 now.
        call_ids = sorted({int(c) for c in raw})
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="call_ids must be integers")
    if len(call_ids) > 1000:
        raise HTTPException(status_code=422, detail="max 1000 calls per report")
    with get_db() as db:
        owned = db.execute(
            "SELECT count(*) AS n FROM calls WHERE owner=? AND id = ANY(?)",
            (owner, call_ids),
        ).fetchone()["n"]
        if owned != len(call_ids):
            raise HTTPException(status_code=403, detail="selection contains calls you do not own")
        row = db.execute(
            """INSERT INTO reports (owner, title, query_prompt, filters, call_ids,
                                    call_count, status, created_by)
               VALUES (?, ?, ?, ?, ?, ?, 'queued', ?) RETURNING id""",
            (owner, (body.get("title") or "Call intelligence report")[:200],
             (body.get("query_prompt") or "")[:1000],
             json.dumps(body.get("filters") or {}),
             json.dumps(call_ids), len(call_ids), owner),
        ).fetchone()
    intel_queue.enqueue(intel_queue.KIND_REPORT_GENERATE, owner=owner,
                        payload={"report_id": row["id"]},
                        dedupe_key=f"report:{row['id']}")
    return {"ok": True, "id": row["id"]}


@router.get("/reports/{rid}")
def report_get(rid: int, owner: str = Depends(current_owner)):
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM reports WHERE id=? AND owner=? AND deleted_at IS NULL",
            (rid, owner),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="report not found")
    rep = dict(row)
    rep["filters"] = _j(rep.get("filters"), "{}")
    rep["call_ids"] = _j(rep.get("call_ids"))
    return rep


def _report_export_payload(rid: int, owner: str):
    """Shared loader for both export formats: the report row plus every call it
    covers, already joined to the intelligence layer and to its commitments."""
    from shared.intelligence import report_export as rx

    with get_db() as db:
        row = db.execute(
            "SELECT * FROM reports WHERE id=? AND owner=? AND deleted_at IS NULL",
            (rid, owner)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="report not found")
        rep = dict(row)
        rows = rx.load_report_rows(db, rep, owner)
        commitments = rx.load_commitments(
            db, owner, [r["id"] for r in rows if r.get("id") is not None])
    rows = rx._prepare(rows, commitments)
    return rep, rows


@router.get("/reports/{rid}/export.docx")
def report_export_docx(rid: int, owner: str = Depends(current_owner)):
    """The report as a branded Word document (LesCall watermark on every page)."""
    from fastapi.responses import Response
    from shared.intelligence import report_export as rx

    rep, rows = _report_export_payload(rid, owner)
    try:
        blob = rx.build_docx(rep, rows, owner)
    except Exception as exc:
        logger.exception("docx export failed for report %s", rid)
        raise HTTPException(status_code=500, detail=f"docx export failed: {exc}")
    name = rx.safe_filename(rep.get("title"), "docx")
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/reports/{rid}/export.xlsx")
def report_export_xlsx(rid: int, owner: str = Depends(current_owner)):
    """Per-call analysis workbook: every call, hot leads, the ones needing a
    human, and every outstanding promise — typed and filterable."""
    from fastapi.responses import Response
    from shared.intelligence import report_export as rx

    rep, rows = _report_export_payload(rid, owner)
    try:
        blob = rx.build_xlsx(rep, rows, owner)
    except Exception as exc:
        logger.exception("xlsx export failed for report %s", rid)
        raise HTTPException(status_code=500, detail=f"xlsx export failed: {exc}")
    name = rx.safe_filename(rep.get("title"), "xlsx")
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/reports/{rid}/export.{fmt}", include_in_schema=False)
def report_export_text(rid: int, fmt: str, owner: str = Depends(current_owner)):
    """The report as markdown or plain text.

    The synthesized document IS markdown, so .md is the source with a
    provenance header — nothing is re-rendered and nothing can drift from what
    the reader saw on screen. .txt is the same content with the markup taken
    off, for mail clients and CRM note fields.
    """
    from fastapi.responses import Response
    from shared.intelligence import report_export as rx

    builders = {"md": (rx.build_md, "text/markdown"),
                "txt": (rx.build_txt, "text/plain")}
    if fmt not in builders:
        raise HTTPException(status_code=404, detail=f"unknown export format {fmt!r}")
    build, media = builders[fmt]
    rep, rows = _report_export_payload(rid, owner)
    if not (rep.get("document") or "").strip():
        raise HTTPException(
            status_code=409,
            detail="This report has no document yet — it is still generating.")
    blob = build(rep, rows, owner)
    name = rx.safe_filename(rep.get("title"), fmt)
    return Response(content=blob, media_type=f"{media}; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.delete("/reports/{rid}")
def report_delete(rid: int, owner: str = Depends(current_owner)):
    with get_db() as db:
        cur = db.execute(
            "UPDATE reports SET deleted_at=now() WHERE id=? AND owner=?",
            (rid, owner),
        )
    if not getattr(cur, "rowcount", 0):
        raise HTTPException(status_code=404, detail="report not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Excel exports of a call selection → Google Cloud Storage
#
# "Generate report" synthesises prose. This is the other half of what a
# selection is for: the rows themselves, as a workbook someone can pivot,
# hand to a dialling team, or load into a CRM — and living in the customer's
# cloud bucket rather than a browser download folder, so it survives the
# laptop it was made on and can be shared by link.
# ---------------------------------------------------------------------------

# Ceiling on one workbook. At the live corpus average (1.7 kB of transcript per
# call) 2000 calls is a ~10 MB file that Excel still opens comfortably; past
# that the Transcripts sheet is what breaks first.
_EXPORT_MAX_CALLS = 2000


def _utcnow():
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc)


def _safe_criterion(raw: Any) -> dict:
    """The filter criterion is echoed onto the workbook's Summary sheet, and it
    arrives from the browser — so it is bounded here rather than trusted. The
    client sends it (instead of the server re-deriving it) so the sheet records
    the checklist the user actually SAW, not one a re-plan might reword."""
    if not isinstance(raw, dict):
        return {}
    reqs = [str(r)[:300] for r in (raw.get("requirements") or [])[:8]
            if isinstance(r, (str, int, float))]
    return {"restated": str(raw.get("restated") or "")[:400],
            "requirements": reqs}


def _revoked_token(eid: int) -> str:
    """Deleting an export must invalidate its link, but download_token is
    UNIQUE — so the row keeps a value that cannot collide and cannot be
    guessed back into a working link."""
    import secrets as _s

    return f"revoked-{eid}-{_s.token_urlsafe(8)}"


def _export_row(row: dict) -> dict:
    """One export as the UI sees it. `url` is the tokenized link (the bucket is
    private; the token is the credential — see shared/export_store.py)."""
    from shared import export_store

    obj = row.get("gcs_object")
    return {
        "id": row["id"], "title": row.get("title") or "",
        "query": row.get("query") or "",
        "call_count": row.get("call_count") or 0,
        "contact_count": row.get("contact_count") or 0,
        "filename": row.get("filename"),
        "size_bytes": row.get("size_bytes"),
        "storage": row.get("storage"),
        "gcs_uri": (f"gs://{row['gcs_bucket']}/{obj}"
                    if obj and row.get("gcs_bucket") else None),
        "gcs_object": obj,
        "gcs_console_url": export_store.console_url(obj) if obj else None,
        "uploaded_at": row.get("gcs_uploaded_at"),
        "upload_error": row.get("upload_error"),
        "url": export_store.public_download_url(row["download_token"]),
        "created_at": row.get("created_at"),
        "created_by": row.get("created_by"),
    }


@router.post("/exports")
async def export_create(body: dict, owner: str = Depends(current_owner)):
    """Build the workbook for a selection and put it in the bucket.

    Selection is either `call_ids` (the ticked rows) or `filters` (the same
    vocabulary the Intelligence list uses). `query` is the filter phrase that
    produced the selection: it is stored for provenance AND used to pull the
    AI filter's recorded evidence, so the workbook can show the line from each
    call that made it qualify.

    Synchronous on purpose — the workbook is built from rows already in the
    database, so it is ready in about a second and a background job would only
    add a place for it to get lost.
    """
    import asyncio

    from shared import export_store
    from shared.db.database import get_db as _get_db
    from shared.intelligence import config as intel_config
    from shared.intelligence import selection_export, verify as intel_verify

    raw = body.get("call_ids") or []
    if not raw and isinstance(body.get("filters"), dict) and body["filters"]:
        # An export is a data dump, not a synthesis: a call with no analysis
        # still has a number, a duration and a transcript worth exporting, so
        # this path does NOT restrict to analysed calls the way reports do.
        raw = _calls_matching_filters(owner, {**body["filters"],
                                             "limit": _EXPORT_MAX_CALLS},
                                      analyzed_only=False)
    try:
        ids = [int(x) for x in raw][:_EXPORT_MAX_CALLS]
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="call_ids must be integers")
    if not ids:
        raise HTTPException(status_code=422,
                            detail="select at least one call to export")

    title = (str(body.get("title") or "").strip()
             or f"Calls — {(str(body.get('query') or '').strip() or 'selection')}")[:200]
    query = str(body.get("query") or "").strip()[:2000]
    token = export_store.new_token()

    with get_db() as db:
        row = db.execute(
            """INSERT INTO call_exports
                   (owner, title, query, call_ids, filename, download_token,
                    created_by, storage)
               VALUES (?,?,?,?,?,?,?,'pending') RETURNING id""",
            (owner, title, query, json.dumps(ids), "pending.xlsx", token, owner),
        ).fetchone()
    export_id = row["id"]
    filename = export_store.filename_for(title, export_id)

    def _build():
        with _get_db() as db2:
            # Verdicts are stored under the phrase the JUDGE was given, which is
            # the query with any "...using agent shubhangi" constraint lifted
            # out (see intel_search_calls). The client sends the phrase the user
            # TYPED, so whenever an agent was named inside the sentence — a
            # documented way to use this box — the lookup missed every row and
            # the workbook shipped with an empty Evidence sheet and
            # `verified: false`, silently downgrading a filtered export to an
            # unverified one. Fixed here rather than in the caller so it holds
            # for every client, not just the dashboard.
            evidence = {}
            if query:
                evidence = intel_verify.load_evidence(owner, query, ids)
                if not evidence:
                    lifted = intel_search.parse_query_intent(owner, query)["query"]
                    if lifted and lifted != query:
                        evidence = intel_verify.load_evidence(owner, lifted, ids)
            return selection_export.build_for_selection(
                db2, owner, ids,
                {"title": title, "query": query, "created_by": owner,
                 "verified": bool(evidence),
                 "verify_model": intel_config.FILTER_LLM_MODEL,
                 "criterion": _safe_criterion(body.get("criterion"))},
                evidence=evidence)

    try:
        blob, stats = await asyncio.to_thread(_build)
    except Exception as exc:
        logger.exception("export %s failed to build", export_id)
        with get_db() as db:
            db.execute("UPDATE call_exports SET deleted_at=now(), "
                       "upload_error=? WHERE id=? AND owner=?",
                       (f"build failed: {exc}"[:500], export_id, owner))
        raise HTTPException(status_code=500, detail=f"export failed: {exc}")

    if not stats["call_count"]:
        with get_db() as db:
            db.execute("UPDATE call_exports SET deleted_at=now() WHERE id=? AND owner=?",
                       (export_id, owner))
        raise HTTPException(status_code=404,
                            detail="none of those calls belong to this account")

    # Local copy first: it is the serving fallback while GCS is unreachable,
    # and the retry source afterwards. Only then the upload.
    local = await asyncio.to_thread(export_store.write_local, filename, blob)
    storage, obj, bucket, err = "local", None, None, None
    if export_store.gcs_enabled():
        obj = export_store.object_name(owner, export_id, filename)
        try:
            await asyncio.to_thread(export_store.upload_bytes, obj, blob)
            storage, bucket = "gcs", export_store.bucket_name()
        except Exception as exc:
            logger.error("export %s upload to GCS failed: %r", export_id, exc)
            obj, err = None, f"{type(exc).__name__}: {exc}"[:500]
    else:
        err = "GCS_EXPORTS_BUCKET/GCS_RECORDINGS_BUCKET is not configured"

    with get_db() as db:
        db.execute(
            """UPDATE call_exports
                  SET filename=?, size_bytes=?, sha256=?, call_ids=?,
                      call_count=?, contact_count=?, storage=?, gcs_bucket=?,
                      gcs_object=?, gcs_uploaded_at=?, local_path=?,
                      upload_error=?
                WHERE id=? AND owner=?""",
            (filename, len(blob), export_store.sha256(blob),
             json.dumps(stats["call_ids"]), stats["call_count"],
             stats["contact_count"], storage, bucket, obj,
             (_utcnow() if storage == "gcs" else None),
             str(local), err, export_id, owner),
        )
        out = dict(db.execute("SELECT * FROM call_exports WHERE id=? AND owner=?",
                              (export_id, owner)).fetchone())
    payload = _export_row(out)
    payload["number_count"] = stats["number_count"]
    payload["ok"] = True
    return payload


@router.get("/exports")
def exports_list(owner: str = Depends(current_owner),
                 limit: int = Query(25, le=100)):
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            """SELECT * FROM call_exports
                WHERE owner=? AND deleted_at IS NULL
                ORDER BY id DESC LIMIT ?""", (owner, limit)).fetchall()]
    return {"items": [_export_row(r) for r in rows]}


@router.get("/exports/{eid}/download")
def export_download(eid: int, owner: str = Depends(current_owner)):
    """Session-authenticated download. Same bytes the tokenized link serves."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM call_exports WHERE id=? AND owner=? AND deleted_at IS NULL",
            (eid, owner)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="export not found")
    return stream_export(dict(row))


@router.delete("/exports/{eid}")
def export_delete(eid: int, owner: str = Depends(current_owner)):
    """Revokes the link and hides the row. The GCS object is deliberately left
    in place: the bucket is the customer's record of what was exported, and a
    dashboard click should not silently delete from their cloud storage."""
    with get_db() as db:
        cur = db.execute(
            "UPDATE call_exports SET deleted_at=now(), download_token=? "
            "WHERE id=? AND owner=? AND deleted_at IS NULL",
            (_revoked_token(eid), eid, owner))
    if not getattr(cur, "rowcount", 0):
        raise HTTPException(status_code=404, detail="export not found")
    return {"ok": True}


def stream_export(row: dict):
    """Local staging copy first, GCS second — the same order recordings use,
    because the local file is the one that certainly exists right after the
    export was built."""
    from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

    from shared import export_store

    name = row.get("filename") or "calls.xlsx"
    headers = {"Content-Disposition": f'attachment; filename="{name}"'}
    path = row.get("local_path")
    if path and Path(path).exists():
        return FileResponse(path, media_type=export_store.XLSX_MIME,
                            headers=headers)
    obj = row.get("gcs_object")
    if obj:
        try:
            # The bucket recorded on the ROW, not the one configured now —
            # see export_store.open_stream.
            stream = export_store.open_stream(obj, row.get("gcs_bucket"))
        except Exception as exc:
            logger.error("export %s unreadable from GCS: %r", row.get("id"), exc)
            return JSONResponse({"error": "export file is unavailable"},
                                status_code=404)
        return StreamingResponse(stream, media_type=export_store.XLSX_MIME,
                                 headers=headers)
    return JSONResponse({"error": "export file is unavailable"}, status_code=404)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

@router.get("/notifications")
def notifications_list(owner: str = Depends(current_owner),
                       unread: int = 0, limit: int = Query(50, le=200)):
    where = "owner=?" + (" AND read_at IS NULL" if unread else "")
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            f"""SELECT id, kind, severity, title, body, link, ref, read_at, created_at
                  FROM notifications WHERE {where}
                 ORDER BY id DESC LIMIT ?""",
            (owner, limit),
        ).fetchall()]
        unread_n = db.execute(
            "SELECT count(*) AS n FROM notifications WHERE owner=? AND read_at IS NULL",
            (owner,),
        ).fetchone()["n"]
    return {"items": rows, "unread": unread_n}


@router.patch("/notifications/{nid}/read")
def notification_read(nid: int, owner: str = Depends(current_owner)):
    with get_db() as db:
        db.execute(
            "UPDATE notifications SET read_at=now() WHERE id=? AND owner=?"
            " AND read_at IS NULL", (nid, owner),
        )
    return {"ok": True}


@router.post("/notifications/read-all")
def notifications_read_all(owner: str = Depends(current_owner)):
    with get_db() as db:
        db.execute(
            "UPDATE notifications SET read_at=now() WHERE owner=? AND read_at IS NULL",
            (owner,),
        )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Customer memory
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Customer Intelligence Hub (per-contact workspace)
# ---------------------------------------------------------------------------

def _contact_scoped(db, owner: str, contact_id: int) -> tuple[dict[str, Any], str]:
    """Resolve a contact the caller may see: their own ('owner' access) or one
    shared with them via contact_grants ('read'/'edit'). 404 otherwise —
    deny-by-default, mirroring the middleware doctrine."""
    row = db.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="contact not found")
    contact = dict(row)
    if contact.get("owner") == owner:
        return contact, "owner"
    try:
        grant = db.execute(
            "SELECT access FROM contact_grants WHERE contact_id=? AND grantee_email=?",
            (contact_id, owner)).fetchone()
    except Exception:
        grant = None
    if grant:
        return contact, grant["access"]
    raise HTTPException(status_code=404, detail="contact not found")


@router.get("/contacts/{contact_id}")
def contact_hub(contact_id: int, owner: str = Depends(current_owner)):
    """Hub payload: contact + lifetime stats + memory/digest + score trend +
    open commitments + upcoming follow-ups + recommended next actions."""
    with get_db() as db:
        contact, access = _contact_scoped(db, owner, contact_id)
        c_owner = contact.get("owner") or ""

        stats = dict(db.execute(
            """SELECT count(*) AS calls,
                      COALESCE(SUM(COALESCE(u.billsec_final, u.billsec_pipeline,
                                            c.duration_s, 0)), 0) AS talk_seconds,
                      MIN(c.started_at) AS first_call_at,
                      MAX(c.started_at) AS last_call_at
                 FROM calls c
                 LEFT JOIN usage_ledger u ON u.conn_id = c.conn_id
                WHERE c.contact_id=? AND c.owner=?""",
            (contact_id, c_owner)).fetchone())
        stats["calls_30d"] = db.execute(
            """SELECT count(*) AS n FROM calls
                WHERE contact_id=? AND owner=?
                  AND started_at::timestamptz > now() - interval '30 days'""",
            (contact_id, c_owner)).fetchone()["n"]
        outcome_mix = {r["k"]: r["n"] for r in db.execute(
            """SELECT COALESCE(NULLIF(outcome_kind, ''), 'unclassified') AS k,
                      count(*) AS n
                 FROM calls WHERE contact_id=? AND owner=? GROUP BY 1""",
            (contact_id, c_owner)).fetchall()}
        try:
            outcome_mix["missed"] = db.execute(
                """SELECT count(*) AS n FROM contact_events
                    WHERE contact_id=? AND kind='call.missed'""",
                (contact_id,)).fetchone()["n"]
        except Exception:
            pass

        mem_row = db.execute(
            """SELECT memory, calls_count, last_call_at, version,
                      context_digest, digest_updated_at
                 FROM customer_memory WHERE contact_id=?""",
            (contact_id,)).fetchone()

        trend = [dict(r) for r in db.execute(
            """SELECT ci.call_id, c.call_key, c.started_at,
                      ci.lead_score, ci.opportunity_score, ci.risk_score,
                      ci.sentiment
                 FROM call_intelligence ci
                 JOIN calls c ON c.id = ci.call_id
                WHERE ci.contact_id=? AND ci.owner=?
                ORDER BY c.started_at ASC LIMIT 50""",
            (contact_id, c_owner)).fetchall()]

        commitments = [dict(r) for r in db.execute(
            """SELECT id, call_id, made_by, kind, description, deadline_text,
                      deadline_at, is_vague, clarified, status, created_at
                 FROM commitments
                WHERE contact_id=? AND owner=? AND status='open'
                ORDER BY COALESCE(deadline_at::text, created_at::text)
                LIMIT 20""",
            (contact_id, c_owner)).fetchall()]

        followups = [dict(r) for r in db.execute(
            """SELECT id, call_id, reason, summary, followup_type, promised_by,
                      scheduled_at, priority, status
                 FROM followups
                WHERE contact_id=? AND owner=? AND deleted_at IS NULL
                  AND status IN ('pending', 'scheduled')
                ORDER BY scheduled_at NULLS LAST LIMIT 20""",
            (contact_id, c_owner)).fetchall()]

        latest = db.execute(
            """SELECT recommendations, summary, customer_intent, processed_at
                 FROM call_intelligence
                WHERE contact_id=? AND owner=?
                ORDER BY processed_at DESC LIMIT 1""",
            (contact_id, c_owner)).fetchone()

        grants = []
        if access == "owner":
            try:
                grants = [dict(r) for r in db.execute(
                    """SELECT id, grantee_email, access, created_at
                         FROM contact_grants WHERE contact_id=?""",
                    (contact_id,)).fetchall()]
            except Exception:
                grants = []

    memory = _j(mem_row["memory"], "{}") if mem_row else {}
    return {
        "contact": contact,
        "access": access,
        "stats": {**stats, "outcome_mix": outcome_mix},
        "memory": memory,
        "memory_meta": ({
            "calls_count": mem_row["calls_count"],
            "last_call_at": mem_row["last_call_at"],
            "version": mem_row["version"],
            "digest": mem_row["context_digest"],
            "digest_updated_at": mem_row["digest_updated_at"],
        } if mem_row else None),
        "scores": {
            "trend": trend,
            "latest": trend[-1] if trend else None,
        },
        "open_commitments": commitments,
        "upcoming_followups": followups,
        "recommendations": _j(latest["recommendations"], "{}") if latest else {},
        "latest_analysis": ({
            "summary": latest["summary"],
            "customer_intent": latest["customer_intent"],
            "processed_at": latest["processed_at"],
        } if latest else None),
        "grants": grants,
    }


@router.get("/contacts/{contact_id}/timeline")
def contact_timeline(contact_id: int, owner: str = Depends(current_owner),
                     before: int = Query(0, ge=0), kinds: str = "",
                     limit: int = Query(50, ge=1, le=100)):
    """Paginated unified timeline (contact_events), newest first. `before` is
    an id cursor; `kinds` an optional CSV filter."""
    with get_db() as db:
        _contact_scoped(db, owner, contact_id)
        where = ["contact_id = ?"]
        params: list[Any] = [contact_id]
        if before:
            where.append("id < ?")
            params.append(before)
        kind_list = [k.strip() for k in kinds.split(",") if k.strip()]
        if kind_list:
            where.append("kind = ANY(?)")
            params.append(kind_list)
        rows = [dict(r) for r in db.execute(
            # ORDER BY must match the cursor key. This sorted by occurred_at
            # while paginating on id — a late-inserted event with an older
            # occurred_at (missed-call backfills do this) could be skipped or
            # duplicated across pages. ids are assigned in emit order and the
            # UI labels rows from payload timestamps, so id alone is correct.
            f"""SELECT id, kind, ref_table, ref_id, occurred_at, payload
                  FROM contact_events
                 WHERE {' AND '.join(where)}
                 ORDER BY id DESC LIMIT ?""",
            (*params, limit)).fetchall()]
    for r in rows:
        r["payload"] = _j(r.get("payload"), "{}")
    next_before = rows[-1]["id"] if len(rows) == limit else None
    return {"items": rows, "next_before": next_before}


@router.get("/contacts/{contact_id}/calls")
def contact_calls(contact_id: int, owner: str = Depends(current_owner),
                  page: int = Query(1, ge=1),
                  limit: int = Query(25, ge=1, le=100)):
    """Call list for one contact, same row shape as /api/intel/calls."""
    with get_db() as db:
        contact, _access = _contact_scoped(db, owner, contact_id)
        c_owner = contact.get("owner") or ""
        offset = (page - 1) * limit
        rows = [dict(r) for r in db.execute(
            """SELECT c.id AS call_id, c.call_key, c.phone, c.direction,
                      c.agent_slug, c.started_at, c.duration_s, c.outcome,
                      c.contact_id, c.language,
                      ci.summary, ci.customer_intent, ci.sentiment, ci.urgency,
                      ci.lead_score, ci.opportunity_score, ci.risk_score,
                      ci.tags, ci.processed_at,
                      f.id AS followup_id, f.decision AS followup_decision,
                      f.status AS followup_status, f.scheduled_at AS followup_at
                 FROM calls c
                 LEFT JOIN call_intelligence ci ON ci.call_id = c.id
                 LEFT JOIN LATERAL (
                       SELECT * FROM followups f2
                        WHERE f2.call_id = c.id AND f2.deleted_at IS NULL
                        ORDER BY f2.id DESC LIMIT 1
                 ) f ON true
                WHERE c.contact_id=? AND c.owner=?
                ORDER BY c.started_at DESC NULLS LAST
                LIMIT ? OFFSET ?""",
            (contact_id, c_owner, limit, offset)).fetchall()]
        total = db.execute(
            "SELECT count(*) AS n FROM calls WHERE contact_id=? AND owner=?",
            (contact_id, c_owner)).fetchone()["n"]
    for r in rows:
        r["tags"] = _j(r.get("tags"))
    return {"items": rows, "total": total, "page": page, "limit": limit}


@router.patch("/contacts/{contact_id}")
def contact_hub_patch(contact_id: int, body: dict,
                      owner: str = Depends(current_owner)):
    """Relationship ownership: reassign the primary agent. Owner-only —
    grants never confer this (flattened-roles doctrine: no admin, the owning
    tenant is the only authority)."""
    with get_db() as db:
        contact, access = _contact_scoped(db, owner, contact_id)
        if access != "owner":
            raise HTTPException(status_code=403, detail="owner only")
        if "primary_agent_slug" not in body:
            raise HTTPException(status_code=422, detail="nothing to update")
        slug = (body.get("primary_agent_slug") or "").strip() or None
        db.execute("UPDATE contacts SET primary_agent_slug=? WHERE id=?",
                   (slug, contact_id))
    _timeline_emit(contact_id, owner, "contact.updated",
                   payload={"fields": ["primary_agent_slug"],
                            "primary_agent_slug": slug})
    return {"ok": True}


@router.post("/contacts/{contact_id}/grants")
def contact_grant_add(contact_id: int, body: dict,
                      owner: str = Depends(current_owner)):
    """Share a contact with another tenant (read or edit). Granted and
    revoked only by the owning tenant."""
    grantee = (body.get("grantee_email") or "").strip().lower()
    access = (body.get("access") or "read").strip().lower()
    if not grantee or "@" not in grantee:
        raise HTTPException(status_code=422, detail="grantee_email required")
    if access not in ("read", "edit"):
        raise HTTPException(status_code=422, detail="access must be read|edit")
    if grantee == owner:
        raise HTTPException(status_code=422, detail="cannot grant to yourself")
    with get_db() as db:
        _contact, who = _contact_scoped(db, owner, contact_id)
        if who != "owner":
            raise HTTPException(status_code=403, detail="owner only")
        row = db.execute(
            """INSERT INTO contact_grants (contact_id, owner, grantee_email, access)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (contact_id, grantee_email)
               DO UPDATE SET access=EXCLUDED.access
               RETURNING id""",
            (contact_id, owner, grantee, access)).fetchone()
    _timeline_emit(contact_id, owner, "contact.updated",
                   payload={"fields": ["grant"], "grantee": grantee,
                            "access": access})
    return {"ok": True, "id": row["id"]}


@router.delete("/contacts/{contact_id}/grants/{grant_id}")
def contact_grant_revoke(contact_id: int, grant_id: int,
                         owner: str = Depends(current_owner)):
    with get_db() as db:
        _contact, who = _contact_scoped(db, owner, contact_id)
        if who != "owner":
            raise HTTPException(status_code=403, detail="owner only")
        cur = db.execute(
            "DELETE FROM contact_grants WHERE id=? AND contact_id=?",
            (grant_id, contact_id))
    if not getattr(cur, "rowcount", 0):
        raise HTTPException(status_code=404, detail="grant not found")
    return {"ok": True}


@router.get("/memory/{contact_id}")
def memory_get(contact_id: int, owner: str = Depends(current_owner)):
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM customer_memory WHERE contact_id=? AND owner=?",
            (contact_id, owner),
        ).fetchone()
    if not row:
        return {"contact_id": contact_id, "memory": None}
    return {"contact_id": contact_id, "calls_count": row["calls_count"],
            "last_call_at": row["last_call_at"], "updated_at": row["updated_at"],
            "memory": _j(row["memory"], "{}"),
            "digest": digest_for_contact(contact_id)}


# ---------------------------------------------------------------------------
# Automation rules
# ---------------------------------------------------------------------------

def _validate_rule(body: dict) -> None:
    from shared.intelligence.automation import ACTIONS, TRIGGERS

    if body.get("trigger") not in TRIGGERS:
        raise HTTPException(status_code=422, detail=f"trigger must be one of {TRIGGERS}")
    actions = body.get("actions") or []
    if not isinstance(actions, list) or not actions:
        raise HTTPException(status_code=422, detail="at least one action required")
    for a in actions:
        if not isinstance(a, dict) or a.get("type") not in ACTIONS:
            raise HTTPException(status_code=422, detail=f"action type must be one of {ACTIONS}")
    if not isinstance(body.get("conditions") or {}, dict):
        raise HTTPException(status_code=422, detail="conditions must be an object")


def _snapshot_rule(db, rule: dict, actor: str) -> None:
    db.execute(
        """INSERT INTO automation_rule_versions (rule_id, version, owner, actor, definition)
           VALUES (?, ?, ?, ?, ?) ON CONFLICT (rule_id, version) DO NOTHING""",
        (rule["id"], rule["version"], rule["owner"], actor,
         json.dumps({k: rule.get(k) for k in
                     ("name", "description", "trigger", "conditions", "actions",
                      "enabled")})),
    )
    db.execute(  # retention: keep the newest 50 snapshots per rule
        """DELETE FROM automation_rule_versions WHERE rule_id=? AND id NOT IN
           (SELECT id FROM automation_rule_versions WHERE rule_id=?
             ORDER BY id DESC LIMIT 50)""",
        (rule["id"], rule["id"]),
    )


@router.get("/automations")
def automations_list(owner: str = Depends(current_owner)):
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            """SELECT r.*,
                      (SELECT count(*) FROM automation_runs ar
                        WHERE ar.rule_id = r.id) AS runs_total,
                      (SELECT count(*) FROM automation_runs ar
                        WHERE ar.rule_id = r.id
                          AND ar.created_at > now() - interval '7 days') AS runs_7d
                 FROM automation_rules r
                WHERE r.owner=? AND r.deleted_at IS NULL
                ORDER BY r.id DESC""",
            (owner,),
        ).fetchall()]
    for r in rows:
        r["conditions"] = _j(r.get("conditions"), "{}")
        r["actions"] = _j(r.get("actions"))
    return {"items": rows}


@router.get("/automations/templates")
def automations_templates(owner: str = Depends(current_owner)):
    from shared.intelligence.automation import ACTIONS, TEMPLATES, TRIGGERS

    return {"templates": TEMPLATES, "triggers": TRIGGERS, "actions": ACTIONS}


@router.post("/automations")
def automation_create(body: dict, owner: str = Depends(current_owner)):
    if not (body.get("name") or "").strip():
        raise HTTPException(status_code=422, detail="name required")
    _validate_rule(body)
    with get_db() as db:
        row = db.execute(
            """INSERT INTO automation_rules
                   (owner, name, description, enabled, trigger, conditions,
                    actions, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING *""",
            (owner, body["name"][:120], (body.get("description") or "")[:500],
             1 if body.get("enabled", True) else 0, body["trigger"],
             json.dumps(body.get("conditions") or {}),
             json.dumps(body.get("actions") or []), owner),
        ).fetchone()
        _snapshot_rule(db, dict(row), owner)
    return {"ok": True, "id": row["id"]}


@router.patch("/automations/{rid}")
def automation_patch(rid: int, body: dict, owner: str = Depends(current_owner)):
    with get_db() as db:
        rule = db.execute(
            "SELECT * FROM automation_rules WHERE id=? AND owner=? AND deleted_at IS NULL",
            (rid, owner),
        ).fetchone()
    if not rule:
        raise HTTPException(status_code=404, detail="rule not found")
    merged = {**dict(rule), **{k: body[k] for k in
              ("name", "description", "trigger", "conditions", "actions", "enabled")
              if k in body}}
    if any(k in body for k in ("trigger", "conditions", "actions")):
        _validate_rule({"trigger": merged["trigger"],
                        "conditions": merged["conditions"] if isinstance(merged["conditions"], dict)
                        else _j(merged["conditions"], "{}"),
                        "actions": merged["actions"] if isinstance(merged["actions"], list)
                        else _j(merged["actions"])})
    with get_db() as db:
        row = db.execute(
            """UPDATE automation_rules
                   SET name=?, description=?, trigger=?, conditions=?, actions=?,
                       enabled=?, version=version+1, updated_at=now()
                 WHERE id=? AND owner=? RETURNING *""",
            (str(merged["name"])[:120], str(merged.get("description") or "")[:500],
             merged["trigger"],
             json.dumps(merged["conditions"]) if isinstance(merged["conditions"], dict)
             else merged["conditions"],
             json.dumps(merged["actions"]) if isinstance(merged["actions"], list)
             else merged["actions"],
             1 if merged.get("enabled") in (1, True) else 0, rid, owner),
        ).fetchone()
        _snapshot_rule(db, dict(row), owner)
    return {"ok": True, "version": row["version"]}


@router.delete("/automations/{rid}")
def automation_delete(rid: int, owner: str = Depends(current_owner)):
    with get_db() as db:
        cur = db.execute(
            "UPDATE automation_rules SET deleted_at=now(), enabled=0"
            " WHERE id=? AND owner=?", (rid, owner),
        )
    if not getattr(cur, "rowcount", 0):
        raise HTTPException(status_code=404, detail="rule not found")
    return {"ok": True}


@router.get("/automations/{rid}/runs")
def automation_runs(rid: int, owner: str = Depends(current_owner),
                    limit: int = Query(50, le=200)):
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            """SELECT ar.*, c.call_key, c.phone
                 FROM automation_runs ar
                 LEFT JOIN calls c ON c.id = ar.call_id
                WHERE ar.rule_id=? AND ar.owner=?
                ORDER BY ar.id DESC LIMIT ?""",
            (rid, owner, limit),
        ).fetchall()]
    for r in rows:
        r["steps"] = _j(r.get("steps"))
        r["trigger_event"] = _j(r.get("trigger_event"), "{}")
    return {"items": rows}


# ---------------------------------------------------------------------------
# Pipeline status + DLQ
# ---------------------------------------------------------------------------

@router.get("/status")
def intel_status(owner: str = Depends(current_owner)):
    with get_db() as db:
        jobs = {r["status"]: r["n"] for r in db.execute(
            "SELECT status, count(*) AS n FROM intel_jobs GROUP BY status"
        ).fetchall()}
        coverage = db.execute(
            """SELECT count(c.id) AS calls, count(ci.call_id) AS analyzed,
                      count(*) FILTER (WHERE ci.stages::jsonb->>'embed' = 'done') AS embedded
                 FROM calls c LEFT JOIN call_intelligence ci ON ci.call_id = c.id
                WHERE c.owner = ?""",
            (owner,),
        ).fetchone()
        beat = db.execute(
            "SELECT value FROM settings WHERE owner='' AND key='intel_worker_beat'"
        ).fetchone()
    return {"queue": jobs, "coverage": dict(coverage),
            "worker": _j(beat["value"], "{}") if beat else None}


@router.get("/jobs")
def jobs_list(owner: str = Depends(current_owner), status: str = "failed",
              limit: int = Query(50, le=200)):
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            """SELECT j.id, j.kind, j.status, j.attempts, j.last_error,
                      j.created_at, j.finished_at, c.call_key, c.phone
                 FROM intel_jobs j LEFT JOIN calls c ON c.id = j.call_id
                WHERE j.owner = ? AND j.status = ?
                ORDER BY j.id DESC LIMIT ?""",
            (owner, status, limit),
        ).fetchall()]
    return {"items": rows}


@router.post("/jobs/{jid}/retry")
def job_retry(jid: int, owner: str = Depends(current_owner)):
    with get_db() as db:
        row = db.execute(
            "SELECT id FROM intel_jobs WHERE id=? AND owner=?", (jid, owner)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="job not found")
    if not intel_queue.requeue(jid):
        raise HTTPException(status_code=409, detail="job is not in a retryable state")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Aggregated analytics (SQL-side — replaces the O(n)-blob-scan legacy shape)
# ---------------------------------------------------------------------------

# Every window below is measured on c.started_at — WHEN THE CALL HAPPENED —
# never on ci.processed_at, which is when the analysis LLM happened to run.
# Those are not the same clock: the intelligence layer went live 2026-07-20 and
# backfilled a month of history, so 86 of 149 calls had a processed_at on a
# different day than the call. Bucketing on processed_at put that whole backfill
# on one day ("7d" showed every call ever, with a cliff-edge spike) and made
# every trend line a picture of worker scheduling rather than of the business.
_WINDOW = "c.started_at::timestamptz > now() - make_interval(days => ?)"

# Named lead bands, shared by the KPI row and the distribution chart so a call
# can never be counted hot in one place and warm in another.
_LEAD_BANDS = [("hot", 70, 101), ("warm", 40, 70), ("cold", 0, 40)]


@router.get("/analytics")
def intel_analytics(owner: str = Depends(current_owner), days: int = Query(30, ge=1),
                    agent: str = ""):
    # Every panel below the filter row is scoped by the SAME clause, so the KPI
    # cards, the topic/objection lists and the trend line can never describe a
    # different population than the table underneath them.
    _agents = _agent_list(agent)
    agent_sql = " AND c.agent_slug = ANY(?)" if _agents else ""
    ap: list = [_agents] if _agents else []
    with get_db() as db:
        head = db.execute(
            f"""SELECT count(*) AS analyzed,
                      round(avg(ci.lead_score))::int AS avg_lead,
                      round(avg(ci.opportunity_score))::int AS avg_opportunity,
                      round(avg(ci.risk_score))::int AS avg_risk,
                      count(*) FILTER (WHERE ci.sentiment='positive') AS positive,
                      count(*) FILTER (WHERE ci.sentiment='neutral') AS neutral,
                      count(*) FILTER (WHERE ci.sentiment='negative') AS negative,
                      count(*) FILTER (WHERE ci.lead_score >= 70) AS hot_leads
                 FROM call_intelligence ci
                 JOIN calls c ON c.id = ci.call_id
                WHERE ci.owner=? AND {_WINDOW}{agent_sql}""",
            (owner, days, *ap),
        ).fetchone()
        # followups carries no agent column; join through the call it came
        # from so this row reads the same population as everything else.
        followups = {r["decision"]: r["n"] for r in db.execute(
            f"""SELECT fu.decision, count(*) AS n FROM followups fu
                  LEFT JOIN calls c ON c.id = fu.call_id
                 WHERE fu.owner=? AND fu.deleted_at IS NULL
                   AND fu.created_at > now() - make_interval(days => ?)
                   {agent_sql}
                 GROUP BY fu.decision""",
            (owner, days, *ap),
        ).fetchall()}

        def top_list(field: str, cap: int = 15):
            return [{"label": r["item"], "count": r["n"]} for r in db.execute(
                f"""SELECT item, count(*) AS n
                      FROM call_intelligence ci
                      JOIN calls c ON c.id = ci.call_id,
                           jsonb_array_elements_text(ci.{field}::jsonb) AS item
                     WHERE ci.owner=? AND ci.{field} IS NOT NULL AND {_WINDOW}
                           {agent_sql}
                     GROUP BY item ORDER BY n DESC LIMIT ?""",
                (owner, days, *ap, cap),
            ).fetchall()]

        topics = top_list("topics")
        objections = top_list("objections")
        products = top_list("product_interest")
        trends = [dict(r) for r in db.execute(
            f"""SELECT c.started_at::date::text AS day, count(*) AS calls,
                      round(avg(ci.lead_score))::int AS avg_lead,
                      count(*) FILTER (WHERE ci.sentiment='negative') AS negative
                 FROM call_intelligence ci
                 JOIN calls c ON c.id = ci.call_id
                WHERE ci.owner=? AND {_WINDOW}{agent_sql}
                GROUP BY 1 ORDER BY 1""",
            (owner, days, *ap),
        ).fetchall()]
        # Options come from the WINDOW, never the agent filter itself — a
        # dropdown that removes its own other choices cannot be changed back.
        agent_options = [r["agent_slug"] for r in db.execute(
            f"""SELECT DISTINCT c.agent_slug FROM calls c
                 WHERE c.owner=? AND {_WINDOW}
                   AND c.agent_slug IS NOT NULL AND c.agent_slug <> ''
                 ORDER BY 1""",
            (owner, days),
        ).fetchall()]
    return {"summary": dict(head), "followup_decisions": followups,
            "topics": topics, "objections": objections, "products": products,
            "trends": trends, "days": days, "agent": agent,
            "agent_options": agent_options}


# ---------------------------------------------------------------------------
# Conversation analytics — the deep-dive dashboard behind /conv-analytics.
#
# Replaces GET /api/analytics/conversation, which read the LEGACY calls.analysis
# JSON blob: every request pulled and json.loads()'d every analyzed row in the
# tenant, then aggregated in Python, with no date range, no agent filter, and a
# trend axis parsed out of recording-folder names. Everything here is one SQL
# pass over call_intelligence with a real window and a period-over-period delta.
# ---------------------------------------------------------------------------

def _pct(part: float | None, whole: float | None) -> float | None:
    if not whole:
        return None
    return round((part or 0) / whole * 100, 1)


@router.get("/analytics/conversation",
            dependencies=[Depends(require_feature("conversation_analytics"))])
def conversation_analytics(owner: str = Depends(current_owner),
                           days: int = Query(30, ge=1, le=365),
                           agent: str = "",
                           direction: str = "",
                           language: str = "",
                           sentiment: str = "",
                           lead_band: str = ""):
    """Every panel on /conv-analytics, over one scoped set of calls.

    `agent` was the only way to narrow the call set, which made the page an
    all-or-nothing view: there was no way to ask "how do outbound Gujarati
    calls compare" without leaving for Intelligence and hand-picking rows.
    Direction / language / sentiment / lead band are deliberately COARSE — they
    name a cohort, not individual calls — and every one of them folds into the
    single `scoped` predicate below, so the KPI deltas, the funnel, the
    heatmap and the leaderboard all describe exactly the same population.
    """
    scope = ["ci.owner = ?"]
    base: list[Any] = [owner]
    if agent:
        scope.append("c.agent_slug = ?")
        base.append(agent)
    if direction:
        scope.append("COALESCE(c.direction,'unknown') = ?")
        base.append(direction)
    if language:
        scope.append("COALESCE(NULLIF(c.language,''),'unknown') = ?")
        base.append(language)
    if sentiment in ("positive", "neutral", "negative"):
        scope.append("ci.sentiment = ?")
        base.append(sentiment)
    band = next((b for b in _LEAD_BANDS if b[0] == lead_band), None)
    if band:
        # Same band edges the KPI row and the distribution chart use, so a
        # call can never be counted hot by the filter and warm by the chart.
        scope.append("(ci.lead_score >= ? AND ci.lead_score < ?)")
        base += [band[1], band[2]]
    elif lead_band == "unscored":
        scope.append("ci.lead_score IS NULL")
    scoped = " AND ".join(scope)

    # `offset_days` shifts the window back one full period so the same query
    # serves both "current" and "previous" for the delta chips.
    window = ("c.started_at::timestamptz > now() - make_interval(days => ?) "
              "AND c.started_at::timestamptz <= now() - make_interval(days => ?)")

    def kpi(db, days_back: int, offset_days: int) -> dict:
        return dict(db.execute(
            f"""SELECT count(*) AS analyzed,
                       round(avg(ci.lead_score))::int      AS avg_lead,
                       round(avg(ci.risk_score))::int      AS avg_risk,
                       round(avg(ci.opportunity_score))::int AS avg_opportunity,
                       round(avg(ci.conversation_quality)::numeric, 2) AS avg_quality,
                       round(avg(ci.satisfaction)::numeric, 2)         AS avg_satisfaction,
                       round(avg(ci.conversion_probability)::numeric, 3) AS avg_conversion,
                       round(avg(c.duration_s)::numeric)::int AS avg_duration,
                       sum(c.duration_s)::bigint            AS total_duration,
                       count(*) FILTER (WHERE ci.lead_score >= 70)   AS hot,
                       count(*) FILTER (WHERE ci.sentiment='positive') AS positive,
                       count(*) FILTER (WHERE ci.sentiment='neutral')  AS neutral,
                       count(*) FILTER (WHERE ci.sentiment='negative') AS negative,
                       count(*) FILTER (WHERE ci.low_confidence = 1)   AS low_confidence
                  FROM call_intelligence ci
                  JOIN calls c ON c.id = ci.call_id
                 WHERE {scoped} AND {window}""",
            (*base, days_back + offset_days, offset_days),
        ).fetchone())

    def top_list(db, field: str, cap: int = 12) -> list[dict]:
        return [{"label": r["item"], "count": r["n"]} for r in db.execute(
            f"""SELECT item, count(*) AS n
                  FROM call_intelligence ci
                  JOIN calls c ON c.id = ci.call_id,
                       jsonb_array_elements_text(ci.{field}::jsonb) AS item
                 WHERE {scoped} AND ci.{field} IS NOT NULL AND {_WINDOW}
                   AND length(trim(item)) > 0
                 GROUP BY item ORDER BY n DESC, item LIMIT ?""",
            (*base, days, cap),
        ).fetchall()]

    with get_db() as db:
        current = kpi(db, days, 0)
        previous = kpi(db, days, days)

        # One row per calendar day in the window, zero-filled, so the chart has
        # no phantom gaps where a quiet day simply produced no rows.
        # The tenant filter lives INSIDE the aggregate subquery, not in the
        # outer join — putting ci.owner in a LEFT JOIN's ON clause would let
        # other tenants' calls survive the join and pollute the duration mean.
        series = [dict(r) for r in db.execute(
            f"""WITH daily AS (
                    SELECT c.started_at::date AS day,
                           count(*) AS calls,
                           count(*) FILTER (WHERE ci.sentiment='positive') AS positive,
                           count(*) FILTER (WHERE ci.sentiment='neutral')  AS neutral,
                           count(*) FILTER (WHERE ci.sentiment='negative') AS negative,
                           count(*) FILTER (WHERE ci.lead_score >= 70)     AS hot,
                           round(avg(ci.lead_score))::int                  AS avg_lead,
                           round(avg(c.duration_s)::numeric)::int          AS avg_duration
                      FROM call_intelligence ci
                      JOIN calls c ON c.id = ci.call_id
                     WHERE {scoped} AND {_WINDOW}
                     GROUP BY 1
                )
                SELECT d::date::text AS day,
                       COALESCE(daily.calls, 0)    AS calls,
                       COALESCE(daily.positive, 0) AS positive,
                       COALESCE(daily.neutral, 0)  AS neutral,
                       COALESCE(daily.negative, 0) AS negative,
                       COALESCE(daily.hot, 0)      AS hot,
                       daily.avg_lead, daily.avg_duration
                  FROM generate_series(
                         (now() - make_interval(days => ?))::date, now()::date,
                         interval '1 day') AS d
                  LEFT JOIN daily ON daily.day = d::date
                 ORDER BY 1""",
            (*base, days, days - 1),
        ).fetchall()]

        bands = dict(db.execute(
            f"""SELECT {', '.join(
                    f"count(*) FILTER (WHERE ci.lead_score >= {lo} AND ci.lead_score < {hi}) AS {name}"
                    for name, lo, hi in _LEAD_BANDS)},
                       count(*) FILTER (WHERE ci.lead_score IS NULL) AS unscored
                  FROM call_intelligence ci
                  JOIN calls c ON c.id = ci.call_id
                 WHERE {scoped} AND {_WINDOW}""",
            (*base, days),
        ).fetchone())

        # Per-agent leaderboard: the single view that says which agent script is
        # actually working. Not derivable from the legacy blob at all.
        agents = [dict(r) for r in db.execute(
            f"""SELECT COALESCE(c.agent_slug, 'unknown') AS agent,
                       count(*) AS calls,
                       round(avg(ci.lead_score))::int AS avg_lead,
                       round(avg(ci.conversation_quality)::numeric, 2) AS avg_quality,
                       round(avg(c.duration_s)::numeric)::int AS avg_duration,
                       count(*) FILTER (WHERE ci.lead_score >= 70) AS hot,
                       count(*) FILTER (WHERE ci.sentiment='positive') AS positive,
                       count(*) FILTER (WHERE ci.sentiment='negative') AS negative
                  FROM call_intelligence ci
                  JOIN calls c ON c.id = ci.call_id
                 WHERE {scoped} AND {_WINDOW}
                 GROUP BY 1 ORDER BY calls DESC LIMIT 20""",
            (*base, days),
        ).fetchall()]

        # Conversion funnel. Each stage is a real recorded fact, not a guess:
        #   reached    — the call connected and the customer actually spoke
        #                (a 0s or agent-only leg is not a conversation)
        #   qualified  — the analysis scored the lead at 40+
        #   committed  — the customer agreed to SOMETHING concrete and the
        #                commitment extractor logged it (visit, callback, quote…)
        #   scheduled  — a follow-up was booked off the back of it
        #   won        — the call was marked outcome_kind='success'
        # Stages are counted independently, not as strict subsets, so a stage
        # can legitimately exceed the one below it; the UI shows each as a share
        # of `reached` rather than implying every drop-off is a loss.
        funnel = dict(db.execute(
            f"""SELECT count(*) AS analyzed,
                       count(*) FILTER (WHERE COALESCE(c.duration_s,0) >= 5) AS reached,
                       count(*) FILTER (WHERE ci.lead_score >= 40)           AS qualified,
                       count(*) FILTER (WHERE EXISTS (
                           SELECT 1 FROM commitments cm WHERE cm.call_id = c.id))
                                                                             AS committed,
                       count(*) FILTER (WHERE EXISTS (
                           SELECT 1 FROM followups f2
                            WHERE f2.call_id = c.id AND f2.deleted_at IS NULL
                              AND f2.decision = 'yes'))                      AS scheduled,
                       count(*) FILTER (WHERE c.outcome_kind = 'success')    AS won
                  FROM call_intelligence ci
                  JOIN calls c ON c.id = ci.call_id
                 WHERE {scoped} AND {_WINDOW}""",
            (*base, days),
        ).fetchone())

        # What customers actually agreed to — the commitment mix is the closest
        # thing to a revenue pipeline this data supports.
        commitments = [dict(r) for r in db.execute(
            f"""SELECT cm.kind, count(*) AS n,
                       count(*) FILTER (WHERE cm.status <> 'open') AS closed
                  FROM commitments cm
                  JOIN calls c ON c.id = cm.call_id
                  JOIN call_intelligence ci ON ci.call_id = c.id
                 WHERE {scoped} AND {_WINDOW}
                 GROUP BY 1 ORDER BY n DESC""",
            (*base, days),
        ).fetchall()]

        # Hour-of-day × day-of-week volume: staffing/dial-window evidence.
        heat = [dict(r) for r in db.execute(
            f"""SELECT extract(dow from c.started_at)::int AS dow,
                       extract(hour from c.started_at)::int AS hour,
                       count(*) AS calls,
                       round(avg(ci.lead_score))::int AS avg_lead
                  FROM call_intelligence ci
                  JOIN calls c ON c.id = ci.call_id
                 WHERE {scoped} AND {_WINDOW}
                 GROUP BY 1, 2""",
            (*base, days),
        ).fetchall()]

        mix = [dict(r) for r in db.execute(
            f"""SELECT COALESCE(c.direction,'unknown') AS direction,
                       COALESCE(NULLIF(c.language,''),'unknown') AS language,
                       count(*) AS calls,
                       round(avg(ci.lead_score))::int AS avg_lead
                  FROM call_intelligence ci
                  JOIN calls c ON c.id = ci.call_id
                 WHERE {scoped} AND {_WINDOW}
                 GROUP BY 1, 2 ORDER BY calls DESC""",
            (*base, days),
        ).fetchall()]

        # Joined through call_intelligence and scoped like every other panel:
        # a footer counting follow-ups from calls the rest of the page has
        # filtered out is a number that contradicts the page it sits on.
        followups = dict(db.execute(
            f"""SELECT count(*) AS total,
                       count(*) FILTER (WHERE f.decision='yes')    AS yes,
                       count(*) FILTER (WHERE f.decision='unsure') AS unsure,
                       count(*) FILTER (WHERE f.status='completed') AS completed,
                       count(*) FILTER (WHERE f.status='missed')    AS missed
                  FROM followups f
                  JOIN calls c ON c.id = f.call_id
                  JOIN call_intelligence ci ON ci.call_id = c.id
                 WHERE f.deleted_at IS NULL AND {scoped} AND {_WINDOW}""",
            (*base, days),
        ).fetchone())

        lists = {name: top_list(db, name) for name in
                 ("topics", "objections", "product_interest", "pain_points",
                  "buying_signals", "competitor_mentions")}

        # Picker options are deliberately NOT scoped by the active filters —
        # narrowing them to the current selection is how a filter row becomes a
        # dead end you can only escape with Reset.
        agent_options = [r["agent_slug"] for r in db.execute(
            """SELECT DISTINCT agent_slug FROM calls
                WHERE owner=? AND agent_slug IS NOT NULL ORDER BY 1""",
            (owner,),
        ).fetchall()]
        language_options = [r["language"] for r in db.execute(
            """SELECT DISTINCT COALESCE(NULLIF(c.language,''),'unknown') AS language
                 FROM calls c
                 JOIN call_intelligence ci ON ci.call_id = c.id
                WHERE ci.owner=? ORDER BY 1""",
            (owner,),
        ).fetchall()]
        direction_options = [r["direction"] for r in db.execute(
            """SELECT DISTINCT COALESCE(c.direction,'unknown') AS direction
                 FROM calls c
                 JOIN call_intelligence ci ON ci.call_id = c.id
                WHERE ci.owner=? ORDER BY 1""",
            (owner,),
        ).fetchall()]

    analyzed = current.get("analyzed") or 0
    current["hot_rate"] = _pct(current.get("hot"), analyzed)
    current["negative_rate"] = _pct(current.get("negative"), analyzed)
    current["positive_rate"] = _pct(current.get("positive"), analyzed)
    prev_analyzed = previous.get("analyzed") or 0
    previous["hot_rate"] = _pct(previous.get("hot"), prev_analyzed)
    previous["negative_rate"] = _pct(previous.get("negative"), prev_analyzed)
    previous["positive_rate"] = _pct(previous.get("positive"), prev_analyzed)

    return {"days": days, "agent": agent or None,
            "filters": {"agent": agent or "", "direction": direction or "",
                        "language": language or "", "sentiment": sentiment or "",
                        "lead_band": lead_band or ""},
            "current": current, "previous": previous,
            "series": series, "lead_bands": bands, "agents": agents,
            "heatmap": heat, "mix": mix, "followups": followups,
            "funnel": funnel, "commitments": commitments,
            "agent_options": agent_options,
            "language_options": language_options,
            "direction_options": direction_options, **lists}
