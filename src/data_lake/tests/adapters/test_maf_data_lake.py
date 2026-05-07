"""Tests for DataLake catalog integration."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("agent_framework")


from data_lake.tools.adapters.local import LocalDataLakeSearchBackend
from data_lake.tools.adapters.maf import (
    DataLakeSearchBackend,
    DataLakeSearchClientManager,
    DataLakeSearchParams,
    DefaultDataLakeSearchBackend,
    _build_search_params_model,
    _discover_available_domains,
    create_data_lake_search_tool,
    is_data_lake_configured,
)


class TestIsDataLakeConfigured:
    """Test cases for is_data_lake_configured."""

    @pytest.mark.unit
    def test_returns_true_when_endpoint_set(self, monkeypatch):
        """Test returns True when DATA_LAKE_SEARCH_ENDPOINT is set."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")
        assert is_data_lake_configured() is True

    @pytest.mark.unit
    def test_returns_false_when_endpoint_not_set(self, monkeypatch):
        """Test returns False when DATA_LAKE_SEARCH_ENDPOINT is not set."""
        monkeypatch.delenv("DATA_LAKE_SEARCH_ENDPOINT", raising=False)
        assert is_data_lake_configured() is False

    @pytest.mark.unit
    def test_returns_false_when_endpoint_empty(self, monkeypatch):
        """Test returns False when DATA_LAKE_SEARCH_ENDPOINT is empty string."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "")
        monkeypatch.delenv("DATA_LAKE_LOCAL_CATALOG", raising=False)
        assert is_data_lake_configured() is False

    @pytest.mark.unit
    def test_returns_true_when_local_catalog_set(self, monkeypatch):
        """Test returns True when DATA_LAKE_LOCAL_CATALOG is set."""
        monkeypatch.delenv("DATA_LAKE_SEARCH_ENDPOINT", raising=False)
        monkeypatch.setenv("DATA_LAKE_LOCAL_CATALOG", "/tmp/catalog.yaml")
        assert is_data_lake_configured() is True

    @pytest.mark.unit
    def test_returns_false_when_local_catalog_empty(self, monkeypatch):
        """Test returns False when DATA_LAKE_LOCAL_CATALOG is empty string."""
        monkeypatch.delenv("DATA_LAKE_SEARCH_ENDPOINT", raising=False)
        monkeypatch.setenv("DATA_LAKE_LOCAL_CATALOG", "")
        assert is_data_lake_configured() is False


class TestGetDataLakeConfig:
    """Test cases for DataLakeSearchClientManager._get_data_lake_config."""

    @pytest.mark.unit
    def test_returns_config_with_defaults(self, monkeypatch):
        """Test returns config tuple with default index name."""
        endpoint = "https://test.search.windows.net"
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", endpoint)
        monkeypatch.delenv("DATA_LAKE_CATALOG_INDEX_NAME", raising=False)

        result = DataLakeSearchClientManager._get_data_lake_config()

        assert result == (endpoint, "artifact-registry")

    @pytest.mark.unit
    def test_returns_config_with_custom_index(self, monkeypatch):
        """Test returns config tuple with custom index name."""
        endpoint = "https://test.search.windows.net"
        index_name = "custom_catalog"
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", endpoint)
        monkeypatch.setenv("DATA_LAKE_CATALOG_INDEX_NAME", index_name)

        result = DataLakeSearchClientManager._get_data_lake_config()

        assert result == (endpoint, index_name)

    @pytest.mark.unit
    def test_raises_error_when_not_configured(self, monkeypatch):
        """Test raises ValueError when DATA_LAKE_SEARCH_ENDPOINT is not set."""
        monkeypatch.delenv("DATA_LAKE_SEARCH_ENDPOINT", raising=False)

        with pytest.raises(ValueError, match="DataLake catalog not configured"):
            DataLakeSearchClientManager._get_data_lake_config()


class TestCreateDataLakeSearchTool:
    """Test cases for create_data_lake_search_tool."""

    @pytest.fixture(autouse=True)
    def _mock_domain_discovery(self):
        with patch("data_lake.tools.adapters.maf._discover_available_domains", new_callable=AsyncMock, return_value=[]):
            yield

    @pytest.mark.asyncio
    @pytest.mark.unit
    @patch("data_lake.tools.adapters.maf.SearchClient")
    @patch("data_lake.tools.adapters.maf.get_search_credential_async")
    async def test_creates_tool_with_credential(self, mock_create_cred, mock_search_client, monkeypatch):
        """Test creates tool using search credential from providers."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")
        monkeypatch.delenv("DATA_LAKE_CATALOG_INDEX_NAME", raising=False)
        mock_cred = MagicMock()
        mock_create_cred.return_value = mock_cred
        mock_client = MagicMock()
        mock_search_client.return_value = mock_client

        # Mock async search results
        async def mock_search_results(*args, **kwargs):
            yield {"name": "test_asset", "description": "Test asset"}

        mock_client.search = AsyncMock(return_value=mock_search_results())

        tool = await create_data_lake_search_tool()

        # Verify tool is callable
        assert callable(tool)

        # Credentials and client should NOT be created yet (lazy initialization)
        mock_create_cred.assert_not_called()
        mock_search_client.assert_not_called()

        # Now call the tool - this should trigger credential/client creation
        params = DataLakeSearchParams(query="test query")
        await tool(params)

        # Now verify credential and client were created
        mock_create_cred.assert_called_once()
        mock_search_client.assert_called_once_with(
            endpoint="https://test.search.windows.net",
            index_name="artifact-registry",
            credential=mock_cred,
        )

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_raises_error_when_not_configured(self, monkeypatch):
        """Test raises ValueError when DataLake is not configured and tool is used."""
        monkeypatch.delenv("DATA_LAKE_SEARCH_ENDPOINT", raising=False)

        # Tool creation should succeed (lazy initialization)
        tool = await create_data_lake_search_tool()

        # Error should occur when actually trying to use the tool
        params = DataLakeSearchParams(query="test query")
        result = await tool(params)

        # Should return error in JSON format
        result_json = json.loads(result)
        assert isinstance(result_json, list)
        assert len(result_json) == 1
        assert "error" in result_json[0]
        assert "DataLake catalog not configured" in result_json[0]["error"]

    @pytest.mark.asyncio
    @pytest.mark.unit
    @patch("data_lake.tools.adapters.maf.SearchClient")
    @patch("data_lake.tools.adapters.maf.get_search_credential_async")
    async def test_raises_error_on_client_creation_failure(self, mock_create_cred, mock_search_client, monkeypatch):
        """Test returns error JSON when SearchClient creation fails."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")
        mock_cred = MagicMock()
        mock_create_cred.return_value = mock_cred
        mock_search_client.side_effect = Exception("Connection failed")

        # Tool creation should succeed
        tool = await create_data_lake_search_tool()

        # Error should be caught and returned as JSON when tool is used
        params = DataLakeSearchParams(query="test query")
        result = await tool(params)

        # Should return error in JSON format
        result_json = json.loads(result)
        assert isinstance(result_json, list)
        assert len(result_json) == 1
        assert "error" in result_json[0]
        assert "Connection failed" in result_json[0]["error"]


class TestDataLakeSearchTool:
    """Test cases for DataLake search tool functionality."""

    @pytest.fixture(autouse=True)
    def _mock_domain_discovery(self):
        with patch("data_lake.tools.adapters.maf._discover_available_domains", new_callable=AsyncMock, return_value=[]):
            yield

    @pytest.mark.asyncio
    @pytest.mark.unit
    @patch("data_lake.tools.adapters.maf.SearchClient")
    @patch("data_lake.tools.adapters.maf.get_search_credential_async")
    async def test_search_returns_results(self, mock_create_cred, mock_search_client, monkeypatch):
        """Test tool returns search results as JSON."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")

        # Mock search results
        mock_result1 = {
            "name": "test_table",
            "asset_type": "azure_sql_table",
            "description": "Test table",
        }
        mock_result2 = {
            "name": "test_file",
            "asset_type": "parquet",
            "description": "Test file",
        }

        # Create async iterator for results
        async def async_results():
            yield mock_result1
            yield mock_result2

        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=async_results())
        mock_search_client.return_value = mock_client
        mock_create_cred.return_value = MagicMock()

        # Create and invoke tool
        tool = await create_data_lake_search_tool()
        params = DataLakeSearchParams(query="test data")
        result = await tool(params)

        # Verify search was called with correct parameters
        mock_client.search.assert_called_once()
        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["search_text"] == "test data"
        assert call_kwargs["top"] == 20  # default
        assert call_kwargs["query_type"] == "semantic"

        # Verify result is valid JSON with both results
        result_data = json.loads(result)
        assert len(result_data) == 2
        assert result_data[0]["name"] == "test_table"
        assert result_data[1]["name"] == "test_file"

    @pytest.mark.asyncio
    @pytest.mark.unit
    @patch("data_lake.tools.adapters.maf.SearchClient")
    @patch("data_lake.tools.adapters.maf.get_search_credential_async")
    async def test_search_with_asset_type_filter(self, mock_create_cred, mock_search_client, monkeypatch):
        """Test tool applies asset type filter correctly."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")

        async def async_results():
            yield {"name": "test"}

        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=async_results())
        mock_search_client.return_value = mock_client
        mock_create_cred.return_value = MagicMock()

        # Create and invoke tool with asset types
        tool = await create_data_lake_search_tool()
        params = DataLakeSearchParams(query="test data", artifact_types=["azure_sql_table", "parquet"])
        await tool(params)

        # Verify filter was applied
        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["filter"] == "(artifact_type eq 'azure_sql_table' or artifact_type eq 'parquet')"

    @pytest.mark.asyncio
    @pytest.mark.unit
    @patch("data_lake.tools.adapters.maf.SearchClient")
    @patch("data_lake.tools.adapters.maf.get_search_credential_async")
    @patch("data_lake.tools.adapters.maf.check_resource_permissions")
    async def test_search_filters_results_by_user_permissions(
        self, mock_check_permissions, mock_create_cred, mock_search_client, monkeypatch
    ):
        """Test tool filters artifacts using per-resource permission checks."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")

        async def async_results():
            yield {"artifact_id": "a1", "name": "public", "rbacScope": None}
            yield {"artifact_id": "a2", "name": "allowed", "rbacScope": "/scope/allowed"}
            yield {"artifact_id": "a3", "name": "denied", "rbacScope": "/scope/denied"}

        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=async_results())
        mock_search_client.return_value = mock_client
        mock_create_cred.return_value = MagicMock()
        mock_check_permissions.side_effect = [True, False]

        tool = await create_data_lake_search_tool()
        params = DataLakeSearchParams(query="test data")
        result = await tool(params)

        result_data = json.loads(result)
        assert len(result_data) == 2
        assert {asset["artifact_id"] for asset in result_data} == {"a1", "a2"}
        assert mock_check_permissions.await_count == 2

    @pytest.mark.asyncio
    @pytest.mark.unit
    @patch("data_lake.tools.adapters.maf.SearchClient")
    @patch("data_lake.tools.adapters.maf.get_search_credential_async")
    @patch("data_lake.tools.adapters.maf.check_resource_permissions")
    async def test_search_excludes_asset_on_permission_check_exception(
        self, mock_check_permissions, mock_create_cred, mock_search_client, monkeypatch
    ):
        """Test tool excludes scoped assets when permission check raises errors."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")

        async def async_results():
            yield {"artifact_id": "a1", "name": "asset", "rbacScope": "/scope/error"}

        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=async_results())
        mock_search_client.return_value = mock_client
        mock_create_cred.return_value = MagicMock()
        mock_check_permissions.side_effect = RuntimeError("permission API error")

        tool = await create_data_lake_search_tool()
        params = DataLakeSearchParams(query="test data")
        result = await tool(params)

        assert json.loads(result) == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    @patch("data_lake.tools.adapters.maf.SearchClient")
    @patch("data_lake.tools.adapters.maf.get_search_credential_async")
    async def test_search_with_select_fields(self, mock_create_cred, mock_search_client, monkeypatch):
        """Test tool uses select_fields parameter."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")

        async def async_results():
            yield {"name": "test"}

        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=async_results())
        mock_search_client.return_value = mock_client
        mock_create_cred.return_value = MagicMock()

        # Create and invoke tool with specific fields
        tool = await create_data_lake_search_tool()
        params = DataLakeSearchParams(query="test data", select_fields=["name", "description", "owner"])
        await tool(params)

        # Verify select parameter was passed
        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["select"] == ["name", "description", "owner"]

    @pytest.mark.asyncio
    @pytest.mark.unit
    @patch("data_lake.tools.adapters.maf.SearchClient")
    @patch("data_lake.tools.adapters.maf.get_search_credential_async")
    async def test_search_defaults_to_all_fields(self, mock_create_cred, mock_search_client, monkeypatch):
        """Test tool defaults to all fields when select_fields not specified."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")

        async def async_results():
            yield {"name": "test"}

        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=async_results())
        mock_search_client.return_value = mock_client
        mock_create_cred.return_value = MagicMock()

        # Create and invoke tool without select_fields
        tool = await create_data_lake_search_tool()
        params = DataLakeSearchParams(query="test data")
        await tool(params)

        # Verify select=None (all fields)
        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["select"] is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    @patch("data_lake.tools.adapters.maf.SearchClient")
    @patch("data_lake.tools.adapters.maf.get_search_credential_async")
    async def test_search_with_order_by(self, mock_create_cred, mock_search_client, monkeypatch):
        """Test tool uses order_by parameter."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")

        async def async_results():
            yield {"name": "test"}

        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=async_results())
        mock_search_client.return_value = mock_client
        mock_create_cred.return_value = MagicMock()

        # Create and invoke tool with order_by
        tool = await create_data_lake_search_tool()
        params = DataLakeSearchParams(query="test data", order_by=["last_modified desc", "name asc"])
        await tool(params)

        # Verify order_by parameter was passed
        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["order_by"] == ["last_modified desc", "name asc"]

    @pytest.mark.asyncio
    @pytest.mark.unit
    @patch("data_lake.tools.adapters.maf.SearchClient")
    @patch("data_lake.tools.adapters.maf.get_search_credential_async")
    async def test_search_with_search_mode(self, mock_create_cred, mock_search_client, monkeypatch):
        """Test tool uses search_mode parameter."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")

        async def async_results():
            yield {"name": "test"}

        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=async_results())
        mock_search_client.return_value = mock_client
        mock_create_cred.return_value = MagicMock()

        # Create and invoke tool with search_mode
        tool = await create_data_lake_search_tool()
        params = DataLakeSearchParams(query="test data", search_mode="all")
        await tool(params)

        # Verify search_mode parameter was passed
        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["search_mode"] == "all"

    @pytest.mark.asyncio
    @pytest.mark.unit
    @patch("data_lake.tools.adapters.maf.SearchClient")
    @patch("data_lake.tools.adapters.maf.get_search_credential_async")
    async def test_search_handles_errors_gracefully(self, mock_create_cred, mock_search_client, monkeypatch):
        """Test tool returns error message on search failure."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")

        mock_client = MagicMock()
        mock_client.search = AsyncMock(side_effect=Exception("Search failed"))
        mock_search_client.return_value = mock_client
        mock_create_cred.return_value = MagicMock()

        # Create and invoke tool
        tool = await create_data_lake_search_tool()
        params = DataLakeSearchParams(query="test data")
        result = await tool(params)

        # Verify error is returned as JSON
        result_data = json.loads(result)
        assert len(result_data) == 1
        assert "error" in result_data[0]
        assert "Search failed" in result_data[0]["error"]

    @pytest.mark.asyncio
    @pytest.mark.unit
    @patch("data_lake.tools.adapters.maf.SearchClient")
    @patch("data_lake.tools.adapters.maf.get_search_credential_async")
    async def test_search_respects_top_parameter(self, mock_create_cred, mock_search_client, monkeypatch):
        """Test tool respects top parameter for result limit."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")

        async def async_results():
            yield {"name": "test"}

        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=async_results())
        mock_search_client.return_value = mock_client
        mock_create_cred.return_value = MagicMock()

        # Create and invoke tool with custom top
        tool = await create_data_lake_search_tool()
        params = DataLakeSearchParams(query="test data", top=5)
        await tool(params)

        # Verify top parameter was passed
        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["top"] == 5


class TestDataLakeSearchParams:
    """Test cases for DataLakeSearchParams validation."""

    @pytest.mark.unit
    def test_valid_params_with_defaults(self):
        """Test valid params with default values."""
        params = DataLakeSearchParams(query="test query")

        assert params.query == "test query"
        assert params.artifact_types is None
        assert params.top == 20
        assert params.select_fields is None
        assert params.search_mode is None
        assert params.order_by is None

    @pytest.mark.unit
    def test_valid_params_with_all_fields(self):
        """Test valid params with all fields specified."""
        params = DataLakeSearchParams(
            query="test query",
            artifact_types=["azure_sql_table"],
            top=30,
            select_fields=["name", "description"],
            search_mode="all",
            order_by=["name asc"],
        )

        assert params.query == "test query"
        assert params.artifact_types == ["azure_sql_table"]
        assert params.top == 30
        assert params.select_fields == ["name", "description"]
        assert params.search_mode == "all"
        assert params.order_by == ["name asc"]

    @pytest.mark.unit
    def test_top_must_be_positive(self):
        """Test top parameter must be >= 1."""
        with pytest.raises(ValueError):
            DataLakeSearchParams(query="test", top=0)

    @pytest.mark.unit
    def test_top_must_not_exceed_50(self):
        """Test top parameter must be <= 50."""
        with pytest.raises(ValueError):
            DataLakeSearchParams(query="test", top=51)


class TestDiscoverAvailableDomains:
    """Test cases for _discover_available_domains."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_returns_empty_list_when_not_configured(self, monkeypatch):
        """Test returns empty list when DATA_LAKE_SEARCH_ENDPOINT is not set."""
        monkeypatch.delenv("DATA_LAKE_SEARCH_ENDPOINT", raising=False)
        result = await _discover_available_domains()
        assert result == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    @patch("data_lake.tools.adapters.maf.SearchClient")
    @patch("data_lake.tools.adapters.maf.get_search_credential_async")
    async def test_returns_sorted_domains_from_facets(self, mock_create_cred, mock_search_client, monkeypatch):
        """Test returns sorted list of domains discovered via facets query."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")
        mock_create_cred.return_value = MagicMock()

        mock_results = AsyncMock()
        mock_results.get_facets = AsyncMock(
            return_value={"domain": [{"value": "energy"}, {"value": "climate"}, {"value": "power-grid"}]}
        )
        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=mock_results)
        mock_client.close = AsyncMock()
        mock_search_client.return_value = mock_client

        result = await _discover_available_domains()

        assert result == ["climate", "energy", "power-grid"]
        mock_client.search.assert_awaited_once_with(search_text="*", facets=["domain"], top=0)
        mock_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.unit
    @patch("data_lake.tools.adapters.maf.SearchClient")
    @patch("data_lake.tools.adapters.maf.get_search_credential_async")
    async def test_returns_empty_list_when_no_domain_facets(self, mock_create_cred, mock_search_client, monkeypatch):
        """Test returns empty list when index has no domain facet values."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")
        mock_create_cred.return_value = MagicMock()

        mock_results = AsyncMock()
        mock_results.get_facets = AsyncMock(return_value={})
        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=mock_results)
        mock_client.close = AsyncMock()
        mock_search_client.return_value = mock_client

        result = await _discover_available_domains()

        assert result == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    @patch("data_lake.tools.adapters.maf.SearchClient")
    @patch("data_lake.tools.adapters.maf.get_search_credential_async")
    async def test_returns_empty_list_on_search_error(self, mock_create_cred, mock_search_client, monkeypatch):
        """Test returns empty list when search raises an exception."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")
        mock_create_cred.return_value = MagicMock()

        mock_client = MagicMock()
        mock_client.search = AsyncMock(side_effect=Exception("Network error"))
        mock_client.close = AsyncMock()
        mock_search_client.return_value = mock_client

        result = await _discover_available_domains()

        assert result == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    @patch("data_lake.tools.adapters.maf.SearchClient")
    @patch("data_lake.tools.adapters.maf.get_search_credential_async")
    async def test_skips_facet_entries_without_value(self, mock_create_cred, mock_search_client, monkeypatch):
        """Test ignores facet entries with missing or empty value field."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")
        mock_create_cred.return_value = MagicMock()

        mock_results = AsyncMock()
        mock_results.get_facets = AsyncMock(
            return_value={"domain": [{"value": "energy"}, {"value": None}, {"count": 5}]}
        )
        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=mock_results)
        mock_client.close = AsyncMock()
        mock_search_client.return_value = mock_client

        result = await _discover_available_domains()

        assert result == ["energy"]


