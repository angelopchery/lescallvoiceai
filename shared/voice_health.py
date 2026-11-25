"""Voice-provider health for the sentinel — the check that was missing on
2026-08-03, when Sarvam STT/TTS hung intermittently for ~90 minutes and the
platform's clients discovered it before the platform did.

Two independent signal sources, fused into one healthy/degraded verdict:

PASSIVE (free, every cycle) — incremental tail of ws_audio_receiver.log:
  * "[STT:Sarvam] Request timed out"      → STT hang on a REAL call turn
  * "[TTS:Sarvam] Stream timeout"          → TTS hang on a REAL utterance
  * "speech_end→engine NNNNms"             → true caller-felt turn latency
  * hedge / Whisper-fallback counts        → how hard the runtime is fighting
  Detects degradation within one cycle (~30 s) whenever calls are flowing.

ACTIVE (costs a fraction of a paisa, adaptive cadence) — direct timed probes
of api.sarvam.ai STT (tiny in-memory WAV) and TTS ("ok"), using the live
platform key from calling-agent/.env (picks up watchdog rotations). Covers
the quiet hours when no calls are flowing: every PROBE_EVERY_HEALTHY cycles
while healthy, every PROBE_EVERY_SUSPECT cycles once anything looks off.

Verdict:
  DEGRADED when the 10-min window holds ≥ TIMEOUT_ALERT_N call-turn
  timeouts, or ≥ LAT_BAD_N turns slower than LAT_BAD_MS, or ≥ 2 of the last
  3 probe rounds hung/5xx. HEALTHY again after CLEAR_AFTER_S with no bad
  signal — recovery clears the alert rate-limit so the NEXT incident pages
  immediately (the sentinel's 6-h alert window is per-incident, not per-day).

Escalation (optional, OFF until configured): on a fresh degradation the
sentinel can also RING a human — an originate through FreeSWITCH playing a
pre-rendered local WAV (deliberately independent of Sarvam: the alert must
be speakable while the provider is down). Arm it with:
    SENTINEL_ALERT_CALL_NUMBER=91XXXXXXXXXX    (E.164 digits, no +)
    SENTINEL_ALERT_CALL_GATEWAY=<fs gateway name from `sofia status`>
Capped at one call per ALERT_CALL_COOLDOWN_S regardless of alert volume.
"""

from __future__ import annotations

import io
import logging
import math
import os
import re
import struct
import time
import wave
from pathlib import Path

logger = logging.getLogger("sentinel")
# Probe requests would otherwise add two INFO lines per round to the
# sentinel log — noise in exactly the log a human reads during an incident.
logging.getLogger("httpx").setLevel(logging.WARNING)

REPO = Path(__file__).resolve().parent.parent
WS_LOG = REPO / "phase14_runtime" / "logs" / "docker" / "ws_audio_receiver.log"
ENV_FILE = REPO / "calling-agent" / ".env"

STT_URL = "https://api.sarvam.ai/speech-to-text"
TTS_URL = "https://api.sarvam.ai/text-to-speech"

# Cadence (in sentinel cycles, default cycle = 30 s)
PROBE_EVERY_HEALTHY = int(os.environ.get("SENTINEL_VOICE_PROBE_EVERY", "10"))
PROBE_EVERY_SUSPECT = int(os.environ.get("SENTINEL_VOICE_PROBE_SUSPECT", "2"))

# Verdict thresholds
WINDOW_S = 600           # rolling window for passive signals
TIMEOUT_ALERT_N = 3      # call-turn timeouts in window → degraded
LAT_BAD_MS = 5000        # a caller waited ≥ this for the agent to think
LAT_BAD_N = 2            # this many slow turns in window → degraded
PROBE_BAD_MS = 3500      # probe slower than this counts as hung
PROBE_TIMEOUT_S = 4.0    # hard cap per probe request
CLEAR_AFTER_S = 600      # quiet time before declaring recovery

