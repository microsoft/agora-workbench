"""Tests for Activity UI app-layer auth on /events."""

from __future__ import annotations

import time
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient


@pytest.fixture
def _auth_disabled():
    """Disable auth for tests that don't need it."""
    with patch.dict("os.environ", {"ACTIVITY_UI_AUTH_DISABLED": "true"}):
        # Re-import to pick up env change
        import importlib
        import activity_ui.auth as auth_mod

        importlib.reload(auth_mod)
        yield
        importlib.reload(auth_mod)


@pytest.fixture
def _auth_enabled(tmp_path):
    """Enable auth with a test RSA key."""
    env = {
        "ACTIVITY_UI_AUTH_DISABLED": "false",
        "ENTRA_TENANT_ID": "test-tenant-id",
        "ACTIVITY_UI_AUDIENCE": "api://test-audience",
        "ACTIVITY_UI_CLIENT_ID": "test-client-id",
    }
    with patch.dict("os.environ", env):
        import importlib
        import activity_ui.auth as auth_mod

        importlib.reload(auth_mod)
        yield
        importlib.reload(auth_mod)


@pytest.fixture
def rsa_keypair():
    """Generate a test RSA key pair."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return private_key, public_key


@pytest.fixture
def client():
    """Fresh test client (re-creates app to pick up patched env)."""
    from activity_ui.server import create_app

    app = create_app()
    return TestClient(app)


def _make_token(private_key, claims: dict) -> str:
    """Create a signed JWT."""
    return jwt.encode(claims, private_key, algorithm="RS256")


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


class TestHealthEndpoints:
    """Health endpoints are always open."""

    @pytest.mark.usefixtures("_auth_enabled")
    def test_health(self, client):
        assert client.get("/health").status_code == 200

    @pytest.mark.usefixtures("_auth_enabled")
    def test_healthz(self, client):
        assert client.get("/healthz").status_code == 200