class TestBuildSearchParamsModel:
    """Test cases for _build_search_params_model."""

    @pytest.mark.unit
    def test_returns_model_with_dynamic_domains_when_available(self):
        """Test returns model with domain list in description when domains found."""
        model = _build_search_params_model(["climate", "energy", "power-grid"])

        field_info = model.model_fields["domains"]
        assert field_info.description is not None
        assert "'climate'" in field_info.description
        assert "'energy'" in field_info.description
        assert "'power-grid'" in field_info.description

    @pytest.mark.unit
    def test_returns_model_with_generic_description_when_no_domains(self):
        """Test returns model with generic description when no domains discovered."""
        model = _build_search_params_model([])

        field_info = model.model_fields["domains"]
        assert field_info.description is not None
        assert "Leave empty to search all domains" in field_info.description
        assert "Available domains" not in field_info.description

    @pytest.mark.unit
    def test_returned_model_name_is_data_lake_search_params(self):
        """Test returned model has the clean DataLakeSearchParams name."""
        model = _build_search_params_model(["energy"])

        assert model.__name__ == "DataLakeSearchParams"
        assert model.__qualname__ == "DataLakeSearchParams"

    @pytest.mark.unit
    def test_returned_model_is_subclass_of_data_lake_search_params(self):
        """Test returned model inherits all fields from DataLakeSearchParams."""
        model = _build_search_params_model(["energy"])

        assert issubclass(model, DataLakeSearchParams)
        instance = model(query="test")
        assert instance.query == "test"
        assert instance.top == 20


