"""
Vitrimer Tg Simulation Code Execution Server.

A code execution server for estimating the glass transition temperature (Tg)
of vitrimer polymers via molecular dynamics simulation. Uses EMC for initial
box construction with PCFF force field and LAMMPS for equilibration and
production cooling runs with parallel replica execution.
"""

import asyncio
import logging
import os
from pathlib import Path

from code_execution import CodeExecutionServer, EnvironmentConfig

from .tool_registry import create_vitrimer_tg_sim_tool_registry

LOGGER = logging.getLogger(__name__)


def create_vitrimer_tg_sim_config() -> EnvironmentConfig:
    """Create vitrimer_tg_sim environment configuration."""

    requirements_path = Path(__file__).parent / "requirements.yaml"
    dependency_file = requirements_path.read_text()

    return EnvironmentConfig(
        name="vitrimer_tg_sim",
        description=(
            "Execute Python code for vitrimer Tg estimation via MD simulation "
            "with EMC box construction, LAMMPS equilibration and production "
            "cooling, and bilinear Tg fitting"
        ),
        type="uv",
        dependency_file=dependency_file,
        auto_build=True,
    )


async def main():
    """Run the vitrimer_tg_sim code execution server."""

    entra_client_id = os.getenv("ENTRA_CLIENT_ID")
    entra_tenant_id = os.getenv("ENTRA_TENANT_ID")
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")

    config = create_vitrimer_tg_sim_config()
    tool_registry = create_vitrimer_tg_sim_tool_registry()

    # Session idle timeout must exceed max_timeout so the session
    # manager doesn't reap sessions while LAMMPS is still running.
    from code_execution.sessions import SessionManager, SessionConfig

    max_timeout = 432000  # 5 days
    session_mgr = SessionManager(
        SessionConfig(
            max_sessions=5,
            timeout_minutes=max_timeout // 60 + 60,  # max_timeout + 1 hour buffer
        )
    )

    server = CodeExecutionServer(
        environment_config=config,
        tool_registry=tool_registry,
        entra_client_id=entra_client_id,
        entra_tenant_id=entra_tenant_id,
        # MD simulation runtimes are unpredictable and vary widely with
        # system size (minutes to days).  Set a generous ceiling and let
        # the actual LAMMPS runtime determine when the call returns.
        # Jobs exceeding 5 days are likely hung and will time out.
        max_timeout=max_timeout,
        default_timeout=max_timeout,
        session_manager=session_mgr,
    )

    LOGGER.info(f"Starting vitrimer_tg_sim code execution server on {host}:{port}")
    LOGGER.info(f"Environment: {config.name} ({config.type})")
    if tool_registry:
        LOGGER.info(f"Domain tools registered: {len(tool_registry.tools)}")

    await server.run_http(
        host=host,
        port=port,
    )


if __name__ == "__main__":
    asyncio.run(main())
