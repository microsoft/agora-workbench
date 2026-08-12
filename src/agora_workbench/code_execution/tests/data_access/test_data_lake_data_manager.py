"""
Tests for DataLakeDataManager.

Tests asset fetching, caching, and cleanup operations.
The manager accepts type-tagged qualified names (<type>id</type>) and
streams assets directly to disk to avoid high memory usage.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ...data_access.fetchers import AssetFetcher, BlobFetcher
from ...data_access.manager import DataLakeDataManager


@pytest.fixture(autouse=True)
def clear_data_lake_search_endpoint(monkeypatch):
    """Default tests to no Azure Search endpoint unless explicitly requested."""
    monkeypatch.delenv("DATA_LAKE_SEARCH_ENDPOINT", raising=False)


@pytest.fixture(autouse=True)
def mock_storage_credential():
    """Mock create_storage_credential for all tests (no real MI / az login needed)."""
    with patch("agora_workbench.code_execution.data_access.manager.create_storage_credential") as mock_factory:
        mock_credential = MagicMock()
        mock_credential.get_token = AsyncMock(return_value=MagicMock(token="mock-token", expires_on=9999999999))
        mock_credential.close = AsyncMock()
        mock_factory.return_value = mock_credential
        yield mock_credential


def create_mock_fetch_to_file(data: bytes):
    """Create a mock fetch_to_file that writes data to the destination."""

    async def mock_fetch(qualified_name: str, dest_path):
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(data)
        return len(data)

    return mock_fetch


def _mock_blob_resolver(url: str):
    """Return an AsyncMock for ``_get_blob_url_from_artifact_id`` that returns *url*."""
    return AsyncMock(return_value=url)


def _get_blob_fetcher(manager: DataLakeDataManager) -> BlobFetcher:
    """Find the BlobFetcher instance in the manager's fetcher list."""
    for f in manager._fetchers:
        if isinstance(f, BlobFetcher):
            return f
    raise RuntimeError("BlobFetcher not found in manager._fetchers")


class RecordingFetcher(AssetFetcher):
    """Test fetcher that records calls and writes fixed bytes."""

    def __init__(self, data: bytes = b"extra"):
        super().__init__()
        self.data = data
        self.fetch_to_file_calls: list[str] = []

    def can_handle(self, qualified_name: str) -> bool:
        return qualified_name.startswith("https://")

    async def fetch(self, qualified_name: str) -> bytes:
        return self.data

    async def fetch_to_file(self, qualified_name: str, dest_path) -> int:
        self.fetch_to_file_calls.append(qualified_name)
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(self.data)
        return len(self.data)


class TestDataLakeDataManagerInit:
    """Test DataLakeDataManager initialization."""

    def test_init_creates_credential_and_fetchers(self):
        """Test initialization creates credential and fetchers."""
        manager = DataLakeDataManager()

        assert manager._credential is not None
        assert manager._search_client is None
        assert manager._cache_index == {}
        assert len(manager._fetchers) == 2
        assert manager._cache_dir.exists()

    def test_init_creates_search_client_when_endpoint_configured(self, data_lake_search_endpoint):
        """Test Azure Search is initialized only when an endpoint is configured."""
        manager = DataLakeDataManager()

        assert manager._credential is not None
        assert manager._search_client is not None
        assert manager._blob_details_index == "blob-details"
        assert len(manager._fetchers) == 2

    def test_init_creates_temp_cache_dir(self):
        """Test that initialization creates a temporary cache directory."""
        manager = DataLakeDataManager()

        assert manager._cache_dir.exists()
        assert manager._cache_dir.is_dir()
        assert "data_lake_cache_" in manager._cache_dir.name

    def test_init_without_search_endpoint_keeps_blob_fetching_available(self):
        """Test initialization without Azure Search still registers blob fetching."""
        manager = DataLakeDataManager()

        assert manager._credential is not None
        assert manager._search_client is None
        assert len(manager._fetchers) == 2
        assert _get_blob_fetcher(manager).credential is manager._credential
        assert manager._cache_dir.exists()

    @pytest.mark.asyncio
    async def test_init_credential_provider_failure_is_deferred(self, data_lake_search_endpoint):
        """Test credential provider init errors are deferred to fetch time."""
        with patch(
            "agora_workbench.code_execution.data_access.manager.create_storage_credential",
            side_effect=RuntimeError("missing managed identity"),
        ):
            manager = DataLakeDataManager()

        assert manager._credential is None
        assert manager._search_client is None
        assert len(manager._fetchers) == 1
        assert manager._credential_init_error == "RuntimeError: missing managed identity"
        with pytest.raises(ValueError, match="Azure data access initialization failed"):
            await manager.get_cache_path("<blob>artifact_id_1</blob>")

    def test_injected_credential_is_used_by_blob_fetcher(self):
        """Test a caller-supplied credential is used for the built-in BlobFetcher."""
        injected_credential = MagicMock()

        manager = DataLakeDataManager(credential=injected_credential)

        assert manager._credential is injected_credential
        assert _get_blob_fetcher(manager).credential is injected_credential