class TestDomainDiscoveryIntegration:
    """Integration tests: domain discovery wires into the tool schema."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    @patch("data_lake.tools.adapters.maf._build_search_params_model")
    @patch("data_lake.tools.adapters.maf._discover_available_domains", new_callable=AsyncMock)
    async def test_tool_creation_discovers_domains_and_builds_model(self, mock_discover, mock_build_model, monkeypatch):
        """Test that create_data_lake_search_tool calls discovery and model builder."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")
        discovered = ["climate", "energy"]
        mock_discover.return_value = discovered
        # Return a real model so the tool decorator doesn't fail
        mock_build_model.return_value = _build_search_params_model(discovered)

        await create_data_lake_search_tool()

        mock_discover.assert_awaited_once()
        mock_build_model.assert_called_once_with(discovered)

    @pytest.mark.asyncio
    @pytest.mark.unit
    @patch("data_lake.tools.adapters.maf._discover_available_domains", new_callable=AsyncMock)
    @patch("data_lake.tools.adapters.maf.SearchClient")
    @patch("data_lake.tools.adapters.maf.get_search_credential_async")
    async def test_tool_accepts_dict_params_with_dynamic_model(
        self, mock_create_cred, mock_search_client, mock_discover, monkeypatch
    ):
        """Test that dict params are converted using the dynamic model (not static base)."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")
        mock_create_cred.return_value = MagicMock()
        mock_discover.return_value = ["energy"]

        async def mock_search_results():
            yield {"name": "result"}

        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=mock_search_results())
        mock_search_client.return_value = mock_client

        tool = await create_data_lake_search_tool()

        # Pass params as a dict (as MAF sometimes does)
        result = await tool({"query": "energy datasets", "domains": ["energy"]})

        result_data = json.loads(result)
        assert isinstance(result_data, list)
        # Verify the domain filter was applied
        call_kwargs = mock_client.search.call_args.kwargs
        assert "domain eq 'energy'" in call_kwargs["filter"]

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_discover_domains_uses_local_catalog_when_no_endpoint(self, monkeypatch, tmp_path):
        catalog = tmp_path / "catalog.yaml"
        catalog.write_text(
            """
