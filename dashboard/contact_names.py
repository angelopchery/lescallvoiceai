"""Phone number → saved contact name, for one tenant.

Every list in the app that renders a caller wants the same thing: the name the
number is saved under in the tenant's phone book, with the digits kept as the
secondary line. The mapping lives here — not in server.py — because the
intelligence router needs it too and cannot import server.py (server.py mounts
that router, so the import only goes one way).

`calls.contact_id` is bound when the call happens, which covers every call
placed or received after the contact was saved. This map covers the other
direction: save a contact today and yesterday's calls from that number start
reading as that person.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_contact_name_cache: dict[str, tuple[float, dict]] = {}
_CONTACT_NAME_TTL = 30.0


def contact_name_map(owner: str) -> dict:
    """{significant digits -> saved contact name} for one tenant.

    Keyed on the last 10 significant digits, the same key the contacts unique
    index uses, so "+9198…", "098…" and "98…" all resolve to the same person.
    Short-cached: this is called per list request, and a phone book changes far
    more slowly than it is rendered.
    """
    now = time.monotonic()
    hit = _contact_name_cache.get(owner)
    if hit and (now - hit[0]) < _CONTACT_NAME_TTL:
        return hit[1]
    out: dict[str, str] = {}
    try:
        from shared.db.database import get_db
        with get_db() as db:
            for r in db.execute(
                    "SELECT sig_digits, name FROM contacts "
                    " WHERE owner=? AND name IS NOT NULL AND name <> ''",
                    (owner,)).fetchall():
                row = dict(r)
                sig = (row.get("sig_digits") or "").strip()
                if sig:
                    out[sig] = row["name"]
    except Exception as exc:
        logger.warning("contact name map failed for %s: %r", owner, exc)
    _contact_name_cache[owner] = (now, out)
    return out


def name_for_phone(phone: str, names: dict) -> str:
    """Saved contact name for a raw phone string, or '' when it is not in the
    tenant's phone book."""
    if not phone:
        return ""
    try:
        from shared.phone import significant_digits
        return names.get(significant_digits(phone) or "", "")
    except Exception:
        return ""


def label_rows(owner: str, rows: list, phone_key: str = "phone",
               name_key: str = "contact_name") -> list:
    """Fill `name_key` on rows that don't already carry a name.

    Rows that resolved through a real `contact_id` join keep the name they
    came with — that binding is exact, while this one matches on digits.
    """
    if not rows:
        return rows
    names = contact_name_map(owner)
    for row in rows:
        if (row.get(name_key) or "").strip():
            continue
        row[name_key] = name_for_phone(str(row.get(phone_key) or ""), names)
    return rows
