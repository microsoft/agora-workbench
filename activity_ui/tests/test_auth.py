"""Tests for Activity UI app-layer auth on /events."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def _auth_disabled():
    """Disable auth for tests that don't need it."""
    with patch.dict("os.environ", {"ACTIVITY_UI_AUTH_DISABLED": "true"}):
        import activity_ui.auth as auth_mod

        auth_mod._validator = None  # Reset singleton
        yield
        auth_mod._validator = None


@pytest.fixture
def _auth_enabled():
    """Enable auth with test config."""
    env = {
        "ACTIVITY_UI_AUTH_DISABLED": "false",
        "ENTRA_TENANT_ID": "test-tenant-id",
        "ACTIVITY_UI_AUDIENCE": "api://test-audience",
        "ACTIVITY_UI_CLIENT_ID": "test-client-id",
    }
    with patch.dict("os.environ", env):
        import activity_ui.auth as auth_mod

        auth_mod._validator = None  # Reset singleton
        yield
        auth_mod._validator = None


@pytest.fixture
def client():
    """Fresh test client (re-creates app to pick up patched env)."""
    from activity_ui.server import create_app

    app = create_app()
    return TestClient(app)


class TestAuthDisabled:
    """When ACTIVITY_UI_AUTH_DISABLED=true, /events is open."""

    @pytest.mark.usefixtures("_auth_disabled")
    def test_post_events_no_token(self, client):
        resp = client.post(
            "/events",
            json={
                "type": "code_executed",
                "server": "test",
                "timestamp": time.time(),
                "event_type": "code_executed",
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    @pytest.mark.usefixtures("_auth_disabled")
    def test_events_recent_open_without_token(self, client):
        """In dev mode, /events/recent is accessible without a stream token."""
        resp = client.get("/events/recent")
        assert resp.status_code == 200


class TestAuthEnabled:
    """When auth is enabled, /events requires valid bearer token."""

    @pytest.mark.usefixtures("_auth_enabled")
    def test_post_events_no_token_returns_401(self, client):
        resp = client.post(
            "/events",
            json={
                "type": "code_executed",
                "server": "test",
                "timestamp": time.time(),
                "event_type": "code_executed",
            },
        )
        assert resp.status_code == 401

    @pytest.mark.usefixtures("_auth_enabled")
    def test_post_events_invalid_token_returns_401(self, client):
        resp = client.post(
            "/events",
            json={"type": "code_executed", "server": "test", "timestamp": time.time(), "event_type": "code_executed"},
            headers={"Authorization": "Bearer invalid.token.here"},
        )
        assert resp.status_code == 401


class TestStreamToken:
    """Stream token minting and validation for /stream and /events/recent."""

    @pytest.mark.usefixtures("_auth_enabled")
    def test_stream_no_token_returns_401(self, client):
        """Without a stream token, /stream returns 401."""
        with client.stream("GET", "/stream") as resp:
            assert resp.status_code == 401

    @pytest.mark.usefixtures("_auth_enabled")
    def test_events_recent_no_token_returns_401(self, client):
        """Without a stream token, /events/recent returns 401."""
        resp = client.get("/events/recent")
        assert resp.status_code == 401

    @pytest.mark.usefixtures("_auth_enabled")
    def test_stream_token_mint_and_use(self, client):
        """POST /stream-token sets cookie; /events/recent accepts it."""
        # Mint a stream token (simulating EasyAuth-protected path)
        mint_resp = client.post(
            "/stream-token",
            headers={"X-MS-CLIENT-PRINCIPAL-ID": "test-user-oid"},
        )
        assert mint_resp.status_code == 200
        assert mint_resp.headers.get("cache-control") == "no-store"

        # The cookie should be set
        assert "activity_stream_token" in mint_resp.cookies

        # Use the cookie to access /events/recent
        token = mint_resp.cookies["activity_stream_token"]
        resp = client.get("/events/recent", cookies={"activity_stream_token": token})
        assert resp.status_code == 200

    @pytest.mark.usefixtures("_auth_enabled")
    def test_stream_token_query_param_fallback(self, client):
        """Stream token can be passed as query param for non-browser clients."""
        import activity_ui.auth as auth_mod

        token = auth_mod.mint_stream_token("test-user")
        resp = client.get(f"/events/recent?token={token}")
        assert resp.status_code == 200

    @pytest.mark.usefixtures("_auth_enabled")
    def test_stream_token_expired(self, client):
        """Expired stream token returns 401."""
        import activity_ui.auth as auth_mod
        import jwt as pyjwt

        # Mint a token that's already expired
        payload = {
            "sub": "test-user",
            "purpose": "stream",
            "iat": int(time.time()) - 600,
            "exp": int(time.time()) - 1,
        }
        expired_token = pyjwt.encode(payload, auth_mod._STREAM_TOKEN_SECRET, algorithm="HS256")
        resp = client.get(f"/events/recent?token={expired_token}")
        assert resp.status_code == 401

    @pytest.mark.usefixtures("_auth_enabled")
    def test_stream_token_wrong_purpose(self, client):
        """Token with wrong purpose claim is rejected."""
        import activity_ui.auth as auth_mod
        import jwt as pyjwt

        payload = {
            "sub": "test-user",
            "purpose": "other",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        }
        bad_token = pyjwt.encode(payload, auth_mod._STREAM_TOKEN_SECRET, algorithm="HS256")
        resp = client.get(f"/events/recent?token={bad_token}")
        assert resp.status_code == 403

    @pytest.mark.usefixtures("_auth_enabled")
    def test_stream_token_bad_signature(self, client):
        """Token signed with wrong secret is rejected."""
        import jwt as pyjwt

        payload = {
            "sub": "test-user",
            "purpose": "stream",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        }
        bad_token = pyjwt.encode(payload, "wrong-secret", algorithm="HS256")
        resp = client.get(f"/events/recent?token={bad_token}")
        assert resp.status_code == 401


class TestHealthEndpoints:
    """Health endpoints are always open."""

    @pytest.mark.usefixtures("_auth_enabled")
    def test_health(self, client):
        assert client.get("/health").status_code == 200

    @pytest.mark.usefixtures("_auth_enabled")
    def test_healthz(self, client):
        assert client.get("/healthz").status_code == 200
