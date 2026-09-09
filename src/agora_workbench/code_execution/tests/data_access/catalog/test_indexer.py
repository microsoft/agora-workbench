"""Tests for the catalog indexer."""

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agora_workbench.data_lake.identity import azure_uri_from_blob_name, canonicalize_azure_uri

from ....data_access.catalog.config import CatalogConfig, FileOverride, SourceConfig, SearchConfig
from ....data_access.catalog.db import CatalogDB, artifact_id_from_uri
from ....data_access.catalog.indexer import CatalogIndexer, _build_indexable_text, _EnumerationResult, _parse_blob_path


class TestParseBlobPath:
    """Tests for blob path parsing."""

    def test_az_scheme(self):
        account, container, prefix = _parse_blob_path("az://myaccount/mycontainer/some/prefix")
        assert account == "myaccount"
        assert container == "mycontainer"
        assert prefix == "some/prefix"

    def test_az_scheme_no_prefix(self):
        account, container, prefix = _parse_blob_path("az://myaccount/mycontainer")
        assert account == "myaccount"
        assert container == "mycontainer"
        assert prefix == ""

    def test_https_blob_url(self):
        account, container, prefix = _parse_blob_path(
            "https://orfb0eastus.blob.core.windows.net/fingerprints/.amltconfig"
        )
        assert account == "orfb0eastus"
        assert container == "fingerprints"
        assert prefix == ".amltconfig"

    def test_https_blob_url_with_prefix(self):
        account, container, prefix = _parse_blob_path("https://mystorage.blob.core.windows.net/data/weather/2024/")
        assert account == "mystorage"
        assert container == "data"
        assert prefix == "weather/2024/"

    @pytest.mark.parametrize(
        "path",
        [
            "https://mystorage.dfs.core.windows.net/data/weather/2024/",
            "abfss://data@mystorage.dfs.core.windows.net/weather/2024/",
        ],
    )
    def test_adls_forms(self, path):
        assert _parse_blob_path(path) == ("mystorage", "data", "weather/2024/")

    def test_invalid_path_raises(self):
        with pytest.raises(ValueError, match="Not a blob source"):
            _parse_blob_path("/local/path/data")


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

    def test_rejects_provider_dimension_mismatch(self, config, db):
        indexer = CatalogIndexer(config, db)
        mock_provider = MagicMock()
        mock_provider.dimensions = 3
        indexer._embedding_provider = mock_provider

        with pytest.raises(ValueError, match="provider returns 3, but CatalogDB expects 4"):
            _ = indexer.embedding_provider

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
                        "daily_obs.csv": FileOverride(description="Override description", domain="custom"),
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
        from ....data_access.catalog.db import artifact_id_from_uri

        record = db.get_artifact(artifact_id_from_uri(uri))
        assert record.description == "Override description"
        assert record.domain == "custom"

    @pytest.mark.asyncio
    async def test_rejects_provider_result_count_mismatch(self, config, db):
        indexer = CatalogIndexer(config, db)
        mock_provider = MagicMock()
        mock_provider.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])
        mock_provider.dimensions = 4
        indexer._embedding_provider = mock_provider

        with pytest.raises(ValueError, match="1 vectors for a batch of 2 texts"):
            await indexer.index()

    @pytest.mark.asyncio
    async def test_configured_non_default_dimensions_construct_database(self, data_dir, tmp_path):
        config = CatalogConfig(
            sources=[SourceConfig(path=str(data_dir / "weather"))],
            search=SearchConfig(embedding_model="test-model", embedding_dimensions=3),
        )
        catalog_db = CatalogDB(
            db_path=tmp_path / "catalog.db",
            vec_dimensions=config.search.embedding_dimensions,
        )
        catalog_db.open()
        indexer = CatalogIndexer(config, catalog_db)
        mock_provider = MagicMock()
        mock_provider.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3]] * 2)
        mock_provider.dimensions = 3
        indexer._embedding_provider = mock_provider

        try:
            assert await indexer.index() == 2
            assert catalog_db.vec_dimensions == 3
            assert len(catalog_db.search("", query_embedding=[0.1, 0.2, 0.3])) == 2
        finally:
            catalog_db.close()

    @pytest.mark.asyncio
    async def test_service_default_dimensions_are_inferred(self, data_dir, tmp_path):
        config = CatalogConfig(
            sources=[SourceConfig(path=str(data_dir / "weather"))],
            search=SearchConfig(embedding_model="test-model"),
        )
        catalog_db = CatalogDB(
            db_path=tmp_path / "catalog.db",
            vec_dimensions=config.search.embedding_dimensions,
        )
        catalog_db.open()
        indexer = CatalogIndexer(config, catalog_db)
        mock_provider = MagicMock()
        mock_provider.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4, 0.5]] * 2)
        mock_provider.dimensions = None
        indexer._embedding_provider = mock_provider

        try:
            assert await indexer.index() == 2
            assert catalog_db.vec_dimensions == 5
        finally:
            catalog_db.close()

    async def test_adding_configured_id_preserves_existing_canonical_id(self, db, tmp_path):
        root = tmp_path / "weather"
        root.mkdir()
        (root / "daily.csv").write_text("data")
        first = CatalogIndexer(
            CatalogConfig(sources=[SourceConfig(source_id="weather", path=str(root))]),
            db,
        )
        await first.index()
        canonical = db.find_by_source_path("weather", "daily.csv").id

        second = CatalogIndexer(
            CatalogConfig(
                sources=[
                    SourceConfig(
                        source_id="weather",
                        path=str(root),
                        files={"daily.csv": FileOverride(artifact_id="configured-id")},
                    )
                ]
            ),
            db,
        )
        assert await second.index() == 1
        assert db.find_by_source_path("weather", "daily.csv").id == canonical
        assert db.resolve_artifact_id("configured-id", "weather") == canonical

    @pytest.mark.asyncio
    async def test_adding_namespaced_configured_id_preserves_namespace(self, db, tmp_path):
        root = tmp_path / "weather"
        root.mkdir()
        (root / "daily.csv").write_text("data")
        await CatalogIndexer(
            CatalogConfig(sources=[SourceConfig(source_id="weather", path=str(root))]),
            db,
        ).index()
        canonical = db.find_by_source_path("weather", "daily.csv").id

        second = CatalogIndexer(
            CatalogConfig(
                sources=[
                    SourceConfig(
                        source_id="weather",
                        path=str(root),
                        files={"daily.csv": FileOverride(artifact_id="external:configured-id")},
                    )
                ]
            ),
            db,
        )
        assert await second.index() == 1
        assert db.find_by_source_path("weather", "daily.csv").id == canonical
        assert db.resolve_artifact_id("external:configured-id", "weather") == canonical
        assert db.resolve_artifact_id("artifact-id:external:configured-id", "weather") is None

    @pytest.mark.asyncio
    async def test_explicit_source_id_survives_restart_and_root_relocation(self, tmp_path):
        first_root = tmp_path / "first"
        second_root = tmp_path / "second"
        first_root.mkdir()
        second_root.mkdir()
        (first_root / "daily.csv").write_text("same data")
        (second_root / "daily.csv").write_text("same data")
        db_path = tmp_path / "catalog.db"

        first_db = CatalogDB(db_path, vec_dimensions=4)
        first_db.open()
        first_indexer = CatalogIndexer(
            CatalogConfig(sources=[SourceConfig(source_id="weather", path=str(first_root))]),
            first_db,
        )
        await first_indexer.index()
        first_record = first_db.find_by_source_path("weather", "daily.csv")
        first_db.close()

        second_db = CatalogDB(db_path, vec_dimensions=4)
        second_db.open()
        second_indexer = CatalogIndexer(
            CatalogConfig(sources=[SourceConfig(source_id="weather", path=str(second_root))]),
            second_db,
        )
        await second_indexer.index()
        second_record = second_db.find_by_source_path("weather", "daily.csv")
        try:
            assert second_record.id == first_record.id
            assert second_record.storage_uri == str(second_root / "daily.csv")
            assert second_record.current_revision == 2
        finally:
            second_db.close()

    @pytest.mark.asyncio
    async def test_move_creates_new_identity_and_tombstones_old_path(self, db, tmp_path):
        root = tmp_path / "data"
        root.mkdir()
        old_path = root / "old.csv"
        old_path.write_text("data")
        config = CatalogConfig(sources=[SourceConfig(source_id="source", path=str(root))])
        indexer = CatalogIndexer(config, db)

        await indexer.index()
        old_record = db.find_by_source_path("source", "old.csv")
        old_path.rename(root / "new.csv")
        await indexer.index()

        new_record = db.find_by_source_path("source", "new.csv")
        tombstone = db.find_by_source_path("source", "old.csv", include_deleted=True)
        assert new_record.id != old_record.id
        assert tombstone.deleted_at is not None

    @pytest.mark.asyncio
    async def test_successful_empty_source_tombstones_but_failed_source_does_not(self, db, tmp_path):
        successful_root = tmp_path / "successful"
        successful_root.mkdir()
        failed_root = tmp_path / "missing"
        for source_id, uri in (("successful", "/old-success.csv"), ("failed", "/old-failed.csv")):
            db.upsert_artifact(
                artifact_id=source_id,
                source_id=source_id,
                logical_path="old.csv",
                name="old.csv",
                storage_uri=uri,
                source_type="local",
            )
        indexer = CatalogIndexer(
            CatalogConfig(
                sources=[
                    SourceConfig(source_id="successful", path=str(successful_root)),
                    SourceConfig(source_id="failed", path=str(failed_root)),
                ]
            ),
            db,
        )

        assert await indexer.index() == 0
        assert db.get_artifact("successful") is None
        assert db.get_artifact("failed") is not None

    @pytest.mark.asyncio
    async def test_total_enumeration_failure_preserves_catalog(self, db, tmp_path):
        db.upsert_artifact(
            artifact_id="existing",
            source_id="failed",
            logical_path="old.csv",
            name="old.csv",
            storage_uri="/old.csv",
            source_type="local",
        )
        indexer = CatalogIndexer(
            CatalogConfig(sources=[SourceConfig(source_id="failed", path=str(tmp_path / "missing"))]),
            db,
        )
        assert await indexer.index() == 0
        assert db.get_artifact("existing") is not None

    @pytest.mark.asyncio
    async def test_unreadable_local_source_preserves_catalog(self, db, tmp_path, monkeypatch):
        root = tmp_path / "unreadable"
        root.mkdir()
        db.upsert_artifact(
            artifact_id="existing",
            source_id="source",
            logical_path="old.csv",
            name="old.csv",
            storage_uri="/old.csv",
            source_type="local",
        )

        def failed_walk(_path, onerror):
            onerror(PermissionError("denied"))
            return iter(())

        monkeypatch.setattr("agora_workbench.code_execution.data_access.catalog.indexer.os.walk", failed_walk)
        indexer = CatalogIndexer(CatalogConfig(sources=[SourceConfig(source_id="source", path=str(root))]), db)
        assert await indexer.index() == 0
        assert db.get_artifact("existing") is not None

    @pytest.mark.asyncio
    async def test_failed_blob_source_does_not_tombstone_when_local_source_succeeds(self, db, tmp_path, monkeypatch):
        local_root = tmp_path / "empty"
        local_root.mkdir()
        db.upsert_artifact(
            artifact_id="blob-existing",
            source_id="blob-source",
            logical_path="old.csv",
            name="old.csv",
            storage_uri="az://account123/container/old.csv",
            source_type="blob",
        )
        indexer = CatalogIndexer(
            CatalogConfig(
                sources=[
                    SourceConfig(source_id="local-source", path=str(local_root)),
                    SourceConfig(source_id="blob-source", path="az://account123/container"),
                ]
            ),
            db,
        )
        monkeypatch.setattr(
            indexer,
            "_enumerate_blob_sources_concurrent",
            AsyncMock(return_value=_EnumerationResult([], set())),
        )
        assert await indexer.index() == 0
        assert db.get_artifact("blob-existing") is not None


