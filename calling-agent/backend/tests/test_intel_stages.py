"""Pipeline stages 2-6: embedding chunker, follow-up decision engine,
customer-memory merge, notifications — pure logic + test-DB integration."""

from __future__ import annotations

import asyncio
import json

from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401
from tests.test_intel_extract import (  # noqa: F401
    TRANSCRIPT, _call_row, _llm_output, _insert_call,
)


# ---------------------------------------------------------------------------
# embed: chunker
# ---------------------------------------------------------------------------

def test_parse_turns_handles_platform_format():
    from shared.intelligence import embed

    turns = embed.parse_turns(TRANSCRIPT)
    assert len(turns) == 5
    assert turns[0][1] == "Agent"
    assert turns[1] == ("00:05.000", "Caller", "Haan, mujhe 3BHK flat ke baare mein jaanna tha.")


def test_chunk_turns_overlap_and_size():
    from shared.intelligence import embed

    turns = [(f"00:{i:02d}.000", "Agent" if i % 2 == 0 else "Caller", "word " * 40)
             for i in range(30)]
    chunks = embed.chunk_turns(turns, chunk_chars=800)
    assert len(chunks) > 1
    assert all(len(c) <= 1200 for c in chunks)
    # one-turn overlap: last line of chunk N appears in chunk N+1
    first_tail = chunks[0].splitlines()[-1]
    assert first_tail in chunks[1]


def test_vec_literal_and_normalize():
    from shared.intelligence.embed import _normalize, _vec_literal

    v = _normalize([3.0, 4.0])
    assert abs(sum(x * x for x in v) - 1.0) < 1e-9
    assert _vec_literal([0.5, -0.25]).startswith("[0.5000000,-0.2500000")


def test_stage_embed_stores_chunks_and_summary(pg_test_db, monkeypatch):
    from shared.db.database import get_db
    from shared.intelligence import config, embed, extract, pipeline

    call_id = _insert_call(transcript=TRANSCRIPT + "\nCaller: theek hai bhai.")
    call = pipeline._load_call(call_id)
    rec = extract.parse_and_validate(_llm_output(), call)
    pipeline.store_extraction(call, rec, model="test")

    async def fake_embed(texts):
        return [[0.1] * 768 for _ in texts]

    monkeypatch.setattr(config, "EMBED_ENABLED", True)
    monkeypatch.setattr(config, "EMBED_API_KEY", "test-key")
    monkeypatch.setattr(embed, "embed_texts", fake_embed)
    asyncio.run(embed.stage_embed(call))
    asyncio.run(embed.stage_embed(call))  # idempotent re-run
    with get_db() as db:
        rows = db.execute(
            "SELECT kind, count(*) AS n FROM call_embeddings WHERE call_id=?"
            " GROUP BY kind ORDER BY kind", (call_id,)
        ).fetchall()
    kinds = {r["kind"]: r["n"] for r in rows}
    assert kinds.get("summary") == 1
    assert kinds.get("chunk", 0) >= 1
    # FTS generated column populated
    with get_db() as db:
        hit = db.execute(
            "SELECT count(*) AS n FROM call_embeddings WHERE call_id=?"
            " AND tsv @@ websearch_to_tsquery('simple', ?)", (call_id, "3BHK")
        ).fetchone()["n"]
    assert hit >= 1


# ---------------------------------------------------------------------------
# followup: decision engine (pure)
# ---------------------------------------------------------------------------

def _judgment(**over):
    j = {"required": "yes", "confidence": 0.9, "reason": "customer asked",
         "who_promised": "customer_requested", "expected_outcome": "decision",
         "type": "callback", "priority": "high",
         "suggested_date": "2099-01-05", "suggested_time": "15:00"}
    j.update(over)
    return j


def test_decide_clarified_in_call_commitment_wins():
    from shared.intelligence import followup

    row = followup.decide(
        _call_row(),
        _judgment(required="no"),  # even a NO judgment is overridden
        [{"id": 5, "source": "in_call", "clarified": 1, "made_by": "customer",
          "kind": "callback", "description": "call Tuesday 3pm",
          "deadline_at": "2099-01-06 15:00:00+05:30"}],
    )
    assert row["decision"] == "yes"
    assert row["source"] == "in_call"
    assert row["scheduled_at"] == "2099-01-06 15:00:00+05:30"
    assert row["commitment_id"] == 5


def test_decide_callback_flag_forces_yes():
    from shared.intelligence import followup

    call = _call_row(collected_data=json.dumps(
        {"flags": {"callback_requested": True, "callback_time": "evening"}}))
    row = followup.decide(call, _judgment(required="unsure", suggested_date=None,
                                          suggested_time=None), [])
    assert row["decision"] == "yes"
    assert row["followup_type"] == "callback"
    assert row["scheduled_at"] is not None
    assert row["auto_scheduled"] == 1


