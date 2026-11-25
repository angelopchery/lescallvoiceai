from __future__ import annotations

import abc
from typing import AsyncIterator

class AudioSource(abc.ABC):
    """Abstract interface for audio input (e.g., Microphone, Telephony stream)."""

    @abc.abstractmethod
    async def read_frame(self) -> bytes:
        """Read a single PCM16 mono frame."""
        raise NotImplementedError

    async def start(self):
        """Initialize the audio source."""
        pass

    async def stop(self):
        """Shutdown the audio source."""
        pass

    async def frames(self) -> AsyncIterator[bytes]:
        """Async iterator yielding PCM16 frames."""
        await self.start()
        try:
            while True:
                frame = await self.read_frame()
                if not frame:
                    break
                yield frame
        finally:
            await self.stop()


class AudioSink(abc.ABC):
    """Abstract interface for audio output (e.g., Speaker, Telephony playback)."""

    @abc.abstractmethod
    async def write_frame(self, pcm: bytes):
        """Write a single PCM16 mono frame."""
        raise NotImplementedError

    async def start(self):
        """Initialize the audio sink."""
        pass

    async def stop(self):
        """Shutdown the audio sink."""
        pass

    async def flush(self):
        """Flush buffered audio (e.g. at utterance boundaries)."""
        pass

