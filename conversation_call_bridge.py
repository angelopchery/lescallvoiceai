import asyncio
import logging
import json
import struct
import time
import wave
from collections import deque
from typing import Dict

import sys
import os

# Add calling-agent to Python path to allow imports
sys.path.insert(0, os.path.abspath('calling-agent'))

# Repo root on sys.path so `shared.*` (multi-tenant routing DB) imports work
# regardless of the launcher's cwd.
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from pathlib import Path

from call_audit import audit_manager

from backend.app.voice.pipeline import VoicePipeline
from backend.app.voice.event_hub import EventHub
from backend.app.audio_transport.telephony_source import TelephonySource
from backend.app.audio_transport.telephony_sink import TelephonySink

# Data-driven agent runtime (replaces the old hardcoded ConversationRouter).
# Telephony resolves the operator-selected "active" agent per call and runs it
# through the exact same stack run.py uses for the CLI/web pipeline.
from backend.app.voice.conversation import ConversationRouter
from backend.app.voice.stt.router import STTRouter
from backend.app.voice.tracking_stt import TrackingSarvamSTT, TrackingWhisperSTT
from backend.app.voice.tracking_tts import TrackingSarvamTTS
from backend.app.voice.credit_tracker import SarvamCreditTracker
from backend.app.agent_definition.registry import agent_registry, default_agents_dir

logger = logging.getLogger("call_bridge")

# Telephony playback injection server details
PLAYBACK_HOST = "127.0.0.1"
PLAYBACK_PORT = 9004

# --- "empty call" detection (calls.answer_state) ---------------------------
# A connected call where no real conversation happened: the callee picked up
# but stayed silent, only noise came through, an answering machine took it, or
# it was over before it began. FreeSWITCH answers all of these identically, so
# it is decided at teardown from what the caller actually said. Every threshold
# is env-tunable because the right value is deployment-specific.
#
# Below EMPTY_CALL_MAX_SECONDS an answered call is treated as empty regardless
# of content — an 8-second "call" is a pickup-and-hang-up, not a conversation.
_EMPTY_CALL_MAX_SECONDS = float(os.environ.get("EMPTY_CALL_MAX_SECONDS", "10"))
# Total caller characters (across <=2 turns) below which they effectively said
# nothing — "hello?" and then silence.
_EMPTY_CALL_MIN_CHARS = int(os.environ.get("EMPTY_CALL_MIN_CHARS", "15"))
# A single caller utterance this long, with nothing responsive after it, has
# the shape of a recorded announcement.
_VOICEMAIL_MIN_CHARS = int(os.environ.get("VOICEMAIL_MIN_CHARS", "120"))
# Announcement phrases across the languages this platform serves. Matched
# case-insensitively as substrings of the caller side of the transcript.
_VOICEMAIL_PHRASES = tuple(p.strip().lower() for p in os.environ.get(
    "VOICEMAIL_PHRASES",
    "leave a message|after the tone|after the beep|voice mail|voicemail|"
    "not available right now|unable to take your call|is switched off|"
    "record your message|please try again later|"
    "आपका संदेश|संदेश रिकॉर्ड|उपलब्ध नहीं|स्विच ऑफ|बाद में प्रयास|"
    "ઉપલબ્ધ નથી|સંદેશ"
).split("|") if p.strip())

# Agent-initiated graceful hangup (platform rule, all agents). The engine
# arms ConversationRouter.hangup_pending only on caller-consented closes
# (farewell exchange, close-permission confirm, wrong number, DND); the
# bridge then asks socket_server to end the channel once the farewell audio
# has drained. Kill switch for rollback.
AGENT_HANGUP_ENABLED = os.environ.get(
    "ENABLE_AGENT_HANGUP", "true").lower() == "true"
# Seconds after hangup_pending is first seen before the channel may drop —
# covers TTS synth + flush latency of the farewell line, so a slow flush is
# never mistaken for "audio already drained".
AGENT_HANGUP_MIN_WAIT_S = float(os.environ.get("AGENT_HANGUP_MIN_WAIT_S", "3.0"))
# Seconds of post-playback grace after the estimated wire-playback end.
AGENT_HANGUP_GRACE_S = float(os.environ.get("AGENT_HANGUP_GRACE_S", "1.0"))
# Ceiling on how long caller speech may hold the channel open after a hangup
# was armed. The quiet-check below is what stops us cutting someone off
# mid-sentence; this stops a stuck VAD/STT flag from holding a dead call open
# forever.
AGENT_HANGUP_MAX_WAIT_S = float(os.environ.get("AGENT_HANGUP_MAX_WAIT_S", "20.0"))

# TTS synthesis/output sample rate. 8000 = Sarvam synthesizes at telephony
# rate directly and TelephonySink skips the ffmpeg downsample entirely
# (~100-300ms blocking subprocess per flush). Set 16000 to restore the old
# 16k-synth + ffmpeg path.
TTS_OUTPUT_SR = int(os.environ.get("TTS_OUTPUT_SR", "8000"))

# Open the usage-ledger row in the background instead of blocking session
# start (~hundreds of ms of DB work before the greeting can fire). The row
# opens ≤~1s later; heartbeats are 15s-granular and close_call is unchanged.
METERING_OPEN_DEFERRED = os.environ.get(
    "METERING_OPEN_DEFERRED", "true").lower() == "true"

# Cache preflight verdicts per (slug, sha256, revision): the prompt build is
# deterministic and the registry watcher bumps sha/revision on any agent.yaml
# edit, so an edit self-invalidates. Saves a full stage×language prompt render
# on every call for unchanged agents.
PREFLIGHT_CACHE_ENABLED = os.environ.get(
    "PREFLIGHT_CACHE", "true").lower() == "true"
_preflight_cache: dict[tuple, str] = {}
_PREFLIGHT_CACHE_MAX = 64

# Pre-call customer context (Customer Intelligence Hub): fetch the returning
# caller's precomputed digest inside the routing thread. Platform kill-switch;
# the per-agent opt-out lives in agent.yaml (memory.use_customer_context).
INTEL_PRECALL_CONTEXT = os.environ.get(
    "INTEL_PRECALL_CONTEXT", "true").lower() == "true"
_customer_ctx_cache: dict[tuple, tuple[float, dict | None]] = {}
_CUSTOMER_CTX_TTL_S = 300.0
_CUSTOMER_CTX_CACHE_MAX = 512

# ---------------------------------------------------------------------------
# Active-agent resolution (single-active-agent model)
# ---------------------------------------------------------------------------
# Agents are YAML folders under calling-agent/agents/<slug>/agent.yaml. The
# dashboard's Agent Builder "Activate" button writes agents/active_agent.json
# ({"active": "<slug>"}); every inbound call runs that agent. Multi-agent /
# per-DID routing can be layered on later via routing.yaml + resolve_agent().
# Derived from the registry module location (NOT the CWD) so the watcher and
# every yaml-exists probe below watch the exact directory the registry loads
# from, no matter where the process was launched. Resolves to the identical
# <repo>/calling-agent/agents path as the old CWD-based form under
# cd /home/angelo/lescall.
AGENTS_DIR = default_agents_dir()
ACTIVE_AGENT_FILE = AGENTS_DIR / "active_agent.json"
DEFAULT_AGENT = os.environ.get("DEFAULT_AGENT", "pramukh")

# Start the registry hot-reload watcher once for this process so edits made in
# the builder take effect on the next call without a restart.
try:
    agent_registry.start_watcher(AGENTS_DIR)
except Exception as _exc:  # pragma: no cover - watcher is best-effort
    logger.warning("[BRIDGE] registry watcher not started: %r", _exc)


