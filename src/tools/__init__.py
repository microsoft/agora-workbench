"""Tools module — MCP registry and tool search infrastructure."""

# Framework-agnostic tool descriptor
from .tool_descriptor import ToolDescriptor

# MCP registry (framework-agnostic)
from .mcp import get_mcp_registry, MCPServerDescriptor, MCPServerRegistry

# Tool search contract (re-exported from utilities for convenience)
from .tool_search import ToolSearchBackend, ToolSearchResult, ToolKey

# Tool search shared model (also in utilities.tool_search)
from utilities.tool_search import ToolInfo

# Tool search implementations
from .search import (
    BM25ToolSearchBackend,
    StateGraph,
    StateGraphToolSearchBackend,
    create_query_state_graph_descriptor,
    create_load_skill_descriptor,
)

__all__ = [
    # Framework-agnostic tool descriptor
    "ToolDescriptor",
    # MCP
    "get_mcp_registry",
    "MCPServerDescriptor",
    "MCPServerRegistry",
    # Tool search contract
    "ToolSearchBackend",
    "ToolSearchResult",
    "ToolKey",
    "ToolInfo",
    # Search implementations
    "BM25ToolSearchBackend",
    "StateGraph",
    "StateGraphToolSearchBackend",
    # Framework-agnostic descriptor factories
    "create_query_state_graph_descriptor",
    "create_load_skill_descriptor",
]
