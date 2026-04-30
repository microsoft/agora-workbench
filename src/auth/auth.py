"""
Authentication utilities for Azure services.

Provides reusable authentication helpers for Entra ID token providers.
"""

import os
import logging
from typing import Callable, Any, Generator, Union

import httpx
from azure.identity import (
    get_bearer_token_provider,
    AzureCliCredential,
    ChainedTokenCredential,
    ManagedIdentityCredential,
    OnBehalfOfCredential,
)
from azure.identity.aio import (
    AzureCliCredential as AsyncAzureCliCredential,
    ChainedTokenCredential as AsyncChainedTokenCredential,
    ManagedIdentityCredential as AsyncManagedIdentityCredential,
    OnBehalfOfCredential as AsyncOnBehalfOfCredential,
)

LOGGER = logging.getLogger(__name__)


class BearerTokenAuth(httpx.Auth):
    """Custom httpx Auth that uses a token provider callable to get fresh tokens."""

    def __init__(self, token_provider: Callable[[], str]):
        self.token_provider = token_provider

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        """Add Bearer token to the request Authorization header."""
        token = self.token_provider()
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


def create_azure_credential() -> ChainedTokenCredential:
    """
    Create a reusable Azure credential for service authentication.

    Uses a credential chain that tries Azure CLI first, then Managed Identity.
    This allows the same code to work in local development (CLI) and deployed
    environments (Managed Identity).

    Returns:
        ChainedTokenCredential that can be used with Azure SDK clients

    Example:
        >>> credential = create_azure_credential()
        >>> client = AIProjectClient(endpoint=endpoint, credential=credential)
    """
    managed_identity_client_id = os.getenv("DEFAULT_IDENTITY_CLIENT_ID")

    # Build credential chain: CLI → Managed Identity
    credentials = [
        AzureCliCredential(),
        ManagedIdentityCredential(client_id=managed_identity_client_id),
    ]

    azure_credential = ChainedTokenCredential(*credentials)
    LOGGER.debug("Created Azure credential chain (CLI → Managed Identity)")
    return azure_credential


def create_azure_credential_async() -> AsyncChainedTokenCredential:
    """
    Create an async Azure credential for service authentication.

    Uses a credential chain that tries Azure CLI first, then Managed Identity.
    This allows the same code to work in local development (CLI) and deployed
    environments (Managed Identity).

    Returns:
        Async ChainedTokenCredential that can be used with Azure SDK clients
        that accept AsyncTokenCredential

    Example:
        >>> credential = create_azure_credential_async()
        >>> # Use credential with Azure SDK clients like AzureAISearchContextProvider
    """
    managed_identity_client_id = os.getenv("DEFAULT_IDENTITY_CLIENT_ID")

    # Build credential chain: CLI → Managed Identity
    credentials = [
        AsyncAzureCliCredential(),
        AsyncManagedIdentityCredential(client_id=managed_identity_client_id),
    ]

    azure_credential = AsyncChainedTokenCredential(*credentials)

    LOGGER.debug("Created async Azure credential chain (CLI → Managed Identity)")
    return azure_credential


def create_entra_token_provider(scope: str) -> Callable[[], Any]:
    """
    Create an Entra ID token provider for Azure service authentication.

    Uses a credential chain that tries Azure CLI first, then Managed Identity.
    This allows the same code to work in local development (CLI) and deployed
    environments (Managed Identity).

    Args:
        scope: Azure scope for token (e.g., 'https://cognitiveservices.azure.com/.default')

    Returns:
        Token provider function that can be called to get bearer tokens

    Example:
        >>> token_provider = create_entra_token_provider("https://cognitiveservices.azure.com/.default")
        >>> # Use token_provider with Azure SDK clients
    """
    managed_identity_client_id = os.getenv("DEFAULT_IDENTITY_CLIENT_ID")

    # Build synchronous credential chain for token provider: CLI → Managed Identity
    credentials = [
        AzureCliCredential(),
        ManagedIdentityCredential(client_id=managed_identity_client_id),
    ]

    azure_credential = ChainedTokenCredential(*credentials)
    token_provider = get_bearer_token_provider(azure_credential, scope)

    LOGGER.debug(f"Created Entra ID token provider for scope: {scope}")
    return token_provider


