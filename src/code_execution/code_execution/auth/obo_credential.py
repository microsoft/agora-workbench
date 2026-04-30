"""
Credential Provider for downstream Azure resource access.

This module provides the OBOCredentialProvider which handles authentication
to downstream Azure resources (Storage, AI Search, etc.) on behalf of the
MCP server.

Two independent axes control credential behaviour:

Authentication Method (credential type — strict priority order):
1. OBO_SIMULATION_MODE=true   → AzureCliCredential  (local dev only)
2. AZURE_CLIENT_ID is set     → ManagedIdentityCredential(client_id=...)
3. AZURE_FEDERATED_TOKEN_FILE → Workload identity federation credential
4. Default                    → ManagedIdentityCredential() (system-assigned)

Authentication Path (how the credential is applied — OBO_AUTH_PATH env var):
- False (default) → Direct access: credential calls get_token() directly.
  Resources are accessed as the server's own identity.
- True (OBO_AUTH_PATH=true) → On-behalf-of: user's JWT is exchanged for a
  resource-scoped token, preserving the user's identity.
  Only supported with the federated-token method (method 3) or simulation.

Credential matrix (method × path):
  Simulation      × direct  → AzureCliCredential
  Simulation      × OBO     → AzureCliCredential (simulates OBO; no real exchange)
  User-assigned MI × direct  → ManagedIdentityCredential(client_id=…)
  User-assigned MI × OBO     → ValueError (MI cannot perform OBO exchange)
  Federated token  × direct  → ClientAssertionCredential(…)
  Federated token  × OBO     → OnBehalfOfCredential(…)
  System MI        × direct  → ManagedIdentityCredential()
  System MI        × OBO     → ValueError (MI cannot perform OBO exchange)

Local Development Flow:
1. Developer runs `az login` on their machine
2. Developer mounts ~/.azure volume into container
3. Set OBO_SIMULATION_MODE=true
4. Server uses AzureCliCredential to get tokens as the developer
"""

import asyncio
import functools
import logging
import os
import time
from typing import Any, Callable, Optional

from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity import AzureCliCredential, ClientAssertionCredential, ManagedIdentityCredential, OnBehalfOfCredential


LOGGER = logging.getLogger(__name__)


class OBOTokenExchangeError(Exception):
    """Exception raised when OBO token exchange fails."""

    def __init__(self, message: str, scope: str, original_error: Optional[Exception] = None):
        self.scope = scope
        self.original_error = original_error
        super().__init__(message)