ALERT_CALL_NUMBER = os.environ.get("SENTINEL_ALERT_CALL_NUMBER", "")
ALERT_CALL_GATEWAY = os.environ.get("SENTINEL_ALERT_CALL_GATEWAY", "")
# Caller ID presented on the escalation call — must be a DID the gateway
# accepts (Plivo 403s unregistered CLIs). Defaults to the platform DID.
ALERT_CALL_CLI = os.environ.get("SENTINEL_ALERT_CALL_CLI", "918065953160")
ALERT_CALL_WAV = os.environ.get(
    "SENTINEL_ALERT_CALL_WAV", str(REPO / "deploy" / "sentinel_alert.wav"))
ALERT_CALL_COOLDOWN_S = 2 * 3600

_RE_STT_TIMEOUT = re.compile(r"\[STT:Sarvam\] Request timed out")
_RE_TTS_TIMEOUT = re.compile(r"\[TTS:Sarvam\] Stream timeout")
_RE_TURN_LAT = re.compile(r"speech_end→engine (\d+)ms")
_RE_HEDGE_WON = re.compile(r"Hedge won the race|Primary-race rescued")
_RE_WHISPER = re.compile(r"\[STT\] Fallback succeeded")


def _probe_wav() -> bytes:
    """0.4 s 300 Hz tone, PCM16 mono 16 kHz — a real decode workload for the
    API at the smallest billable size. Built once per process."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        n = int(16000 * 0.4)
        w.writeframes(b"".join(
            struct.pack("<h", int(6000 * math.sin(2 * math.pi * 300 * i / 16000)))
            for i in range(n)))
    return buf.getvalue()


_PROBE_WAV = _probe_wav()


def _api_key() -> str:
    """Live platform key from calling-agent/.env — the same file the key
    watchdog rewrites on rotation, so probes always test the key calls use."""
    try:
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("SARVAM_API_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception as exc:
        logger.warning("voice probe: cannot read API key: %r", exc)
    return ""


def _timed_post(post) -> tuple[str, int]:
    """Run one probe callable → (verdict, elapsed_ms).
    verdict: ok | hung | server-error | auth-error | no-credits | net-error"""
    t0 = time.monotonic()
    try:
        status = post()
    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        is_timeout = "timeout" in type(exc).__name__.lower()
        return ("hung" if is_timeout else "net-error"), ms
    ms = int((time.monotonic() - t0) * 1000)
    if status >= 500:
        return "server-error", ms
    if status in (401, 403):
        return "auth-error", ms
    # 402 = "No credits available". This used to fall through to "ok"
    # because only 5xx/401/403 were checked, so the sentinel reported the
    # voice stack HEALTHY from 2026-08-31 to 09-08 while every single call
    # went out mute — the loudest possible failure, invisible to the one
    # watcher built to catch it (RUNBOOK §34).
    if status == 402:
        return "no-credits", ms
    if ms >= PROBE_BAD_MS:
        return "hung", ms
    return "ok", ms


def probe_sarvam() -> dict:
    """One timed STT + TTS probe round against the live key."""
    import httpx
    key = _api_key()
    if not key:
        return {"stt": "no-key", "tts": "no-key", "stt_ms": 0, "tts_ms": 0}
    timeout = httpx.Timeout(PROBE_TIMEOUT_S, connect=2.0)
    with httpx.Client(timeout=timeout) as client:
        stt_v, stt_ms = _timed_post(lambda: client.post(
            STT_URL,
            headers={"api-subscription-key": key},
            files={"file": ("probe.wav", _PROBE_WAV, "audio/wav")},
            data={"model": "saarika:v2.5"},
        ).status_code)
        tts_v, tts_ms = _timed_post(lambda: client.post(
            TTS_URL,
            headers={"api-subscription-key": key},
            json={"text": "ok", "target_language_code": "en-IN",
                  "model": "bulbul:v3", "speaker": "shubh"},
        ).status_code)
    return {"stt": stt_v, "tts": tts_v, "stt_ms": stt_ms, "tts_ms": tts_ms}


def _tail_new_lines(state_d: dict) -> list[str]:
    """New lines of ws_audio_receiver.log since the last cycle. Rotation-safe
    (inode + truncation check); first sight of the file starts at EOF so
    history is never replayed as fresh signal."""
    try:
        st = WS_LOG.stat()
    except OSError:
        return []
    ino, off = state_d.get("vh:ino"), state_d.get("vh:off", 0)
    if ino != st.st_ino or off > st.st_size:
        # rotated / truncated / first run — arm at current EOF
        state_d["vh:ino"] = st.st_ino
        state_d["vh:off"] = st.st_size
        return []
    if st.st_size == off:
        return []
    try:
        with WS_LOG.open("rb") as f:
            f.seek(off)
            chunk = f.read(4 * 1024 * 1024)
        state_d["vh:off"] = off + len(chunk)
        return chunk.decode("utf-8", errors="replace").splitlines()
    except OSError as exc:
        logger.warning("voice tail failed: %r", exc)
        return []


def _trim(events: list, now: float) -> list:
    keep = now - WINDOW_S
    return [e for e in events if e[0] >= keep][-500:]


def _scan_passive(state_d: dict, now: float) -> dict:
    lines = _tail_new_lines(state_d)
    timeouts = state_d.get("vh:timeouts", [])
    lats = state_d.get("vh:lat", [])
    hedges = whisper = 0
    for line in lines:
        if _RE_STT_TIMEOUT.search(line):
            timeouts.append([now, "stt"])
        elif _RE_TTS_TIMEOUT.search(line):
            timeouts.append([now, "tts"])
        elif _RE_HEDGE_WON.search(line):
            hedges += 1
        elif _RE_WHISPER.search(line):
            whisper += 1
        m = _RE_TURN_LAT.search(line)
        if m:
            lats.append([now, int(m.group(1))])
    state_d["vh:timeouts"] = _trim(timeouts, now)
    state_d["vh:lat"] = _trim(lats, now)
    window_lats = [ms for _, ms in state_d["vh:lat"]]
    return {
        "timeouts_10m": len(state_d["vh:timeouts"]),
        "slow_turns_10m": sum(1 for ms in window_lats if ms >= LAT_BAD_MS),
        "turns_10m": len(window_lats),
        "worst_turn_ms": max(window_lats, default=0),
        "hedge_wins_new": hedges,
        "whisper_wins_new": whisper,
    }


def _alert_call(state_d: dict) -> None:
    """Ring a human. Only armed when both env vars are set; hard-capped."""
    if not (ALERT_CALL_NUMBER and ALERT_CALL_GATEWAY):
        return
    now = time.time()
    if now - state_d.get("vh:last_call_alert", 0) < ALERT_CALL_COOLDOWN_S:
        return
    if not Path(ALERT_CALL_WAV).exists():
        logger.warning("alert call skipped: %s missing", ALERT_CALL_WAV)
        return
    state_d["vh:last_call_alert"] = now
    try:
        from shared.fs_esl import fs_api
        cmd = ("originate {ignore_early_media=true,originate_timeout=25,"
               "lescall_probe=1,"
               f"origination_caller_id_number={ALERT_CALL_CLI}}}"
               f"sofia/gateway/{ALERT_CALL_GATEWAY}/{ALERT_CALL_NUMBER} "
               f"&playback({ALERT_CALL_WAV})")
        fs_api(cmd, timeout=45)
        logger.warning("ALERT CALL placed to %s", ALERT_CALL_NUMBER)
    except Exception as exc:
        logger.warning("alert call failed: %r", exc)


def check_voice(state, cycle: int, alert_fn) -> dict:
    """Main entry, called from sentinel.run_cycle every cycle."""
    now = time.time()
    d = state.d
    passive = _scan_passive(d, now)

    prev_state = d.get("vh:state", "healthy")
    probe_every = (PROBE_EVERY_SUSPECT if prev_state == "degraded"
                   else PROBE_EVERY_HEALTHY)
    probe = None
    if cycle % max(probe_every, 1) == 0:
        probe = probe_sarvam()
        probes = d.get("vh:probes", [])
        bad = (probe["stt"] in ("hung", "server-error", "no-credits")
               or probe["tts"] in ("hung", "server-error", "no-credits"))
        probes.append([now, bad, probe["stt_ms"], probe["tts_ms"]])
        d["vh:probes"] = probes[-10:]
        if probe["stt"] == "no-credits" or probe["tts"] == "no-credits":
            # The active key cannot synthesize or transcribe. Every call
            # placed from here on connects, bills telephony and plays
            # SILENCE. Alert on the first probe, not after a rolling window.
            alert_fn(state, "voice-probe-credits",
                     "Sarvam key out of credits — calls are going out MUTE",
                     "api.sarvam.ai returned HTTP 402 for the live "
                     "SARVAM_API_KEY. The agent cannot speak and the caller "
                     "hears dead air, while the call is still billed and is "
                     "recorded as 'empty / no caller speech'. Check "
                     "dashboard/sarvam_key_status.json — if pool_remaining "
                     "is 0 the key watchdog has nothing left to rotate to "
                     "and credits must be refilled or keys added to "
                     "sarvam_key_master.json.")
        if probe["stt"] == "auth-error" or probe["tts"] == "auth-error":
            alert_fn(state, "voice-probe-auth",
                     "Sarvam probe rejected (401/403)",
                     "The live SARVAM_API_KEY was rejected by api.sarvam.ai. "
                     "Calls are likely failing over to Whisper-only. Check "
                     "the key watchdog / key pool.")

    recent_probes = [p for p in d.get("vh:probes", []) if p[0] >= now - WINDOW_S]
    probe_bad = sum(1 for p in recent_probes[-3:] if p[1]) >= 2

    passive_bad = (passive["timeouts_10m"] >= TIMEOUT_ALERT_N
                   or passive["slow_turns_10m"] >= LAT_BAD_N)
    any_bad_signal = (passive["timeouts_10m"] > 0
                      or passive["slow_turns_10m"] > 0
                      or any(p[1] for p in recent_probes))

    if any_bad_signal:
        d["vh:last_bad"] = now

    new_state = prev_state
    if passive_bad or probe_bad:
        new_state = "degraded"
    elif prev_state == "degraded" and now - d.get("vh:last_bad", 0) >= CLEAR_AFTER_S:
        new_state = "healthy"

    if new_state == "degraded" and prev_state != "degraded":
        d["vh:degraded_since"] = now
        cause = []
        if passive["timeouts_10m"]:
            cause.append(f"{passive['timeouts_10m']} provider timeouts on live "
                         f"call turns in 10 min")
        if passive["slow_turns_10m"]:
            cause.append(f"{passive['slow_turns_10m']} turns slower than "
                         f"{LAT_BAD_MS / 1000:.0f}s (worst "
                         f"{passive['worst_turn_ms'] / 1000:.1f}s)")
        if probe_bad:
            cause.append("2 of last 3 direct API probes hung/5xx")
        alert_fn(state, "voice-degraded",
                 "Sarvam voice provider DEGRADED — calls are being rescued "
                 "by hedges",
                 "Detected: " + "; ".join(cause) + ".\n\n"
                 "The runtime is self-defending (primary-race + Whisper "
                 "hedge + TTS fast-fail), so calls continue at reduced "
                 "quality — but contact Sarvam support NOW and watch "
                 "hedge/Whisper share in ws_audio_receiver.log. Recovery "
                 "notice follows automatically.")
        _alert_call(d)
    elif new_state == "healthy" and prev_state == "degraded":
        mins = (now - d.get("vh:degraded_since", now)) / 60
        # Re-arm the 6-h alert window so the NEXT incident pages instantly.
        state.d.pop("alert:voice-degraded", None)
        alert_fn(state, "voice-recovered",
                 "Sarvam voice provider recovered",
                 f"Degradation lasted ~{mins:.0f} min; no bad signal for "
                 f"{CLEAR_AFTER_S // 60} min. Alert window re-armed.")
        state.d.pop("alert:voice-recovered", None)

    d["vh:state"] = new_state
    out = {"state": new_state, **passive}
    if probe is not None:
        out["probe"] = probe
    return out
