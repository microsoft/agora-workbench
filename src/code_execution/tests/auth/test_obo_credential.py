"""Tests for OBO credential provider with federated credentials."""

import pytest
from unittest.mock import MagicMock, patch, mock_open

from azure.core.credentials import AccessToken

from ...code_execution.auth.obo_credential import (
    OBOCredentialProvider,
    OBOTokenExchangeError,
    get_obo_credential_provider,
    configure_obo_provider_factory,
)
from ...code_execution.auth import obo_credential as obo_credential_module


class TestOBOCredentialProviderSimulationMode:
    """Tests for OBO simulation mode (local development)."""

    def test_simulation_mode_via_env_var(self, monkeypatch):
        """Should use Azure CLI credentials when OBO_SIMULATION_MODE=true."""
        monkeypatch.setenv("OBO_SIMULATION_MODE", "true")
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        with patch.object(obo_credential_module, "AzureCliCredential") as mock_cli_cred:
            OBOCredentialProvider(user_assertion="ignored-token")
            # Should have created AzureCliCredential with no tenant constraint
            mock_cli_cred.assert_called_once_with()

    def test_simulation_mode_via_parameter(self, monkeypatch):
        """Should use Azure CLI credentials when simulation_mode=True."""
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        with patch.object(obo_credential_module, "AzureCliCredential") as mock_cli_cred:
            OBOCredentialProvider(user_assertion="ignored-token", simulation_mode=True)
            mock_cli_cred.assert_called_once_with()

    def test_simulation_mode_with_tenant(self, monkeypatch):
        """Should constrain to tenant when provided in simulation mode."""
        monkeypatch.setenv("OBO_SIMULATION_MODE", "true")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        with patch.object(obo_credential_module, "AzureCliCredential") as mock_cli_cred:
            OBOCredentialProvider(user_assertion="ignored-token")
            mock_cli_cred.assert_called_once_with(tenant_id="test-tenant-id")

    def test_simulation_mode_env_values(self, monkeypatch):
        """Should accept various truthy values for OBO_SIMULATION_MODE."""
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)
        for value in ["true", "True", "TRUE", "1", "yes", "Yes"]:
            monkeypatch.setenv("OBO_SIMULATION_MODE", value)
            monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)

            with patch.object(obo_credential_module, "AzureCliCredential") as mock_cli_cred:
                OBOCredentialProvider(user_assertion="ignored")
                mock_cli_cred.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_simulation_mode_get_token(self, monkeypatch):
        """Should successfully get token in simulation mode."""
        monkeypatch.setenv("OBO_SIMULATION_MODE", "true")
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_TENANT_ID", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        mock_credential = MagicMock()
        mock_credential.get_token = MagicMock(return_value=AccessToken(token="cli-token", expires_on=9999999999))

        with patch.object(obo_credential_module, "AzureCliCredential", return_value=mock_credential):
            provider = OBOCredentialProvider(user_assertion="ignored")
            token = await provider.get_token_async("https://storage.azure.com/.default")

            assert token.token == "cli-token"
            mock_credential.get_token.assert_called_with("https://storage.azure.com/.default")

    def test_simulation_mode_takes_priority_over_managed_identity(self, monkeypatch):
        """Should prefer simulation mode when both simulation and AZURE_CLIENT_ID are set."""
        monkeypatch.setenv("OBO_SIMULATION_MODE", "true")
        monkeypatch.setenv("AZURE_CLIENT_ID", "mi-client-id")
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        with patch.object(obo_credential_module, "AzureCliCredential") as mock_cli_cred:
            OBOCredentialProvider(user_assertion="ignored-token")
            mock_cli_cred.assert_called_once_with()


