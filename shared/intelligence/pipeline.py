"""Per-call pipeline orchestration for `call.process` jobs.

Stages run in order with a checkpoint map persisted in
call_intelligence.stages, so a retried job resumes at the first stage that is
not 'done' — an LLM extraction is never paid for twice.

Stage set:
    extract   — LLM intelligence extraction (reads the customer digest)
    embed     — embeddings + FTS chunks
    followup  — follow-up decision engine
    memory    — customer memory merge (governed fact lifecycle)
    digest    — write-time precompute of the pre-call context digest
    timeline  — contact_events emit (unified contact timeline)
    automation— rule evaluation
    notify    — notifications
    whatsapp  — WhatsApp follow-up sends (shared/whatsapp/stage.py): only
                calls under an hour old, so the sweeper's backfill of this
                stage over history sends nothing

Idempotency contracts:
    * call_intelligence upserts ON CONFLICT (call_id)
    * commitments/action_items re-extraction only replaces rows the extractor
      itself wrote (source='post_call_llm' / 'ai'); in-call and manual rows
      are never touched
    * calls.analysis gets the legacy-shaped blob so every existing consumer
      (ConvAnalytics, per-call analysis endpoint, callback context builder)
      keeps working unmodified until it is migrated
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from shared.db.database import get_db
from shared.intelligence import config, extract

log = logging.getLogger("intel.pipeline")

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = REPO_ROOT / "calling-agent" / "agents"

STAGE_ORDER = ("extract", "embed", "followup", "memory", "digest", "timeline",
               "automation", "notify", "whatsapp")


def _load_call(call_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        return dict(row) if row else None


def _load_stages(call_id: int) -> dict[str, str]:
    with get_db() as db:
        row = db.execute(
            "SELECT stages FROM call_intelligence WHERE call_id=?", (call_id,)
        ).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["stages"] or "{}")
    except Exception:
        return {}


def _mark_stage(call_id: int, stage: str, status: str) -> None:
    stages = _load_stages(call_id)
    stages[stage] = status
    with get_db() as db:
        db.execute(
            "UPDATE call_intelligence SET stages=?, updated_at=now() WHERE call_id=?",
            (json.dumps(stages), call_id),
        )


def _agent_yaml(agent_slug: str | None) -> dict[str, Any] | None:
    if not agent_slug:
        return None
    path = AGENTS_DIR / agent_slug / "agent.yaml"
    try:
        import yaml

        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Stage 1: extract
# ---------------------------------------------------------------------------

_JSON_COLS = (
    "sentiment_journey", "topics", "pain_points", "objections", "buying_signals",
    "competitor_mentions", "product_interest", "budget_discussion",
    "timeline_discussion", "decision_makers", "compliance_issues", "escalations",
    "risk_indicators", "agent_performance", "key_moments", "recommendations",
    "entities", "tags",
)
_SCALAR_COLS = (
    "schema_version", "summary", "outcome_summary", "customer_intent",
    "conversation_quality", "conversion_probability", "satisfaction", "urgency",
    "confidence", "low_confidence", "lead_score", "opportunity_score",
    "risk_score", "compliance_score", "sentiment",
)


def _minimal_record(reason: str) -> dict[str, Any]:
    """Deterministic record for calls with no usable transcript — no LLM call."""
    return {
        "schema_version": extract.SCHEMA_VERSION,
        "summary": f"No analyzable conversation ({reason}).",
        "outcome_summary": reason,
        "customer_intent": None,
        "conversation_quality": None, "conversion_probability": 0.0,
        "satisfaction": None, "urgency": "low", "confidence": 0.0,
        "low_confidence": 1,
        "lead_score": 0, "opportunity_score": 0, "risk_score": 0,
        "compliance_score": None, "sentiment": "neutral",
        "sentiment_journey": [], "agent_performance": {}, "key_moments": [],
        "recommendations": {}, "entities": {},
        **{f: [] for f in extract._LIST_FIELDS},
        "commitments": [], "action_items": [],
        "follow_up": {"required": "no", "confidence": 1.0,
                      "reason": reason, "who_promised": None,
                      "expected_outcome": None, "type": None,
                      "priority": "low", "suggested_date": None,
                      "suggested_time": None},
    }


def store_extraction(call: dict[str, Any], rec: dict[str, Any], model: str) -> None:
    """Persist the validated record: call_intelligence upsert + child rows +
    legacy compat blob. Single transaction."""
    call_id = call["id"]
    owner = call.get("owner") or ""
    contact_id = call.get("contact_id")
    fu = rec.get("follow_up") or {}
    payload_cols: dict[str, Any] = {c: rec.get(c) for c in _SCALAR_COLS}
    for c in _JSON_COLS:
        payload_cols[c] = json.dumps(rec.get(c) or ([] if c != "agent_performance"
                                     and c != "recommendations" and c != "entities" else {}),
                                     ensure_ascii=False)

    stages = _load_stages(call_id)
    stages["extract"] = "done"
    # follow_up judgment rides inside recommendations? No — stored for the
    # followup stage via a dedicated key in stages-adjacent storage: keep it
    # in the record blob the followup stage re-reads from commitments +
    # calls.analysis. Simplest durable home: the legacy blob (below) plus
    # commitments rows; the followup stage also receives it via the job run
    # when in-process. To survive retries across processes it is embedded in
    # the stages map.
    stages["_follow_up_judgment"] = fu

    cols = list(payload_cols)
    with get_db() as db:
        db.execute(
            f"""INSERT INTO call_intelligence
                    (call_id, owner, contact_id, agent_slug, model, stages,
                     processed_at, {', '.join(cols)})
                VALUES (?, ?, ?, ?, ?, ?, now(), {', '.join('?' for _ in cols)})
                ON CONFLICT (call_id) DO UPDATE SET
                    owner=EXCLUDED.owner, contact_id=EXCLUDED.contact_id,
                    agent_slug=EXCLUDED.agent_slug, model=EXCLUDED.model,
                    stages=EXCLUDED.stages, processed_at=EXCLUDED.processed_at,
                    updated_at=now(),
                    {', '.join(f'{c}=EXCLUDED.{c}' for c in cols)}""",
            (call_id, owner, contact_id, call.get("agent_slug"), model,
             json.dumps(stages), *[payload_cols[c] for c in cols]),
        )

        # Replace only the rows this extractor owns (idempotent re-run).
        db.execute(
            "DELETE FROM commitments WHERE call_id=? AND source='post_call_llm'",
            (call_id,),
        )
        for c in rec.get("commitments", []):
            deadline_at = None
            if c.get("deadline_date"):
                deadline_at = f"{c['deadline_date']} {c.get('deadline_time') or '10:00'}:00+05:30"
            db.execute(
                """INSERT INTO commitments
                       (call_id, owner, contact_id, made_by, kind, description,
                        quote, deadline_text, deadline_at, is_vague, clarified,
                        source, confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'post_call_llm', ?)""",
                (call_id, owner, contact_id, c["made_by"], c["kind"],
                 c["description"], c.get("quote"), c.get("deadline_text"),
                 deadline_at, 1 if c.get("is_vague") else 0, c.get("confidence")),
            )

        db.execute(
            "DELETE FROM action_items WHERE call_id=? AND source='ai'", (call_id,)
        )
        for a in rec.get("action_items", []):
            due_at = f"{a['due_date']} 18:00:00+05:30" if a.get("due_date") else None
            db.execute(
                """INSERT INTO action_items
                       (call_id, owner, description, priority, due_at, source)
                   VALUES (?, ?, ?, ?, ?, 'ai')""",
                (call_id, owner, a["description"], a["priority"], due_at),
            )

        # Legacy compatibility: calls.analysis blob + summary overwrite
        # (matches the retired manual analyzer's observable behavior).
        blob = extract.legacy_blob(rec, model)
        db.execute(
            "UPDATE calls SET analysis=?, summary=? WHERE id=?",
            (json.dumps(blob, ensure_ascii=False), rec.get("summary"), call_id),
        )


async def stage_extract(call: dict[str, Any]) -> None:
    transcript = (call.get("transcript") or "").strip()
    # Fewer than 2 real turns → nothing to analyze; deterministic record.
    if len(transcript) < 40:
        store_extraction(call, _minimal_record("empty or too-short transcript"),
                         model="deterministic")
        return
    agent_ctx = extract.agent_context(_agent_yaml(call.get("agent_slug")))
    # Prior-relationship context: the analysis LLM reads the same customer
    # digest the live agent gets. Failure here must never cost an extraction.
    memory_digest = ""
    if call.get("contact_id"):
        try:
            from shared.intelligence import memory as _memory

            memory_digest = _memory.digest_for_contact(call["contact_id"])
        except Exception as exc:
            log.warning("extract: memory digest unavailable for contact %s: %r",
                        call.get("contact_id"), exc)
    prompt = extract.build_user_prompt(call, agent_ctx, memory_digest)
    raw = await extract.run_llm(prompt)
    try:
        rec = extract.parse_and_validate(raw, call)
    except ValueError:
        # One repair retry: re-ask with the error appended.
        raw = await extract.run_llm(
            prompt + "\n\nYour previous reply was not a single valid JSON object"
                     " matching the schema. Reply again with ONLY the JSON object.")
        rec = extract.parse_and_validate(raw, call)
    store_extraction(call, rec, model=config.LLM_MODEL)


# ---------------------------------------------------------------------------
# Job entry point
# ---------------------------------------------------------------------------

async def process_call(job: dict[str, Any]) -> None:
    call_id = job.get("call_id")
    if not call_id:
        raise ValueError("call.process job without call_id")
    call = _load_call(call_id)
    if not call:
        raise ValueError(f"call {call_id} not found")

    stages = _load_stages(call_id)
    for stage in STAGE_ORDER:
        if stages.get(stage) == "done":
            continue
        handler = _STAGE_HANDLERS.get(stage)
        if handler is None:  # later-phase stage not built yet: leave unmarked
            continue
        try:
            await handler(call)
            if stage != "extract":  # extract marks itself inside store_extraction
                _mark_stage(call_id, stage, "done")
        except Exception as exc:
            _mark_stage_safe(call_id, stage, f"error: {exc}")
            raise
        stages = _load_stages(call_id)


def _mark_stage_safe(call_id: int, stage: str, status: str) -> None:
    try:
        if _load_stages(call_id) or _exists(call_id):
            _mark_stage(call_id, stage, status[:300])
    except Exception:
        pass


def _exists(call_id: int) -> bool:
    with get_db() as db:
        return bool(db.execute(
            "SELECT 1 FROM call_intelligence WHERE call_id=?", (call_id,)
        ).fetchone())


def _lazy(module: str, attr: str):
    async def handler(call):
        import importlib

        mod = importlib.import_module(f"shared.intelligence.{module}")
        await getattr(mod, attr)(call)
    return handler


async def _whatsapp_stage(call):
    # Lives in shared/whatsapp, not shared/intelligence: the feature owns its
    # tables and its sender, the pipeline only decides WHEN it runs.
    from shared.whatsapp.stage import stage_whatsapp

    await stage_whatsapp(call)


_STAGE_HANDLERS = {
    "extract": stage_extract,
    "embed": _lazy("embed", "stage_embed"),
    "followup": _lazy("followup", "stage_followup"),
    "memory": _lazy("memory", "stage_memory"),
    # digest runs after memory so the precomputed pre-call context always
    # reflects this call; timeline after digest so its memory.updated event
    # sees the final version. Neither costs an LLM call — the sweeper's
    # historical backfill of these two stages is free.
    "digest": _lazy("memory", "stage_digest"),
    "timeline": _lazy("timeline", "stage_timeline"),
    "automation": _lazy("automation", "stage_automation"),
    "notify": _lazy("notify", "stage_notify"),
    "whatsapp": _whatsapp_stage,
}


# ---------------------------------------------------------------------------
# Sweeper: the guarantee that EVERY finished call gets processed, even if the
# teardown enqueue was lost — and the backfill path for historical calls.
# ---------------------------------------------------------------------------

def sweep_missing(limit: int = 25) -> int:
    """Enqueue call.process for (a) finished calls with no intelligence row at
    all, and (b) calls whose stages map lacks a 'done' for a registered stage
    — which is how newly shipped stages retrofit onto already-analyzed calls."""
    from shared.intelligence import queue

    if not queue.is_pg():
        return 0
    stage_checks = " OR ".join(
        f"COALESCE(ci.stages::jsonb->>'{s}', '') <> 'done'"
        for s in STAGE_ORDER if s in _STAGE_HANDLERS
    )
    with get_db() as db:
        rows = db.execute(
            f"""SELECT c.id, c.owner, COALESCE(c.conn_id, c.call_key) AS ck
                  FROM calls c
                  LEFT JOIN call_intelligence ci ON ci.call_id = c.id
                 WHERE c.created_at < now() - make_interval(secs => ?)
                   AND (ci.call_id IS NULL OR {stage_checks})
                 ORDER BY c.id DESC
                 LIMIT ?""",
            (config.SWEEP_MIN_CALL_AGE_S, limit),
        ).fetchall()
    n = 0
    for r in rows:
        job_id = queue.enqueue(queue.KIND_CALL_PROCESS, owner=r["owner"] or "",
                               call_id=r["id"],
                               dedupe_key=f"call.process:{r['ck'] or r['id']}")
        if job_id is None:
            # Dedupe hit. Only a job that finished CLEANLY but predates a newly
            # shipped stage gets re-driven; failed jobs stay in the DLQ for
            # manual re-drive (auto-requeue would burn LLM spend in a loop on
            # permanently broken calls) and pending/running jobs are left alone.
            with get_db() as db:
                job = db.execute(
                    "SELECT id, status FROM intel_jobs WHERE dedupe_key=?",
                    (f"call.process:{r['ck'] or r['id']}",),
                ).fetchone()
            if job and job["status"] == "done" and queue.requeue(job["id"]):
                n += 1
        else:
            n += 1
    return n
