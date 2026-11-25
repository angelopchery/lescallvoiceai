"""
ws_audio_receiver.py — Phase 15 Audio Format Normalization.

This process receives audio from FreeSWITCH via WebSocket,
normalizes it (PCMU -> PCM16, 8k -> 16k), and forwards it to the
AI Pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import audioop
import base64
import datetime
import json
import logging
import os
import struct
import time
import uuid as uuidlib
import wave

from conversation_call_bridge import call_bridge
from call_audit import audit_manager

# ---------------------------------------------------------------------
# Live-call state snapshot (additive, dashboard-only consumer).
#
# Periodically dumps a small JSON snapshot of every active call's
# coarse state (stage, language, who's-speaking, last transcript lines)
# to disk so the separate dashboard process (port 8080) can poll/stream
# it. Pure read-and-snapshot — never mutates pipeline/session state.
# ---------------------------------------------------------------------
LIVE_STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "phase14_runtime", "live_calls_state.json"
)
LIVE_STATE_INTERVAL_S = 1.5

# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------

INPUT_CODEC = os.environ.get("TELEPHONY_INPUT_CODEC", "l16").lower()
TARGET_SR = int(os.environ.get("TELEPHONY_TARGET_SR", "16000"))

PHASE15_DEBUG_DIR = "/tmp/phase15_audio_debug"
DEBUG_CAPTURE_SECONDS = 5

# Loopback by default. FreeSWITCH and every app container are on this host
# (containers use network_mode: host), so 0.0.0.0 bought nothing and exposed an
# UNAUTHENTICATED media endpoint: whoever reached 9001 could open a session
# naming any agent slug that exists on disk and run full AI calls billed to that
# slug's tenant — Sarvam + LLM spend on our account, quota on theirs, bypassing
# every billing gate in this codebase. Override with LESCALL_WS_HOST only if
# FreeSWITCH genuinely moves off-box, and put a firewall in front of it.
HOST_DEFAULT = os.environ.get("LESCALL_WS_HOST", "127.0.0.1")
PORT_DEFAULT = int(os.environ.get("LESCALL_WS_PORT", "9001"))

DEFAULT_SAMPLE_RATE_HZ = 8000
DEFAULT_BYTES_PER_SAMPLE = 2

EXPECTED_CHUNK_MS = 20
GAP_STALL_MS = 200
RX_LOG_EVERY_N_FRAMES = 50

# ---------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s.%(msecs)03d %(levelname)-5s "
        "[%(threadName)s] %(message)s"
    ),
    datefmt="%H:%M:%S",
)

log = logging.getLogger("wsaudio")

# ---------------------------------------------------------------------
# DEBUG AUDIO DUMPER
# ---------------------------------------------------------------------


class _Phase15AudioDebugger:

    def __init__(self, conn_id: str, sample_rate: int):

        self.conn_id = conn_id
        self.sample_rate = sample_rate

        self.capture_limit_bytes = 32000 * DEBUG_CAPTURE_SECONDS
        self.captured_bytes = 0

        self.raw_fp = None
        self.first_frame_logged = False

        os.makedirs(PHASE15_DEBUG_DIR, exist_ok=True)

        self.raw_path = os.path.join(
            PHASE15_DEBUG_DIR,
            f"raw_{conn_id}.raw",
        )

        self.wav_path = os.path.join(
            PHASE15_DEBUG_DIR,
            f"wav_{conn_id}.wav",
        )

    def process_frame(self, data: bytes):

        if self.captured_bytes >= self.capture_limit_bytes:

            if self.raw_fp:
                self.close()

            return

        if not self.raw_fp:

            self.raw_fp = open(self.raw_path, "wb")

        self.raw_fp.write(data)

        self.captured_bytes += len(data)

        if not self.first_frame_logged:

            self.log_metadata(data)

            self.first_frame_logged = True

    def log_metadata(self, data: bytes):

        size = len(data)

        if size == 0:
            return

        ff_ratio = data.count(b"\xff") / size
        zero_ratio = data.count(b"\x00") / size

        codec_guess = INPUT_CODEC

        try:

            if INPUT_CODEC == "pcmu":

                lin_data = audioop.ulaw2lin(data, 2)

                rms = audioop.rms(lin_data, 2)

                min_v, max_v = audioop.minmax(lin_data, 2)

            else:

                rms = audioop.rms(data, 2)

                min_v, max_v = audioop.minmax(data, 2)

        except Exception:

            rms = -1
            min_v = -1
            max_v = -1

        log.info("[AUDIO_DEBUG] conn_id=%s", self.conn_id)
        log.info("[AUDIO_DEBUG] codec_guess=%s", codec_guess)
        log.info("[AUDIO_DEBUG] frame_bytes=%d", size)
        log.info("[AUDIO_DEBUG] rms=%d", rms)
        log.info("[AUDIO_DEBUG] min=%d", min_v)
        log.info("[AUDIO_DEBUG] max=%d", max_v)
        log.info(
            "[AUDIO_DEBUG] ff_ratio=%.2f zero_ratio=%.2f",
            ff_ratio,
            zero_ratio,
        )

    def close(self):

        if not self.raw_fp:
            return

        try:

            self.raw_fp.close()

            self.raw_fp = None

            self.generate_wav()

        except Exception:
            pass

    def generate_wav(self):

        try:

            with open(self.raw_path, "rb") as f:
                raw_data = f.read()

            with wave.open(self.wav_path, "wb") as wf:

                wf.setnchannels(1)

                wf.setsampwidth(2)

                wf.setframerate(self.sample_rate)

                wf.writeframes(raw_data)

            log.info(
                "[AUDIO_DEBUG] Generated WAV: %s",
                self.wav_path,
            )

        except Exception as exc:

            log.error(
                "[AUDIO_DEBUG] WAV generation failed: %r",
                exc,
            )

# ---------------------------------------------------------------------
# STT FORWARDER
# ---------------------------------------------------------------------

STT_FORWARD_HOST = os.environ.get("STT_FORWARD_HOST", "")
STT_FORWARD_PORT = int(os.environ.get("STT_FORWARD_PORT", "9002"))

_STT_FRAME_HELLO = 0x01
_STT_FRAME_AUDIO = 0x02
_STT_FRAME_BYE = 0x03


def _stt_pack(ftype: int, payload: bytes) -> bytes:

    return bytes([ftype]) + struct.pack("<I", len(payload)) + payload


class _STTForwarder:

    def __init__(
        self,
        conn_id: str,
        sample_rate: int,
        host: str,
        port: int,
    ):

        self.conn_id = conn_id
        self.sample_rate = sample_rate
        self.host = host
        self.port = port

        self.writer = None

        self.connected = False
        self.disabled = False

    async def open(self):

        if self.disabled or self.connected:
            return

        try:

            _, w = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=1.0,
            )

            self.writer = w

            hello = (
                f"conn_id={self.conn_id};sr={self.sample_rate}"
            ).encode("utf-8")

            w.write(_stt_pack(_STT_FRAME_HELLO, hello))

            await w.drain()

            self.connected = True

            log.info(
                "STT FORWARD open conn_id=%s -> %s:%d",
                self.conn_id,
                self.host,
                self.port,
            )

        except Exception as exc:

            self.disabled = True

            log.warning(
                "STT FORWARD open failed conn_id=%s err=%r",
                self.conn_id,
                exc,
            )

    def forward_audio(self, pcm_bytes: bytes):

        if (
            self.disabled
            or not self.connected
            or self.writer is None
        ):
            return

        try:

            self.writer.write(
                _stt_pack(_STT_FRAME_AUDIO, pcm_bytes)
            )

        except Exception as exc:

            self.disabled = True

            log.warning(
                "STT FORWARD write failed conn_id=%s err=%r",
                self.conn_id,
                exc,
            )

    async def close(self):

        if self.writer is None:
            return

        try:

            try:

                self.writer.write(
                    _stt_pack(_STT_FRAME_BYE, b"")
                )

                await self.writer.drain()

            except Exception:
                pass

            self.writer.close()

            try:
                await self.writer.wait_closed()
            except Exception:
                pass

        finally:

            self.writer = None
            self.connected = False

# ---------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------


def _fmt_t(cp: dict, key: str) -> str:

    v = cp.get(key)

    return f"{v:.3f}s" if v is not None else "—"


def _fmt_delta(cp: dict, a: str, b: str) -> str:

    va = cp.get(a)
    vb = cp.get(b)

    if va is None or vb is None:
        return "—"

    return f"+{(vb - va) * 1000:.1f}ms"


# ---------------------------------------------------------------------
# CONNECTION HANDLER
# ---------------------------------------------------------------------


async def handle_connection(websocket, path: str, args):

    import websockets

    fs_uuid = ""
    caller_number = ""
    dialed_did = ""
    campaign_id = None
    agent_slug = ""
    dialed_number = ""
    _rejected_agent_slug = ""

    try:

        from urllib.parse import urlsplit

        sp = urlsplit(path or "")

        parts = (sp.path or "").lstrip("/").split("/")

        seg = parts[0].strip() if parts else ""

        if (
            seg
            and 8 <= len(seg) <= 80
            and all(
                (c.isalnum() or c in "-_.")
                for c in seg
            )
        ):
            fs_uuid = seg

        # Positional path segments set by socket_server.py's
        # build_audio_stream_payload: /<uuid>/<caller>/<did>/<campaign>/<slug>.
        # "-" is the explicit empty placeholder (positions never shift);
        # anything failing its charset check is treated as absent.

        # Second segment carries the caller number (digits only, set by
        # socket_server.py from Caller-Caller-ID-Number header).
        if len(parts) > 1 and parts[1].strip().isdigit():
            caller_number = parts[1].strip()

        # Third segment: the dialed DID (digits only) — drives per-DID
        # agent routing in conversation_call_bridge.resolve_agent_slug.
        if len(parts) > 2 and parts[2].strip().isdigit():
            dialed_did = parts[2].strip()

        # Fourth segment: outbound campaign id (digits only).
        if len(parts) > 3 and parts[3].strip().isdigit():
            campaign_id = int(parts[3].strip())

        # Fifth segment: explicit agent slug from the lescall_agent_slug
        # channel variable (outbound binds the agent at dial time).
        if len(parts) > 4:
            s = parts[4].strip()
            if s and s != "-" and all(
                (c.isalnum() or c in "-_") for c in s
            ):
                agent_slug = s
            elif s and s != "-":
                # LOUD routing: a present-but-invalid slug segment used to be
                # dropped silently — the call then answered as whatever agent
                # legacy resolution picked, with zero trace of why.
                _rejected_agent_slug = s

        # Sixth segment: the number this OUTBOUND leg dialled, from the
        # lescall_dest channel variable. This is the only first-hand statement
        # about who an originated call actually reached — the channel's caller
        # id is our own DID and Caller-Destination-Number is the dialplan
        # extension. Without it the bridge had to guess from recent dial
        # timestamps, and during a campaign burst it guessed wrong (§33).
        if len(parts) > 5 and parts[5].strip().isdigit():
            dialed_number = parts[5].strip()

    except Exception as exc:

        # LOUD routing: log the parse failure BEFORE zeroing everything —
        # a malformed path silently downgrading a call to legacy agent
        # resolution must leave a trace.
        log.warning(
            "[WS] path parse failed for %r: %r — routing segments cleared",
            path,
            exc,
        )

        fs_uuid = ""
        caller_number = ""
        dialed_did = ""
        campaign_id = None
        agent_slug = ""
        dialed_number = ""
        _rejected_agent_slug = ""

    conn_id = fs_uuid if fs_uuid else uuidlib.uuid4().hex[:8]

    if _rejected_agent_slug:
        log.warning(
            "[%s] WS path agent slug rejected: %r",
            conn_id,
            _rejected_agent_slug,
        )

    t0 = time.monotonic()

    cp = {"T0_ws_open": 0.0}

    counters = {
        "binary_frames": 0,
        "binary_bytes": 0,
        "text_frames": 0,
        "stalls": 0,
        "max_gap_ms": 0.0,
        "audio_packets_forwarded": 0,
    }

    log.info(
        "=== WS CONNECT conn_id=%s path=%s ===",
        conn_id,
        path,
    )

    # --------------------------------------------------------------
    # START AI SESSION
    # --------------------------------------------------------------

    try:

        await call_bridge.get_or_create(
            conn_id,
            TARGET_SR,
            caller_number=caller_number,
            dialed_number=dialed_number,
            did=dialed_did,
            campaign_id=campaign_id,
            agent_slug=agent_slug,
        )

        log.info(
            "[CALL_BRIDGE] session created conn=%s",
            conn_id,
        )

    except Exception as exc:

        log.error(
            "[CALL_BRIDGE] failed conn=%s err=%r",
            conn_id,
            exc,
        )

        return

    # --------------------------------------------------------------
    # DEBUG CAPTURE
    # --------------------------------------------------------------

    p15_debug = _Phase15AudioDebugger(
        conn_id,
        TARGET_SR,
    )

    resample_state = None

    resample_count = 0

    # --------------------------------------------------------------
    # OPTIONAL STT FORWARDER
    # --------------------------------------------------------------

    stt_fwd = None

    if STT_FORWARD_HOST:

        stt_fwd = _STTForwarder(
            conn_id=conn_id,
            sample_rate=TARGET_SR,
            host=STT_FORWARD_HOST,
            port=STT_FORWARD_PORT,
        )

        try:
            await stt_fwd.open()
        except Exception:
            stt_fwd = None

    last_binary_t = None

    try:

        async for msg in websocket:

            try:

                now_abs = time.monotonic()

                now = now_abs - t0

                raw_bytes = b""

                # --------------------------------------------------
                # BINARY FRAMES
                # --------------------------------------------------

                if isinstance(msg, (bytes, bytearray)):

                    raw_bytes = bytes(msg)

                # --------------------------------------------------
                # TEXT PAYLOADS
                # --------------------------------------------------

                else:

                    counters["text_frames"] += 1

                    try:

                        text_str = str(msg)

                        if text_str.startswith("{"):

                            payload = json.loads(text_str)

                            if (
                                "event" in payload
                                and payload["event"] == "media"
                            ):

                                raw_bytes = base64.b64decode(
                                    payload["media"]["payload"]
                                )

                    except Exception:
                        pass

                # --------------------------------------------------
                # IGNORE EMPTY
                # --------------------------------------------------

                if not raw_bytes:
                    continue

                size = len(raw_bytes)

                preview = raw_bytes[:8].hex()

                log.debug(
                    "[WS_FRAME] conn=%s bytes=%d preview=%s",
                    conn_id,
                    size,
                    preview,
                )

                counters["binary_frames"] += 1
                counters["binary_bytes"] += size

                if "T1_first_binary" not in cp:

                    cp["T1_first_binary"] = now

                cp["T1_last_binary"] = now

                if last_binary_t is not None:

                    gap_ms = (
                        (now - last_binary_t) * 1000.0
                    )

                    if gap_ms > counters["max_gap_ms"]:
                        counters["max_gap_ms"] = gap_ms

                    if gap_ms > GAP_STALL_MS:
                        counters["stalls"] += 1

                last_binary_t = now

                # --------------------------------------------------
                # DEBUG SAVE
                # --------------------------------------------------

                p15_debug.process_frame(raw_bytes)

                # --------------------------------------------------
                # NORMALIZATION
                # --------------------------------------------------

                norm_msg = raw_bytes

                try:

                    if log.isEnabledFor(logging.DEBUG):
                        pre_rms = -1

                        if (
                            len(norm_msg) >= 2
                            and len(norm_msg) % 2 == 0
                        ):

                            try:
                                pre_rms = audioop.rms(
                                    norm_msg,
                                    2,
                                )
                            except Exception:
                                pass

                        log.debug(
                            "[DECODE_PRE] conn=%s bytes=%d rms=%d codec=%s",
                            conn_id,
                            len(norm_msg),
                            pre_rms,
                            INPUT_CODEC,
                        )

                    performed_ulaw_decode = False

                    # ------------------------------------------
                    # DECODE PCMU (only if explicitly configured)
                    # ------------------------------------------
                    # mod_audio_stream delivers L16 PCM directly from the
                    # FreeSWITCH media bug (switch_core_media_bug_read always
                    # returns decoded L16). Default INPUT_CODEC is "l16" so
                    # this block is skipped. Only set TELEPHONY_INPUT_CODEC=pcmu
                    # if a custom mod_audio_stream variant delivers raw PCMU bytes.

                    if INPUT_CODEC == "pcmu":

                        if len(norm_msg) > 0:

                            norm_msg = audioop.ulaw2lin(
                                norm_msg,
                                2,
                            )

                            performed_ulaw_decode = True

                            log.debug(
                                "[ULAW_DECODE] conn=%s decoded %dB PCMU -> %dB PCM16",
                                conn_id,
                                len(norm_msg) // 2,
                                len(norm_msg),
                            )

                    # ------------------------------------------
                    # VALIDATE PCM
                    # ------------------------------------------

                    if len(norm_msg) == 0:

                        log.warning(
                            "[AUDIO_DROP] empty frame conn=%s",
                            conn_id,
                        )

                        continue

                    if len(norm_msg) % 2 != 0:

                        log.warning(
                            "[AUDIO_DROP] odd pcm bytes=%d conn=%s",
                            len(norm_msg),
                            conn_id,
                        )

                        continue

                    # ------------------------------------------
                    # RESAMPLE
                    # ------------------------------------------

                    if TARGET_SR != args.sample_rate:

                        before_resample = len(norm_msg)

                        norm_msg, resample_state = (
                            audioop.ratecv(
                                norm_msg,
                                2,
                                1,
                                args.sample_rate,
                                TARGET_SR,
                                resample_state,
                            )
                        )

                        # Hot path: one frame every 20ms. Per-frame lines go to
                        # DEBUG; INFO gets a summary every 500 frames (~10s).
                        resample_count += 1
                        if resample_count % 500 == 1:
                            log.info(
                                "[RESAMPLE] conn=%s %dB -> %dB (frame %d)",
                                conn_id,
                                before_resample,
                                len(norm_msg),
                                resample_count,
                            )
                        elif log.isEnabledFor(logging.DEBUG):
                            log.debug(
                                "[RESAMPLE] conn=%s %dB -> %dB",
                                conn_id,
                                before_resample,
                                len(norm_msg),
                            )

                    # ------------------------------------------
                    # FINAL RMS (debug only — skip the per-frame
                    # rms computation entirely when not logging it)
                    # ------------------------------------------

                    # NOTE: final_rms must be bound unconditionally — it is read
                    # again by the [AUDIO_PIPE] log call below, and logging
                    # evaluates its arguments eagerly regardless of level. When
                    # this was assigned only under the DEBUG guard, every frame
                    # raised UnboundLocalError into [NORMALIZATION_FATAL].
                    final_rms = -1

                    if log.isEnabledFor(logging.DEBUG):
                        final_rms = audioop.rms(
                            norm_msg,
                            2,
                        )

                        log.debug(
                            "[PCM_LEVEL] conn=%s rms=%d bytes=%d",
                            conn_id,
                            final_rms,
                            len(norm_msg),
                        )

                    # ------------------------------------------
                    # STT FORWARD
                    # ------------------------------------------

                    if stt_fwd is not None:

                        stt_fwd.forward_audio(norm_msg)

                    # ------------------------------------------
                    # CALL AUDIT — capture normalized PCM
                    # ------------------------------------------

                    audit_manager.push_audio(conn_id, norm_msg)

                    # ------------------------------------------
                    # PUSH TO PIPELINE
                    # ------------------------------------------

                    call_bridge.push_audio(
                        conn_id,
                        norm_msg,
                    )
                    counters["audio_packets_forwarded"] += 1

                    queued = 0

                    try:

                        queued = (
                            call_bridge.sessions[conn_id]
                            .source.queue.qsize()
                        )

                    except Exception:
                        pass

                    log.debug(
                        "[AUDIO_PIPE] conn=%s received=%d normalized=%d queued=%d rms=%d",
                        conn_id,
                        size,
                        len(norm_msg),
                        queued,
                        final_rms,
                    )

                except Exception as exc:

                    log.error(
                        "[NORMALIZATION_FATAL] conn=%s err=%r",
                        conn_id,
                        exc,
                    )

            except Exception as exc:

                log.error(
                    "[WS_FRAME_ERROR] conn=%s err=%r",
                    conn_id,
                    exc,
                )

    except websockets.exceptions.ConnectionClosed as exc:

        log.info(
            "[MEDIA_STREAM_CLOSED] conn=%s code=%s",
            conn_id,
            exc.code,
        )

    except Exception as exc:

        log.error(
            "[MEDIA_STREAM_FAILED] conn=%s err=%r",
            conn_id,
            exc,
        )

    finally:

        cp["T2_ws_close"] = time.monotonic() - t0

        p15_debug.close()

        if stt_fwd is not None:

            await stt_fwd.close()

        try:

            await call_bridge.remove(conn_id)

        except Exception:
            pass

        log.info(
            "[SESSION_END] conn=%s frames_received=%d bytes=%d frames_forwarded=%d",
            conn_id,
            counters["binary_frames"],
            counters["binary_bytes"],
            counters["audio_packets_forwarded"],
        )

# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------


def _snapshot_live_calls() -> dict:
    """Build the live-call snapshot dict from in-memory session state.

    Read-only: only ever reads attributes off existing session/pipeline
    objects, never sets anything. If any expected attribute is missing
    (e.g. a session mid-teardown), that call is skipped rather than
    raising — this must never be able to affect call handling.
    """
    calls = []
    for conn_id, session in list(call_bridge.sessions.items()):
        try:
            pipeline = session.pipeline
            conv_state = getattr(getattr(pipeline, "conversation", None), "state", None)
            stage = getattr(conv_state, "stage", None)
            language = getattr(conv_state, "language", None)
            tts_playing = bool(getattr(pipeline, "_tts_playing", False))
            user_speaking = bool(getattr(pipeline, "_user_speaking", False))
            speaking = "ai" if tts_playing else ("user" if user_speaking else "listening")
            start_time_epoch = session.stats.get("start_time")
            start_time_iso = (
                datetime.datetime.fromtimestamp(start_time_epoch, tz=datetime.timezone.utc).isoformat()
                if start_time_epoch else None
            )
            calls.append({
                "conn_id": conn_id,
                "caller_number": getattr(session, "caller_number", "") or "",
                "start_time": start_time_iso,
                "language": language,
                "stage": stage,
                "speaking": speaking,
                "transcript_tail": list(getattr(session, "transcript_tail", []) or []),
                # Tenant that owns this call (from the resolved agent) so the
                # dashboard can show each tenant only their own live calls.
                "owner": getattr(session, "owner", "") or "",
                "agent_slug": getattr(session, "agent_slug", "") or "",
                "direction": getattr(session, "direction", "") or "",
            })
        except Exception as exc:
            log.warning("[LIVE-STATE] Skipped snapshot for %s: %r", conn_id, exc, exc_info=True)
    return {"updated_at": time.time(), "calls": calls}


async def _live_state_writer_loop():
    """Background task: snapshot active calls to disk every ~1.5s."""
    tmp_path = LIVE_STATE_PATH + ".tmp"
    while True:
        try:
            snapshot = _snapshot_live_calls()
            os.makedirs(os.path.dirname(LIVE_STATE_PATH), exist_ok=True)
            with open(tmp_path, "w") as f:
                json.dump(snapshot, f)
            os.replace(tmp_path, LIVE_STATE_PATH)
        except Exception:
            log.warning("[LIVE-STATE] Snapshot write failed", exc_info=True)
        await asyncio.sleep(LIVE_STATE_INTERVAL_S)


async def _boot_warmup():
    """Warm the shared Sarvam STT/TTS connection pools and the LLM endpoint
    at process boot instead of racing the first call's first utterance.

    With SARVAM_HTTP_SHARED (default on) the pools opened here are the exact
    clients every call reuses. Best-effort: any failure is logged and the
    per-call background warmup remains as fallback. BOOT_WARMUP=false skips.
    """
    if os.environ.get("BOOT_WARMUP", "true").lower() != "true":
        return
    try:
        from backend.app.voice.stt.sarvam import SarvamSTT
        from backend.app.voice.tts_sarvam import SarvamTTS
        from backend.app.audio_transport.telephony_sink import TelephonySink

        async def _llm_warm():
            from backend.app.voice.conversation import ConversationRouter
            from backend.app.agent_definition.fallback_agent import (
                get_neutral_fallback_agent,
            )
            await ConversationRouter(
                agent_def=get_neutral_fallback_agent()).warmup()

        # Preload the shared FSMN VAD model so no call ever pays the
        # construction lag (roadmap #3 warm-model).
        if os.environ.get("VAD_ENGINE", "").strip().lower() == "fsmn":
            try:
                from backend.app.voice.vad_stack import preload_model
                await asyncio.to_thread(preload_model)
            except Exception as exc:
                log.warning("[BOOT-WARMUP] FSMN preload failed "
                            "(per-call load remains): %r", exc)

        stt = SarvamSTT()
        # Throwaway sink: SarvamTTS's default LocalSpeakerSink may probe an
        # audio device that headless servers lack.
        tts = SarvamTTS(audio_sink=TelephonySink(conn_id="boot-warmup"))
        await asyncio.wait_for(
            asyncio.gather(
                stt.warmup(), tts.warmup(), _llm_warm(),
                return_exceptions=True,
            ),
            timeout=10.0,
        )
        log.info("[BOOT-WARMUP] STT/TTS/LLM warmup complete")
    except Exception as exc:
        log.warning("[BOOT-WARMUP] failed (per-call warmup remains): %r", exc)


async def main_async(args):

    import websockets

    asyncio.create_task(_live_state_writer_loop())

    asyncio.create_task(_boot_warmup())

    log.info(
        "ws_audio_receiver listening on ws://%s:%d",
        args.host,
        args.port,
    )

    async def _entry(ws):

        path = getattr(ws, "path", None)

        if path is None and hasattr(ws, "request"):

            path = ws.request.path

        await handle_connection(
            ws,
            path,
            args,
        )

    async with websockets.serve(
        _entry,
        args.host,
        args.port,
        max_size=2**20,
        # Send WebSocket pings every 20s so FreeSWITCH mod_audio_stream
        # does not idle-timeout the connection at ~30s and drop the call.
        ping_interval=20,
        ping_timeout=10,
    ):

        await asyncio.Future()


def main():

    p = argparse.ArgumentParser()

    p.add_argument(
        "--host",
        default=HOST_DEFAULT,
    )

    p.add_argument(
        "--port",
        type=int,
        default=PORT_DEFAULT,
    )

    p.add_argument(
        "--sample-rate",
        type=int,
        default=8000,
    )

    p.add_argument(
        "--bytes-per-sample",
        type=int,
        default=2,
    )

    args = p.parse_args()

    try:

        asyncio.run(main_async(args))

    except KeyboardInterrupt:

        pass


if __name__ == "__main__":

    main()