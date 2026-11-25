"""Contact timeline stage + failed-dial outcome classification.

stage_timeline: every analyzed call lands its events on contact_events,
idempotently (uq_contact_events_ref). dial_outcomes: CDR rows with no
answer_stamp are matched to call_links dials and classified into the
outcome taxonomy with retry hints — pure join logic + DB integration.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401  (fixtures)
from tests.test_intel_extract import (  # noqa: F401
    TRANSCRIPT, _insert_call, _llm_output,
)


def _analyzed_call(transcript_suffix: str):
    from shared.intelligence import extract, pipeline

    call_id = _insert_call(transcript=TRANSCRIPT + transcript_suffix)
    call = pipeline._load_call(call_id)
    rec = extract.parse_and_validate(_llm_output(), call)
    pipeline.store_extraction(call, rec, model="test")
    return call


# ---------------------------------------------------------------------------
# stage_timeline
# ---------------------------------------------------------------------------

def test_stage_timeline_emits_call_events(pg_test_db):
    from shared.db.database import get_db
    from shared.intelligence import memory, timeline

    call = _analyzed_call("\nCaller: haan bilkul.")
    asyncio.run(memory.stage_memory(call))
    asyncio.run(timeline.stage_timeline(call))
    asyncio.run(timeline.stage_timeline(call))  # idempotent re-run

    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT kind, ref_table, ref_id, payload FROM contact_events"
            " WHERE contact_id=7 AND ref_id=? AND ref_table='calls'",
            (call["id"],)).fetchall()]
    kinds = sorted(r["kind"] for r in rows)
    assert kinds == ["call.analyzed", "call.inbound", "memory.updated"]
    analyzed = next(r for r in rows if r["kind"] == "call.analyzed")
    assert "lead_score" in json.loads(analyzed["payload"])

    with get_db() as db:
        n_commit = db.execute(
            "SELECT count(*) AS n FROM contact_events WHERE kind='commitment.made'"
            " AND contact_id=7 AND ref_table='commitments'").fetchone()["n"]
    assert n_commit >= 1  # _llm_output carries a commitment


def test_stage_timeline_skips_contactless_calls(pg_test_db):
    from shared.db.database import get_db
    from shared.intelligence import timeline

    call = _analyzed_call("\nCaller: thik hai.")
    call = {**call, "contact_id": None}
    asyncio.run(timeline.stage_timeline(call))
    with get_db() as db:
        n = db.execute(
            "SELECT count(*) AS n FROM contact_events WHERE ref_table='calls'"
            " AND ref_id=?", (call["id"],)).fetchone()["n"]
    assert n == 0


# ---------------------------------------------------------------------------
# dial_outcomes: pure classification + join
# ---------------------------------------------------------------------------

def test_classify_cause_taxonomy():
    from shared.intelligence import dial_outcomes as do

    assert do.classify_cause("USER_BUSY") == "busy"
    assert do.classify_cause("NO_ANSWER") == "no_answer"
    assert do.classify_cause("CALL_REJECTED") == "rejected"
    assert do.classify_cause("UNALLOCATED_NUMBER") == "invalid_number"
    assert do.classify_cause("NORMAL_TEMPORARY_FAILURE") == "network_failure"
    assert do.classify_cause("SOMETHING_ELSE") == "other_failed"


def test_match_dials_joins_on_sig_and_window():
    from shared.intelligence import dial_outcomes as do

    dials = [{"id": 1, "owner": "t@x.com", "phone": "+919876543210",
              "sig_digits": "9876543210", "dialed_at": "2026-07-20T10:00:00Z"},
             {"id": 2, "owner": "t@x.com", "phone": "+919876500000",
              "sig_digits": "9876500000", "dialed_at": "2026-07-20T10:00:00Z"}]
    cdr = [{"dest": "09876543210", "start": "2026-07-20 10:00:20",
            "cause": "USER_BUSY", "uuid": "u1"},
           # outside the window — no match
           {"dest": "9876500000", "start": "2026-07-20 11:30:00",
            "cause": "NO_ANSWER", "uuid": "u2"}]
    matches = do.match_dials(dials, cdr)
    assert len(matches) == 1
    assert matches[0]["id"] == 1 and matches[0]["outcome"] == "busy"


def test_classify_recent_emits_missed_event(pg_test_db, tmp_path):
    from shared.db.database import get_db
    from shared.intelligence import dial_outcomes as do

    now = dt.datetime.utcnow().replace(microsecond=0)
    dialed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    start = (now + dt.timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as db:
        contact = db.execute(
            """INSERT INTO contacts (owner, phone, sig_digits, name)
               VALUES ('t@x.com', '+919812345678', '9812345678', 'Ramesh')
               ON CONFLICT (owner, phone) DO UPDATE SET name=EXCLUDED.name
               RETURNING id""").fetchone()
        link = db.execute(
            """INSERT INTO call_links (owner, phone, sig_digits, contact_id,
                                       name, agent_slug, dialed_at)
               VALUES ('t@x.com', '+919812345678', '9812345678', ?, 'Ramesh',
                       'a1', ?) RETURNING id""",
            (str(contact["id"]), dialed_at)).fetchone()

    # 15-column CDR line, answer_stamp empty (never connected).
    cdr_file = tmp_path / "Master.csv"
    cdr_file.write_text(
        f'"","1000","09812345678","default","{start}","","{start}",'
        f'"30","0","USER_BUSY","uuid-busy-1","","","PCMU","PCMU"\n')

    written = do.classify_recent(cdr_path=str(cdr_file))
    assert written == 1
    written_again = do.classify_recent(cdr_path=str(cdr_file))  # idempotent
    assert written_again == 0

    with get_db() as db:
        row = db.execute(
            "SELECT kind, payload FROM contact_events WHERE ref_table='call_links'"
            " AND ref_id=?", (link["id"],)).fetchone()
    assert row["kind"] == "call.missed"
    payload = json.loads(row["payload"])
    assert payload["outcome"] == "busy"
    assert payload["retry"]["retry_after_min"] == 30
