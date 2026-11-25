import asyncio
import logging
import os
import wave
import uuid
import subprocess
import time
from .base import AudioSink

logger = logging.getLogger(__name__)

# _validate_wav rejects anything under 100ms; flush() pads up to this so a
# short real fragment is played rather than discarded. 20ms of headroom.
_MIN_PLAYABLE_S = 0.12

class TelephonySink(AudioSink):
    """
    Telephony output sink.
    Buffers PCM and writes to temporary WAV files for playback injection.
    """

    def __init__(
        self,
        tmp_dir: str = "/tmp/ai_telephony_playback",
        sample_rate: int = 16000,
        conn_id: str = "unknown",
    ) -> None:
        self._tmp_dir = tmp_dir
        self._sample_rate = sample_rate
        self._pcm_buffer = bytearray()
        self._running = False
        self._conn_id = conn_id
        self.wav_queue: asyncio.Queue[str] = asyncio.Queue()
        # Duration of the most recently flushed WAV (seconds).  Read by
        # VoicePipeline._tts_layer to size the echo-suppression mute window.
        self.last_flush_duration: float = 0.0
        # Monotonic timestamp until which injected audio is expected to still
        # be playing on the call (playback is serialized by the bridge's
        # _playback_loop, so flushes queue back-to-back).  Read by the
        # pipeline's barge-in guard: while injected audio is in flight, its
        # echo returns via mod_audio_stream and must not be mistaken for the
        # caller interrupting.
        self.playback_busy_until: float = 0.0
        # Barge-in support.  on_interrupt is injected by the bridge and asks
        # socket_server to uuid_break the FreeSWITCH channel — audio already
        # handed to FS keeps playing no matter what we drop locally, so the
        # media-layer break must come first.  abort_event releases the
        # bridge's pacing sleep so the next turn's audio isn't stuck behind
        # the remaining duration of an utterance nobody is hearing anymore.
        self.on_interrupt = None
        self.abort_event: asyncio.Event = asyncio.Event()

        if not os.path.exists(self._tmp_dir):
            os.makedirs(self._tmp_dir, exist_ok=True)
            # Ensure it's readable by others (FreeSWITCH)
            os.chmod(self._tmp_dir, 0o777)

    async def start(self) -> None:
        self._running = True
        self._pcm_buffer.clear()
        logger.info("[%s] [TelephonySink] Started", self._conn_id)

    async def stop(self) -> None:
        self._running = False
        # PHASE17_DEBUG_DISABLE_BARGE_IN_START
        # Do not clear the PCM buffer immediately; allow flush() to finish if running.
        # self._pcm_buffer.clear()
        # PHASE17_DEBUG_DISABLE_BARGE_IN_END
        logger.info("[%s] [TelephonySink] Stopped", self._conn_id)

    async def interrupt_playback(self) -> None:
        """Barge-in: stop the audio the caller is actually hearing.

        Order matters — uuid_break at FreeSWITCH first (via on_interrupt),
        because draining local queues cannot touch WAVs already injected
        into the channel.  Then drop everything queued locally: those parts
        belong to the response the caller just talked over.
        """
        cb = self.on_interrupt
        if cb is not None:
            try:
                await cb()
            except Exception as exc:
                logger.warning(
                    "[%s] [BARGE-IN] uuid_break request failed: %r",
                    self._conn_id, exc)
        dropped = 0
        while True:
            try:
                stale = self.wav_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            dropped += 1
            try:
                os.remove(stale)
            except Exception:
                pass
        self._pcm_buffer.clear()
        self.playback_busy_until = 0.0
        self.abort_event.set()
        logger.info(
            "[%s] [BARGE-IN] media playback interrupted "
            "(dropped %d queued wavs)", self._conn_id, dropped)

    async def write_frame(self, pcm: bytes):
        """
        Accepts PCM chunks. 
        In telephony mode, we often want to write the whole response to a file 
        and then tell FreeSWITCH to play it.
        """
        if not self._running:
            return
        
        self._pcm_buffer.extend(pcm)

    async def flush(self) -> None:
        """
        Writes the current PCM buffer to a temporary WAV file and queues it.
        Performs normalization to 8kHz if needed.
        """
        if not self._pcm_buffer:
            return
        
        raw_filename = f"raw_{self._conn_id}_{uuid.uuid4().hex[:8]}.wav"
        raw_filepath = os.path.join(self._tmp_dir, raw_filename)
        
        try:
            # A real but very short piece of speech (the tail of an utterance,
            # a one-word sentence) used to be written, fail _validate_wav's
            # 100ms floor, and be DELETED — the caller simply never heard
            # those words. Measured live 2026-09-16: four drops in one day,
            # one of them 0.07s out of the middle of the agent's opening.
            # Trailing silence is inaudible; losing speech is not.
            _floor = int(self._sample_rate * 2 * _MIN_PLAYABLE_S)
            if 0 < len(self._pcm_buffer) < _floor:
                logger.info(
                    "[%s] [TelephonySink] padding %.0fms fragment to %.0fms "
                    "so it is played instead of dropped", self._conn_id,
                    len(self._pcm_buffer) / (2 * self._sample_rate) * 1000,
                    _MIN_PLAYABLE_S * 1000)
                self._pcm_buffer.extend(b"\x00" * (_floor - len(self._pcm_buffer)))

            # Phase A: Write initial WAV
            with wave.open(raw_filepath, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)  # PCM16
                wf.setframerate(self._sample_rate)
                wf.writeframes(self._pcm_buffer)
            
            pcm_size = len(self._pcm_buffer)
            # Record duration so the pipeline can size its echo-suppression
            # mute window to cover the full playback + injection latency.
            self.last_flush_duration = pcm_size / (2 * self._sample_rate)
            # Playback is serialized: this WAV starts after any still-playing
            # one ends. 0.7s covers injection overhead, 0.1s the bridge's
            # inter-file pacing gap.
            now = time.monotonic()
            self.playback_busy_until = (
                max(now + 0.7, self.playback_busy_until)
                + self.last_flush_duration + 0.1
            )
            logger.debug(
                "[%s] [TelephonySink] flush duration=%.2fs (%d bytes)",
                self._conn_id, self.last_flush_duration, pcm_size,
            )
            # The pipeline arms turn_speech_end_ts when a caller turn lands;
            # the first flush after that IS the turn's first audible audio
            # (modulo FS injection). One greppable line per turn for the
            # number every latency fix targets.
            anchor = getattr(self, "turn_speech_end_ts", 0.0)
            if anchor > 0.0:
                first_audio_ms = (now - anchor) * 1000.0
                logger.info(
                    "[%s] [LATENCY] speech_end→first_audio %.0fms "
                    "(flush %.2fs)",
                    self._conn_id, first_audio_ms,
                    self.last_flush_duration,
                )
                self.turn_speech_end_ts = 0.0
                # Write the measurement back into the pipeline's turn-trace
                # record (armed alongside the anchor) so it survives log
                # rotation via the call_turns table.
                ref = getattr(self, "turn_trace_ref", None)
                if ref is not None:
                    ref["speech_end_to_first_audio_ms"] = round(
                        first_audio_ms, 1)
                    self.turn_trace_ref = None
            self._pcm_buffer.clear()

            # Phase B & C: Normalization
            final_filepath = raw_filepath
            if self._sample_rate != 8000:
                norm_filename = f"pb_{self._conn_id}_{uuid.uuid4().hex[:8]}.wav"
                norm_filepath = os.path.join(self._tmp_dir, norm_filename)
                
                if self._normalize_to_8k(raw_filepath, norm_filepath):
                    final_filepath = norm_filepath
                    # Cleanup raw file
                    try:
                        os.remove(raw_filepath)
                    except Exception:
                        pass
                else:
                    # DROP, never queue the raw file: FreeSWITCH plays 8kHz;
                    # handing it a 16k WAV plays at half speed / octave down.
                    # This drop was the documented 2026-06-02 fix but never
                    # survived into committed code — the fall-through was
                    # dormant only because TTS_OUTPUT_SR defaults to 8000,
                    # and the RUNBOOK's rollback (TTS_OUTPUT_SR=16000)
                    # re-armed it. A skipped utterance beats demon audio.
                    logger.error(
                        "[%s] [TelephonySink] Normalization failed — DROPPING "
                        "%s (raw %d Hz would play wrong on FS)",
                        self._conn_id, raw_filepath, self._sample_rate)
                    try:
                        os.remove(raw_filepath)
                    except Exception:
                        pass
                    return

            # Phase E: Ensure permissions
            try:
                os.chmod(final_filepath, 0o666)
            except Exception:
                pass

            # Task 4: Validate WAV before injection
            valid, duration, size = self._validate_wav(final_filepath)
            
            if valid:
                logger.info("[%s] [PLAYBACK_WAV] path=%s duration=%.2fs bytes=%d ts=%.3f",
                            self._conn_id, final_filepath, duration, size, time.time())
                await self.wav_queue.put(final_filepath)
            else:
                logger.error("[%s] [PLAYBACK_INVALID] path=%s duration=%.2fs bytes=%d",
                             self._conn_id, final_filepath, duration, size)
                # Cleanup invalid file
                try:
                    os.remove(final_filepath)
                except Exception:
                    pass
        except Exception as exc:
            logger.error("[%s] [TTS] [PLAYBACK_ERROR] Failed to write WAV file: %s", self._conn_id, exc)

    def _validate_wav(self, path: str) -> tuple[bool, float, int]:
        """
        Task 4: Validate WAV exists, has size > 0, and duration > 100ms.
        """
        try:
            if not os.path.exists(path):
                return False, 0.0, 0
            
            size = os.path.getsize(path)
            if size == 0:
                return False, 0.0, 0
            
            with wave.open(path, 'rb') as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                duration = frames / float(rate)
                
            if duration < 0.1: # 100ms
                return False, duration, size
                
            return True, duration, size
        except Exception:
            return False, 0.0, 0

    def _normalize_to_8k(self, input_path: str, output_path: str) -> bool:
        """
        Phase C: Safely downsample to 8000Hz before playback injection.
        Uses ffmpeg for robust resampling and header generation.
        """
        try:
            cmd = [
                "ffmpeg", "-y", "-i", input_path,
                "-ar", "8000", "-ac", "1", "-acodec", "pcm_s16le",
                output_path
            ]
            # Use subprocess with timeout
            subprocess.run(cmd, check=True, capture_output=True, timeout=5.0)
            return True
        except Exception as e:
            logger.error("[TelephonySink] ffmpeg normalization failed: %s", e)
            return False

    def _inspect_and_log_wav(self, path: str, original_pcm_size: int):
        """
        Phase A & B: Detailed logging of the generated WAV file.
        """
        try:
            with wave.open(path, 'rb') as wf:
                channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                sample_rate = wf.getframerate()
                frames = wf.getnframes()
                duration = frames / float(sample_rate)
                
            logger.info("[PLAYBACK_FILE] Path: %s", path)
            logger.info("[PLAYBACK_FILE] Size: %d bytes (PCM: %d)", os.path.getsize(path), original_pcm_size)
            logger.info("[PLAYBACK_FILE] Format: mono=%s, bit=16, rate=%d, duration=%.2fs", 
                        channels == 1, sample_rate, duration)
        except Exception as e:
            logger.warning("[TelephonySink] Could not inspect WAV info: %s", e)

    def clear(self):
        self._pcm_buffer.clear()
