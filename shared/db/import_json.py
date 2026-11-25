"""Part B Phase 1: one-time (but idempotent) import of dashboard JSON stores into the DB.

The JSON files remain the source of truth for now — this only seeds the DB so
Phase-2/3 consumers have rows to read. Re-running changes nothing: every insert
is INSERT ... ON CONFLICT DO NOTHING on the store's natural key ((owner, phone) for contacts,
call_key for calls, ref for follow-ups), and rows already present are counted
as pre_existing. Exception: calls upserts analysis/summary via COALESCE so an
analysis attaches to a bridge-written row without clobbering existing data.

Usage:
    python3 -m shared.db.import_json [--db-url URL] [--dashboard-dir DIR] [--owner EMAIL]

Prints a JSON report and exits non-zero if any store failed to parse or its
verification counts don't add up (source_count == imported + pre_existing +
skipped_invalid, and the owner's DB rows cover what was imported).

Stores handled (missing or empty files are fine — count 0, ok):
- outbound_contacts.json   (array)             -> contacts
- call_analysis.json       (object by call_key)-> calls
- follow_ups.json          (object by ref)     -> follow_ups (source=filename)
- manual_followups.json    (object or array)   -> follow_ups (ref prefixed "manual:"
                                                  unless the key already has a ":")
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from shared.db.database import get_db
from shared.phone import significant_digits

DEFAULT_DASHBOARD_DIR = Path("/home/angelo/lescall/dashboard")
DEFAULT_OWNER = "tech@dhronai.com"

_MISSING = object()  # sentinel: file absent (or empty) — distinct from JSON null


def _read_json(path: Path):
    """Return (data, error). Missing or blank file -> (_MISSING, None)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _MISSING, None
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not text.strip():
        return _MISSING, None
    try:
        return json.loads(text), None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _started_at_from_call_key(call_key: str) -> str | None:
    """"<digits>_<YYYYMMDD>_<HHMMSS>" -> "YYYY-MM-DDTHH:MM:SSZ"; None if malformed."""
    parts = call_key.split("_")
    if len(parts) < 3:
        return None
    try:
        dt = datetime.strptime(parts[1] + parts[2], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _import_contacts(db, data, owner: str):
    if not isinstance(data, list):
        raise ValueError("outbound_contacts.json: expected a JSON array")
    imported = pre_existing = skipped = 0
    for entry in data:
        phone = str(entry.get("phone") or "").strip() if isinstance(entry, dict) else ""
        if not phone:
            skipped += 1
            continue
        extra = None
        if "call_history" in entry:
            extra = json.dumps({"call_history": entry["call_history"]}, ensure_ascii=False)
        try:
            call_count = int(entry.get("call_count") or 0)
        except (TypeError, ValueError):
            call_count = 0
        cur = db.execute(
            "INSERT INTO contacts (legacy_id, owner, phone, sig_digits, name,"
            " notes, call_status, disposition, call_count, last_called_at, call_notes,"
            " extra, added_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT DO NOTHING",
            (entry.get("id"), owner, phone, significant_digits(phone), entry.get("name"),
             entry.get("notes"), entry.get("call_status"), entry.get("disposition"),
             call_count, entry.get("last_called_at"), entry.get("call_notes"),
             extra, entry.get("added_at")))
        if cur.rowcount == 1:
            imported += 1
        else:
            pre_existing += 1
    return len(data), imported, pre_existing, skipped


def _import_calls(db, data, owner: str):
    if not isinstance(data, dict):
        raise ValueError("call_analysis.json: expected a JSON object keyed by call_key")
    imported = pre_existing = skipped = 0
    for call_key, analysis in data.items():
        if not call_key or not isinstance(analysis, dict):
            skipped += 1
            continue
        phone = call_key.split("_", 1)[0]
        exists = db.execute(
            "SELECT 1 FROM calls WHERE call_key=?", (call_key,)).fetchone()
        # Upsert (not insert-ignore): the live bridge creates the row first
        # (with analysis NULL); this attaches the analysis to it instead of
        # silently dropping it. COALESCE keeps any analysis already in the DB.
        db.execute(
            "INSERT INTO calls (call_key, direction, phone, sig_digits,"
            " started_at, summary, analysis, owner) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(call_key) DO UPDATE SET"
            " analysis=COALESCE(calls.analysis, excluded.analysis),"
            " summary=COALESCE(calls.summary, excluded.summary)",
            (call_key, "unknown", phone, significant_digits(phone),
             _started_at_from_call_key(call_key), analysis.get("summary"),
             json.dumps(analysis, ensure_ascii=False), owner))
        if exists:
            pre_existing += 1
        else:
            imported += 1
    return len(data), imported, pre_existing, skipped


def _import_follow_ups(db, data, owner: str, source: str, manual: bool = False):
    # Spec shape is an object key -> entry; the live manual_followups.json is an
    # array of entries carrying their own "id" — accept both.
    if isinstance(data, dict):
        items = list(data.items())
    elif isinstance(data, list):
        items = [(e.get("id") if isinstance(e, dict) else None, e) for e in data]
    else:
        raise ValueError(f"{source}: expected a JSON object or array")
    imported = pre_existing = skipped = 0
    for key, entry in items:
        if not key or not isinstance(entry, dict):
            skipped += 1
            continue
        key = str(key)
        ref = f"manual:{key}" if manual and ":" not in key else key
        rest = {k: v for k, v in entry.items() if k not in ("status", "updated_at")}
        note = json.dumps(rest, ensure_ascii=False) if rest else None
        cur = db.execute(
            "INSERT INTO follow_ups (owner, ref, contact_phone, due_at,"
            " priority, status, note, source, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,COALESCE(?, CURRENT_TIMESTAMP))"
            " ON CONFLICT DO NOTHING",
            (owner, ref, entry.get("contact_phone"),
             entry.get("follow_up_date"), entry.get("priority"),
             entry.get("status") or "pending", note, source,
             entry.get("updated_at")))
        if cur.rowcount == 1:
            imported += 1
        else:
            pre_existing += 1
    return len(items), imported, pre_existing, skipped


def _import_call_links(db, data, owner: str):
    if not isinstance(data, list):
        raise ValueError("call_link.json: expected a JSON array")
    imported = pre_existing = skipped = 0
    for e in data:
        phone = str(e.get("phone") or "").strip() if isinstance(e, dict) else ""
        if not phone or not e.get("dialed_at"):
            skipped += 1
            continue
        dup = db.execute(
            "SELECT 1 FROM call_links WHERE phone=? AND dialed_at=?",
            (phone, e["dialed_at"])).fetchone()
        if dup:
            pre_existing += 1
            continue
        db.execute(
            "INSERT INTO call_links (owner, phone, sig_digits, contact_id,"
            " name, agent_slug, dialed_at) VALUES (?,?,?,?,?,?,?)",
            (e.get("owner") or owner, phone, significant_digits(phone),
             e.get("contact_id"), e.get("name"), e.get("agent_slug"),
             e["dialed_at"]))
        imported += 1
    return len(data), imported, pre_existing, skipped


def _import_automations(db, data, owner: str):
    if not isinstance(data, list):
        raise ValueError("follow_up_automation.json: expected a JSON array")
    imported = pre_existing = skipped = 0
    for e in data:
        if not isinstance(e, dict) or not e.get("id"):
            skipped += 1
            continue
        cur = db.execute(
            "INSERT INTO followup_automations (legacy_id, owner, source_id,"
            " contact_name, contact_phone, past_call_context, call_aim,"
            " script_notes, scheduled_time, linked_contact_id, status,"
            " call_outcome, created_at, called_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (e["id"], owner, e.get("source_id"), e.get("contact_name"),
             e.get("contact_phone"), e.get("past_call_context"),
             e.get("call_aim"), e.get("script_notes"), e.get("scheduled_time"),
             e.get("linked_contact_id"), e.get("status") or "scheduled",
             e.get("call_outcome"), e.get("created_at"), e.get("called_at")))
        if cur.rowcount == 1:
            imported += 1
        else:
            pre_existing += 1
    return len(data), imported, pre_existing, skipped


