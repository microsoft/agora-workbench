"""App-layer authentication for Activity UI endpoints excluded from EasyAuth.

POST /events is excluded from EasyAuth to allow managed-identity bearer tokens.
This module validates those tokens: signature (via JWKS), issuer, audience,
expiry, and the `roles` claim containing `ActivityEventWriter`.

When ACTIVITY_UI_AUTH_DISABLED=true, all checks are skipped (local dev only).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient

LOGGER = logging.getLogger(__name__)

# Env-based configuration
_AUTH_DISABLED = os.getenv("ACTIVITY_UI_AUTH_DISABLED", "").lower() == "true"
_TENANT_ID = os.getenv("ENTRA_TENANT_ID", "")
_AUDIENCE = os.getenv("ACTIVITY_UI_AUDIENCE", "")

# Also accept the bare client ID as audience (Azure AD v2.0 tokens use it)
_CLIENT_ID = os.getenv("ACTIVITY_UI_CLIENT_ID", "")

_REQUIRED_ROLE = "ActivityEventWriter"

# Lazily initialized JWKS client
_jwks_client: Optional[PyJWKClient] = None


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        jwks_uri = f"https://login.microsoftonline.com/{_TENANT_ID}/discovery/v2.0/keys"
        _jwks_client = PyJWKClient(jwks_uri, cache_keys=True)
    return _jwks_client


def _get_valid_audiences() -> list[str]:
    """Build list of acceptable audience values."""
    audiences = []
    if _AUDIENCE:
        audiences.append(_AUDIENCE)
    if _CLIENT_ID:
        audiences.append(_CLIENT_ID)
        audiences.append(f"api://{_CLIENT_ID}")
    return audiences


async def require_event_writer(request: Request) -> None:
    """FastAPI dependency that validates bearer tokens on /events.

    Raises HTTPException(401/403) on failure. No-ops when auth is disabled.
    """
    if _AUTH_DISABLED:
        return

    if not _TENANT_ID or not (_AUDIENCE or _CLIENT_ID):
        LOGGER.warning(
            "Activity UI auth is not configured (missing ENTRA_TENANT_ID / "
            "ACTIVITY_UI_AUDIENCE / ACTIVITY_UI_CLIENT_ID). Rejecting request."
        )
        raise HTTPException(status_code=503, detail="Auth not configured")

    # Extract bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = auth_header[7:]

    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        audiences = _get_valid_audiences()

        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audiences,
            issuer=f"https://login.microsoftonline.com/{_TENANT_ID}/v2.0",
            options={"verify_exp": True, "verify_nbf": True},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidAudienceError:
        raise HTTPException(status_code=401, detail="Invalid audience")
    except jwt.InvalidIssuerError:
        raise HTTPException(status_code=401, detail="Invalid issuer")
    except jwt.PyJWTError as exc:
        LOGGER.debug("JWT validation failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid token")

    # Check roles claim
    roles = payload.get("roles", [])
    if _REQUIRED_ROLE not in roles:
        LOGGER.debug("Token missing required role %r, has: %s", _REQUIRED_ROLE, roles)
        raise HTTPException(status_code=403, detail=f"Missing required role: {_REQUIRED_ROLE}")
