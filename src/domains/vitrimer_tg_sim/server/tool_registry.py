"""
Vitrimer Tg Simulation Tool Registry.

Defines domain-specific tools for estimating glass transition temperature (Tg)
of vitrimer polymers via molecular dynamics simulation, exposed as native MCP
tools by the vitrimer_tg_sim server.
"""

import logging

from code_execution import ToolRegistry, ToolDefinition, ToolParameter, ReturnSpec

LOGGER = logging.getLogger(__name__)

_MODULE_PREFIX = "vitrimer_tg_sim_tools.tools"


def create_vitrimer_tg_sim_tool_registry() -> ToolRegistry:
    """
    Create tool registry for the vitrimer_tg_sim domain.

    Returns:
        ToolRegistry containing all vitrimer Tg simulation tools.
    """
    registry = ToolRegistry()

    # ── build_vitrimer_box ────────────────────────────────────────────
    registry.register_tool(
        ToolDefinition(
            name="build_vitrimer_box",
            description=(
                "Build an initial vitrimer simulation box from acid and epoxide "
                "SMILES using EMC (Enhanced Monte Carlo) with the PCFF force field. "
                "Constructs an alternating copolymer with ~4 chains in a cubic "
                "periodic box at 0.5 g/cm³. Returns paths to LAMMPS data and "
                "params files needed for subsequent equilibration. "
                "Accepts both polymerizable SMILES (with * connection points, "
                "e.g. '*C(=O)CCCCC(=O)*') and standard molecule SMILES "
                "(e.g. 'O=C(O)CCCCC(=O)O'); standard SMILES are auto-converted."
            ),
            required_parameters=[
                ToolParameter(
                    name="acid_smiles",
                    type=str,
                    description=(
                        "SMILES string for the carboxylic acid monomer. "
                        "Accepts either polymerizable form (with * connection "
                        "points) or standard molecule SMILES (auto-converted)."
                    ),
                ),
                ToolParameter(
                    name="epoxide_smiles",
                    type=str,
                    description=(
                        "SMILES string for the epoxide monomer. "
                        "Accepts either polymerizable form (with * connection "
                        "points) or standard molecule SMILES (auto-converted)."
                    ),
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="density",
                    type=float,
                    description="Initial box density in g/cm³.",
                    default=0.5,
                ),
                ToolParameter(
                    name="ntotal",
                    type=int,
                    description="Target total atom count (~4000 for 4 chains).",
                    default=4000,
                ),
                ToolParameter(
                    name="seed",
                    type=int,
                    description="Random seed for EMC placement.",
                    default=42,
                ),
            ],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether box construction succeeded."),
                ReturnSpec(name="work_dir", type=str, description="Path to the working directory with output files."),
                ReturnSpec(name="num_atoms", type=int, description="Number of atoms in the constructed box."),
                ReturnSpec(name="data_file", type=str, description="Path to the LAMMPS data file."),
                ReturnSpec(name="params_file", type=str, description="Path to the LAMMPS params file."),
                ReturnSpec(name="error", type=str, description="Error message if construction failed."),
            ],
            module=f"{_MODULE_PREFIX}.build_box",
            server_name="vitrimer_tg_sim",
        )
    )

    # ── run_equilibration ─────────────────────────────────────────────
    registry.register_tool(
        ToolDefinition(
            name="run_equilibration",
            description=(
                "Run the vitrimer equilibration protocol in LAMMPS. Performs "
                "energy minimization, NVT relaxation (300 K, 50 ps), NPT relaxation "
                "(300 K, 1 atm, 100 ps), annealing to 800 K (500 ps), and holds at "
                "800 K to produce 5 independent restart snapshots for production runs. "
                "Requires a work_dir from build_vitrimer_box."
            ),
            required_parameters=[
                ToolParameter(
                    name="work_dir",
                    type=str,
                    description=(
                        "Working directory containing polymer.data and polymer.params from build_vitrimer_box."
                    ),
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="timeout",
                    type=int,
                    description=(
                        "Maximum wall-clock time in seconds. "
                        "Runtimes vary with system size: ~30 min for ~1000 atoms, "
                        "~90 min for ~4000 atoms. Jobs exceeding 5 days will time out."
                    ),
                    default=432000,
                ),
            ],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether equilibration completed successfully."),
                ReturnSpec(name="restart_files", type=list, description="Paths to the 5 restart snapshot files."),
                ReturnSpec(name="lammps_output", type=str, description="Last 2000 chars of LAMMPS log output."),
                ReturnSpec(name="error", type=str, description="Error message if equilibration failed."),
            ],
            module=f"{_MODULE_PREFIX}.equilibration",
            server_name="vitrimer_tg_sim",
        )
    )

    # ── run_tg_production ─────────────────────────────────────────────
    registry.register_tool(
        ToolDefinition(
            name="run_tg_production",
            description=(
                "Run 5 parallel LAMMPS cooling simulations (800 → 100 K in 10 K "
                "steps) from the restart snapshots produced by run_equilibration. "
                "Each replica executes an independent NPT cooling ramp (25 ps ramp + "
                "25 ps density-averaging hold per step). All 5 replicas run in "
                "parallel to reduce wall time by ~5×."
            ),
            required_parameters=[
                ToolParameter(
                    name="work_dir",
                    type=str,
                    description=("Working directory containing eq/restart.* files from run_equilibration."),
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="timeout_per_replica",
                    type=int,
                    description=(
                        "Max wall time per replica in seconds. This is the "
                        "longest-running step in the pipeline. Runtimes vary "
                        "with system size: ~2 h for ~1000 atoms, ~6–12 h for "
                        "~4000 atoms. Jobs exceeding 5 days will time out."
                    ),
                    default=432000,
                ),
                ToolParameter(
                    name="max_workers",
                    type=int,
                    description="Number of parallel LAMMPS processes.",
                    default=5,
                ),
            ],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether production runs completed."),
                ReturnSpec(name="replica_dirs", type=list, description="Paths to completed replica directories."),
                ReturnSpec(name="num_completed", type=int, description="Number of successfully completed replicas."),
                ReturnSpec(name="replica_results", type=list, description="Per-replica status details."),
                ReturnSpec(name="error", type=str, description="Error or warning message."),
            ],
            module=f"{_MODULE_PREFIX}.production",
            server_name="vitrimer_tg_sim",
        )
    )

    # ── compute_tg ────────────────────────────────────────────────────
    registry.register_tool(
        ToolDefinition(
            name="compute_tg",
            description=(
                "Compute the glass transition temperature (Tg) from production "
                "simulation data. Parses density–temperature profiles from each "
                "replica's cooling run, fits a bilinear (2-segment piecewise-linear) "
                "regression, and averages the breakpoints across replicas. Reports "
                "mean Tg, standard deviation, and coefficient of variation."
            ),
            required_parameters=[
                ToolParameter(
                    name="work_dir",
                    type=str,
                    description=("Working directory containing prod/replica_N/ subdirectories from run_tg_production."),
                ),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(name="success", type=bool, description="Whether Tg computation succeeded."),
                ReturnSpec(name="tg_mean", type=float, description="Mean Tg across replicas (K)."),
                ReturnSpec(name="tg_std", type=float, description="Standard deviation of Tg (K)."),
                ReturnSpec(name="tg_cv", type=float, description="Coefficient of variation (std/mean)."),
                ReturnSpec(name="tg_per_replica", type=list, description="Individual Tg values per replica (K)."),
                ReturnSpec(name="num_replicas", type=int, description="Number of replicas with valid Tg."),
                ReturnSpec(
                    name="density_temperature_summary",
                    type=list,
                    description="Per-replica summary with Tg, point count, and temperature range.",
                ),
                ReturnSpec(name="error", type=str, description="Error message if computation failed."),
            ],
            module=f"{_MODULE_PREFIX}.compute_tg",
            server_name="vitrimer_tg_sim",
        )
    )

    LOGGER.info(f"Registered {len(registry.tools)} vitrimer_tg_sim tools")
    return registry
