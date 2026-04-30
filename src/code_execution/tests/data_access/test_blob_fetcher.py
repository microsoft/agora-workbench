"""
Comprehensive tests for BlobFetcher class.

Tests authentication, URL parsing, error handling, and basic functionality.
"""

import pytest
from unittest.mock import AsyncMock, Mock

from azure.core.exceptions import (
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
)

from ...code_execution.data_access.fetchers import BlobFetcher


@pytest.fixture
def mock_obo_provider(create_mock_obo_provider):
    """Provide a default mock OBO provider for tests."""
    return create_mock_obo_provider()


@pytest.fixture
def mock_azure_blob_client(monkeypatch):
    """
    Fixture that patches Azure BlobServiceClient for blob fetcher tests.

    Automatically patches the Azure SDK components in the fetchers module.
    Returns a configured mock object with a configure() method.

    Usage:
        def test_something(mock_azure_blob_client, mock_obo_provider):
            mock_azure_blob_client.configure(expected_data=b"test")
            fetcher = BlobFetcher(obo_provider=mock_obo_provider)
            result = await fetcher.fetch(url)  # Uses mocked Azure SDK
    """
    # Import the actual module to patch it directly
    from ...code_execution.data_access import fetchers

    class MockAzureBlobClient:
        def __init__(self):
            self.mock_client = None
            self.mock_cred = None
            self.mock_client_class = None
            self.mock_cred_class = None
            self._setup_defaults()

        def _setup_defaults(self):
            """Setup default mocks."""
            # Setup credential mock
            self.mock_cred = AsyncMock()
            self.mock_cred.close = AsyncMock()
            self.mock_cred_class = Mock(return_value=self.mock_cred)

            # Setup BlobServiceClient mock
            self.mock_client = AsyncMock()
            self.mock_blob_client = AsyncMock()
            self.mock_stream = AsyncMock()

            # Default behavior: return empty bytes
            self.mock_stream.readall = AsyncMock(return_value=b"")
            self.mock_blob_client.download_blob = AsyncMock(return_value=self.mock_stream)
            self.mock_client.get_blob_client = Mock(return_value=self.mock_blob_client)

            # Mock async context manager
            self.mock_client.__aenter__ = AsyncMock(return_value=self.mock_client)
            self.mock_client.__aexit__ = AsyncMock(return_value=None)

            self.mock_client_class = Mock(return_value=self.mock_client)

            # Apply patches using the actual module object
            monkeypatch.setattr(fetchers, "BlobServiceClient", self.mock_client_class)
            monkeypatch.setattr(fetchers, "_OBOTokenCredential", self.mock_cred_class)

        def configure(self, expected_data: bytes = b"test data", should_raise=None):
            """
            Configure the mock behavior.

            Args:
                expected_data: The bytes to return from readall()
                should_raise: Exception to raise during download_blob(), or None
            """
            if should_raise:
                self.mock_blob_client.download_blob = AsyncMock(side_effect=should_raise)
            else:
                self.mock_stream.readall = AsyncMock(return_value=expected_data)
                self.mock_blob_client.download_blob = AsyncMock(return_value=self.mock_stream)

    return MockAzureBlobClient()


class TestBlobFetcherInitialization:
    """Test BlobFetcher initialization."""

    def test_init_stores_obo_provider(self, mock_obo_provider):
        """Test that fetcher stores the provided OBO provider."""
        fetcher = BlobFetcher(obo_provider=mock_obo_provider)

        assert fetcher.obo_provider is mock_obo_provider

    def test_init_different_providers(self, create_mock_obo_provider):
        """Test multiple fetchers with different providers."""
        provider1 = create_mock_obo_provider(token="token1")
        provider2 = create_mock_obo_provider(token="token2")
        fetcher1 = BlobFetcher(obo_provider=provider1)
        fetcher2 = BlobFetcher(obo_provider=provider2)

        assert fetcher1.obo_provider is provider1
        assert fetcher2.obo_provider is provider2
        assert fetcher1.obo_provider is not fetcher2.obo_provider


