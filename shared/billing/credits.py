"""Admin-granted minute credits — "reward this tenant with N minutes for M months".

Why this is a top-up and not a new thing
---------------------------------------
`minute_topups` already means "extra minutes on top of the plan's allowance,
for a bounded window", and `entitlements.get_entitlements()` already folds
active rows into `minutes_limit`. Every gate, meter and invoice reads that one
number. A separate "credits" pool would have to be taught to all of them, and
the first place someone forgot would be the place that gave minutes away for
free. So a grant is a `minute_topups` row with `source='admin_credit'`,
`amount_paise=0`, and no gateway order.

The one thing a grant needs that a purchase never did: A POOL THAT DRAINS
-------------------------------------------------------------------------
A paid top-up expires with the billing period that sold it, so "add N to this
period's ceiling" is exact — there is only ever one period. Validity measured
in MONTHS breaks that: `minutes_limit` is recomputed as `included + topup`
every period, so a 1000-minute / 3-month reward would be worth 3000 minutes.
The pool would silently refill at every rollover.

So the pool is drained, one closed calendar month at a time:

    consumed(month) = max(0, minutes_used(month) - minutes_included)

i.e. only what the tenant spent BEYOND their own plan allowance is charged to
the reward — which is exactly what the reward paid for. The live period is
never settled: its overspend is already bounded by `included + remaining`,
and settling it would double-count.

Two honest limits, both deliberate:

  * The grid is the IST calendar month, and the allowance charged against it is
    the tenant's CURRENT `minutes_included`. Historical period boundaries and
    historical plan sizes are not recorded anywhere in this schema, so a plan
    change mid-grant settles against the new size. The error is bounded by one
    plan step on a free reward.
  * Settlement is idempotent and runs from the billing sync worker. If it never
    ran, a grant would behave like today's top-up (refilling monthly) — so it
    is wired into `sync.run_once()`, not left to a cron someone can forget.
"""

from __future__ import annotations

import calendar
import datetime as dt
import logging

from shared.billing.entitlements import (
    as_dt, get_entitlements, ist_month_bounds, minutes_used, now_utc,
)

log = logging.getLogger("billing.credits")

SOURCE = "admin_credit"

# Bounds on what one grant may be. Not arbitrary: 60 months is longer than any
# plan we sell, and a five-digit minute grant is already ~2 months of a Growth
# allowance — anything larger is a typo, and a typo here gives away money.
MAX_GRANT_MINUTES = 100_000
MAX_GRANT_MONTHS = 60

# How far back settlement will walk in one pass. A grant cannot be older than
# MAX_GRANT_MONTHS, and the loop is bounded anyway; this is the seatbelt that
# stops a corrupt timestamp turning a worker pass into an infinite loop.
_MAX_SETTLE_MONTHS = MAX_GRANT_MONTHS + 2


class CreditError(ValueError):
    """A grant the operator asked for that we refuse to make."""


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------

def add_months(when: dt.datetime, months: int) -> dt.datetime:
    """`when` plus `months` calendar months, clamping the day.

    31 Jan + 1 month is 28/29 Feb, not 3 March. Using timedelta(days=30*n)
    instead would make "3 months" mean 90 days and drift a grant's expiry off
    the date the operator was told it would end.
    """
    year = when.year + (when.month - 1 + months) // 12
    month = (when.month - 1 + months) % 12 + 1
    day = min(when.day, calendar.monthrange(year, month)[1])
    return when.replace(year=year, month=month, day=day)


def _next_month(start: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """The IST calendar month AFTER the one starting at `start`."""
    return ist_month_bounds(start + dt.timedelta(days=40))


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------

def _shape(row: dict) -> dict:
    """One grant as the API and console show it."""
    minutes = int(row.get("minutes") or 0)
    consumed = int(row.get("minutes_consumed") or 0)
    return {
        "id": int(row["id"]),
        "owner": row.get("owner"),
        "minutes": minutes,
        "minutes_consumed": consumed,
        "minutes_remaining": max(0, minutes - consumed),
        "status": row.get("status"),
        "source": row.get("source"),
        "granted_by": row.get("granted_by"),
        "note": row.get("note"),
        "period_start": row.get("period_start"),
        "period_end": row.get("period_end"),
        "settled_through": row.get("settled_through"),
        "created_at": row.get("created_at"),
    }


def list_grants(owner: str, limit: int = 50) -> list[dict]:
    """Every admin credit for `owner`, newest first — live, spent and revoked.

    Expired and revoked rows are included on purpose: "why does this account
    have 500 extra minutes" and "why did the extra minutes stop" are the same
    question, and only the full history answers both.
    """
    from shared.db.database import get_db
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM minute_topups WHERE owner=? AND source=? "
            "ORDER BY id DESC LIMIT ?",
            ((owner or "").strip().lower(), SOURCE, max(1, min(limit, 500)))).fetchall()
    return [_shape(dict(r)) for r in rows]


