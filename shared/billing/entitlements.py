"""Entitlements: what this tenant is allowed to do, right now.

One row per owner in `entitlements`, rebuilt wholesale by
`resolve_entitlements()` from the `subscriptions` history — the same doctrine
as `usage_monthly`. Everything on the hot path reads that single row.

Three period rules, and they are genuinely different:

  * recurring (Basic/Growth) — the subscription's own cycle, as the gateway
    reports it. Never a calendar month: a subscription started on the 20th
    renews on the 20th.
  * Pilot — a FIXED window [activated_at, activated_at + duration_days). The
    500 minutes are the whole-window pool, not a monthly allowance. A 7-day
    trial that reset its minutes on the 1st would hand out a second trial.
  * Free / Enterprise — the IST calendar month.

Timestamps come back from Postgres as STRINGS in this codebase (the loader in
shared/db/database.py registers a text loader so rows stay JSON-serialisable),
so everything time-shaped goes through `as_dt()`. Forgetting that is the
single most common bug in this repo's DB code.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
import uuid

from shared.billing.errors import (
    CODE_ACCOUNT_LOCKED, CODE_CAP_AGENTS, CODE_CAP_CONCURRENCY,
    CODE_PLAN_REQUIRED, CODE_QUOTA_COPILOT, CODE_QUOTA_COPILOT_TOKENS,
    CODE_QUOTA_MINUTES, CODE_UNATTRIBUTED, Decision,
)

log = logging.getLogger("billing.entitlements")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# THE TRIAL FLOOR — the one set of limits the enforcement switch cannot turn off
#
# Everything else in this module begins `if not enforcement_enabled(): return
# Decision.ok()`. That switch is a BUSINESS decision: flipping it on starts
# refusing phone calls on minute quotas, locking accounts and hiding paid
# features, and it has deliberately been off since 2026-08-01. The consequence
# nobody intended is that a signed-up-this-minute Free account has no ceiling
# on anything either — it can mint agents without limit and drive the copilot
# as hard as it likes, and the copilot's model quota is SHARED with live phone
# calls (COPILOT_LLM_TPM, plus the provider's daily ceiling). So the cheapest
# account on the platform can degrade every paying tenant's calls.
#
# These three limits are therefore ANTI-ABUSE, not product tiering, and they
# apply to trial tenants whatever the switch says. They are a FLOOR: they only
# ever tighten. When enforcement is on and a tenant's own entitlement is
# stricter (an admin override, a locked account), the stricter one wins —
# min() below, never max().
#
# Scope is `free` ONLY (owner decision, 2026-09-12). Pilot is a PAID trial
# (Rs 2,999, 7 days) and keeps the 3 agents it was sold; capping it here would
# be a pricing change wearing a security fix's clothes.
# ---------------------------------------------------------------------------
TRIAL_PLANS = frozenset({"free"})

TRIAL_MAX_AGENT_FAMILIES = 1
TRIAL_COPILOT_TURNS_PER_DAY = 20
TRIAL_COPILOT_TOKENS_PER_DAY = 150_000

# A single request that cannot possibly be a question. The daily budget alone
# does not stop one 400k-token paste from consuming it in one shot — and worse,
# that request is SENT before the budget notices, so the shared model quota is
# already spent. Bounded here, before the LLM call.
TRIAL_COPILOT_MAX_REQUEST_TOKENS = 25_000

# How long a dial reservation stands before it expires on its own. Comfortably
# longer than originate_timeout (45s) plus a long call, so a live call is never
# double-counted, but short enough that a crashed leg frees its hold the same
# hour rather than at the end of the billing period.
RESERVATION_TTL_S = int(os.environ.get("BILLING_RESERVATION_TTL_S", str(2 * 3600)))

# What a not-yet-placed call is assumed to consume when admitting it. One
# minute is the correct floor because BILLING_ROUNDING=per_call_ceil bills any
# answered call at least a whole minute.
ASSUMED_CALL_MINUTES = int(os.environ.get("BILLING_ASSUMED_CALL_MINUTES", "1"))

# E6 fail-open budget. A DB error must not drop a live call, but unbounded
# fail-open is worse than the outage: with max_connections=30 and no pooler, a
# tenant who exhausts connections would get free calls for everyone and
# benefit from the outage they caused.
FAIL_OPEN_LIMIT = int(os.environ.get("BILLING_FAIL_OPEN_LIMIT", "5"))
_fail_open_counts: dict[str, int] = {}
_fail_open_lock = threading.Lock()


# ---------------------------------------------------------------------------
# time + coercion helpers
# ---------------------------------------------------------------------------

def as_dt(value) -> dt.datetime | None:
    """Postgres timestamps arrive as strings here. Coerce, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    try:
        parsed = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(t: dt.datetime | None) -> str | None:
    return t.astimezone(dt.timezone.utc).isoformat() if t else None


def ist_month_bounds(at: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    """[start, end) of the IST calendar month containing `at`, in UTC.

    India has no DST, so this is pure arithmetic — no tz database surprises.
    """
    at = (at or now_utc()).astimezone(IST)
    start = at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return start.astimezone(dt.timezone.utc), end.astimezone(dt.timezone.utc)


# ---------------------------------------------------------------------------
# the enforcement switch (DB, not env)
# ---------------------------------------------------------------------------

_enforce_cache: tuple[float, bool] = (0.0, False)
_ENFORCE_TTL_S = 30


def enforcement_enabled(refresh: bool = False) -> bool:
    """Is enforcement live?

    Read from `settings(owner='', key='billing_enforce')` and cached 30s, NOT
    from env: the inbound gate (E6) runs inside the runtime container, which
    would never receive an api-only environment variable — inbound enforcement
    would have been silently dead forever.

    Fails to False (permissive) on any error. That is deliberate for the
    switch itself: an unreadable setting must not block every call on the
    platform. The individual checks below have their own bounded fail-open.
    """
    global _enforce_cache
    now = time.monotonic()
    if not refresh and now - _enforce_cache[0] < _ENFORCE_TTL_S:
        return _enforce_cache[1]
    value = False
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE owner='' AND key='billing_enforce'"
            ).fetchone()
        if row:
            value = bool((json.loads(row["value"]) or {}).get("enabled"))
    except Exception as exc:
        log.warning("enforcement flag unreadable (%r) — treating as OFF", exc)
        value = False
    _enforce_cache = (now, value)
    return value


