"""Billing-grade call-minute metering (deploy/PLAN-minutes-docker-logging.md §1).

One immutable `usage_ledger` row per call, keyed by conn_id (== the
FreeSWITCH channel UUID — ws_audio_receiver derives conn_id from the media
stream path). Three writers, all idempotent:

  open_call()   — bridge, when the media session starts (≈ answer)
  heartbeat()   — bridge, every ~15 s while the call is live; a crash can
                  therefore lose at most one beat of usage, never a call
  close_call()  — bridge, at teardown

and one referee:

  reconcile()   — worker: joins FreeSWITCH CDR rows (billsec/answer_stamp,
                  the ground truth) on the channel UUID, closes crashed
                  calls from their last heartbeat, falls back to the
                  pipeline clock when no CDR appears within CDR_WAIT_HOURS,
                  recomputes usage_monthly, and reports discrepancies.

Billing rules:
  - `billsec_final` is the only number invoicing may read. It is set ONCE by
    the reconciler (source 'cdr' > 'pipeline'); manual corrections must use
    set_manual() which requires a note.
  - Displays may show COALESCE(billsec_final, billsec_pipeline) so live and
    just-ended calls appear immediately, marked unreconciled.
  - Seconds are stored exactly; minute rounding is a display/invoice policy
    (BILLING_ROUNDING = per_call_ceil | per_second_total, default
    per_call_ceil).

CLI:
  python -m shared.usage_metering --once          # one reconcile pass
  python -m shared.usage_metering --interval 300  # loop
  python -m shared.usage_metering --backfill      # ledger rows for old calls
  python -m shared.usage_metering --report        # cross-check report
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
import math
import os
import time

logger = logging.getLogger("usage_metering")

CDR_PATH = os.environ.get(
    "FS_CDR_PATH", "/var/log/freeswitch/cdr-csv/Master.csv")
# 'example' template column order (confirmed against the live cdr_csv.conf.xml)
_CDR_COLS = ["caller_id_name", "caller_id_number", "destination_number",
             "context", "start_stamp", "answer_stamp", "end_stamp",
             "duration", "billsec", "hangup_cause", "uuid", "bleg_uuid",
             "accountcode", "read_codec", "write_codec"]
_IDX = {name: i for i, name in enumerate(_CDR_COLS)}

STALE_OPEN_MIN = float(os.environ.get("METERING_STALE_OPEN_MIN", "2"))
CDR_WAIT_HOURS = float(os.environ.get("METERING_CDR_WAIT_HOURS", "24"))
DISCREPANCY_ALERT_S = int(os.environ.get("METERING_DISCREPANCY_S", "5"))
ROUNDING = os.environ.get("BILLING_ROUNDING", "per_call_ceil")


# ---------------------------------------------------------------------------
# time helpers — everything stored as UTC ISO with Z suffix
# ---------------------------------------------------------------------------

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def parse_ts(s: str | None) -> dt.datetime | None:
    """Parse the timestamp shapes that exist in this system: ISO with Z or
    offset, and the CDR/PG 'YYYY-MM-DD HH:MM:SS' (treated as UTC — the host
    and FreeSWITCH both run UTC)."""
    if not s:
        return None
    s = str(s).strip()
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t


# ---------------------------------------------------------------------------
# ledger writers (bridge-facing; every one must be safe to call twice)
# ---------------------------------------------------------------------------

def open_call(conn_id: str, owner: str, agent_slug: str | None = None,
              direction: str | None = None, contact_id: int | None = None,
              phone: str | None = None, billable: bool = True) -> None:
    """Create (or re-open, after a mid-call WS reconnect) the call's ledger
    row. answered_at is the FIRST media start; the reconciler later replaces
    it with the CDR's answer_stamp."""
    if not conn_id or not owner:
        raise ValueError("conn_id and owner are required for metering")
    from shared.db.database import get_db
    now = _iso(_now())
    with get_db() as db:
        db.execute(
            "INSERT INTO usage_ledger (conn_id, fs_uuid, owner, agent_slug, "
            "direction, contact_id, phone, answered_at, last_beat_at, "
            "billsec_pipeline, billable, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,0,?,'open') "
            "ON CONFLICT (conn_id) DO UPDATE SET "
            # a reconnect segment re-opens the row unless the referee already ruled
            "status=CASE WHEN usage_ledger.status='reconciled' "
            "THEN usage_ledger.status ELSE 'open' END, "
            "last_beat_at=excluded.last_beat_at, "
            "updated_at=CURRENT_TIMESTAMP",
            (conn_id, conn_id, owner, agent_slug, direction, contact_id,
             phone, now, now, 1 if billable else 0))