class TestBlobFetcherURLRecognition:
    """Test URL format recognition via can_handle()."""

    def test_can_handle_abfss_urls(self, mock_obo_provider):
        """Test can_handle recognizes abfss:// URLs."""
        fetcher = BlobFetcher(obo_provider=mock_obo_provider)

        assert fetcher.can_handle("abfss://container@storage.dfs.core.windows.net/file.csv")
        assert fetcher.can_handle("abfss://data@account.dfs.core.windows.net/path/to/file.parquet")
        assert fetcher.can_handle("abfss://c@s.dfs.core.windows.net/f")

    def test_can_handle_https_blob_urls(self, mock_obo_provider):
        """Test can_handle recognizes https blob.core.windows.net URLs."""
        fetcher = BlobFetcher(obo_provider=mock_obo_provider)

        assert fetcher.can_handle("https://storage.blob.core.windows.net/container/file.csv")
        assert fetcher.can_handle("https://account.blob.core.windows.net/data/path/file.json")

    def test_can_handle_https_dfs_urls(self, mock_obo_provider):
        """Test can_handle recognizes https dfs.core.windows.net URLs."""
        fetcher = BlobFetcher(obo_provider=mock_obo_provider)

        assert fetcher.can_handle("https://storage.dfs.core.windows.net/container/file.csv")
        assert fetcher.can_handle("https://myaccount.dfs.core.windows.net/mycontainer/data.parquet")

    def test_can_handle_rejects_non_azure_urls(self, mock_obo_provider):
        """Test can_handle rejects non-Azure URLs."""
        fetcher = BlobFetcher(obo_provider=mock_obo_provider)

        assert not fetcher.can_handle("https://example.com/file.csv")
        assert not fetcher.can_handle("https://s3.amazonaws.com/bucket/file.csv")
        assert not fetcher.can_handle("https://storage.googleapis.com/bucket/file.csv")
        assert not fetcher.can_handle("ftp://storage.example.com/file.csv")
        assert not fetcher.can_handle("file:///local/path/file.csv")
        assert not fetcher.can_handle("/local/path/file.csv")
        assert not fetcher.can_handle("file.csv")

    def test_can_handle_rejects_wrong_protocol(self, mock_obo_provider):
        """Test can_handle rejects blob URLs with wrong protocol."""
        fetcher = BlobFetcher(obo_provider=mock_obo_provider)

        assert not fetcher.can_handle("http://storage.blob.core.windows.net/container/file.csv")
        assert not fetcher.can_handle("ftp://storage.blob.core.windows.net/container/file.csv")
        assert not fetcher.can_handle("ws://storage.dfs.core.windows.net/container/file.csv")

    def test_can_handle_case_sensitive_protocol(self, mock_obo_provider):
        """Test protocol matching is case-sensitive."""
        fetcher = BlobFetcher(obo_provider=mock_obo_provider)

        # Protocols should be lowercase
        assert fetcher.can_handle("abfss://container@storage.dfs.core.windows.net/file.csv")
        assert fetcher.can_handle("https://storage.blob.core.windows.net/container/file.csv")

        # These should fail (uppercase protocols)
        assert not fetcher.can_handle("ABFSS://container@storage.dfs.core.windows.net/file.csv")
        assert not fetcher.can_handle("HTTPS://storage.blob.core.windows.net/container/file.csv")


class TestBlobFetcherErrorMessages:
    """Test that error messages are clear and helpful."""

    @pytest.mark.asyncio
    async def test_malformed_url_error_includes_format_hint(self, mock_obo_provider):
        """Test that malformed URL errors include expected format."""
        fetcher = BlobFetcher(obo_provider=mock_obo_provider)

        with pytest.raises(ValueError) as exc_info:
            await fetcher.fetch("abfss://containerstorage.dfs.core.windows.net/file.csv")

        error_message = str(exc_info.value)
        assert "missing '@' separator" in error_message
        assert "Expected format:" in error_message
        assert "abfss://container@storage" in error_message

    @pytest.mark.asyncio
    async def test_unsupported_protocol_error_clear(self, mock_obo_provider):
        """Test unsupported protocol error is clear."""
        fetcher = BlobFetcher(obo_provider=mock_obo_provider)

        with pytest.raises(ValueError) as exc_info:
            await fetcher.fetch("ftp://storage.example.com/file.csv")

        assert "Unsupported blob URL format" in str(exc_info.value)
        assert "ftp://storage.example.com/file.csv" in str(exc_info.value)

    def test_parse_error_includes_actual_url(self, mock_obo_provider):
        """Test parse errors include the problematic URL."""
        fetcher = BlobFetcher(obo_provider=mock_obo_provider)
        malformed_url = "abfss://@storage.dfs.core.windows.net/file"

        with pytest.raises(ValueError) as exc_info:
            fetcher._parse_blob_url(malformed_url)

        # Error should mention the URL that failed
        assert "invalid netloc" in str(exc_info.value)


class TestBlobFetcherEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_can_handle_with_query_parameters(self, mock_obo_provider):
        """Test URLs with query parameters."""
        fetcher = BlobFetcher(obo_provider=mock_obo_provider)

        # Should still handle URLs with SAS tokens or query params
        assert fetcher.can_handle("https://storage.blob.core.windows.net/container/file.csv?sv=2021-06-08&se=2023")

    def test_parse_url_with_query_parameters(self, mock_obo_provider):
        """Test parsing URL with query parameters (SAS token)."""
        fetcher = BlobFetcher(obo_provider=mock_obo_provider)
        url = "https://storage.blob.core.windows.net/container/file.csv?sv=2021-06-08"

        storage, container, path = fetcher._parse_blob_url(url)

        assert storage == "storage"
        assert container == "container"
        # Path should not include query parameters
        assert "file.csv" in path
        assert "?" not in container  # Query shouldn't end up in container

    def test_parse_url_with_unicode_characters(self, mock_obo_provider):
        """Test URL with Unicode characters in path."""
        fetcher = BlobFetcher(obo_provider=mock_obo_provider)
        url = "abfss://container@storage.dfs.core.windows.net/données/fichier.csv"

        storage, container, path = fetcher._parse_blob_url(url)

        assert storage == "storage"
        assert container == "container"
        assert "données/fichier.csv" in path

    def test_can_handle_very_long_urls(self, mock_obo_provider):
        """Test handling of very long URLs."""
        fetcher = BlobFetcher(obo_provider=mock_obo_provider)

        long_path = "/".join([f"level{i}" for i in range(100)])
        url = f"abfss://container@storage.dfs.core.windows.net/{long_path}/file.csv"

        assert fetcher.can_handle(url)

    def test_parse_minimal_valid_abfss_url(self, mock_obo_provider):
        """Test parsing minimal valid abfss URL."""
        fetcher = BlobFetcher(obo_provider=mock_obo_provider)
        url = "abfss://c@s.dfs.core.windows.net/f"

        storage, container, path = fetcher._parse_blob_url(url)

        assert storage == "s"
        assert container == "c"
        assert path == "f"

    def test_parse_minimal_valid_https_url(self, mock_obo_provider):
        """Test parsing minimal valid https URL."""
        fetcher = BlobFetcher(obo_provider=mock_obo_provider)
        url = "https://s.blob.core.windows.net/c"

        storage, container, path = fetcher._parse_blob_url(url)

        assert storage == "s"
        assert container == "c"
        assert path == ""


