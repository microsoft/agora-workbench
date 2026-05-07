"""
Tool: compute_descriptors — Medium complexity.

Computes physicochemical and Lipinski descriptors for a molecule.
Demonstrates optional parameters (descriptor subset selection) and richer output.
"""

from code_execution import ReturnSpec, StateTransition, ToolDefinition, ToolParameter

# Descriptor name → (label, RDKit function path)
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
    from rdkit import Chem
    from rdkit.Chem import Descriptors as Desc, rdMolDescriptors

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"Invalid SMILES: {smiles!r}")

    requested = descriptors if descriptors else ALL_DESCRIPTOR_NAMES
    unknown = set(requested) - set(ALL_DESCRIPTOR_NAMES)
    if unknown:
        raise ValueError(f"Unknown descriptor(s): {sorted(unknown)}. Valid names: {ALL_DESCRIPTOR_NAMES}")

    # Compute each requested descriptor
    _calculators = {
        "molecular_weight": lambda m: round(Desc.MolWt(m), 4),
        "logp": lambda m: round(Desc.MolLogP(m), 4),
        "hbd": lambda m: Desc.NumHDonors(m),
        "hba": lambda m: Desc.NumHAcceptors(m),
        "tpsa": lambda m: round(Desc.TPSA(m), 4),
        "rotatable_bonds": lambda m: Desc.NumRotatableBonds(m),
        "ring_count": lambda m: rdMolDescriptors.CalcNumRings(m),
        "aromatic_rings": lambda m: rdMolDescriptors.CalcNumAromaticRings(m),
        "fraction_csp3": lambda m: round(Desc.FractionCSP3(m), 4),
        "heavy_atom_count": lambda m: m.GetNumHeavyAtoms(),
    }

    results = {}
    for name in requested:
        results[name] = _calculators[name](mol)

    # Lipinski Rule of Five assessment (always computed)
    mw = Desc.MolWt(mol)
    logp = Desc.MolLogP(mol)
    hbd = Desc.NumHDonors(mol)
    hba = Desc.NumHAcceptors(mol)
    lipinski_pass = mw < 500 and logp < 5 and hbd <= 5 and hba <= 10

    return {
        "smiles": Chem.MolToSmiles(mol, isomericSmiles=True),
        "descriptors": results,
        "lipinski_pass": lipinski_pass,
    }


TOOL_DEFINITION = ToolDefinition(
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
    module="domain_examples.chemistry.tools.compute_descriptors",
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
