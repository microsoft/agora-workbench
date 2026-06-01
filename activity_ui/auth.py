"""App-layer authentication for Activity UI endpoints excluded from EasyAuth.

Follows the same architecture as ``code_execution.auth``:

- Abstract ``TokenValidator`` base class with async ``validate()`` method.
- ``EntraTokenValidator`` for production (PyJWT + JWKS, RS256, issuer/audience).
- ``NoOpTokenValidator`` for local development (accepts any token).

POST /events is excluded from EasyAuth to allow managed-identity bearer tokens.
The validator checks signature, issuer, audience, expiry, and the ``roles``
claim containing ``ActivityEventWriter``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient

LOGGER = logging.getLogger(__name__)

# Microsoft identity platform OIDC configuration
_ENTRA_JWKS_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
_ENTRA_ISSUER_TEMPLATES = [
    "https://login.microsoftonline.com/{tenant_id}/v2.0",
    "https://sts.windows.net/{tenant_id}/",
]

_REQUIRED_ROLE = "ActivityEventWriter"


# ── Abstract base ────────────────────────────────────────────────────────────


class TokenValidationError(Exception):
    """Raised when token validation fails."""

    def __init__(self, message: str, status_code: int = 401):
        self.status_code = status_code
        super().__init__(message)


class TokenValidator(ABC):
    """Validates bearer tokens from incoming requests."""

    @abstractmethod
    async def validate(self, token: str) -> dict:
        """Validate a bearer token and return decoded claims.

        Raises:
            TokenValidationError: If the token is invalid.
        """
        ...


# ── Entra ID implementation ──────────────────────────────────────────────────


class EntraTokenValidator(TokenValidator):
    """Validates JWTs issued by Microsoft Entra ID using PyJWT.

    Fetches signing keys from the JWKS endpoint, verifies signature (RS256),
    and validates standard claims (exp, aud, iss) plus the ``roles`` claim.
    """

    def __init__(self, client_id: str, tenant_id: str, audience: str = ""):
        self._client_id = client_id
        self._tenant_id = tenant_id
        jwks_url = _ENTRA_JWKS_URL_TEMPLATE.format(tenant_id=tenant_id)
        self._jwks_client = PyJWKClient(jwks_url, cache_keys=True)
        # Accept multiple audience values (scoped URI, bare client ID, api:// prefix)
        self._valid_audiences: list[str] = []
        if audience:
            self._valid_audiences.append(audience)
        if client_id:
            self._valid_audiences.append(client_id)
            self._valid_audiences.append(f"api://{client_id}")
        self._valid_issuers = [t.format(tenant_id=tenant_id) for t in _ENTRA_ISSUER_TEMPLATES]

    async def validate(self, token: str) -> dict:
        try:
            signing_key = await asyncio.to_thread(self._jwks_client.get_signing_key_from_jwt, token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._valid_audiences,
                issuer=self._valid_issuers,
                options={"verify_exp": True, "verify_aud": True, "verify_iss": True},
            )
        except jwt.ExpiredSignatureError:
            raise TokenValidationError("Token is expired", status_code=401)
        except jwt.InvalidAudienceError:
            raise TokenValidationError("Token has invalid audience", status_code=401)
        except jwt.InvalidIssuerError:
            raise TokenValidationError("Token has invalid issuer", status_code=401)
        except jwt.PyJWKClientError as exc:
            LOGGER.error("JWKS key fetch failed: %s", exc)
            raise TokenValidationError(f"Failed to fetch signing keys: {exc}", status_code=401)
        except jwt.InvalidTokenError as exc:
            LOGGER.warning("Token validation failed: %s", exc)
            raise TokenValidationError(str(exc), status_code=401)

        # Check roles claim
        roles = payload.get("roles", [])
        if _REQUIRED_ROLE not in roles:
            LOGGER.debug("Token missing required role %r, has: %s", _REQUIRED_ROLE, roles)
            raise TokenValidationError(f"Missing required role: {_REQUIRED_ROLE}", status_code=403)

        return payload


# ── No-op implementation (local dev) ─────────────────────────────────────────


class NoOpTokenValidator(TokenValidator):
    """Accepts any bearer token without validation.

    WARNING: Never use in production. Exists for local development only.
    """

    async def validate(self, token: str) -> dict:
        LOGGER.warning("NoOpTokenValidator: accepting token without validation (development mode)")
        # Try to decode payload for claims (no signature check)
        try:
            return jwt.decode(token, options={"verify_signature": False}) if token else {}
        except jwt.PyJWTError:
            return {}


# ── Factory ──────────────────────────────────────────────────────────────────


def create_token_validator() -> TokenValidator:
    """Create the appropriate TokenValidator based on environment configuration.

    Returns NoOpTokenValidator when ACTIVITY_UI_AUTH_DISABLED=true.
    Returns EntraTokenValidator when tenant and audience are configured.
    Raises RuntimeError if auth is enabled but config is incomplete.
    """
    if os.getenv("ACTIVITY_UI_AUTH_DISABLED", "").lower() == "true":
        LOGGER.warning("Activity UI auth DISABLED — using NoOpTokenValidator")
        return NoOpTokenValidator()

    tenant_id = os.getenv("ENTRA_TENANT_ID", "")
    audience = os.getenv("ACTIVITY_UI_AUDIENCE", "")
    client_id = os.getenv("ACTIVITY_UI_CLIENT_ID", "")

    if not tenant_id or not (audience or client_id):
        raise RuntimeError(
            "Activity UI auth is enabled but not configured. "
            "Set ENTRA_TENANT_ID and ACTIVITY_UI_AUDIENCE/ACTIVITY_UI_CLIENT_ID, "
            "or set ACTIVITY_UI_AUTH_DISABLED=true for local development."
        )

    return EntraTokenValidator(client_id=client_id, tenant_id=tenant_id, audience=audience)


# ── FastAPI dependency ───────────────────────────────────────────────────────

# Singleton validator created at import time. In production this fails fast
# if auth config is missing; in local dev it returns NoOpTokenValidator.
_validator: TokenValidator | None = None


def _get_validator() -> TokenValidator:
    global _validator
    if _validator is None:
        _validator = create_token_validator()
    return _validator


async def require_event_writer(request: Request) -> None:
    """FastAPI dependency that validates bearer tokens on /events.

    Raises HTTPException(401/403) on failure.
    """
    validator = _get_validator()

    # Extract bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        if isinstance(validator, NoOpTokenValidator):
            return
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = auth_header[7:]

    try:
        await validator.validate(token)
    except TokenValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
