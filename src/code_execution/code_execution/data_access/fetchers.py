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
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from azure.core.credentials_async import AsyncTokenCredential
from azure.storage.blob.aio import BlobServiceClient

LOGGER = logging.getLogger(__name__)


class AssetFetcher(ABC):
    """Base class for asset fetchers."""

    def __init__(self, credential: AsyncTokenCredential | None = None):
        """
        Initialize fetcher with an optional async token credential.

        Args:
            credential: An ``AsyncTokenCredential`` that provides tokens for
                       downstream Azure resources (e.g. ManagedIdentityCredential).
                       May be ``None`` for fetchers that don't require credentials
                       (e.g. local filesystem).
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


class LocalFileFetcher(AssetFetcher):
    """
    Fetcher for local filesystem paths.

    Handles absolute paths, relative paths, and ``file://`` URIs.
    No credentials are required.

    Security:
        An ``allowed_roots`` list restricts which directories the fetcher
        may read from.  Every resolved path is checked against these roots
        before any I/O occurs.  If *allowed_roots* is empty, **all** paths
        are permitted (use only inside a sandboxed container).
    """

    def __init__(self, allowed_roots: list[str] | None = None):
        """
        Initialize the local file fetcher.

        Args:
            allowed_roots: Optional list of directory paths that the fetcher
                is allowed to read from.  Paths are resolved to absolute form.
                If ``None`` or empty, all paths are permitted.
        """
        super().__init__(credential=None)
        self._allowed_roots: list[Path] = [Path(r).resolve() for r in (allowed_roots or [])]

    def can_handle(self, qualified_name: str) -> bool:
        """Check if this is a local filesystem path."""
        return (
            qualified_name.startswith("/")
            or qualified_name.startswith("./")
            or qualified_name.startswith("../")
            or qualified_name.startswith("file://")
        )

    async def fetch(self, qualified_name: str) -> bytes:
        """
        Read a local file into memory.

        Args:
            qualified_name: Local file path or ``file://`` URI.

        Returns:
            Raw bytes of the file.

        Raises:
            FileNotFoundError: If the file does not exist.
            PermissionError: If the resolved path is outside *allowed_roots*.
        """
        path = self._resolve_and_check(qualified_name)
        LOGGER.info(f"Reading local file: {path}")
        data = path.read_bytes()
        LOGGER.info(f"Read {len(data)} bytes from {path}")
        return data

    async def fetch_to_file(self, qualified_name: str, dest_path: Any) -> int:
        """
        Copy a local file to *dest_path*.

        Args:
            qualified_name: Local file path or ``file://`` URI.
            dest_path: Destination file path.

        Returns:
            Number of bytes written.

        Raises:
            FileNotFoundError: If the source file does not exist.
            PermissionError: If the resolved path is outside *allowed_roots*.
        """
        import shutil
        from pathlib import Path as P

        source = self._resolve_and_check(qualified_name)
        dest = P(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        LOGGER.info(f"Copying local file {source} -> {dest}")
        shutil.copy2(source, dest)
        size = source.stat().st_size
        LOGGER.info(f"Copied {size} bytes to {dest}")
        return size

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_and_check(self, qualified_name: str) -> Path:
        """Resolve the path and validate against allowed roots."""
        raw = qualified_name
        if raw.startswith("file://"):
            raw = raw[7:]

        path = Path(raw).resolve()

        if not path.exists():
            raise FileNotFoundError(f"Local file not found: {path}")

        if self._allowed_roots:
            if not any(self._is_within(path, root) for root in self._allowed_roots):
                raise PermissionError(
                    f"Access denied: {path} is outside allowed roots {[str(r) for r in self._allowed_roots]}"
                )

        return path

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Check if *path* is under *root* (both must be resolved)."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False
