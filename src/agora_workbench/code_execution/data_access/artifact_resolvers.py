"""
Pluggable resolution of opaque artifact IDs to fetchable storage URLs.

``DataLakeDataManager`` accepts ``<blob>id</blob>`` asset tags whose payload is
an opaque catalog identifier. Turning that identifier into something a fetcher
can retrieve is deployment-specific: the built-in implementation queries an
Azure AI Search index, but a manifest file, a database, a REST catalog service,
or an offline test fixture are all equally valid backends. This module defines
the protocol that decouples the tag format from the catalog behind it.

Note the distinction from the similarly named ``resolution`` module: that one
resolves *asset tags in tool parameters* to cached local paths, whereas this one
resolves *an artifact ID to a storage URL* as one step inside that process.
"""

import logging
import os
from typing import Protocol

from azure.core.credentials_async import AsyncTokenCredential
from azure.search.documents.aio import SearchClient

LOGGER = logging.getLogger(__name__)

DEFAULT_BLOB_DETAILS_INDEX = "blob-details"


class ArtifactResolver(Protocol):
    """
    Resolves an opaque artifact ID to a URL that an ``AssetFetcher`` can retrieve.

    Implementations are responsible for their own caching; ``DataLakeDataManager``
    calls :meth:`resolve` on every cache miss and does not memoize the result.

    An implementation may additionally define ``async def aclose(self) -> None``
    to release backend clients. ``DataLakeDataManager`` calls it during cleanup
    when present, following the same optional-``close`` convention used for
    fetchers. A resolver must not close a credential it did not create.
    """

    async def resolve(self, artifact_id: str) -> str:
        """
        Resolve an opaque artifact ID to a fetchable qualified name or URL.

        Args:
            artifact_id: The identifier carried by a ``<blob>id</blob>`` tag.

        Returns:
            A qualified name a registered fetcher can handle (e.g. an
            ``https://``, ``abfss://``, or ``az://`` URL).

        Raises:
            ValueError: If the artifact is unknown, the resolved location is
                invalid, or resolution is unavailable. When unavailable, prefer
                raising with :attr:`unavailable_reason` as the message.
        """
        ...

    @property
    def unavailable_reason(self) -> str | None:
        """
        Human-readable reason resolution is unavailable, or ``None`` if ready.

        This is surfaced to the agent in asset-tag guidance, so it should state
        what an operator would need to configure rather than leaking internals.
        """
        ...


