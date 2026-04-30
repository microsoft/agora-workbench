"""
OpenLCA Tool Registry.

Defines domain-specific tools for life cycle assessment that will be exposed
as native MCP tools by the OpenLCA server.
"""

import logging

from code_execution import ToolRegistry, ToolDefinition, ToolParameter, ReturnSpec

LOGGER = logging.getLogger(__name__)

_MODULE_PREFIX = "openlca_tools.tools"


def create_openlca_tool_registry() -> "ToolRegistry":
    """
    Create tool registry for OpenLCA domain.

    Registers 5 tools for life cycle assessment via the olca-ipc Python client.

    Returns:
        ToolRegistry: Registry containing OpenLCA domain tools
    """

    registry = ToolRegistry()

    # =================================================================
    # run_impact_assessment — stateless LCA calculation
    # =================================================================
    registry.register_tool(
        ToolDefinition(
            name="run_impact_assessment",
            description=(
                "Run a life cycle impact assessment for a product system using a specified impact method. "
                "Returns a dictionary of impact category results (e.g., GWP, AP, EP)."
            ),
            required_parameters=[
                ToolParameter(
                    name="product_system_name",
                    type=str,
                    description="Name of the product system to assess.",
                ),
                ToolParameter(
                    name="impact_method",
                    type=str,
                    description="Name of the impact assessment method (e.g., 'ReCiPe 2016 Midpoint (H)').",
                ),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(
                    name="results",
                    type=dict,
                    description="Dictionary of impact category results keyed by category name.",
                ),
            ],
            module=f"{_MODULE_PREFIX}.impact_assessment",
            server_name="openlca",
        )
    )

    # =================================================================
    # list_databases — list available OpenLCA databases
    # =================================================================
    registry.register_tool(
        ToolDefinition(
            name="list_databases",
            description="List all databases available on the connected OpenLCA IPC server.",
            required_parameters=[],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(
                    name="databases",
                    type=list,
                    description="List of available database names.",
                ),
            ],
            module=f"{_MODULE_PREFIX}.databases",
            server_name="openlca",
        )
    )

    # =================================================================
    # list_processes — list processes in the active database
    # =================================================================
    registry.register_tool(
        ToolDefinition(
            name="list_processes",
            description=("List processes in the active OpenLCA database, optionally filtered by a search string."),
            required_parameters=[],
            optional_parameters=[
                ToolParameter(
                    name="process_filter",
                    type=str,
                    description="Optional search filter applied to process names (case-insensitive substring match).",
                    default="",
                ),
            ],
            return_spec=[
                ReturnSpec(
                    name="processes",
                    type=list,
                    description="List of process descriptors matching the filter.",
                ),
            ],
            module=f"{_MODULE_PREFIX}.databases",
            server_name="openlca",
        )
    )

    # =================================================================
    # create_product_system — create product system from a process
    # =================================================================
    registry.register_tool(
        ToolDefinition(
            name="create_product_system",
            description=(
                "Create a product system from a reference process. Returns the created product system object."
            ),
            required_parameters=[
                ToolParameter(
                    name="process_name",
                    type=str,
                    description="Name of the reference process for the product system.",
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="config",
                    type=dict,
                    description="Optional configuration dict for product system creation (e.g., cutoff, link strategy).",
                    default=None,
                ),
            ],
            return_spec=[
                ReturnSpec(
                    name="product_system",
                    type=object,
                    description="The created product system object.",
                ),
            ],
            module=f"{_MODULE_PREFIX}.product_systems",
            server_name="openlca",
        )
    )

    # =================================================================
    # compare_scenarios — stateless multi-system comparison
    # =================================================================
    registry.register_tool(
        ToolDefinition(
            name="compare_scenarios",
            description=(
                "Compare the environmental impact of multiple product systems using a specified impact method. "
                "Returns a structured comparison of impact results across all scenarios."
            ),
            required_parameters=[
                ToolParameter(
                    name="product_systems",
                    type=list,
                    description="List of product system names to compare.",
                ),
                ToolParameter(
                    name="impact_method",
                    type=str,
                    description="Name of the impact assessment method to use for comparison.",
                ),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(
                    name="comparison",
                    type=dict,
                    description="Comparison results keyed by product system name, with impact category scores.",
                ),
            ],
            module=f"{_MODULE_PREFIX}.compare_scenarios",
            server_name="openlca",
        )
    )

    LOGGER.info(f"Registered {len(registry.tools)} OpenLCA tools")
    return registry
