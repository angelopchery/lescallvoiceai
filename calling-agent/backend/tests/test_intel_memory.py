"""Customer memory: the extraction-loop digest wire + lifecycle governance.

P0 — stage_extract feeds digest_for_contact() into the analysis prompt (the
digest was previously hardwired to ""); a digest failure never costs the
extraction. Lifecycle tests (dedup-by-meaning, supersession, expiry) live
here too, mirroring the mocked-LLM style of the other intel test files.
"""

from __future__ import annotations

import asyncio
import json

from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401  (fixtures)
from tests.test_intel_extract import (  # noqa: F401
    TRANSCRIPT, _insert_call, _llm_output,
)


def _seed_memory(contact_id: int = 7, owner: str = "t@x.com") -> None:
    from shared.db.database import get_db

    mem = {
        "preferences": [{"text": "3BHK in Skyline", "call_id": 1, "date": "2026-07-10"}],
        "last_intent": "site visit",
    }
    with get_db() as db:
        db.execute(
            """INSERT INTO customer_memory (contact_id, owner, memory, calls_count, last_call_at)
               VALUES (?, ?, ?, 2, '2026-07-10T10:00:00Z')
               ON CONFLICT (contact_id) DO UPDATE SET memory=EXCLUDED.memory""",
            (contact_id, owner, json.dumps(mem)),
        )


# ---------------------------------------------------------------------------
# P0: extraction reads the customer digest
# ---------------------------------------------------------------------------

def test_stage_extract_injects_memory_digest(pg_test_db, monkeypatch):
    from shared.intelligence import extract, pipeline

    _seed_memory()
    call_id = _insert_call(transcript=TRANSCRIPT + "\nCaller: theek hai bhai.")
    call = pipeline._load_call(call_id)

    seen: dict = {}

    async def fake_llm(prompt):
        seen["prompt"] = prompt
        return _llm_output()

    monkeypatch.setattr(extract, "run_llm", fake_llm)
    asyncio.run(pipeline.stage_extract(call))

    assert "Known customer history" in seen["prompt"]
    assert "3BHK in Skyline" in seen["prompt"]


def test_stage_extract_survives_digest_failure(pg_test_db, monkeypatch):
    from shared.intelligence import extract, memory, pipeline

    call_id = _insert_call(transcript=TRANSCRIPT + "\nCaller: haan theek hai.")
    call = pipeline._load_call(call_id)

    def boom(contact_id, limit_chars=600):
        raise RuntimeError("digest down")

    async def fake_llm(prompt):
        return _llm_output()

    monkeypatch.setattr(memory, "digest_for_contact", boom)
    monkeypatch.setattr(extract, "run_llm", fake_llm)
    asyncio.run(pipeline.stage_extract(call))  # must not raise


# ---------------------------------------------------------------------------
# P2: governed fact lifecycle (pure merge logic)
# ---------------------------------------------------------------------------

def _ci(**over):
    base = {"summary": "Asked about 3BHK.", "customer_intent": "3BHK inquiry",
            "sentiment": "positive", "risk_score": 20,
            "product_interest": json.dumps(["Pramukh Greens 3BHK"]),
            "objections": json.dumps([]), "decision_makers": json.dumps([]),
            "entities": json.dumps({})}
    base.update(over)
    return base


def test_lifecycle_rephrase_updates_instead_of_appending():
    from shared.intelligence import memory

    call1 = {"id": 1, "started_at": "2026-07-01T10:00:00Z"}
    call2 = {"id": 2, "started_at": "2026-07-10T10:00:00Z"}
    mem = memory.merge_call(
        {}, call1, _ci(product_interest=json.dumps(["budget around 85L"])), [], None)
    mem = memory.merge_call(
        mem, call2, _ci(product_interest=json.dumps(["budget 85 lakh"])), [], None)
    prefs = mem["preferences"]
    assert len(prefs) == 1                      # same meaning — updated, not appended
    assert prefs[0]["text"] == "budget 85 lakh"  # newest phrasing wins
    assert prefs[0]["call_id"] == 2
    assert prefs[0]["confidence"] > 0.6          # repetition raises confidence
    assert prefs[0]["status"] == "active"