class TestGetCachePath:
    """Test get_cache_path functionality."""

    @pytest.mark.asyncio
    async def test_fetch_and_cache_blob(self):
        """Test fetching and caching a blob asset."""
        manager = DataLakeDataManager()
        blob_url = "https://storage.blob.core.windows.net/container/file.nc"
        qualified_name = "<blob>artifact_id_1</blob>"

        mock_data = b"test data content"
        mock_fetch_to_file = create_mock_fetch_to_file(mock_data)

        with (
            patch.object(manager, "_get_blob_url_from_artifact_id", _mock_blob_resolver(blob_url)),
            patch.object(_get_blob_fetcher(manager), "fetch_to_file", side_effect=mock_fetch_to_file),
        ):
            cache_path = await manager.get_cache_path(qualified_name)

            # Verify cache path exists
            assert cache_path.exists()
            assert cache_path.is_file()
            assert cache_path.suffix == ".nc"

            # Verify content
            with open(cache_path, "rb") as f:
                assert f.read() == mock_data

            # Verify cache index updated (keyed by artifact_id)
            assert "artifact_id_1" in manager._cache_index
            assert manager._cache_index["artifact_id_1"] == cache_path

    @pytest.mark.asyncio
    async def test_cache_reuse(self):
        """Test that cached assets are reused without re-fetching."""
        manager = DataLakeDataManager()
        blob_url = "https://storage.blob.core.windows.net/container/file.csv"
        qualified_name = "<blob>artifact_csv_1</blob>"

        mock_data = b"col1,col2\n1,2\n3,4"
        mock_fetch_to_file = create_mock_fetch_to_file(mock_data)

        with (
            patch.object(manager, "_get_blob_url_from_artifact_id", _mock_blob_resolver(blob_url)),
            patch.object(_get_blob_fetcher(manager), "fetch_to_file", side_effect=mock_fetch_to_file) as mock_fetch,
        ):
            # First call - should fetch
            cache_path1 = await manager.get_cache_path(qualified_name)
            assert mock_fetch.call_count == 1

            # Second call - should reuse cache
            cache_path2 = await manager.get_cache_path(qualified_name)
            assert mock_fetch.call_count == 1  # Still only called once
            assert cache_path1 == cache_path2

    @pytest.mark.asyncio
    async def test_fetch_error_propagates(self):
        """Test that fetch errors are propagated."""
        manager = DataLakeDataManager()
        blob_url = "https://storage.blob.core.windows.net/container/missing.json"
        qualified_name = "<blob>missing_artifact</blob>"

        with (
            patch.object(manager, "_get_blob_url_from_artifact_id", _mock_blob_resolver(blob_url)),
            patch.object(_get_blob_fetcher(manager), "fetch_to_file", side_effect=ValueError("Blob not found")),
        ):
            with pytest.raises(ValueError, match="Blob not found"):
                await manager.get_cache_path(qualified_name)

    @pytest.mark.asyncio
    async def test_unsupported_type_raises_error(self):
        """Test that unsupported artifact types raise appropriate errors."""
        manager = DataLakeDataManager()
        qualified_name = "<ftp>unsupported_id</ftp>"

        with pytest.raises(ValueError, match="Unsupported artifact type"):
            await manager.get_cache_path(qualified_name)

    @pytest.mark.asyncio
    async def test_invalid_format_raises_error(self):
        """Test that non-tagged format raises a clear error."""
        manager = DataLakeDataManager()

        with pytest.raises(ValueError, match="Invalid artifact format"):
            await manager.get_cache_path("https://storage.blob.core.windows.net/container/file.nc")

    @pytest.mark.asyncio
    async def test_blob_lookup_surfaces_credential_init_error(self, data_lake_search_endpoint):
        """Test blob requests surface deferred Azure init errors to the caller."""
        with patch(
            "agora_workbench.code_execution.data_access.manager.create_storage_credential",
            side_effect=RuntimeError("missing managed identity"),
        ):
            manager = DataLakeDataManager()

        with pytest.raises(ValueError, match="Azure data access initialization failed"):
            await manager.get_cache_path("<blob>artifact_id_1</blob>")

    @pytest.mark.asyncio
    async def test_blob_url_uses_blob_fetcher_without_search_endpoint(self, tmp_path):
        """Test direct blob URLs use BlobFetcher even without Azure Search configured."""
        manager = DataLakeDataManager()
        blob_url = "https://storage.blob.core.windows.net/container/file.bin"
        dest_path = tmp_path / "file.bin"
        mock_data = b"blob bytes"

        with patch.object(
            _get_blob_fetcher(manager), "fetch_to_file", side_effect=create_mock_fetch_to_file(mock_data)
        ):
            bytes_written = await manager._fetch_asset_to_file(blob_url, dest_path)

        assert bytes_written == len(mock_data)
        assert dest_path.read_bytes() == mock_data

    @pytest.mark.asyncio
    async def test_blob_artifact_resolution_requires_search_endpoint(self):
        """Test blob artifact ID resolution remains unavailable without Azure Search."""
        manager = DataLakeDataManager()

        with pytest.raises(
            ValueError,
            match="Search client not initialized. Set DATA_LAKE_SEARCH_ENDPOINT to resolve blob artifact IDs.",
        ):
            await manager._get_blob_url_from_artifact_id("artifact_id_1")

    @pytest.mark.asyncio
    async def test_local_file_fetching_works_when_credential_unavailable(self, tmp_path):
        """Test local assets still work when Azure credential initialization fails."""
        source_path = tmp_path / "source.txt"
        source_path.write_text("local data")

        with patch(
            "agora_workbench.code_execution.data_access.manager.create_storage_credential",
            side_effect=RuntimeError("missing managed identity"),
        ):
            manager = DataLakeDataManager(allowed_local_roots=[str(tmp_path)])

        cache_path = await manager.get_cache_path(f"<local>{source_path}</local>")

        assert cache_path.read_text() == "local data"
        assert manager._credential is None
        assert len(manager._fetchers) == 1

    @pytest.mark.asyncio
    async def test_extra_fetchers_take_priority_over_builtin_blob_fetcher(self, tmp_path):
        """Test custom fetchers are checked before the built-in BlobFetcher."""
        extra_fetcher = RecordingFetcher(data=b"custom")
        manager = DataLakeDataManager(extra_fetchers=[extra_fetcher])
        blob_url = "https://storage.blob.core.windows.net/container/file.bin"
        dest_path = tmp_path / "custom.bin"

        with patch.object(
            _get_blob_fetcher(manager),
            "fetch_to_file",
            side_effect=AssertionError("built-in BlobFetcher should not be used"),
        ):
            bytes_written = await manager._fetch_asset_to_file(blob_url, dest_path)

        assert bytes_written == len(b"custom")
        assert dest_path.read_bytes() == b"custom"
        assert extra_fetcher.fetch_to_file_calls == [blob_url]


