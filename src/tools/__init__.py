"""Tools module — MCP registry and tool search infrastructure."""

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
)

__all__ = [
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
]
