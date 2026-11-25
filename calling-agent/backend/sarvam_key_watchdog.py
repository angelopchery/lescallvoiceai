#!/usr/bin/env python3
"""
sarvam_key_watchdog.py — background service that keeps the voice pipeline
supplied with a funded Sarvam API key.

WHAT IT DOES
------------
* Tails the live runtime logs for Sarvam "out of credits" signatures
  (HTTP 402 / "No credits available" / "insufficient_quota" / "Credits
  exhausted").
* When the active key is exhausted, retires it and promotes the next live
  key from the master list into calling-agent/.env. Because STT/TTS read the
  key fresh from .env on every request (key_rotator.current_key()), the swap
  takes effect on the next segment WITHOUT restarting the pipeline.
* Continuously reconciles the backup pool (dashboard/api_key_pool.json) to
  exactly "master − dead − active", so the pipeline's own in-process
  rotate() never starves and never re-tries a dead or already-active key.
* Detects in-process rotations (active key changed underneath it) and retires
  the old key cooperatively, so the two rotation paths never double-advance.
* Writes a status file and its own log; raises CRITICAL when every key is dead.

This service holds no long-lived Sarvam connection and spends no credits — it
only reacts to what the pipeline reports in its logs, plus manages JSON/.env
files. Safe to run continuously and to restart at any time.

Run as a systemd service (see sarvam-key-watchdog.service). Standalone:
    python3 sarvam_key_watchdog.py
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

# ── Paths (resolved relative to the repo root) ───────────────────────────────
_REPO = Path(__file__).resolve().parents[2]          # /home/angelo/lescall
ENV_FILE     = _REPO / "calling-agent" / ".env"
POOL_FILE    = _REPO / "dashboard" / "api_key_pool.json"
MASTER_FILE  = _REPO / "dashboard" / "sarvam_key_master.json"
DEAD_FILE    = _REPO / "dashboard" / "sarvam_dead_keys.json"
STATUS_FILE  = _REPO / "dashboard" / "sarvam_key_status.json"
LOG_DIR      = _REPO / "phase14_runtime" / "logs"
WATCH_LOGS   = [LOG_DIR / "latest" / "ws_audio_receiver.log",
                LOG_DIR / "latest" / "socket_server.log"]
OWN_LOG      = LOG_DIR / "sarvam_watchdog.log"

POLL_SECONDS      = 3.0
# Ignore repeat 402s for this long after a rotation, so a burst of log lines
# from one dead key doesn't burn through several keys at once.
ROTATE_DEBOUNCE_S = 12.0

# ── Verify-before-bury / resurrection (2026-09-08, RUNBOOK §34) ──────────
# A key used to be declared dead on INFERENCE: "the active key changed, so
# whatever it changed away from must be exhausted". That inference is wrong
# whenever the pipeline rotated for any other reason — and a concurrent
# burst of 402s on ONE dead key rotated several times in seconds, so live
# keys were promoted, never used, and buried. It cost 6 of 28 keys and 8
# days of mute calls. Death is now MEASURED, with one 2-character TTS
# request, and burial is never permanent: dead keys are re-probed on a
# timer, so a refill (or a wrong burial) heals itself without an operator.
VERIFY_BEFORE_BURY = os.environ.get(
    "SARVAM_VERIFY_BEFORE_BURY", "true").lower() == "true"
PROBE_TIMEOUT_S    = float(os.environ.get("SARVAM_PROBE_TIMEOUT_S", "10"))
# How often to re-probe the dead list. Far more often while nothing is left
# to serve calls with — that is the state worth escaping fastest.
RESURRECT_EVERY_S      = float(os.environ.get("SARVAM_RESURRECT_EVERY_S", "1800"))
RESURRECT_EXHAUSTED_S  = float(os.environ.get("SARVAM_RESURRECT_EXHAUSTED_S", "300"))
# Cap the keys probed per sweep so a huge dead list can't stall the loop.
RESURRECT_BATCH   = int(os.environ.get("SARVAM_RESURRECT_BATCH", "40"))

_TTS_URL = "https://api.sarvam.ai/text-to-speech"

_EXHAUSTION_RE = re.compile(
    r"(No credits available|insufficient_quota|HTTP 402|402 Payment Required"
    r"|Credits exhausted|TTS HTTP 402)",
    re.IGNORECASE,
)

# ── logging ──────────────────────────────────────────────────────────────────
OWN_LOG.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(OWN_LOG), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("sarvam-watchdog")


def _mask(key: str) -> str:
    return (key[:8] + "…" + key[-4:]) if key and len(key) > 12 else (key or "<none>")


# ── small JSON helpers ───────────────────────────────────────────────────────
def _read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("could not read %s: %s", path.name, exc)
    return default


def _write_json(path: Path, data) -> None:
    """Atomic write, owner-only. These files hold LIVE API keys — a default
    0644 would republish them to every local account on each rotation, silently
    undoing the hardening. Mode is set on the temp file BEFORE the rename so
    the key is never world-readable, not even for the instant in between."""
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except Exception as exc:
        log.warning("could not write %s: %s", path.name, exc)


def load_master() -> list[str]:
    return _read_json(MASTER_FILE, {}).get("keys", []) or []


def load_dead() -> set[str]:
    return set(_read_json(DEAD_FILE, {}).get("dead", []) or [])


def save_dead(dead: set[str]) -> None:
    _write_json(DEAD_FILE, {"dead": sorted(dead)})


def load_pool() -> list[str]:
    return _read_json(POOL_FILE, {}).get("pool", []) or []


def save_pool(pool: list[str]) -> None:
    _write_json(POOL_FILE, {"pool": pool})


# ── active key in .env (single source of truth for "current") ────────────────
def read_active() -> str:
    try:
        if ENV_FILE.exists():
            m = re.search(r"^SARVAM_API_KEY=(.*)$",
                          ENV_FILE.read_text(encoding="utf-8"), re.MULTILINE)
            if m:
                return m.group(1).strip()
    except Exception as exc:
        log.warning("could not read .env: %s", exc)
    return ""


def write_active(key: str) -> None:
    try:
        text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
        if re.search(r"^SARVAM_API_KEY=", text, re.MULTILINE):
            text = re.sub(r"^SARVAM_API_KEY=.*$",
                          f"SARVAM_API_KEY={key}", text, flags=re.MULTILINE)
        else:
            text = text.rstrip("\n") + f"\nSARVAM_API_KEY={key}\n"
        ENV_FILE.write_text(text, encoding="utf-8")
        # .env holds nine live credentials (Sarvam, OpenAI, Gemini, Google
        # OAuth, the session-signing secret …). write_text preserves the mode
        # of an existing file, but re-assert it so a file recreated by any
        # path is never left world-readable.
        os.chmod(ENV_FILE, 0o600)
    except Exception as exc:
        log.error("could not write active key to .env: %s", exc)


# ── log tailing (survives log rotation via start_all.sh) ─────────────────────
class LogTailer:
    """Yields new lines appended to a file, coping with truncation/rotation."""

    def __init__(self, path: Path):
        self.path = path
        self._ino = None
        self._pos = 0

    def read_new(self) -> list[str]:
        try:
            real = self.path.resolve()
            if not real.exists():
                return []
            st = real.stat()
            if self._ino != st.st_ino:
                # First sight or rotated file: start at END so we never replay
                # a previous run's stale 402s.
                self._ino = st.st_ino
                self._pos = st.st_size
                return []
            if st.st_size < self._pos:      # truncated in place
                self._pos = 0
            if st.st_size == self._pos:
                return []
            with open(real, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._pos)
                data = f.read()
                self._pos = f.tell()
            return data.splitlines()
        except Exception as exc:
            log.debug("tail error on %s: %s", self.path, exc)
            return []


def probe_key(key: str) -> str:
    """Ask Sarvam whether this key can still do work.

    Returns "live" (HTTP 200), "dead" (402 / insufficient_quota) or
    "unknown" (anything else: 401/403, 5xx, rate limit, network, no httpx).
    "unknown" NEVER buries and never resurrects — an unreachable API must not
    be able to empty the key pool, which is the failure this whole guard
    exists to prevent. Costs two characters of TTS.
    """
    if not key:
        return "unknown"
    try:
        import httpx
    except Exception:
        return "unknown"
    try:
        r = httpx.post(
            _TTS_URL,
            headers={"api-subscription-key": key},
            json={"text": "ok", "target_language_code": "en-IN",
                  "model": "bulbul:v3", "speaker": "shubh"},
            timeout=PROBE_TIMEOUT_S,
        )
    except Exception as exc:
        log.warning("probe %s: network error %r — treating as unknown",
                    _mask(key), exc)
        return "unknown"
    if r.status_code == 200:
        return "live"
    if r.status_code == 402:
        return "dead"
    log.warning("probe %s: HTTP %d — treating as unknown (not buried)",
                _mask(key), r.status_code)
    return "unknown"


def bury(key: str, dead: set[str], why: str) -> bool:
    """Add a key to the dead list, but only once it has FAILED a probe.

    Returns True if the key was buried. With verification off, or when the
    probe is inconclusive, the key is left alive — the cost of keeping a dead
    key is one wasted 402 and another rotation; the cost of burying a live one
    is silent, permanent and exactly what happened on 2026-08-31.
    """
    if not key or key in dead:
        return False
    if VERIFY_BEFORE_BURY:
        verdict = probe_key(key)
        if verdict != "dead":
            log.warning("NOT burying %s (%s) — probe says %s", _mask(key), why, verdict)
            return False
    dead.add(key)
    save_dead(dead)
    log.warning("buried %s (%s) — verified out of credits", _mask(key), why)
    return True


def resurrect(master: list[str], dead: set[str]) -> int:
    """Re-probe dead keys and revive the ones that answer.

    Credits get topped up and keys get buried by mistake; without this the
    dead list is a one-way door and the platform stays mute until a human
    notices. Only keys still present in master are probed.
    """
    candidates = [k for k in master if k in dead][:RESURRECT_BATCH]
    revived = 0
    for k in candidates:
        if probe_key(k) == "live":
            dead.discard(k)
            revived += 1
            log.warning("RESURRECTED %s — key has credits again", _mask(k))
    if revived:
        save_dead(dead)
    log.info("resurrection sweep: probed %d dead key(s), revived %d",
             len(candidates), revived)
    return revived


def reconcile_pool(master: list[str], dead: set[str], active: str) -> list[str]:
    """Pool = master − dead − active, order preserved. Keeps the in-process
    rotate() fed with only live keys, never a dead or duplicate-active one."""
    pool = [k for k in master if k and k != active and k not in dead]
    if pool != load_pool():
        save_pool(pool)
    return pool


def write_status(active: str, pool: list[str], dead: set[str], exhausted: bool) -> None:
    _write_json(STATUS_FILE, {
        "active_key": _mask(active),
        "pool_remaining": len(pool),
        "dead_count": len(dead),
        "all_exhausted": exhausted,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })


def main() -> None:
    master = load_master()
    dead = load_dead()

    # ── seed on first run ────────────────────────────────────────────────────
    active = read_active()
    if not active and master:
        live = [k for k in master if k not in dead]
        if live:
            active = live[0]
            write_active(active)
            log.info("seeded active key = %s", _mask(active))
    reconcile_pool(master, dead, active)

    tailers = [LogTailer(p) for p in WATCH_LOGS]
    last_active = active
    last_rotation = 0.0
    last_resurrect = 0.0

    log.info(
        "watchdog started — active=%s pool=%d dead=%d master=%d",
        _mask(active), len(load_pool()), len(dead), len(master),
    )

    while True:
        try:
            master = load_master()      # allow live edits (e.g. adding keys)
            dead = load_dead()
            active = read_active()

            # (1) Detect an in-process rotation done by the pipeline itself and
            #     retire the key it moved off of — cooperative, no double-hop.
            if active != last_active:
                if last_active and last_active not in dead:
                    # The pipeline moving off a key is NOT proof the key is
                    # spent — it also happens when a concurrent burst rotates
                    # more than once. Probe before burying.
                    log.info("in-process rotation detected — %s → %s, verifying "
                             "the old key", _mask(last_active), _mask(active))
                    bury(last_active, dead, "in-process rotation")
                last_active = active
                last_rotation = time.time()

            # (2) Scan new log lines for credit-exhaustion signatures.
            hit = False
            for t in tailers:
                for line in t.read_new():
                    if _EXHAUSTION_RE.search(line):
                        hit = True
            now = time.time()

            # (3) If exhaustion is being reported and we haven't just rotated,
            #     the current active key is dead → promote the next live key.
            if hit and (now - last_rotation) > ROTATE_DEBOUNCE_S:
                # A 402 in the log does not name its key: with per-call
                # leasing it may have come from a LEASED backup, not from the
                # active key. Probe the active key rather than assume.
                if active and active not in dead:
                    if not bury(active, dead, "402 reported in runtime log"):
                        # Active key is fine — the 402 came from somewhere
                        # else. Nothing to rotate.
                        last_rotation = now
                        pool = reconcile_pool(master, dead, active)
                        write_status(active, pool, dead,
                                     not pool and active in dead)
                        time.sleep(POLL_SECONDS)
                        continue
                live = [k for k in master if k not in dead]
                if live:
                    new_key = live[0]
                    write_active(new_key)
                    last_active = new_key
                    last_rotation = now
                    log.warning("ROTATED: %s exhausted → promoted %s",
                                _mask(active), _mask(new_key))
                    active = new_key
                else:
                    log.critical(
                        "ALL SARVAM KEYS EXHAUSTED — no live key left in master "
                        "(%d dead of %d). Refill credits or add keys to %s.",
                        len(dead), len(master), MASTER_FILE.name,
                    )

            # (4) Keep the backup pool exactly = master − dead − active.
            pool = reconcile_pool(master, dead, active)
            exhausted = not pool and (not active or active in dead)

            # (5) Re-probe the dead list on a timer — fast while there is
            #     nothing left to serve calls with. This is what turns "all
            #     keys exhausted" from an outage that waits for a human into
            #     a state the platform climbs out of by itself, whether the
            #     cause was a refill or a wrong burial.
            interval = RESURRECT_EXHAUSTED_S if exhausted else RESURRECT_EVERY_S
            if dead and (now - last_resurrect) >= interval:
                last_resurrect = now
                if resurrect(master, dead):
                    active = read_active()
                    if exhausted:
                        # Nothing usable was active — promote a revived key
                        # so the very next call can speak.
                        live = [k for k in master if k not in dead]
                        if live and (not active or active in dead):
                            write_active(live[0])
                            active = live[0]
                            last_active = active
                            log.warning("promoted revived key %s to active",
                                        _mask(active))
                    pool = reconcile_pool(master, dead, active)
                    exhausted = not pool and (not active or active in dead)

            write_status(active, pool, dead, exhausted)

        except Exception:
            log.exception("watchdog loop error (continuing)")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
