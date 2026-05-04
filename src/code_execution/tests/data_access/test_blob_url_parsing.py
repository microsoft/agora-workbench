"""
Tests for blob URL parsing validation in BlobFetcher.

Tests ensure that malformed URLs are rejected with clear error messages
before string operations that could raise IndexError.
"""

import pytest

from ...code_execution.data_access.fetchers import BlobFetcher


@pytest.fixture
def mock_credential(create_mock_credential):
    """Provide a mock credential for tests."""
    return create_mock_credential()


class TestBlobURLParsingValidation:
    """Test URL parsing validation and error handling."""

    def test_valid_abfss_url(self, mock_credential):
        """Test parsing valid abfss URL."""
        fetcher = BlobFetcher(credential=mock_credential)
        url = "abfss://container@storage.dfs.core.windows.net/path/to/file.csv"

        storage, container, path = fetcher._parse_blob_url(url)

        assert storage == "storage"
        assert container == "container"
        assert path == "path/to/file.csv"

    def test_valid_https_url(self, mock_credential):
        """Test parsing valid https URL."""
        fetcher = BlobFetcher(credential=mock_credential)
        url = "https://storage.blob.core.windows.net/container/path/to/file.csv"

        storage, container, path = fetcher._parse_blob_url(url)

        assert storage == "storage"
        assert container == "container"
        assert path == "path/to/file.csv"

    def test_https_url_without_blob_path(self, mock_credential):
        """Test https URL with only container (no blob path)."""
        fetcher = BlobFetcher(credential=mock_credential)
        url = "https://storage.blob.core.windows.net/container"

        storage, container, path = fetcher._parse_blob_url(url)

        assert storage == "storage"
        assert container == "container"
        assert path == ""

    def test_parse_abfss_with_nested_path(self, mock_credential):
        """Test abfss URL with deeply nested path."""
        fetcher = BlobFetcher(credential=mock_credential)
        url = "abfss://data@storage.dfs.core.windows.net/year/month/day/hour/file.parquet"

        storage, container, path = fetcher._parse_blob_url(url)

        assert storage == "storage"
        assert container == "data"
        assert path == "year/month/day/hour/file.parquet"

    def test_parse_url_with_special_characters(self, mock_credential):
        """Test URL with special characters in path."""
        fetcher = BlobFetcher(credential=mock_credential)
        url = "abfss://container@storage.dfs.core.windows.net/path with spaces/file-name_123.csv"

        storage, container, path = fetcher._parse_blob_url(url)

        assert storage == "storage"
        assert container == "container"
        assert path == "path with spaces/file-name_123.csv"

    def test_abfss_missing_at_symbol(self, mock_credential):
        """Test abfss URL missing '@' separator."""
        fetcher = BlobFetcher(credential=mock_credential)
        url = "abfss://containerstorage.dfs.core.windows.net/path/file.csv"

        with pytest.raises(ValueError, match="missing '@' separator"):
            fetcher._parse_blob_url(url)

    def test_abfss_empty_container(self, mock_credential):
        """Test abfss URL with empty container name."""
        fetcher = BlobFetcher(credential=mock_credential)
        url = "abfss://@storage.dfs.core.windows.net/path/file.csv"

        with pytest.raises(ValueError, match="invalid netloc"):
            fetcher._parse_blob_url(url)

    def test_abfss_empty_storage_account(self, mock_credential):
        """Test abfss URL with empty storage account."""
        fetcher = BlobFetcher(credential=mock_credential)
        url = "abfss://container@/path/file.csv"

        with pytest.raises(ValueError, match="invalid netloc"):
            fetcher._parse_blob_url(url)

    def test_abfss_malformed_domain(self, mock_credential):
        """Test abfss URL with malformed domain."""
        fetcher = BlobFetcher(credential=mock_credential)
        url = "abfss://container@storage/path/file.csv"

        with pytest.raises(ValueError, match="invalid storage domain"):
            fetcher._parse_blob_url(url)

    def test_abfss_multiple_at_symbols(self, mock_credential):
        """Test abfss URL with multiple '@' symbols."""
        fetcher = BlobFetcher(credential=mock_credential)
        url = "abfss://container@extra@storage.dfs.core.windows.net/path/file.csv"

        with pytest.raises(ValueError, match="invalid netloc"):
            fetcher._parse_blob_url(url)

    def test_https_empty_netloc(self, mock_credential):
        """Test https URL with empty netloc."""
        fetcher = BlobFetcher(credential=mock_credential)
        url = "https:///container/path/file.csv"

        with pytest.raises(ValueError, match="invalid netloc"):
            fetcher._parse_blob_url(url)

    def test_https_missing_container(self, mock_credential):
        """Test https URL missing container in path."""
        fetcher = BlobFetcher(credential=mock_credential)
        url = "https://storage.blob.core.windows.net/"

        with pytest.raises(ValueError, match="missing container and path"):
            fetcher._parse_blob_url(url)

    def test_https_no_path(self, mock_credential):
        """Test https URL with no path at all."""
        fetcher = BlobFetcher(credential=mock_credential)
        url = "https://storage.blob.core.windows.net"

        with pytest.raises(ValueError, match="missing container and path"):
            fetcher._parse_blob_url(url)

    def test_unsupported_protocol(self, mock_credential):
        """Test URL with unsupported protocol."""
        fetcher = BlobFetcher(credential=mock_credential)
        url = "ftp://storage.example.com/container/file.csv"

        with pytest.raises(ValueError, match="Unsupported blob URL format"):
            fetcher._parse_blob_url(url)

    def test_abfss_no_domain_extension(self, mock_credential):
        """Test abfss URL with storage account but no domain extension."""
        fetcher = BlobFetcher(credential=mock_credential)
        url = "abfss://container@storage"

        with pytest.raises(ValueError, match="invalid storage domain"):
            fetcher._parse_blob_url(url)

    def test_can_handle_valid_abfss(self, mock_credential):
        """Test can_handle recognizes valid abfss URLs."""
        fetcher = BlobFetcher(credential=mock_credential)

        assert fetcher.can_handle("abfss://container@storage.dfs.core.windows.net/file.csv")

    def test_can_handle_valid_https_blob(self, mock_credential):
        """Test can_handle recognizes valid https blob URLs."""
        fetcher = BlobFetcher(credential=mock_credential)

        assert fetcher.can_handle("https://storage.blob.core.windows.net/container/file.csv")

    def test_can_handle_valid_https_dfs(self, mock_credential):
        """Test can_handle recognizes valid https dfs URLs."""
        fetcher = BlobFetcher(credential=mock_credential)

        assert fetcher.can_handle("https://storage.dfs.core.windows.net/container/file.csv")

    def test_can_handle_rejects_non_azure(self, mock_credential):
        """Test can_handle rejects non-Azure URLs."""
        fetcher = BlobFetcher(credential=mock_credential)

        assert not fetcher.can_handle("https://example.com/file.csv")
        assert not fetcher.can_handle("ftp://storage.blob.core.windows.net/file.csv")
        assert not fetcher.can_handle("file:///local/path/file.csv")
