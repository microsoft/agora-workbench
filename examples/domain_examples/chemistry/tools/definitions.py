"""
Tool definitions (metadata) for the chemistry domain.

This module contains only ``ToolDefinition`` objects — the server-side
schemas, state transitions, and affordances. The actual implementations
live in the ``chemistry_tools`` pip package, which is installed into the
execution environment at build time.

The ``module`` field on each definition points to the installed package
(e.g. ``chemistry_tools.parse_molecule``), ensuring the kernel's lazy
``from {module} import {name}`` import resolves correctly.
"""

from code_execution import ReturnSpec, StateTransition, ToolDefinition, ToolParameter

# ============================================================================
# Low complexity: Molecular Analysis chain
# ============================================================================

parse_molecule = ToolDefinition(
    name="parse_molecule",
    description=(
        "Parse a SMILES string and return canonical SMILES, molecular formula, "
        "molecular weight, heavy-atom count, and bond count."
    ),
    required_parameters=[
        ToolParameter(name="smiles", type=str, description="SMILES string to parse"),
    ],
    return_spec=[
        ReturnSpec(name="canonical_smiles", type=str, description="Canonical SMILES"),
        ReturnSpec(name="molecular_formula", type=str, description="Molecular formula (e.g. C2H6O)"),
        ReturnSpec(name="molecular_weight", type=float, description="Molecular weight in Da"),
        ReturnSpec(name="num_atoms", type=int, description="Number of heavy (non-hydrogen) atoms"),
        ReturnSpec(name="num_bonds", type=int, description="Number of bonds"),
    ],
    state_transition=StateTransition(
        produces=frozenset({"chemistry.molecule_parsed"}),
    ),
    affordances=[
        "parse a SMILES string",
        "get the molecular weight of a compound",
        "canonicalize SMILES",
        "get the molecular formula",
    ],
)

enumerate_functional_groups = ToolDefinition(
    name="enumerate_functional_groups",
    description=(
        "Identify common functional groups (hydroxyl, carboxyl, amine, ester, "
        "etc.) in a molecule using SMARTS pattern matching. Returns group names, "
        "match counts, and atom indices."
    ),
    required_parameters=[
        ToolParameter(name="smiles", type=str, description="SMILES string for the molecule"),
    ],
    return_spec=[
        ReturnSpec(name="smiles", type=str, description="Canonical SMILES"),
        ReturnSpec(
            name="groups_found",
            type=list,
            description="List of identified functional groups with counts and atom indices",
        ),
        ReturnSpec(name="num_groups_found", type=int, description="Number of distinct functional group types found"),
    ],
    state_transition=StateTransition(
        requires=frozenset({"chemistry.molecule_parsed"}),
        produces=frozenset({"chemistry.groups_identified"}),
    ),
    affordances=[
        "identify functional groups",
        "find hydroxyl or carboxyl groups",
        "SMARTS substructure matching",
        "characterize molecule reactivity",
    ],
)

# ============================================================================
# Medium complexity: Drug Screening chain
# ============================================================================

compute_descriptors = ToolDefinition(
    name="compute_descriptors",
    description=(
        "Compute physicochemical descriptors (MW, LogP, HBD, HBA, TPSA, "
        "rotatable bonds, ring count, etc.) for a molecule given its SMILES. "
        "Also evaluates Lipinski's Rule of Five."
    ),
    required_parameters=[
        ToolParameter(name="smiles", type=str, description="SMILES string for the molecule"),
    ],
    optional_parameters=[
        ToolParameter(
            name="descriptors",
            type=list,
            description=(
                "List of descriptor names to compute. If omitted, all descriptors are "
                "returned. Valid names: molecular_weight, logp, hbd, hba, tpsa, "
                "rotatable_bonds, ring_count, aromatic_rings, fraction_csp3, heavy_atom_count."
            ),
            default=None,
        ),
    ],
    return_spec=[
        ReturnSpec(name="smiles", type=str, description="Canonical SMILES"),
        ReturnSpec(name="descriptors", type=dict, description="Mapping of descriptor name to computed value"),
        ReturnSpec(name="lipinski_pass", type=bool, description="Whether the molecule passes Lipinski's Rule of Five"),
    ],
    state_transition=StateTransition(
        requires=frozenset({"chemistry.molecule_parsed"}),
        produces=frozenset({"chemistry.descriptors_computed"}),
    ),
    affordances=[
        "compute molecular descriptors",
        "check Lipinski's Rule of Five",
        "calculate LogP",
        "calculate TPSA",
        "evaluate drug-likeness",
    ],
)

