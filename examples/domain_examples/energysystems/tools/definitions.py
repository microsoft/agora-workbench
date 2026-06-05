"""
Tool definitions (metadata) for the energy systems domain.

This module contains only ``ToolDefinition`` objects — the server-side
schemas and affordances. The actual implementations
live in the ``energysystems_tools`` pip package, which is installed into the
execution environment at build time.

The ``module`` field on each definition points to the installed package
(e.g. ``energysystems_tools.define_network``), ensuring the kernel's lazy
``from {module} import {name}`` import resolves correctly.
"""

from code_execution import ReturnSpec, ToolDefinition, ToolParameter

# ============================================================================
# Low complexity: Network Setup
# ============================================================================

define_network = ToolDefinition(
    name="define_network",
    description=(
        "Create a PyPSA network with a name and time snapshots. "
        "Returns the network name, snapshot count, frequency, and time range."
    ),
    required_parameters=[
        ToolParameter(name="name", type=str, description="Name for the network"),
    ],
    optional_parameters=[
        ToolParameter(
            name="snapshots",
            type=int,
            description="Number of hourly snapshots (default: 24 for one day)",
            default=24,
        ),
        ToolParameter(
            name="start",
            type=str,
            description='Start datetime as ISO string (default: "2025-01-01")',
            default="2025-01-01",
        ),
        ToolParameter(
            name="freq",
            type=str,
            description='Pandas frequency string (default: "h" for hourly)',
            default="h",
        ),
    ],
    return_spec=[
        ReturnSpec(name="name", type=str, description="Network name"),
        ReturnSpec(name="num_snapshots", type=int, description="Number of time steps"),
        ReturnSpec(name="frequency", type=str, description="Time step frequency"),
        ReturnSpec(name="start", type=str, description="First snapshot timestamp"),
        ReturnSpec(name="end", type=str, description="Last snapshot timestamp"),
    ],
    affordances=[
        "create a power network",
        "set up a PyPSA model",
        "define the simulation time horizon",
        "create snapshots for time-series analysis",
    ],
)

add_components = ToolDefinition(
    name="add_components",
    description=(
        "Add buses, generators, loads, lines, and storage units to a PyPSA network. "
        "Each component type is specified as a list of dictionaries with component parameters."
    ),
    required_parameters=[
        ToolParameter(
            name="network_name",
            type=str,
            description="Name of the network (must match define_network output)",
        ),
    ],
    optional_parameters=[
        ToolParameter(
            name="buses",
            type=list,
            description='List of bus dicts, e.g. [{"name": "Bus0", "v_nom": 110}]',
            default=None,
        ),
        ToolParameter(
            name="generators",
            type=list,
            description='List of generator dicts, e.g. [{"name": "Gen0", "bus": "Bus0", "p_nom": 100, "marginal_cost": 30}]',
            default=None,
        ),
        ToolParameter(
            name="loads",
            type=list,
            description='List of load dicts, e.g. [{"name": "Load0", "bus": "Bus0", "p_set": 50}]',
            default=None,
        ),
        ToolParameter(
            name="lines",
            type=list,
            description='List of line dicts, e.g. [{"name": "Line0", "bus0": "Bus0", "bus1": "Bus1", "s_nom": 200, "x": 0.01}]',
            default=None,
        ),
        ToolParameter(
            name="storage_units",
            type=list,
            description='List of storage unit dicts, e.g. [{"name": "Battery0", "bus": "Bus0", "p_nom": 50, "max_hours": 4}]',
            default=None,
        ),
    ],
    return_spec=[
        ReturnSpec(name="num_buses", type=int, description="Number of buses added"),
        ReturnSpec(name="num_generators", type=int, description="Number of generators added"),
        ReturnSpec(name="num_loads", type=int, description="Number of loads added"),
        ReturnSpec(name="num_lines", type=int, description="Number of lines added"),
        ReturnSpec(name="num_storage_units", type=int, description="Number of storage units added"),
        ReturnSpec(name="summary", type=str, description="Human-readable summary of components"),
    ],
    affordances=[
        "add buses to a network",
        "add generators with costs",
        "add loads and demand",
        "add transmission lines",
        "add storage units or batteries",
        "build a power system model",
    ],
)

# ============================================================================
# Medium complexity: Time Series & Analysis
# ============================================================================

add_time_series = ToolDefinition(
    name="add_time_series",
    description=(
        "Attach time-varying profiles (load curves, renewable capacity factors) "
        "to existing network components. Profiles are specified as lists of values "
        "matching the snapshot count."
    ),
    required_parameters=[
        ToolParameter(
            name="network_name",
            type=str,
            description="Name of the network",
        ),
        ToolParameter(
            name="profiles",
            type=list,
            description=(
                'List of profile dicts, e.g. [{"component_type": "generators", '
                '"component_name": "Wind0", "attribute": "p_max_pu", '
                '"values": [0.3, 0.5, ...]}]'
            ),
        ),
    ],
    return_spec=[
        ReturnSpec(name="num_profiles_attached", type=int, description="Number of profiles attached"),
        ReturnSpec(name="snapshot_count", type=int, description="Number of snapshots in network"),
        ReturnSpec(name="components", type=list, description="List of component names updated"),
    ],
    affordances=[
        "attach load profiles",
        "set renewable capacity factors over time",
        "add wind or solar generation profiles",
        "model time-varying demand",
    ],
)

