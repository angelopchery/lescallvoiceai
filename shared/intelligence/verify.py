"""LLM membership verification for the natural-language call filter.

WHY THIS EXISTS
---------------
Hybrid retrieval (search.py) answers "which calls are ABOUT this?". A filter
asks something stricter: "which calls SATISFY this?" — and the two questions
have nearly identical embeddings.

    "whoever relocated to Vapi"            ─┐  cosine similarity ≈ 0.95
    "whoever said yes to relocate to Delhi" ─┘

Every call that discusses relocation ranks high on both, so the Delhi query
returned the Vapi calls: relevant-looking, and wrong. The honest answer to the
Delhi query on this corpus is ZERO ROWS, and no amount of re-ranking a
similarity score produces a zero.

So retrieval is demoted to candidate generation and membership becomes a
decision. Each candidate is read by a small model against an explicit
requirement checklist derived from the query, and must be defended with a
verbatim quote from that call's own transcript. Three gates, in order:

    1. every requirement met                  (the model's own checklist)
    2. stated confidence ≥ FILTER_MIN_CONFIDENCE
    3. the quote actually appears in the transcript   ← checked in code,
                                                        not taken on trust

Gate 3 is what makes the empty result trustworthy: a model that wants to be
helpful can assert a match, but it cannot fabricate words into a transcript we
already hold. `extract.quote_in_transcript` is the same hallucination guard
the extraction stage uses on customer quotes.

COST
----
One planner call per QUERY plus one judge call per CANDIDATE, on the smallest
model on the key (gemini-3.5-flash-lite, ~1.3s, ~600 tokens for a 1.7k-char
transcript). Verdicts are cached per (call, question, call-context) in
intel_filter_verdicts, so re-running or paging a filter costs nothing.

DEGRADATION
-----------
Never raises into a search. No key, a provider outage, a timeout: the pass
returns `verified=False` and the caller falls back to unverified retrieval
ranking, flagged in the response so the UI says so instead of quietly serving
the loose results this module was built to replace.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Any

from shared.db.database import get_db
from shared.intelligence import config
from shared.intelligence.extract import quote_in_transcript, speech_runs, _normalize

log = logging.getLogger("intel.verify")

MAX_REQUIREMENTS = 8
MAX_PARAPHRASES = 5


# ---------------------------------------------------------------------------
# Query planning — turn a phrase into a checklist
# ---------------------------------------------------------------------------

_PLANNER_SYSTEM = """\
You convert a business user's call-filter phrase into a STRICT membership test \
that will be applied to individual phone-call transcripts.

Return JSON only:
{
  "restated": "one precise sentence describing the calls that qualify",
  "subject": "caller" | "agent" | "either",
  "requirements": [{"id": "r1", "text": "...", "kind": "entity|action|stance|attribute|time"}],
  "excludes": ["a call that only does THIS does not qualify", ...],
  "paraphrases": ["alternative phrasings of the same request, for retrieval"],
  "keywords": ["proper nouns, place names, project names, numbers, plus likely spellings/transliterations"]
}

Rules for building the requirement list — these decide whether the filter is
accurate or useless:

1. EVERY named entity becomes its OWN requirement. "relocate to Vapi" is two
   requirements: relocation is discussed, AND the destination is Vapi. A call
   about relocating to Surat must fail the second one.
2. Agreement, refusal and interest are STANCE requirements and are separate
   from the topic. "said yes to relocating" requires an affirmative commitment
   from the subject, not merely that relocating came up.
3. WHO said it matters. Default subject is the caller/customer. The agent
   offering something is not the customer accepting it.
4. Numbers, budgets, sizes, dates and durations are exact requirements
   ("3BHK" is not satisfied by "2BHK", "under 50 lakh" is not satisfied by
   "80 lakh").
5. Be strict about ENTITIES, NUMBERS and STANCE — never about tense. A phone
   call captures what someone AGREES to, not what they later did, so a user
   who types "whoever relocated to Vapi" is asking for the callers who said
   yes to relocating to Vapi. Require the act to have already happened ONLY
   when the phrase says so ("has already moved", "who has shifted"). Do not
   invent a completion requirement the user did not ask for — that turns a
   real answer into an empty one, which is just as wrong as a loose match.
6. Write requirements a reader could tick off against a transcript. No
   requirement may be a restatement of the whole query.
7. `excludes` names the near-miss that a similarity search would wrongly
   return — usually "the topic is discussed but <the specific claim> is not
   true", and any sibling entity the user did NOT ask for.
