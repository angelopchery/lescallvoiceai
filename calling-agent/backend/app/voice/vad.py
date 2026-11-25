"""
Voice Activity Detection.

Energy-based VAD using numpy-vectorized mean-absolute computation.
"""

from __future__ import annotations

import logging
import numpy as np

logger = logging.getLogger(__name__)


class VoiceActivityDetector:
    def __init__(
        self,
        sample_rate: int = 16000,
        energy_threshold: float | None = None,
        calibration_frames: int = 25,
        calibration_multiplier: float = 2.0,
        minimum_threshold: float = 250.0,
        maximum_threshold: float = 1200.0,
        barge_in_multiplier: float = 2.0,
        barge_in_minimum: float = 500.0,
        minimum_rms_floor: float = 40.0,
    ) -> None:

        self.sample_rate = sample_rate

        self._threshold: float = energy_threshold or 0.0

        self._minimum_threshold = minimum_threshold
        self._maximum_threshold = maximum_threshold

        self._calibration_multiplier = calibration_multiplier

        self._barge_in_multiplier = barge_in_multiplier
        self._barge_in_minimum = barge_in_minimum

        self._minimum_rms_floor = minimum_rms_floor

        self._calibrating = energy_threshold is None
        self._calibration_energies: list[float] = []
        self._calibration_target = calibration_frames
        self._calibrated = not self._calibrating

        self._last_energy: float = 0.0
        self._log_counter = 0

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def calibrated(self) -> bool:
        return self._calibrated

    @property
    def last_energy(self) -> float:
        return self._last_energy

    @property
    def barge_in_threshold(self) -> float:
        return max(
            self._threshold * self._barge_in_multiplier,
            self._barge_in_minimum,
        )

    def _compute_energy(self, frame: bytes) -> float:
        audio = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        return float(np.abs(audio).mean())

    def is_speech(self, frame: bytes) -> bool:
        energy = self._compute_energy(frame)

        self._last_energy = energy

        self._log_counter += 1

        # Periodic diagnostic: energy vs thresholds every ~1 second (50 frames * 20ms)
        if self._log_counter % 50 == 1:
            if self._calibrated:
                logger.info(
                    "[VAD] energy=%.1f threshold=%.1f barge_in=%.1f "
                    "rms_floor=%.1f decision=%s",
                    energy,
                    self._threshold,
                    self.barge_in_threshold,
                    self._minimum_rms_floor,
                    "SPEECH" if (energy > self._minimum_rms_floor and energy > self._threshold) else "silence",
                )
            else:
                logger.info(
                    "[VAD] Calibrating: energy=%.1f frames=%d/%d",
                    energy,
                    len(self._calibration_energies),
                    self._calibration_target,
                )

        # Calibration phase: sample ALL frames including silence so calibration
        # always completes in 25 frames (500ms). The rms_floor guard must not
        # run here — PCMU comfort-noise (0xFF → energy=0) would block every frame
        # and leave the VAD permanently uncalibrated, preventing all STT.
        # _finish_calibration() clamps threshold to minimum_threshold (150/260)
        # when the sampled frames are all silence, which is the correct baseline
        # for a telephony channel before the caller first speaks.
        if not self._calibrated:
            self._calibration_energies.append(energy)

            if len(self._calibration_energies) >= self._calibration_target:
                self._finish_calibration()

            return False

        # Post-calibration: ignore absolute silence (below the noise floor).
        if energy < self._minimum_rms_floor:
            return False

        return energy > self._threshold

    def _finish_calibration(self) -> None:
        arr = np.array(self._calibration_energies)

        noise_mean = float(arr.mean())
        noise_max = float(arr.max())

        sorted_arr = np.sort(arr)

        quiet_half = sorted_arr[: max(1, len(sorted_arr) // 2)]

        noise_p95 = float(np.percentile(quiet_half, 95))

        raw = noise_p95 * self._calibration_multiplier

        capped = min(raw, self._maximum_threshold)

        self._threshold = max(capped, self._minimum_threshold)

        if raw > self._maximum_threshold:
            logger.warning(
                "[VAD] Calibration noise too high "
                "(p95=%.1f * %.1f = %.1f) capped at %.1f",
                noise_p95,
                self._calibration_multiplier,
                raw,
                self._maximum_threshold,
            )

        self._calibrated = True

        logger.info(
            "[VAD] Calibrated in %d frames "
            "(%.0fms) mean=%.2f p95=%.2f max=%.2f "
            "threshold=%.1f barge_in=%.1f",
            len(self._calibration_energies),
            len(self._calibration_energies) * 20,
            noise_mean,
            noise_p95,
            noise_max,
            self._threshold,
            self.barge_in_threshold,
        )
