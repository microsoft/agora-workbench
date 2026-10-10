"""Tests for direct code-execution MCP tool registration."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client

from .. import CodeExecutionServer, ServerConfig
from ..auth import create_noop_auth_config
from ..code_execution_models import CodeExecutionResult, ToolCallRecord


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
        return_value=CodeExecutionResult(
            stdout="hello\n",
            tool_calls=[
                ToolCallRecord(
                    tool_name="successful_tool",
                    args={"large": "input"},
                    result={"large": "output"},
                )
            ],
        ),
    )
    server.session_manager.session_resource_operation = MagicMock(return_value=_SessionResourceOperation())
    server.session_manager.update_session = MagicMock()
    server.activity_publisher.publish_nowait = MagicMock()

    async with Client(server.mcp) as client:
        result = await client.call_tool(tool_name, {"code": "print('hello')"})

    payload = json.loads(result.content[0].text)
    assert payload["stdout"] == "hello\n"
    assert "tool_calls" not in payload
    assert "failed_tool_calls" not in payload
    assert result.structured_content is None
    activity_event = server.activity_publisher.publish_nowait.call_args.args[0]
    assert activity_event["tool_calls"][0]["result"] == {"large": "output"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_tool_returns_bounded_failed_internal_tool_calls():
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
    session = SimpleNamespace(
        session_id="test-session",
        extensions={},
        data_manager=MagicMock(),
    )
    server._get_or_create_session = AsyncMock(return_value=session)
    server._inject_tool_proxies = AsyncMock()
    server._execute_code_with_tracing = AsyncMock(
        return_value=CodeExecutionResult(
            stdout="continued after a caught error\n",
            success=True,
            tool_calls=[
                ToolCallRecord(
                    tool_name="successful_tool",
                    args={"large": "input"},
                    result={"large": "output"},
                    duration_ms=10,
                ),
                ToolCallRecord(
                    tool_name="failed_tool",
                    args={"secret": "not returned"},
                    result={"large": "not returned"},
                    duration_ms=20,
                    success=False,
                    error="ValueError: invalid input",
                ),
                ToolCallRecord(
                    tool_name="domain_failure",
                    args={"secret": "also not returned"},
                    result={"success": False, "error": "Invalid split specification"},
                    duration_ms=30,
                    success=True,
                ),
            ],
        )
    )
    server.session_manager.session_resource_operation = MagicMock(return_value=_SessionResourceOperation())
    server.session_manager.update_session = MagicMock()
    server.activity_publisher.publish_nowait = MagicMock()

    async with Client(server.mcp) as client:
        result = await client.call_tool(tool_name, {"code": "run_tools()"})

    payload = json.loads(result.content[0].text)
    assert payload["success"] is True
    assert "tool_calls" not in payload
    assert payload["failed_tool_call_count"] == 2
    assert payload["failed_tool_calls_truncated"] is False
    assert payload["failed_tool_calls"] == [
        {
            "first_call_index": 2,
            "tool_name": "failed_tool",
            "error": "ValueError: invalid input",
            "occurrences": 1,
        },
        {
            "first_call_index": 3,
            "tool_name": "domain_failure",
            "error": "Invalid split specification",
            "occurrences": 1,
        },
    ]
    serialized = json.dumps(payload)
    assert "not returned" not in serialized
    assert "secret" not in serialized


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_tool_bounds_and_deduplicates_failed_internal_tool_calls():
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
    session = SimpleNamespace(
        session_id="test-session",
        extensions={},
        data_manager=MagicMock(),
    )
    server._get_or_create_session = AsyncMock(return_value=session)
    server._inject_tool_proxies = AsyncMock()
    repeated_error = "x" * 700
    server._execute_code_with_tracing = AsyncMock(
        return_value=CodeExecutionResult(
            success=True,
            tool_calls=[
                ToolCallRecord(
                    tool_name="repeated_failure",
                    result={"success": False, "error": repeated_error},
                ),
                ToolCallRecord(
                    tool_name="repeated_failure",
                    result={"success": False, "error": repeated_error},
                ),
                *[
                    ToolCallRecord(
                        tool_name=f"failure_{index}",
                        success=False,
                        error=f"failure {index}",
                    )
                    for index in range(9)
                ],
            ],
        )
    )
    server.session_manager.session_resource_operation = MagicMock(return_value=_SessionResourceOperation())
    server.session_manager.update_session = MagicMock()
    server.activity_publisher.publish_nowait = MagicMock()

    async with Client(server.mcp) as client:
        result = await client.call_tool(tool_name, {"code": "run_tools()"})

    payload = json.loads(result.content[0].text)
    assert payload["failed_tool_call_count"] == 11
    assert payload["failed_tool_calls_truncated"] is True
    assert len(payload["failed_tool_calls"]) == 8
    assert payload["failed_tool_calls"][0]["occurrences"] == 2
    assert payload["failed_tool_calls"][0]["error"].endswith("... [truncated]")
    assert len(payload["failed_tool_calls"][0]["error"]) == 500


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_tool_does_not_deduplicate_distinct_truncated_errors():
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
    session = SimpleNamespace(
        session_id="test-session",
        extensions={},
        data_manager=MagicMock(),
    )
    server._get_or_create_session = AsyncMock(return_value=session)
    server._inject_tool_proxies = AsyncMock()
    shared_prefix = "x" * 600
    server._execute_code_with_tracing = AsyncMock(
        return_value=CodeExecutionResult(
            success=True,
            tool_calls=[
                ToolCallRecord(tool_name="failed_tool", success=False, error=shared_prefix + " first"),
                ToolCallRecord(tool_name="failed_tool", success=False, error=shared_prefix + " second"),
            ],
        )
    )
    server.session_manager.session_resource_operation = MagicMock(return_value=_SessionResourceOperation())
    server.session_manager.update_session = MagicMock()
    server.activity_publisher.publish_nowait = MagicMock()

    async with Client(server.mcp) as client:
        result = await client.call_tool(tool_name, {"code": "run_tools()"})

    payload = json.loads(result.content[0].text)
    assert payload["failed_tool_call_count"] == 2
    assert len(payload["failed_tool_calls"]) == 2
    assert payload["failed_tool_calls_truncated"] is False
