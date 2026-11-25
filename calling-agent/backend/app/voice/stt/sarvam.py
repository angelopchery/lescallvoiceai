"""
Sarvam AI Speech-to-Text provider.

Uses the Sarvam REST API with multipart WAV upload.
Requires SARVAM_API_KEY environment variable.
"""

from __future__ import annotations

import asyncio
import os
import logging

import httpx

from .base import STTProvider, STTResult, pcm_to_wav, normalize_pcm_audio

logger = logging.getLogger(__name__)

SARVAM_API_URL = "https://api.sarvam.ai/speech-to-text"
SARVAM_TIMEOUT = 10.0
# Per-phase caps inside the blanket timeout (2026-08-03). A healthy Sarvam
# turn is ~200-300ms median / <1s p95, but during the 08-03 morning incident
# individual requests hung to the full 10s blanket (turns of 10.3-21.8s,
# STT queue overflow dropped live caller speech). Tight connect/read caps
# make a hung request fail fast enough that the router's retry — on a fresh
# connection — still lands inside a salvageable turn.
# Read cap tightened 5.0 → 2.5 later on 08-03: the hangs kept recurring all
# morning and the observed healthy distribution (150-450ms, one 2.4s
# outlier) leaves 2.5s ample; the rare slow-success now goes to Whisper
# instead of stalling the serial STT worker for 5s.
SARVAM_CONNECT_TIMEOUT_S = float(os.environ.get("SARVAM_CONNECT_TIMEOUT_S", "3.0"))
SARVAM_READ_TIMEOUT_S = float(os.environ.get("SARVAM_READ_TIMEOUT_S", "2.5"))

# Production model is saarika:v2.5 — owner decision 2026-08-03 after a brief
# saaras:v3 experiment (switched 08-01, reverted twice at owner request).
# The 08-03 "saarika got slow" episode was NOT the model config: requests
# were byte-identical to the pre-experiment shape; the cause was Sarvam-side
# request hangs amplified by the old blanket 10s timeout (see the timeout
# caps below). Keep for reference: saaras:v3 mode=codemix keeps English in
# Latin script under a hi-IN pin ("2 BHK flat", not "दो बीएच के फ्लैट") at
# latency parity — re-test via SARVAM_STT_MODEL/SARVAM_STT_MODE env only,
# and only with the owner's sign-off.
# "mode" is a saaras-only parameter — never sent for saarika models.
SARVAM_STT_MODEL = os.environ.get("SARVAM_STT_MODEL", "saarika:v2.5").strip()
SARVAM_STT_MODE = os.environ.get("SARVAM_STT_MODE", "codemix").strip()

# Shared per-process HTTP client: STT instances are created per call, but the
# warmup flag is per-process — without sharing, every call after the first
# pays a cold TLS handshake on its first segment. SARVAM_HTTP_SHARED=false
# restores the old per-instance clients.
SARVAM_HTTP_SHARED = os.environ.get("SARVAM_HTTP_SHARED", "true").lower() == "true"
# With a shared pool the connections see cross-call traffic, so a longer
# keepalive is safe; the stale-connection retry paths remain the backstop.
SARVAM_KEEPALIVE_EXPIRY_S = float(os.environ.get("SARVAM_KEEPALIVE_EXPIRY_S", "300"))

_shared_client: httpx.AsyncClient | None = None


def _make_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            timeout,
            connect=SARVAM_CONNECT_TIMEOUT_S,
            read=SARVAM_READ_TIMEOUT_S,
        ),
        limits=httpx.Limits(
            max_keepalive_connections=5,
            keepalive_expiry=SARVAM_KEEPALIVE_EXPIRY_S if SARVAM_HTTP_SHARED else 60,
        ),
    )


