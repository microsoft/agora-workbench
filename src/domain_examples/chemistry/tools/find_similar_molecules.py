"""
Tool: find_similar_molecules — High complexity.

Performs fingerprint-based molecular similarity search against a candidate list.
Demonstrates multiple required and optional parameters, list I/O, algorithm
selection, input validation, and ranked output.
"""

from code_execution import ReturnSpec, StateTransition, ToolDefinition, ToolParameter

SUPPORTED_FINGERPRINTS = ("morgan", "rdkit", "maccs")


def find_similar_molecules(
    query_smiles: str,
    candidate_smiles_list: list,
    threshold: float = 0.7,
    fingerprint_type: str = "morgan",
    radius: int = 2,
    n_bits: int = 2048,
) -> dict:
    """Find molecules similar to a query using fingerprint-based Tanimoto similarity.

    Args:
        query_smiles: SMILES string for the query molecule.
        candidate_smiles_list: List of SMILES strings to compare against.
        threshold: Minimum Tanimoto similarity to include in results (0.0–1.0).
        fingerprint_type: Fingerprint algorithm — ``"morgan"`` (default),
            ``"rdkit"``, or ``"maccs"``.
        radius: Morgan fingerprint radius (only used when fingerprint_type is
            ``"morgan"``). Default 2.
        n_bits: Bit-vector length for Morgan/RDKit fingerprints. Default 2048.

    Returns:
        Dictionary with ``query_smiles`` (canonical), ``fingerprint_type``,
        ``threshold``, ``num_candidates``, ``num_matches``, and ``matches``
        (list of dicts sorted by descending similarity).

    Raises:
        ValueError: If query SMILES is invalid, candidate list is empty, or
            fingerprint_type is unsupported.
    """
    from rdkit import Chem, DataStructs
    from rdkit.Chem import AllChem, MACCSkeys, RDKFingerprint

    # --- Validate inputs ---
    if not candidate_smiles_list or not isinstance(candidate_smiles_list, list):
        raise ValueError("candidate_smiles_list must be a non-empty list of SMILES strings.")

    fp_type = fingerprint_type.lower()
    if fp_type not in SUPPORTED_FINGERPRINTS:
        raise ValueError(
            f"Unsupported fingerprint_type: {fingerprint_type!r}. Choose from: {', '.join(SUPPORTED_FINGERPRINTS)}"
        )

    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"threshold must be between 0.0 and 1.0, got {threshold}")

    query_mol = Chem.MolFromSmiles(query_smiles)
    if query_mol is None:
        raise ValueError(f"Invalid query SMILES: {query_smiles!r}")

    # --- Fingerprint helper ---
    def _compute_fp(mol):
        if fp_type == "morgan":
            return AllChem.GetMorganFingerprintAsBitVect(mol, radius, nBits=n_bits)
        elif fp_type == "rdkit":
            return RDKFingerprint(mol, fpSize=n_bits)
        else:  # maccs
            return MACCSkeys.GenMACCSKeys(mol)

    query_fp = _compute_fp(query_mol)

    # --- Search ---
    matches = []
    for idx, candidate_smi in enumerate(candidate_smiles_list):
        if not isinstance(candidate_smi, str):
            continue

        cand_mol = Chem.MolFromSmiles(candidate_smi)
        if cand_mol is None:
            continue

        cand_fp = _compute_fp(cand_mol)
        sim = DataStructs.TanimotoSimilarity(query_fp, cand_fp)

        if sim >= threshold:
            matches.append(
                {
                    "index": idx,
                    "input_smiles": candidate_smi,
                    "canonical_smiles": Chem.MolToSmiles(cand_mol, isomericSmiles=True),
                    "similarity": round(sim, 4),
                }
            )

    matches.sort(key=lambda m: m["similarity"], reverse=True)

    return {
        "query_smiles": Chem.MolToSmiles(query_mol, isomericSmiles=True),
        "fingerprint_type": fp_type,
        "threshold": threshold,
        "num_candidates": len(candidate_smiles_list),
        "num_matches": len(matches),
        "matches": matches,
    }


TOOL_DEFINITION = ToolDefinition(
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
    module="domain_examples.chemistry.tools.find_similar_molecules",
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
