"""Tests for authentication utilities (new API)."""

import pytest
from unittest.mock import patch, MagicMock

from auth import get_search_credential, get_token_provider


class TestGetSearchCredentialEntraPath:
    """Test the Entra fallback path of get_search_credential (no API key set)."""

    @pytest.mark.unit
    @patch.dict("os.environ", {}, clear=True)
    @patch("auth.providers.ChainedTokenCredential")
    @patch("auth.providers.AzureCliCredential")
    @patch("auth.providers.ManagedIdentityCredential")
    def test_returns_chained_credential(self, mock_managed, mock_cli, mock_chained):
        """Test that function returns a ChainedTokenCredential when no API key."""
        mock_credential = MagicMock()
        mock_chained.return_value = mock_credential

        result = get_search_credential()

        assert result == mock_credential
        mock_chained.assert_called_once()

    @pytest.mark.unit
    @patch.dict("os.environ", {}, clear=True)
    @patch("auth.providers.ChainedTokenCredential")
    @patch("auth.providers.AzureCliCredential")
    @patch("auth.providers.ManagedIdentityCredential")
    def test_credential_chain_order(self, mock_managed, mock_cli, mock_chained):
        """Test that credentials are tried in correct order (CLI then ManagedIdentity)."""
        mock_cli_instance = MagicMock()
        mock_managed_instance = MagicMock()
        mock_cli.return_value = mock_cli_instance
        mock_managed.return_value = mock_managed_instance

        get_search_credential()

        call_args = mock_chained.call_args[0]
        assert len(call_args) == 2
        assert call_args[0] == mock_cli_instance
        assert call_args[1] == mock_managed_instance

    @pytest.mark.unit
    @patch.dict("os.environ", {"DEFAULT_IDENTITY_CLIENT_ID": "test-client-id-12345"}, clear=True)
    @patch("auth.providers.ChainedTokenCredential")
    @patch("auth.providers.AzureCliCredential")
    @patch("auth.providers.ManagedIdentityCredential")
    def test_handles_default_identity_client_id_present(self, mock_managed, mock_cli, mock_chained):
        """Test that DEFAULT_IDENTITY_CLIENT_ID is passed to ManagedIdentityCredential when present."""
        get_search_credential()
        mock_managed.assert_called_once_with(client_id="test-client-id-12345")

    @pytest.mark.unit
    @patch.dict("os.environ", {}, clear=True)
    @patch("auth.providers.ChainedTokenCredential")
    @patch("auth.providers.AzureCliCredential")
    @patch("auth.providers.ManagedIdentityCredential")
    def test_handles_default_identity_client_id_absent(self, mock_managed, mock_cli, mock_chained):
        """Test that ManagedIdentityCredential is created with None when DEFAULT_IDENTITY_CLIENT_ID is not set."""
        get_search_credential()
        mock_managed.assert_called_once_with(client_id=None)

    @pytest.mark.unit
    @patch.dict("os.environ", {}, clear=True)
    @patch("auth.providers.ChainedTokenCredential")
    @patch("auth.providers.AzureCliCredential")
    @patch("auth.providers.ManagedIdentityCredential")
    def test_creates_both_credential_types(self, mock_managed, mock_cli, mock_chained):
        """Test that both AzureCliCredential and ManagedIdentityCredential are instantiated."""
        get_search_credential()
        mock_cli.assert_called_once()
        mock_managed.assert_called_once()


class TestGetTokenProvider:
    """Test cases for get_token_provider."""

    @pytest.mark.unit
    @patch("auth.providers.get_bearer_token_provider")
    @patch("auth.providers.ChainedTokenCredential")
    @patch("auth.providers.AzureCliCredential")
    @patch("auth.providers.ManagedIdentityCredential")
    def test_returns_get_token_callback(self, mock_managed, mock_cli, mock_chained, mock_get_bearer):
        """Test that function returns a get_token callback."""
        mock_credential = MagicMock()
        mock_chained.return_value = mock_credential
        mock_provider = MagicMock()
        mock_get_bearer.return_value = mock_provider

        scope = "https://test.com/.default"
        result = get_token_provider(scope)

        assert result == mock_provider
        mock_get_bearer.assert_called_once_with(mock_credential, scope)

    @pytest.mark.unit
    @patch("auth.providers.get_bearer_token_provider")
    @patch("auth.providers.ChainedTokenCredential")
    @patch("auth.providers.AzureCliCredential")
    @patch("auth.providers.ManagedIdentityCredential")
    def test_works_with_different_scopes(self, mock_managed, mock_cli, mock_chained, mock_get_bearer):
        """Test that different scopes are handled correctly."""
        mock_credential = MagicMock()
        mock_chained.return_value = mock_credential
        mock_get_bearer.return_value = MagicMock()

        custom_scope = "api://custom-api/.default"
        get_token_provider(custom_scope)

        mock_get_bearer.assert_called_once_with(mock_credential, custom_scope)

    @pytest.mark.unit
    @patch("auth.providers.get_bearer_token_provider")
    @patch("auth.providers.ChainedTokenCredential")
    @patch("auth.providers.AzureCliCredential")
    @patch("auth.providers.ManagedIdentityCredential")
    def test_credential_chain_order(self, mock_managed, mock_cli, mock_chained, mock_get_bearer):
        """Test that credentials are tried in correct order (CLI then ManagedIdentity)."""
        mock_cli_instance = MagicMock()
        mock_managed_instance = MagicMock()
        mock_cli.return_value = mock_cli_instance
        mock_managed.return_value = mock_managed_instance
        mock_get_bearer.return_value = MagicMock()

        get_token_provider("https://test.com/.default")

        call_args = mock_chained.call_args[0]
        assert call_args[0] == mock_cli_instance
        assert call_args[1] == mock_managed_instance
