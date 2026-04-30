"""
Azure RBAC permission checking for data lake artifacts.

This module provides functionality to check if a user has permissions on Azure
resources using the Azure Resource Manager permissions API.
"""

import asyncio
import fnmatch
import logging
from typing import Optional

from azure.mgmt.authorization import AuthorizationManagementClient
from azure.mgmt.core.tools import parse_resource_id
from azure.core.exceptions import (
    HttpResponseError,
    ClientAuthenticationError,
    ResourceNotFoundError,
)

from auth import create_obo_credential

LOGGER = logging.getLogger(__name__)

# Resource-type-specific actions for data access
# Maps resource provider namespace to actions needed for data access
# ANY action in the list is sufficient for access
RESOURCE_TYPE_ACTIONS = {
    "Microsoft.Storage": [
        "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read",
        "Microsoft.Storage/storageAccounts/blobServices/containers/read",
        "Microsoft.Storage/*",  # Broad storage wildcard (e.g., Contributor role)
        "*/read",  # General read permission (e.g., Reader role)
    ],
    "Microsoft.Sql": [
        "Microsoft.Sql/servers/databases/query/action",  # Required for data queries
        "Microsoft.Sql/*",  # Broad SQL wildcard
    ],
    "Microsoft.DocumentDB": [
        "Microsoft.DocumentDB/databaseAccounts/readonlykeys/action",  # Data access
        "Microsoft.DocumentDB/*",  # Broad CosmosDB wildcard
    ],
}


def get_acceptable_actions_for_resource(resource_id: str) -> list[str]:
    """
    Get the appropriate acceptable actions for a resource based on its type.

    Parses the resource ID to determine the provider namespace and returns
    the corresponding actions from RESOURCE_TYPE_ACTIONS.

    Args:
        resource_id: Azure Resource ID
            Example: '/subscriptions/.../Microsoft.Storage/storageAccounts/.../containers/data'

    Returns:
        List of acceptable actions for the resource type
    """
    resource_parts = parse_resource_id(resource_id)
    provider_namespace = str(resource_parts["resource_namespace"])

    # Look up actions for this resource type
    if provider_namespace in RESOURCE_TYPE_ACTIONS:
        return RESOURCE_TYPE_ACTIONS[provider_namespace]
    else:
        raise ValueError(f"No specific actions defined for {provider_namespace}")


async def check_resource_permissions(
    resource_id: str,
    user_token: str,
    acceptable_actions: Optional[list[str]] = None,
) -> bool:
    """
    Check if a user has permissions on an Azure resource using ARM permissions API.

    Uses Azure Resource Manager's permissions API to directly query whether the
    authenticated user has access to a specific resource. Returns True if the user
    has ANY of the acceptable actions.

    Args:
        resource_id: Azure Resource ID to check permissions for.
            Format: '/subscriptions/{sub}/resourceGroups/{rg}/providers/{provider}/{type}/{name}'
            Example: '/subscriptions/xxx/.../storageAccounts/myaccount/blobServices/default/containers/data'
        user_token: User's bearer token (JWT) for authentication. Required.
            Must be a valid token that can be used to check the user's permissions.
        acceptable_actions: Optional list of actions where ANY is sufficient for access.
            Examples: ['Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read', '*/read']
            If not provided, automatically determines appropriate actions based on the resource type
            (e.g., Storage blobs need read permissions, SQL databases need query permissions).
            The function returns True as soon as it finds the user has ANY of these actions.

    Returns:
        True if user has ANY of the acceptable actions on the resource, False otherwise.

    Security:
        On errors, fails closed (returns False) to prevent unauthorized access.
        Always authenticates as the user (via token), never as service identity.
    """
    try:
        # Parse resource ID to extract components using Azure's built-in parser
        resource_parts = parse_resource_id(resource_id)
        subscription_id = str(resource_parts["subscription"])
        resource_group = str(resource_parts["resource_group"])
        provider_namespace = str(resource_parts["resource_namespace"])
        resource_type = str(resource_parts["resource_type"])
        resource_name = str(resource_parts["resource_name"])
        parent_path = str(resource_parts.get("resource_parent", "")).rstrip("/")

        LOGGER.debug(
            f"Checking permissions for resource: {resource_name} "
            f"(type: {provider_namespace}/{resource_type}, rg: {resource_group})"
        )

        # Create credential using On-Behalf-Of (OBO) flow with user token
        # This ensures we check permissions in the context of the user, not the service
        credential = create_obo_credential(user_token)

        LOGGER.debug(f"Using user token for permission check (token length: {len(user_token)})")

        # Define acceptable actions - ANY of these is sufficient for access
        # If not explicitly provided, determine based on resource type
        if acceptable_actions is not None:
            _acceptable_actions = acceptable_actions
        else:
            _acceptable_actions = get_acceptable_actions_for_resource(resource_id)
            LOGGER.debug(
                f"Auto-detected {len(_acceptable_actions)} acceptable actions for {provider_namespace} resource"
            )

        # Run the sync operation in a thread pool to avoid blocking
        # AuthorizationManagementClient is sync-only
        loop = asyncio.get_event_loop()
        has_permission = await loop.run_in_executor(
            None,
            _check_permissions_sync,
            credential,
            subscription_id,
            resource_group,
            provider_namespace,
            parent_path,
            resource_type,
            resource_name,
            _acceptable_actions,
        )

        return has_permission

    except ClientAuthenticationError as e:
        LOGGER.error(f"Authentication error checking permissions for {resource_id}: {e}")
        # Fail closed on auth errors
        return False
    except ResourceNotFoundError as e:
        LOGGER.warning(f"Resource not found when checking permissions: {resource_id}: {e}")
        # Resource doesn't exist or user can't see it - deny access
        return False
    except HttpResponseError as e:
        LOGGER.error(f"HTTP error checking permissions for {resource_id}: {e}")
        # Fail closed on API errors
        return False
    except ValueError as e:
        LOGGER.error(f"Invalid resource ID format: {resource_id}: {e}")
        # Fail closed on invalid input
        return False
    except Exception as e:
        LOGGER.error(f"Unexpected error checking permissions for {resource_id}: {type(e).__name__}: {e}", exc_info=True)
        # Fail closed on unexpected errors
        return False


