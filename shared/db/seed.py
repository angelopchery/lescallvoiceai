"""Seed the multi-tenant DB with the currently-live tenant data (plan §6 step 1).

Seeds four tables, all idempotently (INSERT ... ON CONFLICT DO NOTHING / update-only-if-NULL):
  users          — the existing operator account (tech@dhronai.com)
  agents         — one index row per live YAML agent slug in calling-agent/agents/
  trunks         — the existing Plivo outbound trunk (already live => 'applied')
  phone_numbers  — DID +918065953160 bound to that trunk and the active agent

Run from the repo root:
    python3 -m shared.db.seed [--db-url sqlite:///path/to.db]

Or programmatically:
    from shared.db.seed import seed
    summary = seed()          # dict of row counts per seeded table
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from shared.db.database import get_db
from shared.phone import significant_digits

try:  # PyYAML exists in calling-agent/venv but not necessarily system python
    import yaml
except ImportError:  # pragma: no cover - environment-dependent
    yaml = None

OWNER = "tech@dhronai.com"

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AGENTS_DIR = _REPO_ROOT / "calling-agent" / "agents"
DEFAULT_ACTIVE_AGENT_FILE = DEFAULT_AGENTS_DIR / "active_agent.json"
FALLBACK_AGENT_SLUG = "pramukh"

# The live Plivo OUTBOUND trunk (domain from /etc/freeswitch/sip_profiles/
# external/plivo.xml). provider_ips is intentionally empty: Plivo's signaling
# CIDRs are unknown here; the operator fills them in via the dashboard later.
DEFAULT_TRUNK = {
    "name": "tech-dhronai-com_plivo",
    "provider": "plivo",
    "owner": OWNER,
    "sip_domain": "25935838218519915.zt.plivo.com",
    "register": 0,
    "caller_id": "+918065953160",
    "provider_ips": "",
    "config_status": "applied",  # already live in FreeSWITCH
}

SEEDED_TABLES = ("users", "agents", "trunks", "phone_numbers")


def _display_name(yaml_path: Path, slug: str) -> str:
    """identity.agent_name from agent.yaml; regex fallback; slug as last resort."""
    try:
        text = yaml_path.read_text(encoding="utf-8")
    except OSError:
        return slug
    if yaml is not None:
        try:
            data = yaml.safe_load(text)
            name = data["identity"]["agent_name"]
            if isinstance(name, str) and name.strip():
                return name.strip()
        except Exception:
            pass  # malformed YAML — fall through to regex
    m = re.search(r"^\s*agent_name:\s*(.+?)\s*$", text, re.MULTILINE)
    if m:
        return m.group(1).strip().strip("'\"") or slug
    return slug


def _discover_agents(agents_dir: Path) -> list[tuple[str, str]]:
    """[(slug, display_name)] for every live agent dir containing agent.yaml."""
    agents: list[tuple[str, str]] = []
    if not agents_dir.is_dir():
        return agents
    for entry in sorted(agents_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        yaml_path = entry / "agent.yaml"
        if not yaml_path.is_file():
            continue
        agents.append((entry.name, _display_name(yaml_path, entry.name)))
    return agents


def _active_agent_slug(active_agent_file: Path) -> str:
    try:
        slug = json.loads(active_agent_file.read_text(encoding="utf-8"))["active"]
        if isinstance(slug, str) and slug.strip():
            return slug.strip()
    except Exception:
        pass
    return FALLBACK_AGENT_SLUG


def seed(url: str | None = None, *,
         agents_dir: Path | str | None = None,
         active_agent_file: Path | str | None = None,
         trunk: dict | None = None) -> dict:
    """Seed users/agents/trunks/phone_numbers. Idempotent; returns row counts."""
    agents_dir = Path(agents_dir or DEFAULT_AGENTS_DIR)
    active_agent_file = Path(active_agent_file or DEFAULT_ACTIVE_AGENT_FILE)
    trunk = {**DEFAULT_TRUNK, **(trunk or {})}

    agents = _discover_agents(agents_dir)
    active_slug = _active_agent_slug(active_agent_file)
    did = trunk["caller_id"]

    with get_db(url) as db:
        # 1. Operator account.
        db.execute("INSERT INTO users (email) VALUES (?) ON CONFLICT DO NOTHING",
                   (OWNER,))

        # 2. Agents index: insert by slug; backfill display_name only if NULL
        #    (never clobber an operator-edited name).
        for slug, display_name in agents:
            db.execute(
                "INSERT INTO agents (slug, owner, display_name) "
                "VALUES (?,?,?) ON CONFLICT DO NOTHING",
                (slug, OWNER, display_name))
            db.execute(
                "UPDATE agents SET display_name=? "
                "WHERE slug=? AND display_name IS NULL",
                (display_name, slug))

        # 3+4. Trunk + DID are BOOTSTRAP-ONLY: they seed an empty telephony
        #      config. Once ANY trunk exists the operator owns that data —
        #      re-running seed must not resurrect rows they deleted.
        any_trunk = db.execute("SELECT 1 FROM trunks LIMIT 1").fetchone()
        if any_trunk is None:
            db.execute(
                "INSERT INTO trunks "
                "(name, provider, owner, sip_domain, register, caller_id, "
                " provider_ips, config_status) VALUES (?,?,?,?,?,?,?,?)",
                (trunk["name"], trunk["provider"], trunk["owner"],
                 trunk["sip_domain"], trunk["register"], trunk["caller_id"],
                 trunk["provider_ips"], trunk["config_status"]))
            trunk_id = db.execute(
                "SELECT id FROM trunks WHERE name=?", (trunk["name"],)
            ).fetchone()["id"]

            # The DID, routed to the active agent in BOTH directions (a seeded
            # dev number should be able to answer and to dial out immediately).
            db.execute(
                "INSERT INTO phone_numbers "
                "(number, sig_digits, trunk_id, inbound_agent_slug, "
                " outbound_agent_slug, owner, direction, label, config_status) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (did, significant_digits(did), trunk_id, active_slug, active_slug,
                 OWNER, "both", "Plivo DID", "applied"))

        summary = {
            table: db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in SEEDED_TABLES
        }
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Seed the LesCall multi-tenant DB (plan §6 step 1).")
    parser.add_argument("--db-url", default=None,
                        help="Database URL (default: LESCALL_DB_URL or the "
                             "repo sqlite file)")
    args = parser.parse_args(argv)
    print(json.dumps(seed(args.db_url), indent=2))


if __name__ == "__main__":
    main()
