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
    python -m servers.chemistry.server.chemistry_server

Requires the base image (mcp-server-base:local) to be built first.
See the README for full instructions.
"""

import asyncio
import os
import sys
from pathlib import Path

from agora_workbench.code_execution import CodeExecutionServer, ServerConfig, Skill, State, ToolRegistry
from agora_workbench.code_execution.auth import create_noop_auth_config
from agora_workbench.code_execution.data_access import AssetPublisher, BlobPublisher, LocalFilePublisher
from agora_workbench.code_execution.data_access.credentials import create_storage_credential
from servers.chemistry.tools import CHEMISTRY_TOOLS

# Path to the chemistry_tools package (relative to this file so it works
# both inside Docker and when running locally from the repo root).
_CHEMISTRY_TOOLS_PKG = str(Path(__file__).resolve().parent.parent / "chemistry_tools")
_SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "SKILL.md"

# ---------------------------------------------------------------------------
# Skills — explicit definition with content loaded from the markdown file.
# This pattern gives full control over skill metadata and is preferred when
# you have a small number of well-known skills.
# ---------------------------------------------------------------------------

CHEMISTRY_SKILLS = [
    Skill(
        name="chemistry-rdkit",
        description=(
            "Molecular analysis and cheminformatics using RDKit — SMILES handling, "
            "descriptor calculation, fingerprints, substructure search, similarity, "
            "clustering, and drug-likeness screening via domain tools and the "
            "execute_chemistry_code tool."
        ),
        domain="chemistry",
        states=[
            "chemistry.molecule_parsed",
            "chemistry.groups_identified",
            "chemistry.descriptors_computed",
            "chemistry.candidates_filtered",
            "chemistry.fingerprints_computed",
            "chemistry.similarity_computed",
            "chemistry.molecules_clustered",
        ],
        content=_SKILL_PATH.read_text(encoding="utf-8"),
        path=str(_SKILL_PATH),
    ),
]

CHEMISTRY_STATES = [
    State(
        "chemistry.molecule_parsed",
        description="A SMILES molecule has been parsed and validated",
        affordances=[
            "validate a SMILES string",
            "get the canonical form of a molecule",
            "identify a molecule from SMILES",
        ],
    ),
    State(
        "chemistry.groups_identified",
        description="Functional groups have been enumerated",
        affordances=[
            "identify functional groups in a molecule",
            "find hydroxyl, carboxyl, amine, or other groups",
        ],
    ),
    State(
        "chemistry.descriptors_computed",
        description="Molecular descriptors have been calculated",
        affordances=[
            "compute molecular properties",
            "calculate LogP, TPSA, or molecular weight",
            "evaluate drug-likeness",
        ],
    ),
    State(
        "chemistry.candidates_filtered",
        description="Compounds filtered by drug-likeness rules",
        affordances=[
            "screen compounds for drug-likeness",
            "filter molecules by Lipinski or Veber rules",
            "identify drug candidates",
        ],
    ),
    State(
        "chemistry.fingerprints_computed",
        description="Molecular fingerprints have been generated",
        affordances=[
            "generate molecular fingerprints",
            "compute Morgan or MACCS fingerprints",
            "prepare molecules for similarity or clustering",
        ],
    ),
    State(
        "chemistry.similarity_computed",
        description="Pairwise molecular similarity has been computed",
        affordances=[
            "compare molecular similarity",
            "rank molecules by Tanimoto similarity",
            "virtual screening by similarity",
        ],
    ),
    State(
        "chemistry.molecules_clustered",
        description="Molecules have been grouped into clusters",
        affordances=[
            "cluster a molecular library",
            "group similar molecules together",
            "chemical series analysis",
        ],
    ),
]

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
)


class ChemistryServer(CodeExecutionServer):
    """Chemistry-specific MCP server with auto-imported RDKit modules."""

    def preprocess_code(self, code: str) -> str:
        """Inject common RDKit imports before user code."""
        return RDKIT_PRELUDE + code


# Build the tool registry from domain tool definitions
tool_registry = ToolRegistry(package="chemistry_tools")
for tool_def in CHEMISTRY_TOOLS:
    tool_registry.register_tool(tool_def)

# Configure publishers so the publish_artifact tool is available.
_PUBLISH_DIR = Path(os.getenv("PUBLISH_DIR", "/tmp/published_artifacts"))

# Build the list of publishers. LocalFilePublisher is always available;
# BlobPublisher is enabled when BLOB_PUBLISH_ACCOUNT_URL is set.
_publishers: list[AssetPublisher] = [LocalFilePublisher(base_dir=_PUBLISH_DIR)]

_blob_account_url = os.getenv("BLOB_PUBLISH_ACCOUNT_URL")
_blob_container = os.getenv("BLOB_PUBLISH_CONTAINER", "artifacts")
if _blob_account_url:
    _publishers.append(
        BlobPublisher(
            account_url=_blob_account_url,
            container=_blob_container,
            credential=create_storage_credential(),
        )
    )

server = ChemistryServer(
    server_config=config,
    tool_registry=tool_registry,
    auth_config=create_noop_auth_config(),
    publishers=_publishers,
    skills=CHEMISTRY_SKILLS,
    states=CHEMISTRY_STATES,
)

if __name__ == "__main__":
    if "--warm" in sys.argv:
        asyncio.run(server.warm())
    else:
        host = os.getenv("HOST", "0.0.0.0")
        port = int(os.getenv("PORT", "8000"))
        asyncio.run(server.run_http(host=host, port=port))
