#!/usr/bin/env python3
"""
LesCall Dashboard Server

Standalone operational dashboard for the voice AI calling platform.
- Read-only observer of logs, credit_log, and recordings
- Runtime controller: start / stop / restart the calling agent via API
- SSE streams for live logs and command output

Port: 8080  (nginx proxies port 80 → 8080)
Runs under systemd: sudo systemctl restart lescall-dashboard
"""
from __future__ import annotations

import asyncio
import csv
import datetime
import io
import json
import logging
import math
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# This process runs with dashboard/ as its working directory, so the repo root
# is not importable by default (same bootstrap as billing_api / telephony_api).
# server.py's other `shared` imports are lazy and get away with riding on
# whichever sibling module inserted the path first; a top-level import cannot.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# The one rate card (shared/sarvam_pricing.py). Costs shown here are recomputed
# from raw usage counters through it, never read back from the estimates the
# tracker froze into credit_log at write time.
from shared import sarvam_pricing as pricing  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LESCALL_ROOT  = Path("/home/angelo/lescall")
CREDIT_LOG_DIR = LESCALL_ROOT / "credit_log"
RECORDINGS_DIR = LESCALL_ROOT / "recordings"
# Pre-generated TTS voice previews (deploy/generate_voice_samples.py).
VOICE_SAMPLES_DIR = LESCALL_ROOT / "data" / "voice_samples"
RUNTIME_DIR   = LESCALL_ROOT / "phase14_runtime"
LOGS_LATEST   = RUNTIME_DIR / "logs" / "latest"
STATIC_DIR    = Path(__file__).parent / "static"
LIVE_STATE_FILE = RUNTIME_DIR / "live_calls_state.json"
SARVAM_CREDITS_FILE = Path(__file__).parent / "sarvam_credits.json"
API_KEY_POOL_FILE = Path(__file__).parent / "api_key_pool.json"
API_KEY_STATE_FILE = Path(__file__).parent / "api_key_state.json"
CALLING_AGENT_ENV_FILE = LESCALL_ROOT / "calling-agent" / ".env"
LOW_CREDIT_THRESHOLD = 15

CALL_LINK_FILE   = Path(__file__).parent / "call_link.json"
FOLLOWUPS_FILE   = Path(__file__).parent / "follow_ups.json"
MANUAL_FOLLOWUPS_FILE = Path(__file__).parent / "manual_followups.json"
MINUTES_QUOTA_FILE = Path(__file__).parent / "minutes_quota.json"
ANALYSIS_STORE_FILE = Path(__file__).parent / "call_analysis.json"
FOLLOWUP_AUTOMATION_FILE = Path(__file__).parent / "follow_up_automation.json"
AUTOMATION_POLL_SECONDS = 30
CONVERSATION_PY  = LESCALL_ROOT / "calling-agent" / "backend" / "app" / "voice" / "conversation.py"

# A dial is considered matched to a subsequent pipeline call if the pipeline
# call started within this many seconds of the dial being placed.
OUTBOUND_MATCH_WINDOW_SECONDS = 90

START_SCRIPT  = RUNTIME_DIR / "start_all.sh"
STOP_SCRIPT   = RUNTIME_DIR / "stop_all.sh"

# A call with no call_end older than this is considered stale, not "active"
ACTIVE_CALL_TIMEOUT_MINUTES = 20

# Outbound dialing config — override via env vars if needed
OUTBOUND_CALLER_ID  = os.environ.get("OUTBOUND_CALLER_ID",  "918065953160")
OUTBOUND_GATEWAY    = os.environ.get("OUTBOUND_GATEWAY",    "25935838218519915.zt.plivo.com")
# Dialplan extension added in /etc/freeswitch/dialplan/default/outbound_ai.xml
OUTBOUND_DP_EXT     = "outbound_ai_connect"
OUTBOUND_DP_CTX     = "default"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("dashboard")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="LesCall Dashboard", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ---------------------------------------------------------------------------
# Request audit: one structured JSON line per API request (who, what, status,
# latency). Rotated locally; shipped to GCS by the log shipper. Static asset
# noise is skipped. This is the "track everything" layer; semantic actions
# additionally land in the audit_events table (shared/audit.py).
# ---------------------------------------------------------------------------
from logging.handlers import RotatingFileHandler  # noqa: E402

_REQ_LOG_DIR = Path(__file__).parent / "logs"
_REQ_LOG_DIR.mkdir(exist_ok=True)
_req_logger = logging.getLogger("lescall.requests")
_req_logger.setLevel(logging.INFO)
_req_logger.propagate = False
_req_handler = RotatingFileHandler(
    _REQ_LOG_DIR / "requests.jsonl", maxBytes=20 * 1024 * 1024, backupCount=10)
_req_handler.setFormatter(logging.Formatter("%(message)s"))
_req_logger.addHandler(_req_handler)


@app.middleware("http")
async def _request_audit(request: Request, call_next):
    if request.url.path.startswith("/static/"):
        return await call_next(request)
    t0 = time.monotonic()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        try:
            import auth as _audit_auth
            session = _audit_auth.read_session_cookie(
                request.cookies.get(_audit_auth.SESSION_COOKIE))
            # An API-key request has no session cookie; public_api stamps the
            # key and owner on request.state so the audit line still says WHO,
            # not just which IP.
            api_key_id = getattr(request.state, "api_key_id", None)
            _req_logger.info(json.dumps({
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(
                    timespec="milliseconds").replace("+00:00", "Z"),
                "ip": request.client.host if request.client else None,
                "owner": ((session or {}).get("email")
                          or getattr(request.state, "api_owner", None)),
                "api_key": api_key_id,
                "method": request.method,
                "path": request.url.path,
                "status": status,
                "ms": round((time.monotonic() - t0) * 1000, 1),
            }, ensure_ascii=False))
        except Exception:
            pass

# Agent Builder control plane (create/edit/route/RAG/validate/activate) — mounted
# on the same app+port the React dashboard already uses. `activate` writes
# agents/active_agent.json, which the telephony bridge reads per call.
try:
    from agent_builder_api import router as agent_builder_router
    app.include_router(agent_builder_router)
    logging.getLogger("dashboard").info("Agent Builder API mounted")
except Exception as _ab_exc:  # pragma: no cover - never take down the dashboard
    logging.getLogger("dashboard").error("Agent Builder API NOT mounted: %r", _ab_exc)

# Agent sharing — a link that lets another account IMPORT an independent copy.
# Separate router from the builder on purpose: the builder's tenancy dependency
# 404s any agent the caller does not own, which is exactly who reads a share
# link. See dashboard/agent_share_api.py.
try:
    from agent_share_api import router as agent_share_router
    app.include_router(agent_share_router)
    logging.getLogger("dashboard").info("Agent Share API mounted")
except Exception as _sh_exc:  # pragma: no cover - never take down the dashboard
    logging.getLogger("dashboard").error("Agent Share API NOT mounted: %r", _sh_exc)

# Agent Copilot — conversational agent builder (ChatGPT-style dashboard homepage).
# Interviews the user, then writes agents through the same Agent Builder plumbing.
try:
    from agent_copilot_api import router as agent_copilot_router
    app.include_router(agent_copilot_router)
    logging.getLogger("dashboard").info("Agent Copilot API mounted")
except Exception as _cp_exc:  # pragma: no cover - never take down the dashboard
    logging.getLogger("dashboard").error("Agent Copilot API NOT mounted: %r", _cp_exc)

# Auth (Google OAuth sessions + current_owner tenant boundary). Runs in
# single-tenant fallback mode until GOOGLE_CLIENT_ID/SECRET are configured —
# see dashboard/auth.py and docs/MULTITENANT_BYON_PLAN.md §1.
try:
    import auth as _auth_module
    app.include_router(_auth_module.router)
    # Login wall: no-op until GOOGLE_CLIENT_ID/SECRET are configured; then
    # every route except /login, /auth/*, /static/* requires a session.
    _auth_module.install_login_gate(app)
    logging.getLogger("dashboard").info("Auth API + login gate mounted")
except SystemExit:
    raise
except Exception as _auth_exc:  # pragma: no cover - never take down the dashboard
    logging.getLogger("dashboard").error("Auth API NOT mounted: %r", _auth_exc)

# Fail-closed boot assertion (P-1). Deliberately OUTSIDE the try/except above:
# "auth failed to mount" must not degrade to "auth is off". If the login wall
# cannot be configured, this process must not serve traffic at all.
import auth as _auth_assert  # noqa: E402
_auth_assert.assert_auth_configured()

import contact_names  # noqa: E402

# Billing / subscriptions (deploy/PLAN-razorpay-subscriptions.md). Mounted with
# an explicit success line: the try/except pattern above is convenient for
# optional surfaces but a billing router that vanishes silently means quotas
# stop being enforceable while everything still looks healthy.
try:
    from billing_api import router as billing_router
    app.include_router(billing_router)
    logging.getLogger("dashboard").info("Billing API mounted")
except Exception as _bill_exc:  # pragma: no cover
    logging.getLogger("dashboard").error(
        "BILLING API NOT MOUNTED (checkout, webhook and receipts are DOWN): %r",
        _bill_exc, exc_info=True)

# BYON telephony control plane (trunks/DIDs -> FreeSWITCH config + Kamailio
# ACL), owner-scoped. See docs/MULTITENANT_BYON_PLAN.md §3b.
try:
    from telephony_api import router as telephony_router
    app.include_router(telephony_router)
    logging.getLogger("dashboard").info("Telephony API mounted")
except Exception as _tel_exc:  # pragma: no cover - never take down the dashboard
    logging.getLogger("dashboard").error("Telephony API NOT mounted: %r", _tel_exc)

# Intelligence layer (automatic post-call pipeline: unified dashboard,
# semantic search, follow-ups, reports, notifications, customer memory).
# See deploy/PLAN-intelligence-layer.md.
try:
    from intel_api import router as intel_router
    app.include_router(intel_router)
    logging.getLogger("dashboard").info("Intelligence API mounted")
except Exception as _intel_exc:  # pragma: no cover - never take down the dashboard
    logging.getLogger("dashboard").error("Intelligence API NOT mounted: %r", _intel_exc)

# Integrations → WhatsApp: playbooks, approvals, the Meta webhook and tracked
# links (dashboard/whatsapp_api.py). The sender loop lives in this process,
# like the call scheduler: the queue is rows in Postgres, so a restart costs
# one poll interval and never a message.
try:
    from whatsapp_api import router as whatsapp_router
    from whatsapp_api import start_sender as _wa_start_sender
    app.include_router(whatsapp_router)

    @app.on_event("startup")
    async def _start_whatsapp_sender() -> None:
        _wa_start_sender()

    logging.getLogger("dashboard").info("WhatsApp integration API mounted")
except Exception as _wa_exc:  # pragma: no cover - never take down the dashboard
    logging.getLogger("dashboard").error("WhatsApp integration API NOT mounted: %r",
                                         _wa_exc, exc_info=True)

# Public integration API (/v1) — API-key authenticated, for customer CRM/ERP
# systems. Mounted via install() rather than include_router because it also
# registers the ApiError handler that turns its documented failures into the
# one error envelope; without that every 403 would surface as an opaque 500.
try:
    from public_api import install as install_public_api
    install_public_api(app)
    logging.getLogger("dashboard").info("Public API (/v1) mounted")
except Exception as _papi_exc:  # pragma: no cover - never take down the dashboard
    logging.getLogger("dashboard").error(
        "PUBLIC API NOT MOUNTED (customer integrations are DOWN): %r",
        _papi_exc, exc_info=True)

# Tenant-facing key management for Settings -> API access.
try:
    from api_keys_api import router as api_keys_router
    app.include_router(api_keys_router)
    logging.getLogger("dashboard").info("API key management mounted")
except Exception as _keys_exc:  # pragma: no cover
    logging.getLogger("dashboard").error("API key management NOT mounted: %r", _keys_exc)

_img_dir = STATIC_DIR / "img"
_img_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ---------------------------------------------------------------------------
# Runtime control state
# ---------------------------------------------------------------------------

class CmdOutputHub:
    """Broadcast command output lines to all connected SSE clients.
    New subscribers receive recent history so they see a full run even if
    they connect mid-way through.
    """
    MAX_HISTORY = 500

    def __init__(self) -> None:
        self._subs: list[asyncio.Queue[dict | None]] = []
        self._history: list[dict] = []
        self.running = False
        self.last_cmd: str = ""
        self.last_exit: int | None = None

    def subscribe(self) -> asyncio.Queue[dict | None]:
        q: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=300)
        for item in self._history[-200:]:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                pass
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subs.remove(q)
        except ValueError:
            pass

    def publish(self, item: dict) -> None:
        self._history.append(item)
        if len(self._history) > self.MAX_HISTORY:
            self._history = self._history[-self.MAX_HISTORY:]
        for q in list(self._subs):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(item)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def clear(self) -> None:
        self._history.clear()


_cmd_hub = CmdOutputHub()
_cmd_lock = asyncio.Lock()


async def _exec_stream(script: Path, label: str) -> None:
    """Run a shell script and broadcast every output line to _cmd_hub.

    stop_all.sh is run via sudo so it can kill root-owned processes
    (services started from a root shell before the dashboard took over).
    Once services are managed by the dashboard they run as angelo and
    sudo is not needed — but it is harmless.
    """
    _cmd_hub.clear()
    _cmd_hub.running = True
    _cmd_hub.last_cmd = label
    _cmd_hub.last_exit = None

    _cmd_hub.publish({"type": "cmd_start", "cmd": label, "ts": _now_iso()})
    try:
        # Use sudo so the script can kill root-owned processes and bind/free
        # ports regardless of whether prior services were started as root.
        cmd = ["sudo", "bash", str(script)]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(LESCALL_ROOT),
        )
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip("\n")
            _cmd_hub.publish({"type": "line", "text": line, "ts": _now_iso()})
        await proc.wait()
        _cmd_hub.last_exit = proc.returncode
        _cmd_hub.publish({
            "type": "cmd_end",
            "exit_code": proc.returncode,
            "ok": proc.returncode == 0,
            "ts": _now_iso(),
        })
    except Exception as exc:
        _cmd_hub.publish({"type": "error", "text": str(exc), "ts": _now_iso()})
        _cmd_hub.last_exit = -1
    finally:
        _cmd_hub.running = False


# ---------------------------------------------------------------------------
# Credit log helpers
# ---------------------------------------------------------------------------

_credit_cache: dict[str, tuple[float, list[dict]]] = {}
_CREDIT_CACHE_TTL = 30.0


def _load_credit_log(date: datetime.date) -> list[dict]:
    key = date.isoformat()
    cached = _credit_cache.get(key)
    if cached and (time.monotonic() - cached[0]) < _CREDIT_CACHE_TTL:
        return cached[1]
    path = CREDIT_LOG_DIR / f"{key}.jsonl"
    events: list[dict] = []
    if path.exists():
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except Exception as exc:
            logger.warning("credit_log read error %s: %s", path, exc)
    _credit_cache[key] = (time.monotonic(), events)
    return events


def _load_credit_range(start: datetime.date, end: datetime.date) -> list[dict]:
    result = []
    d = start
    while d <= end:
        result.extend(_load_credit_log(d))
        d += datetime.timedelta(days=1)
    return result


def _aggregate_calls(events: list[dict]) -> list[dict]:
    starts: dict[str, str] = {}
    calls: list[dict] = []
    for ev in events:
        t = ev.get("type")
        if t == "call_start":
            starts[ev["call_id"]] = ev.get("ts", "")
        elif t == "call_end":
            cid = ev["call_id"]
            start_ts = starts.pop(cid, ev.get("start_time", ""))
            start_ts = start_ts or ev.get("start_time", "")
            end_ts = ev.get("ts", "")
            duration = 0.0
            try:
                if start_ts and end_ts:
                    sdt = datetime.datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
                    edt = datetime.datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
                    duration = max((edt - sdt).total_seconds(), 0.0)
            except Exception:
                pass
            # Cost is RECOMPUTED from the raw usage counters, never read back
            # from the est_* fields the tracker froze into the log at write
            # time. Those are a snapshot of whatever the rate card said that
            # day; repricing here means a rate change (or a billing-rule fix)
            # applies to all history instead of only to calls made after it.
            billed_chars = ev.get("tts_billed_chars")
            if billed_chars is None:
                # Pre-2026-08-06 events: only intended chars were recorded, so
                # barge-in tails are counted as billed. Upper bound, flagged.
                billed_chars = ev.get("tts_chars", 0)
            stt_c = pricing.stt_cost(ev.get("stt_sarvam_billed_seconds", 0.0))
            tts_c = pricing.tts_cost(billed_chars)
            whisper_c = pricing.whisper_cost(ev.get("stt_whisper_seconds", 0.0))
            calls.append({
                "call_id": cid,
                "start_time": start_ts,
                "end_time": end_ts,
                "duration_seconds": round(duration, 1),
                "stt_sarvam_seconds": ev.get("stt_sarvam_seconds", 0.0),
                "stt_sarvam_billed_seconds": ev.get("stt_sarvam_billed_seconds", 0.0),
                "stt_sarvam_calls": ev.get("stt_sarvam_calls", 0),
                "stt_whisper_seconds": ev.get("stt_whisper_seconds", 0.0),
                "stt_whisper_calls": ev.get("stt_whisper_calls", 0),
                "tts_chars": ev.get("tts_chars", 0),
                "tts_billed_chars": billed_chars,
                "tts_calls": ev.get("tts_calls", 0),
                "tts_cancelled_calls": ev.get("tts_cancelled_calls", 0),
                "est_stt_cost": stt_c,
                "est_tts_cost": tts_c,
                "est_whisper_cost": whisper_c,
                "est_total_cost": round(stt_c + tts_c + whisper_c, 6),
            })
    calls.sort(key=lambda c: c["end_time"], reverse=True)
    return calls


def _get_active_calls_from_log(events: list[dict]) -> list[dict]:
    starts: dict[str, str] = {}
    ended: set[str] = set()
    has_activity: set[str] = set()   # call_ids with real STT/TTS events
    for ev in events:
        t = ev.get("type")
        cid = ev.get("call_id")
        if not cid:
            continue
        if t == "call_start":
            starts[cid] = ev.get("ts", "")
        elif t == "call_end":
            ended.add(cid)
        elif t in ("stt_event", "tts_event"):
            has_activity.add(cid)

    now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    timeout = datetime.timedelta(minutes=ACTIVE_CALL_TIMEOUT_MINUTES)
    grace = datetime.timedelta(seconds=30)   # brand-new calls get a grace period
    active = []
    for cid, ts in starts.items():
        if cid in ended:
            continue
        try:
            start_dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age = now - start_dt
        except Exception:
            continue
        # Drop stale orphans older than timeout
        if age > timeout:
            continue
        # Drop quick-crash calls: older than grace period and zero real activity
        if age > grace and cid not in has_activity:
            continue
        active.append({"call_id": cid, "start_time": ts})
    return active


def _orphan_usage(events: list[dict]) -> dict:
    """Usage belonging to calls that never wrote a call_end.

    end_call() is what folds a call's per-event usage into the call_end
    rollup, so a process that dies mid-call (or is restarted under load)
    leaves its stt_event/tts_event lines in the log with no rollup to
    carry them. Summing rollups alone therefore bills less than the API
    calls we actually made — real requests, really invoiced by Sarvam,
    invisible to the dashboard. ~169 calls to date.

    Caveat: a call straddling UTC midnight writes its call_end into the
    next day's file, so a SINGLE-day window can see it as an orphan and
    the next day counts it again in full. Calls are minutes long, so this
    touches almost nothing — and it errs toward counting real spend
    rather than dropping it.
    """
    ended = {ev.get("call_id") for ev in events if ev.get("type") == "call_end"}
    out = {"stt_sarvam_seconds": 0.0, "stt_sarvam_billed_seconds": 0.0,
           "stt_whisper_seconds": 0.0, "tts_chars": 0, "tts_billed_chars": 0}
    heard: set = set()   # orphan call_ids where the caller spoke
    spoke: set = set()   # orphan call_ids where the agent spoke
    for ev in events:
        t = ev.get("type")
        if t not in ("stt_event", "tts_event") or ev.get("call_id") in ended:
            continue
        cid = ev.get("call_id")
        if t == "stt_event":
            heard.add(cid)
            sec = float(ev.get("audio_seconds") or 0.0)
            if ev.get("provider", "sarvam") == "sarvam":
                out["stt_sarvam_seconds"] += sec
                out["stt_sarvam_billed_seconds"] += float(
                    ev.get("billed_seconds") or pricing.stt_billed_seconds(sec))
            else:
                out["stt_whisper_seconds"] += sec
        else:
            spoke.add(cid)
            chars = int(ev.get("chars") or 0)
            out["tts_chars"] += chars
            out["tts_billed_chars"] += int(ev.get("billed_chars", chars))
    out["conversations"] = len(heard)
    out["greeting_only"] = len(spoke - heard)
    return out


def _summarize_range(events: list[dict]) -> dict:
    calls = _aggregate_calls(events)
    orphan = _orphan_usage(events)
    billed_sec = (sum(c["stt_sarvam_billed_seconds"] for c in calls)
                  + orphan["stt_sarvam_billed_seconds"])
    whisper_sec = (sum(c["stt_whisper_seconds"] for c in calls)
                   + orphan["stt_whisper_seconds"])
    chars = sum(c["tts_chars"] for c in calls) + orphan["tts_chars"]
    billed_chars = (sum(c["tts_billed_chars"] for c in calls)
                    + orphan["tts_billed_chars"])
    stt_cost = pricing.stt_cost(billed_sec)
    tts_cost = pricing.tts_cost(billed_chars)
    whisper_cost = pricing.whisper_cost(whisper_sec)

    # What counts as "a call". The tracker opens a record for every inbound
    # WebSocket, so this log counts CONNECTIONS, and most of them are not
    # calls — 93% of all records to date carried no caller audio at all.
    # Reporting connections as calls divided cost-per-call by ~18 and made the
    # platform look far cheaper per conversation than it is.
    #
    #   conversations  — the caller actually spoke. The honest denominator.
    #   greeting_only  — the agent spoke, nobody ever replied. Billable (TTS
    #                    went out) but worthless; a spike here is the
    #                    greeting-before-answer drain re-appearing (June 2026:
    #                    2,956 of these, ₹935).
    #   silent         — no audio either way, always ₹0. Now ~3/day; the 5.6k
    #                    historical bulk is a June 12-15 episode, not steady
    #                    state, so don't read the all-time ratio as current.
    conversations = sum(
        1 for c in calls
        if c["stt_sarvam_calls"] or c["stt_whisper_calls"]
    ) + orphan["conversations"]
    greeting_only = sum(
        1 for c in calls
        if not (c["stt_sarvam_calls"] or c["stt_whisper_calls"]) and c["tts_calls"]
    ) + orphan["greeting_only"]
    connections = len(calls) + orphan["conversations"] + orphan["greeting_only"]

    return {
        # call_count keeps its name (callers depend on it) but now means a
        # real conversation rather than a socket that opened.
        "call_count": conversations,
        "conversation_count": conversations,
        "greeting_only_count": greeting_only,
        "silent_connection_count": max(connections - conversations - greeting_only, 0),
        "connection_count": connections,
        "est_cost_per_conversation": round(
            (stt_cost + tts_cost + whisper_cost) / conversations, 4
        ) if conversations else 0.0,
        "est_stt_cost": round(stt_cost, 4),
        "est_tts_cost": round(tts_cost, 4),
        "est_whisper_cost": round(whisper_cost, 4),
        "est_total_cost": round(stt_cost + tts_cost + whisper_cost, 4),
        "stt_sarvam_billed_seconds": round(billed_sec, 1),
        "stt_sarvam_seconds": round(
            sum(c["stt_sarvam_seconds"] for c in calls)
            + orphan["stt_sarvam_seconds"], 1),
        "stt_whisper_seconds": round(whisper_sec, 1),
        "tts_chars": chars,
        "tts_billed_chars": billed_chars,
        # Text synthesized and then thrown away by barge-in. Real money with
        # nothing to show for it — worth a number, not a footnote.
        "tts_wasted_chars": max(chars - billed_chars, 0),
    }


def _in_range(ts: str, start_date: datetime.date, end_date: datetime.date) -> bool:
    if not ts:
        return False
    try:
        d = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
        return start_date <= d <= end_date
    except Exception:
        return False


def _compute_kpis(call_stats: dict, contacts: list[dict],
                   start_date: datetime.date, end_date: datetime.date) -> dict:
    """KPIs for the Overview tab. Every number here is derived from real
    stored data; where the data genuinely doesn't exist (inbound missed
    calls — the pipeline only ever logs calls that connected), the field is
    explicitly flagged rather than estimated.

    `call_stats` is one owner-scoped aggregate row from _owner_call_stats
    (calls table). It replaced a scan of credit-log events, which carried no
    owner and so made every tenant's Overview show platform-wide totals.
    """
    inbound_calls = int(call_stats.get("inbound") or 0)
    minutes_used = float(call_stats.get("seconds") or 0) / 60.0

    outbound_attempts_in_range = []
    for c in contacts:
        for h in c.get("call_history", []):
            if _in_range(h.get("ts", ""), start_date, end_date):
                outbound_attempts_in_range.append(h)
    outbound_answered = sum(1 for h in outbound_attempts_in_range if h.get("call_status") == "answered")
    outbound_missed = sum(1 for h in outbound_attempts_in_range if h.get("call_status") != "answered")

    appointments_booked = sum(
        1 for c in contacts
        if c.get("disposition") == "site_visit_booked" and _in_range(c.get("last_called_at", ""), start_date, end_date)
    )
    follow_ups_required = sum(
        1 for c in contacts
        if c.get("disposition") in ("to_contact_later", "in_doubt")
        and _in_range(c.get("last_called_at", ""), start_date, end_date)
    )
    follow_ups_required += _count_ai_followups_in_range(start_date, end_date)

    return {
        "total_calls": inbound_calls + len(outbound_attempts_in_range),
        "answered_calls": inbound_calls + outbound_answered,
        "missed_calls": outbound_missed,
        "missed_calls_note": "Outbound only — inbound calls are only logged once connected to the AI pipeline, so a true inbound 'missed call' count isn't available without telephony-side event logging.",
        "inbound_calls": inbound_calls,
        "outbound_calls": len(outbound_attempts_in_range),
        "appointments_booked": appointments_booked,
        "follow_ups_required": follow_ups_required,
        "minutes_used": round(minutes_used, 1),
    }


# ---------------------------------------------------------------------------
# Call direction linking (outbound dial -> pipeline call)
# ---------------------------------------------------------------------------
#
# Outbound dials and inbound/pipeline calls are recorded in two completely
# separate stores (outbound_contacts.json vs credit_log/recordings) with no
# shared call_id — the FreeSWITCH originate command used for outbound calls
# never surfaces the resulting call_id back to the dashboard. To still
# distinguish inbound vs outbound for KPIs/charts, every dial attempt is
# logged here with its timestamp; a pipeline call is attributed to "outbound"
# if it started within OUTBOUND_MATCH_WINDOW_SECONDS of a logged dial. This
# is a timestamp heuristic, not an exact link — callers of
# `_classify_direction` should treat the result as best-effort.

def _load_call_link() -> list[dict]:
    if not CALL_LINK_FILE.exists():
        return []
    try:
        return json.loads(CALL_LINK_FILE.read_text())
    except Exception:
        return []


def _save_call_link(entries: list[dict]) -> None:
    CALL_LINK_FILE.write_text(json.dumps(entries, indent=2))


def _resolve_dial_gender(owner: str, phone: str) -> str:
    """The contact's gender for a dial about to be placed, or "".

    Resolved HERE rather than at each of the five dial origins on purpose.
    Only two of them (manual dial, campaign) hold a contact dict at all; the
    automation, autodial and test-call paths carry a name and a phone and
    nothing else, so plumbing a parameter through would have given the feature
    to some dial buttons and not others — the exact shape of bug the single
    `originate_cmd` builder exists to prevent (RUNBOOK §40).

    One indexed read on `idx_contacts_owner_sig`, on the dial path and not the
    greeting path. Best-effort by design: a contact with no gender, no contact
    row at all, or a DB hiccup all mean "no honorific", which is the normal
    case and never a reason to fail a dial.
    """
    if not owner or not phone:
        return ""
    try:
        from shared.db.database import get_db
        from shared.phone import significant_digits
        sig = significant_digits(phone)
        if not sig:
            return ""
        with get_db() as db:
            row = db.execute(
                "SELECT gender FROM contacts WHERE owner=? AND sig_digits=?",
                (owner, sig)).fetchone()
        return (row["gender"] or "").strip().lower() if row else ""
    except Exception as exc:
        logger.warning("dial gender lookup failed for %s: %r", phone, exc)
        return ""


