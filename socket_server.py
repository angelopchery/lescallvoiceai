#!/usr/bin/env python3
"""
Outbound ESL Debug Server — Phase 6 (Recording Finalization Fix)
================================================================

Phase 1 (handshake stabilization), Phase 2 (single playback execute
probe), Phase 3 (RTP/media-plane observability + tcpdump-command
generation + deterministic disconnect cleanup), and Phase 4 (passive
inbound media capture) are preserved verbatim. Phase 5 reorders the
dispatch so the recording bug is attached BEFORE playback begins, adds
explicit phase-5 timing checkpoints, enriches WAV validation with PCM
and non-silence checks, and emits a structured FULL_DUPLEX_VERDICT
with operator listening instructions. No STT, no TTS, no AI, no
websockets, no media-fork streaming, no buffering framework, no
multiprocessing, no asyncio. The validated playback execute path
remains byte-for-byte untouched.

Phase 3 layered three observational capabilities and one
deterministic-cleanup fix:

  P3-A  Extracts RTP/media fields from CHANNEL_DATA and logs a
        RTP SESSION INFO block (local/remote IP+port, advertised IP,
        codec/rate/ptime, RTP+DTMF payload types).
  P3-B  Auto-generates exact tcpdump commands using the actual
        negotiated ports — capture, outbound-count, per-second-rate.
        WE DO NOT INVOKE tcpdump. The operator runs them manually.
  P3-C  Adds NEXT REQUIRED ACTION block to the post-mortem when the
        verdict is CASE C/H, naming the exact decision rule that
        resolves CASE C.1 vs CASE C.2/H from the tcpdump output.
  P3-D  On text/disconnect-notice the handler now sends `exit\\n\\n`
        and BREAKS so the SESSION SUMMARY block is emitted
        deterministically. The previous `continue` left the daemon
        thread blocked in recv() (FS holds the socket open under
        Linger-Time: -1) until Ctrl+C killed the thread without
        running the finally block.

Phase 4 adds:

  P4-A  RECORDING_DIR (/tmp/esl_recordings) is pre-created at server
        start. New helpers build_record_payload, validate_wav, and
        log_recording_validation are added. No existing helper is
        touched.
  P4-B  AFTER the validated PLAYBACK_EXECUTE sendmsg, dispatches a
        second sendmsg execute record_session against the same channel
        with NO event-lock so the FS media bug attaches in PARALLEL
        with playback (which holds the channel lock for ~6s). One WAV
        per Channel-Call-UUID is opened by FS at
        /tmp/esl_recordings/<uuid>.wav. Failure of this sendmsg is
        non-fatal: playback is already in flight on the socket.
  P4-C  WAITING-loop classification gains four NEW elif branches:
        CHANNEL_EXECUTE / CHANNEL_EXECUTE_COMPLETE for
        Application=record_session, plus RECORD_START / RECORD_STOP.
        Inserted before the existing non-playback catch-all so no
        prior branch's match conditions change.
  P4-D  After SESSION SUMMARY and NEXT REQUIRED ACTION, finally now
        emits a RECORDING VALIDATION block: file existence, byte size,
        channels, sample rate, sample width, approximate duration, and
        whether the WAV contains non-empty audio frames. Pure local
        read; cannot affect socket teardown.

Phase 5 adds:

  P5-A  Dispatch reorder ONLY: record_session sendmsg now precedes
        playback sendmsg on the wire. Both payloads are byte-for-byte
        unchanged. record_session has no event-lock and is a fast
        media-bug attach inside FS, so it completes before playback
        takes the channel lock. Net effect: the WAV captures the FULL
        playback window (and everything after it until hangup), not
        just whatever sneaks past the start.
  P5-B  New explicit timing checkpoints reflecting the reordered
        timeline: T3_record_sent, T4_record_execute, T5_playback_sent,
        T6_playback_execute, T7_playback_complete. These are
        DUAL-WRITTEN alongside the legacy checkpoint keys so the
        Phase-3 classify() verdict logic is unchanged.
  P5-C  validate_wav now also reports PCM compression type and a peak
        absolute amplitude scan across the first 30 seconds, plus a
        non_silence boolean derived from a conservative noise-floor
        threshold. Pure read; cannot affect playback.
  P5-D  New full_duplex_verdict() function maps (playback verdict,
        WAV validation) to one of FULL_DUPLEX_CONFIRMED, PLAYBACK_ONLY,
        INBOUND_ONLY, EMPTY_RECORDING, RECORDING_FAILED, PARTIAL_DUPLEX.
        This is a NEW verdict — does not replace or modify Phase-3's
        CASE A..H classify() output.
  P5-E  Post-call OPERATOR INSTRUCTIONS block tells the operator how
        to manually listen to the WAV and what each possible audible
        outcome means, with the exact file path embedded.
  P5-F  Zero-regression safeguards: if no CHANNEL_EXECUTE for playback
        was observed, OR Application-Response was not 'FILE PLAYED',
        the finally block emits a CRITICAL PHASE-5 REGRESSION line in
        addition to the standard verdict — this is the explicit signal
        that Phase-5's reordering broke the validated playback path.
  P5-G  classify() is unchanged. The Phase-3 verdict still drives the
        NEXT REQUIRED ACTION block. P5-D's full-duplex verdict is
        purely additive.

Phase 9 adds (live-audio websocket fork — purely additive):

  P9-A  Two new constants and two new helpers, all additive:
            AUDIO_STREAM_WS_URL, AUDIO_STREAM_SAMPLE_RATE,
            build_audio_stream_payload(uuid, ws_url, sample_rate),
            build_audio_stream_stop_payload(uuid).
        These produce single-line `bgapi uuid_audio_stream ...`
        payloads (LF-LF terminated) for mod_audio_stream, which is
        registered as an FS API command, not a dialplan app.
  P9-B  Between Phase-5's record_session sendmsg and Phase-2's
        playback sendmsg, dispatch ONE bgapi uuid_audio_stream
        start. New handler-scope flag `audio_stream_started`. Two
        new checkpoint keys (T3c_audio_stream_sent,
        T9_audio_stream_started). Failure to start the stream is
        NON-FATAL: control falls through to the validated playback
        path. PLAYBACK_PAYLOAD bytes remain byte-identical.
  P9-C  In the existing CHANNEL_HANGUP, CHANNEL_HANGUP_COMPLETE
        and disconnect-notice branches, dispatch
        bgapi uuid_audio_stream stop ONCE (idempotent via
        `audio_stream_stop_sent`). The existing post_stop_drain
        window covers both record_session AND audio_stream stop
        acks because mod_audio_stream's stop returns +OK in the
        same drain.
  P9-D  SESSION SUMMARY gains a `phase9_timeline:` block (mirror of
        phase5_timeline) and the `counters` dict gains
        audio_stream_start / audio_stream_stop / audio_stream_err
        keys. classify() and the Phase-7 decision engine are NOT
        touched.

What Phase 9 does NOT change
----------------------------
* PLAYBACK_PAYLOAD bytes — byte-for-byte identical Phase 2..9.
* build_record_payload bytes — byte-for-byte identical Phase 4..9.
* build_stop_record_payload bytes — byte-for-byte identical Phase
  6..9.
* The handshake (connect / linger / myevents) and its byte order.
* RTP / codec / async-full / dialplan flow.
* classify() decision matrix and the Phase-7 R1/R2/R3/R4 engine.
* All Phase 1..8 logging blocks (RTP SESSION INFO, TCPDUMP COMMANDS,
  SESSION SUMMARY, RECORDING VALIDATION, FULL DUPLEX VERDICT,
  OPERATOR INSTRUCTIONS, A/B COMPARISON, FS INTERNAL DIAGNOSTICS,
  PHASE-7 DECISION ENGINE).
* No new external Python deps (bgapi is plain ESL text).
* No STT / LLM / TTS / VAD / interruption / barge-in.

Phase 9 rollback
----------------
* Set AUDIO_STREAM_WS_URL = "" to disable dispatch (the existing
  guard skips the start sendmsg when empty).
* OR `fs_cli -x 'unload mod_audio_stream'` and remove the
  `<load module="mod_audio_stream"/>` line from
  /etc/freeswitch/autoload_configs/modules.conf.xml.

Phase 7 adds (DIAGNOSTIC ONLY — no behaviour change):

  P7-A  Dialplan-side change ONLY (in /etc/freeswitch/dialplan/public.xml):
        a single record_session /tmp/dialplan_test.wav action is
        inserted between `sleep 1000` and `socket 127.0.0.1:9000 async
        full`. The socket-server.py side does NOT change handshake,
        playback, RTP, framing, or any existing log line.
  P7-B  After OPERATOR INSTRUCTIONS (and only there) the finally block
        now emits, in order:
          * DIALPLAN RECORDING VALIDATION  — validate_wav() on
            /tmp/dialplan_test.wav (the file produced by the
            dialplan-native record_session, independent of ESL).
          * A/B COMPARISON                 — side-by-side table.
          * FS INTERNAL DIAGNOSTICS        — best-effort `fs_cli -x
            "show modules"` filtered for recording modules, plus a
            check on RECORDING_DIR ownership/perms vs the freeswitch
            user. This block NEVER raises out.
          * PHASE-7 DECISION ENGINE        — emits exactly one of
            CASE R1 / R2 / R3 / R4 based on the two validation
            results.
  P7-C  Phase 7 adds NO new payloads, NO new sendmsg, NO new wire
        traffic. Every Phase-2..6 invariant is preserved byte-for-byte.

Phase 7 rollback
----------------
  socket_server.py: this code path is purely additive in the finally
  block AFTER OPERATOR INSTRUCTIONS. To roll back, delete the four
  log_* / compare_* / decision_* calls between OPERATOR INSTRUCTIONS
  and conn.shutdown.

  dialplan: restore /etc/freeswitch/dialplan/public.xml from
  /etc/freeswitch/dialplan/public.xml.pre-phase7.bak and run
  `sudo fs_cli -x reloadxml`.

Phase 6 adds:

  P6-A  Two new helpers and one constant: build_stop_record_payload(),
        post_stop_drain(), POST_STOP_WAIT_S=0.7. No existing helper is
        modified.
  P6-B  In the existing CHANNEL_HANGUP and CHANNEL_HANGUP_COMPLETE
        branches inside the WAITING loop, dispatch
        sendmsg execute stop_record_session ONCE (idempotent via
        stop_record_sent flag) so FS gets an explicit signal to detach
        the recording bug and finalize the WAV header before the
        channel is fully torn down. The branches still log exactly as
        before; the stop dispatch is purely additive.
  P6-C  In the existing disconnect-notice branch, the teardown order
        is now:
            (1) send stop_record_session if not already sent
            (2) drain frames for up to POST_STOP_WAIT_S so FS has a
                window to emit +OK and RECORD_STOP and rewrite the
                WAV header on disk
            (3) send exit
            (4) break to finally
        Step (2) and the optional step (1) are the only new behaviour;
        the existing P3-D `exit` and break still happen unchanged at
        the end.

What Phase 6 does NOT change:

* PLAYBACK_PAYLOAD bytes — byte-for-byte identical from Phase 2.
* build_record_payload bytes — byte-for-byte identical from Phase 4.
* The handshake (connect / linger / myevents) and its byte order.
* Phase-3 RTP-info extraction, tcpdump-command generation, classify()
  decision matrix, and NEXT REQUIRED ACTION block.
* Phase-4 RECORD_START / RECORD_STOP event observation.
* Phase-5 dispatch order (record_session before playback) and the
  full-duplex verdict.
* Threading model, async/full ownership (lives in dialplan), socket
  architecture, framing.
* All existing logs.

What is intentionally NOT here
------------------------------
* No retries, no fallbacks, no chained commands.
* No dialplan changes. No async/full changes.
* No GCP firewall, ext-rtp-ip, NAT, or codec changes.
* No tcpdump invocation — only command generation.
* No new external dependencies (wave + contextlib are stdlib).
* No change to existing CHANNEL_EXECUTE / CHANNEL_EXECUTE_COMPLETE
  detection for Application=playback (P4-C only adds NEW elif
  branches before the existing non-playback catch-all).
* No change to PLAYBACK_PAYLOAD bytes.
* No STT, TTS, AI, websockets, multiprocessing, asyncio, or
  media-fork streaming. Phase 5 only reorders dispatch and adds
  validation/verdict reporting on top of Phase 4's FS-native
  record_session.
* No stop_record_session — FS auto-stops the recording on hangup.
* No replacement of validated playback bytes. PLAYBACK_PAYLOAD
  remains byte-for-byte identical from Phase 2 through Phase 5.

State machine (Phase 5 dispatch order)
--------------------------------------
    CONNECTED
        | send  connect\\n\\n                                  (T1)
        v
    SENT_CONNECT
        | recv  CHANNEL_DATA  -> log RTP SESSION INFO          (T2)
        |                     -> log TCPDUMP COMMANDS
        v
    RECEIVED_CHANNEL_DATA
        | send  linger\\n\\n
        v
    SENT_LINGER
        | send  myevents\\n\\n
        v
    SENT_MYEVENTS
        | send  sendmsg/execute record_session                 (T3)
        v
    SENT_RECORD
        | send  sendmsg/execute playback tone_stream           (T5)
        v
    SENT_PLAYBACK
        | recv  command/reply (+OK or -ERR — sendmsg ack) x2
        | recv  CHANNEL_EXECUTE  Application=record_session    (T4)
        | recv  RECORD_START      (FS opens WAV)
        | recv  CHANNEL_EXECUTE  Application=playback          (T6)
        | recv  CHANNEL_EXECUTE_COMPLETE Application=playback  (T7)
        v
    WAITING ----> DISCONNECTED                                 (T6_disc)
                  | send exit\\n\\n, break                     (P3-D)
                  v
                  finally: SESSION SUMMARY + NEXT REQUIRED ACTION
                           + RECORDING VALIDATION
                           + FULL DUPLEX VERDICT
                           + OPERATOR INSTRUCTIONS

Decision matrix — interpret a single run by these rules
-------------------------------------------------------
  CASE A   sendmsg malformed
  CASE B   accepted, never executed
  CASE C   executed cleanly (EXECUTE@T4 + COMPLETE@T5 + FILE PLAYED)
           but caller still silent. Phase 3 splits this into:
             C.1  RTP IS leaving FS (downstream loss)
             C.2  RTP is NOT leaving FS (== H without external RTP
                  inspection — collapses to one verdict here)
  CASE D   executed after disconnect
  CASE E   async/full queue behaviour
  CASE F   event-lock semantics
  CASE G   playback application failure
  CASE H   media path failure

Phase 3 produces tcpdump commands that resolve C.1 vs C.2/H.

ESL framing rules (unchanged from Phase 1)
------------------------------------------
* LF line terminator. Never CRLF.
* Header block ends at the first blank line (\\n\\n).
* If Content-Length: N is present, exactly N body bytes follow.
* sendmsg/execute payload format used here:
      sendmsg
      call-command: execute
      execute-app-name: <app>
      execute-app-arg: <arg>
      event-lock: true
      <blank line, i.e. final \\n\\n>
"""

