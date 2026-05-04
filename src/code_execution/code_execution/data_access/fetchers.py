"""
Asset fetchers for different storage types.

Each fetcher handles data retrieval for a specific storage backend
(Blob, SQL, Delta Lake, etc.).

Authentication:
    Fetchers accept an ``AsyncTokenCredential`` (from ``azure.core``) which
    provides tokens for downstream Azure resources. In production this is
    typically backed by managed identity.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlparse

from azure.core.credentials_async import AsyncTokenCredential
from azure.storage.blob.aio import BlobServiceClient

LOGGER = logging.getLogger(__name__)


class AssetFetcher(ABC):
    """Base class for asset fetchers."""

    def __init__(self, credential: AsyncTokenCredential):
        """
        Initialize fetcher with an async token credential.

        Args:
            credential: An ``AsyncTokenCredential`` that provides tokens for
                       downstream Azure resources (e.g. ManagedIdentityCredential).
        """
        self.credential = credential

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

    # Azure Storage scope for token acquisition
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

        Args:
            qualified_name: Blob URL

        Returns:
            Raw bytes of the file

        Raises:
            azure.core.exceptions.ClientAuthenticationError: If access is denied
        """
        # Parse the URL first to sanitize for logging (removes query params like SAS tokens)
        storage_account, container, blob_path = self._parse_blob_url(qualified_name)
        sanitized_url = f"{storage_account}/{container}/{blob_path}"
        LOGGER.info(f"Fetching blob asset: {sanitized_url}")

        # Create authenticated client with managed identity credential
        account_url = f"https://{storage_account}.blob.core.windows.net"

        async with BlobServiceClient(account_url=account_url, credential=self.credential) as client:
            blob_client = client.get_blob_client(container=container, blob=blob_path)

            # Download blob data
            stream = await blob_client.download_blob()
            data = await stream.readall()

            LOGGER.info(f"Successfully fetched {len(data)} bytes from {sanitized_url}")
            return data

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
            azure.core.exceptions.ClientAuthenticationError: If access is denied
        """
        from pathlib import Path

        dest_path = Path(dest_path)

        # Parse the URL first to sanitize for logging
        storage_account, container, blob_path = self._parse_blob_url(qualified_name)
        sanitized_url = f"{storage_account}/{container}/{blob_path}"
        LOGGER.info(f"Streaming blob asset to file: {sanitized_url}")

        # Create authenticated client with managed identity credential
        account_url = f"https://{storage_account}.blob.core.windows.net"

        async with BlobServiceClient(account_url=account_url, credential=self.credential) as client:
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
