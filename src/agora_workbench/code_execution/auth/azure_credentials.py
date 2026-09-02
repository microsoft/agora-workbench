"""
Service-specific credential factories for Azure services.

These factories support both Entra ID (token-based) and API key authentication,
enabling open-source users to run without an Entra ID configuration.

Usage:
    # For Azure AI Search:
    credential = get_search_credential()       # sync
    credential = get_search_credential_async() # async

    # For raw HTTP calls that need auth headers:
    headers = await get_search_auth_headers_async()

    # For bearer token provider (Entra-only, e.g. MCP servers):
    token_provider = get_token_provider(scope)

Environment Variables:
    AZURE_SEARCH_API_KEY: API key for Azure AI Search (query or admin key).
        When set, key-based auth is used instead of Entra ID.
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
    ChainedTokenCredential as AsyncChainedTokenCredential,
    ManagedIdentityCredential as AsyncManagedIdentityCredential,
)

from agora_workbench.code_execution.data_access.credentials import MsalCacheCredential

LOGGER = logging.getLogger(__name__)

# Type aliases for clarity
SearchCredential = Union[TokenCredential, AzureKeyCredential]
AsyncSearchCredential = Union[AsyncTokenCredential, AzureKeyCredential]

# Environment variable names
AZURE_SEARCH_API_KEY_ENV = "AZURE_SEARCH_API_KEY"


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
    falls back to Entra ID authentication (Managed Identity -> CLI chain).

    Returns:
        AzureKeyCredential if AZURE_SEARCH_API_KEY is set, otherwise a
        ChainedTokenCredential (ManagedIdentity -> AzureCLI).
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
    falls back to Entra ID authentication (Managed Identity -> MSAL cache chain).

    Returns:
        AzureKeyCredential if AZURE_SEARCH_API_KEY is set, otherwise an
        async ChainedTokenCredential (ManagedIdentity -> MsalCacheCredential).
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
# Token provider (Entra-only)
# =============================================================================


def get_token_provider(scope: str) -> Callable[[], str]:
    """
    Create an Entra ID token provider for Azure service authentication.

    Uses a credential chain that tries Managed Identity first, then Azure CLI.
    This avoids invoking the Azure CLI in deployed environments while preserving
    it as a fallback for local development.

    Note: This function requires Entra ID authentication and does not support
    API key auth. It is used for services that require bearer tokens (e.g.,
    MCP servers, Azure OpenAI with Entra).

    Args:
        scope: Azure scope for token (e.g., 'https://cognitiveservices.azure.com/.default')

    Returns:
        Token provider function that can be called to get bearer tokens.
    """
    credential = _create_sync_credential_chain()
    token_provider = get_bearer_token_provider(credential, scope)
    LOGGER.debug(f"Created Entra ID token provider for scope: {scope}")
    return token_provider


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
    """Create the sync credential chain (Managed Identity -> CLI)."""
    managed_identity_client_id = _get_managed_identity_client_id()
    managed_identity = (
        ManagedIdentityCredential(client_id=managed_identity_client_id)
        if managed_identity_client_id
        else ManagedIdentityCredential()
    )
    credentials = [
        managed_identity,
        AzureCliCredential(),
    ]
    return ChainedTokenCredential(*credentials)


def _create_async_credential_chain() -> AsyncChainedTokenCredential:
    """Create the async credential chain (Managed Identity -> MSAL cache)."""
    managed_identity_client_id = _get_managed_identity_client_id()
    managed_identity = (
        AsyncManagedIdentityCredential(client_id=managed_identity_client_id)
        if managed_identity_client_id
        else AsyncManagedIdentityCredential()
    )
    credentials = [
        managed_identity,
        MsalCacheCredential(),
    ]
    return AsyncChainedTokenCredential(*credentials)


def _get_managed_identity_client_id() -> str | None:
    """Return the configured managed identity client ID when non-empty."""
    client_id = os.getenv("DEFAULT_IDENTITY_CLIENT_ID")
    if client_id is None:
        return None
    return client_id.strip() or None
