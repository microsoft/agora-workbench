"""Tests for direct code-execution MCP tool registration."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client

from .. import CodeExecutionServer, ServerConfig
from ..auth import create_noop_auth_config
from ..code_execution_models import CodeExecutionResult


class _SessionResourceOperation:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        del exc_type, exc_value, traceback


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_tool_has_no_duplicate_structured_content():
    server = CodeExecutionServer(
        server_config=ServerConfig(
            name="test_direct",
            description="Test direct code execution",
            type="uv",
            dependency_file="# empty",
        ),
        auth_config=create_noop_auth_config(),
    )
    tool_name = server.get_tool_name()
    tool = await server.mcp.get_tool(tool_name)
    assert tool.output_schema is None

    session = SimpleNamespace(
        session_id="test-session",
        extensions={},
        data_manager=MagicMock(),
    )
    server._get_or_create_session = AsyncMock(return_value=session)
    server._inject_tool_proxies = AsyncMock()
    server._execute_code_with_tracing = AsyncMock(
        return_value=CodeExecutionResult(stdout="hello\n"),
    )
    server.session_manager.session_resource_operation = MagicMock(return_value=_SessionResourceOperation())
    server.session_manager.update_session = MagicMock()
    server.activity_publisher.publish_nowait = MagicMock()

    async with Client(server.mcp) as client:
        result = await client.call_tool(tool_name, {"code": "print('hello')"})

    assert json.loads(result.content[0].text)["stdout"] == "hello\n"
    assert result.structured_content is None