def _reap_played_wav(path: str) -> None:
    """Delete a played-out TTS WAV; scheduled 30s after its pacing sleep."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception as exc:  # pragma: no cover - reaping is best-effort
        logger.debug("[BRIDGE] wav reap failed for %s: %r", path, exc)


def _resolve_active_agent_slug() -> str:
    """Slug of the agent that should answer inbound calls.

    Reads agents.is_active from the shared DB first (the Activate button
    writes it); falls back to agents/active_agent.json, then DEFAULT_AGENT,
    then the first agent folder that has an agent.yaml. Never raises.
    """
    try:
        from shared.db.database import get_db
        # Scoped to the FOUNDING tenant. `is_active` is a single global flag
        # that every tenant's Activate button writes, so an unscoped
        # "most recently activated" query returns whoever last pressed the
        # button — currently one of four different accounts. This function is
        # the last-resort fallback for the founding tenant's pre-tenancy
        # numbers; it must never hand a call to an unrelated tenant's agent,
        # speaking their company name and billing their minutes.
        default_owner = os.environ.get("LESCALL_DEFAULT_OWNER", "tech@dhronai.com")
        with get_db() as db:
            row = db.execute(
                "SELECT slug FROM agents WHERE is_active=1 AND owner=? "
                "ORDER BY updated_at DESC LIMIT 1", (default_owner,)).fetchone()
        if row and _agent_yaml_exists(row["slug"] or ""):
            return row["slug"]
    except Exception as exc:
        logger.warning("[BRIDGE] DB active-agent lookup unavailable: %r", exc)
    try:
        if ACTIVE_AGENT_FILE.exists():
            data = json.loads(ACTIVE_AGENT_FILE.read_text(encoding="utf-8")) or {}
            slug = data.get("active")
            if slug and (AGENTS_DIR / slug / "agent.yaml").exists():
                return slug
    except Exception as exc:
        logger.warning("[BRIDGE] Could not read active_agent.json: %r", exc)
    if (AGENTS_DIR / DEFAULT_AGENT / "agent.yaml").exists():
        return DEFAULT_AGENT
    try:
        for p in sorted(AGENTS_DIR.iterdir()):
            if p.is_dir() and not p.name.startswith("_") and (p / "agent.yaml").exists():
                return p.name
    except Exception:
        pass
    return DEFAULT_AGENT


def _agent_yaml_exists(slug: str) -> bool:
    return bool(slug) and (AGENTS_DIR / slug / "agent.yaml").exists()


# Sentinel slug: the DID is registered to a tenant but has no agent bound to
# the direction being used. Not a real folder — _build_agent_stack maps it to
# the neutral fallback persona. Exists so this case is DISTINCT from "agent
# failed to load", which is a different incident with a different fix.
UNROUTED_AGENT = "__unrouted__"


def _did_owner(did: str) -> str:
    """Tenant a registered DID belongs to. "" when unknown. Never raises.

    Used to attribute a call whose DID resolved to no agent — the tenant is
    known from the number even when the binding is missing, so their quota and
    their ledger are still the right ones to use.
    """
    if not did:
        return ""
    try:
        from shared.db.database import get_db
        from shared.phone import significant_digits
        sig = significant_digits(did)
        if not sig:
            return ""
        with get_db() as db:
            row = db.execute("SELECT owner FROM phone_numbers WHERE sig_digits=?",
                             (sig,)).fetchone()
        return (row["owner"] or "") if row else ""
    except Exception:
        return ""


# When false, an unclaimed explicit slug is allowed through with a warning
# instead of being refused. Exists purely so a fleet with unmigrated legacy
# agents can turn the check off for one deploy; leave it on.
STRICT_SLUG_OWNERSHIP = os.environ.get(
    "LESCALL_STRICT_SLUG_OWNERSHIP", "true").lower() in ("1", "true", "yes")

# What a refused caller HEARS. English only and deliberately vague about the
# reason: the person on the phone is usually the tenant's customer, not the
# tenant, and "this business has run out of prepaid minutes" is not their
# problem to hear. The tenant learns the real reason in the dashboard.
_REFUSAL_LINES = {
    "quota_minutes": "I'm sorry, we're unable to take calls right now. "
                     "Please try again a little later. Thank you for calling.",
    "cap_concurrency": "I'm sorry, all our lines are busy at the moment. "
                       "Please call back shortly. Thank you.",
    "account_locked": "I'm sorry, this number is not accepting calls at the "
                      "moment. Thank you for calling.",
    "unattributed": "I'm sorry, we're unable to take this call right now. "
                    "Thank you for calling.",
    "default": "I'm sorry, we're unable to take calls right now. "
               "Please try again later. Thank you for calling.",
}


def _explicit_slug_permitted(slug: str, did: str = "") -> bool:
    """May this caller-supplied slug answer this call?

    Two rules, both cheap:
      1. the slug must be CLAIMED in the agents index (an unclaimed folder on
         disk is not a tenant's agent — it is an unowned artifact);
      2. if the dialed DID is registered to a tenant, the slug's owner must be
         that same tenant — a DID may not be answered by another tenant's agent.

    NEVER raises and fails OPEN on a DB error: routing must not become a new
    way to drop live calls. The bind-to-loopback change (S2) is the wall; this
    is the second lock, not the only one.
    """
    if not STRICT_SLUG_OWNERSHIP:
        return True
    try:
        from shared.db.database import get_db
        from shared.phone import significant_digits
        with get_db() as db:
            row = db.execute("SELECT owner FROM agents WHERE slug=?",
                             (slug,)).fetchone()
            slug_owner = (row["owner"] or "") if row else ""
            if not slug_owner:
                return False
            sig = significant_digits(did) if did else ""
            if not sig:
                return True
            did_row = db.execute(
                "SELECT owner FROM phone_numbers WHERE sig_digits=?",
                (sig,)).fetchone()
            did_owner = (did_row["owner"] or "") if did_row else ""
            # Unregistered DID: nothing to contradict the claim.
            return (not did_owner) or did_owner == slug_owner
    except Exception as exc:
        logger.warning("[BRIDGE] slug ownership check unavailable (%r) — "
                       "allowing %s", exc, slug)
        return True


def resolve_agent_slug(did: str = "", campaign_id: int | None = None,
                       agent_slug: str = "", dial_meta: dict | None = None,
                       direction: str = "inbound") -> str:
    """Resolve WHICH agent answers this call (multi-tenant routing, plan §2).

    Precedence:
      1. explicit agent_slug   — set as a channel variable at originate time
      2. campaign.agent_slug   — outbound campaign binding (shared DB)
      3. phone_numbers lookup  — the DID's agent for THIS DIRECTION (shared DB)
      4. dial_meta agent_slug  — the call_link.json dial record (fallback link)
      5. active_agent.json     — legacy single-active-agent behavior

    `direction` picks which binding column step 3 reads: an outbound leg uses
    outbound_agent_slug (falling back to inbound_agent_slug when the tenant has
    not bound one), so a number's outbound sales agent is never mistaken for its
    inbound receptionist.

    NEVER raises: if the shared DB is absent/unseeded or a bound slug has no
    agent.yaml on disk, resolution falls through to the legacy path so a live
    call can never be dropped by routing.
    """
    if _agent_yaml_exists(agent_slug):
        # An explicit slug arrives as a media-path segment, so it is only as
        # trustworthy as whoever opened the WebSocket. "The folder exists on
        # disk" was the entire check — meaning anyone who reached :9001 could
        # name ANY tenant's agent and run a full AI session billed to them.
        # The slug must additionally be CLAIMED, and when the DID names a
        # different tenant's number the two must agree.
        if _explicit_slug_permitted(agent_slug, did):
            logger.info("[BRIDGE] agent %s resolved via explicit", agent_slug)
            return agent_slug
        # Fall through to DID/campaign routing rather than dropping the call:
        # a live caller must never hear dead air because of a routing dispute.
        logger.error("[BRIDGE] explicit agent slug %r REFUSED (unclaimed or "
                     "cross-tenant vs did=%s) — falling through to DID routing",
                     agent_slug, did or "-")
    elif agent_slug:
        logger.warning("[BRIDGE] explicit agent slug %r has no agent.yaml — falling through",
                       agent_slug)
    try:
        from shared.db.database import get_db
        from shared.phone import significant_digits
        with get_db() as db:
            if campaign_id is not None:
                row = db.execute("SELECT agent_slug FROM campaigns WHERE id=?",
                                 (campaign_id,)).fetchone()
                if row and _agent_yaml_exists(row["agent_slug"] or ""):
                    logger.info("[BRIDGE] agent %s resolved via campaign",
                                row["agent_slug"])
                    return row["agent_slug"]
            if did:
                sig = significant_digits(did)
                if sig:
                    row = db.execute(
                        "SELECT inbound_agent_slug, outbound_agent_slug "
                        "FROM phone_numbers WHERE sig_digits=?", (sig,)).fetchone()
                    if row:
                        # Outbound prefers its own binding; an unbound outbound
                        # slot degrades to the inbound agent rather than
                        # dropping through to the legacy global agent.
                        if direction == "outbound":
                            candidates = [row["outbound_agent_slug"],
                                          row["inbound_agent_slug"]]
                        else:
                            candidates = [row["inbound_agent_slug"]]
                        for cand in candidates:
                            if _agent_yaml_exists(cand or ""):
                                logger.info("[BRIDGE] agent %s resolved via did "
                                            "(direction=%s)", cand, direction)
                                return cand
                        # The DID is REGISTERED but has no usable agent bound.
                        #
                        # Falling through from here is a cross-tenant leak: the
                        # legacy path below picks whichever agent happens to be
                        # flagged is_active, which belongs to an arbitrary
                        # tenant. A customer calling tenant A's number would be
                        # answered by tenant B's agent, saying tenant B's
                        # company name — and the minutes would bill to B.
                        # Stop here and answer neutrally instead.
                        logger.error(
                            "[BRIDGE] DID %s is registered but has no agent bound "
                            "(direction=%s) — answering with the NEUTRAL fallback "
                            "rather than another tenant's agent", sig, direction)
                        return UNROUTED_AGENT
    except Exception as exc:
        logger.warning("[BRIDGE] DB agent routing unavailable (%r) — legacy resolution", exc)
    if dial_meta and _agent_yaml_exists(dial_meta.get("agent_slug", "")):
        logger.info("[BRIDGE] agent %s resolved via dial_meta",
                    dial_meta["agent_slug"])
        return dial_meta["agent_slug"]
    slug = _resolve_active_agent_slug()
    logger.info("[BRIDGE] agent %s resolved via active-legacy", slug)
    return slug


def _sig10(number: str) -> str:
    """Last ten digits — the comparable core of an Indian number however it was
    written (+91…, 91…, a trunk prefix like 9122… in front of the dial string)."""
    return "".join(c for c in (number or "") if c.isdigit())[-10:]


def _dial_matches_phone(caller_number: str, dial: dict | None,
                        dialed_number: str = "") -> bool:
    """True only when the dial record's phone suffix-matches THIS call's other
    party — the bar for letting a dial record speak for who we are talking to.

    Both numbers are offered because which one is the customer depends on the
    leg: inbound it is the caller id, outbound it is the number FreeSWITCH
    dialled (`Caller-Destination-Number`, carried here as `did`). Checking the
    caller id alone can never match on an outbound call, because on that leg
    the caller id is OUR origination number.
    """
    if not dial:
        return False
    b = _sig10(dial.get("phone", ""))
    if not b:
        return False
    return b in {x for x in (_sig10(caller_number), _sig10(dialed_number)) if x}


def _lookup_agent_owner(slug: str) -> str:
    """Tenant email for a slug from the agents index. Best-effort, never raises."""
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute("SELECT owner FROM agents WHERE slug=?", (slug,)).fetchone()
            return (row["owner"] or "") if row else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Outbound dial linkage
# ---------------------------------------------------------------------------
# The dashboard logs every outbound dial (phone, contact name, timestamp) to
# dashboard/call_link.json just before running the FreeSWITCH originate. When
# a pipeline session starts shortly after a logged dial, this call is treated
# as that outbound call and the contact's name/number are handed to the agent
# so it can greet the person by name and confirm identity.
CALL_LINK_FILE = Path(os.path.abspath("dashboard")) / "call_link.json"
OUTBOUND_DIAL_MATCH_WINDOW_S = 120.0


def _lookup_recent_outbound_dial(caller_number: str = "",
                                 dialed_number: str = "") -> dict | None:
    """Most recent dial logged within the match window; prefers a phone match.

    Best-effort timestamp heuristic (same contract as the dashboard's own
    _classify_direction) — returns None on any doubt so inbound calls are
    never mislabeled outbound.

    The returned record carries `_exact`: True when its phone actually matches
    this call's other party, False when it is merely the newest dial in the
    window. That distinction is the whole point. A time-only match is a fair
    hint that SOME dial is happening, and it is worthless as a statement about
    WHO is on this call — during a campaign burst the newest dial is almost
    always a different person. Callers must consult `_exact` before using
    anything identifying from it (see _resolve_call_routing).
    """
    try:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        entries: list[dict] = []
        try:
            from shared.db.database import get_db
            window_start = (now - datetime.timedelta(
                seconds=OUTBOUND_DIAL_MATCH_WINDOW_S)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            with get_db() as db:
                entries = [dict(r) for r in db.execute(
                    "SELECT owner, phone, contact_id, name, agent_slug,"
                    " dialed_at, context_note, followup_id, placed_by,"
                    " callback_meta, gender"
                    " FROM call_links WHERE dialed_at >= ?"
                    " ORDER BY dialed_at DESC LIMIT 50",
                    (window_start,)).fetchall()]
        except Exception as exc:
            logger.warning("[BRIDGE] call_links DB lookup failed (%r) — "
                           "using legacy file", exc)
        if not entries and CALL_LINK_FILE.exists():
            entries = (json.loads(CALL_LINK_FILE.read_text(encoding="utf-8"))
                       or [])[-50:]
        # Whichever of the two numbers is the customer on this leg — inbound
        # the caller id, outbound the dialled number. Before 2026-08-29 only
        # the caller id was consulted, so on every outbound leg the comparison
        # was against OUR origination number and could not match: the exact
        # branch never fired and every outbound call fell through to the
        # newest-dial fallback below.
        wanted = {d for d in (_sig10(caller_number), _sig10(dialed_number)) if d}
        best, best_age = None, None
        for entry in entries:
            try:
                dial_dt = datetime.datetime.fromisoformat(
                    str(entry.get("dialed_at", "")).replace("Z", "+00:00"))
                if dial_dt.tzinfo is None:  # DB timestamps come back naive-UTC
                    dial_dt = dial_dt.replace(tzinfo=datetime.timezone.utc)
            except Exception:
                continue
            age = (now - dial_dt).total_seconds()
            if not (0 <= age <= OUTBOUND_DIAL_MATCH_WINDOW_S):
                continue
            entry_digits = _sig10(entry.get("phone", ""))
            if entry_digits and entry_digits in wanted:
                return {**entry, "_exact": True}   # this really is that dial
            if best_age is None or age < best_age:
                best, best_age = entry, age
        # Time-only match: a hint that a dial burst is in progress, NOT a claim
        # about who is on this call. Flagged so no caller can mistake it.
        return {**best, "_exact": False} if best else None
    except Exception as exc:
        logger.warning("[BRIDGE] outbound dial lookup failed: %r", exc)
        return None


def _fetch_customer_context(owner: str, phone: str) -> dict | None:
    """Returning-caller context: one contacts lookup + one customer_memory
    read (with open-item counts as scalar subqueries). Precomputed at write
    time by the intel pipeline's digest stage, so this is ~1ms of indexed
    reads. Any failure returns None — context must NEVER delay or kill a
    call. In-proc TTL cache absorbs rapid re-calls."""
    if not owner or not phone:
        return None
    try:
        from shared.phone import significant_digits
        sig = significant_digits(phone)
        if not sig:
            return None
        key = (owner, sig)
        now = time.monotonic()
        hit = _customer_ctx_cache.get(key)
        if hit is not None and now - hit[0] < _CUSTOMER_CTX_TTL_S:
            return hit[1]

        from shared.db.database import get_db
        t0 = time.perf_counter()
        ctx: dict | None = None
        with get_db() as db:
            contact = db.execute(
                "SELECT id, name, gender FROM contacts "
                "WHERE owner=? AND sig_digits=?",
                (owner, sig)).fetchone()
            if contact:
                row = db.execute(
                    """SELECT cm.context_digest, cm.calls_count, cm.last_call_at,
                              (SELECT count(*) FROM followups f
                                WHERE f.contact_id=cm.contact_id
                                  AND f.status IN ('pending','scheduled')
                                  AND f.deleted_at IS NULL) AS open_followups,
                              (SELECT count(*) FROM commitments cc
                                WHERE cc.contact_id=cm.contact_id
                                  AND cc.status='open') AS open_commitments,
                              (SELECT c2.outcome FROM calls c2
                                WHERE c2.contact_id=cm.contact_id
                                ORDER BY c2.id DESC LIMIT 1) AS last_outcome
                         FROM customer_memory cm WHERE cm.contact_id=?""",
                    (contact["id"],)).fetchone()
                if row and (row["context_digest"] or row["calls_count"]):
                    ctx = {
                        "contact_id": contact["id"],
                        "name": (contact["name"] or "").strip(),
                        # Optional; drives the {honorific} placeholder on an
                        # INBOUND call, where there is no dial record to carry
                        # it. "" for the vast majority of contacts.
                        "gender": (contact["gender"] or "").strip().lower(),
                        "calls_count": row["calls_count"],
                        "last_call_at": row["last_call_at"],
                        "last_outcome": row["last_outcome"],
                        "open_followups": row["open_followups"],
                        "open_commitments": row["open_commitments"],
                        "digest": row["context_digest"] or "",
                    }
        if len(_customer_ctx_cache) >= _CUSTOMER_CTX_CACHE_MAX:
            _customer_ctx_cache.pop(next(iter(_customer_ctx_cache)))
        _customer_ctx_cache[key] = (now, ctx)
        logger.info("[BRIDGE] customer context %s in %.1fms",
                    "hit" if ctx else "none",
                    (time.perf_counter() - t0) * 1000)
        return ctx
    except Exception as exc:
        logger.warning("[BRIDGE] customer context fetch failed: %r", exc)
        return None


def _customer_number(direction: str, caller_number: str, did: str,
                     identity_dial: dict | None) -> str:
    """The OTHER party's number — the one fact every per-customer lookup keys on.

    Inbound that is the caller id. Outbound it is the number FreeSWITCH dialled
    (`Caller-Destination-Number`), NOT the channel's caller id, which on that
    leg is our own origination number.

    A VERIFIED dial record wins over the raw dial string because the dial
    string can carry a trunk prefix (we have seen "9122" + the number), and the
    record holds the normalized form the contacts table is keyed on. An
    unverified record is not consulted at all — it describes a different call.
    """
    if direction != "outbound":
        return (caller_number or "").strip()
    if identity_dial and identity_dial.get("phone"):
        return str(identity_dial["phone"]).strip()
    dialed = (did or "").strip()
    if dialed:
        try:
            from shared.phone import significant_digits
            sig = significant_digits(dialed)
            if sig:
                return sig
        except Exception:
            pass
        return dialed
    return (caller_number or "").strip()


def _resolve_call_routing(conn_id: str, caller_number: str = "", did: str = "",
                          campaign_id: int | None = None,
                          agent_slug: str = "",
                          dialed_number: str = "") -> dict:
    """All blocking routing work for call setup, bundled in one place.

    Runs the serial DB round-trips (recent-dial lookup → agent-slug
    resolution → owner lookup) that used to execute one-by-one on the event
    loop inside TelephonySession.__init__ (~250ms measured). The session
    factory runs this in a worker thread; __init__ falls back to calling it
    inline for any legacy direct-constructor path.
    """
    dial = None
    try:
        # `dialed_number` is the lescall_dest channel variable — the number
        # this leg actually dialled, set at originate time and belonging to
        # this channel alone. `did` (Caller-Destination-Number) is the
        # fallback: it is the customer's number on a leg that arrived through
        # a DID, but on an ORIGINATED leg FreeSWITCH fills it with the
        # dialplan extension, which is why it could never be matched against.
        dial = _lookup_recent_outbound_dial(caller_number, dialed_number or did)
    except Exception as exc:
        logger.warning("[%s] outbound dial lookup failed: %r", conn_id, exc)

    # Routing guard rails:
    # - The loose most-recent dial match is fine for LABELING a call
    #   outbound, but may only steer AGENT selection on an exact phone
    #   match — otherwise an unrelated inbound call arriving during a
    #   dial burst could inherit the dial's agent.
    # - On a confident outbound leg, Caller-Destination-Number is the
    #   CUSTOMER's number, not one of our DIDs — never DID-route on it.
    dial_phone_match = _dial_matches_phone(caller_number, dial,
                                          dialed_number or did)
    confident_outbound = campaign_id is not None or dial_phone_match
    routing_did = "" if confident_outbound else did
    routing_dial_meta = dial if dial_phone_match else None

    # IDENTITY vs DIRECTION — the distinction this whole function turns on.
    #
    # A time-only dial match may say "a dial burst is happening", and that is
    # enough to LABEL the leg outbound. It may never say WHO is on the call.
    # Until 2026-08-29 the unverified record was handed straight to
    # set_call_context(), so during a campaign the agent opened with whichever
    # contact had been dialled most recently: 44 calls across two tenants
    # greeted a real person by a stranger's name (RUNBOOK §33). The guard
    # already existed for agent selection two lines up — identity simply never
    # got it.
    identity_dial = dial if dial_phone_match else None
    if dial is not None and identity_dial is None:
        logger.info("[%s] dial record within the window is NOT this call "
                    "(dialed=%s record=%s) — no contact identity applied",
                    conn_id, _sig10(dialed_number or did) or "-",
                    _sig10(dial.get("phone", "")) or "-")

    # Direction is decided BEFORE the agent stack is built: it selects which
    # of the number's two agent bindings (inbound vs outbound) applies.
    direction = "outbound" if (campaign_id is not None or dial) else "inbound"

    slug = resolve_agent_slug(did=routing_did, campaign_id=campaign_id,
                              agent_slug=agent_slug, dial_meta=routing_dial_meta,
                              direction=direction)
    # An unrouted DID still belongs to somebody: attribute the call to the
    # number's tenant so their quota and their ledger are the ones used, even
    # though no agent of theirs answered it.
    owner = _lookup_agent_owner(slug) or _did_owner(routing_did or did)

    # Returning-caller memory: still inside the routing worker thread, so the
    # greeting path never pays for it. The customer's number is the caller id
    # inbound and the dialed number outbound.
    customer = None
    if INTEL_PRECALL_CONTEXT:
        # Returning-caller memory is IDENTITY too: loading it off an unverified
        # dial record hands one customer's history to another.
        customer_phone = (_customer_number(direction, caller_number,
                                           dialed_number or did, identity_dial))
        customer = _fetch_customer_context(owner, customer_phone) if customer_phone else None

    return {
        # `dial` is the direction hint and may be someone else's dial.
        # `identity_dial` is the only one allowed to name the person.
        "dial": dial,
        "identity_dial": identity_dial,
        "direction": direction,
        "slug": slug,
        "owner": owner,
        "customer": customer,
    }


def _build_agent_stack(conn_id: str, did: str = "", campaign_id: int | None = None,
                       agent_slug: str = "", dial_meta: dict | None = None,
                       direction: str = "inbound", resolved_slug: str = ""):
    """Resolve this call's agent and build (slug, conversation, stt, tts, health).

    Mirrors backend/run.py so telephony runs the data-driven agent identically
    to the CLI/web pipeline: STT corrections + TTS voice come from the agent's
    YAML. A load failure degrades to the default ConversationRouter rather than
    dropping the call.

    agent_health (F6): 'ok' | 'load_failed' | 'preflight_failed:<stage>' —
    recorded on the calls row so a degraded call is visible after the fact.
    """
    slug = resolved_slug or resolve_agent_slug(
        did=did, campaign_id=campaign_id,
        agent_slug=agent_slug, dial_meta=dial_meta,
        direction=direction)
    agent_def = None
    agent_health = "ok"
    if slug == UNROUTED_AGENT:
        # Registered DID with no agent bound. Answer politely as nobody in
        # particular rather than as some other tenant's brand.
        logger.error("[%s] [BRIDGE] no agent bound to this number — answering "
                     "with NEUTRAL_FALLBACK_AGENT", conn_id)
        agent_health = "unrouted"
        try:
            from backend.app.agent_definition.fallback_agent import (
                get_neutral_fallback_agent)
            agent_def = get_neutral_fallback_agent()
        except Exception as exc:
            logger.error("[%s] neutral fallback unavailable: %r", conn_id, exc)
        slug = UNROUTED_AGENT
    else:
        try:
            agent_def = agent_registry.get(slug)
        except Exception as exc:
            # NEVER fall back to another tenant's agent (the old path answered
            # as Pramukh's Vishal Patel). Substitute the built-in neutral
            # fallback: generic persona, no brand, apologizes and asks to call
            # back.
            logger.error("[%s] [BRIDGE] agent load FAILED for '%s': %r — "
                         "answering with NEUTRAL_FALLBACK_AGENT",
                         conn_id, slug, exc)
            agent_health = "load_failed"

    # F6 call-start preflight (PROMPT_PREFLIGHT, default ON): render every
    # stage × language prompt on THROWAWAY routers before the greeting. A
    # deterministic prompt-build bug (Incident B: per-turn TypeError swallowed
    # into "Sorry, could you repeat that?") is caught here in ~5-15ms and the
    # call degrades to the neutral fallback instead of a dead loop. The live
    # router/session state is never touched.
    if (agent_def is not None
            and os.environ.get("PROMPT_PREFLIGHT", "").strip().lower() != "false"):
        cache_key = None
        if PREFLIGHT_CACHE_ENABLED:
            try:
                _pmeta = agent_registry.meta(slug)
                if _pmeta and _pmeta.get("sha256"):
                    cache_key = (slug, _pmeta.get("sha256"), _pmeta.get("revision"))
            except Exception:
                cache_key = None
        if cache_key is not None and cache_key in _preflight_cache:
            agent_health = _preflight_cache[cache_key]
            logger.info("[%s] [PREFLIGHT] cache hit for '%s' → %s",
                        conn_id, slug, agent_health)
        else:
            try:
                from backend.app.agent_definition.preflight import preflight_agent
                agent_health = preflight_agent(agent_def, slug=slug)
                if cache_key is not None:
                    if len(_preflight_cache) >= _PREFLIGHT_CACHE_MAX:
                        _preflight_cache.pop(next(iter(_preflight_cache)))
                    _preflight_cache[cache_key] = agent_health
            except Exception as exc:  # preflight harness itself must never drop a call
                logger.error("[%s] [PREFLIGHT] harness error for '%s': %r",
                             conn_id, slug, exc, exc_info=True)
                agent_health = "ok"
        if agent_health != "ok":
            logger.error("[%s] [BRIDGE] preflight FAILED for '%s' (%s) — "
                         "answering with NEUTRAL_FALLBACK_AGENT",
                         conn_id, slug, agent_health)
            agent_def = None  # same degradation path as a load failure

    if agent_def is None:
        from backend.app.agent_definition.fallback_agent import get_neutral_fallback_agent
        agent_def = get_neutral_fallback_agent()

    call_id_provider = lambda: conn_id  # noqa: E731 - tiny provider closure
    corrections = agent_def.knowledge_base.stt_corrections if agent_def else None
    # Batch STT: multipart WAV POST to api.sarvam.ai/speech-to-text per
    # finalized VAD segment. The WebSocket streaming provider that briefly
    # sat in front of this was removed 2026-07-31 (owner request) — this is
    # the only STT primary again.
    stt_primary = TrackingSarvamSTT(call_id_provider=call_id_provider)
    # Per-call key lease (owner directive 2026-08-03): concurrent calls stop
    # sharing one Sarvam key's rate limits. One lease serves this call's STT
    # AND TTS; it re-leases only after a 402. SARVAM_PER_CALL_KEYS=false
    # restores the process-wide key.
    try:
        from backend.app.voice.key_rotator import per_call_key_provider
        _key_provider = per_call_key_provider(conn_id)
        stt_primary.key_provider = _key_provider
    except Exception as exc:
        _key_provider = None
        logger.warning("[%s] per-call key lease unavailable: %r",
                       conn_id, exc)
    stt = STTRouter(
        primary=stt_primary,
        fallback=TrackingWhisperSTT(call_id_provider=call_id_provider),
        stt_corrections=corrections,
    )
    tts_model, tts_speaker, tts_pace = "bulbul:v3", "shubh", 1.0
    if agent_def:
        dlang = agent_def.languages.default
        vcfg = agent_def.languages.tts_voices.get(dlang)
        if vcfg:
            tts_model, tts_speaker = vcfg.model, vcfg.speaker
            tts_pace = getattr(vcfg, "pace", 1.0) or 1.0
    tts = TrackingSarvamTTS(
        call_id_provider=call_id_provider,
        model=tts_model,
        speaker=tts_speaker,
        pace=tts_pace,
        sample_rate=TTS_OUTPUT_SR,
    )
    if _key_provider is not None:
        tts.key_provider = _key_provider
    # Seed the session language from the agent's own default — without this
    # every call opened in the hardcoded "hi" and English agents greeted with
    # a hi-IN accent (PLAN-tts-accent-consistency.md P0).
    conversation = ConversationRouter(
        agent_def=agent_def,
        default_language=(agent_def.languages.default or "hi") if agent_def else "hi",
        # Same closure the STT/TTS trackers get, so this call's token spend
        # lands on the same credit-log row as its speech spend.
        call_id_provider=call_id_provider,
    )
    # Namespaces this agent's LLM context cache (RUNBOOK §62): one provider
    # cache per agent slug, never shared across agents.
    conversation.agent_slug = slug or ""
    return slug, conversation, stt, tts, agent_health

class TelephonySession:
    """Manages one AI pipeline per phone call."""
    def __init__(self, conn_id: str, sample_rate: int, caller_number: str = "",
                 skip_greeting: bool = False, did: str = "", campaign_id: int | None = None,
                 agent_slug: str = "", routing: dict | None = None,
                 dialed_number: str = ""):
        self.conn_id = conn_id
        self.sample_rate = sample_rate
        self.caller_number = caller_number
        # The number an OUTBOUND leg actually dialled (lescall_dest channel
        # variable). Empty on inbound and on legacy dial paths.
        self.dialed_number = dialed_number
        self.did = did
        self.campaign_id = campaign_id
        self.call_key = None  # recording folder basename; set in start()
        self.source = TelephonySource(sample_rate=sample_rate)
        # Sink runs at the TTS output rate (8k skips ffmpeg), independent of
        # the 16k STT-side source rate.
        self.sink = TelephonySink(sample_rate=TTS_OUTPUT_SR, conn_id=conn_id)
        # Barge-in: the sink's interrupt path asks socket_server to
        # uuid_break the FS channel so audio already injected stops at the
        # media layer, not just in our queues.
        self.sink.on_interrupt = self._send_break_request
        self.hub = EventHub()

        # Routing (dial linkage, direction, agent slug, owner) is normally
        # pre-resolved in a worker thread by TelephonySession.create() so the
        # serial DB round-trips never run on the event loop; fall back to
        # inline resolution for any legacy direct-constructor path.
        if routing is None:
            routing = _resolve_call_routing(
                conn_id, caller_number=caller_number, did=did,
                campaign_id=campaign_id, agent_slug=agent_slug,
                dialed_number=dialed_number)
        # `_dial` is the record allowed to SPEAK FOR the person on this call,
        # so it is the phone-verified one only. `_dial_hint` keeps the loose
        # match for the things that legitimately only need "a dial happened"
        # (direction labelling). Conflating the two greeted 44 real people by
        # a stranger's name — RUNBOOK §33.
        self._dial = routing.get("identity_dial")
        self._dial_hint = routing.get("dial")
        # Local alias used further down for the call-context handoff. It is
        # the VERIFIED record on purpose: an unverified one must never reach
        # set_call_context(), which is what puts a name in the agent's mouth.
        dial = self._dial
        self.direction = routing.get("direction", "inbound")
        self.customer_number = _customer_number(
            self.direction, caller_number, dialed_number or did, self._dial)

        # Build the resolved agent's STT/TTS the same way run.py does.
        self.agent_slug, conversation, stt, tts, self.agent_health = _build_agent_stack(
            conn_id, resolved_slug=routing.get("slug", ""))
        self.conversation = conversation
        # F8 per-call fidelity: WHICH agent bytes answered this call. Meta is
        # captured by the registry at load; absent (e.g. neutral fallback or
        # put() entries) records 0/''.
        _meta = None
        try:
            _meta = agent_registry.meta(self.agent_slug)
        except Exception as exc:
            logger.warning("[%s] agent meta lookup failed: %r", conn_id, exc)
        self.agent_revision = int((_meta or {}).get("revision") or 0)
        self.agent_sha = str((_meta or {}).get("sha256") or "")
        # Owner was resolved with the rest of the routing bundle (off-loop).
        self.owner = routing.get("owner", "")
        logger.info("[%s] Active agent: %s rev=%s sha=%s (direction=%s did=%s "
                    "campaign=%s owner=%s health=%s)",
                    conn_id, self.agent_slug, self.agent_revision,
                    self.agent_sha[:12] or "-", self.direction, did or "-",
                    campaign_id if campaign_id is not None else "-",
                    self.owner or "-", self.agent_health)

        # Call context: was this session started by an outbound dial? If so,
        # hand the dialed contact's name/number to the agent so it opens by
        # confirming identity instead of asking who it's speaking with.
        try:
            if dial:
                conversation.set_call_context(
                    direction="outbound",
                    contact_name=dial.get("name", ""),
                    contact_phone=dial.get("phone", ""),
                    conn_id=conn_id,
                    # Optional and usually absent: only a contact list that
                    # carried a gender column has one. It rides the SAME
                    # verified dial record as the name, so it can never be
                    # borrowed from a different person's dial (RUNBOOK §33).
                    contact_gender=dial.get("gender") or "",
                )
                logger.info("[%s] Outbound call context: contact=%r phone=%s "
                            "gender=%s",
                            conn_id, dial.get("name"), dial.get("phone"),
                            dial.get("gender") or "-")
                # Callback context (call_links.context_note): why this call
                # is being made — WHY-YOU-ARE-CALLING-BACK prompt block.
                # callback_meta (scheduled follow-ups only) additionally makes
                # the OPENING line reference the earlier call.
                if dial.get("context_note") and hasattr(conversation, "set_callback_context"):
                    _cb_meta = dial.get("callback_meta")
                    if isinstance(_cb_meta, str) and _cb_meta.strip():
                        try:
                            _cb_meta = json.loads(_cb_meta)
                        except ValueError:
                            _cb_meta = None
                    conversation.set_callback_context(
                        str(dial["context_note"]),
                        meta=_cb_meta if isinstance(_cb_meta, dict) else None)
            else:
                conversation.set_call_context(conn_id=conn_id)
        except Exception as exc:
            logger.warning("[%s] set_call_context failed: %r", conn_id, exc)

        # Persistent customer memory: pre-fetched off-loop with the routing
        # bundle. The router renders it as the KNOWN CUSTOMER prompt band
        # (and honors the per-agent memory.use_customer_context flag).
        try:
            customer = routing.get("customer")
            if customer and hasattr(conversation, "set_customer_context"):
                conversation.set_customer_context(customer)
        except Exception as exc:
            logger.warning("[%s] set_customer_context failed: %r", conn_id, exc)

        # Initialize VoicePipeline with telephony endpoints + data-driven agent
        self.pipeline = VoicePipeline(
            audio_source=self.source,
            audio_sink=self.sink,
            event_hub=self.hub,
            conversation=conversation,
            stt=stt,
            tts=tts,
            mic_enabled=True,
            skip_greeting=skip_greeting,
        )
        self.pipeline_task = None
        self.playback_task = None
        self.event_task = None
        self._metering_task = None
        self._running = False
        # Agent-initiated hangup bookkeeping (see _maybe_agent_hangup).
        self._hangup_sent = False
        self._hangup_armed_ts: float | None = None

        # Rolling tail of recent transcript turns, for the dashboard's live
        # snapshot writer (see ws_audio_receiver.py). Purely additive — fed
        # from the same hub events already consumed for call-audit logging.
        # 200 turns ≈ the whole call for the dashboard's fullscreen live
        # transcript (6 was only enough for the old card snippet); still a
        # few KB of JSON per snapshot write.
        self.transcript_tail: deque = deque(maxlen=200)

        # Diagnostics
        self.stats = {
            "first_audio_t": None,
            "first_transcript_t": None,
            "first_response_t": None,
            "first_playback_t": None,
            "playback_count": 0,
            "start_time": time.time(),
        }
        
    @classmethod
    async def create(cls, conn_id: str, sample_rate: int, caller_number: str = "",
                     skip_greeting: bool = False, did: str = "",
                     campaign_id: int | None = None,
                     agent_slug: str = "",
                     dialed_number: str = "") -> "TelephonySession":
        """Async factory: resolve routing in a worker thread, then construct.

        Keeps the serial DB round-trips (dial lookup → slug resolution →
        owner) off the event loop so buffered inbound audio — and with it the
        greeting gate — isn't stalled behind blocking sqlite work.
        """
        routing = await asyncio.to_thread(
            _resolve_call_routing, conn_id, caller_number, did,
            campaign_id, agent_slug, dialed_number)
        session = cls(conn_id, sample_rate, caller_number=caller_number,
                      skip_greeting=skip_greeting, did=did,
                      campaign_id=campaign_id, agent_slug=agent_slug,
                      routing=routing, dialed_number=dialed_number)
        # E6 — billing admission, off the event loop, BEFORE the greeting gate
        # opens. A refused call is answered politely and hung up rather than
        # dropped silently: a caller who hears nothing redials, which costs us
        # another leg and them their patience.
        decision = await asyncio.to_thread(session.check_billing_admission)
        if not decision.allowed:
            session.billing_refusal = _REFUSAL_LINES.get(
                decision.code, _REFUSAL_LINES["default"])
            # Suppress the agent's own greeting: the pipeline reads these two
            # flags when its task starts, and that has not happened yet
            # (start() creates it). Setting skip_greeting on the SESSION would
            # do nothing — the pipeline was already constructed above.
            session.pipeline._skip_greeting = True
            session.pipeline._greeting_done = True
        return session

    async def start(self):
        """Starts the AI pipeline and the playback injection loop."""
        self._running = True
        SarvamCreditTracker.get().start_call(self.conn_id)
        # Subscribe SYNCHRONOUSLY, before anything can emit. EventHub has no
        # replay buffer, and the pipeline task reaches its opening-line emit
        # without ever suspending (the 25ms first-audio yield is skipped when
        # ws_audio_receiver has already pushed frame 1, which it now always
        # has — session setup grew past ~900ms). The agent's opening line was
        # therefore broadcast to zero subscribers and lost from EVERY audit
        # transcript from 2026-07-22 onward. subscribe() is a plain sync call
        # returning a bounded drop-oldest queue, so creating it here buffers
        # everything until _event_loop starts draining.
        self._hub_queue = self.hub.subscribe()
        self.pipeline_task = asyncio.create_task(self.pipeline.run_until_shutdown())
        self.playback_task = asyncio.create_task(self._playback_loop())
        _audit_session = await audit_manager.create(
            self.conn_id, caller_number=self.caller_number,
            agent_slug=self.agent_slug,
            agent_revision=self.agent_revision,
            agent_sha=self.agent_sha)
        # Capture the recording folder name NOW — it IS the call_key the
        # dashboard history/recordings/analysis are keyed by. The audit session
        # is popped from the manager at close(), so we can't fetch it later in
        # _persist_call_row; stash it on the session here.
        try:
            _cd = getattr(_audit_session, "_call_dir", "") if _audit_session else ""
            if _cd:
                self.call_key = os.path.basename(str(_cd).rstrip("/"))
        except Exception as exc:
            logger.warning("[%s] could not capture call_key: %r", self.conn_id, exc)
        # Consumer starts only now: add_event needs the audit session to
        # exist, and the queue above has been holding everything meanwhile.
        self.event_task = asyncio.create_task(self._event_loop())
        logger.info(f"[{self.conn_id}] Session started (Phase 14) call_key=%s", self.call_key)

        # E6 refusal: say one line through the normal TTS path, then arm the
        # existing agent-hangup drain so the channel drops only after the
        # sentence has actually reached the wire. Reusing hangup_pending means
        # this shares every guard the consented-close path already has.
        refusal = getattr(self, "billing_refusal", "")
        if refusal:
            try:
                self.pipeline._queue_watchdog_line(refusal, time.monotonic())
                conv = getattr(self.pipeline, "conversation", None)
                if conv is not None:
                    conv.hangup_pending = True
                logger.warning("[%s] BILLING refusal spoken; ending call",
                               self.conn_id)
            except Exception as exc:
                logger.error("[%s] BILLING refusal delivery failed: %r",
                             self.conn_id, exc)

        # BILLING: open this call's usage-ledger row and start the 15s
        # heartbeat. Metering must never affect call handling — every touch
        # is wrapped and off the event loop. (shared/usage_metering.py)
        # Deferred by default: the open is DB work the greeting doesn't need
        # to wait for; the single task preserves open-before-heartbeat order.
        if METERING_OPEN_DEFERRED:
            self._metering_task = asyncio.create_task(self._deferred_metering())
        else:
            try:
                await asyncio.to_thread(self._metering_open)
                self._metering_task = asyncio.create_task(self._metering_beat_loop())
            except Exception as exc:
                logger.error("[%s] METERING open failed: %r", self.conn_id, exc)

    async def _deferred_metering(self):
        try:
            await asyncio.to_thread(self._metering_open)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[%s] METERING open failed: %r", self.conn_id, exc)
            return
        await self._metering_beat_loop()
        
    async def stop(self):
        """Stops and cleans up the session."""
        self._running = False
        
        # PHASE17_DEBUG_DISABLE_BARGE_IN_START
        # Wait briefly for any pending playback to enqueue
        if not self.sink.wav_queue.empty():
            logger.info(f"[{self.conn_id}] [PLAYBACK_PRESERVATION] Draining wav_queue before stop")
            for _ in range(10):
                if self.sink.wav_queue.empty():
                    break
                await asyncio.sleep(0.1)
        # PHASE17_DEBUG_DISABLE_BARGE_IN_END

        if self.pipeline_task:
            await self.pipeline.stop()
            self.pipeline_task.cancel()
            try:
                await self.pipeline_task
            except asyncio.CancelledError:
                pass
        
        for t in [self.playback_task, self.event_task]:
            if t:
                # PHASE17_DEBUG_DISABLE_BARGE_IN_START
                # Make sure we don't hard-cancel mid-injection if we can help it, 
                # though `_playback_loop` checks `self._running`. We will let it gracefully exit.
                # PHASE17_DEBUG_DISABLE_BARGE_IN_END
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        # If start() never got as far as creating the event task, the queue
        # it pre-subscribed is still registered — release it.
        if getattr(self, "_hub_queue", None) is not None and not self.event_task:
            try:
                self.hub.unsubscribe(self._hub_queue)
            except Exception:
                pass
            self._hub_queue = None

        await self.source.stop()
        await self.sink.stop()
        self._print_summary()

        # Persist the conversation-state snapshot (slots + audit trail) so every
        # call leaves a machine-readable record of what was collected and when.
        try:
            conv = getattr(self.pipeline, "conversation", None)
            sm = getattr(conv, "state_mgr", None)
            if sm is not None:
                # Cancel any in-flight LLM slot extractor BEFORE snapshotting so
                # a late merge can't land after the file is written.
                task = getattr(conv, "_slot_extract_task", None)
                if task is not None and not task.done():
                    task.cancel()
                call_dir = audit_manager.call_dir(self.conn_id) if hasattr(audit_manager, "call_dir") else None
                if not call_dir:
                    call_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "recordings", "_state")
                os.makedirs(call_dir, exist_ok=True)
                state_path = os.path.join(call_dir, f"state_{self.conn_id}.json")
                with open(state_path, "w", encoding="utf-8") as f:
                    f.write(sm.to_json())
                logger.info("[%s] Conversation state persisted: %s", self.conn_id, state_path)
        except Exception as exc:
            logger.warning("[%s] State persistence failed: %r", self.conn_id, exc)

        # Close audit session — collect language from conversation state
        try:
            lang = "unknown"
            fallback_used = False
            if hasattr(self.pipeline, "conversation") and hasattr(self.pipeline.conversation, "state"):
                lang = getattr(self.pipeline.conversation.state, "language", "unknown") or "unknown"
            await audit_manager.close(
                self.conn_id,
                language_detected=lang,
                stt_provider="sarvam",
                fallback_used=fallback_used,
            )
        except Exception as exc:
            logger.warning("[%s] Audit close failed: %r", self.conn_id, exc)

        try:
            SarvamCreditTracker.get().end_call(self.conn_id)
        except Exception as exc:
            logger.warning("[%s] Credit end_call failed: %r", self.conn_id, exc)

        # BILLING: stop the heartbeat and close the usage-ledger row. If this
        # ever fails, the reconciler crash-closes the row from its last beat —
        # usage is never lost, at most ~15s under-counted.
        task = getattr(self, "_metering_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            from shared.usage_metering import close_call
            await asyncio.to_thread(close_call, self.conn_id)
        except Exception as exc:
            logger.error("[%s] METERING close failed (reconciler will "
                         "recover): %r", self.conn_id, exc)

        # Release the admission hold now that the ledger row is closed and the
        # real seconds are recorded. A crashed teardown leaks the hold only
        # until it expires, never permanently.
        _decision = getattr(self, "_billing_decision", None)
        if _decision is not None and getattr(_decision, "reservation", ""):
            try:
                from shared.billing.entitlements import release_reservation
                await asyncio.to_thread(release_reservation, _decision.reservation)
            except Exception as exc:
                logger.warning("[%s] BILLING reservation release failed: %r",
                               self.conn_id, exc)

        # Owner-tagged calls row for the multi-tenant dashboard (plan §2).
        # Off the event loop: a contended sqlite write (busy_timeout 5s) must
        # never stall audio for other live calls sharing this loop.
        try:
            await asyncio.to_thread(self._persist_call_row)
        except Exception as exc:
            logger.warning("[%s] Call row persistence failed: %r", self.conn_id, exc)

        # Capture the FULL two-party recording (agent + caller) from
        # FreeSWITCH's record_session WAV into the call folder. Must precede
        # the GCS upload so both files ship together; the recording-sync pass
        # is the catch-up if this misses. capture_fs_recording never raises —
        # log its boolean result here so a failure (e.g. EACCES on the
        # freeswitch-owned staging file) is loudly visible in THIS process,
        # not just a maybe-suppressed library logger.
        try:
            from shared.recording_storage import capture_fs_recording
            if self.call_key:
                ok = await asyncio.to_thread(
                    capture_fs_recording, self.conn_id, self.call_key)
                if ok:
                    logger.info("[%s] Full two-party recording captured", self.conn_id)
                else:
                    logger.warning("[%s] Full-recording capture DID NOT land "
                                   "(recsync pass will retry in <=15 min)",
                                   self.conn_id)
        except Exception as exc:
            logger.warning("[%s] Full-recording capture failed (sync pass "
                           "will retry): %r", self.conn_id, exc)

        # Push the finished WAV to GCS (best-effort, off the loop). Failures
        # are retried by the recording-sync pass (shared/recording_storage.py
        # CLI); until then the local file remains the serving copy.
        try:
            from shared.recording_storage import gcs_enabled, upload_recording
            if self.call_key and gcs_enabled():
                await asyncio.to_thread(upload_recording, self.call_key)
        except Exception as exc:
            logger.warning("[%s] Recording upload failed: %r", self.conn_id, exc)

        # Hand the finished call to the intelligence pipeline (best-effort;
        # the worker's sweeper enqueues any call this misses).
        try:
            await asyncio.to_thread(self._enqueue_intelligence)
        except Exception as exc:
            logger.warning("[%s] Intelligence enqueue failed (sweeper will "
                           "recover): %r", self.conn_id, exc)

        logger.info(f"[{self.conn_id}] Session stopped")

    def _enqueue_intelligence(self) -> None:
        from shared.db.database import get_db
        from shared.intelligence import queue

        with get_db() as db:
            row = db.execute(
                "SELECT id, owner FROM calls WHERE conn_id=?", (self.conn_id,)
            ).fetchone()
        if row:
            queue.enqueue(queue.KIND_CALL_PROCESS, owner=row["owner"] or "",
                          call_id=row["id"],
                          dedupe_key=f"call.process:{self.conn_id}")

    # ------------------------------------------------------------------
    # BILLING metering hooks (shared/usage_metering.py). The ledger is the
    # basis for charging users; these must be correct AND must never take
    # a call down. All DB writes run in worker threads.
    # ------------------------------------------------------------------

    def _metering_owner(self) -> str:
        """The tenant this call is billed to, or "" if it cannot be attributed.

        There is deliberately NO fallback to LESCALL_DEFAULT_OWNER any more.
        That fallback and `_resolve_call_routing`'s owner="" disagreed, and the
        disagreement was exploitable: a call naming a slug with no agents row
        checked its quota against the empty-string tenant (whose usage never
        accumulates) and billed its minutes to the founding account (which is
        unlimited). Naming an unclaimed slug was a self-serve free-minutes
        switch. An unattributable call is now refused, not defaulted —
        see billing_owner() in shared/billing/entitlements.py.
        """
        try:
            from shared.billing.entitlements import billing_owner
            return billing_owner({"owner": self.owner})
        except Exception:
            return (self.owner or "").strip().lower()

    def _metering_open(self):
        from shared.usage_metering import open_call
        phone = getattr(self, "customer_number", "") or self.caller_number
        owner = self._metering_owner()
        decision = getattr(self, "_billing_decision", None)
        # An unattributed call is metered but NOT billable: it must leave a
        # trace (so the volume is visible and recoverable) without landing
        # minutes in someone else's account.
        billable = bool(owner)
        open_call(self.conn_id, owner or "unattributed",
                  agent_slug=self.agent_slug, direction=self.direction,
                  phone=phone or None, billable=billable)
        # Stamp HOW this call was admitted, so fail-open minutes stay
        # identifiable after the fact instead of being indistinguishable from
        # properly admitted usage.
        check = getattr(decision, "check", "ok") if decision else (
            "ok" if billable else "unattributed")
        try:
            from shared.db.database import get_db
            with get_db() as db:
                db.execute("UPDATE usage_ledger SET entitlement_check=? "
                           "WHERE conn_id=?", (check, self.conn_id))
        except Exception as exc:
            logger.warning("[%s] METERING entitlement stamp failed: %r",
                           self.conn_id, exc)

    def check_billing_admission(self) -> object:
        """E6. Decide whether this call may proceed, and reserve for it.

        Called from the session factory BEFORE the greeting fires. Returns a
        Decision; the caller answers with the neutral refusal prompt and hangs
        up when it denies. Never raises — a live call must not drop because
        the billing DB hiccupped, which is why the fail-open inside
        check_dial_allowed is bounded rather than blind.
        """
        try:
            from shared.billing.entitlements import check_dial_allowed
            decision = check_dial_allowed(
                self._metering_owner(),
                origin=("inbound" if self.direction == "inbound" else "outbound"),
                ref=self.conn_id)
        except Exception as exc:
            logger.error("[%s] BILLING admission check unavailable: %r",
                         self.conn_id, exc)
            from shared.billing.errors import Decision
            decision = Decision.ok(check="error")
        self._billing_decision = decision
        if not decision.allowed:
            logger.warning("[%s] BILLING refused call for owner=%r: %s",
                           self.conn_id, self.owner, decision.code)
        return decision

    async def _metering_beat_loop(self):
        from shared.usage_metering import heartbeat
        try:
            while self._running:
                await asyncio.sleep(15)
                if not self._running:
                    break
                try:
                    await asyncio.to_thread(heartbeat, self.conn_id)
                except Exception as exc:
                    logger.warning("[%s] METERING heartbeat failed: %r",
                                   self.conn_id, exc)
        except asyncio.CancelledError:
            pass

    def _collect_call_media(self, call_key):
        """Gather transcript/summary/collected-data for the calls row.

        Everything is best-effort: a missing transcript file or an agent
        without a state manager must never block the row write."""
        import json
        from shared.recording_storage import RECORDINGS_DIR

        transcript = None
        recording_path = None
        if call_key:
            base = str(RECORDINGS_DIR / call_key)
            t_path = os.path.join(base, "transcript.txt")
            try:
                if os.path.exists(t_path):
                    with open(t_path, "r", encoding="utf-8", errors="replace") as f:
                        transcript = f.read().strip() or None
            except Exception as exc:
                logger.warning("[%s] transcript read failed: %r", self.conn_id, exc)
            wav = os.path.join(base, "caller_audio.wav")
            if os.path.exists(wav):
                recording_path = wav

        summary = None
        collected = None
        try:
            conv = getattr(self.pipeline, "conversation", None)
            sm = getattr(conv, "state_mgr", None)
            if sm is not None:
                data = sm.to_dict()
                summary = data.get("summary") or None
                collected = json.dumps(data, ensure_ascii=False)
        except Exception as exc:
            logger.warning("[%s] collected-data snapshot failed: %r", self.conn_id, exc)
        return transcript, summary, collected, recording_path

    def _agent_was_mute(self) -> bool:
        """True when this call's TTS produced no audible turn at all.

        Deliberately conservative: a single audible utterance is enough to
        say the line was working, so a mid-call TTS hiccup never re-labels a
        genuinely silent callee. Never raises — teardown path.
        """
        try:
            tts = getattr(self.pipeline, "tts", None)
            if tts is None:
                return False
            spoke = int(getattr(tts, "spoke_utterances", 0) or 0)
            mute = int(getattr(tts, "mute_utterances", 0) or 0)
            blocked = bool(getattr(tts, "credit_blocked", False))
            return spoke == 0 and (mute > 0 or blocked)
        except Exception:
            return False

    def _compute_answer_state(self, duration_s: float):
        """Did a real conversation happen on this connected call?

        FreeSWITCH reports the same "+OK / answered" whether a person picked up
        and talked, picked up and said nothing, or an answering machine took
        the call — the dial result cannot tell them apart. This looks at what
        the caller actually contributed and returns
        ``('empty' | 'answered', reason)``.

        Returns ``(None, None)`` if anything goes wrong: an unclassified call
        must read as a normal answered call, never as a false 'empty'.
        Never raises — it runs inside call teardown.
        """
        try:
            # (0) The engine KNOWS a machine had the line (RUNBOOK §63): a
            # mailbox or network announcement it closed on, or a screener /
            # hold that no person ever came through.
            conv = getattr(self.pipeline, "conversation", None)
            if conv is not None and not getattr(conv, "_human_spoke", True):
                flags = getattr(getattr(conv, "state_mgr", None),
                                "extra_flags", {}) or {}
                _kind = flags.get("machine_answered") or getattr(
                    conv, "_machine_wait_kind", None)
                if _kind:
                    return "empty", {"voicemail": "voicemail_detected",
                                     "unreachable": "unreachable_announcement",
                                     "screener": "screened_no_pickup",
                                     "hold": "hold_no_pickup"}.get(
                                         str(_kind), f"machine_{_kind}")
            turns = list(getattr(self, "transcript_tail", None) or [])
            caller_raw = [str(t.get("text") or "").strip() for t in turns
                          if str(t.get("speaker") or "").strip().lower().startswith("caller")]
            caller_raw = [t for t in caller_raw if t]

            # (1) The callee never produced a single transcript.
            if not caller_raw:
                # ...but first: were WE audible? A call where TTS never
                # emitted a single audio chunk is dead air from the
                # customer's side — they were never spoken to, so of course
                # they never answered. Recording that as "no_caller_speech"
                # blames them for our outage and, worse, marks the phone-book
                # row 'empty_call' so the number reads as unreachable. On
                # 2026-09-08 two calls were logged exactly that way while
                # every Sarvam key sat wrongly marked dead (RUNBOOK §34).
                if self._agent_was_mute():
                    return "empty", "agent_tts_unavailable"
                return "empty", "no_caller_speech"

            # (2) Everything they produced was noise/filler. The STT router
            # already rejects most of this upstream, so this is a second pass
            # for anything that slipped through as a "valid" transcript.
            try:
                from backend.app.voice.stt.router import validate_transcript
                meaningful = [t for t in caller_raw if validate_transcript(t)]
            except Exception:
                meaningful = caller_raw
            if not meaningful:
                return "empty", "caller_noise_only"

            # (2b) Only an acknowledgement — "hello?" and then nothing. Two
            # turns or fewer, and barely any content across all of them.
            if len(meaningful) <= 2 and sum(len(t) for t in meaningful) < _EMPTY_CALL_MIN_CHARS:
                return "empty", "caller_silent"

            # (3) Voicemail / recorded greeting. Either a known announcement
            # phrase, or the shape of one: a single long uninterrupted
            # utterance and nothing responsive afterwards. Heuristic — a
            # talkative human opening looks the same, so it is checked last.
            low = " ".join(meaningful).lower()
            if any(p in low for p in _VOICEMAIL_PHRASES):
                return "empty", "voicemail_phrase"
            if len(meaningful) == 1 and len(meaningful[0]) >= _VOICEMAIL_MIN_CHARS:
                return "empty", "voicemail_suspected"

            # (4) Answered, but over before a conversation could happen.
            if duration_s and duration_s < _EMPTY_CALL_MAX_SECONDS:
                return "empty", "too_short"

            return "answered", None
        except Exception as exc:
            logger.warning("[%s] answer-state detection failed: %r", self.conn_id, exc)
            return None, None

    def _compute_outcome(self):
        """Deterministic call outcome (plan §4.6) — ONLY when the agent
        configures post_call.outcomes (else (None, None) = today, including
        Pramukh until backfilled)."""
        try:
            conv = getattr(self.pipeline, "conversation", None)
            adef = getattr(conv, "_def", None)
            outcomes = list(getattr(getattr(adef, "post_call", None), "outcomes", None) or [])
            if not outcomes or conv is None:
                return None, None
            sm = getattr(conv, "state_mgr", None)
            flags = getattr(sm, "extra_flags", {}) if sm is not None else {}
            booking = getattr(conv, "memory", None)
            appt = bool(getattr(getattr(booking, "booking", None), "appointment_time", None))
            signal = getattr(conv, "call_outcome", "") or ""
            if not signal:
                if appt:
                    signal = "booked"
                elif flags.get("whatsapp_promised"):
                    signal = "whatsapp_followup"
                elif flags.get("callback_requested"):
                    signal = "callback_requested"
                else:
                    signal = "no_outcome"
            by_id = {str(o.get("id") or "").lower(): o for o in outcomes if isinstance(o, dict)}
            hit = by_id.get(signal.lower())
            if hit is None:
                # kind-level fallback: success ← appointment; progress ← any
                # follow-up promise; failure ← everything else.
                want_kind = ("success" if appt or signal == "completed" else
                             "progress" if signal in ("whatsapp_followup",
                                                      "callback_requested") else
                             "failure")
                hit = next((o for o in outcomes
                            if str(o.get("kind") or "") == want_kind), None)
            if hit is None:
                return signal, None
            return (str(hit.get("id") or signal),
                    str(hit.get("kind") or "") or None)
        except Exception as exc:
            logger.warning("[%s] outcome computation failed: %r", self.conn_id, exc)
            return None, None

    def _resolve_contact_id(self, db, phone: str) -> int | None:
        """Bind this call to the owner's phone-book contact (by the last 10
        significant digits); auto-create a bare contact for unknown callers so
        EVERY call carries a contact_id. Same number under a different owner
        is a different contact — lookup is always owner-scoped."""
        from shared.phone import significant_digits

        sig = significant_digits(phone)
        if not sig:
            return None
        owner = self.owner or os.environ.get("LESCALL_DEFAULT_OWNER", "tech@dhronai.com")
        row = db.execute(
            "SELECT id FROM contacts WHERE owner=? AND sig_digits=?",
            (owner, sig)).fetchone()
        if row:
            return row["id"]
        # Unknown caller — create a minimal contact (implicit "No group").
        name = ""
        dial = getattr(self, "_dial", None)
        if dial and dial.get("name"):
            name = dial["name"]
        else:
            try:
                name = getattr(self.pipeline.conversation.state, "customer_name", "") or ""
            except Exception:
                pass
        legacy_id = str(int(time.time() * 1000)) + "_" + phone[-4:]
        db.execute(
            "INSERT INTO contacts (legacy_id, owner, phone, sig_digits, name, "
            "notes, call_status, disposition, call_count, extra, added_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT (owner, sig_digits) DO NOTHING",
            (legacy_id, owner, phone, sig, name,
             "auto-created from call", "not_called", "not_classified", 0,
             '{"call_history": []}'))
        row = db.execute(
            "SELECT id FROM contacts WHERE owner=? AND sig_digits=?",
            (owner, sig)).fetchone()
        return row["id"] if row else None

    def _persist_call_row(self):
        """Best-effort INSERT into shared DB `calls`; must never block teardown."""
        import datetime
        from shared.db.database import get_db
        from shared.phone import significant_digits
        from shared.recording_storage import new_download_token, public_download_url

        lang = ""
        try:
            conv = self.pipeline.conversation
            # The language the CALL was in, not the last turn's flip — see
            # ConversationRouter.dominant_language (RUNBOOK §56).
            _dom = getattr(conv, "dominant_language", None)
            lang = (_dom() if callable(_dom) else "") or \
                getattr(conv.state, "language", "") or ""
        except Exception:
            pass
        started = self.stats.get("start_time") or time.time()
        ended = time.time()

        # On outbound legs the channel's caller id is OUR origination number.
        # The customer is the number FreeSWITCH dialled, preferring a VERIFIED
        # dial record's normalized form — never an unverified one, which is
        # how calls.phone and calls.contact_id used to be stamped with the
        # wrong person entirely (RUNBOOK §33).
        phone = getattr(self, "customer_number", "") or self.caller_number
        dial = getattr(self, "_dial", None)

        # Billing transparency (0009): who initiated the call. Inbound = the
        # customer dialled us; outbound = whatever the dial record declared
        # (user | autodial | automation). A matched dial that predates 0009
        # carries no placed_by — fall back on followup_id (autodialer is the
        # only path that sets it), else assume a human clicked Call.
        placed_by = "user"
        if self.direction == "outbound" and dial:
            placed_by = (dial.get("placed_by")
                         or ("autodial" if dial.get("followup_id") else "user"))

        def _iso(t: float) -> str:
            return datetime.datetime.fromtimestamp(
                t, datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

        # Recording folder basename == the call_key the dashboard history/
        # analysis/recordings are all keyed by. Captured in start() (the audit
        # session is gone by now), it's what lets the owner-scoped history map a
        # recording folder back to its tenant.
        call_key = self.call_key
        transcript, summary, collected, recording_path = self._collect_call_media(call_key)
        outcome, outcome_kind = self._compute_outcome()
        answer_state, answer_reason = self._compute_answer_state(
            round(ended - started, 1))
        if answer_state == "empty":
            logger.info("[%s] answer_state=empty reason=%s duration=%.1fs",
                        self.conn_id, answer_reason, ended - started)

        # Public download link: a fresh 256-bit random token per call. On a
        # reconnect upsert the FIRST segment's token wins (COALESCE on the
        # existing row) so an already-shared link keeps working.
        token = new_download_token()
        rec_url = public_download_url(token)

        # A mid-call WS reconnect produces two sessions with one conn_id; the
        # upsert extends the first row instead of dropping the second segment.
        with get_db() as db:
            contact_id = None
            try:
                if phone:
                    contact_id = self._resolve_contact_id(db, phone)
                # Relationship default: the first agent that talked to this
                # contact becomes its primary agent (never overwrites an
                # explicit assignment).
                if contact_id and self.agent_slug:
                    db.execute(
                        "UPDATE contacts SET primary_agent_slug=? "
                        "WHERE id=? AND primary_agent_slug IS NULL",
                        (self.agent_slug, contact_id))
            except Exception as exc:
                logger.warning("[%s] contact binding failed: %r", self.conn_id, exc)
            # F8 fidelity fields. llm_models_used / llm_fallback_turns are
            # populated by the ConversationRouter (parallel change); getattr
            # with defaults keeps this write correct regardless of which side
            # lands first.
            conv = getattr(self, "conversation", None)
            llm_models_used = json.dumps(
                getattr(conv, "llm_models_used", []) or [])
            llm_fallback_turns = int(
                getattr(conv, "llm_fallback_turns", 0) or 0)
            db.execute(
                "INSERT INTO calls (conn_id, call_key, direction, agent_slug, campaign_id, "
                "phone, sig_digits, language, started_at, ended_at, duration_s, owner, "
                "transcript, summary, collected_data, recording_path, "
                "download_token, recording_url, contact_id, outcome, outcome_kind, "
                "agent_revision, agent_sha, agent_health, "
                "llm_models_used, llm_fallback_turns, placed_by, "
                "answer_state, answer_state_reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (conn_id) DO UPDATE SET "
                "call_key=COALESCE(excluded.call_key, calls.call_key), "
                "ended_at=excluded.ended_at, "
                "duration_s=COALESCE(calls.duration_s,0)+excluded.duration_s, "
                "language=COALESCE(NULLIF(excluded.language,''), calls.language), "
                "transcript=COALESCE(excluded.transcript, calls.transcript), "
                "summary=COALESCE(excluded.summary, calls.summary), "
                "collected_data=COALESCE(excluded.collected_data, calls.collected_data), "
                "recording_path=COALESCE(excluded.recording_path, calls.recording_path), "
                "download_token=COALESCE(calls.download_token, excluded.download_token), "
                "recording_url=COALESCE(calls.recording_url, excluded.recording_url), "
                "contact_id=COALESCE(calls.contact_id, excluded.contact_id), "
                "outcome=COALESCE(excluded.outcome, calls.outcome), "
                "outcome_kind=COALESCE(excluded.outcome_kind, calls.outcome_kind), "
                # First segment's fidelity record wins on a reconnect upsert.
                "agent_revision=COALESCE(calls.agent_revision, excluded.agent_revision), "
                "agent_sha=COALESCE(calls.agent_sha, excluded.agent_sha), "
                "agent_health=COALESCE(calls.agent_health, excluded.agent_health), "
                "llm_models_used=COALESCE(calls.llm_models_used, excluded.llm_models_used), "
                "llm_fallback_turns=COALESCE(calls.llm_fallback_turns, excluded.llm_fallback_turns), "
                "placed_by=COALESCE(calls.placed_by, excluded.placed_by), "
                # A reconnect splits one conversation across two segments; the
                # segment that heard real speech decides. Only let 'empty'
                # stand if the existing row is also empty/unset.
                "answer_state=CASE WHEN excluded.answer_state='answered' "
                "                  OR calls.answer_state IS NULL "
                "             THEN excluded.answer_state ELSE calls.answer_state END, "
                "answer_state_reason=CASE WHEN excluded.answer_state='answered' "
                "                    THEN NULL ELSE COALESCE(calls.answer_state_reason, "
                "                                            excluded.answer_state_reason) END",
                (self.conn_id, call_key, self.direction, self.agent_slug, self.campaign_id,
                 phone or None, significant_digits(phone) or None,
                 lang or None, _iso(started), _iso(ended), round(ended - started, 1),
                 self.owner or None,
                 transcript, summary, collected, recording_path,
                 token, rec_url, contact_id, outcome, outcome_kind,
                 int(getattr(self, "agent_revision", 0) or 0),
                 str(getattr(self, "agent_sha", "") or "") or None,
                 str(getattr(self, "agent_health", "") or "") or None,
                 llm_models_used, llm_fallback_turns, placed_by,
                 answer_state, answer_reason),
            )

            # Mirror an empty call onto the phone-book row so the Outbound
            # status filter can find these contacts. Only overwrites the
            # dial-time result ('answered' / never called) — a status the user
            # or a later real conversation set is left alone.
            if (answer_state == "empty"
                    and answer_reason != "agent_tts_unavailable"
                    and contact_id):
                try:
                    db.execute(
                        "UPDATE contacts SET call_status='empty_call' "
                        " WHERE id=? AND COALESCE(call_status,'') "
                        "       IN ('answered', 'not_called', '')",
                        (contact_id,))
                except Exception as exc:
                    logger.warning("[%s] contact empty_call mark failed: %r",
                                   self.conn_id, exc)

        # Turn-level traces (0012) — deliberately a SEPARATE transaction:
        # a missing call_turns table (sqlite dev fallback, un-migrated PG)
        # must never roll back the calls row above.
        try:
            self._persist_call_turns(_iso)
        except Exception as exc:
            logger.warning("[%s] call_turns persist failed: %r",
                           self.conn_id, exc)

    def _persist_call_turns(self, _iso):
        """Batch-insert the pipeline's in-memory turn traces. Idempotent on
        reconnect upserts via the (call_id, turn_index) unique key."""
        from shared.db.database import get_db

        traces = list(getattr(self.pipeline, "turn_traces", []) or [])
        if not traces:
            return
        with get_db() as db:
            row = db.execute("SELECT id FROM calls WHERE conn_id=?",
                             (self.conn_id,)).fetchone()
            if not row:
                return
            call_id = row["id"]
            for t in traces:
                # rag_sources: the documents whose chunks were placed in this
                # turn's prompt (migration 0018). Serialised here rather than in
                # the pipeline so the in-memory trace stays a plain dict.
                _sources = t.get("rag_sources")
                db.execute(
                    "INSERT INTO call_turns (call_id, turn_index, ts, stage, "
                    "intent, language, user_chars, agent_chars, "
                    "stt_latency_ms, stt_confidence, speech_end_to_engine_ms, "
                    "llm_first_chunk_ms, llm_total_ms, "
                    "speech_end_to_first_audio_ms, is_filler, barged_in, "
                    "rag_sources, rag_trace_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT (call_id, turn_index) DO NOTHING",
                    (call_id, t.get("turn_index"),
                     _iso(t["ts"]) if t.get("ts") else None,
                     t.get("stage"), t.get("intent"), t.get("language"),
                     t.get("user_chars"), t.get("agent_chars"),
                     t.get("stt_latency_ms"), t.get("stt_confidence"),
                     t.get("speech_end_to_engine_ms"),
                     t.get("llm_first_chunk_ms"), t.get("llm_total_ms"),
                     t.get("speech_end_to_first_audio_ms"),
                     bool(t.get("is_filler")), bool(t.get("barged_in")),
                     json.dumps(_sources) if _sources else None,
                     t.get("rag_trace_id")),
                )
        logger.info("[%s] call_turns: persisted %d turn traces",
                    self.conn_id, len(traces))

    async def _event_loop(self):
        """Listens for AI pipeline events to capture first_transcript_t."""
        # start() pre-subscribes so nothing emitted before this task runs is
        # lost; the fallback keeps the CLI/--web paths working unchanged.
        queue = getattr(self, "_hub_queue", None) or self.hub.subscribe()
        try:
            while self._running:
                event = await queue.get()
                etype = event.get("type")
                if etype == "user":
                    if self.stats["first_transcript_t"] is None:
                        self.stats["first_transcript_t"] = time.time()
                        logger.info(f"[{self.conn_id}] First transcript received")
                    # Convert the pipeline's monotonic speech span to
                    # recording-relative seconds (t=0 ≈ first pushed frame)
                    # so the transcript carries exact caller-audio offsets.
                    span = None
                    sst = event.get("speech_start_mono") or 0.0
                    sen = event.get("speech_end_mono") or 0.0
                    fa = self.stats.get("first_audio_t")
                    if sst > 0.0 and sen > sst and fa:
                        _nm, _nw = time.monotonic(), time.time()
                        span = (max(0.0, _nw - (_nm - sst) - fa),
                                max(0.0, _nw - (_nm - sen) - fa))
                    audit_manager.add_event(self.conn_id, "Caller",
                                            event.get("text", ""),
                                            span=span)
                    self.transcript_tail.append({"speaker": "Caller", "text": event.get("text", "")})
                elif etype == "agent":
                    audit_manager.add_event(self.conn_id, "Agent", event.get("text", ""))
                    self.transcript_tail.append({"speaker": "Agent", "text": event.get("text", "")})
                elif etype == "agent_retract":
                    # The pipeline cut this reply before a frame of it reached
                    # the wire (continuation merge). It is not part of the
                    # call, and leaving it on the transcript is what makes a
                    # clean call read back as the agent repeating itself.
                    _text = event.get("text", "")
                    audit_manager.drop_last_event(self.conn_id, "Agent", _text)
                    if (self.transcript_tail
                            and self.transcript_tail[-1].get("speaker") == "Agent"
                            and self.transcript_tail[-1].get("text") == _text):
                        self.transcript_tail.pop()
                elif etype == "user_retranscribed":
                    # The pieces of a split turn were written as heard; the
                    # engine acts on the joined re-transcription (RUNBOOK
                    # §78) — the transcript must say what the agent heard.
                    _pieces = [p for p in (event.get("pieces") or []) if p]
                    _text = event.get("text", "")
                    span = None
                    sst = event.get("speech_start_mono") or 0.0
                    sen = event.get("speech_end_mono") or 0.0
                    fa = self.stats.get("first_audio_t")
                    if sst > 0.0 and sen > sst and fa:
                        _nm, _nw = time.monotonic(), time.time()
                        span = (max(0.0, _nw - (_nm - sst) - fa),
                                max(0.0, _nw - (_nm - sen) - fa))
                    if _pieces and _text and audit_manager.replace_last_events(
                            self.conn_id, "Caller", _pieces, _text, span=span):
                        k = len(_pieces)
                        tail = list(self.transcript_tail)[-k:]
                        if (len(tail) == k and all(
                                e.get("speaker") == "Caller" and e.get("text") == p
                                for e, p in zip(tail, _pieces))):
                            for _ in range(k):
                                self.transcript_tail.pop()
                        self.transcript_tail.append({"speaker": "Caller", "text": _text})
        except asyncio.CancelledError:
            pass
        finally:
            self.hub.unsubscribe(queue)
        
    def push_audio(self, pcm: bytes):
        """Called by ws_audio_receiver when a PCM frame arrives."""
        if not self._running:
            return
        if self.stats["first_audio_t"] is None:
            self.stats["first_audio_t"] = time.time()
        self.source.push_frame(pcm)

    async def _playback_loop(self):
        """Polls the TelephonySink for finalized WAV files and injects them.

        Also owns the agent-initiated hangup (platform rule, all agents):
        once the conversation armed hangup_pending (farewell exchange /
        wrong number / DND / close-permission confirm) and the farewell
        audio has drained, ask socket_server to end the channel. The
        timeout-poll below exists for exactly this — a blocking get() would
        never re-check hangup_pending after the final WAV."""
        while self._running:
            try:
                try:
                    wav_path = await asyncio.wait_for(
                        self.sink.wav_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    await self._maybe_agent_hangup()
                    continue

                # Task 3: Playback Enqueue Observability
                duration = 0.0
                size = 0
                try:
                    size = os.path.getsize(wav_path)
                    with wave.open(wav_path, 'rb') as wf:
                        frames = wf.getnframes()
                        rate = wf.getframerate()
                        duration = frames / float(rate)
                except Exception as e:
                    logger.warning(f"[{self.conn_id}] Could not parse WAV info for {wav_path}: {e}")

                logger.info("[%s] [PLAYBACK_ENQUEUE] path=%s duration=%.2fs bytes=%d ts=%.3f",
                            self.conn_id, wav_path, duration, size, time.time())

                # Re-arm the barge-in abort signal for THIS injection.  Cleared
                # here (not after the send) so an interrupt landing while the
                # request is in flight is still caught by the pacing wait.
                abort_event = getattr(self.sink, "abort_event", None)
                if abort_event is not None:
                    abort_event.clear()
                
                if self.stats["first_response_t"] is None:
                    self.stats["first_response_t"] = time.time()

                # Send to socket_server
                success = await self._send_playback_request(wav_path, duration, size)
                
                if success:
                    if self.stats["first_playback_t"] is None:
                        self.stats["first_playback_t"] = time.time()
                    self.stats["playback_count"] += 1
                    logger.info("[%s] [PLAYBACK_COMPLETE] path=%s duration=%.2fs bytes=%d ts=%.3f",
                                self.conn_id, wav_path, duration, size, time.time())
                
                # Pace the injection to prevent overlapping AI speech.  The
                # wait is barge-in interruptible: uuid_break already silenced
                # the channel, so finishing the sleep would only delay the
                # response to whatever the caller barged in with.
                if duration > 0:
                    logger.info(f"[{self.conn_id}] [PLAYBACK_PACE] Sleeping for {duration:.2f}s duration")
                    if abort_event is not None:
                        try:
                            await asyncio.wait_for(
                                abort_event.wait(), timeout=duration + 0.1)
                            logger.info(
                                "[%s] [PLAYBACK_PACE] cut short by barge-in",
                                self.conn_id)
                        except asyncio.TimeoutError:
                            pass
                    else:
                        await asyncio.sleep(duration + 0.1) # slight buffer

                # Reap the WAV once it can no longer be in use: playback ends
                # ~injection-overhead after the pacing sleep, and FreeSWITCH
                # holds its own fd anyway (a Linux unlink never cuts audio
                # mid-play). Nothing else deleted these — 2 calls left 53
                # files in /tmp/ai_telephony_playback, and this VM has
                # already had a disk-full incident.
                asyncio.get_running_loop().call_later(
                    30.0, _reap_played_wav, wav_path)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{self.conn_id}] [PLAYBACK_ERROR] Loop error: {e}")
                
    async def _send_playback_request(self, wav_path: str, duration: float, size: int) -> bool:
        """Connects to socket_server.py to trigger playback."""
        logger.info("[%s] [PLAYBACK_SEND] path=%s duration=%.2fs bytes=%d ts=%.3f",
                    self.conn_id, wav_path, duration, size, time.time())
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(PLAYBACK_HOST, PLAYBACK_PORT),
                timeout=2.0
            )
            req = {"type": "playback_request", "conn_id": self.conn_id, "wav_path": wav_path}
            payload = json.dumps(req).encode("utf-8")
            
            # Send 4-byte big-endian length + JSON payload
            writer.write(struct.pack(">I", len(payload)) + payload)
            await writer.drain()
            
            # Wait for ack
            try:
                ack_hdr = await asyncio.wait_for(reader.readexactly(4), timeout=2.0)
                (length,) = struct.unpack(">I", ack_hdr)
                ack_payload = await asyncio.wait_for(reader.readexactly(length), timeout=2.0)
                ack = json.loads(ack_payload.decode("utf-8"))
                
                if ack.get("ok"):
                    writer.close()
                    await writer.wait_closed()
                    return True
                else:
                    logger.error(f"[{self.conn_id}] [PLAYBACK_RESULT] Failed: {ack.get('reason')}")
            except Exception as e:
                logger.warning(f"[{self.conn_id}] [PLAYBACK_RESULT] Ack failed: {e}")
            
            writer.close()
            await writer.wait_closed()
            # If we sent it, we assume success unless ack says otherwise
            return True
        except Exception as e:
            logger.error(f"[{self.conn_id}] [PLAYBACK_ERROR] Failed to inject playback: {e}")
            return False

    async def _maybe_agent_hangup(self) -> None:
        """End the channel once the conversation's farewell has fully drained.

        Fires at most once, and only when ALL of:
        - the engine armed hangup_pending (caller-consented close),
        - no WAV is queued for injection,
        - the estimated wire playback has finished (sink.playback_busy_until
          + grace) — the farewell line is never cut mid-sentence,
        - hangup_pending has been armed for AGENT_HANGUP_MIN_WAIT_S, so a
          farewell still inside TTS synthesis (queue momentarily empty, no
          flush yet) can't be mistaken for drained audio,
        - the pipeline is quiet — the caller is not speaking and no transcript
          is in flight (bounded by AGENT_HANGUP_MAX_WAIT_S).
        """
        if not AGENT_HANGUP_ENABLED or self._hangup_sent:
            return
        conv = getattr(self.pipeline, "conversation", None)
        if conv is None or not getattr(conv, "hangup_pending", False):
            self._hangup_armed_ts = None  # engine cancelled (caller spoke again)
            return
        now = time.monotonic()
        if getattr(conv, "hangup_immediate", False):
            # RUNBOOK §83: a machine has the line (mailbox, network
            # announcement, a hold nobody came back from). Nothing queued or
            # playing is for anyone — break the audio at FreeSWITCH and end
            # the channel now, without the drain or the quiet check.
            self._hangup_sent = True
            logger.info("[%s] [HANGUP_REQUEST] machine on the line (%s) — "
                        "ending call immediately", self.conn_id,
                        getattr(conv, "call_outcome", "") or "machine")
            await self._send_break_request()
            await self._send_hangup_request()
            return
        if self._hangup_armed_ts is None:
            self._hangup_armed_ts = now
            logger.info("[%s] [HANGUP_ARMED] farewell delivered — waiting for "
                        "audio to drain", self.conn_id)
            return
        if now - self._hangup_armed_ts < AGENT_HANGUP_MIN_WAIT_S:
            return
        if not self.sink.wav_queue.empty():
            return
        busy_until = float(getattr(self.sink, "playback_busy_until", 0.0) or 0.0)
        if now < busy_until + AGENT_HANGUP_GRACE_S:
            return
        # The engine only cancels a pending hangup once a FINALIZED transcript
        # reaches process_stream. A caller who starts speaking during the drain
        # window is still inside VAD/STT at that moment, so the cancel has not
        # happened yet and the channel would drop on top of them — observed
        # live 2026-07-31, where the caller answered "anything else?" with "can
        # you take my email address?" and was cut off 2.6s into it. Waiting for
        # the pipeline to go quiet closes that race. A HARD close (max-duration
        # cap) skips the check by design: caller speech does not defer it.
        if not getattr(conv, "_hangup_hard", False):
            quiet = getattr(self.pipeline, "_pipeline_quiet", None)
            if callable(quiet):
                try:
                    speaking = not quiet(now)
                except Exception:
                    speaking = False
                if speaking:
                    if now - self._hangup_armed_ts < AGENT_HANGUP_MAX_WAIT_S:
                        return
                    logger.warning(
                        "[%s] [HANGUP_FORCED] pipeline never went quiet in "
                        "%.0fs — ending anyway", self.conn_id,
                        AGENT_HANGUP_MAX_WAIT_S)
        self._hangup_sent = True
        logger.info("[%s] [HANGUP_REQUEST] farewell drained — ending call",
                    self.conn_id)
        await self._send_hangup_request()

    async def _send_break_request(self) -> bool:
        """Barge-in: ask socket_server (TCP 9004) to uuid_break the channel
        so audio FreeSWITCH is already playing stops immediately.  Same
        one-shot length-prefixed JSON protocol as playback/hangup."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(PLAYBACK_HOST, PLAYBACK_PORT),
                timeout=2.0
            )
            req = {"type": "break_request", "conn_id": self.conn_id}
            payload = json.dumps(req).encode("utf-8")
            writer.write(struct.pack(">I", len(payload)) + payload)
            await writer.drain()
            try:
                ack_hdr = await asyncio.wait_for(reader.readexactly(4), timeout=2.0)
                (length,) = struct.unpack(">I", ack_hdr)
                ack_payload = await asyncio.wait_for(
                    reader.readexactly(length), timeout=2.0)
                ack = json.loads(ack_payload.decode("utf-8"))
                if not ack.get("ok"):
                    logger.warning("[%s] [BREAK_RESULT] refused: %r",
                                   self.conn_id, ack.get("reason"))
            except Exception as e:
                logger.warning("[%s] [BREAK_RESULT] ack failed: %r",
                               self.conn_id, e)
            writer.close()
            await writer.wait_closed()
            return True
        except Exception as e:
            logger.error("[%s] [BREAK_ERROR] failed to send break request: %r",
                         self.conn_id, e)
            return False

    async def _send_hangup_request(self) -> bool:
        """Ask socket_server (TCP 9004) to queue an event-locked hangup on
        this call. Same one-shot length-prefixed JSON protocol as playback."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(PLAYBACK_HOST, PLAYBACK_PORT),
                timeout=2.0
            )
            req = {"type": "hangup_request", "conn_id": self.conn_id}
            payload = json.dumps(req).encode("utf-8")
            writer.write(struct.pack(">I", len(payload)) + payload)
            await writer.drain()
            try:
                ack_hdr = await asyncio.wait_for(reader.readexactly(4), timeout=2.0)
                (length,) = struct.unpack(">I", ack_hdr)
                ack_payload = await asyncio.wait_for(
                    reader.readexactly(length), timeout=2.0)
                ack = json.loads(ack_payload.decode("utf-8"))
                if not ack.get("ok"):
                    logger.warning("[%s] [HANGUP_RESULT] refused: %r",
                                   self.conn_id, ack.get("reason"))
            except Exception as e:
                logger.warning("[%s] [HANGUP_RESULT] ack failed: %r",
                               self.conn_id, e)
            writer.close()
            await writer.wait_closed()
            return True
        except Exception as e:
            logger.error("[%s] [HANGUP_ERROR] failed to send hangup request: %r",
                         self.conn_id, e)
            return False

    def _print_summary(self):
        """Logs the Phase 14 Session Summary."""
        st = self.stats
        t0 = st["start_time"]
        
        def _delta(t):
            return f"{t - t0:.3f}s" if t else "—"

        logger.info("---------- PHASE-14 SESSION SUMMARY ---")
        logger.info(f"    conn_id               {self.conn_id}")
        logger.info(f"    first_audio           {_delta(st['first_audio_t'])}")
        logger.info(f"    first_transcript      {_delta(st['first_transcript_t'])}")
        logger.info(f"    first_response        {_delta(st['first_response_t'])}")
        logger.info(f"    first_playback        {_delta(st['first_playback_t'])}")
        logger.info(f"    playback_count        {st['playback_count']}")
        logger.info(f"    source_queue_depth    {self.source.stats['queue_size']}")
        logger.info(f"    source_dropped        {self.source.stats['dropped']}")
        logger.info("---------------------------------------")


class SessionManager:
    """Manages all active telephony sessions."""

    # Issue 3 & 4: process-level registry of greeted calls.
    # Prevents duplicate greetings when FreeSWITCH reconnects the WebSocket
    # for the same call (e.g., transient network glitch). TTL of 30 minutes
    # covers long calls where a mid-call reconnect should not replay the greeting.
    _greeted: Dict[str, float] = {}
    _GREETING_TTL_S: float = 1800.0

    def __init__(self):
        self.sessions: Dict[str, TelephonySession] = {}

    async def get_or_create(self, conn_id: str, sample_rate: int, caller_number: str = "",
                            dialed_number: str = "",
                            did: str = "", campaign_id: int | None = None,
                            agent_slug: str = "") -> TelephonySession:
        if conn_id not in self.sessions:
            now = time.time()
            # Prune stale greeted entries to avoid unbounded growth
            expired = [k for k, ts in SessionManager._greeted.items()
                       if now - ts > SessionManager._GREETING_TTL_S]
            for k in expired:
                del SessionManager._greeted[k]

            # Skip greeting if this conn_id was already greeted recently
            skip_greeting = (
                conn_id in SessionManager._greeted
                and now - SessionManager._greeted[conn_id] < SessionManager._GREETING_TTL_S
            )
            if skip_greeting:
                logger.info("[BRIDGE] Reconnect detected for conn_id=%s — greeting suppressed", conn_id)

            session = await TelephonySession.create(
                conn_id, sample_rate, caller_number=caller_number,
                dialed_number=dialed_number,
                skip_greeting=skip_greeting, did=did,
                campaign_id=campaign_id, agent_slug=agent_slug)
            self.sessions[conn_id] = session

            # Mark as greeted on first creation (before start so no race)
            if not skip_greeting:
                SessionManager._greeted[conn_id] = now

            await session.start()
        return self.sessions[conn_id]

    def push_audio(self, conn_id: str, pcm: bytes):
        if conn_id in self.sessions:
            self.sessions[conn_id].push_audio(pcm)

    async def remove(self, conn_id: str):
        if conn_id in self.sessions:
            session = self.sessions.pop(conn_id)
            await session.stop()

# Global registry
call_bridge = SessionManager()
