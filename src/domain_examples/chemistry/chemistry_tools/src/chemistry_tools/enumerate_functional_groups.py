"""Identify common functional groups in a molecule using SMARTS patterns."""

from rdkit import Chem

# SMARTS patterns for common functional groups
_FUNCTIONAL_GROUPS: dict[str, str] = {
    "hydroxyl": "[OX2H]",
    "carboxyl": "[CX3](=O)[OX2H1]",
    "amine_primary": "[NX3H2]",
    "amine_secondary": "[NX3H1]([#6])[#6]",
    "amine_tertiary": "[NX3]([#6])([#6])[#6]",
    "amide": "[NX3][CX3](=[OX1])[#6]",
    "aldehyde": "[CX3H1](=O)[#6]",
    "ketone": "[#6][CX3](=O)[#6]",
    "ester": "[#6][CX3](=O)[OX2H0][#6]",
    "ether": "[OD2]([#6])[#6]",
    "nitro": "[$([NX3](=O)=O),$([NX3+](=O)[O-])][!#8]",
    "sulfhydryl": "[#16X2H]",
    "phenol": "[OX2H][cX3]:[c]",
    "halide": "[#6][F,Cl,Br,I]",
    "nitrile": "[NX1]#[CX2]",
    "phosphate": "[$(P(=[OX1])(O)(O)O)]",
}


def enumerate_functional_groups(smiles: str) -> dict:
    """Identify common functional groups in a molecule via SMARTS matching.

    Args:
        smiles: SMILES string for the molecule.

    Returns:
        Dictionary with ``smiles`` (canonical), ``groups_found`` (list of
        dicts with name, smarts, count, and atom indices), and
        ``num_groups_found``.

    Raises:
        ValueError: If the SMILES string is invalid.
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"Invalid SMILES: {smiles!r}")

    groups_found = []
    for name, smarts in _FUNCTIONAL_GROUPS.items():
        pattern = Chem.MolFromSmarts(smarts)
        if pattern is None:
            continue
        matches = mol.GetSubstructMatches(pattern)
        if matches:
            groups_found.append(
                {
                    "name": name,
                    "smarts": smarts,
                    "count": len(matches),
                    "atom_indices": [list(m) for m in matches],
                }
            )

    return {
        "smiles": Chem.MolToSmiles(mol, isomericSmiles=True),
        "groups_found": groups_found,
        "num_groups_found": len(groups_found),
    }
