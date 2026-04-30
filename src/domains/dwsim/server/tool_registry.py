"""
DWSIM Tool Registry.

Defines domain-specific tools for chemical process simulation that will be
exposed as native MCP tools by the DWSIM server.
"""

import logging

from code_execution import ReturnSpec, StateTransition, ToolDefinition, ToolParameter, ToolRegistry
from domains.dwsim.states import DwsimState as S

LOGGER = logging.getLogger(__name__)

_MODULE_PREFIX = "dwsim_tools.tools"


def create_dwsim_tool_registry() -> "ToolRegistry":
    """
    Create tool registry for the DWSIM chemical simulation domain.

    Returns
    -------
    ToolRegistry
        Registry containing all DWSIM domain tools.
    """
    registry = ToolRegistry()

    # =================================================================
    # Compound database
    # =================================================================

    registry.register_tool(
        ToolDefinition(
            name="search_compounds",
            description=(
                "Search the DWSIM compound database. With no query, returns "
                "every available compound name. With a query string, returns "
                "only compounds whose name contains the query (case-insensitive). "
                "Use this to discover or validate exact compound names before "
                "calling create_flowsheet."
            ),
            required_parameters=[],
            optional_parameters=[
                ToolParameter(
                    name="query",
                    type=str,
                    description=(
                        "Substring to filter compound names, e.g. 'ethanol', "
                        "'acet', 'methyl'. Omit or pass empty string to list all."
                    ),
                ),
            ],
            module=f"{_MODULE_PREFIX}.flowsheet",
            server_name="dwsim",
            state_transition=StateTransition(produces={S.COMPOUNDS_AVAILABLE}),  # type: ignore[arg-type]
            affordances=[
                "search chemical database",
                "check compound spelling",
                "list available species",
            ],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether the search completed."),
                ReturnSpec(name="compounds", type=list, description="List of matching compound names."),
                ReturnSpec(name="count", type=int, description="Number of matches."),
                ReturnSpec(name="query", type=str, description="The query string that was used."),
                ReturnSpec(name="error", type=str, description="Error message if search failed."),
            ],
        )
    )

    # =================================================================
    # Flowsheet lifecycle
    # =================================================================

    registry.register_tool(
        ToolDefinition(
            name="create_flowsheet",
            description=(
                "Create a new DWSIM flowsheet with specified compounds and a "
                "thermodynamic property package. Returns a flowsheet handle for "
                "use in subsequent tool calls."
            ),
            required_parameters=[
                ToolParameter(
                    name="compounds",
                    type=str,
                    description=(
                        "Compound names as they appear in the DWSIM database. "
                        "Use semicolons when names contain commas, e.g. "
                        "'2,2,4-Trimethylpentane;Water;Ethanol'; otherwise "
                        "commas work too, e.g. 'Water,Ethanol,Methanol'."
                    ),
                ),
                ToolParameter(
                    name="property_package",
                    type=str,
                    description=(
                        "Name of the thermodynamic property package. Supported: "
                        "Peng-Robinson, SRK, NRTL, UNIQUAC, Raoult's Law, "
                        "Lee-Kesler-Plocker, UNIFAC, Modified UNIFAC (Dortmund), "
                        "Steam Tables (IAPWS-IF97), CoolProp."
                    ),
                ),
            ],
            optional_parameters=[],
            module=f"{_MODULE_PREFIX}.flowsheet",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.COMPOUNDS_AVAILABLE},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "new process simulation",
                "initialise chemical model",
                "start flowsheet from scratch",
            ],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether the flowsheet was created."),
                ReturnSpec(
                    name="flowsheet",
                    type=object,
                    description="Handle to the DWSIM flowsheet object. Pass this to other DWSIM tools via the handles parameter.",
                ),
                ReturnSpec(name="error", type=str, description="Error message if creation failed."),
            ],
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="load_flowsheet",
            description=(
                "Load an existing DWSIM flowsheet from a .dwxmz or .dwxml file. "
                "Returns a flowsheet handle for use in subsequent tool calls."
            ),
            required_parameters=[
                ToolParameter(
                    name="file_path",
                    type=str,
                    description="Absolute path to the flowsheet file on the server file-system.",
                ),
            ],
            optional_parameters=[],
            module=f"{_MODULE_PREFIX}.flowsheet",
            server_name="dwsim",
            state_transition=StateTransition(produces={S.FLOWSHEET_EXISTS}),  # type: ignore[arg-type]
            affordances=[
                "open saved simulation",
                "import existing flowsheet",
                "resume previous model",
            ],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether the flowsheet was loaded."),
                ReturnSpec(
                    name="flowsheet",
                    type=object,
                    description="Handle to the loaded DWSIM flowsheet object.",
                ),
                ReturnSpec(name="error", type=str, description="Error message if load failed."),
            ],
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="save_flowsheet",
            description=(
                "Save a DWSIM flowsheet to a file. Use .dwxmz extension for "
                "compressed format or .dwxml for plain XML. Creates parent "
                "directories if needed."
            ),
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(
                    name="file_path",
                    type=str,
                    description="Absolute destination path, e.g. '/tmp/my_process.dwxmz'.",
                ),
            ],
            optional_parameters=[],
            module=f"{_MODULE_PREFIX}.flowsheet",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "export simulation to file",
                "persist flowsheet to disk",
                "save .dwxmz model",
            ],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether the flowsheet was saved."),
                ReturnSpec(name="file_path", type=str, description="Path where the file was written."),
                ReturnSpec(name="error", type=str, description="Error message if save failed."),
            ],
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="solve_flowsheet",
            description="Solve (calculate) a DWSIM flowsheet. Reports convergence status and per-object errors.",
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
            ],
            optional_parameters=[],
            module=f"{_MODULE_PREFIX}.flowsheet",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_SOLVED, S.RESULTS_AVAILABLE},  # type: ignore[arg-type]
            ),
            affordances=[
                "converge process model",
                "calculate heat and mass balances",
                "run steady-state solver",
            ],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether the solver ran without exceptions."),
                ReturnSpec(
                    name="converged",
                    type=bool,
                    description="True if all objects converged without errors.",
                ),
                ReturnSpec(
                    name="error_messages",
                    type=list,
                    description="Per-object error messages (empty if converged).",
                ),
                ReturnSpec(name="error", type=str, description="Error message if solver raised an exception."),
            ],
        )
    )

    # =================================================================
    # Streams
    # =================================================================

    registry.register_tool(
        ToolDefinition(
            name="add_material_stream",
            description=(
                "Add a material stream to the flowsheet with temperature, "
                "pressure, mole-fraction composition, and total molar flow."
            ),
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Display tag for the stream."),
                ToolParameter(name="temperature", type=float, description="Temperature in Kelvin."),
                ToolParameter(name="pressure", type=float, description="Pressure in Pascal."),
                ToolParameter(
                    name="compound_mole_fractions",
                    type=str,
                    description='JSON mapping compound name to mole fraction, e.g. \'{"Water": 0.5, "Ethanol": 0.5}\'. Fractions should sum to 1.',
                ),
                ToolParameter(name="total_molar_flow", type=float, description="Total molar flow rate in mol/s."),
            ],
            optional_parameters=[],
            module=f"{_MODULE_PREFIX}.streams",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "define feed or product stream",
                "set stream temperature and pressure",
                "specify molar composition",
            ],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether the stream was added."),
                ReturnSpec(name="stream_name", type=str, description="Tag of the created stream."),
                ReturnSpec(name="error", type=str, description="Error message if failed."),
            ],
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="add_energy_stream",
            description="Add an energy stream (heat or work) to the flowsheet for connecting to unit operations.",
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Display tag for the energy stream."),
            ],
            optional_parameters=[],
            module=f"{_MODULE_PREFIX}.streams",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "add heat duty stream",
                "create work or power stream",
            ],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether the energy stream was added."),
                ReturnSpec(name="stream_name", type=str, description="Tag of the created energy stream."),
                ReturnSpec(name="error", type=str, description="Error message if failed."),
            ],
        )
    )

    # =================================================================
    # Unit operations
    # =================================================================

    _unit_return = [
        ReturnSpec(name="success", type=bool, description="Whether the unit was added."),
        ReturnSpec(name="unit_name", type=str, description="Tag of the created unit."),
        ReturnSpec(name="error", type=str, description="Error message if failed."),
    ]

    registry.register_tool(
        ToolDefinition(
            name="add_mixer",
            description="Add a stream mixer that combines multiple inlet streams into one outlet.",
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Tag for the mixer."),
                ToolParameter(name="inlet_stream_names", type=str, description="Comma-separated inlet stream tags."),
                ToolParameter(name="outlet_stream_name", type=str, description="Tag of the outlet stream."),
            ],
            optional_parameters=[],
            module=f"{_MODULE_PREFIX}.unit_operations",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "combine multiple streams",
                "merge feed flows",
            ],
            return_spec=_unit_return,
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="add_splitter",
            description="Add a stream splitter that divides one inlet into multiple outlets by split ratio.",
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Tag for the splitter."),
                ToolParameter(name="inlet_stream_name", type=str, description="Tag of the inlet stream."),
                ToolParameter(name="outlet_stream_names", type=str, description="Comma-separated outlet stream tags."),
                ToolParameter(
                    name="split_ratios", type=str, description="Comma-separated split ratios (should sum to 1)."
                ),
            ],
            optional_parameters=[],
            module=f"{_MODULE_PREFIX}.unit_operations",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "divide stream by ratio",
                "split flow into branches",
            ],
            return_spec=_unit_return,
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="add_heater",
            description="Add a heater that raises the temperature of a material stream. Optionally connect an energy stream for the duty.",
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Tag for the heater."),
                ToolParameter(name="inlet_stream_name", type=str, description="Inlet material stream tag."),
                ToolParameter(name="outlet_stream_name", type=str, description="Outlet material stream tag."),
                ToolParameter(
                    name="outlet_temperature", type=float, description="Desired outlet temperature in Kelvin."
                ),
                ToolParameter(
                    name="pressure_drop", type=float, description="Pressure drop across the heater in Pascal."
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="energy_stream_name",
                    type=str,
                    description="Energy stream tag supplying heat. Omit if no energy stream is needed.",
                    default="",
                ),
            ],
            module=f"{_MODULE_PREFIX}.unit_operations",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "raise stream temperature",
                "preheat feed",
                "add heat exchanger duty",
            ],
            return_spec=_unit_return,
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="add_cooler",
            description="Add a cooler that lowers the temperature of a material stream. Optionally connect an energy stream for the duty.",
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Tag for the cooler."),
                ToolParameter(name="inlet_stream_name", type=str, description="Inlet material stream tag."),
                ToolParameter(name="outlet_stream_name", type=str, description="Outlet material stream tag."),
                ToolParameter(
                    name="outlet_temperature", type=float, description="Desired outlet temperature in Kelvin."
                ),
                ToolParameter(
                    name="pressure_drop", type=float, description="Pressure drop across the cooler in Pascal."
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="energy_stream_name",
                    type=str,
                    description="Energy stream tag receiving rejected heat. Omit if no energy stream is needed.",
                    default="",
                ),
            ],
            module=f"{_MODULE_PREFIX}.unit_operations",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "lower stream temperature",
                "cool product stream",
                "condense vapour",
            ],
            return_spec=_unit_return,
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="add_pump",
            description="Add a pump to raise the pressure of a liquid stream. Optionally connect an energy stream for power.",
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Tag for the pump."),
                ToolParameter(name="inlet_stream_name", type=str, description="Inlet material stream tag."),
                ToolParameter(name="outlet_stream_name", type=str, description="Outlet material stream tag."),
                ToolParameter(name="outlet_pressure", type=float, description="Discharge pressure in Pascal."),
            ],
            optional_parameters=[
                ToolParameter(
                    name="energy_stream_name",
                    type=str,
                    description="Energy stream tag for pump work. Omit if not needed.",
                    default="",
                ),
            ],
            module=f"{_MODULE_PREFIX}.unit_operations",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "boost liquid pressure",
                "pump liquid to higher pressure",
            ],
            return_spec=_unit_return,
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="add_valve",
            description="Add an isenthalpic expansion valve to reduce pressure.",
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Tag for the valve."),
                ToolParameter(name="inlet_stream_name", type=str, description="Inlet material stream tag."),
                ToolParameter(name="outlet_stream_name", type=str, description="Outlet material stream tag."),
                ToolParameter(name="outlet_pressure", type=float, description="Outlet pressure in Pascal."),
            ],
            optional_parameters=[],
            module=f"{_MODULE_PREFIX}.unit_operations",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "reduce stream pressure",
                "throttle flow",
                "let-down valve",
            ],
            return_spec=_unit_return,
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="add_compressor",
            description="Add a gas compressor to raise the pressure of a vapour stream. Optionally connect an energy stream.",
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Tag for the compressor."),
                ToolParameter(name="inlet_stream_name", type=str, description="Inlet material stream tag."),
                ToolParameter(name="outlet_stream_name", type=str, description="Outlet material stream tag."),
                ToolParameter(name="outlet_pressure", type=float, description="Discharge pressure in Pascal."),
            ],
            optional_parameters=[
                ToolParameter(
                    name="energy_stream_name",
                    type=str,
                    description="Energy stream tag for compressor work. Omit if not needed.",
                    default="",
                ),
            ],
            module=f"{_MODULE_PREFIX}.unit_operations",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "compress gas stream",
                "increase gas discharge pressure",
            ],
            return_spec=_unit_return,
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="add_heat_exchanger",
            description="Add a two-stream heat exchanger (hot side and cold side) with a target hot-side outlet temperature.",
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Tag for the heat exchanger."),
                ToolParameter(name="hot_inlet", type=str, description="Hot-side inlet stream tag."),
                ToolParameter(name="hot_outlet", type=str, description="Hot-side outlet stream tag."),
                ToolParameter(name="cold_inlet", type=str, description="Cold-side inlet stream tag."),
                ToolParameter(name="cold_outlet", type=str, description="Cold-side outlet stream tag."),
                ToolParameter(
                    name="hot_outlet_temperature",
                    type=float,
                    description="Target hot-side outlet temperature in Kelvin.",
                ),
            ],
            optional_parameters=[],
            module=f"{_MODULE_PREFIX}.unit_operations",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "transfer heat between streams",
                "counter-current heat exchange",
                "recover process heat",
            ],
            return_spec=_unit_return,
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="add_separator",
            description="Add a flash separator (vessel) that splits a feed into vapour and liquid phases at specified T and P.",
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Tag for the separator."),
                ToolParameter(name="inlet_stream_name", type=str, description="Inlet material stream tag."),
                ToolParameter(name="vapor_outlet_name", type=str, description="Vapour outlet stream tag."),
                ToolParameter(name="liquid_outlet_name", type=str, description="Liquid outlet stream tag."),
                ToolParameter(
                    name="temperature", type=float, description="Operating temperature in Kelvin (0 for adiabatic)."
                ),
                ToolParameter(name="pressure", type=float, description="Operating pressure in Pascal."),
            ],
            optional_parameters=[],
            module=f"{_MODULE_PREFIX}.unit_operations",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "flash drum",
                "vapour-liquid separation",
                "equilibrium flash at given T and P",
            ],
            return_spec=_unit_return,
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="add_conversion_reactor",
            description=(
                "Add a conversion reactor. The reaction is defined as a JSON string with "
                "base compound, fractional conversion, and stoichiometric coefficients."
            ),
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Tag for the reactor."),
                ToolParameter(name="inlet_stream_name", type=str, description="Feed stream tag."),
                ToolParameter(name="vapor_outlet_name", type=str, description="Vapour product stream tag."),
                ToolParameter(name="liquid_outlet_name", type=str, description="Liquid product stream tag."),
                ToolParameter(
                    name="reaction_set",
                    type=str,
                    description=(
                        'JSON reaction definition, e.g. \'{"base_compound": "Ethanol", "conversion": 0.95, '
                        '"stoichiometry": {"Ethanol": -1, "Water": 1, "Carbon Dioxide": 2}}\''
                    ),
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="energy_stream_name",
                    type=str,
                    description="Energy stream tag. Omit if not needed.",
                    default="",
                ),
            ],
            module=f"{_MODULE_PREFIX}.unit_operations",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "model chemical reaction with fixed conversion",
                "fixed-conversion chemical reaction",
                "specify fractional conversion",
            ],
            return_spec=_unit_return,
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="add_equilibrium_reactor",
            description=(
                "Add an equilibrium reactor. The reaction is defined as a JSON string with "
                "base compound, Keq expression (or omit for Keq=1), and stoichiometric coefficients."
            ),
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Tag for the reactor."),
                ToolParameter(name="inlet_stream_name", type=str, description="Feed stream tag."),
                ToolParameter(name="vapor_outlet_name", type=str, description="Vapour product stream tag."),
                ToolParameter(name="liquid_outlet_name", type=str, description="Liquid product stream tag."),
                ToolParameter(
                    name="reaction_set",
                    type=str,
                    description=(
                        'JSON reaction definition, e.g. \'{"base_compound": "Ethanol", '
                        '"stoichiometry": {"Ethanol": -1, "Water": 1}, "Keq_expression": "exp(-5000/T + 10)"}\''
                    ),
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="energy_stream_name",
                    type=str,
                    description="Energy stream tag. Omit if not needed.",
                    default="",
                ),
            ],
            module=f"{_MODULE_PREFIX}.unit_operations",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "model reaction at chemical equilibrium",
                "equilibrium-limited reactor",
                "Gibbs reactor with Keq",
            ],
            return_spec=_unit_return,
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="add_distillation_column",
            description="Add a rigorous distillation column with condenser, reboiler, feed stage, and reflux ratio.",
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Tag for the column."),
                ToolParameter(name="feed_stream_name", type=str, description="Feed stream tag."),
                ToolParameter(
                    name="feed_stage", type=int, description="Feed tray number (1-based from top; 1 = condenser)."
                ),
                ToolParameter(
                    name="num_stages", type=int, description="Total number of stages including condenser and reboiler."
                ),
                ToolParameter(name="condenser_type", type=str, description="'TotalCondenser' or 'PartialCondenser'."),
                ToolParameter(name="distillate_stream_name", type=str, description="Distillate product stream tag."),
                ToolParameter(name="bottoms_stream_name", type=str, description="Bottoms product stream tag."),
                ToolParameter(name="reflux_ratio", type=float, description="Reflux ratio (L/D)."),
            ],
            optional_parameters=[
                ToolParameter(
                    name="reboiler_duty",
                    type=float,
                    description="Reboiler duty in Watts (used when reboiler_spec_type='Heat_Duty').",
                ),
                ToolParameter(
                    name="bottoms_rate",
                    type=float,
                    description="Bottoms product molar flow in mol/s (used when reboiler_spec_type='Product_Molar_Flow_Rate', default).",
                ),
                ToolParameter(
                    name="reboiler_spec_type",
                    type=str,
                    description="Reboiler spec type: 'Product_Molar_Flow_Rate' (default, most robust) or 'Heat_Duty'.",
                ),
            ],
            module=f"{_MODULE_PREFIX}.unit_operations",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "multi-stage distillation",
                "fractional distillation column",
                "separate liquid mixture by boiling point",
            ],
            return_spec=_unit_return,
        )
    )

    # =================================================================
    # Multi-Feed Distillation Column
    # =================================================================

    registry.register_tool(
        ToolDefinition(
            name="add_multi_feed_distillation_column",
            description=(
                "Add a rigorous distillation column with multiple feed "
                "streams entering at different stages. Supports extractive "
                "distillation (main feed + solvent feed) and any multi-feed "
                "configuration. Columns can have more than 12 stages."
            ),
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Tag for the column."),
                ToolParameter(
                    name="feeds_json",
                    type=str,
                    description=(
                        'JSON list of feeds: [{"stream_name": "Feed", "stage": 10}, '
                        '{"stream_name": "Solvent", "stage": 3}]. '
                        "Stage is 1-based (1 = condenser)."
                    ),
                ),
                ToolParameter(
                    name="num_stages",
                    type=int,
                    description="Total stages including condenser and reboiler.",
                ),
                ToolParameter(
                    name="condenser_type",
                    type=str,
                    description="'TotalCondenser' or 'PartialCondenser'.",
                ),
                ToolParameter(
                    name="distillate_stream_name",
                    type=str,
                    description="Distillate product stream tag.",
                ),
                ToolParameter(
                    name="bottoms_stream_name",
                    type=str,
                    description="Bottoms product stream tag.",
                ),
                ToolParameter(
                    name="reflux_ratio",
                    type=float,
                    description="Reflux ratio (L/D).",
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="bottoms_rate",
                    type=float,
                    description="Reboiler spec value — interpretation depends on reboiler_spec_type: "
                    "molar flow in mol/s for Product_Molar_Flow_Rate (default), "
                    "mass flow in kg/s for Product_Mass_Flow_Rate, "
                    "duty in W for Heat_Duty, temperature in K for Temperature.",
                ),
                ToolParameter(
                    name="reboiler_spec_type",
                    type=str,
                    description="Reboiler specification type. Supported: 'Product_Molar_Flow_Rate' (default), "
                    "'Product_Mass_Flow_Rate', 'Heat_Duty', 'Component_Molar_Flow_Rate', "
                    "'Component_Fraction', 'Temperature'.",
                ),
                ToolParameter(
                    name="condenser_pressure",
                    type=float,
                    description="Condenser pressure in Pa (default: first feed pressure).",
                ),
                ToolParameter(
                    name="reboiler_pressure",
                    type=float,
                    description="Reboiler pressure in Pa (default: condenser pressure).",
                ),
            ],
            module=f"{_MODULE_PREFIX}.unit_operations",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "azeotrope-breaking extractive distillation with entrainer feed",
                "dual-feed or multi-feed distillation column",
                "distillation with side feeds at different stages",
            ],
            return_spec=_unit_return,
        )
    )

    # =================================================================
    # Recycle / Tear stream
    # =================================================================

    registry.register_tool(
        ToolDefinition(
            name="add_recycle",
            description=(
                "Add a recycle (tear stream) convergence block. Connects a "
                "downstream outlet to an upstream inlet, iterating until the "
                "assumed values match the calculated values within tolerance. "
                "Essential for processes with recycle loops."
            ),
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Tag for the recycle block."),
                ToolParameter(
                    name="inlet_stream_name",
                    type=str,
                    description="Tag of the stream entering the recycle block (from downstream — the 'calculated' values).",
                ),
                ToolParameter(
                    name="outlet_stream_name",
                    type=str,
                    description="Tag of the stream leaving the recycle block (going upstream — the 'assumed' values).",
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="max_iterations",
                    type=int,
                    description="Maximum convergence iterations (default 100).",
                ),
                ToolParameter(
                    name="tolerance_mass_flow",
                    type=float,
                    description="Relative tolerance for mass flow convergence (default 1e-3).",
                ),
                ToolParameter(
                    name="tolerance_temperature",
                    type=float,
                    description="Relative tolerance for temperature convergence (default 1e-3).",
                ),
                ToolParameter(
                    name="tolerance_pressure",
                    type=float,
                    description="Relative tolerance for pressure convergence (default 1e-3).",
                ),
                ToolParameter(
                    name="acceleration_method",
                    type=str,
                    description="Convergence acceleration: 'Wegstein' (default) or 'Direct'.",
                ),
            ],
            module=f"{_MODULE_PREFIX}.unit_operations",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "converge recycle loop",
                "tear stream for iterative process",
                "close material recycle",
            ],
            return_spec=_unit_return,
        )
    )

    # =================================================================
    # Expander / Turbine
    # =================================================================

    registry.register_tool(
        ToolDefinition(
            name="add_expander",
            description=(
                "Add an expander (turbine) that extracts work from a gas or "
                "steam stream by reducing its pressure. Used in power cycles, "
                "refrigeration, and process letdown."
            ),
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Tag for the expander."),
                ToolParameter(name="inlet_stream_name", type=str, description="Inlet material stream tag."),
                ToolParameter(name="outlet_stream_name", type=str, description="Outlet material stream tag."),
                ToolParameter(name="outlet_pressure", type=float, description="Discharge pressure in Pascal."),
            ],
            optional_parameters=[
                ToolParameter(
                    name="efficiency",
                    type=float,
                    description="Adiabatic efficiency in percent (default 75).",
                ),
                ToolParameter(
                    name="energy_stream_name",
                    type=str,
                    description="Energy stream tag receiving generated work.",
                ),
            ],
            module=f"{_MODULE_PREFIX}.unit_operations",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "expand gas to generate power",
                "turbine for power cycle",
                "let-down gas pressure and recover work",
            ],
            return_spec=_unit_return,
        )
    )

    # =================================================================
    # Absorption Column
    # =================================================================

    registry.register_tool(
        ToolDefinition(
            name="add_absorption_column",
            description=(
                "Add an absorption (or stripping) column. Gas enters at the "
                "bottom and liquid solvent enters at the top. No condenser or "
                "reboiler — separation relies on gas-liquid contacting. Used "
                "for gas scrubbing, natural gas drying, and solvent absorption."
            ),
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Tag for the absorption column."),
                ToolParameter(name="num_stages", type=int, description="Number of theoretical stages."),
                ToolParameter(
                    name="gas_inlet_name",
                    type=str,
                    description="Tag of the gas feed stream (enters at bottom).",
                ),
                ToolParameter(
                    name="liquid_inlet_name",
                    type=str,
                    description="Tag of the liquid solvent stream (enters at top).",
                ),
                ToolParameter(
                    name="gas_outlet_name",
                    type=str,
                    description="Tag of the treated gas outlet stream (exits at top).",
                ),
                ToolParameter(
                    name="liquid_outlet_name",
                    type=str,
                    description="Tag of the rich solvent outlet stream (exits at bottom).",
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="operating_pressure",
                    type=float,
                    description="Column operating pressure in Pascal. 0 to use feed pressure.",
                ),
            ],
            module=f"{_MODULE_PREFIX}.unit_operations",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "acid gas scrubbing and wet absorption",
                "natural gas drying with solvent",
                "CO2 capture by solvent absorption",
            ],
            return_spec=_unit_return,
        )
    )

    # =================================================================
    # Decanter / Three-phase separator
    # =================================================================

    registry.register_tool(
        ToolDefinition(
            name="add_decanter",
            description=(
                "Add a liquid-liquid decanter (three-phase separator). Separates "
                "a feed into a light liquid phase (organic) and a heavy liquid "
                "phase (aqueous) based on liquid-liquid equilibrium. Used for "
                "azeotropic distillation decanters, water/organic separations."
            ),
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Tag for the decanter."),
                ToolParameter(name="inlet_stream_name", type=str, description="Inlet material stream tag."),
                ToolParameter(
                    name="light_liquid_outlet_name",
                    type=str,
                    description="Tag for the light (organic) liquid outlet stream.",
                ),
                ToolParameter(
                    name="heavy_liquid_outlet_name",
                    type=str,
                    description="Tag for the heavy (aqueous) liquid outlet stream.",
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="temperature",
                    type=float,
                    description="Operating temperature in Kelvin (0 for adiabatic).",
                ),
                ToolParameter(
                    name="pressure",
                    type=float,
                    description="Operating pressure in Pascal (0 to use feed pressure).",
                ),
                ToolParameter(
                    name="energy_stream_name",
                    type=str,
                    description="Optional energy stream tag.",
                ),
            ],
            module=f"{_MODULE_PREFIX}.unit_operations",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "liquid-liquid phase separation",
                "decant organic from aqueous phase",
                "three-phase separator for immiscible liquids",
            ],
            return_spec=_unit_return,
        )
    )

    # =================================================================
    # Results extraction
    # =================================================================

    registry.register_tool(
        ToolDefinition(
            name="get_stream_results",
            description="Read thermodynamic properties and phase compositions of a material stream after solving.",
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="stream_name", type=str, description="Tag of the material stream."),
            ],
            optional_parameters=[],
            module=f"{_MODULE_PREFIX}.results",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.RESULTS_AVAILABLE},  # type: ignore[arg-type]
                produces={S.RESULTS_AVAILABLE},  # type: ignore[arg-type]
            ),
            affordances=[
                "read outlet composition",
                "check stream temperature and pressure",
                "get phase fractions",
            ],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether results were read."),
                ReturnSpec(name="temperature", type=float, description="Temperature in Kelvin."),
                ReturnSpec(name="pressure", type=float, description="Pressure in Pascal."),
                ReturnSpec(name="total_molar_flow", type=float, description="Total molar flow in mol/s."),
                ReturnSpec(name="total_mass_flow", type=float, description="Total mass flow in kg/s."),
                ReturnSpec(name="vapor_fraction", type=float, description="Vapour mole fraction (0-1)."),
                ReturnSpec(
                    name="phase_compositions",
                    type=dict,
                    description="Per-phase per-compound mole and mass fractions.",
                ),
                ReturnSpec(name="error", type=str, description="Error message if failed."),
            ],
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="get_unit_operation_results",
            description="Read key results for a unit operation — duty, efficiency, and detailed properties.",
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="unit_name", type=str, description="Tag of the unit operation."),
            ],
            optional_parameters=[],
            module=f"{_MODULE_PREFIX}.results",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.RESULTS_AVAILABLE},  # type: ignore[arg-type]
                produces={S.RESULTS_AVAILABLE},  # type: ignore[arg-type]
            ),
            affordances=[
                "read equipment duty",
                "check unit efficiency",
                "inspect unit operation performance",
            ],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether results were read."),
                ReturnSpec(name="unit_type", type=str, description="DWSIM class name of the unit."),
                ReturnSpec(name="duty", type=float, description="Heat duty or energy flow in Watts."),
                ReturnSpec(name="efficiency", type=float, description="Efficiency if applicable."),
                ReturnSpec(name="details", type=dict, description="All extracted numeric properties."),
                ReturnSpec(name="error", type=str, description="Error message if failed."),
            ],
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="get_flowsheet_summary",
            description="Return a high-level summary of all flowsheet objects, convergence status, and mass/energy balances.",
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
            ],
            optional_parameters=[],
            module=f"{_MODULE_PREFIX}.results",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "overview of all flowsheet objects",
                "check overall convergence",
                "mass and energy balance summary",
            ],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether the summary was generated."),
                ReturnSpec(
                    name="object_list",
                    type=list,
                    description="List of flowsheet objects with tag, type, and error status.",
                ),
                ReturnSpec(name="convergence_status", type=str, description="'converged' or 'errors'."),
                ReturnSpec(
                    name="mass_balance",
                    type=dict,
                    description="Aggregate mass in/out/difference (kg/s).",
                ),
                ReturnSpec(
                    name="energy_balance",
                    type=dict,
                    description="Aggregate energy in/out/difference (W).",
                ),
                ReturnSpec(name="error", type=str, description="Error message if failed."),
            ],
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="list_object_properties",
            description="List available DWSIM PROP_* property codes for any flowsheet object tag (stream, unit, etc.).",
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="object_tag", type=str, description="Tag of the object to introspect."),
            ],
            optional_parameters=[],
            module=f"{_MODULE_PREFIX}.introspection",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "discover available PROP codes",
                "list readable properties on object",
            ],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether properties were listed."),
                ReturnSpec(name="object_tag", type=str, description="Object tag."),
                ReturnSpec(name="properties", type=list, description="List of PROP_* codes."),
                ReturnSpec(name="error", type=str, description="Error message if failed."),
            ],
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="get_object_property",
            description="Read a single PROP_* property from an object (via GetPropertyValue).",
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="object_tag", type=str, description="Object tag."),
                ToolParameter(name="property_code", type=str, description="DWSIM PROP_* code to read."),
            ],
            optional_parameters=[],
            module=f"{_MODULE_PREFIX}.introspection",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "read specific DWSIM property",
                "query individual simulation value",
            ],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether the read succeeded."),
                ReturnSpec(name="object_tag", type=str, description="Object tag."),
                ReturnSpec(name="property_code", type=str, description="Property code."),
                ReturnSpec(name="value", type=float, description="Value (numeric) if available."),
                ReturnSpec(name="error", type=str, description="Error message if failed."),
            ],
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="set_object_property",
            description="Set a single PROP_* property on an object (via SetPropertyValue).",
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="object_tag", type=str, description="Object tag."),
                ToolParameter(name="property_code", type=str, description="DWSIM PROP_* code to set."),
                ToolParameter(name="value", type=float, description="New numeric value."),
            ],
            optional_parameters=[],
            module=f"{_MODULE_PREFIX}.introspection",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "change simulation parameter",
                "update DWSIM property value",
                "modify operating condition",
            ],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether the write succeeded."),
                ReturnSpec(name="object_tag", type=str, description="Object tag."),
                ReturnSpec(name="property_code", type=str, description="Property code."),
                ReturnSpec(name="old_value", type=float, description="Old value if readable."),
                ReturnSpec(name="new_value", type=float, description="New value if readable."),
                ReturnSpec(name="error", type=str, description="Error message if failed."),
            ],
        )
    )

    # =================================================================
    # Sensitivity & Optimization
    # =================================================================

    registry.register_tool(
        ToolDefinition(
            name="run_sensitivity_analysis",
            description=(
                "Sweep a single flowsheet variable across a range, re-solving at each point, "
                "and record an objective property. Returns parallel arrays of variable and objective values."
            ),
            required_parameters=[
                ToolParameter(
                    name="flowsheet",
                    type=object,
                    description="Flowsheet handle (should already be solved at the base case).",
                ),
                ToolParameter(
                    name="variable_object", type=str, description="Tag of the object whose property is varied."
                ),
                ToolParameter(
                    name="variable_property",
                    type=str,
                    description=(
                        "DWSIM property code (PROP_*) to vary, e.g. 'PROP_MS_0' or 'PROP_HT_2'. "
                        "Must be a valid DWSIM PROP_ code, not a .NET property name."
                    ),
                ),
                ToolParameter(name="min_value", type=float, description="Lower bound for the sweep."),
                ToolParameter(name="max_value", type=float, description="Upper bound for the sweep."),
                ToolParameter(
                    name="num_points", type=int, description="Number of evenly spaced points (including endpoints)."
                ),
                ToolParameter(
                    name="objective_object", type=str, description="Tag of the object from which the objective is read."
                ),
                ToolParameter(
                    name="objective_property",
                    type=str,
                    description=(
                        "DWSIM property code to read as the objective (e.g. 'PROP_MS_0', 'PROP_HT_3'); "
                        "this is passed to GetPropertyValue."
                    ),
                ),
            ],
            optional_parameters=[],
            module=f"{_MODULE_PREFIX}.optimization",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_SOLVED},  # type: ignore[arg-type]
                produces={S.OPTIMIZATION_COMPLETE},  # type: ignore[arg-type]
            ),
            affordances=[
                "parameter sweep",
                "vary operating conditions",
                "study effect of changing a variable",
            ],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether the sweep completed."),
                ReturnSpec(name="variable_values", type=list, description="Sampled variable values."),
                ReturnSpec(name="objective_values", type=list, description="Objective at each point."),
                ReturnSpec(name="error", type=str, description="Error message if failed."),
            ],
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="run_optimization",
            description=(
                "Run a Nelder-Mead numerical optimization on the flowsheet, varying one or more "
                "decision variables to minimize or maximize an objective property, subject to "
                "inequality constraints."
            ),
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(
                    name="objective_object", type=str, description="Tag of the object whose property is the objective."
                ),
                ToolParameter(
                    name="objective_property",
                    type=str,
                    description="DWSIM property code to optimise (e.g. 'PROP_HT_2' for heater duty).",
                ),
                ToolParameter(name="minimize", type=bool, description="True to minimize, False to maximize."),
                ToolParameter(
                    name="variables",
                    type=str,
                    description=(
                        "JSON list of decision-variable specs using DWSIM property codes: "
                        '[{"object": "Heater1", "property": "PROP_MS_0", "min": 350, "max": 500, "initial": 400}, ...]'
                    ),
                ),
                ToolParameter(
                    name="constraints",
                    type=str,
                    description=(
                        "JSON list of inequality constraints (value >= 0 convention) using DWSIM property codes: "
                        '[{"object": "S-OUT", "property": "PROP_MS_0", "type": ">=", "value": 300}, ...]. '
                        'Pass "[]" if no constraints.'
                    ),
                ),
            ],
            optional_parameters=[],
            module=f"{_MODULE_PREFIX}.optimization",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_SOLVED},  # type: ignore[arg-type]
                produces={S.OPTIMIZATION_COMPLETE},  # type: ignore[arg-type]
            ),
            affordances=[
                "minimise or maximise objective",
                "find optimal operating point",
                "Nelder-Mead process optimisation",
            ],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether the optimizer converged."),
                ReturnSpec(name="optimal_values", type=dict, description="Optimal decision-variable values."),
                ReturnSpec(name="objective_value", type=float, description="Final objective value."),
                ReturnSpec(name="error", type=str, description="Error message if failed."),
            ],
        )
    )

    # =================================================================
    # Kinetic Reactor (PFR / CSTR)
    # =================================================================

    registry.register_tool(
        ToolDefinition(
            name="add_kinetic_reactor",
            description=(
                "Add a kinetic reactor (Plug-Flow Reactor or Continuous Stirred-Tank Reactor) "
                "with Arrhenius kinetic or heterogeneous-catalytic rate expressions. Supports "
                "multiple reactions, catalyst bed parameters, and isothermic/adiabatic operation."
            ),
            required_parameters=[
                ToolParameter(name="flowsheet", type=object, description="Flowsheet handle."),
                ToolParameter(name="name", type=str, description="Tag for the reactor."),
                ToolParameter(name="inlet_stream_name", type=str, description="Feed material stream tag."),
                ToolParameter(name="outlet_stream_name", type=str, description="Main (liquid) product stream tag."),
            ],
            optional_parameters=[
                ToolParameter(
                    name="energy_stream_name",
                    type=str,
                    description="Energy stream tag. Connects to the reactor energy inlet port.",
                    default="",
                ),
                ToolParameter(
                    name="reactor_type",
                    type=str,
                    description='Reactor type: "PFR" (plug-flow) or "CSTR" (stirred-tank). Default "PFR".',
                    default="PFR",
                ),
                ToolParameter(
                    name="reactions_json",
                    type=str,
                    description=(
                        "JSON list of kinetic reaction dicts. Each dict has: "
                        '"stoichiometry" ({compound: coeff}), "direct_orders" ({compound: order}), '
                        '"reverse_orders" (optional), "base_compound", "reaction_phase" '
                        '("Liquid"/"Vapor"/"Mixture"), "basis" ("Molar"/"Mass"/"PartialPress"), '
                        '"amount_units" (e.g. "mol/L"), "rate_units" (e.g. "mol/[L.s]"), '
                        '"A_forward", "E_forward" (J/mol), "A_reverse", "E_reverse" (0=irreversible). '
                        'For het-cat: include "type": "HetCat", "numerator", "denominator" rate expressions.'
                    ),
                    default="[]",
                ),
                ToolParameter(name="volume", type=float, description="Reactor volume in m³. Default 1.0.", default=1.0),
                ToolParameter(name="length", type=float, description="PFR tube length in m. Default 5.0.", default=5.0),
                ToolParameter(
                    name="number_of_tubes", type=int, description="Number of parallel tubes (PFR). Default 1.", default=1
                ),
                ToolParameter(
                    name="catalyst_loading",
                    type=float,
                    description="Catalyst loading in kg/m³. Default 0 (homogeneous).",
                    default=0.0,
                ),
                ToolParameter(
                    name="catalyst_particle_diameter",
                    type=float,
                    description="Catalyst particle diameter in m. Default 0.",
                    default=0.0,
                ),
                ToolParameter(
                    name="catalyst_void_fraction",
                    type=float,
                    description="Catalyst bed void fraction (0-1). Default 0.",
                    default=0.0,
                ),
                ToolParameter(
                    name="operation_mode",
                    type=str,
                    description=(
                        'Operation mode: "Isothermic" (default), "Adiabatic", '
                        '"OutletTemperature", or "NonIsothermalNonAdiabatic".'
                    ),
                    default="Isothermic",
                ),
                ToolParameter(
                    name="outlet_temperature",
                    type=float,
                    description="Outlet temperature in K (only for OutletTemperature mode). Default 0.",
                    default=0.0,
                ),
                ToolParameter(
                    name="vapor_outlet_name",
                    type=str,
                    description="CSTR vapor outlet stream tag. Only used for CSTR (port 1). Default empty.",
                    default="",
                ),
            ],
            module=f"{_MODULE_PREFIX}.unit_operations",
            server_name="dwsim",
            state_transition=StateTransition(
                requires={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
                produces={S.FLOWSHEET_EXISTS},  # type: ignore[arg-type]
            ),
            affordances=[
                "model reaction with Arrhenius kinetics",
                "plug-flow reactor PFR",
                "continuous stirred-tank reactor CSTR",
                "Langmuir-Hinshelwood surface reaction",
                "rate-based reaction model",
            ],
            return_spec=_unit_return,
        )
    )

    # =================================================================
    # FSD → DWSIM converter
    # =================================================================

    registry.register_tool(
        ToolDefinition(
            name="convert_fsd_to_dwsim",
            description=(
                "Convert a COCO simulator .fsd flowsheet to DWSIM .dwxmz format. "
                "Parses the FSD file, maps compounds and property packages, builds "
                "the equivalent DWSIM flowsheet, saves it, and optionally solves it. "
                "Reactors are rebuilt as conversion reactors with stoichiometry "
                "back-calculated from solved inlet/outlet data. Returns the saved "
                "file path and a detailed mapping/topology report."
            ),
            required_parameters=[
                ToolParameter(
                    name="fsd_file_path",
                    type=str,
                    description="Absolute path to the .fsd file on the server filesystem.",
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="output_file_path",
                    type=str,
                    description=(
                        "Path for the output .dwxmz file. Defaults to the same "
                        "directory and name as the FSD file with a _converted.dwxmz suffix."
                    ),
                    default="",
                ),
                ToolParameter(
                    name="property_package",
                    type=str,
                    description=(
                        "DWSIM property package to use. If empty, maps from the "
                        "COCO property package or defaults to Peng-Robinson. "
                        "Supported: Peng-Robinson, SRK, NRTL, UNIQUAC, Raoult's Law, "
                        "Lee-Kesler-Plocker, UNIFAC, Modified UNIFAC (Dortmund), "
                        "Steam Tables (IAPWS-IF97), CoolProp."
                    ),
                    default="",
                ),
                ToolParameter(
                    name="solve",
                    type=bool,
                    description=(
                        "Whether to solve the flowsheet after building it. "
                        "Default True. The file is saved regardless of solve outcome."
                    ),
                    default=True,
                ),
            ],
            module=f"{_MODULE_PREFIX}.converter",
            server_name="dwsim",
            state_transition=StateTransition(
                produces={S.FLOWSHEET_EXISTS, S.FLOWSHEET_SOLVED},  # type: ignore[arg-type]
            ),
            affordances=[
                "translate COCO flowsheet to DWSIM",
                "import FSD file",
                "migrate COCO simulation",
                "FSD export to DWSIM",
            ],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether the conversion completed."),
                ReturnSpec(name="file_path", type=str, description="Path to the saved .dwxmz file."),
                ReturnSpec(name="converged", type=bool, description="Whether the flowsheet converged (None if solve=False)."),
                ReturnSpec(name="compound_mapping", type=dict, description="COCO→DWSIM compound name mapping."),
                ReturnSpec(name="topology_report", type=dict, description="Summary of parsed FSD topology."),
                ReturnSpec(name="warnings", type=list, description="Conversion warnings and notes."),
                ReturnSpec(name="unsupported_unit_ops", type=list, description="Unit ops that could not be converted."),
                ReturnSpec(name="error", type=str, description="Error message if conversion failed."),
            ],
        )
    )

    LOGGER.info(f"Created dwsim tool registry with {len(registry.tools)} tools")
    return registry
