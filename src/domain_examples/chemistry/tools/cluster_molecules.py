"""
Tool: cluster_molecules — High complexity (chain step 3).

Clusters a set of molecules by fingerprint similarity using Butina
clustering (Taylor–Butina). Chains from ``compute_fingerprints`` via the
``chemistry.fingerprints_computed`` → ``chemistry.molecules_clustered``
state edge, demonstrating a branching graph where both this tool and
``find_similar_molecules`` share the same prerequisite state.
"""

from code_execution import ReturnSpec, StateTransition, ToolDefinition, ToolParameter

SUPPORTED_FINGERPRINTS = ("morgan", "rdkit", "maccs")


def cluster_molecules(
    smiles_list: list,
    cutoff: float = 0.5,
    fingerprint_type: str = "morgan",
    radius: int = 2,
    n_bits: int = 2048,
) -> dict:
    """Cluster molecules by fingerprint similarity using Butina clustering.

    The Butina algorithm is a sphere-exclusion method that groups molecules
    whose Tanimoto distance is within *cutoff* of a cluster centroid.

    Args:
        smiles_list: List of SMILES strings to cluster.
        cutoff: Tanimoto distance cutoff (0.0–1.0). Smaller values give
            tighter, more numerous clusters. Default 0.5.
        fingerprint_type: Fingerprint algorithm — ``"morgan"`` (default),
            ``"rdkit"``, or ``"maccs"``.
        radius: Morgan fingerprint radius.
        n_bits: Bit-vector length for Morgan/RDKit fingerprints.

    Returns:
        Dictionary with ``num_molecules``, ``num_clusters``,
        ``cutoff``, ``fingerprint_type``, and ``clusters`` (list of dicts
        with cluster_id, centroid, and members).

    Raises:
        ValueError: If smiles_list has fewer than 2 valid molecules or
            cutoff is out of range.
    """
    from rdkit import Chem, DataStructs
    from rdkit.Chem import AllChem, MACCSkeys, RDKFingerprint
    from rdkit.ML.Cluster import Butina

    if not smiles_list or not isinstance(smiles_list, list):
        raise ValueError("smiles_list must be a non-empty list of SMILES strings.")

    if not 0.0 < cutoff < 1.0:
        raise ValueError(f"cutoff must be between 0.0 and 1.0 (exclusive), got {cutoff}")

    fp_type = fingerprint_type.lower()
    if fp_type not in SUPPORTED_FINGERPRINTS:
        raise ValueError(
            f"Unsupported fingerprint_type: {fingerprint_type!r}. Choose from: {', '.join(SUPPORTED_FINGERPRINTS)}"
        )

    # Parse molecules and compute fingerprints
    mols = []
    canonical_smiles = []
    input_smiles = []

    for smi in smiles_list:
        if not isinstance(smi, str):
            continue
        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            continue
        mols.append(mol)
        canonical_smiles.append(Chem.MolToSmiles(mol, isomericSmiles=True))
        input_smiles.append(smi)

    if len(mols) < 2:
        raise ValueError(f"Need at least 2 valid molecules for clustering, got {len(mols)}.")

    def _compute_fp(mol):
        if fp_type == "morgan":
            return AllChem.GetMorganFingerprintAsBitVect(mol, radius, nBits=n_bits)
        elif fp_type == "rdkit":
            return RDKFingerprint(mol, fpSize=n_bits)
        else:
            return MACCSkeys.GenMACCSKeys(mol)

    fps = [_compute_fp(m) for m in mols]

    # Compute pairwise Tanimoto distance matrix (upper triangle)
    n = len(fps)
    dists = []
    for i in range(1, n):
        for j in range(i):
            dists.append(1.0 - DataStructs.TanimotoSimilarity(fps[i], fps[j]))

    # Butina clustering
    cluster_indices = Butina.ClusterData(dists, n, cutoff, isDistData=True)

    clusters = []
    for cluster_id, members in enumerate(cluster_indices):
        centroid_idx = members[0]  # Butina puts centroid first
        clusters.append(
            {
                "cluster_id": cluster_id,
                "size": len(members),
                "centroid": {
                    "index": centroid_idx,
                    "input_smiles": input_smiles[centroid_idx],
                    "canonical_smiles": canonical_smiles[centroid_idx],
                },
                "members": [
                    {
                        "index": idx,
                        "input_smiles": input_smiles[idx],
                        "canonical_smiles": canonical_smiles[idx],
                    }
                    for idx in members
                ],
            }
        )

    # Sort clusters by size descending
    clusters.sort(key=lambda c: c["size"], reverse=True)

    return {
        "num_molecules": len(mols),
        "num_clusters": len(clusters),
        "cutoff": cutoff,
        "fingerprint_type": fp_type,
        "clusters": clusters,
    }


TOOL_DEFINITION = ToolDefinition(
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
    module="domain_examples.chemistry.tools.cluster_molecules",
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