def _record_dial(phone: str, contact_id: str, name: str, agent_slug: str = "",
                 owner: str = "", context_note: str = "",
                 followup_id: int | None = None,
                 placed_by: str = "user",
                 callback_meta: dict | None = None,
                 gender: str = "") -> None:
    # Snapshot the honorific source alongside the name, from the same contact
    # record, at the same moment — see migration 0027.
    gender = (gender or "").strip().lower() or _resolve_dial_gender(owner, phone)
    entry = {
        "phone": phone, "contact_id": contact_id, "name": name,
        "dialed_at": _now_iso(),
        # Optional: "male"/"female" when this tenant's contact list carries a
        # gender column, "" otherwise. Read back by the telephony bridge to
        # resolve {honorific} in the opening line.
        "gender": gender,
        # Which agent this dial was bound to at dial time — read back by the
        # telephony bridge (resolve_agent_slug dial_meta fallback) when the
        # lescall_agent_slug channel variable didn't survive the leg.
        "agent_slug": agent_slug,
        # Tenant that placed the dial — so call-history direction matching
        # never grafts another tenant's contact name/phone onto a call row.
        "owner": owner,
        # Why we are calling (callback aim / past context) — the bridge feeds
        # it into the WHY-YOU-ARE-CALLING-BACK prompt block.
        "context_note": context_note,
        # Billing transparency: who initiated this dial (user | autodial |
        # automation). Stamped onto calls.placed_by by the bridge so tenants
        # can audit platform-placed spend.
        "placed_by": placed_by,
        # Structured fields for the follow-up call's OPENING line
        # ({reason, topic, requested_when, source_call_at}) — the bridge
        # hands them to ConversationRouter.set_callback_context(meta=...).
        "callback_meta": callback_meta or None,
    }
    link_id = None
    try:
        from shared.db.database import get_db
        from shared.phone import significant_digits
        with get_db() as db:
            row = db.execute(
                "INSERT INTO call_links (owner, phone, sig_digits, contact_id,"
                " name, agent_slug, dialed_at, context_note, followup_id,"
                " placed_by, callback_meta, gender)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
                (owner, phone, significant_digits(phone), contact_id, name,
                 agent_slug, entry["dialed_at"], context_note or None,
                 followup_id, placed_by,
                 json.dumps(callback_meta) if callback_meta else None,
                 gender or None)).fetchone()
            link_id = row["id"] if row else None
            # Dial records only matter inside the matching windows; prune
            # anything older than a week (replaces the file's last-5000 cap).
            cutoff = (datetime.datetime.utcnow()
                      - datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
            db.execute("DELETE FROM call_links WHERE dialed_at < ?", (cutoff,))
    except Exception as exc:
        logger.error("call_links insert failed: %r", exc)
    # Contact timeline: every dial attempt lands as an event (append-only,
    # best-effort — a timeline failure must never block a dial).
    if link_id is not None and owner:
        try:
            from shared.db.database import get_db
            from shared.intelligence import timeline
            from shared.phone import significant_digits
            with get_db() as db:
                cid = timeline.resolve_contact(
                    db, owner, contact_id, significant_digits(phone))
            if cid is not None:
                timeline.emit(cid, owner, "dial.attempted",
                              ref_table="call_links", ref_id=link_id,
                              occurred_at=entry["dialed_at"],
                              payload={"phone": phone, "agent_slug": agent_slug,
                                       "name": name})
        except Exception as exc:
            logger.warning("dial timeline event failed: %r", exc)
    # Legacy dual-write: the runtime bridge read call_link.json until its DB
    # path soaked — remove together with CALL_LINK_FILE once verified.
    entries = _load_call_link()
    entries.append(entry)
    _save_call_link(entries[-5000:])


# How long after a dial a second dial to the same number is treated as a
# double-click rather than a deliberate retry. Matches originate_timeout (45s)
# plus carrier ring setup, so a leg that is still ringing is always covered.
_DUPLICATE_DIAL_WINDOW_S = 60


def _recent_dial_for(phone: str, owner: str, within_s: int) -> float | None:
    """Seconds since this owner last dialled this number, or None if longer
    ago than `within_s`. Compares on significant digits so +91/0 prefix
    variants of the same handset still collide."""
    try:
        from shared.db.database import get_db
        from shared.phone import significant_digits
        sig = significant_digits(phone)
        with get_db() as db:
            row = db.execute(
                "SELECT dialed_at FROM call_links WHERE sig_digits = ? AND owner = ?"
                " ORDER BY dialed_at DESC LIMIT 1", (sig, owner)).fetchone()
        if row is None:
            return None
        # Rows come back dict-like (mapping by column name), not as tuples.
        try:
            raw_val = row["dialed_at"]
        except (TypeError, KeyError, IndexError):
            raw_val = row[0]
        if raw_val is None:
            return None
        # dialed_at comes back as a datetime from Postgres but as a string from
        # the legacy loader, and the string form has varied between
        # '...T..:..:..Z' and '... ..:..:..(.ms)'. Normalise all of them rather
        # than trusting one layout — a parse failure here would silently
        # disable the guard.
        raw = raw_val
        if isinstance(raw, datetime.datetime):
            prev = raw
        else:
            txt = str(raw).strip().replace("T", " ").rstrip("Z")
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    prev = datetime.datetime.strptime(txt, fmt)
                    break
                except ValueError:
                    continue
            else:
                logger.error("duplicate-dial: unparseable dialed_at %r", raw)
                return None
        delta = (datetime.datetime.utcnow() - prev).total_seconds()
        if delta < 0 or delta > within_s:
            return None
        return delta
    except Exception as exc:
        # Never let the guard block a legitimate call if the lookup breaks.
        logger.error("duplicate-dial lookup failed (allowing dial): %r", exc)
        return None


def _classify_direction(start_time_iso: str, owner: str | None = None) -> dict:
    """Best-effort: was this pipeline call likely outbound-originated?

    When `owner` is given (and not an admin), only that tenant's own dial
    records are considered — a near-simultaneous dial by another tenant can
    never bleed its contact name/phone onto this owner's call row.
    """
    none = {"direction": "inbound", "contact_id": None, "name": None, "phone": None}
    if not start_time_iso:
        return none
    try:
        start_dt = datetime.datetime.fromisoformat(start_time_iso.replace("Z", "+00:00"))
    except Exception:
        return none
    import auth
    scoped = bool(owner) and not auth.is_admin(owner)
    best = None
    best_delta = None
    try:
        from shared.db.database import get_db
        window_start = (start_dt.astimezone(datetime.timezone.utc)
                        - datetime.timedelta(seconds=OUTBOUND_MATCH_WINDOW_SECONDS)
                        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        with get_db() as db:
            entries = [dict(r) for r in db.execute(
                "SELECT owner, phone, contact_id, name, dialed_at "
                "FROM call_links WHERE dialed_at >= ?", (window_start,)).fetchall()]
    except Exception as exc:
        logger.error("call_links load failed, using legacy file: %r", exc)
        entries = _load_call_link()
    for entry in entries:
        if scoped and (entry.get("owner") or "") != owner:
            continue
        try:
            dial_dt = datetime.datetime.fromisoformat(
                str(entry["dialed_at"]).replace("Z", "+00:00"))
            if dial_dt.tzinfo is None:  # DB timestamps come back naive-UTC
                dial_dt = dial_dt.replace(tzinfo=datetime.timezone.utc)
        except Exception:
            continue
        delta = (start_dt - dial_dt).total_seconds()
        if 0 <= delta <= OUTBOUND_MATCH_WINDOW_SECONDS:
            if best_delta is None or delta < best_delta:
                best, best_delta = entry, delta
    if best:
        return {"direction": "outbound", "contact_id": best.get("contact_id"),
                "name": best.get("name"), "phone": best.get("phone")}
    return none


# ---------------------------------------------------------------------------
# Settings store — generic per-tenant key/value rows in the DB (owner='' is
# platform-global). Backs minutes quota, Sarvam credits and key state.
# ---------------------------------------------------------------------------

def _setting_get(key: str, default: dict, owner: str = "") -> dict:
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE owner=? AND key=?",
                (owner, key)).fetchone()
        return json.loads(row["value"]) if row else default
    except Exception as exc:
        logger.error("setting %s load failed: %r", key, exc)
        return default


def _setting_set(key: str, value: dict, owner: str = "") -> None:
    from shared.db.database import get_db
    with get_db() as db:
        db.execute(
            "INSERT INTO settings (owner, key, value) VALUES (?,?,?) "
            "ON CONFLICT(owner, key) DO UPDATE SET value=excluded.value, "
            "updated_at=CURRENT_TIMESTAMP",
            (owner, key, json.dumps(value)))


# ---------------------------------------------------------------------------
# Minutes quota (user-reported — same pattern as Sarvam credits: no provider
# quota API exists, so the total is a manually-synced number, not invented)
# ---------------------------------------------------------------------------

def _load_minutes_quota() -> dict:
    return _setting_get("minutes_quota", {"total_minutes": None, "updated_at": None})


def _save_minutes_quota(data: dict) -> None:
    _setting_set("minutes_quota", data)


# ---------------------------------------------------------------------------
# Recordings helpers
# ---------------------------------------------------------------------------

_recordings_cache: tuple[float, list[dict]] | None = None
_RECORDINGS_CACHE_TTL = 15.0  # short TTL so new calls appear quickly


def _scan_recordings(limit: int = 500) -> list[dict]:
    global _recordings_cache
    now = time.monotonic()
    if _recordings_cache and (now - _recordings_cache[0]) < _RECORDINGS_CACHE_TTL:
        return _recordings_cache[1]
    entries: list[str] = []
    try:
        with os.scandir(RECORDINGS_DIR) as it:
            for entry in it:
                if entry.is_dir():
                    entries.append(entry.name)
    except Exception:
        return []

    def _sort_key(name: str) -> str:
        m = re.match(r"^.+_(\d{8})_(\d{6})$", name)
        return (m.group(1) + m.group(2)) if m else "0"

    entries.sort(key=_sort_key, reverse=True)
    entries = entries[:limit]
    records = []
    for name in entries:
        meta_path = RECORDINGS_DIR / name / "metadata.json"
        rec: dict[str, Any] = {"folder": name, "call_id": None,
                                "caller": name.split("_")[0]}
        m = re.match(r"^(.+)_(\d{8})_(\d{6})$", name)
        if m:
            rec["caller"] = m.group(1)
            try:
                dt = datetime.datetime.strptime(m.group(2) + m.group(3),
                                                "%Y%m%d%H%M%S")
                rec["folder_dt"] = dt.isoformat()
            except ValueError:
                rec["folder_dt"] = ""
        if meta_path.exists():
            try:
                with meta_path.open() as f:
                    rec.update(json.load(f))
            except Exception:
                pass
        records.append(rec)
    _recordings_cache = (now, records)
    return records


# The phone-book resolver moved to dashboard/contact_names.py so intel_api can
# share it (it cannot import server.py — server.py mounts its router).
_contact_name_map = contact_names.contact_name_map
_name_for_phone = contact_names.name_for_phone


def _is_recording_complete(folder: str) -> bool:
    """metadata.json is written exactly once, in CallAuditSession.close() —
    its presence is the signal a call is fully wrapped up and safe to read."""
    return (RECORDINGS_DIR / folder / "metadata.json").exists()


# --- per-tenant history scoping (Part B Phase 3) ---------------------------
# A recording folder's name IS the call_key. The bridge records call_key+owner
# on the `calls` table, so folder → owner is a DB lookup. Folders with no DB
# row are pre-tenancy calls owned by the founding admin.
_call_owner_cache: tuple[float, dict] | None = None
_CALL_OWNER_TTL = 5.0


def _call_owner_map() -> dict:
    """(call_key OR conn_id) → owner for every tagged call. Short-cached.

    Keyed by BOTH so every consumer resolves regardless of which id it holds:
    recordings/analysis use the folder name (= call_key); the Call History feed
    keys on the credit-log call_id (= the conn_id/UUID)."""
    global _call_owner_cache
    now = time.monotonic()
    if _call_owner_cache and (now - _call_owner_cache[0]) < _CALL_OWNER_TTL:
        return _call_owner_cache[1]
    mapping: dict = {}
    try:
        from shared.db.database import get_db
        with get_db() as db:
            for r in db.execute(
                    "SELECT call_key, conn_id, owner FROM calls "
                    "WHERE owner IS NOT NULL").fetchall():
                if r["call_key"]:
                    mapping[r["call_key"]] = r["owner"]
                if r["conn_id"]:
                    mapping[r["conn_id"]] = r["owner"]
    except Exception as exc:
        logger.warning("call owner map failed: %r", exc)
    _call_owner_cache = (now, mapping)
    return mapping


_call_media_cache: tuple[float, dict] | None = None


def _call_media_map() -> dict:
    """call_key → {download_url, gcs_uploaded} for the recordings API.
    Short-cached like the owner map; tokens are minted at call end (bridge)
    or by the recording-sync backfill, never here."""
    global _call_media_cache
    now = time.monotonic()
    if _call_media_cache and (now - _call_media_cache[0]) < _CALL_OWNER_TTL:
        return _call_media_cache[1]
    mapping: dict = {}
    try:
        from shared.db.database import get_db
        with get_db() as db:
            for r in db.execute(
                    "SELECT call_key, recording_url, gcs_uploaded_at "
                    "FROM calls WHERE call_key IS NOT NULL "
                    "AND recording_url IS NOT NULL").fetchall():
                mapping[r["call_key"]] = {
                    "download_url": r["recording_url"],
                    "gcs_uploaded": bool(r["gcs_uploaded_at"]),
                }
    except Exception as exc:
        logger.warning("call media map failed: %r", exc)
    _call_media_cache = (now, mapping)
    return mapping


def _folder_owner(folder: str, owner_map: dict | None = None) -> str:
    """Owner of a recording folder; unknown/legacy folders belong to admin."""
    import auth
    m = owner_map if owner_map is not None else _call_owner_map()
    return m.get(folder) or auth.DEFAULT_OWNER


def _can_see_folder(folder: str, owner: str) -> bool:
    import auth
    if auth.is_admin(owner):
        return True
    return _folder_owner(folder) == owner


def _filter_history(rows: list[dict], owner: str, key: str = "folder") -> list[dict]:
    """Keep only rows whose folder/call_key belongs to `owner` (admins keep
    everything). `key` is the dict field holding the folder/call_key."""
    import auth
    if auth.is_admin(owner):
        return rows
    m = _call_owner_map()
    return [r for r in rows if _folder_owner(r.get(key) or "", m) == owner]


def _load_analysis_store() -> dict[str, dict]:
    """folder -> analysis dict, from calls.analysis (rows keyed by call_key —
    the recording folder name). Owner scoping happens downstream via
    folder-owner filtering, same as before the DB move."""
    try:
        from shared.db.database import get_db
        with get_db() as db:
            rows = db.execute(
                "SELECT call_key, analysis FROM calls "
                "WHERE call_key IS NOT NULL AND analysis IS NOT NULL").fetchall()
        return {r["call_key"]: json.loads(r["analysis"]) for r in rows}
    except Exception as exc:
        logger.error("analysis store load failed: %r", exc)
        return {}


def _store_analysis(folder: str, analysis: dict, owner: str) -> None:
    """Upsert one call's analysis. Row-level on purpose: the calls table is
    shared with the live bridge writer, so re-analyzing one call must never
    rewrite every row."""
    from shared.db.database import get_db
    from shared.phone import significant_digits
    phone = folder.split("_", 1)[0]
    started_at = None
    m = re.match(r"^.+_(\d{8})_(\d{6})$", folder)
    if m:
        try:
            started_at = datetime.datetime.strptime(
                m.group(1) + m.group(2), "%Y%m%d%H%M%S"
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
    # Follow-up request extract, denormalized from the analysis blob so the
    # calls table alone answers "does this call need a follow-up".
    follow_up = None
    if analysis.get("follow_up_needed"):
        follow_up = json.dumps({
            "needed": True,
            "reason": analysis.get("follow_up_reason"),
            "priority": analysis.get("follow_up_priority"),
            "date": analysis.get("recommended_follow_up_date"),
        }, ensure_ascii=False)
    with get_db() as db:
        db.execute(
            "INSERT INTO calls (call_key, direction, phone, sig_digits,"
            " started_at, summary, analysis, owner, follow_up)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(call_key) DO UPDATE SET"
            " analysis=excluded.analysis, summary=excluded.summary,"
            " follow_up=excluded.follow_up",
            (folder, "unknown", phone, significant_digits(phone), started_at,
             analysis.get("summary"), json.dumps(analysis, ensure_ascii=False),
             owner, follow_up))


def _scan_analyses() -> dict[str, dict]:
    return _load_analysis_store()


def _count_ai_followups_in_range(start_date: datetime.date, end_date: datetime.date) -> int:
    count = 0
    for folder, analysis in _scan_analyses().items():
        if not analysis.get("follow_up_needed"):
            continue
        m = re.match(r"^.+_(\d{8})_(\d{6})$", folder)
        if not m:
            continue
        try:
            dt = datetime.datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        if start_date <= dt <= end_date:
            count += 1
    return count


# A transcript block opens with an elapsed stamp, optionally followed by
# annotations call_audit.py appends in brackets — today "[spoke 7.26-9.98s]",
# the live-VAD span it writes for CALLER turns only (commit bacbd82, 2026-08-04).
# The old pattern anchored "$" straight after the stamp, so every annotated
# header stopped being a header: from 08-04 on, 76-99% of caller turns per call
# were parsed as a speaker literally named "[00" and vanished from the
# transcript view and the AI analysis. Trailing groups stay optional and
# repeatable so the next annotation added upstream cannot re-break this.
_TS_HEADER_RE = re.compile(
    r"^\[(\d+:\d+\.\d+)\]\s*(?:\[spoke\s+([\d.]+-[\d.]+)s\])?(?:\s*\[[^\]]*\])*$")
# Speaker labels are short bare words ("Agent", "Caller", "Unknown"), never
# bracketed and never numeric.
_SPEAKER_RE = re.compile(r"^\s*[A-Za-z][A-Za-z0-9 _-]{0,30}\s*$")


def _read_transcript(folder: str) -> list[dict]:
    path = RECORDINGS_DIR / folder / "transcript.txt"
    if not path.exists():
        return []
    messages = []
    current_ts = ""
    try:
        with path.open(encoding="utf-8") as f:
            content = f.read()
        for block in re.split(r"\n\n+", content.strip()):
            block = block.strip()
            if not block:
                continue
            lines = block.split("\n")
            ts_match = _TS_HEADER_RE.match(lines[0].strip())
            if ts_match:
                current_ts = ts_match.group(1)
                span = ts_match.group(2) or ""
                text_line = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
            else:
                span = ""
                text_line = block
            if not text_line:
                continue
            idx = text_line.find(":")
            # Only a real speaker token may claim the prefix. Without this a
            # header the regex failed to match falls through with the whole
            # block as text, and find(":") splits inside "[00:10.545]" — the
            # turn is then filed under a speaker named "[00" and no longer
            # reads as the caller anywhere downstream.
            if idx > 0 and _SPEAKER_RE.match(text_line[:idx]):
                speaker = text_line[:idx].strip()
                text = text_line[idx + 1:].strip()
            else:
                speaker = "Unknown"
                text = text_line
            messages.append({"speaker": speaker, "text": text,
                              "timestamp": current_ts, "span": span})
    except Exception as exc:
        logger.warning("transcript read error %s: %s", folder, exc)
    return messages


# ---------------------------------------------------------------------------
# Health / service status helpers
# ---------------------------------------------------------------------------

# Runtime services and the port each one owns. Liveness comes from the port,
# not a PID: the runtime runs in its own container, so its PIDs are namespaced
# (10, 11) and mean nothing here — /proc/<pid> from the API container would hit
# an unrelated process or, worse, report a dead runtime as alive.
RUNTIME_SERVICE_PORTS = {
    "socket_server": 9000,      # FreeSWITCH ESL control
    "ws_audio_receiver": 9001,  # media WebSocket + AI bridge
}


def _check_port(port: int, host: str = "127.0.0.1") -> bool:
    """TCP-connect probe.

    Must not shell out to `ss`: that binary is absent from the app image, so
    the old implementation raised FileNotFoundError, swallowed it, and reported
    every port down — pinning the dashboard to "degraded" after the 2026-07-14
    container cutover. Every service shares the host network namespace, so a
    loopback connect probes them accurately from inside any container.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            return sock.connect_ex((host, port)) == 0
    except OSError:
        return False


def _read_pid_file(name: str) -> str | None:
    """Best-effort PID, for display only — never for liveness.

    Only present under the legacy start_all.sh path, which writes PID files
    beside the logs. The container entrypoint keeps them inside the runtime
    container, so this returns None there and the UI shows a dash.
    """
    try:
        f = LOGS_LATEST / f"{name}.pid"
        return f.read_text().strip() if f.is_file() else None
    except OSError:
        return None


def _get_runtime_health() -> dict:
    run_dir = LOGS_LATEST
    ports = {str(p): _check_port(p) for p in RUNTIME_SERVICE_PORTS.values()}

    services: dict[str, dict] = {}
    for name, port in RUNTIME_SERVICE_PORTS.items():
        entry: dict[str, Any] = {"running": ports[str(port)], "port": port}
        pid = _read_pid_file(name)
        if pid:
            entry["pid"] = pid
        services[name] = entry

    all_up = bool(services) and all(s["running"] for s in services.values())
    return {
        "services": services,
        "ports": ports,
        "overall": "healthy" if all_up else "degraded",
        "checked_at": _now_iso(),
        "logs_dir": str(run_dir) if run_dir.exists() else None,
        "cmd_running": _cmd_hub.running,
        "cmd_last": _cmd_hub.last_cmd,
        "cmd_exit": _cmd_hub.last_exit,
    }


# ---------------------------------------------------------------------------
# Log streaming helpers
# ---------------------------------------------------------------------------

_LOG_PATTERNS = {
    "error":   re.compile(r"ERROR|CRITICAL|Failed|failed|Error|Exception|Traceback",
                          re.IGNORECASE),
    "stt":     re.compile(r"\[STT|transcript", re.IGNORECASE),
    "ai":      re.compile(
        r"\[LLM\]|\[TTS\]|\[ENGINE\]|\[CONVERSATION\]|\[PIPELINE\].*Agent|\[PIPELINE\].*greeting",
        re.IGNORECASE),
    "credits": re.compile(r"\[CREDITS\]"),
    "call":    re.compile(r"\[CALL_BRIDGE\]|\[AUDIT\]|\[SESSION"),
}
_LOG_COLORS = {
    "error": "#f85149", "stt": "#a5a0f8", "ai": "#3fb950",
    "credits": "#d29922", "call": "#38bdf8", "system": "#7d8590",
}


def _categorize_line(line: str) -> tuple[str, str]:
    for cat, pat in _LOG_PATTERNS.items():
        if pat.search(line):
            return cat, _LOG_COLORS[cat]
    return "system", _LOG_COLORS["system"]


async def _tail_file(path: Path, tail_lines: int = 100) -> AsyncIterator[str]:
    if not path.exists():
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "tail", "-n", str(tail_lines), str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        for line in stdout.decode(errors="replace").splitlines():
            yield line
    except Exception:
        pass
    try:
        proc = await asyncio.create_subprocess_exec(
            "tail", "-F", "-n", "0", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        while True:
            line_bytes = await asyncio.wait_for(proc.stdout.readline(), timeout=30.0)
            if not line_bytes:
                break
            yield line_bytes.decode(errors="replace").rstrip("\n")
    except asyncio.TimeoutError:
        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


async def _log_event_stream(categories: set[str]) -> AsyncIterator[str]:
    log_files = [LOGS_LATEST / "ws_audio_receiver.log",
                 LOGS_LATEST / "socket_server.log"]
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=500)

    async def feed(path: Path) -> None:
        async for line in _tail_file(path):
            try:
                queue.put_nowait(line)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(line)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    tasks = [asyncio.create_task(feed(p)) for p in log_files]
    try:
        while True:
            try:
                line = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield 'data: {"type":"keepalive"}\n\n'
                continue
            if not line or not line.strip():
                continue
            cat, color = _categorize_line(line)
            if categories and cat not in categories and "all" not in categories:
                continue
            yield f"data: {json.dumps({'type':'log','category':cat,'color':color,'line':line,'ts':_now_iso()})}\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat(timespec="milliseconds") + "Z"


# ---------------------------------------------------------------------------
# Routes — static
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    # The dashboard UI is the React app (frontend/, served by nginx); this
    # process is API-only.
    return HTMLResponse("<h1>LesCall API</h1><p>The dashboard lives at "
                        "<a href='https://agent.lescall.com'>agent.lescall.com</a>.</p>")


# ---------------------------------------------------------------------------
# Routes — runtime control
# ---------------------------------------------------------------------------

@app.get("/api/runtime/status")
async def runtime_status() -> JSONResponse:
    return JSONResponse(_get_runtime_health())


@app.post("/api/runtime/start")
async def runtime_start() -> JSONResponse:
    if _cmd_hub.running:
        return JSONResponse({"ok": False, "reason": "A command is already running"}, status_code=409)
    if not START_SCRIPT.exists():
        return JSONResponse({"ok": False, "reason": f"start script not found: {START_SCRIPT}"}, status_code=500)
    async with _cmd_lock:
        if _cmd_hub.running:
            return JSONResponse({"ok": False, "reason": "already running"}, status_code=409)
        asyncio.create_task(_exec_stream(START_SCRIPT, "start"))
    return JSONResponse({"ok": True, "msg": "Starting calling agent…"})


@app.post("/api/runtime/stop")
async def runtime_stop() -> JSONResponse:
    if _cmd_hub.running:
        return JSONResponse({"ok": False, "reason": "A command is already running"}, status_code=409)
    if not STOP_SCRIPT.exists():
        return JSONResponse({"ok": False, "reason": f"stop script not found: {STOP_SCRIPT}"}, status_code=500)
    async with _cmd_lock:
        if _cmd_hub.running:
            return JSONResponse({"ok": False, "reason": "already running"}, status_code=409)
        asyncio.create_task(_exec_stream(STOP_SCRIPT, "stop"))
    return JSONResponse({"ok": True, "msg": "Stopping calling agent…"})


@app.post("/api/runtime/restart")
async def runtime_restart() -> JSONResponse:
    if _cmd_hub.running:
        return JSONResponse({"ok": False, "reason": "A command is already running"}, status_code=409)

    async def _do_restart() -> None:
        await _exec_stream(STOP_SCRIPT, "stop")
        await asyncio.sleep(1.5)
        await _exec_stream(START_SCRIPT, "start")

    asyncio.create_task(_do_restart())
    return JSONResponse({"ok": True, "msg": "Restarting calling agent…"})


@app.get("/api/runtime/output/stream")
async def runtime_output_stream(request: Request) -> StreamingResponse:
    async def generator() -> AsyncIterator[str]:
        q = _cmd_hub.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield f"data: {json.dumps(item)}\n\n"
                except asyncio.TimeoutError:
                    yield 'data: {"type":"keepalive"}\n\n'
        except asyncio.CancelledError:
            pass
        finally:
            _cmd_hub.unsubscribe(q)

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Routes — data APIs
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health() -> JSONResponse:
    payload = _get_runtime_health()
    # Billing posture, for the sentinel. Two things worth alarming on:
    #   * `enforcement` disagreeing between the api and runtime containers —
    #     they read the same DB setting, so a mismatch means one of them is
    #     wedged on a stale 30s cache or lost its DB.
    #   * fail_open — how many owners are currently being admitted WITHOUT a
    #     verifiable entitlement because the billing DB is erroring. Nonzero
    #     for more than a moment means calls are going out unmetered.
    try:
        from shared.billing.entitlements import enforcement_enabled, fail_open_state
        payload["billing"] = {"enforcement": enforcement_enabled(),
                              "fail_open": fail_open_state()}
    except Exception as exc:
        payload["billing"] = {"error": repr(exc)[:120]}
    return JSONResponse(payload)


def _owner_call_stats(owner: str, start: datetime.date,
                      end: datetime.date) -> dict[str, Any]:
    """Per-tenant call counts straight from the `calls` table.

    The Overview used to build every one of its numbers from the credit-log
    JSONL, which has no owner attribution — so the page could only ever show
    PLATFORM-WIDE totals. That is why /api/overview is not in the tenancy
    allowlist and the page 403'd: the gate was right to block it. The calls
    table carries `owner` and a real `direction` column, so the same figures
    come out correctly scoped (and without the old time-of-day direction
    guess in _classify_direction).
    """
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute(
                """SELECT count(*) AS total,
                          count(*) FILTER (WHERE direction='inbound')  AS inbound,
                          count(*) FILTER (WHERE direction='outbound') AS outbound,
                          COALESCE(sum(duration_s), 0) AS seconds,
                          count(*) FILTER (WHERE COALESCE(duration_s,0) >= 5) AS answered
                     FROM calls
                    WHERE owner = ? AND started_at::date BETWEEN ? AND ?""",
                (owner, start.isoformat(), end.isoformat()),
            ).fetchone()
            daily = {r["d"]: r["n"] for r in db.execute(
                """SELECT started_at::date::text AS d, count(*) AS n
                     FROM calls
                    WHERE owner = ? AND started_at::date BETWEEN ? AND ?
                    GROUP BY 1""",
                (owner, (end - datetime.timedelta(days=13)).isoformat(), end.isoformat()),
            ).fetchall()}
            hourly_rows = {r["h"]: r["n"] for r in db.execute(
                """SELECT extract(hour from started_at)::int AS h, count(*) AS n
                     FROM calls
                    WHERE owner = ? AND started_at::date = ?
                    GROUP BY 1""",
                (owner, end.isoformat()),
            ).fetchall()}
        return {"row": dict(row), "daily": daily, "hourly": hourly_rows}
    except Exception as exc:
        logger.error("overview call stats failed: %r", exc)
        return {"row": {"total": 0, "inbound": 0, "outbound": 0,
                        "seconds": 0, "answered": 0},
                "daily": {}, "hourly": {}}


@app.get("/api/overview")
async def overview(request: Request, period: str = Query("today")) -> JSONResponse:
    today = datetime.date.today()
    week_start  = today - datetime.timedelta(days=6)
    month_start = today - datetime.timedelta(days=29)
    _ov_owner = _acting_owner(request)

    period_starts = {"today": today, "week": week_start, "month": month_start}
    kpi_start = period_starts.get(period, today)
    contacts = _load_outbound(_ov_owner)

    stats = _owner_call_stats(_ov_owner, kpi_start, today)
    srow = stats["row"]
    hourly = {h: stats["hourly"].get(h, 0) for h in range(24)}

    # Calls Over Time — this tenant's daily volume, last 14 days.
    daily_calls = [
        {"date": (today - datetime.timedelta(days=i)).isoformat(),
         "count": stats["daily"].get((today - datetime.timedelta(days=i)).isoformat(), 0)}
        for i in range(13, -1, -1)
    ]

    # Call Type / Status Distribution for the selected period
    kpis = _compute_kpis(srow, contacts, kpi_start, today)
    call_type_distribution = {"inbound": kpis["inbound_calls"], "outbound": kpis["outbound_calls"]}
    call_status_distribution = {
        "answered": kpis["answered_calls"], "missed": kpis["missed_calls"], "failed": 0,
    }

    # Minutes quota is now PER TENANT, from the entitlements row. The legacy
    # settings(owner='') value was a single platform-global number shown
    # identically to every tenant — meaningless once accounts have their own
    # plans, and it disagreed with what the dial gate would actually enforce.
    total_minutes = remaining_minutes = None
    plan_code = None
    try:
        from shared.billing.entitlements import usage_summary
        _ent = usage_summary(_ov_owner)
        total_minutes = _ent.get("minutes_limit")
        remaining_minutes = _ent.get("minutes_remaining")
        plan_code = _ent.get("plan_code")
    except Exception as exc:
        logger.warning("overview entitlement view unavailable: %r", exc)
        quota = _load_minutes_quota()
        total_minutes = quota.get("total_minutes")
        remaining_minutes = (round(total_minutes - kpis["minutes_used"], 1)
                             if total_minutes is not None else None)

    # Call counts per window, owner-scoped. The old shape carried credit-log
    # cost estimates here too (est_stt_cost / tts_chars / …); those are
    # platform-wide, unattributable to a tenant, and nothing in the UI read
    # them — billing lives in /api/usage/minutes off the usage_ledger.
    def _window(start: datetime.date) -> dict:
        return {"call_count": _owner_call_stats(_ov_owner, start, today)["row"]["total"]}

    return JSONResponse({
        "today":        _window(today),
        "week":         _window(week_start),
        "month":        _window(month_start),
        # Already owner-scoped via _scope_live_state.
        "active_calls": len(_scope_live_state(_read_live_state(), _ov_owner)["calls"]),
        "runtime_status": _get_runtime_health()["overall"],
        "hourly_calls": [{"hour": h, "count": hourly[h]} for h in range(24)],
        "kpis": kpis,
        "minutes_quota": {"total_minutes": total_minutes,
                          "remaining_minutes": remaining_minutes,
                          "plan_code": plan_code},
        "charts": {
            "calls_over_time": daily_calls,
            "call_type_distribution": call_type_distribution,
            "call_status_distribution": call_status_distribution,
        },
    })


# RETIRED (billing, 2026-07-31). This was a single platform-global number in
# settings(owner=''), shown identically to every tenant and enforced nowhere.
# Minute allowances are now per-tenant, come from the `entitlements` row, and
# are set by the plan (or by the super-admin plane for Enterprise). Kept as
# 410s rather than deleted so a stale cached SPA gets a clear answer instead
# of a 404 that looks like a routing bug.
@app.get("/api/minutes/quota")
async def minutes_quota_get(request: Request) -> JSONResponse:
    try:
        from shared.billing.entitlements import usage_summary
        ent = usage_summary(_acting_owner(request))
        return JSONResponse({"total_minutes": ent.get("minutes_limit"),
                             "remaining_minutes": ent.get("minutes_remaining"),
                             "plan_code": ent.get("plan_code"),
                             "source": "entitlements"})
    except Exception:
        return JSONResponse(_load_minutes_quota())


@app.post("/api/minutes/quota")
async def minutes_quota_set(request: Request) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "code": "retired",
         "reason": "Minute allowances come from your plan now. See Billing.",
         "detail": "the platform-global minutes quota has been retired",
         "upgrade": "/billing"}, status_code=410)


async def _minutes_quota_set_legacy(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        total = float(body.get("total_minutes"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "reason": "total_minutes must be a number"}, status_code=400)
    data = {"total_minutes": total, "updated_at": _now_iso()}
    _save_minutes_quota(data)
    return JSONResponse({"ok": True, **data})


def _read_live_state() -> dict:
    if not LIVE_STATE_FILE.exists():
        return {"updated_at": None, "calls": []}
    try:
        with LIVE_STATE_FILE.open() as f:
            return json.load(f)
    except Exception:
        return {"updated_at": None, "calls": []}


def _scope_live_state(state: dict, owner: str) -> dict:
    """Keep only the caller's own live calls (admins see all). A call whose
    snapshot has no owner (legacy/pre-tagging) belongs to the founding admin."""
    import auth
    if auth.is_admin(owner):
        return {"updated_at": state.get("updated_at"),
                "calls": _label_live_calls(list(state.get("calls") or []), owner)}
    calls = [c for c in (state.get("calls") or [])
             if (c.get("owner") or auth.DEFAULT_OWNER) == owner]
    return {"updated_at": state.get("updated_at"),
            "calls": _label_live_calls(calls, owner)}


def _label_live_calls(calls: list, owner: str) -> list:
    """Attach the saved phone-book name to each live call.

    Note: on OUTBOUND legs the snapshot's caller_number is our own origination
    number, not the customer's (the runtime publishes the channel caller id),
    so those simply will not resolve and keep showing the number as before.
    Inbound — where knowing who is on the line actually matters — resolves.
    """
    if not calls:
        return calls
    names = _contact_name_map(owner)
    return [{**c, "contact_name": _name_for_phone(c.get("caller_number", ""), names)}
            for c in calls]


@app.get("/api/calls/active")
async def active_calls(request: Request) -> JSONResponse:
    # Active calls = the owner's own in-flight sessions from the live snapshot
    # (the credit-log source can't be owner-attributed). Powers the live badge.
    state = _scope_live_state(_read_live_state(), _acting_owner(request))
    now = time.time()
    result = []
    for c in state["calls"]:
        dur = 0.0
        st = c.get("start_time")
        try:
            if st:
                s = datetime.datetime.fromisoformat(st.replace("Z", "+00:00"))
                dur = now - s.timestamp()
        except Exception:
            pass
        result.append({"call_id": c.get("conn_id"), "start_time": st,
                        "duration_seconds": round(dur, 0)})
    return JSONResponse({"active_calls": result, "count": len(result)})


@app.get("/api/calls/live-state")
async def calls_live_state(request: Request) -> JSONResponse:
    return JSONResponse(_scope_live_state(_read_live_state(), _acting_owner(request)))


async def _live_state_event_stream(owner: str) -> AsyncIterator[str]:
    last_mtime: float | None = None
    while True:
        try:
            mtime = LIVE_STATE_FILE.stat().st_mtime if LIVE_STATE_FILE.exists() else None
        except Exception:
            mtime = None
        if mtime != last_mtime:
            last_mtime = mtime
            scoped = _scope_live_state(_read_live_state(), owner)
            yield f"data: {json.dumps({'type': 'live_state', **scoped})}\n\n"
        else:
            yield 'data: {"type":"keepalive"}\n\n'
        await asyncio.sleep(1.0)


@app.get("/api/calls/live-state/stream")
async def calls_live_state_stream(request: Request) -> StreamingResponse:
    owner = _acting_owner(request)

    async def generator() -> AsyncIterator[str]:
        async for chunk in _live_state_event_stream(owner):
            if await request.is_disconnected():
                break
            yield chunk

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


_recording_phone_cache: tuple[float, dict[str, str]] | None = None
_RECORDING_PHONE_CACHE_TTL = 120  # seconds


def _build_recording_phone_map() -> dict[str, str]:
    """Scan recording metadata.json files and build {call_id: caller_phone} map.
    Phone is extracted from folder name (segment before first underscore) only
    if it has >= 7 digits — skips extension-style short numbers."""
    global _recording_phone_cache
    now = time.monotonic()
    if _recording_phone_cache and now - _recording_phone_cache[0] < _RECORDING_PHONE_CACHE_TTL:
        return _recording_phone_cache[1]
    result: dict[str, str] = {}
    try:
        for folder_path in RECORDINGS_DIR.iterdir():
            if not folder_path.is_dir():
                continue
            folder = folder_path.name
            phone_candidate = folder.split("_")[0]
            if not phone_candidate.isdigit() or len(phone_candidate) < 7:
                continue
            meta_path = folder_path / "metadata.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
                cid = meta.get("call_id")
                if cid:
                    result[cid] = phone_candidate
            except Exception:
                pass
    except Exception:
        pass
    _recording_phone_cache = (now, result)
    return result


_ZERO_COST_FIELDS = {"stt_sarvam_seconds": 0.0, "stt_sarvam_billed_seconds": 0.0,
                      "stt_sarvam_calls": 0, "stt_whisper_seconds": 0.0, "stt_whisper_calls": 0,
                      "tts_chars": 0, "tts_billed_chars": 0, "tts_calls": 0,
                      "tts_cancelled_calls": 0,
                      "est_stt_cost": 0.0, "est_tts_cost": 0.0,
                      "est_whisper_cost": 0.0, "est_total_cost": 0.0}


def _as_iso(value) -> str:
    """DB timestamps back as either datetime or str depending on driver/table —
    normalise to the ISO-Z string the dashboard renders. Never raises."""
    if not value:
        return ""
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.astimezone(datetime.timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
    s = str(value).strip()
    if not s:
        return ""
    # "2026-08-01 10:15:00" (psycopg text form) -> ISO-Z
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    if not s.endswith("Z") and "+" not in s[10:]:
        s += "Z"
    return s


def _build_call_activity(start_date: datetime.date, end_date: datetime.date,
                         owner: str) -> list[dict]:
    """Unified Call Activity feed for one tenant.

    Sourced from the `calls` table — the authoritative per-call record, which
    carries the REAL direction, phone, contact binding, duration and (since
    answer_state) whether the callee actually spoke. Outbound attempts that
    never connected have no `calls` row at all (the pipeline never ran), so
    those still come from each contact's call_history.

    The credit log is joined in only for the STT/TTS cost columns, keyed by
    conn_id. It used to be the *primary* source, which is why this feed was
    broken: every pipeline call was hardcoded "answered" and its direction was
    guessed by timestamp-matching a dial record, defaulting to "inbound" on any
    miss. Both filters were therefore close to meaningless.
    """
    import auth as _auth
    from shared.db.database import get_db

    # Cost sidecar: call_id here IS the bridge's conn_id.
    costs = {c["call_id"]: c for c in _aggregate_calls(_load_credit_range(start_date, end_date))}

    # Half-open upper bound so the whole of end_date is included regardless of
    # the stored timestamp's time component.
    range_start = start_date.isoformat()
    range_end = (end_date + datetime.timedelta(days=1)).isoformat()

    rows: list[dict] = []
    seen_conn: set[str] = set()
    try:
        with get_db() as db:
            records = [dict(r) for r in db.execute(
                "SELECT c.conn_id, c.direction, c.phone, c.started_at, c.ended_at, "
                "       c.duration_s, c.answer_state, c.contact_id, "
                "       ct.name AS contact_name, ct.phone AS contact_phone "
                "  FROM calls c "
                "  LEFT JOIN contacts ct ON ct.id = c.contact_id "
                " WHERE COALESCE(NULLIF(c.owner, ''), ?) = ? "
                "   AND c.started_at >= ? AND c.started_at < ? "
                " ORDER BY c.started_at DESC",
                (_auth.DEFAULT_OWNER, owner, range_start, range_end)).fetchall()]
    except Exception as exc:
        logger.error("call activity: calls query failed: %r", exc)
        records = []

    for r in records:
        conn_id = r.get("conn_id") or ""
        if conn_id:
            seen_conn.add(conn_id)
        cost = costs.get(conn_id, {})
        # A connected call is 'answered' unless the teardown detector decided
        # nobody actually spoke. NULL (every pre-existing row) means the
        # detector never ran — those stay 'answered' rather than guessing.
        status = "empty_call" if (r.get("answer_state") or "") == "empty" else "answered"
        rows.append({
            "call_id": conn_id or None,
            "start_time": _as_iso(r.get("started_at")),
            "end_time": _as_iso(r.get("ended_at")) or _as_iso(r.get("started_at")),
            "duration_seconds": round(float(r.get("duration_s") or 0.0), 1),
            **{**_ZERO_COST_FIELDS,
               **{k: v for k, v in cost.items()
                  if k in _ZERO_COST_FIELDS}},
            "call_type": (r.get("direction") or "inbound"),
            "contact_id": r.get("contact_id"),
            "contact_name": r.get("contact_name") or "",
            "contact_phone": r.get("contact_phone") or r.get("phone") or "",
            "status": status,
        })

    # Dial attempts that never became a call: no_answer / busy / declined /
    # failed. 'answered' entries are skipped — the `calls` row above is that
    # call, and listing both would double-count it.
    for contact in _load_outbound(owner):
        for h in contact.get("call_history", []):
            if h.get("call_status") == "answered":
                continue
            if not _in_range(h.get("ts", ""), start_date, end_date):
                continue
            rows.append({
                "call_id": None, "end_time": h.get("ts", ""), "start_time": h.get("ts", ""),
                "duration_seconds": 0.0,
                **_ZERO_COST_FIELDS,
                "call_type": "outbound",
                "contact_id": contact.get("db_id"),
                "contact_name": contact.get("name", ""), "contact_phone": contact.get("phone", ""),
                "status": h.get("call_status", "failed"),
            })

    rows.sort(key=lambda r: r.get("end_time") or "", reverse=True)
    return rows


@app.get("/api/calls/history")
async def call_history(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    search: str = Query(""),
    days: int = Query(7, ge=1, le=90),
    call_type: str = Query("all"),
    status: str = Query("all"),
) -> JSONResponse:
    end_date   = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=days - 1)
    rows = _build_call_activity(start_date, end_date, _acting_owner(request))
    if call_type != "all":
        rows = [r for r in rows if r["call_type"] == call_type]
    if status != "all":
        wanted = {"missed": ("no_answer", "busy", "declined")}.get(status, (status,))
        rows = [r for r in rows if r["status"] in wanted]
    if search:
        sl = search.lower()
        rows = [r for r in rows if sl in (r.get("call_id") or "").lower()
                or sl in (r.get("contact_name") or "").lower()
                or sl in (r.get("contact_phone") or "").lower()]
    total = len(rows)
    page_rows = rows[(page - 1) * limit: page * limit]
    return JSONResponse({"calls": page_rows, "total": total, "page": page,
                         "limit": limit, "pages": math.ceil(total / limit) if total else 1})


@app.get("/api/calls/recordings")
async def recordings_list(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    search: str = Query(""),
) -> JSONResponse:
    # E12 — recordings are a paid capability (every paid tier; not Free).
    owner = _require_feature(request, "recordings")
    records = _filter_history(_scan_recordings(limit=2000), owner)
    # Label saved numbers with the name they are saved under, so the list reads
    # as people rather than digits. Done BEFORE search so typing a contact's
    # name finds their recordings too.
    names = _contact_name_map(owner)
    records = [{**r, "contact_name": _name_for_phone(r.get("caller", ""), names)}
               for r in records]
    if search:
        s = search.lower()
        records = [r for r in records
                   if s in r.get("folder", "").lower()
                   or s in (r.get("call_id") or "").lower()
                   or s in r.get("caller", "").lower()
                   or s in (r.get("contact_name") or "").lower()]
    media = _call_media_map()
    records = [{**r, **media.get(r.get("folder", ""), {})} for r in records]
    total = len(records)
    return JSONResponse({
        "recordings": records[(page - 1) * limit: page * limit],
        "total": total, "page": page, "limit": limit,
        "pages": math.ceil(total / limit) if total else 1,
    })


@app.get("/api/calls/recordings/{folder}/transcript")
async def recording_transcript(folder: str, request: Request) -> JSONResponse:
    if "/" in folder or ".." in folder:
        return JSONResponse({"error": "invalid folder"}, status_code=400)
    if not _can_see_folder(folder, _require_feature(request, "transcripts")):
        return JSONResponse({"error": "not found"}, status_code=404)
    meta: dict = {}
    meta_path = RECORDINGS_DIR / folder / "metadata.json"
    if meta_path.exists():
        try:
            with meta_path.open() as f:
                meta = json.load(f)
        except Exception:
            pass
    return JSONResponse({"folder": folder, "metadata": meta,
                         "messages": _read_transcript(folder)})


# ---------------------------------------------------------------------------
# AI Call Analysis — on demand, per selected call, after it has completed.
#
# This reads only transcript.txt (already on disk once a call ends) and
# makes a single fresh LLM call; it never touches the live conversation
# engine, CallState, or any file written by call_audit.py. Results are
# cached to recordings/<folder>/analysis.json so the same call is never
# re-billed unless the user explicitly re-runs it.
# ---------------------------------------------------------------------------

_ANALYSIS_MODEL = os.environ.get("DASHBOARD_ANALYSIS_MODEL", "gpt-4o-mini")
_analysis_client: Any = None


def _get_analysis_client() -> Any:
    global _analysis_client
    if _analysis_client is None:
        from openai import AsyncOpenAI
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key and CALLING_AGENT_ENV_FILE.exists():
            m = re.search(r'^OPENAI_API_KEY=(.*)$', CALLING_AGENT_ENV_FILE.read_text(), re.MULTILINE)
            api_key = m.group(1).strip() if m else ""
        _analysis_client = AsyncOpenAI(api_key=api_key, timeout=30.0)
    return _analysis_client


_ANALYSIS_SYSTEM_PROMPT = """You analyze a phone call transcript between a sales agent and a prospect/customer. Respond ONLY with a JSON object with these exact keys:
{
  "summary": "2-3 sentence plain-language summary of the call",
  "customer_intent": "short phrase describing what the customer wants",
  "key_points": ["short bullet", "short bullet"],
  "customer_questions": ["question the customer literally asked, if any"],
  "suggested_next_action": "what the agent/business should do next",
  "sentiment": "positive" | "neutral" | "negative",
  "lead_quality": "hot" | "warm" | "cold",
  "objections": ["objection raised, if any"],
  "topics": ["topic discussed"],
  "products_mentioned": ["product/project name mentioned, if any"],
  "follow_up_needed": true | false,
  "follow_up_reason": "why a follow-up is/isn't needed",
  "follow_up_priority": "high" | "medium" | "low",
  "recommended_follow_up_date": "YYYY-MM-DD or null"
}
Use null/empty values honestly if the transcript is too short or unclear to judge something — never invent specifics that aren't supported by the transcript."""


def _agent_scoring_context(folder: str) -> str:
    """v2 (plan §4.6): when the call's agent configures post_call.scoring,
    return an agent-context block appended to the analysis system prompt.
    Any other case returns '' — the existing prompt byte-for-byte."""
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT agent_slug, collected_data FROM calls "
                "WHERE call_key=? ORDER BY id DESC LIMIT 1", (folder,)).fetchone()
        if not row or not row["agent_slug"]:
            return ""
        from backend.app.agent_definition.registry import agent_registry
        agent = agent_registry.get(row["agent_slug"])
        scoring = agent.post_call.scoring or {}
        if not scoring.get("enabled"):
            return ""
        lines = ["", "AGENT CONTEXT (score against THIS agent's actual job):",
                 f"- Agent: {agent.identity.agent_name} for "
                 f"{agent.identity.company_name} ({agent.identity.role_description})"]
        if agent.call_flow.call_goal:
            lines.append(f"- Call mission: {agent.call_flow.call_goal}")
        goals = [f"{s.id}: {s.goal}" for s in agent.call_flow.stages if s.goal]
        if goals:
            lines.append("- Stage goals: " + " | ".join(goals)[:1500])
        outcomes = agent.post_call.outcomes or []
        if outcomes:
            lines.append("- Outcome taxonomy: " + "; ".join(
                f"{o.get('id')}({o.get('kind')})" for o in outcomes if isinstance(o, dict)))
        if scoring.get("criteria_prompt"):
            lines.append(f"- Scoring criteria: {scoring['criteria_prompt']}")
        if scoring.get("score_labels"):
            lines.append("- Score labels to use: " + ", ".join(
                str(x) for x in scoring["score_labels"]))
        if row["collected_data"]:
            lines.append(f"- Data collected on the call: {str(row['collected_data'])[:1200]}")
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("agent scoring context failed for %s: %r", folder, exc)
        return ""


async def _analyze_transcript(folder: str) -> dict:
    messages = _read_transcript(folder)
    if not messages:
        raise ValueError("No transcript available for this call")
    transcript_text = "\n".join(f"{m['speaker']}: {m['text']}" for m in messages)
    client = _get_analysis_client()
    _sys_prompt = _ANALYSIS_SYSTEM_PROMPT + _agent_scoring_context(folder)
    response = await client.chat.completions.create(
        model=_ANALYSIS_MODEL,
        messages=[
            {"role": "system", "content": _sys_prompt},
            {"role": "user", "content": f"Transcript:\n{transcript_text}"},
        ],
        response_format={"type": "json_object"},
        max_tokens=600,
        temperature=0.2,
    )
    result = json.loads(response.choices[0].message.content)
    result["_analyzed_at"] = _now_iso()
    result["_model"] = _ANALYSIS_MODEL
    return result


# RETIRED (intelligence layer, deploy/PLAN-intelligence-layer.md): analysis is
# automatic for every call — there is no manual "eligible list + run" workflow.
# 410 keeps stale clients from silently reviving the old flow.
@app.get("/api/analysis/eligible")
async def analysis_eligible(request: Request) -> JSONResponse:
    return JSONResponse(
        {"error": "retired", "detail": "Analysis is automatic now — see /api/intel/calls"},
        status_code=410)


@app.get("/api/calls/recordings/{folder}/analysis")
async def get_analysis(folder: str, request: Request) -> JSONResponse:
    if "/" in folder or ".." in folder:
        return JSONResponse({"error": "invalid folder"}, status_code=400)
    if not _can_see_folder(folder, _acting_owner(request)):
        return JSONResponse({"error": "not found"}, status_code=404)
    store = _load_analysis_store()
    if folder not in store:
        return JSONResponse({"analyzed": False, "analysis": None})
    return JSONResponse({"analyzed": True, "analysis": store[folder]})


@app.post("/api/analysis/run")
async def run_analysis(request: Request) -> JSONResponse:
    """RETIRED: every call is analyzed automatically by the intelligence
    pipeline the moment it ends (shared/intelligence/). Re-analysis, when
    genuinely needed, is POST /api/intel/calls/{call_key}/reanalyze."""
    return JSONResponse(
        {"error": "retired",
         "detail": "Analysis is automatic now. Use /api/intel/calls/"
                   "{call_key}/reanalyze to re-run a specific call."},
        status_code=410)


# ---------------------------------------------------------------------------
# Agent Settings — READ-ONLY introspection of the live agent's configuration.
#
# The agent's identity/prompt/pitch/objection-handling text is hardcoded as
# Python string literals inside conversation.py's _build_system_prompt() —
# there is no separate prompt/pitch/knowledge-base file today. Editing that
# live conversation engine is out of scope here (it drives real calls); this
# section only reads conversation.py as plain text and extracts the static
# literal lines for display, so the dashboard has *something* to show in
# this tab without risking the call pipeline. It never imports or executes
# conversation.py.
# ---------------------------------------------------------------------------

def _extract_quoted_lines_after(text: str, marker: str) -> list[str]:
    """Given the raw source text, find `marker` and return every following
    quoted string literal up to the next blank "" entry or section marker.
    Skips f-string lines (they contain live CallState, not static config)."""
    idx = text.find(marker)
    if idx == -1:
        return []
    rest = text[idx + len(marker):]
    lines = []
    for line in rest.splitlines()[1:]:
        stripped = line.strip()
        if stripped in ('"",', '""'):
            break
        if "━━" in stripped:
            break
        m = re.match(r'^"(.*)",?$', stripped)
        if not m:
            if lines:
                break
            continue
        content = m.group(1)
        if "{" in content:
            continue  # dynamic (f-string) line, not static config
        lines.append(content)
    return lines


def _introspect_agent_config() -> dict:
    if not CONVERSATION_PY.exists():
        return {"available": False, "reason": "conversation.py not found"}
    try:
        text = CONVERSATION_PY.read_text(encoding="utf-8")
    except Exception as exc:
        return {"available": False, "reason": str(exc)}

    identity_lines = _extract_quoted_lines_after(text, '"━━ AGENT IDENTITY ━━",')
    brand_lines = _extract_quoted_lines_after(text, '"━━ BRAND ━━",')
    portfolio_lines = _extract_quoted_lines_after(text, '"━━ PROJECT PORTFOLIO ━━",')
    guidelines_lines = _extract_quoted_lines_after(text, '"━━ CONVERSATION GUIDELINES ━━",')
    objections_lines = _extract_quoted_lines_after(text, '"━━ OBJECTION HANDLING ━━",')
    firm_rules_lines = _extract_quoted_lines_after(text, '"━━ FIRM RULES ━━",')

    greeting_match = re.search(r'"Hello! Namaste[^"]*"', text)
    name_match = re.search(r"You are ([\w\s]+?) — a (.+?) inbound sales agent", " ".join(identity_lines))

    return {
        "available": True,
        "agent_name": name_match.group(1).strip() if name_match else None,
        "agent_tone": name_match.group(2).strip() if name_match else None,
        "greeting": greeting_match.group(0).strip('"') if greeting_match else None,
        "identity": identity_lines,
        "brand": brand_lines,
        "prompt_guidelines": guidelines_lines,
        "firm_rules": firm_rules_lines,
        "pitch": {
            "project_portfolio": portfolio_lines,
            "objection_handling": objections_lines,
        },
        "source_file": str(CONVERSATION_PY),
        "editable": False,
        "editable_note": ("Read-only for now — this is generated directly from the live "
                          "conversation engine's source code, not a separate config file. "
                          "Editing support (separate prompt/pitch/knowledge-base files the "
                          "engine loads at runtime) is planned for a future update."),
    }


@app.get("/api/agent/config")
async def agent_config() -> JSONResponse:
    return JSONResponse(_introspect_agent_config())


@app.get("/api/analytics/conversation")
async def conversation_analytics(request: Request, folders: str = Query("")) -> JSONResponse:
    """Aggregates AI Call Analysis results. If folders is provided (comma-separated),
    only those folders are included; otherwise all analyzed calls are returned.
    Owner-scoped: a tenant only aggregates calls whose folder they own."""
    _ca_owner = _acting_owner(request)
    all_analyses = _scan_analyses()
    if folders.strip():
        folder_set = {f.strip() for f in folders.split(",") if f.strip()}
        analyses = {k: v for k, v in all_analyses.items() if k in folder_set}
    else:
        analyses = all_analyses
    analyses = {k: v for k, v in analyses.items() if _can_see_folder(k, _ca_owner)}
    n = len(analyses)

    def _counter(key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in analyses.values():
            v = a.get(key)
            if v:
                out[v] = out.get(v, 0) + 1
        return out

    def _list_counter(key: str) -> list[dict]:
        out: dict[str, int] = {}
        for a in analyses.values():
            for item in (a.get(key) or []):
                if item:
                    out[item] = out.get(item, 0) + 1
        return sorted([{"text": k, "count": v} for k, v in out.items()],
                      key=lambda x: x["count"], reverse=True)[:15]

    sentiment = _counter("sentiment")
    lead_quality = _counter("lead_quality")
    objections = _list_counter("objections")
    topics = _list_counter("topics")
    products = _list_counter("products_mentioned")
    questions = _list_counter("customer_questions")

    recommendations = []
    if objections:
        recommendations.append(f"Most common objection: \"{objections[0]['text']}\" ({objections[0]['count']} of {n} analyzed calls).")
    if products:
        recommendations.append(f"Highest customer interest: \"{products[0]['text']}\" ({products[0]['count']} mentions across analyzed calls).")
    if lead_quality.get("hot", 0):
        recommendations.append(f"{lead_quality['hot']} of {n} analyzed calls are classified as hot leads — ready to buy.")
    if sentiment.get("negative", 0) and n:
        pct = round(sentiment["negative"] / n * 100)
        recommendations.append(f"{pct}% of analyzed calls had negative sentiment.")
    if not recommendations:
        recommendations.append("Not enough analyzed calls yet to surface a pattern — analyze more calls from the AI Call Analysis tab.")

    trends: dict[str, dict] = {}
    for folder, a in analyses.items():
        m = re.match(r"^.+_(\d{8})_(\d{6})$", folder)
        day = m.group(1) if m else "unknown"
        day_fmt = f"{day[:4]}-{day[4:6]}-{day[6:8]}" if m else "unknown"
        bucket = trends.setdefault(day_fmt, {"positive": 0, "neutral": 0, "negative": 0,
                                              "hot": 0, "warm": 0, "cold": 0})
        if a.get("sentiment") in bucket:
            bucket[a["sentiment"]] += 1
        if a.get("lead_quality") in bucket:
            bucket[a["lead_quality"]] += 1

    return JSONResponse({
        "analyzed_call_count": n,
        "sentiment": sentiment,
        "lead_intelligence": lead_quality,
        "common_objections": objections,
        "topic_analysis": topics,
        "product_interest": products,
        "top_customer_questions": questions,
        "ai_recommendations": recommendations,
        "trends": [{"date": d, **vals} for d, vals in sorted(trends.items())],
    })


@app.get("/api/calls/transcripts/search")
async def transcripts_search(request: Request, q: str = Query(..., min_length=1), limit: int = Query(50, ge=1, le=200)) -> JSONResponse:
    """Full-text search across transcript.txt files, plus caller/contact/call-id
    metadata. Owner-scoped: a tenant only searches calls whose folder they own."""
    # E12 — transcripts are Growth+.
    _ts_owner = _require_feature(request, "transcripts")
    ql = q.lower()
    digits = "".join(ch for ch in q if ch.isdigit())

    def _scan() -> tuple[list[dict], int]:
        matches: list[dict] = []
        total = 0
        for r in _filter_history(_scan_recordings(limit=2000), _ts_owner):
            # Metadata first — it needs no file read, and it is what makes a
            # pasted call ID or a caller's name/number resolve here at all.
            meta = " ".join(str(r.get(k) or "") for k in
                            ("folder", "call_id", "conn_id", "caller", "phone",
                             "contact_name", "agent_slug")).lower()
            hit = ql in meta or (len(digits) >= 4 and digits in meta)
            if not hit:
                messages = _read_transcript(r["folder"])
                hit = ql in " ".join(m["text"] for m in messages).lower()
            if hit:
                total += 1
                if len(matches) < limit:
                    matches.append(r)
        return matches, total

    # Sync fs walk over up to 2000 folders: off the event loop, or every other
    # request on the API stalls behind it (same failure mode as the copilot
    # upload 502). `total` is the true match count, not the truncated page —
    # the UI showed "50 results" for any query with more than 50 hits.
    matches, total = await asyncio.to_thread(_scan)
    return JSONResponse({"recordings": matches, "total": total, "query": q})


def _gcs_object_for(call_key: str) -> str | None:
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute("SELECT gcs_object FROM calls WHERE call_key=?",
                             (call_key,)).fetchone()
        return row["gcs_object"] if row else None
    except Exception as exc:
        logger.warning("gcs object lookup failed for %s: %r", call_key, exc)
        return None


def _stream_recording(call_key: str, gcs_object: str | None, download: bool):
    """Serve a call's WAV. The implementation lives in recording_response.py so
    the public API (/v1/calls/{id}/recording) serves byte-for-byte the same
    file this dashboard plays."""
    from recording_response import stream_recording
    return stream_recording(call_key, gcs_object, download)


@app.get("/api/calls/recordings/{folder}/audio")
async def recording_audio(folder: str, request: Request, download: int = Query(0)):
    if "/" in folder or ".." in folder:
        return JSONResponse({"error": "invalid folder"}, status_code=400)
    # E12 — the audio itself, not just the list. Gating the index while
    # leaving the stream open would be enforcement in name only.
    if not _can_see_folder(folder, _require_feature(request, "recordings")):
        return JSONResponse({"error": "not found"}, status_code=404)
    return _stream_recording(folder, _gcs_object_for(folder), bool(download))


# ── TTS voice previews ──────────────────────────────────────────────────────
# Platform-wide samples (no tenant scoping — behind the login wall like every
# other route). The copilot names voices during the interview; the chat UI
# renders these as play buttons so users hear a voice before choosing it.

@app.get("/api/voices")
async def list_voice_samples():
    try:
        index = json.loads((VOICE_SAMPLES_DIR / "index.json").read_text())
    except Exception:
        index = []
    voices = [
        {**v, "sample_url": f"/api/voices/sample/{v['speaker']}"}
        for v in index
        if isinstance(v, dict) and re.fullmatch(r"[a-z]+", str(v.get("speaker", "")))
        and (VOICE_SAMPLES_DIR / f"{v['speaker']}.wav").exists()
    ]
    return {"voices": voices}


@app.get("/api/voices/sample/{speaker}")
async def voice_sample(speaker: str):
    if not re.fullmatch(r"[a-z]+", speaker):
        return JSONResponse({"error": "invalid speaker"}, status_code=400)
    path = VOICE_SAMPLES_DIR / f"{speaker}.wav"
    if not path.exists():
        return JSONResponse({"error": "no sample for this voice"}, status_code=404)
    return FileResponse(str(path), media_type="audio/wav",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/exports/dl/{token}")
async def export_public_download(token: str):
    """Public tokenized download of a generated .xlsx export.

    Same contract as the recording link above: the 256-bit random token IS the
    credential (route is auth-exempt), and every miss — bad shape, unknown
    token, revoked export — returns the identical 404 so the endpoint never
    reveals which tokens exist. This is how an export in the private GCS
    bucket reaches a colleague who has no dashboard login."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{22,128}", token or ""):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM call_exports"
                " WHERE download_token=? AND deleted_at IS NULL",
                (token,)).fetchone()
    except Exception as exc:
        logger.error("export token lookup failed: %r", exc)
        return JSONResponse({"error": "not found"}, status_code=404)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    from intel_api import stream_export
    return stream_export(dict(row))


@app.get("/api/recordings/dl/{token}")
async def recording_public_download(token: str):
    """Public tokenized download. The 256-bit random token IS the credential
    (route is auth-exempt); any miss is an identical 404 so the endpoint
    leaks nothing about which tokens exist."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{22,128}", token or ""):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT call_key, gcs_object FROM calls WHERE download_token=?",
                (token,)).fetchone()
    except Exception as exc:
        logger.error("token lookup failed: %r", exc)
        return JSONResponse({"error": "not found"}, status_code=404)
    if not row or not row["call_key"]:
        return JSONResponse({"error": "not found"}, status_code=404)
    return _stream_recording(row["call_key"], row["gcs_object"], download=True)


# ---------------------------------------------------------------------------
# Usage minutes (BILLING view) — reads the usage_ledger written by the bridge
# and reconciled against FreeSWITCH CDRs (shared/usage_metering.py). This is
# the number users will eventually be charged on.
# ---------------------------------------------------------------------------

@app.get("/api/usage/minutes")
async def usage_minutes(request: Request) -> JSONResponse:
    import auth as _auth
    from shared.usage_metering import display_minutes, ROUNDING, parse_ts
    owner = _acting_owner(request)
    target = request.query_params.get("owner")
    if not _auth.is_admin(owner):
        target = None
    scope_all = target == "all"
    scope_owner = target or owner

    try:
        from shared.db.database import get_db
        with get_db() as db:
            if scope_all:
                rows = [dict(r) for r in db.execute(
                    "SELECT * FROM usage_ledger WHERE billable=1").fetchall()]
            else:
                rows = [dict(r) for r in db.execute(
                    "SELECT * FROM usage_ledger WHERE billable=1 AND owner=?",
                    (scope_owner,)).fetchall()]
    except Exception as exc:
        logger.error("usage minutes read failed: %r", exc)
        return JSONResponse({"error": "usage unavailable"}, status_code=500)

    now = datetime.datetime.now(datetime.timezone.utc)
    month_key = now.strftime("%Y-%m")
    today_key = now.strftime("%Y-%m-%d")

    def _sec(r: dict) -> int:
        return int(r["billsec_final"] if r["billsec_final"] is not None
                   else (r["billsec_pipeline"] or 0))

    month_secs: list[int] = []
    today_s = total_s = 0
    per_day: dict[str, dict] = {}
    unreconciled = 0
    for r in rows:
        t = parse_ts(r.get("answered_at"))
        if t is None:
            continue
        s = _sec(r)
        total_s += s
        day = t.strftime("%Y-%m-%d")
        if day == today_key:
            today_s += s
        if t.strftime("%Y-%m") == month_key:
            month_secs.append(s)
        if (now - t).days <= 30:
            d = per_day.setdefault(day, {"date": day, "seconds": 0, "calls": 0})
            d["seconds"] += s
            d["calls"] += 1
        if r["status"] != "reconciled":
            unreconciled += 1

    recent = sorted(rows, key=lambda r: r.get("answered_at") or "", reverse=True)[:20]

    # Entitlement view, from the SAME functions the dial gates read. If the
    # meter here and a refusal at dial time ever disagree, the meter is lying
    # to the user about why their call was blocked — so both come from
    # usage_summary() rather than being computed twice.
    entitlement: dict = {}
    if not scope_all:
        try:
            from shared.billing.entitlements import usage_summary
            entitlement = usage_summary(scope_owner)
        except Exception as exc:
            logger.warning("usage entitlement view unavailable: %r", exc)

    return JSONResponse({
        "owner": "all" if scope_all else scope_owner,
        "month": month_key,
        "plan_code": entitlement.get("plan_code"),
        "plan_name": entitlement.get("plan_name"),
        "minutes_limit": entitlement.get("minutes_limit"),
        "minutes_used_period": entitlement.get("minutes_used"),
        "minutes_remaining": entitlement.get("minutes_remaining"),
        "period_start": entitlement.get("period_start"),
        "period_end": entitlement.get("period_end"),
        "entitlement_status": entitlement.get("status"),
        "seconds_month": sum(month_secs),
        "minutes_month": display_minutes(month_secs),
        "calls_month": len(month_secs),
        "seconds_today": today_s,
        "seconds_total": total_s,
        "rounding": ROUNDING,
        "unreconciled": unreconciled,
        "per_day": sorted(per_day.values(), key=lambda d: d["date"]),
        "recent": [{
            "conn_id": r["conn_id"], "answered_at": r["answered_at"],
            "phone": r["phone"], "agent_slug": r["agent_slug"],
            "direction": r["direction"], "seconds": _sec(r),
            "status": r["status"], "source": r["source"],
        } for r in recent],
    })


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "…" + key[-2:]
    return key[:6] + "…" + key[-4:]


def _read_active_api_key() -> str:
    if not CALLING_AGENT_ENV_FILE.exists():
        return ""
    m = re.search(r'^SARVAM_API_KEY=(.*)$', CALLING_AGENT_ENV_FILE.read_text(), re.MULTILINE)
    return m.group(1).strip() if m else ""


def _write_active_api_key(new_key: str) -> None:
    text = CALLING_AGENT_ENV_FILE.read_text() if CALLING_AGENT_ENV_FILE.exists() else ""
    if re.search(r'^SARVAM_API_KEY=.*$', text, re.MULTILINE):
        text = re.sub(r'^SARVAM_API_KEY=.*$', f'SARVAM_API_KEY={new_key}', text, flags=re.MULTILINE)
    else:
        text = text.rstrip("\n") + f"\nSARVAM_API_KEY={new_key}\n"
    CALLING_AGENT_ENV_FILE.write_text(text)


def _load_api_key_pool() -> list[str]:
    if not API_KEY_POOL_FILE.exists():
        return []
    try:
        return json.loads(API_KEY_POOL_FILE.read_text()).get("pool", [])
    except Exception:
        return []


def _save_api_key_pool(pool: list[str]) -> None:
    API_KEY_POOL_FILE.write_text(json.dumps({"pool": pool}, indent=2))


def _load_key_state() -> dict:
    return _setting_get("api_key_state",
                        {"restart_required": False, "last_changed_at": None})


def _save_key_state(state: dict) -> None:
    _setting_set("api_key_state", state)


def _apply_new_key(new_key: str, *, total_credits: float | None = None) -> dict:
    """Write a new active key to .env, reset the tracked credit balance to
    full, and mark a restart as required (the running agent process only
    reads SARVAM_API_KEY once, at startup — a key swap here doesn't reach it
    until the process is relaunched)."""
    _write_active_api_key(new_key)
    current = _load_sarvam_credits()
    total = total_credits if total_credits is not None else current.get("total_credits", 100)
    data = {"total_credits": total, "remaining_credits": total, "updated_at": _now_iso()}
    _save_sarvam_credits(data)
    _save_key_state({"restart_required": True, "last_changed_at": _now_iso()})
    return data


def _maybe_auto_rotate_key() -> dict | None:
    """If credits are exhausted and a backup key is queued, auto-promote it.
    Called as a side effect of polling /api/sarvam/credits so it self-drives
    off the dashboard's existing polling cadence — no separate background
    daemon needed. Still requires a manual restart to take effect on live
    calls (see _apply_new_key)."""
    credits = _load_sarvam_credits()
    if credits.get("remaining_credits", 0) > 0:
        return None
    pool = _load_api_key_pool()
    if not pool:
        return None
    next_key = pool.pop(0)
    _save_api_key_pool(pool)
    _apply_new_key(next_key)
    return {"rotated_to": _mask_key(next_key), "pool_remaining": len(pool)}


def _load_sarvam_credits() -> dict:
    """User-reported Sarvam account credit balance.

    Sarvam exposes no quota API (see credit_tracker.py's own docstring), so
    this is a manually-synced number, not derived from the ₹ usage estimate
    elsewhere on this tab — there's no documented ₹-per-credit conversion
    rate to derive it from, and inventing one would misrepresent a real
    account balance as a computed fact.
    """
    return _setting_get(
        "sarvam_credits",
        {"total_credits": 100, "remaining_credits": 100, "updated_at": None})


def _save_sarvam_credits(data: dict) -> None:
    _setting_set("sarvam_credits", data)


@app.get("/api/sarvam/credits")
async def sarvam_credits() -> JSONResponse:
    rotation = _maybe_auto_rotate_key()
    d = _load_sarvam_credits()
    total = d.get("total_credits", 100)
    remaining = d.get("remaining_credits", total)
    used = max(total - remaining, 0)
    percent_used = round((used / total) * 100, 1) if total else 0.0
    exhausted = remaining <= 0
    pool_count = len(_load_api_key_pool())
    key_state = _load_key_state()
    return JSONResponse({
        "total_credits": total, "remaining_credits": remaining,
        "used_credits": used, "percent_used": percent_used,
        "updated_at": d.get("updated_at"),
        "low": 0 < remaining <= LOW_CREDIT_THRESHOLD,
        "exhausted": exhausted,
        "needs_new_key": exhausted and pool_count == 0 and rotation is None,
        "pool_count": pool_count,
        "restart_required": key_state.get("restart_required", False),
        "auto_rotated": rotation,
    })


@app.post("/api/sarvam/credits")
async def sarvam_credits_update(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        remaining = float(body.get("remaining_credits"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "reason": "remaining_credits must be a number"}, status_code=400)
    current = _load_sarvam_credits()
    total = current.get("total_credits", 100)
    if body.get("total_credits") is not None:
        try:
            total = float(body["total_credits"])
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "reason": "total_credits must be a number"}, status_code=400)
    data = {"total_credits": total, "remaining_credits": remaining, "updated_at": _now_iso()}
    _save_sarvam_credits(data)
    return JSONResponse({"ok": True, **data})