filter_drug_candidates = ToolDefinition(
    name="filter_drug_candidates",
    description=(
        "Screen a list of molecules against drug-likeness rules. Supports "
        "Lipinski's Rule of Five, Veber's rules (rotatable bonds ≤ 10, "
        "TPSA ≤ 140 Å²), or both combined. Returns passed and failed "
        "molecules with computed properties and failure reasons."
    ),
    required_parameters=[
        ToolParameter(
            name="smiles_list",
            type=list,
            description="List of SMILES strings to screen",
        ),
    ],
    optional_parameters=[
        ToolParameter(
            name="rules",
            type=str,
            description='Rule set: "lipinski" (default), "veber", or "both"',
            default="lipinski",
        ),
    ],
    return_spec=[
        ReturnSpec(name="rules", type=str, description="Rule set applied"),
        ReturnSpec(name="num_input", type=int, description="Total molecules evaluated"),
        ReturnSpec(name="num_passed", type=int, description="Molecules passing all rules"),
        ReturnSpec(name="num_failed", type=int, description="Molecules failing one or more rules"),
        ReturnSpec(name="passed", type=list, description="Molecules that passed with properties"),
        ReturnSpec(name="failed", type=list, description="Molecules that failed with reasons"),
    ],
    state_transition=StateTransition(
        requires=frozenset({"chemistry.descriptors_computed"}),
        produces=frozenset({"chemistry.candidates_filtered"}),
    ),
    affordances=[
        "screen compounds for drug-likeness",
        "filter by Lipinski's Rule of Five",
        "filter by Veber's rules",
        "identify drug candidates from a library",
    ],
)

# ============================================================================
# High complexity: Similarity & Clustering chain (branching graph)
# ============================================================================

compute_fingerprints = ToolDefinition(
    name="compute_fingerprints",
    description=(
        "Compute molecular fingerprints for a list of SMILES strings. "
        "Supports Morgan (ECFP), RDKit, and MACCS fingerprint types. "
        "Returns bit-vector data for each molecule. This is a prerequisite "
        "for similarity search and clustering."
    ),
    required_parameters=[
        ToolParameter(
            name="smiles_list",
            type=list,
            description="List of SMILES strings to fingerprint",
        ),
    ],
    optional_parameters=[
        ToolParameter(
            name="fingerprint_type",
            type=str,
            description='Fingerprint algorithm: "morgan" (default), "rdkit", or "maccs"',
            default="morgan",
        ),
        ToolParameter(
            name="radius",
            type=int,
            description="Morgan fingerprint radius (only for morgan type)",
            default=2,
        ),
        ToolParameter(
            name="n_bits",
            type=int,
            description="Bit-vector length for Morgan/RDKit fingerprints",
            default=2048,
        ),
    ],
    return_spec=[
        ReturnSpec(name="fingerprint_type", type=str, description="Fingerprint algorithm used"),
        ReturnSpec(name="num_molecules", type=int, description="Total input molecules"),
        ReturnSpec(name="num_valid", type=int, description="Successfully fingerprinted"),
        ReturnSpec(name="num_invalid", type=int, description="Skipped (invalid SMILES)"),
        ReturnSpec(name="fingerprints", type=list, description="Per-molecule fingerprint data"),
    ],
    state_transition=StateTransition(
        requires=frozenset({"chemistry.molecule_parsed"}),
        produces=frozenset({"chemistry.fingerprints_computed"}),
    ),
    affordances=[
        "generate molecular fingerprints",
        "compute Morgan or MACCS fingerprints",
        "prepare molecules for similarity search",
        "prepare molecules for clustering",
    ],
)