# Single-object stores that become one settings row each (owner='' = global).
_SETTINGS_FILES = [
    ("minutes_quota.json", "minutes_quota"),
    ("sarvam_credits.json", "sarvam_credits"),
    ("api_key_state.json", "api_key_state"),
]


def _import_settings(db, data, key: str):
    if not isinstance(data, dict):
        raise ValueError(f"{key}: expected a JSON object")
    cur = db.execute(
        "INSERT INTO settings (owner, key, value) VALUES ('',?,?)"
        " ON CONFLICT DO NOTHING",
        (key, json.dumps(data, ensure_ascii=False)))
    imported = 1 if cur.rowcount == 1 else 0
    return 1, imported, 1 - imported, 0


def _db_count(db, table: str, owner: str, source: str | None = None) -> int:
    if table == "call_links":
        # Dial records keep whatever owner placed the dial (may be '' or
        # another tenant) — count the whole table.
        return db.execute("SELECT COUNT(*) AS n FROM call_links").fetchone()["n"]
    if source is None:
        row = db.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE owner=?", (owner,)).fetchone()
    else:
        row = db.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE owner=? AND source=?",
            (owner, source)).fetchone()
    return row["n"]


# (filename, importer(db, data, owner), target table, follow_ups source filter or None)
_STORES = [
    ("outbound_contacts.json",
     lambda db, data, owner: _import_contacts(db, data, owner),
     "contacts", None),
    ("call_analysis.json",
     lambda db, data, owner: _import_calls(db, data, owner),
     "calls", None),
    ("follow_ups.json",
     lambda db, data, owner: _import_follow_ups(db, data, owner, "follow_ups.json"),
     "follow_ups", "follow_ups.json"),
    ("manual_followups.json",
     lambda db, data, owner: _import_follow_ups(
         db, data, owner, "manual_followups.json", manual=True),
     "follow_ups", "manual_followups.json"),
    ("follow_up_automation.json",
     lambda db, data, owner: _import_automations(db, data, owner),
     "followup_automations", None),
    ("call_link.json",
     lambda db, data, owner: _import_call_links(db, data, owner),
     "call_links", None),
]