import re
import socket
import threading
import time
import logging
import os
import json
import struct
import wave
import contextlib

# Loopback by default: this socket IS the FreeSWITCH ESL controller, and
# FreeSWITCH runs on this host. Binding 0.0.0.0 exposed the call-control plane
# to anyone who could reach the VM on 9000. Override only if FreeSWITCH moves
# off-box (and then firewall it).
HOST = os.environ.get("LESCALL_SOCKET_HOST", "127.0.0.1")
PORT = int(os.environ.get("LESCALL_SOCKET_PORT", "9000"))

# ESL frame-end marker. Two LFs, no CR.
END = b"\n\n"

# The ONE Phase-2 probe. Bytes literal → no platform line-ending mangling.
PLAYBACK_PAYLOAD = (
    b"sendmsg\n"
    b"call-command: execute\n"
    b"execute-app-name: playback\n"
    b"execute-app-arg: tone_stream://%(2000,4000,440)\n"
    b"event-lock: true\n"
    b"\n"
)

# Phase 3 graceful teardown. After text/disconnect-notice (Linger-Time:
# -1) FS keeps the TCP open expecting a final command. Sending `exit`
# tells FS we're done so it closes deterministically and our finally
# block can print the SESSION SUMMARY before the thread dies.
EXIT_PAYLOAD = b"exit\n\n"

# Phase 4 (P4-A): passive inbound media capture target. /tmp because it
# is writable by the freeswitch user on the standard install, has no
# SELinux constraints in the dev environment, and is acceptable for
# short-lived validation WAVs. One file per Channel-Call-UUID means
# concurrent calls never collide on filenames.
RECORDING_DIR = "/tmp/esl_recordings"

# Phase 6 (P6-A): post stop_record_session drain window. After we
# explicitly stop the recording we wait this long for FS to deliver
# the +OK ack and the RECORD_STOP event, which is how we know the WAV
# header has been finalized on disk. 700 ms sits in the user-specified
# 500..1000 ms band and is well below FS's typical hangup grace window.
POST_STOP_WAIT_S = 0.7

# Phase 7 (P7-A): canonical path of the dialplan-native record_session
# action that runs before the socket app. Lives in world-writable /tmp
# so it cannot fail because of a directory-permission issue. The
# Phase-7 finally-block diagnostics validate this WAV alongside the
# ESL-recorded one and surface the comparison.
DIALPLAN_TEST_WAV = "/tmp/dialplan_test.wav"

# Phase 7 (P7-C): tokens for the deterministic decision engine.
P7_CASE_R1 = ("CASE R1 — Dialplan recording WORKS, ESL recording FAILS"
              " -> outbound ESL execution-context / media-bug issue")
P7_CASE_R2 = ("CASE R2 — Both dialplan and ESL recordings FAIL"
              " -> lower-level FS recording / media subsystem issue")
P7_CASE_R3 = ("CASE R3 — Both dialplan and ESL recordings WORK"
              " -> previous failure was an ESL recording-sequencing"
              " issue and is now resolved")
P7_CASE_R4 = ("CASE R4 — Dialplan recording WORKS but ESL recording"
              " is zero-byte / silent -> media attachment timing /"
              " routing issue on the ESL execute path")
P7_CASE_INC = ("PHASE-7 INCONCLUSIVE — neither recording produced"
               " a usable artefact; review log evidence above")

# Phase 9 (P9-A): live-audio websocket fork target. mod_audio_stream
# opens a WS connection from FS to this URL and pushes mono L16 PCM
# binary frames at the configured sample rate. Default points at the
# local ws_audio_receiver.py listener (loopback, port 9001). Set to
# "" to disable the audio_stream dispatch entirely (rollback path).
AUDIO_STREAM_WS_URL = os.environ.get(
    "AUDIO_STREAM_WS_URL", "ws://127.0.0.1:9001")
AUDIO_STREAM_SAMPLE_RATE = int(
    os.environ.get("AUDIO_STREAM_SAMPLE_RATE", "8000"))

# Phase 12 (P12-PL): localhost playback-injection listener. The
# conversation_orchestrator.py process (a separate program) connects
# here once per AI turn and asks us to play a synthesized WAV back
# into the live call. Disabled with PLAYBACK_LISTEN_ENABLED=false
# (rollback path). The listener runs on a daemon thread so a stuck
# accept() cannot block process shutdown. The validated Phase-2
# PLAYBACK_PAYLOAD path for the canonical tone is COMPLETELY
# UNTOUCHED — Phase 12 only adds a NEW dispatch path for file://
# WAVs originated by the orchestrator.
PLAYBACK_LISTEN_HOST = os.environ.get("PLAYBACK_LISTEN_HOST",
                                      "127.0.0.1")
PLAYBACK_LISTEN_PORT = int(os.environ.get("PLAYBACK_LISTEN_PORT",
                                          "9004"))
PLAYBACK_LISTEN_ENABLED = (
    os.environ.get("PLAYBACK_LISTEN_ENABLED", "true").lower()
    not in ("false", "0", "no"))
