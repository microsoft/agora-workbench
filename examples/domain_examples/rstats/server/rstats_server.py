"""
R Statistics MCP Server — a tier-A "skills-only" domain backed by an R kernel.

This is the canonical example that a CodeExecutionServer domain need not be
Python. Setting ``language="r"`` on the ServerConfig makes the server register
an IRkernel-backed R kernel from its conda environment; the agent then writes
idiomatic **R** into the single ``execute_rstats_code`` tool. As with the
earthscience reference, there are no wrapper tools — the deliverable is the
environment (conda spec + R prelude + skill markdown), not a catalogue of
pre-canned functions.

How the agent knows to write R (not Python): it reads the per-tool
``description`` below. The server is wired to exactly one R kernel; there is no
language auto-detection.

Usage:
    python -m domain_examples.rstats.server.rstats_server

The conda environment must provide r-base and r-irkernel (see ENVIRONMENT_YML).
See the README for full instructions.
"""

import asyncio
import os
import sys
from pathlib import Path

from code_execution import CodeExecutionServer, ServerConfig
from code_execution.auth import create_noop_auth_config

ENVIRONMENT_YML = """\
name: rstats
channels:
  - conda-forge
dependencies:
  # python is present only so the server can resolve the env via get_python_path();
  # the execution kernel itself is R (IRkernel), selected by language="r" below.
  - python=3.11
  - r-base
  - r-irkernel
  - r-data.table
  - r-jsonlite
  - r-ggplot2
"""

# Loaded before every snippet so the agent can use these without a library()
# call. Keep this short: heavy stacks (e.g. the full tidyverse) are better
# pulled in on demand from a skill than paid for on every execute.
RSTATS_PRELUDE = """\
suppressPackageStartupMessages({
  library(data.table)
  library(jsonlite)
})
"""

config = ServerConfig(
    name="rstats",
    description=(
        "Execute R code for statistics and data analysis. The kernel is R, not "
        "Python — write idiomatic R. Preloaded packages: data.table, jsonlite "
        "(also available via library(): ggplot2). Use this for data wrangling, "
        "summary statistics, regression and modeling, and plotting. Write plots "
        "and output files to the AGORA_OUTPUT_DIR directory to return them as "
        "downloadable artifacts."
    ),
    type="conda",
    language="r",
    dependency_file=ENVIRONMENT_YML,
    auto_build=True,
    # Enable skill discovery: scans <domains_dir>/<name>/skills/*.md.
    domains_dir=Path(__file__).resolve().parents[2],
)


class RStatsServer(CodeExecutionServer):
    """R statistics MCP server: an R kernel with a small data-analysis prelude."""

    def preprocess_code(self, code: str) -> str:
        """Load the standard R packages before the agent's code."""
        return RSTATS_PRELUDE + code


server = RStatsServer(
    server_config=config,
    auth_config=create_noop_auth_config(),
)

if __name__ == "__main__":
    if "--warm" in sys.argv:
        asyncio.run(server.warm())
    else:
        host = os.getenv("HOST", "0.0.0.0")
        port = int(os.getenv("PORT", "8000"))
        asyncio.run(server.run_http(host=host, port=port))
