"""Extraction stage: parsing/validation guards + storage idempotency.

LLM output is mocked throughout; the storage tests reuse the lescall_test
database fixture from test_intel_queue.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401  (fixtures)


TRANSCRIPT = """[00:01.000]
Agent: Namaste, main Vishal bol raha hoon Pramukh Group se.

[00:05.000]
Caller: Haan, mujhe 3BHK flat ke baare mein jaanna tha.

[00:12.000]
Agent: Bilkul. Hamare paas Pramukh Greens mein 3BHK available hai.

[00:20.000]
Caller: Price kya hai? Main next week decide karunga, aap mujhe call kar lena.

[00:30.000]
Agent: Zaroor, main aapko call kar lunga. Details WhatsApp par bhej deta hoon.
"""


def _call_row(**over):
    row = {
        "id": 1, "owner": "t@x.com", "contact_id": 7, "agent_slug": "pramukh_agent",
        "direction": "inbound", "duration_s": 95.0, "language": "hi-IN",
        "started_at": "2026-07-18T10:00:00Z", "outcome": "callback_requested",
        "collected_data": json.dumps({"flags": {"callback_requested": True}}),
        "transcript": TRANSCRIPT,
    }
    row.update(over)
    return row


def _llm_output(**over):
    data = {
        "summary": "Customer inquired about a 3BHK flat in Pramukh Greens and asked for pricing.",
        "outcome_summary": "Agent promised a callback and WhatsApp details.",
        "customer_intent": "3BHK purchase inquiry",
        "conversation_quality": 0.8, "conversion_probability": 0.6, "satisfaction": 0.7,
        "urgency": "medium", "confidence": 0.85,
        "lead_score": 72, "opportunity_score": 65, "risk_score": 20, "compliance_score": 100,
        "sentiment": "positive",
        "sentiment_journey": [{"phase": "opening", "sentiment": "neutral", "note": "greeting"}],
        "topics": ["3BHK flat", "pricing"], "pain_points": [], "objections": [],
        "buying_signals": ["asked for price"], "competitor_mentions": [],
        "product_interest": ["Pramukh Greens 3BHK"], "budget_discussion": [],
        "timeline_discussion": ["decision next week"], "decision_makers": [],
        "compliance_issues": [], "escalations": [], "risk_indicators": [],
        "agent_performance": {"score": 80, "strengths": ["polite"], "improvements": []},
        "key_moments": [{"label": "Price asked", "detail": "Caller asked for pricing"}],
        "recommendations": {"next_actions": ["Send pricing on WhatsApp"], "emails": [],
                            "documents": ["brochure"], "crm_updates": []},
        "entities": {"names": [], "orgs": ["Pramukh Group"], "amounts": [],
                     "dates": [], "places": []},
        "tags": ["3bhk", "pricing", "hot-lead"],
        "commitments": [
            {"made_by": "agent", "kind": "callback",
             "description": "Agent will call back when customer decides",
             "quote": "main aapko call kar lunga",
             "deadline_text": "next week", "deadline_date": None,
             "deadline_time": None, "is_vague": True, "confidence": 0.9},
            {"made_by": "agent", "kind": "whatsapp",
             "description": "Send details on WhatsApp",
             "quote": "Details WhatsApp par bhej deta hoon",
             "deadline_text": None, "deadline_date": "2026-07-18",
             "deadline_time": None, "is_vague": False, "confidence": 0.95},
        ],
        "action_items": [{"description": "Send 3BHK pricing on WhatsApp",
                          "priority": "high", "due_date": "2026-07-19"}],
        "follow_up": {"required": "yes", "confidence": 0.9,
                      "reason": "Customer asked to be called next week to decide",
                      "who_promised": "customer_requested",
                      "expected_outcome": "purchase decision", "type": "callback",
                      "priority": "high", "suggested_date": "2026-07-24",
                      "suggested_time": "11:00"},
    }
    data.update(over)
    return json.dumps(data)


# ---------------------------------------------------------------------------
# Pure validation tests (no DB)
# ---------------------------------------------------------------------------

def test_parse_valid_output():
    from shared.intelligence import extract

    rec = extract.parse_and_validate(_llm_output(), _call_row())
    assert rec["lead_score"] == 72
    assert rec["sentiment"] == "positive"
    assert len(rec["commitments"]) == 2
    assert rec["commitments"][0]["is_vague"] is True
    assert rec["follow_up"]["required"] == "yes"


def test_parse_strips_markdown_fences():
    from shared.intelligence import extract

    fenced = "```json\n" + _llm_output() + "\n```"
    rec = extract.parse_and_validate(fenced, _call_row())
    assert rec["summary"].startswith("Customer inquired")


def test_hallucinated_quote_drops_commitment():
    from shared.intelligence import extract

    out = _llm_output(commitments=[{
        "made_by": "customer", "kind": "payment",
        "description": "Customer will pay advance tomorrow",
        "quote": "I will transfer the advance tomorrow morning",  # not in transcript
        "deadline_text": "tomorrow", "deadline_date": "2026-07-19",
        "deadline_time": None, "is_vague": False, "confidence": 0.9}])
    rec = extract.parse_and_validate(out, _call_row())
    assert rec["commitments"] == []


def test_past_dates_are_nulled():
    from shared.intelligence import extract

    out = _llm_output(follow_up={"required": "yes", "confidence": 0.8,
                                 "reason": "callback", "who_promised": "agent_promised",
                                 "expected_outcome": None, "type": "callback",
                                 "priority": "medium",
                                 "suggested_date": "2023-10-30",  # the legacy bug
                                 "suggested_time": "11:00"})
    rec = extract.parse_and_validate(out, _call_row())
    assert rec["follow_up"]["suggested_date"] is None


def test_bad_enums_and_scores_are_coerced():
    from shared.intelligence import extract

    out = _llm_output(sentiment="ecstatic", urgency="mega", lead_score=250,
                      confidence="not-a-number")
    rec = extract.parse_and_validate(out, _call_row())
    assert rec["sentiment"] == "neutral"
    assert rec["urgency"] == "low"
    assert rec["lead_score"] == 100
    assert rec["confidence"] is None
    assert rec["low_confidence"] == 1


def test_missing_summary_raises():
    from shared.intelligence import extract

    with pytest.raises(ValueError):
        extract.parse_and_validate(json.dumps({"lead_score": 5}), _call_row())


def test_middle_out_truncation():
    from shared.intelligence import extract

    text = "A" * 600 + "MIDDLE" + "B" * 600
    out = extract.middle_out(text, 400)
    assert len(out) < 500
    assert out.startswith("A") and out.endswith("B")
    assert "MIDDLE" not in out


def test_legacy_blob_shape():
    from shared.intelligence import extract

    rec = extract.parse_and_validate(_llm_output(), _call_row())
    blob = extract.legacy_blob(rec, "gemini-2.5-flash")
    assert blob["lead_quality"] == "hot"
    assert blob["follow_up_needed"] is True
    assert blob["sentiment"] == "positive"
    assert blob["_model"] == "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# Storage integration (lescall_test DB)
# ---------------------------------------------------------------------------

_call_seq = iter(range(1, 10_000))


def _insert_call(transcript=TRANSCRIPT) -> int:
    from shared.db.database import get_db

    n = next(_call_seq)
    with get_db() as db:
        row = db.execute(
            """INSERT INTO calls (conn_id, call_key, direction, owner, agent_slug,
                                  transcript, started_at, duration_s, contact_id)
               VALUES (?, ?, 'inbound', 't@x.com', 'pramukh_agent', ?,
                       '2026-07-18T10:00:00Z', 95, 7)
               RETURNING id""",
            (f"conn-{n}", f"9198765{n:05d}_20260718_100000", transcript),
        ).fetchone()
        return row["id"]


def test_store_extraction_and_rerun_idempotency(pg_test_db):
    from shared.db.database import get_db
    from shared.intelligence import extract, pipeline

    call_id = _insert_call()
    call = pipeline._load_call(call_id)
    rec = extract.parse_and_validate(_llm_output(), call)

    pipeline.store_extraction(call, rec, model="test-model")
    pipeline.store_extraction(call, rec, model="test-model")  # idempotent re-run

    with get_db() as db:
        ci = db.execute(
            "SELECT * FROM call_intelligence WHERE call_id=?", (call_id,)
        ).fetchone()
        assert ci["lead_score"] == 72
        assert json.loads(ci["stages"])["extract"] == "done"
        n_commit = db.execute(
            "SELECT count(*) AS n FROM commitments WHERE call_id=?", (call_id,)
        ).fetchone()["n"]
        assert n_commit == 2  # replaced, not duplicated
        n_items = db.execute(
            "SELECT count(*) AS n FROM action_items WHERE call_id=?", (call_id,)
        ).fetchone()["n"]
        assert n_items == 1
        calls_row = db.execute(
            "SELECT analysis, summary FROM calls WHERE id=?", (call_id,)
        ).fetchone()
        blob = json.loads(calls_row["analysis"])
        assert blob["_pipeline"] == "intel_v1"
        assert calls_row["summary"].startswith("Customer inquired")


def test_store_preserves_in_call_commitments(pg_test_db):
    from shared.db.database import get_db
    from shared.intelligence import extract, pipeline

    call_id = _insert_call(transcript=TRANSCRIPT + "\nCaller: theek hai.")
    with get_db() as db:
        db.execute(
            """INSERT INTO commitments (call_id, owner, made_by, kind, description,
                                        source, clarified)
               VALUES (?, 't@x.com', 'agent', 'callback', 'clarified in-call',
                       'in_call', 1)""",
            (call_id,),
        )
    call = pipeline._load_call(call_id)
    rec = extract.parse_and_validate(_llm_output(), call)
    pipeline.store_extraction(call, rec, model="test-model")
    with get_db() as db:
        sources = [r["source"] for r in db.execute(
            "SELECT source FROM commitments WHERE call_id=? ORDER BY id", (call_id,)
        ).fetchall()]
    assert "in_call" in sources  # extractor never touches in-call rows
    assert sources.count("post_call_llm") == 2


def test_process_call_with_mocked_llm(pg_test_db, monkeypatch):
    from shared.db.database import get_db
    from shared.intelligence import extract, pipeline

    call_id = _insert_call(transcript=TRANSCRIPT + "\nAgent: dhanyavaad.")

    async def fake_llm(prompt):
        assert "Today's date/time (IST)" in prompt  # date anchor present
        assert "TRANSCRIPT" in prompt
        return _llm_output()

    monkeypatch.setattr(extract, "run_llm", fake_llm)
    asyncio.run(pipeline.process_call({"call_id": call_id}))
    with get_db() as db:
        ci = db.execute(
            "SELECT stages FROM call_intelligence WHERE call_id=?", (call_id,)
        ).fetchone()
    assert json.loads(ci["stages"])["extract"] == "done"


def test_empty_transcript_gets_minimal_record_without_llm(pg_test_db, monkeypatch):
    from shared.db.database import get_db
    from shared.intelligence import extract, pipeline

    call_id = _insert_call(transcript="")

    async def explode(prompt):  # LLM must NOT be called
        raise AssertionError("LLM called for empty transcript")

    monkeypatch.setattr(extract, "run_llm", explode)
    asyncio.run(pipeline.process_call({"call_id": call_id}))
    with get_db() as db:
        ci = db.execute(
            "SELECT confidence, model FROM call_intelligence WHERE call_id=?",
            (call_id,),
        ).fetchone()
    assert ci["model"] == "deterministic"


def test_sweeper_enqueues_missing_calls(pg_test_db):
    from shared.db.database import get_db
    from shared.intelligence import pipeline, queue

    call_id = _insert_call(transcript=TRANSCRIPT + "\nCaller: ok bye.")
    with get_db() as db:  # age the call past SWEEP_MIN_CALL_AGE_S
        db.execute(
            "UPDATE calls SET created_at = now() - interval '10 minutes' WHERE id=?",
            (call_id,),
        )
        db.execute("DELETE FROM intel_jobs")
    n = pipeline.sweep_missing()
    assert n >= 1
    job = queue.claim("sweep-test", kinds=(queue.KIND_CALL_PROCESS,))
    assert job is not None
    # second sweep must not duplicate (dedupe_key)
    assert pipeline.sweep_missing() == 0 or queue.claim("sweep-test") is None
