"""
call_audit.py — Call Audit System

Records caller audio, transcripts, and metadata for every phone call.

Enabled by:  ENABLE_CALL_AUDIT=true
Disabled by: ENABLE_CALL_AUDIT=false  (zero overhead — early-exit everywhere)

Directory layout:
    recordings/
    YYYYMMDD/
        <call_id>/
            caller_audio.wav   (PCM16, 16kHz, mono)
            transcript.txt
            metadata.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import wave
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Optional

logger = logging.getLogger("call_audit")

ENABLE_CALL_AUDIT = os.environ.get("ENABLE_CALL_AUDIT", "false").lower() == "true"

RECORDINGS_ROOT = os.environ.get(
    "CALL_AUDIT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings"),
)

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # PCM16 = 2 bytes per sample
CHANNELS = 1

_AUDIO_QUEUE_MAXSIZE = 200  # ~4s of 20ms frames at 16kHz; excess frames dropped


class CallAuditSession:
    """Per-call recording session. All writes are non-blocking."""

    def __init__(self, call_id: str, caller_number: str = "",
                 agent_slug: str = "", agent_revision: Optional[int] = None,
                 agent_sha: str = "") -> None:
        self.call_id = call_id
        self.caller_number = caller_number or "unknown"
        # F8 per-call agent fidelity (optional — defaults keep old callers
        # byte-compatible): which agent revision/sha served this call.
        self.agent_slug = agent_slug or ""
        self.agent_revision = agent_revision
        self.agent_sha = agent_sha or ""
        self._start_time = time.time()
        self._start_dt = datetime.now(timezone.utc)

        # Folder: <caller_number>_<YYYYMMDD_HHMMSS>
        dt_str = self._start_dt.strftime("%Y%m%d_%H%M%S")
        folder_name = f"{self.caller_number}_{dt_str}"
        self._call_dir = os.path.join(RECORDINGS_ROOT, folder_name)
        os.makedirs(self._call_dir, exist_ok=True)

        self._wav_path = os.path.join(self._call_dir, "caller_audio.wav")
        self._txt_path = os.path.join(self._call_dir, "transcript.txt")
        self._meta_path = os.path.join(self._call_dir, "metadata.json")

        # WAV writer — opened immediately so we can stream frames
        self._wav_file = wave.open(self._wav_path, "wb")
        self._wav_file.setnchannels(CHANNELS)
        self._wav_file.setsampwidth(SAMPLE_WIDTH)
        self._wav_file.setframerate(SAMPLE_RATE)

        # Transcript file
        self._txt_file = open(self._txt_path, "a", encoding="utf-8")

        # Non-blocking audio queue → background writer
        self._audio_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(
            maxsize=_AUDIO_QUEUE_MAXSIZE
        )
        self._writer_task: Optional[asyncio.Task] = None

        # Transcript counter for elapsed-time calculation
        self._transcript_count = 0

        # Last entry written, so a line the caller never heard can be taken
        # back off the transcript (see drop_last_event).
        self._last_entry_offset: int = -1
        self._last_entry_speaker: str = ""
        self._last_entry_text: str = ""
        # The last few entries (offset, speaker, text), so the pieces of a
        # turn the engine re-transcribed whole can be replaced by that text
        # (replace_last_events, RUNBOOK §78).
        self._recent_entries: deque = deque(maxlen=16)

        # Metadata fields populated at close()
        self._language_detected: str = "unknown"
        self._stt_provider: str = "sarvam"
        self._fallback_used: bool = False

        logger.info("[AUDIT] Session created call_id=%s dir=%s", call_id, self._call_dir)

    async def start(self) -> None:
        self._writer_task = asyncio.create_task(
            self._audio_writer_loop(), name=f"audit_writer_{self.call_id}"
        )

    def push_audio(self, pcm_bytes: bytes) -> None:
        """Non-blocking: queue PCM frame for background writing."""
        if not pcm_bytes:
            return
        try:
            self._audio_queue.put_nowait(pcm_bytes)
        except asyncio.QueueFull:
            pass  # Drop oldest-equivalent: just skip this frame

    def add_event(self, speaker: str, text: str,
                  span: "tuple[float, float] | None" = None) -> None:
        """Record a transcript entry (caller or agent) with elapsed timestamp.

        span: exact (start_s, end_s) of this speech within caller_audio.wav
        (live VAD boundaries) — makes the recording a self-documenting
        corpus segment; absent = legacy format, fully compatible."""
        if not text:
            return
        elapsed = time.time() - self._start_time
        minutes = int(elapsed // 60)
        seconds = elapsed % 60
        timestamp_str = f"[{minutes:02d}:{seconds:06.3f}]"
        if span is not None:
            timestamp_str += f" [spoke {span[0]:.2f}-{span[1]:.2f}s]"
        entry = f"{timestamp_str}\n{speaker}: {text}\n\n"
        try:
            self._last_entry_offset = self._txt_file.tell()
            self._txt_file.write(entry)
            self._txt_file.flush()
            self._last_entry_speaker = speaker
            self._last_entry_text = text
            self._recent().append((self._last_entry_offset, speaker, text))
        except Exception as exc:
            logger.warning("[AUDIT] Transcript write failed: %r", exc)
            self._last_entry_offset = -1
        self._transcript_count += 1

    def drop_last_event(self, speaker: str, text: str) -> bool:
        """Take the last entry back off the transcript.

        Used for an agent line the caller never heard: the engine finished
        generating it, the caller was still mid-sentence, and the platform cut
        it before a single frame reached the wire. Recording it anyway is how
        a clean call reads back as the agent repeating itself — the owner's
        2026-09-16 report of "asked the same question six times" was a call
        where the caller heard the question ONCE and the other five were
        cancelled replies written to this file.

        Only ever removes the entry it is handed, and only while that entry is
        still the last one — a later write makes the retraction a no-op rather
        than corrupting the file.
        """
        if (self._last_entry_offset < 0
                or self._last_entry_speaker != speaker
                or self._last_entry_text != text):
            return False
        try:
            self._txt_file.flush()
            self._txt_file.close()
            with open(self._txt_path, "r+", encoding="utf-8") as f:
                f.truncate(self._last_entry_offset)
            self._txt_file = open(self._txt_path, "a", encoding="utf-8")
        except Exception as exc:
            logger.warning("[AUDIT] Transcript retraction failed: %r", exc)
            try:
                self._txt_file = open(self._txt_path, "a", encoding="utf-8")
            except Exception:
                pass
            return False
        self._last_entry_offset = -1
        self._last_entry_speaker = ""
        self._last_entry_text = ""
        if self._recent():
            self._recent().pop()
        self._transcript_count = max(0, self._transcript_count - 1)
        logger.info("[AUDIT] Retracted unheard %s line: %r", speaker, text[:60])
        return True

    def _recent(self) -> deque:
        """The recent-entries ring, created lazily — tests build sessions
        with __new__ and set only the fields they need."""
        return self.__dict__.setdefault("_recent_entries", deque(maxlen=16))

    def replace_last_events(self, speaker: str, texts: list, new_text: str,
                            span: "tuple[float, float] | None" = None) -> bool:
        """Replace the last entries for `speaker` — exactly `texts`, in
        order — with ONE entry `new_text` (RUNBOOK §78).

        The pieces of a split turn are written as they are heard; the engine
        then re-transcribes the joined audio and acts on THAT text. The owner
        read the pieces on call b5452b23 ("…परिष्टों में इंटरेस्ट है।") and
        concluded "4 BHK" was never heard — the joined pass had it. Only ever
        replaces the entries it is handed, and only while they are still the
        last ones; anything written since makes this a no-op.
        """
        if not texts or not new_text:
            return False
        recent = list(self._recent())
        n = len(texts)
        if len(recent) < n:
            return False
        tail = recent[-n:]
        if any(sp != speaker or tx != want
               for (_, sp, tx), want in zip(tail, texts)):
            return False
        offset = tail[0][0]
        if offset < 0:
            return False
        try:
            self._txt_file.flush()
            self._txt_file.close()
            with open(self._txt_path, "r+", encoding="utf-8") as f:
                f.truncate(offset)
            self._txt_file = open(self._txt_path, "a", encoding="utf-8")
        except Exception as exc:
            logger.warning("[AUDIT] Transcript replace failed: %r", exc)
            try:
                self._txt_file = open(self._txt_path, "a", encoding="utf-8")
            except Exception:
                pass
            return False
        for _ in range(n):
            self._recent().pop()
        self._transcript_count = max(0, self._transcript_count - n)
        self.add_event(speaker, new_text, span=span)
        logger.info("[AUDIT] Replaced %d %s piece(s) with the joined turn: %r",
                    n, speaker, new_text[:60])
        return True

    async def close(
        self,
        language_detected: str = "unknown",
        stt_provider: str = "sarvam",
        fallback_used: bool = False,
    ) -> None:
        """Flush all buffers, close files, write metadata."""
        self._language_detected = language_detected
        self._stt_provider = stt_provider
        self._fallback_used = fallback_used

        # Signal writer to stop
        try:
            await self._audio_queue.put(None)
        except Exception:
            pass

        if self._writer_task is not None:
            try:
                await asyncio.wait_for(self._writer_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._writer_task.cancel()

        # Close WAV
        try:
            self._wav_file.close()
        except Exception as exc:
            logger.warning("[AUDIT] WAV close error: %r", exc)

        # Close transcript
        try:
            self._txt_file.flush()
            self._txt_file.close()
        except Exception as exc:
            logger.warning("[AUDIT] Transcript close error: %r", exc)

        # Write metadata
        end_time = time.time()
        duration = end_time - self._start_time
        metadata = {
            "call_id": self.call_id,
            "start_time": self._start_dt.isoformat(),
            "end_time": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(duration, 3),
            "language_detected": self._language_detected,
            "stt_provider": self._stt_provider,
            "fallback_used": self._fallback_used,
            "transcript_count": self._transcript_count,
            # F8 per-call agent fidelity — which agent bytes served the call.
            "agent_slug": self.agent_slug,
            "agent_revision": self.agent_revision,
            "agent_sha": self.agent_sha,
        }
        try:
            with open(self._meta_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)
        except Exception as exc:
            logger.warning("[AUDIT] Metadata write failed: %r", exc)

        logger.info(
            "[AUDIT] Session closed call_id=%s duration=%.1fs transcripts=%d",
            self.call_id,
            duration,
            self._transcript_count,
        )

    async def _audio_writer_loop(self) -> None:
        """Background coroutine: drains audio queue → WAV file."""
        try:
            while True:
                chunk = await self._audio_queue.get()
                if chunk is None:
                    break
                try:
                    self._wav_file.writeframes(chunk)
                except Exception as exc:
                    logger.warning("[AUDIT] WAV write error: %r", exc)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[AUDIT] Audio writer crashed: %r", exc)


class CallAuditManager:
    """Global registry of active audit sessions."""

    def __init__(self) -> None:
        self._sessions: Dict[str, CallAuditSession] = {}

    def enabled(self) -> bool:
        return ENABLE_CALL_AUDIT

    async def create(self, call_id: str, caller_number: str = "",
                     agent_slug: str = "", agent_revision: Optional[int] = None,
                     agent_sha: str = "") -> Optional[CallAuditSession]:
        if not ENABLE_CALL_AUDIT:
            return None
        if call_id in self._sessions:
            return self._sessions[call_id]
        session = CallAuditSession(call_id, caller_number=caller_number,
                                   agent_slug=agent_slug,
                                   agent_revision=agent_revision,
                                   agent_sha=agent_sha)
        self._sessions[call_id] = session
        await session.start()
        return session

    def push_audio(self, call_id: str, pcm_bytes: bytes) -> None:
        if not ENABLE_CALL_AUDIT:
            return
        session = self._sessions.get(call_id)
        if session is not None:
            session.push_audio(pcm_bytes)

    def add_event(self, call_id: str, speaker: str, text: str,
                  span: "tuple[float, float] | None" = None) -> None:
        if not ENABLE_CALL_AUDIT:
            return
        session = self._sessions.get(call_id)
        if session is not None:
            session.add_event(speaker, text, span=span)

    def drop_last_event(self, call_id: str, speaker: str, text: str) -> bool:
        if not ENABLE_CALL_AUDIT:
            return False
        session = self._sessions.get(call_id)
        if session is None:
            return False
        return session.drop_last_event(speaker, text)

    def replace_last_events(self, call_id: str, speaker: str, texts: list,
                            new_text: str,
                            span: "tuple[float, float] | None" = None) -> bool:
        if not ENABLE_CALL_AUDIT:
            return False
        session = self._sessions.get(call_id)
        if session is None:
            return False
        return session.replace_last_events(speaker, texts, new_text, span=span)

    async def close(
        self,
        call_id: str,
        language_detected: str = "unknown",
        stt_provider: str = "sarvam",
        fallback_used: bool = False,
    ) -> None:
        if not ENABLE_CALL_AUDIT:
            return
        session = self._sessions.pop(call_id, None)
        if session is not None:
            await session.close(language_detected, stt_provider, fallback_used)


# Global singleton
audit_manager = CallAuditManager()
