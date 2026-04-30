"""
Asset fetchers for different storage types.

Each fetcher handles authentication, data retrieval, and basic parsing
for a specific storage backend (Blob, SQL, Delta Lake).

Authentication:
    Fetchers use On-Behalf-Of (OBO) token exchange to access downstream
    Azure resources. The user's assertion token (scoped for the MCP server)
    is exchanged for tokens with appropriate scopes for each resource type.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, TYPE_CHECKING
from urllib.parse import urlparse

from azure.core.credentials_async import AsyncTokenCredential
from azure.core.credentials import AccessToken
from azure.storage.blob.aio import BlobServiceClient

if TYPE_CHECKING:
    from ..auth import OBOCredentialProvider

LOGGER = logging.getLogger(__name__)


class _OBOTokenCredential(AsyncTokenCredential):
    """
    Credential wrapper for OBO-exchanged tokens.

    Wraps an already-exchanged token to provide the async get_token() interface
    required by Azure SDK clients.
    """

    def __init__(self, token: str, expires_on: int):
        """
        Initialize with an exchanged token.

        Args:
            token: The OBO-exchanged access token
            expires_on: Token expiration timestamp (Unix epoch seconds)
        """
        self._token = token
        self._expires_on = expires_on

    async def get_token(
        self,
        *scopes: str,
        claims: str | None = None,
        tenant_id: str | None = None,
        enable_cae: bool = False,
        **kwargs: Any,
    ) -> AccessToken:
        """Return the pre-exchanged token."""
        return AccessToken(token=self._token, expires_on=self._expires_on)

    async def close(self) -> None:
        """No-op close for compatibility."""
        pass

    async def __aenter__(self) -> "_OBOTokenCredential":
        """Async context manager entry."""
        return self

    async def __aexit__(
        self,
        exc_type: Any = None,
        exc_val: Any = None,
        exc_tb: Any = None,
    ) -> None:
        """Async context manager exit."""
        await self.close()


class AssetFetcher(ABC):
    """Base class for asset fetchers."""

    def __init__(self, obo_provider: "OBOCredentialProvider"):
        """
        Initialize fetcher with OBO credential provider.

        Args:
            obo_provider: OBO credential provider for token exchange.
                         The provider will exchange the user's assertion token
                         for tokens with appropriate scopes for downstream resources.
        """
        self.obo_provider = obo_provider

    @abstractmethod
    async def fetch(self, qualified_name: str) -> Any:
        """
        Fetch asset data into memory.

        Args:
            qualified_name: DataLake asset qualified name

        Returns:
            Raw data (bytes, DataFrame, etc.)
        """
        pass

    @abstractmethod
    async def fetch_to_file(self, qualified_name: str, dest_path: Any) -> int:
        """
        Fetch asset data and stream directly to a file.

        Streams data to disk to avoid loading large assets into memory.

        Args:
            qualified_name: DataLake asset qualified name
            dest_path: Destination file path (Path object or string)

        Returns:
            Number of bytes written
        """
        pass

    @abstractmethod
    def can_handle(self, qualified_name: str) -> bool:
        """
        Check if this fetcher can handle the given qualified name.

        Args:
            qualified_name: DataLake asset qualified name

        Returns:
            True if this fetcher supports the asset type
        """
        pass


class BlobFetcher(AssetFetcher):
    """
    Fetcher for Azure Blob Storage / ADLS Gen2 assets.
    """

    # Azure Storage scope for OBO token exchange
    STORAGE_SCOPE = "https://storage.azure.com/.default"

    def can_handle(self, qualified_name: str) -> bool:
        """Check if this is a blob storage URL."""
        if qualified_name.startswith("abfss://"):
            return True

        if qualified_name.startswith("https://"):
            # Properly parse URL and check hostname to avoid substring injection
            try:
                parsed = urlparse(qualified_name)
                hostname = parsed.netloc.lower()
                return hostname.endswith(".blob.core.windows.net") or hostname.endswith(".dfs.core.windows.net")
            except Exception:
                return False

        return False

    async def fetch(self, qualified_name: str) -> bytes:
        """
        Fetch data from Azure Blob Storage.

        Supports:
        - abfss://container@storage.dfs.core.windows.net/path/to/file
        - https://storage.blob.core.windows.net/container/path/to/file

        Uses OBO token exchange to get a storage-scoped token from the
        user's assertion token.

        Args:
            qualified_name: Blob URL

        Returns:
            Raw bytes of the file

        Raises:
            OBOTokenExchangeError: If token exchange fails
            azure.core.exceptions.ClientAuthenticationError: If access is denied
        """
        # Parse the URL first to sanitize for logging (removes query params like SAS tokens)
        storage_account, container, blob_path = self._parse_blob_url(qualified_name)
        sanitized_url = f"{storage_account}/{container}/{blob_path}"
        LOGGER.info(f"Fetching blob asset: {sanitized_url}")

        # Exchange user token for storage-scoped token via OBO
        LOGGER.debug("Exchanging user token for storage scope via OBO")
        storage_token = await self.obo_provider.get_token_async(self.STORAGE_SCOPE)

        # Create authenticated client with the OBO-exchanged token
        account_url = f"https://{storage_account}.blob.core.windows.net"

        # Create a credential wrapper for the exchanged token
        credential = _OBOTokenCredential(storage_token.token, storage_token.expires_on)

        try:
            async with BlobServiceClient(account_url=account_url, credential=credential) as client:
                blob_client = client.get_blob_client(container=container, blob=blob_path)

                # Download blob data
                stream = await blob_client.download_blob()
                data = await stream.readall()

                LOGGER.info(f"Successfully fetched {len(data)} bytes from {sanitized_url}")
                return data

        finally:
            await credential.close()

    async def fetch_to_file(self, qualified_name: str, dest_path: Any) -> int:
        """
        Fetch blob data and stream directly to a file.

        Streams data in chunks to avoid loading large files into memory.

        Args:
            qualified_name: Blob URL
            dest_path: Destination file path

        Returns:
            Number of bytes written

        Raises:
            OBOTokenExchangeError: If token exchange fails
            azure.core.exceptions.ClientAuthenticationError: If access is denied
        """
        from pathlib import Path

        dest_path = Path(dest_path)

        # Parse the URL first to sanitize for logging
        storage_account, container, blob_path = self._parse_blob_url(qualified_name)
        sanitized_url = f"{storage_account}/{container}/{blob_path}"
        LOGGER.info(f"Streaming blob asset to file: {sanitized_url}")

        # Exchange user token for storage-scoped token via OBO
        LOGGER.debug("Exchanging user token for storage scope via OBO")
        storage_token = await self.obo_provider.get_token_async(self.STORAGE_SCOPE)

        # Create authenticated client with the OBO-exchanged token
        account_url = f"https://{storage_account}.blob.core.windows.net"
        credential = _OBOTokenCredential(storage_token.token, storage_token.expires_on)

        try:
            async with BlobServiceClient(account_url=account_url, credential=credential) as client:
                blob_client = client.get_blob_client(container=container, blob=blob_path)

                # Ensure parent directory exists
                dest_path.parent.mkdir(parents=True, exist_ok=True)

                # Download blob data and stream to file in chunks
                bytes_written = 0
                stream = await blob_client.download_blob()

                with open(dest_path, "wb") as f:
                    async for chunk in stream.chunks():
                        f.write(chunk)
                        bytes_written += len(chunk)

                LOGGER.info(f"Successfully streamed {bytes_written} bytes to {dest_path}")
                return bytes_written

        finally:
            await credential.close()

    def _parse_blob_url(self, url: str) -> tuple[str, str, str]:
        """
        Parse blob URL to extract storage account, container, and path.

        Args:
            url: Blob URL in abfss:// or https:// format

        Returns:
            Tuple of (storage_account, container, blob_path)

        Raises:
            ValueError: If URL format is malformed or unsupported
        """
        try:
            if url.startswith("abfss://"):
                # Format: abfss://container@storage.dfs.core.windows.net/path
                parsed = urlparse(url)

                # Validate netloc contains '@'
                if "@" not in parsed.netloc:
                    raise ValueError(
                        f"Malformed abfss URL: missing '@' separator in '{parsed.netloc}'. "
                        f"Expected format: abfss://container@storage.dfs.core.windows.net/path"
                    )

                netloc_parts = parsed.netloc.split("@")
                if len(netloc_parts) != 2 or not netloc_parts[0] or not netloc_parts[1]:
                    raise ValueError(
                        f"Malformed abfss URL: invalid netloc '{parsed.netloc}'. "
                        f"Expected format: abfss://container@storage.dfs.core.windows.net/path"
                    )

                container = netloc_parts[0]

                # Parse storage account from domain
                domain_parts = netloc_parts[1].split(".")
                if len(domain_parts) < 2 or not domain_parts[0]:
                    raise ValueError(
                        f"Malformed abfss URL: invalid storage domain '{netloc_parts[1]}'. "
                        f"Expected format: storage.dfs.core.windows.net"
                    )

                storage_account = domain_parts[0]
                blob_path = parsed.path.lstrip("/")

            elif url.startswith("https://"):
                # Format: https://storage.blob.core.windows.net/container/path
                parsed = urlparse(url)

                # Validate netloc
                if not parsed.netloc or "." not in parsed.netloc:
                    raise ValueError(
                        f"Malformed https URL: invalid netloc '{parsed.netloc}'. "
                        f"Expected format: https://storage.blob.core.windows.net/container/path"
                    )

                storage_account = parsed.netloc.split(".")[0]

                # Parse container and path
                path_stripped = parsed.path.lstrip("/")
                if not path_stripped:
                    raise ValueError(
                        f"Malformed https URL: missing container and path in '{url}'. "
                        f"Expected format: https://storage.blob.core.windows.net/container/path"
                    )

                path_parts = path_stripped.split("/", 1)
                container = path_parts[0]

                if not container:
                    raise ValueError(
                        f"Malformed https URL: empty container name in '{url}'. "
                        f"Expected format: https://storage.blob.core.windows.net/container/path"
                    )

                blob_path = path_parts[1] if len(path_parts) > 1 else ""

            else:
                raise ValueError(f"Unsupported blob URL format: {url}")

            return storage_account, container, blob_path

        except (IndexError, AttributeError) as e:
            raise ValueError(
                f"Failed to parse blob URL '{url}': {str(e)}. "
                f"Expected format: abfss://container@storage.dfs.core.windows.net/path "
                f"or https://storage.blob.core.windows.net/container/path"
            ) from e
