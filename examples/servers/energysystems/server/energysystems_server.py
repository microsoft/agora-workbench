"""
Energy Systems MCP Server — PyPSA-based power system analysis code execution.

Provides an `execute_energysystems_code` MCP tool backed by a conda environment
with PyPSA, HiGHS solver, and common geospatial/time-series packages from
conda-forge.

Usage:
    python -m servers.energysystems.server.energysystems_server

Requires the base image (mcp-server-base:local) to be built first.
See the README for full instructions.
"""

import asyncio
import os
import sys
from pathlib import Path

from agora_workbench.code_execution import CodeExecutionServer, ServerConfig, Skill, ToolRegistry
from agora_workbench.code_execution.auth import create_noop_auth_config
from servers.energysystems.tools import ENERGYSYSTEMS_TOOLS
from servers.energysystems.server.catalog_setup import setup_catalog
from servers.peers import peer_registry_for

# Path to the energysystems_tools package (relative to this file so it works
# both inside Docker and when running locally from the repo root).
_ENERGYSYSTEMS_TOOLS_PKG = str(Path(__file__).resolve().parent.parent / "energysystems_tools")
_ENERGYSYSTEMS_DIR = Path(__file__).resolve().parents[1]
_SKILL_PATH = _ENERGYSYSTEMS_DIR / "skills" / "SKILL.md"

ENVIRONMENT_YML = """\
name: energysystems
channels:
  - conda-forge
dependencies:
  - python=3.11
  - pypsa
  - linopy
  - highspy
  - numpy
  - pandas
  - scipy
  - matplotlib
  - networkx
  - geopandas
  - shapely
  - xarray
  - netcdf4
  - plotly
  - seaborn
"""

PYPSA_PRELUDE = """\
import pypsa
import numpy as np
import pandas as pd
import networkx as nx
import matplotlib.pyplot as plt

# Networks loaded from NetCDF can carry pyarrow-backed string columns that
# break linopy's optimizer; force classic object strings for compatibility.
pd.set_option("future.infer_string", False)
"""

config = ServerConfig(
    name="energysystems",
    description=(
        "Execute Python code with PyPSA and power system analysis packages. "
        "Available libraries: PyPSA, linopy, HiGHS solver, numpy, pandas, scipy, "
        "matplotlib, networkx, geopandas, shapely, xarray, netcdf4, plotly, seaborn. "
        "Use this for power system modeling, optimal power flow, capacity expansion, "
        "network topology analysis, and renewable energy integration studies."
    ),
    type="conda",
    dependency_file=ENVIRONMENT_YML,
    auto_build=True,
    additional_commands=[
        # Install the energysystems_tools package into the conda environment
        # so that tool proxy imports resolve correctly inside the kernel.
        # NOTE: _ENERGYSYSTEMS_TOOLS_PKG is an absolute path resolved at
        # import time; it must be reachable from the kernel build context
        # (the Dockerfile COPYs the repo layout to preserve this).
        f'python -m pip install --no-deps "{_ENERGYSYSTEMS_TOOLS_PKG}"',
    ],
    # Peer servers reachable via energysystems_send(to=...). Override per-deployment
    # with the AGORA_PEER_REGISTRY env var. See servers/peers.py.
    peer_registry=peer_registry_for("energysystems"),
)


class EnergySystemsServer(CodeExecutionServer):
    """Energy systems MCP server with auto-imported PyPSA modules."""

    def preprocess_code(self, code: str) -> str:
        """Inject common imports without breaking Python top-of-file directives."""
        lines = code.splitlines(keepends=True)
        insert_at = 0

        if insert_at < len(lines) and lines[insert_at].startswith("#!"):
            insert_at += 1

        for encoding_line_index in range(min(2, len(lines))):
            stripped_line = lines[encoding_line_index].lstrip()
            if "coding" in stripped_line and stripped_line.startswith("#"):
                if encoding_line_index >= insert_at:
                    insert_at = encoding_line_index + 1
                break

        while insert_at < len(lines):
            stripped_line = lines[insert_at].lstrip()
            if stripped_line.startswith("from __future__ import "):
                insert_at += 1
                continue
            break

        return "".join(lines[:insert_at]) + PYPSA_PRELUDE + "".join(lines[insert_at:])


# Build the tool registry from domain tool definitions
tool_registry = ToolRegistry(package="energysystems_tools")
for tool_def in ENERGYSYSTEMS_TOOLS:
    tool_registry.register_tool(tool_def)

# ---------------------------------------------------------------------------
# Skills — explicit definition with content loaded from markdown.
# ---------------------------------------------------------------------------

ENERGYSYSTEMS_SKILLS = [
    Skill(
        name="energysystems-pypsa",
        description=(
            "Power system modeling and analysis with PyPSA inside "
            "execute_energysystems_code — network setup, components, power flow, "
            "optimal dispatch, capacity expansion, cost and topology analysis."
        ),
        domain="energysystems",
        content=_SKILL_PATH.read_text(encoding="utf-8"),
        path=str(_SKILL_PATH),
    ),
]

server = EnergySystemsServer(
    server_config=config,
    tool_registry=tool_registry,
    auth_config=create_noop_auth_config(),
    skills=ENERGYSYSTEMS_SKILLS,
)

if __name__ == "__main__":
    if "--warm" in sys.argv:
        asyncio.run(server.warm())
    else:
        # Index the local data catalog and register search_data / query_catalog /
        # get_artifact / list_domains before serving.
        setup_catalog(server, _ENERGYSYSTEMS_DIR)
        host = os.getenv("HOST", "0.0.0.0")
        port = int(os.getenv("PORT", "8000"))
        asyncio.run(server.run_http(host=host, port=port))
