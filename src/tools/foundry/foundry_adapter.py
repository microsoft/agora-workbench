"""
Azure AI Foundry tool adapter.

Wraps Azure AI Foundry tools to work with the existing ToolRegistry system,
handling Foundry-specific authentication, invocation, and result formatting.
"""

import logging
from typing import Optional, TYPE_CHECKING

from code_execution import ReturnSpec, ToolDefinition, ToolParameter
from .foundry_client import FoundryClientManager, get_foundry_client
from .foundry_models import FoundryBuiltinTool, FoundryToolResult

if TYPE_CHECKING:
    from code_execution import ToolRegistry

LOGGER = logging.getLogger(__name__)


class FoundryToolAdapter:
    """Adapter that wraps Azure AI Foundry tools for integration with ToolRegistry."""

    def __init__(self, client_manager: Optional[FoundryClientManager] = None):
        """
        Initialize the Foundry tool adapter.

        Args:
            client_manager: Optional FoundryClientManager instance. If not provided,
                          uses the global singleton.
        """
        self.client = client_manager or get_foundry_client()

    def discover_tools(self) -> list[ToolDefinition]:
        """
        Discover all available tools from Azure AI Foundry workspace.

        Returns:
            List of ToolDefinition objects for each Foundry tool

        Note:
            If individual tool conversions fail, they are logged and skipped.
            A warning is emitted with all failed tool names at the end.
            The method returns a partial list of successfully converted tools.
        """
        foundry_tools = self.client.list_builtin_tools
        tool_definitions = []
        failed_tools: list[tuple[str, str]] = []  # (tool_name, error_message)

        for tool in foundry_tools:
            tool_name = tool.name
            try:
                tool_def = self._convert_foundry_tool_to_definition(tool)
                tool_definitions.append(tool_def)
                LOGGER.info(f"Discovered Foundry tool: {tool_def.name}")
            except Exception as e:
                error_msg = str(e)
                failed_tools.append((tool_name, error_msg))
                LOGGER.error(f"Failed to convert Foundry tool '{tool_name}' to definition: {e}")

        if failed_tools:
            failed_names = [name for name, _ in failed_tools]
            LOGGER.warning(
                f"Tool discovery completed with {len(failed_tools)} failure(s). "
                f"Failed tools: {failed_names}. "
                f"Successfully discovered {len(tool_definitions)} of {len(foundry_tools)} tools."
            )

        return tool_definitions

    def get_tool(self, tool_name: str) -> ToolDefinition:
        """
        Get a specific tool from Azure AI Foundry and convert to ToolDefinition.

        Args:
            tool_name: Name of the tool to retrieve

        Returns:
            ToolDefinition for the Foundry tool
        """
        foundry_tool = self.client.get_tool(tool_name)
        return self._convert_foundry_tool_to_definition(foundry_tool)

    def execute_tool(self, tool_name: str, parameters: dict) -> FoundryToolResult:
        """
        Execute a Foundry tool with the given parameters.

        Args:
            tool_name: Name of the tool to execute
            parameters: Tool parameters as dictionary

        Returns:
            FoundryToolResult with success, result, and error fields
        """
        return self.client.call_tool(tool_name, parameters)

    def _convert_foundry_tool_to_definition(self, foundry_tool: FoundryBuiltinTool) -> ToolDefinition:
        """
        Convert Azure AI Foundry tool format to ToolDefinition.

        Args:
            foundry_tool: FoundryBuiltinTool definition from Azure AI Foundry

        Returns:
            ToolDefinition object
        """
        # Extract tool metadata
        tool_name = foundry_tool.name
        description = foundry_tool.description

        # Parse parameters from Foundry schema
        parameters_schema = foundry_tool.parameters
        required_params = []
        optional_params = []

        required_names = set(parameters_schema.required)

        for param_name, param_schema in parameters_schema.properties.items():
            param_type = self._map_json_type_to_python(param_schema.get("type", "string"))
            param_desc = param_schema.get("description", "")
            param_default = param_schema.get("default")

            tool_param = ToolParameter(
                name=param_name,
                type=param_type,
                description=param_desc,
                default=param_default,
            )

            if param_name in required_names:
                required_params.append(tool_param)
            else:
                optional_params.append(tool_param)

        # Create tool definition with Foundry server name
        return ToolDefinition(
            name=tool_name,
            description=description,
            required_parameters=required_params,
            optional_parameters=optional_params,
            module="tools.foundry.foundry_adapter",  # This module handles execution
            return_spec=[
                ReturnSpec(
                    name="result",
                    type=dict,
                    description="Result from Azure AI Foundry tool execution",
                )
            ],
            server_name="foundry",
        )

    def _map_json_type_to_python(self, json_type: str) -> type:
        """
        Map JSON Schema types to Python types.

        Args:
            json_type: JSON Schema type string

        Returns:
            Python type
        """
        type_mapping = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "object": dict,
            "array": list,
        }
        return type_mapping.get(json_type, str)


def get_foundry_adapter(client_manager: Optional[FoundryClientManager] = None) -> FoundryToolAdapter:
    """
    Create a Foundry tool adapter instance.

    The adapter is a lightweight wrapper, so we create a new one each time.
    The underlying client is a singleton managed by get_foundry_client().

    Args:
        client_manager: Optional FoundryClientManager instance

    Returns:
        FoundryToolAdapter instance
    """
    return FoundryToolAdapter(client_manager)


def register_foundry_tools(registry: "ToolRegistry", tool_names: Optional[list[str]] = None) -> list[str]:
    """
    Register Azure AI Foundry tools into a ToolRegistry.

    This is a convenience function to add Foundry built-in tools (bing_grounding,
    code_interpreter, deep_research, etc.) to an existing ToolRegistry so they
    can be used by AgoraAgent.

    Args:
        registry: ToolRegistry instance to register tools into
        tool_names: Optional list of specific tool names to register.
                   If None, registers all available Foundry tools.

    Returns:
        List of registered tool names

    Example:
        >>> from code_execution import ToolRegistry
        >>> from tools.foundry.foundry_adapter import register_foundry_tools
        >>>
        >>> registry = ToolRegistry()
        >>> registered = register_foundry_tools(registry)
        >>> print(f"Registered tools: {registered}")
        Registered tools: ['bing_grounding', 'code_interpreter', 'deep_research', ...]

        >>> # Or register specific tools only
        >>> registered = register_foundry_tools(registry, ["bing_grounding", "deep_research"])
    """

    adapter = get_foundry_adapter()
    all_tools = adapter.discover_tools()

    registered = []
    for tool in all_tools:
        # Filter by name if specified
        if tool_names and tool.name not in tool_names:
            continue

        try:
            registry.register_tool(tool)
            registered.append(tool.name)
            LOGGER.info(f"Registered Foundry tool: {tool.name}")
        except Exception as e:
            LOGGER.error(f"Failed to register Foundry tool {tool.name}: {e}")

    return registered
