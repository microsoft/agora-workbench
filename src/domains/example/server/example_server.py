"""
Example Code Execution Server.

A simple server with common data science packages for general-purpose code execution.
Includes minimal tool registry for testing session persistence.
"""

import asyncio
import logging
import os
from pathlib import Path

from code_execution import CodeExecutionServer, EnvironmentConfig

from .tool_registry import create_example_tool_registry

LOGGER = logging.getLogger(__name__)


def create_example_config() -> EnvironmentConfig:
    """Create example environment configuration."""

    requirements_path = Path(__file__).parent / "requirements.txt"

    with open(requirements_path) as f:
        dependency_file = f.read()

    return EnvironmentConfig(
        name="example",
        description="Execute Python code with pandas, numpy, and matplotlib",
        type="uv",
        dependency_file=dependency_file,
        auto_build=True,
    )


async def main():
    """Run the example code execution server."""

    # Get configuration from environment or use defaults
    entra_client_id = os.getenv("ENTRA_CLIENT_ID")
    entra_tenant_id = os.getenv("ENTRA_TENANT_ID")
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")

    # Create server
    config = create_example_config()
    tool_registry = create_example_tool_registry()
    server = CodeExecutionServer(
        environment_config=config,
        tool_registry=tool_registry,
        entra_client_id=entra_client_id,
        entra_tenant_id=entra_tenant_id,
    )

    # Run server
    LOGGER.info(f"Starting example code execution server on {host}:{port}")
    LOGGER.info(f"Environment: {config.name} ({config.type})")
    if tool_registry:
        LOGGER.info(f"Domain tools registered: {len(tool_registry.tools)}")

    await server.run_http(
        host=host,
        port=port,
    )


if __name__ == "__main__":
    asyncio.run(main())
