"""Fixed-window rate limiting for the public API.

Every request is counted against TWO subjects, each in a one-minute and a
one-day window:

    key:<key_id>        this integration's own budget
    acct:<owner>        everything the account is doing, across all its keys

The account subject is not optional book-keeping — it is what makes the
operator's ceiling a ceiling. With per-key counters alone, a tenant allowed
60 req/min could mint five keys and take 300, and the number in the admin
console would describe nothing anybody was actually held to.

Fixed windows rather than a token bucket because the numbers have to be
legible: an operator reading the admin console, a customer reading
`X-RateLimit-Remaining`, and the limiter itself must all be talking about the
same count over the same interval.

The counter is incremented BEFORE the verdict, so a client that keeps hammering
after a 429 keeps its window pinned. That is intentional — the limit protects a
2-core VM that is also carrying live calls, and a refused request still cost us
a database round trip.

State lives in Postgres, not in process memory: `docker restart lescall-api`
must not hand every caller a fresh budget.
"""

from __future__ import annotations

import datetime as dt
import logging
import random

logger = logging.getLogger("public_api.ratelimit")

# ---------------------------------------------------------------------------
# In-process refusal cache.
#
# Without it, a client hammering an exhausted budget costs five database
# statements PER REFUSED REQUEST — four window upserts and a key touch — on a
# 2-core box that is also carrying live calls. The limit would be doing its job
# for the caller while doing the opposite for us.
#
# Once a subject is known to be over a window, refusals are served from memory
# until that window rolls. Postgres stays authoritative: this only ever
# short-circuits a decision the database already made, and never admits a
# request the database would have refused.
# ---------------------------------------------------------------------------
_blocked: dict[str, tuple] = {}      # subject -> (cache_until, reset_at, kind, limit, used, scope)
_BLOCKED_MAX = 10_000

# How long a refusal may be served from memory. Deliberately capped well below
# a day window's own lifetime: an operator who RAISES a limit to unblock a
# customer must see that take effect in under a minute, not at midnight UTC.
# The verdict handed back still carries the true reset time, so Retry-After
# stays honest.
_CACHE_TTL_S = 30


def _remember_block(subject: str, verdict: "RateVerdict", now: dt.datetime) -> None:
    if len(_blocked) >= _BLOCKED_MAX:        # pathological; drop the oldest half
        for stale in sorted(_blocked, key=lambda s: _blocked[s][0])[:_BLOCKED_MAX // 2]:
            _blocked.pop(stale, None)
    cache_until = min(verdict.reset_at, now + dt.timedelta(seconds=_CACHE_TTL_S))
    _blocked[subject] = (cache_until, verdict.reset_at, verdict.kind,
                         verdict.limit, verdict.used, verdict.scope)


def _cached_block(subjects: list[str], now: dt.datetime) -> "RateVerdict | None":
    worst = None
    for subject in subjects:
        hit = _blocked.get(subject)
        if not hit:
            continue
        cache_until, reset_at, kind, limit, used, scope = hit
        if cache_until <= now:
            _blocked.pop(subject, None)
            continue
        verdict = RateVerdict(False, kind, limit, used, reset_at, scope, cached=True)
        if worst is None or verdict.reset_at > worst.reset_at:
            worst = verdict
    return worst


def reset_cache() -> None:
    """Test hook — this is process-local state that outlives a fixture."""
    _blocked.clear()
    _auth_failures.clear()
    from shared.public_api import policy as _policy
    _policy.invalidate()


# ---------------------------------------------------------------------------
# Failed-authentication throttle.
#
# An invalid key costs one indexed SELECT, which is cheap but not free, and a
# scanner walking key ids gets unlimited attempts. Twenty failures from one
# address in a minute is not a misconfigured integration; it is somebody
# trying. Blocked for the remainder of that minute only, so a genuine
# fat-fingered deploy recovers on its own.
# ---------------------------------------------------------------------------
_auth_failures: dict[str, tuple[dt.datetime, int]] = {}
AUTH_FAILURE_LIMIT = 20


def note_auth_failure(ip: str) -> None:
    if not ip:
        return
    window = window_starts()["minute"]
    start, count = _auth_failures.get(ip, (window, 0))
    if start != window:
        start, count = window, 0
    _auth_failures[ip] = (start, count + 1)
    if len(_auth_failures) > _BLOCKED_MAX:
        for stale in [k for k, v in _auth_failures.items() if v[0] != window]:
            _auth_failures.pop(stale, None)


def auth_failures_exceeded(ip: str) -> bool:
    if not ip:
        return False
    start, count = _auth_failures.get(ip, (None, 0))
    return start == window_starts()["minute"] and count >= AUTH_FAILURE_LIMIT

# Pruned opportunistically so the table cannot grow without bound and no cron
# job has to exist for it. Minute windows are enforcement state and worthless
# once elapsed; DAY windows are also the usage history the dashboards render,
# so they outlive their enforcement role by a long way.
_MINUTE_RETENTION_DAYS = 3
_DAY_RETENTION_DAYS = 120
_PRUNE_ODDS = 0.002          # ~1 request in 500


def key_subject(key_id: str) -> str:
    return f"key:{key_id}"


def account_subject(owner: str) -> str:
    return f"acct:{(owner or '').strip().lower()}"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def window_starts(now: dt.datetime | None = None) -> dict[str, dt.datetime]:
    now = now or _now()
    return {
        "minute": now.replace(second=0, microsecond=0),
        "day": now.replace(hour=0, minute=0, second=0, microsecond=0),
    }


def _reset_at(kind: str, start: dt.datetime) -> dt.datetime:
    return start + (dt.timedelta(minutes=1) if kind == "minute" else dt.timedelta(days=1))


class RateVerdict:
    """What the limiter decided, plus the headers to say so."""

    def __init__(self, allowed: bool, kind: str, limit: int, used: int,
                 reset_at: dt.datetime, scope: str = "key", cached: bool = False):
        self.allowed = allowed
        # True when this verdict came from the in-process cache, i.e. no
        # database work was done for it. Callers use it to skip their own
        # writes as well — see keys._touch.
        self.cached = cached
        self.kind = kind
        self.limit = limit
        self.used = used
        self.reset_at = reset_at
        self.scope = scope          # "key" | "account" — which budget bound this

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def retry_after(self) -> int:
        return max(1, int((self.reset_at - _now()).total_seconds()))

    def headers(self) -> dict[str, str]:
        h = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(int(self.reset_at.timestamp())),
            "X-RateLimit-Window": self.kind,
            "X-RateLimit-Scope": self.scope,
        }
        if not self.allowed:
            h["Retry-After"] = str(self.retry_after)
        return h