def _touch(conn_id: str, closing: bool) -> None:
    from shared.db.database import get_db
    now = _now()
    with get_db() as db:
        row = db.execute(
            "SELECT answered_at, status FROM usage_ledger WHERE conn_id=?",
            (conn_id,)).fetchone()
        if row is None or row["status"] == "reconciled":
            return
        answered = parse_ts(row["answered_at"]) or now
        elapsed = max(0, int((now - answered).total_seconds()))
        if closing:
            db.execute(
                "UPDATE usage_ledger SET billsec_pipeline=?, last_beat_at=?, "
                "ended_at=?, status='closed', updated_at=CURRENT_TIMESTAMP "
                "WHERE conn_id=? AND status!='reconciled'",
                (elapsed, _iso(now), _iso(now), conn_id))
        else:
            db.execute(
                "UPDATE usage_ledger SET billsec_pipeline=?, last_beat_at=?, "
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE conn_id=? AND status!='reconciled'",
                (elapsed, _iso(now), conn_id))


def heartbeat(conn_id: str) -> None:
    """Refresh billsec_pipeline from wall clock. Self-healing: elapsed is
    always recomputed from answered_at, so missed beats or reconnects can
    never double-count."""
    _touch(conn_id, closing=False)


def close_call(conn_id: str) -> None:
    _touch(conn_id, closing=True)


def set_manual(conn_id: str, billsec: int, note: str) -> None:
    """The only sanctioned after-the-fact correction. Note is mandatory."""
    if not note or not note.strip():
        raise ValueError("manual corrections require a note")
    from shared.db.database import get_db
    with get_db() as db:
        db.execute(
            "UPDATE usage_ledger SET billsec_final=?, source='manual', "
            "note=?, status='reconciled', updated_at=CURRENT_TIMESTAMP "
            "WHERE conn_id=?",
            (int(billsec), note.strip(), conn_id))
    recompute_monthly()


# ---------------------------------------------------------------------------
# CDR parsing
# ---------------------------------------------------------------------------

def load_cdr(path: str | None = None) -> dict[str, dict]:
    """uuid -> {answer_stamp, end_stamp, billsec, hangup_cause}. Unanswered
    calls have an empty answer_stamp and billsec 0 — they stay in the map so
    reconciliation can zero them out rather than falling back to the
    pipeline clock."""
    path = path or CDR_PATH
    out: dict[str, dict] = {}
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            for cells in csv.reader(f):
                if len(cells) < len(_CDR_COLS):
                    continue
                uuid = cells[_IDX["uuid"]].strip()
                if not uuid:
                    continue
                try:
                    billsec = int(cells[_IDX["billsec"]] or 0)
                except ValueError:
                    billsec = 0
                out[uuid] = {
                    "answer_stamp": cells[_IDX["answer_stamp"]].strip(),
                    "end_stamp": cells[_IDX["end_stamp"]].strip(),
                    "billsec": billsec,
                    "hangup_cause": cells[_IDX["hangup_cause"]].strip(),
                }
    except FileNotFoundError:
        logger.warning("CDR file not found: %s", path)
    except PermissionError:
        logger.error("CDR file not readable (add user to the freeswitch "
                     "group): %s", path)
    return out


# ---------------------------------------------------------------------------
# the referee
# ---------------------------------------------------------------------------

