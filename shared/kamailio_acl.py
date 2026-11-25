"""Runtime sync of the Kamailio carrier allow-list (plan §4b).

Kamailio's permissions module reads an `address` table (grp 1 = trusted
carriers) from its own SQLite DB — deliberately separate from the app DB.
sync_carrier_acl() rewrites grp 1 from the union of all trunks' provider_ips
and triggers a live reload, so registering a trunk opens its carrier at the
SIP edge without restarting anything.

Reload order: `kamcmd permissions.addressReload` directly (the dashboard runs
on the host); if the ctl socket is unreachable, touch the trigger file that
the optional lescall-kamailio-reload.path systemd unit watches.

NEVER raises into the caller — a trunk "apply" must still succeed if Kamailio
is momentarily down; the ACL reconciles on the next apply or at boot via
deploy/lescall-kamailio-acl-seed.sh.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import sqlite3
import subprocess
from pathlib import Path

logger = logging.getLogger("kamailio_acl")

ACL_GROUP = int(os.environ.get("KAMAILIO_ACL_GROUP", "1"))
KAMAILIO_DB_PATH = os.environ.get("KAMAILIO_DB_PATH", "/etc/kamailio/kamailio.db")
KAMCMD = os.environ.get("KAMCMD", "kamcmd")
RELOAD_TRIGGER = Path(os.environ.get(
    "KAMAILIO_RELOAD_TRIGGER",
    str(Path(__file__).resolve().parent.parent / "data" / "kamailio_reload"),
))

# Matches kamailio 5.x permissions schema (see kamailio/schema-sqlite.sql).
_ADDRESS_DDL = (
    "CREATE TABLE IF NOT EXISTS address ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " grp INTEGER DEFAULT 1 NOT NULL,"
    " ip_addr VARCHAR(50) NOT NULL,"
    " mask INTEGER DEFAULT 32 NOT NULL,"
    " port SMALLINT DEFAULT 0 NOT NULL,"
    " tag VARCHAR(64))"
)


def parse_cidrs(cidrs) -> tuple[list[tuple[str, int]], list[str]]:
    """Normalize a list of CIDR/IP strings to (network_address, prefixlen).

    Accepts bare IPs (-> /32), CIDRs, and comma-separated strings; returns
    (parsed, skipped) with invalid entries skipped rather than fatal.
    """
    parsed: list[tuple[str, int]] = []
    skipped: list[str] = []
    seen: set[tuple[str, int]] = set()
    flat: list[str] = []
    for entry in cidrs or []:
        flat.extend(str(entry).split(","))
    for raw in flat:
        item = raw.strip()
        if not item:
            continue
        try:
            net = ipaddress.ip_network(item, strict=False)
            key = (str(net.network_address), net.prefixlen)
            if key not in seen:
                seen.add(key)
                parsed.append(key)
        except ValueError:
            skipped.append(item)
    return parsed, skipped


def _reload() -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [KAMCMD, "permissions.addressReload"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            return True, "kamcmd"
    except Exception as exc:
        logger.warning("kamcmd reload failed: %r", exc)
    try:
        RELOAD_TRIGGER.parent.mkdir(parents=True, exist_ok=True)
        RELOAD_TRIGGER.touch()
        return True, "trigger"
    except Exception as exc:
        logger.warning("reload trigger touch failed: %r", exc)
        return False, ""


def sync_carrier_acl(cidrs, db_path: str | None = None) -> dict:
    """Rewrite grp-ACL_GROUP of Kamailio's address table + live reload.

    Returns {"synced": n, "skipped": [...], "reloaded": bool, "reload_via": str}
    and on failure {"error": ...} — never raises.
    """
    parsed, skipped = parse_cidrs(cidrs)
    result: dict = {"synced": 0, "skipped": skipped, "reloaded": False, "reload_via": ""}
    try:
        with sqlite3.connect(db_path or KAMAILIO_DB_PATH, timeout=10) as conn:
            conn.execute(_ADDRESS_DDL)
            conn.execute("DELETE FROM address WHERE grp=?", (ACL_GROUP,))
            for ip, mask in parsed:
                conn.execute(
                    "INSERT INTO address (grp, ip_addr, mask, port, tag) "
                    "VALUES (?,?,?,0,'carrier')",
                    (ACL_GROUP, ip, mask),
                )
        result["synced"] = len(parsed)
    except Exception as exc:
        logger.warning("kamailio ACL DB write failed: %r", exc)
        result["error"] = repr(exc)
        return result
    result["reloaded"], result["reload_via"] = _reload()
    return result
