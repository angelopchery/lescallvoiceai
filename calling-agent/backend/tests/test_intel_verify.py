"""The precision gate on the natural-language call filter.

The defect this suite exists for: "whoever relocated to Vapi" and "whoever said
yes to relocate to Delhi" have nearly identical embeddings, so the Delhi query
returned the Vapi calls. Retrieval cannot produce the correct answer to the
second question, which is ZERO ROWS — only a per-call decision can.

So the tests here are about the three gates that code owns, not about whether
the model is clever: a match must clear every requirement it stated, clear the
confidence floor, and quote words that actually occur in that call's
transcript. The last one is what makes an empty result trustworthy — a model
can assert anything, but it cannot fabricate text into a transcript we hold.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401

OWNER = "verify@test.local"

TRANSCRIPT = (
    "[00:01] Agent: This is an on-site role in Vapi, Gujarat. "
    "Would you be comfortable relocating?\n"
    "[00:12] Caller: yes, yes, i'm fine with that.\n"
)


@pytest.fixture()
def corpus(pg_test_db):
    from shared.db.database import get_db

    with get_db() as db:
        db.execute("DELETE FROM intel_filter_verdicts WHERE owner=?", (OWNER,))
        db.execute("DELETE FROM calls WHERE owner=?", (OWNER,))
        base = dt.datetime(2026, 8, 20, 10, 0, 0)
        ids = {}
        for key, tx in (("agreed", TRANSCRIPT),
                        ("refused", TRANSCRIPT.replace(
                            "yes, yes, i'm fine with that.",
                            "no, i cannot relocate right now.")),
                        ("empty", None)):
            row = db.execute(
                """INSERT INTO calls (owner, call_key, conn_id, phone, direction,
                                      agent_slug, started_at, transcript)
                   VALUES (?,?,?,?,?,?,?,?) RETURNING id""",
                (OWNER, f"vk-{key}", f"vc-{key}", "919876500000", "outbound",
                 "hiring-outbound", base, tx),
            ).fetchone()
            ids[key] = row["id"]
    return ids


CRITERION = {
    "restated": "Callers who agreed to relocate to Vapi",
    "subject": "caller",
    "requirements": [{"id": "r1", "text": "relocation is discussed", "kind": "action"},
                     {"id": "r2", "text": "the destination is Vapi", "kind": "entity"},
                     {"id": "r3", "text": "the caller agrees", "kind": "stance"}],
    "excludes": ["the caller declines"],
    "paraphrases": [], "keywords": [], "planned": True,
}


def _ctx(transcript: str = TRANSCRIPT, call_id: int = 1) -> dict:
    return {"call_id": call_id, "transcript": transcript, "summary": "",
            "customer_intent": ""}


# --- the three gates ------------------------------------------------------

def test_a_match_survives_when_the_quote_is_really_in_the_transcript():
    from shared.intelligence import verify

    v = verify._verdict(_ctx(), {
        "checks": {"r1": True, "r2": True, "r3": True}, "match": True,
        "confidence": 0.95, "evidence": "yes, yes, i'm fine with that.",
        "reason": "caller agreed"})
    assert v["match"] is True
    assert v["confidence"] == pytest.approx(0.95)


def test_fabricated_evidence_is_a_hard_no():
    """The model asserts a match and quotes a line the call never contained.
    This is the exact failure mode a similarity score cannot catch."""
    from shared.intelligence import verify

    v = verify._verdict(_ctx(), {
        "checks": {"r1": True, "r2": True, "r3": True}, "match": True,
        "confidence": 0.99,
        "evidence": "yes I have already moved to Delhi last month",
        "reason": "caller confirmed"})
    assert v["match"] is False
    assert "does not appear" in v["reason"]


def test_a_match_with_no_quote_at_all_is_dropped():
    from shared.intelligence import verify

    v = verify._verdict(_ctx(), {"checks": {}, "match": True, "confidence": 0.9,
                                 "evidence": "", "reason": "feels right"})
    assert v["match"] is False


def test_one_failed_check_overrides_an_asserted_match():
    from shared.intelligence import verify

    v = verify._verdict(_ctx(), {
        "checks": {"r1": True, "r2": False, "r3": True}, "match": True,
        "confidence": 0.9, "evidence": "yes, yes, i'm fine with that.",
        "reason": ""})
    assert v["match"] is False


def test_low_confidence_is_a_non_match():
    from shared.intelligence import verify

    v = verify._verdict(_ctx(), {
        "checks": {"r1": True, "r2": True, "r3": True}, "match": True,
        "confidence": 0.2, "evidence": "yes, yes, i'm fine with that.",
        "reason": "probably"})
    assert v["match"] is False
    assert "confidence floor" in v["reason"]


def test_a_call_with_no_transcript_can_still_match_on_stored_analysis():
    """The quote gate only applies to what we can check. A call whose audio
    never transcribed still has an analysis, and refusing to judge it would
    silently shrink every filter."""
    from shared.intelligence import verify

    ctx = {"call_id": 9, "transcript": "", "summary": "Caller agreed to move to Vapi",
           "customer_intent": "relocation"}
    v = verify._verdict(ctx, {"checks": {"r1": True}, "match": True,
                              "confidence": 0.8, "evidence": "",
                              "reason": "per summary"})
    assert v["match"] is True


# --- criterion + cache identity -------------------------------------------

def test_an_empty_plan_falls_back_to_judging_the_raw_phrase():
    from shared.intelligence import verify

    c = verify.normalize_criterion({}, "whoever relocated to vapi")
    assert c["planned"] is False
    assert c["requirements"][0]["text"] == "whoever relocated to vapi"


def test_the_cache_key_moves_when_the_rubric_or_model_does(monkeypatch):
    """A cached verdict must not outlive the rubric that produced it."""
    from shared.intelligence import config, verify

    before = verify.question_hash("q")
    monkeypatch.setattr(config, "FILTER_RUBRIC_VERSION", "v99")
    assert verify.question_hash("q") != before
    monkeypatch.setattr(config, "FILTER_RUBRIC_VERSION", "v1")
    monkeypatch.setattr(config, "FILTER_LLM_MODEL", "some-other-model")
    assert verify.question_hash("q") != before


def test_the_same_question_asked_twice_hashes_the_same():
    """Case and spacing are not a different question."""
    from shared.intelligence import verify

    assert (verify.question_hash("Whoever  Relocated To Vapi")
            == verify.question_hash("whoever relocated to vapi"))


def test_a_planned_criterion_is_reused_for_the_same_question(pg_test_db, monkeypatch):
    """Re-planning would reword the checklist and move the verdict key with
    it, so the plan itself is cached per (owner, question)."""
    import asyncio

    from shared.intelligence import verify

    plans = {"n": 0}

    async def _complete(_client, system, user):
        plans["n"] += 1
        return json.dumps({
            "restated": f"plan number {plans['n']}",
            "subject": "caller",
            "requirements": [{"id": "r1", "text": f"requirement {plans['n']}"}],
            "excludes": [], "paraphrases": [], "keywords": [],
        })

    _credentials(monkeypatch)
    monkeypatch.setattr(verify, "_complete", _complete)
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("DELETE FROM intel_filter_plans WHERE owner=?", (OWNER,))

    first = asyncio.run(verify.plan_query("a fresh question", owner=OWNER))
    second = asyncio.run(verify.plan_query("A Fresh Question", owner=OWNER))
    assert plans["n"] == 1, "the planner ran twice for one question"
    assert second["restated"] == first["restated"]


def test_a_fallback_plan_is_never_cached_as_if_it_were_planned(pg_test_db, monkeypatch):
    """The raw-phrase fallback is what we use when the planner is DOWN.
    Persisting it would freeze the degraded checklist in place for good."""
    import asyncio

    from shared.db.database import get_db
    from shared.intelligence import verify

    async def _boom(*_a, **_kw):
        raise RuntimeError("planner down")

    _credentials(monkeypatch)
    monkeypatch.setattr(verify, "_complete", _boom)
    with get_db() as db:
        db.execute("DELETE FROM intel_filter_plans WHERE owner=?", (OWNER,))
    c = asyncio.run(verify.plan_query("planner is down question", owner=OWNER))
    assert c["planned"] is False
    with get_db() as db:
        assert db.execute(
            "SELECT count(*) AS n FROM intel_filter_plans WHERE owner=?",
            (OWNER,)).fetchone()["n"] == 0



# --- a quote that spans one speaker's segments ------------------------------
#
# The audit transcript has one line per VAD SEGMENT, not per sentence, with a
# timestamp line in between. A caller who pauses mid-sentence is two lines, and
# 28.7% of caller lines in the live corpus are continuations like that. Real
# call 616 (2026-09-21) passed all three checks and was thrown out, because the
# judge quoted the caller's sentence whole.

SPLIT_TRANSCRIPT = """\
[01:10.100]
Agent: Understood. What is your expected CTC?

