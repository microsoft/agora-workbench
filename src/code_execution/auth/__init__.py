"""Authentication module for MCP code execution servers.

Provides pluggable authentication via abstract interfaces (TokenValidator,
IdentityExtractor, CredentialProvider) with concrete implementations for
Azure Entra ID and a no-op/development mode.

Also provides service-specific Azure credential factories (for Search,
Storage, etc.) that support both API key and Entra ID auth modes.
"""

from .base import (
    AccessToken,
    AuthConfig,
    CredentialError,
    CredentialProvider,
    IdentityExtractor,
    TokenValidationError,
    TokenValidator,
)
from .entra import (
    CredentialProviderTokenCredential,
    EntraCredentialProvider,
    EntraIdentityExtractor,
    EntraTokenValidator,
    create_entra_auth_config,
)
from .noop import (
    NoOpCredentialProvider,
    NoOpIdentityExtractor,
    NoOpTokenValidator,
    create_noop_auth_config,
)
from .azure_credentials import (
    BearerTokenAuth,
    get_search_auth_headers_async,
    get_search_credential,
    get_search_credential_async,
    get_token_provider,
    is_key_based_auth,
)


__all__ = [
    # Abstract interfaces
    "AccessToken",
    "AuthConfig",
    "CredentialError",
    "CredentialProvider",
    "IdentityExtractor",
    "TokenValidationError",
    "TokenValidator",
    # Entra ID implementations
    "CredentialProviderTokenCredential",
    "EntraCredentialProvider",
    "EntraIdentityExtractor",
    "EntraTokenValidator",
    "create_entra_auth_config",
    # No-op / development implementations
    "NoOpCredentialProvider",
    "NoOpIdentityExtractor",
    "NoOpTokenValidator",
    "create_noop_auth_config",
    # Azure credential helpers
    "BearerTokenAuth",
    "get_search_auth_headers_async",
    "get_search_credential",
    "get_search_credential_async",
    "get_token_provider",
    "is_key_based_auth",
]
