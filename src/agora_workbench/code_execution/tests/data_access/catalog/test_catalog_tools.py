"""Tests for the catalog MCP tools."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from ....data_access.catalog.config import CatalogConfig, SearchConfig
from ....data_access.catalog.db import CatalogDB
from ....catalog_tools import CatalogToolsContext, register_catalog_tools


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


class TestQueryCatalog:
    """Tests for the query_catalog tool."""

    @pytest.fixture
    def file_db(self, tmp_path):
        """On-disk DB required for read-only connections."""
        db_path = tmp_path / "catalog.db"
        catalog_db = CatalogDB(db_path=str(db_path), vec_dimensions=4)
        catalog_db.open()
        catalog_db.upsert_artifact(
            artifact_id="weather1",
            name="daily_obs.csv",
            storage_uri="/data/weather/daily_obs.csv",
            description="NOAA daily weather observations",
            domain="earthscience",
            source_type="local",
            content_type="text/csv",
            size_bytes=2048,
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
            content_type="application/geo+json",
            size_bytes=10000000,
            indexed_at="2026-01-01T00:00:00Z",
            embedding=[0.0, 1.0, 0.0, 0.0],
        )
        yield catalog_db
        catalog_db.close()

    @pytest.fixture
    def file_tools(self, file_db):
        """Register tools with on-disk DB."""
        mock_provider = MagicMock()
        mock_provider.embed = AsyncMock(return_value=[[0.9, 0.1, 0.0, 0.0]])
        mock_provider.dimensions = 4
        config = CatalogConfig(search=SearchConfig(embedding_model="test"))
        ctx = CatalogToolsContext(db=file_db, embedding_provider=mock_provider, config=config)

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

    @pytest.mark.asyncio
    async def test_select_query(self, file_tools):
        results = await file_tools["query_catalog"]("SELECT name, domain FROM artifacts ORDER BY name")
        assert len(results) == 2
        assert results[0]["name"] == "daily_obs.csv"

    @pytest.mark.asyncio
    async def test_filter_by_content_type(self, file_tools):
        results = await file_tools["query_catalog"]("SELECT name FROM artifacts WHERE content_type = 'text/csv'")
        assert len(results) == 1
        assert results[0]["name"] == "daily_obs.csv"

    @pytest.mark.asyncio
    async def test_aggregation(self, file_tools):
        results = await file_tools["query_catalog"]("SELECT domain, COUNT(*) as cnt FROM artifacts GROUP BY domain")
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_rejects_write(self, file_tools):
        result = await file_tools["query_catalog"]("DELETE FROM artifacts")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_max_rows(self, file_tools):
        results = await file_tools["query_catalog"]("SELECT * FROM artifacts", max_rows=1)
        assert len(results) == 1
