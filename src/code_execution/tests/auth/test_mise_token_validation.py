"""Tests for MISE native library integration in token validation."""

import json
from typing import Optional

import pytest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException


@pytest.fixture
def mock_entra_env():
    """Mock ENTRA environment variables required for CodeExecutionServer."""
    with patch.dict("os.environ", {"ENTRA_CLIENT_ID": "test-client-id", "ENTRA_TENANT_ID": "test-tenant-id"}):
        yield


def _create_server(
    entra_client_id: Optional[str] = "test-client-id", entra_tenant_id: Optional[str] = "test-tenant-id"
):
    """Helper to create a CodeExecutionServer for testing.

    Relies on conftest.py having already mocked the Mise module.
    """
    from ...code_execution import CodeExecutionServer
    from ...code_execution.code_execution_models import EnvironmentConfig

    config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
    return CodeExecutionServer(
        environment_config=config,
        entra_client_id=entra_client_id,
        entra_tenant_id=entra_tenant_id,
    )


def _make_mock_claim(name: str, value: str) -> MagicMock:
    """Create a mock MiseClaim with name and value attributes."""
    claim = MagicMock()
    claim.name = name
    claim.value = value
    return claim


class TestVerifyEntraTokenWithMise:
    """Test verify_entra_token uses MISE native library for token validation."""

    @pytest.mark.asyncio
    async def test_mise_validates_token(self, mock_entra_env):
        """verify_entra_token should use the MISE client to validate tokens."""
        server = _create_server()

        mock_result = MagicMock()
        mock_result.http_response_status_code = 200
        mock_result.subject_claims = [
            _make_mock_claim("aud", "test-client-id"),
            _make_mock_claim("tid", "test-tenant-id"),
            _make_mock_claim("oid", "user-123"),
            _make_mock_claim("name", "Test User"),
            _make_mock_claim("appid", "app-456"),
        ]

        mock_client = MagicMock()
        mock_client.validate.return_value = mock_result

        mock_validation_input = MagicMock()

        with (
            patch("code_execution.code_execution.server.Mise", MagicMock()),
            patch("code_execution.code_execution.server.MiseValidationInput", return_value=mock_validation_input),
        ):
            server._mise_client = mock_client
            result = await server.verify_entra_token("test-token")

        mock_client.validate.assert_called_once_with(mock_validation_input)
        assert mock_validation_input.authorization_header == "Bearer test-token"
        assert result["oid"] == "user-123"
        assert result["name"] == "Test User"

    @pytest.mark.asyncio
    async def test_mise_validation_failure_raises_http_exception(self, mock_entra_env):
        """When MISE returns a non-200 status, it should raise HTTPException."""
        server = _create_server()

        mock_result = MagicMock()
        mock_result.http_response_status_code = 401
        mock_result.error_description = "Token is expired"

        mock_client = MagicMock()
        mock_client.validate.return_value = mock_result

        with (
            patch("code_execution.code_execution.server.Mise", MagicMock()),
            patch("code_execution.code_execution.server.MiseValidationInput", return_value=MagicMock()),
        ):
            server._mise_client = mock_client
            with pytest.raises(HTTPException) as exc_info:
                await server.verify_entra_token("bad-token")

        assert exc_info.value.status_code == 401
        assert "Token is expired" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_mise_exception_raises_http_exception(self, mock_entra_env):
        """When MISE raises an exception, it should be wrapped in HTTPException."""
        server = _create_server()

        mock_client = MagicMock()
        mock_client.validate.side_effect = Exception("FFI error")

        with (
            patch("code_execution.code_execution.server.Mise", MagicMock()),
            patch("code_execution.code_execution.server.MiseValidationInput", return_value=MagicMock()),
        ):
            server._mise_client = mock_client
            with pytest.raises(HTTPException) as exc_info:
                await server.verify_entra_token("bad-token")

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_entra_config_raises_on_construction(self):
        """When entra credentials are not set, server construction should raise ValueError."""
        with patch.dict("os.environ", {"ENTRA_CLIENT_ID": "", "ENTRA_TENANT_ID": ""}, clear=False):
            with pytest.raises(ValueError, match="Missing required Entra ID configuration"):
                _create_server(entra_client_id=None, entra_tenant_id=None)


class TestGetMiseClient:
    """Test the _get_mise_client method."""

    def test_get_mise_client_creates_on_first_call(self, mock_entra_env):
        """_get_mise_client should create client on first call and cache it."""
        mock_mise_instance = MagicMock()
        mock_mise_instance.configure.return_value = MagicMock(error_description=None)

        with patch("code_execution.code_execution.server.Mise", return_value=mock_mise_instance):
            server = _create_server()

            client = server._get_mise_client()
            assert client is mock_mise_instance

            # Second call should return cached instance
            client2 = server._get_mise_client()
            assert client2 is client

    def test_get_mise_client_config_includes_api_prefix_audience(self, mock_entra_env):
        """MISE config should include both client_id and api://{client_id} as valid audiences."""
        mock_mise_instance = MagicMock()
        mock_mise_instance.configure.return_value = MagicMock(error_description=None)

        with patch("code_execution.code_execution.server.Mise", return_value=mock_mise_instance):
            server = _create_server()
            server._get_mise_client()

            config_json = mock_mise_instance.configure.call_args[0][0]
            config = json.loads(config_json)
            assert "test-client-id" in config["AzureAd"]["ValidAudiences"]
            assert "api://test-client-id" in config["AzureAd"]["ValidAudiences"]

    def test_get_mise_client_raises_without_credentials(self, mock_entra_env):
        """Server construction should raise ValueError without required credentials."""
        with patch.dict("os.environ", {"ENTRA_CLIENT_ID": "", "ENTRA_TENANT_ID": ""}, clear=False):
            with pytest.raises(ValueError, match="Missing required Entra ID configuration"):
                _create_server(entra_client_id=None, entra_tenant_id=None)

    def test_missing_entra_config_raises_value_error(self, mock_entra_env):
        """Server construction should raise ValueError when only one credential is missing."""
        with patch.dict("os.environ", {"ENTRA_CLIENT_ID": "some-id", "ENTRA_TENANT_ID": ""}, clear=False):
            with pytest.raises(ValueError, match="ENTRA_TENANT_ID"):
                _create_server(entra_client_id="some-id", entra_tenant_id=None)

    def test_construction_raises_when_mise_not_installed(self, mock_entra_env):
        """Server construction should raise ImportError when mise package is not installed."""
        from ...code_execution import CodeExecutionServer
        from ...code_execution.code_execution_models import EnvironmentConfig

        config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
        with patch("code_execution.code_execution.server.Mise", None):
            with pytest.raises(ImportError, match="mise"):
                CodeExecutionServer(
                    environment_config=config,
                    entra_client_id="test-client-id",
                    entra_tenant_id="test-tenant-id",
                )
