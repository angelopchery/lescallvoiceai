"""
Sarvam AI Bulbul v3 TTS via WebSocket.

Maintains a persistent WebSocket connection for the call duration, eliminating
per-utterance TCP/TLS reconnect overhead compared with the HTTP stream approach.
Config is sent once at connect; language changes send a new config message without
reopening the connection.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time

from sarvamai import AsyncSarvamAI
from sarvamai.types.audio_output import AudioOutput
from sarvamai.types.event_response import EventResponse
from sarvamai.types.error_response import ErrorResponse

from .tts_stream import StreamingTTS, TTSChunk
from ..audio_transport.base import AudioSink
from ..audio_transport.local_speaker_sink import LocalSpeakerSink

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "bulbul:v3"
DEFAULT_SPEAKER = "shubh"
DEFAULT_SAMPLE_RATE = 16_000
CONNECT_TIMEOUT = 5.0
RECV_TIMEOUT = 10.0
DRAIN_TIMEOUT = 0.5
MAX_RECONNECT_ATTEMPTS = 3
KEEPALIVE_INTERVAL = 30.0

_LANG_CODE_MAP = {
    "en": "en-IN",
    "hi": "hi-IN",
    "gu": "gu-IN",
    "mr": "mr-IN",
    "ta": "ta-IN",
    "te": "te-IN",
    "kn": "kn-IN",
    "ml": "ml-IN",
    "bn": "bn-IN",
    "pa": "pa-IN",
    "od": "od-IN",
}


class SarvamWSTTS(StreamingTTS):
    """
    Sarvam Bulbul v3 WebSocket TTS — persistent connection, lowest per-turn latency.

    Compared with SarvamTTS (HTTP stream):
    - No reconnect overhead between successive utterances (same call)
    - Language changes apply via a new config message, not reconnection
    - send_completion_event=true gives a clean end-of-audio signal
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        speaker: str = DEFAULT_SPEAKER,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        pace: float = 1.0,
        output_device: int | str | None = None,
        audio_sink: AudioSink | None = None,
    ) -> None:
        super().__init__(audio_sink=audio_sink or LocalSpeakerSink(
            sample_rate=sample_rate,
            output_device=output_device,
        ))
        self._api_key = api_key or os.environ.get("SARVAM_API_KEY", "")
        self._model = model
        self._speaker = speaker
        self._sample_rate = sample_rate
        self._pace = pace
        self._language = "en"
        self._output_device = output_device

        self._sarvam_client: AsyncSarvamAI | None = None
        self._ws = None         # AsyncTextToSpeechStreamingSocketClient
        self._ws_ctx = None     # async context manager for cleanup
        self._connect_lock = asyncio.Lock()
        self._configured_lang: str | None = None
        self._needs_drain: bool = False
        self._keepalive_task: asyncio.Task | None = None

        if not self._api_key:
            logger.warning("[TTS:SarvamWS] SARVAM_API_KEY not set — calls will fail")
        else:
            masked = self._api_key[:6] + "…" + self._api_key[-4:]
            logger.info("[TTS:SarvamWS] API key loaded: %s", masked)

    def _get_client(self) -> AsyncSarvamAI:
        if self._sarvam_client is None:
            self._sarvam_client = AsyncSarvamAI(api_subscription_key=self._api_key)
        return self._sarvam_client

    def set_language(self, lang: str) -> None:
        self._language = lang

    def _lang_code(self) -> str:
        return _LANG_CODE_MAP.get(self._language, "en-IN")

    async def _ensure_connected(self):
        async with self._connect_lock:
            if self._ws is not None:
                return self._ws

            for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
                try:
                    ctx = self._get_client().text_to_speech_streaming.connect(
                        model=self._model,
                        send_completion_event="true",
                    )
                    self._ws_ctx = ctx
                    self._ws = await asyncio.wait_for(
                        ctx.__aenter__(),
                        timeout=CONNECT_TIMEOUT,
                    )
                    await self._send_config()
                    self._configured_lang = self._language
                    self._needs_drain = False
                    self._start_keepalive()
                    logger.info("[TTS:SarvamWS] Connection opened (attempt %d)", attempt)
                    return self._ws
                except Exception as exc:
                    logger.warning(
                        "[TTS:SarvamWS] Connect failed attempt %d/%d: %s",
                        attempt, MAX_RECONNECT_ATTEMPTS, exc,
                    )
                    await self._close_ws_locked()
                    if attempt < MAX_RECONNECT_ATTEMPTS:
                        await asyncio.sleep(0.3 * attempt)
            logger.error("[TTS:SarvamWS] All connect attempts failed")
            return None

    async def _send_config(self) -> None:
        if self._ws is None:
            return
        await self._ws.configure(
            target_language_code=self._lang_code(),
            speaker=self._speaker,
            pace=self._pace,
            speech_sample_rate=self._sample_rate,
            output_audio_codec="linear16",
            max_chunk_length=500,
        )
        logger.info("[TTS:SarvamWS] Config sent lang=%s", self._lang_code())

    def _start_keepalive(self) -> None:
        if self._keepalive_task is not None and not self._keepalive_task.done():
            return
        self._keepalive_task = asyncio.create_task(self._keepalive_loop(), name="tts_ws_keepalive")

    async def _keepalive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                if self._ws is None:
                    break
                try:
                    await self._ws.ping()
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    async def _drain_until_final(self) -> None:
        if self._ws is None:
            return
        deadline = time.monotonic() + DRAIN_TIMEOUT
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                msg = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
                if isinstance(msg, EventResponse) and msg.data.event_type == "final":
                    return
            except asyncio.TimeoutError:
                break
            except Exception as exc:
                logger.debug("[TTS:SarvamWS] Drain error: %s — closing connection", exc)
                await self._close_ws_locked()
                return
        logger.warning("[TTS:SarvamWS] Drain timeout — closing connection for clean state")
        await self._close_ws_locked()

    async def _close_ws_locked(self) -> None:
        ws, self._ws = self._ws, None
        ctx, self._ws_ctx = self._ws_ctx, None
        self._configured_lang = None
        self._needs_drain = False
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        if ctx is not None:
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:
                pass

    async def speak(self, text: str) -> None:
        self._reset()
        self._playing = True

        if not self._api_key:
            logger.warning("[TTS:SarvamWS] No API key — skipping")
            self._playing = False
            return

        await self._sink.start()
        chunk_count = 0

        try:
            async for chunk in self._stream_audio(text):
                if self._cancelled:
                    logger.info("[TTS:SarvamWS] Cancelled mid-stream")
                    break
                await self._sink.write_frame(chunk.audio)
                chunk_count += 1
                if chunk_count == 1:
                    logger.info("[TTS:SarvamWS] First chunk sent to sink")

        except Exception:
            logger.exception("[TTS:SarvamWS] speak() failed")
        finally:
            self._playing = False
            if not self._cancelled:
                if hasattr(self._sink, "flush"):
                    await self._sink.flush()
                logger.info("[TTS:SarvamWS] Playback complete (%d chunks)", chunk_count)
            else:
                self._needs_drain = True
            self._reset()

    async def _stream_audio(self, text: str):
        ws = await self._ensure_connected()
        if ws is None:
            logger.error("[TTS:SarvamWS] No connection available — cannot stream")
            return

        # Drain leftover audio from a previous cancelled speak()
        if self._needs_drain:
            logger.info("[TTS:SarvamWS] Draining previous interrupted stream before new speak")
            await self._drain_until_final()
            self._needs_drain = False
            ws = await self._ensure_connected()
            if ws is None:
                return

        # Update config if language changed since last utterance
        if self._language != self._configured_lang:
            await self._send_config()
            self._configured_lang = self._language

        await ws.convert(text)
        await ws.flush()
        logger.info("[TTS:SarvamWS] Streaming %r (%s, %s)", text[:50], self._lang_code(), self._speaker)

        try:
            while not self._cancelled:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning("[TTS:SarvamWS] Timeout waiting for audio")
                    await self._close_ws_locked()
                    return

                if isinstance(msg, AudioOutput):
                    if msg.data.audio:
                        pcm_bytes = base64.b64decode(msg.data.audio)
                        yield TTSChunk(audio=pcm_bytes, text_segment=text, is_last=False)

                elif isinstance(msg, EventResponse):
                    if msg.data.event_type == "final":
                        return

                elif isinstance(msg, ErrorResponse):
                    msg_text = getattr(msg.data, "message", "") or ""
                    code = getattr(msg.data, "code", None)
                    is_credit_error = (
                        code == 402
                        or "payment" in msg_text.lower()
                        or "credit" in msg_text.lower()
                        or "quota" in msg_text.lower()
                    )
                    if is_credit_error:
                        logger.error(
                            "[SARVAM:CREDITS] TTS credits exhausted (code=%s): %s — "
                            "attempting auto-rotation from key pool.",
                            code, msg_text,
                        )
                        from .key_rotator import rotate as _rotate_key
                        # Same-key guard as the HTTP path: one dead key must
                        # cost the pool one key (see key_rotator.rotate).
                        new_key = _rotate_key(self._api_key)
                        if new_key:
                            self._api_key = new_key
                            self._sarvam_client = None
                            logger.info("[TTS:SarvamWS] Key rotated — next speak() will use new key")
                    else:
                        logger.error(
                            "[TTS:SarvamWS] Server error: %s (code=%s)",
                            msg_text, code,
                        )
                    await self._close_ws_locked()
                    return

        except Exception as exc:
            logger.warning("[TTS:SarvamWS] Stream error: %s — resetting connection", exc)
            await self._close_ws_locked()

    async def stop(self) -> None:
        await super().stop()
        await self._sink.stop()

    async def close(self) -> None:
        await self._close_ws_locked()
        logger.info("[TTS:SarvamWS] Closed")
