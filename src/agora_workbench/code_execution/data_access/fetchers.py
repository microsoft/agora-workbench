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
import os
import stat
import asyncio
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from agora_workbench.data_lake.errors import (
    InvalidRequestError,
    PermissionDeniedError,
    TransferTimeoutError,
    UnsupportedOperationError,
)
from agora_workbench.data_lake.identity import (
    AzureBlobScope,
    parse_azure_uri,
    sanitize_uri_for_display,
    validate_azure_object_path,
)
from agora_workbench.data_lake.models import RequestContext
from agora_workbench.data_lake.transfer import TransferOptions, TransferResult, await_transfer, stream_chunks_to_file

if TYPE_CHECKING:
    from azure.core.credentials_async import AsyncTokenCredential
    from azure.storage.blob.aio import BlobServiceClient as AzureBlobServiceClient

BlobServiceClient: Any = None

LOGGER = logging.getLogger(__name__)

# Tunable via environment variables for deployment-specific optimization.
# Parallel streams for large blob downloads (default: 4).
_BLOB_MAX_CONCURRENCY = int(os.getenv("MCP_BLOB_MAX_CONCURRENCY", "4"))
# Chunk size per range request in bytes (default: 4 MiB). The SDK may retain
# one chunk per concurrent request, so this is also part of the documented
# streaming memory budget.
_BLOB_CHUNK_SIZE = int(os.getenv("MCP_BLOB_CHUNK_SIZE", str(4 * 1024 * 1024)))
# Files smaller than this are fetched in a single GET (default: 4 MiB).
_BLOB_MAX_SINGLE_GET = int(os.getenv("MCP_BLOB_MAX_SINGLE_GET", str(4 * 1024 * 1024)))