def create_obo_credential(
    user_token: str,
) -> Union[OnBehalfOfCredential, ChainedTokenCredential]:
    """
    Create synchronous credential using On-Behalf-Of (OBO) flow.

    Exchanges the user's token for a new token that:
    1. Maintains the user's identity (OID)
    2. Has permissions for the target scope

    For local development, set OBO_SIMULATION_MODE=true to use Azure CLI
    credentials instead of OBO flow.

    Args:
        user_token: User's bearer token from Authorization header
                   (ignored in simulation mode)

    Returns:
        OnBehalfOfCredential that can be used with synchronous Azure SDK clients,
        or ChainedTokenCredential in simulation mode

    Environment Variables:
        OBO_SIMULATION_MODE: Set to 'true', '1', or 'yes' to enable simulation mode
        ENTRA_TENANT_ID: Azure AD tenant ID (required for OBO mode)
        ENTRA_CLIENT_ID: Application (client) ID (required for OBO mode)

    Example:
        >>> credential = create_obo_credential(request_token)
        >>> client = AuthorizationManagementClient(credential=credential, subscription_id=sub_id)

    Raises:
        EnvironmentError: If required environment variables are missing
    """
    # Check for simulation mode
    sim_env = os.getenv("OBO_SIMULATION_MODE", "").lower()
    simulation_mode = sim_env in ("true", "1", "yes")

    if simulation_mode:
        LOGGER.warning(
            "OBO SIMULATION MODE ENABLED - Using Azure CLI/Managed Identity credentials. "
            "This should only be used for local development!"
        )
        return create_azure_credential()

    # Production OBO mode
    tenant_id = os.getenv("ENTRA_TENANT_ID")
    client_id = os.getenv("ENTRA_CLIENT_ID")

    if not tenant_id or not client_id:
        raise EnvironmentError("Failed to retrieve ENTRA_TENANT_ID and ENTRA_CLIENT_ID environment variables")

    credential = OnBehalfOfCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        user_assertion=user_token,
    )

    LOGGER.debug("Created sync OBO credential")
    return credential


def create_async_obo_credential(
    user_token: str,
) -> Union[AsyncOnBehalfOfCredential, AsyncChainedTokenCredential]:
    """
    Create async credential using On-Behalf-Of (OBO) flow.

    Exchanges the user's token for a new token that:
    1. Maintains the user's identity (OID)
    2. Has permissions for the target scope

    For local development, set OBO_SIMULATION_MODE=true to use Azure CLI
    credentials instead of OBO flow.

    Args:
        user_token: User's bearer token from Authorization header
                   (ignored in simulation mode)

    Returns:
        Async OnBehalfOfCredential that can be used with Azure SDK clients
        that accept AsyncTokenCredential, or AsyncChainedTokenCredential in
        simulation mode

    Environment Variables:
        OBO_SIMULATION_MODE: Set to 'true', '1', or 'yes' to enable simulation mode
        ENTRA_TENANT_ID: Azure AD tenant ID (required for OBO mode)
        ENTRA_CLIENT_ID: Application (client) ID (required for OBO mode)

    Example:
        >>> credential = create_async_obo_credential(request_token)
        >>> # Use credential with Azure SDK clients that accept AsyncTokenCredential

    Raises:
        EnvironmentError: If required environment variables are missing
    """
    # Check for simulation mode
    sim_env = os.getenv("OBO_SIMULATION_MODE", "").lower()
    simulation_mode = sim_env in ("true", "1", "yes")

    if simulation_mode:
        LOGGER.warning(
            "OBO SIMULATION MODE ENABLED - Using Azure CLI/Managed Identity credentials. "
            "This should only be used for local development!"
        )
        return create_azure_credential_async()

    # Production OBO mode
    tenant_id = os.getenv("ENTRA_TENANT_ID")
    client_id = os.getenv("ENTRA_CLIENT_ID")

    if not tenant_id or not client_id:
        raise EnvironmentError("Failed to retrieve ENTRA_TENANT_ID and ENTRA_CLIENT_ID environment variables")

    credential = AsyncOnBehalfOfCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        user_assertion=user_token,
    )

    LOGGER.debug("Created async OBO credential")
    return credential
