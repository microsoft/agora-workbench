"""Authentication module for MCP code execution servers.

Provides pluggable authentication via abstract interfaces (TokenValidator,
IdentityExtractor, CredentialProvider) with concrete implementations for
Azure Entra ID and a no-op/development mode.

Legacy OBO token exchange and IRM decryption are also re-exported for
backward compatibility.
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
from .obo_credential import (
    _AsyncOBOCredentialWrapper,
    OBOTokenExchangeError,
    OBOCredentialProvider,
    get_obo_credential_provider,
    configure_obo_provider_factory,
)

# IRM functions are imported lazily because their dependencies (olefile,
# httpx, cryptography) live in the code-execution Docker image but are not
# required by the outer AgoraAgentMAF package.  Consumers should import
# directly from ``code_execution.auth.irm`` or use the lazy accessors below.


def __getattr__(name: str):
    _irm_names = {"IRMDecryptionError", "is_irm_protected", "decrypt_irm_file"}
    if name in _irm_names:
        from . import irm

        return getattr(irm, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Abstract interfaces
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
    # Legacy OBO
    "_AsyncOBOCredentialWrapper",
    "OBOTokenExchangeError",
    "OBOCredentialProvider",
    "get_obo_credential_provider",
    "configure_obo_provider_factory",
    # IRM (lazy)
    "IRMDecryptionError",
    "is_irm_protected",
    "decrypt_irm_file",
]