class OBOCredentialProvider:
    """
    Provides Azure credentials for downstream resource access.

    Two independent axes control credential behaviour.

    Authentication Method — strict priority order:
    1. OBO_SIMULATION_MODE=true   → AzureCliCredential
    2. AZURE_CLIENT_ID is set     → ManagedIdentityCredential(client_id=...)
    3. AZURE_FEDERATED_TOKEN_FILE → Workload identity federation
    4. Default                    → ManagedIdentityCredential() (system-assigned)

    Authentication Path (OBO_AUTH_PATH env var, default False):
    - False → Direct access: credential gets tokens as the server identity.
    - True  → On-behalf-of: user JWT is exchanged for a resource-scoped token.
      Only supported with method 3 (federated) or method 1 (simulation).

    Environment variables:
        OBO_SIMULATION_MODE  - Enable local-dev simulation (method 1)
        AZURE_CLIENT_ID      - User-assigned managed identity (method 2)
        AZURE_FEDERATED_TOKEN_FILE - Workload identity file path (method 3)
        OBO_AUTH_PATH        - Set to "true" to enable OBO path

    For OBO / Federated (method 3) the following are also required:
        ENTRA_CLIENT_ID      - The MCP server App Registration client ID
        ENTRA_TENANT_ID / AZURE_TENANT_ID - Azure AD tenant ID

    Example (default — system-assigned managed identity, direct access):
        >>> provider = OBOCredentialProvider(user_assertion="ignored")
        >>> token = await provider.get_token_async("https://storage.azure.com/.default")

    Example (federated token, OBO path):
        >>> # AZURE_FEDERATED_TOKEN_FILE, ENTRA_CLIENT_ID, ENTRA_TENANT_ID set
        >>> # OBO_AUTH_PATH=true
        >>> provider = OBOCredentialProvider(user_assertion="eyJ...")
        >>> token = await provider.get_token_async("https://storage.azure.com/.default")

    Example (local development):
        >>> # OBO_SIMULATION_MODE=true
        >>> provider = OBOCredentialProvider(user_assertion="ignored")
        >>> token = await provider.get_token_async("https://storage.azure.com/.default")
    """

    # Common Azure resource scopes
    STORAGE_SCOPE = "https://storage.azure.com/.default"
    SQL_SCOPE = "https://database.windows.net/.default"
    AADRM_SCOPE = "https://aadrm.com/.default"

    def __init__(
        self,
        user_assertion: str,
        client_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        federated_token_file: Optional[str] = None,
        client_assertion_func: Optional[Callable[[], str]] = None,
        simulation_mode: Optional[bool] = None,
        managed_identity: Optional[bool] = None,
        obo_path: Optional[bool] = None,
    ):
        """
        Initialize the credential provider.

        Two independent axes control behaviour:

        Authentication method (strict priority — which credential type is used):
        1. simulation_mode=True or OBO_SIMULATION_MODE=true  → AzureCliCredential
        2. AZURE_CLIENT_ID is set                            → ManagedIdentityCredential(client_id=…)
        3. federated_token_file / client_assertion_func      → Workload identity federation
        4. Default                                           → ManagedIdentityCredential()

        managed_identity=True forces the MI method regardless of env vars.
        managed_identity=False forces the federated method.

        Authentication path (OBO_AUTH_PATH env var / obo_path param, default False):
        - False → Direct access using the method credential.
        - True  → On-behalf-of: user assertion exchanged for a resource token.
          Only valid with the federated method or simulation.

        Args:
            user_assertion: The user's bearer token (JWT). Used only in OBO path.
            client_id: App Registration client ID. Falls back to ENTRA_CLIENT_ID.
                      Required for the federated method.
            tenant_id: Azure AD tenant ID. Falls back to ENTRA_TENANT_ID / AZURE_TENANT_ID.
                      Required for the federated method.
            federated_token_file: Workload identity token file path.
                                 Falls back to AZURE_FEDERATED_TOKEN_FILE.
                                 Selects the federated method when set.
            client_assertion_func: Custom function returning a client assertion token.
                                   Selects the federated method when set.
            simulation_mode: Use AzureCliCredential (local dev). Falls back to
                            OBO_SIMULATION_MODE env var.
            managed_identity: Explicit method override.
                            True → force MI method.
                            False → force federated method.
                            None (default) → auto-detect from env vars.
            obo_path: Use the on-behalf-of path. Falls back to OBO_AUTH_PATH env var.
                     Default is False (direct access).

        Raises:
            ValueError: If required configuration is missing or OBO path is
                       requested with a method that does not support it.
        """
        self._user_assertion = user_assertion
        # ENTRA_CLIENT_ID identifies the App Registration (for federated method)
        self._client_id = client_id or os.getenv("ENTRA_CLIENT_ID")
        self._tenant_id = tenant_id or os.getenv("ENTRA_TENANT_ID") or os.getenv("AZURE_TENANT_ID")
        self._federated_token_file = federated_token_file or os.getenv("AZURE_FEDERATED_TOKEN_FILE")
        self._client_assertion_func = client_assertion_func
        # AZURE_CLIENT_ID identifies the user-assigned managed identity
        # Treat empty/whitespace-only values as unset
        azure_client_id = (os.getenv("AZURE_CLIENT_ID") or "").strip() or None
        self._managed_identity_client_id = azure_client_id

        # --- Axis 1: Authentication path ---
        # OBO_AUTH_PATH=true enables on-behalf-of; default is direct access.
        if obo_path is not None:
            self._obo_path = obo_path
        else:
            obo_env = os.getenv("OBO_AUTH_PATH", "").lower()
            self._obo_path = obo_env in ("true", "1", "yes")

        # --- Axis 2: Authentication method ---
        if simulation_mode is not None:
            self._simulation_mode = simulation_mode
        else:
            sim_env = os.getenv("OBO_SIMULATION_MODE", "").lower()
            self._simulation_mode = sim_env in ("true", "1", "yes")

        # --- Method selection (strict priority order) ---
        # managed_identity parameter is an explicit override that takes effect
        # after simulation is ruled out.
        if self._simulation_mode:
            # Method 1: simulation
            self._init_simulation_mode()
        elif managed_identity is True or (
            managed_identity is None and self._managed_identity_client_id is not None
        ):
            # Method 2: user-assigned managed identity
            self._init_managed_identity_mode()
        elif managed_identity is False or self._federated_token_file or self._client_assertion_func:
            # Method 3: workload identity federation (explicit or env-var)
            self._init_federated_mode()
        else:
            # Method 4: system-assigned managed identity (default)
            self._init_managed_identity_mode()
    def _init_simulation_mode(self):
        """
        Initialize in simulation mode using Azure CLI credentials.

        This mode is for local development only. It uses the developer's
        az login session to get tokens, bypassing OBO entirely.

        To use simulation mode:
        1. Run `az login` on your development machine
        2. Mount ~/.azure into container: -v ~/.azure:/home/app/.azure
        3. Set OBO_SIMULATION_MODE=true
        """
        LOGGER.warning(
            "OBO SIMULATION MODE ENABLED - Using Azure CLI credentials. This should only be used for local development!"
        )

        # AzureCliCredential uses the az login token cache
        # Optionally constrain to a specific tenant
        if self._tenant_id:
            self._credential = AzureCliCredential(tenant_id=self._tenant_id)
            LOGGER.info(f"Using Azure CLI credentials for tenant {self._tenant_id[:8]}...")
        else:
            self._credential = AzureCliCredential()
            LOGGER.info("Using Azure CLI credentials (default tenant)")

    def _init_managed_identity_mode(self):
        """
        Initialize using ManagedIdentityCredential (direct access only).

        Only the direct-access path is supported with managed identity; managed
        identities cannot perform the OBO token exchange.  Raises ValueError if
        OBO path is requested.
        """
        if self._obo_path:
            raise ValueError(
                "OBO path (on-behalf-of) is not supported with managed identity authentication. "
                "OBO requires workload identity federation (AZURE_FEDERATED_TOKEN_FILE). "
                "Either remove OBO_AUTH_PATH=true or switch to the federated method."
            )

        LOGGER.info(
            "MANAGED IDENTITY MODE ENABLED - Using ManagedIdentityCredential for Azure Container App deployment"
        )

        if self._managed_identity_client_id:
            # User-assigned managed identity (AZURE_CLIENT_ID)
            self._credential = ManagedIdentityCredential(client_id=self._managed_identity_client_id)
            LOGGER.info(f"Using user-assigned managed identity for client {self._managed_identity_client_id[:8]}...")
        else:
            # System-assigned managed identity
            self._credential = ManagedIdentityCredential()
            LOGGER.info("Using system-assigned managed identity")

    def _init_federated_mode(self):
        """
        Initialize using workload identity federation credentials.

        Supports two paths, selected by self._obo_path:
        - Direct (default): ClientAssertionCredential — server accesses resources
          as its own application identity using the federated token.
        - OBO (OBO_AUTH_PATH=true): OnBehalfOfCredential — server exchanges the
          user's JWT assertion for a resource-scoped token.

        Both paths require ENTRA_CLIENT_ID and ENTRA_TENANT_ID.
        One of AZURE_FEDERATED_TOKEN_FILE or client_assertion_func must also
        be provided.
        """
        # Validate required configuration
        missing = []
        if not self._client_id:
            missing.append("client_id (or ENTRA_CLIENT_ID env var)")
        if not self._tenant_id:
            missing.append("tenant_id (or ENTRA_TENANT_ID/AZURE_TENANT_ID env var)")

        has_federated_token = bool(self._federated_token_file)
        has_custom_assertion = self._client_assertion_func is not None

        if not has_federated_token and not has_custom_assertion:
            missing.append(
                "AZURE_FEDERATED_TOKEN_FILE (for workload identity) or client_assertion_func"
            )

        if missing:
            raise ValueError(
                f"Federated method requires: {', '.join(missing)}. "
                "Ensure the MCP server's app registration has federated credentials configured."
            )

        assert self._client_id is not None
        assert self._tenant_id is not None

        # Choose assertion source
        assertion_func = self._client_assertion_func or self._create_workload_identity_assertion_func()

        if self._obo_path:
            # OBO path: exchange the user's assertion for a downstream resource token
            LOGGER.info(
                "FEDERATED METHOD + OBO PATH — Using OnBehalfOfCredential "
                f"(client {self._client_id[:8]}..., tenant {self._tenant_id[:8]}...)"
            )
            self._credential = OnBehalfOfCredential(
                tenant_id=self._tenant_id,
                client_id=self._client_id,
                client_assertion_func=assertion_func,
                user_assertion=self._user_assertion,
            )
        else:
            # Direct path: access resources as the server's own application identity
            LOGGER.info(
                "FEDERATED METHOD + DIRECT PATH — Using ClientAssertionCredential "
                f"(client {self._client_id[:8]}..., tenant {self._tenant_id[:8]}...)"
            )
            self._credential = ClientAssertionCredential(
                tenant_id=self._tenant_id,
                client_id=self._client_id,
                func=assertion_func,
            )

        LOGGER.debug(
            f"Initialized federated credential for client {self._client_id[:8]}... "
            f"in tenant {self._tenant_id[:8]}..."
        )

    def _create_workload_identity_assertion_func(self) -> Callable[[], str]:
        """
        Create a client assertion function for workload identity federation.

        This reads the federated token from the file specified by
        AZURE_FEDERATED_TOKEN_FILE (typically injected by AKS workload identity
        or similar platforms).

        Returns:
            A function that reads and returns the current federated token.
        """
        token_file = self._federated_token_file
        if token_file is None:
            raise RuntimeError(
                "Cannot create workload identity assertion: "
                "AZURE_FEDERATED_TOKEN_FILE is not set."
            )

        def get_assertion() -> str:
            """Read the federated token from file."""
            try:
                with open(token_file, "r") as f:
                    token = f.read().strip()
                LOGGER.debug(f"Read federated token from {token_file}")
                return token
            except Exception as e:
                LOGGER.error(f"Failed to read federated token from {token_file}: {e}")
                raise

        return get_assertion

    async def get_token_async(self, scope: str) -> AccessToken:
        """
        Exchange user assertion for an access token with the specified scope.

        This performs the OBO token exchange, returning a token that can be
        used to access downstream Azure resources on behalf of the user.

        Args:
            scope: The target resource scope (e.g., "https://storage.azure.com/.default")

        Returns:
            AccessToken with the token string and expiration timestamp

        Raises:
            OBOTokenExchangeError: If token exchange fails
        """
        import asyncio

        try:
            LOGGER.debug(f"Exchanging user token for scope: {scope}")
            # OnBehalfOfCredential.get_token is synchronous, so run in executor
            loop = asyncio.get_running_loop()
            token = await loop.run_in_executor(None, self._credential.get_token, scope)
            LOGGER.debug(f"Successfully obtained OBO token for scope: {scope}")
            return token
        except Exception as e:
            LOGGER.error(f"OBO token exchange failed for scope {scope}: {e}")
            raise OBOTokenExchangeError(
                f"Failed to exchange user token for scope '{scope}': {e}",
                scope=scope,
                original_error=e,
            ) from e

    def get_token(self, scope: str) -> AccessToken:
        """
        Synchronous wrapper for get_token_async.

        For async contexts, prefer get_token_async directly.

        Args:
            scope: The target resource scope

        Returns:
            AccessToken with the token string and expiration timestamp

        Raises:
            OBOTokenExchangeError: If token exchange fails
        """
        try:
            LOGGER.debug(f"Exchanging user token for scope: {scope}")
            token = self._credential.get_token(scope)
            LOGGER.debug(f"Successfully obtained OBO token for scope: {scope}")
            return token
        except Exception as e:
            LOGGER.error(f"OBO token exchange failed for scope {scope}: {e}")
            raise OBOTokenExchangeError(
                f"Failed to exchange user token for scope '{scope}': {e}",
                scope=scope,
                original_error=e,
            ) from e

    async def get_storage_token_async(self) -> AccessToken:
        """
        Get an access token for Azure Blob Storage.

        Convenience method for the common case of accessing storage.

        Returns:
            AccessToken for Azure Storage
        """
        return await self.get_token_async(self.STORAGE_SCOPE)

    async def get_sql_token_async(self) -> AccessToken:
        """
        Get an access token for Azure SQL Database.

        Convenience method for the common case of accessing SQL databases.

        Returns:
            AccessToken for Azure SQL Database
        """
        return await self.get_token_async(self.SQL_SCOPE)

    async def get_aadrm_token_async(self) -> AccessToken:
        """
        Get an access token for Azure Rights Management Services.

        Used for IRM/DRM decryption of protected Office files.

        In simulation mode, Azure CLI tokens are rejected by RMS because
        the CLI app ID is classified as a "Web" platform. Instead, we use
        MSAL device code flow with the MCP server's app registration
        (ENTRA_CLIENT_ID) which has RMS API permissions.

        Returns:
            AccessToken for Azure RMS
        """
        if not self._simulation_mode:
            return await self.get_token_async(self.AADRM_SCOPE)

        # Simulation mode: use MSAL device code flow with the MCP server app
        return await self._get_rms_token_via_device_code()

    async def _get_rms_token_via_device_code(self) -> AccessToken:
        """
        Acquire RMS token using MSAL device code flow with ENTRA_CLIENT_ID.

        Azure CLI tokens carry appid 04b07795-... which Azure RMS blocks
        with DevicePlatformDisabledException. Using the MCP server's app ID
        avoids this because it has proper RMS API permissions.

        MSAL's device code flow is synchronous and polls until the user
        completes interactive auth, so the blocking work is offloaded to a
        thread executor to avoid stalling the event loop.
        """
        from msal import PublicClientApplication

        client_id = self._client_id or os.getenv("ENTRA_CLIENT_ID")
        if not client_id:
            raise OBOTokenExchangeError(
                "ENTRA_CLIENT_ID is required for RMS token acquisition in simulation mode",
                scope=self.AADRM_SCOPE,
            )

        tenant_id = self._tenant_id or "common"
        authority = f"https://login.microsoftonline.com/{tenant_id}"
        scopes = ["https://aadrm.com/.default"]

        app = PublicClientApplication(client_id, authority=authority)

        loop = asyncio.get_running_loop()

        # Try silent acquisition first (cached token)
        accounts = await loop.run_in_executor(None, app.get_accounts)
        if accounts:
            result = await loop.run_in_executor(
                None,
                functools.partial(app.acquire_token_silent, scopes, account=accounts[0]),
            )
            if result and "access_token" in result:
                LOGGER.debug("Acquired RMS token from MSAL cache")
                return AccessToken(result["access_token"], int(time.time()) + result.get("expires_in", 3600))

        # Need interactive device code flow
        flow = await loop.run_in_executor(
            None,
            functools.partial(app.initiate_device_flow, scopes=scopes),
        )
        if "error" in flow:
            raise OBOTokenExchangeError(
                f"Failed to initiate device code flow: {flow.get('error_description', flow['error'])}",
                scope=self.AADRM_SCOPE,
            )

        LOGGER.warning(
            f"RMS requires interactive auth (one-time). "
            f"Go to {flow['verification_uri']} and enter code: {flow['user_code']}"
        )
        print(f"\n{'=' * 60}")
        print("IRM DECRYPTION: Azure RMS requires interactive authentication.")
        print(f"Go to: {flow['verification_uri']}")
        print(f"Enter code: {flow['user_code']}")
        print(f"{'=' * 60}\n")

        result = await loop.run_in_executor(
            None,
            functools.partial(app.acquire_token_by_device_flow, flow),
        )

        if "access_token" not in result:
            error = result.get("error_description", result.get("error", "Unknown error"))
            raise OBOTokenExchangeError(f"RMS device code auth failed: {error}", scope=self.AADRM_SCOPE)

        LOGGER.info("Successfully acquired RMS token via device code flow")
        return AccessToken(result["access_token"], int(time.time()) + result.get("expires_in", 3600))

    def get_credential(self):
        """
        Get the underlying credential object.

        Returns the wrapped credential (OnBehalfOfCredential, ManagedIdentityCredential,
        or AzureCliCredential) that can be used directly with Azure SDK clients.

        Returns:
            The underlying Azure credential that implements get_token()
        """
        return self._credential

    def close(self):
        """Close the underlying credential and release resources."""
        if hasattr(self._credential, "close"):
            self._credential.close()

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - close resources."""
        self.close()


# Module-level factory function for dependency injection
_obo_provider_factory = None


def configure_obo_provider_factory(factory):
    """
    Configure a custom factory for creating OBO credential providers.

    Useful for testing or custom authentication scenarios.

    Args:
        factory: A callable that takes (user_assertion, **kwargs) and returns
                OBOCredentialProvider. The factory receives keyword arguments
                including obo_path, client_id, tenant_id, federated_token_file,
                and client_assertion_func so callers can control the auth path
                even under dependency injection. For backward compatibility,
                simple factories that only accept user_assertion also work —
                extra kwargs are passed but ignored if the factory signature
                does not declare them.

    Returns:
        The previous factory value (for save/restore patterns in tests)
    """
    global _obo_provider_factory
    previous = _obo_provider_factory
    _obo_provider_factory = factory
    return previous


def get_obo_credential_provider(
    user_assertion: str,
    client_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    federated_token_file: Optional[str] = None,
    client_assertion_func: Optional[Callable[[], str]] = None,
    obo_path: Optional[bool] = None,
) -> OBOCredentialProvider:
    """
    Get a credential provider for the given user assertion.

    Uses the configured factory if set, otherwise creates a standard provider.

    When a custom factory is configured, all keyword arguments (obo_path,
    client_id, tenant_id, etc.) are forwarded to it as **kwargs so the factory
    can honour the auth-path axis. Factories that only accept user_assertion
    (older signature) continue to work because the extra kwargs are forwarded
    but only consumed if the factory declares them.

    Args:
        user_assertion: The user's bearer token
        client_id: Optional override for client ID
        tenant_id: Optional override for tenant ID
        federated_token_file: Optional path to federated token file
        client_assertion_func: Optional custom assertion function
        obo_path: Optional override for OBO path selection

    Returns:
        OBOCredentialProvider instance
    """
    if _obo_provider_factory:
        import inspect

        kwargs = dict(
            client_id=client_id,
            tenant_id=tenant_id,
            federated_token_file=federated_token_file,
            client_assertion_func=client_assertion_func,
            obo_path=obo_path,
        )
        sig = inspect.signature(_obo_provider_factory)
        # Only pass kwargs the factory actually accepts to maintain backward
        # compatibility with factories that only take (user_assertion).
        if any(
            p.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            or p.name in kwargs
            for p in sig.parameters.values()
        ):
            return _obo_provider_factory(user_assertion, **kwargs)
        return _obo_provider_factory(user_assertion)

    return OBOCredentialProvider(
        user_assertion=user_assertion,
        client_id=client_id,
        tenant_id=tenant_id,
        federated_token_file=federated_token_file,
        client_assertion_func=client_assertion_func,
        obo_path=obo_path,
    )


class _AsyncOBOCredentialWrapper(AsyncTokenCredential):
    """
    Wraps a synchronous OBO credential provider for use with async Azure SDK clients.

    Azure SDK async clients expect AsyncTokenCredential, but our OBOCredentialProvider
    uses synchronous OnBehalfOfCredential internally. This wrapper bridges the gap
    by running the synchronous get_token() in an executor.
    """

    def __init__(self, obo_provider):
        """
        Initialize the wrapper.

        Args:
            obo_provider: OBOCredentialProvider instance
        """
        self._obo_provider = obo_provider

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        """
        Get an access token for the requested scopes.

        Args:
            *scopes: Token scopes to request
            **kwargs: Additional keyword arguments

        Returns:
            AccessToken with token string and expiry
        """
        # Use the first scope provided
        if scopes:
            scope = scopes[0]
        else:
            raise ValueError("No scope given for token.")

        # Run the synchronous get_token in an executor to avoid blocking
        loop = asyncio.get_running_loop()
        credential = self._obo_provider.get_credential()
        token = await loop.run_in_executor(None, credential.get_token, scope)
        return token

    async def close(self) -> None:
        """Close the credential and release resources."""
        pass