@app.get("/api/sarvam/keys")
async def api_keys_info() -> JSONResponse:
    active = _read_active_api_key()
    pool = _load_api_key_pool()
    state = _load_key_state()
    return JSONResponse({
        "active_key_masked": _mask_key(active) if active else None,
        "pool": [_mask_key(k) for k in pool],
        "pool_count": len(pool),
        "restart_required": state.get("restart_required", False),
        "last_changed_at": state.get("last_changed_at"),
    })


@app.post("/api/sarvam/keys/replace")
async def api_keys_replace(request: Request) -> JSONResponse:
    body = await request.json()
    new_key = (body.get("api_key") or "").strip()
    if not new_key:
        return JSONResponse({"ok": False, "reason": "api_key is required"}, status_code=400)
    _apply_new_key(new_key)
    return JSONResponse({"ok": True, "masked_key": _mask_key(new_key), "restart_required": True})


@app.post("/api/sarvam/keys/bulk")
async def api_keys_bulk_add(request: Request) -> JSONResponse:
    body = await request.json()
    raw_keys = body.get("keys") or []
    if not isinstance(raw_keys, list):
        return JSONResponse({"ok": False, "reason": "keys must be a list"}, status_code=400)
    cleaned = [k.strip() for k in raw_keys if isinstance(k, str) and k.strip()]
    pool = _load_api_key_pool()
    active = _read_active_api_key()
    added = 0
    for k in cleaned:
        if k != active and k not in pool:
            pool.append(k)
            added += 1
    _save_api_key_pool(pool)
    return JSONResponse({"ok": True, "added": added, "skipped": len(cleaned) - added, "pool_count": len(pool)})


