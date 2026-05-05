"""
On-Behalf-Of (OBO) authentication helpers.

These functions are used in multi-tenant enterprise deployments where the
backend service needs to act on behalf of a signed-in user. They exchange
the user's JWT token for a downstream token that carries the user's identity.

For single-user / open-source deployments:
    Set OBO_SIMULATION_MODE=true to bypass OBO and use your local credentials
    (Azure CLI / Managed Identity) directly. This is the correct production
    mode for single-user environments.

Features that require OBO (enterprise-only):
    - Azure RBAC permission checks (data_lake/tools/permissions.py)
    - User-scoped search queries (data_lake/tools/adapters/maf.py)
    - Per-user tool search (tools/search/azure_ai_tool_search.py)
"""

import logging
import os

from azure.identity import OnBehalfOfCredential
from azure.identity.aio import OnBehalfOfCredential as AsyncOnBehalfOfCredential

from .providers import (
    _create_async_credential_chain,
    _create_sync_credential_chain,
    AsyncSearchCredential,
    SearchCredential,
)

LOGGER = logging.getLogger(__name__)

__all__ = [
    "create_async_obo_credential",
    "create_obo_credential",
]


def create_obo_credential(user_token: str) -> SearchCredential:
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
        OnBehalfOfCredential or ChainedTokenCredential (in simulation mode).

    Environment Variables:
        OBO_SIMULATION_MODE: Set to 'true', '1', or 'yes' to enable simulation mode
        ENTRA_TENANT_ID: Azure AD tenant ID (required for OBO mode)
        ENTRA_CLIENT_ID: Application (client) ID (required for OBO mode)

    Raises:
        EnvironmentError: If required environment variables are missing
    """
    sim_env = os.getenv("OBO_SIMULATION_MODE", "").lower()
    simulation_mode = sim_env in ("true", "1", "yes")

    if simulation_mode:
        LOGGER.warning(
            "OBO SIMULATION MODE ENABLED - Using Azure CLI/Managed Identity credentials. "
            "This should only be used for local development!"
        )
        return _create_sync_credential_chain()

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


def create_async_obo_credential(user_token: str) -> AsyncSearchCredential:
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
        Async OnBehalfOfCredential or AsyncChainedTokenCredential (in simulation mode).

    Environment Variables:
        OBO_SIMULATION_MODE: Set to 'true', '1', or 'yes' to enable simulation mode
        ENTRA_TENANT_ID: Azure AD tenant ID (required for OBO mode)
        ENTRA_CLIENT_ID: Application (client) ID (required for OBO mode)

    Raises:
        EnvironmentError: If required environment variables are missing
    """
    sim_env = os.getenv("OBO_SIMULATION_MODE", "").lower()
    simulation_mode = sim_env in ("true", "1", "yes")

    if simulation_mode:
        LOGGER.warning(
            "OBO SIMULATION MODE ENABLED - Using Azure CLI/Managed Identity credentials. "
            "This should only be used for local development!"
        )
        return _create_async_credential_chain()

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