8. `paraphrases` and `keywords` are for RETRIEVAL only, never for judging.
   Include Hinglish/Indian-English phrasings and common transliterations,
   because the transcripts are code-mixed English/Hindi/Gujarati.
9. At most 8 requirements. Fewer, sharper requirements beat many vague ones.

Worked examples — rule 5 is the one models get wrong, so read these:

  "whoever relocated to Vapi"
    -> r1 relocating/moving for the role is discussed on the call
       r2 the destination named is Vapi (not Surat, not Valsad, not Delhi)
       r3 the caller answers affirmatively — agrees, accepts, says yes
    excludes: the caller declines or is unsure; the destination is another
              city; only the agent raises it and the caller never answers
    NOT a requirement: "the caller has already completed the move". The
    phrase does not say "already", and a phone call cannot show it.

  "whoever said yes to relocate to Delhi"
    -> the same three requirements with Delhi as r2. On a corpus where every
       relocation conversation is about Vapi, the honest answer is zero rows,
       and r2 is what produces it.

  "customers who have already moved into their flat"
    -> here "already" IS in the phrase, so completion IS a requirement.

Never write a requirement containing "completed", "already", "has finished"
or "actually did" unless the user's own phrase contains a word like it.
"""


def _client():
    from openai import AsyncOpenAI

    return AsyncOpenAI(base_url=config.FILTER_LLM_BASE_URL,
                       api_key=config.FILTER_LLM_API_KEY,
                       timeout=config.FILTER_LLM_TIMEOUT_S)


async def _complete(client, system: str, user: str) -> str:
    kwargs: dict[str, Any] = {}
    if config.FILTER_LLM_REASONING_EFFORT:
        kwargs["reasoning_effort"] = config.FILTER_LLM_REASONING_EFFORT
    resp = await client.chat.completions.create(
        model=config.FILTER_LLM_MODEL,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        max_tokens=config.FILTER_LLM_MAX_OUTPUT_TOKENS,
        temperature=0,
        **kwargs,
    )
    return resp.choices[0].message.content or ""


def _loads(raw: str) -> dict:
    """Tolerant JSON read — a fenced block or a preamble must not lose a whole
    verification pass."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        out = json.loads(text)
    except (ValueError, TypeError):
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return {}
        try:
            out = json.loads(m.group(0))
        except (ValueError, TypeError):
            return {}
    return out if isinstance(out, dict) else {}


def _str_list(val: Any, cap: int) -> list[str]:
    if not isinstance(val, list):
        return []
    out = []
    for v in val:
        s = str(v).strip()
        if s and s not in out:
            out.append(s[:200])
        if len(out) >= cap:
            break
    return out


def fallback_criterion(query: str) -> dict:
    """The checklist we use when the planner itself is unavailable: the query
    as a single requirement. Strictly worse than a planned criterion, but it
    still runs the per-call judge — which is the part that fixes precision."""
    return {
        "restated": query,
        "subject": "either",
        "requirements": [{"id": "r1", "text": query, "kind": "action"}],
        "excludes": [],
        "paraphrases": [],
        "keywords": [],
        "planned": False,
    }


def normalize_criterion(raw: dict, query: str) -> dict:
    reqs: list[dict] = []
    for i, item in enumerate(raw.get("requirements") or [], start=1):
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        reqs.append({"id": str(item.get("id") or f"r{i}")[:8],
                     "text": text[:300],
                     "kind": str(item.get("kind") or "action")[:20]})
        if len(reqs) >= MAX_REQUIREMENTS:
            break
    if not reqs:
        return fallback_criterion(query)
    subject = str(raw.get("subject") or "either").lower()
    if subject not in ("caller", "agent", "either"):
        subject = "either"
    return {
        "restated": str(raw.get("restated") or query)[:400],
        "subject": subject,
        "requirements": reqs,
        "excludes": _str_list(raw.get("excludes"), 6),
        "paraphrases": _str_list(raw.get("paraphrases"), MAX_PARAPHRASES),
        "keywords": _str_list(raw.get("keywords"), 12),
        "planned": True,
    }


