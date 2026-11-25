"""
Agent registry — per-agent lazy cache with optional live-reload watcher.

Usage
-----
    from app.agent_definition.registry import agent_registry

    agent = agent_registry.get("pramukh")        # cached after first load
    agent_registry.invalidate("pramukh")          # drop cache entry
    agent_registry.start_watcher(agents_dir)      # begin FS watch
    agent_registry.stop_watcher()                 # clean up on shutdown

Freshness (F8)
--------------
The watcher is best-effort: it can die (and is now restarted with backoff),
but a call must NEVER be served a stale definition just because a thread
crashed. So get() also does a cheap os.stat() on every cache hit (gated by
AGENT_CACHE_STAT_CHECK, default ON) and reloads when mtime/size changed.
Per-slug load metadata (mtime_ns/size/sha256/revision) is kept so each call
can record exactly WHICH agent bytes served it (meta()/health()).
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path
from typing import Optional

from .schema import AgentDefinition

logger = logging.getLogger(__name__)

# Backoff between watcher restarts after an unexpected crash (seconds).
_WATCHER_RESTART_BACKOFF_S = 5.0


def default_agents_dir() -> Path:
    """The canonical agents/ folder beside calling-agent/backend.

    Single source of truth for every consumer (registry loads AND the
    telephony bridge's watcher) so the resolved directory never depends on
    the launcher's CWD.
    """
    return Path(__file__).resolve().parents[3] / "agents"


class AgentRegistry:
    """Thread-safe cache of AgentDefinition objects keyed by folder name."""

    def __init__(self) -> None:
        self._cache: dict[str, AgentDefinition] = {}
        self._lock = threading.Lock()
        self._observer: Optional[threading.Thread] = None  # watcher thread when running
        # Load-failure visibility (P0): last error per agent + total counter,
        # so a broken agent is flagged loudly instead of silently masked.
        self._load_errors: dict[str, str] = {}
        self._load_failure_count: int = 0
        # Per-slug fidelity metadata captured at load time (F8):
        # {mtime_ns, size, sha256, path, revision}. Entries put() directly
        # (no backing file) have no meta and are exempt from stat checks.
        self._meta: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, name: str, agents_dir: Optional[Path] = None) -> AgentDefinition:
        """Return the cached AgentDefinition for *name*, loading it on first access.

        agents_dir defaults to the canonical agents/ folder beside calling-agent/.

        On a cache HIT the backing agent.yaml is stat()ed (unless
        AGENT_CACHE_STAT_CHECK=false) and the entry is transparently reloaded
        when mtime/size changed — freshness no longer depends on the watcher
        thread being alive. A yaml that vanished mid-flight (agent archived)
        keeps serving the cached definition rather than dropping the call.
        """
        with self._lock:
            if name not in self._cache:
                self._load_locked(name, agents_dir)
            elif self._stat_check_enabled():
                self._maybe_stat_reload_locked(name, agents_dir)
            return self._cache[name]

    def meta(self, name: str) -> Optional[dict]:
        """Load metadata for *name*: {mtime_ns, size, sha256, path, revision}.

        None when the agent isn't cached or was put() without a backing file.
        """
        with self._lock:
            m = self._meta.get(name)
            return dict(m) if m is not None else None

    def health(self) -> dict:
        """Snapshot for ops: watcher liveness + per-slug revision/sha."""
        with self._lock:
            cached = {
                slug: {
                    "revision": (self._meta.get(slug) or {}).get("revision", 0),
                    "sha256": (self._meta.get(slug) or {}).get("sha256", ""),
                }
                for slug in self._cache
            }
            observer = self._observer
            failures = self._load_failure_count
        return {
            "watcher_alive": bool(observer is not None and observer.is_alive()),
            "cached": cached,
            "load_failures": failures,
        }

    def last_load_error(self, name: str) -> Optional[str]:
        """Last load failure for *name* (None once a load has succeeded)."""
        with self._lock:
            return self._load_errors.get(name)

    def load_failure_count(self) -> int:
        with self._lock:
            return self._load_failure_count

    def put(self, name: str, agent: AgentDefinition) -> None:
        """Store (or replace) a pre-built AgentDefinition in the cache."""
        with self._lock:
            self._cache[name] = agent
            # No backing file: drop stale file meta so stat checks skip it.
            self._meta.pop(name, None)

    def invalidate(self, name: str) -> None:
        """Drop the cached entry for *name* so the next get() re-reads the YAML."""
        with self._lock:
            dropped = self._cache.pop(name, None)
            self._meta.pop(name, None)
        if dropped is not None:
            logger.info("[registry] invalidated cache for agent '%s'", name)

    def invalidate_all(self) -> None:
        with self._lock:
            self._cache.clear()
            self._meta.clear()
        logger.info("[registry] all agent caches invalidated")

    def known_names(self) -> list[str]:
        """Names currently in the cache (may not reflect all agents on disk)."""
        with self._lock:
            return list(self._cache.keys())

    # ------------------------------------------------------------------
    # Live-reload watcher (uses watchfiles, already in requirements.txt)
    # ------------------------------------------------------------------

    def start_watcher(self, agents_dir: Path) -> None:
        """Start a background thread that invalidates cache when agent.yaml files change."""
        try:
            from watchfiles import watch as wf_watch
        except ImportError:
            logger.warning("[registry] watchfiles not installed — live reload disabled")
            return

        stop_event = threading.Event()
        self._stop_event = stop_event
        registry = self

        def _run() -> None:
            # Self-healing loop: a crashed watch resumes after a short
            # backoff instead of silently leaving the cache stale forever.
            while not stop_event.is_set():
                try:
                    for changes in wf_watch(str(agents_dir), stop_event=stop_event):
                        for _change_type, path in changes:
                            p = Path(path)
                            if p.name == "agent.yaml":
                                registry.invalidate(p.parent.name)
                except Exception as exc:
                    logger.error(
                        "[registry] watcher crashed (restarting in %.0fs): %r",
                        _WATCHER_RESTART_BACKOFF_S, exc, exc_info=True)
                    stop_event.wait(_WATCHER_RESTART_BACKOFF_S)

        t = threading.Thread(target=_run, daemon=True, name="agent-registry-watcher")
        t.start()
        self._observer = t
        logger.info("[registry] live-reload watcher started on %s", agents_dir)

    def stop_watcher(self) -> None:
        if hasattr(self, "_stop_event"):
            self._stop_event.set()
        self._observer = None
        logger.info("[registry] live-reload watcher stopped")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _stat_check_enabled() -> bool:
        # Default ON — opt out with AGENT_CACHE_STAT_CHECK=false.
        return os.environ.get(
            "AGENT_CACHE_STAT_CHECK", "").strip().lower() != "false"

    def _load_locked(self, name: str, agents_dir: Optional[Path],
                     reason: str = "loaded") -> None:
        """(Re)load *name* from disk into cache + meta. Caller holds the lock."""
        try:
            path = self._yaml_path(name, agents_dir)
            st = os.stat(path)
            raw = path.read_bytes()
            self._cache[name] = AgentDefinition.from_yaml(path)
        except Exception as exc:
            self._load_errors[name] = repr(exc)
            self._load_failure_count += 1
            logger.error(
                "[registry] AGENT LOAD FAILED for '%s' (failure #%d): %r",
                name, self._load_failure_count, exc)
            raise
        self._meta[name] = {
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "path": str(path),
            "revision": int(getattr(self._cache[name], "revision", 0) or 0),
        }
        self._load_errors.pop(name, None)
        logger.info("[registry] %s agent '%s' from %s", reason, name, path)

    def _maybe_stat_reload_locked(self, name: str,
                                  agents_dir: Optional[Path]) -> None:
        """Cheap freshness check on a cache hit; reload when the yaml changed.

        Never propagates: a vanished yaml (agent archived mid-flight) or a
        reload failure keeps serving the already-cached definition — a live
        call may get a slightly stale agent but is never dropped here.
        """
        meta = self._meta.get(name)
        if meta is None:
            return  # put() entry with no backing file — nothing to stat
        try:
            st = os.stat(meta["path"])
        except FileNotFoundError:
            logger.warning(
                "[registry] agent.yaml for '%s' vanished (%s) — serving "
                "cached definition", name, meta["path"])
            return
        except Exception as exc:
            logger.warning(
                "[registry] stat check failed for '%s': %r — serving "
                "cached definition", name, exc)
            return
        if st.st_mtime_ns == meta["mtime_ns"] and st.st_size == meta["size"]:
            return  # unchanged — the common (hot) path
        try:
            # Reload from the directory this entry was originally loaded from
            # (meta path), so a hit without agents_dir can't silently reload
            # the same slug from a different base directory.
            self._load_locked(
                name, agents_dir or Path(meta["path"]).parents[1],
                reason="stat-reload")
        except Exception:
            # _load_locked already logged loudly; keep the previous good
            # definition in cache so the call proceeds.
            logger.warning(
                "[registry] stat-reload failed for '%s' — serving previous "
                "cached definition", name)

    @staticmethod
    def _yaml_path(name: str, agents_dir: Optional[Path]) -> Path:
        base = agents_dir or default_agents_dir()
        path = base / name / "agent.yaml"
        if not path.exists():
            raise FileNotFoundError(f"No agent.yaml found for '{name}' at {path}")
        return path


# Module-level singleton used by default throughout the platform.
agent_registry = AgentRegistry()
