"""Tests for build_tool_list function."""

import json

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from tools.search.build_tool_list import build_tool_list, ToolInfo, _is_meta_tool


class TestToolInfo:
    """Test cases for ToolInfo dataclass."""

    @pytest.mark.unit
    def test_tool_info_creation(self):
        """Test ToolInfo can be created with required fields."""
        info = ToolInfo(name="run_opf", description="Run optimal power flow", server_name="powergrid")
        assert info.name == "run_opf"
        assert info.description == "Run optimal power flow"
        assert info.server_name == "powergrid"

    @pytest.mark.unit
    def test_tool_info_frozen(self):
        """Test ToolInfo is immutable."""
        info = ToolInfo(name="run_opf", description="Run optimal power flow", server_name="powergrid")
        with pytest.raises(AttributeError):
            info.name = "other"

    @pytest.mark.unit
    def test_tool_info_equality(self):
        """Test ToolInfo equality comparison."""
        info1 = ToolInfo(name="run_opf", description="Run optimal power flow", server_name="powergrid")
        info2 = ToolInfo(name="run_opf", description="Run optimal power flow", server_name="powergrid")
        assert info1 == info2


class TestIsMetaTool:
    """Test cases for _is_meta_tool heuristic."""

    @pytest.mark.unit
    def test_execute_code_is_meta(self):
        assert _is_meta_tool("execute_powergrid_code") is True

    @pytest.mark.unit
    def test_list_sessions_is_meta(self):
        assert _is_meta_tool("powergrid_list_sessions") is True

    @pytest.mark.unit
    def test_get_session_info_is_meta(self):
        assert _is_meta_tool("powergrid_get_session_info") is True

    @pytest.mark.unit
    def test_close_session_is_meta(self):
        assert _is_meta_tool("powergrid_close_session") is True

    @pytest.mark.unit
    def test_list_domain_tools_is_meta(self):
        assert _is_meta_tool("list_powergrid_domain_tools") is True

    @pytest.mark.unit
    def test_domain_tool_is_not_meta(self):
        assert _is_meta_tool("run_opf") is False

    @pytest.mark.unit
    def test_domain_tool_with_prefix_but_wrong_suffix_is_not_meta(self):
        assert _is_meta_tool("execute_simulation") is False

    @pytest.mark.unit
    def test_domain_tool_with_suffix_but_wrong_prefix_is_not_meta(self):
        assert _is_meta_tool("run_code") is False

    @pytest.mark.unit
    def test_wrong_session_name_pattern_list(self):
        """Verify the old-style list_<server>_sessions pattern is NOT treated as meta."""
        assert _is_meta_tool("list_powergrid_sessions") is False

    @pytest.mark.unit
    def test_wrong_session_name_pattern_get(self):
        """Verify the old-style get_<server>_session_info pattern is NOT treated as meta."""
        assert _is_meta_tool("get_powergrid_session_info") is False

    @pytest.mark.unit
    def test_wrong_session_name_pattern_close(self):
        """Verify the old-style close_<server>_session pattern is NOT treated as meta."""
        assert _is_meta_tool("close_powergrid_session") is False


