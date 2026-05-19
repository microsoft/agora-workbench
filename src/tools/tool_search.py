"""
Shared protocol and models for tool search backends.

Re-exports the canonical definitions from :mod:`utilities.tool_search` so
that existing imports from ``tools.tool_search`` continue to work.
"""

# Re-export from the canonical location in utilities.
from utilities.tool_search import ToolKey, ToolInfo, ToolSearchResult, ToolSearchBackend

__all__ = ["ToolKey", "ToolInfo", "ToolSearchResult", "ToolSearchBackend"]
