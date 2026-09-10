"""Authentication exports with optional Azure implementations."""

from __future__ import annotations

from importlib import import_module

from .base import (
    AccessToken,
    AuthConfig,
    CredentialError,
    CredentialProvider,
    IdentityExtractor,
    TokenValidationError,
    TokenValidator,
)
from .noop import (
    NoOpCredentialProvider,
    NoOpIdentityExtractor,
    NoOpTokenValidator,
    create_noop_auth_config,
)

_AZURE_EXPORTS = {
    "BearerTokenAuth": ("azure_credentials", "BearerTokenAuth"),
    "CredentialProviderTokenCredential": ("entra", "CredentialProviderTokenCredential"),
    "EntraCredentialProvider": ("entra", "EntraCredentialProvider"),
    "EntraIdentityExtractor": ("entra", "EntraIdentityExtractor"),
    "EntraTokenValidator": ("entra", "EntraTokenValidator"),
    "create_entra_auth_config": ("entra", "create_entra_auth_config"),
    "get_search_auth_headers_async": ("azure_credentials", "get_search_auth_headers_async"),
    "get_search_credential": ("azure_credentials", "get_search_credential"),
    "get_search_credential_async": ("azure_credentials", "get_search_credential_async"),
    "get_token_provider": ("azure_credentials", "get_token_provider"),
    "is_key_based_auth": ("azure_credentials", "is_key_based_auth"),
}
_AZURE_SUBMODULES = {"azure_credentials", "entra"}
_AZURE_AVAILABLE = False

try:
    from .entra import (
        CredentialProviderTokenCredential,
        EntraCredentialProvider,
        EntraIdentityExtractor,
        EntraTokenValidator,
        create_entra_auth_config,
    )
    from .azure_credentials import (
        BearerTokenAuth,
        get_search_auth_headers_async,
        get_search_credential,
        get_search_credential_async,
        get_token_provider,
        is_key_based_auth,
    )
except ModuleNotFoundError as exc:
    if exc.name != "azure" and not (exc.name or "").startswith("azure."):
        raise
else:
    _AZURE_AVAILABLE = True


def _missing_azure_extra() -> ImportError:
    return ImportError("Azure authentication and credential helpers require the 'agora-workbench[azure]>=0.3.0' extra.")


def __getattr__(name: str) -> object:
    if name in _AZURE_SUBMODULES:
        if not _AZURE_AVAILABLE:
            raise _missing_azure_extra()
        value = import_module(f"{__name__}.{name}")
        globals()[name] = value
        return value
    try:
        module_name, attribute_name = _AZURE_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    if not _AZURE_AVAILABLE:
        raise _missing_azure_extra()
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | _AZURE_SUBMODULES | _AZURE_EXPORTS.keys())


__all__ = [
    "AccessToken",
    "AuthConfig",
    "CredentialError",
    "CredentialProvider",
    "IdentityExtractor",
    "TokenValidationError",
    "TokenValidator",
    "NoOpCredentialProvider",
    "NoOpIdentityExtractor",
    "NoOpTokenValidator",
    "create_noop_auth_config",
]

if _AZURE_AVAILABLE:
    __all__ += [
        "CredentialProviderTokenCredential",
        "EntraCredentialProvider",
        "EntraIdentityExtractor",
        "EntraTokenValidator",
        "create_entra_auth_config",
        "BearerTokenAuth",
        "get_search_auth_headers_async",
        "get_search_credential",
        "get_search_credential_async",
        "get_token_provider",
        "is_key_based_auth",
    ]
