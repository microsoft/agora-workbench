"""
Foundry Tool Registry.

Provides ToolDefinition entries for Azure AI Foundry built-in tools so they
can be discovered by the agent's retrieval system. Each tool's
server_name points to the ``foundry`` MCP server.
"""

import logging

from code_execution import (
    ReturnSpec,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
)

LOGGER = logging.getLogger(__name__)


def create_foundry_tool_registry() -> ToolRegistry:
    """
    Create a ToolRegistry pre-populated with Foundry built-in tools.

    The tools are configured with ``server_name = "foundry"`` so the
    agent routes calls through the Foundry MCP server rather than calling
    the Foundry SDK directly.

    Returns:
        ToolRegistry containing Foundry tool definitions.
    """
    registry = ToolRegistry()

    _FOUNDRY_TOOLS = [
        ToolDefinition(
            name="bing_grounding",
            description="Search the web using Bing to ground responses with real-time information.",
            required_parameters=[
                ToolParameter(name="query", type=str, description="The search query"),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(
                    name="result",
                    type=str,
                    description="Search results",
                ),
            ],
            module="domains.foundry.server.foundry_tools",
            server_name="foundry",
        ),
        ToolDefinition(
            name="deep_research",
            description="Perform multi-step web research to answer complex questions with comprehensive analysis.",
            required_parameters=[
                ToolParameter(name="query", type=str, description="The research query or question"),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(
                    name="result",
                    type=str,
                    description="Research report",
                ),
            ],
            module="domains.foundry.server.foundry_tools",
            server_name="foundry",
        ),
    ]

    # -----------------------------------------------------------------
    # Tools below are kept for future use but not exposed yet because
    # the required connection endpoints are not available.
    # -----------------------------------------------------------------
    # ToolDefinition(
    #     name="code_interpreter",
    #     description="Execute Python code in a sandboxed environment for calculations, data analysis, and file processing.",
    #     required_parameters=[
    #         ToolParameter(
    #             name="query",
    #             type=str,
    #             description="Python code or a natural-language description of what to compute",
    #         ),
    #     ],
    #     optional_parameters=[],
    #     return_spec=[
    #         ReturnSpec(
    #             name="result",
    #             type=str,
    #
    #             description="Execution output",
    #         ),
    #     ],
    #     module="domains.foundry.server.foundry_tools",
    #     server_name="foundry",
    # ),
    # ToolDefinition(
    #     name="file_search",
    #     description="Search through uploaded files using semantic search.",
    #     required_parameters=[
    #         ToolParameter(name="query", type=str, description="The search query"),
    #     ],
    #     optional_parameters=[],
    #     return_spec=[
    #         ReturnSpec(
    #             name="result",
    #             type=str,
    #
    #             description="Matching file excerpts",
    #         ),
    #     ],
    #     module="domains.foundry.server.foundry_tools",
    #     server_name="foundry",
    # ),
    # ToolDefinition(
    #     name="azure_ai_search",
    #     description="Query Azure AI Search indexes for relevant information.",
    #     required_parameters=[
    #         ToolParameter(name="query", type=str, description="The search query"),
    #     ],
    #     optional_parameters=[],
    #     return_spec=[
    #         ReturnSpec(
    #             name="result",
    #             type=str,
    #
    #             description="Search index results",
    #         ),
    #     ],
    #     module="domains.foundry.server.foundry_tools",
    #     server_name="foundry",
    # ),
    # ToolDefinition(
    #     name="microsoft_fabric",
    #     description="Query and analyze data from Microsoft Fabric data sources.",
    #     required_parameters=[
    #         ToolParameter(name="query", type=str, description="The data query"),
    #     ],
    #     optional_parameters=[],
    #     return_spec=[
    #         ReturnSpec(
    #             name="result",
    #             type=str,
    #
    #             description="Fabric query results",
    #         ),
    #     ],
    #     module="domains.foundry.server.foundry_tools",
    #     server_name="foundry",
    # ),
    # ToolDefinition(
    #     name="sharepoint_grounding",
    #     description="Search and retrieve information from SharePoint sites and documents.",
    #     required_parameters=[
    #         ToolParameter(name="query", type=str, description="The search query"),
    #     ],
    #     optional_parameters=[],
    #     return_spec=[
    #         ReturnSpec(
    #             name="result",
    #             type=str,
    #
    #             description="SharePoint results",
    #         ),
    #     ],
    #     module="domains.foundry.server.foundry_tools",
    #     server_name="foundry",
    # ),

    for tool_def in _FOUNDRY_TOOLS:
        registry.register_tool(tool_def)
        LOGGER.info(f"Registered Foundry MCP tool: {tool_def.name}")

    return registry
