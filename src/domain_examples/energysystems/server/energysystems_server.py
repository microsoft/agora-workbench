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

from code_execution import CodeExecutionServer, EnvironmentConfig
from code_execution.auth import create_noop_auth_config

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


server = EnergySystemsServer(
    environment_config=config,
    auth_config=create_noop_auth_config(),
)

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    asyncio.run(server.run_http(host=host, port=port))