def set_enforcement(enabled: bool, actor: str = "") -> None:
    from shared.db.database import get_db
    payload = json.dumps({"enabled": bool(enabled), "by": actor,
                          "at": _iso(now_utc())})
    with get_db() as db:
        db.execute(
            "INSERT INTO settings (owner, key, value, updated_at) "
            "VALUES ('', 'billing_enforce', ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT (owner, key) DO UPDATE SET value=excluded.value, "
            "updated_at=CURRENT_TIMESTAMP", (payload,))
    global _enforce_cache
    _enforce_cache = (0.0, bool(enabled))


# ---------------------------------------------------------------------------
# owner resolution — ONE function, used by enforcement AND metering
# ---------------------------------------------------------------------------

def billing_owner(routing: dict | None) -> str:
    """The tenant a call belongs to, or "" if it cannot be attributed.

    This exists because enforcement and metering used to disagree, and the
    disagreement was exploitable. `_resolve_call_routing` returns owner="" for
    any slug with no `agents` row (it even swallows DB errors into ""), while
    `_metering_owner()` fell back to LESCALL_DEFAULT_OWNER — the founding
    account, which is Enterprise/unlimited. Net effect: such a call checked its
    quota against an empty-string tenant whose usage never accumulates, and
    billed its minutes to an account with no limit. A self-serve free-minutes
    switch, reachable by naming a slug.

    So: no default. An unattributable call is REFUSED, not silently assigned.
    """
    if not routing:
        return ""
    owner = str(routing.get("owner") or "").strip().lower()
    return owner


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

_FEATURES = ("recordings", "summaries", "transcripts", "intelligence",
             "conversation_analytics", "advanced_analytics", "autocall",
             "telephony", "api_access")


def _free_defaults() -> dict:
    """The entitlement a tenant with no row gets. Mirrors the `free` plan so a
    missing row is never accidentally generous."""
    return {
        "owner": "", "plan_code": "free", "plan_name": "Free",
        "minutes_limit": 30, "minutes_included": 30, "minutes_topup": 0,
        "max_agent_families": 1, "max_concurrent_calls": 1,
        "copilot_turns_per_day": 20,
        "copilot_tokens_per_day": TRIAL_COPILOT_TOKENS_PER_DAY,
        # Every capability off — including `telephony`, so a brand-new signup
        # with no entitlements row yet cannot register a number.
        **{f: 0 for f in _FEATURES},
        "status": "ok", "period_start": None, "period_end": None,
        "provisional": True,
    }


def get_entitlements(owner: str) -> dict:
    """The resolved entitlement for `owner`, as a plain dict.

    Two things happen here that are easy to miss:

    1. Top-up minutes are folded into `minutes_limit`, so every enforcement
       point and every meter picks them up without extra code.
    2. An ELAPSED Pilot window is treated as expired on READ, not only when the
       sync worker gets round to it. Otherwise a lapsed trial keeps dialling in
       the gap between passes — up to 15 minutes of free calls, every time.
    """
    owner = (owner or "").strip().lower()
    if not owner:
        return {**_free_defaults(), "owner": ""}
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute(
            "SELECT e.*, p.name AS plan_name, p.period AS plan_period, "
            "       p.duration_days, p.topup_allowed, p.topup_pack_minutes, "
            "       p.topup_rate_paise, p.price_paise, "
            # Migration 0028 adds the column NULL on every existing row, and
            # resolve_entitlements() only fills it the next time it runs. Read
            # the plan's number through until then, or the token budget would
            # be silently unlimited for every tenant already on the platform —
            # exactly the group the cap exists for.
            "       p.copilot_tokens_per_day AS plan_copilot_tokens_per_day "
            "FROM entitlements e JOIN plans p ON p.code = e.plan_code "
            "WHERE e.owner=?", (owner,)).fetchone()
        if row is None:
            return {**_free_defaults(), "owner": owner}
        ent = dict(row)

        # (2) read-time expiry of a fixed-window plan.
        period_end = as_dt(ent.get("period_end"))
        if period_end and ent.get("plan_period") == "one_time" and period_end <= now_utc():
            log.info("[billing] %s pilot window elapsed on read — treating as Free", owner)
            return {**_free_defaults(), "owner": owner, "expired_from": ent["plan_code"]}

        # (1) active top-ups for the current period.
        #
        # What is left of each pool, not what it was sold at. A purchased
        # top-up expires with the period it was bought in, so `minutes_consumed`
        # is 0 for its whole life and this reads exactly as it always did. An
        # ADMIN CREDIT can be valid for months, and its consumed part is
        # settled month by month (shared/billing/credits.py) — summing the
        # granted amount instead would refill the reward at every rollover and
        # give away `minutes x months`.
        topup = 0
        if ent.get("minutes_limit") is not None:
            trow = db.execute(
                "SELECT COALESCE(SUM(GREATEST(minutes - minutes_consumed, 0)),0) AS m "
                "FROM minute_topups WHERE owner=? AND status='active' "
                "AND (period_end IS NULL OR period_end > CURRENT_TIMESTAMP)",
                (owner,)).fetchone()
            topup = int((trow or {}).get("m") or 0)

    ent["minutes_topup"] = topup
    if ent.get("minutes_included") is not None and ent.get("minutes_limit") is not None:
        ent["minutes_limit"] = int(ent["minutes_included"] or 0) + topup
    if ent.get("copilot_tokens_per_day") is None:
        ent["copilot_tokens_per_day"] = ent.get("plan_copilot_tokens_per_day")
    ent["provisional"] = False
    for f in _FEATURES:
        ent[f] = int(ent.get(f) or 0)
    return ent