find_similar_molecules = ToolDefinition(
    name="find_similar_molecules",
    description=(
        "Search a list of candidate molecules for those similar to a query molecule "
        "using fingerprint-based Tanimoto similarity. Supports Morgan, RDKit, and "
        "MACCS fingerprints. Returns ranked matches above a similarity threshold."
    ),
    required_parameters=[
        ToolParameter(name="query_smiles", type=str, description="SMILES string for the query molecule"),
        ToolParameter(
            name="candidate_smiles_list",
            type=list,
            description="List of SMILES strings to search against",
        ),
    ],
    optional_parameters=[
        ToolParameter(
            name="threshold",
            type=float,
            description="Minimum Tanimoto similarity (0.0–1.0) to include in results",
            default=0.7,
        ),
        ToolParameter(
            name="fingerprint_type",
            type=str,
            description='Fingerprint algorithm: "morgan" (default), "rdkit", or "maccs"',
            default="morgan",
        ),
        ToolParameter(
            name="radius",
            type=int,
            description="Morgan fingerprint radius (only for morgan type)",
            default=2,
        ),
        ToolParameter(
            name="n_bits",
            type=int,
            description="Bit-vector length for Morgan/RDKit fingerprints",
            default=2048,
        ),
    ],
    return_spec=[
        ReturnSpec(name="query_smiles", type=str, description="Canonical SMILES of the query"),
        ReturnSpec(name="fingerprint_type", type=str, description="Fingerprint algorithm used"),
        ReturnSpec(name="threshold", type=float, description="Similarity threshold applied"),
        ReturnSpec(name="num_candidates", type=int, description="Total candidates evaluated"),
        ReturnSpec(name="num_matches", type=int, description="Number of matches above threshold"),
        ReturnSpec(name="matches", type=list, description="Ranked list of matching molecules with similarity scores"),
    ],
    state_transition=StateTransition(
        requires=frozenset({"chemistry.fingerprints_computed"}),
        produces=frozenset({"chemistry.similarity_computed"}),
    ),
    affordances=[
        "find similar molecules",
        "molecular similarity search",
        "compare molecules by fingerprint",
        "Tanimoto similarity screening",
        "virtual screening by similarity",
    ],
)

cluster_molecules = ToolDefinition(
    name="cluster_molecules",
    description=(
        "Cluster a set of molecules by fingerprint similarity using Butina "
        "(sphere-exclusion) clustering. Groups molecules whose Tanimoto "
        "distance is within a cutoff of a cluster centroid. Returns cluster "
        "assignments with centroids and members."
    ),
    required_parameters=[
        ToolParameter(
            name="smiles_list",
            type=list,
            description="List of SMILES strings to cluster",
        ),
    ],
    optional_parameters=[
        ToolParameter(
            name="cutoff",
            type=float,
            description="Tanimoto distance cutoff (0.0–1.0). Smaller = tighter clusters",
            default=0.5,
        ),
        ToolParameter(
            name="fingerprint_type",
            type=str,
            description='Fingerprint algorithm: "morgan" (default), "rdkit", or "maccs"',
            default="morgan",
        ),
        ToolParameter(
            name="radius",
            type=int,
            description="Morgan fingerprint radius (only for morgan type)",
            default=2,
        ),
        ToolParameter(
            name="n_bits",
            type=int,
            description="Bit-vector length for Morgan/RDKit fingerprints",
            default=2048,
        ),
    ],
    return_spec=[
        ReturnSpec(name="num_molecules", type=int, description="Valid molecules clustered"),
        ReturnSpec(name="num_clusters", type=int, description="Number of clusters formed"),
        ReturnSpec(name="cutoff", type=float, description="Distance cutoff used"),
        ReturnSpec(name="fingerprint_type", type=str, description="Fingerprint algorithm used"),
        ReturnSpec(name="clusters", type=list, description="Cluster assignments with centroids and members"),
    ],
    state_transition=StateTransition(
        requires=frozenset({"chemistry.fingerprints_computed"}),
        produces=frozenset({"chemistry.molecules_clustered"}),
    ),
    affordances=[
        "cluster a molecular library",
        "group similar molecules",
        "Butina clustering",
        "chemical series analysis",
        "diversity analysis",
    ],
)