def test_decide_yes_uses_suggested_datetime():
    from shared.intelligence import followup

    call = _call_row(collected_data="{}")
    row = followup.decide(call, _judgment(), [])
    assert row["decision"] == "yes"
    assert row["scheduled_at"] == "2099-01-05 15:00:00+05:30"
    assert row["auto_scheduled"] == 0


def test_decide_relative_offset_schedules_from_call_end():
    """'Call me in 10 minutes' → scheduled 10 minutes after the call ENDED,
    never coerced to a next-day calendar guess."""
    from shared.intelligence import followup

    call = _call_row(
        collected_data=json.dumps({"flags": {"callback_requested": True}}),
        started_at="2026-07-18T10:00:00Z", duration_s=120.0)
    row = followup.decide(call, _judgment(required="unsure", suggested_date=None,
                                          suggested_time=None,
                                          suggested_offset_minutes=10), [])
    assert row["decision"] == "yes"
    # anchor 10:00 IST + 2 min call + 10 min offset = 10:12 IST
    assert row["scheduled_at"].startswith("2026-07-18T10:12")
    assert row["auto_scheduled"] == 0  # customer-stated → no grace, courtesy window


def test_decide_no_is_archived_with_explanation():
    from shared.intelligence import followup

    call = _call_row(collected_data="{}")
    row = followup.decide(call, _judgment(required="no",
                                          reason="wrong number, nothing pending"), [])
    assert row["decision"] == "no"
    assert row["status"] == "archived"
    assert "wrong number" in row["explanation"]


def test_decide_unsure_needs_review():
    from shared.intelligence import followup

    call = _call_row(collected_data="{}")
    row = followup.decide(call, _judgment(required="unsure", confidence=0.4), [])
    assert row["decision"] == "unsure"
    assert row["status"] == "pending"
    assert row["scheduled_at"] is None


