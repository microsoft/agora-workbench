"""
Unit tests for MsalCacheCredential and create_storage_credential.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ...data_access.credentials import (
    MsalCacheCredential,
    _AZURE_CLI_CLIENT_ID,
    create_storage_credential,
)


# ---------------------------------------------------------------------------
# MsalCacheCredential
# ---------------------------------------------------------------------------


class TestMsalCacheCredentialInit:
    """Tests for MsalCacheCredential construction."""

    def test_default_cache_path(self):
        cred = MsalCacheCredential()
        assert cred._cache_path == Path.home() / ".azure" / "msal_token_cache.json"

    def test_custom_cache_path(self, tmp_path):
        custom = tmp_path / "my_cache.json"
        cred = MsalCacheCredential(cache_path=custom)
        assert cred._cache_path == custom

    def test_username_stored(self):
        cred = MsalCacheCredential(username="user@example.com")
        assert cred._username == "user@example.com"

    def test_custom_authority(self):
        cred = MsalCacheCredential(authority="https://login.microsoftonline.com/mytenant")
        assert cred._authority == "https://login.microsoftonline.com/mytenant"


class TestMsalCacheCredentialGetToken:
    """Tests for MsalCacheCredential.get_token()."""

    @pytest.mark.asyncio
    async def test_raises_if_cache_missing(self, tmp_path):
        """Should raise CredentialUnavailableError if cache file doesn't exist."""
        from azure.identity import CredentialUnavailableError

        cred = MsalCacheCredential(cache_path=tmp_path / "nonexistent.json")
        with pytest.raises(CredentialUnavailableError, match="not found"):
            await cred.get_token("https://storage.azure.com/.default")

    @pytest.mark.asyncio
    async def test_raises_if_no_accounts(self, tmp_path):
        """Should raise CredentialUnavailableError if no accounts in cache."""
        from azure.identity import CredentialUnavailableError

        cache_file = tmp_path / "msal_token_cache.json"
        cache_file.write_text(json.dumps({"Account": {}, "AccessToken": {}, "RefreshToken": {}}))

        mock_app = MagicMock()
        mock_app.get_accounts.return_value = []

        with patch("msal.PublicClientApplication", return_value=mock_app):
            cred = MsalCacheCredential(cache_path=cache_file)
            with pytest.raises(CredentialUnavailableError, match="No accounts"):
                await cred.get_token("https://storage.azure.com/.default")

    @pytest.mark.asyncio
    async def test_acquires_token_from_cache(self, tmp_path):
        """Should return AccessToken when silent acquisition succeeds."""
        cache_file = tmp_path / "msal_token_cache.json"
        cache_file.write_text(json.dumps({"Account": {}, "AccessToken": {}, "RefreshToken": {}}))

        mock_app = MagicMock()
        mock_app.get_accounts.return_value = [{"username": "user@example.com"}]
        mock_app.acquire_token_silent.return_value = {
            "access_token": "fake-token-123",
            "expires_in": 3600,
        }

        with patch("msal.PublicClientApplication", return_value=mock_app) as mock_cls:
            cred = MsalCacheCredential(cache_path=cache_file)
            token = await cred.get_token("https://storage.azure.com/.default")

            assert token.token == "fake-token-123"
            # expires_on should be an absolute timestamp (now + expires_in)
            import time as _time

            assert token.expires_on >= int(_time.time()) + 3599
            mock_cls.assert_called_once_with(
                _AZURE_CLI_CLIENT_ID,
                authority="https://login.microsoftonline.com/organizations",
                token_cache=mock_cls.call_args[1]["token_cache"],
            )
            mock_app.acquire_token_silent.assert_called_once_with(
                ["https://storage.azure.com/.default"],
                account={"username": "user@example.com"},
            )

    @pytest.mark.asyncio
    async def test_force_refresh_on_cache_miss(self, tmp_path):
        """Should retry with force_refresh=True if first attempt returns no token."""
        cache_file = tmp_path / "msal_token_cache.json"
        cache_file.write_text(json.dumps({"Account": {}, "AccessToken": {}, "RefreshToken": {}}))

        mock_app = MagicMock()
        mock_app.get_accounts.return_value = [{"username": "user@example.com"}]
        # First call returns None (cache miss), second returns token
        mock_app.acquire_token_silent.side_effect = [
            None,
            {"access_token": "refreshed-token", "expires_in": 7200},
        ]

        with patch("msal.PublicClientApplication", return_value=mock_app):
            cred = MsalCacheCredential(cache_path=cache_file)
            token = await cred.get_token("https://storage.azure.com/.default")

            assert token.token == "refreshed-token"
            assert mock_app.acquire_token_silent.call_count == 2
            # Second call should have force_refresh=True
            second_call = mock_app.acquire_token_silent.call_args_list[1]
            assert second_call[1]["force_refresh"] is True

    @pytest.mark.asyncio
    async def test_raises_on_total_failure(self, tmp_path):
        """Should raise CredentialUnavailableError if both attempts fail."""
        from azure.identity import CredentialUnavailableError

        cache_file = tmp_path / "msal_token_cache.json"
        cache_file.write_text(json.dumps({"Account": {}, "AccessToken": {}, "RefreshToken": {}}))

        mock_app = MagicMock()
        mock_app.get_accounts.return_value = [{"username": "user@example.com"}]
        mock_app.acquire_token_silent.return_value = {
            "error": "invalid_grant",
            "error_description": "Token expired",
        }

        with patch("msal.PublicClientApplication", return_value=mock_app):
            cred = MsalCacheCredential(cache_path=cache_file)
            with pytest.raises(CredentialUnavailableError, match="Token expired"):
                await cred.get_token("https://storage.azure.com/.default")

    @pytest.mark.asyncio
    async def test_username_filter(self, tmp_path):
        """Should pass username to get_accounts when specified."""
        cache_file = tmp_path / "msal_token_cache.json"
        cache_file.write_text(json.dumps({"Account": {}, "AccessToken": {}, "RefreshToken": {}}))

        mock_app = MagicMock()
        mock_app.get_accounts.return_value = [{"username": "specific@example.com"}]
        mock_app.acquire_token_silent.return_value = {
            "access_token": "token",
            "expires_in": 3600,
        }

        with patch("msal.PublicClientApplication", return_value=mock_app):
            cred = MsalCacheCredential(cache_path=cache_file, username="specific@example.com")
            await cred.get_token("https://storage.azure.com/.default")

            mock_app.get_accounts.assert_called_once_with(username="specific@example.com")


