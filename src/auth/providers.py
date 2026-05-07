"""
Service-specific credential factories for Azure services.

These factories support both Entra ID (token-based) and API key authentication,
enabling open-source users to run without an Entra ID configuration.

Usage:
    # For Azure AI Search:
    credential = get_search_credential()       # sync
    credential = get_search_credential_async() # async

    # For raw HTTP calls that need auth headers:
    headers = get_search_auth_headers()        # returns appropriate headers

    # For bearer token provider (Entra-only, e.g. MCP servers):
    token_provider = get_token_provider(scope)

Environment Variables:
    AZURE_SEARCH_API_KEY: API key for Azure AI Search (query or admin key).
        When set, key-based auth is used instead of Entra ID.
    AZURE_STORAGE_CONNECTION_STRING: Connection string for Azure Storage.
        When set, returns this directly for storage clients that accept it.
    DEFAULT_IDENTITY_CLIENT_ID: Client ID for managed identity (Entra mode only).
"""

import logging
import os
from typing import Callable, Generator, Union

import httpx
from azure.core.credentials import AzureKeyCredential, TokenCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity import (
    AzureCliCredential,
    ChainedTokenCredential,
    ManagedIdentityCredential,
    get_bearer_token_provider,
)
from azure.identity.aio import (
    AzureCliCredential as AsyncAzureCliCredential,
    ChainedTokenCredential as AsyncChainedTokenCredential,
    ManagedIdentityCredential as AsyncManagedIdentityCredential,
)

LOGGER = logging.getLogger(__name__)

# Type aliases for clarity
SearchCredential = Union[TokenCredential, AzureKeyCredential]
AsyncSearchCredential = Union[AsyncTokenCredential, AzureKeyCredential]

# Environment variable names
AZURE_SEARCH_API_KEY_ENV = "AZURE_SEARCH_API_KEY"
AZURE_STORAGE_CONNECTION_STRING_ENV = "AZURE_STORAGE_CONNECTION_STRING"


# =============================================================================
# Utility classes
# =============================================================================


class BearerTokenAuth(httpx.Auth):
    """Custom httpx Auth that uses a token provider callable to get fresh tokens."""

    def __init__(self, token_provider: Callable[[], str]):
        self.token_provider = token_provider

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        """Add Bearer token to the request Authorization header."""
        token = self.token_provider()
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


# =============================================================================
# Search credential factories
# =============================================================================


def get_search_credential() -> SearchCredential:
    """
    Get a credential for Azure AI Search clients.

    Checks for an API key first (via AZURE_SEARCH_API_KEY env var), then
    falls back to Entra ID authentication (CLI → Managed Identity chain).

    Returns:
        AzureKeyCredential if AZURE_SEARCH_API_KEY is set, otherwise a
        ChainedTokenCredential (AzureCLI → ManagedIdentity).

    Example:
        >>> from auth.providers import get_search_credential
        >>> from azure.search.documents import SearchClient
        >>> credential = get_search_credential()
        >>> client = SearchClient(endpoint=endpoint, index_name=index, credential=credential)
    """
    api_key = os.getenv(AZURE_SEARCH_API_KEY_ENV)
    if api_key:
        LOGGER.debug("Using API key authentication for Azure AI Search")
        return AzureKeyCredential(api_key)

    LOGGER.debug("Using Entra ID authentication for Azure AI Search")
    return _create_sync_credential_chain()


def get_search_credential_async() -> AsyncSearchCredential:
    """
    Get an async credential for Azure AI Search clients.

    Checks for an API key first (via AZURE_SEARCH_API_KEY env var), then
    falls back to Entra ID authentication (CLI → Managed Identity chain).

    Returns:
        AzureKeyCredential if AZURE_SEARCH_API_KEY is set, otherwise an
        async ChainedTokenCredential (AzureCLI → ManagedIdentity).

    Example:
        >>> from auth.providers import get_search_credential_async
        >>> from azure.search.documents.aio import SearchClient
        >>> credential = get_search_credential_async()
        >>> client = SearchClient(endpoint=endpoint, index_name=index, credential=credential)
    """
    api_key = os.getenv(AZURE_SEARCH_API_KEY_ENV)
    if api_key:
        LOGGER.debug("Using API key authentication for Azure AI Search (async)")
        return AzureKeyCredential(api_key)

    LOGGER.debug("Using Entra ID authentication for Azure AI Search (async)")
    return _create_async_credential_chain()