def import_all(url: str | None = None, dashboard_dir=None,
               owner: str = DEFAULT_OWNER) -> dict:
    """Import every dashboard store; return the verification report dict."""
    dashboard_dir = Path(dashboard_dir or DEFAULT_DASHBOARD_DIR)
    report = {"dashboard_dir": str(dashboard_dir), "owner": owner,
              "stores": {}, "ok": True}
    for filename, importer, table, source in _STORES:
        data, error = _read_json(dashboard_dir / filename)
        if error is not None:
            report["stores"][filename] = {"ok": False, "error": error}
            report["ok"] = False
            continue
        if data is _MISSING:
            with get_db(url) as db:
                after = _db_count(db, table, owner, source)
            report["stores"][filename] = {
                "source_count": 0, "imported": 0, "pre_existing": 0,
                "skipped_invalid": 0, "db_count_after": after,
                "missing": True, "ok": True}
            continue
        try:
            with get_db(url) as db:
                source_count, imported, pre_existing, skipped = importer(db, data, owner)
                after = _db_count(db, table, owner, source)
        except ValueError as exc:  # unexpected top-level shape; nothing committed
            report["stores"][filename] = {"ok": False, "error": str(exc)}
            report["ok"] = False
            continue
        ok = (source_count == imported + pre_existing + skipped
              and after >= imported)
        report["stores"][filename] = {
            "source_count": source_count, "imported": imported,
            "pre_existing": pre_existing, "skipped_invalid": skipped,
            "db_count_after": after, "ok": ok}
        if not ok:
            report["ok"] = False

    # Per-agent bookings.json files → bookings table (keyed by agent slug).
    agents_dir = Path("/home/angelo/lescall/calling-agent/agents")
    b_src = b_imported = b_pre = b_skipped = 0
    b_ok = True
    for bpath in sorted(agents_dir.glob("*/bookings.json")):
        slug = bpath.parent.name
        data, error = _read_json(bpath)
        if error is not None or data is _MISSING or not isinstance(data, list):
            if error is not None:
                b_ok = False
            continue
        b_src += len(data)
        with get_db(url) as db:
            for e in data:
                if not isinstance(e, dict) or not e.get("appointment_time"):
                    b_skipped += 1
                    continue
                dup = db.execute(
                    "SELECT 1 FROM bookings WHERE agent_slug=? AND"
                    " appointment_time=? AND conn_id=? AND booked_at=?",
                    (slug, e.get("appointment_time"), e.get("conn_id") or "",
                     e.get("booked_at"))).fetchone()
                if dup:
                    b_pre += 1
                    continue
                db.execute(
                    "INSERT INTO bookings (agent_slug, slot_day, slot_time,"
                    " appointment_time, caller_name, caller_phone, conn_id,"
                    " booked_at) VALUES (?,?,?,?,?,?,?,?)",
                    (slug, e.get("slot_day"), e.get("slot_time"),
                     e.get("appointment_time"), e.get("caller_name"),
                     e.get("caller_phone"), e.get("conn_id") or "",
                     e.get("booked_at")))
                b_imported += 1
    report["stores"]["agents/*/bookings.json"] = {
        "source_count": b_src, "imported": b_imported, "pre_existing": b_pre,
        "skipped_invalid": b_skipped, "ok": b_ok}
    if not b_ok:
        report["ok"] = False

    # Single-object settings stores (global rows, owner='').
    for filename, key in _SETTINGS_FILES:
        data, error = _read_json(dashboard_dir / filename)
        if error is not None:
            report["stores"][filename] = {"ok": False, "error": error}
            report["ok"] = False
            continue
        if data is _MISSING:
            report["stores"][filename] = {"source_count": 0, "imported": 0,
                                          "missing": True, "ok": True}
            continue
        try:
            with get_db(url) as db:
                _, imported, pre_existing, _ = _import_settings(db, data, key)
        except ValueError as exc:
            report["stores"][filename] = {"ok": False, "error": str(exc)}
            report["ok"] = False
            continue
        report["stores"][filename] = {"source_count": 1, "imported": imported,
                                      "pre_existing": pre_existing, "ok": True}

    # Go-live guard: the telephony bridge writes calls rows keyed by conn_id
    # (call_key NULL) while this importer keys by call_key (conn_id NULL) —
    # the two cannot be joined, so importing call_analysis AFTER the bridge
    # has started persisting duplicates those calls. Warn loudly.
    try:
        with get_db(url) as db:
            bridge_rows = db.execute(
                "SELECT COUNT(*) AS n FROM calls "
                "WHERE conn_id IS NOT NULL AND call_key IS NULL").fetchone()["n"]
        if bridge_rows:
            report["warning"] = (
                f"{bridge_rows} live bridge-written calls row(s) present — "
                "call_analysis imports for calls made after go-live create "
                "DUPLICATE rows (unjoinable keys). This import is meant to run "
                "before the bridge starts persisting (Phase 1).")
    except Exception:
        pass
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Import dashboard JSON stores into the multi-tenant DB "
                    "(idempotent; files stay source of truth).")
    parser.add_argument("--db-url", default=None,
                        help="Database URL (default: LESCALL_DB_URL or the repo sqlite file)")
    parser.add_argument("--dashboard-dir", default=None,
                        help=f"Dashboard JSON dir (default: {DEFAULT_DASHBOARD_DIR})")
    parser.add_argument("--owner", default=DEFAULT_OWNER,
                        help=f"Tenant email to own imported rows (default: {DEFAULT_OWNER})")
    args = parser.parse_args(argv)
    report = import_all(url=args.db_url, dashboard_dir=args.dashboard_dir,
                        owner=args.owner)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