def test_lifecycle_conflict_supersedes_not_deletes():
    from shared.intelligence import memory

    call1 = {"id": 1, "started_at": "2026-07-01T10:00:00Z"}
    call2 = {"id": 2, "started_at": "2026-07-10T10:00:00Z"}
    mem = memory.merge_call(
        {}, call1, _ci(product_interest=json.dumps(["budget 85 lakh"])), [], None)
    mem = memory.merge_call(
        mem, call2, _ci(product_interest=json.dumps(["budget 1.2 crore"])), [], None)
    by_status = {f["status"]: f for f in mem["preferences"]}
    assert by_status["superseded"]["text"] == "budget 85 lakh"   # flipped, kept
    assert by_status["active"]["text"] == "budget 1.2 crore"
    # digest shows only the active fact
    digest = memory.render_digest(mem, 2)
    assert "1.2 crore" in digest and "85 lakh" not in digest


def test_lifecycle_time_sensitive_facts_expire():
    from shared.intelligence import memory

    old_call = {"id": 1, "started_at": "2026-01-05T10:00:00Z"}
    mem = memory.merge_call({}, old_call, _ci(),
                            [{"made_by": "agent", "description": "send brochure"}],
                            None)
    # 6 months later: the open promise (TTL 45d) expires; the stable
    # preference (TTL 365d) survives.
    new_call = {"id": 2, "started_at": "2026-07-10T10:00:00Z"}
    mem = memory.merge_call(mem, new_call, _ci(product_interest=json.dumps([])),
                            [], None)
    assert mem["promises"][0]["status"] == "expired"
    assert mem["preferences"][0]["status"] == "active"
    assert "send brochure" not in memory.render_digest(mem, 2)


def test_lifecycle_preferred_language_and_call_time():
    from shared.intelligence import memory

    mem = {}
    for i, start in enumerate(
            ("2026-07-01T13:30:00Z", "2026-07-02T14:00:00Z"), start=1):
        # 13:30 UTC = 19:00 IST → evening
        mem = memory.merge_call(mem, {"id": i, "started_at": start,
                                      "language": "gu"}, _ci(), [], None)
    assert mem["preferred_language"] == "gu"
    assert mem["preferred_call_time"] == "evening"
    digest = memory.render_digest(mem, 2)
    assert "Gujarati" in digest and "evening" in digest


def test_legacy_entries_upgrade_cleanly():
    from shared.intelligence import memory

    legacy = {"preferences": [{"text": "3BHK in Skyline", "call_id": 1,
                               "date": "2026-07-01"}]}
    call = {"id": 2, "started_at": "2026-07-10T10:00:00Z"}
    mem = memory.merge_call(legacy, call,
                            _ci(product_interest=json.dumps(["2BHK budget option"])),
                            [], None)
    texts = {f["text"] for f in mem["preferences"]}
    assert "3BHK in Skyline" in texts and "2BHK budget option" in texts
    assert all(f.get("status") for f in mem["preferences"])


def test_stage_digest_precomputes_context(pg_test_db):
    from shared.db.database import get_db
    from shared.intelligence import memory

    _seed_memory()
    call = {"id": 999999, "contact_id": 7, "owner": "t@x.com",
            "started_at": "2026-07-18T10:00:00Z"}
    asyncio.run(memory.stage_digest(call))
    with get_db() as db:
        row = db.execute(
            "SELECT context_digest, digest_updated_at FROM customer_memory"
            " WHERE contact_id=7").fetchone()
    assert row["context_digest"] and "3BHK in Skyline" in row["context_digest"]
    assert row["digest_updated_at"]


def test_stage_extract_no_contact_skips_digest(pg_test_db, monkeypatch):
    from shared.db.database import get_db
    from shared.intelligence import extract, memory, pipeline

    call_id = _insert_call(transcript=TRANSCRIPT + "\nCaller: chalega.")
    with get_db() as db:
        db.execute("UPDATE calls SET contact_id=NULL WHERE id=?", (call_id,))
    call = pipeline._load_call(call_id)

    called = {"n": 0}

    def counting(contact_id, limit_chars=600):
        called["n"] += 1
        return ""

    async def fake_llm(prompt):
        return _llm_output()

    monkeypatch.setattr(memory, "digest_for_contact", counting)
    monkeypatch.setattr(extract, "run_llm", fake_llm)
    asyncio.run(pipeline.stage_extract(call))
    assert called["n"] == 0
