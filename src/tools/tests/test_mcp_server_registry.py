"""Tests for MCP Server Registry."""

import pytest
import httpx
from unittest.mock import MagicMock, patch, AsyncMock

from tools.mcp import (
    MCPServerDescriptor,
    MCPServerRegistry,
    get_mcp_registry,
    extract_packages_from_dependency_file,
    create_mcp_descriptor_from_config,
)


class TestPackageExtraction:
    """Test cases for extract_packages_from_dependency_file."""

    @pytest.mark.unit
    def test_extract_simple_packages(self):
        """Test extracting simple package names."""
        dependency_file = """
pandas
numpy
matplotlib
"""
        packages = extract_packages_from_dependency_file(dependency_file)

        assert len(packages) == 3
        assert "pandas" in packages
        assert "numpy" in packages
        assert "matplotlib" in packages

    @pytest.mark.unit
    def test_extract_packages_with_versions(self):
        """Test extracting packages with version specifiers."""
        dependency_file = """
pandas>=2.0.0
numpy==1.24.0
matplotlib~=3.7.0
scipy>1.10.0
requests<=2.31.0
"""
        packages = extract_packages_from_dependency_file(dependency_file)

        assert len(packages) == 5
        assert "pandas>=2.0.0" in packages
        assert "numpy==1.24.0" in packages
        assert "matplotlib~=3.7.0" in packages
        assert "scipy>1.10.0" in packages
        assert "requests<=2.31.0" in packages

    @pytest.mark.unit
    def test_extract_packages_with_extras(self):
        """Test extracting packages with extras."""
        dependency_file = """
requests[security]>=2.31.0
pandas[performance,computation]>=2.0.0
"""
        packages = extract_packages_from_dependency_file(dependency_file)

        assert len(packages) == 2
        assert "requests[security]>=2.31.0" in packages
        assert "pandas[performance,computation]>=2.0.0" in packages

    @pytest.mark.unit
    def test_extract_packages_with_comments(self):
        """Test that comments are ignored."""
        dependency_file = """
# This is a comment
pandas>=2.0.0
# Another comment
numpy>=1.24.0
"""
        packages = extract_packages_from_dependency_file(dependency_file)

        assert len(packages) == 2
        assert "pandas>=2.0.0" in packages
        assert "numpy>=1.24.0" in packages

    @pytest.mark.unit
    def test_extract_packages_with_blank_lines(self):
        """Test that blank lines are ignored."""
        dependency_file = """
pandas>=2.0.0

numpy>=1.24.0


matplotlib>=3.7.0
"""
        packages = extract_packages_from_dependency_file(dependency_file)

        assert len(packages) == 3
        assert "pandas>=2.0.0" in packages
        assert "numpy>=1.24.0" in packages
        assert "matplotlib>=3.7.0" in packages

    @pytest.mark.unit
    def test_extract_packages_with_hyphens_underscores(self):
        """Test packages with hyphens and underscores in names."""
        dependency_file = """
scikit-learn>=1.3.0
python_dateutil>=2.8.0
azure-identity>=1.14.0
"""
        packages = extract_packages_from_dependency_file(dependency_file)

        assert len(packages) == 3
        assert "scikit-learn>=1.3.0" in packages
        assert "python_dateutil>=2.8.0" in packages
        assert "azure-identity>=1.14.0" in packages

    @pytest.mark.unit
    def test_extract_empty_dependency_file(self):
        """Test empty dependency file returns empty list."""
        dependency_file = ""
        packages = extract_packages_from_dependency_file(dependency_file)

        assert packages == []

    @pytest.mark.unit
    def test_extract_packages_only_comments(self):
        """Test dependency file with only comments."""
        dependency_file = """
# Comment 1
# Comment 2
# Comment 3
"""
        packages = extract_packages_from_dependency_file(dependency_file)

        assert packages == []


