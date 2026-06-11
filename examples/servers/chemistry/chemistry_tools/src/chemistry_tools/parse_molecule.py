"""Parse a SMILES string and return basic molecular identity and properties."""

from rdkit import Chem
from rdkit.Chem import Descriptors, rdMolDescriptors


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
