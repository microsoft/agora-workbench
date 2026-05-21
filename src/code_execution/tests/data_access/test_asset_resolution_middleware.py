"""
Unit tests for AssetResolutionMiddleware.

Tests the middleware's ability to detect, resolve, and inject asset references
before FastMCP/Pydantic validation runs.
"""

import importlib
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from ...code_execution.data_access.resolution import (
    AssetResolutionMiddleware,
    _resolved_assets,
)
from ...code_execution.sessions import Session


def _patch_set_current_session():
    package_name = importlib.import_module(AssetResolutionMiddleware.__module__).__package__
    sessions_module = importlib.import_module(f"{package_name.rsplit('.', 1)[0]}.sessions")
    return patch.object(sessions_module, "set_current_session")


@pytest.fixture
def mock_server():
    """Create a mock CodeExecutionServer for testing."""
    server = MagicMock()
    server._restore_auth_context_for_mcp_session = MagicMock()
    server._clear_auth_context = MagicMock()
    server._get_or_create_session = AsyncMock()
    return server


@pytest.fixture
def mock_context():
    """Create a mock MiddlewareContext for testing."""
    context = MagicMock()
    context.message = MagicMock()
    context.message.name = "test_tool"
    context.message.arguments = {}
    context.fastmcp_context = MagicMock()
    context.fastmcp_context.session_id = "test-session-123"
    return context


@pytest.fixture
def mock_session():
    """Create a mock Session with data manager."""
    session = MagicMock(spec=Session)
    session.session_id = "test-session-123"
    session.data = {}
    session._asset_counter = 0
    session.object_store = MagicMock()
    session.object_store.store = MagicMock()
    session.data_manager = MagicMock()
    session.data_manager.get_cache_path = AsyncMock()
    return session


