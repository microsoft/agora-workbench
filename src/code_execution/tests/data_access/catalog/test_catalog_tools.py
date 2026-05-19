"""Tests for the catalog MCP tools."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from ....code_execution.data_access.catalog.config import CatalogConfig, SearchConfig
from ....code_execution.data_access.catalog.db import CatalogDB
from ....code_execution.catalog_tools import CatalogToolsContext, register_catalog_tools


@pytest.fixture
def db():
    """Create an in-memory catalog database with test data."""
    catalog_db = CatalogDB(db_path=":memory:", vec_dimensions=4)
    catalog_db.open()
    catalog_db.upsert_artifact(
        artifact_id="weather1",
        name="daily_obs.csv",
        storage_uri="/data/weather/daily_obs.csv",
        description="NOAA daily weather observations",
        domain="earthscience",
        source_type="local",
        indexed_at="2026-01-01T00:00:00Z",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )
    catalog_db.upsert_artifact(
        artifact_id="grid1",
        name="transmission_lines.geojson",
        storage_uri="/data/grid/lines.geojson",
        description="US power grid transmission lines",
        domain="powergrid",
        source_type="local",
        indexed_at="2026-01-01T00:00:00Z",
        embedding=[0.0, 1.0, 0.0, 0.0],
    )
    yield catalog_db
    catalog_db.close()


@pytest.fixture
def mock_embedding_provider():
    provider = MagicMock()
    provider.embed = AsyncMock(return_value=[[0.9, 0.1, 0.0, 0.0]])
    provider.dimensions = 4
    return provider


@pytest.fixture
def ctx(db, mock_embedding_provider):
    config = CatalogConfig(search=SearchConfig(embedding_model="test"))
    return CatalogToolsContext(db=db, embedding_provider=mock_embedding_provider, config=config)


@pytest.fixture
def tools(ctx):
    """Register tools and capture the tool functions."""
    captured = {}
    mock_mcp = MagicMock()

    def capture_tool(name, description):
        def decorator(fn):
            captured[name] = fn
            return fn

        return decorator

    mock_mcp.tool = capture_tool
    register_catalog_tools(mock_mcp, ctx)
    return captured


class TestSearchData:
    """Tests for the search_data tool."""

    @pytest.mark.asyncio
    async def test_basic_search(self, tools):
        results = await tools["search_data"]("weather")
        assert len(results) >= 1
        assert any(r["id"] == "weather1" for r in results)

    @pytest.mark.asyncio
    async def test_domain_filter(self, tools):
        results = await tools["search_data"]("data", domain="powergrid")
        assert all(r.get("domain") == "powergrid" for r in results)

    @pytest.mark.asyncio
    async def test_top_limit(self, tools):
        results = await tools["search_data"]("data", top=1)
        assert len(results) <= 1


class TestGetArtifact:
    """Tests for the get_artifact tool."""

    @pytest.mark.asyncio
    async def test_existing_artifact(self, tools):
        result = await tools["get_artifact"]("weather1")
        assert result["name"] == "daily_obs.csv"
        assert result["domain"] == "earthscience"

    @pytest.mark.asyncio
    async def test_nonexistent_artifact(self, tools):
        result = await tools["get_artifact"]("nonexistent")
        assert "error" in result


class TestListDomains:
    """Tests for the list_domains tool."""

    @pytest.mark.asyncio
    async def test_returns_domains(self, tools):
        domains = await tools["list_domains"]()
        assert "earthscience" in domains
        assert "powergrid" in domains
        assert domains == sorted(domains)
