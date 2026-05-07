"""
DataLake catalog integration for data asset discovery.
"""

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Callable, Optional

try:
    from agent_framework import tool
except ImportError as e:
    raise ImportError(
        "agent-framework is required for MAF adapters. Install with: pip install agora-workbench[maf]"
    ) from e
from azure.core.exceptions import HttpResponseError
from azure.search.documents.aio import SearchClient
from pydantic import BaseModel, Field

from ..permissions import check_resource_permissions
from auth import get_search_credential_async

LOGGER = logging.getLogger(__name__)


def format_asset_tag(asset: dict) -> str | None:
    """Format an artifact into the canonical XML-like asset tag."""
    artifact_type = asset.get("artifact_type", "")
    artifact_id = asset.get("artifact_id", "")
    if artifact_type and artifact_id:
        return f"<{artifact_type}>{artifact_id}</{artifact_type}>"
    return None


class DataLakeSearchClientManager:
    """
    Manages SearchClient lifecycle with automatic credential refresh on auth errors.

    Caches the SearchClient for performance but recreates it when authentication
    errors occur (e.g., expired JWT tokens). This allows long-running agents
    to continue working even after token expiration.
    """

    def __init__(self):
        """Initialize the manager."""
        self._client: Optional[SearchClient] = None
        self._endpoint: Optional[str] = None
        self._index_name: Optional[str] = None

    @staticmethod
    def _get_data_lake_config() -> tuple[str, str]:
        """
        Get DataLake catalog search configuration from environment variables.

        Returns:
            Tuple of (endpoint, index_name)

        Raises:
            ValueError: If DATA_LAKE_SEARCH_ENDPOINT is not configured

        Environment Variables:
            DATA_LAKE_SEARCH_ENDPOINT: Required - Azure AI Search service endpoint
            DATA_LAKE_CATALOG_INDEX_NAME: Optional - Artifact registry index name (default: artifact-registry)
        """
        endpoint = os.getenv("DATA_LAKE_SEARCH_ENDPOINT")
        if not endpoint:
            raise ValueError("DataLake catalog not configured. Set DATA_LAKE_SEARCH_ENDPOINT environment variable.")
        index_name = os.getenv("DATA_LAKE_CATALOG_INDEX_NAME", "artifact-registry")

        LOGGER.debug(f"DataLake catalog configuration: endpoint={endpoint}, index={index_name}")

        return (endpoint, index_name)

    def _create_search_client(self) -> SearchClient:
        """
        Create and configure a SearchClient for DataLake catalog queries.

        Returns:
            Configured SearchClient

        Raises:
            ValueError: If DataLake is not configured or configuration is invalid
        """
        endpoint, index_name = self._get_data_lake_config()

        # Create credential for search
        credential = get_search_credential_async()
        LOGGER.debug("Created credential for DataLake search")

        # Create and return SearchClient
        try:
            search_client = SearchClient(
                endpoint=endpoint,
                index_name=index_name,
                credential=credential,
            )
            LOGGER.debug(f"Created SearchClient for DataLake catalog: {endpoint}/{index_name}")
            return search_client
        except Exception as e:
            raise ValueError(
                f"Failed to create SearchClient for DataLake catalog. "
                f"Verify DATA_LAKE_SEARCH_ENDPOINT and DATA_LAKE_CATALOG_INDEX_NAME are correct. "
                f"Error: {type(e).__name__}: {str(e)}"
            ) from e

    async def get_client(self) -> SearchClient:
        """
        Get or create the SearchClient.

        Returns:
            Configured SearchClient

        Raises:
            ValueError: If DataLake configuration is invalid
        """
        if self._client is None:
            self._client = self._create_search_client()

        return self._client

    async def search(self, **kwargs):
        """
        Execute a search with automatic retry on authentication errors.

        If authentication fails (e.g., expired token), recreates the client
        with fresh credentials and retries once.

        Args:
            **kwargs: Arguments passed to SearchClient.search()

        Returns:
            Search results iterator

        Raises:
            HttpResponseError: If search fails after retry
        """
        try:
            client = await self.get_client()
            return await client.search(**kwargs)
        except HttpResponseError as e:
            # Check if it's an auth error (401/403)
            if hasattr(e, "status_code") and e.status_code in (401, 403):
                LOGGER.info(
                    "SearchClient authentication error (possibly expired token), recreating with fresh credentials"
                )
                await self._reset_client()
                # Retry once with fresh client
                client = await self.get_client()
                return await client.search(**kwargs)
            else:
                # Not an auth error, re-raise
                raise

    async def _reset_client(self):
        """Close and reset the cached client."""
        if self._client:
            try:
                await self._client.close()
            except Exception as e:
                LOGGER.debug(f"Error closing SearchClient: {e}")
            self._client = None

    async def close(self):
        """Close the SearchClient and clean up resources."""
        await self._reset_client()


