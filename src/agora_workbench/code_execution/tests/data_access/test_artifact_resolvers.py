"""
Tests for pluggable artifact resolution.

Covers the built-in ``SearchIndexArtifactResolver`` — availability reporting,
caching, scheme validation, and cleanup — which carries the blob-details
lookup that previously lived inside ``DataLakeDataManager``.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ...data_access.artifact_resolvers import (
    DEFAULT_BLOB_DETAILS_INDEX,
    SearchIndexArtifactResolver,
)


def _resolver_with_stored_path(metadata_storage_path: str) -> SearchIndexArtifactResolver:
    """Build a resolver whose index returns *metadata_storage_path* for any key."""
    resolver = SearchIndexArtifactResolver(credential=None)
    resolver._search_client = MagicMock(
        get_document=AsyncMock(return_value={"metadata_storage_path": metadata_storage_path}),
        close=AsyncMock(),
    )
    return resolver


class TestFromEnv:
    """Environment wiring for the default resolver."""

    def test_reads_endpoint_and_default_index(self, monkeypatch):
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://search.example.net")
        monkeypatch.delenv("DATA_LAKE_BLOB_DETAILS_INDEX", raising=False)

        resolver = SearchIndexArtifactResolver.from_env(credential=MagicMock())

        assert resolver._endpoint == "https://search.example.net"
        assert resolver._index_name == DEFAULT_BLOB_DETAILS_INDEX
        assert resolver._search_client is not None

    def test_index_name_is_none_without_endpoint(self, monkeypatch):
        """Without an endpoint the index name stays unset, not the default."""
        monkeypatch.delenv("DATA_LAKE_SEARCH_ENDPOINT", raising=False)
        monkeypatch.setenv("DATA_LAKE_BLOB_DETAILS_INDEX", "custom-index")

        resolver = SearchIndexArtifactResolver.from_env(credential=MagicMock())

        assert resolver._index_name is None
        assert resolver._search_client is None

    def test_honors_custom_index_name(self, monkeypatch):
        monkeypatch.setenv("DATA_LAKE_SEARCH_ENDPOINT", "https://search.example.net")
        monkeypatch.setenv("DATA_LAKE_BLOB_DETAILS_INDEX", "custom-index")

        resolver = SearchIndexArtifactResolver.from_env(credential=MagicMock())

        assert resolver._index_name == "custom-index"


class TestUnavailableReason:
    """The three availability states the manager distinguishes."""

    def test_none_when_search_client_is_ready(self):
        resolver = SearchIndexArtifactResolver(
            credential=MagicMock(), endpoint="https://search.example.net", index_name="blob-details"
        )

        assert resolver.unavailable_reason is None

    def test_points_at_endpoint_env_var_when_unconfigured(self):
        resolver = SearchIndexArtifactResolver(credential=MagicMock())

        assert resolver.unavailable_reason == (
            "Search client not initialized. Set DATA_LAKE_SEARCH_ENDPOINT to resolve blob artifact IDs."
        )

    def test_surfaces_deferred_credential_error_when_configured(self):
        resolver = SearchIndexArtifactResolver(
            credential=None,
            endpoint="https://search.example.net",
            index_name="blob-details",
            credential_init_error="RuntimeError: missing managed identity",
        )

        reason = resolver.unavailable_reason
        assert reason is not None
        assert "Azure data access initialization failed" in reason
        assert "Error type: RuntimeError" in reason

    def test_surfaces_deferred_credential_error_with_empty_index_name(self):
        """An empty index name must not be mistaken for "search not configured"."""
        resolver = SearchIndexArtifactResolver(
            credential=None,
            endpoint="https://search.example.net",
            index_name="",
            credential_init_error="RuntimeError: missing managed identity",
        )

        reason = resolver.unavailable_reason
        assert reason is not None
        assert "Azure data access initialization failed" in reason

    def test_ignores_credential_error_when_search_is_unconfigured(self):
        """Without an endpoint, the generic message is the actionable one."""
        resolver = SearchIndexArtifactResolver(
            credential=None,
            endpoint=None,
            credential_init_error="RuntimeError: missing managed identity",
        )

        assert resolver.unavailable_reason == (
            "Search client not initialized. Set DATA_LAKE_SEARCH_ENDPOINT to resolve blob artifact IDs."
        )

    def test_blames_the_credential_when_the_endpoint_is_configured(self):
        """A configured endpoint must not be reported as the missing piece."""
        resolver = SearchIndexArtifactResolver(credential=None, endpoint="https://search.example.net")

        reason = resolver.unavailable_reason

        assert reason is not None
        assert "DATA_LAKE_SEARCH_ENDPOINT" not in reason
        assert reason == (
            "Blob artifact resolution is unavailable because no Azure credential was available for "
            "https://search.example.net. Check managed identity configuration."
        )

    def test_reports_closed_state(self):
        """A closed resolver must not report itself as ready."""
        resolver = SearchIndexArtifactResolver(
            credential=MagicMock(), endpoint="https://search.example.net", index_name="blob-details"
        )
        assert resolver.unavailable_reason is None

        asyncio.run(resolver.aclose())

        assert resolver.unavailable_reason == "The artifact resolver has been closed."


class TestResolve:
    """Lookup, caching, and validation behavior."""

    @pytest.mark.asyncio
    async def test_raises_unavailable_reason_when_not_ready(self):
        resolver = SearchIndexArtifactResolver(credential=MagicMock())

        with pytest.raises(ValueError, match="Set DATA_LAKE_SEARCH_ENDPOINT"):
            await resolver.resolve("artifact_key")

    @pytest.mark.asyncio
    async def test_resolves_az_scheme_path(self):
        resolver = _resolver_with_stored_path("az://account/container/path/file.csv")

        assert await resolver.resolve("artifact_key") == "az://account/container/path/file.csv"

    @pytest.mark.asyncio
    async def test_resolves_https_scheme_path(self):
        resolver = _resolver_with_stored_path("https://acct.blob.core.windows.net/c/file.csv")

        assert await resolver.resolve("artifact_key") == "https://acct.blob.core.windows.net/c/file.csv"

    @pytest.mark.asyncio
    async def test_rejects_unsupported_scheme(self):
        resolver = _resolver_with_stored_path("ftp://host/file.csv")

        with pytest.raises(ValueError, match="not a valid URL"):
            await resolver.resolve("artifact_key")

    @pytest.mark.asyncio
    async def test_strips_surrounding_whitespace(self):
        resolver = _resolver_with_stored_path("  https://acct.blob.core.windows.net/c/file.csv  ")

        assert await resolver.resolve("artifact_key") == "https://acct.blob.core.windows.net/c/file.csv"

    @pytest.mark.asyncio
    async def test_rejects_missing_storage_path(self):
        resolver = _resolver_with_stored_path("")

        with pytest.raises(ValueError, match="no metadata_storage_path"):
            await resolver.resolve("artifact_key")

    @pytest.mark.asyncio
    async def test_caches_resolved_url(self):
        """A second resolution of the same artifact skips the search round-trip."""
        resolver = _resolver_with_stored_path("https://acct.blob.core.windows.net/c/file.csv")

        first = await resolver.resolve("artifact_key")
        second = await resolver.resolve("artifact_key")

        assert first == second
        assert resolver._search_client is not None
        assert resolver._search_client.get_document.await_count == 1

    @pytest.mark.asyncio
    async def test_wraps_backend_errors(self):
        resolver = SearchIndexArtifactResolver(credential=None)
        resolver._search_client = MagicMock(
            get_document=AsyncMock(side_effect=RuntimeError("index offline")),
            close=AsyncMock(),
        )

        with pytest.raises(ValueError, match="Failed to resolve blob artifact"):
            await resolver.resolve("artifact_key")


class TestAclose:
    """Cleanup releases the search client but not the borrowed credential."""

    @pytest.mark.asyncio
    async def test_closes_search_client_and_clears_cache(self):
        resolver = _resolver_with_stored_path("https://acct.blob.core.windows.net/c/file.csv")
        await resolver.resolve("artifact_key")
        search_client = resolver._search_client

        await resolver.aclose()

        assert resolver._url_cache == {}
        assert search_client is not None
        search_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_close_borrowed_credential(self):
        credential = MagicMock(close=AsyncMock())
        resolver = SearchIndexArtifactResolver(
            credential=credential, endpoint="https://search.example.net", index_name="blob-details"
        )

        await resolver.aclose()

        credential.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tolerates_search_client_close_failure(self):
        resolver = SearchIndexArtifactResolver(credential=None)
        resolver._search_client = MagicMock(close=AsyncMock(side_effect=RuntimeError("boom")))

        await resolver.aclose()

    @pytest.mark.asyncio
    async def test_is_idempotent(self):
        """A second close must not re-close the already-released client."""
        resolver = _resolver_with_stored_path("https://acct.blob.core.windows.net/c/file.csv")
        search_client = resolver._search_client

        await resolver.aclose()
        await resolver.aclose()

        assert search_client is not None
        search_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resolve_after_close_reports_the_closed_state(self):
        resolver = _resolver_with_stored_path("https://acct.blob.core.windows.net/c/file.csv")

        await resolver.aclose()

        with pytest.raises(ValueError, match="has been closed"):
            await resolver.resolve("artifact_key")
