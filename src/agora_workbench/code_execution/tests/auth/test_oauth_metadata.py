"""Tests for OAuth 2.0 Protected Resource Metadata (RFC 9728).

Validates that CodeExecutionServer correctly exposes the
/.well-known/oauth-protected-resource endpoint and includes
WWW-Authenticate headers on 401 responses for MCP OAuth discovery.
"""

import logging
from unittest.mock import patch

from starlette.testclient import TestClient

from ... import CodeExecutionServer
from ...auth import AuthConfig, NoOpIdentityExtractor, TokenValidator, create_entra_auth_config, create_noop_auth_config
from ...code_execution_models import ServerConfig


class PartialTokenValidator(TokenValidator):
    """Validator exposing client metadata without a tenant attribute."""

    def __init__(self) -> None:
        self._client_id = "partial-client-id"

    async def validate(self, token: str, *, request_path: str = "/mcp", request_method: str = "POST") -> dict:
        return {}


def _create_server(entra_client_id="test-client-id", entra_tenant_id="test-tenant-id"):
    """Helper to create a CodeExecutionServer for testing with Entra auth."""
    config = ServerConfig(name="test", type="uv", description="Test", dependency_file="# Test")
    auth_config = create_entra_auth_config(client_id=entra_client_id, tenant_id=entra_tenant_id)
    return CodeExecutionServer(
        server_config=config,
        auth_config=auth_config,
    )


def _create_server_with_noop_auth():
    """Helper to create a CodeExecutionServer using no-op auth_config."""
    config = ServerConfig(name="test", type="uv", description="Test", dependency_file="# Test")
    return CodeExecutionServer(
        server_config=config,
        auth_config=create_noop_auth_config(),
    )


def _create_test_app(server):
    """Build a Starlette app with custom endpoints but no auth middleware."""
    app = server.mcp.http_app(transport="streamable-http")
    server._add_custom_endpoints(app)
    return app


def _create_test_app_with_auth(server):
    """Build a Starlette app with auth middleware (for testing 401 responses)."""
    app = server.mcp.http_app(transport="streamable-http")
    server._add_custom_endpoints(app)
    for middleware_cls, middleware_kwargs in server._create_middleware():
        app.add_middleware(middleware_cls, **middleware_kwargs)
    return app


class TestProtectedResourceMetadata:
    """Test /.well-known/oauth-protected-resource endpoint."""

    def test_returns_200(self):
        """Metadata endpoint should return 200 without authentication."""
        server = _create_server()
        client = TestClient(_create_test_app(server))

        response = client.get("/.well-known/oauth-protected-resource")
        assert response.status_code == 200

    def test_returns_correct_content_type(self):
        """Metadata should be returned as JSON."""
        server = _create_server()
        client = TestClient(_create_test_app(server))

        response = client.get("/.well-known/oauth-protected-resource")
        assert "application/json" in response.headers["content-type"]

    def test_contains_required_fields(self):
        """Metadata must include resource, authorization_servers, and bearer_methods_supported."""
        server = _create_server()
        client = TestClient(_create_test_app(server))

        data = client.get("/.well-known/oauth-protected-resource").json()
        assert "resource" in data
        assert "authorization_servers" in data
        assert "bearer_methods_supported" in data

    def test_resource_is_entra_identifier_uri(self):
        """Resource URI should be the Entra app identifier URI (for RFC 8707 audience binding)."""
        with patch.dict("os.environ", {"ENTRA_CLIENT_ID": "my-app-id", "ENTRA_TENANT_ID": "test-tenant-id"}):
            server = _create_server(entra_client_id="my-app-id")
        client = TestClient(_create_test_app(server))

        data = client.get("/.well-known/oauth-protected-resource").json()
        assert data["resource"] == "api://my-app-id"

    def test_authorization_server_uses_tenant_id(self):
        """Authorization server URL should include the configured tenant ID."""
        with patch.dict("os.environ", {"ENTRA_CLIENT_ID": "test-client-id", "ENTRA_TENANT_ID": "my-tenant-123"}):
            server = _create_server(entra_tenant_id="my-tenant-123")
        client = TestClient(_create_test_app(server))

        data = client.get("/.well-known/oauth-protected-resource").json()
        assert len(data["authorization_servers"]) == 1
        assert "my-tenant-123" in data["authorization_servers"][0]
        assert data["authorization_servers"][0] == ("https://login.microsoftonline.com/my-tenant-123/v2.0")

    def test_scopes_include_client_id(self):
        """Scopes should reference the configured client ID."""
        with patch.dict("os.environ", {"ENTRA_CLIENT_ID": "my-app-id", "ENTRA_TENANT_ID": "test-tenant-id"}):
            server = _create_server(entra_client_id="my-app-id")
        client = TestClient(_create_test_app(server))

        data = client.get("/.well-known/oauth-protected-resource").json()
        assert "scopes_supported" in data
        assert "api://my-app-id/.default" in data["scopes_supported"]

    def test_bearer_methods_header_only(self):
        """Only 'header' bearer method should be supported."""
        server = _create_server()
        client = TestClient(_create_test_app(server))

        data = client.get("/.well-known/oauth-protected-resource").json()
        assert data["bearer_methods_supported"] == ["header"]

    def test_no_auth_required_with_middleware(self):
        """Metadata endpoint should bypass auth middleware."""
        server = _create_server()
        client = TestClient(_create_test_app_with_auth(server))

        response = client.get("/.well-known/oauth-protected-resource")
        assert response.status_code == 200