class AssetFetcher(ABC):
    """Base class for asset fetchers.

    ``fetch`` is a full-memory convenience. Use ``fetch_to_file`` (or
    ``fetch_to_file_result`` when diagnostics are needed) for bounded-memory
    transfer.
    """

    def __init__(self, credential: "AsyncTokenCredential | None" = None):
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
    async def fetch_to_file(
        self,
        qualified_name: str,
        dest_path: Any,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> int:
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

    async def fetch_to_file_result(
        self,
        qualified_name: str,
        dest_path: Any,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> TransferResult:
        """Return detailed transfer diagnostics when the fetcher supports them."""
        raise UnsupportedOperationError(
            f"{type(self).__name__} does not implement bounded streaming.",
            operation="download",
        )

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

    Maintains a per-account client cache to amortize TCP/TLS handshake and
    token acquisition costs across multiple fetches.
    """

    # Azure Storage scope for token acquisition
    STORAGE_SCOPE = "https://storage.azure.com/.default"

    def __init__(
        self,
        credential: "AsyncTokenCredential | None" = None,
        *,
        allowed_locations: list[str | AzureBlobScope] | None = None,
        allow_reserved_paths: bool = False,
    ):
        super().__init__(credential=credential)
        # Cache of account_url -> BlobServiceClient for connection reuse
        self._clients: dict[str, "AzureBlobServiceClient"] = {}
        self._allowed_scopes = tuple(
            value if isinstance(value, AzureBlobScope) else AzureBlobScope.from_uri(value)
            for value in (allowed_locations or [])
        )
        self._allow_reserved_paths = allow_reserved_paths

    def _get_client(self, account_url: str) -> "AzureBlobServiceClient":
        """Get or create a long-lived BlobServiceClient for the given account."""
        if account_url not in self._clients:
            client_class = BlobServiceClient
            if client_class is None:
                try:
                    from azure.storage.blob.aio import BlobServiceClient as client_class
                except ImportError as exc:
                    raise RuntimeError("Azure Blob fetching requires the 'agora-workbench[azure]' extra.") from exc
            self._clients[account_url] = client_class(
                account_url=account_url,
                credential=self.credential,
                max_single_get_size=_BLOB_MAX_SINGLE_GET,
                max_chunk_get_size=_BLOB_CHUNK_SIZE,
            )
        return self._clients[account_url]

    async def close(self) -> None:
        """Close all cached blob service clients."""
        for client in self._clients.values():
            await client.close()
        self._clients.clear()

    def can_handle(self, qualified_name: str) -> bool:
        """Check if this is a blob storage URL."""
        if qualified_name.startswith("abfss://"):
            return True

        if qualified_name.startswith("az://"):
            # az://account/container/blob — the scheme emitted by the catalog
            # indexer. Structural validation happens in _parse_blob_url.
            return True

        if qualified_name.startswith("https://"):
            # Properly parse URL and check hostname to avoid substring injection
            try:
                parsed = urlsplit(qualified_name)
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
        - az://account/container/path/to/file
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
        self._require_allowed(storage_account, container, blob_path)
        sanitized_url = f"{storage_account}/{container}/{blob_path}"
        LOGGER.info(f"Fetching blob asset: {sanitized_url}")

        # Get or create authenticated client (connection reuse)
        account_url = f"https://{storage_account}.blob.core.windows.net"
        client = self._get_client(account_url)
        blob_client = client.get_blob_client(container=container, blob=blob_path)

        # Download blob data with parallel range requests
        stream = await blob_client.download_blob(max_concurrency=_BLOB_MAX_CONCURRENCY)
        data = await stream.readall()

        LOGGER.info(f"Successfully fetched {len(data)} bytes from {sanitized_url}")
        return data

    async def fetch_to_file(
        self,
        qualified_name: str,
        dest_path: Any,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> int:
        """
        Fetch blob data and stream directly to a file.

        Streams data in chunks to avoid loading large files into memory.
        Uses parallel range requests for improved throughput on large blobs.

        Args:
            qualified_name: Blob URL
            dest_path: Destination file path

        Returns:
            Number of bytes written

        Raises:
            azure.core.exceptions.ClientAuthenticationError: If access is denied
        """
        result = await self.fetch_to_file_result(
            qualified_name,
            dest_path,
            options=options,
            context=context,
        )
        return result.bytes_transferred

    async def fetch_to_file_result(
        self,
        qualified_name: str,
        dest_path: Any,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> TransferResult:
        """Download one Blob object with bounded memory and atomic local commit."""
        options = options or TransferOptions()
        context = context or RequestContext()
        storage_account, container, blob_path = self._parse_blob_url(qualified_name)
        self._require_allowed(
            storage_account,
            container,
            blob_path,
            allow_reserved=options.allow_reserved,
        )
        sanitized_url = sanitize_uri_for_display(qualified_name)
        LOGGER.info("Streaming blob asset to file: %s", sanitized_url)
        account_url = f"https://{storage_account}.blob.core.windows.net"
        client = self._get_client(account_url)
        blob_client = client.get_blob_client(container=container, blob=blob_path)
        inner_options = TransferOptions(
            max_bytes=options.max_bytes,
            quota_bytes=options.quota_bytes,
            timeout_seconds=None,
            chunk_size=options.chunk_size,
            expected_sha256=options.expected_sha256,
            cancellation_event=options.cancellation_event,
            diagnostic_hook=options.diagnostic_hook,
        )

        async def download() -> TransferResult:
            stream = await await_transfer(
                blob_client.download_blob(max_concurrency=_BLOB_MAX_CONCURRENCY),
                inner_options,
                operation="download",
                resource=sanitized_url,
            )
            return await stream_chunks_to_file(
                stream.chunks(),
                dest_path,
                options=inner_options,
                context=context,
                operation="download",
                resource=sanitized_url,
            )

        try:
            if options.timeout_seconds is None:
                result = await download()
            else:
                async with asyncio.timeout(options.timeout_seconds):
                    result = await download()
        except TimeoutError as exc:
            raise TransferTimeoutError(
                f"Transfer exceeded the configured {options.timeout_seconds:g}-second timeout.",
                resource_id=sanitized_url,
                operation="download",
            ) from exc
        LOGGER.info("Successfully streamed %d bytes from %s", result.bytes_transferred, sanitized_url)
        return result

    def _require_allowed(
        self,
        account: str,
        container: str,
        blob_path: str,
        *,
        allow_reserved: bool = False,
    ) -> None:
        validate_azure_object_path(
            blob_path,
            allow_reserved=self._allow_reserved_paths or allow_reserved,
        )
        if self._allowed_scopes and not any(
            scope.contains(account, container, blob_path) for scope in self._allowed_scopes
        ):
            raise PermissionDeniedError(
                "Azure Blob object is outside the fetcher's configured account/container/prefix.",
                operation="download",
            )

    def _parse_blob_url(self, url: str) -> tuple[str, str, str]:
        """Parse and decode one supported Azure Blob locator."""
        scheme = urlsplit(url).scheme.lower()
        if scheme not in {"az", "abfss", "https"}:
            raise ValueError(f"Unsupported blob URL format: {sanitize_uri_for_display(url)}")
        try:
            account, container, blob_path = parse_azure_uri(url)
        except InvalidRequestError as exc:
            raise ValueError(
                f"{exc}. Expected az://account/container/path, "
                "abfss://container@account.dfs.core.windows.net/path, or Azure Blob/DFS HTTPS."
            ) from exc
        if scheme == "az" and not blob_path:
            raise ValueError("Malformed az URL. Expected az://account/container/path.")
        return account, container, blob_path


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
        self._allowed_root_fds: list[int] = []
        self._allowed_root_identities: list[tuple[int, int]] = []
        if os.name == "posix":
            try:
                for root in self._allowed_roots:
                    descriptor = os.open(
                        root,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    )
                    stat_result = os.fstat(descriptor)
                    self._allowed_root_fds.append(descriptor)
                    self._allowed_root_identities.append((stat_result.st_dev, stat_result.st_ino))
            except BaseException:
                for descriptor in self._allowed_root_fds:
                    os.close(descriptor)
                self._allowed_root_fds.clear()
                self._allowed_root_identities.clear()
                raise

    async def close(self) -> None:
        """Close retained trusted allowed-root descriptors."""
        for descriptor in self._allowed_root_fds:
            os.close(descriptor)
        self._allowed_root_fds.clear()
        self._allowed_root_identities.clear()

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
        path, descriptor = self._open_checked(qualified_name)
        LOGGER.info(f"Reading local file: {path}")
        with os.fdopen(descriptor, "rb", closefd=True) as input_file:
            data = input_file.read()
        LOGGER.info(f"Read {len(data)} bytes from {path}")
        return data

    async def fetch_to_file(
        self,
        qualified_name: str,
        dest_path: Any,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> int:
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
        result = await self.fetch_to_file_result(
            qualified_name,
            dest_path,
            options=options,
            context=context,
        )
        return result.bytes_transferred

    async def fetch_to_file_result(
        self,
        qualified_name: str,
        dest_path: Any,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> TransferResult:
        """Copy a local file through a descriptor that cannot follow symlinks."""
        options = options or TransferOptions()
        context = context or RequestContext()
        source, descriptor = self._open_checked(qualified_name)
        LOGGER.info("Streaming local file to cache: %s", source)

        async def chunks():
            with os.fdopen(descriptor, "rb", closefd=True) as input_file:
                while True:
                    chunk = input_file.read(options.chunk_size)
                    if not chunk:
                        break
                    yield chunk
                    await asyncio.sleep(0)

        return await stream_chunks_to_file(
            chunks(),
            dest_path,
            options=options,
            context=context,
            operation="download",
            resource=str(source),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_and_check(self, qualified_name: str) -> Path:
        """Resolve the path and validate against allowed roots."""
        path = self._parse_local_path(qualified_name).resolve()

        if not path.exists():
            raise FileNotFoundError(f"Local file not found: {path}")

        if self._allowed_roots:
            if not any(self._is_within(path, root) for root in self._allowed_roots):
                raise PermissionError(
                    f"Access denied: {path} is outside allowed roots {[str(r) for r in self._allowed_roots]}"
                )

        return path

    def _open_checked(self, qualified_name: str) -> tuple[Path, int]:
        """Open a contained regular file without following path-component symlinks."""
        path = self._resolve_and_check(qualified_name)
        if not self._allowed_roots or os.name != "posix":
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
        else:
            root_index = next(index for index, root in enumerate(self._allowed_roots) if self._is_within(path, root))
            root = self._allowed_roots[root_index]
            relative = path.relative_to(root)
            current = os.dup(self._allowed_root_fds[root_index])
            stat_result = os.fstat(current)
            if (stat_result.st_dev, stat_result.st_ino) != self._allowed_root_identities[root_index]:
                os.close(current)
                raise PermissionError("Configured allowed root identity changed.")
            try:
                for part in relative.parts[:-1]:
                    next_fd = os.open(
                        part,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=current,
                    )
                    os.close(current)
                    current = next_fd
                descriptor = os.open(
                    relative.parts[-1],
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current,
                )
            finally:
                os.close(current)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise PermissionError("Local asset must be a regular file.")
        return path, descriptor

    @staticmethod
    def _parse_local_path(qualified_name: str) -> Path:
        if not qualified_name.startswith("file://"):
            return Path(qualified_name)
        parsed = urlsplit(qualified_name)
        if parsed.query or parsed.fragment:
            raise ValueError("Local file URI must not contain a query or fragment.")
        if parsed.netloc not in {"", "localhost"}:
            raise ValueError("Local file URI authority must be empty or localhost.")
        decoded = unquote(parsed.path)
        if "\x00" in decoded:
            raise ValueError("Local file URI contains a NUL byte.")
        return Path(decoded)

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Check if *path* is under *root* (both must be resolved)."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False
