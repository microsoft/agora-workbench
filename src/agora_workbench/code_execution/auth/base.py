"""
Abstract authentication interfaces for MCP code execution servers.

This module defines the pluggable authentication contract that decouples
the server from any specific identity provider (Azure Entra ID, Auth0,
Keycloak, etc.).

Three interfaces form the abstraction:

- TokenValidator: Validates bearer tokens and returns claims.
- IdentityExtractor: Derives a unique user identity string from claims.
- CredentialProvider: Provides credentials for downstream resource access.

Implementations for Azure Entra ID live in ``auth.entra``. A no-op
implementation suitable for local development lives in ``auth.noop``.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, NamedTuple, Optional


class AccessToken(NamedTuple):
    """A provider-agnostic access token.

    Structurally compatible with ``azure.core.credentials.AccessToken``.
    """

    token: str
    expires_on: int


class TokenValidationError(Exception):
    """Raised when token validation fails."""

    def __init__(self, message: str, status_code: int = 401):
        self.status_code = status_code
        super().__init__(message)


class TokenValidator(ABC):
    """
    Validates bearer tokens from incoming requests.

    Implementations handle signature verification, expiry checks,
    audience/issuer validation, and any provider-specific logic.

    Implementations do **not** need to publish OAuth discovery metadata. Set
    ``AuthConfig.protected_resource_metadata`` instead — it is provider-agnostic
    and part of the public contract.

    .. deprecated::
        Servers also probe validators for private ``_client_id`` / ``_tenant_id``
        attributes to compose Entra-shaped metadata. That fallback is retained for
        validators written against the old convention; new implementations should
        use ``AuthConfig.protected_resource_metadata``.
    """

    @abstractmethod
    async def validate(self, token: str, *, request_path: str = "/mcp", request_method: str = "POST") -> dict:
        """
        Validate a bearer token and return decoded claims.

        Args:
            token: The raw bearer token string.
            request_path: Optional hint for validators that support path-based
                authorization. Not used by standard JWT validation.
            request_method: Optional hint for validators that support
                method-based authorization. Not used by standard JWT validation.

        Returns:
            A dictionary of validated claims (e.g. ``{"oid": "...", "tid": "...", "name": "..."}``).

        Raises:
            TokenValidationError: If the token is invalid, expired, or
                fails any validation check.
        """
        ...


class IdentityExtractor(ABC):
    """
    Extracts a unique user identity string from validated token claims.

    The identity is used for session ownership and access control.
    Different providers encode identity differently (Azure uses oid+tid,
    generic OIDC uses ``sub``, etc.).
    """

    @abstractmethod
    def extract(self, claims: dict) -> Optional[str]:
        """
        Derive a unique user identity from token claims.

        Args:
            claims: Validated token claims dictionary.

        Returns:
            A unique string identifying the user (e.g. ``"oid@tid"``),
            or ``None`` if required claims are missing.
        """
        ...


class CredentialProvider(ABC):
    """
    Provides credentials for accessing downstream resources on behalf
    of the authenticated user or the server's own identity.

    Implementations may use managed identity or provide static/no-op
    credentials depending on deployment context.
    """

    @abstractmethod
    async def get_token(self, scope: str) -> AccessToken:
        """
        Obtain an access token for the given resource scope.

        Args:
            scope: The target resource scope
                   (e.g. ``"https://storage.azure.com/.default"``).

        Returns:
            An AccessToken with the token string and expiration.

        Raises:
            CredentialError: If token acquisition fails.
        """
        ...

    async def close(self) -> None:
        """Release any held resources."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


class CredentialError(Exception):
    """Raised when credential acquisition fails."""

    def __init__(self, message: str, scope: str, original_error: Optional[Exception] = None):
        self.scope = scope
        self.original_error = original_error
        super().__init__(message)


@dataclass
class AuthConfig:
    """
    Composite auth configuration injected into CodeExecutionServer.

    Bundles the three auth interfaces so servers can be constructed with
    a single ``auth_config`` parameter instead of scattered provider args.
    """

    token_validator: TokenValidator
    identity_extractor: IdentityExtractor
    credential_provider_factory: Optional[Callable[[str], CredentialProvider]] = None
    """
    Optional callable: ``(user_token: str) -> CredentialProvider``.

    Called per-session to create a credential provider scoped to the
    authenticated user's token.  If ``None``, downstream resource access
    is not available (suitable for servers that don't call external APIs).
    """

    # Additional metadata for WWW-Authenticate headers, OAuth discovery, etc.
    www_authenticate_value: str = ""
    """Value for the WWW-Authenticate response header on 401."""

    require_authorization_header: bool = True
    """Whether protected endpoints require an Authorization header."""

    protected_resource_metadata: Optional[dict] = None
    """
    RFC 9728 document served verbatim at ``/.well-known/oauth-protected-resource``.

    Lets any identity provider describe itself without the server needing
    provider-specific knowledge. ``create_entra_auth_config()`` populates this with
    an Entra-shaped document; custom validators should set it explicitly.

    When set, it must be a non-empty mapping containing at least ``resource``, the
    only field RFC 9728 marks REQUIRED. This is validated on construction, so the
    endpoint can serve any non-``None`` value as-is rather than silently returning
    a document that discovery clients cannot use.

    When ``None``, servers fall back to composing an Entra document from their
    ``entra_client_id`` / ``entra_tenant_id`` attributes.
    """

    def __post_init__(self) -> None:
        metadata = self.protected_resource_metadata
        if metadata is None:
            return
        if not isinstance(metadata, dict) or not metadata:
            raise ValueError(
                "AuthConfig.protected_resource_metadata must be a non-empty dict when set; "
                f"got {metadata!r}. Leave it as None to fall back to Entra ID resolution."
            )
        if "resource" not in metadata:
            raise ValueError(
                "AuthConfig.protected_resource_metadata is missing the 'resource' field, which "
                "RFC 9728 requires. Serving it would break clients relying on OAuth discovery."
            )