class TestBlobFetcherDataRetrieval:
    """Integration tests for data fetching with mocked Azure SDK."""

    @pytest.mark.asyncio
    async def test_successful_fetch_abfss_url(self, mock_azure_blob_client, mock_obo_provider):
        """Test successful data fetch with abfss URL."""
        expected_data = b"test,data\n1,2\n3,4"
        mock_azure_blob_client.configure(expected_data=expected_data)

        fetcher = BlobFetcher(obo_provider=mock_obo_provider)
        url = "abfss://mycontainer@mystorage.dfs.core.windows.net/path/to/data.csv"

        result = await fetcher.fetch(url)

        assert result == expected_data
        mock_azure_blob_client.mock_client.get_blob_client.assert_called_once_with(
            container="mycontainer", blob="path/to/data.csv"
        )

    @pytest.mark.asyncio
    async def test_successful_fetch_https_url(self, mock_azure_blob_client, mock_obo_provider):
        """Test successful data fetch with https URL."""
        expected_data = b'{"key": "value"}'
        mock_azure_blob_client.configure(expected_data=expected_data)

        fetcher = BlobFetcher(obo_provider=mock_obo_provider)
        url = "https://mystorage.blob.core.windows.net/mycontainer/path/to/data.json"

        result = await fetcher.fetch(url)

        assert result == expected_data
        mock_azure_blob_client.mock_client.get_blob_client.assert_called_once_with(
            container="mycontainer", blob="path/to/data.json"
        )

    @pytest.mark.asyncio
    async def test_fetch_uses_correct_account_url(self, mock_azure_blob_client, mock_obo_provider):
        """Test that fetch constructs correct storage account URL."""
        mock_azure_blob_client.configure(expected_data=b"data")

        fetcher = BlobFetcher(obo_provider=mock_obo_provider)
        url = "abfss://container@mystorage.dfs.core.windows.net/file.csv"

        await fetcher.fetch(url)

        # Verify BlobServiceClient was created with correct account URL
        mock_azure_blob_client.mock_client_class.assert_called_once()
        call_kwargs = mock_azure_blob_client.mock_client_class.call_args[1]
        assert call_kwargs["account_url"] == "https://mystorage.blob.core.windows.net"

    @pytest.mark.asyncio
    async def test_fetch_empty_blob(self, mock_azure_blob_client, mock_obo_provider):
        """Test fetching an empty blob."""
        mock_azure_blob_client.configure(expected_data=b"")

        fetcher = BlobFetcher(obo_provider=mock_obo_provider)
        url = "abfss://container@storage.dfs.core.windows.net/empty.txt"

        result = await fetcher.fetch(url)

        assert result == b""

    @pytest.mark.asyncio
    async def test_fetch_large_blob(self, mock_azure_blob_client, mock_obo_provider):
        """Test fetching a large blob."""
        large_data = b"x" * (10 * 1024 * 1024)  # 10 MB
        mock_azure_blob_client.configure(expected_data=large_data)

        fetcher = BlobFetcher(obo_provider=mock_obo_provider)
        url = "abfss://container@storage.dfs.core.windows.net/large.bin"

        result = await fetcher.fetch(url)

        assert result == large_data
        assert len(result) == 10 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_credential_closed_after_successful_fetch(self, mock_azure_blob_client, mock_obo_provider):
        """Test that credential is closed after successful fetch."""
        mock_azure_blob_client.configure(expected_data=b"data")

        fetcher = BlobFetcher(obo_provider=mock_obo_provider)
        url = "abfss://container@storage.dfs.core.windows.net/file.csv"

        await fetcher.fetch(url)

        # Verify credential was closed
        mock_azure_blob_client.mock_cred.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_blob_not_found_error(self, mock_azure_blob_client, mock_obo_provider):
        """Test handling of blob not found error."""
        error = ResourceNotFoundError("Blob not found")
        mock_azure_blob_client.configure(should_raise=error)

        fetcher = BlobFetcher(obo_provider=mock_obo_provider)
        url = "abfss://container@storage.dfs.core.windows.net/nonexistent.csv"

        with pytest.raises(ResourceNotFoundError, match="Blob not found"):
            await fetcher.fetch(url)

        # Verify credential was closed even on error
        mock_azure_blob_client.mock_cred.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_authentication_error(self, mock_azure_blob_client, mock_obo_provider):
        """Test handling of authentication failure."""
        auth_error = HttpResponseError("Authentication failed")
        auth_error.status_code = 401
        mock_azure_blob_client.configure(should_raise=auth_error)

        fetcher = BlobFetcher(obo_provider=mock_obo_provider)
        url = "abfss://container@storage.dfs.core.windows.net/file.csv"

        with pytest.raises(HttpResponseError, match="Authentication failed"):
            await fetcher.fetch(url)

        mock_azure_blob_client.mock_cred.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_permission_denied_error(self, mock_azure_blob_client, mock_obo_provider):
        """Test handling of permission denied error."""
        permission_error = HttpResponseError("Access denied")
        permission_error.status_code = 403
        mock_azure_blob_client.configure(should_raise=permission_error)

        fetcher = BlobFetcher(obo_provider=mock_obo_provider)
        url = "abfss://container@storage.dfs.core.windows.net/restricted.csv"

        with pytest.raises(HttpResponseError, match="Access denied"):
            await fetcher.fetch(url)

        mock_azure_blob_client.mock_cred.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_network_error(self, mock_azure_blob_client, mock_obo_provider):
        """Test handling of network connectivity error."""
        network_error = ServiceRequestError("Connection timeout")
        mock_azure_blob_client.configure(should_raise=network_error)

        fetcher = BlobFetcher(obo_provider=mock_obo_provider)
        url = "abfss://container@storage.dfs.core.windows.net/file.csv"

        with pytest.raises(ServiceRequestError, match="Connection timeout"):
            await fetcher.fetch(url)

        mock_azure_blob_client.mock_cred.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_fetch_with_path_containing_special_chars(self, mock_azure_blob_client, mock_obo_provider):
        """Test fetching blob with special characters in path."""
        mock_azure_blob_client.configure(expected_data=b"data")

        fetcher = BlobFetcher(obo_provider=mock_obo_provider)
        url = "abfss://container@storage.dfs.core.windows.net/path with spaces/file-name_123.csv"

        result = await fetcher.fetch(url)

        assert result == b"data"
        mock_azure_blob_client.mock_client.get_blob_client.assert_called_once_with(
            container="container", blob="path with spaces/file-name_123.csv"
        )
