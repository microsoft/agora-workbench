"""MAF adapters for MCP tools.

Requires the ``maf`` extra: ``pip install agora-workbench[maf]``
"""

from .maf import create_mcp_tools

__all__ = ["create_mcp_tools"]