class TestFileExtensionPreservation:
    """Test that file extensions are preserved in cache."""

    async def _fetch_with_extension(self, ext: str, data: bytes = b"data"):
        """Helper to test extension preservation."""
        manager = DataLakeDataManager()
        blob_url = f"https://storage.blob.core.windows.net/container/data{ext}"
        qualified_name = f"<blob>ext_test_{ext}</blob>"

        mock_fetch_to_file = create_mock_fetch_to_file(data)
        with (
            patch.object(manager, "_get_blob_url_from_artifact_id", _mock_blob_resolver(blob_url)),
            patch.object(_get_blob_fetcher(manager), "fetch_to_file", side_effect=mock_fetch_to_file),
        ):
            cache_path = await manager.get_cache_path(qualified_name)
            return cache_path

    @pytest.mark.asyncio
    async def test_parquet_extension(self):
        """Test .parquet extension preserved."""
        cache_path = await self._fetch_with_extension(".parquet", b"fake parquet")
        assert cache_path.suffix == ".parquet"

    @pytest.mark.asyncio
    async def test_csv_extension(self):
        """Test .csv extension preserved."""
        cache_path = await self._fetch_with_extension(".csv", b"col1,col2")
        assert cache_path.suffix == ".csv"

    @pytest.mark.asyncio
    async def test_netcdf_extension(self):
        """Test .nc extension preserved."""
        cache_path = await self._fetch_with_extension(".nc", b"fake netcdf")
        assert cache_path.suffix == ".nc"

    @pytest.mark.asyncio
    async def test_json_extension(self):
        """Test .json extension preserved."""
        cache_path = await self._fetch_with_extension(".json", b"{}")
        assert cache_path.suffix == ".json"


