"""Agent-scoped provider context cache for the live-call LLM (RUNBOOK §62).

WHY
---
Every spoken turn re-sends the whole system prompt. Measured on Viraj call 972
(2026-09-26): 7,947 input tokens per request, of which the AGENT band — persona,
knowledge, rules, objection playbook, closure rules — is ~5,200 tokens that are
byte-identical on every turn of every call that agent takes. The provider billed
all of it fresh every time (`cached=0` on 20/20 turns): Gemini's implicit cache
never engaged on the OpenAI-compat route, and nothing asked for an explicit one.

Billing is now enabled on the key (the free-tier `limit=0` of 2026-08-11 is
gone — `[PROMPT-CACHE] … AVAILABLE`), so this module creates an explicit
`cachedContents` object per AGENT and serves every turn of every call of that
agent from it. Cached input is billed at a fraction of fresh input.

HOW IT IS SCOPED — one cache per agent, never shared
----------------------------------------------------
The cache key is (agent namespace, model, sha256 of the agent band + the tool
roster). The namespace is the agent's slug (set by the call bridge) or its
agent_id, and it is written into the cache's displayName. Consequences:

* two agents never share a cache, even if their prompts were identical;
* a new agent revision (any edit that changes the agent band) produces a new
  key on its next call, and the superseded cache for that agent is deleted;
* the per-call band (who the caller is, this call's style variant) and the
  per-turn band (stage, slots, goal) are NEVER cached — they ride in the
  request, so one caller's data can never be served to another call.

PROVIDER CONSTRAINTS — measured 2026-09-26, not assumed
-------------------------------------------------------
A request that names a `cached_content` may NOT also set a system instruction,
tools or tool_config (400 "CachedContent can not be used with GenerateContent
request setting system_instruction, tools or tool_config"). So:

* the cache holds the agent band as its systemInstruction AND the turn's tool
  roster (one cache per distinct roster — at most two per agent/language:
  with and without the booking tool);
* the per-call + per-turn text goes into a leading user message, framed as
  system context (`CONTEXT_HEADER`);
* the OpenAI-compat usage block never reports cached tokens, so the ledger is
  fed the token count the provider returned when the cache was created.

NEVER ON THE CRITICAL PATH
--------------------------
Creation and TTL refresh are background tasks (single-flight per key). A turn
uses the cache only if it is already READY; otherwise it goes out uncached
exactly as before, and the creation it triggered serves the next turn. A
request that fails because of the cache (expired, deleted, rejected) marks the
entry dead and the SAME turn is retried uncached. The fallback provider never
sees the cache. Any error in this module degrades to "no cache".
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

ENABLED = os.environ.get("LLM_CONTEXT_CACHE", "true").lower() == "true"
# How long a cache lives with no traffic. Storage is billed per token-hour, so
# an idle agent's cache is released ~TTL after its last call; an active one is
# refreshed (REFRESH_BELOW_S) and never expires mid-call.
TTL_S = int(os.environ.get("LLM_CONTEXT_CACHE_TTL_S", "1800"))
REFRESH_BELOW_S = int(os.environ.get("LLM_CONTEXT_CACHE_REFRESH_BELOW_S", "900"))
# Never use an entry this close to its expiry — the request could land after it.
SAFETY_MARGIN_S = 60
# Below this the provider refuses to cache (or it is not worth a cache object).
MIN_TOKENS = int(os.environ.get("LLM_CONTEXT_CACHE_MIN_TOKENS", "1024"))
# A key whose creation failed is not retried for this long (quota, too small…).
NEGATIVE_TTL_S = int(os.environ.get("LLM_CONTEXT_CACHE_NEGATIVE_TTL_S", "600"))
STATE_PATH = Path(os.environ.get(
    "LLM_CONTEXT_CACHE_STATE",
    str(Path(__file__).resolve().parents[4] / "data" / "llm_context_cache.json")))

CALL_HEADER = (
    "━━ THIS CALL — from the system, NOT spoken by the caller. It continues "
    "your instructions and carries the same authority. ━━")
CONTEXT_HEADER = (
    "━━ LIVE CALL CONTEXT — from the system, NOT spoken by the caller. It "
    "carries the same authority as your instructions. Reply now to the "
    "caller's LAST message above, following it. ━━")

_CACHE_ERROR_RE = re.compile(
    r"cachedcontent|cached_content|cached content|not found|expired|"
    r"permission denied|does not exist", re.IGNORECASE)


@dataclass
class CacheEntry:
    key: str
    namespace: str
    model: str
    label: str = ""                # language + tool-roster tag within the agent
    name: str = ""                 # cachedContents/…; empty until created
    tokens: int = 0                # provider-reported size of the cached prefix
    expire_ts: float = 0.0         # wall clock (persisted across restarts)
    created_ts: float = 0.0
    hits: int = 0
    dead: bool = False

    def ready(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return bool(self.name) and not self.dead and \
            self.expire_ts - now > SAFETY_MARGIN_S


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    creates: int = 0
    create_failures: int = 0
    refreshes: int = 0
    invalidations: int = 0
    cached_tokens_served: int = 0
    by_namespace: dict = field(default_factory=dict)


def is_supported(base_url: str | None) -> bool:
    return "generativelanguage.googleapis.com" in (base_url or "")


def to_function_declarations(tools: list[dict]) -> list[dict]:
    """OpenAI `tools` → Gemini `functionDeclarations`. JSON-schema keys the
    native API rejects are dropped (the compat shim does the same)."""
    def clean(schema):
        if isinstance(schema, dict):
            return {k: clean(v) for k, v in schema.items()
                    if k not in ("additionalProperties", "$schema", "strict",
                                 "default", "title")}
        if isinstance(schema, list):
            return [clean(x) for x in schema]
        return schema
    out = []
    for t in tools or []:
        f = t.get("function", t)
        fd = {"name": f["name"], "description": f.get("description", "")}
        if f.get("parameters"):
            fd["parameters"] = clean(f["parameters"])
        out.append(fd)
    return out


def make_key(namespace: str, model: str, agent_band: str,
             tools: list[dict] | None) -> str:
    h = hashlib.sha256()
    h.update(namespace.encode()); h.update(b"\x00")
    h.update(model.encode()); h.update(b"\x00")
    h.update(agent_band.encode("utf-8")); h.update(b"\x00")
    h.update(json.dumps(tools or [], sort_keys=True,
                        ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()[:24]


def is_cache_error(exc: BaseException) -> bool:
    """Did this request fail BECAUSE of the cache (so retrying uncached is
    right), rather than for a reason that would fail uncached too?"""
    msg = str(exc)
    status = getattr(exc, "status_code", None)
    if status in (400, 403, 404) and _CACHE_ERROR_RE.search(msg):
        return True
    return "CachedContent" in msg


class ContextCacheRegistry:
    """Process-wide, agent-keyed registry of provider cache objects."""

    def __init__(self, state_path: Path | None = STATE_PATH) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._negative: dict[str, float] = {}      # key -> retry-after (monotonic)
        self._state_path = state_path
        self._loaded = False
        self.stats = CacheStats()

    # ── persistence (a restart must not pay for every agent's cache again) ──
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._state_path:
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            logger.warning("[CTX-CACHE] state file unreadable — starting empty")
            return
        now = time.time()
        for k, v in (raw or {}).items():
            try:
                e = CacheEntry(**v)
            except TypeError:
                continue
            if e.ready(now):
                self._entries[k] = e
        if self._entries:
            logger.info("[CTX-CACHE] restored %d live cache(s) from %s",
                        len(self._entries), self._state_path)

    def _save(self) -> None:
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            now = time.time()
            data = {k: asdict(e) for k, e in self._entries.items()
                    if e.ready(now)}
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except Exception:
            logger.debug("[CTX-CACHE] state save failed", exc_info=True)

    # ── lookup ──────────────────────────────────────────────────────────────
    def lookup(self, key: str) -> CacheEntry | None:
        """READY entry for this key, or None. Never blocks, never raises."""
        self._load()
        e = self._entries.get(key)
        if e is None or not e.ready():
            return None
        return e

    def note_hit(self, entry: CacheEntry) -> None:
        entry.hits += 1
        self.stats.hits += 1
        self.stats.cached_tokens_served += entry.tokens
        ns = self.stats.by_namespace.setdefault(entry.namespace,
                                                {"hits": 0, "misses": 0})
        ns["hits"] += 1
        if self.stats.hits % 25 == 0:
            logger.info("[CTX-CACHE] stats %s", self.summary())

    def note_miss(self, namespace: str) -> None:
        self.stats.misses += 1
        ns = self.stats.by_namespace.setdefault(namespace,
                                                {"hits": 0, "misses": 0})
        ns["misses"] += 1

    def invalidate(self, key: str, reason: str) -> None:
        e = self._entries.get(key)
        if e is not None:
            e.dead = True
            self.stats.invalidations += 1
            logger.warning("[CTX-CACHE] %s: cache %s invalidated — %s",
                           e.namespace, e.name or "?", reason)
            self._save()

    # ── background maintenance ─────────────────────────────────────────────
    def ensure(self, *, key: str, namespace: str, model: str, agent_band: str,
               tools: list[dict] | None, api_key: str, base_url: str | None,
               label: str = "") -> asyncio.Task | None:
        """Make sure a cache for `key` exists and has TTL headroom. Returns the
        background task (tests await it) or None when nothing needs doing.
        Never blocks and never raises."""
        try:
            if not ENABLED or not api_key or not is_supported(base_url):
                return None
            asyncio.get_running_loop()   # RuntimeError → no loop → no-op
            self._load()
            if key in self._inflight and not self._inflight[key].done():
                return None
            retry_after = self._negative.get(key, 0.0)
            if retry_after > time.monotonic():
                return None
            e = self._entries.get(key)
            now = time.time()
            if e is not None and e.ready(now):
                if e.expire_ts - now >= REFRESH_BELOW_S:
                    return None
                coro = self._refresh(e, api_key, base_url)
            else:
                from .prompt_cache import estimate_tokens
                if estimate_tokens(agent_band) < MIN_TOKENS:
                    self._negative[key] = time.monotonic() + NEGATIVE_TTL_S
                    logger.info("[CTX-CACHE] %s: agent band ~%d tokens is below "
                                "the %d-token floor — not cached", namespace,
                                estimate_tokens(agent_band), MIN_TOKENS)
                    return None
                coro = self._create(key, namespace, model, agent_band, tools,
                                    api_key, base_url, label)
            task = asyncio.ensure_future(coro)
            self._inflight[key] = task
            return task
        except RuntimeError:
            return None          # no running loop (sync context)
        except Exception:
            logger.debug("[CTX-CACHE] ensure failed", exc_info=True)
            return None

    @staticmethod
    def _root(base_url: str | None) -> str:
        return (base_url or "").rstrip("/").split("/openai")[0].rstrip("/")

    async def _create(self, key, namespace, model, agent_band, tools,
                      api_key, base_url, label) -> None:
        import httpx
        body = {
            "model": f"models/{model}",
            "systemInstruction": {"parts": [{"text": agent_band}]},
            "ttl": f"{TTL_S}s",
            # Agent-scoped by name too, so the cache objects on the account
            # are attributable (and deletable) per agent.
            "displayName": f"lescall:{namespace}:{label}"[:120],
        }
        fds = to_function_declarations(tools or [])
        if fds:
            body["tools"] = [{"functionDeclarations": fds}]
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(
                    timeout=20.0, headers={"x-goog-api-key": api_key}) as c:
                r = await c.post(f"{self._root(base_url)}/cachedContents",
                                 json=body)
            if r.status_code != 200:
                detail = r.text[:240]
                self.stats.create_failures += 1
                self._negative[key] = time.monotonic() + NEGATIVE_TTL_S
                logger.warning("[CTX-CACHE] %s: create failed HTTP %d — %s "
                               "(uncached for %ds)", namespace, r.status_code,
                               detail, NEGATIVE_TTL_S)
                return
            j = r.json()
            tokens = int((j.get("usageMetadata") or {}).get("totalTokenCount") or 0)
            entry = CacheEntry(
                key=key, namespace=namespace, model=model, label=label,
                name=j["name"], tokens=tokens, expire_ts=time.time() + TTL_S,
                created_ts=time.time())
            # Superseded caches of THIS agent (same namespace/model/label, a
            # different band = an older revision) are released now instead of
            # being billed until their TTL runs out.
            stale = [e for k2, e in self._entries.items()
                     if k2 != key and e.namespace == namespace
                     and e.model == model and e.label == label
                     and not e.dead and e.name]
            self._entries[key] = entry
            self.stats.creates += 1
            logger.info("[CTX-CACHE] %s: created %s (%d tokens, %s, ttl %ds) "
                        "in %.0fms", namespace, entry.name, tokens, label,
                        TTL_S, (time.monotonic() - t0) * 1000)
            self._save()
            for e in stale:
                await self._delete(e, api_key, base_url,
                                   "superseded by a new agent revision")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats.create_failures += 1
            self._negative[key] = time.monotonic() + NEGATIVE_TTL_S
            logger.warning("[CTX-CACHE] %s: create error %r", namespace, exc)
        finally:
            self._inflight.pop(key, None)

    async def _refresh(self, e: CacheEntry, api_key, base_url) -> None:
        import httpx
        try:
            async with httpx.AsyncClient(
                    timeout=10.0, headers={"x-goog-api-key": api_key}) as c:
                r = await c.patch(f"{self._root(base_url)}/{e.name}",
                                  params={"updateMask": "ttl"},
                                  json={"ttl": f"{TTL_S}s"})
            if r.status_code == 200:
                e.expire_ts = time.time() + TTL_S
                self.stats.refreshes += 1
                logger.info("[CTX-CACHE] %s: refreshed %s (+%ds)",
                            e.namespace, e.name, TTL_S)
                self._save()
            elif r.status_code in (403, 404):
                self.invalidate(e.key, f"refresh HTTP {r.status_code}")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("[CTX-CACHE] refresh failed", exc_info=True)
        finally:
            self._inflight.pop(e.key, None)

    async def _delete(self, e: CacheEntry, api_key, base_url, why: str) -> None:
        import httpx
        try:
            async with httpx.AsyncClient(
                    timeout=10.0, headers={"x-goog-api-key": api_key}) as c:
                await c.delete(f"{self._root(base_url)}/{e.name}")
            e.dead = True
            logger.info("[CTX-CACHE] %s: deleted %s — %s", e.namespace,
                        e.name, why)
            self._save()
        except Exception:
            logger.debug("[CTX-CACHE] delete failed", exc_info=True)

    def summary(self) -> str:
        s = self.stats
        total = s.hits + s.misses
        rate = (100.0 * s.hits / total) if total else 0.0
        return (f"hits={s.hits} misses={s.misses} ({rate:.0f}%) "
                f"creates={s.creates} failures={s.create_failures} "
                f"refreshes={s.refreshes} invalidations={s.invalidations} "
                f"cached_tokens_served={s.cached_tokens_served}")


REGISTRY = ContextCacheRegistry()


def build_cached_messages(call_context: str, turn_context: str,
                          history: list[dict]) -> list[dict] | None:
    """The request body for a cached turn.

    The per-call context (who the caller is, this call's style) LEADS the
    conversation, where the system prompt used to sit; the per-turn context
    (call state, current goal, reply trailer) FOLLOWS the caller's latest
    words. Measured 2026-09-26 on Viraj, 10 samples per arm:

    * everything first  → the goal lost weight: 5/20 FEEDBACK turns ended the
      call with a tool-only end_call mid-survey (uncached: 0/20);
    * everything last   → the identity instructions became the most recent
      thing read and the agent re-asked "क्या मेरी बात … हो रही है?" mid-flow.

    None when the history holds a system-role message (it would become a
    system_instruction, which a cached request may not carry)."""
    if any(m.get("role") == "system" for m in history):
        return None
    msgs: list[dict] = []
    if call_context.strip():
        msgs.append({"role": "user",
                     "content": f"{CALL_HEADER}\n{call_context.strip()}"})
    msgs.extend(history)
    msgs.append({"role": "user",
                 "content": f"{CONTEXT_HEADER}\n{turn_context.strip()}"})
    return msgs
