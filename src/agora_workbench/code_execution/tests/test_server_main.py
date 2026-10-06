"""Tests for CodeExecutionServer.main() CLI entrypoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .. import CodeExecutionServer, ServerConfig, SidecarConfig
from ..auth import create_noop_auth_config
from ..sessions import SessionConfig, SessionManager


def _make_server() -> CodeExecutionServer:
    config = ServerConfig(
        name="test_main",
        description="Test server",
        type="uv",
        dependency_file="# empty",
    )
    return CodeExecutionServer(
        server_config=config,
        auth_config=create_noop_auth_config(),
    )


def test_server_config_propagates_isolated_kernel_network_mode():
    config = ServerConfig(
        name="isolated",
        description="Test server",
        type="uv",
        dependency_file="# empty",
        kernel_network_mode="isolated",
    )

    server = CodeExecutionServer(
        server_config=config,
        auth_config=create_noop_auth_config(),
    )

    assert server.session_manager.config.kernel_network_mode == "isolated"


def test_server_config_overrides_custom_manager_network_mode():
    manager = SessionManager(SessionConfig(kernel_network_mode="inherit"))
    config = ServerConfig(
        name="isolated",
        description="Test server",
        type="uv",
        dependency_file="# empty",
        kernel_network_mode="isolated",
    )

    server = CodeExecutionServer(
        server_config=config,
        session_manager=manager,
        auth_config=create_noop_auth_config(),
    )

    assert server.session_manager is manager
    assert manager.config.kernel_network_mode == "isolated"


def test_custom_manager_can_enable_isolation_directly():
    manager = SessionManager(SessionConfig(kernel_network_mode="isolated"))
    config = ServerConfig(
        name="isolated",
        description="Test server",
        type="uv",
        dependency_file="# empty",
    )

    server = CodeExecutionServer(
        server_config=config,
        session_manager=manager,
        auth_config=create_noop_auth_config(),
    )

    assert server.session_manager is manager
    assert manager.config.kernel_network_mode == "isolated"


def test_explicit_server_inherit_overrides_custom_manager_isolation():
    manager = SessionManager(SessionConfig(kernel_network_mode="isolated"))
    config = ServerConfig(
        name="inherited",
        description="Test server",
        type="uv",
        dependency_file="# empty",
        kernel_network_mode="inherit",
    )

    server = CodeExecutionServer(
        server_config=config,
        session_manager=manager,
        auth_config=create_noop_auth_config(),
    )

    assert server.session_manager is manager
    assert manager.config.kernel_network_mode == "inherit"


def test_server_rejects_network_mode_override_with_cached_kernel():
    manager = SessionManager(SessionConfig(kernel_network_mode="inherit"))
    manager._kernels["active-session"] = (MagicMock(), MagicMock())
    config = ServerConfig(
        name="isolated",
        description="Test server",
        type="uv",
        dependency_file="# empty",
        kernel_network_mode="isolated",
    )

    with pytest.raises(RuntimeError, match="kernels are running or starting: active-session"):
        CodeExecutionServer(
            server_config=config,
            session_manager=manager,
            auth_config=create_noop_auth_config(),
        )

    assert manager.config.kernel_network_mode == "inherit"


def test_manager_rejects_network_mode_change_during_kernel_start():
    manager = SessionManager(SessionConfig(kernel_network_mode="inherit"))
    manager._kernel_start_tasks["starting-session"] = {MagicMock(): 1}

    with pytest.raises(RuntimeError, match="kernels are running or starting: starting-session"):
        manager.set_kernel_network_mode("isolated")

    assert manager.config.kernel_network_mode == "inherit"


def test_custom_isolated_manager_rejects_loopback_sidecars():
    manager = SessionManager(SessionConfig(kernel_network_mode="isolated"))
    config = ServerConfig(
        name="isolated",
        description="Test server",
        type="uv",
        dependency_file="# empty",
        sidecars=[SidecarConfig(name="model", command=["-m", "svc"], url_env_var="SVC_URL", port=9100)],
    )

    with pytest.raises(ValueError, match="cannot be combined with loopback HTTP sidecars"):
        CodeExecutionServer(
            server_config=config,
            session_manager=manager,
            auth_config=create_noop_auth_config(),
        )


class TestServerMain:
    @pytest.mark.unit
    def test_warm_flag_calls_warm(self):
        server = _make_server()
        server.warm = AsyncMock()

        with patch("sys.argv", ["server", "--warm"]):
            server.main()

        server.warm.assert_awaited_once()

    @pytest.mark.unit
    def test_no_flags_calls_run_http_with_defaults(self):
        server = _make_server()
        server.run_http = AsyncMock()

        with patch("sys.argv", ["server"]), patch.dict("os.environ", {"HOST": "", "PORT": ""}, clear=False):
            server.main()

        server.run_http.assert_awaited_once_with(
            host="127.0.0.1",
            port=8000,
            allow_unauthenticated_remote=False,
        )

    @pytest.mark.unit
    def test_host_and_port_flags(self):
        server = _make_server()
        server.run_http = AsyncMock()

        with patch("sys.argv", ["server", "--host", "127.0.0.1", "--port", "9000"]):
            server.main()

        server.run_http.assert_awaited_once_with(
            host="127.0.0.1",
            port=9000,
            allow_unauthenticated_remote=False,
        )

    @pytest.mark.unit
    def test_env_vars_override_defaults(self):
        server = _make_server()
        server.run_http = AsyncMock()

        env = {"HOST": "10.0.0.1", "PORT": "3000"}
        with patch("sys.argv", ["server"]), patch.dict("os.environ", env):
            server.main()

        server.run_http.assert_awaited_once_with(
            host="10.0.0.1",
            port=3000,
            allow_unauthenticated_remote=False,
        )

    @pytest.mark.unit
    def test_explicit_flags_override_env_vars(self):
        server = _make_server()
        server.run_http = AsyncMock()

        env = {"HOST": "10.0.0.1", "PORT": "3000"}
        with patch("sys.argv", ["server", "--host", "localhost", "--port", "5000"]), patch.dict("os.environ", env):
            server.main()

        server.run_http.assert_awaited_once_with(
            host="localhost",
            port=5000,
            allow_unauthenticated_remote=False,
        )

    @pytest.mark.unit
    def test_custom_defaults(self):
        server = _make_server()
        server.run_http = AsyncMock()

        with patch("sys.argv", ["server"]), patch.dict("os.environ", {"HOST": "", "PORT": ""}, clear=False):
            server.main(default_host="127.0.0.1", default_port=4000)

        server.run_http.assert_awaited_once_with(
            host="127.0.0.1",
            port=4000,
            allow_unauthenticated_remote=False,
        )

    @pytest.mark.unit
    def test_unauthenticated_remote_flag_is_forwarded(self):
        server = _make_server()
        server.run_http = AsyncMock()

        with patch(
            "sys.argv",
            ["server", "--host", "0.0.0.0", "--allow-unauthenticated-remote"],
        ):
            server.main()

        server.run_http.assert_awaited_once_with(
            host="0.0.0.0",
            port=8000,
            allow_unauthenticated_remote=True,
        )

    @pytest.mark.unit
    def test_unauthenticated_remote_env_is_forwarded(self):
        server = _make_server()
        server.run_http = AsyncMock()

        env = {
            "HOST": "0.0.0.0",
            "AGORA_ALLOW_UNAUTHENTICATED_REMOTE": "1",
        }
        with patch("sys.argv", ["server"]), patch.dict("os.environ", env):
            server.main()

        server.run_http.assert_awaited_once_with(
            host="0.0.0.0",
            port=8000,
            allow_unauthenticated_remote=True,
        )

    @pytest.mark.unit
    def test_cli_can_disable_unauthenticated_remote_env(self):
        server = _make_server()
        server.run_http = AsyncMock()

        env = {
            "HOST": "0.0.0.0",
            "AGORA_ALLOW_UNAUTHENTICATED_REMOTE": "1",
        }
        with (
            patch("sys.argv", ["server", "--no-allow-unauthenticated-remote"]),
            patch.dict("os.environ", env),
        ):
            server.main()

        server.run_http.assert_awaited_once_with(
            host="0.0.0.0",
            port=8000,
            allow_unauthenticated_remote=False,
        )

    @pytest.mark.unit
    def test_invalid_unauthenticated_remote_env_exits(self):
        server = _make_server()

        with (
            patch("sys.argv", ["server"]),
            patch.dict("os.environ", {"AGORA_ALLOW_UNAUTHENTICATED_REMOTE": "maybe"}),
            pytest.raises(SystemExit),
        ):
            server.main()
