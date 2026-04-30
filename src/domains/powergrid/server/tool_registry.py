"""
PowerGrid Tool Registry.

Defines domain-specific tools for power grid analysis that will be exposed
as native MCP tools by the powergrid server.
"""

import logging

from code_execution import ToolRegistry, ToolDefinition, ToolParameter, ReturnSpec

LOGGER = logging.getLogger(__name__)


def create_powergrid_tool_registry() -> "ToolRegistry":
    """
    Create tool registry for power grid domain.

    This registry defines domain-specific tools that will be exposed as native
    MCP tools by the powergrid server. These tools will be available for
    on-demand retrieval by the agent.

    Note: The actual tool implementations should exist in a 'powergrid.tools' module
    that gets installed in the powergrid environment. For now, this serves as a
    template for future tool registration.

    Returns:
        ToolRegistry: Registry containing power grid domain tools
    """

    registry = ToolRegistry()

    # PyPSA OPF Tool
    registry.register_tool(
        ToolDefinition(
            name="run_opf",
            description="Run an optimal power flow (OPF) optimization on a PyPSA network loaded from a file using the HiGHS solver with PDLP algorithm. The function loads the network from a NetCDF (.nc), executes the optimization to determine the cost-minimizing dispatch of power generators while satisfying grid constraints, and returns the optimization results including success status, objective value, and a network handle for follow-up inspection.",
            required_parameters=[
                ToolParameter(
                    name="network_path",
                    type=str,
                    description="Path to a PyPSA network file in NetCDF (.nc) format. Can be a local file path or a DataLake asset reference (e.g., <blob>base64_id</blob>). The network should be properly initialized with all necessary components (generators, lines, loads, etc.) and their parameters before running the optimization.",
                ),
            ],
            optional_parameters=[],
            module="pypsa_powergrid_tools.tools.pypsa_opf",
            server_name="powergrid",
            return_spec=[
                ReturnSpec(
                    name="success",
                    type=bool,
                    description="Indicates whether the optimization completed successfully.",
                ),
                ReturnSpec(
                    name="network",
                    type=object,
                    description='Handle to the optimized PyPSA network object. Use the handle ID directly as a string literal in your code (e.g. network = "h_abc123def456") and it will be auto-resolved to the actual object.',
                ),
                ReturnSpec(
                    name="status",
                    type=str,
                    description="Solver status message indicating the result of the optimization.",
                ),
                ReturnSpec(
                    name="objective",
                    type=float,
                    description="Objective function value of the optimization (if successful).",
                ),
                ReturnSpec(
                    name="error",
                    type=str,
                    description="Error message (if optimization failed).",
                ),
            ],
        )
    )

    LOGGER.info(f"Created powergrid tool registry with {len(registry.tools)} tools")
    return registry