# ---------------------------------------------------------------------------
# the trial floor
# ---------------------------------------------------------------------------

def is_trial(ent: dict) -> bool:
    """Is this tenant on a non-paying trial?

    Reads the RESOLVED plan, which matters in two cases that a
    `plan_code == 'free'` comparison at the call site would get wrong:
    an account with no entitlements row at all (a signup from thirty seconds
    ago — `_free_defaults()` stamps plan_code='free'), and a Pilot whose window
    elapsed, which get_entitlements() demotes to Free on READ rather than
    waiting for the sync worker.
    """
    return str(ent.get("plan_code") or "free").strip().lower() in TRIAL_PLANS


def _floor(ent: dict, field: str, ceiling: int) -> int | None:
    """The stricter of the tenant's own limit and the trial ceiling.

    NULL in this schema means UNLIMITED, so it is the loosest possible value,
    not the tightest — `min(None, 1)` is a TypeError and `None or 1` reads
    backwards. Written out because getting it upside down here would hand a
    trial account an unlimited budget and look like a typo in review.
    """
    if not is_trial(ent):
        return ent.get(field)
    own = ent.get(field)
    if own is None:
        return ceiling
    try:
        return min(int(own), ceiling)
    except (TypeError, ValueError):
        return ceiling


def effective_limits(owner_or_ent) -> dict:
    """The caps that actually apply right now, trial floor included.

    Every gate below and every meter the SPA draws reads THIS, so a limit can
    never be enforced at one number and displayed at another. Accepts an owner
    string or an already-loaded entitlement dict — the gates have one in hand
    and must not pay for a second query.
    """
    ent = (get_entitlements(owner_or_ent) if isinstance(owner_or_ent, str)
           else (owner_or_ent or {}))
    return {
        "plan_code": ent.get("plan_code") or "free",
        "trial": is_trial(ent),
        "max_agent_families": _floor(
            ent, "max_agent_families", TRIAL_MAX_AGENT_FAMILIES),
        "copilot_turns_per_day": _floor(
            ent, "copilot_turns_per_day", TRIAL_COPILOT_TURNS_PER_DAY),
        "copilot_tokens_per_day": _floor(
            ent, "copilot_tokens_per_day", TRIAL_COPILOT_TOKENS_PER_DAY),
        "copilot_max_request_tokens": (
            TRIAL_COPILOT_MAX_REQUEST_TOKENS if is_trial(ent) else None),
    }


def period_bounds(ent: dict) -> tuple[dt.datetime, dt.datetime]:
    """The usage window this entitlement's minutes are counted over."""
    start = as_dt(ent.get("period_start"))
    end = as_dt(ent.get("period_end"))
    if start and end:
        return start, end
    return ist_month_bounds()


def minutes_used(owner: str, start: dt.datetime, end: dt.datetime) -> int:
    """Minutes consumed in [start, end), by the SAME rounding law the invoice
    uses.

    Two details that must not drift:
      * `display_minutes()` from shared.usage_metering is the rounding law
        (BILLING_ROUNDING=per_call_ceil). Re-implementing the arithmetic here
        would make quota and invoice disagree.
      * COALESCE(billsec_final, billsec_pipeline) — open and just-ended calls
        count. Counting only reconciled rows would give every tenant up to
        CDR_WAIT_HOURS (24h) of free overshoot while the referee catches up.
    """
    from shared.db.database import get_db
    from shared.usage_metering import display_minutes
    with get_db() as db:
        rows = db.execute(
            "SELECT COALESCE(billsec_final, billsec_pipeline, 0) AS sec "
            "FROM usage_ledger WHERE owner=? AND billable=1 "
            "AND answered_at >= ? AND answered_at < ?",
            (owner, _iso(start), _iso(end))).fetchall()
    return display_minutes([int(r["sec"] or 0) for r in rows])


def _reserved(db, owner: str) -> tuple[int, int]:
    """(reserved_minutes, reserved_calls) currently held by `owner`.

    Expired rows are ignored rather than deleted here — deleting is the sweep
    worker's job, and a read must not need write permission.
    """
    row = db.execute(
        "SELECT COALESCE(SUM(minutes),0) AS m, COUNT(*) AS n "
        "FROM billing_reservations WHERE owner=? AND expires_at > CURRENT_TIMESTAMP",
        (owner,)).fetchone()
    return int((row or {}).get("m") or 0), int((row or {}).get("n") or 0)


