"""
Vitrimer VAE Code Execution Server.

A code execution server for AI-guided inverse design of recyclable vitrimeric
polymers. Provides tools for molecule generation, property prediction,
latent-space exploration, calibration, and Bayesian optimization using a
hierarchical graph neural network VAE.
"""

import asyncio
import logging
import os
from pathlib import Path

from code_execution import CodeExecutionServer, EnvironmentConfig
from .tool_registry import create_vitrimer_vae_tool_registry

LOGGER = logging.getLogger(__name__)


def create_vitrimer_vae_config() -> EnvironmentConfig:
    """Create vitrimer_vae environment configuration."""

    requirements_path = Path(__file__).parent / "requirements.txt"

    with open(requirements_path) as f:
        dependency_file = f.read()

    return EnvironmentConfig(
        name="vitrimer_vae",
        description=(
            "Execute Python code for AI-guided vitrimer polymer design with "
            "hierarchical VAE, Bayesian optimization, and GP calibration"
        ),
        type="uv",
        dependency_file=dependency_file,
        auto_build=True,
    )


async def main():
    """Run the vitrimer_vae code execution server."""

    entra_client_id = os.getenv("ENTRA_CLIENT_ID")
    entra_tenant_id = os.getenv("ENTRA_TENANT_ID")
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")

    config = create_vitrimer_vae_config()
    tool_registry = create_vitrimer_vae_tool_registry()

    # Bayesian optimization with large pool sizes can take hours on CPU.
    from code_execution.sessions import SessionManager, SessionConfig

    max_timeout = 14400  # 4 hours
    session_mgr = SessionManager(
        SessionConfig(
            max_sessions=10,
            timeout_minutes=max_timeout // 60 + 60,  # max_timeout + 1 hour buffer
        )
    )

    server = CodeExecutionServer(
        environment_config=config,
        tool_registry=tool_registry,
        entra_client_id=entra_client_id,
        entra_tenant_id=entra_tenant_id,
        max_timeout=max_timeout,
        default_timeout=max_timeout,
        session_manager=session_mgr,
    )

    LOGGER.info(f"Starting vitrimer_vae code execution server on {host}:{port}")
    LOGGER.info(f"Environment: {config.name} ({config.type})")
    if tool_registry:
        LOGGER.info(f"Domain tools registered: {len(tool_registry.tools)}")

    await server.run_http(
        host=host,
        port=port,
    )


if __name__ == "__main__":
    asyncio.run(main())
