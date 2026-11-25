import asyncio
import logging
import queue as _queue
import threading
import numpy as np
from .base import AudioSink

logger = logging.getLogger(__name__)

# Pre-buffer: accumulate this many samples before releasing audio to the
# playback thread. Absorbs momentary gaps between TTS HTTP stream chunks.
# Kept short (100 ms) to minimise start-of-speech delay; Sarvam streams
# chunks densely enough that 100 ms is sufficient to avoid audible gaps.
_PREBUFFER_SAMPLES = 1600


class LocalSpeakerSink(AudioSink):
    """
    Real-time speaker output using sounddevice.
    Wraps the existing logic to fit the new AudioSink interface.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        output_device: int | str | None = None,
    ) -> None:
        self._sample_rate = sample_rate
        self._output_device = output_device
        self._audio_q: _queue.Queue[np.ndarray | None] = _queue.Queue(maxsize=200)
        self._playback_thread: threading.Thread | None = None
        self._running = False
        self._prebuffer: list[np.ndarray] = []
        self._prebuffer_samples: int = 0
        self._prebuffer_done: bool = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._prebuffer = []
        self._prebuffer_samples = 0
        self._prebuffer_done = False
        self._playback_thread = threading.Thread(target=self._play_loop, daemon=True)
        self._playback_thread.start()
        logger.info("[LocalSpeakerSink] Playback thread started")

    def _play_loop(self) -> None:
        import sounddevice as sd
        try:
            with sd.OutputStream(
                samplerate=self._sample_rate,
                channels=1,
                dtype="float32",
                device=self._output_device,
            ) as stream:
                while self._running:
                    try:
                        data = self._audio_q.get(timeout=0.5)
                    except _queue.Empty:
                        continue
                    if data is None:
                        break
                    try:
                        stream.write(data.reshape(-1, 1))
                    except sd.PortAudioError as exc:
                        logger.error("[LocalSpeakerSink] PortAudio error: %s", exc)
                        break
        except Exception as exc:
            logger.error("[LocalSpeakerSink] Playback error: %s", exc)
        finally:
            self._running = False
            logger.info("[LocalSpeakerSink] Playback loop stopped")

    async def write_frame(self, pcm: bytes):
        if not self._running:
            return

        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

        if not self._prebuffer_done:
            self._prebuffer.append(samples)
            self._prebuffer_samples += len(samples)
            if self._prebuffer_samples >= _PREBUFFER_SAMPLES:
                # Pre-buffer full — flush everything to the playback queue at once
                self._prebuffer_done = True
                logger.debug(
                    "[LocalSpeakerSink] Pre-buffer full (%d samples) — releasing to playback",
                    self._prebuffer_samples,
                )
                combined = np.concatenate(self._prebuffer)
                self._prebuffer = []
                self._prebuffer_samples = 0
                try:
                    self._audio_q.put(combined, timeout=0.1)
                except _queue.Full:
                    logger.warning("[LocalSpeakerSink] Audio queue full — dropping pre-buffer")
        else:
            try:
                self._audio_q.put(samples, timeout=0.1)
            except _queue.Full:
                logger.warning("[LocalSpeakerSink] Audio queue full — dropping chunk")

    async def flush(self) -> None:
        """Release any pre-buffer content that hasn't hit the threshold yet.

        Called by tts_sarvam after speak() completes normally (not on barge-in).
        This ensures short TTS utterances whose total audio is under 300ms still
        get played — without flush() they would sit in the pre-buffer forever.
        After flushing, reset pre-buffer state so the next speak() starts fresh.
        """
        if not self._running:
            return
        if self._prebuffer:
            combined = np.concatenate(self._prebuffer)
            self._prebuffer = []
            self._prebuffer_samples = 0
            self._prebuffer_done = True
            logger.debug(
                "[LocalSpeakerSink] flush(): releasing %d pre-buffer samples to playback",
                len(combined),
            )
            try:
                self._audio_q.put(combined, timeout=0.2)
            except _queue.Full:
                logger.warning("[LocalSpeakerSink] Audio queue full — dropping flush content")
        # Reset so the NEXT speak() starts with a fresh pre-buffer
        self._prebuffer_done = False

    async def stop(self) -> None:
        """Stop playback immediately (called on barge-in).

        Discard the pre-buffer — on barge-in we don't want leftover audio
        from the interrupted utterance to play after the caller speaks.
        """
        if not self._running:
            return

        # Discard any pending pre-buffer (barge-in means stop now, not drain)
        self._prebuffer = []
        self._prebuffer_samples = 0
        self._prebuffer_done = False

        self._running = False
        try:
            self._audio_q.put_nowait(None)
        except _queue.Full:
            pass
        if self._playback_thread:
            self._playback_thread.join(timeout=2.0)
            self._playback_thread = None

        import sounddevice as sd
        try:
            sd.stop()
        except Exception:
            pass
        logger.info("[LocalSpeakerSink] Stopped")
