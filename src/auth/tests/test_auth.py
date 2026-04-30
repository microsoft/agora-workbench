"""Tests for authentication utilities."""

import pytest
from unittest.mock import patch, MagicMock

from auth import create_entra_token_provider, create_azure_credential


class TestCreateAzureCredential:
    """Test cases for create_azure_credential."""

    @pytest.mark.unit
    @patch("auth.auth.ChainedTokenCredential")
    @patch("auth.auth.AzureCliCredential")
    @patch("auth.auth.ManagedIdentityCredential")
    def test_returns_chained_credential(self, mock_managed, mock_cli, mock_chained):
        """Test that function returns a ChainedTokenCredential."""
        mock_credential = MagicMock()
        mock_chained.return_value = mock_credential

        result = create_azure_credential()

        # Result should be the ChainedTokenCredential instance
        assert result == mock_credential
        mock_chained.assert_called_once()

    @pytest.mark.unit
    @patch("auth.auth.ChainedTokenCredential")
    @patch("auth.auth.AzureCliCredential")
    @patch("auth.auth.ManagedIdentityCredential")
    def test_credential_chain_order(self, mock_managed, mock_cli, mock_chained):
        """Test that credentials are tried in correct order (CLI then ManagedIdentity)."""
        mock_cli_instance = MagicMock()
        mock_managed_instance = MagicMock()
        mock_cli.return_value = mock_cli_instance
        mock_managed.return_value = mock_managed_instance

        create_azure_credential()

        # Verify order: first arg should be CLI, second should be ManagedIdentity
        call_args = mock_chained.call_args[0]
        assert len(call_args) == 2
        assert call_args[0] == mock_cli_instance
        assert call_args[1] == mock_managed_instance

    @pytest.mark.unit
    @patch("auth.auth.os.getenv")
    @patch("auth.auth.ChainedTokenCredential")
    @patch("auth.auth.AzureCliCredential")
    @patch("auth.auth.ManagedIdentityCredential")
    def test_handles_default_identity_client_id_present(self, mock_managed, mock_cli, mock_chained, mock_getenv):
        """Test that DEFAULT_IDENTITY_CLIENT_ID is passed to ManagedIdentityCredential when present."""
        test_client_id = "test-client-id-12345"
        mock_getenv.return_value = test_client_id

        create_azure_credential()

        # Verify ManagedIdentityCredential was created with client_id
        mock_managed.assert_called_once_with(client_id=test_client_id)

    @pytest.mark.unit
    @patch("auth.auth.os.getenv")
    @patch("auth.auth.ChainedTokenCredential")
    @patch("auth.auth.AzureCliCredential")
    @patch("auth.auth.ManagedIdentityCredential")
    def test_handles_default_identity_client_id_absent(self, mock_managed, mock_cli, mock_chained, mock_getenv):
        """Test that ManagedIdentityCredential is created with None when DEFAULT_IDENTITY_CLIENT_ID is not set."""
        mock_getenv.return_value = None

        create_azure_credential()

        # Verify ManagedIdentityCredential was created with client_id=None
        mock_managed.assert_called_once_with(client_id=None)

    @pytest.mark.unit
    @patch("auth.auth.ChainedTokenCredential")
    @patch("auth.auth.AzureCliCredential")
    @patch("auth.auth.ManagedIdentityCredential")
    def test_creates_both_credential_types(self, mock_managed, mock_cli, mock_chained):
        """Test that both AzureCliCredential and ManagedIdentityCredential are instantiated."""
        create_azure_credential()

        # Verify both credential types were created
        mock_cli.assert_called_once()
        mock_managed.assert_called_once()


class TestCreateEntraTokenProvider:
    """Test cases for create_entra_token_provider."""

    @pytest.mark.unit
    @patch("auth.auth.get_bearer_token_provider")
    @patch("auth.auth.ChainedTokenCredential")
    @patch("auth.auth.AzureCliCredential")
    @patch("auth.auth.ManagedIdentityCredential")
    def test_returns_get_token_callback(self, mock_managed, mock_cli, mock_chained, mock_get_bearer):
        """Test that function returns a get_token callback."""
        mock_credential = MagicMock()
        mock_chained.return_value = mock_credential
        mock_provider = MagicMock()
        mock_get_bearer.return_value = mock_provider

        scope = "https://test.com/.default"
        result = create_entra_token_provider(scope)

        # Result should be the provider from get_bearer_token_provider
        assert result == mock_provider

        # Verify get_bearer_token_provider was called with credential and scope
        mock_get_bearer.assert_called_once_with(mock_credential, scope)

    @pytest.mark.unit
    @patch("auth.auth.get_bearer_token_provider")
    @patch("auth.auth.ChainedTokenCredential")
    @patch("auth.auth.AzureCliCredential")
    @patch("auth.auth.ManagedIdentityCredential")
    def test_works_with_different_scopes(self, mock_managed, mock_cli, mock_chained, mock_get_bearer):
        """Test that different scopes are handled correctly."""
        mock_credential = MagicMock()
        mock_chained.return_value = mock_credential
        mock_get_bearer.return_value = MagicMock()

        # Test with custom scope
        custom_scope = "api://custom-api/.default"
        create_entra_token_provider(custom_scope)

        # Verify get_bearer_token_provider was called with custom scope
        mock_get_bearer.assert_called_once_with(mock_credential, custom_scope)

    @pytest.mark.unit
    @patch("auth.auth.ChainedTokenCredential")
    @patch("auth.auth.AzureCliCredential")
    @patch("auth.auth.ManagedIdentityCredential")
    def test_credential_chain_order(self, mock_managed, mock_cli, mock_chained):
        """Test that credentials are tried in correct order (CLI then ManagedIdentity)."""
        mock_cli_instance = MagicMock()
        mock_managed_instance = MagicMock()
        mock_cli.return_value = mock_cli_instance
        mock_managed.return_value = mock_managed_instance

        create_entra_token_provider("https://test.com/.default")

        # Verify order: first arg should be CLI, second should be ManagedIdentity
        call_args = mock_chained.call_args[0]
        assert call_args[0] == mock_cli_instance
        assert call_args[1] == mock_managed_instance