async def get_search_auth_headers_async() -> dict[str, str]:
    """
    Get authentication headers for raw HTTP calls to Azure AI Search.

    For API key auth, returns an ``api-key`` header.
    For Entra auth, acquires a bearer token and returns an ``Authorization`` header.

    Returns:
        Dict with the appropriate auth header for the current auth mode.
    """
    api_key = os.getenv(AZURE_SEARCH_API_KEY_ENV)
    if api_key:
        return {"api-key": api_key}

    credential = _create_async_credential_chain()
    try:
        token_response = await credential.get_token("https://search.azure.com/.default")
        return {"Authorization": f"Bearer {token_response.token}"}
    finally:
        await credential.close()


# =============================================================================
# Purview credential factory
# =============================================================================


def get_purview_credential() -> ChainedTokenCredential:
    """
    Get a credential for Microsoft Purview clients.

    Uses a credential chain that tries Azure CLI first, then Managed Identity.
    This allows the same code to work in local development (CLI) and deployed
    environments (Managed Identity).

    Returns:
        ChainedTokenCredential (AzureCLI → ManagedIdentity).

    Example:
        >>> from auth.providers import get_purview_credential
        >>> from azure.purview.catalog import PurviewCatalogClient
        >>> credential = get_purview_credential()
        >>> client = PurviewCatalogClient(endpoint=endpoint, credential=credential)
    """
    LOGGER.debug("Using Entra ID credential chain for Microsoft Purview")
    return _create_sync_credential_chain()


# =============================================================================
# Token provider (Entra-only)
# =============================================================================


def get_token_provider(scope: str) -> Callable[[], str]:
    """
    Create an Entra ID token provider for Azure service authentication.

    Uses a credential chain that tries Azure CLI first, then Managed Identity.
    This allows the same code to work in local development (CLI) and deployed
    environments (Managed Identity).

    Note: This function requires Entra ID authentication and does not support
    API key auth. It is used for services that require bearer tokens (e.g.,
    MCP servers, Azure OpenAI with Entra).

    Args:
        scope: Azure scope for token (e.g., 'https://cognitiveservices.azure.com/.default')

    Returns:
        Token provider function that can be called to get bearer tokens.

    Example:
        >>> from auth import get_token_provider
        >>> token_provider = get_token_provider("https://cognitiveservices.azure.com/.default")
        >>> token = token_provider()
    """
    credential = _create_sync_credential_chain()
    token_provider = get_bearer_token_provider(credential, scope)
    LOGGER.debug(f"Created Entra ID token provider for scope: {scope}")
    return token_provider


# =============================================================================
# Storage helpers
# =============================================================================


def get_storage_connection_string() -> str | None:
    """
    Get an Azure Storage connection string if configured.

    Returns:
        The connection string if AZURE_STORAGE_CONNECTION_STRING is set,
        otherwise None (caller should fall back to Entra credential).

    Example:
        >>> from auth.providers import get_storage_connection_string
        >>> conn_str = get_storage_connection_string()
        >>> if conn_str:
        ...     client = BlobServiceClient.from_connection_string(conn_str)
        ... else:
        ...     from azure.identity import DefaultAzureCredential
        ...
        ...     client = BlobServiceClient(account_url, credential=DefaultAzureCredential())
    """
    return os.getenv(AZURE_STORAGE_CONNECTION_STRING_ENV) or None


# =============================================================================
# Auth mode introspection
# =============================================================================


def is_key_based_auth() -> bool:
    """
    Check if Azure AI Search is configured for API key authentication.

    Returns True if AZURE_SEARCH_API_KEY is set, indicating the user
    prefers key-based auth over Entra ID for search operations.

    This is useful for consumers that need to branch behavior based on
    auth mode (e.g., skipping operations that require token-based auth).
    """
    return bool(os.getenv(AZURE_SEARCH_API_KEY_ENV))


# =============================================================================
# Internal helpers
# =============================================================================


def _create_sync_credential_chain() -> ChainedTokenCredential:
    """Create the standard sync credential chain (CLI → Managed Identity)."""
    managed_identity_client_id = os.getenv("DEFAULT_IDENTITY_CLIENT_ID")
    credentials = [
        AzureCliCredential(),
        ManagedIdentityCredential(client_id=managed_identity_client_id),
    ]
    return ChainedTokenCredential(*credentials)


def _create_async_credential_chain() -> AsyncChainedTokenCredential:
    """Create the standard async credential chain (CLI → Managed Identity)."""
    managed_identity_client_id = os.getenv("DEFAULT_IDENTITY_CLIENT_ID")
    credentials = [
        AsyncAzureCliCredential(),
        AsyncManagedIdentityCredential(client_id=managed_identity_client_id),
    ]
    return AsyncChainedTokenCredential(*credentials)
