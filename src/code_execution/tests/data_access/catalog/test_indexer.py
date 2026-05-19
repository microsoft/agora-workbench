"""Tests for the catalog indexer."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from ....code_execution.data_access.catalog.config import CatalogConfig, SourceConfig, SearchConfig
from ....code_execution.data_access.catalog.db import CatalogDB
from ....code_execution.data_access.catalog.indexer import CatalogIndexer, _build_indexable_text


class TestBuildIndexableText:
    """Tests for the indexable text builder."""

    def test_name_only(self):
        assert _build_indexable_text("test.csv", None, None) == "test.csv"

    def test_name_and_description(self):
        result = _build_indexable_text("test.csv", "A test file", None)
        assert result == "test.csv A test file"

    def test_all_fields(self):
        result = _build_indexable_text("test.csv", "A test file", "weather")
        assert result == "test.csv A test file weather"


class TestCatalogIndexerLocal:
    """Tests for local filesystem indexing."""

    @pytest.fixture
    def data_dir(self, tmp_path):
        """Create a temporary data directory with test files."""
        weather = tmp_path / "weather"
        weather.mkdir()
        (weather / "daily_obs.csv").write_text("date,temp\n2026-01-01,5.2")
        (weather / "hourly_wind.parquet").write_bytes(b"\x00" * 100)
        (weather / ".hidden_file").write_text("hidden")
        return tmp_path

    @pytest.fixture
    def config(self, data_dir):
        return CatalogConfig(
            sources=[
                SourceConfig(
                    path=str(data_dir / "weather"),
                    domain="earthscience",
                    description="Weather data",
                )
            ],
            search=SearchConfig(embedding_model="test-model"),
        )

    @pytest.fixture
    def db(self):
        catalog_db = CatalogDB(db_path=":memory:", vec_dimensions=4)
        catalog_db.open()
        yield catalog_db
        catalog_db.close()

    @pytest.mark.asyncio
    async def test_indexes_local_files(self, config, db, data_dir):
        indexer = CatalogIndexer(config, db)

        # Mock the embedding provider
        mock_provider = MagicMock()
        mock_provider.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]] * 2)
        mock_provider.dimensions = 4
        indexer._embedding_provider = mock_provider

        count = await indexer.index()
        assert count == 2  # daily_obs.csv + hourly_wind.parquet (not .hidden_file)

    @pytest.mark.asyncio
    async def test_skips_hidden_files(self, config, db, data_dir):
        indexer = CatalogIndexer(config, db)
        mock_provider = MagicMock()
        mock_provider.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]] * 2)
        mock_provider.dimensions = 4
        indexer._embedding_provider = mock_provider

        await indexer.index()
        uris = db.get_existing_uris()
        assert not any(".hidden" in uri for uri in uris)

    @pytest.mark.asyncio
    async def test_idempotent_reindex(self, config, db, data_dir):
        indexer = CatalogIndexer(config, db)
        mock_provider = MagicMock()
        mock_provider.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]] * 2)
        mock_provider.dimensions = 4
        indexer._embedding_provider = mock_provider

        count1 = await indexer.index()
        count2 = await indexer.index()
        assert count1 == 2
        assert count2 == 0  # No new artifacts

    @pytest.mark.asyncio
    async def test_removes_stale_artifacts(self, config, db, data_dir):
        indexer = CatalogIndexer(config, db)
        mock_provider = MagicMock()
        mock_provider.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]] * 2)
        mock_provider.dimensions = 4
        indexer._embedding_provider = mock_provider

        await indexer.index()

        # Delete a file
        (data_dir / "weather" / "hourly_wind.parquet").unlink()
        mock_provider.embed = AsyncMock(return_value=[])

        await indexer.index()
        uris = db.get_existing_uris()
        assert len(uris) == 1
        assert any("daily_obs" in uri for uri in uris)

    @pytest.mark.asyncio
    async def test_nonexistent_source_path(self, db):
        config = CatalogConfig(
            sources=[SourceConfig(path="/nonexistent/path", domain="test")],
            search=SearchConfig(embedding_model="test-model"),
        )
        indexer = CatalogIndexer(config, db)
        mock_provider = MagicMock()
        mock_provider.embed = AsyncMock(return_value=[])
        mock_provider.dimensions = 4
        indexer._embedding_provider = mock_provider

        count = await indexer.index()
        assert count == 0

    @pytest.mark.asyncio
    async def test_per_file_overrides(self, db, tmp_path):
        weather = tmp_path / "weather"
        weather.mkdir()
        (weather / "daily_obs.csv").write_text("data")

        config = CatalogConfig(
            sources=[
                SourceConfig(
                    path=str(weather),
                    domain="earthscience",
                    description="Default description",
                    files={
                        "daily_obs.csv": {"description": "Override description", "domain": "custom"},
                    },
                )
            ],
            search=SearchConfig(embedding_model="test-model"),
        )
        indexer = CatalogIndexer(config, db)
        mock_provider = MagicMock()
        mock_provider.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])
        mock_provider.dimensions = 4
        indexer._embedding_provider = mock_provider

        await indexer.index()
        uris = db.get_existing_uris()
        uri = next(iter(uris))
        from ....code_execution.data_access.catalog.db import artifact_id_from_uri

        record = db.get_artifact(artifact_id_from_uri(uri))
        assert record.description == "Override description"
        assert record.domain == "custom"