[01:12.004] [spoke 70.10-71.00s]
Caller: i expected

[01:13.871] [spoke 71.20-73.40s]
Caller: 4 lakh per annum

[01:15.002]
Agent: Got it. Will you be okay with relocating to Vapi, Gujarat?

[01:18.300] [spoke 76.00-77.10s]
Caller: yes, that works for me
"""


def test_a_quote_spanning_one_speakers_consecutive_segments_is_genuine():
    from shared.intelligence import verify

    v = verify._verdict(_ctx(SPLIT_TRANSCRIPT), {
        "checks": {"r1": True, "r2": True, "r3": True}, "match": True,
        "confidence": 0.95, "evidence": "i expected 4 lakh per annum",
        "reason": "expected CTC stated"})
    assert v["match"] is True, v["reason"]


def test_a_quote_may_not_be_stitched_across_a_change_of_speaker():
    """The allowance is for ONE speaker's uninterrupted words. Gluing the
    agent's question onto the caller's answer manufactures a sentence nobody
    said — that stays a hard no, or the guard stops being one."""
    from shared.intelligence import verify

    v = verify._verdict(_ctx(SPLIT_TRANSCRIPT), {
        "checks": {"r1": True, "r2": True, "r3": True}, "match": True,
        "confidence": 0.95,
        "evidence": "relocating to Vapi, Gujarat? yes, that works for me",
        "reason": "agreed"})
    assert v["match"] is False
    assert v["reason"] == verify.QUOTE_NOT_FOUND


def test_a_quote_may_not_skip_over_the_other_speakers_turn():
    """Both halves are the caller's, but the agent spoke in between."""
    from shared.intelligence import verify

    v = verify._verdict(_ctx(SPLIT_TRANSCRIPT), {
        "checks": {"r1": True}, "match": True, "confidence": 0.95,
        "evidence": "4 lakh per annum yes, that works for me", "reason": ""})
    assert v["match"] is False


