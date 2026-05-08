"""
DataLakeDataManager for fetching and caching data assets.

This module provides the core manager that handles fetching assets from
DataLake-cataloged sources and caching them to disk for tool access.
"""

import asyncio
import hashlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from azure.search.documents.aio import SearchClient

from ..auth import CredentialProviderTokenCredential, EntraCredentialProvider
from .fetchers import AssetFetcher, BlobFetcher, LocalFileFetcher
from ..types import AssetId

LOGGER = logging.getLogger(__name__)


class DataLakeDataManager:
    """
    Manages data asset retrieval and caching for code execution sessions.

    Fetches data assets from DataLake-cataloged sources and caches them
    to disk in their original format. Tools receive Path objects and handle
    loading the data as needed.

    Authentication is handled via managed identity — the server's identity
    is used to access downstream Azure resources (Storage, AI Search).

    Supports:
    - Azure Blob Storage (abfss://, https://)
    - Local filesystem (absolute paths, relative paths, file:// URIs)
    """

    def __init__(self, allowed_local_roots: list[str] | None = None):
        """
        Initialize the data manager.

        Uses managed identity for downstream resource access when Azure
        services are configured. Falls back to local-only mode when
        ``DATA_LAKE_SEARCH_ENDPOINT`` is not set.

        Args:
            allowed_local_roots: Optional list of directory paths the local
                file fetcher is allowed to read from. If ``None`` or empty,
                all paths are permitted (suitable for sandboxed containers).
        """
        self._cache_dir = Path(tempfile.mkdtemp(prefix="data_lake_cache_"))
        self._cache_index = {}  # Maps artifact_id -> cache file path

        search_endpoint = os.getenv("DATA_LAKE_SEARCH_ENDPOINT")
        self._credential_init_error: str | None = None

        # Initialize fetchers
        self._fetchers: list[AssetFetcher] = [
            LocalFileFetcher(allowed_roots=allowed_local_roots),
        ]

        if search_endpoint:
            # Azure mode: add blob fetcher and search client
            self._blob_details_index = os.getenv("DATA_LAKE_BLOB_DETAILS_INDEX", "blob-details")
            try:
                mi_client_id = (os.getenv("AZURE_CLIENT_ID") or "").strip() or None
                self._credential = CredentialProviderTokenCredential(EntraCredentialProvider(client_id=mi_client_id))
                self._fetchers.append(BlobFetcher(credential=self._credential))

                self._search_client = SearchClient(
                    endpoint=search_endpoint,
                    index_name=self._blob_details_index,
                    credential=self._credential,
                )
                LOGGER.info(f"Initialized blob-details search client: {search_endpoint}/{self._blob_details_index}")
            except (ImportError, RuntimeError, TypeError, ValueError) as e:
                self._credential_init_error = f"{type(e).__name__}: {e}"
                self._credential = None
                self._search_client = None
                LOGGER.warning(f"Failed to initialize Azure data access components: {e}")
        else:
            self._credential = None
            self._search_client = None
            self._blob_details_index = None
            LOGGER.info("DataLakeDataManager running in local-only mode (no Azure Search endpoint configured)")

    async def _get_blob_url_from_artifact_id(self, artifact_id: str) -> str:
        """
        Retrieve blob storage URL from artifact_id by querying the blob-details index.

        Args:
            artifact_id: Base64-encoded artifact identifier from blob-details index

        Returns:
            The blob storage URL (e.g. ``https://account.blob.core.windows.net/container/path``)

        Raises:
            ValueError: If the artifact is not found in the index or URL is invalid
        """
        if not self._search_client:
            if self._credential_init_error:
                init_error_type = self._credential_init_error.split(":", 1)[0]
                raise ValueError(
                    "Blob artifact resolution is unavailable because Azure data access initialization failed. "
                    f"Error type: {init_error_type}. "
                    "Check managed identity and Azure search endpoint configuration."
                )

            raise ValueError(
                "Search client not initialized. Set DATA_LAKE_SEARCH_ENDPOINT to resolve blob artifact IDs."
            )

        try:
            # Query the blob-details index using artifact_id as the document key
            # The artifact_id field is the unique key in the blob-details index
            result = await self._search_client.get_document(key=artifact_id)

            if not result:
                raise ValueError(f"Artifact not found in blob-details index: {artifact_id}")

            # Extract metadata_storage_path and strip any trailing whitespace
            blob_url = result.get("metadata_storage_path", "").strip()

            if not blob_url:
                raise ValueError(f"Artifact {artifact_id} has no metadata_storage_path in blob-details index")

            if not blob_url.startswith(("https://", "http://", "abfss://")):
                raise ValueError(f"Retrieved storage path is not a valid URL: {blob_url!r}")

            LOGGER.info(f"Retrieved blob URL for artifact {artifact_id[:40]}... -> {blob_url}")
            return blob_url

        except Exception as e:
            LOGGER.error(f"Failed to retrieve blob URL for artifact {artifact_id}: {e}")
            raise ValueError(f"Failed to resolve blob artifact {artifact_id}: {e}") from e

    async def get_cache_path(self, qualified_name: "AssetId") -> Path:
        """
        Get the filesystem path where the asset is cached.

        Ensures the asset is fetched and cached to disk in its original format,
        then returns the path. This allows kernel subprocesses to load the
        asset directly.

        Args:
            qualified_name: Type-tagged artifact format <type>base64_id</type>
                          Examples: <blob>id</blob>, <sql>id</sql>

        Returns:
            Path to the cached file

        Raises:
            ValueError: If not in tagged format, unsupported type, or artifact not found
        """
        # Extract artifact type and ID from tagged format
        artifact_match = re.match(r"^<(\w+)>([^<>]+)</\1>$", qualified_name.strip())
        if not artifact_match:
            # Fallback: accept unclosed tags like "<blob>id" (LLM sometimes omits closing tag)
            artifact_match = re.match(r"^<(\w+)>([^<>]+)$", qualified_name.strip())
        if not artifact_match:
            raise ValueError(f"Invalid artifact format - expected <type>id</type>, got: {qualified_name}")

        artifact_type = artifact_match.group(1)
        artifact_id = artifact_match.group(2)

        LOGGER.info(f"Resolving {artifact_type} artifact: {artifact_id}")

        # Check if already cached (use artifact_id as cache key)
        if artifact_id in self._cache_index:
            cache_path = self._cache_index[artifact_id]
            if cache_path.exists():
                LOGGER.debug(f"Asset already cached: {cache_path}")
                return cache_path

        # Route to appropriate resolver based on artifact type
        if artifact_type == "blob":
            resource_url = await self._get_blob_url_from_artifact_id(artifact_id)
        elif artifact_type == "local":
            # Local artifacts: the artifact_id is the file path itself
            resource_url = artifact_id
        else:
            raise ValueError(f"Unsupported artifact type: {artifact_type}.")

        # Fetch and cache the asset
        LOGGER.info(f"Fetching and caching {artifact_type} asset: {resource_url}")
        cache_path = self._get_cache_file_path(resource_url)

        # Stream asset directly to file to avoid loading into memory
        bytes_written = await self._fetch_asset_to_file(resource_url, cache_path)

        # Update index (use artifact_id as key)
        self._cache_index[artifact_id] = cache_path

        LOGGER.info(f"Cached asset: {cache_path} ({bytes_written} bytes)")
        return cache_path

    async def _fetch_asset_to_file(self, qualified_name: str, dest_path: Path) -> int:
        """
        Fetch asset and stream directly to file using appropriate fetcher.

        Args:
            qualified_name: Asset URL
            dest_path: Destination file path

        Returns:
            Number of bytes written
        """
        # Find appropriate fetcher
        for fetcher in self._fetchers:
            if fetcher.can_handle(qualified_name):
                LOGGER.info(f"Using {fetcher.__class__.__name__} for {qualified_name}")
                return await fetcher.fetch_to_file(qualified_name, dest_path)

        raise ValueError(
            f"No fetcher available for asset: {qualified_name}. "
            f"Supported formats: local paths, file:// URIs, Azure Blob/ADLS (abfss://, https://)"
        )

    def _get_cache_file_path(self, qualified_name: str) -> Path:
        """
        Get cache file path for an asset, preserving original extension.

        Args:
            qualified_name: Asset URL

        Returns:
            Path with appropriate extension based on file type
        """
        name_hash = hashlib.sha256(qualified_name.encode()).hexdigest()

        # Parse URL to extract path component
        parsed = urlparse(qualified_name)
        url_path = Path(parsed.path)
        ext = url_path.suffix if url_path.suffix else ".dat"

        return self._cache_dir / f"{name_hash}{ext}"

    def get_asset_info(self, qualified_name: str) -> dict:
        """
        Get information about a cached asset.

        Args:
            qualified_name: Asset identifier

        Returns:
            Dict with asset metadata
        """
        info = {
            "qualified_name": qualified_name,
            "cached": qualified_name in self._cache_index,
        }

        if qualified_name in self._cache_index:
            cache_path = self._cache_index[qualified_name]
            if cache_path.exists():
                info["size_bytes"] = cache_path.stat().st_size
                info["cache_location"] = str(cache_path)

        return info

    def list_available(self) -> list[str]:
        """
        List assets available in this session's cache.

        Returns:
            List of qualified names currently cached
        """
        return list(self._cache_index.keys())

    def cleanup(self) -> None:
        """
        Clean up cache directory, credentials, and resources.

        Removes the temporary cache directory if it was created by this manager.
        Call this when the session is ending to free up disk space.
        """
        # Clear cache index
        self._cache_index.clear()

        # Close search client
        if hasattr(self, "_search_client") and self._search_client:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._search_client.close())
            except RuntimeError:
                try:
                    loop = asyncio.new_event_loop()
                    loop.run_until_complete(self._search_client.close())
                    loop.close()
                except Exception as e:
                    LOGGER.debug(f"Error closing search client: {e}")

        # Close managed identity credential
        if hasattr(self, "_credential") and self._credential is not None:
            try:
                loop = asyncio.get_running_loop()
                # We're inside a running loop — schedule close as a task
                loop.create_task(self._credential.close())
            except RuntimeError:
                # No running loop — safe to create a temporary one
                try:
                    loop = asyncio.new_event_loop()
                    loop.run_until_complete(self._credential.close())
                    loop.close()
                except Exception as e:
                    LOGGER.debug(f"Error closing credential: {e}")

        # Remove temp directory
        if self._cache_dir and self._cache_dir.exists():
            try:
                shutil.rmtree(self._cache_dir)
                LOGGER.info(f"Cleaned up cache directory: {self._cache_dir}")
            except Exception as e:
                LOGGER.warning(f"Failed to clean up cache directory: {e}")

    async def aclose(self) -> None:
        """Async cleanup — preferred over sync cleanup() when inside an event loop."""
        self._cache_index.clear()

        if hasattr(self, "_search_client") and self._search_client:
            try:
                await self._search_client.close()
            except Exception as e:
                LOGGER.debug(f"Error closing search client: {e}")

        if hasattr(self, "_credential"):
            try:
                await self._credential.close()
            except Exception as e:
                LOGGER.debug(f"Error closing credential: {e}")

        if self._cache_dir and self._cache_dir.exists():
            try:
                shutil.rmtree(self._cache_dir)
                LOGGER.info(f"Cleaned up cache directory: {self._cache_dir}")
            except Exception as e:
                LOGGER.warning(f"Failed to clean up cache directory: {e}")

    def __del__(self):
        """Cleanup on garbage collection."""
        try:
            self.cleanup()
        except Exception as e:
            LOGGER.exception(f"Error during DataLakeDataManager cleanup: {e}")