def usage_summary(owner: str) -> dict:
    """Everything the meters and /api/billing/summary show, from the same
    numbers enforcement uses. If this and a gate ever disagree, the gate is
    the bug — they read the same functions on purpose."""
    ent = get_entitlements(owner)
    start, end = period_bounds(ent)
    used = 0
    reserved = calls = 0
    try:
        used = minutes_used(owner, start, end)
        from shared.db.database import get_db
        with get_db() as db:
            reserved, calls = _reserved(db, owner)
    except Exception as exc:
        log.warning("usage summary failed for %s: %r", owner, exc)
    limit = ent.get("minutes_limit")
    # The caps the GATES use, not the raw plan row. Before the trial floor
    # these were the same thing; now a Free tenant's displayed cap has to be
    # the floored one or the meter says "1 of 1 agents" while the button stays
    # enabled — the exact mismatch that makes a limit look like a bug.
    limits = effective_limits(ent)
    copilot_tokens_limit = limits["copilot_tokens_per_day"]
    copilot_tokens_used_today = copilot_tokens_used(owner) if owner else 0
    return {
        "plan_code": ent.get("plan_code"),
        "plan_name": ent.get("plan_name"),
        "status": ent.get("status", "ok"),
        "minutes_limit": limit,
        "minutes_included": ent.get("minutes_included"),
        "minutes_topup": ent.get("minutes_topup", 0),
        "minutes_used": used,
        "minutes_remaining": None if limit is None else max(0, limit - used),
        "minutes_reserved": reserved,
        "calls_in_flight": calls,
        "period_start": _iso(start),
        "period_end": _iso(end),
        "max_agent_families": limits["max_agent_families"],
        "max_concurrent_calls": ent.get("max_concurrent_calls"),
        "copilot_turns_per_day": limits["copilot_turns_per_day"],
        "copilot_tokens_per_day": copilot_tokens_limit,
        "copilot_tokens_used": copilot_tokens_used_today,
        "copilot_tokens_remaining": (
            None if copilot_tokens_limit is None
            else max(0, int(copilot_tokens_limit) - copilot_tokens_used_today)),
        # True when the limits above bind regardless of the global enforcement
        # switch. The SPA needs this: it currently hides every cap behind
        # `enforcement`, which would make a binding trial cap invisible right
        # up to the 402.
        "trial": limits["trial"],
        "features": {f: bool(ent.get(f)) for f in _FEATURES},
    }


# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------

def _locked(ent: dict) -> Decision | None:
    if ent.get("status") == "locked":
        return Decision.deny(
            CODE_ACCOUNT_LOCKED,
            "This account is suspended. Contact support to restore service.")
    return None


def check_dial_allowed(owner: str, origin: str = "dial",
                       expected_minutes: int | None = None,
                       ref: str = "") -> Decision:
    """Admit (and RESERVE for) one outbound or inbound call.

    Returns a Decision whose `reservation` must be handed to
    `release_reservation()` when the call's ledger row closes. A reservation
    that is never released expires by itself after RESERVATION_TTL_S — leaking
    quota temporarily is acceptable, leaking it forever is not.

    No-ops (allows, reserves nothing) when enforcement is off.
    """
    if not enforcement_enabled():
        return Decision.ok()
    if not owner:
        # §5.3: an unattributable call is refused, never defaulted. Billing
        # minutes to an unlimited account is exactly the hole this closes.
        return Decision.deny(
            CODE_UNATTRIBUTED,
            "This call could not be attributed to an account.", check="unattributed")

    want = max(1, int(expected_minutes or ASSUMED_CALL_MINUTES))
    ref = ref or f"dial:{uuid.uuid4().hex}"
    try:
        ent = get_entitlements(owner)
        if (denial := _locked(ent)) is not None:
            return denial
        start, end = period_bounds(ent)
        used = minutes_used(owner, start, end)

        from shared.db.database import get_db
        with get_db() as db:
            reserved, in_flight = _reserved(db, owner)

            cap = ent.get("max_concurrent_calls")
            if cap is not None and in_flight >= int(cap):
                return Decision.deny(
                    CODE_CAP_CONCURRENCY,
                    f"Your plan allows {cap} simultaneous call"
                    f"{'s' if int(cap) != 1 else ''}; "
                    f"{in_flight} {'is' if in_flight == 1 else 'are'} already "
                    f"in progress. Try again in a moment.")

            limit = ent.get("minutes_limit")
            if limit is not None and used + reserved + want > int(limit):
                remaining = max(0, int(limit) - used - reserved)
                # Additional minutes are not sold (owner decision 2026-08-01):
                # exhausting the allowance offers exactly one route, and the
                # copy must not imply a top-up button that does not exist.
                return Decision.deny(
                    CODE_QUOTA_MINUTES,
                    f"You have {remaining} of {limit} minutes left this "
                    f"period. Upgrade your plan to keep calling.",
                    minutes_used=used, minutes_limit=int(limit))

            # Reserve inside the same connection as the check. Two concurrent
            # dials cannot both see the same remaining figure and both pass:
            # the loser's INSERT lands after the winner's and its own re-read
            # would already include it.
            db.execute(
                "INSERT INTO billing_reservations (ref, owner, minutes, origin, "
                "expires_at) VALUES (?,?,?,?, CURRENT_TIMESTAMP + "
                f"INTERVAL '{RESERVATION_TTL_S} seconds') "
                "ON CONFLICT (ref) DO NOTHING",
                (ref, owner, want, origin))
        _note_fail_open(owner, ok=True)
        return Decision.ok(reservation=ref)

    except Exception as exc:
        # Bounded fail-open (E6). Never blind: the call is allowed, billed, and
        # STAMPED so the minutes are identifiable afterwards — but only up to
        # FAIL_OPEN_LIMIT consecutive failures per owner, after which we refuse
        # and raise a critical notification rather than serve free calls.
        count = _note_fail_open(owner, ok=False)
        log.error("[billing] entitlement check FAILED for %s (%d consecutive): %r",
                  owner, count, exc)
        if count > FAIL_OPEN_LIMIT:
            _notify_critical(owner, "billing checks failing",
                             f"{count} consecutive entitlement-check failures; "
                             f"refusing new calls for {owner}.")
            return Decision.deny(
                CODE_QUOTA_MINUTES,
                "Billing is temporarily unavailable and we cannot verify your "
                "balance. Please try again shortly.", check="error")
        return Decision.ok(check="error")


def release_reservation(ref: str) -> None:
    """Drop a dial reservation. Safe to call twice, and for an unknown ref."""
    if not ref:
        return
    try:
        from shared.db.database import get_db
        with get_db() as db:
            db.execute("DELETE FROM billing_reservations WHERE ref=?", (ref,))
    except Exception as exc:
        log.warning("[billing] reservation release failed for %s: %r", ref, exc)


