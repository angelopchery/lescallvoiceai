"""Contact writes that both the dashboard and the public API go through.

The dashboard's own contact endpoints are a load-all / mutate / save-all sync
(`server._save_outbound`) — fine for a page that already holds the whole list in
memory, wrong for an API that must apply one row without reading a tenant's
entire phone book first. So the row-level operations live here, and the two
surfaces share the rules that actually matter:

  * **identity is the last 10 significant digits**, matching the
    `idx_contacts_owner_sig` unique index — "+919876543210", "09876543210" and
    "9876543210" are one person under one owner;
  * **deleting re-homes** every intelligence row that referenced the contact
    instead of stranding it (see `detach_contact_references`);
  * **groups are owner-scoped**, and a name that does not exist yet is created
    rather than silently dropping the row into "No group".
"""

from __future__ import annotations

import datetime as dt
import json
import logging

from shared.phone import digits_only, significant_digits, to_e164

logger = logging.getLogger("shared.contacts")

RESERVED_GROUP_NAMES = ("all", "no group", "none", "ungrouped")


# What a Gender column actually holds in Indian contact sheets — English
# words and initials, Hindi and Gujarati words, and the honorific a CRM
# export sometimes puts in that column instead. Everything the call engine
# reads is "male" / "female"; any other value is stored as written and
# addresses the contact by name only (RUNBOOK §42 — never guessed).
_MALE_VALUES = frozenset({
    "m", "male", "man", "boy", "gents", "gent", "mr", "mr.", "sir",
    "पुरुष", "पुरूष", "आदमी", "मर्द", "पु", "पुरुष्", "પુરુષ", "પુ", "ભાઈ",
})
_FEMALE_VALUES = frozenset({
    "f", "female", "woman", "girl", "ladies", "lady", "mrs", "mrs.", "ms",
    "ms.", "miss", "madam", "ma'am", "maam",
    "महिला", "स्त्री", "औरत", "म", "સ્ત્રી", "મહિલા", "સ્ત્રી.", "બેન",
})


def normalize_gender(value: str) -> str:
    v = (value or "").strip().lower()
    if v in _MALE_VALUES:
        return "male"
    if v in _FEMALE_VALUES:
        return "female"
    return v[:32]


def detach_contact_references(db, pk: int) -> None:
    """Re-home intelligence rows before a contact row is deleted.

    Deleting a contact used to strand every row that referenced it: 90 of 154
    contact-linked followups pointed at nonexistent contacts after one
    phone-book cleanup (2026-07-23 audit) — the hub 404'd for the owner's
    most-called number and the follow-up auto-dialer permanently blocked 38
    live follow-ups with 'no_contact'. Deletion is normal user behavior, so
    the delete path must leave the data coherent:

      * if another contact with the same significant digits survives (the
        delete-and-recreate pattern), references move to it;
      * otherwise they detach to NULL — lists fall back to the raw phone
        number instead of linking to a 404.

    contact_events and customer_memory are only reachable through a live
    contact id, so they move on re-bind and are left in place on detach
    (ids are serial and never reused — the rows are inert, not dangling).
    """
    row = db.execute(
        "SELECT owner, sig_digits FROM contacts WHERE id=?", (pk,)).fetchone()
    if not row:
        return
    survivor = db.execute(
        "SELECT id FROM contacts WHERE owner=? AND sig_digits=? AND id<>? "
        "ORDER BY id DESC LIMIT 1",
        (row["owner"], row["sig_digits"], pk)).fetchone()
    new_id = survivor["id"] if survivor else None
    for table in ("calls", "call_intelligence", "commitments", "followups"):
        db.execute(f"UPDATE {table} SET contact_id=? WHERE contact_id=?",
                   (new_id, pk))
    if new_id is not None:
        db.execute("UPDATE contact_events SET contact_id=? WHERE contact_id=?",
                   (new_id, pk))
        # keep at most one memory row: prefer the survivor's if both exist
        has_target = db.execute(
            "SELECT 1 FROM customer_memory WHERE contact_id=?",
            (new_id,)).fetchone()
        if has_target:
            db.execute("DELETE FROM customer_memory WHERE contact_id=?", (pk,))
        else:
            db.execute(
                "UPDATE customer_memory SET contact_id=? WHERE contact_id=?",
                (new_id, pk))


def resolve_group(owner: str, group_id=None, group_name=None) -> int | None:
    """A caller-supplied group → an id this owner actually owns, or None.

    Owner-scoped both ways: an id belonging to another tenant resolves to None
    (No group), never to their group.
    """
    from shared.db.database import get_db
    if group_id not in (None, "", "none", "all"):
        try:
            gid = int(group_id)
        except (TypeError, ValueError):
            gid = None
        if gid is not None:
            with get_db() as db:
                row = db.execute(
                    "SELECT id FROM contact_groups WHERE id=? AND owner=?",
                    (gid, owner)).fetchone()
            if row:
                return int(row["id"])
    name = (str(group_name or "").strip())[:64]
    if not name or name.lower() in RESERVED_GROUP_NAMES:
        return None
    with get_db() as db:
        row = db.execute("SELECT id FROM contact_groups WHERE owner=? AND name=?",
                         (owner, name)).fetchone()
        if row:
            return int(row["id"])
        db.execute("INSERT INTO contact_groups (owner, name) VALUES (?,?)",
                   (owner, name))
        row = db.execute("SELECT id FROM contact_groups WHERE owner=? AND name=?",
                         (owner, name)).fetchone()
    return int(row["id"]) if row else None