class DataLakeSearchParams(BaseModel):
    """Parameters for DataLake artifact registry search."""

    query: str = Field(
        description=(
            "Search query describing the data artifacts you're looking for. "
            "This query will be used for hybrid search (text + semantic vector matching). "
            "Examples: 'wind energy power generation data', "
            "'customer transaction tables 2024', 'climate model outputs'"
        )
    )
    artifact_types: Optional[list[str]] = Field(
        default=None,
        description=(
            "Optional list of artifact types to filter by. Available types: 'blob'. Leave empty to search all types."
        ),
    )
    domains: Optional[list[str]] = Field(
        default=None,
        description="Optional list of domains to filter by. Leave empty to search all domains.",
    )
    sources: Optional[list[str]] = Field(
        default=None,
        description=(
            "Optional list of sources to filter by. "
            "Examples: 'azure_storage', 'synapse', 'databricks'. "
            "Leave empty to search all sources."
        ),
    )
    top: int = Field(
        default=20,
        description="Maximum number of results to return (default: 20, max: 50)",
        ge=1,
        le=50,
    )
    select_fields: Optional[list[str]] = Field(
        default=None,
        description=(
            "Optional list of specific fields to return. "
            "Available fields: 'artifact_id', 'artifact_type', 'name', 'description', "
            "'semantic_dataset_id', 'semantic_dataset_name', 'semantic_dataset_description', "
            "'domain', 'rbacScope', 'source', 'created_at', 'updated_at', 'detail_index', 'detail_key'. "
            "Note: 'asset_tag' is always auto-generated in results (do NOT request it via select_fields). "
            "Leave empty to return all available fields (recommended for initial exploration)."
        ),
    )
    search_mode: Optional[str] = Field(
        default=None,
        description=(
            "Optional search mode: 'any' (matches any search term) or 'all' (matches all terms). "
            "Default behavior uses Azure AI Search's intelligent matching."
        ),
    )
    order_by: Optional[list[str]] = Field(
        default=None,
        description=(
            "Optional list of fields to sort by. "
            "Examples: ['updated_at desc'], ['name asc'], ['created_at desc']. "
            "Leave empty for relevance-based ranking (recommended for best results)."
        ),
    )


class DataLakeSearchBackend(ABC):
    """Abstract base class for searching the DataLake catalog.

    Subclasses must implement :meth:`search`. Authentication is handled
    by the credential factories in ``auth.providers`` — backends obtain
    credentials directly rather than accepting user tokens.

    Implement a custom subclass to add hard constraints on what assets
    the agent can discover (e.g., restricting to specific domains or
    sources) or to override the default search behavior entirely.
    """

    @abstractmethod
    async def search(self, params: "DataLakeSearchParams") -> list[dict]:
        """Search the DataLake catalog with the given parameters.

        Args:
            params: Search parameters including query, filters, and pagination.

        Returns:
            List of matching catalog asset dicts, filtered by access permissions.
        """
        ...