@app.delete("/api/sarvam/keys/pool")
async def api_keys_clear_pool() -> JSONResponse:
    _save_api_key_pool([])
    return JSONResponse({"ok": True, "pool_count": 0})


@app.post("/api/sarvam/keys/ack-restart")
async def api_keys_ack_restart() -> JSONResponse:
    state = _load_key_state()
    state["restart_required"] = False
    _save_key_state(state)
    return JSONResponse({"ok": True})


def _credit_log_first_date() -> datetime.date:
    """Earliest day the tracker ever wrote. Cheap: filenames are the dates."""
    dates = []
    try:
        for p in CREDIT_LOG_DIR.glob("*.jsonl"):
            try:
                dates.append(datetime.date.fromisoformat(p.stem))
            except ValueError:
                continue
    except Exception as exc:
        logger.warning("credit_log scan failed: %s", exc)
    return min(dates) if dates else datetime.date.today()


@app.get("/api/sarvam/usage")
async def sarvam_usage(period: str = Query("today")) -> JSONResponse:
    today = datetime.date.today()
    ranges = {"today": 0, "week": 6, "month": 29}
    if period == "all":
        # Lifetime spend. Previously unanswerable from the product at all —
        # the widest window was 30 days, so "what have we spent so far" had
        # no endpoint behind it.
        start = _credit_log_first_date()
    else:
        start = today - datetime.timedelta(days=ranges.get(period, 0))
    events = _load_credit_range(start, today)
    calls  = _aggregate_calls(events)
    top10  = sorted(calls, key=lambda c: c["est_total_cost"], reverse=True)[:10]
    daily: dict[str, dict] = {}
    d = start
    while d <= today:
        daily[d.isoformat()] = _summarize_range(_load_credit_log(d))
        d += datetime.timedelta(days=1)
    return JSONResponse({"period": period, "start": start.isoformat(),
                         "end": today.isoformat(), "summary": _summarize_range(events),
                         "daily": daily, "top_calls": top10,
                         "rates": pricing.rate_card()})


@app.get("/api/sarvam/usage/history")
async def usage_history(days: int = Query(30, ge=1, le=90)) -> JSONResponse:
    today = datetime.date.today()
    result = []
    for i in range(days - 1, -1, -1):
        d = today - datetime.timedelta(days=i)
        s = _summarize_range(_load_credit_log(d))
        result.append({"date": d.isoformat(), **s})
    return JSONResponse({"history": result})


@app.get("/api/logs/stream")
async def log_stream(request: Request,
                     categories: str = Query("all")) -> StreamingResponse:
    cat_set = set(c.strip() for c in categories.split(","))

    async def generator() -> AsyncIterator[str]:
        async for chunk in _log_event_stream(cat_set):
            if await request.is_disconnected():
                break
            yield chunk

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/logs/tail")
async def log_tail(lines: int = Query(200, ge=1, le=2000)) -> JSONResponse:
    log_file = LOGS_LATEST / "ws_audio_receiver.log"
    result = []
    if log_file.exists():
        try:
            proc = subprocess.run(["tail", "-n", str(lines), str(log_file)],
                                  capture_output=True, text=True, timeout=5)
            for line in proc.stdout.splitlines():
                if line.strip():
                    cat, color = _categorize_line(line)
                    result.append({"line": line, "category": cat, "color": color})
        except Exception as exc:
            logger.warning("log_tail error: %s", exc)
    return JSONResponse({"lines": result})


# ---------------------------------------------------------------------------
# Outbound contacts
# ---------------------------------------------------------------------------

# `empty_call` is derived, not dialled: the originate result can only ever say
# "answered", so a contact whose most recent call connected with nobody actually
# speaking is resolved at READ time from calls.answer_state (see
# _empty_call_contacts). It is a first-class status everywhere else — stats,
# the phone-book filter, and the campaign audience — because "redial everyone
# whose call connected but went nowhere" is a real audience. Before this it was
# a filter option the API could never return a row for.
CALL_STATUSES   = ("not_called", "answered", "empty_call", "no_answer", "busy",
                   "declined", "invalid_number", "failed")
DISPOSITIONS    = ("not_classified", "interested", "site_visit_booked", "inquiry",
                   "in_doubt", "to_contact_later", "rejected")

# Campaign state is PER-OWNER — one running campaign per tenant. A shared
# global would leak one tenant's live current_phone/current_name to every other
# tenant polling status, and let any tenant stop another's campaign.
def _fresh_campaign() -> dict:
    return {
        "running": False, "campaign_id": None, "total": 0, "completed": 0,
        "answered": 0, "no_answer": 0, "busy": 0, "declined": 0,
        "invalid_number": 0, "failed": 0,
        "skipped": 0, "dnd_skipped": 0, "capped": False,
        "current_phone": "", "current_name": "",
        "started_at": None, "stopped": False, "error": None,
        "audience": None, "audience_disposition": None,
        "audience_group": None, "audience_size": 0,
    }


_campaigns: dict[str, dict] = {}      # owner -> campaign state
_campaign_tasks: dict[str, Any] = {}  # owner -> asyncio task


def _campaign_for(owner: str) -> dict:
    return _campaigns.setdefault(owner, _fresh_campaign())


# Contacts live in the owner-scoped `contacts` DB table (Part B Phase 3).
# Every reader/writer passes the acting owner's email so no tenant ever sees
# or mutates another's contacts. The dict shape below is exactly what the UI
# expects (id == the string handle used by dial/patch/delete).
_CONTACT_COLS = ("name", "notes", "call_status", "disposition",
                 "call_count", "last_called_at", "call_notes")


def _contact_to_dict(row: dict) -> dict:
    try:
        extra = json.loads(row.get("extra") or "{}")
    except Exception:
        extra = {}
    try:
        details = json.loads(row.get("details") or "{}")
        if not isinstance(details, dict):
            details = {"value": details}
    except Exception:
        details = {}
    return {
        "id": row.get("legacy_id") or f"db{row.get('id')}",
        "db_id": row.get("id"),
        "phone": row.get("phone") or "",
        "name": row.get("name") or "",
        "notes": row.get("notes") or "",
        "gender": row.get("gender") or "",
        "details": details,
        "group_id": row.get("group_id"),
        "added_at": row.get("added_at"),
        "call_status": row.get("call_status") or "not_called",
        "disposition": row.get("disposition") or "not_classified",
        "call_count": row.get("call_count") or 0,
        "last_called_at": row.get("last_called_at"),
        "call_notes": row.get("call_notes") or "",
        "call_history": extra.get("call_history", []),
    }


def _empty_call_contacts(db, owner: str) -> set:
    """Contact PKs whose MOST RECENT call connected but went nowhere.

    The dial path can only ever stamp 'answered' — whether anybody actually
    spoke is decided at teardown and lands in calls.answer_state. Folding the
    latest verdict back over the phone book is what makes the 'Empty call'
    filter (and audience) return the rows it names instead of always zero.

    Rows with a NULL verdict are read too, and deliberately: they are the
    LATEST word on that contact when the detector did not run, and skipping
    them would leave a months-old 'empty' verdict standing over a call that has
    since connected properly. Unknown is not empty. Indexed by idx_calls_owner.
    """
    latest: dict = {}
    for r in db.execute(
            "SELECT contact_id, answer_state FROM calls "
            " WHERE owner=? AND contact_id IS NOT NULL"
            " ORDER BY started_at", (owner,)).fetchall():
        latest[r["contact_id"]] = r["answer_state"]      # ordered → last wins
    return {cid for cid, state in latest.items() if (state or "") == "empty"}


def _load_outbound(owner: str) -> list[dict]:
    """Every contact owned by `owner`, newest first."""
    try:
        from shared.db.database import get_db
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM contacts WHERE owner=? ORDER BY id", (owner,)).fetchall()
            empties = _empty_call_contacts(db, owner)
        contacts = [_contact_to_dict(dict(r)) for r in rows]
        for c in contacts:
            if c["call_status"] == "answered" and c["db_id"] in empties:
                c["call_status"] = "empty_call"
        return contacts
    except Exception as exc:
        logger.error("contacts load failed for %s: %r", owner, exc)
        return []