def _check_permissions_sync(
    credential,
    subscription_id: str,
    resource_group: str,
    provider_namespace: str,
    parent_path: str,
    resource_type: str,
    resource_name: str,
    acceptable_actions: list[str],
) -> bool:
    """
    Synchronous helper to check permissions using AuthorizationManagementClient.

    This runs in a thread pool executor to avoid blocking the async event loop.
    Returns True if the user has ANY of the acceptable actions.

    Args:
        credential: Azure credential (sync)
        subscription_id: Azure subscription ID
        resource_group: Resource group name
        provider_namespace: Provider namespace (e.g., 'Microsoft.Storage')
        parent_path: Parent resource path for nested resources
        resource_type: Resource type (e.g., 'containers')
        resource_name: Resource name
        acceptable_actions: List of actions where ANY is sufficient for access

    Returns:
        True if user has ANY of the acceptable actions, False otherwise
    """
    try:
        # Create authorization management client (sync)
        auth_client = AuthorizationManagementClient(credential=credential, subscription_id=subscription_id)

        # List permissions for the resource (sync operation)
        permissions = auth_client.permissions.list_for_resource(
            resource_group_name=resource_group,
            resource_provider_namespace=provider_namespace,
            parent_resource_path=parent_path,
            resource_type=resource_type,
            resource_name=resource_name,
        )

        # Check if user has ANY of the acceptable actions
        for permission in permissions:
            # Check allowed actions
            allowed_actions = permission.actions or []
            denied_actions = permission.not_actions or []

            for acceptable_action in acceptable_actions:
                # Check if this action is explicitly allowed
                if _matches_any_action(acceptable_action, allowed_actions):
                    # Make sure it's not explicitly denied
                    if not _matches_any_action(acceptable_action, denied_actions):
                        LOGGER.debug(f"User has sufficient permission via action: {acceptable_action}")
                        return True  # Found a match - user has access

        LOGGER.info("User does not have any of the acceptable actions")
        return False

    except Exception as e:
        LOGGER.error(f"Error in sync permission check: {e}", exc_info=True)
        # Fail closed
        return False


def _matches_any_action(required_action: str, allowed_actions: list[str]) -> bool:
    """
    Check if a required action matches any of the allowed actions.

    Supports wildcard matching (e.g., '*/read' or 'Microsoft.Storage/*') using
    Python's fnmatch module for standard Unix-style pattern matching.

    Args:
        required_action: The action to check (e.g., 'Microsoft.Storage/.../read')
        allowed_actions: List of allowed action patterns

    Returns:
        True if required action matches any allowed action pattern

    Example:
        >>> _matches_any_action("Microsoft.Storage/storageAccounts/read", ["*/read", "Microsoft.Storage/*"])
        True
    """
    for allowed in allowed_actions:
        if fnmatch.fnmatch(required_action, allowed):
            return True
    return False
