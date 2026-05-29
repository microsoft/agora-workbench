"""
Outbound credential factories for data access backends (Blob Storage, etc.).

Provides ``AsyncTokenCredential`` implementations that server components
(publishers, fetchers) use to authenticate against Azure resources.

Local development:
    ``MsalCacheCredential`` reads the Azure CLI's MSAL token cache
    (``~/.azure/msal_token_cache.json``) directly — no ``az`` binary
    required inside the container.  Mount the host cache file read-only
    and the credential acquires tokens silently via the cached refresh token.

Production (Azure Container Apps / AKS / VMs):
    ``ManagedIdentityCredential`` is used.  The MSAL cache does not exist
    in production images, so ``MsalCacheCredential`` falls through
    automatically when wrapped in a ``ChainedTokenCredential``.

Usage::

    from code_execution.data_access.credentials import create_storage_credential

    credential = create_storage_credential()
    publisher = BlobPublisher(account_url=..., container=..., credential=credential)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from azure.core.credentials import AccessToken
from azure.identity import CredentialUnavailableError

LOGGER = logging.getLogger(__name__)

# The Azure CLI's registered public-client application ID.
_AZURE_CLI_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"

# Default MSAL token cache location (matches az CLI on Linux/macOS).
_DEFAULT_CACHE_PATH = Path.home() / ".azure" / "msal_token_cache.json"


class MsalCacheCredential:
    """Async credential that reads Azure CLI's MSAL token cache directly.

    Acquires tokens silently using the refresh token stored by ``az login``.
    Does not require the ``az`` binary — only the ``msal`` Python library
    and a readable ``msal_token_cache.json``.

    Implements the ``AsyncTokenCredential`` protocol from
    ``azure.core.credentials_async`` so it can be used with
    ``BlobPublisher``, ``BlobFetcher``, ``BlobServiceClient``, etc.

    The cache file is re-read on every ``get_token()`` call so the
    credential picks up tokens refreshed by the host's ``az`` CLI
    without requiring a server restart.

    Args:
        cache_path: Path to the MSAL token cache file.  Defaults to
            ``~/.azure/msal_token_cache.json``.
        username: Optional username filter.  When multiple accounts
            exist in the cache, only the matching account is used.
            If ``None``, the first account is selected.
        authority: Azure AD authority URL.  Defaults to the
            ``organizations`` endpoint (multi-tenant).
    """

    def __init__(
        self,
        cache_path: str | Path | None = None,
        username: str | None = None,
        authority: str = "https://login.microsoftonline.com/organizations",
    ):
        self._cache_path = Path(cache_path) if cache_path else _DEFAULT_CACHE_PATH
        self._username = username
        self._authority = authority

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        """Acquire a token for the requested scopes from the MSAL cache.

        Args:
            *scopes: One or more Azure resource scopes
                (e.g. ``"https://storage.azure.com/.default"``).

        Returns:
            An ``AccessToken`` with the token string and expiry.

        Raises:
            CredentialUnavailableError: If the cache file is missing,
                no accounts are found, or silent acquisition fails.
                This allows ``ChainedTokenCredential`` to fall through
                to the next credential in the chain.
        """
        import msal

        if not self._cache_path.is_file():
            raise CredentialUnavailableError(message=f"MSAL token cache not found at {self._cache_path}")

        cache = msal.SerializableTokenCache()
        cache.deserialize(self._cache_path.read_text())

        app = msal.PublicClientApplication(
            _AZURE_CLI_CLIENT_ID,
            authority=self._authority,
            token_cache=cache,
        )

        accounts = app.get_accounts(username=self._username)
        if not accounts:
            raise CredentialUnavailableError(message="No accounts found in MSAL token cache")

        account = accounts[0]
        scope_list = list(scopes)

        # Try cached access token first, then force a refresh via RT.
        result = app.acquire_token_silent(scope_list, account=account)
        if not result or "access_token" not in result:
            result = app.acquire_token_silent(scope_list, account=account, force_refresh=True)

        if not result or "access_token" not in result:
            error_desc = (
                result.get("error_description", "unknown error") if result else "acquire_token_silent returned None"
            )
            raise CredentialUnavailableError(message=f"MSAL silent token acquisition failed: {error_desc}")

        LOGGER.debug(
            "MsalCacheCredential: acquired token for %s (account=%s)",
            scopes[0][:50],
            account.get("username", "?"),
        )
        return AccessToken(result["access_token"], result["expires_in"])

    async def close(self) -> None:
        """No-op — no persistent connections to release."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


def create_storage_credential():
    """Create an async credential chain suitable for Azure Storage access.

    Returns a ``ChainedTokenCredential`` that tries:
      1. **MsalCacheCredential** — reads mounted ``az login`` cache (local dev)
      2. **ManagedIdentityCredential** — uses Azure-assigned identity (production)

    The chain raises ``CredentialUnavailableError`` from each link until one
    succeeds, making the credential work transparently in both environments.

    Returns:
        An ``AsyncTokenCredential`` usable with ``BlobPublisher``,
        ``BlobFetcher``, ``BlobServiceClient``, etc.
    """
    from azure.identity.aio import (
        ChainedTokenCredential,
        ManagedIdentityCredential,
    )

    managed_identity_client_id = os.getenv("DEFAULT_IDENTITY_CLIENT_ID")

    credentials = [MsalCacheCredential()]

    if managed_identity_client_id:
        credentials.append(ManagedIdentityCredential(client_id=managed_identity_client_id))
    else:
        credentials.append(ManagedIdentityCredential())

    return ChainedTokenCredential(*credentials)