class TestMCPDescriptorCreation:
    """Test cases for create_mcp_descriptor_from_config."""

    @pytest.mark.unit
    def test_create_descriptor_basic(self):
        """Test creating descriptor with required values."""

        # Create a mock EnvironmentConfig-like object
        class MockConfig:
            name = "test_env"
            description = "Test environment"
            type = "uv"
            dependency_file = "pandas>=2.0.0\nnumpy>=1.24.0"

        config = MockConfig()

        descriptor = create_mcp_descriptor_from_config(
            env_config=config,
            name="test_server",
            port=8001,
            base_url="http://test.example.com",
            scope="https://test.scope/.default",
        )

        assert descriptor.name == "test_server"
        assert descriptor.url == "http://test.example.com:8001/mcp"
        assert descriptor.description == "Test environment"
        assert descriptor.environment_type == "uv"
        assert descriptor.scope == "https://test.scope/.default"
        assert descriptor.packages is not None
        assert len(descriptor.packages) == 2
        assert "pandas>=2.0.0" in descriptor.packages
        assert "numpy>=1.24.0" in descriptor.packages

    @pytest.mark.unit
    def test_create_descriptor_extracts_packages(self):
        """Test that packages are correctly extracted from dependency file."""

        class MockConfig:
            name = "test"
            description = "Test"
            type = "pip"
            dependency_file = """
# Common packages
pandas>=2.0.0
numpy==1.24.0

# Scientific computing
scipy>=1.10.0
"""

        config = MockConfig()

        descriptor = create_mcp_descriptor_from_config(
            env_config=config,
            name="pkg_test",
            port=8000,
            base_url="http://localhost",
            scope="https://test.scope/.default",
        )

        assert descriptor.packages is not None
        assert len(descriptor.packages) == 3
        assert "pandas>=2.0.0" in descriptor.packages
        assert "numpy==1.24.0" in descriptor.packages
        assert "scipy>=1.10.0" in descriptor.packages

    @pytest.mark.unit
    def test_create_descriptor_with_conda_type(self):
        """Test creating descriptor for conda environment."""

        class MockConfig:
            name = "conda_env"
            description = "Conda environment"
            type = "conda"
            dependency_file = "name: test\nchannels:\n  - conda-forge\ndependencies:\n  - python=3.11\n  - numpy"

        config = MockConfig()

        descriptor = create_mcp_descriptor_from_config(
            env_config=config,
            name="conda_server",
            port=8002,
            base_url="http://localhost",
            scope="https://test.scope/.default",
        )

        assert descriptor.name == "conda_server"
        assert descriptor.environment_type == "conda"


class TestMCPServerDescriptor:
    """Test cases for MCPServerDescriptor."""

    @pytest.mark.unit
    def test_descriptor_initialization(self):
        """Test basic descriptor creation."""
        descriptor = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:8000/mcp",
            description="Test server",
            packages=["numpy>=1.0", "pandas>=2.0"],
            environment_type="uv",
            scope="https://cognitiveservices.azure.com/.default",
        )

        assert descriptor.name == "test_server"
        assert descriptor.url == "http://localhost:8000/mcp"
        assert descriptor.description == "Test server"
        assert descriptor.packages is not None
        assert len(descriptor.packages) == 2
        assert descriptor.environment_type == "uv"
        assert descriptor.scope == "https://cognitiveservices.azure.com/.default"

    @pytest.mark.unit
    def test_descriptor_minimal(self):
        """Test descriptor with minimal required fields."""
        descriptor = MCPServerDescriptor(
            name="minimal_server",
            url="http://localhost:8001/mcp",
            description="Minimal test",
            scope="https://test.scope/.default",
        )

        assert descriptor.name == "minimal_server"
        assert descriptor.packages == []
        assert descriptor.environment_type is None
        assert descriptor.scope == "https://test.scope/.default"


