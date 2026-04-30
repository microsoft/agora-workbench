"""
Pytest configuration and fixtures for code execution tests.
"""

import os
import subprocess
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


@pytest.fixture(scope="session")
def azure_cli_token():
    """Get Azure CLI access token for live server testing.

    Only used in live tests (marked with @pytest.mark.live).
    Requires Azure CLI to be installed and user to be logged in.

    Gets a token for the ENTRA_CLIENT_ID specified in environment,
    which should match the audience expected by the MCP servers.
    """
    scope = os.getenv("MCP_SERVER_SCOPE", "api://12c90937-013c-468e-93cf-eb8083d69ca7/.default")
    try:
        # Request token for the specific application scope
        result = subprocess.run(
            [
                "az",
                "account",
                "get-access-token",
                "--scope",
                scope,
                "--query",
                "accessToken",
                "-o",
                "tsv",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        token = result.stdout.strip()
        if not token:
            pytest.skip("Azure CLI token is empty. Please run 'az login'.")
        return token
    except FileNotFoundError:
        pytest.skip("Azure CLI not found. Install it to run live tests.")


@pytest_asyncio.fixture
async def authenticated_client_session(azure_cli_token):
    """Create an authenticated MCP client session for live server testing.

    Automatically adds Bearer token authentication required by the server.
    Use this fixture instead of manually creating ClientSession in live tests.

    The returned function accepts an optional `url` parameter to connect to
    different servers (defaults to http://localhost:8000/mcp).

    Example:
        async def test_something(authenticated_client_session):
            async with authenticated_client_session() as session:
                await session.initialize()
                result = await session.call_tool(...)

        async def test_powergrid(authenticated_client_session):
            async with authenticated_client_session(url="http://localhost:8001/mcp") as session:
                await session.initialize()
                ...
    """

    @asynccontextmanager
    async def _session(url: str = "http://localhost:8000/mcp"):
        """Context manager for authenticated client session."""
        # StreamableHTTP keeps a long-lived GET stream open; disable read timeout.
        timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)

        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {azure_cli_token}"},
            timeout=timeout,
        ) as http_client:
            async with streamable_http_client(
                url,
                http_client=http_client,
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    yield session

    return _session
