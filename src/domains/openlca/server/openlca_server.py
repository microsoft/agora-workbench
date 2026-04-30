"""
OpenLCA Code Execution Server.

A code execution server for life cycle assessment using the OpenLCA IPC client.
Connects to an OpenLCA IPC server running as a Docker sidecar.
"""

import asyncio
import logging
import os
from pathlib import Path

from code_execution import CodeExecutionServer, EnvironmentConfig

from .tool_registry import create_openlca_tool_registry

LOGGER = logging.getLogger(__name__)


def create_openlca_config() -> EnvironmentConfig:
    """Create OpenLCA environment configuration."""
    requirements_path = Path(__file__).parent / "requirements.txt"

    with open(requirements_path) as f:
        dependency_file = f.read()

    return EnvironmentConfig(
        name="openlca",
        description="Execute Python code for life cycle assessment using OpenLCA — impact assessment, product system modeling, and environmental analysis.",
        type="uv",
        dependency_file=dependency_file,
        auto_build=True,
    )


async def main():
    """Run the OpenLCA code execution server."""
    entra_client_id = os.getenv("ENTRA_CLIENT_ID")
    entra_tenant_id = os.getenv("ENTRA_TENANT_ID")
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")

    config = create_openlca_config()
    tool_registry = create_openlca_tool_registry()

    server = CodeExecutionServer(
        environment_config=config,
        entra_client_id=entra_client_id,
        entra_tenant_id=entra_tenant_id,
        max_timeout=36000,
        default_timeout=1500,
        tool_registry=tool_registry,
    )

    LOGGER.info(f"Starting openlca code execution server on {host}:{port}")
    LOGGER.info(f"Environment: {config.name} ({config.type})")
    LOGGER.info(f"Domain tools registered: {len(tool_registry.tools)}")

    await server.run_http(host=host, port=port)


if __name__ == "__main__":
    asyncio.run(main())