def _save_outbound(contacts: list[dict], owner: str) -> None:
    """Sync `owner`'s contact set to `contacts` (one transaction).

    Load-all/mutate/save-all is how the endpoints work; this used to be a
    DELETE+re-INSERT, but calls.contact_id now references the contacts
    integer PK, so rows must be UPDATEd in place (stable id), with only
    genuinely new/removed contacts inserted/deleted. The UI handle stays the
    string `id` (legacy_id, or "db<pk>" for rows imported without one).
    """
    from shared.db.database import get_db
    from shared.phone import significant_digits
    with get_db() as db:
        existing = {}  # ui-handle -> integer pk
        for r in db.execute("SELECT id, legacy_id FROM contacts WHERE owner=?",
                            (owner,)).fetchall():
            existing[r["legacy_id"] or f"db{r['id']}"] = r["id"]
        keep: set = set()
        for c in contacts:
            phone = c.get("phone") or ""
            if not phone:
                continue
            handle = c.get("id")
            extra = json.dumps({"call_history": c.get("call_history", [])})
            details = c.get("details")
            details_json = (json.dumps(details, ensure_ascii=False)
                            if isinstance(details, dict) else (details or None))
            vals = (phone, significant_digits(phone),
                    c.get("name") or "", c.get("notes") or "",
                    c.get("call_status") or "not_called",
                    c.get("disposition") or "not_classified",
                    int(c.get("call_count") or 0), c.get("last_called_at"),
                    c.get("call_notes") or "", extra, c.get("added_at"),
                    c.get("gender") or None, details_json, c.get("group_id"))
            if handle in existing:
                keep.add(handle)
                db.execute(
                    "UPDATE contacts SET phone=?, sig_digits=?, name=?, notes=?, "
                    "call_status=?, disposition=?, call_count=?, last_called_at=?, "
                    "call_notes=?, extra=?, added_at=?, gender=?, details=?, "
                    "group_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    vals + (existing[handle],))
            else:
                db.execute(
                    "INSERT INTO contacts (legacy_id, owner, phone, sig_digits, name, "
                    "notes, call_status, disposition, call_count, last_called_at, "
                    "call_notes, extra, added_at, gender, details, group_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (handle, owner) + vals)
        for handle, pk in existing.items():
            if handle not in keep:
                _detach_contact_references(db, pk)
                db.execute("DELETE FROM contacts WHERE id=?", (pk,))


def _detach_contact_references(db, pk: int) -> None:
    """Re-home intelligence rows before a contact row is deleted. Implemented in
    shared/contacts.py so the public API's DELETE leaves the data exactly as
    coherent as this one does."""
    from shared.contacts import detach_contact_references
    detach_contact_references(db, pk)


def _normalize_gender(value: str) -> str:
    from shared.contacts import normalize_gender
    return normalize_gender(value)


def _dialable_phone(value) -> str:
    """A contact's number in +E.164, or "" when nothing dialable was typed.

    One rule for every hand-entry point, matching `_format_phone_digits` (CSV
    import) and `shared.contacts.upsert_contact` (public API). The phone book
    used to hold whatever the operator typed; a bare 10-digit row then reached
    the carrier without a country code and was rejected 404 while the UI said
    only "declined".
    """
    from shared.phone import digits_only, to_e164

    e164 = to_e164((str(value or "")).strip())
    return e164 if len(digits_only(e164)) >= 7 else ""


def _make_contact(phone: str, name: str = "", notes: str = "",
                  gender: str = "", details: dict | None = None,
                  group_id: int | None = None) -> dict:
    return {
        "id": str(int(time.time() * 1000)) + "_" + phone[-4:],
        "phone": phone,
        "name": name,
        "notes": notes,
        "gender": _normalize_gender(gender),
        "details": details or {},
        "group_id": group_id,
        "added_at": datetime.datetime.utcnow().isoformat() + "Z",
        "call_status": "not_called",
        "disposition": "not_classified",
        "call_count": 0,
        "last_called_at": None,
        "call_notes": "",
        "call_history": [],
    }


# Common job-title words used to split a free-text "name designation" tail
# when a line has no tab/comma/multi-space delimiter to split on reliably.
_DESIGNATION_KEYWORDS = (
    "manager", "engineer", "director", "lead", "head", "officer", "executive",
    "founder", "co-founder", "ceo", "cto", "coo", "cfo", "vp", "president",
    "chairman", "analyst", "consultant", "developer", "designer", "specialist",
    "associate", "assistant", "coordinator", "supervisor", "administrator",
    "representative", "intern", "owner", "partner", "architect", "scientist",
    "recruiter", "accountant", "advisor", "agent", "broker",
)

# Modifier words that, when immediately preceding a keyword match, belong
# to the designation phrase too (e.g. "project manager", "senior engineer").
_DESIGNATION_MODIFIERS = (
    "project", "senior", "junior", "deputy", "regional", "national", "global",
    "chief", "vice", "principal", "general", "technical", "product", "sales",
    "marketing", "operations", "hr", "it", "finance", "business", "team",
    "group", "area", "branch", "country", "zonal", "vp",
)


def _excel_sci_to_digits(token: str) -> str | None:
    """Recover digits from an Excel scientific-notation cell like
    '9.18077E+11' (Excel renders long numeric phone numbers this way).
    Returns None if the token isn't scientific notation."""
    t = token.strip()
    if not re.match(r"^\d+(\.\d+)?[eE]\+?\d+$", t):
        return None
    try:
        return str(int(float(t)))
    except (ValueError, OverflowError):
        return None


def _xlsx_cell_text(value) -> str:
    """One spreadsheet cell as the text a human would have typed.

    A phone column stored as a NUMBER comes back from openpyxl as an int or a
    float; `str(9128692865.0)` is "9128692865.0", and the digit stripper
    downstream turned that into 91286928650 — an 11-digit number the carrier
    does not know (RUNBOOK §58). Integral floats print as integers; anything
    else prints as before.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e17 else repr(value)
    return str(value)


def _normalize_phone_cell(cell: str) -> str:
    """Extract digits from a single cell, recovering Excel sci-notation and
    float formatting first (`shared.phone.numeric_text`, the one rule every
    write path shares)."""
    from shared.phone import numeric_text
    sci = _excel_sci_to_digits(cell)
    return sci if sci is not None else re.sub(r"\D", "", numeric_text(cell))


def _format_phone_digits(digits: str) -> str:
    """Format a digit string as a dialable number, assuming India for
    national-format numbers. The rules live in shared/phone.py so the API's
    contact push normalizes numbers identically to a CSV upload."""
    from shared.phone import to_e164
    return to_e164(digits)


def _phone_from_contact_cell(cell: str) -> str:
    """Extract ONE dialable phone number from a contact-details cell.
    Handles multiple listed numbers ('23250771/72/73' → first one) and
    trailing extension ranges ('079-23251306-308' → 079-23251306)."""
    part = re.split(r"[/;]|\bor\b", cell.strip(), maxsplit=1)[0]
    # "(079)0232-..." — redundant trunk 0 repeated after a parenthesized
    # STD code; drop it so the digits join into one national number.
    part = re.sub(r"^\((0\d+)\)\s*0", r"\1", part.strip())
    digits = _normalize_phone_cell(part)
    # Drop a short trailing hyphen-group (extension / range suffix) if what
    # precedes it is already a complete number on its own.
    m = re.match(r"^(.*\d)[-–]\s*(\d{1,5})\s*$", part.strip())
    if m:
        base = _normalize_phone_cell(m.group(1))
        if len(base) >= 10:
            digits = base
    return _format_phone_digits(digits)


_PERSON_NAME_RE = re.compile(
    r"^(shri|smt|dr|mr|mrs|ms|miss|prof|kum|kumari)\b\.?", re.IGNORECASE)


def _is_nameish(cell: str) -> bool:
    """A cell usable as a contact name: not a URL, email, or bare number."""
    c = cell.strip()
    return bool(c) and "@" not in c and not re.match(r"https?://|www\.", c, re.I) \
        and not re.fullmatch(r"[\d\s\-+()./]+", c)


def _map_header_columns(header: list[str]) -> dict | None:
    """Map a header row to column indices: which column holds the phone,
    which hold names, and which (if any) holds gender. Every OTHER column is
    remembered by header label so its values land in the contact's `details`
    dict. Returns None if no phone-like header exists (caller falls back to
    positional heuristics)."""
    phone_idx = None
    gender_idx = None
    name_idxs: list[int] = []
    extras: dict[int, str] = {}  # column index -> header label
    for i, h in enumerate(header):
        hl = h.strip().lower()
        if phone_idx is None and re.search(r"phone|mobile|whatsapp|contact|telephone|\bnumber\b", hl):
            phone_idx = i
        elif re.search(r"\bname\b", hl):
            name_idxs.append(i)
        elif gender_idx is None and re.search(r"\bgender\b|\bsex\b", hl):
            gender_idx = i
        elif hl:
            extras[i] = h.strip()
    if phone_idx is None:
        return None
    return {"phone": phone_idx, "names": name_idxs, "gender": gender_idx,
            "extras": extras}


def _row_to_contact_mapped(cells: list[str], colmap: dict) -> dict | None:
    """Extract a contact using header-derived column positions. The phone
    comes only from the mapped phone column. Among multiple name columns
    (e.g. org name + officer names), prefer a person-looking value
    (honorific prefix); remaining name values become notes."""
    pi = colmap["phone"]
    if pi >= len(cells):
        return None
    phone = _phone_from_contact_cell(cells[pi])
    if not phone:
        return None

    name_vals = [cells[i].strip() for i in colmap["names"]
                 if i < len(cells) and _is_nameish(cells[i])]
    # de-dup while preserving order (same officer may appear twice)
    seen: list[str] = []
    for v in name_vals:
        if v not in seen:
            seen.append(v)
    name = next((v for v in seen if _PERSON_NAME_RE.match(v)), seen[0] if seen else "")
    notes = " | ".join(v for v in seen if v != name)

    gi = colmap.get("gender")
    gender = _normalize_gender(cells[gi]) if gi is not None and gi < len(cells) else ""
    # Every unmapped column becomes a details entry keyed by its header label.
    details = {label: cells[i].strip()
               for i, label in (colmap.get("extras") or {}).items()
               if i < len(cells) and cells[i].strip()}
    return {"phone": phone, "name": name, "notes": notes,
            "gender": gender, "details": details}


def _looks_like_header_row(cells: list[str]) -> bool:
    joined = " ".join(c.lower() for c in cells)
    return (any(k in joined for k in ("phone", "number", "mobile", "contact", "designation", "name"))
            and not any(7 <= len(_normalize_phone_cell(c)) <= 15 for c in cells))


def _split_free_text_line(line: str) -> list[str]:
    """Split a single TXT line into cells, trying the most reliable
    delimiter available: tab > comma > 2-or-more spaces. If none of those
    are present, return the whole line as one cell — _row_to_contact's
    single-cell path handles plain single-space free text by tokenizing
    on words itself, anchored on the phone-shaped word."""
    if "\t" in line:
        return [c.strip() for c in line.split("\t")]
    if "," in line:
        return [c.strip() for c in line.split(",")]
    if re.search(r" {2,}", line):
        return [c.strip() for c in re.split(r" {2,}", line)]
    return [line]


def _row_to_contact(cells: list[str]) -> dict | None:
    """Turn a row of cells into {"phone","name","notes"}, or None if no
    phone-shaped value is found.

    Two cases:
    - A single cell (plain space-separated free text, no reliable
      delimiter): tokenize into words, the phone-shaped WORD anchors the
      split, remaining words become name + designation/notes (split on a
      common job-title keyword if one appears).
    - Multiple cells (tab/comma/2+space delimited, i.e. real columns):
      whichever cell normalizes to 7-15 digits is the phone, the next
      non-empty cell is the name, everything else is joined into notes."""
    cells = [c for c in cells if c is not None and c.strip()]
    if not cells:
        return None

    if len(cells) == 1:
        words = cells[0].split()
        phone_i = next((i for i, w in enumerate(words)
                         if 7 <= len(_normalize_phone_cell(w)) <= 15), None)
        if phone_i is None:
            return None
        phone_digits = _normalize_phone_cell(words[phone_i])
        remaining = words[:phone_i] + words[phone_i + 1:]
        split_at = next((i for i, w in enumerate(remaining)
                          if w.strip(".,").lower() in _DESIGNATION_KEYWORDS), None)
        if split_at is not None:
            while (split_at > 0
                   and remaining[split_at - 1].strip(".,").lower() in _DESIGNATION_MODIFIERS):
                split_at -= 1
            name = " ".join(remaining[:split_at]).strip()
            notes = " ".join(remaining[split_at:]).strip()
        else:
            name, notes = " ".join(remaining).strip(), ""
        return {"phone": _format_phone_digits(phone_digits), "name": name, "notes": notes}

    phone_idx = None
    phone = ""
    for i, c in enumerate(cells):
        if "@" in c or re.match(r"https?://|www\.", c, re.I):
            continue
        candidate = _phone_from_contact_cell(c)
        if candidate:
            phone_idx = i
            phone = candidate
            break
    if phone_idx is None:
        return None

    rest = [c.strip() for i, c in enumerate(cells) if i != phone_idx]
    name_val = next((c for c in rest if _is_nameish(c)), "")
    others = [c for c in rest if c and c != name_val]
    name = name_val
    notes = " | ".join(others)
    return {"phone": phone, "name": name, "notes": notes}


def _parse_uploaded_file(filename: str, raw: bytes) -> tuple[list[dict], int]:
    """Return (list_of_{"phone","name","notes"}, parse_error_count).

    If a header row is present it drives column selection (phone comes only
    from the phone/contact column, name from name columns). Without a
    header, positional heuristics apply."""
    rows: list[dict] = []
    errors = 0
    fname = filename.lower()
    colmap: dict | None = None

    def consume(cells: list[str]) -> None:
        nonlocal colmap, errors
        cells = [(c or "").strip() for c in cells]
        if not any(cells):
            return
        if _looks_like_header_row(cells):
            if colmap is None:
                colmap = _map_header_columns(cells)
            return
        contact = (_row_to_contact_mapped(cells, colmap) if colmap
                   else _row_to_contact(cells))
        if contact and contact["phone"]:
            rows.append(contact)
        else:
            errors += 1

    if fname.endswith(".xlsx") or fname.endswith(".xls"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    consume([_xlsx_cell_text(c) for c in row])
        except Exception as e:
            logger.warning("xlsx parse error: %r", e)
            errors += 1
    else:
        text = raw.decode("utf-8-sig", errors="replace")
        first_line = text.splitlines()[0] if text.strip() else ""
        if fname.endswith(".csv") or "\t" in first_line:
            # Delimited text (CSV, or tab-separated TXT pasted from a
            # spreadsheet). csv.reader handles quoted multi-line fields.
            if "\t" in first_line:
                dialect: type[csv.Dialect] | csv.Dialect = csv.excel_tab
            else:
                try:
                    dialect = csv.Sniffer().sniff(first_line)
                except csv.Error:
                    dialect = csv.excel
            for cells in csv.reader(io.StringIO(text), dialect=dialect):
                consume(cells)
        else:
            # Plain free-text TXT — one contact per non-empty line.
            for line in text.splitlines():
                if line.strip():
                    consume(_split_free_text_line(line))

    return rows, errors


def _filter_by_group(contacts: list[dict], group: str | None) -> list[dict]:
    """Narrow a contact list to one phone-book group.

    'all'/blank keeps everything, 'none' is the implicit No-group bucket, and
    anything else is a group id. An unparseable id keeps the list intact rather
    than silently returning everyone's contacts as if no group were selected —
    the caller states a group, so a bad one is a no-op, not a widening.
    """
    if not group or group == "all":
        return contacts
    if group == "none":
        return [c for c in contacts if not c.get("group_id")]
    try:
        gid = int(group)
    except (TypeError, ValueError):
        return contacts
    return [c for c in contacts if c.get("group_id") == gid]


@app.get("/api/outbound/contacts")
async def outbound_list(
    request: Request,
    call_status: str = Query(None),
    disposition: str = Query(None),
    q: str = Query(None),
    group: str = Query(None),
    page: int = Query(1),
    limit: int = Query(0),
) -> JSONResponse:
    contacts = _load_outbound(_acting_owner(request))
    if call_status and call_status != "all":
        contacts = [c for c in contacts if c.get("call_status") == call_status]
    if disposition and disposition != "all":
        contacts = [c for c in contacts if c.get("disposition") == disposition]
    contacts = _filter_by_group(contacts, group)
    if q:
        ql = q.lower()
        contacts = [c for c in contacts if ql in c.get("phone", "") or ql in c.get("name", "").lower()]
    # `total` is always the full filtered count — the page slice must never be
    # what the caller counts by. Pagination is opt-in (limit>0) so callers that
    # want the whole filtered list keep getting it.
    total = len(contacts)
    if limit and limit > 0:
        start = max(0, (max(1, page) - 1) * limit)
        contacts = contacts[start:start + limit]
    return JSONResponse({"contacts": contacts, "total": total})


@app.post("/api/outbound/contacts")
async def outbound_add(request: Request) -> JSONResponse:
    owner = _acting_owner(request)
    body = await request.json()
    # Store +E.164, exactly as the CSV import and the public API do. Typing a
    # 10-digit number into this form is the normal way to add a contact, and
    # the number that comes out of it is dialled verbatim on a carrier trunk —
    # without the country code the gateway answers 404 and the operator is told
    # only "declined" (2026-09-09). `_dialable_phone` rejects what cannot be
    # made dialable rather than storing a number that will fail at dial time.
    phone = _dialable_phone(body.get("phone"))
    if not phone:
        return JSONResponse({"ok": False, "reason": "phone required (min 7 digits)"}, status_code=400)
    from shared.phone import significant_digits
    contacts = _load_outbound(owner)
    # Compare by the last 10 significant digits so "+9198…", "098…" and
    # "98…" are all the same person (matches the DB unique index).
    existing_digits = {significant_digits(c["phone"]) for c in contacts}
    if significant_digits(phone) in existing_digits:
        return JSONResponse({"ok": False, "reason": "number already exists"}, status_code=409)
    details = body.get("details")
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except Exception:
            details = {"info": details} if details.strip() else {}
    if not isinstance(details, dict):
        details = {}
    group_id = body.get("group_id")
    try:
        group_id = int(group_id) if group_id not in (None, "", "none") else None
    except (TypeError, ValueError):
        group_id = None
    contact = _make_contact(phone, (body.get("name") or "").strip(),
                            (body.get("notes") or "").strip(),
                            gender=(body.get("gender") or ""),
                            details=details, group_id=group_id)
    contacts.append(contact)
    _save_outbound(contacts, owner)
    from shared.audit import record as _audit
    _audit(owner, "contact_add", phone)
    return JSONResponse({"ok": True, "contact": contact})


@app.post("/api/outbound/contacts/upload")
async def outbound_upload(request: Request) -> JSONResponse:
    """Accept multipart file upload (CSV / TXT / XLSX) and bulk-add phone numbers."""
    from fastapi import UploadFile
    import io as _io

    content_type = request.headers.get("content-type", "")
    if "multipart" not in content_type:
        return JSONResponse({"ok": False, "reason": "multipart/form-data required"}, status_code=400)

    form = await request.form()
    file_field = form.get("file")
    if not file_field:
        return JSONResponse({"ok": False, "reason": "no file field"}, status_code=400)

    filename = getattr(file_field, "filename", "upload.txt") or "upload.txt"
    raw = await file_field.read()

    owner = _acting_owner(request)
    # Import straight into a group. Without this every CSV landed in the
    # implicit No-group bucket, so the only way to fill "Marketing" from a
    # spreadsheet was to import and then re-file every row by hand.
    group_id = _resolve_group_id(owner, form.get("group_id"),
                                 form.get("group_name"))
    rows, parse_errors = _parse_uploaded_file(filename, raw)
    if not rows:
        return JSONResponse({"ok": False, "reason": f"No valid phone numbers found in file. parse_errors={parse_errors}"})

    from shared.phone import significant_digits
    contacts = _load_outbound(owner)
    existing_digits = {significant_digits(c["phone"]) for c in contacts}
    added, dupes = 0, 0
    # Re-filing an existing contact is not an import, but "import my Marketing
    # list" plainly means those people end up in Marketing — so a duplicate is
    # still counted as a duplicate and additionally moved into the group.
    moved = 0
    if group_id is not None:
        by_digits = {significant_digits(c["phone"]): c for c in contacts}
    for row in rows:
        digits = significant_digits(row["phone"])
        if digits in existing_digits:
            dupes += 1
            if group_id is not None:
                existing = by_digits.get(digits)
                if existing is not None and existing.get("group_id") != group_id:
                    existing["group_id"] = group_id
                    moved += 1
            continue
        contacts.append(_make_contact(row["phone"], row.get("name", ""),
                                      row.get("notes", ""),
                                      gender=row.get("gender", ""),
                                      details=row.get("details") or {},
                                      group_id=group_id))
        existing_digits.add(digits)
        added += 1
        time.sleep(0.001)  # ensure unique IDs

    _save_outbound(contacts, owner)
    from shared.audit import record as _audit
    _audit(owner, "contacts_upload", filename,
           {"added": added, "duplicates": dupes, "group_id": group_id})
    return JSONResponse({"ok": True, "added": added, "duplicates": dupes,
                          "moved": moved, "group_id": group_id,
                          "parse_errors": parse_errors, "total_in_file": len(rows)})


@app.delete("/api/outbound/contacts/all")
async def outbound_delete_all(request: Request) -> JSONResponse:
    _save_outbound([], _acting_owner(request))
    return JSONResponse({"ok": True})


@app.delete("/api/outbound/contacts/{contact_id}")
async def outbound_delete(contact_id: str, request: Request) -> JSONResponse:
    owner = _acting_owner(request)
    contacts = _load_outbound(owner)
    before = len(contacts)
    contacts = [c for c in contacts if c["id"] != contact_id]
    if len(contacts) == before:
        return JSONResponse({"ok": False, "reason": "not found"}, status_code=404)
    _save_outbound(contacts, owner)
    from shared.audit import record as _audit
    _audit(owner, "contact_delete", contact_id)
    return JSONResponse({"ok": True})


@app.patch("/api/outbound/contacts/{contact_id}")
async def outbound_update(contact_id: str, request: Request) -> JSONResponse:
    owner = _acting_owner(request)
    body = await request.json()
    contacts = _load_outbound(owner)
    for c in contacts:
        if c["id"] == contact_id:
            # Phone is editable — a typo'd number is the single most common
            # reason to edit a contact at all, and re-typing the whole person
            # loses their call history. Same dedupe rule as create: the last 10
            # significant digits, compared against everyone ELSE.
            if "phone" in body:
                from shared.phone import significant_digits
                # Normalized on the way in for the same reason as create: a
                # corrected number is dialled, so it has to carry a country code.
                new_phone = _dialable_phone(body.get("phone"))
                if not new_phone:
                    return JSONResponse(
                        {"ok": False, "reason": "phone required (min 7 digits)"},
                        status_code=400)
                if significant_digits(new_phone) != significant_digits(c["phone"]):
                    clash = {significant_digits(o["phone"]) for o in contacts
                             if o["id"] != contact_id}
                    if significant_digits(new_phone) in clash:
                        return JSONResponse(
                            {"ok": False, "reason": "number already exists"},
                            status_code=409)
                c["phone"] = new_phone
            for field in ("name", "notes", "call_status", "disposition", "call_notes"):
                if field in body:
                    c[field] = (body[field] or "").strip() if isinstance(body[field], str) else body[field]
            if "gender" in body:
                c["gender"] = _normalize_gender(body["gender"] or "")
            if "details" in body:
                d = body["details"]
                if isinstance(d, str):
                    try:
                        d = json.loads(d)
                    except Exception:
                        d = {"info": d} if d.strip() else {}
                c["details"] = d if isinstance(d, dict) else {}
            if "group_id" in body:
                g = body["group_id"]
                try:
                    c["group_id"] = int(g) if g not in (None, "", "none") else None
                except (TypeError, ValueError):
                    c["group_id"] = None
            break
    else:
        return JSONResponse({"ok": False, "reason": "not found"}, status_code=404)
    _save_outbound(contacts, owner)
    # Contact timeline: record the edit (note.added when only notes changed).
    try:
        from shared.db.database import get_db
        from shared.intelligence import timeline
        from shared.phone import significant_digits
        changed = [k for k in ("phone", "name", "notes", "call_status",
                               "disposition", "call_notes", "gender",
                               "details", "group_id")
                   if k in body]
        if changed:
            with get_db() as db:
                cid = timeline.resolve_contact(
                    db, owner, c.get("id"), significant_digits(c.get("phone") or ""))
            if cid is not None:
                kind = ("note.added" if set(changed) <= {"notes", "call_notes"}
                        else "contact.updated")
                timeline.emit(cid, owner, kind, payload={"fields": changed})
    except Exception as exc:
        logger.warning("contact timeline event failed: %r", exc)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Contact groups (phone-book buckets). Contacts with group_id NULL are the
# implicit "No group" bucket — it is not a row in contact_groups.
# ---------------------------------------------------------------------------

_RESERVED_GROUP_NAMES = ("all", "no group", "none", "ungrouped")


def _resolve_group_id(owner: str, raw_id: Any, raw_name: Any = None) -> int | None:
    """A caller-supplied group → an id this owner actually owns, or None.

    Accepts either an existing id or a NAME, creating the group when only a
    name is given. That is what makes "import this CSV into a new group called
    Marketing" one action instead of three, and it is owner-scoped both ways —
    an id belonging to another tenant resolves to None (No group), never to
    their group. Shared with the public API via shared/contacts.py.
    """
    from shared.contacts import resolve_group
    return resolve_group(owner, raw_id, raw_name)


@app.post("/api/outbound/contacts/bulk")
async def outbound_bulk(request: Request) -> JSONResponse:
    """Apply one change to many contacts at once (ids come from the table's
    row selection).

    Actions: `group` (re-file, including into a brand-new named group),
    `disposition`, `delete`. One write of the whole set, because
    _save_outbound is a full sync — issuing N single-contact PATCHes would be
    N load-all/save-all round trips over the same rows.
    """
    owner = _acting_owner(request)
    body = await request.json()
    action = (body.get("action") or "").strip()
    ids = [str(i) for i in (body.get("contact_ids") or []) if str(i).strip()]
    if not ids:
        return JSONResponse({"ok": False, "reason": "contact_ids required"},
                            status_code=400)
    wanted = set(ids)
    contacts = _load_outbound(owner)
    known = sum(1 for c in contacts if c["id"] in wanted)
    if not known:
        return JSONResponse({"ok": False, "reason": "no matching contacts"},
                            status_code=404)

    if action == "delete":
        contacts = [c for c in contacts if c["id"] not in wanted]
        changed = known
    elif action == "group":
        gid = _resolve_group_id(owner, body.get("group_id"), body.get("group_name"))
        changed = 0
        for c in contacts:
            if c["id"] in wanted and c.get("group_id") != gid:
                c["group_id"] = gid
                changed += 1
    elif action == "disposition":
        disp = (body.get("disposition") or "").strip()
        if disp not in DISPOSITIONS:
            return JSONResponse({"ok": False, "reason": "unknown disposition"},
                                status_code=400)
        changed = 0
        for c in contacts:
            if c["id"] in wanted and c.get("disposition") != disp:
                c["disposition"] = disp
                changed += 1
    else:
        return JSONResponse({"ok": False, "reason": f"unknown action {action!r}"},
                            status_code=400)

    _save_outbound(contacts, owner)
    from shared.audit import record as _audit
    _audit(owner, f"contacts_bulk_{action}", ",".join(ids[:20]),
           {"count": known, "changed": changed})
    return JSONResponse({"ok": True, "matched": known, "changed": changed})


@app.get("/api/outbound/groups")
async def contact_groups_list(request: Request) -> JSONResponse:
    owner = _acting_owner(request)
    from shared.db.database import get_db
    with get_db() as db:
        groups = [dict(r) for r in db.execute(
            "SELECT id, name, created_at FROM contact_groups "
            "WHERE owner=? ORDER BY name", (owner,)).fetchall()]
        counts = {r["group_id"]: r["c"] for r in db.execute(
            "SELECT group_id, COUNT(*) AS c FROM contacts "
            "WHERE owner=? GROUP BY group_id", (owner,)).fetchall()}
    for g in groups:
        g["count"] = counts.get(g["id"], 0)
    ungrouped = counts.get(None, 0)
    return JSONResponse({"groups": groups, "ungrouped": ungrouped,
                         "total": sum(counts.values())})


@app.post("/api/outbound/groups")
async def contact_groups_add(request: Request) -> JSONResponse:
    owner = _acting_owner(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "reason": "name required"}, status_code=400)
    if len(name) > 64:
        return JSONResponse({"ok": False, "reason": "name too long"}, status_code=400)
    if name.lower() in _RESERVED_GROUP_NAMES:
        return JSONResponse({"ok": False, "reason": "reserved name"}, status_code=400)
    from shared.db.database import get_db
    with get_db() as db:
        dup = db.execute("SELECT id FROM contact_groups WHERE owner=? AND name=?",
                         (owner, name)).fetchone()
        if dup:
            return JSONResponse({"ok": False, "reason": "group already exists"},
                                status_code=409)
        db.execute("INSERT INTO contact_groups (owner, name) VALUES (?,?)",
                   (owner, name))
        row = db.execute("SELECT id, name FROM contact_groups WHERE owner=? AND name=?",
                         (owner, name)).fetchone()
    from shared.audit import record as _audit
    _audit(owner, "group_create", name)
    return JSONResponse({"ok": True, "group": dict(row)})


@app.patch("/api/outbound/groups/{group_id}")
async def contact_groups_rename(group_id: int, request: Request) -> JSONResponse:
    """Rename a group. Deleting and recreating was the only way to fix a typo,
    and that re-files every member into No group on the way through."""
    owner = _acting_owner(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "reason": "name required"}, status_code=400)
    if len(name) > 64:
        return JSONResponse({"ok": False, "reason": "name too long"}, status_code=400)
    if name.lower() in _RESERVED_GROUP_NAMES:
        return JSONResponse({"ok": False, "reason": "reserved name"}, status_code=400)
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute("SELECT id FROM contact_groups WHERE id=? AND owner=?",
                         (group_id, owner)).fetchone()
        if not row:
            return JSONResponse({"ok": False, "reason": "not found"}, status_code=404)
        dup = db.execute(
            "SELECT id FROM contact_groups WHERE owner=? AND name=? AND id<>?",
            (owner, name, group_id)).fetchone()
        if dup:
            return JSONResponse({"ok": False, "reason": "group already exists"},
                                status_code=409)
        db.execute("UPDATE contact_groups SET name=? WHERE id=?", (name, group_id))
    from shared.audit import record as _audit
    _audit(owner, "group_rename", name, {"group_id": group_id})
    return JSONResponse({"ok": True, "group": {"id": group_id, "name": name}})


@app.delete("/api/outbound/groups/{group_id}")
async def contact_groups_delete(group_id: int, request: Request) -> JSONResponse:
    """Delete a group; its members drop back to the implicit No group."""
    owner = _acting_owner(request)
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute("SELECT id FROM contact_groups WHERE id=? AND owner=?",
                         (group_id, owner)).fetchone()
        if not row:
            return JSONResponse({"ok": False, "reason": "not found"}, status_code=404)
        db.execute("UPDATE contacts SET group_id=NULL WHERE owner=? AND group_id=?",
                   (owner, group_id))
        db.execute("DELETE FROM contact_groups WHERE id=?", (group_id,))
    return JSONResponse({"ok": True})


_FOLLOWUPS_SOURCE = "follow_ups.json"          # historical source tags — keep
_MANUAL_FOLLOWUPS_SOURCE = "manual_followups.json"  # stable so imports stay idempotent


def _load_followups_overrides() -> dict[str, dict]:
    """User-set status overrides: ref -> {status, updated_at}."""
    try:
        from shared.db.database import get_db
        with get_db() as db:
            rows = db.execute(
                "SELECT ref, status, updated_at FROM follow_ups WHERE source=?",
                (_FOLLOWUPS_SOURCE,)).fetchall()
        return {r["ref"]: {"status": r["status"], "updated_at": r["updated_at"]}
                for r in rows}
    except Exception as exc:
        logger.error("followup overrides load failed: %r", exc)
        return {}


def _save_followup_override(ref: str, status: str, owner: str) -> None:
    from shared.db.database import get_db
    with get_db() as db:
        db.execute(
            "INSERT INTO follow_ups (owner, ref, status, source, updated_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(ref) DO UPDATE SET"
            " status=excluded.status, updated_at=excluded.updated_at",
            (owner, ref, status, _FOLLOWUPS_SOURCE, _now_iso()))


def _manual_row_to_dict(r: dict) -> dict:
    note = {}
    try:
        note = json.loads(r["note"]) if r["note"] else {}
    except Exception:
        pass
    return {
        "id": r["ref"].split(":", 1)[1] if ":" in (r["ref"] or "") else r["ref"],
        "contact_name": note.get("contact_name") or "",
        "contact_phone": r["contact_phone"] or note.get("contact_phone") or "",
        "reason": note.get("reason") or "",
        "follow_up_date": r["due_at"] or note.get("follow_up_date"),
        "priority": r["priority"] or note.get("priority") or "medium",
        "call_id": note.get("call_id"),
        "status": r["status"] or "pending",
        "created_at": note.get("created_at") or r["created_at"],
        "updated_at": r["updated_at"],
    }


def _load_manual_followups() -> list[dict]:
    try:
        from shared.db.database import get_db
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM follow_ups WHERE source=? ORDER BY id",
                (_MANUAL_FOLLOWUPS_SOURCE,)).fetchall()
        return [_manual_row_to_dict(dict(r)) for r in rows]
    except Exception as exc:
        logger.error("manual followups load failed: %r", exc)
        return []


def _save_manual_followups(entries: list[dict]) -> None:
    """Replace the manual-followups set (one transaction). Manual follow-ups
    predate multi-tenancy and are platform-global; rows are owned by the
    platform admin, matching what the importer assigned."""
    import auth as _auth
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("DELETE FROM follow_ups WHERE source=?",
                   (_MANUAL_FOLLOWUPS_SOURCE,))
        for e in entries:
            note = {k: e.get(k) for k in
                    ("contact_name", "contact_phone", "reason", "call_id",
                     "created_at") if e.get(k) is not None}
            db.execute(
                "INSERT INTO follow_ups (owner, ref, contact_phone, due_at,"
                " note, status, priority, source, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(ref) DO UPDATE SET"
                " status=excluded.status, updated_at=excluded.updated_at,"
                " note=excluded.note, due_at=excluded.due_at,"
                " priority=excluded.priority",
                (_auth.DEFAULT_OWNER, f"manual:{e['id']}",
                 e.get("contact_phone"), e.get("follow_up_date"),
                 json.dumps(note, ensure_ascii=False),
                 e.get("status") or "pending", e.get("priority") or "medium",
                 _MANUAL_FOLLOWUPS_SOURCE, e.get("updated_at") or _now_iso()))


@app.post("/api/followups/manual")
async def manual_followup_create(request: Request) -> JSONResponse:
    body = await request.json()
    phone = (body.get("contact_phone") or "").strip()
    if not phone:
        return JSONResponse({"ok": False, "reason": "contact_phone is required"}, status_code=400)
    import uuid as _uuid
    entry = {
        "id": _uuid.uuid4().hex[:12],
        "contact_name": (body.get("contact_name") or "").strip(),
        "contact_phone": phone,
        "reason": (body.get("reason") or "").strip(),
        "follow_up_date": body.get("follow_up_date") or None,
        "priority": body.get("priority") or "medium",
        "call_id": body.get("call_id") or None,
        "status": "pending",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    entries = _load_manual_followups()
    entries.append(entry)
    _save_manual_followups(entries)
    return JSONResponse({"ok": True, "id": entry["id"], "entry": entry})


@app.patch("/api/followups/manual/{entry_id}")
async def manual_followup_update(entry_id: str, request: Request) -> JSONResponse:
    body = await request.json()
    entries = _load_manual_followups()
    for e in entries:
        if e["id"] == entry_id:
            for field in ("contact_name", "contact_phone", "reason", "follow_up_date", "priority", "status"):
                if field in body:
                    e[field] = body[field]
            e["updated_at"] = _now_iso()
            _save_manual_followups(entries)
            return JSONResponse({"ok": True, "entry": e})
    return JSONResponse({"ok": False, "reason": "not found"}, status_code=404)


@app.delete("/api/followups/manual/{entry_id}")
async def manual_followup_delete(entry_id: str) -> JSONResponse:
    entries = _load_manual_followups()
    new_entries = [e for e in entries if e["id"] != entry_id]
    if len(new_entries) == len(entries):
        return JSONResponse({"ok": False, "reason": "not found"}, status_code=404)
    _save_manual_followups(new_entries)
    return JSONResponse({"ok": True})


@app.get("/api/followups")
async def followups_list(request: Request, status: str = Query("all")) -> JSONResponse:
    """Merges two real sources of follow-up need:
    1. Outbound contacts whose disposition is to_contact_later/in_doubt
       (set manually, or auto-set after a no_answer/declined call).
    2. Calls that have gone through on-demand AI Call Analysis and were
       flagged follow_up_needed — covers both inbound and outbound calls,
       but only ones a user has chosen to analyze (analysis is on-demand).
    A user-set completion status overrides the default "pending" via
    follow_ups.json, keyed the same way as the entry id below.
    """
    fu_owner = _acting_owner(request)
    overrides = _load_followups_overrides()
    entries: list[dict] = []

    for c in _load_outbound(fu_owner):
        if c.get("disposition") not in ("to_contact_later", "in_doubt"):
            continue
        key = f"contact:{c['id']}"
        entries.append({
            "id": key, "source": "outbound",
            "contact_name": c.get("name") or "", "contact_phone": c.get("phone", ""),
            "follow_up_required": True,
            "reason": f"Outbound disposition: {c.get('disposition')}",
            "follow_up_date": None,
            "priority": "medium",
            "status": overrides.get(key, {}).get("status", "pending"),
            "updated_at": c.get("last_called_at"),
        })

    for folder, analysis in _scan_analyses().items():
        if not analysis.get("follow_up_needed"):
            continue
        key = f"call:{folder}"
        caller = folder.split("_")[0]
        entries.append({
            "id": key, "source": "ai_analysis",
            "contact_name": "", "contact_phone": caller if caller.isdigit() else "",
            "follow_up_required": True,
            "reason": analysis.get("follow_up_reason") or "",
            "follow_up_date": analysis.get("recommended_follow_up_date"),
            "priority": analysis.get("follow_up_priority") or "medium",
            "status": overrides.get(key, {}).get("status", "pending"),
            "updated_at": analysis.get("_analyzed_at"),
            "folder": folder,
        })

    for m in _load_manual_followups():
        key = f"manual:{m['id']}"
        ov_status = overrides.get(key, {}).get("status")
        entries.append({
            "id": key, "source": "manual",
            "contact_name": m.get("contact_name") or "",
            "contact_phone": m.get("contact_phone") or "",
            "follow_up_required": True,
            "reason": m.get("reason") or "",
            "follow_up_date": m.get("follow_up_date"),
            "priority": m.get("priority") or "medium",
            "status": ov_status or m.get("status") or "pending",
            "updated_at": m.get("updated_at"),
            "call_id": m.get("call_id"),
            "_manual_id": m["id"],
        })

    entries = [e for e in entries if e["status"] != "dismissed"]
    if status != "all":
        entries = [e for e in entries if e["status"] == status]
    entries.sort(key=lambda e: e.get("updated_at") or "", reverse=True)
    return JSONResponse({"followups": entries, "total": len(entries)})


@app.patch("/api/followups/{entry_id}")
async def followups_update(entry_id: str, request: Request) -> JSONResponse:
    body = await request.json()
    new_status = body.get("status")
    if new_status not in ("pending", "completed", "missed", "dismissed"):
        return JSONResponse({"ok": False, "reason": "status must be pending/completed/missed/dismissed"}, status_code=400)
    if entry_id.startswith("manual:"):
        manual_id = entry_id.split(":", 1)[1]
        entries = _load_manual_followups()
        for e in entries:
            if e["id"] == manual_id:
                e["status"] = new_status
                e["updated_at"] = _now_iso()
                _save_manual_followups(entries)
                return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "reason": "manual entry not found"}, status_code=404)
    _save_followup_override(entry_id, new_status, _acting_owner(request))
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Follow-Up Automation — schedule an outbound follow-up call for an entry
# already flagged in the Follow-Up Tracker, carrying over what's known about
# the past call (disposition / AI analysis) plus a user-editable note and
# call aim. Dialing reuses the exact same FreeSWITCH originate path as manual
# Outbound Calls (_fs_originate_cmd/_parse_fs_result below) — the live
# conversation engine is untouched, so the call content itself does not
# change; this only automates *placing* the call, not what the agent says.
# ---------------------------------------------------------------------------

def _automation_row_to_dict(r: dict) -> dict:
    return {
        "id": r["legacy_id"],
        "source_id": r["source_id"],
        "owner": r.get("owner") or "",
        "contact_name": r["contact_name"] or "",
        "contact_phone": r["contact_phone"] or "",
        "past_call_context": r["past_call_context"] or "",
        "call_aim": r["call_aim"] or "",
        "script_notes": r["script_notes"] or "",
        "scheduled_time": r["scheduled_time"],
        "linked_contact_id": r["linked_contact_id"],
        "status": r["status"],
        "created_at": r["created_at"],
        "called_at": r["called_at"],
        "call_outcome": r["call_outcome"],
    }


def _load_automation_store() -> list[dict]:
    try:
        from shared.db.database import get_db
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM followup_automations ORDER BY id").fetchall()
        return [_automation_row_to_dict(dict(r)) for r in rows]
    except Exception as exc:
        logger.error("automation store load failed: %r", exc)
        return []


def _get_automation(automation_id: str) -> dict | None:
    from shared.db.database import get_db
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM followup_automations WHERE legacy_id=?",
            (automation_id,)).fetchone()
    return _automation_row_to_dict(dict(row)) if row else None


def _update_automation(automation_id: str, fields: dict,
                       expect_status: str | None = None) -> bool:
    """Targeted column update; with expect_status set this is an atomic
    claim — returns False if another worker already transitioned the row."""
    from shared.db.database import get_db
    cols = ", ".join(f"{k}=?" for k in fields)
    params = list(fields.values()) + [automation_id]
    sql = f"UPDATE followup_automations SET {cols} WHERE legacy_id=?"
    if expect_status is not None:
        sql += " AND status=?"
        params.append(expect_status)
    with get_db() as db:
        cur = db.execute(sql, tuple(params))
        return cur.rowcount > 0


def _build_followup_context(followup_id: str, owner: str) -> dict | None:
    """Pulls together what's already known about why this follow-up exists,
    from whichever real source produced it — never invents call history."""
    if followup_id.startswith("manual:"):
        manual_id = followup_id.split(":", 1)[1]
        entry = next((m for m in _load_manual_followups() if m["id"] == manual_id), None)
        if not entry:
            return None
        notes_parts = []
        if entry.get("reason"):
            notes_parts.append(f"Reason: {entry['reason']}")
        if entry.get("call_id"):
            notes_parts.append(f"Referenced call: {entry['call_id']}")
        if entry.get("follow_up_date"):
            notes_parts.append(f"Target follow-up date: {entry['follow_up_date']}")
        return {
            "contact_name": entry.get("contact_name") or "",
            "contact_phone": entry.get("contact_phone") or "",
            "past_call_context": " ".join(notes_parts) if notes_parts else "Manually added follow-up from call history.",
            "suggested_call_aim": "Follow up with the contact as noted.",
            "linked_contact_id": None,
        }
    if followup_id.startswith("contact:"):
        contact_id = followup_id.split(":", 1)[1]
        contact = next((c for c in _load_outbound(owner) if c["id"] == contact_id), None)
        if not contact:
            return None
        disp = contact.get("disposition", "not_classified")
        aim_by_disp = {
            "to_contact_later": "Re-engage and check current interest/availability.",
            "in_doubt": "Resolve remaining doubts and confirm next step.",
        }
        notes_parts = [f"Outbound disposition from last call: {disp}."]
        if contact.get("notes"):
            notes_parts.append(f"Contact notes: {contact['notes']}")
        if contact.get("call_notes"):
            notes_parts.append(f"Call notes: {contact['call_notes']}")
        return {
            "contact_name": contact.get("name") or "",
            "contact_phone": contact.get("phone", ""),
            "past_call_context": " ".join(notes_parts),
            "suggested_call_aim": aim_by_disp.get(disp, "Re-engage the lead and progress toward a decision."),
            "linked_contact_id": contact_id,
        }
    if followup_id.startswith("call:"):
        folder = followup_id.split(":", 1)[1]
        analysis = _scan_analyses().get(folder)
        if not analysis:
            return None
        caller = folder.split("_")[0]
        notes_parts = [f"Call summary: {analysis.get('summary', '')}",
                       f"Customer intent: {analysis.get('customer_intent', '')}",
                       f"Why follow-up was flagged: {analysis.get('follow_up_reason', '')}"]
        return {
            "contact_name": "", "contact_phone": caller if caller.isdigit() else "",
            "past_call_context": " ".join(p for p in notes_parts if p.strip().split(":")[-1].strip()),
            "suggested_call_aim": analysis.get("suggested_next_action") or "Follow up per AI Call Analysis recommendation.",
            "linked_contact_id": None,
        }
    return None


@app.get("/api/followup-automation")
async def automation_list() -> JSONResponse:
    entries = _load_automation_store()
    entries.sort(key=lambda e: e.get("scheduled_time") or "", reverse=False)
    return JSONResponse({"automations": entries, "total": len(entries)})


@app.post("/api/followup-automation")
async def automation_create(request: Request) -> JSONResponse:
    body = await request.json()
    followup_id = body.get("followup_id")
    if not followup_id:
        return JSONResponse({"ok": False, "reason": "followup_id is required"}, status_code=400)
    ctx = _build_followup_context(followup_id, _acting_owner(request))
    if ctx is None:
        return JSONResponse({"ok": False, "reason": "could not resolve followup_id to a known contact/call"}, status_code=404)

    from shared.db.database import get_db
    import uuid as _uuid
    entry = {
        "id": _uuid.uuid4().hex[:10],
        "source_id": followup_id,
        "contact_name": ctx["contact_name"], "contact_phone": ctx["contact_phone"],
        "past_call_context": ctx["past_call_context"],
        "call_aim": body.get("call_aim") or ctx["suggested_call_aim"],
        "script_notes": body.get("script_notes", ""),
        "scheduled_time": body.get("scheduled_time"),
        "linked_contact_id": ctx["linked_contact_id"],
        "status": "scheduled",
        "created_at": _now_iso(),
        "called_at": None,
        "call_outcome": None,
    }
    with get_db() as db:
        dup = db.execute(
            "SELECT 1 FROM followup_automations WHERE source_id=? AND status='scheduled'",
            (followup_id,)).fetchone()
        if dup:
            return JSONResponse({"ok": False, "reason": "already scheduled for this follow-up"}, status_code=409)
        db.execute(
            "INSERT INTO followup_automations (legacy_id, owner, source_id,"
            " contact_name, contact_phone, past_call_context, call_aim,"
            " script_notes, scheduled_time, linked_contact_id, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry["id"], _acting_owner(request), followup_id,
             entry["contact_name"], entry["contact_phone"],
             entry["past_call_context"], entry["call_aim"],
             entry["script_notes"], entry["scheduled_time"],
             entry["linked_contact_id"], "scheduled", entry["created_at"]))
    return JSONResponse({"ok": True, "automation": entry})


@app.patch("/api/followup-automation/{automation_id}")
async def automation_update(automation_id: str, request: Request) -> JSONResponse:
    body = await request.json()
    entry = _get_automation(automation_id)
    if not entry:
        return JSONResponse({"ok": False, "reason": "not found"}, status_code=404)
    if entry["status"] in ("calling", "called"):
        return JSONResponse({"ok": False, "reason": "already called or in progress — cannot edit"}, status_code=409)
    fields = {f: body[f] for f in ("scheduled_time", "script_notes", "call_aim")
              if f in body}
    if body.get("status") == "cancelled":
        fields["status"] = "cancelled"
    if fields:
        _update_automation(automation_id, fields)
        entry.update(fields)
    return JSONResponse({"ok": True, "automation": entry})


@app.delete("/api/followup-automation/{automation_id}")
async def automation_delete(automation_id: str) -> JSONResponse:
    from shared.db.database import get_db
    with get_db() as db:
        cur = db.execute(
            "DELETE FROM followup_automations WHERE legacy_id=?",
            (automation_id,))
        if cur.rowcount == 0:
            return JSONResponse({"ok": False, "reason": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


async def _dial_automation_entry(automation_id: str) -> None:
    """Places the actual outbound call for one automation entry, reusing the
    same FreeSWITCH originate path as manual Outbound Calls. Does not pass
    past_call_context/script_notes/call_aim into the call in any way — those
    exist for the dashboard operator's reference, not the live agent prompt."""
    entry = _get_automation(automation_id)
    if not entry or entry["status"] != "scheduled":
        return
    phone = re.sub(r"[^\d]", "", entry.get("contact_phone", ""))
    if len(phone) < 7:
        _update_automation(automation_id,
                           {"status": "failed",
                            "call_outcome": "invalid or missing phone number",
                            "called_at": _now_iso()},
                           expect_status="scheduled")
        return

    # Atomic claim: if a concurrent Call Now / scheduler tick got here first,
    # rowcount is 0 and we bail instead of double-dialing.
    if not _update_automation(automation_id, {"status": "calling"},
                              expect_status="scheduled"):
        return
    # v2 fix (plan §4.6): the callback dials AS THE OWNER of the automation
    # row (legacy rows without an owner keep running as the platform admin),
    # so a tenant's callback never goes out with another tenant's identity.
    import auth as _auth
    _cb_owner = entry.get("owner") or _auth.DEFAULT_OWNER
    try:
        _ident = _outbound_identity(_cb_owner)
    except OutboundIdentityError as exc:
        if _cb_owner != _auth.DEFAULT_OWNER:
            _update_automation(automation_id,
                               {"status": "failed",
                                "call_outcome": f"no outbound identity: {exc}",
                                "called_at": _now_iso()})
            return
        _ident = {"caller_id": OUTBOUND_CALLER_ID, "gateway_name": ADMIN_GATEWAY_NAME,
                  "agent_slug": _active_agent_slug(), "source": "admin-global"}
    # Prefer the ORIGINAL call's agent over the globally-active one — the
    # caller expects to hear back from the same agent, not whoever is live.
    _src_agent = ""
    try:
        from shared.db.database import get_db as _get_db
        with _get_db() as _db:
            _row = _db.execute(
                "SELECT agent_slug FROM calls WHERE (call_key=? OR conn_id=?) "
                "AND (owner=? OR ? = '') ORDER BY id DESC LIMIT 1",
                (entry.get("source_id") or "", entry.get("source_id") or "",
                 _cb_owner, entry.get("owner") or "")).fetchone()
            if _row and _row["agent_slug"]:
                _src_agent = _row["agent_slug"]
    except Exception as exc:
        logger.warning("automation source-agent lookup failed: %r", exc)
    # V1 — an automated callback that plays silence is worse than one never
    # placed: the customer picks up expecting the call they were promised.
    _cb_speak_ok, _cb_speak_why = await asyncio.to_thread(_voice_can_speak)
    if not _cb_speak_ok:
        _update_automation(automation_id,
                           {"status": "failed",
                            "call_outcome": f"not placed: {_cb_speak_why}",
                            "called_at": _now_iso()})
        logger.warning("FOLLOWUP_AUTOMATION %s skipped for %s: voice provider "
                       "has no credits", automation_id, _cb_owner)
        return

    # E4 — the automation callback is a real outbound call placed on the
    # tenant's behalf, so it obeys the same quota as one they click.
    _cb_decision = await asyncio.to_thread(
        _billing_check_dial, _cb_owner, "automation")
    if not _cb_decision.allowed:
        _update_automation(automation_id,
                           {"status": "failed",
                            "call_outcome": f"not placed: {_cb_decision.reason}",
                            "called_at": _now_iso()})
        logger.info("FOLLOWUP_AUTOMATION %s skipped for %s: %s",
                    automation_id, _cb_owner, _cb_decision.code)
        return

    _dial_slug = _src_agent or _ident["agent_slug"]
    # WHY-YOU-ARE-CALLING-BACK context for the live agent prompt.
    _ctx_bits = [entry.get("call_aim") or "", entry.get("past_call_context") or ""]
    _context_note = ". ".join(b.strip().rstrip(".") for b in _ctx_bits if b.strip())[:800]
    _record_dial(phone, entry["id"], entry.get("contact_name", ""), agent_slug=_dial_slug,
                 owner=_cb_owner, context_note=_context_note, placed_by="automation")

    try:
        fs_out = await _fs_esl_exec(_fs_originate_cmd(
                phone, agent_slug=_dial_slug,
                caller_id=_ident["caller_id"], gateway_name=_ident["gateway_name"]), timeout=55)
        cs = _parse_fs_result(fs_out, "")
    except Exception as exc:
        logger.error("automation dial %s error: %r", phone, exc)
        cs = "failed"
    finally:
        _billing_release(_cb_decision.reservation)

    _update_automation(automation_id,
                       {"status": "called", "call_outcome": cs,
                        "called_at": _now_iso()})
    if entry.get("linked_contact_id"):
        import auth as _auth
        _update_contact_after_call(entry["linked_contact_id"], cs, _auth.DEFAULT_OWNER)
    logger.info("FOLLOWUP_AUTOMATION %s -> %s (%s)", automation_id, phone, cs)


@app.get("/api/followups/whatsapp-pending")
async def whatsapp_pending(request: Request) -> JSONResponse:
    """Calls where the agent PROMISED WhatsApp media (branch action
    promise_whatsapp_media → collected_data.flags.whatsapp_promised) — the
    fulfillment queue's first consumer (plan §4.3 item 4). Owner-scoped."""
    owner = _acting_owner(request)
    import auth as _auth
    rows: list[dict] = []
    try:
        from shared.db.database import get_db
        with get_db() as db:
            q = ("SELECT conn_id, call_key, phone, contact_id, agent_slug, "
                 "started_at, collected_data, owner FROM calls "
                 "WHERE collected_data LIKE ? ")
            args: list = ['%"whatsapp_promised": true%']
            if not _auth.is_admin(owner):
                q += "AND owner=? "
                args.append(owner)
            q += "ORDER BY started_at DESC LIMIT 200"
            for r in db.execute(q, tuple(args)).fetchall():
                d = dict(r)
                try:
                    flags = (json.loads(d.get("collected_data") or "{}")
                             .get("flags") or {})
                except Exception:
                    flags = {}
                rows.append({
                    "conn_id": d["conn_id"], "call_key": d["call_key"],
                    "phone": d["phone"], "contact_id": d["contact_id"],
                    "agent_slug": d["agent_slug"], "started_at": d["started_at"],
                    "assets": flags.get("whatsapp_assets") or [],
                })
    except Exception as exc:
        logger.error("whatsapp-pending query failed: %r", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "pending": rows})


@app.post("/api/followup-automation/{automation_id}/call-now")
async def automation_call_now(automation_id: str) -> JSONResponse:
    entry = _get_automation(automation_id)
    if not entry:
        return JSONResponse({"ok": False, "reason": "not found"}, status_code=404)
    if entry["status"] != "scheduled":
        return JSONResponse({"ok": False, "reason": f"entry is {entry['status']}, not scheduled"}, status_code=409)
    asyncio.create_task(_dial_automation_entry(automation_id))
    return JSONResponse({"ok": True, "msg": "Calling now…"})


@app.on_event("startup")
async def _start_automation_scheduler() -> None:
    # DELETED, not merely disabled (2026-07-29). The legacy
    # followup_automations scheduler dialled a REAL phone for any 'scheduled'
    # row whose time had passed — no approval, no DND, no calling window, no
    # cap. It had been left in the file as a commented-out
    # `asyncio.create_task(_automation_scheduler_loop())`, which meant the
    # platform was one uncommented line away from automatic dialling that the
    # follow-up approval gate cannot see: it reads followup_automations, a
    # table shared/intelligence/autodial.py never queries.
    #
    # After the 2026-07-29 incident that is not an acceptable thing to leave
    # lying around, so the loop is gone from the source. `git show` it if you
    # ever need it back — but the replacement is the followups engine, where
    # a named human approves each call first.
    #
    # _dial_automation_entry survives: it still backs the operator-triggered
    # POST /api/followup-automation/{id}/call-now, which is a person clicking.
    logger.info("legacy followup-automation auto-dial scheduler REMOVED "
                "(follow-ups require human approval — see migration 0011)")


# ---------------------------------------------------------------------------
# Follow-up auto-dialer
#
# Lives in the API process, not the intel worker, because everything a real
# call needs is here: _outbound_identity (caller ID + trunk + agent, fully
# tenancy-checked), the duplicate-dial guard, _record_dial, and the ESL
# channel. The worker would have to reimplement all of it or punch through the
# auth wall; both are worse than a 2-minute loop next to the code that already
# does this correctly.
#
# Guardrails and the kill switch are in shared/intelligence/autodial.py +
# config.py, and are re-read every tick so INTEL_AUTODIAL_ENABLED=false stops
# it within one poll interval without a restart.
# ---------------------------------------------------------------------------

async def _autodial_once() -> int:
    """One sweep. Returns the number of calls actually placed."""
    from shared.intelligence import autodial, config as intel_config
    from shared.intelligence import notify as intel_notify

    if not intel_config.AUTODIAL_ENABLED:
        return 0

    now = autodial.now_local()
    rows = await asyncio.to_thread(autodial.due_candidates, 200)
    if not rows:
        return 0

    # V1 — checked once for the whole sweep: if the agent cannot speak, not
    # one of these follow-ups should go out. Leaving the rows due means they
    # dial on the next sweep, once credits are back.
    _sweep_speak_ok, _ = await asyncio.to_thread(_voice_can_speak)
    if not _sweep_speak_ok:
        logger.warning("autodial: sweep skipped — voice provider has no "
                       "credits, %d row(s) left due", len(rows))
        return 0

    placed = 0
    # Per-owner caches so one sweep issues a single count query and a single
    # identity resolution per tenant.
    day_counts: dict[str, int] = {}
    identities: dict[str, Any] = {}
    unroutable: dict[str, list] = {}
    # The batch is PER TENANT: `rows` is ordered oldest-first across all
    # tenants, so a global cap would let one account's 30-row backlog stand in
    # front of another account's 3 for an hour. Each tenant gets its own slice
    # of every sweep; AUTODIAL_MAX_PER_DAY is still the hard per-tenant ceiling.
    placed_by_owner: dict[str, int] = {}

    for row in rows:
        owner = row["owner"]
        if placed_by_owner.get(owner, 0) >= intel_config.AUTODIAL_BATCH:
            continue
        if owner not in day_counts:
            day_counts[owner] = await asyncio.to_thread(autodial.placed_today, owner)

        reason = autodial.blocked_reason(row, now=now, placed_today=day_counts[owner])
        if reason:
            if reason in autodial.PERMANENT:
                await asyncio.to_thread(
                    autodial.record_attempt, row["id"], ok=False,
                    detail=autodial.explain(reason, now), permanent=True)
                intel_notify.notify_autodial_blocked(
                    owner, row["id"], row.get("contact_name") or row.get("dial_phone") or "contact",
                    autodial.explain(reason, now))
            elif reason != "not_due":
                # Transient: record WHY so the row explains itself in the UI,
                # but leave it eligible for the next sweep.
                await asyncio.to_thread(
                    autodial.record_attempt, row["id"], ok=False,
                    detail=autodial.explain(reason, now), permanent=False)
            continue

        # The account must have somewhere to call FROM. This is the failure the
        # user cannot otherwise see: without it every follow-up silently ages
        # into 'missed' as though the customer never picked up.
        if owner not in identities:
            try:
                identities[owner] = _outbound_identity(owner)
            except OutboundIdentityError as exc:
                identities[owner] = None
                unroutable[owner] = [str(exc), 0]
        ident = identities[owner]
        if ident is None:
            unroutable[owner][1] += 1
            await asyncio.to_thread(
                autodial.record_attempt, row["id"], ok=False,
                detail=autodial.explain("no_identity"), permanent=False)
            continue

        phone = re.sub(r"[^\d]", "", row.get("dial_phone") or "")
        if len(phone) < 7:
            await asyncio.to_thread(
                autodial.record_attempt, row["id"], ok=False,
                detail="Contact has no usable phone number", permanent=True)
            continue
        # Same guard the manual button gets: a still-ringing leg must not be
        # dialled a second time.
        if _recent_dial_for(phone, owner, within_s=_DUPLICATE_DIAL_WINDOW_S) is not None:
            continue

        # Last line of defence, sitting directly above the code that rings a
        # real phone. due_candidates already filters on approval and
        # blocked_reason re-checks it — this third check is deliberate
        # redundancy, because the cost of being wrong here is a stranger's
        # handset lighting up with an AI on the other end. If it ever fires,
        # something upstream regressed; say so loudly rather than dialling.
        if str(row.get("approval_state") or "") != "approved":
            logger.error(
                "autodial REFUSED follow-up %s (%s): reached the dial path "
                "without human approval (approval_state=%r). The SQL gate "
                "should have excluded this row — treat as a bug.",
                row["id"], phone, row.get("approval_state"))
            await asyncio.to_thread(
                autodial.record_attempt, row["id"], ok=False,
                detail="Refused: no human approval on file", permanent=False)
            continue

        # E3 — automated follow-up calls are a paid capability (Growth+), and
        # they still consume the tenant's minutes. Two separate questions:
        #   * autocall_allowed  — is this tier even entitled to auto-dial?
        #     Checked here as well as at staging (E8), because a row staged
        #     while the tenant was on Growth must not keep dialling after a
        #     downgrade.
        #   * check_dial_allowed — do they have minutes left?
        # Both are recorded as transient attempts so the row explains itself
        # in the UI rather than silently never dialling.
        _ac_ok = await asyncio.to_thread(autodial_billing_allowed, owner)
        if not _ac_ok:
            await asyncio.to_thread(
                autodial.record_attempt, row["id"], ok=False,
                detail="Automated follow-up calls are not included in this "
                       "plan — upgrade to Growth to enable them.",
                permanent=False)
            continue
        _ad_decision = await asyncio.to_thread(
            _billing_check_dial, owner, "autodial")
        if not _ad_decision.allowed:
            await asyncio.to_thread(
                autodial.record_attempt, row["id"], ok=False,
                detail=_ad_decision.reason, permanent=False)
            continue

        slug = ident["agent_slug"]
        # The agent on this call must open like someone who remembers the
        # last conversation: the follow-up reason (and the commitment being
        # chased) travels via call_links.context_note into the bridge's
        # WHY-YOU-ARE-CALLING-BACK prompt band. The prior-call digest is NOT
        # duplicated here — the KNOWN CUSTOMER band already injects it from
        # customer_memory on every outbound call. Cap mirrors the automation
        # path's (server.py _dial_automation_entry).
        ctx = f"Scheduled follow-up call: {row.get('reason') or 'follow up on the previous conversation'}"
        if row.get("commitment_text"):
            ctx += f" Commitment to fulfil: {row['commitment_text']}."
        if row.get("summary"):
            ctx += f" Previous call: {row['summary']}"
        # Structured meta for the OPENING line itself ("we spoke earlier
        # today about X and you'd asked me to call you back") — the topic is
        # the source call's customer_intent, already a short phrase.
        cb_meta = {
            "reason": str(row.get("reason") or "")[:300],
            "topic": str(row.get("source_call_intent") or "")[:120],
            "requested_when": str(row.get("scheduled_at") or ""),
            "source_call_at": str(row.get("source_call_at") or ""),
        }
        _record_dial(phone, str(row.get("contact_handle") or ""),
                     row.get("contact_name") or "", agent_slug=slug, owner=owner,
                     context_note=ctx[:800], followup_id=row["id"],
                     placed_by="autodial", callback_meta=cb_meta)
        await asyncio.to_thread(
            autodial.record_attempt, row["id"], ok=True,
            detail=f"auto-dialled {phone} as {slug}")
        day_counts[owner] += 1
        placed_by_owner[owner] = placed_by_owner.get(owner, 0) + 1
        placed += 1

        async def _place(p=phone, s=slug, i=ident, o=owner,
                         h=row.get("contact_handle"), fid=row["id"],
                         res=_ad_decision.reservation):
            try:
                out = await _fs_esl_exec(_fs_originate_cmd(
                    p, agent_slug=s, caller_id=i["caller_id"],
                    gateway_name=i["gateway_name"]), timeout=55)
                cs = _parse_fs_result(out, "")
            except Exception as exc:
                logger.error("autodial %s error: %r", p, exc)
                cs = "failed"
            finally:
                _billing_release(res)
            if h:
                _update_contact_after_call(h, cs, o)
            # Outcome loop: stamp the originate result. 'answered' stays
            # provisional — stage_followup's completion hook (which sees the
            # analyzed call) is what marks the row completed, because +OK
            # also fires for voicemail pickups.
            try:
                await asyncio.to_thread(autodial.record_result, fid, cs)
            except Exception:
                logger.warning("autodial: result stamp failed for %s", fid,
                               exc_info=True)
            logger.info("AUTODIAL %s from %s → %s", p, i["caller_id"], cs)

        asyncio.create_task(_place())

    for owner, (detail, count) in unroutable.items():
        if count:
            await asyncio.to_thread(
                intel_notify.notify_autodial_unroutable, owner, detail, count)
    return placed


async def _autodial_loop() -> None:
    from shared.intelligence import config as intel_config

    # Stagger the first pass so a restart storm never dials in lockstep.
    await asyncio.sleep(30)
    while True:
        try:
            n = await _autodial_once()
            if n:
                logger.info("autodial: placed %d follow-up call(s)", n)
        except Exception as exc:
            logger.error("autodial sweep failed: %r", exc)
        await asyncio.sleep(max(30, intel_config.AUTODIAL_POLL_S))


@app.on_event("startup")
async def _start_autodialer() -> None:
    from shared.intelligence import config as intel_config

    if not intel_config.AUTODIAL_ENABLED:
        logger.info("follow-up auto-dialer DISABLED (INTEL_AUTODIAL_ENABLED=false)")
        return
    logger.info(
        "follow-up auto-dialer ON — %02d:00-%02d:00 IST, days=%s, "
        "max %d/day, %d attempts, %dmin gap, batch %d",
        intel_config.AUTODIAL_HOUR_START, intel_config.AUTODIAL_HOUR_END,
        sorted(intel_config.autodial_weekdays()), intel_config.AUTODIAL_MAX_PER_DAY,
        intel_config.AUTODIAL_MAX_ATTEMPTS, intel_config.AUTODIAL_RETRY_GAP_MIN,
        intel_config.AUTODIAL_BATCH)
    asyncio.create_task(_autodial_loop())


@app.get("/api/outbound/stats")
async def outbound_stats(request: Request, group: str = Query(None)) -> JSONResponse:
    """Phone-book tallies, optionally narrowed to one group.

    The campaign runner reads this with the group it is about to dial, so the
    "up to N contacts" line under Who-to-call counts the same people the run
    will actually queue. Callers that omit `group` get the whole book, exactly
    as before.

    The exact "how many will this campaign dial" number comes from
    /outbound/campaign/preview, which runs the real planner; these are the
    headline tallies the stat cards show.
    """
    contacts = _filter_by_group(_load_outbound(_acting_owner(request)), group)
    stats: dict = {"total": len(contacts)}
    for s in CALL_STATUSES:
        stats[s] = sum(1 for c in contacts if c.get("call_status") == s)
    disp: dict = {}
    for d in DISPOSITIONS:
        disp[d] = sum(1 for c in contacts if c.get("disposition") == d)
    return JSONResponse({"stats": stats, "dispositions": disp,
                         "group": group or "all"})


# ---------------------------------------------------------------------------
# Outbound dialing + campaign engine
# ---------------------------------------------------------------------------

ACTIVE_AGENT_FILE = LESCALL_ROOT / "calling-agent" / "agents" / "active_agent.json"


def _active_agent_slug() -> str:
    """Slug an outbound dial binds to: the agent active at dial time.

    Travels to the telephony bridge as the lescall_agent_slug channel variable
    (plus the call_links dial record), so switching the active agent mid-campaign
    can't re-route calls already dialed. Best-effort — "" when unreadable.
    """
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT slug FROM agents WHERE is_active=1 "
                "ORDER BY updated_at DESC LIMIT 1").fetchone()
        if row and row["slug"]:
            return re.sub(r"[^A-Za-z0-9_-]", "", str(row["slug"]))
    except Exception as exc:
        logger.error("DB active-agent lookup failed: %r", exc)
    try:  # legacy fallback while active_agent.json is still dual-written
        data = json.loads(ACTIVE_AGENT_FILE.read_text(encoding="utf-8")) or {}
        return re.sub(r"[^A-Za-z0-9_-]", "", str(data.get("active") or ""))
    except Exception:
        return ""