class ContactError(ValueError):
    """A rejected write, with the sentence to show the caller."""


def upsert_contact(owner: str, *, phone: str, name: str = "", notes: str = "",
                   gender: str = "", fields: dict | None = None,
                   group_id: int | None = None, allow_update: bool = True,
                   source: str = "api") -> tuple[dict, bool]:
    """Create or update one contact. Returns (row, created).

    `allow_update=False` makes a matching number a conflict instead of an
    update — the difference between "sync my CRM into LesCall" and "add this
    new lead, and tell me if you already had them".

    Only fields the caller actually SENT are written: a CRM that pushes a phone
    number and a name must not blank out the notes an operator typed into the
    dashboard yesterday.
    """
    from shared.db.database import get_db

    raw = (phone or "").strip()
    e164 = to_e164(raw)
    if not e164 or len(digits_only(e164)) < 7:
        raise ContactError(f"{raw!r} is not a usable phone number "
                           f"(needs at least 7 digits, country code included)")
    sig = significant_digits(e164)
    details_json = json.dumps(fields or {}, ensure_ascii=False)

    with get_db() as db:
        existing = db.execute(
            "SELECT * FROM contacts WHERE owner=? AND sig_digits=?",
            (owner, sig)).fetchone()
        if existing:
            if not allow_update:
                raise ContactError("a contact with this number already exists")
            row = dict(existing)
            sets, vals = [], []
            if name:
                sets.append("name=?"), vals.append(name[:120])
            if notes:
                sets.append("notes=?"), vals.append(notes[:2000])
            if gender:
                sets.append("gender=?"), vals.append(normalize_gender(gender))
            if group_id is not None:
                sets.append("group_id=?"), vals.append(group_id)
            if fields:
                merged = {}
                try:
                    merged = json.loads(row.get("details") or "{}")
                    if not isinstance(merged, dict):
                        merged = {}
                except Exception:
                    merged = {}
                merged.update(fields)
                sets.append("details=?"), vals.append(json.dumps(merged, ensure_ascii=False))
            sets.append("updated_at=CURRENT_TIMESTAMP")
            db.execute(f"UPDATE contacts SET {', '.join(sets)} WHERE id=?",
                       (*vals, row["id"]))
            row = dict(db.execute("SELECT * FROM contacts WHERE id=?",
                                  (row["id"],)).fetchone())
            return row, False

        # The dashboard's string handle stays the UI's key; API-created rows get
        # one in the same shape so both surfaces list them identically.
        handle = f"{int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)}_{sig[-4:]}"
        db.execute(
            "INSERT INTO contacts (legacy_id, owner, phone, sig_digits, name, notes, "
            " call_status, disposition, call_count, call_notes, extra, added_at, "
            " gender, details, group_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (handle, owner, e164, sig, name[:120], notes[:2000],
             "not_called", "not_classified", 0, "",
             json.dumps({"call_history": [], "source": source}),
             dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
             normalize_gender(gender) or None, details_json, group_id))
        row = dict(db.execute(
            "SELECT * FROM contacts WHERE owner=? AND sig_digits=?",
            (owner, sig)).fetchone())
    return row, True


def renumber_contact(owner: str, pk: int, new_phone: str) -> dict:
    """Change an existing contact's phone number, keeping the row (and every
    call, commitment and memory bound to it).

    Needed because identity here is the number: without this, "the customer
    changed their number" pushed through an update would look like a brand-new
    person and silently fork the relationship in two. Refuses when the new
    number already belongs to another contact — merging two histories is a
    decision, not a side effect of a PATCH.
    """
    from shared.db.database import get_db

    e164 = to_e164(new_phone)
    if not e164 or len(digits_only(e164)) < 7:
        raise ContactError(f"{new_phone!r} is not a usable phone number")
    sig = significant_digits(e164)
    with get_db() as db:
        row = db.execute("SELECT * FROM contacts WHERE id=? AND owner=?",
                         (pk, owner)).fetchone()
        if not row:
            raise ContactError("no such contact")
        clash = db.execute(
            "SELECT id FROM contacts WHERE owner=? AND sig_digits=? AND id<>?",
            (owner, sig, pk)).fetchone()
        if clash:
            raise ContactError("another contact already has that number")
        db.execute("UPDATE contacts SET phone=?, sig_digits=?, "
                   " updated_at=CURRENT_TIMESTAMP WHERE id=?", (e164, sig, pk))
        return dict(db.execute("SELECT * FROM contacts WHERE id=?", (pk,)).fetchone())


def delete_contact(owner: str, pk: int) -> bool:
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute("SELECT id FROM contacts WHERE id=? AND owner=?",
                         (pk, owner)).fetchone()
        if not row:
            return False
        detach_contact_references(db, pk)
        db.execute("DELETE FROM contacts WHERE id=?", (pk,))
    return True