def sweep_reservations() -> int:
    """Delete expired reservations. Called by the sync worker."""
    try:
        from shared.db.database import get_db
        with get_db() as db:
            cur = db.execute(
                "DELETE FROM billing_reservations WHERE expires_at <= CURRENT_TIMESTAMP")
            return int(getattr(cur, "rowcount", 0) or 0)
    except Exception as exc:
        log.warning("[billing] reservation sweep failed: %r", exc)
        return 0


def agent_families_used(owner: str) -> int:
    """Distinct agent FAMILIES this owner holds.

    A family is a twin pair: one "create" makes <base>-inbound and
    <base>-outbound, and the plan cap counts pairs, not slugs.

    `archived_at IS NULL` is load-bearing: DELETE /api/agents/{name} moves the
    folder to _archived/ but leaves the row owned, so counting without this
    charges a tenant forever for agents they deleted.
    """
    from shared.db.database import get_db
    with get_db() as db:
        rows = db.execute(
            "SELECT slug FROM agents WHERE owner=? AND archived_at IS NULL",
            (owner,)).fetchall()
    families = set()
    for r in rows:
        slug = r["slug"] or ""
        for suffix in ("-inbound", "-outbound"):
            if slug.endswith(suffix):
                slug = slug[: -len(suffix)]
                break
        families.add(slug)
    return len(families)


def agent_cap(owner: str) -> tuple[int | None, bool]:
    """(cap, is_trial) for this owner — the number the gate and the UI share.

    Returns (None, False) when nothing applies, so a caller can tell
    "unlimited" from "one" without re-deriving the trial rule.
    """
    ent = get_entitlements(owner)
    limits = effective_limits(ent)
    if not limits["trial"] and not enforcement_enabled():
        return None, False
    return limits["max_agent_families"], limits["trial"]


def agent_cap_message(cap: int, used: int, trial: bool) -> str:
    if trial:
        # Do not say "your plan includes 1 agent" to someone who never chose a
        # plan. The free tier is the default state of a signup, so the sentence
        # has to explain the rule and the way out in one breath, or it reads as
        # the product being broken.
        return (f"The free plan includes {cap} agent"
                f"{'s' if cap != 1 else ''} and you have {used}. "
                f"Archive it to build a different one, or upgrade for more.")
    return (f"Your plan includes {cap} agent{'s' if cap != 1 else ''} "
            f"and you have {used}. Archive one to free a slot, or upgrade.")


def check_agent_create(owner: str, family: str = "") -> Decision:
    """May `owner` create one more agent family?

    `family` is the base name being created: re-creating a half of a pair the
    tenant already owns is not a new family and must not be refused.

    Note the missing `if not enforcement_enabled(): return ok()` that every
    other gate opens with. It is deliberate, and it is the point of this
    change: the cap has to bind for trial tenants while the global switch stays
    off. `agent_cap()` puts the switch back in play for everyone else.
    """
    if not owner:
        return Decision.deny(CODE_PLAN_REQUIRED, "No account for this request.")
    try:
        ent = get_entitlements(owner)
        limits = effective_limits(ent)
        trial = limits["trial"]
        # A locked account is an administrative act and outranks everything,
        # but only where enforcement is live — locking is not an anti-abuse
        # measure and must not start biting because of this change.
        if (enforcement_enabled() and (denial := _locked(ent)) is not None):
            return denial
        if not trial and not enforcement_enabled():
            return Decision.ok()
        cap = limits["max_agent_families"]
        if cap is None:
            return Decision.ok()
        used = agent_families_used(owner)
        if family:
            from shared.db.database import get_db
            with get_db() as db:
                existing = db.execute(
                    "SELECT 1 FROM agents WHERE owner=? AND archived_at IS NULL "
                    "AND (slug=? OR slug=? OR slug=?) LIMIT 1",
                    (owner, family, f"{family}-inbound", f"{family}-outbound")
                ).fetchone()
            if existing:
                return Decision.ok()
        if used >= int(cap):
            return Decision.deny(
                CODE_CAP_AGENTS, agent_cap_message(int(cap), used, trial),
                agents_used=used, agents_limit=int(cap), trial=trial)
        return Decision.ok()
    except Exception as exc:
        log.error("[billing] agent-cap check failed for %s: %r", owner, exc)
        return Decision.ok(check="error")


def check_copilot_turn(owner: str, cost: int = 1) -> Decision:
    """Daily budget for the LLM surfaces (copilot chat, try-agent, document
    extraction).

    These are unmetered today, and the copilot's model quota is SHARED with
    live calls (COPILOT_LLM_TPM, and the provider's daily ceiling). So one Free
    tenant looping the copilot degrades paying tenants' phone calls at zero
    cost to themselves. This is an abuse ceiling, not a product limit — which
    is why, for a trial tenant, it binds whether or not billing enforcement is
    switched on.

    Counts MESSAGES. `check_copilot_tokens` counts the work those messages
    cause, and the two are checked together (see `check_copilot_budget`).
    """
    if not owner:
        return Decision.deny(CODE_PLAN_REQUIRED, "No account for this request.")
    try:
        ent = get_entitlements(owner)
        limits = effective_limits(ent)
        if enforcement_enabled() and (denial := _locked(ent)) is not None:
            return denial
        if not limits["trial"] and not enforcement_enabled():
            return Decision.ok()
        cap = limits["copilot_turns_per_day"]
        if cap is None:
            return Decision.ok()
        start, _ = _ist_day_bounds()
        from shared.db.database import get_db
        with get_db() as db:
            # audit_events timestamps its rows in `at`, not created_at.
            row = db.execute(
                "SELECT COUNT(*) AS n FROM audit_events "
                "WHERE owner=? AND action='copilot_turn' AND at >= ?",
                (owner, _iso(start))).fetchone()
        used = int((row or {}).get("n") or 0)
        if used + cost > int(cap):
            return Decision.deny(
                CODE_QUOTA_COPILOT,
                f"You have used today's {cap} AI-assistant messages. The limit "
                f"resets at midnight IST, or upgrade for a higher allowance.",
                copilot_used=used, copilot_limit=int(cap))
        return Decision.ok()
    except Exception as exc:
        log.error("[billing] copilot check failed for %s: %r", owner, exc)
        return Decision.ok(check="error")