class OutboundIdentityError(Exception):
    """Raised when a tenant cannot legitimately place an outbound call —
    carries a specific, user-facing reason (no number / not applied /
    not verified / no agent)."""


# The FreeSWITCH gateway name for the founding operator's legacy Plivo trunk
# (sip_profiles/external/plivo.xml). Admin outbound egresses through it.
ADMIN_GATEWAY_NAME = os.environ.get("OUTBOUND_GATEWAY_NAME", "plivo-outbound")


_AGENTS_DIR = LESCALL_ROOT / "calling-agent" / "agents"


def _agent_owned_by(slug: str, owner: str) -> bool:
    """True if `slug` exists on disk AND the owner owns it. The founding
    account also owns the legacy pre-tenancy agents that predate the agents
    index (resource ownership, not an admin role — roles are flattened)."""
    if not slug:
        return False
    try:
        import auth
        if not (_AGENTS_DIR / slug / "agent.yaml").exists():
            return False
        if _is_founding_owner(owner):
            return True
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute("SELECT owner FROM agents WHERE slug=?", (slug,)).fetchone()
        return bool(row) and row["owner"] == owner
    except Exception as exc:
        logger.warning("agent ownership check failed for %s/%s: %r", slug, owner, exc)
        return False


def _outbound_identity(owner: str) -> dict:
    """Resolve — and fully validate — the number + trunk + agent a tenant's
    outbound call MUST use. Every leg of the call stays inside the tenant:

      * caller ID  = a number the tenant registered AND verified (verified=1)
      * egress     = that number's OWN trunk (gateway applied in FreeSWITCH),
                     so the tenant's carrier authorises the caller ID + bills it
      * agent      = an agent the tenant OWNS, bound to that number

    Any missing precondition raises OutboundIdentityError with a specific
    reason — the call is REFUSED, never silently placed with someone else's
    number, trunk, or agent. The founding account falls back to its own
    legacy global trunk/agent (pre-tenancy resources it owns) so the
    founding operator keeps working.

    Returns {caller_id, gateway_name, agent_slug, number, source}.
    """
    is_founder = _is_founding_owner(owner)
    try:
        from shared.db.database import get_db
        with get_db() as db:
            # Every outbound-capable number the tenant registered, best first.
            rows = [dict(r) for r in db.execute(
                "SELECT p.number, p.inbound_agent_slug, p.outbound_agent_slug, "
                "t.name AS trunk_name, "
                "t.config_status AS trunk_status, t.owner AS trunk_owner "
                "FROM phone_numbers p JOIN trunks t ON t.id = p.trunk_id "
                "WHERE p.owner=? AND (p.direction='both' OR p.direction='outbound') "
                "ORDER BY p.id", (owner,)).fetchall()]
    except Exception as exc:
        logger.warning("outbound identity lookup failed for %s: %r", owner, exc)
        rows = []

    if not rows:
        if is_founder:
            return {"caller_id": OUTBOUND_CALLER_ID, "gateway_name": ADMIN_GATEWAY_NAME,
                    "agent_slug": _active_agent_slug(), "number": OUTBOUND_CALLER_ID,
                    "source": "admin-global"}
        raise OutboundIdentityError(
            "You have no number to call from. Register your number in the "
            "Telephony panel — calls must originate from your own number.")

    # Walk the tenant's numbers; the first that passes EVERY gate wins.
    # Track why each failed so the error names a concrete next step.
    # Caller-ID ownership is enforced by the CARRIER: because we egress through
    # the tenant's OWN trunk (sofia/gateway/<trunk>), Plivo/Twilio/etc. reject
    # any caller ID the tenant's account isn't authorised to send — no separate
    # platform "verify this number" step (which made no sense for a DID that no
    # human answers). Gates here are only: their own applied trunk + their agent.
    reasons: list[str] = []
    for r in rows:
        num = r["number"]
        if r["trunk_owner"] != owner and not is_founder:
            reasons.append(f"{num}: its trunk belongs to another account")
            continue
        if r["trunk_status"] != "applied":
            reasons.append(f"{num}: press Apply on its trunk in the Telephony panel first")
            continue
        # This is the OUTBOUND leg: use the number's outbound agent. An unbound
        # outbound slot falls back to the inbound agent so a tenant who never
        # touched the new field keeps dialing exactly as before.
        slug = r["outbound_agent_slug"] or r["inbound_agent_slug"] or ""
        if not _agent_owned_by(slug, owner):
            reasons.append(f"{num}: bind one of YOUR agents to this number's OUTBOUND slot in the Telephony panel first")
            continue
        return {
            "caller_id": re.sub(r"[^\d]", "", num),
            "gateway_name": r["trunk_name"],
            "agent_slug": slug,
            "number": num,
            "source": "own-number",
        }

    raise OutboundIdentityError("Cannot place the call yet — " + "; ".join(reasons[:3]))