def consume(budgets: list[tuple[str, str, int, int]],
            now: dt.datetime | None = None) -> RateVerdict:
    """Count one request against every budget and return the binding verdict.

    `budgets` is a list of `(scope, subject, per_min, per_day)`. All of them are
    incremented — a request that is refused by one budget still consumed the
    others, which is what stops a client from probing for whichever counter is
    coldest.

    Fails OPEN on a database error. A limiter that turns a Postgres hiccup into
    a total API outage for every integrated CRM is a worse failure than briefly
    unmetered traffic — and nginx's own `limit_req` on /v1/ is still underneath
    us as a coarse floor.
    """
    starts = window_starts(now)

    cached = _cached_block([b[1] for b in budgets], now or _now())
    if cached is not None:
        return cached

    counts: dict[tuple[str, str], int] = {}
    try:
        from shared.db.database import get_db
        with get_db() as db:
            for _scope, subject, _pm, _pd in budgets:
                for kind, start in starts.items():
                    row = db.execute(
                        "INSERT INTO api_rate_windows (subject, window_kind, window_start, hits) "
                        "VALUES (?,?,?,1) "
                        "ON CONFLICT (subject, window_kind, window_start) "
                        "DO UPDATE SET hits = api_rate_windows.hits + 1 "
                        "RETURNING hits",
                        (subject, kind, start)).fetchone()
                    counts[(subject, kind)] = int(dict(row)["hits"]) if row else 1
    except Exception as exc:
        logger.error("[api] rate window write failed: %r — failing open", exc)
        fallback = budgets[0] if budgets else ("key", "", 60, 20000)
        return RateVerdict(True, "minute", fallback[2], 0,
                           _reset_at("minute", starts["minute"]), fallback[0])

    if random.random() < _PRUNE_ODDS:
        prune()

    # A refusal wins over everything, and among refusals the one that resets
    # LAST is the honest Retry-After — telling a client to come back in 20s
    # when a daily budget is exhausted just buys another 429.
    refusals = []
    for scope, subject, per_min, per_day in budgets:
        for kind, limit in (("minute", per_min), ("day", per_day)):
            used = counts.get((subject, kind), 0)
            if used > limit:
                refusals.append(RateVerdict(False, kind, limit, used,
                                            _reset_at(kind, starts[kind]), scope))
    if refusals:
        worst = max(refusals, key=lambda v: v.reset_at)
        stamp = now or _now()
        for verdict in refusals:
            subject = next(b[1] for b in budgets if b[0] == verdict.scope)
            _remember_block(subject, verdict, stamp)
        return worst

    # Nothing refused: report the window closest to its ceiling, so the headers
    # a client reads describe the constraint that will actually stop them next.
    def _pressure(item):
        scope, subject, per_min, per_day = item[0]
        kind = item[1]
        limit = per_min if kind == "minute" else per_day
        return counts.get((subject, kind), 0) / max(1, limit)

    candidates = [(b, k) for b in budgets for k in ("minute", "day")]
    budget, kind = max(candidates, key=_pressure)
    scope, subject, per_min, per_day = budget
    limit = per_min if kind == "minute" else per_day
    return RateVerdict(True, kind, limit, counts.get((subject, kind), 0),
                       _reset_at(kind, starts[kind]), scope)