def test_stage_followup_persists_and_is_idempotent(pg_test_db):
    from shared.db.database import get_db
    from shared.intelligence import extract, followup, pipeline

    call_id = _insert_call(transcript=TRANSCRIPT + "\nAgent: shukriya, bye.")
    call = pipeline._load_call(call_id)
    rec = extract.parse_and_validate(_llm_output(), call)
    pipeline.store_extraction(call, rec, model="test")

    asyncio.run(followup.stage_followup(call))
    asyncio.run(followup.stage_followup(call))  # idempotent
    with get_db() as db:
        rows = db.execute(
            "SELECT decision, status, scheduled_at FROM followups WHERE call_id=?",
            (call_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["decision"] == "yes"
    assert rows[0]["status"] == "scheduled"


def test_sweep_overdue_marks_missed(pg_test_db):
    from shared.db.database import get_db
    from shared.intelligence import followup

    # Assert on OUR row, never on the global count: earlier tests in this
    # session leave legitimately-scheduled rows whose times age past sweep's
    # 1-hour grace as the wall clock advances (a "== 1" here went red at
    # exactly 12:30 IST on 2026-07-24 while passing all morning).
    with get_db() as db:
        row_id = db.execute(
            """INSERT INTO followups (owner, decision, status, scheduled_at, source)
               VALUES ('t@x.com', 'yes', 'scheduled',
                       now() - interval '3 hours', 'pipeline')
               RETURNING id""",
        ).fetchone()["id"]
    assert followup.sweep_overdue() >= 1
    with get_db() as db:
        status = db.execute(
            "SELECT status FROM followups WHERE id=?", (row_id,)
        ).fetchone()["status"]
    assert status == "missed"


# ---------------------------------------------------------------------------
# memory: merge (pure) + digest
# ---------------------------------------------------------------------------

def test_memory_merge_dedupes_and_keeps_provenance():
    from shared.intelligence import memory

    ci = {"summary": "Asked about 3BHK.", "customer_intent": "3BHK inquiry",
          "sentiment": "positive", "risk_score": 20,
          "product_interest": json.dumps(["Pramukh Greens 3BHK"]),
          "objections": json.dumps(["price too high"]),
          "decision_makers": json.dumps([])}
    call = {"id": 11, "started_at": "2026-07-18T10:00:00Z"}
    mem = memory.merge_call({}, call, ci, [{"made_by": "agent", "description": "callback"}], "yes")
    mem2 = memory.merge_call(mem, call, ci, [{"made_by": "agent", "description": "callback"}], "yes")
    assert len(mem2["preferences"]) == 1  # deduped
    assert mem2["preferences"][0]["call_id"] == 11
    assert mem2["risk_level"] == "low"
    assert mem2["last_intent"] == "3BHK inquiry"


def test_stage_memory_and_digest(pg_test_db):
    from shared.db.database import get_db
    from shared.intelligence import extract, memory, pipeline

    call_id = _insert_call(transcript=TRANSCRIPT + "\nCaller: accha thik hai.")
    call = pipeline._load_call(call_id)
    rec = extract.parse_and_validate(_llm_output(), call)
    pipeline.store_extraction(call, rec, model="test")

    asyncio.run(memory.stage_memory(call))
    asyncio.run(memory.stage_memory(call))
    with get_db() as db:
        row = db.execute(
            "SELECT calls_count, memory FROM customer_memory WHERE contact_id=7"
        ).fetchone()
    assert row["calls_count"] == 2  # count increments per run (per-call in prod)
    digest = memory.digest_for_contact(7)
    assert "Interested in" in digest
    assert len(digest) <= 600


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------

def test_notify_hot_lead_and_idempotency(pg_test_db):
    from shared.db.database import get_db
    from shared.intelligence import extract, notify, pipeline
    from shared.intelligence import followup as fu_mod

    call_id = _insert_call(transcript=TRANSCRIPT + "\nAgent: namaste ji.")
    call = pipeline._load_call(call_id)
    rec = extract.parse_and_validate(_llm_output(), call)
    pipeline.store_extraction(call, rec, model="test")
    asyncio.run(fu_mod.stage_followup(call))

    asyncio.run(notify.stage_notify(call))
    asyncio.run(notify.stage_notify(call))  # no duplicates
    with get_db() as db:
        rows = db.execute(
            "SELECT kind FROM notifications WHERE ref LIKE ?",
            (f"call:{call_id}:%",),
        ).fetchall()
    kinds = sorted(r["kind"] for r in rows)
    assert "lead.hot" in kinds and "followup.created" in kinds
    assert len(kinds) == len(set(kinds))


def test_notify_skips_stale_backfill_calls(pg_test_db):
    from shared.db.database import get_db
    from shared.intelligence import extract, notify, pipeline

    call_id = _insert_call(transcript=TRANSCRIPT + "\nCaller: haan bhai.")
    with get_db() as db:
        db.execute(
            "UPDATE calls SET created_at = now() - interval '2 days' WHERE id=?",
            (call_id,),
        )
    call = pipeline._load_call(call_id)
    rec = extract.parse_and_validate(_llm_output(), call)
    pipeline.store_extraction(call, rec, model="test")
    asyncio.run(notify.stage_notify(call))
    with get_db() as db:
        n = db.execute(
            "SELECT count(*) AS n FROM notifications WHERE ref LIKE ?",
            (f"call:{call_id}:%",),
        ).fetchone()["n"]
    assert n == 0


# ---------------------------------------------------------------------------
# full pipeline: all stages end-to-end with mocked LLM + embeddings
# ---------------------------------------------------------------------------

def test_process_call_runs_all_stages(pg_test_db, monkeypatch):
    from shared.db.database import get_db
    from shared.intelligence import config, embed, extract, pipeline

    call_id = _insert_call(transcript=TRANSCRIPT + "\nAgent: alvida.")

    async def fake_llm(prompt):
        return _llm_output()

    async def fake_embed(texts):
        return [[0.05] * 768 for _ in texts]

    monkeypatch.setattr(config, "EMBED_ENABLED", True)
    monkeypatch.setattr(config, "EMBED_API_KEY", "test-key")
    monkeypatch.setattr(extract, "run_llm", fake_llm)
    monkeypatch.setattr(embed, "embed_texts", fake_embed)
    asyncio.run(pipeline.process_call({"call_id": call_id}))

    with get_db() as db:
        stages = json.loads(db.execute(
            "SELECT stages FROM call_intelligence WHERE call_id=?", (call_id,)
        ).fetchone()["stages"])
    for stage in ("extract", "embed", "followup", "memory", "digest",
                  "timeline", "notify"):
        assert stages.get(stage) == "done", f"stage {stage}: {stages}"
    with get_db() as db:  # embed really stored rows (not a vacuous skip)
        n = db.execute(
            "SELECT count(*) AS n FROM call_embeddings WHERE call_id=?", (call_id,)
        ).fetchone()["n"]
    assert n >= 2
    with get_db() as db:  # digest precomputed + timeline landed
        digest = db.execute(
            "SELECT context_digest FROM customer_memory WHERE contact_id=7"
        ).fetchone()["context_digest"]
        events = db.execute(
            "SELECT count(*) AS n FROM contact_events WHERE ref_table='calls'"
            " AND ref_id=?", (call_id,)).fetchone()["n"]
    assert digest and "previous call" in digest
    assert events >= 2  # call.* + call.analyzed (+ memory.updated)