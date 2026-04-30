"""
Foundry Code Execution Server.

An MCP server that wraps Azure AI Foundry built-in tools so they can be
consumed by any MCP client (including agents that only speak MCP).

Each Foundry tool (bing_grounding, code_interpreter, deep_research, etc.)
is exposed as a native MCP tool. Calls are forwarded to the Foundry API
via the existing FoundryClientManager.

Environment variables required:
    AZURE_AI_FOUNDRY_ENDPOINT  — Foundry project endpoint

Optional (depending on which tools you enable):
    BING_GROUNDING_CONNECTION_ID
    AZURE_AI_SEARCH_CONNECTION_ID / AZURE_AI_SEARCH_INDEX_NAME
    DEEP_RESEARCH_MODEL_DEPLOYMENT_NAME
    MICROSOFT_FABRIC_CONNECTION_ID
"""

import asyncio
import logging
import os
from pathlib import Path

from code_execution import CodeExecutionServer, EnvironmentConfig

from .tool_registry import create_foundry_tool_registry

LOGGER = logging.getLogger(__name__)


def create_foundry_config() -> EnvironmentConfig:
    """Create foundry environment configuration."""

    requirements_path = Path(__file__).parent / "requirements.yaml"
    dependency_file = requirements_path.read_text()

    return EnvironmentConfig(
        name="foundry",
        description="Azure AI Foundry built-in tools (bing_grounding, code_interpreter, deep_research, etc.) exposed via MCP",
        type="uv",
        dependency_file=dependency_file,
        auto_build=True,
    )


async def main():
    """Run the foundry code execution server."""

    # Get configuration from environment or use defaults
    entra_client_id = os.getenv("ENTRA_CLIENT_ID")
    entra_tenant_id = os.getenv("ENTRA_TENANT_ID")
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")

    # Create server configuration and tool registry
    config = create_foundry_config()
    tool_registry = create_foundry_tool_registry()

    # Create server
    server = CodeExecutionServer(
        environment_config=config,
        entra_client_id=entra_client_id,
        entra_tenant_id=entra_tenant_id,
        tool_registry=tool_registry,
    )

    # Run server
    LOGGER.info(f"Starting foundry code execution server on {host}:{port}")
    LOGGER.info(f"Environment: {config.name} ({config.type})")
    if tool_registry:
        LOGGER.info(f"Domain tools registered: {len(tool_registry.tools)}")

    await server.run_http(host=host, port=port)


if __name__ == "__main__":
    asyncio.run(main())