async def _fs_esl_exec(cmd: str, timeout: float = 55.0) -> str:
    """Run an FS api command over the event socket. fs_cli does not exist in
    the containerized api; ESL on 127.0.0.1:8021 is the same control plane.
    Returns the response body; transport failures come back as '-ERR ...' so
    _parse_fs_result sees exactly what fs_cli would have printed."""
    from shared.fs_esl import fs_api
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(fs_api, cmd, timeout), timeout=timeout + 5)
    except Exception as exc:
        return f"-ERR esl transport failure: {exc!r}"


def _fs_originate_cmd(phone: str, agent_slug: str = "", campaign_id: str = "",
                      caller_id: str = "", gateway_name: str = "") -> str:
    """Build the fs_cli originate — see shared/fs_originate.py.

    The body moved there when the admin plane gained its own "test this number"
    dial: two hand-written copies of this string is how `lescall_dest` (RUNBOOK
    §33) goes missing on one surface and nobody notices for months. This stays
    as the name every caller in this module already uses.
    """
    from shared.fs_originate import originate_cmd
    return originate_cmd(phone, agent_slug=agent_slug, campaign_id=campaign_id,
                         caller_id=caller_id or OUTBOUND_CALLER_ID,
                         gateway_name=gateway_name or ADMIN_GATEWAY_NAME)


# Hangup causes that mean the NUMBER is wrong — the carrier could not route
# it — as opposed to a person (or their network) turning the call away. They
# used to be folded into "declined", which is also what a human pressing
# reject looks like, so a whole list of mis-typed numbers read as twenty people
# who did not want to talk (2026-09-09 and again 2026-09-25, RUNBOOK §58).
_INVALID_NUMBER_CAUSES = ("UNALLOCATED_NUMBER", "UNALLOCATED", "INVALID_NUMBER_FORMAT",
                          "NO_ROUTE_DESTINATION", "NO_ROUTE_TRANSIT_NET")


def _parse_fs_result(stdout: str, stderr: str) -> str:
    """Map fs_cli originate output to a call_status string."""
    out = stdout.strip() + " " + stderr.strip()
    if "+OK" in out:
        return "answered"
    if "NO_ANSWER" in out or "ALLOTTED_TIMEOUT" in out:
        return "no_answer"
    if "USER_BUSY" in out or "BUSY" in out:
        return "busy"
    if any(x in out for x in _INVALID_NUMBER_CAUSES):
        return "invalid_number"
    if any(x in out for x in ("NORMAL_CLEARING", "CALL_REJECTED", "USER_NOT_REGISTERED",
                               "ORIGINATOR_CANCEL", "SUBSCRIBER_ABSENT")):
        return "declined"
    return "failed"


def _update_contact_after_call(contact_id: str, call_status: str, owner: str) -> None:
    contacts = _load_outbound(owner)
    now = datetime.datetime.utcnow().isoformat() + "Z"
    for c in contacts:
        if c["id"] != contact_id:
            continue
        c["call_status"] = call_status
        c["last_called_at"] = now
        c["call_count"] = c.get("call_count", 0) + 1
        hist = c.get("call_history", [])
        hist.append({"ts": now, "call_status": call_status})
        c["call_history"] = hist[-20:]
        # Auto-disposition only if user hasn't already set one
        if c.get("disposition") in ("not_classified", None, ""):
            if call_status == "declined":
                c["disposition"] = "rejected"
            elif call_status == "no_answer":
                c["disposition"] = "to_contact_later"
        break
    _save_outbound(contacts, owner)


def _require_feature(request: Request, feature: str) -> str:
    """E12 — per-tier capability gate for the legacy (non-intel) endpoints.

    Returns the owner so call sites can use it directly. Raises a 402
    BillingRefusal when the plan does not include the capability.
    """
    owner = _acting_owner(request)
    try:
        from shared.billing.entitlements import feature_allowed
        from shared.billing.errors import BillingRefusal, CODE_PLAN_REQUIRED
    except Exception:
        return owner
    if not feature_allowed(owner, feature):
        copy = {
            "recordings": "Call recordings are included in every paid plan. "
                          "Upgrade to listen back to your calls.",
            "transcripts": "Call transcripts are included in Growth. Upgrade "
                           "to search every conversation.",
        }.get(feature, "This feature is not included in your plan.")
        raise BillingRefusal(copy, CODE_PLAN_REQUIRED, feature=feature)
    return owner


def _acting_owner(request: Request) -> str:
    """Email placing this dial — used to pick the caller's OWN number/trunk."""
    try:
        import auth
        return auth.current_owner(request)
    except Exception:
        import auth
        return auth.DEFAULT_OWNER


# ---------------------------------------------------------------------------
# BILLING enforcement at the dial origins (plan §7, E1-E5).
#
# Gate rule: EVERY call site of _fs_esl_exec(_fs_originate_cmd(...)) must be
# preceded by an entitlement check AND by the voice-capability check below.
# There are six origins in this file plus shared/sentinel.py's loopback probe
# (which is exempt from both — it is a synthetic self-test marked billable=0,
# it rings no human, and it plays a pre-recorded WAV rather than TTS).
#
# Admission RESERVES rather than reads: this endpoint returns immediately and
# originates in a background task, so N parallel POSTs to N different contacts
# would otherwise all pass the same "remaining >= 1" check in the same instant.
# The reservation must be released when the call's ledger row closes — the
# bridge does that at teardown; a crashed leg's reservation expires on its own.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# VOICE-CAPABILITY gate at the dial origins (2026-09-08, RUNBOOK §34).
#
# A dial whose agent cannot speak is worse than no dial: the handset rings,
# the customer answers, hears NOTHING for ~25s and hangs up — and the platform
# bills the minutes, marks the contact 'empty_call' and moves on. Two such
# calls went to a real contact on 2026-09-08 while all 28 Sarvam keys sat
# marked dead (6 of them wrongly). Refusing to dial is the only response that
# does not spend the customer's goodwill on a guaranteed-silent call.
#
# The authority is the key watchdog's status file, rewritten every 3s. This
# gate FAILS OPEN on anything unexpected — a missing, stale or unreadable
# status file must never be able to stop a tenant from calling.
# ---------------------------------------------------------------------------
_VOICE_STATUS_FILE = Path(__file__).parent / "sarvam_key_status.json"
_VOICE_STATUS_MAX_AGE_S = 120.0


def _voice_can_speak() -> tuple[bool, str]:
    """(allowed, reason). Only a FRESH, unambiguous 'all keys exhausted'
    blocks a dial."""
    try:
        raw = json.loads(_VOICE_STATUS_FILE.read_text(encoding="utf-8"))
        if not raw.get("all_exhausted"):
            return True, ""
        updated = raw.get("updated") or ""
        age = (datetime.datetime.now()
               - datetime.datetime.fromisoformat(updated)).total_seconds()
        if age > _VOICE_STATUS_MAX_AGE_S:
            logger.warning("voice gate: status file is %.0fs stale — allowing dial", age)
            return True, ""
        return False, (
            "The voice provider has no credits left, so this call would "
            "connect and play silence. Top up Sarvam credits or add a key, "
            "then dial again."
        )
    except Exception as exc:
        logger.warning("voice gate unavailable (%r) — allowing dial", exc)
        return True, ""


def _voice_refusal_response() -> JSONResponse:
    _ok, reason = _voice_can_speak()
    return JSONResponse({"ok": False, "reason": reason, "detail": reason,
                         "code": "voice_unavailable"}, status_code=503)


def _billing_check_dial(owner: str, origin: str):
    """Decision for one outbound origination. Never raises."""
    try:
        from shared.billing.entitlements import check_dial_allowed
        return check_dial_allowed(owner, origin=origin)
    except Exception as exc:
        logger.error("BILLING dial check unavailable (%s): %r", origin, exc)
        from shared.billing.errors import Decision
        return Decision.ok(check="error")


def _billing_release(reservation: str) -> None:
    if not reservation:
        return
    try:
        from shared.billing.entitlements import release_reservation
        release_reservation(reservation)
    except Exception as exc:
        logger.warning("BILLING reservation release failed: %r", exc)


def autodial_billing_allowed(owner: str) -> bool:
    """Is this tenant's plan entitled to automated follow-up calls?

    Kept as a thin wrapper so `shared/intelligence/autodial.py` stays a PURE
    predicate module with no billing import — it is the piece that has to stay
    trivially auditable after the 2026-07-29 incident.
    """
    try:
        from shared.billing.entitlements import autocall_allowed
        return autocall_allowed(owner)
    except Exception as exc:
        logger.error("BILLING autocall check unavailable: %r", exc)
        return True


def _billing_refusal_response(decision) -> JSONResponse:
    """402 in BOTH dialects. `reason` is what the Outbound UI reads; `detail`
    is what FastAPI clients read; `code` is what the SPA switches on. Never
    carries duplicate:true — that is the 409 duplicate-dial shape, and
    ?force=1 must not bypass billing."""
    return JSONResponse(decision.detail | {
        "ok": False, "reason": decision.reason, "detail": decision.reason,
        "code": decision.code, "upgrade": "/billing",
    }, status_code=402)


class DialRefused(Exception):
    """A dial the guardrails declined. `payload`/`status` are the HTTP shape the
    outbound UI already understands, so the endpoint can return it verbatim and
    the scheduler can record `reason` on the schedule row."""

    def __init__(self, payload: dict, status: int = 400):
        super().__init__(payload.get("reason") or "dial refused")
        self.payload = payload
        self.status = status


async def _place_call(owner: str, contact_id: str, *, origin: str,
                      force: bool = False, actor_ip: str | None = None) -> dict:
    """Place ONE outbound call for `owner`, through every dial guardrail.

    The manual Call button and the scheduler both come through here. They MUST:
    a second copy of this sequence is a second place for the duplicate guard,
    the voice-capability gate (V1) or the billing admission (E1) to be
    forgotten, and the gate rule above says every originate call site carries
    all of them. The scheduler is the seventh dial origin; it does not get its
    own rules, it gets these.

    Returns the started-dial payload, or raises DialRefused. Origination itself
    runs in a background task — the caller does not wait 45s for a handset.
    """
    contacts = _load_outbound(owner)
    contact = next((c for c in contacts if c["id"] == contact_id), None)
    if not contact:
        raise DialRefused({"ok": False, "reason": "contact not found"}, 404)
    from shared.audit import record as _audit
    _audit(owner, "dial", contact.get("phone"),
           {"contact_id": contact_id, "origin": origin}, actor_ip)

    phone = re.sub(r"[^\d]", "", contact.get("phone", ""))
    if len(phone) < 7:
        raise DialRefused({"ok": False, "reason": "invalid phone number"}, 400)

    # Duplicate-dial guard. This endpoint returns "dialing" instantly but the
    # handset does not ring for 15-30s, so an operator who sees nothing happen
    # clicks Call again. Both originates then land on the same handset as two
    # concurrent legs, and the carrier/handset drops one mid-conversation —
    # which reads to everyone as the call being cut for no reason.
    # Window tracks originate_timeout (45s) so a still-ringing leg is covered.
    # ?force=1 re-dials anyway, for a genuine immediate retry.
    if not force:
        _recent = _recent_dial_for(phone, owner, within_s=_DUPLICATE_DIAL_WINDOW_S)
        if _recent is not None:
            logger.info("DIAL blocked as duplicate: %s (previous dial %.0fs ago)",
                        phone, _recent)
            raise DialRefused({
                "ok": False, "duplicate": True,
                "reason": f"A call to this contact was already placed "
                          f"{int(_recent)}s ago and may still be connecting. "
                          f"Wait for it to finish, or use Force call to dial anyway.",
                "seconds_ago": int(_recent),
            }, 409)

    # V1 — before billing, so a refused-for-silence dial burns no
    # reservation. A call the agent cannot speak on is not worth placing.
    _speak_ok, _speak_why = _voice_can_speak()
    if not _speak_ok:
        logger.warning("DIAL refused for %s: voice provider has no credits", owner)
        raise DialRefused({"ok": False, "reason": _speak_why, "detail": _speak_why,
                           "code": "voice_unavailable"}, 503)

    # E1 — after the duplicate guard (so a duplicate click does not burn a
    # reservation) and before we resolve an identity or touch FreeSWITCH.
    _decision = _billing_check_dial(owner, origin)
    if not _decision.allowed:
        logger.info("DIAL refused for %s: %s", owner, _decision.code)
        raise DialRefused(_decision.detail | {
            "ok": False, "reason": _decision.reason, "detail": _decision.reason,
            "code": _decision.code, "upgrade": "/billing"}, 402)

    try:
        ident = _outbound_identity(owner)
    except OutboundIdentityError as exc:
        _billing_release(_decision.reservation)
        raise DialRefused({"ok": False, "reason": str(exc), "detail": str(exc)}, 400)

    _dial_slug = ident["agent_slug"]
    _record_dial(phone, contact_id, contact.get("name", ""), agent_slug=_dial_slug,
                 owner=owner, gender=contact.get("gender", ""))

    async def _do_dial():
        fs_out = ""
        try:
            fs_out = await _fs_esl_exec(_fs_originate_cmd(
                    phone, agent_slug=_dial_slug,
                    caller_id=ident["caller_id"], gateway_name=ident["gateway_name"]), timeout=55)
            cs = _parse_fs_result(fs_out, "")
        except Exception as e:
            logger.error("dial %s error: %r", phone, e)
            cs = "failed"
        finally:
            # A leg that never answered consumes no minutes, and one that did
            # is accounted for by its own ledger row — either way the
            # admission hold is done once the originate returns.
            _billing_release(_decision.reservation)
        _update_contact_after_call(contact_id, cs, owner)
        # The carrier's own words next to our bucket, so "was a call even
        # placed?" is answered by this log line rather than the FreeSWITCH log.
        logger.info("DIAL %s from %s (%s, %s) → %s (%s)", phone, ident["caller_id"],
                    ident["source"], origin, cs, " ".join(fs_out.split())[:120])

    asyncio.create_task(_do_dial())
    return {"ok": True, "phone": phone, "status": "dialing",
            "name": contact.get("name", ""), "from": ident["caller_id"]}


@app.post("/api/outbound/dial/{contact_id}")
async def outbound_dial(contact_id: str, request: Request) -> JSONResponse:
    """Trigger a single outbound call. Returns immediately; call runs async.

    The call originates from the ACTING owner's own registered number/trunk as
    that number's agent — never a shared or another tenant's caller ID.

    ?force=1 re-dials past the duplicate guard, for a genuine immediate retry.
    """
    try:
        out = await _place_call(
            _acting_owner(request), contact_id, origin="dial",
            force=request.query_params.get("force") in ("1", "true", "yes"),
            actor_ip=request.client.host if request.client else None)
    except DialRefused as exc:
        return JSONResponse(exc.payload, status_code=exc.status)
    return JSONResponse(out)


@app.post("/api/numbers/{number_id}/test-call")
async def number_test_call(number_id: int, request: Request) -> JSONResponse:
    """End-to-end check of a BYON number: ring the ACCOUNT OWNER's phone
    (Settings → profile phone) FROM this number, over its own trunk, with its
    bound agent live on the line. Replaces the old OTP 'verify' flow — the
    carrier already enforces caller-ID ownership; this proves the whole path
    (trunk → FreeSWITCH → agent) actually works."""
    owner = _acting_owner(request)
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT p.number, p.inbound_agent_slug, p.outbound_agent_slug, "
                "t.name AS trunk_name, "
                "t.config_status AS trunk_status, t.owner AS trunk_owner "
                "FROM phone_numbers p LEFT JOIN trunks t ON t.id = p.trunk_id "
                "WHERE p.id=? AND p.owner=?", (number_id, owner)).fetchone()
            user = db.execute("SELECT phone FROM users WHERE email=?",
                              (owner,)).fetchone()
    except Exception as exc:
        logger.error("test-call lookup failed: %r", exc)
        return JSONResponse({"ok": False, "reason": "lookup failed"}, status_code=500)

    if not row:
        return JSONResponse({"ok": False, "reason": "number not found"}, status_code=404)
    if not row["trunk_name"] or (row["trunk_owner"] != owner and not _is_founding_owner(owner)):
        return JSONResponse({"ok": False, "reason": "attach this number to your trunk first"},
                            status_code=400)
    if row["trunk_status"] != "applied":
        return JSONResponse({"ok": False,
                             "reason": "press Apply on this number's trunk first"},
                            status_code=400)
    # The test call is placed FROM this number, so it exercises the OUTBOUND
    # binding — which is the one worth proving before a campaign runs.
    slug = row["outbound_agent_slug"] or row["inbound_agent_slug"] or ""
    if not _agent_owned_by(slug, owner):
        return JSONResponse({"ok": False,
                             "reason": "bind one of your agents to this number first"},
                            status_code=400)

    dest = re.sub(r"[^\d]", "", (user["phone"] if user else "") or "")
    if len(dest) < 7:
        return JSONResponse({"ok": False, "code": "no_profile_phone",
                             "reason": "Add your phone number in Settings first — "
                                       "the test call rings YOUR phone."},
                            status_code=400)

    # E5 — a test call is a real call on a real trunk with a real agent on the
    # line, so it meters and it counts against quota exactly like any other.
    _speak_ok, _ = _voice_can_speak()
    if not _speak_ok:
        logger.warning("TEST CALL refused for %s: voice provider has no credits", owner)
        return _voice_refusal_response()

    _decision = _billing_check_dial(owner, "test")
    if not _decision.allowed:
        return _billing_refusal_response(_decision)

    caller = re.sub(r"[^\d]", "", row["number"])
    _record_dial(dest, f"test-call-{number_id}", "Test call (you)",
                 agent_slug=slug, owner=owner)

    async def _do_test():
        try:
            fs_out = await _fs_esl_exec(_fs_originate_cmd(
                    dest, agent_slug=slug,
                    caller_id=caller, gateway_name=row["trunk_name"]), timeout=55)
            cs = _parse_fs_result(fs_out, "")
        except Exception as e:
            logger.error("test-call %s error: %r", dest, e)
            cs = "failed"
        finally:
            _billing_release(_decision.reservation)
        logger.info("TEST-CALL %s from %s (agent=%s, owner=%s) → %s",
                    dest, caller, slug, owner, cs)

    asyncio.create_task(_do_test())
    return JSONResponse({"ok": True, "status": "calling", "to": dest,
                         "from": row["number"], "agent": slug,
                         "message": f"Calling your phone (+{dest}) from {row['number']} "
                                    f"now — pick up to talk to '{slug}'."})


def _is_founding_owner(owner: str) -> bool:
    """The founding account owns the legacy pre-tenancy resources (the shared
    Plivo trunk, agents without an index row). Resource ownership only —
    roles are flattened and no account has admin status."""
    try:
        import auth
        return bool(owner) and owner == auth.DEFAULT_OWNER
    except Exception:
        return False


# ── Campaign engine ────────────────────────────────────────────────────────
#
# A campaign is the only path in the platform that dials a LIST of people from
# one click, so a mistake here is a mistake times N. After the 2026-07-29
# autodial incident (RUNBOOK §16) it obeys the same three physical limits the
# follow-up dialer obeys: never a do-not-call contact, never outside calling
# hours, never an unbounded number of calls.
CAMPAIGN_MAX_CALLS = int(os.environ.get("CAMPAIGN_MAX_CALLS", "200"))
CAMPAIGN_MIN_DELAY_S = float(os.environ.get("CAMPAIGN_MIN_DELAY_SECONDS", "3"))


