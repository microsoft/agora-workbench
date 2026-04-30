"""Authentication module for OBO token exchange, credential management, and IRM decryption."""

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
    "_AsyncOBOCredentialWrapper",
    "OBOTokenExchangeError",
    "OBOCredentialProvider",
    "get_obo_credential_provider",
    "configure_obo_provider_factory",
    "IRMDecryptionError",
    "is_irm_protected",
    "decrypt_irm_file",
]
