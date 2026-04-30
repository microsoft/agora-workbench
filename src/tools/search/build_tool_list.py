"""
Build a list of available tools by querying MCP servers.

This module provides the `build_tool_list` function that discovers tools
from all registered MCP servers.  For servers that expose a
``list_{name}_domain_tools`` meta-tool, the domain tool catalog is
retrieved via that tool.  Other servers fall back to iterating their
``functions`` list.
"""

import json
import logging
from dataclasses import dataclass

from tools.mcp.mcp_server_registry import get_mcp_registry

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolInfo:
    """Lightweight tool metadata for search indexing.

    Attributes:
        name: Tool name as exposed by the MCP server
        description: Human-readable description of the tool
        server_name: Name of the MCP server hosting this tool
        affordances: Natural-language phrases that improve search recall
        state_requires: State tokens that must hold before this tool can run
        state_produces: State tokens that hold after a successful run
    """

    name: str
    description: str
    server_name: str
    affordances: tuple[str, ...] = ()
    state_requires: tuple[str, ...] = ()
    state_produces: tuple[str, ...] = ()


# Tools that are infrastructure / meta-tools and should not appear in the
# search index (they are always present in the agent's tool list).
# Only matches when BOTH a prefix AND suffix match (e.g. execute_powergrid_code).
_META_TOOL_PREFIXES = (
    "execute_",
    "list_",
)

_META_TOOL_SUFFIXES = (
    "_code",
    "_domain_tools",
)

# Session meta-tools are registered as `{server}_list_sessions`,
# `{server}_get_session_info`, `{server}_close_session`.  These use the
# server name as a *prefix*, so the prefix+suffix heuristic above will not
# match them.  We catch them by their unique compound suffixes instead.
# Cross-server object transfer tools (`{server}_push_object`, etc.) follow
# the same pattern.
_INFRA_SUFFIXES = (
    "_list_sessions",
    "_get_session_info",
    "_close_session",
    "_push_object",
    "_pull_object",
)


def _is_meta_tool(name: str) -> bool:
    """Return True if *name* looks like an infrastructure/session tool."""
    has_meta_prefix_and_suffix = any(name.startswith(p) for p in _META_TOOL_PREFIXES) and any(
        name.endswith(s) for s in _META_TOOL_SUFFIXES
    )
    has_infra_suffix = any(name.endswith(suffix) for suffix in _INFRA_SUFFIXES)
    return has_meta_prefix_and_suffix or has_infra_suffix


async def build_tool_list() -> list[ToolInfo]:
    """
    Compile a list of available domain tools by querying all registered MCP servers.

    For each server, if a ``list_{name}_domain_tools`` meta-tool is available
    it is called to retrieve the structured catalog.  Otherwise, the function
    falls back to iterating the server's ``functions`` list (filtering out
    infrastructure tools).

    Returns:
        List of ToolInfo objects containing tool name, description, and server name
    """
    registry = get_mcp_registry()
    servers = registry.list_servers()

    if not servers:
        LOGGER.info("No MCP servers available for tool discovery")
        return []

    tools: list[ToolInfo] = []

    for server_name, descriptor in servers.items():
        mcp_tool = registry.get_mcp_tool(server_name)
        if mcp_tool is None:
            LOGGER.warning(f"No MCPStreamableHTTPTool for server '{server_name}', skipping")
            continue

        try:
            if not mcp_tool.is_connected:
                await mcp_tool.connect()
            await mcp_tool.load_tools()

            # Try the meta-tool first
            meta_tool_name = f"list_{descriptor.name}_domain_tools"
            func_names = {f.name for f in mcp_tool.functions}

            if meta_tool_name in func_names:
                result = await mcp_tool.call_tool(tool_name=meta_tool_name)
                # Result may be a list of Content objects; extract text
                if isinstance(result, list):
                    parts: list[str] = []
                    for item in result:
                        text = getattr(item, "text", None)
                        if isinstance(text, str):
                            parts.append(text)
                        else:
                            parts.append(str(item))
                    result_str = "".join(parts)
                else:
                    result_str = str(result)

                catalog = json.loads(result_str)
                for entry in catalog:
                    st = entry.get("state_transition", {})
                    tools.append(
                        ToolInfo(
                            name=entry["name"],
                            description=entry.get("description", ""),
                            server_name=entry.get("server_name", server_name),
                            affordances=tuple(entry.get("affordances", [])),
                            state_requires=tuple(st.get("requires", [])),
                            state_produces=tuple(st.get("produces", [])),
                        )
                    )
                LOGGER.info(f"Discovered {len(catalog)} domain tools from '{server_name}' via meta-tool")
            else:
                # Fallback: iterate functions, filtering out meta/infrastructure tools
                funcs = mcp_tool.functions
                count = 0
                for func in funcs:
                    if not _is_meta_tool(func.name):
                        tools.append(
                            ToolInfo(
                                name=func.name,
                                description=func.description or "",
                                server_name=server_name,
                            )
                        )
                        count += 1
                LOGGER.info(f"Discovered {count} tools from MCP server '{server_name}' via functions fallback")

        except Exception as e:
            LOGGER.warning(f"Failed to discover tools from MCP server '{server_name}': {e}")

    LOGGER.info(f"build_tool_list: discovered {len(tools)} total tools from {len(servers)} servers")
    return tools
