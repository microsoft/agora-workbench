"""Tests for Azure AI Search-based tool search."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tools.search.azure_ai_tool_search import (
    AzureAIToolSearchBackend,
    ToolSearchClientManager,
    create_and_setup_azure_ai_tool_search,
)


# ---------------------------------------------------------------------------
# ToolSearchClientManager
# ---------------------------------------------------------------------------


class TestToolSearchClientManager:
    @pytest.mark.unit
    def test_get_endpoint_raises_when_missing(self, monkeypatch):
        monkeypatch.delenv("TOOL_SEARCH_ENDPOINT", raising=False)
        with pytest.raises(ValueError, match="TOOL_SEARCH_ENDPOINT"):
            ToolSearchClientManager._get_endpoint()

    @pytest.mark.unit
    def test_get_endpoint_returns_value(self, monkeypatch):
        monkeypatch.setenv("TOOL_SEARCH_ENDPOINT", "https://example.search.windows.net")
        endpoint = ToolSearchClientManager._get_endpoint()
        assert endpoint == "https://example.search.windows.net"

    @pytest.mark.unit
    def test_stores_index_name(self):
        manager = ToolSearchClientManager("my-tools")
        assert manager._index_name == "my-tools"

    @pytest.mark.unit
    def test_uses_injected_credential(self, monkeypatch):
        """When a credential is injected, SearchClient uses it instead of creating its own."""
        monkeypatch.setenv("TOOL_SEARCH_ENDPOINT", "https://example.search.windows.net")
        cred = MagicMock()
        manager = ToolSearchClientManager("my-tools", credential=cred)
        assert manager._credential is cred
        assert manager._external_credential is True

    @pytest.mark.unit
    def test_default_credential_is_internal(self):
        """Without an injected credential, manager creates its own."""
        manager = ToolSearchClientManager("my-tools")
        assert manager._credential is None
        assert manager._external_credential is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_close_does_not_close_external_credential(self):
        """An injected credential must not be closed by the manager."""
        cred = MagicMock()
        cred.close = AsyncMock()
        manager = ToolSearchClientManager("my-tools", credential=cred)
        await manager.close()
        cred.close.assert_not_awaited()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_get_client_caches_instance(self, monkeypatch):
        monkeypatch.setenv("TOOL_SEARCH_ENDPOINT", "https://example.search.windows.net")
        manager = ToolSearchClientManager("test-index")
        mock_client = MagicMock()
        with patch.object(manager, "_create_client", return_value=mock_client):
            c1 = await manager.get_client()
            c2 = await manager.get_client()
            assert c1 is c2

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_search_retries_on_auth_error(self, monkeypatch):
        """Search recreates client and retries once on 401/403."""
        monkeypatch.setenv("TOOL_SEARCH_ENDPOINT", "https://example.search.windows.net")
        from azure.core.exceptions import HttpResponseError

        manager = ToolSearchClientManager("test-index")

        auth_error = HttpResponseError(message="Unauthorized")
        auth_error.status_code = 401

        fresh_client = MagicMock()
        fresh_iter = AsyncMock()
        fresh_client.search = AsyncMock(return_value=fresh_iter)

        call_count = 0

        def patched_create():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                bad_client = MagicMock()
                bad_client.search = AsyncMock(side_effect=auth_error)
                bad_client.close = AsyncMock()
                return bad_client
            return fresh_client

        with patch.object(manager, "_create_client", side_effect=patched_create):
            result = await manager.search(search_text="power flow")

        assert call_count == 2
        assert result is fresh_iter


# ---------------------------------------------------------------------------
# create_and_setup_azure_ai_tool_search
# ---------------------------------------------------------------------------


class TestCreateAndSetupToolSearch:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_returns_backend_and_manager(self):
        from tools.search.build_tool_list import ToolInfo

        mock_tools = [
            ToolInfo(name="run_opf", description="Run OPF", server_name="powergrid"),
        ]

        mock_manager = MagicMock()
        mock_manager.index_name = "tool-registry-abc12345"
        mock_manager.setup = AsyncMock()

        with (
            patch(
                "tools.search.azure_ai_tool_search.build_tool_list",
                new=AsyncMock(return_value=mock_tools),
            ),
            patch("tools.search.azure_ai_tool_search.ToolSearchIndexManager") as MockManagerClass,
            patch(
                "tools.search.azure_ai_tool_search.get_search_credential_async",
                return_value=MagicMock(),
            ),
        ):
            MockManagerClass.from_env.return_value = mock_manager

            backend, manager = await create_and_setup_azure_ai_tool_search()

        assert isinstance(backend, AzureAIToolSearchBackend)
        assert manager is mock_manager
        mock_manager.setup.assert_awaited_once_with(mock_tools)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_propagates_setup_errors(self):
        from tools.search.build_tool_list import ToolInfo

        mock_tools = [
            ToolInfo(name="run_opf", description="Run OPF", server_name="powergrid"),
        ]

        mock_manager = MagicMock()
        mock_manager.setup = AsyncMock(side_effect=Exception("deploy failed"))

        with (
            patch(
                "tools.search.azure_ai_tool_search.build_tool_list",
                new=AsyncMock(return_value=mock_tools),
            ),
            patch("tools.search.azure_ai_tool_search.ToolSearchIndexManager") as MockManagerClass,
        ):
            MockManagerClass.from_env.return_value = mock_manager

            with pytest.raises(Exception, match="deploy failed"):
                await create_and_setup_azure_ai_tool_search()