artifacts:
  - artifact_id: id1
    name: A
    artifact_type: blob
    domain: energy
    source: local
  - artifact_id: id2
    name: B
    artifact_type: blob
    domain: climate
    source: local
""".strip(),
            encoding="utf-8",
        )
        monkeypatch.delenv("DATA_LAKE_SEARCH_ENDPOINT", raising=False)
        monkeypatch.setenv("DATA_LAKE_LOCAL_CATALOG", str(catalog))

        result = await _discover_available_domains()
        assert result == ["climate", "energy"]


class TestDataLakeSearchBackendABC:
    """Tests for the DataLakeSearchBackend abstract base class."""

    @pytest.mark.unit
    def test_abc_cannot_be_instantiated_directly(self):
        """DataLakeSearchBackend is abstract and cannot be instantiated."""
        with pytest.raises(TypeError):
            DataLakeSearchBackend()  # type: ignore[abstract]

    @pytest.mark.unit
    def test_subclass_with_search_is_valid(self):
        """A concrete subclass implementing search() is a valid backend."""

        class _FakeBackend(DataLakeSearchBackend):
            async def search(self, params: DataLakeSearchParams) -> list[dict]:
                return []

        backend = _FakeBackend()
        assert isinstance(backend, DataLakeSearchBackend)

    @pytest.mark.unit
    def test_non_subclass_is_not_instance(self):
        """A class that doesn't inherit DataLakeSearchBackend is not an instance."""

        class _NoInherit:
            async def search(self, params: DataLakeSearchParams) -> list[dict]:
                return []

        assert not isinstance(_NoInherit(), DataLakeSearchBackend)

    @pytest.mark.unit
    def test_default_backend_is_subclass(self):
        """DefaultDataLakeSearchBackend inherits from DataLakeSearchBackend."""
        with patch("data_lake.tools.adapters.maf.get_search_credential_async", return_value=MagicMock()):
            backend = DefaultDataLakeSearchBackend()
            assert isinstance(backend, DataLakeSearchBackend)

    @pytest.mark.unit
    def test_subclass_without_search_raises_type_error(self):
        """A subclass that does not implement search() cannot be instantiated."""

        class _IncompleteBackend(DataLakeSearchBackend):
            pass

        with pytest.raises(TypeError):
            _IncompleteBackend()  # type: ignore[abstract]


