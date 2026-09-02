"""Live tests for server-to-server object transfer.

These tests verify the full end-to-end object transfer flow between two
CodeExecutionServer instances communicating over HTTP.  They require:

  1. A built Python environment (auto-built on first run)
  2. No external services (self-contained with two in-process servers)

Run with::

    uv run pytest code_execution/tests/test_object_transfer_live.py -m live

Skipped in CI by default (``-m "not live"``).
"""

import json
import logging
import threading

import pytest
import pytest_asyncio
import uvicorn
from pathlib import Path
from typing import AsyncGenerator

from .. import CodeExecutionServer, ServerConfig
from ..sessions import (
    SessionManager,
    SessionConfig,
    set_current_session,
    set_current_request_token,
    set_current_user_identity,
    set_current_token_claims,
)

LOGGER = logging.getLogger(__name__)

TEST_USER_IDENTITY = "test-user-oid@test-tenant-id"
TEST_USER_TOKEN = "test-user-token-for-live-testing"


def _get_free_port() -> int:
    """Find an available TCP port."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def module_temp_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("transfer_live")


@pytest.fixture(scope="module")
def module_build_dir(module_temp_dir: Path) -> Path:
    return module_temp_dir / "env" / "uv"


@pytest.fixture(scope="module")
def module_work_dir(module_temp_dir: Path) -> Path:
    d = module_temp_dir / "work"
    d.mkdir(exist_ok=True)
    return d


def _make_server(
    name: str,
    build_dir: Path,
    work_dir: Path,
    publishers: "list | None" = None,
) -> CodeExecutionServer:
    """Create a CodeExecutionServer with test-friendly settings."""
    config = ServerConfig(
        name=name,
        description=f"Live test environment ({name})",
        type="uv",
        dependency_file="numpy>=1.24.0\nipykernel>=6.29.0\ndill>=0.3.8\n",
        auto_build=True,
        build_dir=build_dir,
    )
    session_manager = SessionManager(SessionConfig(max_sessions=10, timeout_minutes=5, cleanup_interval_seconds=60))
    from ..auth import create_noop_auth_config

    return CodeExecutionServer(
        server_config=config,
        session_manager=session_manager,
        auth_config=create_noop_auth_config(),
        working_dir=work_dir,
        publishers=publishers,
    )


def _set_auth_context():
    """Set fake auth context vars so _get_or_create_session succeeds."""
    set_current_user_identity(TEST_USER_IDENTITY)
    set_current_request_token(TEST_USER_TOKEN)
    set_current_token_claims({"oid": "test-user-oid", "tid": "test-tenant-id"})


def _clear_auth_context():
    set_current_user_identity(None)
    set_current_request_token(None)
    set_current_token_claims(None)


@pytest_asyncio.fixture(scope="module")
async def source_server(
    module_build_dir: Path, module_work_dir: Path, target_http_server: str
) -> AsyncGenerator[CodeExecutionServer, None]:
    """Source server that will send objects to the target."""
    from ..data_access.publishers import ServerPublisher

    # Register a ServerPublisher pointing at the target HTTP server
    target_pub = ServerPublisher(server_name="target", target_url=target_http_server)
    server = _make_server("source", module_build_dir, module_work_dir / "source", publishers=[target_pub])
    (module_work_dir / "source").mkdir(exist_ok=True)
    await server._ensure_environment()
    await server._register_kernel(kernel_name=server.kernel_name)
    yield server
    try:
        for s in server.session_manager.list_sessions():
            server.session_manager.close_session(s["session_id"])
        for sid in list(server.session_manager._kernels):
            await server.session_manager._shutdown_kernel(sid)
    except Exception:
        pass


@pytest_asyncio.fixture(scope="module")
async def target_server(module_build_dir: Path, module_work_dir: Path) -> AsyncGenerator[CodeExecutionServer, None]:
    """Target server that will receive objects."""
    server = _make_server("target", module_build_dir, module_work_dir / "target")
    (module_work_dir / "target").mkdir(exist_ok=True)
    await server._ensure_environment()
    await server._register_kernel(kernel_name=server.kernel_name)
    yield server
    try:
        for s in server.session_manager.list_sessions():
            server.session_manager.close_session(s["session_id"])
        for sid in list(server.session_manager._kernels):
            await server.session_manager._shutdown_kernel(sid)
    except Exception:
        pass


@pytest.fixture(scope="module")
def target_http_server(target_server: CodeExecutionServer):
    """Run the target server as an HTTP server in a background thread.

    Builds the Starlette app the same way ``run_http`` does, but replaces
    the real AuthMiddleware with a test shim that injects fake credentials.
    """
    app = target_server.mcp.http_app(transport="streamable-http")
    target_server._add_custom_endpoints(app)

    from starlette.types import ASGIApp, Receive, Scope, Send

    class TestAuthMiddleware:
        """Bypass Entra ID validation; inject test identity on every request."""

        def __init__(self, app: ASGIApp, **kwargs):
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send):
            if scope["type"] == "http":
                set_current_user_identity(TEST_USER_IDENTITY)
                set_current_request_token(TEST_USER_TOKEN)
                set_current_token_claims({"oid": "test-user-oid", "tid": "test-tenant-id"})
            await self.app(scope, receive, send)

    app.add_middleware(TestAuthMiddleware)

    target_port = _get_free_port()

    config = uvicorn.Config(app=app, host="127.0.0.1", port=target_port, log_level="warning")
    http_server = uvicorn.Server(config)

    thread = threading.Thread(target=http_server.run, daemon=True)
    thread.start()

    # Wait for server to start
    import time

    for _ in range(50):
        if http_server.started:
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("Target HTTP server did not start in time")

    yield f"http://127.0.0.1:{target_port}"

    http_server.should_exit = True
    thread.join(timeout=5)


def _create_session(server: CodeExecutionServer) -> str:
    """Create a session on a server with test auth context."""
    return server.session_manager.create_session(
        data={},
        user_identity=TEST_USER_IDENTITY,
        user_token=TEST_USER_TOKEN,
        token_claims={"oid": "test-user-oid", "tid": "test-tenant-id"},
    )


async def _exec(server: CodeExecutionServer, session_id: str, code: str, timeout: int = 30):
    """Execute code in a session, managing context vars."""
    session = server.session_manager.get_session(session_id)
    set_current_session(session)
    try:
        result = await server._execute_code(code=code, timeout=timeout)
        server.session_manager.update_session(session_id, session)
        return result
    finally:
        set_current_session(None)


async def _send_object(
    source_server: CodeExecutionServer,
    src_session: str,
    to: str,
    data_ref: str,
    name: str = "",
    session_id: str = "",
) -> dict:
    """Call send on the source server with proper auth context."""
    _set_auth_context()
    try:
        session = source_server.session_manager.get_session(src_session)
        set_current_session(session)

        tool_name = f"{source_server.server_config.name}_send"
        tool = await source_server.mcp.get_tool(tool_name)

        result_json = await tool.fn(
            ctx=None,
            data_ref=data_ref,
            to=to,
            name=name,
            session_id=session_id,
        )
        return json.loads(result_json)
    finally:
        set_current_session(None)
        _clear_auth_context()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.asyncio
class TestObjectTransferLive:
    """End-to-end object transfer between two CodeExecutionServer instances."""

    async def test_push_simple_variable(self, source_server, target_server):
        """Transfer a simple Python dict from source to target."""
        src_session = _create_session(source_server)
        tgt_session = _create_session(target_server)

        r = await _exec(source_server, src_session, "transfer_data = {'key': 'value', 'count': 42}")
        assert r.success, f"Source setup failed: {r.error}"

        result = await _send_object(
            source_server,
            src_session,
            to="target",
            data_ref="transfer_data",
            name="received_data",
            session_id=tgt_session,
        )
        assert result["success"], f"Push failed: {result}"
        assert result["data_ref"] == "transfer_data"
        assert result["name"] == "received_data"
        assert result["transfer_id"]

        r = await _exec(target_server, tgt_session, "import json; print(json.dumps(received_data))")
        assert r.success, f"Target read failed: {r.error}"
        received = json.loads(r.stdout.strip())
        assert received == {"key": "value", "count": 42}

    async def test_push_numpy_array(self, source_server, target_server):
        """Transfer a numpy array between servers."""
        src_session = _create_session(source_server)
        tgt_session = _create_session(target_server)

        r = await _exec(
            source_server,
            src_session,
            "import numpy as np\narr = np.arange(100).reshape(10, 10)",
        )
        assert r.success, f"Source setup failed: {r.error}"

        result = await _send_object(
            source_server,
            src_session,
            to="target",
            data_ref="arr",
            name="remote_arr",
            session_id=tgt_session,
        )
        assert result["success"], f"Push failed: {result}"

        r = await _exec(
            target_server,
            tgt_session,
            "import numpy as np\nprint(remote_arr.shape)\nprint(np.array_equal(remote_arr, np.arange(100).reshape(10, 10)))",
        )
        assert r.success, f"Target read failed: {r.error}"
        lines = r.stdout.strip().split("\n")
        assert "(10, 10)" in lines[0]
        assert "True" in lines[1]

    async def test_push_preserves_variable_name(self, source_server, target_server):
        """When name is empty, source data_ref is used as the variable name."""
        src_session = _create_session(source_server)
        tgt_session = _create_session(target_server)

        r = await _exec(source_server, src_session, "same_name_var = [1, 2, 3]")
        assert r.success

        result = await _send_object(
            source_server,
            src_session,
            to="target",
            data_ref="same_name_var",
            name="",
            session_id=tgt_session,
        )
        assert result["success"]
        assert result["name"] == "same_name_var"

        r = await _exec(target_server, tgt_session, "print(same_name_var)")
        assert r.success
        assert "[1, 2, 3]" in r.stdout

    async def test_push_nonexistent_variable_fails(self, source_server, target_server):
        """Pushing a variable that doesn't exist in the kernel should fail gracefully."""
        src_session = _create_session(source_server)

        result = await _send_object(
            source_server,
            src_session,
            to="target",
            data_ref="no_such_var",
        )
        assert not result["success"]
        assert "NameError" in result["error"] or "no_such_var" in result["error"]

    async def test_push_invalid_variable_name_rejected(self, source_server, target_server):
        """Variable names that aren't valid Python identifiers are rejected."""
        src_session = _create_session(source_server)

        for bad_name in ["123abc", "my-var", "import", "class"]:
            result = await _send_object(
                source_server,
                src_session,
                to="target",
                data_ref=bad_name,
            )
            assert not result["success"], f"Should have rejected '{bad_name}'"
            assert "Invalid" in result["error"] or "variable name" in result["error"]

    async def test_push_class_instance(self, source_server, target_server):
        """Transfer a custom class instance — the kind of object that can't travel through JSON."""
        src_session = _create_session(source_server)
        tgt_session = _create_session(target_server)

        r = await _exec(
            source_server,
            src_session,
            (
                "class GridModel:\n"
                "    def __init__(self, name, buses):\n"
                "        self.name = name\n"
                "        self.buses = buses\n"
                "    def bus_count(self):\n"
                "        return len(self.buses)\n"
                "\n"
                "grid = GridModel('test_grid', ['bus_a', 'bus_b', 'bus_c'])\n"
            ),
        )
        assert r.success, f"Source setup failed: {r.error}"

        result = await _send_object(
            source_server,
            src_session,
            to="target",
            data_ref="grid",
            name="remote_grid",
            session_id=tgt_session,
        )
        assert result["success"], f"Push failed: {result}"

        r = await _exec(
            target_server,
            tgt_session,
            "print(remote_grid.name)\nprint(remote_grid.buses)\nprint(remote_grid.bus_count())",
        )
        assert r.success, f"Target read failed: {r.error}"
        lines = r.stdout.strip().split("\n")
        assert "test_grid" in lines[0]
        assert "bus_a" in lines[1]
        assert "3" in lines[2]

    async def test_push_large_object(self, source_server, target_server):
        """Transfer a moderately large object (~10 MB numpy array)."""
        src_session = _create_session(source_server)
        tgt_session = _create_session(target_server)

        r = await _exec(
            source_server,
            src_session,
            "import numpy as np\nlarge_arr = np.random.rand(1000, 1000)",
        )
        assert r.success

        result = await _send_object(
            source_server,
            src_session,
            to="target",
            data_ref="large_arr",
            session_id=tgt_session,
        )
        assert result["success"], f"Push failed: {result}"
        assert result["transfer_id"]

        r = await _exec(target_server, tgt_session, "print(large_arr.shape)")
        assert r.success
        assert "(1000, 1000)" in r.stdout

    async def test_push_does_not_mutate_source(self, source_server, target_server):
        """The source variable must remain intact after transfer."""
        src_session = _create_session(source_server)
        tgt_session = _create_session(target_server)

        r = await _exec(source_server, src_session, "src_obj = {'original': True}")
        assert r.success

        result = await _send_object(
            source_server,
            src_session,
            to="target",
            data_ref="src_obj",
            session_id=tgt_session,
        )
        assert result["success"]

        # Verify source is untouched
        r = await _exec(source_server, src_session, "import json; print(json.dumps(src_obj))")
        assert r.success
        assert json.loads(r.stdout.strip()) == {"original": True}

        # Mutate on target and verify source is independent
        r = await _exec(target_server, tgt_session, "src_obj['mutated'] = True")
        assert r.success
        r = await _exec(source_server, src_session, "print('mutated' in src_obj)")
        assert r.success
        assert "False" in r.stdout
