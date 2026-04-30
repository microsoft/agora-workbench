"""
Office Code Execution Server.

A server for extracting data from Office documents (Excel, Word, PowerPoint).
IRM-protected files are transparently decrypted via the asset resolution
pipeline before tools receive them.
"""

import asyncio
import logging
import os
from pathlib import Path

from code_execution import CodeExecutionServer, EnvironmentConfig

from .tool_registry import create_office_tool_registry

LOGGER = logging.getLogger(__name__)


def create_office_config() -> EnvironmentConfig:
    """Create office environment configuration."""

    requirements_path = Path(__file__).parent / "requirements.txt"

    with open(requirements_path) as f:
        dependency_file = f.read()

    return EnvironmentConfig(
        name="office",
        description="Extract data from Excel, Word, and PowerPoint documents",
        type="uv",
        dependency_file=dependency_file,
        auto_build=True,
    )


async def main():
    """Run the office code execution server."""

    entra_client_id = os.getenv("ENTRA_CLIENT_ID")
    entra_tenant_id = os.getenv("ENTRA_TENANT_ID")
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")

    config = create_office_config()
    tool_registry = create_office_tool_registry()
    server = CodeExecutionServer(
        environment_config=config,
        tool_registry=tool_registry,
        entra_client_id=entra_client_id,
        entra_tenant_id=entra_tenant_id,
    )

    LOGGER.info(f"Starting office code execution server on {host}:{port}")
    LOGGER.info(f"Environment: {config.name} ({config.type})")
    if tool_registry:
        LOGGER.info(f"Domain tools registered: {len(tool_registry.tools)}")

    await server.run_http(
        host=host,
        port=port,
    )


if __name__ == "__main__":
    asyncio.run(main())