class TestLocalDataLakeSearchBackend:
    """Tests for local file-backed DataLake search backend."""

    @staticmethod
    def _write_catalog(tmp_path) -> str:
        catalog = tmp_path / "catalog.yaml"
        catalog.write_text(
            """
artifacts:
  - artifact_id: weather-1
    name: Daily Weather Observations
    description: NOAA weather station temperatures
    artifact_type: blob
    domain: earthscience
    source: local
    tags: [weather, noaa, temperature]
  - artifact_id: grid-1
    name: Transmission Lines
    description: Power grid transmission line geospatial data
    artifact_type: blob
    domain: powergrid
    source: local
    tags: [grid, transmission, geospatial]
  - artifact_id: market-1
    name: Electricity Market Prices
    description: Hourly wholesale electricity prices
    artifact_type: blob
    domain: energy
    source: local
    tags: [market, electricity, prices]
""".strip(),
            encoding="utf-8",
        )
        return str(catalog)

    @pytest.mark.unit
    def test_raises_for_missing_catalog_file(self):
        with pytest.raises(FileNotFoundError):
            LocalDataLakeSearchBackend("/tmp/does-not-exist-catalog.yaml")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_returns_empty_for_empty_catalog(self, tmp_path):
        catalog = tmp_path / "catalog.yaml"
        catalog.write_text("artifacts: []", encoding="utf-8")
        backend = LocalDataLakeSearchBackend(str(catalog))
        results = await backend.search(DataLakeSearchParams(query="weather"))
        assert results == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_bm25_search_ranking(self, tmp_path):
        backend = LocalDataLakeSearchBackend(self._write_catalog(tmp_path))
        results = await backend.search(DataLakeSearchParams(query="weather temperature", top=2))
        assert len(results) == 2
        assert results[0]["artifact_id"] == "weather-1"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_applies_filters(self, tmp_path):
        backend = LocalDataLakeSearchBackend(self._write_catalog(tmp_path))
        results = await backend.search(
            DataLakeSearchParams(
                query="data",
                artifact_types=["blob"],
                domains=["powergrid"],
                sources=["local"],
            )
        )
        assert len(results) == 1
        assert results[0]["artifact_id"] == "grid-1"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_select_fields_projection(self, tmp_path):
        backend = LocalDataLakeSearchBackend(self._write_catalog(tmp_path))
        results = await backend.search(DataLakeSearchParams(query="weather", select_fields=["artifact_id", "name"]))
        assert "artifact_id" in results[0]
        assert "name" in results[0]
        assert "description" not in results[0]
        assert "asset_tag" in results[0]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_order_by_overrides_relevance_order(self, tmp_path):
        backend = LocalDataLakeSearchBackend(self._write_catalog(tmp_path))
        results = await backend.search(
            DataLakeSearchParams(
                query="data",
                order_by=["name desc"],
                top=3,
            )
        )
        names = [result["name"] for result in results]
        assert names == sorted(names, reverse=True)


