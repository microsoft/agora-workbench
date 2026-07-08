"""Regression test: FastMCP >=3.4.3 DNS-rebinding protection bypass.

FastMCP auto-enables DNS-rebinding protection when settings.host is a
localhost variant, returning HTTP 421 for non-localhost Host headers.
BaseMCPServer.run_http() must prevent this for non-localhost bind addresses
so that service-to-service catalog fetches work over internal hostnames.
"""

from unittest.mock import AsyncMock, patch

import pytest

import fastmcp as _fastmcp

from agora_workbench.code_execution import CodeExecutionServer, ServerConfig
from agora_workbench.code_execution.auth import create_noop_auth_config


def _make_server() -> CodeExecutionServer:
    config = ServerConfig(
        name="test_dns",
        description="Test server",
        type="uv",
        dependency_file="# empty",
    )
    return CodeExecutionServer(
        server_config=config,
        auth_config=create_noop_auth_config(),
    )


class TestDnsRebindingProtectionBypass:
    """Ensure run_http adjusts fastmcp.settings.host to avoid 421s."""

    @pytest.mark.unit
    async def test_non_localhost_bind_overrides_settings_host(self):
        """When binding to 0.0.0.0, settings.host should be updated."""
        server = _make_server()
        server._startup = AsyncMock()

        original_host = _fastmcp.settings.host
        try:
            # Simulate default fastmcp setting (localhost triggers protection)
            _fastmcp.settings.host = "127.0.0.1"

            # Patch http_app and uvicorn to avoid actually starting a server
            with (
                patch.object(server.mcp, "http_app") as mock_http_app,
                patch("uvicorn.Server.serve", new_callable=AsyncMock),
            ):
                mock_app = AsyncMock()
                mock_app.add_middleware = lambda *a, **kw: None
                mock_app.routes = []
                mock_http_app.return_value = mock_app

                await server.run_http(host="0.0.0.0", port=9999)

            # After run_http, settings.host should no longer be localhost
            assert _fastmcp.settings.host == "0.0.0.0"
        finally:
            _fastmcp.settings.host = original_host

    @pytest.mark.unit
    async def test_localhost_bind_preserves_settings_host(self):
        """When binding to localhost, settings.host should NOT be changed."""
        server = _make_server()
        server._startup = AsyncMock()

        original_host = _fastmcp.settings.host
        try:
            _fastmcp.settings.host = "127.0.0.1"

            with (
                patch.object(server.mcp, "http_app") as mock_http_app,
                patch("uvicorn.Server.serve", new_callable=AsyncMock),
            ):
                mock_app = AsyncMock()
                mock_app.add_middleware = lambda *a, **kw: None
                mock_app.routes = []
                mock_http_app.return_value = mock_app

                await server.run_http(host="127.0.0.1", port=9999)

            # Should remain unchanged — localhost bind keeps protection active
            assert _fastmcp.settings.host == "127.0.0.1"
        finally:
            _fastmcp.settings.host = original_host

    @pytest.mark.unit
    async def test_explicit_non_localhost_setting_not_overridden(self):
        """If operator already set a non-localhost host, don't touch it."""
        server = _make_server()
        server._startup = AsyncMock()

        original_host = _fastmcp.settings.host
        try:
            # Operator explicitly configured a public host
            _fastmcp.settings.host = "10.0.0.5"

            with (
                patch.object(server.mcp, "http_app") as mock_http_app,
                patch("uvicorn.Server.serve", new_callable=AsyncMock),
            ):
                mock_app = AsyncMock()
                mock_app.add_middleware = lambda *a, **kw: None
                mock_app.routes = []
                mock_http_app.return_value = mock_app

                await server.run_http(host="0.0.0.0", port=9999)

            # Should not be overwritten — operator's choice is respected
            assert _fastmcp.settings.host == "10.0.0.5"
        finally:
            _fastmcp.settings.host = original_host
