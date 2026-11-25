"""The deny-by-default tenancy gate vs. the voice-preview endpoints.

Regression source (2026-08-01): /api/voices was never added to
_TENANT_API_PREFIXES when voice previews shipped (07-24), so every logged-in
user's "tap to hear" chip in the copilot got a silent 403 — the gate rejects
before the request-audit middleware runs, so nothing was even logged app-side.
These tests pin that a valid session can fetch voice samples while the
deny-by-default rule still blocks unlisted legacy /api paths.
"""
from __future__ import annotations

import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import REPO_ROOT


@pytest.fixture()
def gated_client(monkeypatch):
    dash = str(REPO_ROOT / "dashboard")
    if dash not in sys.path:
        sys.path.insert(0, dash)
    import auth

    app = FastAPI()

    @app.get("/api/voices/sample/{speaker}")
    async def sample(speaker: str):
        return {"ok": speaker}

    @app.get("/api/voices")
    async def voices():
        return {"voices": []}

    @app.get("/api/sarvam/keys")
    async def legacy():  # single-tenant legacy surface — must stay blocked
        return {"secret": True}

    auth.install_login_gate(app)
    monkeypatch.setattr(auth, "oauth_enabled", lambda: True)
    monkeypatch.setattr(
        auth, "read_session_cookie",
        lambda c: {"email": "t@example.com"} if c == "good" else None)
    return TestClient(app), auth


def test_logged_in_user_can_fetch_voice_sample(gated_client):
    client, auth = gated_client
    r = client.get("/api/voices/sample/shubh",
                   cookies={auth.SESSION_COOKIE: "good"})
    assert r.status_code == 200
    r = client.get("/api/voices", cookies={auth.SESSION_COOKIE: "good"})
    assert r.status_code == 200


def test_anonymous_still_gets_401(gated_client):
    client, auth = gated_client
    r = client.get("/api/voices/sample/shubh")
    assert r.status_code == 401


def test_unlisted_legacy_api_still_403s_for_sessions(gated_client):
    client, auth = gated_client
    r = client.get("/api/sarvam/keys", cookies={auth.SESSION_COOKIE: "good"})
    assert r.status_code == 403