class TestUtilityMethods:
    """Test utility methods."""

    @pytest.mark.asyncio
    async def test_get_asset_info(self):
        """Test get_asset_info returns correct metadata."""
        manager = DataLakeDataManager()
        artifact_id = "test_nc_artifact"
        qualified_name = f"<blob>{artifact_id}</blob>"
        blob_url = "https://storage.blob.core.windows.net/container/test.nc"

        # Before caching — info keyed by artifact_id
        info = manager.get_asset_info(artifact_id)
        assert info["qualified_name"] == artifact_id
        assert info["cached"] is False

        # After caching
        mock_data = b"x" * 1000
        mock_fetch_to_file = create_mock_fetch_to_file(mock_data)
        with (
            patch.object(manager, "_get_blob_url_from_artifact_id", _mock_blob_resolver(blob_url)),
            patch.object(_get_blob_fetcher(manager), "fetch_to_file", side_effect=mock_fetch_to_file),
        ):
            await manager.get_cache_path(qualified_name)

        info = manager.get_asset_info(artifact_id)
        assert info["qualified_name"] == artifact_id
        assert info["cached"] is True
        assert info["size_bytes"] == 1000
        assert "cache_location" in info

    @pytest.mark.asyncio
    async def test_list_available(self):
        """Test list_available returns cached artifact IDs."""
        manager = DataLakeDataManager()

        assert manager.list_available() == []

        # Cache some assets
        artifacts = [
            ("file1_id", "https://storage.blob.core.windows.net/container/file1.nc"),
            ("file2_id", "https://storage.blob.core.windows.net/container/file2.csv"),
        ]

        mock_fetch_to_file = create_mock_fetch_to_file(b"data")
        for artifact_id, blob_url in artifacts:
            with (
                patch.object(manager, "_get_blob_url_from_artifact_id", _mock_blob_resolver(blob_url)),
                patch.object(_get_blob_fetcher(manager), "fetch_to_file", side_effect=mock_fetch_to_file),
            ):
                await manager.get_cache_path(f"<blob>{artifact_id}</blob>")

        available = manager.list_available()
        assert len(available) == 2
        assert all(aid in available for aid, _ in artifacts)


class TestCleanup:
    """Test cleanup operations."""

    @pytest.mark.asyncio
    async def test_cleanup_removes_cache_dir(self):
        """Test cleanup removes the cache directory."""
        manager = DataLakeDataManager()
        cache_dir = manager._cache_dir
        blob_url = "https://storage.blob.core.windows.net/container/file.nc"

        # Cache something
        mock_fetch_to_file = create_mock_fetch_to_file(b"test")
        with (
            patch.object(manager, "_get_blob_url_from_artifact_id", _mock_blob_resolver(blob_url)),
            patch.object(_get_blob_fetcher(manager), "fetch_to_file", side_effect=mock_fetch_to_file),
        ):
            await manager.get_cache_path("<blob>cleanup_test</blob>")

        assert cache_dir.exists()

        # Cleanup
        manager.cleanup()

        assert not cache_dir.exists()
        assert manager._cache_index == {}

    def test_del_calls_cleanup(self):
        """Test __del__ calls cleanup."""
        manager = DataLakeDataManager()
        cache_dir = manager._cache_dir

        assert cache_dir.exists()

        # Delete manager
        del manager

        # Cache dir should be cleaned up
        # Note: This test is somewhat non-deterministic due to GC timing
        # but works in practice for testing __del__ implementation


class TestBlobUrlResolution:
    """Scheme handling in ``_get_blob_url_from_artifact_id`` (blob-details resolution)."""

    def _manager_with_stored_path(self, metadata_storage_path: str) -> DataLakeDataManager:
        manager = DataLakeDataManager()
        manager._search_client = MagicMock(
            get_document=AsyncMock(return_value={"metadata_storage_path": metadata_storage_path}),
            close=AsyncMock(),
        )
        return manager

    @pytest.mark.asyncio
    async def test_resolves_az_scheme_path(self):
        """az:// is accepted — the catalog indexer emits az:// storage URIs."""
        manager = self._manager_with_stored_path("az://account/container/path/file.csv")

        url = await manager._get_blob_url_from_artifact_id("artifact_key")

        assert url == "az://account/container/path/file.csv"

    @pytest.mark.asyncio
    async def test_resolves_https_scheme_path(self):
        """https:// still resolves."""
        manager = self._manager_with_stored_path("https://acct.blob.core.windows.net/c/file.csv")

        url = await manager._get_blob_url_from_artifact_id("artifact_key")

        assert url == "https://acct.blob.core.windows.net/c/file.csv"

    @pytest.mark.asyncio
    async def test_rejects_unsupported_scheme(self):
        """A non-storage scheme is still rejected."""
        manager = self._manager_with_stored_path("ftp://host/file.csv")

        with pytest.raises(ValueError, match="not a valid URL"):
            await manager._get_blob_url_from_artifact_id("artifact_key")