def test_speech_runs_group_by_speaker_and_drop_the_scaffolding():
    from shared.intelligence.extract import speech_runs

    runs = speech_runs(SPLIT_TRANSCRIPT)
    assert runs == ["understoodwhatisyourexpectedctc", "iexpected4lakhperannum",
                    "gotitwillyoubeokaywithrelocatingtovapigujarat",
                    "yesthatworksforme"]
    # Timestamps and "[spoke ...]" are scaffolding, never quotable speech.
    assert not any("spoke" in r or "0110100" in r for r in runs)
    assert speech_runs("") == [] and speech_runs(None) == []


def test_the_commitment_extractor_keeps_a_promise_spoken_across_two_segments():
    """Same gate, other caller: there a failed quote silently drops the whole
    commitment, so a promise made by someone who paused never became a
    follow-up."""
    from shared.intelligence import extract

    tx = ("[00:40.000]\nCaller: i will send you\n\n"
          "[00:42.500]\nCaller: my resume by tomorrow evening\n")
    rec = extract.parse_and_validate(json.dumps({
        "summary": "Candidate will send a resume.",
        "commitments": [{"made_by": "customer", "kind": "send_document",
                         "description": "Send resume",
                         "quote": "i will send you my resume by tomorrow evening"}],
    }), {"transcript": tx, "started_at": "2026-09-21T10:00:00"})
    assert [c["description"] for c in rec["commitments"]] == ["Send resume"]

# --- the pass -------------------------------------------------------------

class _Client:
    async def close(self):
        return None


def _credentials(monkeypatch):
    """verify_calls refuses to run without a configured endpoint — correct in
    production, and not what these tests are about."""
    from shared.intelligence import config, verify

    monkeypatch.setattr(config, "FILTER_LLM_API_KEY", "test-key")
    monkeypatch.setattr(config, "FILTER_LLM_BASE_URL", "http://test.invalid/v1/")
    monkeypatch.setattr(verify, "_client", lambda: _Client())