class TestCreateDataLakeSearchToolWithCustomBackend:
    """Tests for create_data_lake_search_tool when a custom backend is injected."""

    @pytest.fixture(autouse=True)
    def _mock_domain_discovery(self):
        with patch("data_lake.tools.adapters.maf._discover_available_domains", new_callable=AsyncMock, return_value=[]):
            yield

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_uses_provided_backend(self):
        """Tool delegates search to the injected backend."""
        expected_assets = [{"name": "custom-asset", "artifact_id": "custom-id"}]

        class _FixedBackend(DataLakeSearchBackend):
            async def search(self, params: DataLakeSearchParams) -> list[dict]:
                return expected_assets

        tool = await create_data_lake_search_tool(backend=_FixedBackend())
        result = await tool(DataLakeSearchParams(query="anything"))
        assert json.loads(result) == expected_assets

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_custom_backend_receives_params(self):
        """Tool passes the search params to the backend's search method."""
        received: list[DataLakeSearchParams] = []

        class _CapturingBackend(DataLakeSearchBackend):
            async def search(self, params: DataLakeSearchParams) -> list[dict]:
                received.append(params)
                return []

        tool = await create_data_lake_search_tool(backend=_CapturingBackend())
        await tool(DataLakeSearchParams(query="my query", top=10))

        assert len(received) == 1
        assert received[0].query == "my query"
        assert received[0].top == 10

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_custom_backend_can_enforce_domain_constraints(self):
        """A custom backend can restrict results to allowed domains."""
        ALLOWED_DOMAIN = "energy"

        class _DomainRestrictedBackend(DataLakeSearchBackend):
            async def search(self, params: DataLakeSearchParams) -> list[dict]:
                # Override domains to enforce hard constraint regardless of agent input
                restricted = params.model_copy(update={"domains": [ALLOWED_DOMAIN]})
                # Return domain so we can assert it was applied
                return [{"domain": restricted.domains[0]}]

        tool = await create_data_lake_search_tool(backend=_DomainRestrictedBackend())
        # Even if the agent asks for all domains, only the allowed domain is returned
        result = await tool(DataLakeSearchParams(query="wind power"))
        result_data = json.loads(result)
        assert result_data == [{"domain": ALLOWED_DOMAIN}]

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_no_backend_creates_default_backend(self, monkeypatch):
        """When no backend is provided, DefaultDataLakeSearchBackend is used."""
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://test.search.windows.net")

        async def async_results():
            yield {"name": "test_asset"}

        with (
            patch("data_lake.tools.adapters.maf.SearchClient") as mock_sc,
            patch("data_lake.tools.adapters.maf.get_search_credential_async", return_value=MagicMock()),
        ):
            mock_client = MagicMock()
            mock_client.search = AsyncMock(return_value=async_results())
            mock_sc.return_value = mock_client

            tool = await create_data_lake_search_tool()
            result = await tool(DataLakeSearchParams(query="test"))

        result_data = json.loads(result)
        assert result_data[0]["name"] == "test_asset"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_no_backend_uses_local_backend_when_only_local_catalog_is_set(self, monkeypatch, tmp_path):
        """When only DATA_LAKE_LOCAL_CATALOG is set, local backend is auto-selected."""
        catalog = tmp_path / "catalog.yaml"
        catalog.write_text(
            """
artifacts:
  - artifact_id: local-1
    name: Local Weather Data
    description: NOAA observations
    artifact_type: blob
    domain: earthscience
    source: local
    tags: [weather]
""".strip(),
            encoding="utf-8",
        )
        monkeypatch.delenv("DATA_LAKE_SEARCH_ENDPOINT", raising=False)
        monkeypatch.setenv("DATA_LAKE_LOCAL_CATALOG", str(catalog))

        tool = await create_data_lake_search_tool()
        result = await tool(DataLakeSearchParams(query="weather"))

        result_data = json.loads(result)
        assert len(result_data) == 1
        assert result_data[0]["artifact_id"] == "local-1"