def _campaign_audience(contacts: list[dict], filter_cs: str,
                       disposition: str | None = None) -> list[dict]:
    """Resolve the audience picker to the contacts it names.

    The picker mixes two different columns: 'not_called'/'no_answer' are
    call_status values, 'to_contact_later' is a disposition. Matching only
    call_status — as this did until 2026-07-29 — made the "To contact later"
    audience resolve to zero while the UI counted dozens, and the start call
    then returned ok:false with HTTP 200, which the SPA toasted as success.

    `disposition` is an INDEPENDENT second axis, so the campaign can express
    the same thing the phone-book filter row does ("answered AND interested").
    Before it existed the two surfaces could not name the same audience, and
    the filter you had just narrowed to was not the filter you could dial.
    """
    out = list(contacts)
    if filter_cs and filter_cs != "all":
        if filter_cs in DISPOSITIONS:
            out = [c for c in out if c.get("disposition") == filter_cs]
        else:
            out = [c for c in out if c.get("call_status") == filter_cs]
    if disposition and disposition != "all":
        out = [c for c in out if c.get("disposition") == disposition]
    return out


def _campaign_limits(body: dict) -> tuple[float, int]:
    """(delay between calls, hard ceiling on calls) for one campaign.

    Both are clamped server-side. The form used to send delay_seconds:0 and
    max_calls:0 ("no limit") straight through, which is an unbounded burst of
    dials from one click.
    """
    delay = max(CAMPAIGN_MIN_DELAY_S, float(body.get("delay_seconds") or 0))
    asked = int(body.get("max_calls") or 0)
    return delay, (CAMPAIGN_MAX_CALLS if asked <= 0
                   else min(asked, CAMPAIGN_MAX_CALLS))


def _campaign_plan(contacts: list[dict], filter_cs: str, max_calls: int,
                   group: str | None = None,
                   contact_ids: list[str] | None = None,
                   disposition: str | None = None,
                   ) -> tuple[list[dict], int, bool]:
    """Who this campaign will actually dial → (to_call, dnd_skipped, capped).

    Three ways to name an audience, narrowest first:

      * `contact_ids` — an explicit selection made in the table. It wins
        outright: someone who ticked eight rows means those eight, and the
        status/group pickers are not a second opinion on that.
      * `group` — one phone-book group ("Marketing"), crossed with…
      * `filter_cs` + `disposition` — the same two axes the phone-book filter
        row offers, so "what I am looking at" and "what I will dial" are the
        same sentence.

    Do-not-call is applied BEFORE the cap, so a list full of rejected contacts
    can never silently consume the whole budget, and 'rejected' as an audience
    resolves to nobody rather than to exactly the people who opted out. It also
    applies to an explicit selection: ticking a row is not consent from the
    person on the other end.
    """
    if contact_ids:
        wanted = list(dict.fromkeys(contact_ids))          # de-dupe, keep order
        by_id = {c["id"]: c for c in contacts}
        audience = [by_id[i] for i in wanted if i in by_id]
    else:
        audience = _campaign_audience(_filter_by_group(contacts, group),
                                      filter_cs, disposition)
    dnd_skipped = sum(1 for c in audience if c.get("disposition") == "rejected")
    to_call = [c for c in audience if c.get("disposition") != "rejected"]
    capped = len(to_call) > max_calls
    return to_call[:max_calls], dnd_skipped, capped


def _contact_is_dnd(db_id: Any, owner: str) -> bool:
    """Whether this contact is on do-not-call, read fresh from the database.

    Fails CLOSED — if we cannot establish that someone is callable, we do not
    call them. Same direction every guardrail added on 2026-07-29 points; see
    shared/intelligence/autodial.blocked_reason.
    """
    if not db_id:
        logger.error("campaign DND check: contact has no db_id — skipping the "
                     "call rather than dialling an unverifiable number")
        return True
    try:
        from shared.db.database import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT disposition FROM contacts WHERE id=? AND owner=?",
                (int(db_id), owner)).fetchone()
    except Exception as exc:
        logger.error("campaign DND check failed for contact %s: %r — skipping",
                     db_id, exc)
        return True
    # Deleted mid-run: deletion is not a do-not-call signal, and the operator's
    # queued list is the record of what they asked for.
    return bool(row) and (row["disposition"] or "") == "rejected"


@app.get("/api/outbound/campaign/status")
async def campaign_status(request: Request) -> JSONResponse:
    # The CALLER's own campaign only — never another tenant's live state.
    # The limits ride along so the form can state the real ceiling instead of
    # promising "0 = no limit", which stopped being true on 2026-07-29.
    return JSONResponse({**_campaign_for(_acting_owner(request)),
                         "max_calls_limit": CAMPAIGN_MAX_CALLS,
                         "min_delay_seconds": CAMPAIGN_MIN_DELAY_S})


def _campaign_request(body: dict) -> dict:
    """The audience half of a campaign body, normalised once.

    Start and preview MUST read the request identically or the number on the
    button is not the number that dials.
    """
    raw_ids = body.get("contact_ids") or []
    if not isinstance(raw_ids, list):
        raw_ids = []
    return {
        "filter_cs": body.get("call_status_filter") or "not_called",
        "disposition": body.get("disposition_filter") or None,
        "group": body.get("group") or None,
        "contact_ids": [str(i) for i in raw_ids
                        if str(i).strip()][:CAMPAIGN_MAX_CALLS],
    }


@app.post("/api/outbound/campaign/preview")
async def campaign_preview(request: Request) -> JSONResponse:
    """Exactly who a start with this body would dial — no side effects.

    Runs the real planner rather than re-deriving a count in the browser, so
    the "up to N contacts" line under Who-to-call cannot drift from what the
    engine queues (it did: the form counted a raw status tally while the
    engine also dropped do-not-call and applied the per-run cap).
    """
    owner = _acting_owner(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    req = _campaign_request(body if isinstance(body, dict) else {})
    _, max_calls = _campaign_limits(body if isinstance(body, dict) else {})
    contacts = _load_outbound(owner)
    to_call, dnd_skipped, capped = _campaign_plan(
        contacts, req["filter_cs"], max_calls, group=req["group"],
        contact_ids=req["contact_ids"], disposition=req["disposition"])
    # Matched-before-cap, so the UI can say "200 of 640" rather than just 200.
    matched, _, _ = _campaign_plan(
        contacts, req["filter_cs"], 10 ** 9, group=req["group"],
        contact_ids=req["contact_ids"], disposition=req["disposition"])
    return JSONResponse({"ok": True, "will_dial": len(to_call),
                         "matched": len(matched), "dnd_skipped": dnd_skipped,
                         "capped": capped, "max_calls": max_calls,
                         "max_calls_limit": CAMPAIGN_MAX_CALLS})


class CampaignRefused(Exception):
    """A campaign the guardrails declined — same shape contract as DialRefused,
    so the endpoint returns it verbatim and the scheduler records `reason`."""

    def __init__(self, payload: dict, status: int = 400):
        super().__init__(payload.get("reason") or "campaign refused")
        self.payload = payload
        self.status = status


async def _campaign_launch(camp_owner: str, body: dict, *, origin: str,
                           actor_ip: str | None = None) -> dict:
    """Plan and start ONE campaign run for `camp_owner`.

    The Start button and the scheduler both come through here, for the reason
    `_place_call` exists: this function is a stack of guardrails — calling
    hours, do-not-call, per-call voice capability, per-call billing admission,
    quota truncation, the inter-call delay — and a scheduled campaign that
    reimplemented them would be one edit away from obeying a different set. A
    campaign is the dial origin that MULTIPLIES; it is the last one that should
    have two implementations.

    Returns the started-run summary, or raises CampaignRefused.
    """
    camp = _campaign_for(camp_owner)
    if camp.get("running"):
        raise CampaignRefused(
            {"ok": False, "reason": "You already have a campaign running"}, 409)
    from shared.audit import record as _audit
    _audit(camp_owner, "campaign_start", None, {"origin": origin}, actor_ip)

    from shared.intelligence import autodial, config as intel_config

    if body.get("contact_ids") is not None and not isinstance(body["contact_ids"], list):
        raise CampaignRefused({"ok": False, "reason": "contact_ids must be a list"}, 400)
    # Audience: a call status, a disposition, one group, or an explicit
    # tick-the-rows selection (strings — that is the handle the whole outbound
    # surface keys on: legacy_id, or "db<pk>").
    req = _campaign_request(body)
    filter_cs, disposition = req["filter_cs"], req["disposition"]
    group, contact_ids = req["group"], req["contact_ids"]
    # A floor and a ceiling, not suggestions: dialling with no gap puts a burst
    # of legs on one trunk and reads to the carrier as spam.
    delay_secs, max_calls = _campaign_limits(body)

    # Calling hours — the same window the follow-up dialer obeys. A bulk
    # campaign is the last thing that should ever ring someone at 6am.
    now_ist = autodial.now_local()
    if not autodial.within_window(now_ist):
        opens = autodial.next_window_open(now_ist)
        raise CampaignRefused({"ok": False, "code": "outside_hours", "reason":
            f"Outside calling hours ({intel_config.AUTODIAL_HOUR_START:02d}:00–"
            f"{intel_config.AUTODIAL_HOUR_END:02d}:00 IST, Mon–Sat). "
            f"Campaigns can start again {opens.strftime('%a %d %b at %H:%M')} IST."},
            400)

    # Do-not-call is not a preference this screen can override: a contact
    # marked 'rejected' asked us to stop.
    to_call, dnd_skipped, capped = _campaign_plan(
        _load_outbound(camp_owner), filter_cs, max_calls,
        group=group, contact_ids=contact_ids, disposition=disposition)

    if not to_call:
        reason = ("Every contact in this audience is marked Rejected — they "
                  "asked not to be called") if dnd_skipped else \
                 ("None of the selected contacts could be found"
                  if contact_ids else "No contacts match the filter")
        # 400, not 200: an ok:false body with a success status was toasted by
        # the SPA as "Campaign started" while nothing had been queued.
        raise CampaignRefused({"ok": False, "reason": reason,
                               "dnd_skipped": dnd_skipped}, 400)

    # Resolve the caller's own number/trunk ONCE, up front — the whole campaign
    # dials from it, and we refuse to start if they have no number of their own.
    try:
        camp_ident = _outbound_identity(camp_owner)
    except OutboundIdentityError as exc:
        raise CampaignRefused({"ok": False, "reason": str(exc), "detail": str(exc)}, 400)

    # E2 — the sixth dial origin, and the one that multiplies. A campaign is
    # the only path that dials a LIST from one click, so an unchecked campaign
    # is a quota bypass times N. Two gates:
    #   * one admission check up front, so a tenant with no minutes cannot
    #     start a run at all; and
    #   * the audience is TRUNCATED to what the remaining balance can pay for,
    #     so a 5,000-contact list on a 30-minute plan queues 30 calls, not
    #     5,000 that fail one by one on the trunk.
    _camp_decision = _billing_check_dial(camp_owner, origin)
    if not _camp_decision.allowed:
        raise CampaignRefused(_camp_decision.detail | {
            "ok": False, "reason": _camp_decision.reason,
            "detail": _camp_decision.reason, "code": _camp_decision.code,
            "upgrade": "/billing"}, 402)
    _billing_release(_camp_decision.reservation)   # the per-call gate reserves

    _quota_capped = 0
    try:
        from shared.billing.entitlements import (
            enforcement_enabled as _enf, get_entitlements as _get_ent,
            minutes_used as _mins_used, period_bounds as _pbounds)
        if _enf():
            _ent = _get_ent(camp_owner)
            if _ent.get("minutes_limit") is not None:
                _remaining = max(0, int(_ent["minutes_limit"])
                                 - _mins_used(camp_owner, *_pbounds(_ent)))
                if len(to_call) > _remaining:
                    _quota_capped = len(to_call) - _remaining
                    to_call = to_call[:_remaining]
    except Exception as exc:
        logger.error("BILLING campaign sizing failed: %r", exc)

    if not to_call:
        raise CampaignRefused({
            "ok": False, "code": "quota_minutes", "upgrade": "/billing",
            "reason": "You have no minutes left this period, so no calls were "
                      "queued. Add minutes or upgrade to run this campaign.",
            "detail": "no minutes remaining"}, 402)

    import uuid as _uuid
    cid = _uuid.uuid4().hex[:8]
    camp.update({
        "running": True, "campaign_id": cid,
        "total": len(to_call), "completed": 0,
        "answered": 0, "no_answer": 0, "busy": 0,
        "declined": 0, "invalid_number": 0, "failed": 0, "skipped": 0,
        "dnd_skipped": dnd_skipped, "capped": capped,
        "current_phone": "", "current_name": "",
        "started_at": datetime.datetime.utcnow().isoformat() + "Z",
        "stopped": False, "error": None,
        # What this run was aimed at, so the live panel and the last-run
        # summary can say WHICH audience produced these numbers instead of
        # showing tallies with no subject.
        "audience": ("selection" if contact_ids else filter_cs),
        "audience_disposition": (None if contact_ids else disposition),
        "audience_group": group or "all",
        "audience_size": len(to_call),
    })
    if dnd_skipped or capped:
        logger.info("CAMPAIGN [%s owner=%s] queued %s · %s on do-not-call skipped"
                    " · cap %s%s", cid, camp_owner, len(to_call), dnd_skipped,
                    max_calls, " (truncated)" if capped else "")

    async def _run():
        try:
            for contact in to_call:
                if camp.get("stopped"):
                    break
                # Hours are re-checked every iteration: a long run started at
                # 18:55 must not still be dialling people at 20:30.
                if not autodial.within_window(autodial.now_local()):
                    remaining = camp["total"] - camp["completed"]
                    camp["stopped"] = True
                    camp["error"] = (
                        f"Stopped at {intel_config.AUTODIAL_HOUR_END:02d}:00 IST — "
                        f"calling hours ended with {remaining} contacts undialled.")
                    logger.info("CAMPAIGN [%s] halted at window close, %s undialled",
                                cid, remaining)
                    break

                contact_id = contact["id"]
                phone = re.sub(r"[^\d]", "", contact.get("phone", ""))
                if len(phone) < 7:
                    camp["skipped"] += 1
                    camp["completed"] += 1
                    continue

                # Do-not-call re-read from the DB, not from the start-time
                # snapshot: someone who says "stop calling me" on call 3 must
                # not be dialled again on call 40 of the same run.
                if await asyncio.to_thread(_contact_is_dnd, contact.get("db_id"), camp_owner):
                    logger.info("CAMPAIGN [%s] skipping %s — do-not-call", cid, phone)
                    camp["dnd_skipped"] = camp.get("dnd_skipped", 0) + 1
                    camp["skipped"] += 1
                    camp["completed"] += 1
                    continue

                # V1 (per call), same reasoning as E2 below: a campaign that
                # runs into a credit wall must HALT, not keep dialling a list
                # of real people who will each answer to silence.
                _c_speak_ok, _c_speak_why = await asyncio.to_thread(_voice_can_speak)
                if not _c_speak_ok:
                    camp["stopped"] = True
                    camp["error"] = _c_speak_why
                    camp["error_code"] = "voice_unavailable"
                    logger.warning("CAMPAIGN [%s] halted — voice provider has no "
                                   "credits, %s undialled", cid,
                                   camp["total"] - camp["completed"])
                    break

                # E2 (per call). Re-checked every iteration, not just at start:
                # a long run burns minutes as it goes, and the whole point is
                # that call 40 must not go out on a balance that ran dry at
                # call 12.
                _c_decision = await asyncio.to_thread(
                    _billing_check_dial, camp_owner, origin)
                if not _c_decision.allowed:
                    remaining = camp["total"] - camp["completed"]
                    camp["stopped"] = True
                    camp["error"] = _c_decision.reason
                    camp["error_code"] = _c_decision.code
                    logger.info("CAMPAIGN [%s] halted on %s, %s undialled",
                                cid, _c_decision.code, remaining)
                    break

                camp["current_phone"] = phone
                camp["current_name"] = contact.get("name", "")
                logger.info("CAMPAIGN [%s owner=%s] dialing %s (%s/%s)",
                            cid, camp_owner, phone, camp["completed"] + 1, camp["total"])
                _dial_slug = camp_ident["agent_slug"]
                _record_dial(phone, contact_id, contact.get("name", ""),
                             agent_slug=_dial_slug, owner=camp_owner,
                             gender=contact.get("gender", ""))
                try:
                    fs_out = await _fs_esl_exec(_fs_originate_cmd(
                            phone, agent_slug=_dial_slug,
                            caller_id=camp_ident["caller_id"],
                            gateway_name=camp_ident["gateway_name"]), timeout=55)
                    cs = _parse_fs_result(fs_out, "")
                    # The carrier's own words, next to our bucket — the line
                    # that answers "was a call even placed?" without opening
                    # the FreeSWITCH log.
                    logger.info("CAMPAIGN [%s] %s -> %s (%s)", cid, phone, cs,
                                " ".join(fs_out.split())[:120])
                except asyncio.TimeoutError:
                    cs = "no_answer"
                except Exception as e:
                    logger.error("campaign dial %s error: %r", phone, e)
                    cs = "failed"
                finally:
                    _billing_release(_c_decision.reservation)

                _update_contact_after_call(contact_id, cs, camp_owner)
                key = cs if cs in camp else "failed"
                camp[key] = camp.get(key, 0) + 1
                camp["completed"] += 1

                if not camp.get("stopped") and delay_secs > 0:
                    await asyncio.sleep(delay_secs)
        except Exception as e:
            logger.exception("campaign crashed: %r", e)
            camp["error"] = str(e)
        finally:
            camp["running"] = False
            camp["current_phone"] = ""
            camp["current_name"] = ""
            logger.info("CAMPAIGN [%s] done: %s", cid, {k: camp[k] for k in
                        ("total","completed","answered","no_answer","busy",
                         "declined","invalid_number","failed","skipped","dnd_skipped")})

    _campaign_tasks[camp_owner] = asyncio.create_task(_run())
    return {"ok": True, "campaign_id": cid, "total": len(to_call),
            "dnd_skipped": dnd_skipped, "capped": capped,
            "quota_capped": _quota_capped,
            "max_calls": max_calls, "delay_seconds": delay_secs}


@app.post("/api/outbound/campaign/start")
async def campaign_start(request: Request) -> JSONResponse:
    try:
        out = await _campaign_launch(
            _acting_owner(request), await request.json(), origin="campaign",
            actor_ip=request.client.host if request.client else None)
    except CampaignRefused as exc:
        return JSONResponse(exc.payload, status_code=exc.status)
    return JSONResponse(out)


# ---------------------------------------------------------------------------
# SCHEDULED calls and campaigns (RUNBOOK §40).
#
# The seventh and eighth dial origins, and the only ones with nobody watching.
# Both go through _place_call / _campaign_launch, so they inherit every
# guardrail the Call button and the Start button obey — see those two functions
# for why a second implementation is the thing to avoid here.
#
# What the executor adds on top, because "later" is different from "now":
#   * calling hours are re-checked AT FIRE TIME, not only when the row was
#     created (shared/scheduling.fire_decision);
#   * a schedule the loop reaches too late is MISSED, never fired late — after
#     a deploy or an outage, "everything that is due" is a burst of calls at
#     whatever hour the box came back;
#   * a claimed row is settled exactly once, and a failed one is not retried.
# ---------------------------------------------------------------------------

SCHEDULE_POLL_S = int(os.environ.get("SCHEDULE_POLL_S", "30") or 30)


def _schedule_when(body: dict):
    """The instant a schedule body asks for.

    `when` is an ISO-8601 string; the SPA sends one WITH an offset, so the
    instant is unambiguous. A naive string is read as IST — the operator's
    wall clock — rather than as UTC, which would silently move every schedule
    five and a half hours.
    """
    from shared.scheduling import ScheduleError, to_utc
    raw = (body.get("when") or body.get("scheduled_at") or "").strip()
    if not raw:
        raise ScheduleError("Pick a date and time for this call.")
    when = to_utc(raw)
    if when is None:
        raise ScheduleError(f"{raw!r} is not a date and time we can read.")
    return when


@app.get("/api/outbound/schedule")
async def schedule_list(request: Request) -> JSONResponse:
    """This tenant's scheduled calls and campaigns, pending first."""
    from shared import scheduling
    owner = _acting_owner(request)
    try:
        rows = await asyncio.to_thread(scheduling.upcoming, owner, limit=200)
    except Exception as exc:
        logger.error("schedule list failed for %s: %r", owner, exc)
        return JSONResponse({"ok": False, "reason": "could not load schedules"},
                            status_code=500)
    # The contact may have been renamed or deleted since; resolve names now so
    # the list says who, not just a handle.
    names = {c["id"]: c for c in _load_outbound(owner)}
    out = []
    for r in rows:
        c = names.get(r.get("contact_handle") or "")
        out.append(r | {
            "contact_name": (c or {}).get("name") or "",
            "contact_phone": (c or {}).get("phone") or r.get("phone") or "",
            "contact_missing": bool(r.get("kind") == "call" and c is None),
            "status_text": scheduling.explain(r.get("status") or "", r),
        })
    return JSONResponse({"ok": True, "schedules": out,
                         "hours": {"start": intel_config_hours()[0],
                                   "end": intel_config_hours()[1]}})


def intel_config_hours() -> tuple[int, int]:
    from shared.intelligence import config as intel_config
    return intel_config.AUTODIAL_HOUR_START, intel_config.AUTODIAL_HOUR_END


@app.post("/api/outbound/schedule")
async def schedule_create(request: Request) -> JSONResponse:
    """Schedule one call, or one campaign run, for a time.

    kind='call'     needs contact_id (the outbound surface's string handle).
    kind='campaign' takes the SAME body /campaign/start takes, under
                    `audience` — so what is scheduled is exactly what a Start
                    would have done, and the planner has one input shape.
    """
    from shared import scheduling
    from shared.scheduling import ScheduleError

    owner = _acting_owner(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    kind = (body.get("kind") or "call").strip()

    try:
        when = _schedule_when(body)
        if kind == "call":
            handle = str(body.get("contact_id") or "").strip()
            contact = next((c for c in _load_outbound(owner) if c["id"] == handle), None)
            if contact is None:
                return JSONResponse({"ok": False, "reason": "contact not found"},
                                    status_code=404)
            # Refuse at CREATE time what we would refuse at dial time. A
            # do-not-call contact must not be schedulable — a row that sits
            # looking scheduled and silently skips is worse than a refusal the
            # operator can see.
            if (contact.get("disposition") or "") == "rejected":
                return JSONResponse({"ok": False, "reason":
                    f"{contact.get('name') or handle} is marked Rejected — they "
                    f"asked not to be called."}, status_code=400)
            row = await asyncio.to_thread(
                scheduling.create, owner, kind="call", when=when,
                label=(body.get("label") or "").strip(),
                contact_handle=handle, phone=contact.get("phone") or "",
                created_by=owner)
        elif kind == "campaign":
            audience = body.get("audience")
            if not isinstance(audience, dict):
                audience = {}
            # Validate the audience NOW by running the real planner, so an
            # empty-audience campaign is refused while the operator is looking
            # at the form rather than at 10am tomorrow.
            req = _campaign_request(audience)
            _, max_calls = _campaign_limits(audience)
            to_call, _dnd, _capped = _campaign_plan(
                _load_outbound(owner), req["filter_cs"], max_calls,
                group=req["group"], contact_ids=req["contact_ids"],
                disposition=req["disposition"])
            if not to_call:
                return JSONResponse({"ok": False, "reason":
                    "No contacts match this audience, so there would be "
                    "nothing to dial."}, status_code=400)
            row = await asyncio.to_thread(
                scheduling.create, owner, kind="campaign", when=when,
                label=(body.get("label") or "").strip(),
                audience=audience, created_by=owner)
            row["will_dial"] = len(to_call)
        else:
            return JSONResponse({"ok": False, "reason": f"unknown kind {kind!r}"},
                                status_code=400)
    except ScheduleError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    except Exception as exc:
        logger.error("schedule create failed for %s: %r", owner, exc)
        return JSONResponse({"ok": False, "reason": "could not save this schedule"},
                            status_code=500)

    from shared.audit import record as _audit
    _audit(owner, "schedule_create", str(row.get("id")),
           {"kind": kind, "when": str(row.get("scheduled_at"))},
           request.client.host if request.client else None)
    return JSONResponse({"ok": True, "schedule": row})


@app.patch("/api/outbound/schedule/{sid}")
async def schedule_move(sid: int, request: Request) -> JSONResponse:
    """Move a pending schedule to a different time."""
    from shared import scheduling
    from shared.scheduling import ScheduleError

    owner = _acting_owner(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        when = _schedule_when(body if isinstance(body, dict) else {})
        row = await asyncio.to_thread(scheduling.reschedule, owner, sid, when)
    except ScheduleError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    except Exception as exc:
        logger.error("schedule move failed for %s: %r", owner, exc)
        return JSONResponse({"ok": False, "reason": "could not move this schedule"},
                            status_code=500)
    if row is None:
        return JSONResponse({"ok": False, "reason":
            "That schedule has already run or was cancelled."}, status_code=409)
    return JSONResponse({"ok": True, "schedule": row})


@app.delete("/api/outbound/schedule/{sid}")
async def schedule_cancel(sid: int, request: Request) -> JSONResponse:
    from shared import scheduling
    owner = _acting_owner(request)
    try:
        ok = await asyncio.to_thread(scheduling.cancel, owner, sid)
    except Exception as exc:
        logger.error("schedule cancel failed for %s: %r", owner, exc)
        return JSONResponse({"ok": False, "reason": "could not cancel"},
                            status_code=500)
    if not ok:
        return JSONResponse({"ok": False, "reason":
            "That schedule has already run or was cancelled."}, status_code=409)
    from shared.audit import record as _audit
    _audit(owner, "schedule_cancel", str(sid), None,
           request.client.host if request.client else None)
    return JSONResponse({"ok": True})


async def _fire_schedule(row: dict) -> None:
    """Run one claimed schedule and settle it. Never raises."""
    from shared import scheduling
    sid, owner, kind = row["id"], row["owner"], row.get("kind")
    try:
        if kind == "call":
            # force=True: the duplicate guard exists to catch an operator
            # double-clicking Call, and a schedule set days ago is not that.
            # It stays off only for the manual button.
            out = await _place_call(owner, row.get("contact_handle") or "",
                                    origin="schedule", force=True)
            detail = f"dialling {out.get('phone')}"
        elif kind == "campaign":
            out = await _campaign_launch(owner, row.get("audience") or {},
                                         origin="schedule-campaign")
            detail = f"campaign {out.get('campaign_id')} · {out.get('total')} queued"
        else:
            raise ValueError(f"unknown schedule kind {kind!r}")
    except (DialRefused, CampaignRefused) as exc:
        reason = exc.payload.get("reason") or str(exc)
        logger.info("SCHEDULE %s (%s %s) refused: %s", sid, owner, kind, reason)
        await asyncio.to_thread(scheduling.finish, sid, status="failed",
                                result=exc.payload, error=reason)
        return
    except Exception as exc:
        logger.exception("SCHEDULE %s (%s %s) crashed: %r", sid, owner, kind, exc)
        await asyncio.to_thread(
            scheduling.finish, sid, status="failed", result={},
            error=f"The scheduler could not place this call: {exc!r}")
        return
    logger.info("SCHEDULE %s (%s %s) fired — %s", sid, owner, kind, detail)
    await asyncio.to_thread(scheduling.finish, sid, status="done", result=out)


async def _schedule_once() -> int:
    """One pass: claim what is due, fire each, settle each. Returns how many
    fired."""
    from shared import scheduling
    import datetime as _dt
    rows = await asyncio.to_thread(scheduling.claim_due,
                                   _dt.datetime.now(_dt.timezone.utc))
    for row in rows:
        await _fire_schedule(row)
    return len(rows)


async def _schedule_loop() -> None:
    from shared import scheduling
    # A row left 'running' belongs to a process that died mid-dial; nothing
    # else will ever settle it. Swept once at boot, which is when it happened.
    try:
        stranded = await asyncio.to_thread(scheduling.release_running)
        if stranded:
            logger.warning("scheduler: failed %d schedule(s) stranded mid-dial "
                           "by a previous restart", stranded)
    except Exception as exc:
        logger.error("scheduler: stranded-row sweep failed: %r", exc)
    while True:
        try:
            n = await _schedule_once()
            if n:
                logger.info("scheduler: fired %d scheduled item(s)", n)
        except Exception as exc:
            logger.error("scheduler pass failed: %r", exc)
        await asyncio.sleep(max(15, SCHEDULE_POLL_S))


@app.on_event("startup")
async def _start_scheduler() -> None:
    """The schedule lives in Postgres; only this loop lives in the process, so
    a deploy costs at most one poll interval — never a pending schedule."""
    from shared.scheduling import STALE_AFTER
    logger.info("call scheduler ON — polling every %ds, firing within %d min "
                "of the scheduled time", max(15, SCHEDULE_POLL_S),
                int(STALE_AFTER.total_seconds() // 60))
    asyncio.create_task(_schedule_loop())


@app.post("/api/outbound/campaign/stop")
async def campaign_stop(request: Request) -> JSONResponse:
    # Only the caller's OWN campaign — a tenant can't stop another's.
    _campaign_for(_acting_owner(request))["stopped"] = True
    return JSONResponse({"ok": True, "message": "Stop signal sent"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080,
                log_level="info", workers=1)
