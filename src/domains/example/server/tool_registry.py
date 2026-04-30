"""
Example Tool Registry.

Defines domain-specific tools for testing session persistence that will be exposed
as native MCP tools by the example server.
"""

import logging

from code_execution import ToolRegistry, ToolDefinition, ToolParameter, ReturnSpec

LOGGER = logging.getLogger(__name__)


def create_example_tool_registry() -> "ToolRegistry":
    """
    Create tool registry for example domain.

    This registry defines minimal tools for testing session persistence across
    MCP tool calls.

    Returns:
        ToolRegistry: Registry containing example domain tools
    """

    registry = ToolRegistry()

    # Tool that creates a session
    registry.register_tool(
        ToolDefinition(
            name="create_counter",
            description="Create a counter with an initial value. Returns a handle for the counter.",
            required_parameters=[],
            optional_parameters=[
                ToolParameter(
                    name="initial_value",
                    type=int,
                    description="Initial counter value",
                    default=0,
                ),
            ],
            return_spec=[
                ReturnSpec(
                    name="counter",
                    type=int,
                    description="Handle to the created counter",
                ),
            ],
            module="example_tools.tools.counter",
            server_name="example",
        )
    )

    # Tool that requires a session
    registry.register_tool(
        ToolDefinition(
            name="increment_counter",
            description="Increment the counter. Requires a counter handle from create_counter.",
            required_parameters=[
                ToolParameter(
                    name="counter",
                    type=int,
                    description="Counter handle from create_counter",
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="amount",
                    type=int,
                    description="Amount to increment",
                    default=1,
                ),
            ],
            return_spec=[
                ReturnSpec(
                    name="result",
                    type=int,
                    description="The new counter value after incrementing",
                ),
            ],
            module="example_tools.tools.counter",
            server_name="example",
        )
    )

    # Tool that requires a session (read-only)
    registry.register_tool(
        ToolDefinition(
            name="get_counter_value",
            description="Get the current counter value. Requires a counter handle.",
            required_parameters=[
                ToolParameter(
                    name="counter",
                    type=int,
                    description="Counter handle to read",
                ),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(
                    name="result",
                    type=int,
                    description="The current counter value",
                ),
            ],
            module="example_tools.tools.counter",
            server_name="example",
        )
    )

    # Stateless tool (no session needed)
    registry.register_tool(
        ToolDefinition(
            name="calculate_fibonacci",
            description="Calculate Fibonacci sequence. No persistence needed.",
            required_parameters=[
                ToolParameter(
                    name="n",
                    type=int,
                    description="Number of Fibonacci terms to calculate",
                ),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(
                    name="sequence",
                    type=list,
                    description="The Fibonacci sequence as a list of integers",
                ),
                ReturnSpec(
                    name="count",
                    type=int,
                    description="Number of terms in the sequence",
                ),
            ],
            module="example_tools.tools.fibonacci",
            server_name="example",
        )
    )

    # Tool that creates multiple handles
    registry.register_tool(
        ToolDefinition(
            name="create_pair",
            description="Create two handles simultaneously. Tests multiple handle creation.",
            required_parameters=[],
            optional_parameters=[
                ToolParameter(
                    name="first",
                    type=int,
                    description="First value",
                    default=1,
                ),
                ToolParameter(
                    name="second",
                    type=int,
                    description="Second value",
                    default=2,
                ),
            ],
            return_spec=[
                ReturnSpec(
                    name="first_handle",
                    type=int,
                    description="Handle to the first value",
                ),
                ReturnSpec(
                    name="second_handle",
                    type=int,
                    description="Handle to the second value",
                ),
            ],
            module="example_tools.tools.handle_tests",
            server_name="example",
        )
    )

    # Tool that consumes multiple handles
    registry.register_tool(
        ToolDefinition(
            name="combine_handles",
            description="Combine two handles with an operation. Tests multiple handle consumption.",
            required_parameters=[
                ToolParameter(
                    name="first",
                    type=int,
                    description="First value handle",
                ),
                ToolParameter(
                    name="second",
                    type=int,
                    description="Second value handle",
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="operation",
                    type=str,
                    description="Operation to perform: 'add', 'multiply', 'subtract'",
                    default="add",
                ),
            ],
            return_spec=[
                ReturnSpec(
                    name="result",
                    type=int,
                    description="Result of the operation",
                ),
            ],
            module="example_tools.tools.handle_tests",
            server_name="example",
        )
    )

    # Tool that both consumes and creates a handle
    registry.register_tool(
        ToolDefinition(
            name="transform_and_create",
            description="Transform an input handle and create a new handle. Tests both consuming and creating handles.",
            required_parameters=[
                ToolParameter(
                    name="input_value",
                    type=int,
                    description="Input value handle to transform",
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="multiplier",
                    type=int,
                    description="Multiplier to apply",
                    default=2,
                ),
            ],
            return_spec=[
                ReturnSpec(
                    name="transformed",
                    type=int,
                    description="Handle to the transformed value",
                ),
                ReturnSpec(
                    name="original_value",
                    type=int,
                    description="The original value for reference",
                ),
            ],
            module="example_tools.tools.handle_tests",
            server_name="example",
        )
    )

    # Tool for testing data lake asset resolution
    registry.register_tool(
        ToolDefinition(
            name="inspect_asset",
            description=(
                "Inspect a data lake asset and return its type and value information. "
                "Tests data lake asset resolution by accepting a qualified_name and returning details about the resolved object."
            ),
            required_parameters=[
                ToolParameter(
                    name="asset",
                    type=object,
                    description="Data lake asset to inspect (qualified_name will be automatically resolved)",
                ),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(
                    name="type",
                    type=str,
                    description="Full type of the asset (module.classname)",
                ),
                ReturnSpec(
                    name="value_summary",
                    type=str,
                    description="Summary of the asset's value (e.g., row count for DataFrame)",
                ),
                ReturnSpec(
                    name="details",
                    type=dict,
                    description="Detailed information about the asset structure",
                ),
            ],
            module="example_tools.tools.data_lake_tests",
            server_name="example",
        )
    )

    LOGGER.info(f"Registered {len(registry.tools)} example tools")
    return registry