def load_plan(owner: str, qhash: str) -> dict | None:
    if not owner:
        return None
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT criterion FROM intel_filter_plans"
                " WHERE owner=? AND query_hash=?", (owner, qhash)).fetchone()
            if not row:
                return None
            db.execute("UPDATE intel_filter_plans SET used_at=now()"
                       " WHERE owner=? AND query_hash=?", (owner, qhash))
    except Exception as exc:
        log.warning("plan cache unreadable: %r", exc)
        return None
    try:
        plan = json.loads(row["criterion"])
    except (ValueError, TypeError):
        return None
    return plan if isinstance(plan, dict) and plan.get("requirements") else None


def store_plan(owner: str, qhash: str, query: str, criterion: dict) -> None:
    if not (owner and criterion.get("planned")):
        return          # never cache the fallback checklist as if it were one
    try:
        with get_db() as db:
            db.execute(
                """INSERT INTO intel_filter_plans
                       (owner, query_hash, query, model, rubric_version, criterion)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT (owner, query_hash) DO UPDATE
                       SET criterion = EXCLUDED.criterion, used_at = now()""",
                (owner, qhash, query[:2000], config.FILTER_LLM_MODEL,
                 config.FILTER_RUBRIC_VERSION, json.dumps(criterion)),
            )
    except Exception as exc:
        log.warning("plan cache unwritable: %r", exc)


async def plan_query(query: str, client=None, owner: str = "") -> dict:
    """Phrase -> requirement checklist. One small-model call per QUESTION, then
    cached: re-planning the same phrase would reword the checklist and, with
    it, move the verdict cache key (see question_hash)."""
    query = (query or "").strip()
    if not query:
        return fallback_criterion("")
    qhash = question_hash(query)
    cached = await asyncio.to_thread(load_plan, owner, qhash) if owner else None
    if cached:
        return cached
    own = client is None
    client = client or _client()
    try:
        raw = await _complete(client, _PLANNER_SYSTEM, f"Filter phrase: {query}")
        criterion = normalize_criterion(_loads(raw), query)
    except Exception as exc:
        log.warning("filter planner unavailable (%r) — judging on the raw phrase", exc)
        return fallback_criterion(query)
    finally:
        if own:
            await client.close()
    await asyncio.to_thread(store_plan, owner, qhash, query, criterion)
    return criterion


# ---------------------------------------------------------------------------
# Per-call judgement
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM = """\
You decide whether ONE phone call satisfies a filter. You are the precision \
gate after a similarity search, so the calls you are shown are all plausibly \
related — being on-topic is NOT the test.

Return JSON only:
{
  "checks": {"r1": true, "r2": false, ...},
  "match": true|false,
  "confidence": 0.0-1.0,
  "evidence": "a VERBATIM span copied from the transcript, or \\"\\" if none exists",
  "reason": "one short sentence"
}

Rules:
- "match" is true ONLY if EVERY requirement is true for this call. One false
  check means match is false, however relevant the call feels.
- The evidence must be copied character-for-character from the transcript
  above. If you cannot find such a span, evidence is "" and match is false.
  Never write the quote from memory, never translate it, never tidy it.
- Discussing a topic is not the same as the claim being true. An agent
  offering, suggesting or asking about something is not the customer doing,
  accepting or confirming it. But the customer ANSWERING that offer clearly —
  "yes, I'm okay with that" — is a stance, and it counts.
- Judge stance and entities strictly; judge tense charitably. A clear
  affirmative commitment satisfies a filter written in the past tense, unless
  a requirement explicitly demands the act was already completed.
- A DIFFERENT entity of the same kind fails the requirement. Another city,
  another project, another size, another price is a NO, not a near-match.
- Silence is a NO. If the transcript never establishes a requirement, that
  check is false — do not infer it from the summary, from the agent's script,
  or from what usually happens on these calls.
- If you are unsure, answer false with a low confidence. An empty result set
  is a correct and useful answer; a wrong row is not.
- Transcripts are code-mixed English/Hindi/Gujarati and contain speech-to-text
  errors. Judge the MEANING of what was said, but quote the text as it stands.
"""


def _criterion_block(criterion: dict) -> str:
    lines = [f"Filter: {criterion.get('restated') or ''}",
             f"Whose words decide it: {criterion.get('subject') or 'either'}",
             "Requirements (ALL must be true):"]
    for r in criterion.get("requirements") or []:
        lines.append(f"  {r['id']}. [{r.get('kind', 'action')}] {r['text']}")
    if criterion.get("excludes"):
        lines.append("Does NOT qualify:")
        lines += [f"  - {x}" for x in criterion["excludes"]]
    return "\n".join(lines)


