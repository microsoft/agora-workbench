"""
Tool: enumerate_functional_groups — Low complexity (chain step 2).

Identifies common functional groups present in a molecule using SMARTS
pattern matching. Chains from ``parse_molecule`` via the
``chemistry.molecule_parsed`` → ``chemistry.groups_identified`` state edge.
"""

from code_execution import ReturnSpec, StateTransition, ToolDefinition, ToolParameter

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
    from rdkit import Chem

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


TOOL_DEFINITION = ToolDefinition(
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
    module="domain_examples.chemistry.tools.enumerate_functional_groups",
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
