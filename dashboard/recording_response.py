"""Serving a call's WAV — one implementation, three callers.

The dashboard (session-authenticated), the tokenized public download link, and
the public API (`/v1/calls/{id}/recording`) all have to answer the same
question: where does this call's audio actually live? Local disk until the
retention cleanup runs, GCS afterwards, and `call_audio.wav` (both parties) in
preference to the legacy caller-only `caller_audio.wav`.

Extracted from server.py so the API cannot drift from what the dashboard plays;
a customer's CRM importing a different file than the one an operator listened
to is exactly the kind of divergence nobody notices until it matters.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

logger = logging.getLogger("dashboard.recording")

_REPO_ROOT = Path(__file__).resolve().parents[1]
RECORDINGS_DIR = _REPO_ROOT / "recordings"

# Full two-party recording first, legacy caller-only second.
_FILENAMES = ("call_audio.wav", "caller_audio.wav")


# A call_key is "<digits>_<YYYYMMDD>_<HHMMSS>". It reaches this function from
# a database row, never straight from a URL — but it lands in a filesystem path
# and in a GCS object name, so it is validated here anyway. Defence in depth is
# cheap; the day someone adds a caller that passes user input is the day this
# matters, and that caller will not think to check.
_SAFE_CALL_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def stream_recording(call_key: str, gcs_object: str | None, download: bool):
    if not _SAFE_CALL_KEY.match(call_key or "") or ".." in call_key:
        logger.error("refusing unsafe call_key %r", call_key[:80])
        return JSONResponse({"error": "no audio for this recording"}, status_code=404)
    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{call_key}.wav"'
    for filename in _FILENAMES:
        audio_path = RECORDINGS_DIR / call_key / filename
        if audio_path.exists():
            return FileResponse(str(audio_path), media_type="audio/wav", headers=headers)
    if gcs_object:
        # Prefer the full-recording object; fall back to the stamped legacy
        # object (old calls have no call_audio.wav in the bucket).
        full_object = gcs_object.rsplit("/", 1)[0] + "/call_audio.wav"
        for obj in (full_object, gcs_object):
            try:
                from shared.recording_storage import open_gcs_stream
                stream = open_gcs_stream(obj)
                stream.read(1)  # force existence check before we commit
                stream.seek(0)

                def _iter(s=stream):
                    try:
                        while chunk := s.read(256 * 1024):
                            yield chunk
                    finally:
                        s.close()

                return StreamingResponse(_iter(), media_type="audio/wav", headers=headers)
            except Exception as exc:
                logger.debug("GCS stream miss for %s (%s): %r", call_key, obj, exc)
        logger.error("GCS stream failed for %s (%s)", call_key, gcs_object)
    return JSONResponse({"error": "no audio for this recording"}, status_code=404)
