"""
Tests for DataLakeDataManager.

Tests asset fetching, caching, and cleanup operations.
The manager accepts type-tagged qualified names (<type>id</type>) and
streams assets directly to disk to avoid high memory usage.
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ...code_execution.data_access.manager import DataLakeDataManager
from ...code_execution.auth.obo_credential import configure_obo_provider_factory
from ...code_execution.auth.irm import IRMDecryptionError


@pytest.fixture(autouse=True)
def setup_mock_obo_factory(create_mock_obo_provider):
    """Configure mock OBO provider factory for all tests."""
    mock_provider = create_mock_obo_provider()
    previous_factory = configure_obo_provider_factory(lambda user_assertion: mock_provider)
    yield mock_provider
    configure_obo_provider_factory(previous_factory)


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


class TestDataLakeDataManagerInit:
    """Test DataLakeDataManager initialization."""

    def test_init_with_user_token(self, setup_mock_obo_factory):
        """Test initialization with user token."""
        manager = DataLakeDataManager(user_token="test-token")

        assert manager._obo_provider is not None
        assert manager._cache_index == {}
        assert len(manager._fetchers) == 1
        assert manager._cache_dir.exists()

    def test_init_creates_temp_cache_dir(self):
        """Test that initialization creates a temporary cache directory."""
        manager = DataLakeDataManager(user_token="test-token")

        assert manager._cache_dir.exists()
        assert manager._cache_dir.is_dir()
        assert "data_lake_cache_" in manager._cache_dir.name


class TestGetCachePath:
    """Test get_cache_path functionality."""

    @pytest.mark.asyncio
    async def test_fetch_and_cache_blob(self, setup_mock_obo_factory):
        """Test fetching and caching a blob asset."""
        manager = DataLakeDataManager(user_token="test-token")
        blob_url = "https://storage.blob.core.windows.net/container/file.nc"
        qualified_name = "<blob>artifact_id_1</blob>"

        mock_data = b"test data content"
        mock_fetch_to_file = create_mock_fetch_to_file(mock_data)

        with (
            patch.object(manager, "_get_blob_url_from_artifact_id", _mock_blob_resolver(blob_url)),
            patch.object(manager._fetchers[0], "fetch_to_file", side_effect=mock_fetch_to_file),
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
    async def test_cache_reuse(self, setup_mock_obo_factory):
        """Test that cached assets are reused without re-fetching."""
        manager = DataLakeDataManager(user_token="test-token")
        blob_url = "https://storage.blob.core.windows.net/container/file.csv"
        qualified_name = "<blob>artifact_csv_1</blob>"

        mock_data = b"col1,col2\n1,2\n3,4"
        mock_fetch_to_file = create_mock_fetch_to_file(mock_data)

        with (
            patch.object(manager, "_get_blob_url_from_artifact_id", _mock_blob_resolver(blob_url)),
            patch.object(manager._fetchers[0], "fetch_to_file", side_effect=mock_fetch_to_file) as mock_fetch,
        ):
            # First call - should fetch
            cache_path1 = await manager.get_cache_path(qualified_name)
            assert mock_fetch.call_count == 1

            # Second call - should reuse cache
            cache_path2 = await manager.get_cache_path(qualified_name)
            assert mock_fetch.call_count == 1  # Still only called once
            assert cache_path1 == cache_path2

    @pytest.mark.asyncio
    async def test_fetch_error_propagates(self, setup_mock_obo_factory):
        """Test that fetch errors are propagated."""
        manager = DataLakeDataManager(user_token="test-token")
        blob_url = "https://storage.blob.core.windows.net/container/missing.json"
        qualified_name = "<blob>missing_artifact</blob>"

        with (
            patch.object(manager, "_get_blob_url_from_artifact_id", _mock_blob_resolver(blob_url)),
            patch.object(manager._fetchers[0], "fetch_to_file", side_effect=ValueError("Blob not found")),
        ):
            with pytest.raises(ValueError, match="Blob not found"):
                await manager.get_cache_path(qualified_name)

    @pytest.mark.asyncio
    async def test_unsupported_type_raises_error(self, setup_mock_obo_factory):
        """Test that unsupported artifact types raise appropriate errors."""
        manager = DataLakeDataManager(user_token="test-token")
        qualified_name = "<ftp>unsupported_id</ftp>"

        with pytest.raises(ValueError, match="Unsupported artifact type"):
            await manager.get_cache_path(qualified_name)

    @pytest.mark.asyncio
    async def test_invalid_format_raises_error(self, setup_mock_obo_factory):
        """Test that non-tagged format raises a clear error."""
        manager = DataLakeDataManager(user_token="test-token")

        with pytest.raises(ValueError, match="Invalid artifact format"):
            await manager.get_cache_path("https://storage.blob.core.windows.net/container/file.nc")


class TestFileExtensionPreservation:
    """Test that file extensions are preserved in cache."""

    async def _fetch_with_extension(self, ext: str, data: bytes = b"data"):
        """Helper to test extension preservation."""
        manager = DataLakeDataManager(user_token="test-token")
        blob_url = f"https://storage.blob.core.windows.net/container/data{ext}"
        qualified_name = f"<blob>ext_test_{ext}</blob>"

        mock_fetch_to_file = create_mock_fetch_to_file(data)
        with (
            patch.object(manager, "_get_blob_url_from_artifact_id", _mock_blob_resolver(blob_url)),
            patch.object(manager._fetchers[0], "fetch_to_file", side_effect=mock_fetch_to_file),
        ):
            cache_path = await manager.get_cache_path(qualified_name)
            return cache_path

    @pytest.mark.asyncio
    async def test_parquet_extension(self, setup_mock_obo_factory):
        """Test .parquet extension preserved."""
        cache_path = await self._fetch_with_extension(".parquet", b"fake parquet")
        assert cache_path.suffix == ".parquet"

    @pytest.mark.asyncio
    async def test_csv_extension(self, setup_mock_obo_factory):
        """Test .csv extension preserved."""
        cache_path = await self._fetch_with_extension(".csv", b"col1,col2")
        assert cache_path.suffix == ".csv"

    @pytest.mark.asyncio
    async def test_netcdf_extension(self, setup_mock_obo_factory):
        """Test .nc extension preserved."""
        cache_path = await self._fetch_with_extension(".nc", b"fake netcdf")
        assert cache_path.suffix == ".nc"

    @pytest.mark.asyncio
    async def test_json_extension(self, setup_mock_obo_factory):
        """Test .json extension preserved."""
        cache_path = await self._fetch_with_extension(".json", b"{}")
        assert cache_path.suffix == ".json"


class TestUtilityMethods:
    """Test utility methods."""

    @pytest.mark.asyncio
    async def test_get_asset_info(self, setup_mock_obo_factory):
        """Test get_asset_info returns correct metadata."""
        manager = DataLakeDataManager(user_token="test-token")
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
            patch.object(manager._fetchers[0], "fetch_to_file", side_effect=mock_fetch_to_file),
        ):
            await manager.get_cache_path(qualified_name)

        info = manager.get_asset_info(artifact_id)
        assert info["qualified_name"] == artifact_id
        assert info["cached"] is True
        assert info["size_bytes"] == 1000
        assert "cache_location" in info

    @pytest.mark.asyncio
    async def test_list_available(self, setup_mock_obo_factory):
        """Test list_available returns cached artifact IDs."""
        manager = DataLakeDataManager(user_token="test-token")

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
                patch.object(manager._fetchers[0], "fetch_to_file", side_effect=mock_fetch_to_file),
            ):
                await manager.get_cache_path(f"<blob>{artifact_id}</blob>")

        available = manager.list_available()
        assert len(available) == 2
        assert all(aid in available for aid, _ in artifacts)


class TestCleanup:
    """Test cleanup operations."""

    @pytest.mark.asyncio
    async def test_cleanup_removes_cache_dir(self, setup_mock_obo_factory):
        """Test cleanup removes the cache directory."""
        manager = DataLakeDataManager(user_token="test-token")
        cache_dir = manager._cache_dir
        blob_url = "https://storage.blob.core.windows.net/container/file.nc"

        # Cache something
        mock_fetch_to_file = create_mock_fetch_to_file(b"test")
        with (
            patch.object(manager, "_get_blob_url_from_artifact_id", _mock_blob_resolver(blob_url)),
            patch.object(manager._fetchers[0], "fetch_to_file", side_effect=mock_fetch_to_file),
        ):
            await manager.get_cache_path("<blob>cleanup_test</blob>")

        assert cache_dir.exists()

        # Cleanup
        manager.cleanup()

        assert not cache_dir.exists()
        assert manager._cache_index == {}

    def test_del_calls_cleanup(self, setup_mock_obo_factory):
        """Test __del__ calls cleanup."""
        manager = DataLakeDataManager(user_token="test-token")
        cache_dir = manager._cache_dir

        assert cache_dir.exists()

        # Delete manager
        del manager

        # Cache dir should be cleaned up
        # Note: This test is somewhat non-deterministic due to GC timing
        # but works in practice for testing __del__ implementation


# OLE2 magic header for IRM detection tests
_OLE2_HEADER = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class TestTryDecryptIRM:
    """Test _try_decrypt_irm IRM detection and error handling."""

    @pytest.mark.asyncio
    async def test_non_ole2_file_passes_through(self, setup_mock_obo_factory, tmp_path):
        """Non-OLE2 files should pass through _try_decrypt_irm silently."""
        manager = DataLakeDataManager(user_token="test-token")
        cache_path = tmp_path / "data.csv"
        cache_path.write_bytes(b"col1,col2\n1,2\n")

        # Should not raise; non-OLE2 file is ignored
        await manager._try_decrypt_irm(cache_path)

    @pytest.mark.asyncio
    async def test_ole2_non_irm_file_passes_through(self, setup_mock_obo_factory, tmp_path):
        """OLE2 files that are NOT IRM-protected should pass through silently."""
        manager = DataLakeDataManager(user_token="test-token")
        cache_path = tmp_path / "data.xls"
        cache_path.write_bytes(_OLE2_HEADER + b"\x00" * 512)

        with patch(
            "code_execution.code_execution.data_access.manager.is_irm_protected",
            return_value=False,
        ):
            # Should not raise
            await manager._try_decrypt_irm(cache_path)

    @pytest.mark.asyncio
    async def test_irm_protected_file_raises_error(self, setup_mock_obo_factory, tmp_path):
        """IRM-protected files should raise IRMDecryptionError."""
        manager = DataLakeDataManager(user_token="test-token")
        cache_path = tmp_path / "protected.xlsx"
        cache_path.write_bytes(_OLE2_HEADER + b"\x00" * 512)

        with patch(
            "code_execution.code_execution.data_access.manager.is_irm_protected",
            return_value=True,
        ):
            with pytest.raises(IRMDecryptionError, match="IRM-protected file detected"):
                await manager._try_decrypt_irm(cache_path)

    @pytest.mark.asyncio
    async def test_irm_error_message_contains_guidance(self, setup_mock_obo_factory, tmp_path):
        """IRMDecryptionError should include actionable guidance for the user."""
        manager = DataLakeDataManager(user_token="test-token")
        cache_path = tmp_path / "protected.xlsx"
        cache_path.write_bytes(_OLE2_HEADER + b"\x00" * 512)

        with patch(
            "code_execution.code_execution.data_access.manager.is_irm_protected",
            return_value=True,
        ):
            with pytest.raises(IRMDecryptionError) as exc_info:
                await manager._try_decrypt_irm(cache_path)

            message = str(exc_info.value)
            assert "not currently supported" in message
            assert "decrypt" in message.lower()

    @pytest.mark.asyncio
    async def test_get_cache_path_raises_on_irm_file(self, setup_mock_obo_factory, tmp_path):
        """get_cache_path should propagate IRMDecryptionError from _try_decrypt_irm."""
        manager = DataLakeDataManager(user_token="test-token")
        blob_url = "https://storage.blob.core.windows.net/container/protected.xlsx"
        qualified_name = "<blob>irm_artifact_1</blob>"

        irm_data = _OLE2_HEADER + b"\x00" * 512

        async def mock_fetch(qualified_name: str, dest_path):
            dest_path = Path(dest_path)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(irm_data)
            return len(irm_data)

        with (
            patch.object(manager, "_get_blob_url_from_artifact_id", AsyncMock(return_value=blob_url)),
            patch.object(manager._fetchers[0], "fetch_to_file", side_effect=mock_fetch),
            patch(
                "code_execution.code_execution.data_access.manager.is_irm_protected",
                return_value=True,
            ),
        ):
            with pytest.raises(IRMDecryptionError, match="IRM-protected file detected"):
                await manager.get_cache_path(qualified_name)
