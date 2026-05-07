"""Chemistry domain tools for the RDKit MCP server.

Exports ``CHEMISTRY_TOOLS``, a list of all tool definitions in this package.
Import this in the server module to register them with the ToolRegistry.
"""

from .compute_descriptors import TOOL_DEFINITION as _compute_descriptors
from .find_similar_molecules import TOOL_DEFINITION as _find_similar_molecules
from .parse_molecule import TOOL_DEFINITION as _parse_molecule

CHEMISTRY_TOOLS = [
    _parse_molecule,
    _compute_descriptors,
    _find_similar_molecules,
]

__all__ = ["CHEMISTRY_TOOLS"]