class DefaultDataLakeSearchBackend(DataLakeSearchBackend):
    """Default Azure AI Search implementation of :class:`DataLakeSearchBackend`.

    Performs hybrid search (text + semantic vector matching) against the
    DataLake artifact registry. Applies per-resource RBAC permission
    filtering to ensure users only see assets they can access.
    """

    def __init__(self):
        super().__init__()
        self._client_manager = DataLakeSearchClientManager()

    async def search(self, params: "DataLakeSearchParams") -> list[dict]:
        """Search the DataLake catalog using Azure AI Search with RBAC filtering.

        Args:
            params: Validated search parameters.

        Returns:
            List of accessible catalog asset dicts.
        """
        # Build search filters
        filter_parts = []

        # Filter by artifact types if specified
        if params.artifact_types:
            # Escape single quotes to prevent OData filter injection
            escaped_types = [t.replace("'", "''") for t in params.artifact_types]
            type_filters = [f"artifact_type eq '{t}'" for t in escaped_types]
            filter_parts.append(f"({' or '.join(type_filters)})")
            LOGGER.debug(f"Applying artifact type filter: {type_filters}")

        # Filter by domains if specified
        if params.domains:
            escaped_domains = [d.replace("'", "''") for d in params.domains]
            domain_filters = [f"domain eq '{d}'" for d in escaped_domains]
            filter_parts.append(f"({' or '.join(domain_filters)})")
            LOGGER.debug(f"Applying domain filter: {domain_filters}")

        # Filter by sources if specified
        if params.sources:
            escaped_sources = [s.replace("'", "''") for s in params.sources]
            source_filters = [f"source eq '{s}'" for s in escaped_sources]
            filter_parts.append(f"({' or '.join(source_filters)})")
            LOGGER.debug(f"Applying source filter: {source_filters}")

        # Combine all filters with AND
        filter_expr = " and ".join(filter_parts) if filter_parts else None
        LOGGER.debug(f"Final OData filter: {filter_expr}")

        # Query catalog using hybrid search (text + vector) with semantic ranking
        # The index has vector fields (description_vector, semantic_dataset_description_vector)
        # and semantic configuration "default-semantic-config"
        try:
            # Strip 'asset_tag' from select_fields — it's a computed field, not in the search index
            effective_select = params.select_fields
            if effective_select and "asset_tag" in effective_select:
                effective_select = [f for f in effective_select if f != "asset_tag"]
                if not effective_select:
                    effective_select = None  # empty list → return all fields

            results = await self._client_manager.search(
                search_text=params.query,
                filter=filter_expr,
                select=effective_select,  # None = all fields, or agent-specified list
                top=params.top,
                query_type="semantic",
                semantic_configuration_name="default-semantic-config",
                search_mode=params.search_mode,  # None uses default intelligent matching
                order_by=params.order_by,  # None uses semantic relevance ranking
            )
            LOGGER.debug("Using hybrid semantic search with vector embeddings")
        except HttpResponseError as e:
            # If semantic search fails, fall back to regular search
            if "semanticConfiguration" in str(e) or "semantic" in str(e).lower():
                LOGGER.warning("Semantic search failed, falling back to regular full-text search")
                results = await self._client_manager.search(
                    search_text=params.query,
                    filter=filter_expr,
                    select=effective_select,
                    top=params.top,
                    search_mode=params.search_mode,
                    order_by=params.order_by,
                )
            else:
                raise

        # Convert async iterator to list
        catalog_assets = []
        async for result in results:
            asset = dict(result)
            # Pre-format asset tag so agents can copy it directly
            asset_tag = format_asset_tag(asset)
            if asset_tag:
                asset["asset_tag"] = asset_tag
            catalog_assets.append(asset)

        LOGGER.info(f"Retrieved {len(catalog_assets)} DataLake artifacts for query: '{params.query}'")

        # Filter artifacts by per-resource RBAC permissions
        assets_with_scope = []
        assets_without_scope = []
        for asset in catalog_assets:
            rbac_scope = asset.get("rbacScope")
            if not rbac_scope:
                assets_without_scope.append(asset)
            else:
                assets_with_scope.append((asset, rbac_scope))

        accessible_assets = assets_without_scope
        if assets_with_scope:
            permission_tasks = [
                check_resource_permissions(resource_id=rbac_scope) for _, rbac_scope in assets_with_scope
            ]
            permission_results = await asyncio.gather(*permission_tasks, return_exceptions=True)

            for (asset, _), result in zip(assets_with_scope, permission_results):
                if isinstance(result, Exception):
                    LOGGER.error(
                        f"Error checking permissions for {asset.get('artifact_id')}: {result}", exc_info=result
                    )
                    LOGGER.warning(f"Excluding asset due to permission check failure: {asset.get('artifact_id')}")
                elif result:
                    accessible_assets.append(asset)
                else:
                    LOGGER.info(
                        f"User does not have access to {asset.get('name', asset.get('artifact_id'))}, filtered out"
                    )

        LOGGER.info(f"After RBAC filtering: {len(accessible_assets)} accessible DataLake artifacts")
        return accessible_assets


def is_data_lake_configured() -> bool:
    """
    Check if DataLake catalog integration is configured.

    Returns:
        True if DATA_LAKE_SEARCH_ENDPOINT or DATA_LAKE_LOCAL_CATALOG is set, False otherwise.
    """
    return bool(os.getenv("DATA_LAKE_SEARCH_ENDPOINT") or os.getenv("DATA_LAKE_LOCAL_CATALOG"))


