"""WhatsApp knobs. Every reader is a function, not a module constant.

The dashboard loads calling-agent/.env with python-dotenv AFTER some modules
are imported, and the kill switch must take effect on the next sender pass
without anyone remembering which import order froze which value. Reading the
environment at call time costs nothing and removes the question.

Platform-level (one Meta app for the whole platform; a tenant on Interakt
needs none of the Meta ones — RUNBOOK §80):
    WA_SEND_ENABLED        kill switch. Anything but true/1/yes/on = nothing
                           leaves the box; sends stay queued and say why.
    WA_CANARY_PHONES       comma-separated numbers. When set, ONLY these may
                           receive a message — every other send is blocked.
    WA_APP_SECRET          verifies X-Hub-Signature-256 on the webhook.
    WA_VERIFY_TOKEN        answers Meta's webhook subscription handshake.
    WA_APP_ID              needed only to create media-header templates
                           (Meta's resumable upload is keyed by app id).
    WA_TOKEN_KEY           Fernet key that encrypts stored access tokens.
    WA_API_VERSION         Graph API version, default v23.0.
    WA_PUBLIC_BASE_URL     where tracked links point, default agent.lescall.com.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MEDIA_DIR = REPO_ROOT / "data" / "whatsapp" / "media"

GRAPH_BASE = "https://graph.facebook.com"

# A call older than this never produces a send. The sweeper re-drives every
# registered stage over historical calls when a stage ships; without this
# gate, deploying the feature would message every person who ever said
# "yes, WhatsApp me" — months late and all at once. Same window as notify.
FRESH_WINDOW_S = 3600

# Meta's document limit is 100 MB and image 5 MB; nginx caps the request at
# 25 MB. A brochure bigger than this is not something to send to a phone.
MEDIA_MAX_BYTES = 20 * 1024 * 1024
MEDIA_TYPES = {
    "application/pdf": "document",
    "image/jpeg": "image",
    "image/png": "image",
    "video/mp4": "video",
}

SENDER_POLL_S = 20
SENDER_BATCH = 20
MAX_ATTEMPTS = 5


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def send_enabled() -> bool:
    return _env("WA_SEND_ENABLED", "false").lower() in ("1", "true", "yes", "on")


def canary_phones() -> set[str]:
    """Last-10-digit keys of the canary allowlist (empty = no canary)."""
    out = set()
    for part in _env("WA_CANARY_PHONES").split(","):
        digits = re.sub(r"\D", "", part)
        if len(digits) >= 10:
            out.add(digits[-10:])
    return out


def app_secret() -> str:
    return _env("WA_APP_SECRET")


def verify_token() -> str:
    return _env("WA_VERIFY_TOKEN")


def app_id() -> str:
    return _env("WA_APP_ID")


def token_key() -> str:
    return _env("WA_TOKEN_KEY")


def api_version() -> str:
    v = _env("WA_API_VERSION", "v23.0")
    return v if re.fullmatch(r"v\d+\.\d+", v) else "v23.0"


def public_base_url() -> str:
    return _env("WA_PUBLIC_BASE_URL", "https://agent.lescall.com").rstrip("/")


def media_public_url(token: str) -> str:
    """Where a BSP fetches an attachment from (RUNBOOK §80): one unguessable
    address per file, served by the dashboard under the open /r/ prefix."""
    return f"{public_base_url()}/r/f/{token}"


def interakt_webhook_url(token: str) -> str:
    """The per-tenant URL the tenant pastes into Interakt → Developer
    Settings → Webhooks. The token names the account; the HMAC secret the
    tenant chooses there authenticates each delivery."""
    return f"{public_base_url()}/api/whatsapp/webhook/interakt/{token}"


def platform_status() -> dict:
    """What the Setup tab shows about the platform side, without secrets."""
    return {
        "send_enabled": send_enabled(),
        "canary": bool(canary_phones()),
        "webhook_ready": bool(app_secret() and verify_token()),
        "token_key_ready": bool(token_key()),
        "webhook_url": public_base_url() + "/api/whatsapp/webhook",
    }
