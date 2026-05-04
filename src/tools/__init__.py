"""Tools module — MCP registry and tool search infrastructure."""

# Framework-agnostic tool descriptor
from .tool_descriptor import ToolDescriptor

# MCP registry (framework-agnostic)
from .mcp import get_mcp_registry, MCPServerDescriptor, MCPServerRegistry

# Tool search contract
from .tool_search import ToolSearchBackend, ToolSearchResult, ToolKey

# Tool search implementations
from .search import (
    AzureAIToolSearchBackend,
    BM25ToolSearchBackend,
    create_and_setup_azure_ai_tool_search,
    build_tool_list,
    ToolInfo,
    create_search_tools_descriptor,
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
    # Search implementations
    "AzureAIToolSearchBackend",
    "BM25ToolSearchBackend",
    "create_and_setup_azure_ai_tool_search",
    "build_tool_list",
    "ToolInfo",
    # Framework-agnostic descriptor factories
    "create_search_tools_descriptor",
    "create_query_state_graph_descriptor",
    "create_load_skill_descriptor",
]
