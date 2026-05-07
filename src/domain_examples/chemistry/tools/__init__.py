"""Chemistry domain tools for the RDKit MCP server.

Exports ``CHEMISTRY_TOOLS``, a list of all tool definitions in this package.
Import this in the server module to register them with the ToolRegistry.
"""

from .cluster_molecules import TOOL_DEFINITION as _cluster_molecules
from .compute_descriptors import TOOL_DEFINITION as _compute_descriptors
from .compute_fingerprints import TOOL_DEFINITION as _compute_fingerprints
from .enumerate_functional_groups import TOOL_DEFINITION as _enumerate_functional_groups
from .filter_drug_candidates import TOOL_DEFINITION as _filter_drug_candidates
from .find_similar_molecules import TOOL_DEFINITION as _find_similar_molecules
from .parse_molecule import TOOL_DEFINITION as _parse_molecule

CHEMISTRY_TOOLS = [
    _parse_molecule,
    _enumerate_functional_groups,
    _compute_descriptors,
    _filter_drug_candidates,
    _compute_fingerprints,
    _find_similar_molecules,
    _cluster_molecules,
]

__all__ = ["CHEMISTRY_TOOLS"]