def reconcile(cdr_path: str | None = None) -> dict:
    """One full pass. Idempotent; safe to run as often as you like."""
    from shared.db.database import get_db

    stats = {"reconciled_cdr": 0, "crash_closed": 0, "fallback_pipeline": 0,
             "discrepancies": 0}
    now = _now()
    cdr = load_cdr(cdr_path)
    touched_owner_months: set[tuple[str, str]] = set()

    # Sweep 1 — crash detection FIRST, so a dead call's row is 'closed' by the
    # time the CDR sweep below runs and reconciles it in this same pass.
    with get_db() as db:
        open_rows = [dict(r) for r in db.execute(
            "SELECT conn_id, answered_at, last_beat_at FROM usage_ledger "
            "WHERE status='open'").fetchall()]
    for row in open_rows:
        last_beat = parse_ts(row["last_beat_at"])
        beat_age_min = ((now - last_beat).total_seconds() / 60
                        if last_beat else 1e9)
        if beat_age_min > STALE_OPEN_MIN:
            ended = last_beat or parse_ts(row["answered_at"]) or now
            with get_db() as db:
                db.execute(
                    "UPDATE usage_ledger SET status='closed', ended_at=?, "
                    "note=COALESCE(note, 'crash-recovered from last "
                    "heartbeat'), updated_at=CURRENT_TIMESTAMP "
                    "WHERE conn_id=? AND status='open'",
                    (_iso(ended), row["conn_id"]))
            stats["crash_closed"] += 1

    # Sweep 2 — CDR reconciliation + no-CDR fallback over everything pending.
    with get_db() as db:
        pending = [dict(r) for r in db.execute(
            "SELECT * FROM usage_ledger WHERE status IN ('open','closed')"
        ).fetchall()]

    for row in pending:
        conn_id = row["conn_id"]
        rec = cdr.get(row["fs_uuid"] or conn_id) or cdr.get(conn_id)
        answered = parse_ts(row["answered_at"])
        last_beat = parse_ts(row["last_beat_at"])

        if rec is not None and row["status"] == "closed":
            # Ground truth available and the call is over: CDR rules.
            answer_ts = parse_ts(rec["answer_stamp"])
            end_ts = parse_ts(rec["end_stamp"])
            billsec = rec["billsec"] if rec["answer_stamp"] else 0
            pipeline = row["billsec_pipeline"] or 0
            if abs(billsec - pipeline) > DISCREPANCY_ALERT_S:
                stats["discrepancies"] += 1
                logger.warning(
                    "[METERING-DISCREPANCY] conn=%s owner=%s cdr=%ss "
                    "pipeline=%ss cause=%s", conn_id, row["owner"], billsec,
                    pipeline, rec["hangup_cause"])
            with get_db() as db:
                db.execute(
                    "UPDATE usage_ledger SET billsec_cdr=?, billsec_final=?, "
                    "answered_at=COALESCE(?, answered_at), "
                    "ended_at=COALESCE(?, ended_at), "
                    "source='cdr', status='reconciled', "
                    "updated_at=CURRENT_TIMESTAMP WHERE conn_id=? "
                    "AND status='closed'",
                    (rec["billsec"], billsec,
                     _iso(answer_ts) if answer_ts else None,
                     _iso(end_ts) if end_ts else None, conn_id))
            stats["reconciled_cdr"] += 1
            touched_owner_months.add((row["owner"], _month_of(answer_ts or answered or now)))
            continue

        if row["status"] == "open":
            # Still alive (fresh heartbeat) — leave it for the next pass.
            continue

        # closed, no CDR yet: give FreeSWITCH CDR_WAIT_HOURS to show up,
        # then trust our own clock rather than billing nothing.
        ended = parse_ts(row["ended_at"])
        age_h = ((now - ended).total_seconds() / 3600 if ended else 0)
        if age_h > CDR_WAIT_HOURS:
            with get_db() as db:
                db.execute(
                    "UPDATE usage_ledger SET billsec_final=?, "
                    "source='pipeline', status='reconciled', "
                    "note=COALESCE(note, 'no CDR within wait window'), "
                    "updated_at=CURRENT_TIMESTAMP WHERE conn_id=? "
                    "AND status='closed'",
                    (row["billsec_pipeline"] or 0, conn_id))
            stats["fallback_pipeline"] += 1
            touched_owner_months.add((row["owner"], _month_of(answered or now)))

    recompute_monthly(touched_owner_months or None)
    logger.info("reconcile: %s", stats)
    return stats


