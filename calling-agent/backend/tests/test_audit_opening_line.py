"""The agent's opening line must reach the transcript (2026-08-04).

From 2026-07-22 every audit transcript lost the agent's first line. Counted
across all 513 recordings: 06-15→07-21 is 275 Agent-first / 0 Caller-first
with the first line stamped at 00:00.03–00:00.86; from 07-24 it is 16
Agent-first / 157 Caller-first, and all 16 of those "Agent-first" files begin
at ≥00:09 — they are dead-air reprompts, not openings. So the loss rate is
effectively 100%, not the 90.7% a naive first-speaker count reports.

Mechanism: `conversation_call_bridge.start()` created the pipeline task
BEFORE the event task, and `_event_loop` called `hub.subscribe()` inside its
own body. The pipeline reaches its opening `emit_nowait` without ever
suspending — the 25ms first-audio yield is skipped once ws_audio_receiver has
already pushed frame 1, and `generate_opening` is fully synchronous — so the
event was broadcast to an empty subscriber list. EventHub has no replay
buffer. The trigger was session-setup latency growing past the point where
frame 1 always arrives first, not a change to the greeting path.

The fix removes the race rather than papering over it: subscribe
synchronously before anything can emit, and start the consumer only once the
audit session exists.
"""

from __future__ import annotations

import asyncio

from app.voice.event_hub import EventHub


# ── the bug and the fix, in six lines ───────────────────────────────────────

def test_event_emitted_before_the_consumer_runs_is_kept():
    """A queue created up front buffers everything until someone drains it."""
    async def _go():
        hub = EventHub()
        queue = hub.subscribe()                      # sync, before any emit
        hub.emit_nowait({"type": "agent", "text": "Hello"})
        return await asyncio.wait_for(queue.get(), 1.0)

    assert asyncio.run(_go())["text"] == "Hello"


def test_event_emitted_before_subscribe_is_lost():
    """The inverse — this is exactly what the bridge used to do."""
    async def _go():
        hub = EventHub()
        hub.emit_nowait({"type": "agent", "text": "Hello"})
        queue = hub.subscribe()
        try:
            await asyncio.wait_for(queue.get(), 0.05)
            return False
        except asyncio.TimeoutError:
            return True

    assert asyncio.run(_go()) is True


# ── the bridge wiring ───────────────────────────────────────────────────────

def _bridge_source() -> str:
    from conftest import REPO_ROOT
    return (REPO_ROOT / "conversation_call_bridge.py").read_text()


def test_bridge_subscribes_before_creating_the_pipeline_task():
    src = _bridge_source()
    sub = src.index("self._hub_queue = self.hub.subscribe()")
    pipe = src.index("self.pipeline_task = asyncio.create_task(")
    assert sub < pipe, "the subscription must exist before anything can emit"


def test_event_task_starts_after_the_audit_session_exists():
    """_event_loop calls audit_manager.add_event — starting it before the
    session is created would drop the very line this fix restores."""
    src = _bridge_source()
    create = src.index("_audit_session = await audit_manager.create(")
    evt = src.index("self.event_task = asyncio.create_task(self._event_loop())")
    assert create < evt


def test_event_loop_reuses_the_pre_created_queue():
    src = _bridge_source()
    assert 'queue = getattr(self, "_hub_queue", None) or self.hub.subscribe()' \
        in src, "the CLI/--web path must still work when start() didn't run"
