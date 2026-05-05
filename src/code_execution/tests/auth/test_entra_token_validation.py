"""Tests for PyJWT-based token validation in EntraTokenValidator."""

import jwt
import pytest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException


@pytest.fixture
def mock_entra_env():
    """Mock ENTRA environment variables required for CodeExecutionServer."""
    with patch.dict("os.environ", {"ENTRA_CLIENT_ID": "test-client-id", "ENTRA_TENANT_ID": "test-tenant-id"}):
        yield


def _create_server():
    """Helper to create a CodeExecutionServer for testing with Entra auth."""
    from ...code_execution import CodeExecutionServer
    from ...code_execution.code_execution_models import EnvironmentConfig
    from ...code_execution.auth import create_entra_auth_config

    config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
    auth_config = create_entra_auth_config(client_id="test-client-id", tenant_id="test-tenant-id")
    return CodeExecutionServer(
        environment_config=config,
        auth_config=auth_config,
    )


class TestValidateTokenWithPyJWT:
    """Test validate_token uses PyJWT for token validation."""

    @pytest.mark.asyncio
    async def test_validates_token_successfully(self, mock_entra_env):
        """validate_token should validate tokens and return claims."""
        server = _create_server()

        expected_claims = {
            "aud": "test-client-id",
            "tid": "test-tenant-id",
            "oid": "user-123",
            "name": "Test User",
            "iss": "https://login.microsoftonline.com/test-tenant-id/v2.0",
            "exp": 9999999999,
        }

        mock_signing_key = MagicMock()
        mock_signing_key.key = "mock-key"

        with (
            patch.object(
                server.auth_config.token_validator._jwks_client,
                "get_signing_key_from_jwt",
                return_value=mock_signing_key,
            ),
            patch("jwt.decode", return_value=expected_claims),
        ):
            result = await server.validate_token("test-token")

        assert result["oid"] == "user-123"
        assert result["name"] == "Test User"
        assert result["tid"] == "test-tenant-id"

    @pytest.mark.asyncio
    async def test_expired_token_raises_http_exception(self, mock_entra_env):
        """When token is expired, should raise HTTPException with 401."""
        server = _create_server()

        mock_signing_key = MagicMock()
        mock_signing_key.key = "mock-key"

        with (
            patch.object(
                server.auth_config.token_validator._jwks_client,
                "get_signing_key_from_jwt",
                return_value=mock_signing_key,
            ),
            patch("jwt.decode", side_effect=jwt.ExpiredSignatureError("Token is expired")),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await server.validate_token("expired-token")

        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_invalid_audience_raises_http_exception(self, mock_entra_env):
        """When token has wrong audience, should raise HTTPException with 401."""
        server = _create_server()

        mock_signing_key = MagicMock()
        mock_signing_key.key = "mock-key"

        with (
            patch.object(
                server.auth_config.token_validator._jwks_client,
                "get_signing_key_from_jwt",
                return_value=mock_signing_key,
            ),
            patch("jwt.decode", side_effect=jwt.InvalidAudienceError("Invalid audience")),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await server.validate_token("bad-aud-token")

        assert exc_info.value.status_code == 401
        assert "audience" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_invalid_issuer_raises_http_exception(self, mock_entra_env):
        """When token has wrong issuer, should raise HTTPException with 401."""
        server = _create_server()

        mock_signing_key = MagicMock()
        mock_signing_key.key = "mock-key"

        with (
            patch.object(
                server.auth_config.token_validator._jwks_client,
                "get_signing_key_from_jwt",
                return_value=mock_signing_key,
            ),
            patch("jwt.decode", side_effect=jwt.InvalidIssuerError("Invalid issuer")),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await server.validate_token("bad-iss-token")

        assert exc_info.value.status_code == 401
        assert "issuer" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_jwks_fetch_failure_raises_http_exception(self, mock_entra_env):
        """When JWKS key fetch fails, should raise HTTPException with 401."""
        server = _create_server()

        with patch.object(
            server.auth_config.token_validator._jwks_client,
            "get_signing_key_from_jwt",
            side_effect=jwt.PyJWKClientError("Connection refused"),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await server.validate_token("some-token")

        assert exc_info.value.status_code == 401
        assert "signing keys" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_generic_invalid_token_raises_http_exception(self, mock_entra_env):
        """When token is invalid for any other reason, should raise HTTPException."""
        server = _create_server()

        mock_signing_key = MagicMock()
        mock_signing_key.key = "mock-key"

        with (
            patch.object(
                server.auth_config.token_validator._jwks_client,
                "get_signing_key_from_jwt",
                return_value=mock_signing_key,
            ),
            patch("jwt.decode", side_effect=jwt.InvalidTokenError("Malformed token")),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await server.validate_token("malformed-token")

        assert exc_info.value.status_code == 401


class TestEntraTokenValidatorConfig:
    """Test EntraTokenValidator configuration."""

    def test_validator_accepts_both_audience_formats(self, mock_entra_env):
        """Validator should accept both client_id and api://client_id as audience."""
        server = _create_server()
        validator = server.auth_config.token_validator
        assert "test-client-id" in validator._valid_audiences
        assert "api://test-client-id" in validator._valid_audiences

    def test_validator_accepts_both_issuer_formats(self, mock_entra_env):
        """Validator should accept both v2.0 and v1.0 issuer formats."""
        server = _create_server()
        validator = server.auth_config.token_validator
        assert "https://login.microsoftonline.com/test-tenant-id/v2.0" in validator._valid_issuers
        assert "https://sts.windows.net/test-tenant-id/" in validator._valid_issuers

    def test_missing_entra_config_raises_value_error(self):
        """create_entra_auth_config should raise ValueError when config is missing."""
        from ...code_execution.auth import create_entra_auth_config

        with patch.dict("os.environ", {"ENTRA_CLIENT_ID": "", "ENTRA_TENANT_ID": ""}, clear=False):
            with pytest.raises(ValueError, match="Missing required Entra ID configuration"):
                create_entra_auth_config(client_id=None, tenant_id=None)