class TestWWWAuthenticateHeader:
    """Test that 401 responses include WWW-Authenticate per RFC 9728."""

    def test_missing_token_returns_www_authenticate(self):
        """401 for missing token should include WWW-Authenticate header."""
        server = _create_server()
        client = TestClient(_create_test_app_with_auth(server))

        response = client.post("/mcp")
        assert response.status_code == 401
        assert "www-authenticate" in response.headers

    def test_www_authenticate_contains_resource_metadata(self):
        """WWW-Authenticate should point to the resource metadata URL."""
        server = _create_server()
        client = TestClient(_create_test_app_with_auth(server))

        response = client.post("/mcp")
        www_auth = response.headers["www-authenticate"]
        assert www_auth.startswith("Bearer ")
        assert "resource_metadata=" in www_auth
        assert "/.well-known/oauth-protected-resource" in www_auth

    def test_invalid_token_returns_www_authenticate(self):
        """401 for invalid token should also include WWW-Authenticate header."""
        server = _create_server()
        client = TestClient(_create_test_app_with_auth(server))

        from fastapi import HTTPException

        async def mock_validate(*args, **kwargs):
            raise HTTPException(status_code=401, detail="Token expired")

        server.validate_token = mock_validate

        response = client.post(
            "/mcp",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert response.status_code == 401
        assert "www-authenticate" in response.headers

    def test_forbidden_does_not_include_www_authenticate(self):
        """403 responses should NOT include WWW-Authenticate."""
        server = _create_server()
        client = TestClient(_create_test_app_with_auth(server))

        from fastapi import HTTPException

        async def mock_validate(*args, **kwargs):
            raise HTTPException(status_code=403, detail="Forbidden")

        server.validate_token = mock_validate

        response = client.post(
            "/mcp",
            headers={"Authorization": "Bearer some-token"},
        )
        assert response.status_code == 403
        assert "www-authenticate" not in response.headers

    def test_health_endpoint_unaffected(self):
        """Health check should still return 200 with no auth."""
        server = _create_server()
        client = TestClient(_create_test_app_with_auth(server))

        response = client.get("/health")
        assert response.status_code == 200

    def test_healthz_endpoint_unaffected(self):
        """Kubernetes-style /healthz should also return 200 with no auth."""
        server = _create_server()
        client = TestClient(_create_test_app_with_auth(server))

        response = client.get("/healthz")
        assert response.status_code == 200


class TestProtectedResourceMetadataWithAuthConfig:
    """Test metadata endpoint when a pluggable auth_config is used instead of Entra params."""

    def test_returns_minimal_metadata_when_noop_auth(self, monkeypatch):
        """Metadata endpoint should return minimal valid metadata in noop auth mode.

        Agents that perform OAuth discovery (e.g. gh cli) need a valid response
        rather than a 404 to avoid erroring during their discovery flow.
        """
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)

        server = _create_server_with_noop_auth()
        client = TestClient(_create_test_app(server))

        response = client.get("/.well-known/oauth-protected-resource")
        assert response.status_code == 200
        data = response.json()
        assert "resource" in data
        assert data["authorization_servers"] == []
        assert data["bearer_methods_supported"] == ["header"]

    def test_returns_200_when_entra_env_vars_set(self, monkeypatch):
        """Metadata endpoint should work when ENTRA env vars are set alongside noop auth."""
        monkeypatch.setenv("ENTRA_CLIENT_ID", "my-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "my-tenant-id")

        server = _create_server_with_noop_auth()
        client = TestClient(_create_test_app(server))

        response = client.get("/.well-known/oauth-protected-resource")
        assert response.status_code == 200
        data = response.json()
        assert data["resource"] == "api://my-client-id"
        assert "my-tenant-id" in data["authorization_servers"][0]

    def test_custom_validator_without_tenant_metadata_does_not_crash(self):
        config = ServerConfig(
            name="test",
            type="uv",
            description="Test",
            dependency_file="# Test",
            entra_tenant_id="fallback-tenant-id",
        )
        auth_config = AuthConfig(
            token_validator=PartialTokenValidator(),
            identity_extractor=NoOpIdentityExtractor(),
        )

        server = CodeExecutionServer(server_config=config, auth_config=auth_config)

        assert server.entra_client_id == "partial-client-id"
        assert server.entra_tenant_id == "fallback-tenant-id"

    def test_noop_auth_allows_missing_authorization_header(self):
        """No-op auth mode should allow requests without Authorization header."""
        server = _create_server_with_noop_auth()
        client = TestClient(_create_test_app_with_auth(server))

        response = client.post("/object-transfer/receive")
        assert response.status_code == 400
        assert response.json() == {"success": False, "error": "Invalid JSON body"}
        assert "www-authenticate" not in response.headers

    def test_noop_auth_allows_invalid_bearer_token(self):
        """No-op auth mode should also allow malformed or opaque bearer tokens."""
        server = _create_server_with_noop_auth()
        client = TestClient(_create_test_app_with_auth(server))

        response = client.post("/object-transfer/receive", headers={"Authorization": "Bearer " + "opaque-token"})
        assert response.status_code == 400
        assert response.json() == {"success": False, "error": "Invalid JSON body"}
        assert "www-authenticate" not in response.headers


class CustomIdPValidator(TokenValidator):
    """Validator for a non-Entra identity provider."""

    async def validate(self, token: str, *, request_path: str = "/mcp", request_method: str = "POST") -> dict:
        return {}


CUSTOM_METADATA = {
    "resource": "https://api.example.com",
    "authorization_servers": ["https://auth.example.com/realms/main"],
    "scopes_supported": ["read", "write"],
    "bearer_methods_supported": ["header"],
}

WARNING_FRAGMENT = "OAuth protected-resource metadata is unresolvable"


def _server_with_auth_config(auth_config):
    config = ServerConfig(name="test", type="uv", description="Test", dependency_file="# Test")
    return CodeExecutionServer(server_config=config, auth_config=auth_config)


def _custom_idp_auth_config(metadata=None):
    return AuthConfig(
        token_validator=CustomIdPValidator(),
        identity_extractor=NoOpIdentityExtractor(),
        protected_resource_metadata=metadata,
    )


class TestProtectedResourceMetadataFromAuthConfig:
    """Metadata published through the public AuthConfig contract (RFC 9728)."""

    def test_custom_idp_metadata_served_verbatim(self, monkeypatch):
        """A non-Entra provider can describe itself without server-side knowledge."""
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)

        server = _server_with_auth_config(_custom_idp_auth_config(CUSTOM_METADATA))
        client = TestClient(_create_test_app(server))

        response = client.get("/.well-known/oauth-protected-resource")

        assert response.status_code == 200
        assert response.json() == CUSTOM_METADATA

    def test_configured_metadata_takes_precedence_over_entra_ids(self, monkeypatch):
        """Explicit auth-config metadata wins over ambient ENTRA_* environment values."""
        monkeypatch.setenv("ENTRA_CLIENT_ID", "env-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "env-tenant-id")

        server = _server_with_auth_config(_custom_idp_auth_config(CUSTOM_METADATA))
        client = TestClient(_create_test_app(server))

        data = client.get("/.well-known/oauth-protected-resource").json()

        assert data == CUSTOM_METADATA
        assert "env-client-id" not in str(data)

    def test_entra_factory_populates_metadata(self):
        """create_entra_auth_config publishes metadata instead of relying on private attrs."""
        auth_config = create_entra_auth_config(client_id="factory-client", tenant_id="factory-tenant")

        metadata = auth_config.protected_resource_metadata

        assert metadata is not None
        assert metadata["resource"] == "api://factory-client"
        assert metadata["authorization_servers"] == ["https://login.microsoftonline.com/factory-tenant/v2.0"]
        assert metadata["scopes_supported"] == ["api://factory-client/.default"]

    def test_entra_document_unchanged_from_legacy_path(self):
        """The served Entra document must be identical to the previously composed one."""
        server = _create_server(entra_client_id="same-client", entra_tenant_id="same-tenant")
        client = TestClient(_create_test_app(server))

        data = client.get("/.well-known/oauth-protected-resource").json()

        assert data == {
            "resource": "api://same-client",
            "authorization_servers": ["https://login.microsoftonline.com/same-tenant/v2.0"],
            "scopes_supported": ["api://same-client/.default"],
            "bearer_methods_supported": ["header"],
        }

    def test_legacy_private_attr_probe_is_symmetric(self, monkeypatch):
        """A validator exposing only _tenant_id still contributes it."""
        monkeypatch.setenv("ENTRA_CLIENT_ID", "env-client-id")
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)

        class TenantOnlyValidator(TokenValidator):
            def __init__(self) -> None:
                self._tenant_id = "tenant-only"

            async def validate(self, token: str, *, request_path: str = "/mcp", request_method: str = "POST") -> dict:
                return {}

        auth_config = AuthConfig(
            token_validator=TenantOnlyValidator(),
            identity_extractor=NoOpIdentityExtractor(),
        )

        server = _server_with_auth_config(auth_config)

        assert server.entra_tenant_id == "tenant-only"
        assert server.entra_client_id == "env-client-id"


class TestUnresolvableMetadataWarning:
    """Startup diagnostics for the previously silent OAuth discovery failure."""

    def test_warns_when_metadata_unresolvable(self, monkeypatch, caplog):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)

        with caplog.at_level(logging.WARNING, logger="agora_workbench.base"):
            _server_with_auth_config(_custom_idp_auth_config())

        assert WARNING_FRAGMENT in caplog.text

    def test_no_warning_when_metadata_configured(self, monkeypatch, caplog):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)

        with caplog.at_level(logging.WARNING, logger="agora_workbench.base"):
            _server_with_auth_config(_custom_idp_auth_config(CUSTOM_METADATA))

        assert WARNING_FRAGMENT not in caplog.text

    def test_no_warning_when_entra_ids_resolvable(self, caplog):
        with caplog.at_level(logging.WARNING, logger="agora_workbench.base"):
            _create_server()

        assert WARNING_FRAGMENT not in caplog.text

    def test_no_warning_when_authorization_not_required(self, monkeypatch, caplog):
        """Noop auth intentionally serves a minimal document, so it must stay quiet."""
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)

        with caplog.at_level(logging.WARNING, logger="agora_workbench.base"):
            _create_server_with_noop_auth()

        assert WARNING_FRAGMENT not in caplog.text
