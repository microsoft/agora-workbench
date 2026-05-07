"""
Tool: compute_fingerprints — High-complexity chain shared primitive.

Computes molecular fingerprints for a list of molecules. This is a key
branching node in the state graph: both ``find_similar_molecules`` and
``cluster_molecules`` depend on ``chemistry.fingerprints_computed``.
"""

from code_execution import ReturnSpec, StateTransition, ToolDefinition, ToolParameter

SUPPORTED_FINGERPRINTS = ("morgan", "rdkit", "maccs")


def compute_fingerprints(
    smiles_list: list,
    fingerprint_type: str = "morgan",
    radius: int = 2,
    n_bits: int = 2048,
) -> dict:
    """Compute molecular fingerprints for a list of SMILES.

    Args:
        smiles_list: List of SMILES strings.
        fingerprint_type: Algorithm — ``"morgan"`` (default), ``"rdkit"``,
            or ``"maccs"``.
        radius: Morgan fingerprint radius (only for ``"morgan"``).
        n_bits: Bit-vector length for Morgan/RDKit fingerprints.

    Returns:
        Dictionary with ``fingerprint_type``, ``num_molecules``,
        ``num_valid``, ``num_invalid``, and ``fingerprints`` (list of
        dicts with smiles, canonical_smiles, bit_count, and
        on_bits — the set bit positions).

    Raises:
        ValueError: If smiles_list is empty or fingerprint_type is unsupported.
    """
    from rdkit import Chem
    from rdkit.Chem import AllChem, MACCSkeys, RDKFingerprint

    if not smiles_list or not isinstance(smiles_list, list):
        raise ValueError("smiles_list must be a non-empty list of SMILES strings.")

    fp_type = fingerprint_type.lower()
    if fp_type not in SUPPORTED_FINGERPRINTS:
        raise ValueError(
            f"Unsupported fingerprint_type: {fingerprint_type!r}. Choose from: {', '.join(SUPPORTED_FINGERPRINTS)}"
        )

    def _compute_fp(mol):
        if fp_type == "morgan":
            return AllChem.GetMorganFingerprintAsBitVect(mol, radius, nBits=n_bits)
        elif fp_type == "rdkit":
            return RDKFingerprint(mol, fpSize=n_bits)
        else:  # maccs
            return MACCSkeys.GenMACCSKeys(mol)

    results = []
    num_invalid = 0

    for smi in smiles_list:
        if not isinstance(smi, str):
            num_invalid += 1
            continue

        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            num_invalid += 1
            continue

        fp = _compute_fp(mol)
        on_bits = list(fp.GetOnBits())

        results.append(
            {
                "input_smiles": smi,
                "canonical_smiles": Chem.MolToSmiles(mol, isomericSmiles=True),
                "bit_count": len(on_bits),
                "on_bits": on_bits,
            }
        )

    return {
        "fingerprint_type": fp_type,
        "num_molecules": len(smiles_list),
        "num_valid": len(results),
        "num_invalid": num_invalid,
        "fingerprints": results,
    }


TOOL_DEFINITION = ToolDefinition(
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
    module="domain_examples.chemistry.tools.compute_fingerprints",
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
