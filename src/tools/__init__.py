"""Tools module for AgoraAgentMAF."""

# MCP (requires agent dependencies)
from .mcp import get_mcp_registry, MCPServerDescriptor, MCPServerRegistry

# Tool search contract
from .tool_search import ToolSearchBackend, ToolSearchResult, ToolKey

# Tool search implementations
from .search import (
    create_search_tools_function,
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
    "create_search_tools_function",
    "AzureAIToolSearchBackend",
    "BM25ToolSearchBackend",
    "create_and_setup_azure_ai_tool_search",
    "build_tool_list",
    "ToolInfo",
]
