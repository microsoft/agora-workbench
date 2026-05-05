"""
Authentication utilities for Azure services.

This module provides:

1. **Service-specific credential factories** (support both API key and Entra ID):
   - `get_search_credential()` / `get_search_credential_async()` — for Azure AI Search
   - `get_search_auth_headers_async()` — auth headers for raw HTTP to Search
   - `get_storage_connection_string()` — Azure Storage connection string
   - `is_key_based_auth()` — check if running in key-based mode

2. **Token providers** (Entra-only, for services requiring bearer tokens):
   - `get_token_provider(scope)` — returns a callable that yields fresh tokens
   - `BearerTokenAuth` — httpx Auth class using a token provider

3. **OBO helpers** (enterprise multi-tenant only):
   - `create_obo_credential(user_token)` / `create_async_obo_credential(user_token)`

See `auth.providers` for the credential factories and `auth.obo` for
On-Behalf-Of helpers used in enterprise multi-tenant deployments.
"""

from .obo import (
    create_async_obo_credential,
    create_obo_credential,
)
from .providers import (
    BearerTokenAuth,
    get_search_auth_headers_async,
    get_search_credential,
    get_search_credential_async,
    get_storage_connection_string,
    get_token_provider,
    is_key_based_auth,
)

__all__ = [
    # Service-specific factories (support API key + Entra)
    "get_search_credential",
    "get_search_credential_async",
    "get_search_auth_headers_async",
    "get_storage_connection_string",
    "is_key_based_auth",
    # Token providers (Entra-only)
    "BearerTokenAuth",
    "get_token_provider",
    # OBO (enterprise multi-tenant only)
    "create_async_obo_credential",
    "create_obo_credential",
]