def _call_block(ctx: dict) -> str:
    tx = ctx.get("transcript") or ""
    if len(tx) > config.FILTER_MAX_TRANSCRIPT_CHARS:
        # Middle-out: the opening establishes who/what and the close carries
        # the commitment; the middle is where repetition lives.
        half = config.FILTER_MAX_TRANSCRIPT_CHARS // 2
        tx = tx[:half] + "\n…[middle of call omitted]…\n" + tx[-half:]
    parts = [
        f"Call date: {ctx.get('started_at') or 'unknown'}",
        f"Direction: {ctx.get('direction') or '?'}   Agent: {ctx.get('agent_slug') or '?'}",
        f"Customer: {ctx.get('contact_name') or 'unknown'} ({ctx.get('phone') or '?'})",
    ]
    if ctx.get("summary"):
        parts.append(f"Stored summary (context only — NEVER quotable evidence): "
                     f"{ctx['summary']}")
    if ctx.get("customer_intent"):
        parts.append(f"Stored intent (context only): {ctx['customer_intent']}")
    parts.append("Transcript:\n" + (tx or "[no transcript captured for this call]"))
    return "\n".join(parts)


def context_sha(ctx: dict) -> str:
    h = hashlib.sha256()
    for key in ("transcript", "summary", "customer_intent"):
        h.update((ctx.get(key) or "").encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def question_hash(query: str) -> str:
    """Identity of the QUESTION: the phrase, the rubric version and the model.

    Deliberately NOT derived from the planner's output. The planner is an LLM;
    asked the same phrase twice at temperature 0 it still rewords its own
    requirement list, so a key built from that text moved on every run and the
    verdict cache measured a 0% hit rate on a verbatim repeat of the same
    filter. Stability lives one level up instead: the PLAN is cached per
    (owner, question) — see plan_query — so the same question always judges
    against the same checklist, and the key can be the question itself.
    """
    payload = json.dumps({
        "q": " ".join((query or "").lower().split()),
        "v": config.FILTER_RUBRIC_VERSION,
        "m": config.FILTER_LLM_MODEL,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# The one rejection that is about the MODEL'S ANSWER, not about the call. A
# failed check or a low confidence is the judge's reading of the transcript
# and is worth remembering; a quote that is not in the transcript means this
# particular response was unusable. Caching it turned one bad generation into
# a permanent exclusion — the call could never re-enter that filter's result,
# however many times the filter was run (call 616, 2026-09-21: all three
# checks true, dropped forever).
QUOTE_NOT_FOUND = "quoted evidence does not appear in the transcript"


def _is_judge_failure(v: dict) -> bool:
    return (not v.get("match")) and (v.get("reason") or "") == QUOTE_NOT_FOUND


def _verdict(ctx: dict, raw: dict) -> dict:
    """Apply the three gates code owns rather than the model: its own stated
    checks must agree with its verdict, the confidence must clear the floor,
    and the quote must actually occur in the transcript we hold."""
    checks = raw.get("checks") if isinstance(raw.get("checks"), dict) else {}
    match = bool(raw.get("match"))
    try:
        conf = float(raw.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    evidence = str(raw.get("evidence") or "").strip()[:600]
    reason = str(raw.get("reason") or "").strip()[:400]

    # A stated check that is false overrides a match the model asserted anyway.
    if match and checks and not all(bool(v) for v in checks.values()):
        match, reason = False, reason or "a requirement was not met"
    if match and conf < config.FILTER_MIN_CONFIDENCE:
        match = False
        reason = f"below the confidence floor ({conf:.2f}); {reason}".strip("; ")
    tx = ctx.get("transcript") or ""
    if match and tx:
        if not evidence:
            match, reason = False, "no quotable evidence in the transcript"
        elif not quote_in_transcript(evidence, _normalize(tx), speech_runs(tx)):
            # The model produced words this call does not contain. That is the
            # exact failure the filter exists to stop, so it is a hard no.
            # (`speech_runs` lets a quote span one speaker's consecutive
            # segments — a caller who pauses mid-sentence is two transcript
            # lines, and quoting their sentence whole is not a fabrication.)
            log.info("verify: dropped call %s — evidence not in transcript",
                     ctx.get("call_id"))
            match = False
            reason = QUOTE_NOT_FOUND
    return {"call_id": ctx.get("call_id"), "match": match, "confidence": conf,
            "evidence": evidence, "reason": reason,
            "checks": {str(k): bool(v) for k, v in list(checks.items())[:MAX_REQUIREMENTS]}}


async def judge_call(client, criterion: dict, ctx: dict) -> dict:
    user = (_criterion_block(criterion) + "\n\n" + "-" * 40 + "\n\n"
            + _call_block(ctx) + "\n\n"
            + "Does this call satisfy EVERY requirement?")
    raw = _loads(await _complete(client, _JUDGE_SYSTEM, user))
    return _verdict(ctx, raw)


# ---------------------------------------------------------------------------
# Context loading + verdict cache
# ---------------------------------------------------------------------------

def load_contexts(owner: str, call_ids: list[int]) -> dict[int, dict]:
    if not call_ids:
        return {}
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            """SELECT c.id AS call_id, c.phone, c.direction, c.agent_slug,
                      c.started_at, c.duration_s, c.transcript, c.outcome,
                      ct.name AS contact_name,
                      ci.summary, ci.customer_intent
                 FROM calls c
                 LEFT JOIN contacts ct ON ct.id = c.contact_id
                 LEFT JOIN call_intelligence ci ON ci.call_id = c.id
                WHERE c.owner = ? AND c.id = ANY(?)""",
            (owner, list(call_ids)),
        ).fetchall()]
    return {r["call_id"]: r for r in rows}


def load_cached(owner: str, qhash: str, ctxs: dict[int, dict]) -> dict[int, dict]:
    """Cached verdicts still valid for THIS call context (a re-analysed or
    re-transcribed call invalidates its own row, nothing else)."""
    if not ctxs:
        return {}
    want = {cid: context_sha(c) for cid, c in ctxs.items()}
    try:
        with get_db() as db:
            rows = [dict(r) for r in db.execute(
                """SELECT call_id, is_match, confidence, evidence, reason,
                          checks, context_sha
                     FROM intel_filter_verdicts
                    WHERE owner = ? AND query_hash = ? AND call_id = ANY(?)""",
                (owner, qhash, list(want)),
            ).fetchall()]
    except Exception as exc:          # a missing table must not break search
        log.warning("verdict cache unreadable: %r", exc)
        return {}
    out: dict[int, dict] = {}
    for r in rows:
        if r["context_sha"] != want.get(r["call_id"]):
            continue
        if not r["is_match"] and (r["reason"] or "") == QUOTE_NOT_FOUND:
            continue                  # a judge failure, not a verdict — re-ask
        try:
            checks = json.loads(r["checks"] or "{}")
        except (ValueError, TypeError):
            checks = {}
        out[r["call_id"]] = {"call_id": r["call_id"], "match": bool(r["is_match"]),
                             "confidence": r["confidence"] or 0.0,
                             "evidence": r["evidence"] or "",
                             "reason": r["reason"] or "", "checks": checks,
                             "cached": True}
    return out


def store_verdicts(owner: str, query: str, qhash: str,
                   verdicts: list[tuple[dict, str]]) -> None:
    if not verdicts:
        return
    try:
        with get_db() as db:
            for v, sha in verdicts:
                db.execute(
                    """INSERT INTO intel_filter_verdicts
                           (owner, call_id, query_hash, query, model, context_sha,
                            is_match, confidence, evidence, reason, checks)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT (owner, call_id, query_hash, context_sha)
                       DO UPDATE SET is_match = EXCLUDED.is_match,
                                     confidence = EXCLUDED.confidence,
                                     evidence = EXCLUDED.evidence,
                                     reason = EXCLUDED.reason,
                                     checks = EXCLUDED.checks,
                                     created_at = now()""",
                    (owner, v["call_id"], qhash, query[:2000],
                     config.FILTER_LLM_MODEL, sha, v["match"], v["confidence"],
                     v["evidence"], v["reason"], json.dumps(v.get("checks") or {})),
                )
    except Exception as exc:
        log.warning("verdict cache unwritable: %r", exc)


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

async def verify_calls(owner: str, query: str, call_ids: list[int],
                       criterion: dict | None = None,
                       client=None) -> dict[str, Any]:
    """Judge each candidate call. Returns

        {verdicts: {call_id: verdict}, criterion, examined, cached,
         verified: bool, truncated: bool, unexamined: [call_id, ...]}

    `verified=False` means the judge could not run at all and the caller must
    NOT present the result as filtered — it fell back to retrieval ranking.
    """
    out: dict[str, Any] = {"verdicts": {}, "criterion": criterion or {},
                           "examined": 0, "cached": 0, "verified": False,
                           "truncated": False, "unexamined": []}
    call_ids = list(dict.fromkeys(int(c) for c in call_ids))[:config.FILTER_MAX_CANDIDATES]
    if not call_ids:
        out["verified"] = bool(config.FILTER_LLM_API_KEY)
        return out
    if not (config.FILTER_VERIFY_ENABLED and config.FILTER_LLM_API_KEY
            and config.FILTER_LLM_BASE_URL):
        return out

    ctxs = await asyncio.to_thread(load_contexts, owner, call_ids)
    own_client = client is None
    client = client or _client()
    try:
        if criterion is None:
            criterion = await plan_query(query, client, owner)
        out["criterion"] = criterion
        qhash = question_hash(query)

        cached = await asyncio.to_thread(load_cached, owner, qhash, ctxs)
        out["verdicts"].update(cached)
        out["cached"] = len(cached)

        todo = [cid for cid in call_ids if cid in ctxs and cid not in cached]
        # A call with neither transcript nor stored analysis has nothing to
        # judge; spending a request to be told "no" helps nobody. These are
        # decided in code, so they must NOT count as evidence the model ran.
        for cid in list(todo):
            c = ctxs[cid]
            if not (c.get("transcript") or c.get("summary") or c.get("customer_intent")):
                out["verdicts"][cid] = {
                    "call_id": cid, "match": False, "confidence": 1.0,
                    "evidence": "", "reason": "no transcript or analysis on this call",
                    "checks": {}}
                todo.remove(cid)

        deadline = time.monotonic() + config.FILTER_BUDGET_S
        sem = asyncio.Semaphore(max(1, config.FILTER_CONCURRENCY))
        fresh: list[tuple[dict, str]] = []      # verdicts worth caching
        judged: list[int] = []                  # every call the model answered
        skipped: list[int] = []

        async def _one(cid: int):
            ctx = ctxs[cid]
            async with sem:
                if time.monotonic() >= deadline:
                    skipped.append(cid)
                    return
                try:
                    v = await asyncio.wait_for(
                        judge_call(client, criterion, ctx),
                        timeout=max(1, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    skipped.append(cid)
                    return
                except Exception as exc:
                    log.warning("judge failed for call %s: %r", cid, exc)
                    skipped.append(cid)
                    return
            out["verdicts"][cid] = v
            judged.append(cid)
            if not _is_judge_failure(v):
                fresh.append((v, context_sha(ctx)))

        await asyncio.gather(*(_one(cid) for cid in todo))

        if fresh:
            await asyncio.to_thread(store_verdicts, owner, query, qhash, fresh)
        # Honest only if the model actually decided something, or there was
        # nothing for it to decide. Testing `out["verdicts"]` instead was wrong:
        # the contentless calls above put rows in there without the judge ever
        # running, so a pass in which every single request failed could still
        # report itself verified and pass off "0 matches" as a checked answer.
        out["verified"] = bool(judged) or bool(cached) or not todo
        out["examined"] = len(out["verdicts"])
        out["truncated"] = bool(skipped)
        out["unexamined"] = sorted(skipped)
        return out
    except Exception as exc:
        log.warning("verification pass failed (%r) — falling back to retrieval", exc)
        return out
    finally:
        if own_client:
            await client.close()


def load_evidence(owner: str, query: str, call_ids: list[int]) -> dict[int, dict]:
    """The verdicts a filter recorded for these calls, newest first per call.

    Looked up by the QUERY TEXT rather than the query hash so the export path
    does not have to re-plan the criterion (a second planner call, and a second
    chance to plan it differently) just to reprint evidence the filter already
    produced. Only matches are returned: a non-match is not why a row is in a
    hand-picked selection.
    """
    query = " ".join((query or "").split())
    if not (query and call_ids):
        return {}
    try:
        with get_db() as db:
            rows = [dict(r) for r in db.execute(
                """SELECT DISTINCT ON (call_id)
                          call_id, confidence, evidence, reason, created_at
                     FROM intel_filter_verdicts
                    WHERE owner = ? AND call_id = ANY(?) AND is_match = true
                      AND lower(query) = lower(?)
                    ORDER BY call_id, created_at DESC""",
                (owner, list(call_ids), query),
            ).fetchall()]
    except Exception as exc:
        log.warning("evidence lookup failed: %r", exc)
        return {}
    return {r["call_id"]: r for r in rows}
