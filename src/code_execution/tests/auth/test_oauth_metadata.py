"""Tests for OAuth 2.0 Protected Resource Metadata (RFC 9728).

Validates that CodeExecutionServer correctly exposes the
/.well-known/oauth-protected-resource endpoint and includes
WWW-Authenticate headers on 401 responses for MCP OAuth discovery.
"""

from starlette.testclient import TestClient

from ...code_execution import CodeExecutionServer
from ...code_execution.auth.noop import create_noop_auth_config
from ...code_execution.code_execution_models import EnvironmentConfig


def _create_server(
    entra_client_id="test-client-id",
    entra_tenant_id="test-tenant-id",
):
    """Helper to create a CodeExecutionServer for testing."""
    config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
    return CodeExecutionServer(
        environment_config=config,
        entra_client_id=entra_client_id,
        entra_tenant_id=entra_tenant_id,
    )


def _create_server_with_auth_config():
    """Helper to create a CodeExecutionServer using a pluggable auth_config (no Entra params)."""
    config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
    return CodeExecutionServer(
        environment_config=config,
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
        server = _create_server(entra_client_id="my-app-id")
        client = TestClient(_create_test_app(server))

        data = client.get("/.well-known/oauth-protected-resource").json()
        assert data["resource"] == "api://my-app-id"

    def test_authorization_server_uses_tenant_id(self):
        """Authorization server URL should include the configured tenant ID."""
        server = _create_server(entra_tenant_id="my-tenant-123")
        client = TestClient(_create_test_app(server))

        data = client.get("/.well-known/oauth-protected-resource").json()
        assert len(data["authorization_servers"]) == 1
        assert "my-tenant-123" in data["authorization_servers"][0]
        assert data["authorization_servers"][0] == ("https://login.microsoftonline.com/my-tenant-123/v2.0")

    def test_scopes_include_client_id(self):
        """Scopes should reference the configured client ID."""
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

        # Mock verify_entra_token to raise 401
        from fastapi import HTTPException

        async def mock_verify(*args, **kwargs):
            raise HTTPException(status_code=401, detail="Token expired")

        server.verify_entra_token = mock_verify

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

        async def mock_verify(*args, **kwargs):
            raise HTTPException(status_code=403, detail="Forbidden")

        server.verify_entra_token = mock_verify

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


class TestProtectedResourceMetadataWithAuthConfig:
    """Test metadata endpoint when a pluggable auth_config is used instead of Entra params."""

    def test_returns_404_when_no_entra_params(self, monkeypatch):
        """Metadata endpoint should return 404 when no Entra client/tenant IDs are available."""
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)

        server = _create_server_with_auth_config()
        client = TestClient(_create_test_app(server))

        response = client.get("/.well-known/oauth-protected-resource")
        assert response.status_code == 404

    def test_returns_200_when_entra_params_also_provided(self):
        """Metadata endpoint should still work when auth_config AND Entra params are supplied."""
        config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
        server = CodeExecutionServer(
            environment_config=config,
            auth_config=create_noop_auth_config(),
            entra_client_id="my-client-id",
            entra_tenant_id="my-tenant-id",
        )
        client = TestClient(_create_test_app(server))

        response = client.get("/.well-known/oauth-protected-resource")
        assert response.status_code == 200
        data = response.json()
        assert data["resource"] == "api://my-client-id"
        assert "my-tenant-id" in data["authorization_servers"][0]