# ---------------------------------------------------------------------------
# copilot TOKEN budget
#
# The turn cap counts how often someone pressed send. This counts what the
# platform actually spent answering — which is the number that matters, because
# one chat turn runs an agentic tool loop of up to _MAX_TOOL_ROUNDS (20) full
# LLM requests, each carrying the system prompt, the tool schemas and the
# fitted history. Twenty polite messages and twenty adversarial ones are the
# same number to the old counter and two orders of magnitude apart in cost.
# ---------------------------------------------------------------------------

def _ist_day_key(at: dt.datetime | None = None) -> str:
    """The IST calendar date the budget is keyed on, as YYYY-MM-DD.

    Same boundary as `_ist_day_bounds`, on purpose: two budgets that reset at
    different midnights produce support tickets nobody can reproduce.
    """
    return (at or now_utc()).astimezone(IST).strftime("%Y-%m-%d")


def copilot_tokens_used(owner: str, at: dt.datetime | None = None) -> int:
    """Tokens this owner has spent on LLM surfaces today (IST). 0 on any error
    — a counter that cannot be read must not become a wall."""
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT total_tokens FROM copilot_token_usage "
                "WHERE owner=? AND day=?", (owner, _ist_day_key(at))).fetchone()
        return int((row or {}).get("total_tokens") or 0)
    except Exception as exc:
        log.debug("[billing] copilot token read failed for %s: %r", owner, exc)
        return 0


def check_copilot_tokens(owner: str, request_tokens: int = 0) -> Decision:
    """May `owner` spend more LLM tokens today?

    Two ceilings, and they stop different things:

      * the DAILY budget, checked as `used >= cap` rather than
        `used + estimate > cap`. A turn is allowed to cross the line and
        finish; the next one is refused. Pre-charging an estimate would mean
        guessing how many tool rounds a turn will take, and guessing high
        refuses legitimate work while guessing low is the same as not
        checking. Overshoot is bounded by the second ceiling.

      * the PER-REQUEST ceiling, which is what actually bounds that overshoot
        and is the only thing standing between a 400k-token paste and the
        shared model quota. The daily budget cannot stop it: by the time the
        counter moves, the request has already been sent.
    """
    if not owner:
        return Decision.deny(CODE_PLAN_REQUIRED, "No account for this request.")
    try:
        ent = get_entitlements(owner)
        limits = effective_limits(ent)
        if enforcement_enabled() and (denial := _locked(ent)) is not None:
            return denial
        if not limits["trial"] and not enforcement_enabled():
            return Decision.ok()

        per_request = limits["copilot_max_request_tokens"]
        if per_request is not None and request_tokens > int(per_request):
            _note_copilot_refusal(owner)
            return Decision.deny(
                CODE_QUOTA_COPILOT_TOKENS,
                f"That message is too large for the free plan "
                f"(about {request_tokens:,} tokens; the limit is "
                f"{int(per_request):,} per message). Split it into smaller "
                f"parts, upload it as a document, or upgrade.",
                copilot_request_tokens=int(request_tokens),
                copilot_request_limit=int(per_request))

        cap = limits["copilot_tokens_per_day"]
        if cap is None:
            return Decision.ok()
        used = copilot_tokens_used(owner)
        if used >= int(cap):
            _note_copilot_refusal(owner)
            return Decision.deny(
                CODE_QUOTA_COPILOT_TOKENS,
                f"You have used today's AI-assistant allowance "
                f"({used:,} of {int(cap):,} tokens). It resets at midnight "
                f"IST, or upgrade for a higher allowance.",
                copilot_tokens_used=used, copilot_tokens_limit=int(cap))
        return Decision.ok()
    except Exception as exc:
        log.error("[billing] copilot token check failed for %s: %r", owner, exc)
        return Decision.ok(check="error")


def check_copilot_budget(owner: str, request_tokens: int = 0) -> Decision:
    """Both copilot ceilings, first refusal wins.

    One function so a new LLM surface cannot wire up half the budget. The turn
    cap goes first: when a tenant is out of both, "you have used today's 20
    messages" is the more actionable sentence.
    """
    turns = check_copilot_turn(owner)
    if not turns:
        return turns
    return check_copilot_tokens(owner, request_tokens)


def record_copilot_turn(owner: str, surface: str = "chat") -> None:
    """Count one LLM turn against the daily budget. Best-effort by design: a
    dropped count costs one free turn, a raised exception costs a chat reply."""
    try:
        from shared.audit import record
        record(owner, "copilot_turn", surface)
    except Exception as exc:
        log.debug("copilot turn not recorded for %s: %r", owner, exc)


def record_copilot_tokens(owner: str, prompt_tokens: int = 0,
                          completion_tokens: int = 0,
                          total_tokens: int | None = None,
                          requests: int = 1) -> None:
    """Add one LLM request's usage to today's counter. Never raises.

    `total_tokens` is taken from the provider when it reports one rather than
    recomputed, because on Gemini the parts do NOT add up: reasoning tokens are
    billed at the output rate and are folded into the total without appearing
    in `completion_tokens` (see prompt_cache.py and the 2026-09-12 attribution
    note). Summing the two visible numbers would under-count the expensive half
    of every request by ~29%.
    """
    if not owner:
        return
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    total = int(total_tokens) if total_tokens else prompt_tokens + completion_tokens
    total = max(total, prompt_tokens + completion_tokens)
    if total <= 0 and requests <= 0:
        return
    try:
        from shared.db.database import get_db
        with get_db() as db:
            db.execute(
                "INSERT INTO copilot_token_usage "
                "  (owner, day, prompt_tokens, completion_tokens, total_tokens, "
                "   requests, updated_at) "
                "VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT (owner, day) DO UPDATE SET "
                "  prompt_tokens = copilot_token_usage.prompt_tokens + excluded.prompt_tokens, "
                "  completion_tokens = copilot_token_usage.completion_tokens + excluded.completion_tokens, "
                "  total_tokens = copilot_token_usage.total_tokens + excluded.total_tokens, "
                "  requests = copilot_token_usage.requests + excluded.requests, "
                "  updated_at = CURRENT_TIMESTAMP",
                (owner, _ist_day_key(), prompt_tokens, completion_tokens,
                 total, max(0, int(requests))))
    except Exception as exc:
        # A dropped count costs the platform some tokens. A raised exception
        # costs the user their reply, mid-turn, after the model already ran.
        log.warning("[billing] copilot tokens not recorded for %s: %r", owner, exc)