def live_balance(owner: str) -> dict:
    """What the tenant's reward pool is worth right now."""
    grants = [g for g in list_grants(owner)
              if g["status"] == "active"
              and (as_dt(g["period_end"]) or now_utc()) > now_utc()]
    return {
        "grants": len(grants),
        "minutes_granted": sum(g["minutes"] for g in grants),
        "minutes_remaining": sum(g["minutes_remaining"] for g in grants),
        "expires_at": min((g["period_end"] for g in grants if g["period_end"]),
                          default=None),
    }


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------

def grant_minutes(owner: str, minutes: int, valid_months: int,
                  actor: str = "admin", note: str = "") -> dict:
    """Reward `owner` with `minutes` extra minutes, valid `valid_months` months.

    Takes effect on the tenant's very next call: `get_entitlements()` reads the
    live top-up sum, it is not cached in the entitlements row.

    The account must EXIST. Granting to a typo'd address would create minutes
    nobody can spend and, worse, would look in the console exactly like a grant
    that worked.
    """
    owner = (owner or "").strip().lower()
    if not owner:
        raise CreditError("an account is required")
    try:
        minutes = int(minutes)
        valid_months = int(valid_months)
    except (TypeError, ValueError):
        raise CreditError("minutes and validity must be whole numbers")
    if not 1 <= minutes <= MAX_GRANT_MINUTES:
        raise CreditError(f"minutes must be between 1 and {MAX_GRANT_MINUTES:,}")
    if not 1 <= valid_months <= MAX_GRANT_MONTHS:
        raise CreditError(f"validity must be between 1 and {MAX_GRANT_MONTHS} months")

    from shared.db.database import get_db
    start = now_utc()
    end = add_months(start, valid_months)
    with get_db() as db:
        known = db.execute(
            "SELECT 1 FROM users WHERE lower(email)=? "
            "UNION ALL SELECT 1 FROM entitlements WHERE owner=? LIMIT 1",
            (owner, owner)).fetchone()
        if not known:
            raise CreditError(f"no account {owner!r} on this platform")
        row = db.execute(
            "INSERT INTO minute_topups (owner, minutes, amount_paise, status, "
            "source, granted_by, note, period_start, period_end, settled_through) "
            "VALUES (?, ?, 0, 'active', ?, ?, ?, ?, ?, ?) RETURNING *",
            (owner, minutes, SOURCE, actor or "admin", (note or "")[:500],
             start, end, start)).fetchone()
        grant = _shape(dict(row))
    log.info("[credits] granted %s min to %s for %s month(s) by %s (grant %s)",
             minutes, owner, valid_months, actor, grant["id"])
    return grant


def revoke_grant(grant_id: int, actor: str = "admin", reason: str = "") -> dict:
    """Withdraw a grant. Minutes ALREADY SPENT are not clawed back.

    Only an `active` grant can be revoked; a spent or expired one is left
    exactly as it is so the history stays truthful about what was given.
    """
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute("SELECT * FROM minute_topups WHERE id=? AND source=?",
                         (int(grant_id), SOURCE)).fetchone()
        if row is None:
            raise CreditError("no such credit grant")
        row = dict(row)
        if row.get("status") != "active":
            raise CreditError(f"grant {grant_id} is already {row.get('status')}")
        note = (row.get("note") or "")
        if reason:
            note = (note + " | " if note else "") + f"revoked: {reason}"
        out = db.execute(
            "UPDATE minute_topups SET status='revoked', note=?, granted_by=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=? RETURNING *",
            (note[:500], f"{row.get('granted_by') or ''}/revoked-by:{actor}"[:200],
             int(grant_id))).fetchone()
    log.info("[credits] grant %s revoked by %s (%s)", grant_id, actor, reason or "-")
    return _shape(dict(out))


