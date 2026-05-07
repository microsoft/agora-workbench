"""
Tool: parse_molecule — Low complexity.

Parses a SMILES string and returns basic molecular identity and properties.
Demonstrates the simplest ToolDefinition: one required parameter, fixed output.
"""

from code_execution import ReturnSpec, StateTransition, ToolDefinition, ToolParameter


def parse_molecule(smiles: str) -> dict:
    """Parse a SMILES string and return canonical form with basic properties.

    Args:
        smiles: A SMILES string representing a molecule (e.g. ``"CCO"``).

    Returns:
        Dictionary with canonical_smiles, molecular_formula, molecular_weight,
        num_atoms (heavy), and num_bonds.

    Raises:
        ValueError: If the SMILES string is invalid.
    """
    from rdkit import Chem
    from rdkit.Chem import Descriptors, rdMolDescriptors

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"Invalid SMILES: {smiles!r}")

    return {
        "canonical_smiles": Chem.MolToSmiles(mol, isomericSmiles=True),
        "molecular_formula": rdMolDescriptors.CalcMolFormula(mol),
        "molecular_weight": round(Descriptors.MolWt(mol), 4),
        "num_atoms": mol.GetNumHeavyAtoms(),
        "num_bonds": mol.GetNumBonds(),
    }


TOOL_DEFINITION = ToolDefinition(
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
    module="domain_examples.chemistry.tools.parse_molecule",
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
