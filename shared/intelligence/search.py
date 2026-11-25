"""Hybrid semantic call search — the Call History natural-language filter.

Two retrieval arms run over call_embeddings, both tenant-scoped:

    semantic — HNSW cosine over 768-dim gemini-embedding-001 vectors
               (handles paraphrase + Hinglish/Gujarati code-mix)
    lexical  — FTS websearch_to_tsquery('simple') over the generated tsv
               column, with a trigram-similarity fallback for short/misspelled
               queries (handles exact terms like "3BHK")

Results fuse at the CALL level with Reciprocal Rank Fusion (k=60): each call
is ranked by its best chunk in each arm; RRF rewards calls found by both.

Degrades gracefully: if the embedding API is down (or embeddings disabled),
the semantic arm is skipped and results are lexical-only with degraded=True.
Embedding the query is the only per-search API cost (~100ms); everything else
is two index scans.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from shared.db.database import get_db
from shared.intelligence import config, embed

log = logging.getLogger("intel.search")

RRF_K = 60

# How many CALLS each arm may contribute to the fusion.
#
# This was 40, and 40 is the reason a filter with a large true answer showed
# about half of it. Measured 2026-08-29 on the shubhangi recruiting campaign
# (609 calls in the account, 98 of them genuine matches for "people willing to
# relocate to vapi"), sweeping ARM_LIMIT against the candidate pool:
#
#     ARM_LIMIT   pool   candidates   true found   recall
#            40    120          103           68      70%
#            40    400          103           68      70%   <- pool is NOT the
#            80    250          128           85      87%      binding limit
#           150    250          195           98     100%
#           300    400          400           98     100%
#
# Every arm agrees on the same strong calls, so the union of six arms capped at
# 40 is barely wider than one arm: raising the pool alone changed nothing, and
# the 29 matches that never reached the judge were all embedded and retrievable
# — they were simply ranked 41st or worse in every arm. A judge cannot repair a
# call retrieval never surfaced, so this is the ceiling that decides recall.
#
# 150 is where recall saturates with headroom, not the largest value that
# works: the cost of a wider arm is one bigger sort over rows the HNSW scan has
# already produced, while the cost of a NARROW arm is a silently missing half
# of the answer.
ARM_LIMIT = int(os.environ.get("INTEL_SEARCH_ARM_LIMIT", "150"))

# Extra query phrasings / keyword arms accepted per search. Each costs one
# index scan (the embeddings for all of them are one API request), so the
# cap is about keeping fusion honest rather than cost: past a handful, the
# weakest paraphrase starts promoting calls no phrasing ranked highly.
MAX_PROBES = 5
MAX_KEYWORD_ARMS = 6

# Voting BUDGETS, not per-arm weights (see rrf_fuse). Per-arm weights do not
# hold: the planner decides how many paraphrases and keywords to emit, so a
# fixed 0.35 each still lets six single-word arms ("yes", "agree", "moving")
# out-vote the question at 6 x 0.35 = 2.1. A budget is split across however
# many arms arrive, so the ceiling is a property of the code rather than of
# whatever the model happened to write this time.
#
# The question itself gets 1.0 in each modality (semantic + lexical = 2.0);
# every paraphrase together is worth at most one more, and every keyword
# together at most half — enough to RESCUE a call the query wording missed,
# never enough to seat one the query never found.
PROBE_BUDGET = 1.0
KEYWORD_BUDGET = 0.5


# How many CHUNKS the vector arm pulls before de-duplicating to one row per
# call. Sits on a distance-ordered subquery, which is the only place a LIMIT
# may go — see the note in _semantic_arm.
# Raised with ARM_LIMIT: an arm can only return 150 distinct calls if the
# distance-ordered chunk pool it de-duplicates actually contains 150 distinct
# calls. At ~2.4 chunks per call, 1200 chunks bottomed out around 500 calls,
# which was fine for a 609-call account and would have re-introduced the same
# truncation on a larger one.
CHUNK_POOL = int(os.environ.get("INTEL_SEARCH_CHUNK_POOL", "4000"))

# HNSW is an APPROXIMATE index and it stops early. `hnsw.ef_search` defaults to
# 40, which is a hard ceiling on rows the vector arm can return NO MATTER what
# the SQL LIMIT says — the exact 40 that ARM_LIMIT used to impose, moved one
# layer down where it is invisible. Proven 2026-08-29: with 450 embedded chunks
# and LIMIT 4000, the plan
#
#     Index Scan using idx_call_embeddings_hnsw
#       Order By: (embedding <=> $1)
#       Filter: (owner = 'search@test.local')
#
# returned exactly 40 rows.
#
# It is worse than a plain cap, because `owner` is a post-index FILTER, not an
# index condition: HNSW walks its ~ef_search nearest chunks ACROSS ALL TENANTS
# and only then discards other owners' rows. A tenant whose calls are not in
# the global neighbourhood can get nothing back at all.
#
# This is latent rather than live: at the current corpus size Postgres costs a
# sequential/bitmap scan cheaper and picks it, and a seq scan is exact — which
# is why measured live recall was 100%. It flips silently to the HNSW plan as
# the table grows, and recall collapses with no error anywhere.
#
# `iterative_scan` (pgvector >= 0.8) is the fix for a FILTERED vector search: it
# keeps walking the graph until the LIMIT is satisfied after filtering, instead
# of returning a short page. `relaxed_order` is the cheaper mode and costs us
# nothing, because _order_semantic re-sorts by score in Python anyway.
HNSW_EF_SEARCH = int(os.environ.get("INTEL_SEARCH_HNSW_EF", "400"))     # max 1000
HNSW_ITERATIVE = os.environ.get("INTEL_SEARCH_HNSW_ITERATIVE", "relaxed_order")
# The only values pgvector accepts. Enumerated because this one lands in SQL
# as a literal (see _tune_vector_scan) and must never be free text.
_ITERATIVE_MODES = frozenset({"off", "strict_order", "relaxed_order"})


def _tune_vector_scan(db) -> None:
    """Make the vector index able to answer the query we actually asked.

    SET LOCAL, so it lasts exactly one transaction and never leaks into other
    queries on a pooled connection. Wrapped: an older pgvector without these
    GUCs, or SQLite, must degrade to the previous behaviour rather than break
    search — a narrower result is bad, no result is worse.
    """
    # These GUCs take a LITERAL, not a bind parameter — Postgres rejects
    # `SET LOCAL x = $1` — so the value is interpolated into the statement.
    # That is the one construction in this codebase that could carry a payload
    # into SQL, so neither value is allowed to be free text: ef_search is
    # coerced through int() at import, and iterative_scan is matched against
    # the enum Postgres itself accepts. An operator typo in
    # INTEL_SEARCH_HNSW_ITERATIVE now falls back to the default instead of
    # being pasted into a statement.
    _mode = HNSW_ITERATIVE if HNSW_ITERATIVE in _ITERATIVE_MODES else "relaxed_order"
    _ef = max(1, min(1000, int(HNSW_EF_SEARCH)))
    for stmt, val in ((f"SET LOCAL hnsw.ef_search = {_ef}", _ef),
                      (f"SET LOCAL hnsw.iterative_scan = {_mode}", _mode)):
        try:
            db.execute(stmt)
        except Exception as exc:      # noqa: BLE001 - never fail a search here
            log.debug("vector scan tuning unavailable (%s): %r", stmt, exc)


def _agent_clause(agents: list[str] | None) -> tuple[str, list]:
    """Restrict either arm to a set of agent slugs. call_embeddings has no
    agent column, so this joins back to calls."""
    if not agents:
        return "", []
    return ("JOIN calls ca ON ca.id = e.call_id AND ca.agent_slug = ANY(?)",
            [list(agents)])


def _semantic_arm(owner: str, query_vec: list[float], limit: int,
                  agents: list[str] | None = None) -> list[dict]:
    """Nearest chunks by cosine distance, then the best chunk per call.

    The LIMIT must sit on a subquery ordered BY DISTANCE. Until 2026-08-25 it
    sat on a query whose ORDER BY led with call_id (required by DISTINCT ON),
    which made `LIMIT 400` a truncation by ID rather than by relevance: with
    472 embedded calls it kept ids 1..443 and dropped every call above that
    — the newest calls on an account could not be returned by semantic search
    at all, whatever the query said. Ordering the inner query by distance is
    also what lets the HNSW index serve it.
    """
    lit = embed._vec_literal(query_vec)
    join, jparams = _agent_clause(agents)
    with get_db() as db:
        _tune_vector_scan(db)
        rows = [dict(r) for r in db.execute(
            f"""SELECT DISTINCT ON (call_id) call_id, content, 1 - dist AS score
                  FROM (
                    SELECT e.call_id, e.content,
                           (e.embedding <=> ?::vector) AS dist
                      FROM call_embeddings e
                      {join}
                     WHERE e.owner = ? AND e.embedding IS NOT NULL
                     ORDER BY e.embedding <=> ?::vector
                     LIMIT ?
                  ) t
                 ORDER BY call_id, dist""",
            (lit, *jparams, owner, lit, CHUNK_POOL),
        ).fetchall()]
    # DISTINCT ON forces the SQL to come back in call_id order, so the cap
    # below must be applied AFTER ranking by score. Slicing the id-ordered
    # rows (as this did until 2026-08-25) is the same truncation-by-id defect
    # as the old SQL LIMIT, just moved into Python: it kept the 100 lowest
    # call_ids and threw away every better match above them.
    return sorted(rows, key=lambda r: -(r["score"] or 0))[:limit * 4]


def _order_semantic(rows: list[dict], limit: int) -> list[dict]:
    return sorted(rows, key=lambda r: -(r["score"] or 0))[:limit]


def _lexical_arm(owner: str, query: str, limit: int,
                 agents: list[str] | None = None) -> list[dict]:
    join, jparams = _agent_clause(agents)
    with get_db() as db:
        # The LIMIT must sit on a RANK-ordered subquery, for the same reason it
        # does in _semantic_arm. DISTINCT ON (call_id) forces `ORDER BY
        # e.call_id` to lead, so a LIMIT on that query truncates by ID, not by
        # relevance: once a common term matched more chunks than the cap, the
        # arm kept the lowest call_ids and silently dropped the newest calls on
        # the account however well they scored. That defect was fixed in the
        # vector arm on 2026-08-25 and left standing here; on the corpus that
        # exposed the recall bug it had not yet fired (the widest term matched
        # 338 chunks against a cap of 400), so this is closing the class before
        # it bites rather than a fix for an observed loss.
        rows = [dict(r) for r in db.execute(
            f"""SELECT DISTINCT ON (call_id) call_id, content, score
                  FROM (
                    SELECT e.call_id, e.content,
                           ts_rank(e.tsv, websearch_to_tsquery('simple', ?)) AS score
                      FROM call_embeddings e
                      {join}
                     WHERE e.owner = ?
                       AND e.tsv @@ websearch_to_tsquery('simple', ?)
                     ORDER BY score DESC
                     LIMIT ?
                  ) t
                 ORDER BY call_id, score DESC""",
            (query, *jparams, owner, query, CHUNK_POOL),
        ).fetchall()]
        if not rows and len(query) >= 4:
            # trigram fallback for misspellings / partial words. The %% is the
            # pg_trgm similarity operator escaped for psycopg's placeholder
            # parser (the qmark wrapper only rewrites '?', not '%').
            # Same rank-ordered-subquery shape as above, same reason.
            rows = [dict(r) for r in db.execute(
                f"""SELECT DISTINCT ON (call_id) call_id, content, score
                      FROM (
                        SELECT e.call_id, e.content,
                               similarity(e.content, ?) AS score
                          FROM call_embeddings e
                          {join}
                         WHERE e.owner = ? AND e.content %% ?
                         ORDER BY score DESC
                         LIMIT ?
                      ) t
                     ORDER BY call_id, score DESC""",
                (query, *jparams, owner, query, CHUNK_POOL),
            ).fetchall()]
    return sorted(rows, key=lambda r: -(r["score"] or 0))[:limit]


def _shared(budget: float, arms: int) -> list[float]:
    """Split one voting budget evenly across however many auxiliary arms the
    planner produced."""
    return [budget / arms] * arms if arms > 0 else []


def rrf_fuse(arms: list[list[dict]], k: int = RRF_K,
             weights: list[float] | None = None) -> list[dict]:
    """Reciprocal Rank Fusion at the call level; keeps the best snippet.

    `weights` exists because the extra arms are not peers of the query. Six
    unweighted keyword arms contribute up to 6/(k+1) to one call, while the
    real query's top hit contributes 1/(k+1) — so a call that merely contains
    "yes", "agree" and "moving" could out-rank the call the question was
    actually about. Paraphrases and keywords are there to RESCUE recall, not
    to vote, so they share a fixed budget between them (see _shared).
    """
    scores: dict[int, float] = {}
    snippets: dict[int, str] = {}
    for i, arm in enumerate(arms):
        w = weights[i] if weights and i < len(weights) else 1.0
        for rank, row in enumerate(arm):
            cid = row["call_id"]
            scores[cid] = scores.get(cid, 0.0) + w / (k + rank + 1)
            snippets.setdefault(cid, row.get("content") or "")
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return [{"call_id": cid, "rrf": score, "snippet": snippets.get(cid, "")}
            for cid, score in ranked]


# ---------------------------------------------------------------------------
# Query intent — structured constraints written INSIDE the natural-language box
# ---------------------------------------------------------------------------
# "people who said no to coming to vapi, using agent shubhangi" is two things:
# a semantic phrase and an agent constraint. Embedded whole, the constraint
# does no filtering AND dilutes the query vector with words that appear in no
# transcript. Lifting it out makes the filter real and the phrase cleaner.
#
# Deliberately conservative: a name is only read as an agent constraint when a
# CUE word introduces it ("using agent shubhangi", "by shubhangi"). A bare
# mention is left in the text, so "calls about the Pramukh Heights project"
# still searches for the project rather than silently filtering to an agent
# that happens to share the name.

_AGENT_CUE = r"(?:\b(?:using|via|with|by|from|through|on)\s+)?(?:the\s+)?agents?\s+"
_CUE_ONLY = r"\b(?:using|via|by|from|through)\s+(?:the\s+)?"


def _known_agent_slugs(owner: str) -> list[str]:
    with get_db() as db:
        return [r["agent_slug"] for r in db.execute(
            """SELECT DISTINCT agent_slug FROM calls
                WHERE owner = ? AND agent_slug IS NOT NULL AND agent_slug <> ''""",
            (owner,),
        ).fetchall()]


def parse_query_intent(owner: str, query: str) -> dict[str, Any]:
    """Split a natural-language query into {query, agents, agent_label}.

    `agents` is a LIST because a spoken name maps to every direction it was
    built in — "shubhangi" means shubhangi-inbound AND shubhangi-outbound, and
    filtering to one of them would silently hide half the calls.
    """
    import re

    text = (query or "").strip()
    if not text:
        return {"query": "", "agents": [], "agent_label": ""}

    slugs = _known_agent_slugs(owner)
    # base name → every slug that carries it, longest name first so
    # "shubhangi-outbound" is preferred over the bare "shubhangi".
    by_name: dict[str, list[str]] = {}
    for slug in slugs:
        by_name.setdefault(slug.lower(), []).append(slug)
        base = re.sub(r"-(inbound|outbound)$", "", slug, flags=re.I).lower()
        if base != slug.lower():
            by_name.setdefault(base, []).append(slug)

    for name in sorted(by_name, key=len, reverse=True):
        pat = re.compile(_AGENT_CUE + re.escape(name) + r"\b", re.I)
        m = pat.search(text)
        if not m:
            pat = re.compile(_CUE_ONLY + re.escape(name) + r"\b", re.I)
            m = pat.search(text)
        if not m:
            continue
        residual = (text[:m.start()] + " " + text[m.end():])
        residual = re.sub(r"\s*,\s*(?=,|$)", "", residual)
        residual = re.sub(r"\s{2,}", " ", residual).strip(" ,.;-")
        return {"query": residual or text, "agents": sorted(set(by_name[name])),
                "agent_label": name}

    return {"query": text, "agents": [], "agent_label": ""}


async def search_calls(owner: str, query: str, limit: int = 25,
                       agents: list[str] | None = None,
                       probes: list[str] | None = None,
                       keywords: list[str] | None = None) -> dict[str, Any]:
    """Returns {results: [{call_id, rrf, snippet}], degraded: bool}.

    `agents` restricts BOTH arms to those agent slugs, so a filtered search
    ranks within the filtered set instead of retrieving globally and being
    intersected afterwards — the latter loses every relevant call that did not
    make the global top-K.

    `probes` are extra phrasings of the SAME request (the filter planner writes
    them: "shifting to Vapi", "Vapi ma javanu"), each embedded and searched as
    its own arm before the fusion. `keywords` are proper nouns and numbers that
    get their own lexical arm. Both exist for RECALL: when a judge decides
    membership downstream, a relevant call that retrieval never surfaced is the
    only failure it cannot repair — one phrasing of a code-mixed query does not
    reliably reach a transcript that used another.

    Event-loop safe: the embedding call is async httpx; the (sync) index scans
    run in worker threads so a caller inside FastAPI never blocks the loop.
    """
    import asyncio

    query = (query or "").strip()
    if not query:
        return {"results": [], "degraded": False}

    texts = [query] + [p.strip() for p in (probes or [])
                       if p and p.strip().lower() != query.lower()][:MAX_PROBES]
    degraded = False
    arms: list[list[dict]] = []
    weights: list[float] = []
    if config.EMBED_ENABLED and config.EMBED_API_KEY:
        try:
            # One embedding request for every phrasing, then one HNSW scan each.
            vecs = await embed.embed_texts(texts)
            scans = await asyncio.gather(*(
                asyncio.to_thread(_semantic_arm, owner, v, ARM_LIMIT, agents)
                for v in vecs))
            arms += [_order_semantic(rows, ARM_LIMIT) for rows in scans]
            weights += [1.0] + _shared(PROBE_BUDGET, len(scans) - 1)
        except Exception as exc:
            log.warning("semantic arm unavailable, lexical-only: %r", exc)
            degraded = True
    else:
        degraded = True

    lex_terms = [query] + [k.strip() for k in (keywords or [])
                           if k and k.strip().lower() != query.lower()][:MAX_KEYWORD_ARMS]
    lex = await asyncio.gather(*(
        asyncio.to_thread(_lexical_arm, owner, t, ARM_LIMIT, agents)
        for t in lex_terms))
    arms += list(lex)
    weights += [1.0] + _shared(KEYWORD_BUDGET, len(lex) - 1)
    fused = rrf_fuse(arms, weights=weights)[:limit]
    return {"results": fused, "degraded": degraded}
