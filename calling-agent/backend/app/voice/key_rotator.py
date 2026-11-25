"""
Hot-swap Sarvam API key from the shared dashboard pool on HTTP 402 errors.

Both STT and TTS call rotate() when they receive a 402.  The new key is
applied immediately to os.environ and written back to calling-agent/.env so
the next API call in the same process uses it without a restart.

The pool file is the same api_key_pool.json that the dashboard writes — no
separate storage.
"""
import json
import logging
import os
import re
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_POOL_FILE = Path(__file__).resolve().parents[4] / "dashboard" / "api_key_pool.json"
_ENV_FILE  = Path(__file__).resolve().parents[3] / ".env"
_lock = threading.Lock()


def get_active_key() -> str:
    return os.environ.get("SARVAM_API_KEY", "")


def current_key() -> str:
    """Return the live active key, re-read from .env each call.

    Reading .env fresh (instead of trusting a value cached at object
    construction) means a rotation performed by *any* actor — this process's
    own rotate() OR the external sarvam_key_watchdog service — takes effect on
    the very next STT/TTS request, with no process restart. Falls back to the
    process environment if .env is unreadable.
    """
    try:
        if _ENV_FILE.exists():
            m = re.search(
                r"^SARVAM_API_KEY=(.*)$",
                _ENV_FILE.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
            if m:
                key = m.group(1).strip()
                if key:
                    os.environ["SARVAM_API_KEY"] = key
                    return key
    except Exception:  # pragma: no cover - best effort, never break a call
        pass
    return os.environ.get("SARVAM_API_KEY", "")


def pool_size() -> int:
    return len(_load_pool())


# ── Per-call key leasing (2026-08-03, owner directive) ──────────────────
# Concurrent calls used to share the single active key — and its rate
# limits (429 stutter under load). Each call now leases a key round-robin
# across [active + backup pool] and keeps it until it 402s, then re-leases.
# The pool file stays untouched (leases don't pop keys); rotate() and the
# watchdog keep their exact semantics. Kill switch: SARVAM_PER_CALL_KEYS.
PER_CALL_KEYS = os.environ.get(
    "SARVAM_PER_CALL_KEYS", "true").lower() == "true"
_lease_idx = 0
_exhausted: set[str] = set()


def lease_key(conn_id: str = "") -> str:
    global _lease_idx
    with _lock:
        candidates = [current_key()] + [k for k in _load_pool() if k]
        seen: list[str] = []
        for k in candidates:
            if k and k not in _exhausted and k not in seen:
                seen.append(k)
        if not seen:
            return current_key()
        key = seen[_lease_idx % len(seen)]
        _lease_idx += 1
        logger.info("[KEY-LEASE] %s → %s…%s (%d usable)",
                    conn_id or "-", key[:8], key[-4:], len(seen))
        return key


def mark_exhausted(key: str) -> None:
    """402 on a leased key: stop leasing it. The active key is handled by
    rotate() (promotion from pool), never blacklisted here."""
    with _lock:
        if key and key != get_active_key():
            _exhausted.add(key)
            logger.warning("[KEY-LEASE] key %s…%s marked exhausted",
                           key[:8], key[-4:])


def per_call_key_provider(conn_id: str = ""):
    """Stable per-call key: leased once, re-leased only after a 402.
    Falls back to process-wide current_key() when leasing is disabled."""
    if not PER_CALL_KEYS:
        return current_key
    state = {"key": lease_key(conn_id)}

    def _get() -> str:
        if not state["key"] or state["key"] in _exhausted:
            state["key"] = lease_key(conn_id)
        return state["key"] or current_key()
    return _get


def rotate(failed_key: str | None = None) -> str | None:
    """
    Promote the first key from the backup pool to active.
    Updates os.environ and .env in place.
    Returns the new key string, or None if the pool is empty.

    ``failed_key`` is the key that just returned 402 and is what makes this
    safe under concurrency. One dead key produces a BURST of 402s — the STT
    router alone fires up to four requests per segment (primary, its retry,
    the 0.6s race and its retry) and TTS runs alongside them. Each used to
    call rotate(), and each pop promoted-and-abandoned a key nobody had
    tried: on 2026-08-31 three live keys were consumed in six seconds and
    the watchdog buried all three (RUNBOOK §34). A pop now happens only when
    the caller's failed key is still the ACTIVE one:

    * failed_key == active  → this is the first report; rotate for real.
    * failed_key != active  → either a peer already rotated off it (return
      their key, so the retry uses a live one), or the failure was on a
      per-call LEASED key, which mark_exhausted() already handles and which
      must never cost the pool a key.
    """
    with _lock:
        active = current_key()
        if failed_key and active and failed_key != active:
            logger.info(
                "[KEY-ROTATOR] 402 on %s…%s but active is already %s…%s — "
                "adopting it instead of burning a pool key",
                failed_key[:8], failed_key[-4:], active[:8], active[-4:],
            )
            return active

        pool = _load_pool()
        if not pool:
            logger.warning("[KEY-ROTATOR] Backup pool is empty — cannot auto-rotate")
            return None

        new_key = pool.pop(0)
        _save_pool(pool)
        _update_env(new_key)
        os.environ["SARVAM_API_KEY"] = new_key

        masked = new_key[:8] + "…" + new_key[-4:]
        logger.info(
            "[KEY-ROTATOR] Rotated to new key %s (%d key(s) remaining in pool)",
            masked, len(pool),
        )
        return new_key


def retire(failed_key: str) -> str | None:
    """A 402 on `failed_key`: rotate if it is the active key, THEN stop
    leasing it — in that order. mark_exhausted() refuses to blacklist the
    ACTIVE key, so marking before rotating left the dead key looking active
    for that instant and the per-call lease handed it straight back to the
    retry: call 1072 (2026-10-12) sent both STT attempts to the dead key,
    the caller's first answer was lost, and the identity line was asked
    twice. Returns what rotate() returns."""
    new_key = rotate(failed_key)
    mark_exhausted(failed_key)
    return new_key


def _load_pool() -> list[str]:
    if not _POOL_FILE.exists():
        return []
    try:
        return json.loads(_POOL_FILE.read_text(encoding="utf-8")).get("pool", [])
    except Exception:
        return []


def _save_pool(pool: list[str]) -> None:
    try:
        _POOL_FILE.write_text(json.dumps({"pool": pool}, indent=2), encoding="utf-8")
        # Live API keys — never leave them world-readable (see the same guard
        # in sarvam_key_watchdog._write_json).
        os.chmod(_POOL_FILE, 0o600)
    except Exception as exc:
        logger.warning("[KEY-ROTATOR] Could not save pool: %s", exc)


def _update_env(new_key: str) -> None:
    try:
        text = _ENV_FILE.read_text(encoding="utf-8") if _ENV_FILE.exists() else ""
        if re.search(r"^SARVAM_API_KEY=", text, re.MULTILINE):
            text = re.sub(r"^SARVAM_API_KEY=.*$", f"SARVAM_API_KEY={new_key}", text, flags=re.MULTILINE)
        else:
            text = text.rstrip("\n") + f"\nSARVAM_API_KEY={new_key}\n"
        _ENV_FILE.write_text(text, encoding="utf-8")
        os.chmod(_ENV_FILE, 0o600)
    except Exception as exc:
        logger.warning("[KEY-ROTATOR] Could not update .env: %s", exc)
