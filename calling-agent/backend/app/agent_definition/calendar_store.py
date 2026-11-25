"""
Calendar bookings store — per-agent persistence of booked appointment slots.

Bookings live in the shared DB (`bookings` table, keyed by agent slug):

    {
      "slot_day": "Tomorrow",
      "slot_time": "10:00 AM",
      "appointment_time": "Tomorrow at 10:00 AM",
      "caller_name": "Ravi",
      "caller_phone": "+91...",
      "conn_id": "abc123",
      "booked_at": "2026-07-04T10:12:00Z"
    }

A slot from calendar.mock_slots is considered taken once one booking
references it, so availability shrinks as calls book slots. The legacy
per-agent bookings.json is a read-only fallback: mid-call availability
lookups must never raise, and offering a taken slot beats offering none.
"""
from __future__ import annotations

import datetime
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_AGENTS_DIR = Path(__file__).resolve().parents[3] / "agents"
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:  # shared.db lives at the repo root
    sys.path.insert(0, str(_REPO_ROOT))


def bookings_path(agent_folder: str) -> Path:
    """Legacy JSON location — kept for the read-only fallback."""
    return _AGENTS_DIR / agent_folder / "bookings.json"


def _row_to_dict(r: dict) -> dict:
    return {
        "slot_day": r["slot_day"] or "",
        "slot_time": r["slot_time"] or "",
        "appointment_time": r["appointment_time"] or "",
        "caller_name": r["caller_name"] or "",
        "caller_phone": r["caller_phone"] or "",
        "conn_id": r["conn_id"] or "",
        "booked_at": r["booked_at"],
    }


def _list_bookings_file(agent_folder: str) -> list[dict]:
    path = bookings_path(agent_folder)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("[calendar] could not read bookings for '%s': %s", agent_folder, exc)
        return []


def list_bookings(agent_folder: str) -> list[dict]:
    try:
        from shared.db.database import get_db
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM bookings WHERE agent_slug=? ORDER BY id",
                (agent_folder,)).fetchall()
        return [_row_to_dict(dict(r)) for r in rows]
    except Exception as exc:
        logger.warning("[calendar] DB bookings read failed for '%s' (%s) — "
                       "using legacy file", agent_folder, exc)
        return _list_bookings_file(agent_folder)


def add_booking(
    agent_folder: str,
    slot_day: str,
    slot_time: str,
    appointment_time: str,
    caller_name: str = "",
    caller_phone: str = "",
    conn_id: str = "",
) -> dict:
    """Record a booking. Duplicate (conn_id, appointment_time) pairs are ignored
    so re-confirmation within one call doesn't double-book."""
    for b in list_bookings(agent_folder):
        if conn_id and b.get("conn_id") == conn_id and b.get("appointment_time") == appointment_time:
            return b
    entry = {
        "slot_day": slot_day,
        "slot_time": slot_time,
        "appointment_time": appointment_time,
        "caller_name": caller_name,
        "caller_phone": caller_phone,
        "conn_id": conn_id,
        "booked_at": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    from shared.db.database import get_db
    with get_db() as db:
        db.execute(
            "INSERT INTO bookings (agent_slug, slot_day, slot_time,"
            " appointment_time, caller_name, caller_phone, conn_id, booked_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (agent_folder, slot_day, slot_time, appointment_time,
             caller_name, caller_phone, conn_id, entry["booked_at"]))
    logger.info("[calendar] booked %r for '%s' (caller=%s)", appointment_time, agent_folder, caller_name or "?")
    return entry


def delete_booking(agent_folder: str, index: int) -> bool:
    """Delete by list position — the builder UI's contract. Positions are the
    same ORDER BY id view that list_bookings returns."""
    from shared.db.database import get_db
    with get_db() as db:
        rows = db.execute(
            "SELECT id FROM bookings WHERE agent_slug=? ORDER BY id",
            (agent_folder,)).fetchall()
        if not (0 <= index < len(rows)):
            return False
        db.execute("DELETE FROM bookings WHERE id=?", (rows[index]["id"],))
    return True


def _slot_key(day: str, time: str) -> str:
    return f"{day.strip().lower()}|{time.strip().lower()}"


def booked_slot_keys(agent_folder: str) -> set[str]:
    keys = set()
    for b in list_bookings(agent_folder):
        day, time = b.get("slot_day", ""), b.get("slot_time", "")
        if day or time:
            keys.add(_slot_key(day, time))
    return keys


def available_slots(agent_def, agent_folder: str) -> list:
    """mock_slots minus already-booked slots. Falls back to all slots when the
    bookings store is unreachable — offering a taken slot beats offering none."""
    slots = list(getattr(agent_def.calendar, "mock_slots", []) or [])
    if not agent_folder:
        return slots
    try:
        taken = booked_slot_keys(agent_folder)
    except Exception:
        return slots
    if not taken:
        return slots
    return [s for s in slots if _slot_key(s.day, s.time) not in taken]