class TestOBOCredentialProviderManagedIdentityMode:
    """Tests for managed identity mode (Azure Container App deployments)."""

    def test_managed_identity_auto_detected_from_azure_client_id(self, monkeypatch):
        """Should auto-detect managed identity mode when AZURE_CLIENT_ID is set."""
        monkeypatch.setenv("AZURE_CLIENT_ID", "mi-client-id")
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        with patch.object(obo_credential_module, "ManagedIdentityCredential") as mock_mi_cred:
            OBOCredentialProvider(user_assertion="ignored-token")
            mock_mi_cred.assert_called_once_with(client_id="mi-client-id")

    def test_managed_identity_explicit_system_assigned(self, monkeypatch):
        """Should use system-assigned MI when managed_identity=True without AZURE_CLIENT_ID."""
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        with patch.object(obo_credential_module, "ManagedIdentityCredential") as mock_mi_cred:
            OBOCredentialProvider(user_assertion="ignored-token", managed_identity=True)
            mock_mi_cred.assert_called_once_with()

    def test_managed_identity_explicit_false_skips_auto_detection(self, monkeypatch):
        """Should skip MI auto-detection when managed_identity=False, even if AZURE_CLIENT_ID is set."""
        monkeypatch.setenv("AZURE_CLIENT_ID", "mi-client-id")
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        with patch.object(obo_credential_module, "ClientAssertionCredential") as mock_cac:
            OBOCredentialProvider(user_assertion="test-token", managed_identity=False)
            # managed_identity=False → federated method; direct path (default) → ClientAssertionCredential
            mock_cac.assert_called_once()

    def test_managed_identity_entra_client_id_not_used(self, monkeypatch):
        """Should NOT use ENTRA_CLIENT_ID for managed identity (it's for App Registration)."""
        monkeypatch.setenv("ENTRA_CLIENT_ID", "app-reg-client-id")
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        with patch.object(obo_credential_module, "ManagedIdentityCredential") as mock_mi_cred:
            OBOCredentialProvider(user_assertion="ignored-token", managed_identity=True)
            # Should create system-assigned MI, NOT pass ENTRA_CLIENT_ID
            mock_mi_cred.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_managed_identity_get_token(self, monkeypatch):
        """Should successfully get token in managed identity mode."""
        monkeypatch.setenv("AZURE_CLIENT_ID", "mi-client-id")
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        mock_credential = MagicMock()
        mock_credential.get_token = MagicMock(return_value=AccessToken(token="mi-token", expires_on=9999999999))

        with patch.object(obo_credential_module, "ManagedIdentityCredential", return_value=mock_credential):
            provider = OBOCredentialProvider(user_assertion="ignored")
            token = await provider.get_token_async("https://storage.azure.com/.default")

            assert token.token == "mi-token"
            mock_credential.get_token.assert_called_with("https://storage.azure.com/.default")

    def test_federated_method_without_azure_client_id(self, monkeypatch):
        """Should use federated method (ClientAssertionCredential) when AZURE_FEDERATED_TOKEN_FILE is set and no AZURE_CLIENT_ID."""
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        with patch.object(obo_credential_module, "ClientAssertionCredential") as mock_cac:
            OBOCredentialProvider(user_assertion="test-token")
            # Direct path (default) → ClientAssertionCredential
            mock_cac.assert_called_once()

    def test_empty_azure_client_id_does_not_trigger_mi(self, monkeypatch):
        """Should NOT auto-detect MI when AZURE_CLIENT_ID is empty or whitespace."""
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        for empty_value in ["", "  ", "\t"]:
            monkeypatch.setenv("AZURE_CLIENT_ID", empty_value)

            with patch.object(obo_credential_module, "ClientAssertionCredential") as mock_cac:
                OBOCredentialProvider(user_assertion="test-token")
                # Empty AZURE_CLIENT_ID → falls through to federated method; direct path → ClientAssertionCredential
                mock_cac.assert_called_once()

    def test_azure_client_id_takes_precedence_over_federated_token_file(self, monkeypatch):
        """AZURE_CLIENT_ID (method 2) has higher priority than AZURE_FEDERATED_TOKEN_FILE (method 3)."""
        monkeypatch.setenv("AZURE_CLIENT_ID", "mi-client-id")
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        with patch.object(obo_credential_module, "ManagedIdentityCredential") as mock_mi_cred:
            OBOCredentialProvider(user_assertion="test-token")
            # AZURE_CLIENT_ID has higher priority than AZURE_FEDERATED_TOKEN_FILE
            mock_mi_cred.assert_called_once_with(client_id="mi-client-id")


