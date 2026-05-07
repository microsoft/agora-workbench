"""Compute physicochemical and Lipinski descriptors for a molecule."""

from rdkit import Chem
from rdkit.Chem import Descriptors, rdMolDescriptors

# Descriptor name → human label
_DESCRIPTOR_REGISTRY: dict[str, str] = {
    "molecular_weight": "Molecular weight (Da)",
    "logp": "Wildman-Crippen LogP",
    "hbd": "H-bond donors",
    "hba": "H-bond acceptors",
    "tpsa": "Topological polar surface area (Å²)",
    "rotatable_bonds": "Rotatable bond count",
    "ring_count": "Number of rings",
    "aromatic_rings": "Number of aromatic rings",
    "fraction_csp3": "Fraction of sp3-hybridized carbons",
    "heavy_atom_count": "Heavy atom count",
}

ALL_DESCRIPTOR_NAMES = sorted(_DESCRIPTOR_REGISTRY)

_CALCULATORS = {
    "molecular_weight": lambda m: round(Descriptors.MolWt(m), 4),
    "logp": lambda m: round(Descriptors.MolLogP(m), 4),
    "hbd": lambda m: Descriptors.NumHDonors(m),
    "hba": lambda m: Descriptors.NumHAcceptors(m),
    "tpsa": lambda m: round(Descriptors.TPSA(m), 4),
    "rotatable_bonds": lambda m: Descriptors.NumRotatableBonds(m),
    "ring_count": lambda m: rdMolDescriptors.CalcNumRings(m),
    "aromatic_rings": lambda m: rdMolDescriptors.CalcNumAromaticRings(m),
    "fraction_csp3": lambda m: round(Descriptors.FractionCSP3(m), 4),
    "heavy_atom_count": lambda m: m.GetNumHeavyAtoms(),
}


def compute_descriptors(smiles: str, descriptors: list | None = None) -> dict:
    """Compute physicochemical descriptors for a molecule.

    Args:
        smiles: SMILES string for the molecule.
        descriptors: Optional list of descriptor names to compute.
            If ``None`` or empty, all available descriptors are computed.
            Valid names: molecular_weight, logp, hbd, hba, tpsa,
            rotatable_bonds, ring_count, aromatic_rings, fraction_csp3,
            heavy_atom_count.

    Returns:
        Dictionary with ``smiles`` (canonical), ``descriptors`` (name→value map),
        and ``lipinski_pass`` (bool).

    Raises:
        ValueError: If the SMILES string is invalid or an unknown descriptor
            name is requested.
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"Invalid SMILES: {smiles!r}")

    requested = descriptors if descriptors else ALL_DESCRIPTOR_NAMES
    unknown = set(requested) - set(ALL_DESCRIPTOR_NAMES)
    if unknown:
        raise ValueError(f"Unknown descriptor(s): {sorted(unknown)}. Valid names: {ALL_DESCRIPTOR_NAMES}")

    results = {}
    for name in requested:
        results[name] = _CALCULATORS[name](mol)

    # Lipinski Rule of Five assessment (always computed)
    mw = Descriptors.MolWt(mol)
    logp = Descriptors.MolLogP(mol)
    hbd = Descriptors.NumHDonors(mol)
    hba = Descriptors.NumHAcceptors(mol)
    lipinski_pass = mw < 500 and logp < 5 and hbd <= 5 and hba <= 10

    return {
        "smiles": Chem.MolToSmiles(mol, isomericSmiles=True),
        "descriptors": results,
        "lipinski_pass": lipinski_pass,
    }
