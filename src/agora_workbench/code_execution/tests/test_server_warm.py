"""Tests for the public ``CodeExecutionServer.warm`` API."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from .. import CodeExecutionServer, ServerConfig
from ..auth import create_noop_auth_config


def _make_server(name: str = "test_warm") -> CodeExecutionServer:
    config = ServerConfig(
        name=name,
        description="Test environment",
        type="uv",
        dependency_file="# empty",
    )
    return CodeExecutionServer(
        server_config=config,
        auth_config=create_noop_auth_config(),
    )


class TestServerWarm:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_warm_prepares_environment_and_registers_kernel(self):
        server = _make_server()
        server._ensure_environment = AsyncMock()
        server._register_kernel = AsyncMock()

        result = await server.warm()

        assert result is None
        server._ensure_environment.assert_awaited_once_with()
        server._register_kernel.assert_awaited_once_with(kernel_name="tools-py-test_warm")

    @pytest.mark.unit
    def test_servers_use_distinct_kernel_names(self):
        alpha = _make_server("alpha")
        beta = _make_server("beta")

        assert alpha.kernel_name == "tools-py-alpha"
        assert beta.kernel_name == "tools-py-beta"
        assert alpha.session_manager.kernel_name == alpha.kernel_name
        assert beta.session_manager.kernel_name == beta.kernel_name

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_registration_check_honors_jupyter_data_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        server = _make_server()
        server._python_executable = Path("/env/bin/python")
        kernel_dir = tmp_path / "kernels" / server.kernel_name
        kernel_dir.mkdir(parents=True)
        (kernel_dir / "kernel.json").write_text(
            json.dumps(
                {
                    "argv": [str(server._python_executable), "-m", "ipykernel_launcher", "-f", "{connection_file}"],
                    "display_name": "Python (test_warm)",
                    "language": "python",
                }
            )
        )
        monkeypatch.setenv("JUPYTER_DATA_DIR", str(tmp_path))

        with patch("agora_workbench.code_execution.server.subprocess.run") as mock_run:
            await server._register_kernel(server.kernel_name)

        mock_run.assert_not_called()
