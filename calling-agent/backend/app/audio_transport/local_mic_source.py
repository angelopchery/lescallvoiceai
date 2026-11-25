import asyncio
import logging
import numpy as np
from .base import AudioSource

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
FRAME_DURATION_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_DURATION_MS // 1000

class NoiseGate:
    """
    Adaptive spectral noise gate for reducing background noise.
    """

    def __init__(
        self,
        calibration_frames: int = 30,
        suppression_factor: float = 0.08,
        smoothing_alpha: float = 0.15,
        gate_threshold_multiplier: float = 2.5,
    ) -> None:
        self._calibration_frames = calibration_frames
        self._suppression_factor = suppression_factor
        self._smoothing_alpha = smoothing_alpha
        self._gate_threshold_mult = gate_threshold_multiplier

        self._noise_spectrum: np.ndarray | None = None
        self._calibration_spectra: list[np.ndarray] = []
        self._calibrated = False
        self._prev_gain: np.ndarray | None = None
        self._frame_count = 0

    @property
    def calibrated(self) -> bool:
        return self._calibrated

    def process(self, pcm16: np.ndarray) -> np.ndarray:
        self._frame_count += 1
        audio_f = pcm16.astype(np.float32)
        spectrum = np.abs(np.fft.rfft(audio_f))

        if not self._calibrated:
            self._calibration_spectra.append(spectrum)
            if len(self._calibration_spectra) >= self._calibration_frames:
                self._finish_calibration()
            return pcm16

        threshold = self._noise_spectrum * self._gate_threshold_mult
        gain = np.ones_like(spectrum)
        below = spectrum < threshold
        gain[below] = self._suppression_factor

        if self._prev_gain is not None:
            gain = self._smoothing_alpha * gain + (1 - self._smoothing_alpha) * self._prev_gain
        self._prev_gain = gain.copy()

        fft_data = np.fft.rfft(audio_f)
        fft_data *= gain
        result = np.fft.irfft(fft_data, n=len(audio_f))
        return np.clip(result, -32768, 32767).astype(np.int16)

    def _finish_calibration(self) -> None:
        stacked = np.stack(self._calibration_spectra)
        self._noise_spectrum = np.percentile(stacked, 75, axis=0)
        self._calibrated = True
        self._calibration_spectra.clear()
        logger.info(
            "[NOISE_GATE] Calibrated from %d frames — noise floor mean=%.1f",
            self._calibration_frames,
            float(self._noise_spectrum.mean()),
        )


class LocalMicSource(AudioSource):
    """
    Real-time microphone capture using sounddevice.
    """

    def __init__(
        self,
        device: int | str | None = None,
        sample_rate: int = SAMPLE_RATE,
        frame_duration_ms: int = FRAME_DURATION_MS,
        queue_max_size: int = 200,
        noise_suppression: bool = False,
    ) -> None:
        self._device = device
        self._sample_rate = sample_rate
        self._frame_duration_ms = frame_duration_ms
        self._samples_per_frame = sample_rate * frame_duration_ms // 1000
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=queue_max_size)
        self._sd_stream = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._frame_count = 0
        self._noise_gate = NoiseGate() if noise_suppression else None

    @property
    def calibrated(self) -> bool:
        if self._noise_gate:
            return self._noise_gate.calibrated
        return True

    async def start(self) -> None:
        if self._running:
            return
        import sounddevice as sd
        self._loop = asyncio.get_running_loop()
        self._running = True

        try:
            self._sd_stream = sd.InputStream(
                samplerate=self._sample_rate,
                blocksize=self._samples_per_frame,
                channels=1,
                dtype="int16",
                device=self._device,
                callback=self._audio_callback,
            )
            self._sd_stream.start()
            logger.info("[LocalMicSource] Stream opened — device=%s", self._device)
        except Exception as exc:
            self._running = False
            logger.error("[LocalMicSource] Failed to open audio device: %s", exc)
            raise

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        if not self._running:
            return
        if status:
            logger.warning("[LocalMicSource] PortAudio status: %s", status)

        if indata.dtype == np.float32:
            pcm16 = (indata * 32767).astype(np.int16)
        else:
            pcm16 = indata.copy()

        if self._noise_gate is not None:
            pcm16 = self._noise_gate.process(pcm16.flatten()).reshape(pcm16.shape)

        pcm_bytes = pcm16.tobytes()
        self._frame_count += 1

        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, pcm_bytes)
        except asyncio.QueueFull:
            pass

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._sd_stream is not None:
            self._sd_stream.stop()
            self._sd_stream.close()
            self._sd_stream = None
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    async def read_frame(self) -> bytes:
        if not self._running and self._queue.empty():
            return b""
        
        try:
            frame = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            if frame is None:
                return b""
            return frame
        except asyncio.TimeoutError:
            return b"\x00" * (self._samples_per_frame * 2)