def _note_copilot_refusal(owner: str) -> None:
    """Count a refusal on today's row so the operator view can tell a tenant
    who hit the ceiling once from one hammering it."""
    try:
        from shared.db.database import get_db
        with get_db() as db:
            db.execute(
                "INSERT INTO copilot_token_usage (owner, day, refusals, updated_at) "
                "VALUES (?,?,1,CURRENT_TIMESTAMP) "
                "ON CONFLICT (owner, day) DO UPDATE SET "
                "  refusals = copilot_token_usage.refusals + 1, "
                "  updated_at = CURRENT_TIMESTAMP",
                (owner, _ist_day_key()))
    except Exception as exc:
        log.debug("[billing] copilot refusal not recorded for %s: %r", owner, exc)


def _ist_day_bounds(at: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    at = (at or now_utc()).astimezone(IST)
    start = at.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(dt.timezone.utc), (start + dt.timedelta(days=1)).astimezone(dt.timezone.utc)


def feature_allowed(owner: str, feature: str) -> bool:
    """Is a per-tier capability available to this tenant?

    Gate at the API with this, never only in the SPA navigation — hiding a nav
    item is not enforcement, it is decoration.
    """
    if not enforcement_enabled():
        return True
    if feature not in _FEATURES:
        return True
    try:
        return bool(get_entitlements(owner).get(feature))
    except Exception as exc:
        log.error("[billing] feature check failed for %s/%s: %r", owner, feature, exc)
        return True  # fail open on a per-feature read; never hide paid data


def autocall_allowed(owner: str) -> bool:
    return feature_allowed(owner, "autocall")


def telephony_allowed(owner: str) -> bool:
    """May this tenant register a SIP trunk or attach a phone number?

    Deliberately does NOT consult `enforcement_enabled()`, unlike every other
    feature gate. The enforcement switch exists to stage the rollout of QUOTAS
    against tenants who already have plans; this is an access-control gate on
    a capability that puts trunks and DIDs on our carrier edge, and signups
    are open to any Google account. "Locked until enforcement is flipped"
    would mean every stranger who signs up today can register a number.

    Fails CLOSED on a missing entitlement (a brand-new signup has no row yet,
    and `_free_defaults()` has telephony=0) but OPEN on a database error —
    an outage must not lock out the staff accounts that already run numbers.
    """
    if not owner:
        return False
    try:
        return bool(get_entitlements(owner).get("telephony"))
    except Exception as exc:
        log.error("[billing] telephony gate unavailable for %s: %r", owner, exc)
        return True


# ---------------------------------------------------------------------------
# resolution — subscriptions history -> the one entitlements row
# ---------------------------------------------------------------------------

def resolve_entitlements(owner: str, actor: str = "system") -> dict:
    """Rebuild `owner`'s entitlement row from scratch. Idempotent.

    Precedence, highest first:
      1. an admin override (`entitlements.overrides`) — Enterprise custom terms
      2. an ACTIVE recurring subscription
      3. an ACTIVE, unexpired Pilot window
      4. a recurring subscription in grace (halted/paused, within grace hours)
      5. Free

    Rebuilt wholesale rather than patched, so a bug can never leave a tenant
    with a half-applied plan.
    """
    owner = (owner or "").strip().lower()
    if not owner:
        raise ValueError("resolve_entitlements needs an owner")
    from shared.db.database import get_db

    now = now_utc()
    with get_db() as db:
        prior = db.execute("SELECT overrides, status, lock_reason, notes "
                           "FROM entitlements WHERE owner=?", (owner,)).fetchone()
        overrides = {}
        if prior and prior["overrides"]:
            try:
                overrides = json.loads(prior["overrides"]) or {}
            except Exception:
                overrides = {}

        subs = [dict(r) for r in db.execute(
            "SELECT * FROM subscriptions WHERE owner=? ORDER BY id DESC",
            (owner,)).fetchall()]

        chosen = None
        # (2) live recurring
        for s in subs:
            if s["kind"] == "subscription" and not s["scheduled"] \
                    and s["status"] in ("active", "authenticated", "pending"):
                chosen = s
                break
        # (3) live pilot window
        if chosen is None:
            for s in subs:
                if s["kind"] == "one_time" and s["status"] == "active":
                    end = as_dt(s["current_end"])
                    if end and end > now:
                        chosen = s
                    break
        # (4) grace
        grace = False
        if chosen is None:
            for s in subs:
                if s["kind"] == "subscription" and s["status"] in ("halted", "paused"):
                    until = as_dt(s["grace_until"])
                    if until and until > now:
                        chosen, grace = s, True
                    break
        # A VOLUNTARY cancellation still entitles until the cycle they already
        # paid for actually ends.
        #
        # `cancel_at_cycle_end` is what separates that from an INVOLUNTARY
        # cancellation. Grace expiry also sets status='cancelled' (it must, to
        # free the uniqueness index — §6.7), and those rows still carry the
        # current_end of a cycle the customer never paid for. Without this flag
        # check, failing to pay would hand the tenant the rest of the period
        # for free — the exact opposite of dunning.
        if chosen is None:
            for s in subs:
                if s["kind"] == "subscription" and s["status"] == "cancelled":
                    end = as_dt(s["current_end"])
                    if s["cancel_at_cycle_end"] and end and end > now:
                        chosen = s
                    break

        plan_code = (chosen or {}).get("plan_code") or "free"
        if overrides.get("plan_code"):
            plan_code = overrides["plan_code"]
        plan = db.execute("SELECT * FROM plans WHERE code=?", (plan_code,)).fetchone()
        if plan is None:
            log.error("[billing] plan %r missing for %s — falling back to free",
                      plan_code, owner)
            plan = db.execute("SELECT * FROM plans WHERE code='free'").fetchone()
            plan_code = "free"
        plan = dict(plan)

        # Period. An admin override wins, so a hand-granted trial can be given
        # a real end date — without this an admin-granted Pilot would sit on
        # the IST calendar month and effectively never expire.
        if overrides.get("period_end"):
            p_start = as_dt(overrides.get("period_start")) or now
            p_end = as_dt(overrides["period_end"])
        elif chosen and as_dt(chosen.get("current_start")) and as_dt(chosen.get("current_end")):
            p_start = as_dt(chosen["current_start"])
            p_end = as_dt(chosen["current_end"])
        else:
            p_start, p_end = ist_month_bounds(now)

        # An expired admin grant falls back to Free rather than lingering.
        if overrides.get("period_end") and p_end and p_end <= now:
            log.info("[billing] admin grant for %s expired at %s — reverting to free",
                     owner, _iso(p_end))
            overrides = {k: v for k, v in overrides.items()
                         if k not in ("plan_code", "period_start", "period_end")}
            plan_code = "free"
            plan = dict(db.execute("SELECT * FROM plans WHERE code='free'").fetchone())
            p_start, p_end = ist_month_bounds(now)
            db.execute("UPDATE entitlements SET overrides=? WHERE owner=?",
                       (json.dumps(overrides), owner))

        def pick(field, plan_field=None):
            if field in overrides:
                return overrides[field]
            return plan.get(plan_field or field)

        status = "grace" if grace else "ok"
        # A lock is an administrative act; a plan change never clears it.
        if prior and prior["status"] == "locked":
            status = "locked"

        values = {
            "plan_code": plan_code,
            "source_subscription_id": (chosen or {}).get("id"),
            "minutes_included": pick("minutes_limit", "included_minutes"),
            "max_agent_families": pick("max_agent_families"),
            "max_concurrent_calls": pick("max_concurrent_calls"),
            "copilot_turns_per_day": pick("copilot_turns_per_day"),
            "copilot_tokens_per_day": pick("copilot_tokens_per_day"),
            "recordings": int(bool(pick("recordings", "recordings_enabled"))),
            "summaries": int(bool(pick("summaries", "summaries_enabled"))),
            "transcripts": int(bool(pick("transcripts", "transcripts_enabled"))),
            "intelligence": int(bool(pick("intelligence", "intelligence_enabled"))),
            "conversation_analytics": int(bool(pick("conversation_analytics"))),
            "advanced_analytics": int(bool(pick("advanced_analytics"))),
            "autocall": int(bool(pick("autocall", "autocall_enabled"))),
            "telephony": int(bool(pick("telephony", "telephony_enabled"))),
            "api_access": int(bool(pick("api_access", "api_access_enabled"))),
            "period_start": _iso(p_start),
            "period_end": _iso(p_end),
            "status": status,
        }
        # minutes_limit mirrors minutes_included at rest; get_entitlements()
        # adds live top-ups on read so a purchase takes effect immediately.
        values["minutes_limit"] = values["minutes_included"]

        cols = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        updates = ", ".join(f"{c}=excluded.{c}" for c in values)
        db.execute(
            f"INSERT INTO entitlements (owner, {cols}, updated_at, updated_by) "
            f"VALUES (?, {placeholders}, CURRENT_TIMESTAMP, ?) "
            f"ON CONFLICT (owner) DO UPDATE SET {updates}, "
            f"updated_at=CURRENT_TIMESTAMP, updated_by=excluded.updated_by",
            (owner, *values.values(), actor))

    log.info("[billing] entitlements resolved for %s -> %s (%s)",
             owner, values["plan_code"], values["status"])
    return get_entitlements(owner)


# ---------------------------------------------------------------------------
# fail-open bookkeeping + alarms
# ---------------------------------------------------------------------------

def _note_fail_open(owner: str, ok: bool) -> int:
    with _fail_open_lock:
        if ok:
            _fail_open_counts.pop(owner, None)
            return 0
        _fail_open_counts[owner] = _fail_open_counts.get(owner, 0) + 1
        return _fail_open_counts[owner]


def fail_open_state() -> dict:
    """Exposed on /api/health so the sentinel can alarm on it."""
    with _fail_open_lock:
        return dict(_fail_open_counts)


def notify(owner: str, title: str, body: str, severity: str = "info",
           ref: str = "", link: str = "/billing") -> None:
    """In-app notification, deduped by `ref` (notifications has a uniqueness
    check on owner+kind+ref, so a repeated pass does not spam the bell)."""
    try:
        from shared.intelligence.notify import _emit
        _emit(owner, "billing", severity, title, body, link,
              ref or f"billing:{title}")
    except Exception as exc:
        log.debug("billing notification not delivered to %s: %r", owner, exc)


def _notify_critical(owner: str, title: str, body: str) -> None:
    """Alarms go to the workers logline that sentinel and GCS log shipping
    already watch — NOT only to the in-app bell, which nobody is looking at
    when the thing being alarmed about is money."""
    log.critical("[BILLING-ALARM] owner=%s %s :: %s", owner, title, body)
    notify(owner, title, body, severity="critical",
           ref=f"billing-alarm:{owner}:{title}")