class SarvamRateLimitError(Exception):
    """Raised when Sarvam API returns HTTP 429 (Too Many Requests).

    The caller (STTRouter) uses this to apply backoff and optionally
    route to the fallback provider rather than retrying immediately.
    """
    def __init__(self, retry_after: str | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(f"Sarvam rate limited (Retry-After: {retry_after})")


class SarvamSTT(STTProvider):
    """
    Sarvam AI STT provider.

    Converts PCM audio to WAV, uploads via multipart POST, and extracts
    the transcript from the JSON response.

    Uses a persistent httpx client for connection reuse.
    """

    # Class-level flag: warmup runs at most once per process regardless of
    # how many calls / pipeline instances are created (Issue 6).
    _warmup_done_flag: bool = False

    def __init__(
        self,
        api_key: str | None = None,
        api_url: str = SARVAM_API_URL,
        timeout: float = SARVAM_TIMEOUT,
        model: str | None = None,
        mode: str | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("SARVAM_API_KEY", "")
        self._api_url = api_url
        self._timeout = timeout
        self._model = model or SARVAM_STT_MODEL
        self._mode = mode if mode is not None else SARVAM_STT_MODE
        # Per-call dynamic normalizer — adapts gain + noise-gating to THIS
        # caller/line across the call. Falls back to raw audio on any error.
        from .audio_norm import DynamicNormalizer
        self._normalizer = DynamicNormalizer()
        self._client: httpx.AsyncClient | None = None

        if not self._api_key:
            logger.warning("[STT:Sarvam] SARVAM_API_KEY not set — calls will fail")
        else:
            masked = self._api_key[:6] + "…" + self._api_key[-4:]
            logger.info("[STT:Sarvam] API key loaded: %s", masked)

    def _get_client(self) -> httpx.AsyncClient:
        if SARVAM_HTTP_SHARED:
            global _shared_client
            if _shared_client is None or _shared_client.is_closed:
                _shared_client = _make_client(self._timeout)
            return _shared_client
        if self._client is None:
            # keepalive_expiry=60: idle connections older than 60s are never
            # reused, so a call after a long pause always gets a fresh TLS
            # handshake instead of dying on a silently-dropped server socket.
            self._client = _make_client(self._timeout)
        return self._client

    def _reset_client(self) -> None:
        """Drop the active client so the next request opens a fresh one."""
        global _shared_client
        if SARVAM_HTTP_SHARED:
            _shared_client = None
        else:
            self._client = None

    async def transcribe(
        self,
        audio_bytes: bytes,
        sample_rate: int = 16_000,
        language: str = "en",
    ) -> STTResult:
        # Normalize amplitude: telephone audio is often quiet; boosting it
        # before upload significantly improves STT accuracy on quiet callers.
        # Dynamic per-call normalization (AGC + noise-gate + soft-limit). Falls
        # back to the static booster if the dynamic layer is unavailable.
        if getattr(self, "_normalizer", None) is not None:
            audio_bytes = self._normalizer.process(audio_bytes, sample_rate)
        else:
            audio_bytes = normalize_pcm_audio(audio_bytes)
        wav_data = pcm_to_wav(audio_bytes, sample_rate=sample_rate)
        duration_ms = len(audio_bytes) * 1000 // (sample_rate * 2)
        logger.info(
            "[STT:Sarvam] Sending %d bytes (%.0fms) lang=%s",
            len(wav_data), duration_ms, language,
        )

        # Language mapping:
        # - "unknown" → pass through, Sarvam auto-detects
        # - "en" → "en-IN", "hi" → "hi-IN"
        # - already-suffixed codes pass through
        if language == "unknown":
            lang_code = "unknown"
        elif "-" in language:
            lang_code = language
        else:
            lang_code = f"{language}-IN"

        # Per-call leased key when the bridge attached a provider (concurrent
        # calls stop sharing one key's rate limits); else the live process-
        # wide key, re-read so any rotation applies without a restart.
        _provider = getattr(self, "key_provider", None)
        if _provider is not None:
            self._api_key = _provider()
        else:
            from ..key_rotator import current_key as _current_key
            self._api_key = _current_key()

        client = self._get_client()

        form: dict[str, str] = {
            "language_code": lang_code,
            "model": self._model,
        }
        if self._mode and self._model.startswith("saaras"):
            form["mode"] = self._mode

        try:
            response = await client.post(
                self._api_url,
                headers={"api-subscription-key": self._api_key},
                data=form,
                files={"file": ("audio.wav", wav_data, "audio/wav")},
            )
        except httpx.TimeoutException as exc:
            # A hang usually means a silently-dead pooled connection or a
            # wedged server-side worker — drop the client so the router's
            # retry opens a fresh connection instead of reusing the socket.
            logger.warning(
                "[STT:Sarvam] Request timed out: %s — resetting client for retry", exc
            )
            await self._retire_client()
            raise
        except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError) as exc:
            # Stale or closed connection — discard the client so the next
            # attempt (STT router retry or next turn) opens a fresh one.
            logger.warning(
                "[STT:Sarvam] Stale connection error: %s — resetting client for retry", exc
            )
            await self._retire_client()
            raise

        if response.status_code == 402:
            logger.error(
                "[SARVAM:CREDITS] HTTP 402 Payment Required — Sarvam STT credits exhausted. "
                "Attempting auto-rotation from key pool."
            )
            from ..key_rotator import retire as _retire_key
            # Rotate first (a burst of 402s on ONE dead key costs the pool
            # ONE key — key_rotator.rotate), then stop leasing the dead key.
            # Marked BEFORE the rotation it still counted as active, was not
            # marked, and the retry was handed it again (key_rotator.retire).
            new_key = _retire_key(self._api_key)
            if new_key:
                self._api_key = new_key
                self._reset_client()  # force fresh TLS with new key
                logger.info("[STT:Sarvam] Key rotated — next STT call will use new key")
            raise RuntimeError("[SARVAM:CREDITS] Credits exhausted — refill your Sarvam account.")

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            logger.warning(
                "[STT:Sarvam] Rate limited (HTTP 429) Retry-After=%s", retry_after
            )
            raise SarvamRateLimitError(retry_after=retry_after)

        if response.status_code != 200:
            logger.error(
                "[STT:Sarvam] HTTP %d — body: %s",
                response.status_code,
                response.text[:500],
            )
            response.raise_for_status()

        data = response.json()
        transcript = data.get("transcript", "").strip()
        # The REST response carries no transcript-confidence field — report
        # None rather than a fabricated 1.0 (which kept the router's
        # MIN_CONFIDENCE gate from ever firing). If Sarvam ever ships one,
        # it flows through automatically. (saaras:v3 DOES return
        # language_probability, but only on auto-detect — we pin the
        # language, so detection is skipped and it comes back null.)
        _conf_raw = data.get("confidence")
        confidence = float(_conf_raw) if _conf_raw is not None else None
        detected_lang = data.get("language_code") or None
        _lang_prob = data.get("language_probability")
        logger.info("[STT:Sarvam] Transcript: %r (confidence=%s, lang=%s%s)",
                    transcript,
                    "n/a" if confidence is None else f"{confidence:.2f}",
                    detected_lang,
                    "" if _lang_prob is None else f" p={_lang_prob:.2f}")
        return STTResult(text=transcript, confidence=confidence, detected_language=detected_lang)

    async def warmup(self) -> None:
        """Pre-open a TLS connection to the Sarvam host so the first real
        transcribe() call doesn't pay DNS+TCP+TLS cost (~300ms cold start).

        We fire a cheap HEAD request — Sarvam returns 404/405 for it, but the
        connection lands in httpx's per-host pool and the next POST reuses it.
        Errors are silently ignored.

        Runs at most once per process (class-level flag) — Issue 6.
        """
        if SarvamSTT._warmup_done_flag:
            logger.debug("[STT:Sarvam] Warmup skipped — already done this process")
            return
        try:
            import asyncio
            client = self._get_client()
            await asyncio.wait_for(client.head(self._api_url), timeout=3.0)
            SarvamSTT._warmup_done_flag = True
            logger.info("[STT:Sarvam] Connection warmed (first time this process)")
        except Exception as exc:
            logger.debug("[STT:Sarvam] Warmup skipped: %s", exc)

    async def _retire_client(self) -> None:
        """Detach the current client and close it in the BACKGROUND.

        A per-request error path must never `aclose()` the process-global
        client synchronously: with SARVAM_HTTP_SHARED=true that tears down
        connections other in-flight requests are still using. It fired 85
        times during the 2026-08-03 Sarvam degradation and caused no visible
        cascade only because calls were running strictly one at a time —
        i.e. it is a latent concurrency bug, not a safe one. Detaching is
        immediate (the next `_get_client` builds a fresh one, which is the
        whole point of the reset) while the old client drains on its own.
        """
        global _shared_client
        if SARVAM_HTTP_SHARED:
            client, _shared_client = _shared_client, None
        else:
            client, self._client = self._client, None
        if client is None:
            return

        async def _drain() -> None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 — teardown of a dead client
                pass

        try:
            asyncio.get_running_loop().create_task(_drain())
        except RuntimeError:
            await _drain()

    async def close(self) -> None:
        global _shared_client
        if SARVAM_HTTP_SHARED:
            client, _shared_client = _shared_client, None
        else:
            client, self._client = self._client, None
        if client:
            await client.aclose()
