"""
Chemistry MCP Server — RDKit-based cheminformatics code execution.

Provides an `execute_chemistry_code` MCP tool backed by a conda environment
with RDKit and common scientific Python packages from conda-forge.

Usage:
    python -m domain_examples.chemistry.server.chemistry_server

Requires the base image (mcp-server-base:local) to be built first.
See the README for full instructions.
"""


import asyncio
import os

from code_execution import CodeExecutionServer, EnvironmentConfig
from code_execution.auth import create_noop_auth_config

ENVIRONMENT_YML = """\
name: chemistry
channels:
  - conda-forge
dependencies:
  - python=3.11
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

config = EnvironmentConfig(
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
)


class ChemistryServer(CodeExecutionServer):
    """Chemistry-specific MCP server with auto-imported RDKit modules."""

    def preprocess_code(self, code: str) -> str:
        """Inject common RDKit imports before user code."""
        return RDKIT_PRELUDE + code


server = ChemistryServer(
    environment_config=config,
    auth_config=create_noop_auth_config(),
)

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    asyncio.run(server.run_http(host=host, port=port))
