"""Tests for CodeExecutionServer.main() CLI entrypoint."""

from unittest.mock import AsyncMock, patch

import pytest

from .. import CodeExecutionServer, ServerConfig
from ..auth import create_noop_auth_config


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

        server.run_http.assert_awaited_once_with(host="0.0.0.0", port=8000)

    @pytest.mark.unit
    def test_host_and_port_flags(self):
        server = _make_server()
        server.run_http = AsyncMock()

        with patch("sys.argv", ["server", "--host", "127.0.0.1", "--port", "9000"]):
            server.main()

        server.run_http.assert_awaited_once_with(host="127.0.0.1", port=9000)

    @pytest.mark.unit
    def test_env_vars_override_defaults(self):
        server = _make_server()
        server.run_http = AsyncMock()

        env = {"HOST": "10.0.0.1", "PORT": "3000"}
        with patch("sys.argv", ["server"]), patch.dict("os.environ", env):
            server.main()

        server.run_http.assert_awaited_once_with(host="10.0.0.1", port=3000)

    @pytest.mark.unit
    def test_explicit_flags_override_env_vars(self):
        server = _make_server()
        server.run_http = AsyncMock()

        env = {"HOST": "10.0.0.1", "PORT": "3000"}
        with patch("sys.argv", ["server", "--host", "localhost", "--port", "5000"]), patch.dict("os.environ", env):
            server.main()

        server.run_http.assert_awaited_once_with(host="localhost", port=5000)

    @pytest.mark.unit
    def test_custom_defaults(self):
        server = _make_server()
        server.run_http = AsyncMock()

        with patch("sys.argv", ["server"]), patch.dict("os.environ", {"HOST": "", "PORT": ""}, clear=False):
            server.main(default_host="127.0.0.1", default_port=4000)

        server.run_http.assert_awaited_once_with(host="127.0.0.1", port=4000)
