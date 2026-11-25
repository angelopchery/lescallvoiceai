"""Every SQL statement the WhatsApp feature runs.

All statements are constant strings with bind parameters (tests/test_sql_safety
is a gate). Timestamps come back from this database layer as STRINGS, so every
time comparison is done in SQL, never in Python.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from shared.db.database import get_db

SEND_STATUSES = ("pending_approval", "queued", "sending", "sent", "delivered",
                 "read", "failed", "skipped", "expired", "blocked")
# A send in one of these can still be acted on (approved, skipped, delivered).
OPEN_STATUSES = ("pending_approval", "queued")


def _loads(raw: Any, default):
    if isinstance(raw, (dict, list)):
        return raw
    try:
        v = json.loads(raw or "null")
    except (TypeError, ValueError):
        return default
    return default if v is None else v


# ---------------------------------------------------------------------------
# Account (one row per tenant; connection fields NULL until connected)
# ---------------------------------------------------------------------------

_ACCOUNT_PUBLIC = (
    "owner, provider, waba_id, phone_number_id, display_phone, verified_name, status,"
    " quality_rating, messaging_limit, last_error, connected_at, checked_at,"
    " default_approval, send_window_start, send_window_end, send_delay_min,"
    " daily_cap, approval_ttl_h, webhook_token,"
    " (token_enc IS NOT NULL) AS has_token,"
    " (webhook_secret_enc IS NOT NULL) AS has_webhook_secret")


def get_account(owner: str) -> dict[str, Any]:
    with get_db() as db:
        db.execute("INSERT INTO wa_accounts (owner) VALUES (?) ON CONFLICT (owner) DO NOTHING",
                   (owner,))
        row = db.execute("SELECT " + _ACCOUNT_PUBLIC + " FROM wa_accounts WHERE owner=?",
                         (owner,)).fetchone()
    return dict(row)


def account_token_blob(owner: str) -> str | None:
    with get_db() as db:
        row = db.execute("SELECT token_enc FROM wa_accounts WHERE owner=?",
                         (owner,)).fetchone()
    return row["token_enc"] if row else None


def account_by_phone_number_id(phone_number_id: str) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute("SELECT " + _ACCOUNT_PUBLIC + " FROM wa_accounts"
                         " WHERE phone_number_id=?", (phone_number_id,)).fetchone()
    return dict(row) if row else None


def phone_number_id_taken(phone_number_id: str, owner: str) -> bool:
    with get_db() as db:
        row = db.execute("SELECT 1 FROM wa_accounts WHERE phone_number_id=? AND owner<>?",
                         (phone_number_id, owner)).fetchone()
    return bool(row)


def save_connection(owner: str, *, waba_id: str, phone_number_id: str,
                    display_phone: str, verified_name: str, token_enc: str,
                    quality_rating: str | None, messaging_limit: str | None) -> None:
    get_account(owner)
    with get_db() as db:
        db.execute(
            """UPDATE wa_accounts SET provider='meta', waba_id=?, phone_number_id=?,
                      display_phone=?, verified_name=?, token_enc=?, status='connected',
                      quality_rating=?, messaging_limit=?, last_error=NULL,
                      connected_at=now(), checked_at=now(), updated_at=now()
                WHERE owner=?""",
            (waba_id, phone_number_id, display_phone, verified_name, token_enc,
             quality_rating, messaging_limit, owner))


def save_interakt_connection(owner: str, *, token_enc: str, display_phone: str,
                             webhook_secret_enc: str | None) -> None:
    """Connect through Interakt (RUNBOOK §80): the API key is the whole
    credential. The Meta-only columns are cleared so nothing reads a stale
    phone_number_id; the webhook token is minted once and kept across
    reconnects so the URL the tenant pasted into Interakt stays valid."""
    get_account(owner)
    with get_db() as db:
        db.execute(
            """UPDATE wa_accounts SET provider='interakt', token_enc=?, display_phone=?,
                      waba_id=NULL, phone_number_id=NULL, verified_name=NULL,
                      quality_rating=NULL, messaging_limit=NULL,
                      webhook_secret_enc=COALESCE(?, webhook_secret_enc),
                      webhook_token=COALESCE(webhook_token, ?),
                      status='connected', last_error=NULL,
                      connected_at=now(), checked_at=now(), updated_at=now()
                WHERE owner=?""",
            (token_enc, display_phone or None, webhook_secret_enc,
             secrets.token_urlsafe(18), owner))


def set_webhook_secret(owner: str, webhook_secret_enc: str | None) -> None:
    with get_db() as db:
        db.execute(
            """UPDATE wa_accounts SET webhook_secret_enc=?,
                      webhook_token=COALESCE(webhook_token, ?), updated_at=now()
                WHERE owner=?""",
            (webhook_secret_enc, secrets.token_urlsafe(18), owner))


def account_webhook_secret_blob(owner: str) -> str | None:
    with get_db() as db:
        row = db.execute("SELECT webhook_secret_enc FROM wa_accounts WHERE owner=?",
                         (owner,)).fetchone()
    return row["webhook_secret_enc"] if row else None


def account_by_webhook_token(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    with get_db() as db:
        row = db.execute("SELECT " + _ACCOUNT_PUBLIC + " FROM wa_accounts"
                         " WHERE webhook_token=?", (token,)).fetchone()
    return dict(row) if row else None


def disconnect(owner: str) -> None:
    with get_db() as db:
        db.execute(
            """UPDATE wa_accounts SET token_enc=NULL, phone_number_id=NULL,
                      status='disconnected', last_error=NULL, updated_at=now()
                WHERE owner=?""", (owner,))


def set_account_health(owner: str, *, quality_rating: str | None = None,
                       messaging_limit: str | None = None,
                       error: str | None = None) -> None:
    with get_db() as db:
        db.execute(
            """UPDATE wa_accounts SET
                      quality_rating=COALESCE(?, quality_rating),
                      messaging_limit=COALESCE(?, messaging_limit),
                      last_error=?,
                      status=CASE WHEN token_enc IS NULL THEN 'disconnected'
                                  WHEN ?::text IS NULL THEN 'connected' ELSE 'error' END,
                      checked_at=now(), updated_at=now()
                WHERE owner=?""",
            (quality_rating, messaging_limit, error, error, owner))


def update_settings(owner: str, *, default_approval: str, send_window_start: int,
                    send_window_end: int, send_delay_min: int, daily_cap: int,
                    approval_ttl_h: int) -> dict[str, Any]:
    get_account(owner)
    with get_db() as db:
        db.execute(
            """UPDATE wa_accounts SET default_approval=?, send_window_start=?,
                      send_window_end=?, send_delay_min=?, daily_cap=?,
                      approval_ttl_h=?, updated_at=now()
                WHERE owner=?""",
            (default_approval, send_window_start, send_window_end, send_delay_min,
             daily_cap, approval_ttl_h, owner))
    return get_account(owner)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def replace_templates(owner: str, rows: list[dict[str, Any]]) -> int:
    """Make our copy of the WABA's templates exactly Meta's list. Templates the
    tenant registered by hand (source='manual') are not Meta's to delete."""
    with get_db() as db:
        db.execute("DELETE FROM wa_templates WHERE owner=? AND source='meta'", (owner,))
        for t in rows:
            db.execute(
                """INSERT INTO wa_templates (owner, name, language, category, status,
                          parameter_format, meta_id, components, rejected_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (owner, name, language) DO NOTHING""",
                (owner, t.get("name"), t.get("language"), t.get("category"),
                 t.get("status"), t.get("parameter_format"), str(t.get("id") or ""),
                 json.dumps(t.get("components") or [], ensure_ascii=False),
                 t.get("rejected_reason")))
    return len(rows)


