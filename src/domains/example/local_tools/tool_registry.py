"""
Local Tool Registry.

Defines local tools that run in the normal Python environment without
requiring specialized servers or MCP setup.
"""

import logging

from code_execution import ToolRegistry, ToolDefinition, ToolParameter, ReturnSpec

LOGGER = logging.getLogger(__name__)


def create_local_tool_registry() -> ToolRegistry:
    """
    Create tool registry for local tools.

    These tools run directly in the current Python environment and do not
    require any specialized server setup.

    Returns:
        ToolRegistry: Registry containing local tools
    """

    registry = ToolRegistry()

    # Echo Tool - for testing local execution
    registry.register_tool(
        ToolDefinition(
            name="echo_with_magic_word",
            description=(
                "A testing tool that echoes back a message with a unique magic keyword "
                "'AGORA_LOCAL_TOOL_SUCCESS' in the output. Use this tool when asked to "
                "test local tool execution, echo a message, or when explicitly asked to "
                "use the echo tool. This tool runs locally without needing any server."
            ),
            required_parameters=[
                ToolParameter(
                    name="message",
                    type=str,
                    description="The message to echo back. This will be returned with a magic keyword.",
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="repeat_count",
                    type=int,
                    description="Number of times to repeat the message in the output. Default is 1.",
                    default=1,
                ),
            ],
            module="domains.example.local_tools.echo_tool",  # Module path for local import
            return_spec=[
                ReturnSpec(
                    name="result",
                    type=dict,
                    description="Echo result with status, magic_keyword, original_message, repeat_count, echoed_output, and tool_type",
                ),
            ],
        )
    )

    LOGGER.info(f"Created local tool registry with {len(registry.tools)} tools")
    return registry
