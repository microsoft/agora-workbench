"""
Utilities for creating MCP tool instances from the server registry.

This module provides ``create_mcp_tools`` which configures an
``MCPStreamableHTTPTool`` to expose the core code-execution and session
management tools that the agent uses directly.
"""

import logging

from ..mcp_server_registry import get_mcp_registry
try:
    from agent_framework import MCPStreamableHTTPTool
except ImportError as e:
    raise ImportError(
        "agent-framework is required for MAF adapters. "
        "Install with: pip install agora-workbench[maf]"
    ) from e

LOGGER = logging.getLogger(__name__)


def create_mcp_tools(mcp_server_name: str) -> "MCPStreamableHTTPTool | None":
    """
    Configure the ``MCPStreamableHTTPTool`` for an MCP code-execution server
    so that it exposes the core tools that the agent uses directly:

    - ``execute_{name}_code`` — run Python code in the domain environment
    - ``search_{name}_tools`` — BM25 search over the server's tool catalog
    - ``query_state_graph`` — navigate domain workflow states (if registered)
    - ``load_skill`` — load skill instructions by name (if registered)
    - ``{name}_list_sessions``, ``{name}_get_session_info``, ``{name}_close_session``
    - ``{name}_push_object`` — cross-server object transfer

    Domain-specific tools are **not** exposed as individual MCP tools.
    Instead, the agent discovers them via ``search_{name}_tools`` and invokes
    them programmatically inside ``execute_{name}_code``.

    Session management tools are prefixed with the server name to avoid
    collisions when multiple MCP servers are connected simultaneously.

    Args:
        mcp_server_name: Name of the MCP server as registered in the global
            ``MCPServerRegistry`` (e.g. ``"powergrid"``).

    Returns:
        The ``MCPStreamableHTTPTool`` instance with ``allowed_tools`` updated,
        or ``None`` if the server cannot be resolved (with a warning logged).
    """

    registry = get_mcp_registry()
    mcp_server = registry.get_mcp_tool(mcp_server_name)
    descriptor = registry.get(mcp_server_name)

    if not mcp_server or not descriptor:
        LOGGER.warning(f"Cannot create MCP tools: MCP server '{mcp_server_name}' not found in registry.")
        return None

    # Derive the code-execution tool name from the server name
    # (mirrors CodeExecutionServer.get_tool_name())
    code_exec_tool_name = f"execute_{descriptor.name}_code"

    # Session tools are prefixed with server name to avoid collisions
    # (mirrors register_session_meta_tools(name_prefix=...))
    # search_{name}_tools is now registered server-side and exposed via MCP.
    allowed_tool_names = {
        code_exec_tool_name,
        f"search_{descriptor.name}_tools",
        "query_state_graph",
        "load_skill",
        f"{descriptor.name}_list_sessions",
        f"{descriptor.name}_get_session_info",
        f"{descriptor.name}_close_session",
        f"{descriptor.name}_push_object",
    }

    # Update allowed_tools on the existing MCPStreamableHTTPTool instance.
    if mcp_server.allowed_tools is None:
        mcp_server.allowed_tools = allowed_tool_names
    else:
        mcp_server.allowed_tools = set(mcp_server.allowed_tools) | allowed_tool_names

    LOGGER.info(
        f"Configured MCP tools for server '{mcp_server_name}': "
        f"{sorted(allowed_tool_names)} (allowed_tools={sorted(mcp_server.allowed_tools)})"
    )
    return mcp_server
