"""
Bridges the local VoicePipeline to the dashboard's file-based contract.

The dashboard (dashboard/server.py) was built against a telephony deployment
where the pipeline writes live_calls_state.json describing in-progress calls.
Local mode has no FreeSWITCH conn_id, no caller number, and only ever runs
one call at a time — this module fills in that file from EventHub events so
the dashboard's Live Calls panel works unmodified.

Writes are atomic (temp file + os.replace) so the dashboard never reads a
half-written file.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_LIVE_STATE_FILE = "/home/angelo/lescall/phase14_runtime/live_calls_state.json"
LIVE_STATE_FILE = Path(os.environ.get("LIVE_CALLS_STATE_FILE", _DEFAULT_LIVE_STATE_FILE))

_TRANSCRIPT_TAIL_LEN = 6


class DashboardBridge:
    """Subscribes to an EventHub and mirrors call state to LIVE_STATE_FILE."""

    def __init__(self, hub: Any, conversation: Any, call_id: str) -> None:
        self._hub = hub
        self._conversation = conversation
        self._call_id = call_id
        self._start_time = _now_iso()
        self._speaking = "listening"
        self._transcript_tail: list[dict] = []
        self._mic_enabled = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._write(active=True)
        self._task = asyncio.create_task(self._consume(), name="dashboard-bridge")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._write(active=False)

    async def _consume(self) -> None:
        queue = self._hub.subscribe()
        try:
            while True:
                event = await queue.get()
                self._apply(event)
                self._write(active=True)
        finally:
            self._hub.unsubscribe(queue)

    def _apply(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "agent":
            self._speaking = "speaking"
            self._transcript_tail.append({"speaker": "agent", "text": event.get("text", "")})
        elif etype == "user":
            self._speaking = "listening"
            self._transcript_tail.append({"speaker": "user", "text": event.get("text", "")})
        elif etype == "mic_state":
            self._mic_enabled = bool(event.get("enabled"))
        self._transcript_tail = self._transcript_tail[-_TRANSCRIPT_TAIL_LEN:]

    def _write(self, active: bool) -> None:
        state = self._conversation.state if self._conversation is not None else None
        call = {
            "conn_id": self._call_id,
            "caller_number": None,
            "start_time": self._start_time,
            "speaking": self._speaking if self._mic_enabled or active else "listening",
            "stage": getattr(state, "stage", None),
            "language": getattr(state, "language", None),
            "mic_enabled": self._mic_enabled,
            "transcript_tail": self._transcript_tail,
        }
        payload = {
            "updated_at": _now_iso(),
            "calls": [call] if active else [],
        }
        try:
            LIVE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = LIVE_STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, LIVE_STATE_FILE)
        except Exception as exc:
            logger.debug("[DASHBOARD] live state write failed: %s", exc)


def _now_iso() -> str:
    import datetime
    return datetime.datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
