"""
Chemistry MCP Server — RDKit-based cheminformatics code execution.

Provides an `execute_chemistry_code` MCP tool backed by a conda environment
with RDKit and common scientific Python packages from conda-forge.

Domain-specific tool implementations live in the ``chemistry_tools`` pip
package (under ``chemistry_tools/``), which is installed into the conda
environment via ``additional_commands``.  The server holds only the
``ToolDefinition`` metadata (schemas, state transitions, affordances);
the kernel imports implementations from the installed package.

Usage:
    python -m domain_examples.chemistry.server.chemistry_server

Requires the base image (mcp-server-base:local) to be built first.
See the README for full instructions.
"""

import asyncio
import os
import sys
from pathlib import Path

from code_execution import CodeExecutionServer, ServerConfig, ToolRegistry
from code_execution.auth import create_noop_auth_config
from domain_examples.chemistry.tools import CHEMISTRY_TOOLS

# Path to the chemistry_tools package (relative to this file so it works
# both inside Docker and when running locally from the repo root).
_CHEMISTRY_TOOLS_PKG = str(Path(__file__).resolve().parent.parent / "chemistry_tools")

ENVIRONMENT_YML = """\
name: chemistry
channels:
  - conda-forge
dependencies:
  - python=3.11
  - pip
  - rdkit
  - numpy
  - pandas
  - scipy
  - matplotlib
  - scikit-learn
"""

RDKIT_PRELUDE = """\
from rdkit import Chem
from rdkit.Chem import Draw, Descriptors, AllChem, rdMolDescriptors
from rdkit.Chem import PandasTools
import numpy as np
import pandas as pd
"""

config = ServerConfig(
    name="chemistry",
    description=(
        "Execute Python code with RDKit and cheminformatics packages. "
        "Available libraries: RDKit, numpy, pandas, scipy, matplotlib, scikit-learn. "
        "Use this for molecular analysis, SMILES processing, fingerprints, "
        "descriptor calculation, substructure search, and reaction enumeration."
    ),
    type="conda",
    dependency_file=ENVIRONMENT_YML,
    auto_build=True,
    additional_commands=[
        # Install the chemistry_tools package into the conda environment
        # so that tool proxy imports (e.g. ``from chemistry_tools.parse_molecule
        # import parse_molecule``) resolve correctly inside the kernel.
        f"python -m pip install --no-deps {_CHEMISTRY_TOOLS_PKG}",
    ],
    # Enable skill discovery: scans <domains_dir>/<name>/skills/SKILL.md.
    # parents[2] resolves to /app/domain_examples in container and src/domain_examples in dev.
    domains_dir=Path(__file__).resolve().parents[2],
)


class ChemistryServer(CodeExecutionServer):
    """Chemistry-specific MCP server with auto-imported RDKit modules."""

    def preprocess_code(self, code: str) -> str:
        """Inject common RDKit imports before user code."""
        return RDKIT_PRELUDE + code


# Build the tool registry from domain tool definitions
tool_registry = ToolRegistry()
for tool_def in CHEMISTRY_TOOLS:
    tool_registry.register_tool(tool_def)

server = ChemistryServer(
    server_config=config,
    tool_registry=tool_registry,
    auth_config=create_noop_auth_config(),
)

if __name__ == "__main__":
    if "--warm" in sys.argv:
        asyncio.run(server.warm())
    else:
        host = os.getenv("HOST", "0.0.0.0")
        port = int(os.getenv("PORT", "8000"))
        asyncio.run(server.run_http(host=host, port=port))