class TestOBOCredentialProvider:
    """Tests for OBOCredentialProvider class."""

    def test_managed_identity_is_default_without_federated_token(self, monkeypatch):
        """Should default to system-assigned managed identity when no env vars are set."""
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        with patch.object(obo_credential_module, "ManagedIdentityCredential") as mock_mi_cred:
            OBOCredentialProvider(user_assertion="test-token")
            mock_mi_cred.assert_called_once_with()

    def test_managed_identity_default_with_entra_client_id_only(self, monkeypatch):
        """Should use system-assigned MI even when ENTRA_CLIENT_ID is set (not AZURE_CLIENT_ID)."""
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        with patch.object(obo_credential_module, "ManagedIdentityCredential") as mock_mi_cred:
            OBOCredentialProvider(user_assertion="test-token")
            mock_mi_cred.assert_called_once_with()

    def test_direct_path_is_default_with_federated_token_file(self, monkeypatch):
        """AZURE_FEDERATED_TOKEN_FILE selects the federated method; direct path (default) uses ClientAssertionCredential."""
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        with patch.object(obo_credential_module, "ClientAssertionCredential") as mock_cac:
            OBOCredentialProvider(user_assertion="test-token")
            mock_cac.assert_called_once()

    def test_init_success_with_federated_token_file(self, monkeypatch):
        """Should initialize with ClientAssertionCredential (direct path) when federated token file is set."""
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        with patch.object(obo_credential_module, "ClientAssertionCredential") as mock_cac:
            OBOCredentialProvider(user_assertion="test-token")

            # Verify ClientAssertionCredential was called with correct args
            mock_cac.assert_called_once()
            call_kwargs = mock_cac.call_args[1]
            assert call_kwargs["tenant_id"] == "test-tenant-id"
            assert call_kwargs["client_id"] == "test-client-id"
            assert "func" in call_kwargs
            assert callable(call_kwargs["func"])

    def test_init_obo_path_with_federated_token_file(self, monkeypatch):
        """Should initialize with OnBehalfOfCredential when OBO path is enabled with federated token file."""
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        with patch.object(obo_credential_module, "OnBehalfOfCredential") as mock_obo_cred:
            OBOCredentialProvider(user_assertion="test-token", obo_path=True)

            # Verify OnBehalfOfCredential was called with user_assertion
            mock_obo_cred.assert_called_once()
            call_kwargs = mock_obo_cred.call_args[1]
            assert call_kwargs["tenant_id"] == "test-tenant-id"
            assert call_kwargs["client_id"] == "test-client-id"
            assert call_kwargs["user_assertion"] == "test-token"
            assert "client_assertion_func" in call_kwargs
            assert callable(call_kwargs["client_assertion_func"])

    def test_init_success_with_custom_assertion_func_direct_path(self, monkeypatch):
        """Should use ClientAssertionCredential (direct path) when custom assertion func is provided."""
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        def custom_func():
            return "custom-assertion-token"

        with patch.object(obo_credential_module, "ClientAssertionCredential") as mock_cac:
            OBOCredentialProvider(
                user_assertion="test-token",
                client_assertion_func=custom_func,
            )

            mock_cac.assert_called_once_with(
                tenant_id="test-tenant-id",
                client_id="test-client-id",
                func=custom_func,
            )

    def test_init_success_with_custom_assertion_func_obo_path(self, monkeypatch):
        """Should use OnBehalfOfCredential (OBO path) when custom assertion func is provided with obo_path=True."""
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        def custom_func():
            return "custom-assertion-token"

        with patch.object(obo_credential_module, "OnBehalfOfCredential") as mock_obo_cred:
            OBOCredentialProvider(
                user_assertion="test-token",
                client_assertion_func=custom_func,
                obo_path=True,
            )

            mock_obo_cred.assert_called_once_with(
                tenant_id="test-tenant-id",
                client_id="test-client-id",
                client_assertion_func=custom_func,
                user_assertion="test-token",
            )

    def test_workload_identity_assertion_func_reads_token(self, monkeypatch):
        """Should read token from federated token file (passed as func= to ClientAssertionCredential)."""
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        with patch.object(obo_credential_module, "ClientAssertionCredential") as mock_cac:
            # Create provider (direct path — uses ClientAssertionCredential)
            OBOCredentialProvider(user_assertion="test-token")

            # Get the assertion func that was passed as `func=`
            call_kwargs = mock_cac.call_args[1]
            assertion_func = call_kwargs["func"]

            # Test that it reads from the file
            with patch("builtins.open", mock_open(read_data="federated-token-content")):
                token = assertion_func()
                assert token == "federated-token-content"

    @pytest.mark.asyncio
    async def test_get_token_async_success(self, monkeypatch):
        """Should successfully get token via async method."""
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        # Mock the credential instance - get_token is synchronous
        mock_credential = MagicMock()
        mock_credential.get_token = MagicMock(return_value=AccessToken(token="exchanged-token", expires_on=9999999999))

        with patch.object(obo_credential_module, "ClientAssertionCredential", return_value=mock_credential):
            provider = OBOCredentialProvider(user_assertion="test-token")
            token = await provider.get_token_async("https://storage.azure.com/.default")

            assert token.token == "exchanged-token"

    @pytest.mark.asyncio
    async def test_get_token_async_failure(self, monkeypatch):
        """Should raise OBOTokenExchangeError on failure."""
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        # Mock the credential instance to raise an error
        mock_credential = MagicMock()
        mock_credential.get_token = MagicMock(side_effect=Exception("Auth failed"))

        with patch.object(obo_credential_module, "ClientAssertionCredential", return_value=mock_credential):
            provider = OBOCredentialProvider(user_assertion="test-token")

            with pytest.raises(OBOTokenExchangeError) as exc_info:
                await provider.get_token_async("https://storage.azure.com/.default")

            assert exc_info.value.scope == "https://storage.azure.com/.default"
            assert exc_info.value.original_error is not None

    @pytest.mark.asyncio
    async def test_get_storage_token_async(self, monkeypatch):
        """Should use correct scope for storage token."""
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        mock_credential = MagicMock()
        mock_credential.get_token = MagicMock(return_value=AccessToken(token="storage-token", expires_on=9999999999))

        with patch.object(obo_credential_module, "ClientAssertionCredential", return_value=mock_credential):
            provider = OBOCredentialProvider(user_assertion="test-token")
            await provider.get_storage_token_async()

            mock_credential.get_token.assert_called_with("https://storage.azure.com/.default")

    @pytest.mark.asyncio
    async def test_get_sql_token_async(self, monkeypatch):
        """Should use correct scope for SQL token."""
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        mock_credential = MagicMock()
        mock_credential.get_token = MagicMock(return_value=AccessToken(token="sql-token", expires_on=9999999999))

        with patch.object(obo_credential_module, "ClientAssertionCredential", return_value=mock_credential):
            provider = OBOCredentialProvider(user_assertion="test-token")
            await provider.get_sql_token_async()

            mock_credential.get_token.assert_called_with("https://database.windows.net/.default")


