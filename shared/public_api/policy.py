"""Per-tenant API access policy — the operator-owned half of the auth chain.

Two things live here and they are deliberately NOT tenant-editable:

  * **the IP allowlist**, which says which of a client's machines may reach the
    API at all. A tenant who could edit this could undo the restriction their
    own security team asked us for.
  * **the rate ceiling**, for the same reason plus a practical one: the limiter
    protects a 2-core VM that also carries live calls.

A tenant CAN narrow both on their own keys (`api_keys.ip_allowlist`,
`api_keys.rate_per_min`) — narrowing is safe, widening is not, and
`effective_limits()` enforces that direction by taking the minimum.

Allowlist entries are objects so one named machine can be given its own budget:

    {"cidr": "203.0.113.9/32", "label": "Pramukh CRM prod", "rate_per_min": 300}

Operators type them as lines (`cidr | label | per_min | per_day`) in the admin
console; `parse_allowlist_text` is the parser and `format_allowlist_text` the
inverse, so the textarea round-trips exactly what is stored.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time

logger = logging.getLogger("public_api.policy")

# What a tenant with no policy row gets. Generous enough that an integration
# works the moment a key is minted (nobody has to file a ticket to start), tight
# enough that a runaway loop cannot flatten the box.
DEFAULTS = {
    "enabled": 1,
    "ip_allowlist": [],
    "rate_per_min": 60,
    "rate_per_day": 20000,
    "max_keys": 5,
}

# Hard ceilings on what the admin console will accept. A typo of 6000000 in the
# per-minute box should be refused at the form, not discovered by the VM.
MAX_PER_MIN = 6000
MAX_PER_DAY = 2_000_000
MAX_KEYS = 50


def _loads(raw, default):
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
        return val if val is not None else default
    except Exception:
        return default


def normalize_allowlist(entries) -> list[dict]:
    """Accept bare CIDR strings or entry objects; return entry objects.

    Bare strings are supported because that is what a tenant-side key allowlist
    is, and because an operator pasting a plain list of IPs should just work.
    Invalid entries are DROPPED with a log line rather than raising: a policy
    row that fails to parse must not lock a client out of the API on read.
    Writers validate up front, so a drop here means the row was hand-edited.
    """
    out: list[dict] = []
    for raw in (entries or []):
        if isinstance(raw, str):
            entry = {"cidr": raw.strip()}
        elif isinstance(raw, dict):
            entry = dict(raw)
        else:
            continue
        cidr = str(entry.get("cidr") or "").strip()
        if not cidr:
            continue
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            logger.warning("dropping unparseable allowlist entry %r", cidr)
            continue
        clean = {"cidr": str(net), "label": str(entry.get("label") or "").strip()[:80]}
        for field in ("rate_per_min", "rate_per_day"):
            val = entry.get(field)
            if val in (None, "", 0):
                continue
            try:
                clean[field] = max(1, int(val))
            except (TypeError, ValueError):
                continue
        out.append(clean)
    return out


def parse_allowlist_text(text: str) -> tuple[list[dict], list[str]]:
    """Parse the admin textarea. Returns (entries, errors).

    One entry per line:  cidr | label | rate_per_min | rate_per_day
    Everything after the CIDR is optional. `#` starts a comment.
    """
    entries: list[dict] = []
    errors: list[str] = []
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        cidr = parts[0]
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            errors.append(f"line {lineno}: {cidr!r} is not an IP or CIDR block")
            continue
        entry: dict = {"cidr": str(net), "label": parts[1][:80] if len(parts) > 1 else ""}
        for idx, field, cap in ((2, "rate_per_min", MAX_PER_MIN),
                                (3, "rate_per_day", MAX_PER_DAY)):
            if len(parts) > idx and parts[idx]:
                try:
                    val = int(parts[idx])
                except ValueError:
                    errors.append(f"line {lineno}: {parts[idx]!r} is not a number")
                    continue
                if not 1 <= val <= cap:
                    errors.append(f"line {lineno}: {field} must be 1..{cap}")
                    continue
                entry[field] = val
        entries.append(entry)
    return entries, errors


def format_allowlist_text(entries) -> str:
    lines = []
    for e in normalize_allowlist(entries):
        bits = [e["cidr"], e.get("label", "")]
        bits.append(str(e.get("rate_per_min", "")))
        bits.append(str(e.get("rate_per_day", "")))
        while len(bits) > 1 and not bits[-1]:
            bits.pop()
        lines.append(" | ".join(bits))
    return "\n".join(lines)


def match_ip(ip: str, entries) -> tuple[bool, dict | None]:
    """(allowed, matching entry). An EMPTY list allows everything.

    The most specific match wins, so a /32 carrying its own budget beats the
    /16 it sits inside — otherwise the order rows happen to be typed in would
    decide which limit a client gets.
    """
    normalized = normalize_allowlist(entries)
    if not normalized:
        return True, None
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False, None
    best: tuple[int, dict] | None = None
    for entry in normalized:
        net = ipaddress.ip_network(entry["cidr"], strict=False)
        if addr.version != net.version or addr not in net:
            continue
        if best is None or net.prefixlen > best[0]:
            best = (net.prefixlen, entry)
    return (best is not None), (best[1] if best else None)


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

# The policy is read on EVERY api request, so it gets a short in-process cache.
#
# The staleness this buys is bounded and deliberate: an operator who tightens an
# allowlist or drops a limit sees it take effect within _CACHE_TTL_S, not
# instantly. Ten seconds is the trade — it removes a database round trip from
# the hot path (including from the path a client hammering a 429 takes) while
# staying far inside "I changed it and it worked" for a human at the console.
# Writes through `upsert_policy` invalidate immediately in the writing process.
#
# What is NOT cached: the key row itself. Revoking a key must be instant, and it
# is — every request re-reads api_keys.
_CACHE_TTL_S = 10
_cache: dict[str, tuple[float, dict]] = {}


def invalidate(owner: str = "") -> None:
    if owner:
        _cache.pop((owner or "").strip().lower(), None)
    else:
        _cache.clear()


def get_policy(owner: str, use_cache: bool = True) -> dict:
    """The tenant's policy, defaults included. Never raises for a missing row —
    "no policy" is a legitimate state that means "platform defaults"."""
    owner = (owner or "").strip().lower()
    if use_cache:
        hit = _cache.get(owner)
        if hit and hit[0] > time.monotonic():
            return hit[1]
    row = None
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM api_access_policy WHERE owner=?", (owner,)).fetchone()
    except Exception as exc:
        logger.warning("policy read failed for %s: %r", owner, exc)
    if not row:
        pol = {"owner": owner, **DEFAULTS, "provisional": True,
               "note": "", "updated_by": None, "updated_at": None}
    else:
        pol = dict(row)
        pol["ip_allowlist"] = normalize_allowlist(_loads(pol.get("ip_allowlist"), []))
        pol["enabled"] = int(pol.get("enabled") or 0)
        pol["provisional"] = False
    _cache[owner] = (time.monotonic() + _CACHE_TTL_S, pol)
    return pol


def upsert_policy(owner: str, *, enabled: bool, ip_allowlist: list,
                  rate_per_min: int, rate_per_day: int, max_keys: int,
                  note: str = "", actor: str = "admin") -> dict:
    owner = (owner or "").strip().lower()
    if not owner:
        raise ValueError("owner required")
    rate_per_min = max(1, min(int(rate_per_min), MAX_PER_MIN))
    rate_per_day = max(1, min(int(rate_per_day), MAX_PER_DAY))
    max_keys = max(0, min(int(max_keys), MAX_KEYS))
    entries = normalize_allowlist(ip_allowlist)
    payload = json.dumps(entries, ensure_ascii=False)
    from shared.db.database import get_db
    with get_db() as db:
        db.execute(
            "INSERT INTO api_access_policy (owner, enabled, ip_allowlist, "
            " rate_per_min, rate_per_day, max_keys, note, updated_by, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?, now()) "
            "ON CONFLICT (owner) DO UPDATE SET enabled=excluded.enabled, "
            " ip_allowlist=excluded.ip_allowlist, rate_per_min=excluded.rate_per_min, "
            " rate_per_day=excluded.rate_per_day, max_keys=excluded.max_keys, "
            " note=excluded.note, updated_by=excluded.updated_by, updated_at=now()",
            (owner, 1 if enabled else 0, payload, rate_per_min, rate_per_day,
             max_keys, note[:500], actor))
    invalidate(owner)
    logger.info("[api] policy updated for %s by %s (ips=%d, %d/min, %d/day, enabled=%s)",
                owner, actor, len(entries), rate_per_min, rate_per_day, enabled)
    return get_policy(owner, use_cache=False)


def account_ceiling(policy: dict) -> dict:
    """The aggregate budget for everything the account is doing, across all its
    keys.

    Set to the MOST GENEROUS rule anyone in this account was granted — the base
    policy, or a higher per-IP allowance if one was deliberately given. Two
    properties fall out of that choice, and both matter:

      * minting more keys cannot buy more throughput, so the operator's number
        is a real ceiling rather than a per-key suggestion;
      * an explicit per-IP grant is never quietly cancelled by an account
        window set below it, which would make the grant look broken.
    """
    per_min = int(policy.get("rate_per_min") or DEFAULTS["rate_per_min"])
    per_day = int(policy.get("rate_per_day") or DEFAULTS["rate_per_day"])
    for entry in normalize_allowlist(policy.get("ip_allowlist")):
        per_min = max(per_min, int(entry.get("rate_per_min") or 0))
        per_day = max(per_day, int(entry.get("rate_per_day") or 0))
    return {"per_min": max(1, per_min), "per_day": max(1, per_day)}


def effective_limits(policy: dict, key: dict, entry: dict | None) -> dict:
    """The limits that actually apply to this request.

    Precedence is "the tightest wins", computed as a minimum rather than a
    first-match: the policy is the ceiling, a per-IP entry may RAISE it for that
    one named machine (that is its whole purpose), and a per-key value may only
    lower whatever it lands on.
    """
    per_min = int(policy.get("rate_per_min") or DEFAULTS["rate_per_min"])
    per_day = int(policy.get("rate_per_day") or DEFAULTS["rate_per_day"])
    if entry:
        per_min = int(entry.get("rate_per_min") or per_min)
        per_day = int(entry.get("rate_per_day") or per_day)
    if key.get("rate_per_min"):
        per_min = min(per_min, int(key["rate_per_min"]))
    if key.get("rate_per_day"):
        per_day = min(per_day, int(key["rate_per_day"]))
    return {"per_min": max(1, per_min), "per_day": max(1, per_day)}
