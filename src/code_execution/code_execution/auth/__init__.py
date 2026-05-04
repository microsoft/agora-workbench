"""Authentication module for MCP code execution servers.

Provides pluggable authentication via abstract interfaces (TokenValidator,
IdentityExtractor, CredentialProvider) with concrete implementations for
Azure Entra ID and a no-op/development mode.
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
    "EntraCredentialProvider",
    "EntraIdentityExtractor",
    "EntraTokenValidator",
    "create_entra_auth_config",
    # No-op / development implementations
    "NoOpCredentialProvider",
    "NoOpIdentityExtractor",
    "NoOpTokenValidator",
    "create_noop_auth_config",
]
