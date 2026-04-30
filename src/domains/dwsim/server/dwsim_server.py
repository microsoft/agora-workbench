"""
DWSIM Code Execution Server.

A code execution server with the DWSIM process simulation package.
"""

import asyncio
import logging
import os
from pathlib import Path

from code_execution import CodeExecutionServer, EnvironmentConfig

from .tool_registry import create_dwsim_tool_registry

LOGGER = logging.getLogger(__name__)


def create_dwsim_config() -> EnvironmentConfig:
    """Create DWSIM environment configuration."""
    requirements_path = Path(__file__).parent / "requirements.yaml"
    dependency_file = requirements_path.read_text()

    return EnvironmentConfig(
        name="dwsim",
        description="Execute Python code for DWSIM chemical process simulations — flowsheet design, thermodynamic calculations, and process optimization.",
        type="uv",
        dependency_file=dependency_file,
        auto_build=True,
    )


async def main():
    """Run the DWSIM code execution server."""
    entra_client_id = os.getenv("ENTRA_CLIENT_ID")
    entra_tenant_id = os.getenv("ENTRA_TENANT_ID")
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")

    config = create_dwsim_config()
    tool_registry = create_dwsim_tool_registry()

    server = CodeExecutionServer(
        environment_config=config,
        entra_client_id=entra_client_id,
        entra_tenant_id=entra_tenant_id,
        max_timeout=36000,
        default_timeout=1500,
        tool_registry=tool_registry,
    )

    LOGGER.info(f"Starting dwsim code execution server on {host}:{port}")
    LOGGER.info(f"Environment: {config.name} ({config.type})")
    LOGGER.info(f"Domain tools registered: {len(tool_registry.tools)}")

    await server.run_http(host=host, port=port)


if __name__ == "__main__":
    asyncio.run(main())