class SearchIndexArtifactResolver:
    """
    Resolves artifact IDs against an Azure AI Search blob-details index.

    The artifact ID is used directly as the document key; the blob URL is read
    from the document's ``metadata_storage_path`` field. Resolved URLs are
    cached so repeated resolutions of the same artifact skip the round-trip.

    The credential is borrowed, not owned: :meth:`aclose` closes the search
    client but never the credential, which remains the caller's to close.
    """

    def __init__(
        self,
        credential: AsyncTokenCredential | None,
        endpoint: str | None = None,
        index_name: str | None = None,
        credential_init_error: str | None = None,
    ):
        """
        Initialize the resolver.

        Args:
            credential: Async token credential used to query the search index.
                When ``None``, resolution is unavailable.
            endpoint: Azure AI Search endpoint. When falsy, resolution is
                unavailable and the resolver reports that the endpoint is unset.
            index_name: Name of the blob-details index.
            credential_init_error: Optional ``"Type: message"`` string describing
                a credential failure that happened before this resolver was
                constructed, so the deferred error can be surfaced at resolve
                time rather than being replaced by a generic message.
        """
        self._endpoint = endpoint
        self._index_name = index_name
        self._credential_init_error = credential_init_error
        self._url_cache: dict[str, str] = {}  # Maps artifact_id -> resolved blob URL
        self._search_client: SearchClient | None = None
        self._closed = False

        if not endpoint:
            LOGGER.info("No Azure Search endpoint configured; blob artifact ID resolution is disabled")
            return

        if credential is None:
            LOGGER.warning("Azure Search endpoint configured, but no Azure credential is available")
            return

        try:
            self._search_client = SearchClient(
                endpoint=endpoint,
                index_name=index_name or "",
                credential=credential,
            )
            LOGGER.info(f"Initialized blob-details search client: {endpoint}/{index_name}")
        except (ImportError, RuntimeError, TypeError, ValueError) as e:
            self._credential_init_error = f"{type(e).__name__}: {e}"
            LOGGER.warning(f"Failed to initialize Azure data access components: {e}")

    @classmethod
    def from_env(
        cls,
        credential: AsyncTokenCredential | None,
        credential_init_error: str | None = None,
    ) -> "SearchIndexArtifactResolver":
        """
        Build a resolver from ``DATA_LAKE_SEARCH_ENDPOINT`` / ``DATA_LAKE_BLOB_DETAILS_INDEX``.

        The index name is only read when an endpoint is configured, so an
        endpoint-less deployment is distinguishable from one whose index name
        was explicitly set to the empty string.
        """
        endpoint = os.getenv("DATA_LAKE_SEARCH_ENDPOINT")
        index_name = os.getenv("DATA_LAKE_BLOB_DETAILS_INDEX", DEFAULT_BLOB_DETAILS_INDEX) if endpoint else None
        return cls(
            credential=credential,
            endpoint=endpoint,
            index_name=index_name,
            credential_init_error=credential_init_error,
        )

    @property
    def unavailable_reason(self) -> str | None:
        """Reason blob artifact resolution cannot run, or ``None`` when ready."""
        if self._closed:
            return "The artifact resolver has been closed."

        if self._search_client is not None:
            return None

        if not self._endpoint:
            return "Search client not initialized. Set DATA_LAKE_SEARCH_ENDPOINT to resolve blob artifact IDs."

        # Past this point an endpoint *is* configured, so pointing the operator
        # at DATA_LAKE_SEARCH_ENDPOINT would misdirect them. This is also what
        # keeps an empty DATA_LAKE_BLOB_DETAILS_INDEX from being mistaken for
        # search being unconfigured.
        if self._credential_init_error:
            init_error_type = self._credential_init_error.split(":", 1)[0]
            return (
                "Blob artifact resolution is unavailable because Azure data access initialization failed. "
                f"Error type: {init_error_type}. "
                "Check managed identity and Azure search endpoint configuration."
            )

        return (
            f"Blob artifact resolution is unavailable because no Azure credential was available for "
            f"{self._endpoint}. Check managed identity configuration."
        )

    async def resolve(self, artifact_id: str) -> str:
        """
        Retrieve a blob storage URL for *artifact_id* from the blob-details index.

        Args:
            artifact_id: Base64-encoded artifact identifier from the index.

        Returns:
            The blob storage URL (e.g. ``https://account.blob.core.windows.net/container/path``).

        Raises:
            ValueError: If resolution is unavailable, the artifact is not found,
                or the stored path is not a valid storage URL.
        """
        if artifact_id in self._url_cache:
            LOGGER.debug(f"URL cache hit for artifact {artifact_id[:40]}...")
            return self._url_cache[artifact_id]

        if self._search_client is None:
            raise ValueError(self.unavailable_reason)

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

            if not blob_url.startswith(("https://", "abfss://", "az://")):
                raise ValueError(f"Retrieved storage path is not a valid URL: {blob_url!r}")

            LOGGER.info(f"Retrieved blob URL for artifact {artifact_id[:40]}...")
            self._url_cache[artifact_id] = blob_url
            return blob_url

        except Exception as e:
            LOGGER.error(f"Failed to retrieve blob URL for artifact {artifact_id}: {e}")
            raise ValueError(f"Failed to resolve blob artifact {artifact_id}: {e}") from e

    async def aclose(self) -> None:
        """Clear the URL cache and close the search client (never the credential).

        Idempotent: the client reference is dropped so a second call is a no-op
        and :attr:`unavailable_reason` stops reporting readiness after shutdown.
        """
        self._closed = True
        self._url_cache.clear()

        search_client, self._search_client = self._search_client, None
        if search_client is not None:
            try:
                await search_client.close()
            except Exception as e:
                LOGGER.debug(f"Error closing search client: {e}")
