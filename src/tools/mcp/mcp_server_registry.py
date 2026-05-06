"""
MCP Server Registry for dynamic server discovery and management.

This registry allows code execution servers to be registered and discovered without
creating dependencies between core and code_execution modules. Servers can be
registered imperatively or discovered automatically.

Architecture:
- Core module defines the registry
- Code execution module registers servers at runtime
- Agent queries registry for available servers

Token Lifetime:
- MCP sessions are tied to client token lifetime (~1 hour)
- Client is responsible for providing fresh tokens with each request
- When tokens expire, client should create new sessions with fresh tokens
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Callable, Any, List
from functools import lru_cache

import httpx
import yaml
from pydantic import BaseModel, Field

from auth import get_token_provider

LOGGER = logging.getLogger(__name__)

# Default scope for MCP server authentication
DEFAULT_MCP_SERVER_SCOPE: Optional[str] = os.getenv("MCP_SERVER_SCOPE")


class MCPServerConfig(BaseModel):
    """Configuration for an MCP server registration (YAML-expressable).

    This structure defines the expected schema for server registration
    and can be serialized to/from YAML configuration files.

    Attributes:
        name: Unique name for the MCP server (e.g., \"example\", \"powergrid\")
        module: Python module path containing the config function
                (e.g., \"domains.example.server.example_server\")
        config_function: Name of the function that creates EnvironmentConfig
                         (e.g., \"create_example_config\")
        port: Port number where the server runs
        base_url: Base URL for the MCP servers (required)
        scope: Auth scope for authenticating to the MCP server.
               Required (can be set via MCP_SERVER_SCOPE env var in YAML defaults)

    Example YAML representation:
        base_url: http://localhost
        scope: api://your-app-id/user.connect
        servers:
          - name: example
            module: domains.example.server.example_server
            config_function: create_example_config
            port: 8000
          - name: powergrid
            module: domains.powergrid.server.powergrid_server
            config_function: create_powergrid_config
            port: 8001
    """

    name: str = Field(..., description="Unique name for the MCP server")
    module: str = Field(..., description="Python module path containing the config function")
    config_function: str = Field(..., description="Name of the function that creates EnvironmentConfig")
    port: int = Field(..., ge=1, le=65535, description="Port number where the server runs")
    base_url: str = Field(..., description="Base URL for the MCP servers")
    scope: str = Field(..., description="Auth scope for authenticating to the MCP server")

    model_config = {"frozen": True}


@dataclass
class MCPServerDescriptor:
    """Descriptor for an MCP code execution server.

    Contains metadata needed to connect to an MCP server.
    """

    name: str
    url: str
    description: str
    scope: str
    packages: Optional[list[str]] = None
    environment_type: Optional[str] = None
    factory: Optional[Callable[[], Any]] = None  # Optional factory to create server instance

    def __post_init__(self):
        """Initialize packages list if not provided."""
        if self.packages is None:
            self.packages = []


def extract_packages_from_dependency_file(dependency_file: str) -> list[str]:
    """
    Extract package names with versions from dependency file content.

    Args:
        dependency_file: Content of requirements.txt or environment.yml

    Returns:
        List of package specifications (e.g., ["pypsa>=0.30.2", "pandas>=2.0.0"])
    """
    packages = []

    for line in dependency_file.split("\n"):
        line = line.strip()

        # Skip empty lines and comments
        if not line or line.startswith("#"):
            continue

        # Extract package spec (handles: package>=1.0, package==1.0, package[extras]>=1.0)
        # Match: word characters, hyphens, underscores, optional [extras], optional version spec
        match = re.match(r"^([a-zA-Z0-9_\-]+(?:\[[^\]]+\])?(?:[><=!~]+[\d.]+)?)", line)
        if match:
            packages.append(match.group(1))

    return packages


def create_mcp_descriptor_from_config(
    env_config,
    name: str,
    port: int,
    base_url: str,
    scope: str,
) -> MCPServerDescriptor:
    """
    Create MCPServerDescriptor from EnvironmentConfig.

    This automatically extracts description, packages, and environment_type
    from the EnvironmentConfig to avoid duplication.

    Args:
        env_config: EnvironmentConfig instance with environment metadata
        name: Unique name for the MCP server
        port: Port number where the server runs
        base_url: Base URL for the server
        scope: Auth scope for authenticating to the MCP server

    Returns:
        MCPServerDescriptor ready to register
    """
    # Extract packages from dependency file
    packages = extract_packages_from_dependency_file(env_config.dependency_file)

    return MCPServerDescriptor(
        name=name,
        url=f"{base_url}:{port}/mcp",
        description=env_config.description,
        packages=packages,
        environment_type=env_config.type,
        scope=scope,
    )


class MCPServerRegistry:
    """
    Global registry for MCP code execution servers.

    Maintains a catalog of server descriptors for discovery and connection.

    Usage:
        # In code_execution module (registers servers):
        from tools.mcp.mcp_server_registry import get_mcp_registry

        registry = get_mcp_registry()
        registry.register(MCPServerDescriptor(
            name="powergrid_executor",
            url="https://powergrid-mcp.example.com/mcp",
            description="Power grid analysis with PyPSA, PyPower",
            packages=["pypsa", "pypower"],
        ))

        # In client code (discovers servers):
        from tools.mcp.mcp_server_registry import get_mcp_registry

        registry = get_mcp_registry()
        available = registry.list_servers()
        powergrid = registry.get("powergrid_executor")
    """

    def __init__(self):
        """Initialize empty registry."""
        self._servers: Dict[str, MCPServerDescriptor] = {}
        self._initialized = False
        self._auto_discovery_enabled = True

    async def _validate_server_connection(self, descriptor: MCPServerDescriptor) -> tuple[bool, str]:
        """
        Validate that a server is available and authentication works.

        Args:
            descriptor: Server descriptor with connection details

        Returns:
            Tuple of (is_valid, error_message). error_message is empty if valid.
        """
        try:
            # Extract base URL (remove /mcp suffix)
            base_url = descriptor.url.rsplit("/mcp", 1)[0]
            health_url = f"{base_url}/health"

            # First check: Server availability via health endpoint (no auth required)
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)
                ) as client:
                    response = await client.get(health_url)
                    if response.status_code != 200:
                        return False, f"Health check failed with status {response.status_code}"
                    health_data = response.json()
                    if health_data.get("status") != "healthy":
                        return False, f"Server reports unhealthy status: {health_data}"
                    LOGGER.debug(f"Health check passed for {descriptor.name}: {health_data}")
            except httpx.ConnectError as e:
                return False, f"Cannot connect to server: {e}"
            except httpx.TimeoutException:
                return False, "Connection timeout"
            except Exception as e:
                return False, f"Health check failed: {e}"

            # Second check: Authentication via MCP endpoint
            try:
                token_provider = get_token_provider(descriptor.scope)
                token = token_provider()

                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)
                ) as client:
                    # Make a simple OPTIONS request to test auth without executing anything
                    response = await client.options(descriptor.url, headers={"Authorization": f"Bearer {token}"})
                    # Accept 200 (OK) or 405 (Method Not Allowed) as valid auth responses
                    if response.status_code == 401:
                        return False, "Authentication failed: Invalid or expired token"
                    elif response.status_code == 403:
                        return False, "Authentication failed: Insufficient permissions"
                    LOGGER.debug(f"Authentication check passed for {descriptor.name}")
            except Exception as e:
                return False, f"Authentication check failed: {e}"

            return True, ""

        except Exception as e:
            return False, f"Validation error: {e}"

    async def register(self, descriptor: MCPServerDescriptor) -> None:
        """
        Register an MCP server descriptor.

        Validates server availability and authentication before registering.
        If validation fails, the server will not be registered.

        Args:
            descriptor: Server descriptor with connection details
        """
        if descriptor.name in self._servers:
            LOGGER.warning(f"Overwriting existing MCP server registration: {descriptor.name}")

        # Validate server connection
        is_valid, error_msg = await self._validate_server_connection(descriptor)
        if not is_valid:
            LOGGER.error(f"Failed to register MCP server '{descriptor.name}': {error_msg}.")
            return

        self._servers[descriptor.name] = descriptor

        # Explicit registration means the registry has been initialized by caller intent;
        # skip lazy auto-discovery on subsequent get/list/has calls.
        self._initialized = True
        LOGGER.info(f"Registered MCP server: {descriptor.name} @ {descriptor.url}")

    def unregister(self, name: str) -> bool:
        """
        Unregister an MCP server.

        Args:
            name: Server name to unregister

        Returns:
            True if server was unregistered, False if not found
        """
        if name in self._servers:
            del self._servers[name]
            LOGGER.info(f"Unregistered MCP server: {name}")
            return True
        return False

    def get(self, name: str) -> Optional[MCPServerDescriptor]:
        """
        Get server descriptor by name.

        Args:
            name: Server name

        Returns:
            Server descriptor or None if not found
        """
        # Trigger auto-discovery if not already done
        if not self._initialized and self._auto_discovery_enabled:
            self._auto_discover()

        return self._servers.get(name)

    def list_servers(self) -> Dict[str, MCPServerDescriptor]:
        """
        List all registered servers.

        Returns:
            Dictionary mapping server names to descriptors
        """
        # Trigger auto-discovery if not already done
        if not self._initialized and self._auto_discovery_enabled:
            self._auto_discover()

        return self._servers.copy()

    def has_server(self, name: str) -> bool:
        """
        Check if a server is registered.

        Args:
            name: Server name

        Returns:
            True if server is registered
        """
        # Trigger auto-discovery if not already done
        if not self._initialized and self._auto_discovery_enabled:
            self._auto_discover()

        return name in self._servers

    def clear(self) -> None:
        """Clear all registered servers."""
        self._servers.clear()
        self._initialized = False
        LOGGER.info("Cleared MCP server registry")

    def disable_auto_discovery(self) -> None:
        """Disable automatic server discovery."""
        self._auto_discovery_enabled = False

    def enable_auto_discovery(self) -> None:
        """Enable automatic server discovery."""
        self._auto_discovery_enabled = True

    def _auto_discover(self) -> None:
        """
        Auto-discover and register servers from YAML configuration.

        This method loads server configurations from server_registry.yaml
        and registers them with the registry. If the file is not found or
        imports fail, it continues gracefully.

        All servers are validated for availability and authentication before registration.
        """
        if self._initialized:
            return

        self._initialized = True

        # Load server configurations from YAML file
        server_configs = self._load_configs_from_yaml()

        if server_configs:
            LOGGER.info(f"Loading {len(server_configs)} server configs, validating connectivity...")
            coro = self._register_from_configs(server_configs)
            try:
                asyncio.get_running_loop()
                # Already inside an async context – execute in a worker thread
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(asyncio.run, coro).result()
            except RuntimeError:
                # No running loop – safe to use asyncio.run directly
                asyncio.run(coro)

        if self._servers:
            LOGGER.info(f"Successfully registered {len(self._servers)} MCP servers")
        else:
            LOGGER.warning("No MCP servers were successfully registered")

    def _load_configs_from_yaml(self) -> List[MCPServerConfig]:
        """
        Load server configurations from server_registry.yaml.

        Searches for the YAML file in the workspace root (parent of core/).

        Returns:
            List of MCPServerConfig objects, or empty list if file not found
        """
        # Find the workspace root (where server_registry.yaml should be)
        # This file is at tools/mcp/mcp_server_registry.py
        # So workspace root is 3 levels up
        current_file = Path(__file__)
        workspace_root = current_file.parent.parent.parent
        yaml_path = workspace_root / "server_registry.yaml"

        if not yaml_path.exists():
            LOGGER.debug(f"Server registry YAML not found at {yaml_path}")
            return []

        try:
            with open(yaml_path, "r") as f:
                data = yaml.safe_load(f)

            if not data or "servers" not in data:
                LOGGER.warning(f"No 'servers' key found in {yaml_path}")
                return []

            # Get defaults from top level
            default_base_url = data.get("base_url", "http://localhost")
            default_scope = DEFAULT_MCP_SERVER_SCOPE or data.get("scope")

            configs = []
            for server_data in data["servers"]:
                # Apply defaults if not specified per-server
                if "base_url" not in server_data:
                    server_data["base_url"] = default_base_url
                if "scope" not in server_data:
                    server_data["scope"] = default_scope

                config = MCPServerConfig(**server_data)
                configs.append(config)

            LOGGER.info(f"Loaded {len(configs)} server configs from {yaml_path}")
            return configs

        except yaml.YAMLError as e:
            LOGGER.error(f"Failed to parse {yaml_path}: {e}")
            return []
        except Exception as e:
            LOGGER.error(f"Failed to load server configs from {yaml_path}: {e}")
            return []

    async def _register_from_configs(self, configs: list[MCPServerConfig]) -> None:
        """
        Register servers from a list of MCPServerConfig objects.

        All servers are validated for availability and authentication before registration.

        Args:
            configs: List of server configurations to register
        """
        import importlib

        for config in configs:
            try:
                module = importlib.import_module(config.module)
                config_func = getattr(module, config.config_function)
                env_config = config_func()
                descriptor = create_mcp_descriptor_from_config(
                    env_config=env_config,
                    name=config.name,
                    port=config.port,
                    base_url=config.base_url,
                    scope=config.scope,
                )
                await self.register(descriptor)
            except ImportError as e:
                LOGGER.debug(f"Could not import {config.module}: {e}")
            except AttributeError as e:
                LOGGER.warning(f"Config function {config.config_function} not found in {config.module}: {e}")
            except Exception as e:
                LOGGER.warning(f"Failed to register server from {config.module}: {e}")


# Global singleton registry
_global_registry: Optional[MCPServerRegistry] = None


@lru_cache(maxsize=1)
def get_mcp_registry() -> MCPServerRegistry:
    """
    Get the global MCP server registry (singleton).

    Returns:
        The global MCPServerRegistry instance
    """
    global _global_registry
    if _global_registry is None:
        _global_registry = MCPServerRegistry()
    return _global_registry


def reset_mcp_registry() -> None:
    """
    Reset the global registry (useful for testing).

    Note: Also clears the lru_cache for get_mcp_registry().
    """
    global _global_registry
    _global_registry = None
    get_mcp_registry.cache_clear()