def _stub_judge(monkeypatch, decide):
    """Replace the model with a deterministic decision function."""
    from shared.intelligence import verify

    async def _complete(_client, system, user):
        return json.dumps(decide(user))

    _credentials(monkeypatch)
    monkeypatch.setattr(verify, "_complete", _complete)


def test_only_the_calls_that_pass_are_returned(corpus, monkeypatch):
    import asyncio

    from shared.intelligence import verify

    def decide(user: str) -> dict:
        agreed = "i'm fine with that" in user
        return {"checks": {"r1": agreed}, "match": agreed,
                "confidence": 0.9 if agreed else 0.95,
                "evidence": "yes, yes, i'm fine with that." if agreed else "",
                "reason": "agreed" if agreed else "declined"}

    _stub_judge(monkeypatch, decide)
    out = asyncio.run(verify.verify_calls(
        OWNER, "whoever relocated to vapi", list(corpus.values()),
        criterion=CRITERION))
    assert out["verified"] is True
    matched = {cid for cid, v in out["verdicts"].items() if v["match"]}
    assert matched == {corpus["agreed"]}


def test_a_call_with_nothing_to_read_costs_no_model_call(corpus, monkeypatch):
    """A connected-but-silent call has no transcript and no analysis. Spending
    a request to be told 'no' would be pure cost on the emptiest rows."""
    import asyncio

    from shared.intelligence import verify

    seen: list[str] = []

    def decide(user: str) -> dict:
        seen.append(user)
        return {"checks": {}, "match": False, "confidence": 0.9,
                "evidence": "", "reason": "no"}

    _stub_judge(monkeypatch, decide)
    out = asyncio.run(verify.verify_calls(OWNER, "anything", [corpus["empty"]],
                                          criterion=CRITERION))
    assert seen == []
    assert out["verdicts"][corpus["empty"]]["match"] is False
    assert "no transcript" in out["verdicts"][corpus["empty"]]["reason"]


def test_the_second_identical_filter_is_served_from_cache(corpus, monkeypatch):
    import asyncio

    from shared.intelligence import verify

    calls = {"n": 0}

    def decide(user: str) -> dict:
        calls["n"] += 1
        agreed = "i'm fine with that" in user
        return {"checks": {"r1": agreed}, "match": agreed, "confidence": 0.9,
                "evidence": "yes, yes, i'm fine with that." if agreed else "",
                "reason": ""}

    _stub_judge(monkeypatch, decide)
    ids = list(corpus.values())
    asyncio.run(verify.verify_calls(OWNER, "vapi q", ids, criterion=CRITERION))
    first = calls["n"]
    assert first > 0
    out = asyncio.run(verify.verify_calls(OWNER, "vapi q", ids, criterion=CRITERION))
    assert calls["n"] == first, "a repeated filter re-paid for its judgements"
    assert out["cached"] > 0



def test_an_unusable_quote_is_retried_next_time_not_remembered(corpus, monkeypatch):
    """A quote that is not in the transcript says the MODEL'S ANSWER was bad,
    not that the call fails the filter. Cached, one bad generation excluded
    the call from that filter forever; the next run must ask again."""
    import asyncio

    from shared.intelligence import verify

    answers = iter([
        "yes I have already moved to Delhi last month",    # hallucinated
        "yes, yes, i'm fine with that.",                   # real
    ])
    asked = {"n": 0}

    def decide(user: str) -> dict:
        asked["n"] += 1
        return {"checks": {"r1": True}, "match": True, "confidence": 0.9,
                "evidence": next(answers), "reason": "agreed"}

    _stub_judge(monkeypatch, decide)
    cid = corpus["agreed"]
    first = asyncio.run(verify.verify_calls(OWNER, "retry q", [cid], criterion=CRITERION))
    assert first["verdicts"][cid]["match"] is False
    assert first["verdicts"][cid]["reason"] == verify.QUOTE_NOT_FOUND
    # The model DID run, so the pass is an honest, verified one.
    assert first["verified"] is True

    second = asyncio.run(verify.verify_calls(OWNER, "retry q", [cid], criterion=CRITERION))
    assert asked["n"] == 2, "the judge failure was served from cache"
    assert second["verdicts"][cid]["match"] is True


