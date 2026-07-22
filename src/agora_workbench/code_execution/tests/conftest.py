"""
Pytest configuration and fixtures for code execution tests.
"""

import asyncio
import os
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from azure.core.credentials import AccessToken
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

# Set a deterministic test endpoint so DataLakeDataManager can initialize in CI
# without requiring environment-specific Azure Search configuration.
os.environ.setdefault("DATA_LAKE_SEARCH_ENDPOINT", "https://test-search.search.windows.net")
# Provide default Entra ID credentials for tests
os.environ.setdefault("ENTRA_CLIENT_ID", "test-client-id")
os.environ.setdefault("ENTRA_TENANT_ID", "test-tenant-id")

from .. import CodeExecutionServer, ServerConfig
from ..sessions import SessionManager, SessionConfig, set_current_session
from ..sessions import manager as _session_manager_module
from agora_workbench.code_execution import ToolDefinition, ToolParameter, ToolRegistry


# Test authentication token for all tests
TEST_USER_TOKEN = "test-user-token-for-testing"


@pytest.fixture(autouse=True)
def _isolate_outputs_base_dir(tmp_path_factory, monkeypatch):
    """Redirect session artifact output to a temp dir for every test.

    ``SessionManager.create_session`` eagerly creates a per-session subdir
    under the module-level ``_OUTPUTS_BASE_DIR`` constant, which defaults to
    ``~/agora-outputs``. Without this fixture, every test that creates a
    session litters the developer's real home directory with empty UUID
    folders. Patching the constant keeps production behavior intact while
    isolating tests to a temp directory.
    """
    base = tmp_path_factory.mktemp("agora-outputs")
    monkeypatch.setattr(_session_manager_module, "_OUTPUTS_BASE_DIR", base)
    yield


@pytest.fixture
def create_mock_credential():
    """
    Fixture that provides a factory function to create mock credentials.

    Usage:
        def test_something(create_mock_credential):
            credential = create_mock_credential()
            # or with custom token:
            credential = create_mock_credential(token="custom-token")
    """

    def _create_mock_credential(token: str = "mock-credential-token", expires_on: int = 9999999999):
        """
        Create a mock AsyncTokenCredential for testing.

        Args:
            token: The token string to return from get_token calls
            expires_on: Token expiration timestamp

        Returns:
            Mock credential that returns the specified token for all scopes
        """
        mock_cred = MagicMock()
        mock_cred.get_token = AsyncMock(return_value=AccessToken(token=token, expires_on=expires_on))
        mock_cred.close = AsyncMock()
        return mock_cred

    return _create_mock_credential


@pytest.fixture(scope="session")
def mock_credential():
    """Provide a mock Azure credential for testing."""
    credential = MagicMock()

    # Mock get_token to return a realistic AccessToken
    async def mock_get_token(*scopes, **kwargs):
        # Return a token that expires in 1 hour
        expiry = int((datetime.now() + timedelta(hours=1)).timestamp())
        return AccessToken(token="mock_access_token_12345", expires_on=expiry)

    credential.get_token = AsyncMock(side_effect=mock_get_token)

    # Mock close method
    credential.close = AsyncMock()

    return credential