class TestGetOBOCredentialProvider:
    """Tests for get_obo_credential_provider factory function."""

    def test_uses_configured_factory(self, monkeypatch):
        """Should use custom factory when configured and forward kwargs to it."""
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")

        # Configure custom factory, saving previous value
        custom_provider = MagicMock()
        custom_factory = MagicMock(return_value=custom_provider)
        previous_factory = configure_obo_provider_factory(custom_factory)

        try:
            with patch.object(obo_credential_module, "OnBehalfOfCredential"):
                result = get_obo_credential_provider("test-token", obo_path=True)

                # Factory receives user_assertion + kwargs (MagicMock accepts **kwargs)
                custom_factory.assert_called_once_with(
                    "test-token",
                    client_id=None,
                    tenant_id=None,
                    federated_token_file=None,
                    client_assertion_func=None,
                    obo_path=True,
                )
                assert result is custom_provider
        finally:
            # Restore the previous factory
            configure_obo_provider_factory(previous_factory)

    def test_uses_configured_factory_backward_compat(self, monkeypatch):
        """Factory that only accepts user_assertion should still work (backward compatibility)."""
        custom_provider = MagicMock()

        def simple_factory(user_assertion):
            return custom_provider

        previous_factory = configure_obo_provider_factory(simple_factory)

        try:
            result = get_obo_credential_provider("test-token", obo_path=True)
            # backward-compat factory called with just user_assertion, no kwargs
            assert result is custom_provider
        finally:
            configure_obo_provider_factory(previous_factory)

    def test_creates_default_provider(self, monkeypatch):
        """Should create default provider when no factory configured."""
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        # Temporarily clear the factory, saving previous value
        previous_factory = configure_obo_provider_factory(None)

        try:
            with patch.object(obo_credential_module, "ClientAssertionCredential") as mock_cac:
                provider = get_obo_credential_provider("test-token")

                assert isinstance(provider, OBOCredentialProvider)
                mock_cac.assert_called_once()
        finally:
            # Restore the previous factory
            configure_obo_provider_factory(previous_factory)


