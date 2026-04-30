"""
PowerGrid Code Execution Server.

A code execution server with power grid analysis packages including PyPSA,
PyPower, optimization tools, and scientific computing libraries.
"""

import asyncio
import logging
import os
from pathlib import Path

from code_execution import CodeExecutionServer, EnvironmentConfig

from .tool_registry import create_powergrid_tool_registry

LOGGER = logging.getLogger(__name__)


def create_powergrid_config() -> EnvironmentConfig:
    """Create powergrid environment configuration."""

    requirements_path = Path(__file__).parent / "requirements.yaml"
    dependency_file = requirements_path.read_text()

    return EnvironmentConfig(
        name="powergrid",
        description="Execute Python code for power grid analysis with PyPSA, PyPower, and optimization tools",
        type="uv",
        dependency_file=dependency_file,
        auto_build=True,
        additional_commands=[
            # Force-install GPU-enabled highspy wheel (uv --find-links skips non-manylinux tags)
            "uv pip install --force-reinstall --no-deps /opt/wheelhouse/highspy-*.whl",
        ],
    )


async def main():
    """Run the powergrid code execution server."""

    # Get configuration from environment or use defaults
    entra_client_id = os.getenv("ENTRA_CLIENT_ID")
    entra_tenant_id = os.getenv("ENTRA_TENANT_ID")
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")

    # Create server configuration and tool registry
    config = create_powergrid_config()
    tool_registry = create_powergrid_tool_registry()

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
    LOGGER.info(f"Starting powergrid code execution server on {host}:{port}")
    LOGGER.info(f"Environment: {config.name} ({config.type})")
    if tool_registry:
        LOGGER.info(f"Domain tools registered: {len(tool_registry.tools)}")

    await server.run_http(host=host, port=port)


if __name__ == "__main__":
    asyncio.run(main())
