"""
Azure Entra ID authentication implementations.

Provides concrete implementations of the auth interfaces (TokenValidator,
IdentityExtractor, CredentialProvider) for Microsoft Entra ID (Azure AD).

Uses PyJWT for JWT validation with JWKS key fetching from the Microsoft
identity platform's OpenID configuration endpoint.
"""

import asyncio
import logging
import os
from types import TracebackType
from typing import Optional

import jwt
from azure.core.credentials import AccessToken as AzureAccessToken
from jwt import PyJWKClient

from .base import (
    AccessToken,
    AuthConfig,
    CredentialError,
    CredentialProvider,
    IdentityExtractor,
    TokenValidationError,
    TokenValidator,
)

LOGGER = logging.getLogger(__name__)

# Microsoft identity platform OIDC configuration
_ENTRA_JWKS_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
_ENTRA_ISSUER_TEMPLATES = [
    "https://login.microsoftonline.com/{tenant_id}/v2.0",
    "https://sts.windows.net/{tenant_id}/",
]


class EntraTokenValidator(TokenValidator):
    """
    Validates JWTs issued by Microsoft Entra ID using PyJWT.

    Fetches signing keys from the Microsoft identity platform JWKS endpoint,
    verifies signature (RS256), and validates standard claims (exp, aud, iss).

    Note: This implementation is single-tenant only. The JWKS URL and issuer
    validation use the specific tenant ID. For multi-tenant support, use the
    ``/common/`` JWKS endpoint and validate the ``tid`` claim manually.
    """

    def __init__(self, client_id: str, tenant_id: str):
        """
        Args:
            client_id: The Entra ID application (client) ID — used as audience.
            tenant_id: The Entra ID tenant ID.
        """
        self._client_id = client_id
        self._tenant_id = tenant_id
        jwks_url = _ENTRA_JWKS_URL_TEMPLATE.format(tenant_id=tenant_id)
        self._jwks_client = PyJWKClient(jwks_url, cache_keys=True)
        self._valid_audiences = [client_id, f"api://{client_id}"]
        self._valid_issuers = [t.format(tenant_id=tenant_id) for t in _ENTRA_ISSUER_TEMPLATES]

    async def validate(self, token: str, *, request_path: str = "/mcp", request_method: str = "POST") -> dict:
        try:
            signing_key = await asyncio.to_thread(self._jwks_client.get_signing_key_from_jwt, token)
            decoded = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._valid_audiences,
                issuer=self._valid_issuers,
                options={
                    "verify_exp": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
            return decoded
        except jwt.ExpiredSignatureError:
            raise TokenValidationError("Token is expired", status_code=401)
        except jwt.InvalidAudienceError:
            raise TokenValidationError("Token has invalid audience", status_code=401)
        except jwt.InvalidIssuerError:
            raise TokenValidationError("Token has invalid issuer", status_code=401)
        except jwt.PyJWKClientError as e:
            LOGGER.error(f"JWKS key fetch failed: {e}")
            raise TokenValidationError(f"Failed to fetch signing keys: {e}", status_code=401)
        except jwt.InvalidTokenError as e:
            LOGGER.warning(f"Token validation failed: {e}")
            raise TokenValidationError(str(e), status_code=401)


class EntraIdentityExtractor(IdentityExtractor):
    """
    Extracts user identity from Entra ID JWT claims as ``{oid}@{tid}``.

    This composite format ensures session isolation across both users and
    tenants. Falls back to ``sub`` if ``oid`` is absent.
    """

    def extract(self, claims: dict) -> Optional[str]:
        user_id = claims.get("oid") or claims.get("sub")
        tenant_id = claims.get("tid")
        if not user_id or not tenant_id:
            return None
        return f"{user_id}@{tenant_id}"


class EntraCredentialProvider(CredentialProvider):
    """
    Provides Azure credentials for downstream resource access via managed identity.

    Uses Azure ManagedIdentityCredential (user-assigned if AZURE_CLIENT_ID is
    set, otherwise system-assigned) to obtain tokens for downstream resources
    such as Azure Storage and Azure AI Search.
    """

    def __init__(self, client_id: Optional[str] = None):
        """
        Args:
            client_id: User-assigned managed identity client ID.
                      Falls back to AZURE_CLIENT_ID env var.
                      If None/unset, system-assigned managed identity is used.
        """
        from azure.identity.aio import ManagedIdentityCredential

        resolved_client_id = client_id or (os.getenv("AZURE_CLIENT_ID") or "").strip() or None
        self._credential = ManagedIdentityCredential(client_id=resolved_client_id)
        if resolved_client_id:
            LOGGER.info(f"EntraCredentialProvider: using user-assigned MI (client_id={resolved_client_id[:8]}...)")
        else:
            LOGGER.info("EntraCredentialProvider: using system-assigned managed identity")

    async def get_token(self, scope: str) -> AccessToken:
        try:
            token = await self._credential.get_token(scope)
            return AccessToken(token.token, token.expires_on)
        except Exception as e:
            raise CredentialError(
                f"Failed to acquire token for scope '{scope}': {e}",
                scope=scope,
                original_error=e,
            ) from e

    async def close(self) -> None:
        await self._credential.close()


class CredentialProviderTokenCredential:
    """Adapter exposing auth.CredentialProvider via Azure token-credential interface."""

    def __init__(self, credential_provider: CredentialProvider):
        self._credential_provider = credential_provider

    async def get_token(self, *scopes: str, **kwargs: object) -> AzureAccessToken:
        del kwargs
        if not scopes:
            raise ValueError("At least one scope is required")

        token = await self._credential_provider.get_token(scopes[0])
        return AzureAccessToken(token=token.token, expires_on=token.expires_on)

    async def close(self) -> None:
        await self._credential_provider.close()

    async def __aenter__(self):
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ):
        del exc_type, exc_value, traceback
        await self.close()


def create_entra_auth_config(
    client_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> AuthConfig:
    """
    Create an AuthConfig using Azure Entra ID for all three auth concerns.

    This is the default configuration for production and local-dev-with-Azure
    deployments. Reads from environment variables if arguments are not provided.

    Args:
        client_id: Entra ID app client ID. Falls back to ``ENTRA_CLIENT_ID``.
        tenant_id: Entra ID tenant ID. Falls back to ``ENTRA_TENANT_ID``.

    Returns:
        A fully configured AuthConfig for Entra ID.

    Raises:
        ValueError: If required configuration is missing.
    """
    resolved_client_id = client_id or os.getenv("ENTRA_CLIENT_ID")
    resolved_tenant_id = tenant_id or os.getenv("ENTRA_TENANT_ID")

    missing = []
    if not resolved_client_id:
        missing.append("ENTRA_CLIENT_ID")
    if not resolved_tenant_id:
        missing.append("ENTRA_TENANT_ID")
    if missing:
        raise ValueError(
            f"Missing required Entra ID configuration: {', '.join(missing)}. "
            "Set via environment variables or pass directly."
        )

    assert resolved_client_id is not None
    assert resolved_tenant_id is not None

    token_validator = EntraTokenValidator(client_id=resolved_client_id, tenant_id=resolved_tenant_id)
    identity_extractor = EntraIdentityExtractor()

    def credential_factory(_user_token: str) -> CredentialProvider:
        return EntraCredentialProvider()

    www_auth = 'Bearer resource_metadata="/.well-known/oauth-protected-resource"'

    return AuthConfig(
        token_validator=token_validator,
        identity_extractor=identity_extractor,
        credential_provider_factory=credential_factory,
        www_authenticate_value=www_auth,
    )
