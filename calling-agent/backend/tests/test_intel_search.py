"""Hybrid call search — recall, agent scoping, and query-intent parsing.

The defect this suite exists for (2026-08-25): the semantic arm's LIMIT sat on
a query whose ORDER BY led with call_id, because DISTINCT ON requires that.
So `LIMIT 400` truncated by ID, not by relevance — on an account with 472
embedded calls it kept ids 1..443 and dropped every call above. The newest
calls could not be returned by semantic search at all, whatever was typed.
A tenant searching for a conversation from that morning got silence, and the
box looked like it simply did not understand the question.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401

OWNER = "search@test.local"
DIM = 768
N_CALLS = 450          # comfortably past the old 400-row cut


def _vec(closeness: float) -> str:
    """A unit-ish vector whose distance to the probe falls as `closeness`
    rises. One hot dimension is enough to order them deterministically."""
    v = [0.0] * DIM
    v[0] = closeness
    v[1] = 1.0 - closeness
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


PROBE = _vec(1.0)


@pytest.fixture()
def corpus(pg_test_db):
    """N_CALLS calls, one chunk each. The HIGHEST id is the best match — the
    exact arrangement the old truncation could not return."""
    from shared.db.database import get_db

    with get_db() as db:
        db.execute("DELETE FROM call_embeddings WHERE owner=?", (OWNER,))
        db.execute("DELETE FROM calls WHERE owner=?", (OWNER,))
        base = dt.datetime(2026, 1, 1, 12, 0, 0)
        for i in range(N_CALLS):
            agent = "alpha-outbound" if i % 2 else "beta-outbound"
            row = db.execute(
                """INSERT INTO calls (owner, call_key, phone, direction,
                                      agent_slug, started_at)
                   VALUES (?,?,?,?,?,?) RETURNING id""",
                (OWNER, f"k{i}", "919999999999", "outbound", agent,
                 base + dt.timedelta(minutes=i)),
            ).fetchone()
            # closeness rises with i → the last call inserted is the nearest.
            db.execute(
                """INSERT INTO call_embeddings
                       (call_id, owner, kind, chunk_index, content, embedding)
                   VALUES (?,?,?,?,?,?::vector)""",
                (row["id"], OWNER, "transcript", 0,
                 f"relocation question number {i}", _vec(i / N_CALLS)),
            )
    yield
    with get_db() as db:
        db.execute("DELETE FROM call_embeddings WHERE owner=?", (OWNER,))
        db.execute("DELETE FROM calls WHERE owner=?", (OWNER,))


def _newest_id() -> int:
    from shared.db.database import get_db
    with get_db() as db:
        return db.execute(
            "SELECT max(id) AS m FROM calls WHERE owner=?", (OWNER,)
        ).fetchone()["m"]


# ── recall ──────────────────────────────────────────────────────────────────

def test_the_best_match_is_returned_even_when_it_is_the_newest_call(corpus):
    from shared.intelligence import search as S

    probe = [0.0] * DIM
    probe[0] = 1.0
    # _semantic_arm returns call_id order (DISTINCT ON requires it); ranking
    # by score is _order_semantic's job, exactly as search_calls does it.
    rows = S._order_semantic(S._semantic_arm(OWNER, probe, 25), 25)
    assert rows, "semantic arm returned nothing at all"
    assert rows[0]["call_id"] == _newest_id(), (
        "the nearest chunk belongs to the highest call_id; returning anything "
        "else means the LIMIT is truncating by id instead of by distance")


def test_every_call_is_reachable_not_just_the_first_400(corpus):
    from shared.intelligence import search as S

    probe = [0.0] * DIM
    probe[0] = 1.0
    ids = {r["call_id"] for r in S._semantic_arm(OWNER, probe, N_CALLS)}
    newest = _newest_id()
    tail = {newest - k for k in range(50)}
    assert tail <= ids, "the newest 50 calls must all be reachable"


# ── agent scoping ───────────────────────────────────────────────────────────

def test_agent_scope_restricts_both_arms(corpus):
    from shared.db.database import get_db
    from shared.intelligence import search as S

    probe = [0.0] * DIM
    probe[0] = 1.0
    rows = S._semantic_arm(OWNER, probe, 50, ["alpha-outbound"])
    assert rows
    with get_db() as db:
        slugs = {r["agent_slug"] for r in db.execute(
            "SELECT DISTINCT agent_slug FROM calls WHERE id = ANY(?)",
            ([r["call_id"] for r in rows],)).fetchall()}
    assert slugs == {"alpha-outbound"}

    lex = S._lexical_arm(OWNER, "relocation", 50, ["beta-outbound"])
    assert lex
    with get_db() as db:
        slugs = {r["agent_slug"] for r in db.execute(
            "SELECT DISTINCT agent_slug FROM calls WHERE id = ANY(?)",
            ([r["call_id"] for r in lex],)).fetchall()}
    assert slugs == {"beta-outbound"}


def test_unscoped_search_still_spans_agents(corpus):
    from shared.intelligence import search as S
    probe = [0.0] * DIM
    probe[0] = 1.0
    assert len({r["call_id"] for r in S._semantic_arm(OWNER, probe, 100)}) > 50


# ── query intent ────────────────────────────────────────────────────────────

def test_agent_named_in_the_sentence_becomes_a_filter(corpus):
    from shared.intelligence import search as S
    got = S.parse_query_intent(OWNER, "who said no to relocating, using agent alpha")
    assert got["agents"] == ["alpha-outbound"]
    assert "alpha" not in got["query"]
    assert got["query"] == "who said no to relocating"


def test_a_bare_name_is_left_in_the_search_text(corpus):
    """No cue word → it is content, not a constraint. Filtering here would
    hide calls whenever a product shares a name with an agent."""
    from shared.intelligence import search as S
    got = S.parse_query_intent(OWNER, "complaints about the alpha product line")
    assert got["agents"] == []
    assert got["query"] == "complaints about the alpha product line"


def test_a_spoken_name_covers_every_direction_it_was_built_in(pg_test_db):
    from shared.db.database import get_db
    from shared.intelligence import search as S
    with get_db() as db:
        db.execute("DELETE FROM calls WHERE owner=?", (OWNER,))
        for slug in ("gamma-inbound", "gamma-outbound"):
            db.execute(
                """INSERT INTO calls (owner, call_key, phone, direction,
                                      agent_slug, started_at)
                   VALUES (?,?,?,?,?,?)""",
                (OWNER, f"k-{slug}", "919999999999", "outbound", slug,
                 dt.datetime(2026, 1, 1, 12, 0, 0)))
    try:
        got = S.parse_query_intent(OWNER, "angry callers by agent gamma")
        assert got["agents"] == ["gamma-inbound", "gamma-outbound"]
    finally:
        with get_db() as db:
            db.execute("DELETE FROM calls WHERE owner=?", (OWNER,))


def test_plain_query_is_untouched(corpus):
    from shared.intelligence import search as S
    got = S.parse_query_intent(OWNER, "3BHK flat enquiries")
    assert got == {"query": "3BHK flat enquiries", "agents": [], "agent_label": ""}


# ---------------------------------------------------------------------------
# Multi-probe retrieval for the verified filter (2026-08-25)
#
# The judge downstream can drop a wrong candidate, but it cannot invent a
# relevant call retrieval never surfaced. So the filter's planner supplies
# paraphrases and keywords as EXTRA arms — and those arms must widen recall
# without being able to out-vote the question they came from.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n_junk", [1, 3, 6, 12])
def test_extra_arms_cannot_outvote_the_real_query(n_junk):
    """However many keywords the planner emits, a call found only by them must
    not out-rank the call the question itself found. A per-arm weight cannot
    promise this — the arm COUNT is chosen by the model — so the budget is
    split across them."""
    from shared.intelligence import search as S

    query_arm = [{"call_id": 1, "content": "the call the question is about"}]
    # One junk call sitting at rank 1 of every single-word keyword arm.
    junk_arms = [[{"call_id": 2, "content": "yes"}] for _ in range(n_junk)]
    weights = [1.0] + S._shared(S.KEYWORD_BUDGET, n_junk)
    fused = S.rrf_fuse([query_arm, *junk_arms], weights=weights)
    assert fused[0]["call_id"] == 1, (
        f"{n_junk} keyword arms out-voted the query — unweighted RRF lets "
        '"yes"/"agree"/"moving" promote a call the question was not about')


def test_the_auxiliary_budget_does_not_grow_with_the_arm_count():
    from shared.intelligence import search as S

    for n in (1, 4, 9):
        assert sum(S._shared(S.KEYWORD_BUDGET, n)) == pytest.approx(S.KEYWORD_BUDGET)
    assert S._shared(S.PROBE_BUDGET, 0) == []


def test_unweighted_fusion_is_still_the_default():
    """Every existing caller passes no weights and must be unaffected."""
    from shared.intelligence import search as S

    a = [{"call_id": 1, "content": "a"}]
    b = [{"call_id": 2, "content": "b"}]
    assert (S.rrf_fuse([a, b])[0]["rrf"]
            == pytest.approx(S.rrf_fuse([a, b], weights=[1.0, 1.0])[0]["rrf"]))


def test_every_phrasing_gets_its_own_arm(corpus, monkeypatch):
    """The recall half of the fix: one embedding request for all phrasings,
    then one index scan each. A call whose transcript used the planner's
    wording rather than the user's is only reachable this way, and the judge
    downstream cannot invent a candidate retrieval never surfaced."""
    import asyncio

    from shared.intelligence import search as S

    seen: list[str] = []

    async def _embed_texts(texts):
        seen.extend(texts)
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]

    monkeypatch.setattr(S.embed, "embed_texts", _embed_texts)
    monkeypatch.setattr(S.config, "EMBED_ENABLED", True)
    monkeypatch.setattr(S.config, "EMBED_API_KEY", "test")
    out = asyncio.run(S.search_calls(
        OWNER, "moved to vapi", limit=5,
        probes=["shifted to vapi", "vapi relocation"], keywords=["Vapi"]))
    assert seen == ["moved to vapi", "shifted to vapi", "vapi relocation"]
    assert out["degraded"] is False
    assert out["results"], "the fused arms returned nothing"


# ── arm width: the 2026-08-29 recall defect (RUNBOOK §32) ───────────────────
#
# ARM_LIMIT was 40. Because every arm ranks the same strong calls, the union of
# six arms capped at 40 was barely wider than one arm, so a filter whose true
# answer was 98 calls could only ever offer the judge 103 candidates and 29
# genuine matches never reached it. Widening the candidate POOL did not help —
# the arm was the binding constraint. These pin that it stays wide.

def _force_hnsw(monkeypatch):
    """Make _semantic_arm take the HNSW index path deterministically.

    Which plan Postgres picks is cost-based and moves with table statistics, so
    a test that merely hopes for the index scan passes and fails at random —
    that is how the ef_search ceiling below stayed hidden. Forcing the plan is
    what makes these two tests mean something.
    """
    from shared.intelligence import search as S

    real = S._tune_vector_scan

    def forced(db):
        db.execute("SET LOCAL enable_seqscan = off")
        db.execute("SET LOCAL enable_bitmapscan = off")
        real(db)

    monkeypatch.setattr(S, "_tune_vector_scan", forced)


def test_the_vector_index_can_return_far_more_than_forty_calls(corpus, monkeypatch):
    """ARM_LIMIT is only the ceiling if the INDEX can reach it.

    hnsw.ef_search defaults to 40 — the same number ARM_LIMIT used to impose,
    one layer further down where no amount of raising ARM_LIMIT helps. Measured
    2026-08-29: 450 embedded chunks, LIMIT 4000, HNSW plan, 40 rows back.
    """
    from shared.intelligence import search as S

    _force_hnsw(monkeypatch)
    probe = [0.0] * DIM
    probe[0] = 1.0
    rows = S._order_semantic(S._semantic_arm(OWNER, probe, S.ARM_LIMIT),
                             S.ARM_LIMIT)
    assert len(rows) > 40, (
        f"the vector arm returned {len(rows)} calls on the HNSW plan with "
        f"ARM_LIMIT={S.ARM_LIMIT}. ef_search/iterative_scan tuning is what "
        "lets the index answer the query that was actually asked (§32)")


def _hnsw_plan_chosen(probe) -> bool:
    """Does Postgres actually take the HNSW path for the arm's query here?

    Plan choice is cost-based and moves with table statistics — across repeated
    runs of this very file it alternated between the HNSW index and the owner
    index even with seqscan and bitmapscan disabled. So the test below reports
    what it can prove and SKIPS when the planner went elsewhere, rather than
    failing for a reason that has nothing to do with the code under test.
    """
    from shared.db.database import get_db
    from shared.intelligence import search as S

    with get_db() as db:
        db.execute("SET LOCAL enable_seqscan = off")
        db.execute("SET LOCAL enable_bitmapscan = off")
        db.execute("SET LOCAL hnsw.ef_search = 40")
        db.execute("SET LOCAL hnsw.iterative_scan = off")
        plan = " ".join(str(dict(r).get("QUERY PLAN")) for r in db.execute(
            "EXPLAIN SELECT e.call_id FROM call_embeddings e"
            "  WHERE e.owner=? AND e.embedding IS NOT NULL"
            "  ORDER BY e.embedding <=> ?::vector LIMIT ?",
            (OWNER, S.embed._vec_literal(probe), S.CHUNK_POOL)).fetchall())
    return "hnsw" in plan.lower()


def test_without_the_tuning_the_index_stops_at_ef_search(corpus, monkeypatch):
    """The defect itself, pinned: on the HNSW path an untuned scan returns
    ef_search rows (40) however large the LIMIT is. This is why raising
    ARM_LIMIT alone was not enough — the ceiling had simply moved into the
    index, where it is invisible and grows more likely as a table grows."""
    from shared.intelligence import search as S

    probe = [0.0] * DIM
    probe[0] = 1.0
    if not _hnsw_plan_chosen(probe):
        pytest.skip("planner did not choose the HNSW path for this corpus; "
                    "the ef_search ceiling cannot be demonstrated here")

    def untuned(db):
        db.execute("SET LOCAL enable_seqscan = off")
        db.execute("SET LOCAL enable_bitmapscan = off")
        db.execute("SET LOCAL hnsw.ef_search = 40")
        db.execute("SET LOCAL hnsw.iterative_scan = off")

    monkeypatch.setattr(S, "_tune_vector_scan", untuned)
    rows = S._semantic_arm(OWNER, probe, S.ARM_LIMIT)
    assert len(rows) <= 40, (
        f"expected the untuned HNSW scan to stop at ef_search=40, got "
        f"{len(rows)} — if pgvector changed this, revisit _tune_vector_scan")


def test_lexical_arm_ranks_by_relevance_not_by_call_id(corpus):
    """The truncation-by-id defect, in the arm where it survived.

    `DISTINCT ON (e.call_id)` forces `ORDER BY e.call_id` to lead, so a LIMIT on
    that query keeps the LOWEST ids rather than the best matches. Fixed in the
    vector arm on 2026-08-25 and in the lexical arm on 2026-08-29.
    """
    from shared.db.database import get_db
    from shared.intelligence import search as S

    # One extra call, newest id, containing a term nothing else does.
    with get_db() as db:
        row = db.execute(
            """INSERT INTO calls (owner, call_key, phone, direction,
                                  agent_slug, started_at)
               VALUES (?,?,?,?,?,?) RETURNING id""",
            (OWNER, "k-needle", "919999999999", "outbound", "alpha-outbound",
             dt.datetime(2026, 6, 1, 12, 0, 0)),
        ).fetchone()
        db.execute(
            """INSERT INTO call_embeddings
                   (call_id, owner, kind, chunk_index, content, embedding)
               VALUES (?,?,?,?,?,?::vector)""",
            (row["id"], OWNER, "transcript", 0,
             "kryptonite kryptonite kryptonite", _vec(0.0)),
        )
    needle = row["id"]
    try:
        hits = S._lexical_arm(OWNER, "kryptonite", 10)
        assert [h["call_id"] for h in hits] == [needle], (
            "the only call containing the term must come back; a LIMIT applied "
            "in call_id order can drop the newest call however well it scores")
    finally:
        with get_db() as db:
            db.execute("DELETE FROM call_embeddings WHERE call_id=?", (needle,))
            db.execute("DELETE FROM calls WHERE id=?", (needle,))


def test_candidate_pool_is_wide_enough_for_a_real_campaign(corpus):
    """A campaign-sized true answer must fit. 120 candidates could not hold a
    98-call answer once retrieval was fixed; the pool is now 300."""
    from shared.intelligence import config as C

    assert C.FILTER_MAX_CANDIDATES >= 250, (
        f"FILTER_MAX_CANDIDATES={C.FILTER_MAX_CANDIDATES} is below the size of "
        "a single real campaign's answer (RUNBOOK §32)")
    assert C.FILTER_BUDGET_S >= 90, (
        "judging a full pool cold measured ~19s; a budget with no headroom "
        "truncates instead, which silently shortens the answer")
