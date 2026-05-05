"""Tests for service-specific credential factories in auth.providers."""

import pytest
from unittest.mock import patch, MagicMock

from azure.core.credentials import AzureKeyCredential

from auth.providers import (
    get_search_credential,
    get_search_credential_async,
    get_storage_connection_string,
    is_key_based_auth,
    AZURE_SEARCH_API_KEY_ENV,
    AZURE_STORAGE_CONNECTION_STRING_ENV,
)


class TestGetSearchCredential:
    """Tests for get_search_credential (sync)."""

    @pytest.mark.unit
    @patch.dict("os.environ", {AZURE_SEARCH_API_KEY_ENV: "test-api-key-12345"})
    def test_returns_azure_key_credential_when_api_key_set(self):
        """When AZURE_SEARCH_API_KEY is set, returns AzureKeyCredential."""
        result = get_search_credential()
        assert isinstance(result, AzureKeyCredential)
        assert result.key == "test-api-key-12345"

    @pytest.mark.unit
    @patch.dict("os.environ", {}, clear=True)
    @patch("auth.providers.AzureCliCredential")
    @patch("auth.providers.ManagedIdentityCredential")
    @patch("auth.providers.ChainedTokenCredential")
    def test_returns_chained_credential_when_no_api_key(self, mock_chained, mock_managed, mock_cli):
        """When no API key is set, returns ChainedTokenCredential (Entra)."""
        mock_credential = MagicMock()
        mock_chained.return_value = mock_credential

        result = get_search_credential()

        assert result == mock_credential
        mock_chained.assert_called_once()

    @pytest.mark.unit
    @patch.dict("os.environ", {}, clear=True)
    @patch("auth.providers.AzureCliCredential")
    @patch("auth.providers.ManagedIdentityCredential")
    @patch("auth.providers.ChainedTokenCredential")
    def test_credential_chain_order(self, mock_chained, mock_managed, mock_cli):
        """Credential chain should be CLI first, then Managed Identity."""
        mock_cli_instance = MagicMock()
        mock_managed_instance = MagicMock()
        mock_cli.return_value = mock_cli_instance
        mock_managed.return_value = mock_managed_instance

        get_search_credential()

        call_args = mock_chained.call_args[0]
        assert call_args[0] == mock_cli_instance
        assert call_args[1] == mock_managed_instance

    @pytest.mark.unit
    @patch.dict(
        "os.environ",
        {"DEFAULT_IDENTITY_CLIENT_ID": "my-client-id"},
        clear=True,
    )
    @patch("auth.providers.AzureCliCredential")
    @patch("auth.providers.ManagedIdentityCredential")
    @patch("auth.providers.ChainedTokenCredential")
    def test_passes_managed_identity_client_id(self, mock_chained, mock_managed, mock_cli):
        """DEFAULT_IDENTITY_CLIENT_ID is passed to ManagedIdentityCredential."""
        get_search_credential()
        mock_managed.assert_called_once_with(client_id="my-client-id")


class TestGetSearchCredentialAsync:
    """Tests for get_search_credential_async."""

    @pytest.mark.unit
    @patch.dict("os.environ", {AZURE_SEARCH_API_KEY_ENV: "async-key-99"})
    def test_returns_azure_key_credential_when_api_key_set(self):
        """When AZURE_SEARCH_API_KEY is set, returns AzureKeyCredential (same for sync/async)."""
        result = get_search_credential_async()
        assert isinstance(result, AzureKeyCredential)
        assert result.key == "async-key-99"

    @pytest.mark.unit
    @patch.dict("os.environ", {}, clear=True)
    @patch("auth.providers.AsyncAzureCliCredential")
    @patch("auth.providers.AsyncManagedIdentityCredential")
    @patch("auth.providers.AsyncChainedTokenCredential")
    def test_returns_async_chained_credential_when_no_api_key(self, mock_chained, mock_managed, mock_cli):
        """When no API key is set, returns async ChainedTokenCredential."""
        mock_credential = MagicMock()
        mock_chained.return_value = mock_credential

        result = get_search_credential_async()

        assert result == mock_credential
        mock_chained.assert_called_once()


class TestGetStorageConnectionString:
    """Tests for get_storage_connection_string."""

    @pytest.mark.unit
    @patch.dict(
        "os.environ",
        {AZURE_STORAGE_CONNECTION_STRING_ENV: "DefaultEndpointsProtocol=https;AccountName=test;"},
    )
    def test_returns_connection_string_when_set(self):
        """Returns the connection string value when env var is set."""
        result = get_storage_connection_string()
        assert result == "DefaultEndpointsProtocol=https;AccountName=test;"

    @pytest.mark.unit
    @patch.dict("os.environ", {}, clear=True)
    def test_returns_none_when_not_set(self):
        """Returns None when env var is not set."""
        result = get_storage_connection_string()
        assert result is None

    @pytest.mark.unit
    @patch.dict("os.environ", {AZURE_STORAGE_CONNECTION_STRING_ENV: ""})
    def test_returns_none_when_empty(self):
        """Returns None when env var is empty string."""
        result = get_storage_connection_string()
        assert result is None


class TestIsKeyBasedAuth:
    """Tests for is_key_based_auth."""

    @pytest.mark.unit
    @patch.dict("os.environ", {AZURE_SEARCH_API_KEY_ENV: "some-key"})
    def test_returns_true_when_api_key_set(self):
        """Returns True when AZURE_SEARCH_API_KEY is set."""
        assert is_key_based_auth() is True

    @pytest.mark.unit
    @patch.dict("os.environ", {}, clear=True)
    def test_returns_false_when_no_api_key(self):
        """Returns False when no API key env vars are set."""
        assert is_key_based_auth() is False

    @pytest.mark.unit
    @patch.dict("os.environ", {AZURE_SEARCH_API_KEY_ENV: ""})
    def test_returns_false_when_empty(self):
        """Returns False when API key env var is empty."""
        assert is_key_based_auth() is False
