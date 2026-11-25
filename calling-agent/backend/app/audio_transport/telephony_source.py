import asyncio
import logging
from .base import AudioSource

logger = logging.getLogger(__name__)

class TelephonySource(AudioSource):
    """
    Queue-based audio source for telephony.
    Receives PCM frames pushed from an external transport (e.g. WebSocket).
    """

    def __init__(
        self,
        queue_max_size: int = 500,  # Bounded queue
        sample_rate: int = 16000,
        frame_duration_ms: int = 20,
    ) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=queue_max_size)
        self._sample_rate = sample_rate
        self._frame_duration_ms = frame_duration_ms
        self._samples_per_frame = sample_rate * frame_duration_ms // 1000
        
        # Diagnostics
        self._pushed_frames = 0
        self._dropped_frames = 0
        self._read_frames = 0
        self._running = False

    async def start(self) -> None:
        self._running = True
        logger.info("[TelephonySource] Started")

    async def stop(self) -> None:
        self._running = False
        # Clear queue
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        logger.info("[TelephonySource] Stopped. Pushed: %d, Read: %d, Dropped: %d",
                    self._pushed_frames, self._read_frames, self._dropped_frames)

    def push_frame(self, pcm: bytes) -> None:
        """
        Push a PCM frame into the source.
        Called by the transport layer (e.g. ws_audio_receiver).
        Non-blocking with drop-oldest overflow policy.
        Accepts frames even before start() — pre-buffers early audio so
        the first words after call connect are not silently dropped.
        """
        if not self._running and self._read_frames > 0:
            return

        self._pushed_frames += 1
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            # Drop-oldest policy
            try:
                self._queue.get_nowait()
                self._dropped_frames += 1
                self._queue.put_nowait(pcm)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                # Should not really happen in this sequence
                pass

    async def read_frame(self) -> bytes:
        """
        Exposes read_frame() for the AI Pipeline.
        """
        if not self._running:
            return b""

        try:
            # We use a small timeout to avoid hanging if no audio is arriving
            frame = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            self._read_frames += 1
            return frame
        except asyncio.TimeoutError:
            # Return silence if no frame is available
            return b"\x00" * (self._samples_per_frame * 2)

    @property
    def stats(self):
        return {
            "pushed": self._pushed_frames,
            "read": self._read_frames,
            "dropped": self._dropped_frames,
            "queue_size": self._queue.qsize()
        }
