"""
DataLakeDataManager for fetching and caching data assets.

This module provides the core manager that handles fetching assets from
DataLake-cataloged sources and caching them to disk for tool access.
"""

import asyncio
import hashlib
import inspect
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from azure.core.credentials_async import AsyncTokenCredential

from .. import agent_guidance
from ..types import AssetId
from .artifact_resolvers import ArtifactResolver, SearchIndexArtifactResolver
from .credentials import create_storage_credential
from .fetchers import AssetFetcher, BlobFetcher, LocalFileFetcher

LOGGER = logging.getLogger(__name__)


def _validate_artifact_resolver(resolver: ArtifactResolver) -> None:
    """Fail fast on a resolver missing part of the protocol.

    ``unavailable_reason`` is consumed while formatting errors, where a missing
    attribute would be swallowed rather than reported, so both members are
    checked up front instead.
    """
    missing = [
        name
        for name in ("resolve", "unavailable_reason")
        # Probe the class first so a property is detected as a descriptor rather
        # than evaluated; a resolver whose property raises is still well-formed,
        # and validation must not surface that error as a construction failure.
        if not hasattr(type(resolver), name) and not hasattr(resolver, name)
    ]
    if missing:
        raise TypeError(
            f"artifact_resolver must implement the ArtifactResolver protocol; "
            f"{type(resolver).__name__} is missing: {', '.join(missing)}"
        )


def _resolver_aclose(resolver: object) -> Callable[[], Coroutine[Any, Any, None]] | None:
    """Return a resolver's optional ``aclose``, mirroring the fetcher ``close`` convention.

    The result is normalized to a coroutine function. ``cleanup()`` feeds it to
    ``loop.create_task``, which rejects a non-awaitable, so a third-party
    resolver defining ``aclose`` synchronously would otherwise raise a
    ``TypeError`` out of teardown.
    """
    aclose = getattr(resolver, "aclose", None)
    if not callable(aclose):
        return None

    async def close() -> None:
        result = aclose()
        if inspect.isawaitable(result):
            await result

    return close


