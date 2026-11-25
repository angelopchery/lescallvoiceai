"""Automation engine: condition trees, event derivation, rule firing with
dedupe + circuit breaker, and end-to-end run execution."""

from __future__ import annotations

import asyncio
import json

from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401
from tests.test_intel_extract import (  # noqa: F401
    TRANSCRIPT, _llm_output, _insert_call,
)


# ---------------------------------------------------------------------------
# Pure condition evaluation
# ---------------------------------------------------------------------------

def test_condition_ops():
    from shared.intelligence.automation import evaluate_conditions as ev

    ctx = {"lead_score": 85, "sentiment": "negative",
           "topics": ["pricing", "3bhk"], "agent_slug": "pramukh"}
    assert ev({"field": "lead_score", "op": "gte", "value": 70}, ctx)
    assert not ev({"field": "lead_score", "op": "lt", "value": 70}, ctx)
    assert ev({"field": "sentiment", "op": "eq", "value": "negative"}, ctx)
    assert ev({"field": "topics", "op": "contains", "value": "pricing"}, ctx)
    assert ev({"field": "agent_slug", "op": "in", "value": ["pramukh", "kaapd"]}, ctx)
    assert ev({"all": [{"field": "lead_score", "op": "gte", "value": 70},
                       {"field": "sentiment", "op": "eq", "value": "negative"}]}, ctx)
    assert ev({"any": [{"field": "lead_score", "op": "gte", "value": 99},
                       {"field": "sentiment", "op": "eq", "value": "negative"}]}, ctx)
    assert not ev({"all": [{"field": "lead_score", "op": "gte", "value": 99}]}, ctx)
    assert ev({}, ctx)  # empty tree matches everything
    assert not ev({"field": "missing", "op": "gte", "value": 1}, ctx)  # absent field


def test_event_derivation():
    from shared.intelligence.automation import _events_for_call

    events = _events_for_call(
        {"sentiment": "negative", "lead_score": 80, "compliance_score": 60,
         "competitor_mentions": ["X Corp"], "escalations": [],
         "followup_decision": "yes"}, commitments_n=2)
    for expected in ("call.analyzed", "sentiment.negative", "lead.hot",
                     "commitment.detected", "followup.created",
                     "compliance.issue", "competitor.mentioned"):
        assert expected in events
    assert "escalation.detected" not in events


# ---------------------------------------------------------------------------
# Rule firing + execution (test DB)
# ---------------------------------------------------------------------------

def _make_rule(owner="t@x.com", trigger="lead.hot", conditions=None, actions=None,
               enabled=1):
    from shared.db.database import get_db

    with get_db() as db:
        row = db.execute(
            """INSERT INTO automation_rules (owner, name, trigger, conditions,
                                             actions, enabled)
               VALUES (?, 'test rule', ?, ?, ?, ?) RETURNING id""",
            (owner, trigger, json.dumps(conditions or {}),
             json.dumps(actions or [{"type": "notify_inapp",
                                     "params": {"title": "hot!"}}]), enabled),
        ).fetchone()
        return row["id"]


def test_fire_and_execute_run_end_to_end(pg_test_db, monkeypatch):
    from shared.db.database import get_db
    from shared.intelligence import automation, extract, pipeline, queue

    call_id = _insert_call(transcript=TRANSCRIPT + "\nCaller: bahut badhiya.")
    call = pipeline._load_call(call_id)
    rec = extract.parse_and_validate(_llm_output(), call)
    pipeline.store_extraction(call, rec, model="test")

    rule_id = _make_rule(
        conditions={"all": [{"field": "lead_score", "op": "gte", "value": 70}]},
        actions=[{"type": "notify_inapp", "params": {"title": "hot lead"}},
                 {"type": "add_tag", "params": {"tag": "auto-hot"}}])

    asyncio.run(automation.stage_automation(call))
    # dedupe: second pass fires nothing new
    asyncio.run(automation.stage_automation(call))

    with get_db() as db:
        runs = [dict(r) for r in db.execute(
            "SELECT * FROM automation_runs WHERE rule_id=?", (rule_id,)
        ).fetchall()]
    assert len(runs) == 1

    job = queue.claim("t", kinds=(queue.KIND_AUTOMATION_RUN,))
    assert job is not None
    asyncio.run(automation.execute_run(job))

    with get_db() as db:
        run = dict(db.execute("SELECT * FROM automation_runs WHERE id=?",
                              (runs[0]["id"],)).fetchone())
        notif = db.execute(
            "SELECT count(*) AS n FROM notifications WHERE kind='automation.fired'"
        ).fetchone()["n"]
        tags = json.loads(db.execute(
            "SELECT tags FROM call_intelligence WHERE call_id=?", (call_id,)
        ).fetchone()["tags"])
    assert run["status"] == "done"
    steps = json.loads(run["steps"])
    assert len(steps) == 2 and all(s["ok"] for s in steps)
    assert notif == 1
    assert "auto-hot" in tags


def test_conditions_gate_firing(pg_test_db):
    from shared.db.database import get_db
    from shared.intelligence import automation, extract, pipeline

    call_id = _insert_call(transcript=TRANSCRIPT + "\nCaller: thik hai ji.")
    call = pipeline._load_call(call_id)
    rec = extract.parse_and_validate(_llm_output(), call)  # lead_score 72
    pipeline.store_extraction(call, rec, model="test")

    rule_id = _make_rule(conditions={"all": [{"field": "lead_score",
                                              "op": "gte", "value": 95}]})
    asyncio.run(automation.stage_automation(call))
    with get_db() as db:
        n = db.execute("SELECT count(*) AS n FROM automation_runs WHERE rule_id=?",
                       (rule_id,)).fetchone()["n"]
    assert n == 0  # condition not met → no run


def test_circuit_breaker(pg_test_db):
    from shared.db.database import get_db
    from shared.intelligence import automation

    rule_id = _make_rule(trigger="call.analyzed", conditions={})
    with get_db() as db:  # simulate a runaway rule: 20 runs in the last hour
        for i in range(automation.MAX_RUNS_PER_HOUR):
            db.execute(
                """INSERT INTO automation_runs (rule_id, rule_version, owner,
                       trigger_event, dedupe_key)
                   VALUES (?, 1, 't@x.com', '{}', ?)""",
                (rule_id, f"cb-seed-{i}"),
            )
    fired = automation.fire_matching_rules(
        "t@x.com", ["call.analyzed"], {"lead_score": 10}, None)
    assert fired == 0  # breaker open


def test_unconfigured_channel_logs_and_continues(pg_test_db):
    from shared.db.database import get_db
    from shared.intelligence import automation, queue

    rule_id = _make_rule(
        trigger="call.analyzed", conditions={},
        actions=[{"type": "send_sms", "params": {}},
                 {"type": "notify_inapp", "params": {"title": "after sms"}}])
    fired = automation.fire_matching_rules("t@x.com", ["call.analyzed"], {}, None)
    assert fired == 1
    job = queue.claim("t", kinds=(queue.KIND_AUTOMATION_RUN,))
    asyncio.run(automation.execute_run(job))
    with get_db() as db:
        run = dict(db.execute(
            "SELECT * FROM automation_runs WHERE rule_id=?", (rule_id,)
        ).fetchone())
    steps = json.loads(run["steps"])
    assert run["status"] == "done"
    assert steps[0]["ok"] is False and "not_configured" in steps[0]["detail"]
    assert steps[1]["ok"] is True  # later actions still ran