async def _discover_available_domains() -> list[str]:
    """Query the artifact registry index to discover distinct domain values.

    Uses the async SearchClient with credentials from auth.providers so it
    works with both API key and Entra authentication.

    Returns:
        Sorted list of unique domain strings found in the index.
        Returns an empty list on any error (network, auth, missing index).
    """
    endpoint = os.getenv("DATA_LAKE_SEARCH_ENDPOINT")
    local_catalog = os.getenv("DATA_LAKE_LOCAL_CATALOG")

    if local_catalog and not endpoint:
        try:
            from .local import discover_local_catalog_domains

            local_domains = discover_local_catalog_domains(local_catalog)
            LOGGER.info(f"Discovered {len(local_domains)} domain(s) in local catalog: {local_domains}")
            return local_domains
        except Exception as e:
            LOGGER.warning(f"Failed to discover domains from local catalog: {e}")
            return []

    try:
        index_name = os.getenv("DATA_LAKE_CATALOG_INDEX_NAME", "artifact-registry")
        if not endpoint:
            return []

        credential = get_search_credential_async()
        client = SearchClient(
            endpoint=endpoint,
            index_name=index_name,
            credential=credential,
        )

        try:
            # Use a facets query on "domain" to discover all distinct domain values.
            # ("domain" in the index maps to Purview's "collection" concept)
            results = await client.search(
                search_text="*",
                facets=["domain"],
                top=0,
            )

            facets = await results.get_facets() or {}
            domain_facets = facets.get("domain", []) or []
            domains: set[str] = set()
            for facet_entry in domain_facets:
                value = facet_entry.get("value")
                if value:
                    domains.add(value)
        finally:
            await client.close()
        LOGGER.info(f"Discovered {len(domains)} domain(s) in artifact registry: {sorted(domains)}")
        return sorted(domains)
    except Exception as e:
        LOGGER.warning(f"Failed to discover domains from artifact registry: {e}")
        return []


def _build_search_params_model(available_domains: list[str]) -> type[BaseModel]:
    """Build a DataLakeSearchParams model with dynamically discovered domain values.

    Args:
        available_domains: List of domain strings discovered from the index.

    Returns:
        A Pydantic model class with the domains description populated.
    """
    if available_domains:
        domain_list = ", ".join(f"'{d}'" for d in available_domains)
        domain_desc = f"Optional list of domains to filter by. Available domains: {domain_list}. Leave empty to search all domains."
    else:
        domain_desc = "Optional list of domains to filter by. Leave empty to search all domains."

    class DynamicDataLakeSearchParams(DataLakeSearchParams):
        domains: Optional[list[str]] = Field(
            default=None,
            description=domain_desc,
        )

    # Keep the original model name so MAF tool schema looks clean
    DynamicDataLakeSearchParams.__name__ = "DataLakeSearchParams"
    DynamicDataLakeSearchParams.__qualname__ = "DataLakeSearchParams"
    return DynamicDataLakeSearchParams


async def create_data_lake_search_tool(
    backend: Optional[DataLakeSearchBackend] = None,
) -> Callable:
    """
    Create a MAF tool for DataLake catalog search.

    Args:
        backend: Optional :class:`DataLakeSearchBackend` implementation to use.
            When ``None`` (default), a :class:`DefaultDataLakeSearchBackend` is
            created automatically, and its internal ``SearchClient`` lifecycle
            is fully managed by this function — a new client is created for
            each call and refreshed automatically on auth errors.
            When a custom *backend* is provided, the caller owns its resource
            lifecycle (e.g. any ``SearchClient`` or credential it holds).
            The caller is responsible for closing those resources when the
            agent shuts down. Provide a custom backend to add hard constraints
            on which assets the agent can discover.

    Returns:
        MAF tool callable that can be used as a tool

    Raises:
        ValueError: If DataLake is not configured or configuration is invalid.

    Example — default backend (no custom constraints)::

        tool = await create_data_lake_search_tool()
        agent = chat_client.create_agent(name="resource_selector", instructions=prompt, tools=[tool])

    Example — custom backend with a hard domain constraint::

        class EnergyOnlyBackend(DataLakeSearchBackend):
            async def search(self, params):
                restricted = params.model_copy(update={"domains": ["energy"]})
                return await DefaultDataLakeSearchBackend().search(restricted)


        tool = await create_data_lake_search_tool(backend=EnergyOnlyBackend())
    """
    if backend is None:
        if os.getenv("DATA_LAKE_SEARCH_ENDPOINT"):
            backend = DefaultDataLakeSearchBackend()
        elif os.getenv("DATA_LAKE_LOCAL_CATALOG"):
            from .local import LocalDataLakeSearchBackend

            backend = LocalDataLakeSearchBackend(os.getenv("DATA_LAKE_LOCAL_CATALOG", ""))
        else:
            backend = DefaultDataLakeSearchBackend()

    # Discover available domains from the index at tool creation time
    available_domains = await _discover_available_domains()
    SearchParamsModel = _build_search_params_model(available_domains)

    # Create MAF tool with SearchClient manager
    @tool(
        name="search_data_lake_catalog",
        description=(
            "Search the DataLake artifact registry for relevant datasets, models, and data assets. "
            "Uses hybrid search (text + AI semantic vector matching) for best results. "
            "Use this tool to discover what data and artifacts are available before selecting resources. "
            "Returns structured information about matching artifacts including name, description, "
            "artifact type, domain, source, semantic dataset information, and timestamps. "
        ),
        approval_mode="never_require",
    )
    async def search_catalog(params: SearchParamsModel) -> str:  # type: ignore[valid-type]
        """
        Search DataLake catalog using the configured backend.

        Args:
            params: Validated search parameters from Pydantic model

        Returns:
            JSON string of matching catalog assets
        """
        try:
            # Handle dict input (convert to Pydantic model if needed)
            if isinstance(params, dict):
                params = SearchParamsModel(**params)

            assets = await backend.search(params)
            return json.dumps(assets, indent=2)

        except Exception as e:
            error_msg = f"Error searching DataLake catalog: {type(e).__name__}: {str(e)}"
            LOGGER.error(error_msg, exc_info=True)
            return json.dumps([{"error": error_msg}])

    return search_catalog


