"""
Service-specific credential factories for Azure services.

These factories support both Entra ID (token-based) and API key authentication,
enabling open-source users to run without an Entra ID configuration.

Usage:
    # For Azure AI Search:
    credential = get_search_credential()       # sync
    credential = get_search_credential_async() # async

    # The returned credential is either:
    #   - AzureKeyCredential (if AZURE_SEARCH_API_KEY is set)
    #   - ChainedTokenCredential (if using Entra/CLI auth)

Environment Variables:
    AZURE_SEARCH_API_KEY: API key for Azure AI Search (query or admin key).
        When set, key-based auth is used instead of Entra ID.
    AZURE_STORAGE_CONNECTION_STRING: Connection string for Azure Storage.
        When set, returns this directly for storage clients that accept it.
    DEFAULT_IDENTITY_CLIENT_ID: Client ID for managed identity (Entra mode only).
"""

import logging
import os
from typing import Union

from azure.core.credentials import AzureKeyCredential, TokenCredential
from azure.identity import AzureCliCredential, ChainedTokenCredential, ManagedIdentityCredential
from azure.identity.aio import (
    AzureCliCredential as AsyncAzureCliCredential,
    ChainedTokenCredential as AsyncChainedTokenCredential,
    ManagedIdentityCredential as AsyncManagedIdentityCredential,
)

try:
    from azure.core.credentials_async import AsyncTokenCredential
except ImportError:
    from azure.core.credentials import TokenCredential as AsyncTokenCredential  # type: ignore[assignment]

LOGGER = logging.getLogger(__name__)

# Type aliases for clarity
SearchCredential = Union[TokenCredential, AzureKeyCredential]
AsyncSearchCredential = Union[AsyncTokenCredential, AzureKeyCredential]

# Environment variable names
AZURE_SEARCH_API_KEY_ENV = "AZURE_SEARCH_API_KEY"
AZURE_STORAGE_CONNECTION_STRING_ENV = "AZURE_STORAGE_CONNECTION_STRING"


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
        ...     client = BlobServiceClient(account_url, credential=get_search_credential())
    """
    return os.getenv(AZURE_STORAGE_CONNECTION_STRING_ENV) or None


def is_key_based_auth() -> bool:
    """
    Check if the environment is configured for key-based authentication.

    Returns True if any API key environment variables are set, indicating
    the user prefers key-based auth over Entra ID.

    This is useful for consumers that need to branch behavior based on
    auth mode (e.g., skipping operations that require token-based auth).
    """
    return bool(os.getenv(AZURE_SEARCH_API_KEY_ENV))


# --- Internal helpers ---


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