run_power_flow = ToolDefinition(
    name="run_power_flow",
    description=(
        "Run Newton-Raphson AC power flow or linear DC power flow on the network. "
        "Returns bus voltages/angles and line loading percentages."
    ),
    required_parameters=[
        ToolParameter(
            name="network_name",
            type=str,
            description="Name of the network",
        ),
    ],
    optional_parameters=[
        ToolParameter(
            name="method",
            type=str,
            description='"ac" (Newton-Raphson, default) or "dc" (linear approximation)',
            default="ac",
        ),
    ],
    return_spec=[
        ReturnSpec(name="converged", type=bool, description="Whether the power flow converged"),
        ReturnSpec(name="method", type=str, description="Power flow method used (ac/dc)"),
        ReturnSpec(
            name="bus_results", type=list, description="Per-bus voltage magnitude, angle, active/reactive power"
        ),
        ReturnSpec(name="line_loading", type=list, description="Per-line loading percentage and power flow"),
    ],
    affordances=[
        "run power flow analysis",
        "compute bus voltages and angles",
        "check line loading and congestion",
        "verify network feasibility",
    ],
)

run_optimal_power_flow = ToolDefinition(
    name="run_optimal_power_flow",
    description=(
        "Run linear optimal power flow (LOPF) to minimize total generation cost "
        "subject to network constraints. Uses the HiGHS solver via linopy."
    ),
    required_parameters=[
        ToolParameter(
            name="network_name",
            type=str,
            description="Name of the network",
        ),
    ],
    return_spec=[
        ReturnSpec(name="status", type=str, description="Solver status (ok/warning/infeasible)"),
        ReturnSpec(name="objective_value", type=float, description="Total system cost (objective function value)"),
        ReturnSpec(name="generator_dispatch", type=list, description="Per-generator optimal dispatch (MW)"),
        ReturnSpec(name="line_flows", type=list, description="Per-line optimal power flows (MW)"),
        ReturnSpec(name="marginal_prices", type=list, description="Per-bus marginal price (currency/MWh)"),
    ],
    affordances=[
        "minimize generation cost",
        "run optimal power flow",
        "compute economic dispatch",
        "determine locational marginal prices",
    ],
)

# ============================================================================
# High complexity: Capacity Planning & Cost Analysis
# ============================================================================

run_capacity_expansion = ToolDefinition(
    name="run_capacity_expansion",
    description=(
        "Run investment optimization to determine optimal capacity additions. "
        "Generators and storage units marked as extendable are optimized for "
        "both dispatch and capacity. Uses HiGHS solver via linopy."
    ),
    required_parameters=[
        ToolParameter(
            name="network_name",
            type=str,
            description="Name of the network (must have time series and extendable components)",
        ),
    ],
    return_spec=[
        ReturnSpec(name="status", type=str, description="Solver status"),
        ReturnSpec(name="total_system_cost", type=float, description="Total annualized system cost"),
        ReturnSpec(name="optimal_capacities", type=list, description="Per-component optimal capacity (MW)"),
        ReturnSpec(name="investment_by_type", type=dict, description="Investment cost breakdown by carrier/technology"),
    ],
    affordances=[
        "optimize generation investment",
        "plan capacity expansion",
        "evaluate renewable integration scenarios",
        "optimize storage sizing",
    ],
)

analyze_costs = ToolDefinition(
    name="analyze_costs",
    description=(
        "Analyze costs from a solved optimal power flow: total system cost, "
        "cost breakdown by technology/carrier, and marginal price statistics."
    ),
    required_parameters=[
        ToolParameter(
            name="network_name",
            type=str,
            description="Name of the network (must have a solved OPF)",
        ),
    ],
    return_spec=[
        ReturnSpec(name="total_cost", type=float, description="Total system cost"),
        ReturnSpec(name="cost_by_carrier", type=dict, description="Cost breakdown by carrier/technology"),
        ReturnSpec(
            name="marginal_price_stats", type=dict, description="Marginal price statistics (mean, min, max per bus)"
        ),
        ReturnSpec(name="most_expensive_bus", type=str, description="Bus with highest average marginal price"),
    ],
    affordances=[
        "break down system costs by technology",
        "analyze marginal pricing across buses",
        "identify most expensive generation",
        "cost comparison across scenarios",
    ],
)

analyze_topology = ToolDefinition(
    name="analyze_topology",
    description=(
        "Analyze the network graph topology using networkx: connectivity, "
        "island detection, node degree distribution, and bottleneck identification."
    ),
    required_parameters=[
        ToolParameter(
            name="network_name",
            type=str,
            description="Name of the network",
        ),
    ],
    return_spec=[
        ReturnSpec(name="num_buses", type=int, description="Number of buses"),
        ReturnSpec(name="num_lines", type=int, description="Number of lines/links"),
        ReturnSpec(name="is_connected", type=bool, description="Whether the network is fully connected"),
        ReturnSpec(name="num_islands", type=int, description="Number of connected components (islands)"),
        ReturnSpec(name="degree_distribution", type=dict, description="Bus degree distribution"),
        ReturnSpec(name="bottleneck_lines", type=list, description="Lines with highest betweenness centrality"),
    ],
    affordances=[
        "check network connectivity",
        "find electrical islands",
        "identify bottleneck transmission lines",
        "analyze network graph structure",
    ],
)