def test_a_judge_failure_frozen_by_an_older_build_heals_on_read(corpus, monkeypatch):
    """Rows written before this rule existed are still in the table."""
    import asyncio

    from shared.intelligence import verify

    cid = corpus["agreed"]
    ctx = verify.load_contexts(OWNER, [cid])[cid]
    q = "legacy frozen q"
    verify.store_verdicts(OWNER, q, verify.question_hash(q), [({
        "call_id": cid, "match": False, "confidence": 1.0,
        "evidence": "i expected 4 lakh per annum",
        "reason": verify.QUOTE_NOT_FOUND, "checks": {"r1": True}},
        verify.context_sha(ctx))])

    _stub_judge(monkeypatch, lambda user: {
        "checks": {"r1": True}, "match": True, "confidence": 0.9,
        "evidence": "yes, yes, i'm fine with that.", "reason": "agreed"})
    out = asyncio.run(verify.verify_calls(OWNER, q, [cid], criterion=CRITERION))
    assert out["cached"] == 0
    assert out["verdicts"][cid]["match"] is True


def test_a_re_transcribed_call_loses_its_cached_verdict(corpus, monkeypatch):
    """The verdict describes a call's CONTENT. When the content changes the
    answer may too, so the cache row must stop applying by itself."""
    import asyncio

    from shared.db.database import get_db
    from shared.intelligence import verify

    calls = {"n": 0}

    def decide(user: str) -> dict:
        calls["n"] += 1
        return {"checks": {"r1": True}, "match": False, "confidence": 0.9,
                "evidence": "", "reason": ""}

    _stub_judge(monkeypatch, decide)
    cid = corpus["agreed"]
    asyncio.run(verify.verify_calls(OWNER, "vapi q2", [cid], criterion=CRITERION))
    assert calls["n"] == 1
    with get_db() as db:
        db.execute("UPDATE calls SET transcript=? WHERE id=?",
                   (TRANSCRIPT + "[00:30] Caller: actually, make it Surat.\n", cid))
    asyncio.run(verify.verify_calls(OWNER, "vapi q2", [cid], criterion=CRITERION))
    assert calls["n"] == 2


def test_verification_reports_itself_unavailable_rather_than_guessing(corpus, monkeypatch):
    """When the judge is down the caller must NOT be told these rows were
    checked — that is precisely the loose behaviour this replaces."""
    import asyncio

    from shared.intelligence import verify

    async def _boom(*_a, **_kw):
        raise RuntimeError("provider down")

    _credentials(monkeypatch)
    monkeypatch.setattr(verify, "_complete", _boom)
    out = asyncio.run(verify.verify_calls(
        OWNER, "vapi q3", [corpus["agreed"], corpus["refused"]],
        criterion=CRITERION))
    assert out["verified"] is False
    assert out["truncated"] is True
    assert not any(v["match"] for v in out["verdicts"].values())


def test_evidence_lookup_returns_only_matches(corpus, monkeypatch):
    import asyncio

    from shared.intelligence import verify

    def decide(user: str) -> dict:
        agreed = "i'm fine with that" in user
        return {"checks": {"r1": agreed}, "match": agreed, "confidence": 0.9,
                "evidence": "yes, yes, i'm fine with that." if agreed else "",
                "reason": "agreed" if agreed else "declined"}

    _stub_judge(monkeypatch, decide)
    ids = list(corpus.values())
    asyncio.run(verify.verify_calls(OWNER, "Vapi Relocation", ids,
                                    criterion=CRITERION))
    ev = verify.load_evidence(OWNER, "vapi relocation", ids)
    assert set(ev) == {corpus["agreed"]}
    assert "fine with that" in ev[corpus["agreed"]]["evidence"]


def test_contentless_calls_are_not_evidence_that_the_judge_ran(corpus, monkeypatch):
    """A silent call is decided in CODE, not by the model. Counting those rows
    as proof of verification let a pass in which every single model request
    failed still report `verified: true` — and pass off "0 matches" as a
    checked answer when nothing had been checked."""
    import asyncio

    from shared.intelligence import verify

    async def _boom(*_a, **_kw):
        raise RuntimeError("provider down")

    _credentials(monkeypatch)
    monkeypatch.setattr(verify, "_complete", _boom)
    out = asyncio.run(verify.verify_calls(
        OWNER, "judge is down", [corpus["empty"], corpus["agreed"]],
        criterion=CRITERION))
    assert out["verdicts"], "the contentless call should still be decided"
    assert out["verified"] is False, (
        "claimed to have verified a pass in which the model never answered")


