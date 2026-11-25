"""Report generation — `report.generate` jobs.

A report is an LLM synthesis over the STORED intelligence of a selected call
set (never raw transcripts unless the set is tiny — token discipline), saved
as markdown in the reports table. Large sets are map-reduced in batches.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from shared.db.database import get_db
from shared.intelligence import config

_BATCH = 40          # calls per synthesis pass
_RAW_TRANSCRIPT_MAX = 5   # below this call count, raw transcripts are included

_SYSTEM = """You are a senior sales/operations analyst writing an internal business report
from AI-analyzed phone-call records. Write clear, decision-oriented English in GitHub
markdown. Structure:

# <title>
## Executive summary        (3-6 bullet takeaways)
## Key findings             (patterns across calls: intents, objections, sentiment, risks)
## Notable calls            (short table: customer, date, what mattered)
## Recommended actions      (numbered, concrete, assignable)

Ground every claim in the supplied records; never invent customers, numbers, or quotes.
If the record set is thin or low-quality, say so plainly in the executive summary."""


def _call_digest(row: dict[str, Any]) -> dict[str, Any]:
    def j(field, default):
        try:
            return json.loads(row.get(field) or default)
        except Exception:
            return json.loads(default)

    return {
        "call_id": row["call_id"], "date": (row.get("started_at") or "")[:16],
        "phone": row.get("phone"), "direction": row.get("direction"),
        "agent": row.get("agent_slug"), "duration_s": row.get("duration_s"),
        "summary": row.get("summary"), "intent": row.get("customer_intent"),
        "sentiment": row.get("sentiment"), "lead_score": row.get("lead_score"),
        "risk_score": row.get("risk_score"), "urgency": row.get("urgency"),
        "topics": j("topics", "[]"), "objections": j("objections", "[]"),
        "buying_signals": j("buying_signals", "[]"),
        "outcome": row.get("outcome"),
    }


async def _llm(system: str, user: str) -> str:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY,
                         timeout=max(config.LLM_TIMEOUT_S, 120))
    try:
        resp = await client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=config.LLM_MAX_OUTPUT_TOKENS,
            temperature=0.3,
            reasoning_effort=config.LLM_REASONING_EFFORT or "low",
        )
        text = resp.choices[0].message.content or ""
        # Models occasionally degenerate into huge whitespace runs at the end
        # of long generations — collapse them so documents stay clean.
        return re.sub(r"[ \t]{20,}", " ", text).strip()
    finally:
        await client.close()


async def generate(job: dict[str, Any]) -> None:
    report_id = (job.get("payload") or {}).get("report_id")
    if not report_id:
        raise ValueError("report.generate job without report_id")

    with get_db() as db:
        rep = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if not rep:
            raise ValueError(f"report {report_id} not found")
        rep = dict(rep)
        db.execute("UPDATE reports SET status='running' WHERE id=?", (report_id,))

    try:
        call_ids = json.loads(rep.get("call_ids") or "[]")
        owner = rep["owner"]
        with get_db() as db:
            rows = [dict(r) for r in db.execute(
                f"""SELECT ci.*, c.phone, c.direction, c.started_at, c.duration_s,
                           c.outcome, c.transcript
                      FROM call_intelligence ci
                      JOIN calls c ON c.id = ci.call_id
                     WHERE ci.owner=? AND ci.call_id = ANY(?)
                     ORDER BY c.started_at""",
                (owner, call_ids),
            ).fetchall()]
        if not rows:
            raise ValueError("no analyzed calls in the selection")

        digests = [_call_digest(r) for r in rows]
        title = rep.get("title") or "Call intelligence report"
        prompt_head = (
            f"Report title: {title}\n"
            f"Requested by filter: {rep.get('query_prompt') or '(manual selection)'}\n"
            f"Generated: {dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d %H:%M IST')}\n"
            f"Calls in scope: {len(digests)}\n\n"
        )

        if len(digests) <= _BATCH:
            body = prompt_head + "CALL RECORDS (JSON):\n" + json.dumps(
                digests, ensure_ascii=False)
            if len(rows) <= _RAW_TRANSCRIPT_MAX:
                body += "\n\nRAW TRANSCRIPTS:\n" + "\n\n---\n\n".join(
                    (r.get("transcript") or "")[:8000] for r in rows)
            document = await _llm(_SYSTEM, body)
        else:
            partials = []
            for i in range(0, len(digests), _BATCH):
                chunk = digests[i:i + _BATCH]
                partials.append(await _llm(
                    _SYSTEM.replace("business report", "batch analysis"),
                    prompt_head + f"(batch {i // _BATCH + 1}) CALL RECORDS:\n"
                    + json.dumps(chunk, ensure_ascii=False)))
            document = await _llm(
                _SYSTEM,
                prompt_head + "Synthesize ONE final report from these batch "
                "analyses:\n\n" + "\n\n=====\n\n".join(partials))

        with get_db() as db:
            db.execute(
                """UPDATE reports SET status='done', document=?, model=?,
                          call_count=?, completed_at=now() WHERE id=?""",
                (document, config.LLM_MODEL, len(rows), report_id),
            )
            db.execute(
                """INSERT INTO notifications (owner, kind, severity, title, body, link, ref)
                   VALUES (?, 'report.ready', 'info', ?, ?, ?, ?)""",
                (owner, f"Report ready: {title}",
                 f"{len(rows)} calls analyzed", f"/reports?open={report_id}",
                 f"report:{report_id}"),
            )
    except Exception as exc:
        with get_db() as db:
            db.execute("UPDATE reports SET status='failed', error=? WHERE id=?",
                       (repr(exc)[:800], report_id))
        raise
