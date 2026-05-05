"""
Authentication utilities for Azure services.

This module provides two sets of APIs:

1. **Service-specific credential factories** (recommended for new code):
   - `get_search_credential()` / `get_search_credential_async()` — support both
     API key and Entra ID auth, driven by environment variables.
   - `get_storage_connection_string()` — returns connection string if configured.
   - `is_key_based_auth()` — check if running in key-based mode.

2. **Entra ID-specific helpers** (legacy, for features requiring token-based auth):
   - `create_azure_credential()` / `create_azure_credential_async()`
   - `create_entra_token_provider(scope)`
   - `create_obo_credential(user_token)` / `create_async_obo_credential(user_token)`

See `auth.providers` for the service-specific factories and `auth.obo` for
On-Behalf-Of helpers used in enterprise multi-tenant deployments.
"""

from .auth import (
    BearerTokenAuth,
    create_async_obo_credential,
    create_azure_credential,
    create_azure_credential_async,
    create_entra_token_provider,
    create_obo_credential,
)
from .providers import (
    get_search_credential,
    get_search_credential_async,
    get_storage_connection_string,
    is_key_based_auth,
)

__all__ = [
    # Service-specific factories (support API key + Entra)
    "get_search_credential",
    "get_search_credential_async",
    "get_storage_connection_string",
    "is_key_based_auth",
    # Entra ID helpers (token-based only)
    "BearerTokenAuth",
    "create_async_obo_credential",
    "create_azure_credential",
    "create_azure_credential_async",
    "create_entra_token_provider",
    "create_obo_credential",
]
