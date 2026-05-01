"""
No-op / development authentication implementations.

Provides auth implementations that bypass real token validation, suitable
for local development, testing, and environments without an identity
provider configured.
"""

import logging
import time
from typing import Optional
from urllib.parse import urlparse

import jwt

from .base import (
    AccessToken,
    AuthConfig,
    CredentialProvider,
    IdentityExtractor,
    TokenValidator,
)

LOGGER = logging.getLogger(__name__)


class NoOpTokenValidator(TokenValidator):
    """
    Accepts any bearer token without validation.

    **WARNING**: This should never be used in production. It exists solely
    for local development and integration testing where no identity provider
    is available.

    If a token is provided it is decoded as a JWT (without signature
    verification) to extract claims. If no token or an opaque string is
    provided, a synthetic claims dict is returned with a default identity.
    """

    def __init__(self, default_user_id: str = "dev-user", default_tenant_id: str = "dev-tenant"):
        self._default_user_id = default_user_id
        self._default_tenant_id = default_tenant_id

    async def validate(self, token: str, *, request_path: str = "/mcp", request_method: str = "POST") -> dict:
        LOGGER.warning("NoOpTokenValidator: accepting token without validation (development mode)")

        # Attempt to decode JWT payload for claims (no signature check)
        claims = self._try_decode_jwt(token)
        if claims:
            return claims

        # Fall back to synthetic claims
        return {
            "oid": self._default_user_id,
            "tid": self._default_tenant_id,
            "sub": self._default_user_id,
            "name": "Development User",
            "preferred_username": "dev@localhost",
        }

    @staticmethod
    def _try_decode_jwt(token: str) -> Optional[dict]:
        """Best-effort JWT payload decode without verification."""
        try:
            return jwt.decode(token, options={"verify_signature": False}, algorithms=["RS256", "HS256"])
        except Exception:
            return None


class NoOpIdentityExtractor(IdentityExtractor):
    """
    Extracts identity using standard OIDC ``sub`` claim with optional
    tenant/issuer scoping.

    Falls back to a configurable default if claims are missing.
    """

    def __init__(self, default_identity: str = "dev-user@dev-tenant"):
        self._default = default_identity

    def extract(self, claims: dict) -> Optional[str]:
        # Try oid@tid (Azure-style)
        user_id = claims.get("oid") or claims.get("sub")
        tenant_id = claims.get("tid") or self._tenant_from_iss(claims.get("iss", ""))
        if user_id:
            return f"{user_id}@{tenant_id}"
        return self._default

    @staticmethod
    def _tenant_from_iss(iss: str) -> str:
        """Extract tenant ID from an OIDC issuer URL.

        Handles the two common Azure issuer formats:
        - ``https://login.microsoftonline.com/<tenant>/v2.0``  → ``<tenant>``
        - ``https://sts.windows.net/<tenant>/``                → ``<tenant>``

        Falls back to ``"default"`` when the issuer cannot be parsed or has
        no recognizable tenant segment.
        """
        try:
            path = urlparse(iss).path  # e.g. "/<tenant>/v2.0" or "/<tenant>/"
            segments = [s for s in path.split("/") if s]  # drop empty parts
            if not segments:
                return "default"
            # Skip a trailing Azure version token with the exact format v<digits>.<digits>
            # (e.g. "v2.0", "v1.0") to avoid misidentifying it as a tenant segment.
            last = segments[-1]
            if last.startswith("v") and last.count(".") == 1:
                major, minor = last[1:].split(".")
                if major.isdigit() and minor.isdigit():
                    segments = segments[:-1]
            return segments[-1] if segments else "default"
        except Exception:
            return "default"


class NoOpCredentialProvider(CredentialProvider):
    """
    Returns a static/dummy token for downstream calls.

    Useful when the server does not need to call authenticated external
    services, or when running against local emulators (Azurite, etc.).
    """

    def __init__(self, static_token: str = "no-op-token"):
        self._token = static_token

    async def get_token(self, scope: str) -> AccessToken:
        LOGGER.debug(f"NoOpCredentialProvider: returning static token for scope {scope}")
        return AccessToken(self._token, int(time.time()) + 3600)


def create_noop_auth_config(
    default_user_id: str = "dev-user",
    default_tenant_id: str = "dev-tenant",
) -> AuthConfig:
    """
    Create an AuthConfig that bypasses real authentication.

    Suitable for:
    - Local development without Azure/identity provider
    - Integration tests
    - Environments behind a trusted reverse proxy that handles auth

    Args:
        default_user_id: Default user identity when token has no claims.
        default_tenant_id: Default tenant when token has no tenant claim.

    Returns:
        An AuthConfig with no-op implementations.
    """
    LOGGER.warning(
        "Creating no-op auth configuration. "
        "This MUST NOT be used in production — tokens are not validated."
    )

    token_validator = NoOpTokenValidator(default_user_id=default_user_id, default_tenant_id=default_tenant_id)
    identity_extractor = NoOpIdentityExtractor(default_identity=f"{default_user_id}@{default_tenant_id}")

    def credential_factory(user_token: str) -> CredentialProvider:
        return NoOpCredentialProvider()

    return AuthConfig(
        token_validator=token_validator,
        identity_extractor=identity_extractor,
        credential_provider_factory=credential_factory,
        www_authenticate_value='Bearer realm="development"',
    )