class TestMsalCacheCredentialLifecycle:
    """Tests for close and context manager."""

    @pytest.mark.asyncio
    async def test_close_is_noop(self):
        cred = MsalCacheCredential()
        await cred.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with MsalCacheCredential() as cred:
            assert cred is not None


# ---------------------------------------------------------------------------
# create_storage_credential
# ---------------------------------------------------------------------------


class TestCreateStorageCredential:
    """Tests for the create_storage_credential factory."""

    def test_returns_chained_credential(self):
        from azure.identity.aio import ChainedTokenCredential

        cred = create_storage_credential()
        assert isinstance(cred, ChainedTokenCredential)

    @patch.dict("os.environ", {"DEFAULT_IDENTITY_CLIENT_ID": "test-client-id"})
    def test_uses_client_id_for_managed_identity(self):
        """Should pass DEFAULT_IDENTITY_CLIENT_ID to ManagedIdentityCredential."""
        with patch("azure.identity.aio.ManagedIdentityCredential") as mock_mi:
            create_storage_credential()
            mock_mi.assert_called_once_with(client_id="test-client-id")

    @patch.dict("os.environ", {}, clear=True)
    def test_no_client_id_uses_system_assigned(self):
        """Should use system-assigned MI when no client ID is set."""
        with patch("azure.identity.aio.ManagedIdentityCredential") as mock_mi:
            # Remove DEFAULT_IDENTITY_CLIENT_ID if present
            import os

            os.environ.pop("DEFAULT_IDENTITY_CLIENT_ID", None)
            create_storage_credential()
            mock_mi.assert_called_once_with()

    def test_explicit_client_id_used_for_managed_identity(self):
        """Explicit client_id should be passed to ManagedIdentityCredential."""
        with patch("azure.identity.aio.ManagedIdentityCredential") as mock_mi:
            create_storage_credential(client_id="explicit-client-id")
            mock_mi.assert_called_once_with(client_id="explicit-client-id")

    @patch.dict("os.environ", {"DEFAULT_IDENTITY_CLIENT_ID": "env-client-id"})
    def test_explicit_client_id_overrides_env(self):
        """Explicit client_id takes precedence over DEFAULT_IDENTITY_CLIENT_ID.

        This is what keeps production bound to the same user-assigned identity:
        the data manager resolves the id from AZURE_CLIENT_ID and passes it in.
        """
        with patch("azure.identity.aio.ManagedIdentityCredential") as mock_mi:
            create_storage_credential(client_id="explicit-client-id")
            mock_mi.assert_called_once_with(client_id="explicit-client-id")