def usage(key_ids: list[str]) -> dict[str, dict]:
    """Current-minute and today's counts per key, for the UIs. Read-only."""
    if not key_ids:
        return {}
    starts = window_starts()
    subjects = {key_subject(k): k for k in key_ids}
    out = {k: {"minute": 0, "day": 0} for k in key_ids}
    try:
        from shared.db.database import get_db
        placeholders = ",".join("?" for _ in subjects)
        with get_db() as db:
            rows = db.execute(
                f"SELECT subject, window_kind, hits FROM api_rate_windows "
                f" WHERE subject IN ({placeholders}) "
                f"   AND ((window_kind='minute' AND window_start=?) "
                f"     OR (window_kind='day' AND window_start=?))",
                (*subjects, starts["minute"], starts["day"])).fetchall()
        for r in rows:
            row = dict(r)
            key_id = subjects.get(row["subject"])
            if key_id:
                out[key_id][row["window_kind"]] = int(row["hits"])
    except Exception as exc:
        logger.warning("[api] usage read failed: %r", exc)
    return out


def account_usage(owner: str) -> dict:
    """The account's own counters — the ones the ceiling is actually enforced
    against, which is what a tenant near their limit needs to see."""
    starts = window_starts()
    out = {"minute": 0, "day": 0}
    try:
        from shared.db.database import get_db
        with get_db() as db:
            rows = db.execute(
                "SELECT window_kind, hits FROM api_rate_windows "
                " WHERE subject=? AND ((window_kind='minute' AND window_start=?) "
                "   OR (window_kind='day' AND window_start=?))",
                (account_subject(owner), starts["minute"], starts["day"])).fetchall()
        for r in rows:
            row = dict(r)
            out[row["window_kind"]] = int(row["hits"])
    except Exception as exc:
        logger.warning("[api] account usage read failed: %r", exc)
    return out


def daily_history(owner: str, days: int = 14) -> list[dict]:
    """Per-day request totals for one account — the "is this client actually
    using it" answer, read from the account window so it counts every key
    including ones since revoked."""
    cutoff = window_starts()["day"] - dt.timedelta(days=days)
    try:
        from shared.db.database import get_db
        with get_db() as db:
            rows = db.execute(
                "SELECT window_start, hits FROM api_rate_windows "
                " WHERE window_kind='day' AND subject=? AND window_start >= ? "
                " ORDER BY window_start DESC",
                (account_subject(owner), cutoff)).fetchall()
        return [{"day": str(dict(r)["window_start"])[:10], "hits": int(dict(r)["hits"])}
                for r in rows]
    except Exception as exc:
        logger.warning("[api] daily history read failed: %r", exc)
        return []


def prune() -> int:
    now = _now()
    try:
        from shared.db.database import get_db
        with get_db() as db:
            db.execute("DELETE FROM api_rate_windows WHERE window_kind='minute' "
                       " AND window_start < ?",
                       (now - dt.timedelta(days=_MINUTE_RETENTION_DAYS),))
            db.execute("DELETE FROM api_rate_windows WHERE window_kind='day' "
                       " AND window_start < ?",
                       (now - dt.timedelta(days=_DAY_RETENTION_DAYS),))
            db.execute("DELETE FROM api_idempotency WHERE created_at < ?",
                       (now - dt.timedelta(days=_MINUTE_RETENTION_DAYS),))
        return 1
    except Exception as exc:
        logger.warning("[api] prune failed: %r", exc)
        return 0