class TestBuildToolList:
    """Test cases for build_tool_list function."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_build_tool_list_no_servers(self):
        """Test build_tool_list returns empty list when no servers registered."""
        mock_registry = MagicMock()
        mock_registry.list_servers.return_value = {}

        with patch("tools.search.build_tool_list.get_mcp_registry", return_value=mock_registry):
            result = await build_tool_list()

        assert result == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_build_tool_list_via_meta_tool(self):
        """Test build_tool_list discovers tools via the meta-tool when available."""
        catalog = [
            {"name": "run_opf", "description": "Run optimal power flow", "server_name": "powergrid"},
            {"name": "build_network", "description": "Build network topology", "server_name": "powergrid"},
        ]

        # Meta-tool function present in the functions list
        meta_func = MagicMock()
        meta_func.name = "list_powergrid_domain_tools"

        mock_mcp_tool = MagicMock()
        mock_mcp_tool.is_connected = True
        mock_mcp_tool.load_tools = AsyncMock()
        mock_mcp_tool.functions = [meta_func]
        mock_mcp_tool.call_tool = AsyncMock(return_value=json.dumps(catalog))

        mock_descriptor = MagicMock()
        mock_descriptor.name = "powergrid"

        mock_registry = MagicMock()
        mock_registry.list_servers.return_value = {"powergrid": mock_descriptor}
        mock_registry.get_mcp_tool.return_value = mock_mcp_tool

        with patch("tools.search.build_tool_list.get_mcp_registry", return_value=mock_registry):
            result = await build_tool_list()

        assert len(result) == 2
        assert result[0] == ToolInfo(name="run_opf", description="Run optimal power flow", server_name="powergrid")
        assert result[1] == ToolInfo(
            name="build_network", description="Build network topology", server_name="powergrid"
        )
        mock_mcp_tool.call_tool.assert_called_once_with(tool_name="list_powergrid_domain_tools")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_build_tool_list_fallback_filters_meta_tools(self):
        """Test fallback path filters out infrastructure/meta tools."""
        domain_func = MagicMock()
        domain_func.name = "run_opf"
        domain_func.description = "Run optimal power flow"

        execute_func = MagicMock()
        execute_func.name = "execute_powergrid_code"
        execute_func.description = "Execute code"

        list_sessions_func = MagicMock()
        list_sessions_func.name = "powergrid_list_sessions"
        list_sessions_func.description = "List sessions"

        mock_mcp_tool = MagicMock()
        mock_mcp_tool.is_connected = True
        mock_mcp_tool.load_tools = AsyncMock()
        mock_mcp_tool.functions = [domain_func, execute_func, list_sessions_func]

        mock_descriptor = MagicMock()
        mock_descriptor.name = "powergrid"

        mock_registry = MagicMock()
        mock_registry.list_servers.return_value = {"powergrid": mock_descriptor}
        mock_registry.get_mcp_tool.return_value = mock_mcp_tool

        with patch("tools.search.build_tool_list.get_mcp_registry", return_value=mock_registry):
            result = await build_tool_list()

        # Only the domain tool should remain; infrastructure tools filtered out
        assert len(result) == 1
        assert result[0].name == "run_opf"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_build_tool_list_already_connected(self):
        """Test build_tool_list skips connect but still calls load_tools for already-connected servers."""
        mock_func = MagicMock()
        mock_func.name = "run_opf"
        mock_func.description = "Run optimal power flow"

        mock_mcp_tool = MagicMock()
        mock_mcp_tool.is_connected = True
        mock_mcp_tool.connect = AsyncMock()
        mock_mcp_tool.load_tools = AsyncMock()
        mock_mcp_tool.functions = [mock_func]

        mock_descriptor = MagicMock()
        mock_descriptor.name = "powergrid"

        mock_registry = MagicMock()
        mock_registry.list_servers.return_value = {"powergrid": mock_descriptor}
        mock_registry.get_mcp_tool.return_value = mock_mcp_tool

        with patch("tools.search.build_tool_list.get_mcp_registry", return_value=mock_registry):
            result = await build_tool_list()

        assert len(result) == 1
        mock_mcp_tool.connect.assert_not_called()
        mock_mcp_tool.load_tools.assert_called_once()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_build_tool_list_multiple_servers(self):
        """Test build_tool_list discovers tools from multiple servers."""
        mock_func1 = MagicMock()
        mock_func1.name = "run_opf"
        mock_func1.description = "Run optimal power flow"

        mock_func2 = MagicMock()
        mock_func2.name = "analyze_data"
        mock_func2.description = "Analyze data"

        mock_mcp_tool1 = MagicMock()
        mock_mcp_tool1.is_connected = True
        mock_mcp_tool1.load_tools = AsyncMock()
        mock_mcp_tool1.functions = [mock_func1]

        mock_mcp_tool2 = MagicMock()
        mock_mcp_tool2.is_connected = True
        mock_mcp_tool2.load_tools = AsyncMock()
        mock_mcp_tool2.functions = [mock_func2]

        mock_desc1 = MagicMock()
        mock_desc1.name = "powergrid"
        mock_desc2 = MagicMock()
        mock_desc2.name = "process"

        mock_registry = MagicMock()
        mock_registry.list_servers.return_value = {
            "powergrid": mock_desc1,
            "process": mock_desc2,
        }

        def get_mcp_tool(name):
            if name == "powergrid":
                return mock_mcp_tool1
            return mock_mcp_tool2

        mock_registry.get_mcp_tool.side_effect = get_mcp_tool

        with patch("tools.search.build_tool_list.get_mcp_registry", return_value=mock_registry):
            result = await build_tool_list()

        assert len(result) == 2
        server_names = {r.server_name for r in result}
        assert server_names == {"powergrid", "process"}

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_build_tool_list_connection_failure(self):
        """Test build_tool_list handles connection failures gracefully."""
        mock_mcp_tool = MagicMock()
        mock_mcp_tool.is_connected = False
        mock_mcp_tool.connect = AsyncMock(side_effect=Exception("Connection refused"))

        mock_registry = MagicMock()
        mock_registry.list_servers.return_value = {"powergrid": MagicMock()}
        mock_registry.get_mcp_tool.return_value = mock_mcp_tool

        with patch("tools.search.build_tool_list.get_mcp_registry", return_value=mock_registry):
            result = await build_tool_list()

        assert result == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_build_tool_list_no_mcp_tool(self):
        """Test build_tool_list handles missing MCPStreamableHTTPTool."""
        mock_registry = MagicMock()
        mock_registry.list_servers.return_value = {"powergrid": MagicMock()}
        mock_registry.get_mcp_tool.return_value = None

        with patch("tools.search.build_tool_list.get_mcp_registry", return_value=mock_registry):
            result = await build_tool_list()

        assert result == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_build_tool_list_empty_description(self):
        """Test build_tool_list handles tools with empty description."""
        mock_func = MagicMock()
        mock_func.name = "run_opf"
        mock_func.description = None

        mock_mcp_tool = MagicMock()
        mock_mcp_tool.is_connected = True
        mock_mcp_tool.load_tools = AsyncMock()
        mock_mcp_tool.functions = [mock_func]

        mock_descriptor = MagicMock()
        mock_descriptor.name = "powergrid"

        mock_registry = MagicMock()
        mock_registry.list_servers.return_value = {"powergrid": mock_descriptor}
        mock_registry.get_mcp_tool.return_value = mock_mcp_tool

        with patch("tools.search.build_tool_list.get_mcp_registry", return_value=mock_registry):
            result = await build_tool_list()

        assert len(result) == 1
        assert result[0].description == ""