class TestMCPServerRegistry:
    """Test cases for MCPServerRegistry."""

    @pytest.mark.unit
    def test_registry_initialization(self):
        """Test registry starts empty."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()
        assert len(registry.list_servers()) == 0

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_register_server(self):
        """Test registering a server."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()
        descriptor = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:8000/mcp",
            description="Test server",
            scope="https://test.scope/.default",
        )

        # Mock validation to always succeed
        with patch.object(registry, "_validate_server_connection", return_value=(True, "")):
            await registry.register(descriptor)

        assert registry.has_server("test_server")
        assert len(registry.list_servers()) == 1

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_register_duplicate_server_updates(self):
        """Test that registering duplicate server name updates it."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()

        descriptor1 = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:8000/mcp",
            description="First version",
            scope="https://test.scope/.default",
        )
        with patch.object(registry, "_validate_server_connection", return_value=(True, "")):
            await registry.register(descriptor1)

        descriptor2 = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:9000/mcp",
            description="Second version",
            scope="https://test.scope/.default",
        )
        with patch.object(registry, "_validate_server_connection", return_value=(True, "")):
            await registry.register(descriptor2)

        servers = registry.list_servers()
        assert len(servers) == 1
        assert servers["test_server"].url == "http://localhost:9000/mcp"
        assert servers["test_server"].description == "Second version"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_register_multiple_servers(self):
        """Test registering multiple different servers."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()

        descriptors = [
            MCPServerDescriptor(
                name=f"server_{i}",
                url=f"http://localhost:{8000 + i}/mcp",
                description=f"Server {i}",
                scope="https://test.scope/.default",
            )
            for i in range(3)
        ]

        for desc in descriptors:
            with patch.object(registry, "_validate_server_connection", return_value=(True, "")):
                await registry.register(desc)

        assert len(registry.list_servers()) == 3
        assert registry.has_server("server_0")
        assert registry.has_server("server_1")
        assert registry.has_server("server_2")

    @pytest.mark.unit
    def test_has_server_false_for_nonexistent(self):
        """Test has_server returns False for nonexistent server."""
        registry = MCPServerRegistry()
        assert not registry.has_server("nonexistent_server")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_list_servers_returns_dict(self):
        """Test list_servers returns dictionary."""
        registry = MCPServerRegistry()
        descriptor = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:8000/mcp",
            description="Test",
            scope="https://test.scope/.default",
        )
        with patch.object(registry, "_validate_server_connection", return_value=(True, "")):
            await registry.register(descriptor)

        servers = registry.list_servers()
        assert isinstance(servers, dict)
        assert "test_server" in servers
        assert isinstance(servers["test_server"], MCPServerDescriptor)

    @pytest.mark.unit
    def test_get_mcp_registry_singleton(self):
        """Test get_mcp_registry returns same instance."""
        registry1 = get_mcp_registry()
        registry2 = get_mcp_registry()
        assert registry1 is registry2

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_registry_packages_preserved(self):
        """Test that package information is preserved."""
        registry = MCPServerRegistry()
        packages = ["numpy>=1.26.0", "pandas>=2.2.2", "pypsa>=0.30.2"]

        descriptor = MCPServerDescriptor(
            name="pkg_server",
            url="http://localhost:8000/mcp",
            description="Server with packages",
            packages=packages,
            environment_type="uv",
            scope="https://test.scope/.default",
        )
        with patch.object(registry, "_validate_server_connection", return_value=(True, "")):
            await registry.register(descriptor)

        retrieved = registry.list_servers()["pkg_server"]
        assert retrieved.packages == packages
        assert retrieved.environment_type == "uv"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_aclose_closes_http_clients(self):
        """Test that aclose() calls .aclose() on all HTTP clients."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()

        # Create a mock HTTP client
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()

        registry._http_clients = [mock_client]

        await registry.aclose()

        # Assert HTTP client was closed
        mock_client.aclose.assert_awaited_once()
        # Assert lists are cleared
        assert registry._http_clients == []


class TestMCPServerRegistryIntegration:
    """Integration tests for MCP Server Registry with other components."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_registry_server_lookup(self):
        """Test that MCP registry correctly tracks registered servers."""
        registry = get_mcp_registry()
        # Clear registry for clean test
        registry._servers.clear()

        # Register a server
        with patch.object(registry, "_validate_server_connection", return_value=(True, "")):
            await registry.register(
                MCPServerDescriptor(
                    name="valid_server",
                    url="http://localhost:8000/mcp",
                    description="Valid server",
                    scope="https://test.scope/.default",
                )
            )

        # Should find registered server
        assert registry.has_server("valid_server")

        # Should not find unregistered server
        assert not registry.has_server("nonexistent_server")

    @pytest.mark.integration
    def test_registry_autodiscovery_from_yaml(self):
        """Test that auto-discovery loads servers from YAML configuration."""
        from pathlib import Path

        # Check that the YAML file exists
        # Path: core/tests/tools/test_mcp_server_registry.py -> workspace root
        yaml_path = Path(__file__).parent.parent.parent.parent / "server_registry.yaml"
        if not yaml_path.exists():
            pytest.skip(f"server_registry.yaml not found at {yaml_path}")

        # Create a fresh registry and trigger auto-discovery
        registry = MCPServerRegistry()
        registry.enable_auto_discovery()

        # Trigger auto-discovery by calling list_servers
        with patch.object(registry, "_validate_server_connection", new=AsyncMock(return_value=(True, ""))):
            servers = registry.list_servers()

        # Should have registered servers from YAML
        assert len(servers) >= 1, "Expected at least one server to be registered from YAML"

        # Verify example server is present
        if "general" in servers:
            # Verify the descriptor has required fields
            descriptor = servers["general"]
            assert descriptor.name == "general"
            assert descriptor.url is not None
            assert descriptor.description is not None
            assert descriptor.scope is not None