class _FakeContainerClient:
    def __init__(self, blobs):
        self._blobs = blobs if isinstance(blobs, list) else [blobs]

    def list_blobs(self, name_starts_with):
        async def iterator():
            for blob in self._blobs:
                yield blob

        return iterator()


class _FakeBlobServiceClient:
    def __init__(self, blobs):
        self._blobs = blobs

    def get_container_client(self, container):
        assert container == "container"
        return _FakeContainerClient(self._blobs)


class TestBlobMigrationAdoption:
    @pytest.mark.asyncio
    async def test_blob_namespaced_configured_id_preserves_namespace(self):
        db = CatalogDB(":memory:", vec_dimensions=4)
        db.open()
        source = SourceConfig(
            source_id="blob-source",
            path="az://account123/container",
            files={"file.csv": FileOverride(artifact_id="external:configured-id")},
        )
        indexer = CatalogIndexer(CatalogConfig(sources=[source]), db)
        blob = SimpleNamespace(
            name="file.csv",
            etag='"etag"',
            size=10,
            last_modified=None,
            content_settings=SimpleNamespace(content_type="text/csv"),
        )
        clients = {"https://account123.blob.core.windows.net": _FakeBlobServiceClient([blob])}

        try:
            artifacts = await indexer._enumerate_blob_source(source, MagicMock(), clients)
            assert "external:configured-id" in artifacts[0]["aliases"]
            assert "artifact-id:external:configured-id" not in artifacts[0]["aliases"]
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_blob_prefix_uses_directory_boundary_and_stable_relative_identity(self):
        db = CatalogDB(":memory:", vec_dimensions=4)
        db.open()
        try:
            source = SourceConfig(source_id="blob-source", path="az://account123/container/data")
            assert source.path == "az://account123/container/data/"
            indexer = CatalogIndexer(CatalogConfig(sources=[source]), db)
            blobs = [
                SimpleNamespace(
                    name=name,
                    etag=f'"{name}"',
                    size=10,
                    last_modified=None,
                    content_settings=SimpleNamespace(content_type="text/csv"),
                )
                for name in ("data/file.csv", "database/file.csv")
            ]
            clients = {"https://account123.blob.core.windows.net": _FakeBlobServiceClient(blobs)}

            artifacts = await indexer._enumerate_blob_source(source, MagicMock(), clients)

            assert len(artifacts) == 1
            assert artifacts[0]["logical_path"] == "file.csv"
            expected_id = artifacts[0]["artifact_id"]

            equivalent = SourceConfig(source_id="blob-source", path="az://account123/container/data/")
            equivalent_indexer = CatalogIndexer(CatalogConfig(sources=[equivalent]), db)
            repeated = await equivalent_indexer._enumerate_blob_source(equivalent, MagicMock(), clients)
            assert repeated[0]["artifact_id"] == expected_id
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_sdk_literal_percent_escape_does_not_collide_with_decoded_name(self):
        db = CatalogDB(":memory:", vec_dimensions=4)
        db.open()
        source = SourceConfig(source_id="blob-source", path="az://account123/container")
        indexer = CatalogIndexer(CatalogConfig(sources=[source]), db)
        blobs = [
            SimpleNamespace(
                name=name,
                etag=f'"{name}"',
                size=10,
                last_modified=None,
                content_settings=SimpleNamespace(content_type="text/csv"),
            )
            for name in ("literal%41.csv", "literalA.csv", "a b.csv", "a%20b.csv")
        ]
        clients = {"https://account123.blob.core.windows.net": _FakeBlobServiceClient(blobs)}

        try:
            artifacts = await indexer._enumerate_blob_source(source, MagicMock(), clients)
            assert {artifact["storage_uri"] for artifact in artifacts} == {
                "az://account123/container/literal%2541.csv",
                "az://account123/container/literalA.csv",
                "az://account123/container/a%20b.csv",
                "az://account123/container/a%2520b.csv",
            }
            assert len({artifact["artifact_id"] for artifact in artifacts}) == 4
        finally:
            db.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "legacy_uri",
        [
            "https://account123.blob.core.windows.net/container/Folder/a%20b%23c%2Bd%C3%A9.csv?sig=secret",
            "https://account123.dfs.core.windows.net/container/Folder/a%20b%23c%2Bd%C3%A9.csv",
            "abfss://container@account123.dfs.core.windows.net/Folder/a%20b%23c%2Bd%C3%A9.csv",
            "az://account123/container/Folder/a%20b%23c%2Bd%C3%A9.csv",
        ],
    )
    async def test_migrated_blob_is_adopted_across_supported_uri_forms(self, tmp_path, monkeypatch, legacy_uri):
        db_path = tmp_path / "legacy.db"
        old_id = artifact_id_from_uri(legacy_uri)
        connection = sqlite3.connect(db_path)
        connection.execute(
            """CREATE TABLE artifacts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, storage_uri TEXT NOT NULL UNIQUE,
                description TEXT, domain TEXT, source_type TEXT, content_type TEXT,
                size_bytes INTEGER, indexed_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO artifacts VALUES (?, 'a b#c+d\u00e9.csv', ?, NULL, NULL, 'blob', "
            "'text/csv', 10, '2026-01-01T00:00:00Z')",
            (old_id, legacy_uri),
        )
        connection.commit()
        connection.close()

        db = CatalogDB(db_path, vec_dimensions=4)
        db.open()
        try:
            source = SourceConfig(source_id="blob-source", path="az://account123/container")
            indexer = CatalogIndexer(CatalogConfig(sources=[source]), db)
            blob = SimpleNamespace(
                name="Folder/a b#c+d\u00e9.csv",
                etag='"etag"',
                size=10,
                last_modified=None,
                content_settings=SimpleNamespace(content_type="text/csv"),
            )
            clients = {"https://account123.blob.core.windows.net": _FakeBlobServiceClient(blob)}
            artifacts = await indexer._enumerate_blob_source(source, MagicMock(), clients)
            monkeypatch.setattr(
                indexer,
                "_enumerate_blob_sources_concurrent",
                AsyncMock(return_value=_EnumerationResult(artifacts, {"blob-source"})),
            )

            assert await indexer.index() == 1
            live = db.conn.execute(
                "SELECT id, source_id, storage_uri FROM artifacts WHERE deleted_at IS NULL"
            ).fetchall()
            assert len(live) == 1
            assert live[0]["source_id"] == "blob-source"
            assert live[0]["storage_uri"] == azure_uri_from_blob_name(
                "account123", "container", "Folder/a b#c+d\u00e9.csv"
            )
            assert db.get_artifact(old_id).id == live[0]["id"]
            canonical_id = artifact_id_from_uri(canonicalize_azure_uri(legacy_uri))
            assert db.get_artifact(canonical_id).id == live[0]["id"]
            assert db.get_artifact(f"storage-uri:{legacy_uri}").id == live[0]["id"]
            if "sig=secret" in legacy_uri:
                persisted = db.conn.execute(
                    """SELECT storage_uri AS value FROM artifacts
                       UNION ALL
                       SELECT alias AS value FROM artifact_aliases"""
                ).fetchall()
                assert all("secret" not in row["value"] for row in persisted)
        finally:
            db.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("literal_name", "legacy_uri"),
        [
            ("literal%name.csv", "az://account123/container/literal%name.csv"),
            ("literal#name.csv", "az://account123/container/literal#name.csv"),
            ("literal?name.csv", "az://account123/container/literal?name.csv"),
        ],
    )
    async def test_raw_v0_az_literal_object_names_are_adopted(self, tmp_path, monkeypatch, literal_name, legacy_uri):
        db_path = tmp_path / "legacy-literal.db"
        old_id = artifact_id_from_uri(legacy_uri)
        connection = sqlite3.connect(db_path)
        connection.execute(
            """CREATE TABLE artifacts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, storage_uri TEXT NOT NULL UNIQUE,
                description TEXT, domain TEXT, source_type TEXT, content_type TEXT,
                size_bytes INTEGER, indexed_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO artifacts VALUES (?, ?, ?, NULL, NULL, 'blob', 'text/csv', 10, '2026-01-01T00:00:00Z')",
            (old_id, literal_name, legacy_uri),
        )
        connection.commit()
        connection.close()

        db = CatalogDB(db_path, vec_dimensions=4)
        db.open()
        try:
            source = SourceConfig(source_id="blob-source", path="az://account123/container")
            indexer = CatalogIndexer(CatalogConfig(sources=[source]), db)
            blob = SimpleNamespace(
                name=literal_name,
                etag='"etag"',
                size=10,
                last_modified=None,
                content_settings=SimpleNamespace(content_type="text/csv"),
            )
            clients = {"https://account123.blob.core.windows.net": _FakeBlobServiceClient(blob)}
            artifacts = await indexer._enumerate_blob_source(source, MagicMock(), clients)
            monkeypatch.setattr(
                indexer,
                "_enumerate_blob_sources_concurrent",
                AsyncMock(return_value=_EnumerationResult(artifacts, {"blob-source"})),
            )

            assert await indexer.index() == 1
            live = db.conn.execute("SELECT id FROM artifacts WHERE deleted_at IS NULL").fetchall()
            assert len(live) == 1
            assert db.get_artifact(old_id).id == live[0]["id"]
            literal_uri = azure_uri_from_blob_name("account123", "container", literal_name)
            assert db.get_artifact(artifact_id_from_uri(literal_uri)).id == live[0]["id"]
            assert db.get_artifact(f"storage-uri:{literal_uri}").id == live[0]["id"]
        finally:
            db.close()