# ---------------------------------------------------------------------------
# settlement — what makes a multi-month grant a pool instead of a subscription
# ---------------------------------------------------------------------------

def settle_owner(owner: str) -> dict:
    """Charge closed months against `owner`'s open grants. Idempotent.

    Cheap and early-exits: an owner with no multi-month grant does no work at
    all, and a grant younger than a calendar month has nothing to settle.
    """
    owner = (owner or "").strip().lower()
    out = {"owner": owner, "months": 0, "minutes_charged": 0, "grants_closed": 0}
    if not owner:
        return out

    from shared.db.database import get_db
    now = now_utc()
    cur_start, _ = ist_month_bounds(now)

    ent = get_entitlements(owner)
    included = ent.get("minutes_included")
    if included is None:
        # Unlimited plan: there is no overspend, so nothing can be charged to
        # a reward. (get_entitlements() does not add top-ups here either.)
        return out
    included = int(included or 0)

    with get_db() as db:
        grants = [dict(r) for r in db.execute(
            "SELECT * FROM minute_topups WHERE owner=? AND source=? "
            "AND status='active' AND minutes_consumed < minutes ORDER BY id",
            (owner, SOURCE)).fetchall()]
        if not grants:
            return out

        # Walk from the earliest month any open grant has not yet been settled
        # for, up to (never including) the live one.
        marks = [as_dt(g.get("settled_through")) or as_dt(g.get("period_start"))
                 for g in grants]
        marks = [m for m in marks if m]
        if not marks:
            return out
        m_start, m_end = ist_month_bounds(min(marks))

        charged_total = 0
        guard = 0
        while m_end <= cur_start and guard < _MAX_SETTLE_MONTHS:
            guard += 1
            eligible = [
                g for g in grants
                if int(g["minutes"]) > int(g["minutes_consumed"] or 0)
                and (as_dt(g.get("settled_through")) or dt.datetime.min.replace(
                    tzinfo=dt.timezone.utc)) < m_end
                and (as_dt(g.get("period_start")) or m_start) < m_end
                and (as_dt(g.get("period_end")) or m_end) > m_start
            ]
            if eligible:
                overflow = max(0, minutes_used(owner, m_start, m_end) - included)
                for g in eligible:                       # oldest grant drains first
                    if overflow <= 0:
                        break
                    room = int(g["minutes"]) - int(g["minutes_consumed"] or 0)
                    take = min(room, overflow)
                    g["minutes_consumed"] = int(g["minutes_consumed"] or 0) + take
                    overflow -= take
                    charged_total += take
                out["months"] += 1
            # Every grant we considered is settled for this month, including the
            # ones that took nothing — otherwise the next pass re-walks it.
            for g in grants:
                if (as_dt(g.get("period_start")) or m_start) < m_end:
                    g["settled_through"] = m_end
            m_start, m_end = _next_month(m_start)

        closed = 0
        for g in grants:
            consumed = int(g["minutes_consumed"] or 0)
            spent = consumed >= int(g["minutes"])
            if spent:
                closed += 1
            db.execute(
                "UPDATE minute_topups SET minutes_consumed=?, settled_through=?, "
                "status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (consumed, g["settled_through"], "spent" if spent else "active",
                 int(g["id"])))

    out["minutes_charged"] = charged_total
    out["grants_closed"] = closed
    if charged_total or closed:
        log.info("[credits] settled %s: %s min charged over %s month(s), "
                 "%s grant(s) exhausted", owner, charged_total, out["months"], closed)
    return out


def settle_all() -> dict:
    """Settle every owner holding an open grant. The sync worker's entry point."""
    from shared.db.database import get_db
    with get_db() as db:
        owners = [r["owner"] for r in db.execute(
            "SELECT DISTINCT owner FROM minute_topups WHERE source=? "
            "AND status='active' AND minutes_consumed < minutes",
            (SOURCE,)).fetchall()]
    total = {"credit_owners_settled": 0, "credit_minutes_charged": 0,
             "credit_grants_spent": 0}
    for owner in owners:
        try:
            res = settle_owner(owner)
        except Exception:
            log.exception("[credits] settlement failed for %s", owner)
            continue
        total["credit_owners_settled"] += 1
        total["credit_minutes_charged"] += res["minutes_charged"]
        total["credit_grants_spent"] += res["grants_closed"]
    return total