def upsert_template(owner: str, t: dict[str, Any], source: str = "meta") -> int:
    with get_db() as db:
        row = db.execute(
            """INSERT INTO wa_templates (owner, name, language, category, status,
                      parameter_format, meta_id, components, rejected_reason, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (owner, name, language) DO UPDATE SET
                      category=EXCLUDED.category, status=EXCLUDED.status,
                      parameter_format=EXCLUDED.parameter_format,
                      meta_id=EXCLUDED.meta_id, components=EXCLUDED.components,
                      rejected_reason=EXCLUDED.rejected_reason, source=EXCLUDED.source,
                      synced_at=now()
               RETURNING id""",
            (owner, t.get("name"), t.get("language"), t.get("category"),
             t.get("status"), t.get("parameter_format"), str(t.get("id") or ""),
             json.dumps(t.get("components") or [], ensure_ascii=False),
             t.get("rejected_reason"), source)).fetchone()
    return int(row["id"])


def get_template(owner: str, template_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM wa_templates WHERE owner=? AND id=?",
                         (owner, template_id)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["components"] = _loads(d.get("components"), [])
    return d


def template_name_taken(owner: str, name: str, language: str, except_id: int | None) -> bool:
    with get_db() as db:
        row = db.execute(
            """SELECT 1 FROM wa_templates WHERE owner=? AND name=? AND language=?
                  AND (?::bigint IS NULL OR id<>?)""",
            (owner, name, language, except_id, except_id)).fetchone()
    return bool(row)


def update_manual_template(owner: str, template_id: int, t: dict[str, Any]) -> bool:
    """Edit a registered template in place. Playbooks reference templates by
    NAME, so a rename is carried to every playbook that used the old name."""
    with get_db() as db:
        cur = db.execute("SELECT name FROM wa_templates WHERE owner=? AND id=? AND source='manual'",
                         (owner, template_id)).fetchone()
        if not cur:
            return False
        db.execute(
            """UPDATE wa_templates SET name=?, language=?, category=?, status=?,
                      parameter_format=?, components=?, synced_at=now()
                WHERE owner=? AND id=?""",
            (t.get("name"), t.get("language"), t.get("category"), t.get("status"),
             t.get("parameter_format"),
             json.dumps(t.get("components") or [], ensure_ascii=False), owner, template_id))
        if cur["name"] != t.get("name"):
            db.execute("UPDATE wa_playbooks SET template_name=?, updated_at=now()"
                       " WHERE owner=? AND template_name=?",
                       (t.get("name"), owner, cur["name"]))
            db.execute("UPDATE wa_sends SET template_name=? WHERE owner=? AND template_name=?"
                       " AND status IN ('pending_approval','queued')",
                       (t.get("name"), owner, cur["name"]))
    return True


def delete_manual_template(owner: str, template_id: int) -> bool:
    with get_db() as db:
        cur = db.execute("DELETE FROM wa_templates WHERE owner=? AND id=? AND source='manual'",
                         (owner, template_id))
        return cur.rowcount > 0


def list_templates(owner: str) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM wa_templates WHERE owner=? ORDER BY name, language",
            (owner,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["components"] = _loads(d.get("components"), [])
        out.append(d)
    return out


def templates_named(owner: str, name: str) -> list[dict[str, Any]]:
    return [t for t in list_templates(owner) if t["name"] == name]


def set_template_status(meta_id: str, status: str, reason: str | None) -> int:
    with get_db() as db:
        cur = db.execute(
            "UPDATE wa_templates SET status=?, rejected_reason=?, synced_at=now()"
            " WHERE meta_id=?", (status, reason, meta_id))
        return max(cur.rowcount, 0)


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------

def insert_media(owner: str, *, filename: str, mime: str, size_bytes: int,
                 path: str) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """INSERT INTO wa_media (owner, filename, mime, size_bytes, path, public_token)
               VALUES (?, ?, ?, ?, ?, ?)
               RETURNING id, filename, mime, size_bytes, created_at, public_token""",
            (owner, filename, mime, size_bytes, path, secrets.token_urlsafe(16))).fetchone()
    return dict(row)


def media_by_public_token(token: str) -> dict[str, Any] | None:
    """The one file a /r/f/<token> address serves (RUNBOOK §80)."""
    if not token:
        return None
    with get_db() as db:
        row = db.execute("SELECT id, owner, filename, mime, size_bytes, path FROM wa_media"
                         " WHERE public_token=?", (token,)).fetchone()
    return dict(row) if row else None


def ensure_media_token(media_id: int) -> str | None:
    """Files uploaded before 0031 have no token; mint one on first use."""
    with get_db() as db:
        row = db.execute(
            """UPDATE wa_media SET public_token=COALESCE(public_token, ?)
                WHERE id=? RETURNING public_token""",
            (secrets.token_urlsafe(16), media_id)).fetchone()
    return row["public_token"] if row else None


def get_media(owner: str, media_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute(
            """SELECT *, (meta_uploaded_at IS NOT NULL
                          AND meta_uploaded_at > now() - interval '25 days') AS meta_fresh
                 FROM wa_media WHERE owner=? AND id=?""", (owner, media_id)).fetchone()
    return dict(row) if row else None


def list_media(owner: str) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            "SELECT id, filename, mime, size_bytes, created_at FROM wa_media"
            " WHERE owner=? ORDER BY id DESC", (owner,)).fetchall()
    return [dict(r) for r in rows]


def delete_media(owner: str, media_id: int) -> dict[str, Any] | None:
    """Remove a file from the library. Playbooks that attached it lose the
    attachment (sends then wait for a new one rather than fail)."""
    with get_db() as db:
        row = db.execute("SELECT id, path FROM wa_media WHERE owner=? AND id=?",
                         (owner, media_id)).fetchone()
        if not row:
            return None
        db.execute("UPDATE wa_playbooks SET media_id=NULL, updated_at=now()"
                   " WHERE owner=? AND media_id=?", (owner, media_id))
        db.execute("UPDATE wa_sends SET media_id=NULL WHERE owner=? AND media_id=?"
                   " AND status IN ('pending_approval','queued')", (owner, media_id))
        db.execute("DELETE FROM wa_media WHERE owner=? AND id=?", (owner, media_id))
    return dict(row)


def set_media_meta_id(media_id: int, meta_media_id: str) -> None:
    with get_db() as db:
        db.execute("UPDATE wa_media SET meta_media_id=?, meta_uploaded_at=now() WHERE id=?",
                   (meta_media_id, media_id))


# ---------------------------------------------------------------------------
# Playbooks
# ---------------------------------------------------------------------------

def _playbook(row) -> dict[str, Any]:
    d = dict(row)
    d["agent_slugs"] = _loads(d.get("agent_slugs"), [])
    d["variables"] = _loads(d.get("variables"), {})
    d["media_map"] = {str(k): int(v) for k, v in (_loads(d.get("media_map"), {}) or {}).items()
                      if str(v).isdigit()}
    return d


def list_playbooks(owner: str) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            """SELECT p.*, m.filename AS media_filename,
                      (SELECT count(*) FROM wa_sends s WHERE s.playbook_id=p.id) AS sends_total,
                      (SELECT count(*) FROM wa_sends s WHERE s.playbook_id=p.id
                          AND s.status='pending_approval') AS sends_pending
                 FROM wa_playbooks p LEFT JOIN wa_media m ON m.id=p.media_id
                WHERE p.owner=? ORDER BY p.id""", (owner,)).fetchall()
    return [_playbook(r) for r in rows]