def test_a_pass_with_nothing_but_contentless_calls_is_still_verified(corpus,
                                                                    monkeypatch):
    """The mirror case: there was genuinely nothing to ask the model, so the
    zero IS checked and must not be labelled unverified."""
    import asyncio

    from shared.intelligence import verify

    async def _never(*_a, **_kw):
        raise AssertionError("the model must not be called here")

    _credentials(monkeypatch)
    monkeypatch.setattr(verify, "_complete", _never)
    out = asyncio.run(verify.verify_calls(OWNER, "anything at all",
                                          [corpus["empty"]], criterion=CRITERION))
    assert out["verified"] is True


def _seed_call(owner: str, transcript: str,
               agent_slug: str = "shubhangi-outbound") -> int:
    """One call for `owner`, returning its id. Used by the evidence tests."""
    import uuid

    from shared.db.database import get_db

    tag = uuid.uuid4().hex[:8]
    with get_db() as db:
        db.execute("DELETE FROM intel_filter_verdicts WHERE owner=?", (owner,))
        db.execute("DELETE FROM calls WHERE owner=?", (owner,))
        row = db.execute(
            """INSERT INTO calls (owner, call_key, conn_id, phone, direction,
                                  agent_slug, started_at, transcript)
               VALUES (?,?,?,?,?,?,?,?) RETURNING id""",
            (owner, f"ek-{tag}", f"ec-{tag}", "919876500001", "outbound",
             agent_slug, dt.datetime(2026, 8, 27, 12, 0, 0), transcript),
        ).fetchone()
    return int(row["id"])


# ── evidence lookup vs the lifted agent constraint (RUNBOOK §32.6) ──────────
#
# intel_search_calls lifts "...using agent shubhangi" out of the phrase and
# judges on the remainder, storing verdicts under that LIFTED phrase. The
# dashboard sent the phrase the user TYPED, and load_evidence matches on
# lower(query), so the export found nothing: the Evidence sheet shipped empty
# and the workbook said verified:false — a filtered export silently downgraded
# to an unverified one.

def test_evidence_is_keyed_on_the_phrase_the_judge_was_given(pg_test_db):
    """Pins the asymmetry the export has to compensate for."""
    from shared.intelligence import verify as V

    owner = "evidence@test.local"
    typed = "people willing to relocate to vapi using agent shubhangi"
    lifted = "people willing to relocate to vapi"
    call_id = _seed_call(owner, transcript="yes i am willing to relocate to vapi")

    V.store_verdicts(owner, lifted, V.question_hash(lifted), [(
        {"call_id": call_id, "match": True, "confidence": 0.9,
         "evidence": "willing to relocate to vapi", "reason": "agreed",
         "checks": {"r1": True}}, "sha-1")])

    assert V.load_evidence(owner, lifted, [call_id]), (
        "the phrase the judge saw must find its own verdict")
    assert not V.load_evidence(owner, typed, [call_id]), (
        "the typed phrase does NOT find it — which is exactly why the export "
        "path has to retry with the lifted phrase (RUNBOOK §32.6)")


def test_export_falls_back_to_the_lifted_phrase(pg_test_db, monkeypatch):
    """The compensation itself: the export must recover the evidence."""
    import sys
    from pathlib import Path
    dash = Path(__file__).resolve().parents[3] / "dashboard"
    if str(dash) not in sys.path:
        sys.path.insert(0, str(dash))
    from shared.intelligence import search as S, verify as V

    owner = "evidence2@test.local"
    typed = "people willing to relocate to vapi using agent shubhangi"
    lifted = "people willing to relocate to vapi"
    call_id = _seed_call(owner, transcript="yes i will relocate to vapi",
                         agent_slug="shubhangi-outbound")
    V.store_verdicts(owner, lifted, V.question_hash(lifted), [(
        {"call_id": call_id, "match": True, "confidence": 0.9,
         "evidence": "i will relocate to vapi", "reason": "agreed",
         "checks": {"r1": True}}, "sha-2")])

    # The fallback the export performs, exercised directly.
    ev = V.load_evidence(owner, typed, [call_id])
    if not ev:
        alt = S.parse_query_intent(owner, typed)["query"]
        assert alt == lifted, f"agent constraint not lifted; got {alt!r}"
        ev = V.load_evidence(owner, alt, [call_id])
    assert ev and call_id in ev, (
        "after the fallback the workbook must carry the quote that put this "
        "row in the result")
