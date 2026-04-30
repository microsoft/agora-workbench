"""
Process Tool Registry.

Defines domain-specific tools for process simulation that will be exposed
as native MCP tools by the process server.
"""

import logging

from code_execution import ToolDefinition, ToolParameter, ToolRegistry, ReturnSpec

LOGGER = logging.getLogger(__name__)


def create_process_tool_registry() -> "ToolRegistry":
    """
    Create tool registry for the process simulation domain.

    This registry defines domain-specific tools that will be exposed as native
    MCP tools by the process server. These tools will be available for
    on-demand retrieval by the agent.

    Returns:
        ToolRegistry: Registry containing process simulation domain tools
    """

    registry = ToolRegistry()

    # Tool 0: Build Property Package
    registry.register_tool(
        ToolDefinition(
            name="build_idaes_property_package",
            description="Build a complete IDAES property package configuration with thermodynamic correlations and method implementations. Uses automated fitting from thermodynamic databases (NIST, thermo package) to generate parameter_data and property methods (Cp, density, vapor pressure, etc.). Returns a PropertyPackageConfig handle that can be used when building flowsheets.",
            required_parameters=[
                ToolParameter(
                    name="comp_phases",
                    type=dict,
                    description="Dictionary mapping component names to their specific phase lists. Example: {'H2O': ['Vap', 'Liq'], 'N2': ['Vap']}",
                ),
                ToolParameter(
                    name="temperature_range",
                    type=tuple,
                    description="Tuple of (min, max) temperature in K for property correlation fitting. Example: (273.15, 500.0)",
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="eos_config",
                    type=dict,
                    description="Equation of state configuration. Can be {'type': 'ideal'} for all phases, or phase-specific like {'Vap': {'type': 'cubic', 'cubic_type': 'PR'}, 'Liq': {'type': 'ideal'}}",
                    default=None,
                ),
                ToolParameter(
                    name="component_specific_methods",
                    type=dict,
                    description="Optional dictionary specifying correlation methods for specific components. Example: {'H2O': {'vapor': ['HEOS_FIT'], 'psat': ['ANTOINE_WEBBOOK']}}",
                    default=None,
                ),
                ToolParameter(
                    name="state_definition",
                    type=str,
                    description="State variables for IDAES simulations. Options: 'FTPx' (flow, T, P, x), 'FpcTP' (phase flows, T, P). Default: 'FTPx'",
                    default="FTPx",
                ),
                ToolParameter(
                    name="state_bounds",
                    type=dict,
                    description="State variable bounds as dict of (lower, initial, upper, units) tuples. If not provided, defaults will be used.",
                    default=None,
                ),
            ],
            module="idaes_process_tools.tools.build_property_package",
            server_name="process",
            return_spec=[
                ReturnSpec(
                    name="property_config",
                    type=dict,
                    description="Complete property package configuration dict with all thermodynamic correlations and methods. Pass this handle to build_idaes_flowsheet in the property_packages list.",
                ),
            ],
        )
    )

    # Tool 1: Build IDAES Flowsheet
    registry.register_tool(
        ToolDefinition(
            name="build_idaes_flowsheet",
            description="Build a complete IDAES process flowsheet from a FlowsheetConfig. Creates property packages, unit operations, streams, and connections. Returns a builder object containing the constructed model that must be retained for subsequent operations (specification, initialization, solving).",
            required_parameters=[
                ToolParameter(
                    name="flowsheet_config",
                    type=dict,
                    description="Complete flowsheet configuration object. Must be a FlowsheetConfig or dict with: name, property_packages (list of PropertyPackageConfig), material_blocks (list of FeedConfig/ProductConfig), unit_operations (list of unit configs), and optional dynamic/time settings.",
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="property_config",
                    type=dict,
                    description="Optional property package config dict handle from build_idaes_property_package. If provided, this will be injected into the flowsheet as the property package, replacing any property_packages in flowsheet_config.",
                    default=None,
                ),
            ],
            module="idaes_process_tools.tools.build_flowsheet",
            server_name="process",
            return_spec=[
                ReturnSpec(
                    name="builder",
                    type=object,
                    description="IdaesFlowsheetBuilder object containing the model and references to all property packages, units, and material blocks. Must be passed to subsequent tools.",
                ),
            ],
        )
    )

    # Tool 2: Specify Feed and Unit Operations
    registry.register_tool(
        ToolDefinition(
            name="specify_feed_and_unit_operations",
            description="Apply specifications to the flowsheet feeds and unit operations to reduce degrees of freedom. Fixes feed conditions (flow rates, temperatures, pressures, compositions) and unit operation variables (heat duties, pressure changes, etc.) based on the FlowsheetConfig specifications.",
            required_parameters=[
                ToolParameter(
                    name="builder",
                    type=object,
                    description="IdaesFlowsheetBuilder instance from build_idaes_flowsheet with a constructed model",
                ),
            ],
            optional_parameters=[],
            module="idaes_process_tools.tools.specify_feed_and_unit_operations",
            server_name="process",
            return_spec=[
                ReturnSpec(
                    name="builder",
                    type=object,
                    description="IdaesFlowsheetBuilder with specified model ready for initialization",
                ),
            ],
        )
    )

    # Tool 3: Initialize IDAES Flowsheet
    registry.register_tool(
        ToolDefinition(
            name="initialize_idaes_flowsheet",
            description="Initialize the flowsheet by sequentially initializing each unit operation in topological order, starting from feed units. Propagates state information between connected units to provide good initial guesses for the solver. Essential step before solving the full flowsheet.",
            required_parameters=[
                ToolParameter(
                    name="builder",
                    type=object,
                    description="IdaesFlowsheetBuilder instance from build_idaes_flowsheet with specified model",
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="solver",
                    type=str,
                    description="Solver to use for initialization sub-problems. Default: None (uses IDAES default)",
                    default=None,
                ),
                ToolParameter(
                    name="outlvl",
                    type=str,
                    description="Output level for logging during initialization. Options: 'info', 'debug'. Default: 'info'",
                    default="info",
                ),
            ],
            module="idaes_process_tools.tools.initialize_flowsheet",
            server_name="process",
            return_spec=[
                ReturnSpec(
                    name="builder",
                    type=object,
                    description="IdaesFlowsheetBuilder with initialized model ready for solving",
                ),
            ],
        )
    )

    # Tool 4: Solve IDAES Flowsheet
    registry.register_tool(
        ToolDefinition(
            name="solve_idaes_flowsheet",
            description="Solve the fully specified and initialized IDAES flowsheet model using a nonlinear solver (default: IPOPT). Returns solver status indicating whether an optimal solution was found.",
            required_parameters=[
                ToolParameter(
                    name="builder",
                    type=object,
                    description="IdaesFlowsheetBuilder instance from build_idaes_flowsheet with specified and initialized model",
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="tee",
                    type=bool,
                    description="If True, stream solver output to console. Default: True",
                    default=True,
                ),
                ToolParameter(
                    name="solver_options",
                    type=dict,
                    description="Dictionary of solver-specific options. Example: {'tol': 1e-6, 'max_iter': 500}",
                    default=None,
                ),
            ],
            module="idaes_process_tools.tools.solve_flowsheet",
            server_name="process",
            return_spec=[
                ReturnSpec(
                    name="builder",
                    type=object,
                    description="IdaesFlowsheetBuilder with solved model for results extraction",
                ),
                ReturnSpec(
                    name="termination_condition",
                    type=str,
                    description="Solver termination status (e.g., 'optimal', 'maxIterations', 'infeasible')",
                ),
                ReturnSpec(
                    name="success",
                    type=bool,
                    description="Boolean indicating if solve was successful (optimal or feasible)",
                ),
            ],
        )
    )

    LOGGER.info(f"Created process tool registry with {len(registry.tools)} tools")
    return registry
