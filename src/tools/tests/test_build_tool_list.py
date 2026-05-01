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
    async def test_build_tool_list_connection_failure(self):
        """Test build_tool_list handles connection failures gracefully."""
        mock_descriptor = MagicMock()
        mock_descriptor.name = "powergrid"
        mock_descriptor.url = "http://localhost:8000/mcp"
        mock_descriptor.scope = "test-scope"

        mock_registry = MagicMock()
        mock_registry.list_servers.return_value = {"powergrid": mock_descriptor}

        with (
            patch("tools.search.build_tool_list.get_mcp_registry", return_value=mock_registry),
            patch("tools.search.build_tool_list.streamablehttp_client", side_effect=Exception("Connection refused")),
            patch("tools.search.build_tool_list.create_entra_token_provider", return_value=lambda: "token"),
        ):
            result = await build_tool_list()

        assert result == []