def get_playbook(owner: str, playbook_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM wa_playbooks WHERE owner=? AND id=?",
                         (owner, playbook_id)).fetchone()
    return _playbook(row) if row else None


def enabled_playbooks(owner: str) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute("SELECT * FROM wa_playbooks WHERE owner=? AND enabled ORDER BY id",
                          (owner,)).fetchall()
    return [_playbook(r) for r in rows]


def save_playbook(owner: str, pb: dict[str, Any], playbook_id: int | None = None) -> dict[str, Any]:
    params = (pb["name"], bool(pb["enabled"]), json.dumps(pb["agent_slugs"]),
              bool(pb["require_booking"]), pb["recipient"], pb["template_name"],
              json.dumps(pb["variables"], ensure_ascii=False), pb["media_id"],
              pb["link_url"], pb["approval_mode"],
              json.dumps(pb.get("media_map") or {}, ensure_ascii=False))
    with get_db() as db:
        if playbook_id is None:
            row = db.execute(
                """INSERT INTO wa_playbooks (name, enabled, agent_slugs, require_booking,
                          recipient, template_name, variables, media_id, link_url,
                          approval_mode, media_map, owner)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                params + (owner,)).fetchone()
            playbook_id = row["id"]
        else:
            db.execute(
                """UPDATE wa_playbooks SET name=?, enabled=?, agent_slugs=?,
                          require_booking=?, recipient=?, template_name=?, variables=?,
                          media_id=?, link_url=?, approval_mode=?, media_map=?,
                          updated_at=now()
                    WHERE id=? AND owner=?""",
                params + (playbook_id, owner))
    return get_playbook(owner, playbook_id)


def delete_playbook(owner: str, playbook_id: int) -> bool:
    with get_db() as db:
        # Sends waiting on this playbook have nothing left to send.
        db.execute("""UPDATE wa_sends SET status='skipped', reason='playbook deleted',
                             updated_at=now()
                       WHERE owner=? AND playbook_id=? AND status IN ('pending_approval','queued')""",
                   (owner, playbook_id))
        cur = db.execute("DELETE FROM wa_playbooks WHERE owner=? AND id=?",
                         (owner, playbook_id))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Consent (append-only; the newest row for a phone decides)
# ---------------------------------------------------------------------------

def record_consent(owner: str, *, phone: str, contact_id: int | None, state: str,
                   source: str, call_id: int | None, quote: str) -> int | None:
    with get_db() as db:
        if call_id is not None:
            row = db.execute(
                """INSERT INTO wa_consents (owner, phone, contact_id, state, source, call_id, quote)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (owner, call_id) WHERE call_id IS NOT NULL DO NOTHING
                   RETURNING id""",
                (owner, phone, contact_id, state, source, call_id, quote)).fetchone()
            if row:
                return row["id"]
            row = db.execute("SELECT id FROM wa_consents WHERE owner=? AND call_id=?",
                             (owner, call_id)).fetchone()
            return row["id"] if row else None
        row = db.execute(
            """INSERT INTO wa_consents (owner, phone, contact_id, state, source, quote)
               VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
            (owner, phone, contact_id, state, source, quote)).fetchone()
        return row["id"]