# Cap inbound playback-request frame size. Even an absurdly long WAV
# path is well under 4 KiB; we cap at 64 KiB to make a malformed
# producer impossible to OOM us with.
PLAYBACK_MAX_FRAME_BYTES = 64 * 1024


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s.%(msecs)03d %(levelname)-5s [%(threadName)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("esl")


# --------------------------- helpers (Phase 1) ------------------------

def parse_headers(block: bytes) -> dict:
    out = {}
    for line in block.split(b"\n"):
        if not line or b":" not in line:
            continue
        k, _, v = line.partition(b":")
        out[k.strip().decode("utf-8", "replace")] = v.strip().decode("utf-8", "replace")
    return out


def read_frame(sock: socket.socket, buf: bytes):
    """Read exactly one ESL frame.
    (headers, body, raw, leftover_buf) on success.
    (None, None, b"", buf) on EOF.
    Raises socket.timeout on recv timeout."""
    while END not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            return None, None, b"", buf
        buf += chunk
    head, _, buf = buf.partition(END)
    raw = head + END
    headers = parse_headers(head)
    body = b""
    cl = int(headers.get("Content-Length", "0") or 0)
    if cl:
        while len(buf) < cl:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        body = buf[:cl]
        buf = buf[cl:]
        raw = raw + body
    return headers, body, raw, buf


def event_name(headers: dict, body: bytes) -> str:
    if "Event-Name" in headers:
        return headers["Event-Name"]
    for line in body.split(b"\n"):
        if line.startswith(b"Event-Name:"):
            return line.partition(b":")[2].strip().decode("utf-8", "replace")
    return ""


def looks_like_channel_data(headers: dict, body: bytes) -> bool:
    if any(k.startswith("Channel-") or k == "Unique-ID" for k in headers):
        return True
    if b"Event-Name: CHANNEL_DATA" in body:
        return True
    return False


# --------------------------- helpers (Phase 2) ------------------------

def body_field(body: bytes, key: str) -> str:
    """Pull a single 'Key: value' line out of an event-plain body."""
    needle = (key + ":").encode()
    for line in body.split(b"\n"):
        if line.startswith(needle):
            return line.partition(b":")[2].strip().decode("utf-8", "replace")
    return ""


def send_raw(conn: socket.socket, payload: bytes, label: str, t0: float) -> None:
    """sendall() with byte-level forensics."""
    now = time.monotonic() - t0
    log.info("SEND[%s] +%.3fs  len=%dB", label, now, len(payload))
    log.info("SEND[%s] repr=%r", label, payload)
    log.info("SEND[%s] checks  LF=%d  CR=%d  TAB=%d  ends_LFLF=%s",
             label,
             payload.count(b"\n"), payload.count(b"\r"),
             payload.count(b"\t"), payload.endswith(b"\n\n"))
    log.debug("SEND[%s] hex(<=256)=%s", label,
              " ".join(f"{b:02x}" for b in payload[:256]))
    conn.sendall(payload)
    log.info("SEND[%s] sendall returned at +%.3fs",
             label, time.monotonic() - t0)


def fmt_t(cp: dict, key: str) -> str:
    v = cp.get(key)
    return f"{v:7.3f}s" if v is not None else "       —"


def fmt_delta(cp: dict, a: str, b: str) -> str:
    va, vb = cp.get(a), cp.get(b)
    if va is None or vb is None:
        return "       —"
    return f"{(vb - va) * 1000:+8.1f}ms"


def reply_is_ok(reply: str) -> bool:
    return reply.startswith("+OK") or reply.startswith("%2BOK")


def reply_is_err(reply: str) -> bool:
    return reply.startswith("-ERR") or reply.startswith("%2DERR")


# --------------------------- helpers (Phase 3) ------------------------

# Channel-Call-UUID values are plain UUIDs; Channel-Name and other text
# fields may be URL-encoded. We do NOT decode here — raw values are
# fine for logging and tcpdump filters never reference channel names.

def extract_rtp_info(headers: dict) -> dict:
    """Pull every RTP/media field we need for tcpdump filtering and
    expected-rate calculation directly from the CHANNEL_DATA headers.
    Missing fields default to empty strings rather than guessed values
    — guessing here would produce subtly wrong tcpdump filters."""
    g = headers.get

    # Caller-Caller-ID-Number is URL-encoded (e.g. %2B919599761331 → +919...).
    # Strip leading + and any non-digit chars so the value is safe as a URL
    # path segment forwarded to ws_audio_receiver.py.
    from urllib.parse import unquote
    raw_caller = g("Caller-Caller-ID-Number", "") or g("Channel-Caller-ID-Number", "")
    caller_number = re.sub(r"[^\d]", "", unquote(raw_caller)) if raw_caller else ""

    # Multi-tenant routing fields (per-DID agent routing): the dialed DID plus
    # the optional lescall_* channel variables set by the dashboard's outbound
    # originate. URL-encoded like the caller id; sanitized to path-segment-safe
    # charsets so they can ride the ws_audio_receiver URL.
    # Length caps keep the assembled ws URL far below mod_audio_stream's
    # MAX_WS_URI buffer even with a hostile/garbage dial string.
    raw_did = g("Caller-Destination-Number", "")
    did = re.sub(r"[^\d]", "", unquote(raw_did))[:20] if raw_did else ""
    raw_campaign = g("variable_lescall_campaign_id", "")
    campaign_id = re.sub(r"[^\d]", "", unquote(raw_campaign))[:20] if raw_campaign else ""
    raw_slug = g("variable_lescall_agent_slug", "")
    agent_slug = re.sub(r"[^A-Za-z0-9_-]", "", unquote(raw_slug))[:64] if raw_slug else ""
    # The number this outbound leg is calling, set by the dashboard's originate.
    # Caller-Destination-Number is NOT it on an originated leg — FreeSWITCH
    # puts the dialplan extension there — and the channel's caller id is our
    # own DID, so without this variable the bridge cannot tell who it reached.
    raw_dest = g("variable_lescall_dest", "")
    dest = re.sub(r"[^\d]", "", unquote(raw_dest))[:20] if raw_dest else ""

    return {
        "uuid":             g("Channel-Call-UUID") or g("Unique-ID", "unknown"),
        "channel_name":     g("Channel-Name", ""),
        "caller_number":    caller_number,
        "did":              did,
        "campaign_id":      campaign_id,
        "agent_slug":       agent_slug,
        "dest":             dest,
        "local_ip":         g("variable_local_media_ip", ""),
        "local_port":       g("variable_local_media_port", ""),
        "remote_ip":        g("variable_remote_media_ip", ""),
        "remote_port":      g("variable_remote_media_port", ""),
        "advertised_ip":    g("variable_advertised_media_ip", ""),
        "read_codec":       g("Channel-Read-Codec-Name", ""),
        "read_rate":        g("Channel-Read-Codec-Rate", ""),
        "write_codec":      g("Channel-Write-Codec-Name", ""),
        "write_rate":       g("Channel-Write-Codec-Rate", ""),
        "ptime_ms":         g("variable_rtp_use_codec_ptime", "20"),
        "rtp_pt":           g("variable_rtp_use_pt", "0"),
        "dtmf_pt_send":     g("variable_rtp_2833_send_payload", ""),
        "dtmf_pt_recv":     g("variable_rtp_2833_recv_payload", ""),
        "rtcp_remote_port": g("variable_rtp_remote_audio_rtcp_port", ""),
    }


def expected_packets_per_sec(rtp: dict) -> float:
    """Packet-rate from negotiated ptime. PCMU/8000 @ 20ms -> 50 pps.
    Returns 50.0 as a sane default if ptime isn't a positive number."""
    try:
        ptime = float(rtp.get("ptime_ms") or 20)
        if ptime <= 0:
            return 50.0
        return 1000.0 / ptime
    except (TypeError, ValueError):
        return 50.0


def log_rtp_session_info(rtp: dict) -> None:
    log.info("---------- RTP SESSION INFO ----------")
    log.info("    uuid                  %s", rtp["uuid"])
    log.info("    channel_name          %s", rtp["channel_name"])
    log.info("    local  media          %s:%s  (advertised %s)",
             rtp["local_ip"] or "?", rtp["local_port"] or "?",
             rtp["advertised_ip"] or "?")
    log.info("    remote media          %s:%s",
             rtp["remote_ip"] or "?", rtp["remote_port"] or "?")
    log.info("    read  codec           %s/%s",
             rtp["read_codec"] or "?", rtp["read_rate"] or "?")
    log.info("    write codec           %s/%s",
             rtp["write_codec"] or "?", rtp["write_rate"] or "?")
    log.info("    ptime                 %s ms", rtp["ptime_ms"] or "?")
    log.info("    audio RTP PT          %s", rtp["rtp_pt"] or "?")
    log.info("    DTMF PT (send/recv)   %s/%s",
             rtp["dtmf_pt_send"] or "?", rtp["dtmf_pt_recv"] or "?")
    log.info("    expected packet rate  %.1f pps",
             expected_packets_per_sec(rtp))
    log.info("--------------------------------------")


def log_tcpdump_commands(rtp: dict) -> None:
    """Emit three exact shell commands the operator runs by hand.
    Phase 3 is observational only — we never invoke tcpdump ourselves
    (it requires elevated privileges and interactive lifetime control)."""
    uuid = rtp["uuid"] or "unknown"
    pcap = f"/tmp/phase3_{uuid}.pcap"
    li = rtp["local_ip"] or "<local_ip>"
    lp = rtp["local_port"] or "<local_port>"
    rp = rtp["remote_port"] or "<remote_port>"

    log.info("---------- TCPDUMP COMMANDS (manual) -")
    log.info("# 1. capture for the duration of ONE call")
    log.info("sudo tcpdump -ni any -s0 -w %s \\", pcap)
    log.info("    'udp and (port %s or port %s)'", lp, rp)
    log.info("")
    log.info("# 2. count outbound RTP packets emitted from FS")
    log.info("tcpdump -nr %s \\", pcap)
    log.info("    'src %s and udp port %s' | wc -l", li, lp)
    log.info("")
    log.info("# 3. per-second outbound RTP rate (one row per second)")
    log.info("tcpdump -nr %s -tt \\", pcap)
    log.info("    'src %s and udp port %s' \\", li, lp)
    log.info("    | awk '{print int($1)}' | sort | uniq -c")
    log.info("--------------------------------------")


def log_next_action(rtp: dict, cp: dict, verdict: str) -> None:
    """If the verdict points at CASE C/H, emit the decision rule for
    the operator's tcpdump output. Quiet on every other verdict."""
    if "CASE C" not in verdict and "CASE H" not in verdict:
        return
    pps = expected_packets_per_sec(rtp)
    t4 = cp.get("T4_playback_execute")
    t5 = cp.get("T5_playback_execute_complete")
    window_s = (t5 - t4) if (t4 is not None and t5 is not None) else None
    expected = (window_s * pps) if window_s else None
    high = int(expected * 0.83) if expected else 250
    low = max(10, int(expected * 0.05) if expected else 10)

    log.info("---------- NEXT REQUIRED ACTION ------")
    log.info("Run the TCPDUMP COMMANDS block above during ONE call. "
             "Hold the call >= 10s, then hang up.")
    log.info("")
    log.info("Probable current branch: CASE C — playback executed at "
             "the ESL layer, but audio outcome at the caller is "
             "unresolved without RTP capture.")
    log.info("")
    if window_s is not None:
        log.info("Playback window (T4..T5)  %.3f s", window_s)
        log.info("Expected packet rate      %.1f pps", pps)
        log.info("Expected packet count     ~%.0f packets",
                 expected or 0.0)
    else:
        log.info("Playback window (T4..T5)  not measured this run "
                 "(call ended before COMPLETE)")
        log.info("Expected packet rate      %.1f pps", pps)
    log.info("")
    log.info("Decision rule — apply to outbound RTP packet count "
             "from FS during T4..T5:")
    log.info("  count >= ~%d   -> CASE C.1   RTP IS leaving FS. Loss "
             "is downstream (GCP firewall / NAT asymmetry / carrier "
             "RTP policy / advertised-IP mismatch).", high)
    log.info("  count <= ~%d    -> CASE C.2/H  RTP is NOT leaving FS "
             "despite Application-Response='FILE PLAYED'. Failure is "
             "in the media-bug attachment or write-codec routing "
             "inside FS for outbound-ESL-issued executes.", low)
    log.info("  otherwise        -> ambiguous; re-run with the call "
             "held >=10s and confirm tcpdump captured the entire "
             "T4..T5 window.")
    log.info("--------------------------------------")


# --------------------------- helpers (Phase 4) ------------------------

# Phase 4 is OBSERVATIONAL ONLY. It adds one extra `sendmsg execute
# record_session` after the existing playback execute, then watches for
# the resulting RECORD_START / RECORD_STOP and CHANNEL_EXECUTE_COMPLETE
# app=record_session events. Nothing in Phase 4 modifies, replaces,
# delays, or short-circuits the playback execute path. record_session
# is a read-side media-bug attachment inside FS; it does not write to
# the RTP send side and cannot regress the validated tone playback.

def ensure_recording_dir() -> None:
    """Create RECORDING_DIR once at startup. Idempotent. Failures are
    logged but never raised — a busted recordings directory must not
    prevent the playback path from running."""
    try:
        os.makedirs(RECORDING_DIR, exist_ok=True)
        log.info("RECORDING_DIR ready: %s", RECORDING_DIR)
    except OSError as e:
        log.error("RECORDING_DIR setup failed: %r — record_session "
                  "will fail; playback path is unaffected", e)


def build_record_payload(wav_path: str) -> bytes:
    """Build a sendmsg execute record_session frame.

    Note the deliberate omission of `event-lock`. The playback execute
    is already on the channel with event-lock: true; if we also took
    the lock, record_session would queue behind the ~6s playback and
    we'd capture nothing during the playback window. Without the lock
    the media-bug attaches in parallel — the same pattern FS uses in
    production for IVR-while-recording dialogs.

    LF-only line endings (no CRLF), exactly like PLAYBACK_PAYLOAD."""
    return (
        b"sendmsg\n"
        b"call-command: execute\n"
        b"execute-app-name: record_session\n"
        b"execute-app-arg: " + wav_path.encode("utf-8") + b"\n"
        b"\n"
    )


def build_audio_stream_payload(channel_uuid: str, ws_url: str,
                               sample_rate: int = 8000,
                               caller_number: str = "",
                               did: str = "",
                               campaign_id: str = "",
                               agent_slug: str = "",
                               dest: str = "") -> bytes:
    """P9-A: bgapi uuid_audio_stream <uuid> start <ws_url> mono <sr>.

    mod_audio_stream is registered as an FS API command (NOT a
    dialplan application), so we cannot use sendmsg/execute. We use
    bgapi so the call returns immediately with a Job-UUID and the
    actual websocket open happens asynchronously in FS. Failure to
    start the stream is non-fatal at the ESL controller level — it
    must not break record_session or playback. LF-only line endings,
    LF-LF terminator, byte-shape identical to other text commands.

    Phase 12 (P12-UUID-BRIDGE): when channel_uuid is non-empty we
    append it as a path segment `/<uuid>` to the ws_url. The
    receiver (ws_audio_receiver.py) extracts the leading path
    component and propagates it as the per-session conn_id. Without
    this bridge the receiver mints a random conn_id and the
    Phase-12 playback listener (keyed by FS UUID) can't find the
    call.

    When caller_number is non-empty it is appended as a second path
    segment `/<uuid>/<caller_number>` so ws_audio_receiver.py can use
    it for call audit folder naming. caller_number must be digits-only
    (the + prefix is stripped upstream in extract_rtp_info).

    Multi-tenant plumbing: did (dialed number, digits-only),
    campaign_id (digits-only) and agent_slug ([A-Za-z0-9_-] only, from
    the lescall_agent_slug channel variable) travel as positional path
    segments 3-6: /<uuid>/<caller>/<did>/<campaign>/<agent_slug>/<dest>.
    Empty middle segments are sent as "-" so positions never shift;
    trailing empties are omitted. ws_audio_receiver.py treats "-" or a
    charset-invalid segment as absent, so old/new sender+receiver
    combinations stay compatible.

    A path segment — NOT a query string — is required because
    mod_audio_stream's validate_ws_uri (audio_streamer_glue.cpp
    line ~516) only accepts characters {alnum, '-', '.'} for the
    host, digits for the port, and stops scanning at the first
    '/'. URLs containing '?' are silently rejected and the WS
    connection is never opened. Backwards-compatible: if
    channel_uuid is empty/unknown we emit the bare URL so a
    standalone Phase-9 run still works. If ws_url already has a
    path we leave it alone."""
    url = ws_url
    cu = channel_uuid.strip()
    if cu and cu != "unknown":
        # Detect existing path: skip past 'scheme://' then look for
        # the first '/'. If none, append '/<uuid>'. If a path is
        # already present the operator opted to override us; leave
        # it alone.
        scheme_end = url.find("://")
        rest = url[scheme_end + 3:] if scheme_end >= 0 else url
        if "/" not in rest:
            extras = [caller_number.strip(), did.strip(),
                      campaign_id.strip(), agent_slug.strip(),
                      dest.strip()]
            while extras and not extras[-1]:
                extras.pop()  # drop trailing empties, keep positions stable
            segs = [cu] + [s if s else "-" for s in extras]
            url = url + "/" + "/".join(segs)
    line = (
        "bgapi uuid_audio_stream "
        + cu + " start " + url
        + " mono " + str(int(sample_rate))
    )
    return line.encode("utf-8") + b"\n\n"


def build_audio_stream_stop_payload(channel_uuid: str) -> bytes:
    """P9-A: bgapi uuid_audio_stream <uuid> stop. Companion to
    build_audio_stream_payload. mod_audio_stream's stop verb cleanly
    closes the WS, so the receiver sees a normal FIN, not a reset."""
    line = ("bgapi uuid_audio_stream "
            + channel_uuid.strip() + " stop")
    return line.encode("utf-8") + b"\n\n"


def build_uuid_break_payload(channel_uuid: str) -> bytes:
    """Barge-in: bgapi uuid_break <uuid> all.

    Stops the file currently playing on the channel AND flushes FS's queue
    of pending playbacks — audio already handed to FreeSWITCH is beyond the
    reach of the orchestrator's local queue drains, so this is the only way
    the caller actually stops hearing the superseded response.  uuid_break
    is channel-scoped: breaking one call can never touch another tenant's.
    Same bgapi envelope as uuid_audio_stream start/stop above."""
    line = "bgapi uuid_break " + channel_uuid.strip() + " all"
    return line.encode("utf-8") + b"\n\n"


def build_playback_file_payload(wav_path: str) -> bytes:
    """Phase 12 (P12-PL): sendmsg execute playback <wav_path>.

    NEW helper, additive only. The validated Phase-2 PLAYBACK_PAYLOAD
    constant for the canonical tone is byte-for-byte unchanged — that
    payload uses tone_stream://%(2000,4000,440); this one uses an
    absolute file path produced by conversation_orchestrator.py /
    its TTS adapter. Same sendmsg/execute envelope, same headers,
    same event-lock semantics — so the existing CHANNEL_EXECUTE /
    CHANNEL_EXECUTE_COMPLETE classification in the WAITING loop
    handles this playback transparently. LF-only line endings, no
    CRLF; LF-LF terminator."""
    return (
        b"sendmsg\n"
        b"call-command: execute\n"
        b"execute-app-name: playback\n"
        b"execute-app-arg: " + wav_path.encode("utf-8") + b"\n"
        b"event-lock: true\n"
        b"\n"
    )


# Agent-initiated graceful hangup (platform rule): the hangup APPLICATION is
# queued with event-lock like every playback, so FreeSWITCH executes it only
# after any in-flight/queued farewell audio has finished playing — never
# mid-sentence. NORMAL_CLEARING keeps CDRs/billing indistinguishable from a
# caller-side hangup.
HANGUP_PAYLOAD = (
    b"sendmsg\n"
    b"call-command: execute\n"
    b"execute-app-name: hangup\n"
    b"execute-app-arg: NORMAL_CLEARING\n"
    b"event-lock: true\n"
    b"\n"
)


# ------------------ Phase 12: call registry + playback listener ------

# Thread-safe registry of in-flight calls keyed by Channel-Call-UUID.
# Populated by handle() AFTER the validated startup sequence has
# completed (i.e., once the call is in WAITING). Drained in handle()'s
# finally block. The listener thread looks up the conn by uuid and
# acquires the per-call send_lock before writing — so a playback
# injection cannot interleave bytes with a hangup-time write from
# the call thread, and cannot find a half-torn-down conn.
#
# Each entry is a dict: {
#   "conn": socket.socket,         # the live ESL socket
#   "send_lock": threading.Lock(), # serialises writes to conn
#   "live": bool,                  # cleared by handle() at teardown
#   "t0": float,                   # call start (monotonic)
#   "playbacks": int,              # injected playback counter
# }
_active_calls: dict = {}
_active_calls_lock = threading.Lock()


def _register_call(uuid: str, conn: socket.socket,
                   send_lock: threading.Lock, t0: float) -> None:
    if not uuid or uuid == "unknown":
        return
    with _active_calls_lock:
        _active_calls[uuid] = {
            "conn": conn,
            "send_lock": send_lock,
            "live": True,
            "t0": t0,
            "playbacks": 0,
        }
    log.info("PLAYBACK_REGISTRY: registered uuid=%s "
             "(active_calls=%d)", uuid, len(_active_calls))


def _unregister_call(uuid: str) -> None:
    if not uuid or uuid == "unknown":
        return
    with _active_calls_lock:
        entry = _active_calls.pop(uuid, None)
        remaining = len(_active_calls)
    if entry is not None:
        # Flip live False so any concurrent listener call returns
        # without trying to write to a teardown-in-progress conn.
        entry["live"] = False
    log.info("PLAYBACK_REGISTRY: unregistered uuid=%s "
             "(active_calls=%d)", uuid, remaining)


def _read_frame_be(sock: socket.socket,
                   max_bytes: int) -> "bytes | None":
    """Read one length-prefixed frame from a blocking socket. 4-byte
    big-endian length, then payload. Returns the payload bytes or
    None on clean EOF. Raises ValueError on oversize. Used only by
    the playback listener thread — small, stdlib-only, no asyncio."""
    hdr = b""
    while len(hdr) < 4:
        chunk = sock.recv(4 - len(hdr))
        if not chunk:
            return None
        hdr += chunk
    (length,) = struct.unpack(">I", hdr)
    if length == 0:
        return b""
    if length > max_bytes:
        raise ValueError(f"frame too large: {length} bytes")
    out = bytearray()
    while len(out) < length:
        chunk = sock.recv(length - len(out))
        if not chunk:
            raise ValueError("short read on payload")
        out.extend(chunk)
    return bytes(out)


def _dispatch_playback_request(req: dict) -> dict:
    """Look up the call by conn_id (== Channel-Call-UUID), acquire
    the per-call send_lock, and dispatch a playback sendmsg with the
    requested wav path. Returns a small status dict — never raises.
    Stale requests (call already torn down) are reported, not
    treated as errors."""
    uuid = (req.get("conn_id") or "").strip()
    wav_path = (req.get("wav_path") or "").strip()
    if not uuid or not wav_path:
        return {"ok": False, "reason": "missing conn_id or wav_path"}
    # Best-effort sanity: the WAV must exist on disk before we hand
    # FS a path; otherwise the playback execute fails inside FS and
    # the operator sees a misleading "playback executed" line.
    if not os.path.isfile(wav_path):
        return {"ok": False, "reason": "wav_path not found",
                "wav_path": wav_path}
    with _active_calls_lock:
        entry = _active_calls.get(uuid)
        if entry is None or not entry.get("live"):
            return {"ok": False, "reason": "no active call",
                    "uuid": uuid}
        conn = entry["conn"]
        send_lock = entry["send_lock"]
    payload = build_playback_file_payload(wav_path)
    with send_lock:
        # Re-check liveness inside the lock so we never write into a
        # socket that the call thread has just begun tearing down.
        with _active_calls_lock:
            still_live = (uuid in _active_calls
                          and _active_calls[uuid].get("live"))
        if not still_live:
            return {"ok": False, "reason": "call torn down",
                    "uuid": uuid}
        try:
            log.info("[%s] [PLAYBACK_COMMAND] Executing: sendmsg execute playback %s", uuid, wav_path)
            conn.sendall(payload)
        except OSError as exc:
            return {"ok": False, "reason": f"sendall failed: {exc!r}",
                    "uuid": uuid}
    with _active_calls_lock:
        if uuid in _active_calls:
            _active_calls[uuid]["playbacks"] += 1
    return {"ok": True, "uuid": uuid, "wav_path": wav_path,
            "bytes_sent": len(payload)}


def _dispatch_hangup_request(req: dict) -> dict:
    """Sibling of _dispatch_playback_request for {"type": "hangup_request"}:
    queue an event-locked hangup app on the call so FreeSWITCH ends the
    channel after the farewell audio finishes. Same registry lookup, same
    send_lock discipline, never raises."""
    uuid = (req.get("conn_id") or "").strip()
    if not uuid:
        return {"ok": False, "reason": "missing conn_id"}
    with _active_calls_lock:
        entry = _active_calls.get(uuid)
        if entry is None or not entry.get("live"):
            # The caller beat us to it — that is success, not an error.
            return {"ok": True, "uuid": uuid, "already_down": True}
        conn = entry["conn"]
        send_lock = entry["send_lock"]
    with send_lock:
        with _active_calls_lock:
            still_live = (uuid in _active_calls
                          and _active_calls[uuid].get("live"))
        if not still_live:
            return {"ok": True, "uuid": uuid, "already_down": True}
        try:
            log.info("[%s] [HANGUP_COMMAND] agent-initiated hangup "
                     "(event-locked, after queued playback)", uuid)
            conn.sendall(HANGUP_PAYLOAD)
        except OSError as exc:
            return {"ok": False, "reason": f"sendall failed: {exc!r}",
                    "uuid": uuid}
    return {"ok": True, "uuid": uuid, "bytes_sent": len(HANGUP_PAYLOAD)}


def _dispatch_break_request(req: dict) -> dict:
    """Sibling of _dispatch_hangup_request for {"type": "break_request"}:
    barge-in — issue bgapi uuid_break <uuid> all so the audio the caller is
    currently hearing stops at the media layer.  Same registry lookup, same
    send_lock discipline, never raises.  A torn-down call is success (there
    is nothing left to silence), not an error."""
    uuid = (req.get("conn_id") or "").strip()
    if not uuid:
        return {"ok": False, "reason": "missing conn_id"}
    with _active_calls_lock:
        entry = _active_calls.get(uuid)
        if entry is None or not entry.get("live"):
            return {"ok": True, "uuid": uuid, "already_down": True}
        conn = entry["conn"]
        send_lock = entry["send_lock"]
    payload = build_uuid_break_payload(uuid)
    with send_lock:
        with _active_calls_lock:
            still_live = (uuid in _active_calls
                          and _active_calls[uuid].get("live"))
        if not still_live:
            return {"ok": True, "uuid": uuid, "already_down": True}
        try:
            log.info("[%s] [BREAK_COMMAND] barge-in — uuid_break all "
                     "(flush current + queued playback)", uuid)
            conn.sendall(payload)
        except OSError as exc:
            return {"ok": False, "reason": f"sendall failed: {exc!r}",
                    "uuid": uuid}
    return {"ok": True, "uuid": uuid, "bytes_sent": len(payload)}


def _playback_client_thread(client: socket.socket, peer) -> None:
    client.settimeout(5.0)
    try:
        try:
            payload = _read_frame_be(client, PLAYBACK_MAX_FRAME_BYTES)
        except ValueError as exc:
            log.warning("playback listener malformed frame from %s: "
                        "%r", peer, exc)
            return
        except (OSError, socket.timeout) as exc:
            log.warning("playback listener recv failed peer=%s: %r",
                        peer, exc)
            return
        if payload is None:
            return
        try:
            req = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            log.warning("playback listener json decode failed "
                        "peer=%s err=%r", peer, exc)
            return
        _req_type = req.get("type") or ""
        if _req_type == "hangup_request":
            result = _dispatch_hangup_request(req)
            if result.get("ok"):
                log.info("[%s] [HANGUP_DISPATCHED] already_down=%s",
                         result.get("uuid"), result.get("already_down", False))
            else:
                log.warning("[%s] [HANGUP_INVALID] reason=%r",
                            req.get("conn_id"), result.get("reason"))
            try:
                ack = json.dumps(result, ensure_ascii=False).encode("utf-8")
                client.sendall(struct.pack(">I", len(ack)) + ack)
            except OSError:
                pass
            return
        if _req_type == "break_request":
            result = _dispatch_break_request(req)
            if result.get("ok"):
                log.info("[%s] [BREAK_DISPATCHED] already_down=%s",
                         result.get("uuid"),
                         result.get("already_down", False))
            else:
                log.warning("[%s] [BREAK_INVALID] reason=%r",
                            req.get("conn_id"), result.get("reason"))
            try:
                ack = json.dumps(result, ensure_ascii=False).encode("utf-8")
                client.sendall(struct.pack(">I", len(ack)) + ack)
            except OSError:
                pass
            return
        if _req_type != "playback_request":
            log.warning("playback listener unexpected type=%r "
                        "peer=%s", req.get("type"), peer)
            return
        log.info("[%s] [PLAYBACK_SEND] received request for %s",
                 req.get("conn_id"), req.get("wav_path"))
        result = _dispatch_playback_request(req)
        if result.get("ok"):
            log.info("[%s] [PLAYBACK_COMPLETE] path=%s bytes=%d ts=%.3f",
                     result.get("uuid"), result.get("wav_path"),
                     result.get("bytes_sent"), time.time())
        else:
            log.warning("[%s] [PLAYBACK_INVALID] reason=%r path=%s",
                        req.get("conn_id"), result.get("reason"), 
                        req.get("wav_path"))
        # Best-effort ack so the orchestrator can log the outcome.
        try:
            ack = json.dumps(result, ensure_ascii=False).encode("utf-8")
            client.sendall(struct.pack(">I", len(ack)) + ack)
        except OSError:
            pass
    finally:
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            client.close()
        except OSError:
            pass


def _playback_listener_thread() -> None:
    """Daemon thread. Listens on PLAYBACK_LISTEN_HOST:PORT and spawns
    one short-lived worker thread per inbound TCP connection. Each
    worker reads a single playback_request, dispatches, optionally
    acks, and exits. A crash in any worker is contained — the
    listener itself only handles accept(), not protocol parsing."""
    if not PLAYBACK_LISTEN_ENABLED:
        log.info("PLAYBACK_LISTENER disabled "
                 "(PLAYBACK_LISTEN_ENABLED=false)")
        return
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((PLAYBACK_LISTEN_HOST, PLAYBACK_LISTEN_PORT))
    except OSError as exc:
        log.error("PLAYBACK_LISTENER bind failed %s:%d err=%r — "
                  "Phase-12 playback injection unavailable, but "
                  "Phase-1..11A pipeline is unaffected",
                  PLAYBACK_LISTEN_HOST, PLAYBACK_LISTEN_PORT, exc)
        return
    srv.listen(8)
    log.info("PLAYBACK_LISTENER ready on %s:%d (Phase-12 "
             "orchestrator -> ESL injection)",
             PLAYBACK_LISTEN_HOST, PLAYBACK_LISTEN_PORT)
    try:
        while True:
            try:
                client, addr = srv.accept()
            except OSError as exc:
                log.error("PLAYBACK_LISTENER accept failed: %r",
                          exc)
                time.sleep(0.5)
                continue
            t = threading.Thread(
                target=_playback_client_thread,
                args=(client, addr),
                daemon=True,
                name=f"pb-{addr[1]}",
            )
            t.start()
    except Exception as exc:
        log.exception("PLAYBACK_LISTENER crashed: %r", exc)
    finally:
        try:
            srv.close()
        except OSError:
            pass


def build_stop_record_payload(wav_path: str) -> bytes:
    """P6-A: sendmsg execute stop_record_session.

    stop_record_session is the FS-native counterpart to
    record_session. It detaches the read-side media bug and triggers
    the WAV-header finalization (size + chunk-length fields are
    rewritten when the bug closes the file). It accepts the same
    file-path argument as the record_session that opened the file —
    using a different path here would no-op silently inside FS.

    Like build_record_payload, no event-lock: stop_record_session
    must take effect even if the channel is already torn down or
    other event-locked apps are queued. It is dispatch-immediate."""
    return (
        b"sendmsg\n"
        b"call-command: execute\n"
        b"execute-app-name: stop_record_session\n"
        b"execute-app-arg: " + wav_path.encode("utf-8") + b"\n"
        b"\n"
    )


def _scan_peak_amplitude(wf: wave.Wave_read, sample_width: int,
                         sample_rate: int, n_frames: int) -> int:
    """P5-C: scan up to the first 30 seconds of frames and return the
    peak absolute sample value. 0 on any failure or truly silent file.
    Pure read; rewinds nothing, leaves the wave handle at EOF."""
    if not (sample_width and sample_rate and n_frames):
        return 0
    target = min(n_frames, sample_rate * 30)
    try:
        raw = wf.readframes(target)
    except (wave.Error, EOFError):
        return 0
    if not raw:
        return 0
    if sample_width == 2:
        n = len(raw) // 2
        if n == 0:
            return 0
        # struct.unpack on the entire buffer is ~100x faster than a
        # Python loop with int.from_bytes for buffers of this size.
        samples = struct.unpack("<" + "h" * n, raw[: n * 2])
        peak = 0
        for s in samples:
            a = -s if s < 0 else s
            if a > peak:
                peak = a
        return peak
    if sample_width == 1:
        # 8-bit WAV samples are unsigned, biased by 128.
        peak = 0
        for b in raw:
            a = b - 128
            if a < 0:
                a = -a
            if a > peak:
                peak = a
        return peak
    return 0


def validate_wav(path: str) -> dict:
    """Post-call WAV inspection. Pure read; never mutates state. All
    exceptions are swallowed and surfaced as fields so the caller can
    log a single VALIDATION block without a try/except.

    P5-C extends the Phase-4 fields with comp_type (PCM check) and a
    peak_amplitude / non_silence pair derived from a bounded sample
    scan. The Phase-4 fields keep their meanings."""
    info = {
        "path": path,
        "exists": False,
        "size_bytes": 0,
        "channels": None,
        "sample_rate": None,
        "sample_width": None,
        "approx_duration_s": None,
        "non_empty_audio": False,
        "comp_type": None,        # P5-C: 'NONE' for canonical PCM
        "peak_amplitude": 0,      # P5-C: max abs sample in first 30s
        "non_silence": False,     # P5-C: peak above noise floor
        "error": "",
    }
    try:
        st = os.stat(path)
        info["exists"] = True
        info["size_bytes"] = st.st_size
    except FileNotFoundError:
        return info
    except OSError as e:
        info["error"] = f"stat failed: {e!r}"
        return info

    if info["size_bytes"] <= 44:
        # 44 bytes == canonical PCM WAV header alone. The bug attached
        # but no audio frames flowed through it. Treat as empty.
        return info

    try:
        with contextlib.closing(wave.open(path, "rb")) as wf:
            info["channels"] = wf.getnchannels()
            info["sample_rate"] = wf.getframerate()
            info["sample_width"] = wf.getsampwidth()
            info["comp_type"] = wf.getcomptype() or "NONE"
            n_frames = wf.getnframes()
            sr = info["sample_rate"] or 0
            if sr > 0:
                info["approx_duration_s"] = n_frames / float(sr)
            info["non_empty_audio"] = n_frames > 0
            # P5-C: peak/non-silence scan. Threshold 200 on signed 16
            # bit ≈ -44 dBFS; FS PCMU/PCMA decoded silence sits well
            # below that, while a 440 Hz tone or live speech is
            # comfortably above. Threshold 3 on 8-bit unsigned is the
            # equivalent margin.
            peak = _scan_peak_amplitude(
                wf, info["sample_width"] or 0,
                info["sample_rate"] or 0, n_frames)
            info["peak_amplitude"] = peak
            sw = info["sample_width"]
            if sw == 2:
                info["non_silence"] = peak > 200
            elif sw == 1:
                info["non_silence"] = peak > 3
            else:
                info["non_silence"] = peak > 0
    except (wave.Error, EOFError) as e:
        # Fall back to a size-based estimate at PCM 8 kHz / 16-bit
        # mono, the FS narrowband default. Flag the parse error so
        # the operator knows the file is malformed but the bug
        # attached and bytes flowed. We cannot scan amplitude here.
        info["error"] = f"wave.open failed: {e!r}"
        info["approx_duration_s"] = max(
            0.0, (info["size_bytes"] - 44) / 16000.0)
        info["non_empty_audio"] = True
    except OSError as e:
        # P8: FS writes WAVs as freeswitch:freeswitch mode 0660. If
        # the python server is not in group freeswitch (for example
        # immediately after `usermod -aG freeswitch angelo` but
        # before the shell is re-logged or the process is launched
        # via `sg freeswitch -c ...`), wave.open raises
        # PermissionError out of builtins.open. We catch it here so
        # the rest of the post-call diagnostic chain still emits.
        info["error"] = f"open/read failed: {e!r}"
        # Size-based fallback so non_empty_audio still has a defensible
        # value when the file is real but unreadable to this process.
        info["approx_duration_s"] = max(
            0.0, (info["size_bytes"] - 44) / 16000.0)
        info["non_empty_audio"] = info["size_bytes"] > 44
    return info


def log_recording_validation(info: dict) -> None:
    """Single structured block emitted in finally after SESSION
    SUMMARY. Mirrors the layout of RTP SESSION INFO so log scrapers
    can find it consistently."""
    dur = (f"{info['approx_duration_s']:.2f}s"
           if info["approx_duration_s"] is not None else "—")
    log.info("---------- RECORDING VALIDATION ------")
    log.info("    path                  %s", info["path"])
    log.info("    exists                %s", info["exists"])
    log.info("    size_bytes            %d", info["size_bytes"])
    log.info("    channels              %s",
             info["channels"] if info["channels"] is not None else "—")
    log.info("    sample_rate           %s",
             info["sample_rate"] if info["sample_rate"] is not None else "—")
    log.info("    sample_width_bytes    %s",
             info["sample_width"] if info["sample_width"] is not None else "—")
    log.info("    approx_duration       %s", dur)
    log.info("    non_empty_audio       %s", info["non_empty_audio"])
    log.info("    comp_type             %s",
             info.get("comp_type") or "—")
    log.info("    peak_amplitude        %s",
             info.get("peak_amplitude", 0))
    log.info("    non_silence           %s",
             info.get("non_silence", False))
    if info["error"]:
        log.info("    parse_error           %s", info["error"])
    log.info("--------------------------------------")


# --------------------------- helpers (Phase 6) ------------------------

# Phase 6 is the recording-finalization fix. The Phase 4/5 stack
# attaches record_session correctly (CHANNEL_EXECUTE app=record_session
# fires, +OK ack returns) but the WAV file does not appear on disk.
# Root cause: FS does not always flush the recording bug on channel
# hangup when the channel is owned by an outbound ESL session that
# closes its TCP at the same instant. We now dispatch an explicit
# stop_record_session and drain briefly so FS has a chance to deliver
# RECORD_STOP and rewrite the WAV header before the socket is closed.
# Nothing else changes — playback, RTP, handshake, framing, threading,
# and the Phase-3 verdict logic are untouched.


def post_stop_drain(conn: socket.socket, buf: bytes,
                    timeout_s: float = POST_STOP_WAIT_S) -> tuple:
    """P6-A: read frames for up to `timeout_s` seconds after we send
    stop_record_session, logging any +OK ack and any RECORD_STOP we
    observe. This is the explicit flush-window FS needs to finalize
    the WAV header. Called from inside handle() — uses module-level
    helpers (read_frame, event_name, body_field, reply_is_ok/err).

    Returns (buf, saw_stop_ack, saw_record_stop). buf is propagated
    so the caller doesn't lose any partially-buffered bytes when it
    proceeds to send `exit` and break."""
    log.info("POST-STOP WAIT starting (up to %.0f ms) — draining "
             "events to allow FS to flush the WAV header",
             timeout_s * 1000)
    deadline = time.monotonic() + timeout_s
    saw_stop_ack = False
    saw_record_stop = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            conn.settimeout(remaining)
            headers, body, _raw, buf = read_frame(conn, buf)
        except socket.timeout:
            break
        except OSError as e:
            log.warning("POST-STOP WAIT: socket error during drain: %r",
                        e)
            break
        if headers is None:
            log.info("POST-STOP WAIT: socket EOF during drain "
                     "(remaining %.3fs of window)", remaining)
            break
        ctype = headers.get("Content-Type", "")
        ev = event_name(headers, body)
        if ctype == "command/reply":
            reply = headers.get("Reply-Text", "")
            if reply_is_ok(reply):
                saw_stop_ack = True
                log.info("STOP_RECORD_SESSION acknowledged: "
                         "command/reply +OK -> %r", reply)
            elif reply_is_err(reply):
                log.error("STOP_RECORD_SESSION nack: "
                          "command/reply -ERR -> %r", reply)
            else:
                log.info("post-stop drain: command/reply "
                         "Reply-Text=%r", reply)
        elif ev == "RECORD_STOP":
            saw_record_stop = True
            log.info("EVENT RECORD_STOP during post-stop drain  "
                     "Record-File-Path=%r  Record-File-Size=%s  "
                     "Record-Ms=%s",
                     body_field(body, "Record-File-Path"),
                     body_field(body, "Record-File-Size"),
                     body_field(body, "Record-Ms"))
        elif ctype == "text/event-plain":
            log.info("post-stop drain: ev=%s body[:160]=%r",
                     ev, body[:160])
        else:
            log.info("post-stop drain: ctype=%s ev=%s", ctype, ev)
    log.info("POST-STOP WAIT complete  stop_ack=%s  record_stop=%s",
             saw_stop_ack, saw_record_stop)
    return buf, saw_stop_ack, saw_record_stop


# --------------------------- helpers (Phase 5) ------------------------

# Phase 5 is OBSERVATIONAL ONLY on top of Phase 4. It reorders the
# dispatch (record_session sendmsg precedes playback sendmsg), adds new
# explicit timing checkpoints, and emits two NEW post-mortem blocks:
# FULL DUPLEX VERDICT and OPERATOR INSTRUCTIONS. classify() and the
# Phase-3 NEXT REQUIRED ACTION block are unchanged.

# Verdict tokens. Order is from "best" to "worst" so log scrapers can
# rank a run at a glance.
FDX_FULL_DUPLEX_CONFIRMED = "FULL_DUPLEX_CONFIRMED"
FDX_PLAYBACK_ONLY         = "PLAYBACK_ONLY"
FDX_INBOUND_ONLY          = "INBOUND_ONLY"
FDX_PARTIAL_DUPLEX        = "PARTIAL_DUPLEX"
FDX_EMPTY_RECORDING       = "EMPTY_RECORDING"
FDX_RECORDING_FAILED      = "RECORDING_FAILED"

# Approximate duration of the validated tone_stream://%(2000,4000,440)
# beep. 2000 ms tone + 4000 ms tail = ~6 s of write-side audio. Used
# only as a heuristic threshold for FULL_DUPLEX_CONFIRMED — never as a
# control-flow gate.
TONE_BEEP_APPROX_DURATION_S = 6.0


def full_duplex_verdict(playback_verdict: str, info: dict,
                        cp: dict) -> tuple:
    """Map (Phase-3 playback verdict, P4-D WAV info) to a single
    full-duplex token plus a one-line rationale. Pure function — no
    side effects, no I/O.

    The token is conservative: FULL_DUPLEX_CONFIRMED requires that
    playback executed cleanly AND the WAV passed PCM + non-silence AND
    its duration exceeds the playback-tone duration with margin (so
    we know SOMETHING was captured after the beep finished — which is
    where caller speech would have to live).
    """
    playback_ok = (
        ("CASE C" in playback_verdict)
        and cp.get("T7_playback_complete") is not None
    )

    if not info.get("exists"):
        return (FDX_RECORDING_FAILED,
                "WAV not present on disk — record_session never "
                "produced an output file.")

    if info.get("size_bytes", 0) <= 44 or not info.get("non_empty_audio"):
        return (FDX_EMPTY_RECORDING,
                "WAV exists but contains no PCM frames — bug attached "
                "but no media flowed.")

    has_signal = bool(info.get("non_silence"))
    duration = info.get("approx_duration_s") or 0.0
    comp = (info.get("comp_type") or "").upper()
    pcm_ok = comp in ("", "NONE")  # blank == parse fallback path

    # Anything past here means a non-empty WAV was produced.
    if not pcm_ok:
        return (FDX_PARTIAL_DUPLEX,
                f"WAV non-empty but compression type is {comp!r}, not "
                f"PCM — manual decode required to confirm full duplex.")

    if playback_ok and has_signal and duration >= TONE_BEEP_APPROX_DURATION_S + 1.0:
        return (FDX_FULL_DUPLEX_CONFIRMED,
                f"playback executed cleanly, WAV is PCM with audible "
                f"signal (peak={info.get('peak_amplitude', 0)}), "
                f"duration {duration:.2f}s exceeds tone window — "
                f"post-tone region must be checked by ear to verify "
                f"caller speech.")

    if playback_ok and has_signal and duration < TONE_BEEP_APPROX_DURATION_S + 1.0:
        return (FDX_PLAYBACK_ONLY,
                f"playback executed cleanly, WAV has signal but "
                f"duration {duration:.2f}s only covers the tone "
                f"window — no post-tone audio captured.")

    if playback_ok and not has_signal:
        return (FDX_EMPTY_RECORDING,
                "playback executed cleanly but WAV signal is below "
                "the noise-floor threshold — bug attached, no "
                "audible media observed.")

    if not playback_ok and has_signal:
        return (FDX_INBOUND_ONLY,
                "WAV has signal but playback did not complete — "
                "inbound media bug captured something while the "
                "outbound write side never finished.")

    return (FDX_PARTIAL_DUPLEX,
            "neither playback nor WAV passed the full validation set "
            "— see SESSION SUMMARY checkpoints and RECORDING "
            "VALIDATION above.")


def log_full_duplex_verdict(verdict: str, rationale: str,
                            info: dict, cp: dict) -> None:
    """Emit a single FULL DUPLEX VERDICT block. Mirrors RECORDING
    VALIDATION layout."""
    t6 = cp.get("T6_playback_execute")
    t7 = cp.get("T7_playback_complete")
    pb_window = (t7 - t6) if (t6 is not None and t7 is not None) else None
    log.info("---------- FULL DUPLEX VERDICT -------")
    log.info("    verdict               %s", verdict)
    log.info("    rationale             %s", rationale)
    log.info("    wav_path              %s", info.get("path") or "—")
    log.info("    wav_duration          %s",
             f"{info['approx_duration_s']:.2f}s"
             if info.get("approx_duration_s") is not None else "—")
    log.info("    wav_peak_amplitude    %s",
             info.get("peak_amplitude", 0))
    log.info("    wav_non_silence       %s",
             info.get("non_silence", False))
    log.info("    playback_window_s     %s",
             f"{pb_window:.3f}" if pb_window is not None else "—")
    log.info("--------------------------------------")


def log_operator_instructions(info: dict, fdx_verdict: str) -> None:
    """P5-E: explicit instructions the operator runs by hand. We only
    emit the listening commands when there is a file worth listening
    to; for FAILED/EMPTY runs the verdict already explains what to do."""
    log.info("---------- OPERATOR INSTRUCTIONS -----")
    path = info.get("path") or ""
    if (fdx_verdict == FDX_RECORDING_FAILED or not path
            or not info.get("exists")):
        log.info("No WAV was produced. Manual listening is not "
                 "applicable for this run.")
        log.info("Re-run a single call and confirm RECORDING_DIR "
                 "(/tmp/esl_recordings) is writable by the freeswitch "
                 "user, and that no -ERR appeared after "
                 "SEND[RECORD_SESSION].")
        log.info("--------------------------------------")
        return
    log.info("To verify FULL DUPLEX behaviour, listen to the captured "
             "WAV directly:")
    log.info("")
    log.info("    aplay %s", path)
    log.info("    # or, headless / SSH:")
    log.info("    ffplay -nodisp -autoexit -loglevel error %s", path)
    log.info("    # or copy locally:")
    log.info("    scp <host>:%s ./", path)
    log.info("")
    log.info("Expected contents of a healthy full-duplex run:")
    log.info("  - First ~%.0fs: 440 Hz beep tone (the validated "
             "tone_stream playback).", TONE_BEEP_APPROX_DURATION_S)
    log.info("  - After the beep: caller-side speech captured from "
             "inbound RTP up to hangup.")
    log.info("")
    log.info("Audible-outcome decoder:")
    log.info("  beep + caller speech    -> FULL DUPLEX confirmed at "
             "the FS media-bug layer (matches "
             "FULL_DUPLEX_CONFIRMED).")
    log.info("  beep only, then silence -> outbound write side OK, "
             "inbound RTP not reaching FS (NAT / asymmetric media / "
             "remote not sending).")
    log.info("  caller speech only      -> playback never reached the "
             "bug (write-side routing failure inside FS).")
    log.info("  silence throughout      -> bug attached but no media "
             "flowed; matches EMPTY_RECORDING.")
    log.info("--------------------------------------")


# --------------------------- helpers (Phase 7 diagnostics) -----------
# Phase 7 (P7-B / P7-C) is DIAGNOSTIC ONLY. None of the helpers below
# touch the wire, change framing, or alter any prior validated state.
# They run inside the existing `finally` block after the Phase-5
# OPERATOR INSTRUCTIONS section, so a Phase-7 helper failure cannot
# break the SESSION SUMMARY / RECORDING VALIDATION / FULL DUPLEX
# VERDICT / OPERATOR INSTRUCTIONS contracts that callers already rely
# on. Every public function below is best-effort: it catches its own
# exceptions and logs them.

def log_dialplan_recording_validation(info: dict) -> None:
    """P7-B: same shape as log_recording_validation but for the
    dialplan-native /tmp/dialplan_test.wav, so a side-by-side log scrape
    can compare the two recordings produced by the same call."""
    log.info("---------- DIALPLAN RECORDING VALIDATION ------")
    log.info("    %-22s %s", "path", info.get("path") or "-")
    log.info("    %-22s %s", "exists", info.get("exists"))
    log.info("    %-22s %s", "size_bytes", info.get("size_bytes"))
    log.info("    %-22s %s", "channels", info.get("channels"))
    log.info("    %-22s %s", "sample_rate", info.get("sample_rate"))
    log.info("    %-22s %s", "sample_width_bytes",
             info.get("sample_width"))
    log.info("    %-22s %s", "approx_duration",
             info.get("approx_duration_s"))
    log.info("    %-22s %s", "non_empty_audio",
             info.get("non_empty_audio"))
    log.info("    %-22s %s", "comp_type", info.get("comp_type"))
    log.info("    %-22s %s", "peak_amplitude",
             info.get("peak_amplitude"))
    log.info("    %-22s %s", "non_silence", info.get("non_silence"))
    if info.get("error"):
        log.info("    %-22s %s", "error", info.get("error"))
    log.info("-----------------------------------------------")


def _yn(v) -> str:
    if v is True:
        return "yes"
    if v is False:
        return "no"
    return "-"


def _fmt_bytes(n) -> str:
    try:
        return f"{int(n)}B"
    except Exception:
        return "-"


def _fmt_secs(v) -> str:
    try:
        return f"{float(v):.3f}s"
    except Exception:
        return "-"


def log_ab_comparison(esl: dict, dialplan: dict, cp: dict) -> None:
    """P7-B: structured two-column comparison of the two recordings.
    Pure formatting; no decisions yet (decision_engine handles that)."""
    log.info("---------- A/B COMPARISON --------------------")
    log.info("    %-26s %-22s %-22s",
             "metric", "DIALPLAN-NATIVE", "ESL-TRIGGERED")
    log.info("    %-26s %-22s %-22s",
             "path",
             dialplan.get("path") or "-",
             esl.get("path") or "-")
    log.info("    %-26s %-22s %-22s",
             "file_exists",
             _yn(dialplan.get("exists")),
             _yn(esl.get("exists")))
    log.info("    %-26s %-22s %-22s",
             "size_bytes",
             _fmt_bytes(dialplan.get("size_bytes")),
             _fmt_bytes(esl.get("size_bytes")))
    log.info("    %-26s %-22s %-22s",
             "duration",
             _fmt_secs(dialplan.get("approx_duration_s")),
             _fmt_secs(esl.get("approx_duration_s")))
    log.info("    %-26s %-22s %-22s",
             "non_empty",
             _yn(dialplan.get("non_empty_audio")),
             _yn(esl.get("non_empty_audio")))
    log.info("    %-26s %-22s %-22s",
             "non_silent",
             _yn(dialplan.get("non_silence")),
             _yn(esl.get("non_silence")))
    # "Persisted through hangup" = file present after the call ended.
    # This is the same data point as exists+non_empty, surfaced
    # explicitly because that is the question Phase 6 was trying to
    # answer.
    log.info("    %-26s %-22s %-22s",
             "persisted_through_hangup",
             _yn(bool(dialplan.get("exists"))
                 and bool(dialplan.get("size_bytes"))),
             _yn(bool(esl.get("exists"))
                 and bool(esl.get("size_bytes"))))
    # Media-bug lifecycle duration = approx_duration of the file the
    # bug actually wrote. For a healthy bug this is ~ call duration.
    log.info("    %-26s %-22s %-22s",
             "media_bug_lifecycle",
             _fmt_secs(dialplan.get("approx_duration_s")),
             _fmt_secs(esl.get("approx_duration_s")))
    # ESL-side execute_complete timing is what Phase 6 reported as
    # the smoking gun (~2 ms is record_session immediately failing).
    # Dialplan side is not visible from ESL, so we leave a dash.
    esl_exec_complete_dt = None
    try:
        if ("T4b_record_execute" in cp
                and "T5b_record_execute_complete" in cp):
            esl_exec_complete_dt = (cp["T5b_record_execute_complete"]
                                    - cp["T4b_record_execute"])
    except Exception:
        esl_exec_complete_dt = None
    log.info("    %-26s %-22s %-22s",
             "execute_complete_dt",
             "-",
             _fmt_secs(esl_exec_complete_dt))
    log.info("----------------------------------------------")


def log_fs_internal_diagnostics() -> None:
    """P7-C: best-effort FreeSWITCH internal diagnostics. Runs
    `fs_cli -x "show modules"` and reports presence of the modules
    that record_session needs (mod_dptools, mod_sndfile,
    mod_native_file). Also reports the ownership/permissions of
    RECORDING_DIR vs the freeswitch user. This helper NEVER raises
    out — every subprocess call is wrapped in try/except."""
    log.info("---------- FS INTERNAL DIAGNOSTICS -----------")
    # Recording-dir permissions snapshot. record_session opens the
    # WAV as the freeswitch user; if RECORDING_DIR is not writable by
    # that uid, FS emits CHANNEL_EXECUTE / CHANNEL_EXECUTE_COMPLETE
    # back-to-back and never produces a file. This is a common silent
    # failure mode and is worth surfacing on every run.
    try:
        import pwd
        import grp
        st = os.stat(RECORDING_DIR)
        owner = pwd.getpwuid(st.st_uid).pw_name
        group = grp.getgrgid(st.st_gid).gr_name
        mode = oct(st.st_mode & 0o777)
        log.info("    %-22s %s (%s:%s mode=%s)",
                 "recording_dir", RECORDING_DIR,
                 owner, group, mode)
        # Best-effort: is the freeswitch user able to write here?
        try:
            fs_uid = pwd.getpwnam("freeswitch").pw_uid
            fs_gid = pwd.getpwnam("freeswitch").pw_gid
            world_w = bool(st.st_mode & 0o002)
            owner_w = (st.st_uid == fs_uid
                       and bool(st.st_mode & 0o200))
            group_w = (st.st_gid == fs_gid
                       and bool(st.st_mode & 0o020))
            writable = world_w or owner_w or group_w
            log.info("    %-22s %s",
                     "writable_by_freeswitch",
                     "yes" if writable else
                     "NO  *** likely root-cause for ESL "
                     "WAV-never-appears symptom ***")
        except KeyError:
            log.info("    %-22s freeswitch user not present on host",
                     "writable_by_freeswitch")
    except Exception as exc:
        log.info("    %-22s could not stat: %r",
                 "recording_dir", exc)

    # show modules: filtered to recording-relevant entries.
    try:
        import subprocess
        out = subprocess.run(
            ["fs_cli", "-x", "show modules"],
            capture_output=True, text=True, timeout=4.0,
        )
        text = (out.stdout or "") + (out.stderr or "")
        log.info("    fs_cli show modules (filtered):")
        wanted = ("mod_dptools", "mod_sndfile", "mod_native_file",
                  "mod_loopback")
        seen = {}
        for line in text.splitlines():
            for name in wanted:
                if name in line:
                    seen[name] = line.strip()
        for name in wanted:
            log.info("        %-18s %s",
                     name,
                     seen.get(name, "NOT REPORTED — module missing"))
    except FileNotFoundError:
        log.info("    fs_cli not on PATH — skipping show modules")
    except Exception as exc:
        log.info("    fs_cli show modules failed: %r", exc)
    log.info("----------------------------------------------")


def phase7_decision_engine(esl: dict, dialplan: dict) -> str:
    """P7-C: deterministic mapping from the two validate_wav results
    to one of the four CASE Rn tokens, or INCONCLUSIVE. Pure function."""
    def _ok(info: dict) -> bool:
        # "WORKS" = file exists, has audio bytes, is not silent.
        return bool(info.get("exists")
                    and info.get("non_empty_audio")
                    and info.get("non_silence"))

    def _present_but_dead(info: dict) -> bool:
        # "Zero-byte / silent" = file exists but is unusable.
        if not info.get("exists"):
            return False
        if not info.get("non_empty_audio"):
            return True
        if not info.get("non_silence"):
            return True
        return False

    dp_ok = _ok(dialplan)
    esl_ok = _ok(esl)
    dp_dead = _present_but_dead(dialplan)
    esl_dead = _present_but_dead(esl)
    esl_missing = not esl.get("exists")

    if dp_ok and esl_ok:
        return P7_CASE_R3
    if dp_ok and esl_missing:
        return P7_CASE_R1
    if dp_ok and esl_dead:
        return P7_CASE_R4
    if (not dp_ok) and (not esl_ok):
        # Both broken in some way. R2 covers this.
        return P7_CASE_R2
    return P7_CASE_INC


def log_phase7_decision(verdict: str) -> None:
    log.info("---------- PHASE-7 DECISION ENGINE -----------")
    log.info("    %s", verdict)
    log.info("----------------------------------------------")


# --------------------------- handler ----------------------------------

def handle(conn: socket.socket, addr) -> None:
    t0 = time.monotonic()
    state = "CONNECTED"
    cp: dict = {"T0_tcp_accept": 0.0}
    rtp: dict = {}
    log.info("=== TCP CONNECT from %s:%d === (T0)", addr[0], addr[1])
    log.info("[CALL_START] Outbound ESL connected from %s:%d", addr[0], addr[1])
    log.info("STATE -> %s", state)

    conn.settimeout(10.0)
    buf = b""
    # Phase 12: per-call write serialiser. Acquired by the playback
    # listener thread before injecting a playback sendmsg, and by
    # this thread for any teardown-time writes after the call has
    # entered WAITING (the only window in which the listener can
    # legitimately race us). Pre-WAITING writes (handshake +
    # record_session + audio_stream + initial playback) cannot race
    # because the call is not yet registered with the listener.
    send_lock = threading.Lock()
    registered_uuid: str = ""

    counters = {
        "frames_total": 0,
        "command_reply_ok": 0,
        "command_reply_err": 0,
        "events_total": 0,
        "playback_execute": 0,
        "playback_execute_complete": 0,
        "disconnect_notice": 0,
        # P9-D: counters for audio_stream lifecycle. Additive only —
        # classify() and the Phase-7 decision engine ignore these keys.
        "audio_stream_start": 0,
        "audio_stream_stop": 0,
        "audio_stream_err": 0,
    }
    last_command_reply: str = ""
    last_execute_app_resp: str = ""
    # P4-B/P4-D: recording target path. Pre-declared at handler scope
    # so the finally block can run RECORDING VALIDATION even if the
    # try body raised before P4-B set it.
    recording_path: str = ""
    # P6-B/P6-C: ensure stop_record_session is dispatched at most once
    # per call. Multiple senders trigger the stop (CHANNEL_HANGUP,
    # CHANNEL_HANGUP_COMPLETE, text/disconnect-notice) — whichever
    # arrives first wins, the others no-op.
    stop_record_sent: bool = False
    # P9-B/P9-C: same idempotence pattern for the audio_stream start
    # and stop. audio_stream_started flips True the moment the start
    # bgapi sendall returns; audio_stream_stop_sent flips True the
    # moment the stop bgapi sendall returns.
    audio_stream_started: bool = False
    audio_stream_stop_sent: bool = False

    try:
        # ------------- handshake (Phase 1, unchanged) -----------------
        send_raw(conn, b"connect" + END, "CONNECT", t0)
        cp["T1_connect_sent"] = time.monotonic() - t0
        state = "SENT_CONNECT"
        log.info("STATE -> %s", state)

        first_payload_at = None
        got_channel_data = False
        for i in range(3):
            try:
                headers, body, raw, buf = read_frame(conn, buf)
            except socket.timeout:
                log.error("HANDSHAKE TIMEOUT waiting for CHANNEL_DATA "
                          "(frame #%d, +%.3fs).",
                          i, time.monotonic() - t0)
                state = "DISCONNECTED"
                return
            if headers is None:
                log.error("EOF during handshake at +%.3fs.",
                          time.monotonic() - t0)
                state = "DISCONNECTED"
                return
            if first_payload_at is None:
                first_payload_at = time.monotonic()
                log.info("FIRST PAYLOAD at +%.3fs (header_bytes=%d, "
                         "body_bytes=%d)",
                         first_payload_at - t0,
                         len(raw) - len(body), len(body))
            ctype = headers.get("Content-Type", "(missing)")
            ev = event_name(headers, body)
            log.info("RECV frame #%d  Content-Type=%s  Event-Name=%r  "
                     "header_keys=%d  body=%dB",
                     i, ctype, ev, len(headers), len(body))
            for k, v in headers.items():
                log.debug("    hdr  %s: %s", k, v)
            if body:
                log.debug("    body[:400] = %r", body[:400])

            if ctype == "text/disconnect-notice":
                log.error("DISCONNECT-NOTICE during handshake at +%.3fs.",
                          time.monotonic() - t0)
                state = "DISCONNECTED"
                return
            if looks_like_channel_data(headers, body):
                got_channel_data = True
                cp["T2_channel_data"] = time.monotonic() - t0
                state = "RECEIVED_CHANNEL_DATA"
                log.info("STATE -> %s (T2 +%.3fs)",
                         state, cp["T2_channel_data"])
                # Phase 3 additions: extract RTP fields and emit the
                # tcpdump-command block immediately so the operator can
                # copy-paste them while the call is still being set up.
                try:
                    rtp = extract_rtp_info(headers)
                    log_rtp_session_info(rtp)
                    log_tcpdump_commands(rtp)
                except Exception as _rtp_err:
                    log.error("extract_rtp_info failed (non-fatal, "
                              "call continues with empty rtp): %r",
                              _rtp_err)
                break

        if not got_channel_data:
            log.error("Did not see CHANNEL_DATA within 3 frames. "
                      "Aborting.")
            return

        send_raw(conn, b"linger" + END, "LINGER", t0)
        state = "SENT_LINGER"
        log.info("STATE -> %s", state)

        send_raw(conn, b"myevents" + END, "MYEVENTS", t0)
        state = "SENT_MYEVENTS"
        log.info("STATE -> %s", state)

        # ------------- Phase 5 (P5-A): record_session FIRST -----------
        # Sent BEFORE playback so the FS media-bug attaches before any
        # write-side audio is generated. record_session has no
        # event-lock (see build_record_payload's docstring), so it
        # completes its attach before playback's event-locked sendmsg
        # acquires the channel. The playback wire bytes are unchanged.
        # Failure of this sendmsg is non-fatal: playback still runs.
        uuid_for_record = (rtp.get("uuid") or "").strip()
        if uuid_for_record and uuid_for_record != "unknown":
            recording_path = os.path.join(
                RECORDING_DIR, f"{uuid_for_record}.wav")
            try:
                send_raw(conn, build_record_payload(recording_path),
                         "RECORD_SESSION", t0)
                # P5-B: dual-write of NEW (T3_record_sent) and LEGACY
                # (T3b_record_session_sent) checkpoint keys. classify()
                # references neither, so verdict logic is unchanged.
                _t = time.monotonic() - t0
                cp["T3_record_sent"] = _t
                cp["T3b_record_session_sent"] = _t
                state = "SENT_RECORD"
                log.info("STATE -> %s (T3 +%.3fs) — record_session "
                         "sendmsg dispatched  path=%s",
                         state, _t, recording_path)
            except OSError as e:
                # Connection wedged before playback was even attempted.
                # Do NOT re-raise: drop through and try the playback
                # send anyway so we still attempt the validated path.
                log.error("RECORDING sendmsg failed: %r "
                          "(continuing to playback unaffected)", e)
        else:
            log.warning("RECORDING SKIP: no usable UUID from "
                        "CHANNEL_DATA. Playback unaffected.")

        # ------------- Phase 9 (P9-B): live audio_stream fork --------
        # Dispatched AFTER record_session and BEFORE playback so the
        # WS-side media bug attaches before any write-side audio
        # plays. Uses bgapi (mod_audio_stream is API-typed, not a
        # dialplan app). Fire-and-forget: failure to start the stream
        # is non-fatal — control falls through to the validated
        # playback path. The PLAYBACK_PAYLOAD bytes below are
        # byte-for-byte identical from Phase 2.
        if (uuid_for_record and uuid_for_record != "unknown"
                and AUDIO_STREAM_WS_URL):
            try:
                payload = build_audio_stream_payload(
                    uuid_for_record, AUDIO_STREAM_WS_URL,
                    AUDIO_STREAM_SAMPLE_RATE,
                    rtp.get("caller_number", ""),
                    did=rtp.get("did", ""),
                    campaign_id=rtp.get("campaign_id", ""),
                    agent_slug=rtp.get("agent_slug", ""),
                    dest=rtp.get("dest", ""))
                send_raw(conn, payload, "AUDIO_STREAM_START", t0)
                _t = time.monotonic() - t0
                cp["T3c_audio_stream_sent"] = _t
                audio_stream_started = True
                counters["audio_stream_start"] += 1
                log.info("AUDIO_STREAM dispatched (T3c +%.3fs) "
                         "ws_url=%s sr=%d uuid=%s",
                         _t, AUDIO_STREAM_WS_URL,
                         AUDIO_STREAM_SAMPLE_RATE,
                         uuid_for_record)
            except OSError as e:
                counters["audio_stream_err"] += 1
                log.error("AUDIO_STREAM start sendmsg failed: %r "
                          "(playback path unaffected)", e)
        else:
            if not AUDIO_STREAM_WS_URL:
                log.info("AUDIO_STREAM disabled (AUDIO_STREAM_WS_URL "
                         "is empty). Playback path unaffected.")

        # ------------- Phase 2 diagnostic tone: REMOVED ----------------
        # This used to unconditionally fire a 6s tone_stream://%(2000,
        # 4000,440) probe beep (PLAYBACK_PAYLOAD) on every call, left
        # over from the Phase-2..9 RTP forensic investigation. It blocked
        # the channel for ~6s and was audible to callers as a beep
        # before the AI greeting could start. Removed for production —
        # it was never a real call-flow requirement, only a diagnostic.
        state = "SKIPPED_DIAGNOSTIC_PLAYBACK"
        log.info("STATE -> %s (diagnostic tone probe disabled)", state)

        # ------------- WAITING with execution-aware classification ----
        state = "WAITING"
        log.info("STATE -> %s", state)
        conn.settimeout(30.0)
        last_log = time.monotonic()

        # Phase 12 (P12-PL): register the call for playback injection.
        # Done AFTER the validated startup sequence (handshake +
        # record_session + audio_stream + tone playback dispatch) so
        # the listener cannot race the deterministic Phase-1..9 setup.
        # Concurrent listener-side writes from this point on are
        # serialised via send_lock; teardown writes below also take
        # send_lock so they cannot interleave with an in-flight inject.
        if uuid_for_record and uuid_for_record != "unknown":
            registered_uuid = uuid_for_record
            _register_call(registered_uuid, conn, send_lock, t0)

        while True:
            try:
                headers, body, raw, buf = read_frame(conn, buf)
            except socket.timeout:
                now = time.monotonic()
                log.info("idle: no frames for %.1fs (uptime %.1fs)",
                         now - last_log, now - t0)
                last_log = now
                continue

            if headers is None:
                log.error("EOF from FS at uptime %.3fs.",
                          time.monotonic() - t0)
                state = "DISCONNECTED"
                break

            counters["frames_total"] += 1
            ctype = headers.get("Content-Type", "(missing)")
            ev = event_name(headers, body)
            now = time.monotonic() - t0
            log.info("RECV ctype=%s event=%s body=%dB (uptime %.3fs)",
                     ctype, ev, len(body), now)
            for k, v in headers.items():
                log.debug("    hdr  %s: %s", k, v)
            if body:
                log.debug("    body[:400] = %r", body[:400])

            # ----- classify --------------------------------------------
            if ctype == "command/reply":
                reply = headers.get("Reply-Text", "")
                last_command_reply = reply
                if reply_is_ok(reply):
                    counters["command_reply_ok"] += 1
                    log.info("[%s] [ESL RESPONSE] ACK: command/reply +OK -> %r", registered_uuid, reply)
                elif reply_is_err(reply):
                    counters["command_reply_err"] += 1
                    log.error("[%s] [ESL RESPONSE] NACK: command/reply -ERR -> %r  "
                              "(this is the smoking gun if it follows "
                              "the playback sendmsg directly)", registered_uuid, reply)
                else:
                    log.warning("[%s] [ESL RESPONSE] command/reply with unusual "
                                "Reply-Text=%r", registered_uuid, reply)

            elif ctype == "text/event-plain":
                counters["events_total"] += 1
                app = body_field(body, "Application")
                app_data = body_field(body, "Application-Data")
                app_resp = body_field(body, "Application-Response")

                if ev == "CHANNEL_ANSWER":
                    log.info("[%s] [CHANNEL STATE] CHANNEL_ANSWER received at +%.3fs", registered_uuid, now)

                if ev == "CHANNEL_EXECUTE" and app == "playback":
                    cp.setdefault("T4_playback_execute", now)
                    cp.setdefault("T6_playback_execute", now)  # P5-B
                    counters["playback_execute"] += 1
                    t5_sent = cp.get("T5_playback_sent")
                    # T5_playback_sent is only set by the (now-removed)
                    # diagnostic tone probe — every other playback (e.g.
                    # the AI agent's TTS injected via the Phase-12
                    # listener) has no T5, so this must not be a direct
                    # dict lookup or it raises KeyError and crashes this
                    # call's handler thread, which makes FreeSWITCH treat
                    # the dead ESL control connection as a reason to hang
                    # up the channel.
                    if t5_sent is not None:
                        log.info("EXEC START: Application=%s  "
                                 "Application-Data=%r  (T6 +%.3fs, "
                                 "+%.1fms after T5)",
                                 app, app_data, now, (now - t5_sent) * 1000)
                    else:
                        log.info("EXEC START: Application=%s  "
                                 "Application-Data=%r  (T6 +%.3fs)",
                                 app, app_data, now)
                elif ev == "CHANNEL_EXECUTE_COMPLETE" and app == "playback":
                    cp.setdefault("T5_playback_execute_complete", now)
                    cp.setdefault("T7_playback_complete", now)  # P5-B
                    counters["playback_execute_complete"] += 1
                    last_execute_app_resp = app_resp
                    log.info("EXEC DONE: Application=%s  "
                             "Application-Response=%r  (T7 +%.3fs)",
                             app, app_resp, now)
                # P4-C: record_session execute lifecycle, classified
                # before the existing non-playback catch-all so that
                # block keeps its current behaviour for every other
                # application. record_session is a non-blocking app —
                # COMPLETE typically fires right after EXECUTE; the
                # actual recording continues until hangup, which is
                # signaled separately by RECORD_STOP below.
                elif ev == "CHANNEL_EXECUTE" and app == "record_session":
                    cp.setdefault("T4b_record_execute", now)
                    cp.setdefault("T4_record_execute", now)  # P5-B
                    counters["record_execute"] = counters.get(
                        "record_execute", 0) + 1
                    log.info("RECORDING start confirmed: "
                             "CHANNEL_EXECUTE app=record_session  "
                             "Application-Data=%r  (T4 +%.3fs)",
                             app_data, now)
                elif ev == "CHANNEL_EXECUTE_COMPLETE" and app == "record_session":
                    cp.setdefault("T5b_record_execute_complete", now)
                    counters["record_execute_complete"] = counters.get(
                        "record_execute_complete", 0) + 1
                    log.info("RECORDING execute returned: "
                             "CHANNEL_EXECUTE_COMPLETE "
                             "app=record_session  "
                             "Application-Response=%r  (T5b +%.3fs)",
                             app_resp, now)
                elif ev in ("CHANNEL_EXECUTE",
                            "CHANNEL_EXECUTE_COMPLETE"):
                    log.info("EXEC %s for non-playback app=%r "
                             "data=%r resp=%r",
                             ev, app, app_data, app_resp)
                elif ev == "BACKGROUND_JOB":
                    log.info("BACKGROUND_JOB Job-UUID=%s body[:200]=%r",
                             body_field(body, "Job-UUID"), body[:200])
                elif ev == "CHANNEL_HANGUP":
                    log.info("CHANNEL_HANGUP cause=%s",
                             body_field(body, "Hangup-Cause"))
                    # Phase 12: drop the call from the playback
                    # registry FIRST so a concurrent listener
                    # dispatch becomes a no-op before we start
                    # writing teardown bytes. send_lock then
                    # serialises us against any inject already in
                    # progress.
                    if registered_uuid:
                        _unregister_call(registered_uuid)
                        registered_uuid = ""
                    with send_lock:
                        # P6-B: earliest opportunity to flush the
                        # WAV. Send stop_record_session now so FS
                        # rewrites the header before the channel is
                        # fully torn down. Idempotent.
                        if (recording_path and not stop_record_sent):
                            try:
                                send_raw(conn,
                                         build_stop_record_payload(recording_path),
                                         "STOP_RECORD_SESSION", t0)
                                stop_record_sent = True
                                log.info("STOP_RECORD_SESSION dispatched on "
                                         "CHANNEL_HANGUP  path=%s",
                                         recording_path)
                            except OSError as e:
                                log.error("STOP_RECORD_SESSION sendmsg "
                                          "failed on CHANNEL_HANGUP: %r",
                                          e)
                        # P9-C: also stop audio_stream so the WS
                        # closes cleanly. Same idempotence pattern.
                        if (audio_stream_started
                                and not audio_stream_stop_sent
                                and uuid_for_record):
                            try:
                                send_raw(
                                    conn,
                                    build_audio_stream_stop_payload(
                                        uuid_for_record),
                                    "AUDIO_STREAM_STOP", t0)
                                audio_stream_stop_sent = True
                                counters["audio_stream_stop"] += 1
                                log.info("AUDIO_STREAM stop dispatched "
                                         "on CHANNEL_HANGUP  uuid=%s",
                                         uuid_for_record)
                            except OSError as e:
                                counters["audio_stream_err"] += 1
                                log.error("AUDIO_STREAM stop sendmsg "
                                          "failed on CHANNEL_HANGUP: %r",
                                          e)
                elif ev == "CHANNEL_HANGUP_COMPLETE":
                    log.info("CHANNEL_HANGUP_COMPLETE cause=%s",
                             body_field(body, "Hangup-Cause"))
                    # Phase 12: drop from registry then take the
                    # send_lock for teardown writes. Idempotent vs.
                    # CHANNEL_HANGUP if both events fire.
                    if registered_uuid:
                        _unregister_call(registered_uuid)
                        registered_uuid = ""
                    with send_lock:
                        # P6-B: fallback path if CHANNEL_HANGUP was
                        # not delivered on this run. Same idempotence
                        # guard.
                        if (recording_path and not stop_record_sent):
                            try:
                                send_raw(conn,
                                         build_stop_record_payload(recording_path),
                                         "STOP_RECORD_SESSION", t0)
                                stop_record_sent = True
                                log.info("STOP_RECORD_SESSION dispatched on "
                                         "CHANNEL_HANGUP_COMPLETE  path=%s",
                                         recording_path)
                            except OSError as e:
                                log.error("STOP_RECORD_SESSION sendmsg "
                                          "failed on "
                                          "CHANNEL_HANGUP_COMPLETE: %r", e)
                        # P9-C: same audio_stream stop fallback.
                        if (audio_stream_started
                                and not audio_stream_stop_sent
                                and uuid_for_record):
                            try:
                                send_raw(
                                    conn,
                                    build_audio_stream_stop_payload(
                                        uuid_for_record),
                                    "AUDIO_STREAM_STOP", t0)
                                audio_stream_stop_sent = True
                                counters["audio_stream_stop"] += 1
                                log.info("AUDIO_STREAM stop dispatched "
                                         "on CHANNEL_HANGUP_COMPLETE  "
                                         "uuid=%s", uuid_for_record)
                            except OSError as e:
                                counters["audio_stream_err"] += 1
                                log.error("AUDIO_STREAM stop sendmsg "
                                          "failed on "
                                          "CHANNEL_HANGUP_COMPLETE: %r",
                                          e)
                # P4-C: native FS recording lifecycle events. These
                # arrive on the same myevents subscription — no new
                # subscription required.
                elif ev == "RECORD_START":
                    cp.setdefault("T7_record_start", now)
                    log.info("EVENT RECORD_START  "
                             "Record-File-Path=%r  (T7 +%.3fs)",
                             body_field(body, "Record-File-Path"), now)
                elif ev == "RECORD_STOP":
                    cp.setdefault("T8_record_stop", now)
                    log.info("EVENT RECORD_STOP  "
                             "Record-File-Path=%r  "
                             "Record-File-Size=%s  Record-Ms=%s  "
                             "(T8 +%.3fs)",
                             body_field(body, "Record-File-Path"),
                             body_field(body, "Record-File-Size"),
                             body_field(body, "Record-Ms"),
                             now)

            elif ctype == "text/disconnect-notice":
                counters["disconnect_notice"] += 1
                cp.setdefault("T6_disconnect_notice", now)
                disp = headers.get("Content-Disposition", "")
                log.error("DISCONNECT-NOTICE at uptime %.3fs  "
                          "Content-Disposition=%r", now, disp)
                state = "DISCONNECTED"
                # Phase 12: same pattern as the hangup branches.
                # Unregister BEFORE acquiring send_lock so any
                # concurrent listener call rejects against the
                # registry first; send_lock covers the actual writes.
                if registered_uuid:
                    _unregister_call(registered_uuid)
                    registered_uuid = ""
                with send_lock:
                    # P6-C: if no hangup event fired before disconnect-
                    # notice (or stop has not yet been dispatched for
                    # any other reason), send stop_record_session
                    # NOW. We are still inside Linger-Time: -1 so the
                    # socket is open and the channel is reachable for
                    # one last command.
                    if (recording_path and not stop_record_sent):
                        try:
                            send_raw(conn,
                                     build_stop_record_payload(recording_path),
                                     "STOP_RECORD_SESSION", t0)
                            stop_record_sent = True
                            log.info("STOP_RECORD_SESSION dispatched on "
                                     "disconnect-notice  path=%s",
                                     recording_path)
                        except OSError as e:
                            log.error("STOP_RECORD_SESSION sendmsg failed "
                                      "on disconnect-notice: %r", e)
                    # P9-C: also stop audio_stream on the disconnect-
                    # notice path. Must precede post_stop_drain so
                    # the +OK ack arrives in the same drain window.
                    if (audio_stream_started
                            and not audio_stream_stop_sent
                            and uuid_for_record):
                        try:
                            send_raw(
                                conn,
                                build_audio_stream_stop_payload(
                                    uuid_for_record),
                                "AUDIO_STREAM_STOP", t0)
                            audio_stream_stop_sent = True
                            counters["audio_stream_stop"] += 1
                            log.info("AUDIO_STREAM stop dispatched on "
                                     "disconnect-notice  uuid=%s",
                                     uuid_for_record)
                        except OSError as e:
                            counters["audio_stream_err"] += 1
                            log.error("AUDIO_STREAM stop sendmsg failed "
                                      "on disconnect-notice: %r", e)
                    # P6-C: post-stop drain. We do this whenever the
                    # stop has been sent (here or earlier on hangup)
                    # so FS has the explicit window to deliver +OK
                    # and RECORD_STOP and rewrite the WAV header
                    # before we close the TCP.
                    if stop_record_sent:
                        buf, _ack, _rec_stop = post_stop_drain(
                            conn, buf, POST_STOP_WAIT_S)
                    # P3-D: deterministic teardown. With Linger-Time:
                    # -1 FS keeps the TCP open; we send `exit` so it
                    # closes cleanly and break to the finally block
                    # so the SESSION SUMMARY always emits.
                    try:
                        conn.sendall(EXIT_PAYLOAD)
                        log.info("SEND[EXIT] sendall returned (clean "
                                 "teardown after disconnect-notice)")
                    except OSError as e:
                        log.warning("SEND[EXIT] failed: %r "
                                    "(FS already closed?)", e)
                break

    except (BrokenPipeError, ConnectionResetError) as e:
        log.error("LOCAL SOCKET ERROR state=%s after %.3fs: %r",
                  state, time.monotonic() - t0, e)
    except socket.timeout:
        log.error("Unhandled socket timeout state=%s after %.3fs",
                  state, time.monotonic() - t0)
    except Exception as e:
        log.exception("UNEXPECTED state=%s after %.3fs: %r",
                      state, time.monotonic() - t0, e)
    finally:
        # Phase 12: safety net — guarantee unregistration in every
        # exit path (BrokenPipeError, socket.timeout, unexpected
        # exception, normal break). Idempotent: a no-op if a teardown
        # branch already unregistered.
        if registered_uuid:
            try:
                _unregister_call(registered_uuid)
            except Exception:
                pass
            registered_uuid = ""
        # ---------------- post-mortem summary -------------------------
        log.info("---------- SESSION SUMMARY ----------")
        log.info("final_state=%s  uptime=%.3fs  remaining_buf=%dB",
                 state, time.monotonic() - t0, len(buf))
        log.info("checkpoints:")
        for key in ("T0_tcp_accept", "T1_connect_sent", "T2_channel_data",
                    "T3_playback_sent", "T3b_record_session_sent",
                    "T4_playback_execute", "T4b_record_execute",
                    "T5_playback_execute_complete",
                    "T5b_record_execute_complete",
                    "T6_disconnect_notice",
                    "T7_record_start", "T8_record_stop"):
            log.info("    %-32s %s", key, fmt_t(cp, key))
        # P5-B: explicit Phase-5 timeline view of the same call. These
        # are dual-written keys; the values above are unchanged.
        log.info("phase5_timeline:")
        for key in ("T3_record_sent", "T4_record_execute",
                    "T5_playback_sent", "T6_playback_execute",
                    "T7_playback_complete"):
            log.info("    %-32s %s", key, fmt_t(cp, key))
        # P9-D: phase9_timeline mirrors phase5_timeline. Additive
        # only — every value here is sourced from cp keys written
        # by the audio_stream dispatch path.
        log.info("phase9_timeline:")
        for key in ("T3c_audio_stream_sent",):
            log.info("    %-32s %s", key, fmt_t(cp, key))
        log.info("deltas:")
        log.info("    T3->T4  sendmsg -> EXECUTE        %s",
                 fmt_delta(cp, "T3_playback_sent",
                           "T4_playback_execute"))
        log.info("    T4->T5  EXECUTE -> COMPLETE       %s",
                 fmt_delta(cp, "T4_playback_execute",
                           "T5_playback_execute_complete"))
        log.info("    T3->T6  sendmsg -> disconnect     %s",
                 fmt_delta(cp, "T3_playback_sent",
                           "T6_disconnect_notice"))
        log.info("counters: %s", counters)
        log.info("last_command_reply=%r", last_command_reply)
        log.info("last_execute_app_response=%r", last_execute_app_resp)
        # URL-decode FreeSWITCH Application-Response before classify() so
        # "PLAYBACK%20ERROR" is seen as "PLAYBACK ERROR" — consistent with
        # the playback_played check below that already does this decode.
        from urllib.parse import unquote as _url_unquote
        _last_app_resp_clean = _url_unquote(last_execute_app_resp or "")
        verdict = classify(cp, counters, last_command_reply,
                           _last_app_resp_clean)
        log.info("VERDICT: %s", verdict)
        log.info("-------------------------------------")
        # Phase 3: only emits when the verdict is CASE C/H — no noise
        # for unrelated outcomes.
        log_next_action(rtp, cp, verdict)

        # P4-D: post-call WAV validation. Pure local read; never
        # raises out to this finally block. Logged AFTER NEXT REQUIRED
        # ACTION and BEFORE socket teardown so post-mortem log scrapes
        # see all session blocks contiguously.
        # P8: defensive try/except — validate_wav already catches its
        # known failure modes, but if any new failure mode escapes
        # (e.g. an unforeseen OSError category), we MUST not suppress
        # the rest of the diagnostic chain (FULL DUPLEX VERDICT,
        # OPERATOR INSTRUCTIONS, DIALPLAN VALIDATION, A/B COMPARISON,
        # FS INTERNAL DIAGNOSTICS, PHASE-7 DECISION ENGINE).
        if recording_path:
            try:
                wav_info = validate_wav(recording_path)
            except Exception as exc:
                log.info("validate_wav crashed: %r — emitting empty "
                         "info so downstream blocks still log", exc)
                wav_info = {
                    "path": recording_path, "exists": True,
                    "size_bytes": 0, "channels": None,
                    "sample_rate": None, "sample_width": None,
                    "approx_duration_s": None,
                    "non_empty_audio": False, "comp_type": None,
                    "peak_amplitude": 0, "non_silence": False,
                    "error": f"validate_wav crashed: {exc!r}",
                }
            try:
                log_recording_validation(wav_info)
            except Exception as exc:
                log.info("log_recording_validation crashed: %r", exc)
        else:
            wav_info = {
                "path": "", "exists": False, "size_bytes": 0,
                "channels": None, "sample_rate": None,
                "sample_width": None, "approx_duration_s": None,
                "non_empty_audio": False, "comp_type": None,
                "peak_amplitude": 0, "non_silence": False,
                "error": "no recording attempted",
            }
            log.info("---------- RECORDING VALIDATION ------")
            log.info("    no recording was attempted this call")
            log.info("--------------------------------------")

        # P5-F: regression safeguards. Emit BEFORE the full-duplex
        # verdict so the operator sees the alarm first if Phase-5
        # reordering broke the validated playback path. The legacy
        # checkpoint (T4_playback_execute) is the canonical signal.
        playback_executed = "T4_playback_execute" in cp
        # Issue 6: FreeSWITCH URL-encodes Application-Response values
        # (e.g. "FILE%20PLAYED"). URL-decode before checking so the
        # PHASE-5 REGRESSION SUSPECTED log is not a false alarm.
        from urllib.parse import unquote as _unquote
        _resp_decoded = _unquote(last_execute_app_resp or "").upper()
        playback_played = playback_executed and "FILE PLAYED" in _resp_decoded
        if counters.get("events_total", 0) == 0 and counters.get("frames_total", 0) == 0:
            # Zero-event connection = health probe, not a call. Emitting
            # CRITICAL here buried the real regression alarm in noise (two
            # probe CRITICALs per hour, zero real ones — 2026-07-23 audit).
            log.info("PHASE-5 check skipped: no events/frames on this "
                     "connection (health probe)")
        elif not playback_executed:
            log.critical(
                "PHASE-5 REGRESSION: no CHANNEL_EXECUTE for "
                "Application=playback was observed. The validated "
                "playback path did not run on this call. Investigate "
                "before any further Phase-5 testing — caller will "
                "have heard NO beep tone.")
        elif not playback_played:
            log.critical(
                "PHASE-5 REGRESSION SUSPECTED: playback executed but "
                "Application-Response=%r is not 'FILE PLAYED'. "
                "Caller may not have heard the beep cleanly.",
                last_execute_app_resp)

        # P5-D: full-duplex verdict block.
        fdx, rationale = full_duplex_verdict(verdict, wav_info, cp)
        log_full_duplex_verdict(fdx, rationale, wav_info, cp)

        # P5-E: operator listening instructions.
        log_operator_instructions(wav_info, fdx)

        # P7-B: dialplan-native vs ESL-triggered recording A/B. Pure
        # diagnostic; never raises out, never sends on the wire.
        try:
            dialplan_wav_info = validate_wav(DIALPLAN_TEST_WAV)
        except Exception as exc:
            log.info("DIALPLAN RECORDING VALIDATION skipped: %r", exc)
            dialplan_wav_info = {
                "path": DIALPLAN_TEST_WAV, "exists": False,
                "size_bytes": 0, "channels": None,
                "sample_rate": None, "sample_width": None,
                "approx_duration_s": None, "non_empty_audio": False,
                "comp_type": None, "peak_amplitude": 0,
                "non_silence": False, "error": repr(exc),
            }
        try:
            log_dialplan_recording_validation(dialplan_wav_info)
        except Exception as exc:
            log.info("log_dialplan_recording_validation failed: %r",
                     exc)
        try:
            log_ab_comparison(wav_info, dialplan_wav_info, cp)
        except Exception as exc:
            log.info("log_ab_comparison failed: %r", exc)
        try:
            log_fs_internal_diagnostics()
        except Exception as exc:
            log.info("log_fs_internal_diagnostics failed: %r", exc)
        try:
            p7_verdict = phase7_decision_engine(wav_info,
                                                dialplan_wav_info)
            log_phase7_decision(p7_verdict)
        except Exception as exc:
            log.info("phase7_decision_engine failed: %r", exc)

        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()


def classify(cp: dict, counters: dict, last_reply: str,
             last_app_resp: str) -> str:
    """Map observed checkpoints + counters to a decision-matrix case.
    Pure function, no side effects. Returns a single-line verdict."""
    has_t3 = "T3_playback_sent" in cp
    has_t4 = "T4_playback_execute" in cp
    has_t5 = "T5_playback_execute_complete" in cp
    has_t6 = "T6_disconnect_notice" in cp

    if not has_t3:
        return "INCONCLUSIVE: playback was never sent"

    # CASE A — explicit -ERR or instant disconnect.
    if counters["command_reply_err"] > 0 and not has_t4:
        return (f"CASE A — sendmsg malformed or rejected. "
                f"FS replied -ERR. last_reply={last_reply!r}")
    if (has_t6 and not has_t4
            and (cp["T6_disconnect_notice"]
                 - cp["T3_playback_sent"]) < 0.500):
        return ("CASE A — FS disconnected within 500ms of sendmsg, "
                "no CHANNEL_EXECUTE seen. Treat as malformed/rejected.")

    # CASE D — execute arrived after disconnect/hangup.
    if (has_t4 and has_t6
            and cp["T4_playback_execute"] >= cp["T6_disconnect_notice"]):
        return ("CASE D — CHANNEL_EXECUTE arrived after "
                "disconnect-notice. Order/queue race.")

    # CASE B — accepted, never executed.
    if not has_t4 and has_t6:
        return ("CASE B — sendmsg accepted (no -ERR) but no "
                "CHANNEL_EXECUTE for playback before disconnect.")

    # CASE F — COMPLETE before EXECUTE.
    if has_t5 and has_t4 and cp["T5_playback_execute_complete"] < cp["T4_playback_execute"]:
        return ("CASE F — CHANNEL_EXECUTE_COMPLETE arrived before "
                "CHANNEL_EXECUTE. event-lock semantics anomaly.")

    # CASE G — application-level error.
    if has_t4 and has_t5:
        upper = last_app_resp.upper()
        if ("ERR" in upper or upper in ("FILE_NOT_FOUND",
                                         "INVALID_URL", "GENERR")):
            return (f"CASE G — playback application failure. "
                    f"Application-Response={last_app_resp!r}")
        # CASE C / H — clean ESL execution; if audio still silent the
        # failure is below the ESL layer.
        return (f"CASE C — playback executed cleanly at the ESL layer "
                f"(EXECUTE@T4, COMPLETE@T5, "
                f"Application-Response={last_app_resp!r}). If audio is "
                f"still silent, the failure is below the ESL layer "
                f"(RTP/media). Resolve C.1 vs C.2/H via tcpdump.")

    # PARTIAL / E — execute fired but not yet complete, or +OK seen but
    # nothing else.
    if has_t4 and not has_t5 and not has_t6:
        return ("PARTIAL — CHANNEL_EXECUTE seen, no COMPLETE yet. "
                "Either the tone is still playing (~6s expected) or "
                "the app is stuck (CASE F candidate).")
    if (not has_t4 and counters["command_reply_ok"] > 0
            and not has_t6):
        return ("CASE B/E — +OK ack received but no CHANNEL_EXECUTE "
                "for the lifetime so far. Indistinguishable between "
                "queued-and-deferred (E) and accepted-but-dropped (B) "
                "until hangup.")

    return "INCONCLUSIVE — see checkpoints above"


def main() -> None:
    # P4-A: pre-create the recordings directory so record_session does
    # not fail on the first call before an operator has SSH'd in.
    ensure_recording_dir()
    import subprocess
    try:
        subprocess.run(["fs_cli", "-x", "reloadxml"], capture_output=True)
    except FileNotFoundError:
        # Containerized runtime: fs_cli lives on the host, not in the image.
        # The reload is a startup nicety only — FS re-reads config on its own
        # lifecycle; dashboard "apply" paths trigger reloads host-side.
        log.info("fs_cli not on PATH — skipping startup reloadxml")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(8)
    log.info("Phase-9 ESL probe listening on %s:%d "
             "(handshake + record_session BEFORE playback + RTP "
             "observability + tcpdump-command generation + explicit "
             "stop_record_session on hangup + post-stop drain + WAV "
             "validation + full-duplex verdict + operator "
             "instructions + dialplan-native A/B + FS internal "
             "diagnostics + R1/R2/R3/R4 decision engine + live "
             "audio_stream fork to %s sr=%dHz)",
             HOST, PORT,
             AUDIO_STREAM_WS_URL or "(disabled)",
             AUDIO_STREAM_SAMPLE_RATE)
    # Phase 12 (P12-PL): start the playback-injection listener on a
    # daemon thread BEFORE we begin accepting outbound-ESL calls, so
    # the orchestrator (which may already be up) sees its peer
    # immediately on the first turn. A bind failure inside the
    # listener does NOT stop the ESL accept loop — Phase-12 playback
    # injection is purely additive, and the validated tone playback
    # still works on its own.
    if PLAYBACK_LISTEN_ENABLED:
        pl_t = threading.Thread(
            target=_playback_listener_thread,
            daemon=True,
            name="playback-listener",
        )
        pl_t.start()
        log.info("Phase-12 playback listener thread started "
                 "(target %s:%d)",
                 PLAYBACK_LISTEN_HOST, PLAYBACK_LISTEN_PORT)
    else:
        log.info("Phase-12 playback listener DISABLED via env")
    try:
        while True:
            conn, addr = srv.accept()
            t = threading.Thread(
                target=handle, args=(conn, addr), daemon=True,
                name=f"esl-{addr[1]}",
            )
            t.start()
    except KeyboardInterrupt:
        log.info("shutdown requested")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