@pytest.mark.asyncio
class TestAssetResolutionMiddleware:
    """Test suite for AssetResolutionMiddleware."""

    async def test_no_assets_to_resolve(self, mock_server, mock_context):
        """Test that middleware passes through when no assets need resolution."""
        mock_context.message.arguments = {
            "param1": "regular_string",
            "param2": 123,
            "param3": True,
        }

        middleware = AssetResolutionMiddleware(mock_server)
        call_next = AsyncMock(return_value="result")

        result = await middleware.on_call_tool(mock_context, call_next)

        assert result == "result"
        call_next.assert_called_once_with(mock_context)
        assert _resolved_assets.get() == []

    async def test_single_asset_resolution(self, mock_server, mock_context, mock_session):
        """Test resolution of a single asset parameter."""
        mock_context.message.arguments = {
            "grid_file": "<blob>aHR0cHM6Ly9ncmlk</blob>",
            "other_param": "value",
        }

        mock_server._get_or_create_session.return_value = mock_session
        mock_session.data_manager.get_cache_path.return_value = Path("/cache/grid.nc")

        middleware = AssetResolutionMiddleware(mock_server)
        call_next = AsyncMock(return_value="result")

        with _patch_set_current_session():
            result = await middleware.on_call_tool(mock_context, call_next)

        assert result == "result"

        # Verify asset was resolved
        mock_session.data_manager.get_cache_path.assert_called_once_with("<blob>aHR0cHM6Ly9ncmlk</blob>")

        # Verify argument was replaced with cache path
        assert mock_context.message.arguments["grid_file"] == "/cache/grid.nc"
        assert mock_context.message.arguments["other_param"] == "value"

        # Verify metadata was stored
        resolved = _resolved_assets.get()
        assert len(resolved) == 1
        assert resolved[0][0] == "grid_file"  # param_name

    async def test_multiple_assets_resolution(self, mock_server, mock_context, mock_session):
        """Test resolution of multiple asset parameters in a single call."""
        mock_context.message.arguments = {
            "grid_file": "<blob>aHR0cHM6Ly9ncmlk</blob>",
            "config_file": "<blob>Y29uZmlnLmpzb24=</blob>",
            "regular_param": "not_an_asset",
        }

        mock_server._get_or_create_session.return_value = mock_session

        # Mock returns paths based on which parameter is being resolved
        cache_paths_by_id = {
            "<blob>aHR0cHM6Ly9ncmlk</blob>": Path("/cache/grid.nc"),
            "<blob>Y29uZmlnLmpzb24=</blob>": Path("/cache/config.json"),
        }

        async def mock_get_cache_path(asset_id):
            return cache_paths_by_id.get(asset_id, Path("/cache/unknown.nc"))

        mock_session.data_manager.get_cache_path.side_effect = mock_get_cache_path

        middleware = AssetResolutionMiddleware(mock_server)
        call_next = AsyncMock(return_value="result")

        with _patch_set_current_session():
            result = await middleware.on_call_tool(mock_context, call_next)

        assert result == "result"

        # Verify both assets were resolved
        assert mock_session.data_manager.get_cache_path.call_count == 2

        # Verify arguments were replaced
        assert mock_context.message.arguments["grid_file"] == "/cache/grid.nc"
        assert mock_context.message.arguments["config_file"] == "/cache/config.json"
        assert mock_context.message.arguments["regular_param"] == "not_an_asset"

        # Verify metadata for both assets
        resolved = _resolved_assets.get()
        assert len(resolved) == 2

    async def test_asset_resolution_failure(self, mock_server, mock_context, mock_session):
        """Test proper error handling when asset resolution fails."""
        mock_context.message.arguments = {
            "grid_file": "<blob>invalid_asset_id</blob>",
        }

        mock_server._get_or_create_session.return_value = mock_session
        mock_session.data_manager.get_cache_path.side_effect = Exception("Asset not found")

        middleware = AssetResolutionMiddleware(mock_server)
        call_next = AsyncMock()

        with _patch_set_current_session():
            with pytest.raises(RuntimeError, match="Failed to resolve DataLake asset"):
                await middleware.on_call_tool(mock_context, call_next)

        # Verify cleanup was called
        mock_server._clear_auth_context.assert_called_once()

    async def test_different_asset_types(self, mock_server, mock_context, mock_session):
        """Test resolution of different asset types (blob, sql, etc.)."""
        mock_context.message.arguments = {
            "blob_file": "<blob>YmxvYl9pZA==</blob>",
            "sql_query": "<sql>c3FsX3F1ZXJ5X2lk</sql>",
            "delta_table": "<delta>ZGVsdGFfdGFibGU=</delta>",
        }

        mock_server._get_or_create_session.return_value = mock_session

        cache_paths = {
            "<blob>YmxvYl9pZA==</blob>": Path("/cache/blob.nc"),
            "<sql>c3FsX3F1ZXJ5X2lk</sql>": Path("/cache/query_results.csv"),
            "<delta>ZGVsdGFfdGFibGU=</delta>": Path("/cache/delta_table.parquet"),
        }

        async def mock_get_cache_path(asset_id):
            return cache_paths[asset_id]

        mock_session.data_manager.get_cache_path.side_effect = mock_get_cache_path

        middleware = AssetResolutionMiddleware(mock_server)
        call_next = AsyncMock(return_value="result")

        with _patch_set_current_session():
            result = await middleware.on_call_tool(mock_context, call_next)

        assert result == "result"

        # Verify all asset types were resolved
        assert mock_context.message.arguments["blob_file"] == "/cache/blob.nc"
        assert mock_context.message.arguments["sql_query"] == "/cache/query_results.csv"
        assert mock_context.message.arguments["delta_table"] == "/cache/delta_table.parquet"

    async def test_session_setup_and_cleanup(self, mock_server, mock_context, mock_session):
        """Test that session and auth context are properly set up and cleaned up."""
        mock_context.message.arguments = {"param": "<blob>test</blob>"}

        mock_server._get_or_create_session.return_value = mock_session
        mock_session.data_manager.get_cache_path.return_value = Path("/cache/test")

        middleware = AssetResolutionMiddleware(mock_server)
        call_next = AsyncMock(return_value="result")

        with _patch_set_current_session() as mock_set_session:
            await middleware.on_call_tool(mock_context, call_next)

        # Verify session was set and cleared
        assert mock_set_session.call_count == 2
        mock_set_session.assert_any_call(mock_session)
        mock_set_session.assert_any_call(None)

        # Verify auth context was restored and cleared
        mock_server._restore_auth_context_for_mcp_session.assert_called_once()
        mock_server._clear_auth_context.assert_called_once()

    async def test_empty_arguments(self, mock_server, mock_context):
        """Test handling of None or empty arguments."""
        mock_context.message.arguments = None

        middleware = AssetResolutionMiddleware(mock_server)
        call_next = AsyncMock(return_value="result")

        result = await middleware.on_call_tool(mock_context, call_next)

        assert result == "result"
        assert _resolved_assets.get() == []

    async def test_session_id_extraction(self, mock_server, mock_context, mock_session):
        """Test extraction of session ID from FastMCP context."""
        mock_context.message.arguments = {"asset": "<blob>test</blob>"}
        mock_context.fastmcp_context.session_id = "custom-session-456"

        mock_server._get_or_create_session.return_value = mock_session
        mock_session.data_manager.get_cache_path.return_value = Path("/cache/test")

        middleware = AssetResolutionMiddleware(mock_server)
        call_next = AsyncMock(return_value="result")

        with _patch_set_current_session():
            await middleware.on_call_tool(mock_context, call_next)

        # Verify session was created with correct session_id
        mock_server._restore_auth_context_for_mcp_session.assert_called_once_with("custom-session-456")

    async def test_asset_counter_increments(self, mock_server, mock_context, mock_session):
        """Test that asset counter increments for each resolution."""
        mock_context.message.arguments = {
            "asset1": "<blob>first</blob>",
            "asset2": "<blob>second</blob>",
        }

        mock_server._get_or_create_session.return_value = mock_session
        mock_session.data_manager.get_cache_path.return_value = Path("/cache/file")

        middleware = AssetResolutionMiddleware(mock_server)
        call_next = AsyncMock(return_value="result")

        with _patch_set_current_session():
            await middleware.on_call_tool(mock_context, call_next)

        # Verify counter was incremented twice
        assert mock_session._asset_counter == 2

    async def test_object_store_metadata(self, mock_server, mock_context, mock_session):
        """Test that asset metadata is stored in session object store."""
        asset_id = "<blob>test_asset_id</blob>"
        cache_path = "/cache/test.nc"

        mock_context.message.arguments = {"grid": asset_id}
        mock_server._get_or_create_session.return_value = mock_session
        mock_session.data_manager.get_cache_path.return_value = Path(cache_path)

        middleware = AssetResolutionMiddleware(mock_server)
        call_next = AsyncMock(return_value="result")

        with _patch_set_current_session():
            await middleware.on_call_tool(mock_context, call_next)

        # Verify metadata was stored
        mock_session.object_store.store.assert_called_once()
        call_args = mock_session.object_store.store.call_args

        # Check that the stored metadata contains qualified_name and cache_path
        stored_key = call_args[0][0]
        stored_metadata = call_args[0][1]

        assert stored_key.startswith("_asset_grid_")
        assert stored_metadata["qualified_name"] == asset_id
        assert stored_metadata["cache_path"] == cache_path

    async def test_arguments_copy_not_mutated(self, mock_server, mock_context, mock_session):
        """Test that original arguments are copied before mutation."""
        original_args = {
            "asset": "<blob>test</blob>",
            "other": "value",
        }
        mock_context.message.arguments = original_args.copy()

        mock_server._get_or_create_session.return_value = mock_session
        mock_session.data_manager.get_cache_path.return_value = Path("/cache/test")

        middleware = AssetResolutionMiddleware(mock_server)
        call_next = AsyncMock(return_value="result")

        with _patch_set_current_session():
            await middleware.on_call_tool(mock_context, call_next)

        # Verify context.message.arguments was updated (not the original)
        assert mock_context.message.arguments["asset"] == "/cache/test"
        assert mock_context.message.arguments["other"] == "value"
