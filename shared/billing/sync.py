"""Billing sync + dunning worker.

Runs as its OWN supervised process inside lescall-workers, deliberately NOT as
a pass in the 5-minute tick loop. The tick is watched by the sentinel, which
restarts the container if `reconcile:` disappears from 20 minutes of logs — and
N sequential gateway calls make the tick's duration a function of Razorpay's
latency times the subscriber count. One slow afternoon at the payment provider
would restart the container, kill the intelligence worker mid-job, and stop
billing reconciliation: an outage caused entirely by where the code was put.

Nine passes, ordered cheapest-first so a wall-clock budget overrun drops the
expensive gateway work rather than the local bookkeeping:

  1. reservations   — sweep expired dial admissions
  2. pilot expiry   — fixed 7-day windows that have elapsed
  3. topup expiry   — packs that belonged to a finished cycle
  4. grace expiry   — failed payments past their grace window -> Free, and
                      CANCELLED at the gateway (§6.7, the halted trap)
  5. cycle end      — cancel-at-cycle-end subscriptions that have now ended
  6. stale checkouts— `created` rows nobody ever paid -> expired/abandoned
  7. enterprise     — manual plans past period_end -> grace -> locked
  8. recover        — poll the gateway for payments whose webhook never came
  9. alarms         — webhook silence, stuck events, renewal heads-up

Every alarm goes to the LOG, not only the in-app bell: the log is what the
sentinel and GCS shipping already watch, and nobody is looking at a bell dot
when the thing going wrong is money.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import time

from shared.billing import razorpay_client as rz
from shared.billing.entitlements import (
    as_dt, notify, now_utc, resolve_entitlements, sweep_reservations,
)
from shared.billing.service import _epoch, reconcile_owner

log = logging.getLogger("billing.sync")

INTERVAL_S = int(os.environ.get("BILLING_SYNC_INTERVAL_S", "900"))   # 15 min
WALL_BUDGET_S = float(os.environ.get("BILLING_SYNC_BUDGET_S", "120"))
GRACE_HOURS = int(os.environ.get("BILLING_GRACE_HOURS", "72"))
ENTERPRISE_GRACE_DAYS = int(os.environ.get("BILLING_ENTERPRISE_GRACE_DAYS", "7"))
CHECKOUT_TTL_MIN = int(os.environ.get("BILLING_CHECKOUT_TTL_MIN", "30"))
# Razorpay auto-disables a webhook after ~24h of failures. If an active
# subscription's charge_at came and went with no event at all, we are flying
# blind on that tenant's money.
WEBHOOK_SILENCE_H = int(os.environ.get("BILLING_WEBHOOK_SILENCE_H", "36"))


def _iso(t: dt.datetime | None) -> str | None:
    return t.astimezone(dt.timezone.utc).isoformat() if t else None


def _alarm(title: str, detail: str) -> None:
    log.critical("[BILLING-ALARM] %s :: %s", title, detail)


# ---------------------------------------------------------------------------
# passes
# ---------------------------------------------------------------------------

def pass_reservations() -> dict:
    return {"reservations_swept": sweep_reservations()}


def pass_pilot_expiry() -> dict:
    """Close elapsed Pilot windows.

    No webhook announces this — the trial simply runs out of calendar. Note
    that `get_entitlements()` ALSO treats an elapsed window as expired on read,
    so a lapsed trial cannot keep dialling in the gap between passes; this pass
    makes the state durable and sends the notification.
    """
    from shared.db.database import get_db
    out = {"pilots_expired": 0, "pilot_warnings": 0}
    now = now_utc()
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM subscriptions WHERE kind='one_time' AND status='active'"
        ).fetchall()]

    for row in rows:
        end = as_dt(row["current_end"])
        if not end:
            continue
        owner = row["owner"]
        if end <= now:
            with get_db() as db:
                db.execute("UPDATE subscriptions SET status='completed', "
                           "updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='active'",
                           (row["id"],))
            resolve_entitlements(owner, actor="sync:pilot_expiry")
            notify(owner, "Your Pilot has ended",
                   "The 7-day trial is over and your account is on the Free "
                   "plan. Your agents are still here — choose Basic or Growth "
                   "to start calling again.", ref=f"pilot-end:{row['id']}")
            log.info("[billing] pilot window closed for %s", owner)
            out["pilots_expired"] += 1
            continue

        hours_left = (end - now).total_seconds() / 3600
        # Heads-up at T-48h and T-6h. Deduped by ref, so a pass every 15
        # minutes does not send 8 copies an hour.
        for threshold, label in ((48, "48h"), (6, "6h")):
            if 0 < hours_left <= threshold:
                notify(owner, "Trial ending soon",
                       f"Your Pilot ends in about {int(hours_left)} hours. Pick "
                       f"a plan to keep your agents answering calls.",
                       severity="warning",
                       ref=f"pilot-warn:{row['id']}:{label}")
                out["pilot_warnings"] += 1
                break

        # Burn alarm at 80% of the pool, so a trial that will convert — or
        # churn — is visible before it ends rather than after.
        try:
            from shared.billing.entitlements import get_entitlements, minutes_used
            ent = get_entitlements(owner)
            limit = ent.get("minutes_limit")
            if limit:
                used = minutes_used(owner, as_dt(row["current_start"]) or now, end)
                if used >= 0.8 * int(limit):
                    log.warning("[billing] PILOT-BURN %s at %s/%s minutes",
                                owner, used, limit)
        except Exception as exc:
            log.debug("pilot burn check failed for %s: %r", owner, exc)
    return out


def pass_topup_expiry() -> dict:
    from shared.db.database import get_db
    with get_db() as db:
        cur = db.execute(
            "UPDATE minute_topups SET status='expired', updated_at=CURRENT_TIMESTAMP "
            "WHERE status='active' AND period_end IS NOT NULL "
            "AND period_end <= CURRENT_TIMESTAMP RETURNING owner")
        owners = {r["owner"] for r in cur.fetchall()}
    for owner in owners:
        resolve_entitlements(owner, actor="sync:topup_expiry")
    return {"topups_expired": len(owners)}


def pass_credit_settlement() -> dict:
    """Drain admin credit pools by what each CLOSED month overspent.

    This is what stops a multi-month reward from being re-granted at every
    period rollover — see shared/billing/credits.py. It must run BEFORE the
    grace/cycle passes touch periods, and after topup expiry so a grant that
    lapsed this pass is not settled for a month it no longer covers.
    """
    from shared.billing.credits import settle_all
    return settle_all()


def pass_grace_expiry() -> dict:
    """Failed payments past their grace window.

    The important half of this is the CANCEL. Downgrading the entitlement but
    leaving the row `halted` traps the tenant permanently: `halted` sits inside
    the one-current-subscription unique index, so checkout refuses every plan
    forever, with an error that looks like a bug. Cancelling at the gateway
    frees the index and lets them subscribe again.
    """
    from shared.db.database import get_db
    out = {"grace_expired": 0}
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM subscriptions WHERE status IN ('halted','paused') "
            "AND grace_until IS NOT NULL AND grace_until <= CURRENT_TIMESTAMP"
        ).fetchall()]

    for row in rows:
        owner = row["owner"]
        if row["razorpay_subscription_id"] and rz.configured():
            try:
                rz.cancel_subscription(row["razorpay_subscription_id"],
                                       at_cycle_end=False)
            except rz.GatewayError as exc:
                log.error("[billing] gateway cancel failed for %s — freeing the "
                          "local row anyway: %r", owner, exc)
        with get_db() as db:
            db.execute("UPDATE subscriptions SET status='cancelled', "
                       "updated_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
        resolve_entitlements(owner, actor="sync:grace_expiry")
        notify(owner, "Subscription ended",
               "We could not collect payment, so your account has moved to the "
               "Free plan. Your agents and data are intact — subscribe again "
               "any time to resume calling.", severity="critical",
               ref=f"grace-end:{row['id']}")
        _alarm("subscription lapsed after grace",
               f"{owner} dropped to Free (subscription {row['id']})")
        out["grace_expired"] += 1
    return out


def pass_cycle_end() -> dict:
    from shared.db.database import get_db
    with get_db() as db:
        cur = db.execute(
            "UPDATE subscriptions SET status='completed', updated_at=CURRENT_TIMESTAMP "
            "WHERE cancel_at_cycle_end=1 AND status IN ('active','cancelled') "
            "AND current_end IS NOT NULL AND current_end <= CURRENT_TIMESTAMP "
            "RETURNING owner")
        owners = {r["owner"] for r in cur.fetchall()}
    for owner in owners:
        resolve_entitlements(owner, actor="sync:cycle_end")
        notify(owner, "Subscription ended",
               "Your cancellation has taken effect and your account is on the "
               "Free plan.", ref=f"cycle-end:{owner}")
    return {"cycle_ended": len(owners)}


def pass_stale_checkouts() -> dict:
    """Abandoned checkouts.

    A `created` row is a customer who opened the payment modal. Most who never
    finish simply closed the tab. Ageing them out matters because a stale
    `created` Pilot row holds a pilot_claim, and a tenant who abandoned a
    checkout must not be barred from the trial forever.
    """
    from shared.db.database import get_db
    with get_db() as db:
        cur = db.execute(
            "UPDATE subscriptions SET status='expired', updated_at=CURRENT_TIMESTAMP "
            "WHERE status='created' AND created_at < CURRENT_TIMESTAMP - "
            f"INTERVAL '{CHECKOUT_TTL_MIN} minutes' RETURNING id, owner, kind")
        expired = [dict(r) for r in cur.fetchall()]
        # Release the pilot claim an abandoned checkout was holding.
        for row in expired:
            if row["kind"] == "one_time":
                db.execute(
                    "UPDATE pilot_claims SET released_by='sync', "
                    "released_at=CURRENT_TIMESTAMP, "
                    "release_reason='checkout abandoned' "
                    "WHERE owner=? AND subscription_id=? AND released_at IS NULL",
                    (row["owner"], row["id"]))
        db.execute(
            "UPDATE payment_attempts SET status='abandoned', "
            "updated_at=CURRENT_TIMESTAMP WHERE status IN ('initiated','gateway_created') "
            f"AND created_at < CURRENT_TIMESTAMP - INTERVAL '{CHECKOUT_TTL_MIN * 4} minutes'")
        db.execute(
            "UPDATE minute_topups SET status='expired', updated_at=CURRENT_TIMESTAMP "
            "WHERE status='created' AND created_at < CURRENT_TIMESTAMP - "
            f"INTERVAL '{CHECKOUT_TTL_MIN} minutes'")
    return {"checkouts_expired": len(expired)}


def pass_enterprise_expiry() -> dict:
    """Manual (Enterprise) plans past their paid period.

    Deliberately does NOT silently drop to Free: an Enterprise account has a
    hand-built configuration and a human relationship. It goes to `grace`, then
    to `locked`, which is a state a person has to resolve.
    """
    from shared.db.database import get_db
    out = {"enterprise_grace": 0, "enterprise_locked": 0}
    now = now_utc()
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT owner, plan_code, period_end, status FROM entitlements "
            "WHERE plan_code='enterprise' AND period_end IS NOT NULL "
            "AND period_end <= CURRENT_TIMESTAMP AND status <> 'locked'"
        ).fetchall()]
    for row in rows:
        end = as_dt(row["period_end"])
        overdue_days = (now - end).days if end else 0
        with get_db() as db:
            if overdue_days >= ENTERPRISE_GRACE_DAYS:
                db.execute("UPDATE entitlements SET status='locked', "
                           "lock_reason='enterprise period expired', "
                           "updated_at=CURRENT_TIMESTAMP, updated_by='sync' "
                           "WHERE owner=?", (row["owner"],))
                out["enterprise_locked"] += 1
                _alarm("enterprise account locked",
                       f"{row['owner']} is {overdue_days}d past period_end")
            elif row["status"] != "grace":
                db.execute("UPDATE entitlements SET status='grace', "
                           "updated_at=CURRENT_TIMESTAMP, updated_by='sync' "
                           "WHERE owner=?", (row["owner"],))
                out["enterprise_grace"] += 1
        notify(row["owner"], "Renewal due",
               "Your Enterprise term has ended. Please contact us to renew.",
               severity="warning", ref=f"ent-renew:{row['owner']}:{overdue_days // 3}")
    return out


def pass_recover_payments(budget_left: float) -> dict:
    """Poll the gateway for money whose webhook never arrived.

    This is the safety net under the whole design: if webhooks are misconfigured,
    dropped, or the endpoint was down, the customer's payment is still found and
    honoured — just later. Bounded by the wall-clock budget because each owner
    costs at least one round trip.
    """
    if not rz.configured():
        return {"recovery_skipped": "gateway unconfigured"}
    from shared.db.database import get_db
    started = time.monotonic()
    out = {"owners_checked": 0, "payments_recovered": 0}
    with get_db() as db:
        owners = [r["owner"] for r in db.execute(
            "SELECT DISTINCT owner FROM payment_attempts "
            "WHERE status IN ('gateway_created','authorized') "
            "AND created_at > CURRENT_TIMESTAMP - INTERVAL '7 days' "
            "AND created_at < CURRENT_TIMESTAMP - INTERVAL '10 minutes' "
            "LIMIT 50").fetchall()]
    for owner in owners:
        if time.monotonic() - started > budget_left:
            log.warning("[billing] recovery pass out of budget after %d owners "
                        "(%d left unchecked this round)",
                        out["owners_checked"], len(owners) - out["owners_checked"])
            break
        result = reconcile_owner(owner, actor="sync")
        out["owners_checked"] += 1
        out["payments_recovered"] += result.get("recovered", 0)
    return out


def pass_alarms() -> dict:
    """Things that are silently wrong."""
    from shared.db.database import get_db
    out = {"webhook_silence": 0, "stuck_events": 0, "renewals_flagged": 0}
    with get_db() as db:
        # (a) An active subscription whose charge came and went with no event.
        silent = [dict(r) for r in db.execute(
            "SELECT id, owner, charge_at, razorpay_subscription_id "
            "FROM subscriptions WHERE status='active' AND kind='subscription' "
            "AND charge_at IS NOT NULL "
            f"AND charge_at < CURRENT_TIMESTAMP - INTERVAL '{WEBHOOK_SILENCE_H} hours'"
        ).fetchall()]
        # (b) Events that errored and were never reprocessed.
        stuck = [dict(r) for r in db.execute(
            "SELECT id, event_id, event_type, error FROM billing_events "
            "WHERE processed_at IS NULL "
            "AND received_at < CURRENT_TIMESTAMP - INTERVAL '15 minutes' "
            "LIMIT 50").fetchall()]
        # (c) Renewals two days out, so a card that will fail is visible early.
        renewals = [dict(r) for r in db.execute(
            "SELECT id, owner, current_end FROM subscriptions "
            "WHERE status='active' AND kind='subscription' "
            "AND cancel_at_cycle_end=0 AND current_end IS NOT NULL "
            "AND current_end BETWEEN CURRENT_TIMESTAMP + INTERVAL '47 hours' "
            "AND CURRENT_TIMESTAMP + INTERVAL '49 hours'").fetchall()]

    for row in silent:
        _alarm("webhook silence",
               f"subscription {row['razorpay_subscription_id']} ({row['owner']}) "
               f"had charge_at {row['charge_at']} and no event since. Check the "
               f"Razorpay webhook is enabled — it auto-disables after 24h of "
               f"delivery failures.")
        out["webhook_silence"] += 1

    for row in stuck:
        _alarm("billing event stuck",
               f"{row['event_type']} {row['event_id']}: {row['error']}")
        out["stuck_events"] += 1

    for row in renewals:
        notify(row["owner"], "Renewal in 2 days",
               f"Your subscription renews on {(row['current_end'] or '')[:10]}.",
               ref=f"renewal:{row['id']}:{(row['current_end'] or '')[:10]}")
        out["renewals_flagged"] += 1
    return out


def reprocess_stuck_events(limit: int = 20) -> dict:
    """Replay events that errored. Possible only because the raw payload is
    stored verbatim — the gateway will never resend a deduped event."""
    from shared.db.database import get_db
    import json as _json
    from shared.billing.webhooks import dispatch
    out = {"reprocessed": 0, "still_failing": 0}
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT id, event_type, payload, attempts FROM billing_events "
            "WHERE processed_at IS NULL AND attempts < 5 "
            "AND received_at < CURRENT_TIMESTAMP - INTERVAL '15 minutes' "
            "ORDER BY id LIMIT ?", (limit,)).fetchall()]
    for row in rows:
        try:
            payload = _json.loads(row["payload"])
            result = dispatch(row["event_type"], payload)
            with get_db() as db:
                db.execute("UPDATE billing_events SET processed_at=CURRENT_TIMESTAMP, "
                           "error=NULL, owner=?, attempts=attempts+1 WHERE id=?",
                           (result.get("owner"), row["id"]))
            out["reprocessed"] += 1
        except Exception as exc:
            with get_db() as db:
                db.execute("UPDATE billing_events SET error=?, attempts=attempts+1 "
                           "WHERE id=?", (repr(exc)[:500], row["id"]))
            out["still_failing"] += 1
    return out


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def run_once() -> dict:
    from shared.billing import available
    if not available():
        return {"skipped": "billing tables are Postgres-only"}

    started = time.monotonic()
    stats: dict = {}
    for name, fn in (
        ("reservations", pass_reservations),
        ("pilot", pass_pilot_expiry),
        ("topups", pass_topup_expiry),
        ("credits", pass_credit_settlement),
        ("grace", pass_grace_expiry),
        ("cycle", pass_cycle_end),
        ("stale", pass_stale_checkouts),
        ("enterprise", pass_enterprise_expiry),
        ("reprocess", reprocess_stuck_events),
        ("alarms", pass_alarms),
    ):
        try:
            stats.update(fn())
        except Exception:
            log.exception("[billing] sync pass %r failed", name)
        if time.monotonic() - started > WALL_BUDGET_S:
            log.warning("[billing] sync out of wall-clock budget after %r; "
                        "remaining passes deferred to the next run", name)
            stats["budget_exceeded_after"] = name
            return stats

    try:
        stats.update(pass_recover_payments(
            budget_left=max(5.0, WALL_BUDGET_S - (time.monotonic() - started))))
    except Exception:
        log.exception("[billing] recovery pass failed")

    stats["elapsed_s"] = round(time.monotonic() - started, 1)
    log.info("billing sync: %s", stats)
    return stats


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=INTERVAL_S)
    args = ap.parse_args()

    if args.once:
        print(run_once())
        return 0
    log.info("billing sync worker up (interval=%ss)", args.interval)
    while True:
        try:
            run_once()
        except Exception:
            log.exception("billing sync pass crashed")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