def _month_of(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m")


def recompute_monthly(owner_months: set[tuple[str, str]] | None = None) -> None:
    """Rebuild rollups from the ledger — REPLACE, never increment."""
    from shared.db.database import get_db
    with get_db() as db:
        rows = db.execute(
            "SELECT owner, answered_at, billsec_final, billsec_pipeline, "
            "billable FROM usage_ledger").fetchall()
    agg: dict[tuple[str, str], list[int]] = {}
    for r in rows:
        if not r["billable"]:
            continue
        t = parse_ts(r["answered_at"])
        if t is None:
            continue
        key = (r["owner"], _month_of(t))
        if owner_months is not None and key not in owner_months:
            continue
        sec = r["billsec_final"] if r["billsec_final"] is not None \
            else (r["billsec_pipeline"] or 0)
        cur = agg.setdefault(key, [0, 0])
        cur[0] += int(sec)
        cur[1] += 1
    with get_db() as db:
        for (owner, month), (seconds, calls) in agg.items():
            db.execute(
                "INSERT INTO usage_monthly (owner, month, seconds, calls, "
                "updated_at) VALUES (?,?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT (owner, month) DO UPDATE SET "
                "seconds=excluded.seconds, calls=excluded.calls, "
                "updated_at=CURRENT_TIMESTAMP",
                (owner, month, seconds, calls))


# ---------------------------------------------------------------------------
# display policy
# ---------------------------------------------------------------------------

def display_minutes(per_call_seconds: list[int]) -> int:
    """Minutes shown to (and later charged to) the user. per_call_ceil bills
    each call up to the next whole minute; per_second_total sums exact
    seconds and rounds the total up once."""
    if ROUNDING == "per_second_total":
        return math.ceil(sum(per_call_seconds) / 60) if per_call_seconds else 0
    return sum(math.ceil(s / 60) for s in per_call_seconds if s > 0)


# ---------------------------------------------------------------------------
# backfill + cross-check
# ---------------------------------------------------------------------------

def backfill_from_calls() -> int:
    """Ledger rows for calls that predate metering. Marked source='backfill'
    so they are distinguishable from measured usage forever."""
    from shared.db.database import get_db
    n = 0
    with get_db() as db:
        rows = db.execute(
            "SELECT c.conn_id, c.owner, c.agent_slug, c.direction, "
            "c.contact_id, c.phone, c.started_at, c.ended_at, c.duration_s "
            "FROM calls c LEFT JOIN usage_ledger u ON u.conn_id=c.conn_id "
            "WHERE u.id IS NULL AND c.conn_id IS NOT NULL").fetchall()
        for r in rows:
            sec = int(round(r["duration_s"] or 0))
            db.execute(
                "INSERT INTO usage_ledger (conn_id, fs_uuid, owner, "
                "agent_slug, direction, contact_id, phone, answered_at, "
                "ended_at, last_beat_at, billsec_pipeline, billsec_final, "
                "source, billable, status, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'backfill',1,'reconciled',"
                "'backfill pre-metering') ON CONFLICT (conn_id) DO NOTHING",
                (r["conn_id"], r["conn_id"],
                 r["owner"] or os.environ.get("LESCALL_DEFAULT_OWNER",
                                              "tech@dhronai.com"),
                 r["agent_slug"], r["direction"], r["contact_id"], r["phone"],
                 r["started_at"], r["ended_at"], r["ended_at"], sec, sec))
            n += 1
    recompute_monthly()
    return n


def cross_check_report() -> list[dict]:
    """Rows whose two clocks disagree by more than the alert threshold, or
    that have been open suspiciously long. For the nightly report + health."""
    from shared.db.database import get_db
    now = _now()
    problems = []
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT conn_id, owner, status, answered_at, last_beat_at, "
            "billsec_pipeline, billsec_cdr, billsec_final, source "
            "FROM usage_ledger").fetchall()]
    for r in rows:
        if (r["billsec_cdr"] is not None and r["billsec_pipeline"] is not None
                and abs(r["billsec_cdr"] - r["billsec_pipeline"]) > DISCREPANCY_ALERT_S):
            problems.append({**r, "issue": "clock_disagreement"})
        if r["status"] == "open":
            beat = parse_ts(r["last_beat_at"])
            if beat and (now - beat).total_seconds() > 3600:
                problems.append({**r, "issue": "open_over_1h"})
    return problems


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=0)
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.backfill:
        print(f"backfilled {backfill_from_calls()} ledger rows")
        return
    if args.report:
        for p in cross_check_report():
            print(p)
        return
    if args.interval > 0 and not args.once:
        while True:
            try:
                reconcile()
            except Exception:
                logger.exception("reconcile pass failed")
            time.sleep(args.interval)
    else:
        reconcile()


if __name__ == "__main__":
    main()