def latest_consent(owner: str, phone: str) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM wa_consents WHERE owner=? AND phone=? ORDER BY id DESC LIMIT 1",
            (owner, phone)).fetchone()
    return dict(row) if row else None


def consents_for_contact(owner: str, contact_id: int) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM wa_consents WHERE owner=? AND contact_id=? ORDER BY id DESC LIMIT 50",
            (owner, contact_id)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Sends
# ---------------------------------------------------------------------------

def create_send(owner: str, *, playbook_id: int, call_id: int | None,
                contact_id: int | None, consent_id: int | None, to_phone: str,
                template_name: str, language: str, params: dict[str, str],
                status: str, reason: str | None, delay_min: int,
                ttl_h: int, project: str = "", media_id: int | None = None) -> int | None:
    """Insert once per (playbook, call). None = it already existed."""
    with get_db() as db:
        row = db.execute(
            """INSERT INTO wa_sends (owner, playbook_id, call_id, contact_id, consent_id,
                      to_phone, template_name, language, params, status, reason,
                      project, media_id, not_before, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       now() + make_interval(mins => ?::int),
                       now() + make_interval(hours => ?::int))
               ON CONFLICT (playbook_id, call_id) DO NOTHING
               RETURNING id""",
            (owner, playbook_id, call_id, contact_id, consent_id, to_phone,
             template_name, language, json.dumps(params, ensure_ascii=False),
             status, reason, project or None, media_id, delay_min, ttl_h)).fetchone()
    return row["id"] if row else None