class DataLakeDataManager:
    """
    Manages data asset retrieval and caching for code execution sessions.

    Fetches data assets from DataLake-cataloged sources and caches them
    to disk in their original format. Tools receive Path objects and handle
    loading the data as needed.

    Authentication uses a credential chain (``create_storage_credential``):
    the mounted ``az login`` MSAL cache for local development, falling back to
    managed identity in production, for downstream Azure resources
    (Storage, AI Search).

    Blob artifact IDs are resolved through a pluggable ``ArtifactResolver``,
    defaulting to Azure AI Search; see ``artifact_resolvers.py``.

    Supports:
    - Azure Blob Storage (abfss://, az://, https://)
    - Local filesystem (absolute paths, relative paths, file:// URIs)
    """

    def __init__(
        self,
        allowed_local_roots: list[str] | None = None,
        extra_fetchers: list[AssetFetcher] | None = None,
        credential: AsyncTokenCredential | None = None,
        artifact_resolver: ArtifactResolver | None = None,
    ):
        """
        Initialize the data manager.

        Uses a credential chain (``az login`` MSAL cache locally, managed
        identity in production) for Azure Blob access unless a credential is
        provided. Blob URL fetching is available whenever a credential can be
        initialized. Blob artifact ID resolution is delegated to an
        ``ArtifactResolver``; the default one queries Azure AI Search and so
        additionally requires ``DATA_LAKE_SEARCH_ENDPOINT``.

        Args:
            allowed_local_roots: Optional list of directory paths the local
                file fetcher is allowed to read from. If ``None`` or empty,
                all paths are permitted (suitable for sandboxed containers).
            extra_fetchers: Optional list of additional ``AssetFetcher``
                instances to register. These are checked *before* the
                built-in fetchers (local file, blob), allowing custom
                fetchers to override default handling for specific URL
                schemes or patterns.
            credential: Optional async token credential to use for Azure Blob
                Storage and Azure AI Search access. When omitted, the manager
                creates the same storage credential chain as before, resolving
                ``AZURE_CLIENT_ID`` for user-assigned managed identity binding.
            artifact_resolver: Optional resolver turning ``<blob>id</blob>``
                identifiers into fetchable URLs, for deployments whose catalog
                is not an Azure AI Search index. When omitted, a
                ``SearchIndexArtifactResolver`` is built from the environment,
                preserving existing behavior. A supplied resolver is used as-is
                and is *not* given the manager's credential, so it must arrange
                its own authentication.

        Raises:
            TypeError: If ``artifact_resolver`` does not implement the
                ``ArtifactResolver`` protocol.
        """
        self._cache_dir = Path(tempfile.mkdtemp(prefix="data_lake_cache_"))
        self._cache_index = {}  # Maps artifact_id -> cache file path

        self._credential_init_error: str | None = None
        self._credential: AsyncTokenCredential | None = None
        self._owns_credential = credential is None

        # Initialize fetchers — custom fetchers take priority over built-ins
        self._fetchers: list[AssetFetcher] = list(extra_fetchers or [])
        self._fetchers.append(LocalFileFetcher(allowed_roots=allowed_local_roots))

        try:
            if credential is not None:
                self._credential = credential
            else:
                mi_client_id = (os.getenv("AZURE_CLIENT_ID") or "").strip() or None
                # MSAL cache (mounted az login) locally, managed identity in
                # production. Pass the AZURE_CLIENT_ID-resolved id through so
                # prod keeps binding to the same user-assigned identity.
                self._credential = create_storage_credential(client_id=mi_client_id)
        except (ImportError, RuntimeError, TypeError, ValueError) as e:
            self._credential_init_error = f"{type(e).__name__}: {e}"
            LOGGER.warning(f"Failed to initialize Azure storage credential: {e}")

        if self._credential is not None:
            self._fetchers.append(BlobFetcher(credential=self._credential))

        if artifact_resolver is not None:
            _validate_artifact_resolver(artifact_resolver)
            self._artifact_resolver: ArtifactResolver = artifact_resolver
        else:
            # The deferred credential error is handed over so the resolver can
            # explain *why* it is unavailable rather than blaming configuration.
            self._artifact_resolver = SearchIndexArtifactResolver.from_env(
                credential=self._credential,
                credential_init_error=self._credential_init_error,
            )

    def _asset_tag_guidance(self) -> str:
        """Asset-tag guidance for the agent, annotated by the resolver's readiness."""
        try:
            unavailable_reason = self._artifact_resolver.unavailable_reason
        except Exception as e:
            # This runs while building an error message; a misbehaving
            # third-party resolver must not mask the failure being reported.
            LOGGER.debug(f"Artifact resolver failed to report availability: {e}")
            unavailable_reason = None

        return agent_guidance.asset_tag_format(unavailable_reason)

    async def _get_blob_url_from_artifact_id(self, artifact_id: str) -> str:
        """
        Retrieve a blob storage URL from an artifact_id via the configured resolver.

        Retained as a thin delegate for backward compatibility; caching is the
        resolver's responsibility.

        Args:
            artifact_id: Opaque artifact identifier from a ``<blob>`` tag

        Returns:
            The blob storage URL (e.g. ``https://account.blob.core.windows.net/container/path``)

        Raises:
            ValueError: If the artifact cannot be resolved or resolution is unavailable
        """
        return await self._artifact_resolver.resolve(artifact_id)

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
            raise ValueError(
                f"Invalid artifact format - expected <type>id</type>, got: {qualified_name}. "
                f"{self._asset_tag_guidance()} {agent_guidance.DISCOVER_DATA}"
            )

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
            raise ValueError(f"Unsupported artifact type: {artifact_type}. {self._asset_tag_guidance()}")

        # Fetch and cache the asset
        LOGGER.debug(f"Fetching and caching {artifact_type} asset")
        cache_path = self._get_cache_file_path(resource_url)

        # Stream asset directly to file to avoid loading into memory
        bytes_written = await self._fetch_asset_to_file(resource_url, cache_path)

        # Update index (use artifact_id as key)
        self._cache_index[artifact_id] = cache_path

        LOGGER.debug(f"Cached asset to disk ({bytes_written} bytes)")
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
            f"Supported formats: local paths, file:// URIs, Azure Blob/ADLS (abfss://, https://). "
            f"{agent_guidance.DISCOVER_DATA}"
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

        # Close the artifact resolver (releases any catalog client it owns)
        resolver_close = _resolver_aclose(getattr(self, "_artifact_resolver", None))
        if resolver_close is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(resolver_close())
            except RuntimeError:
                try:
                    loop = asyncio.new_event_loop()
                    loop.run_until_complete(resolver_close())
                    loop.close()
                except Exception as e:
                    LOGGER.debug(f"Error closing artifact resolver: {e}")

        # Close managed identity credential
        if hasattr(self, "_credential") and self._credential is not None and self._owns_credential:
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

        # Close fetchers (releases pooled connections)
        for fetcher in self._fetchers:
            if hasattr(fetcher, "close"):
                try:
                    await fetcher.close()
                except Exception as e:
                    LOGGER.debug(f"Error closing fetcher {fetcher.__class__.__name__}: {e}")

        resolver_close = _resolver_aclose(getattr(self, "_artifact_resolver", None))
        if resolver_close is not None:
            try:
                await resolver_close()
            except Exception as e:
                LOGGER.debug(f"Error closing artifact resolver: {e}")

        if hasattr(self, "_credential") and self._credential is not None and self._owns_credential:
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