async def validate_assets_against_catalog(qualified_names: list[str]) -> list[dict]:
    """
    Validate artifact IDs directly against DataLake artifact registry.

    Queries DataLake directly for each artifact_id to:
    1. Verify the artifact exists
    2. Retrieve full metadata
    3. Respect data access permissions (RBAC)

    Supports both Azure AI Search and local catalog backends.

    Args:
        qualified_names: List of artifact_ids to validate

    Returns:
        List of validated artifact dictionaries with full metadata
    """
    if not qualified_names:
        return []

    # Only validate if DataLake is configured
    if not is_data_lake_configured():
        raise ValueError(
            "DataLake catalog not configured. Set DATA_LAKE_SEARCH_ENDPOINT or DATA_LAKE_LOCAL_CATALOG."
        )

    # Use local backend if no Azure endpoint but local catalog is configured
    local_catalog = os.getenv("DATA_LAKE_LOCAL_CATALOG")
    if not os.getenv("DATA_LAKE_SEARCH_ENDPOINT") and local_catalog:
        from .local import LocalDataLakeSearchBackend

        backend = LocalDataLakeSearchBackend(local_catalog)
        validated_assets = []
        for artifact_id in qualified_names:
            # Search with wildcard query and match by artifact_id
            params = DataLakeSearchParams(query=artifact_id, top=50)
            results = await backend.search(params)
            matched = [a for a in results if a.get("artifact_id") == artifact_id]
            if matched:
                LOGGER.info(f"[DATA_LAKE] Validated artifact_id (local): {artifact_id!r}")
                validated_assets.append(matched[0])
            else:
                LOGGER.warning(f"Artifact not found in local catalog: {artifact_id}")
        return validated_assets

    validated_assets = []
    client_manager = DataLakeSearchClientManager()

    for artifact_id in qualified_names:
        try:
            LOGGER.debug(f"Validating artifact_id: {artifact_id}")

            # Escape single quotes for OData filter
            escaped_id = artifact_id.replace("'", "''")
            results = await client_manager.search(
                search_text="*",
                filter=f"artifact_id eq '{escaped_id}'",
                select=[
                    "artifact_id",
                    "name",
                    "description",
                    "artifact_type",
                    "domain",
                    "source",
                    "rbacScope",
                    "semantic_dataset_id",
                    "semantic_dataset_name",
                    "semantic_dataset_description",
                    "detail_index",
                    "detail_key",
                ],
                top=1,
            )

            # Get the artifact record
            found = False
            async for result in results:
                asset = dict(result)
                # Pre-format asset tag
                asset_tag = format_asset_tag(asset)
                if asset_tag:
                    asset["asset_tag"] = asset_tag
                LOGGER.info(f"[DATA_LAKE] Validated artifact_id: {artifact_id!r}")
                validated_assets.append(asset)
                found = True
                break  # cleanly exit async iterator

            if not found:
                LOGGER.warning(f"Artifact not found in registry: {artifact_id}")

        except Exception as e:
            LOGGER.error(f"Error validating artifact {artifact_id}: {e}")

    await client_manager.close()

    return validated_assets
