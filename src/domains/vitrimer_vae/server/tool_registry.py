"""
Vitrimer VAE Tool Registry.

Defines domain-specific tools for AI-guided inverse design of recyclable
vitrimeric polymers, exposed as native MCP tools by the vitrimer_vae server.
"""

import logging

from code_execution import ToolRegistry, ToolDefinition, ToolParameter, ReturnSpec

LOGGER = logging.getLogger(__name__)


def create_vitrimer_vae_tool_registry() -> ToolRegistry:
    """
    Create tool registry for the vitrimer_vae domain.

    Returns:
        ToolRegistry containing all vitrimer VAE domain tools.
    """
    registry = ToolRegistry()

    # ── sample_molecules ──────────────────────────────────────────────
    registry.register_tool(
        ToolDefinition(
            name="sample_molecules",
            description=(
                "Generate novel vitrimer molecules by sampling from the VAE "
                "latent space. Returns pairs of acid and epoxide SMILES with "
                "predicted glass transition temperatures (Tg)."
            ),
            required_parameters=[],
            optional_parameters=[
                ToolParameter(
                    name="num_samples",
                    type=int,
                    description="Number of valid molecules to generate.",
                    default=20,
                ),
                ToolParameter(
                    name="seed",
                    type=int,
                    description="Random seed for reproducibility.",
                    default=1,
                ),
            ],
            return_spec=[
                ReturnSpec(name="acids", type=list, description="List of acid SMILES strings."),
                ReturnSpec(name="epoxides", type=list, description="List of epoxide SMILES strings."),
                ReturnSpec(name="tg_predicted", type=list, description="List of predicted Tg values (K)."),
                ReturnSpec(name="num_valid", type=int, description="Number of valid molecules generated."),
                ReturnSpec(name="num_attempted", type=int, description="Total number of samples attempted."),
            ],
            module="vitrimer_vae_tools.tools.sample",
            server_name="vitrimer_vae",
        )
    )

    # ── predict_tg ────────────────────────────────────────────────────
    registry.register_tool(
        ToolDefinition(
            name="predict_tg",
            description=(
                "Predict the glass transition temperature (Tg) for given "
                "acid/epoxide SMILES pairs by encoding through the VAE and "
                "using the property prediction head."
            ),
            required_parameters=[
                ToolParameter(
                    name="acid_smiles",
                    type=list,
                    description="List of acid SMILES strings.",
                ),
                ToolParameter(
                    name="epoxide_smiles",
                    type=list,
                    description="List of epoxide SMILES strings.",
                ),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(name="tg_predicted", type=list, description="List of predicted Tg values (K)."),
                ReturnSpec(name="latent_vectors", type=list, description="Latent z vectors for each pair."),
            ],
            module="vitrimer_vae_tools.tools.predict",
            server_name="vitrimer_vae",
        )
    )

    # ── search_neighbors ──────────────────────────────────────────────
    registry.register_tool(
        ToolDefinition(
            name="search_neighbors",
            description=(
                "Find similar vitrimer molecules near a query compound by "
                "perturbing its latent vector with Gaussian noise. Supports "
                "acid-only, epoxide-only, or full-space search."
            ),
            required_parameters=[
                ToolParameter(
                    name="acid_smiles",
                    type=str,
                    description="Query acid SMILES string.",
                ),
                ToolParameter(
                    name="epoxide_smiles",
                    type=str,
                    description="Query epoxide SMILES string.",
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="search_type",
                    type=str,
                    description='Latent subspace to perturb: "acid", "epoxide", or "both".',
                    default="both",
                ),
                ToolParameter(
                    name="num_neighbors",
                    type=int,
                    description="Number of noise samples to generate.",
                    default=100,
                ),
                ToolParameter(
                    name="max_noise",
                    type=float,
                    description="Maximum noise magnitude.",
                    default=20.0,
                ),
                ToolParameter(
                    name="seed",
                    type=int,
                    description="Random seed.",
                    default=1,
                ),
            ],
            return_spec=[
                ReturnSpec(name="acids", type=list, description="Neighbor acid SMILES (sorted by distance)."),
                ReturnSpec(name="epoxides", type=list, description="Neighbor epoxide SMILES."),
                ReturnSpec(name="tg_predicted", type=list, description="Predicted Tg for each neighbor (K)."),
                ReturnSpec(name="distances", type=list, description="Latent-space distance from query."),
                ReturnSpec(name="pca_coords", type=list, description="PCA coordinates for visualization."),
            ],
            module="vitrimer_vae_tools.tools.search",
            server_name="vitrimer_vae",
        )
    )

    # ── interpolate_molecules ─────────────────────────────────────────
    registry.register_tool(
        ToolDefinition(
            name="interpolate_molecules",
            description=(
                "Generate intermediate vitrimer molecules by interpolating "
                "between two endpoints in the VAE latent space. Supports "
                "linear and spherical (great-circle) interpolation."
            ),
            required_parameters=[
                ToolParameter(name="acid1", type=str, description="Start-point acid SMILES."),
                ToolParameter(name="epoxide1", type=str, description="Start-point epoxide SMILES."),
                ToolParameter(name="acid2", type=str, description="End-point acid SMILES."),
                ToolParameter(name="epoxide2", type=str, description="End-point epoxide SMILES."),
            ],
            optional_parameters=[
                ToolParameter(
                    name="method",
                    type=str,
                    description='Interpolation method: "linear" or "spherical".',
                    default="linear",
                ),
                ToolParameter(
                    name="num_points",
                    type=int,
                    description="Number of intermediate points to generate.",
                    default=20,
                ),
                ToolParameter(name="seed", type=int, description="Random seed.", default=5),
            ],
            return_spec=[
                ReturnSpec(name="acids", type=list, description="Acid SMILES along the path."),
                ReturnSpec(name="epoxides", type=list, description="Epoxide SMILES along the path."),
                ReturnSpec(name="tg_predicted", type=list, description="Predicted Tg along the path (K)."),
                ReturnSpec(name="distances", type=list, description="Distances from start point."),
                ReturnSpec(name="pca_coords", type=list, description="PCA coordinates for visualization."),
            ],
            module="vitrimer_vae_tools.tools.interpolate",
            server_name="vitrimer_vae",
        )
    )

    # ── calibrate_tg ──────────────────────────────────────────────────
    registry.register_tool(
        ToolDefinition(
            name="calibrate_tg",
            description=(
                "Calibrate MD-simulated glass transition temperatures against "
                "experimental data using a Gaussian Process with Tanimoto kernel "
                "on Morgan fingerprints."
            ),
            required_parameters=[
                ToolParameter(
                    name="acid_smiles",
                    type=list,
                    description="List of acid SMILES strings.",
                ),
                ToolParameter(
                    name="epoxide_smiles",
                    type=list,
                    description="List of epoxide SMILES strings.",
                ),
                ToolParameter(
                    name="tg_md",
                    type=list,
                    description="List of MD-simulated Tg values (K).",
                ),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(name="tg_calibrated", type=list, description="Calibrated Tg values (K)."),
                ReturnSpec(name="vitrimer_smiles", type=list, description="Generated vitrimer SMILES."),
            ],
            module="vitrimer_vae_tools.tools.calibrate",
            server_name="vitrimer_vae",
        )
    )

    # ── reconstruct_molecules ─────────────────────────────────────────
    registry.register_tool(
        ToolDefinition(
            name="reconstruct_molecules",
            description=(
                "Reconstruct vitrimer molecules through the VAE encoder-decoder "
                "pipeline to assess reconstruction fidelity. Compares input SMILES "
                "with round-trip decoded SMILES."
            ),
            required_parameters=[
                ToolParameter(
                    name="acid_smiles",
                    type=list,
                    description="List of acid SMILES strings.",
                ),
                ToolParameter(
                    name="epoxide_smiles",
                    type=list,
                    description="List of epoxide SMILES strings.",
                ),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(name="acid_original", type=list, description="Original acid SMILES."),
                ReturnSpec(name="epoxide_original", type=list, description="Original epoxide SMILES."),
                ReturnSpec(name="acid_reconstructed", type=list, description="Reconstructed acid SMILES."),
                ReturnSpec(name="epoxide_reconstructed", type=list, description="Reconstructed epoxide SMILES."),
                ReturnSpec(name="acid_match", type=list, description="Per-molecule acid match (bool)."),
                ReturnSpec(name="epoxide_match", type=list, description="Per-molecule epoxide match (bool)."),
                ReturnSpec(name="reconstruction_accuracy", type=float, description="Fraction of exact matches."),
            ],
            module="vitrimer_vae_tools.tools.reconstruct",
            server_name="vitrimer_vae",
        )
    )

    # ── bayesian_optimize ─────────────────────────────────────────────
    registry.register_tool(
        ToolDefinition(
            name="bayesian_optimize",
            description=(
                "Run Bayesian optimization in the VAE latent space to discover "
                "vitrimer molecules with a target glass transition temperature (Tg). "
                "Iteratively trains a GP surrogate and uses expected improvement "
                "acquisition to propose candidates."
            ),
            required_parameters=[],
            optional_parameters=[
                ToolParameter(
                    name="target_tg",
                    type=float,
                    description="Target Tg in Kelvin (ignored if maximize=True).",
                    default=373.0,
                ),
                ToolParameter(
                    name="maximize",
                    type=bool,
                    description="If True, maximize Tg instead of targeting a specific value.",
                    default=False,
                ),
                ToolParameter(
                    name="num_iterations",
                    type=int,
                    description="Number of BO iterations.",
                    default=50,
                ),
                ToolParameter(
                    name="candidates_per_iteration",
                    type=int,
                    description="Number of candidate points per iteration.",
                    default=50,
                ),
                ToolParameter(
                    name="pool_size",
                    type=int,
                    description="Size of initial random molecule pool.",
                    default=1000,
                ),
                ToolParameter(
                    name="seed",
                    type=int,
                    description="Random seed.",
                    default=1,
                ),
            ],
            return_spec=[
                ReturnSpec(name="acids", type=list, description="Discovered acid SMILES."),
                ReturnSpec(name="epoxides", type=list, description="Discovered epoxide SMILES."),
                ReturnSpec(name="tg_predicted", type=list, description="Predicted Tg for each discovery (K)."),
                ReturnSpec(name="iterations", type=list, description="BO iteration when each molecule was found."),
                ReturnSpec(name="pca_coords", type=list, description="PCA coordinates for visualization."),
                ReturnSpec(name="best_tg", type=float, description="Tg of the best molecule found."),
            ],
            module="vitrimer_vae_tools.tools.optimize",
            server_name="vitrimer_vae",
        )
    )

    LOGGER.info(f"Registered {len(registry.tools)} vitrimer_vae tools")
    return registry