@pytest.fixture(scope="session")
def event_loop():
    """Create an event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def azure_cli_token():
    """Get Azure CLI access token for live server testing.

    Only used in live tests (marked with @pytest.mark.live).
    Requires Azure CLI to be installed and user to be logged in.
    """
    try:
        result = subprocess.run(
            ["az", "account", "get-access-token", "--query", "accessToken", "-o", "tsv"],
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
    except subprocess.CalledProcessError as e:
        pytest.skip(f"Failed to get Azure CLI token: {e.stderr}")


@pytest.fixture
def sample_tool():
    """Sample tool definition for testing."""
    return ToolDefinition(
        name="solve_network",
        description="Solve power network optimization",
        required_parameters=[ToolParameter(name="network_id", type=str, description="Network identifier")],
        optional_parameters=[ToolParameter(name="solver", type=str, description="Solver name", default="highs")],
        module="test.tools.solvers",
    )


@pytest.fixture
def another_tool():
    """Another sample tool definition for testing."""
    return ToolDefinition(
        name="build_network",
        description="Build base network topology",
        required_parameters=[ToolParameter(name="region", type=str, description="Region name")],
        optional_parameters=[],
        module="test.tools.network",
    )


@pytest.fixture
def empty_registry():
    """Empty tool registry for testing."""
    return ToolRegistry()


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


@pytest.fixture(scope="session")
def session_temp_dir(tmp_path_factory) -> Path:
    """Provide a temporary directory for the entire test session."""
    return tmp_path_factory.mktemp("session")


@pytest.fixture(scope="session")
def session_work_dir(session_temp_dir: Path) -> Path:
    """Provide a temporary working directory for the test session."""
    work_dir = session_temp_dir / "work"
    work_dir.mkdir(exist_ok=True)
    return work_dir


@pytest.fixture(scope="session")
def session_build_dir(session_temp_dir: Path) -> Path:
    """Provide a build directory for the test session environment."""
    build_dir = session_temp_dir / "test_env" / "uv"
    return build_dir


@pytest_asyncio.fixture(scope="session")
async def test_server(
    session_work_dir: Path, session_build_dir: Path, mock_credential
) -> AsyncGenerator[CodeExecutionServer, None]:
    """
    Create a test code execution server with minimal dependencies.

    Builds the environment once at session scope and reuses it for all tests,
    mirroring the intended usage pattern where an environment is created once
    and used for multiple code executions.

    Uses uv with a requirements.txt file for fast package installation.
    """
    config = ServerConfig(
        name="test",
        description="Test environment for unit tests",
        type="uv",
        dependency_file="numpy>=1.24.0\npandas>=2.3.3\ndill>=0.3.8\nipykernel>=6.29.0\n",
        auto_build=True,
        build_dir=session_build_dir,
    )

    # Create session manager with short timeouts for testing
    session_manager = SessionManager(
        SessionConfig(
            max_sessions=10,
            timeout_minutes=1,  # Short timeout for tests
            cleanup_interval_seconds=5,
        )
    )

    from ..auth import create_noop_auth_config

    server = CodeExecutionServer(
        server_config=config,
        session_manager=session_manager,
        auth_config=create_noop_auth_config(),
        working_dir=session_work_dir,
    )

    # Build environment once for all tests
    await server._ensure_environment()

    # The session manager expects to launch a kernel named "tools-py".
    # Register it using the environment's Python interpreter.
    await server._register_kernel(kernel_name="tools-py")

    # Add helper methods for session-based code execution in tests

    async def execute_code_isolated(code: str, timeout: int):
        """Execute code in a fresh session (no persistence between calls)."""
        session_id = server.session_manager.create_session(
            data={}, user_identity="test_user", user_token=TEST_USER_TOKEN, token_claims={}
        )
        session = server.session_manager.get_session(session_id)
        set_current_session(session)

        try:
            return await server._execute_code(code=code, timeout=timeout)
        finally:
            set_current_session(None)
            server.session_manager.close_session(session_id)

    async def execute_code_with_session(code: str, timeout: int, session_id: str):
        """
        Execute code within a session context for testing.

        This method properly manages the session context lifecycle:
        1. Retrieves the session
        2. Sets it as the current session in context
        3. Executes the code
        4. Saves the session
        5. Cleans up the context

        Args:
            code: Python code to execute
            timeout: Execution timeout in seconds
            session_id: ID of the session to execute in

        Returns:
            CodeExecutionResult
        """
        # Retrieve session
        session = server.session_manager.get_session(session_id)

        # Set session context
        set_current_session(session)

        try:
            # Execute code
            result = await server._execute_code(code=code, timeout=timeout)

            # Save session
            server.session_manager.update_session(session_id, session)

            return result
        finally:
            # Clean up context
            set_current_session(None)

    # Bind the method to the server instance
    server.execute_code_isolated = execute_code_isolated
    server.execute_code_with_session = execute_code_with_session

    yield server

    # Best-effort cleanup: ensure any kernels/sessions are fully torn down
    try:
        for session_info in server.session_manager.list_sessions():
            server.session_manager.close_session(session_info["session_id"])

        kernel_ids = list(getattr(server.session_manager, "_kernels", {}).keys())
        for session_id in kernel_ids:
            await server.session_manager._shutdown_kernel(session_id)
    except Exception:
        # Avoid masking test results if teardown has issues.
        pass


@pytest.fixture
def temp_work_dir(tmp_path: Path) -> Path:
    """Provide a temporary working directory for individual tests that need isolation."""
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    return work_dir


@pytest.fixture
def simple_code_samples():
    """Provide sample code snippets for testing."""
    return {
        "hello_world": 'print("Hello, World!")',
        "math_calc": "result = 2 + 2\nprint(result)",
        "import_test": "import numpy as np\nprint(np.__version__)",
        "error_code": "raise ValueError('Test error')",
        "syntax_error": "print('unclosed string",
        "infinite_loop": "while True: pass",
        "multiline": """
import pandas as pd
data = {'a': [1, 2, 3], 'b': [4, 5, 6]}
df = pd.DataFrame(data)
print(df.head())
""",
    }
