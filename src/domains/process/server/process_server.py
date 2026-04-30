"""
Process Code Execution Server.

A code execution server with the IDAES process simulation software.
"""

import asyncio
import logging
import os
from pathlib import Path

from code_execution import CodeExecutionServer, EnvironmentConfig

from .tool_registry import create_process_tool_registry

LOGGER = logging.getLogger(__name__)


def create_process_config() -> EnvironmentConfig:
    """Create process environment configuration."""

    env_path = Path(__file__).parent / "environment.yml"

    with open(env_path) as f:
        dependency_file = f.read()

    return EnvironmentConfig(
        name="process",
        description="Execute Python code for process simulation with IDEAS.",
        type="conda",
        dependency_file=dependency_file,
        auto_build=True,
    )


async def main():
    """Run the process code execution server."""

    # Get configuration from environment or use defaults
    entra_client_id = os.getenv("ENTRA_CLIENT_ID")
    entra_tenant_id = os.getenv("ENTRA_TENANT_ID")
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")

    # Create server configuration
    config = create_process_config()

    # Create tool registry
    tool_registry = create_process_tool_registry()

    # Create server
    server = CodeExecutionServer(
        environment_config=config,
        entra_client_id=entra_client_id,
        entra_tenant_id=entra_tenant_id,
        max_timeout=36000,
        default_timeout=1500,
        tool_registry=tool_registry,
    )

    # Run server
    LOGGER.info(f"Starting process code execution server on {host}:{port}")
    LOGGER.info(f"Environment: {config.name} ({config.type})")
    if tool_registry:
        LOGGER.info(f"Domain tools registered: {len(tool_registry.tools)}")

    await server.run_http(
        host=host,
        port=port,
    )


if __name__ == "__main__":
    asyncio.run(main())