def set_send_media(send_id: int, media_id: int | None) -> None:
    with get_db() as db:
        db.execute("UPDATE wa_sends SET media_id=?, updated_at=now() WHERE id=?",
                   (media_id, send_id))


_SEND_SELECT = """
    SELECT s.*, p.name AS playbook_name, p.approval_mode,
           c.name AS contact_name, cl.call_key, cl.agent_slug, cl.direction,
           cs.quote AS consent_quote, cs.source AS consent_source,
           m.filename AS media_filename,
           (SELECT COALESCE(sum(l.clicks), 0) FROM wa_links l WHERE l.send_id=s.id) AS clicks
      FROM wa_sends s
      LEFT JOIN wa_playbooks p ON p.id = s.playbook_id
      LEFT JOIN contacts c ON c.id = s.contact_id
      LEFT JOIN calls cl ON cl.id = s.call_id
      LEFT JOIN wa_consents cs ON cs.id = s.consent_id
      LEFT JOIN wa_media m ON m.id = s.media_id
"""


def _send(row) -> dict[str, Any]:
    d = dict(row)
    d["params"] = _loads(d.get("params"), {})
    return d


def list_sends(owner: str, *, statuses: list[str] | None, playbook_id: int | None,
               limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
    st = statuses or list(SEND_STATUSES)
    with get_db() as db:
        rows = db.execute(
            _SEND_SELECT + """ WHERE s.owner=? AND s.status = ANY(?)
                                 AND (?::bigint IS NULL OR s.playbook_id=?)
                               ORDER BY s.id DESC LIMIT ? OFFSET ?""",
            (owner, st, playbook_id, playbook_id, limit, offset)).fetchall()
        total = db.execute(
            """SELECT count(*) AS n FROM wa_sends s WHERE s.owner=? AND s.status = ANY(?)
                  AND (?::bigint IS NULL OR s.playbook_id=?)""",
            (owner, st, playbook_id, playbook_id)).fetchone()["n"]
    return [_send(r) for r in rows], int(total)


def get_send(owner: str, send_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute(_SEND_SELECT + " WHERE s.owner=? AND s.id=?",
                         (owner, send_id)).fetchone()
    return _send(row) if row else None


def sends_for_call(owner: str, call_id: int) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(_SEND_SELECT + " WHERE s.owner=? AND s.call_id=? ORDER BY s.id",
                          (owner, call_id)).fetchall()
    return [_send(r) for r in rows]


def update_send_params(owner: str, send_id: int, params: dict[str, str]) -> bool:
    with get_db() as db:
        cur = db.execute(
            """UPDATE wa_sends SET params=?, updated_at=now()
                WHERE owner=? AND id=? AND status IN ('pending_approval','queued')""",
            (json.dumps(params, ensure_ascii=False), owner, send_id))
        return cur.rowcount > 0


def approve_send(owner: str, send_id: int, by: str, ttl_h: int) -> bool:
    """pending_approval → queued. The approval clock restarts: a message
    approved late in its window still gets a full window to leave."""
    with get_db() as db:
        cur = db.execute(
            """UPDATE wa_sends SET status='queued', approved_by=?, approved_at=now(),
                      not_before=GREATEST(COALESCE(not_before, now()), now()),
                      expires_at=now() + make_interval(hours => ?::int), updated_at=now()
                WHERE owner=? AND id=? AND status='pending_approval'
                  AND (expires_at IS NULL OR expires_at > now())""",
            (by, ttl_h, owner, send_id))
        return cur.rowcount > 0


def skip_send(owner: str, send_id: int, by: str) -> bool:
    with get_db() as db:
        cur = db.execute(
            """UPDATE wa_sends SET status='skipped', reason=?, updated_at=now()
                WHERE owner=? AND id=? AND status IN ('pending_approval','queued')""",
            (f"skipped by {by}", owner, send_id))
        return cur.rowcount > 0


def expire_due() -> int:
    with get_db() as db:
        cur = db.execute(
            """UPDATE wa_sends SET status='expired',
                      reason=CASE WHEN status='pending_approval'
                                  THEN 'nobody approved it in time'
                                  ELSE COALESCE(reason, 'could not be sent in time') END,
                      updated_at=now()
                WHERE status IN ('pending_approval','queued') AND expires_at < now()""")
        return max(cur.rowcount, 0)


def claim_due(batch: int) -> list[dict[str, Any]]:
    """queued + due → sending, atomically. Two sender loops never take the
    same row (SKIP LOCKED), and a row is never sent twice."""
    with get_db() as db:
        rows = db.execute(
            """UPDATE wa_sends SET status='sending', attempts=attempts+1, updated_at=now()
                WHERE id IN (SELECT id FROM wa_sends
                              WHERE status='queued' AND (not_before IS NULL OR not_before <= now())
                              ORDER BY id LIMIT ? FOR UPDATE SKIP LOCKED)
            RETURNING *""", (batch,)).fetchall()
    return [_send(r) for r in rows]


def release_sending() -> int:
    """A row left 'sending' belongs to a process that died mid-request. Meta
    may or may not have it; re-queueing risks a duplicate, abandoning it
    risks a lost message. The row says which happened: no wamid = requeue."""
    with get_db() as db:
        cur = db.execute(
            """UPDATE wa_sends SET status='queued', updated_at=now()
                WHERE status='sending' AND wamid IS NULL
                  AND updated_at < now() - interval '2 minutes'""")
        return max(cur.rowcount, 0)


def defer_send(send_id: int, not_before, reason: str, *, undo_attempt: bool = True) -> None:
    with get_db() as db:
        db.execute(
            """UPDATE wa_sends SET status='queued', not_before=?, reason=?,
                      attempts=GREATEST(attempts - ?, 0), updated_at=now()
                WHERE id=?""",
            (not_before, reason, 1 if undo_attempt else 0, send_id))


def mark_sent(send_id: int, wamid: str) -> None:
    with get_db() as db:
        db.execute(
            """UPDATE wa_sends SET status='sent', wamid=?, sent_at=now(), reason=NULL,
                      error=NULL, error_code=NULL, updated_at=now() WHERE id=?""",
            (wamid, send_id))


def mark_final(send_id: int, status: str, reason: str, *,
               error_code: str | None = None) -> None:
    with get_db() as db:
        db.execute(
            """UPDATE wa_sends SET status=?, reason=?, error=?, error_code=?,
                      failed_at=CASE WHEN ?='failed' THEN now() ELSE failed_at END,
                      updated_at=now() WHERE id=?""",
            (status, reason, reason if status == "failed" else None, error_code,
             status, send_id))


_STATUS_RANK = {"sent": 1, "delivered": 2, "read": 3}


def apply_status(wamid: str, status: str, *, error_code: str | None,
                 error: str | None, pricing_category: str | None) -> dict[str, Any] | None:
    """A delivery receipt. Receipts arrive out of order ('read' before
    'delivered' is normal), so a status only ever moves FORWARD."""
    with get_db() as db:
        row = db.execute("SELECT id, owner, status, contact_id FROM wa_sends WHERE wamid=?",
                         (wamid,)).fetchone()
        if not row:
            return None
        cur_rank = _STATUS_RANK.get(row["status"], 0)
        if status == "failed":
            db.execute(
                """UPDATE wa_sends SET status='failed', error=?, error_code=?,
                          reason=?, failed_at=now(), updated_at=now() WHERE id=?""",
                (error, error_code, error, row["id"]))
        elif _STATUS_RANK.get(status, 0) > cur_rank and row["status"] != "failed":
            db.execute(
                """UPDATE wa_sends SET status=?,
                          delivered_at=CASE WHEN ? IN ('delivered','read')
                                            THEN COALESCE(delivered_at, now()) ELSE delivered_at END,
                          read_at=CASE WHEN ?='read' THEN now() ELSE read_at END,
                          pricing_category=COALESCE(?, pricing_category), updated_at=now()
                    WHERE id=?""",
                (status, status, status, pricing_category, row["id"]))
        elif pricing_category:
            db.execute("UPDATE wa_sends SET pricing_category=COALESCE(pricing_category, ?)"
                       " WHERE id=?", (pricing_category, row["id"]))
    return dict(row)


def sent_today(owner: str) -> int:
    """Messages that left today (IST day) — the daily cap's meter."""
    with get_db() as db:
        row = db.execute(
            """SELECT count(*) AS n FROM wa_sends
                WHERE owner=? AND sent_at IS NOT NULL
                  AND (sent_at AT TIME ZONE 'Asia/Kolkata')::date
                      = (now() AT TIME ZONE 'Asia/Kolkata')::date""",
            (owner,)).fetchone()
    return int(row["n"])


def cancel_open_sends_for_phone(owner: str, phone: str, reason: str) -> int:
    with get_db() as db:
        cur = db.execute(
            """UPDATE wa_sends SET status='blocked', reason=?, updated_at=now()
                WHERE owner=? AND to_phone=? AND status IN ('pending_approval','queued')""",
            (reason, owner, phone))
        return max(cur.rowcount, 0)


def counts(owner: str) -> dict[str, int]:
    with get_db() as db:
        rows = db.execute(
            """SELECT status, count(*) AS n FROM wa_sends
                WHERE owner=? AND created_at > now() - interval '30 days'
                GROUP BY status""", (owner,)).fetchall()
        clicks = db.execute(
            """SELECT COALESCE(sum(clicks), 0) AS n FROM wa_links
                WHERE owner=? AND created_at > now() - interval '30 days'""",
            (owner,)).fetchone()["n"]
        replies = db.execute(
            """SELECT count(*) AS n FROM wa_inbound
                WHERE owner=? AND received_at > now() - interval '30 days'""",
            (owner,)).fetchone()["n"]
        pending = db.execute(
            "SELECT count(*) AS n FROM wa_sends WHERE owner=? AND status='pending_approval'",
            (owner,)).fetchone()["n"]
    out = {s: 0 for s in SEND_STATUSES}
    for r in rows:
        out[r["status"]] = int(r["n"])
    out["clicks"] = int(clicks)
    out["replies"] = int(replies)
    out["pending_approval_now"] = int(pending)
    return out


def pending_count(owner: str) -> int:
    with get_db() as db:
        row = db.execute(
            "SELECT count(*) AS n FROM wa_sends WHERE owner=? AND status='pending_approval'"
            " AND (expires_at IS NULL OR expires_at > now())", (owner,)).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# Tracked links
# ---------------------------------------------------------------------------

def new_link_token() -> str:
    return secrets.token_urlsafe(8)


def create_link(owner: str, send_id: int, url: str, token: str | None = None) -> str:
    token = token or new_link_token()
    with get_db() as db:
        db.execute("INSERT INTO wa_links (token, owner, send_id, url) VALUES (?, ?, ?, ?)",
                   (token, owner, send_id, url))
    return token


def link_for_send(send_id: int) -> str | None:
    with get_db() as db:
        row = db.execute("SELECT token FROM wa_links WHERE send_id=? ORDER BY created_at LIMIT 1",
                         (send_id,)).fetchone()
    return row["token"] if row else None


def set_link_url(token: str, url: str) -> None:
    with get_db() as db:
        db.execute("UPDATE wa_links SET url=? WHERE token=?", (url, token))


def follow_link(token: str) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute(
            """UPDATE wa_links SET clicks=clicks+1,
                      first_clicked_at=COALESCE(first_clicked_at, now()),
                      last_clicked_at=now()
                WHERE token=? RETURNING url, owner, send_id, clicks""",
            (token,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Inbound messages + webhook log
# ---------------------------------------------------------------------------

def insert_inbound(owner: str, *, phone: str, contact_id: int | None, wamid: str,
                   kind: str, body: str, is_stop: bool) -> int | None:
    with get_db() as db:
        row = db.execute(
            """INSERT INTO wa_inbound (owner, phone, contact_id, wamid, kind, body, is_stop)
               VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (wamid) DO NOTHING RETURNING id""",
            (owner, phone, contact_id, wamid, kind, body, is_stop)).fetchone()
    return row["id"] if row else None


def list_inbound(owner: str, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
    with get_db() as db:
        rows = db.execute(
            """SELECT i.*, c.name AS contact_name FROM wa_inbound i
                 LEFT JOIN contacts c ON c.id=i.contact_id
                WHERE i.owner=? ORDER BY i.id DESC LIMIT ? OFFSET ?""",
            (owner, limit, offset)).fetchall()
        total = db.execute("SELECT count(*) AS n FROM wa_inbound WHERE owner=?",
                           (owner,)).fetchone()["n"]
    return [dict(r) for r in rows], int(total)


def record_webhook_event(event_key: str, payload: str) -> int | None:
    with get_db() as db:
        row = db.execute(
            """INSERT INTO wa_webhook_events (event_key, payload) VALUES (?, ?)
               ON CONFLICT (event_key) DO NOTHING RETURNING id""",
            (event_key, payload)).fetchone()
    return row["id"] if row else None


def finish_webhook_event(event_id: int, error: str | None) -> None:
    with get_db() as db:
        db.execute("UPDATE wa_webhook_events SET processed_at=now(), error=? WHERE id=?",
                   (error, event_id))


# ---------------------------------------------------------------------------
# Contacts (read-only lookups this feature needs)
# ---------------------------------------------------------------------------

def contact_by_phone(owner: str, sig_digits: str) -> dict[str, Any] | None:
    if not sig_digits:
        return None
    with get_db() as db:
        row = db.execute(
            "SELECT id, name, gender, phone FROM contacts WHERE owner=? AND sig_digits=?"
            " ORDER BY id LIMIT 1", (owner, sig_digits)).fetchone()
    return dict(row) if row else None


def contact_by_id(owner: str, contact_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute("SELECT id, name, gender, phone FROM contacts WHERE owner=? AND id=?",
                         (owner, contact_id)).fetchone()
    return dict(row) if row else None
