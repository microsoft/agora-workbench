"""
DataLakeDataManager for fetching and caching data assets.

This module provides the core manager that handles fetching assets from
DataLake-cataloged sources and caching them to disk for tool access.
"""

import hashlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from azure.search.documents.aio import SearchClient

from ..auth.irm import IRMDecryptionError, is_irm_protected
from ..auth.obo_credential import _AsyncOBOCredentialWrapper, get_obo_credential_provider
from .fetchers import BlobFetcher
from ..types import AssetId

LOGGER = logging.getLogger(__name__)


class DataLakeDataManager:
    """
    Manages data asset retrieval and caching for code execution sessions.

    Fetches data assets from DataLake-cataloged sources and caches them
    to disk in their original format. Tools receive Path objects and handle
    loading the data as needed.

    Authentication is handled via OBO token exchange - the user's assertion
    token is exchanged for tokens with appropriate scopes for each resource.

    Supports:
    - Azure Blob Storage (abfss://, https://)
    """

    def __init__(self, user_token: str):
        """
        Initialize the data manager.

        Args:
            user_token: User's bearer token (JWT) for OBO token exchange.

        Raises:
            ValueError: If OBO is not configured (missing env vars)
        """
        self._cache_dir = Path(tempfile.mkdtemp(prefix="data_lake_cache_"))
        self._cache_index = {}  # Maps artifact_id -> cache file path

        self._obo_provider = get_obo_credential_provider(user_assertion=user_token)

        # Initialize fetchers
        self._fetchers = [
            BlobFetcher(obo_provider=self._obo_provider),
        ]

        # Initialize Azure Search client for blob-details index
        self._blob_details_index = os.getenv("DATA_LAKE_BLOB_DETAILS_INDEX", "blob-details")
        search_endpoint = os.getenv("DATA_LAKE_SEARCH_ENDPOINT")

        if not search_endpoint:
            raise ValueError(
                "DATA_LAKE_SEARCH_ENDPOINT environment variable is required for blob artifact resolution. "
                "Set this to your Azure AI Search service endpoint (e.g., https://your-service.search.windows.net)"
            )

        # Wrap OBO provider with async credential wrapper for Search client
        search_credential = _AsyncOBOCredentialWrapper(self._obo_provider)
        self._search_client = SearchClient(
            endpoint=search_endpoint,
            index_name=self._blob_details_index,
            credential=search_credential,
        )
        LOGGER.info(f"Initialized blob-details search client: {search_endpoint}/{self._blob_details_index}")

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
            raise RuntimeError(
                "Search client not initialized. This is an internal error - "
                "DATA_LAKE_SEARCH_ENDPOINT should have been validated during initialization."
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
        else:
            raise ValueError(f"Unsupported artifact type: {artifact_type}.")

        # Fetch and cache the asset
        LOGGER.info(f"Fetching and caching {artifact_type} asset: {resource_url}")
        cache_path = self._get_cache_file_path(resource_url)

        # Stream asset directly to file to avoid loading into memory
        bytes_written = await self._fetch_asset_to_file(resource_url, cache_path)

        # Attempt IRM decryption if the file is IRM/DRM-protected
        await self._try_decrypt_irm(cache_path)

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
            f"No fetcher available for asset: {qualified_name}. Supported formats: Azure Blob/ADLS (abfss://, https://)"
        )

    async def _try_decrypt_irm(self, cache_path: Path) -> None:
        """
        Check for IRM/DRM-protected files and raise an error if found.

        IRM decryption is not currently supported. If an IRM-protected file is
        detected, this raises IRMDecryptionError to give the caller direct feedback.

        If the file is not IRM-protected, this is a no-op.

        Note: The underlying IRM decryption code (irm.py) and OBO-based token
        acquisition remain in place for potential future use.
        """
        # Quick check: is it an OLE2 file?
        try:
            with open(cache_path, "rb") as f:
                header = f.read(8)
            if header != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
                return  # Not OLE2, definitely not IRM
        except Exception:
            return

        if not is_irm_protected(cache_path):
            LOGGER.debug(f"File is OLE2 but not IRM-protected: {cache_path.name}")
            return

        raise IRMDecryptionError(
            f"IRM-protected file detected ({cache_path.name}). "
            "IRM decryption is not currently supported. "
            "Please decrypt the file before ingestion."
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
        Clean up cache directory, OBO provider, and resources.

        Removes the temporary cache directory if it was created by this manager.
        Call this when the session is ending to free up disk space.
        """
        # Clear cache index
        self._cache_index.clear()

        # Close search client
        if hasattr(self, "_search_client") and self._search_client:
            import asyncio

            try:
                # Close async search client
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # If event loop is running, schedule close
                    asyncio.create_task(self._search_client.close())
                else:
                    # If no loop or loop not running, run synchronously
                    asyncio.run(self._search_client.close())
            except Exception as e:
                LOGGER.debug(f"Error closing search client: {e}")

        # Close OBO credential provider
        if hasattr(self, "_obo_provider"):
            self._obo_provider.close()

        # Remove temp directory if we created it
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
