"""
Energy Systems MCP Server — PyPSA-based power system analysis code execution.

Provides an `execute_energysystems_code` MCP tool backed by a conda environment
with PyPSA, HiGHS solver, and common geospatial/time-series packages from
conda-forge.

Usage:
    python -m domain_examples.energysystems.server.energysystems_server

Requires the base image (mcp-server-base:local) to be built first.
See the README for full instructions.
"""

import asyncio
import os
from pathlib import Path

from code_execution import CodeExecutionServer, EnvironmentConfig, ToolRegistry
from code_execution.auth import create_noop_auth_config
from domain_examples.energysystems.tools import ENERGYSYSTEMS_TOOLS

# Path to the energysystems_tools package (relative to this file so it works
# both inside Docker and when running locally from the repo root).
_ENERGYSYSTEMS_TOOLS_PKG = str(Path(__file__).resolve().parent.parent / "energysystems_tools")

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
"""

config = EnvironmentConfig(
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
        f"python -m pip install --no-deps {_ENERGYSYSTEMS_TOOLS_PKG}",
    ],
    # Enable skill discovery: scans <domains_dir>/<name>/skills/SKILL.md.
    # parents[2] resolves to /app/domain_examples in container and src/domain_examples in dev.
    domains_dir=Path(__file__).resolve().parents[2],
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
tool_registry = ToolRegistry()
for tool_def in ENERGYSYSTEMS_TOOLS:
    tool_registry.register_tool(tool_def)

server = EnergySystemsServer(
    environment_config=config,
    tool_registry=tool_registry,
    auth_config=create_noop_auth_config(),
)

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    asyncio.run(server.run_http(host=host, port=port))