class TestOBOAuthPath:
    """Tests for the OBO_AUTH_PATH / obo_path decoupling from auth method."""

    # --- OBO path via env var ---

    def test_obo_auth_path_env_var_activates_obo_with_federated(self, monkeypatch):
        """OBO_AUTH_PATH=true with federated method should use OnBehalfOfCredential."""
        monkeypatch.setenv("OBO_AUTH_PATH", "true")
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)

        with patch.object(obo_credential_module, "OnBehalfOfCredential") as mock_obo_cred:
            OBOCredentialProvider(user_assertion="user-jwt")
            mock_obo_cred.assert_called_once()
            call_kwargs = mock_obo_cred.call_args[1]
            assert call_kwargs["user_assertion"] == "user-jwt"

    def test_obo_auth_path_default_is_direct(self, monkeypatch):
        """Without OBO_AUTH_PATH, federated method defaults to ClientAssertionCredential."""
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)

        with patch.object(obo_credential_module, "ClientAssertionCredential") as mock_cac:
            OBOCredentialProvider(user_assertion="user-jwt")
            mock_cac.assert_called_once()

    # --- OBO path via parameter ---

    def test_obo_path_param_true_activates_obo(self, monkeypatch):
        """obo_path=True with federated method should use OnBehalfOfCredential."""
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)

        with patch.object(obo_credential_module, "OnBehalfOfCredential") as mock_obo_cred:
            OBOCredentialProvider(user_assertion="user-jwt", obo_path=True)
            mock_obo_cred.assert_called_once()

    def test_obo_path_param_false_overrides_env_var(self, monkeypatch):
        """obo_path=False overrides OBO_AUTH_PATH=true env var."""
        monkeypatch.setenv("OBO_AUTH_PATH", "true")
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)

        with patch.object(obo_credential_module, "ClientAssertionCredential") as mock_cac:
            OBOCredentialProvider(user_assertion="user-jwt", obo_path=False)
            # obo_path=False parameter should override OBO_AUTH_PATH env var
            mock_cac.assert_called_once()

    # --- Method × path combinations ---

    def test_obo_path_with_user_assigned_mi_raises_error(self, monkeypatch):
        """OBO path is not supported with user-assigned managed identity method."""
        monkeypatch.setenv("OBO_AUTH_PATH", "true")
        monkeypatch.setenv("AZURE_CLIENT_ID", "mi-client-id")
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)

        with pytest.raises(ValueError, match="OBO path.*not supported.*managed identity"):
            OBOCredentialProvider(user_assertion="user-jwt")

    def test_obo_path_with_system_mi_raises_error(self, monkeypatch):
        """OBO path is not supported with system-assigned managed identity method."""
        monkeypatch.setenv("OBO_AUTH_PATH", "true")
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)

        with pytest.raises(ValueError, match="OBO path.*not supported.*managed identity"):
            OBOCredentialProvider(user_assertion="user-jwt")

    def test_simulation_method_ignores_obo_path(self, monkeypatch):
        """Simulation method uses AzureCliCredential regardless of OBO_AUTH_PATH."""
        monkeypatch.setenv("OBO_SIMULATION_MODE", "true")
        monkeypatch.setenv("OBO_AUTH_PATH", "true")
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_TENANT_ID", raising=False)

        with patch.object(obo_credential_module, "AzureCliCredential") as mock_cli_cred:
            OBOCredentialProvider(user_assertion="ignored")
            mock_cli_cred.assert_called_once()

    # --- Method priority order ---

    def test_simulation_beats_azure_client_id(self, monkeypatch):
        """OBO_SIMULATION_MODE takes priority over AZURE_CLIENT_ID."""
        monkeypatch.setenv("OBO_SIMULATION_MODE", "true")
        monkeypatch.setenv("AZURE_CLIENT_ID", "mi-client-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_TENANT_ID", raising=False)

        with patch.object(obo_credential_module, "AzureCliCredential") as mock_cli_cred:
            OBOCredentialProvider(user_assertion="ignored")
            mock_cli_cred.assert_called_once()

    def test_azure_client_id_beats_federated_token_file(self, monkeypatch):
        """AZURE_CLIENT_ID (method 2) takes priority over AZURE_FEDERATED_TOKEN_FILE (method 3)."""
        monkeypatch.delenv("OBO_SIMULATION_MODE", raising=False)
        monkeypatch.setenv("AZURE_CLIENT_ID", "mi-client-id")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/tokens/token")
        monkeypatch.delenv("OBO_AUTH_PATH", raising=False)

        with patch.object(obo_credential_module, "ManagedIdentityCredential") as mock_mi_cred:
            OBOCredentialProvider(user_assertion="ignored")
            mock_mi_cred.assert_called_once_with(client_id="mi-client-id")
