"""Tests for the public ``CodeExecutionServer.warm`` API."""

from unittest.mock import AsyncMock

import pytest

from .. import CodeExecutionServer, ServerConfig
from ..auth import create_noop_auth_config


def _make_server() -> CodeExecutionServer:
    config = ServerConfig(
        name="test_warm",
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
        server._register_kernel.assert_awaited_once_with(kernel_name="tools-py")