class TestServerValidation:
    """Test cases for server validation functionality."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_successful_validation(self):
        """Test successful validation when server is healthy and auth works."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()

        descriptor = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:8000/mcp",
            description="Test server",
            scope="https://test.scope/.default",
        )

        # Mock health check response
        mock_health_response = MagicMock()
        mock_health_response.status_code = 200
        mock_health_response.json.return_value = {"status": "healthy", "environment": "test"}

        # Mock auth check response
        mock_auth_response = MagicMock()
        mock_auth_response.status_code = 200

        # Mock httpx.AsyncClient
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.return_value = mock_health_response
        mock_client.options.return_value = mock_auth_response

        with (
            patch("tools.mcp.mcp_server_registry.httpx.AsyncClient", return_value=mock_client),
            patch("tools.mcp.mcp_server_registry.create_entra_token_provider", return_value=lambda: "test_token"),
        ):
            is_valid, error_msg = await registry._validate_server_connection(descriptor)

        assert is_valid is True
        assert error_msg == ""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_health_check_non_200_status(self):
        """Test validation failure when health endpoint returns non-200 status."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()

        descriptor = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:8000/mcp",
            description="Test server",
            scope="https://test.scope/.default",
        )

        # Mock health check with 500 error
        mock_health_response = MagicMock()
        mock_health_response.status_code = 500

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.return_value = mock_health_response

        with patch("tools.mcp.mcp_server_registry.httpx.AsyncClient", return_value=mock_client):
            is_valid, error_msg = await registry._validate_server_connection(descriptor)

        assert is_valid is False
        assert "Health check failed with status 500" in error_msg

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_health_check_unhealthy_status(self):
        """Test validation failure when health endpoint returns unhealthy status."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()

        descriptor = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:8000/mcp",
            description="Test server",
            scope="https://test.scope/.default",
        )

        # Mock health check with unhealthy status
        mock_health_response = MagicMock()
        mock_health_response.status_code = 200
        mock_health_response.json.return_value = {"status": "degraded", "error": "Database connection failed"}

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.return_value = mock_health_response

        with patch("tools.mcp.mcp_server_registry.httpx.AsyncClient", return_value=mock_client):
            is_valid, error_msg = await registry._validate_server_connection(descriptor)

        assert is_valid is False
        assert "Server reports unhealthy status" in error_msg
        assert "degraded" in error_msg

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_connection_error(self):
        """Test validation failure on connection error."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()

        descriptor = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:8000/mcp",
            description="Test server",
            scope="https://test.scope/.default",
        )

        # Mock connection error
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.side_effect = httpx.ConnectError("Connection refused")

        with patch("tools.mcp.mcp_server_registry.httpx.AsyncClient", return_value=mock_client):
            is_valid, error_msg = await registry._validate_server_connection(descriptor)

        assert is_valid is False
        assert "Cannot connect to server" in error_msg

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_connection_timeout(self):
        """Test validation failure on connection timeout."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()

        descriptor = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:8000/mcp",
            description="Test server",
            scope="https://test.scope/.default",
        )

        # Mock timeout error
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.side_effect = httpx.TimeoutException("Request timeout")

        with patch("tools.mcp.mcp_server_registry.httpx.AsyncClient", return_value=mock_client):
            is_valid, error_msg = await registry._validate_server_connection(descriptor)

        assert is_valid is False
        assert "Connection timeout" in error_msg

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_auth_failure_401(self):
        """Test validation failure on authentication error (401)."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()

        descriptor = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:8000/mcp",
            description="Test server",
            scope="https://test.scope/.default",
        )

        # Mock successful health check
        mock_health_response = MagicMock()
        mock_health_response.status_code = 200
        mock_health_response.json.return_value = {"status": "healthy"}

        # Mock auth failure
        mock_auth_response = MagicMock()
        mock_auth_response.status_code = 401

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.return_value = mock_health_response
        mock_client.options.return_value = mock_auth_response

        with (
            patch("tools.mcp.mcp_server_registry.httpx.AsyncClient", return_value=mock_client),
            patch("tools.mcp.mcp_server_registry.create_entra_token_provider", return_value=lambda: "test_token"),
        ):
            is_valid, error_msg = await registry._validate_server_connection(descriptor)

        assert is_valid is False
        assert "Authentication failed: Invalid or expired token" in error_msg

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_auth_failure_403(self):
        """Test validation failure on insufficient permissions (403)."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()

        descriptor = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:8000/mcp",
            description="Test server",
            scope="https://test.scope/.default",
        )

        # Mock successful health check
        mock_health_response = MagicMock()
        mock_health_response.status_code = 200
        mock_health_response.json.return_value = {"status": "healthy"}

        # Mock permission denied
        mock_auth_response = MagicMock()
        mock_auth_response.status_code = 403

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.return_value = mock_health_response
        mock_client.options.return_value = mock_auth_response

        with (
            patch("tools.mcp.mcp_server_registry.httpx.AsyncClient", return_value=mock_client),
            patch("tools.mcp.mcp_server_registry.create_entra_token_provider", return_value=lambda: "test_token"),
        ):
            is_valid, error_msg = await registry._validate_server_connection(descriptor)

        assert is_valid is False
        assert "Authentication failed: Insufficient permissions" in error_msg

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_url_parsing_with_mcp_suffix(self):
        """Test that /mcp suffix is correctly removed for health check URL."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()

        descriptor = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:8000/mcp",
            description="Test server",
            scope="https://test.scope/.default",
        )

        mock_health_response = MagicMock()
        mock_health_response.status_code = 200
        mock_health_response.json.return_value = {"status": "healthy"}

        mock_auth_response = MagicMock()
        mock_auth_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.return_value = mock_health_response
        mock_client.options.return_value = mock_auth_response

        with (
            patch("tools.mcp.mcp_server_registry.httpx.AsyncClient", return_value=mock_client),
            patch("tools.mcp.mcp_server_registry.create_entra_token_provider", return_value=lambda: "test_token"),
        ):
            await registry._validate_server_connection(descriptor)

        # Verify health endpoint was called with correct URL (without /mcp suffix)
        mock_client.get.assert_called_once_with("http://localhost:8000/health")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_auth_check_with_bearer_token(self):
        """Test that authentication check includes Bearer token in header."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()

        descriptor = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:8000/mcp",
            description="Test server",
            scope="https://test.scope/.default",
        )

        mock_health_response = MagicMock()
        mock_health_response.status_code = 200
        mock_health_response.json.return_value = {"status": "healthy"}

        mock_auth_response = MagicMock()
        mock_auth_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.return_value = mock_health_response
        mock_client.options.return_value = mock_auth_response

        test_token = "test_bearer_token_12345"

        with (
            patch("tools.mcp.mcp_server_registry.httpx.AsyncClient", return_value=mock_client),
            patch("tools.mcp.mcp_server_registry.create_entra_token_provider", return_value=lambda: test_token),
        ):
            await registry._validate_server_connection(descriptor)

        # Verify OPTIONS request was made with Bearer token
        mock_client.options.assert_called_once_with(
            "http://localhost:8000/mcp", headers={"Authorization": f"Bearer {test_token}"}
        )

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_validation_accepts_405_method_not_allowed(self):
        """Test that validation accepts 405 as valid auth response."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()

        descriptor = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:8000/mcp",
            description="Test server",
            scope="https://test.scope/.default",
        )

        mock_health_response = MagicMock()
        mock_health_response.status_code = 200
        mock_health_response.json.return_value = {"status": "healthy"}

        # Server doesn't support OPTIONS but auth is valid
        mock_auth_response = MagicMock()
        mock_auth_response.status_code = 405

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.return_value = mock_health_response
        mock_client.options.return_value = mock_auth_response

        with (
            patch("tools.mcp.mcp_server_registry.httpx.AsyncClient", return_value=mock_client),
            patch("tools.mcp.mcp_server_registry.create_entra_token_provider", return_value=lambda: "test_token"),
        ):
            is_valid, error_msg = await registry._validate_server_connection(descriptor)

        # 405 should be accepted as valid (auth worked, just method not supported)
        assert is_valid is True
        assert error_msg == ""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_exception_during_validation(self):
        """Test proper error handling for unexpected exceptions during validation."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()

        descriptor = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:8000/mcp",
            description="Test server",
            scope="https://test.scope/.default",
        )

        # Mock unexpected exception
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.side_effect = Exception("Unexpected error")

        with patch("tools.mcp.mcp_server_registry.httpx.AsyncClient", return_value=mock_client):
            is_valid, error_msg = await registry._validate_server_connection(descriptor)

        assert is_valid is False
        assert "Health check failed" in error_msg

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_register_calls_validation(self):
        """Test that register() calls validation and respects the result."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()

        descriptor = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:8000/mcp",
            description="Test server",
            scope="https://test.scope/.default",
        )

        # Mock successful validation
        with patch.object(registry, "_validate_server_connection", return_value=(True, "")):
            await registry.register(descriptor)

        # Server should be registered
        assert registry.has_server("test_server")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_register_skips_on_validation_failure(self):
        """Test that register() doesn't add server when validation fails."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()

        descriptor = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:8000/mcp",
            description="Test server",
            scope="https://test.scope/.default",
        )

        # Mock failed validation
        with patch.object(registry, "_validate_server_connection", return_value=(False, "Server unreachable")):
            await registry.register(descriptor)

        # Server should NOT be registered
        assert not registry.has_server("test_server")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_token_provider_creation_with_scope(self):
        """Test that token provider is created with correct scope."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()

        descriptor = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:8000/mcp",
            description="Test server",
            scope="https://custom.scope/.default",
        )

        mock_health_response = MagicMock()
        mock_health_response.status_code = 200
        mock_health_response.json.return_value = {"status": "healthy"}

        mock_auth_response = MagicMock()
        mock_auth_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.return_value = mock_health_response
        mock_client.options.return_value = mock_auth_response

        with (
            patch("tools.mcp.mcp_server_registry.httpx.AsyncClient", return_value=mock_client),
            patch("tools.mcp.mcp_server_registry.create_entra_token_provider") as mock_token_provider,
        ):
            mock_token_provider.return_value = lambda: "test_token"

            await registry._validate_server_connection(descriptor)

        # Verify token provider was created with correct scope
        mock_token_provider.assert_called_once_with("https://custom.scope/.default")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_timeout_configuration(self):
        """Test that HTTP clients are created with appropriate timeout settings."""
        registry = MCPServerRegistry()
        registry.disable_auto_discovery()

        descriptor = MCPServerDescriptor(
            name="test_server",
            url="http://localhost:8000/mcp",
            description="Test server",
            scope="https://test.scope/.default",
        )

        mock_health_response = MagicMock()
        mock_health_response.status_code = 200
        mock_health_response.json.return_value = {"status": "healthy"}

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.return_value = mock_health_response

        with (
            patch("tools.mcp.mcp_server_registry.httpx.AsyncClient") as mock_client_class,
            patch("tools.mcp.mcp_server_registry.create_entra_token_provider", return_value=lambda: "test_token"),
        ):
            mock_client_class.return_value = mock_client

            await registry._validate_server_connection(descriptor)

        # Verify AsyncClient was created with timeout
        assert mock_client_class.called
        call_kwargs = mock_client_class.call_args[1]
        assert "timeout" in call_kwargs
        timeout = call_kwargs["timeout"]
        assert timeout.connect == 5.0
        assert timeout.read == 5.0
