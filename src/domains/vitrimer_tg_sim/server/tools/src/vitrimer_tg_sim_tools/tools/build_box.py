"""
Build a vitrimer simulation box from acid + epoxide SMILES.

Uses EMC (Enhanced Monte Carlo) with the PCFF force field to construct
an alternating copolymer box suitable for Tg estimation via MD.

Accepts either polymerizable SMILES (with ``*`` connection points) or
standard molecule SMILES (with carboxylic acid / epoxide groups).
Standard SMILES are automatically converted to polymerizable form.
"""

import logging
import os
import tempfile

from rdkit import Chem

from vitrimer_tg_sim_tools.emc_builder import build_vitrimer_box as _build_box

LOGGER = logging.getLogger(__name__)

# SMARTS patterns for functional groups
_ACID_PATTERN = Chem.MolFromSmarts("[CX3](=O)[OX2H1]")
_EPOXIDE_PATTERN = Chem.MolFromSmarts("[OD2r3]1[#6r3][#6r3]1")


def _acid_to_polymerizable(smiles: str) -> str:
    """
    Convert a dicarboxylic acid SMILES to polymerizable form.

    Replaces the ``-OH`` of each ``-C(=O)OH`` group with ``*`` so that
    ``O=C(O)CCCCC(=O)O`` becomes ``*C(=O)CCCCC(=O)*``.
    """
    mol = Chem.RWMol(Chem.MolFromSmiles(smiles))
    if mol is None:
        raise ValueError(f"Failed to parse acid SMILES: {smiles}")

    matches = mol.GetSubstructMatches(_ACID_PATTERN)
    if len(matches) < 2:
        raise ValueError(
            f"Acid SMILES must contain at least 2 carboxylic acid groups (-C(=O)OH), found {len(matches)}: {smiles}"
        )

    # Collect the -OH oxygen indices (index 2 in the SMARTS match)
    oh_indices = sorted([match[2] for match in matches[:2]], reverse=True)

    for oh_idx in oh_indices:
        # Replace -OH oxygen with dummy atom (*)
        mol.ReplaceAtom(oh_idx, Chem.Atom(0))  # atomic number 0 = *
        # Remove the H on the OH (RDKit manages implicit H automatically)

    try:
        Chem.SanitizeMol(mol)
    except Exception:
        pass

    result = Chem.MolToSmiles(mol)
    LOGGER.info("Converted acid SMILES: %s -> %s", smiles, result)
    return result


def _epoxide_to_polymerizable(smiles: str) -> str:
    """
    Convert a diepoxide SMILES to its ring-opened polymerizable form.

    Opens each epoxide ring (3-membered C-O-C) and replaces one carbon
    with a ``*`` connection point, yielding a SMILES like
    ``*C(O)COc1ccc(...)cc1OCC(O)*``.
    """
    mol = Chem.RWMol(Chem.MolFromSmiles(smiles))
    if mol is None:
        raise ValueError(f"Failed to parse epoxide SMILES: {smiles}")

    matches = mol.GetSubstructMatches(_EPOXIDE_PATTERN)
    if len(matches) < 2:
        raise ValueError(f"Epoxide SMILES must contain at least 2 epoxide rings, found {len(matches)}: {smiles}")

    # Process each epoxide ring: break ring, add OH, replace one C with *
    # Work on matches in reverse index order to preserve indices
    atoms_to_replace = []
    for match in matches[:2]:
        o_idx, c1_idx, c2_idx = match
        # Pick the less-substituted carbon for the * connection point
        c1_degree = mol.GetAtomWithIdx(c1_idx).GetDegree()
        c2_degree = mol.GetAtomWithIdx(c2_idx).GetDegree()
        star_c = c1_idx if c1_degree <= c2_degree else c2_idx
        atoms_to_replace.append(star_c)

        # Break the C-O bond to open the ring (remove bond between O and the * carbon)
        mol.RemoveBond(o_idx, star_c)

    # Replace the selected carbons with dummy atoms (*)
    for idx in atoms_to_replace:
        mol.ReplaceAtom(idx, Chem.Atom(0))

    try:
        Chem.SanitizeMol(mol)
    except Exception:
        pass

    result = Chem.MolToSmiles(mol)
    LOGGER.info("Converted epoxide SMILES: %s -> %s", smiles, result)
    return result


def _validate_polymerizable_smiles(smiles: str, monomer_name: str) -> None:
    """
    Validate that a polymerizable monomer SMILES has exactly two connection points.
    """
    connection_points = smiles.count("*")
    if connection_points != 2:
        raise ValueError(
            f"{monomer_name} SMILES must contain exactly 2 connection points ('*'), "
            f"found {connection_points}: {smiles}"
        )


def _ensure_polymerizable(acid_smiles: str, epoxide_smiles: str) -> tuple[str, str]:
    """
    Ensure both SMILES are in polymerizable form (with ``*`` connection points).

    If the SMILES already contain ``*``, they must contain exactly two
    connection points and are returned unchanged. Otherwise, carboxylic acid
    -OH groups and epoxide rings are converted and the result is validated.
    """
    if "*" in acid_smiles:
        _validate_polymerizable_smiles(acid_smiles, "Acid")
    else:
        acid_smiles = _acid_to_polymerizable(acid_smiles)
        _validate_polymerizable_smiles(acid_smiles, "Acid")

    if "*" in epoxide_smiles:
        _validate_polymerizable_smiles(epoxide_smiles, "Epoxide")
    else:
        epoxide_smiles = _epoxide_to_polymerizable(epoxide_smiles)
        _validate_polymerizable_smiles(epoxide_smiles, "Epoxide")
    return acid_smiles, epoxide_smiles


def build_vitrimer_box(
    acid_smiles: str,
    epoxide_smiles: str,
    density: float = 0.5,
    ntotal: int = 4000,
    seed: int = 42,
) -> dict:
    """
    Build an initial vitrimer simulation box using EMC with PCFF.

    Constructs an alternating acid–epoxide copolymer and places ~4 chains
    in a cubic periodic box at the specified density.

    Accepts either format for monomer SMILES:
      - **Polymerizable** (with ``*`` connection points):
        ``*C(=O)CCCCC(=O)*``
      - **Standard** (complete molecules):
        ``O=C(O)CCCCC(=O)O``

    Standard SMILES are automatically converted to polymerizable form
    by replacing carboxylic acid -OH groups with ``*`` and opening
    epoxide rings.

    Args:
        acid_smiles: SMILES of the carboxylic acid monomer.
        epoxide_smiles: SMILES of the epoxide monomer.
        density: Initial box density in g/cm³ (protocol default: 0.5).
        ntotal: Target total atom count (~4000 yields ~4 chains of ~1000 atoms).
        seed: Random seed for EMC placement.

    Returns:
        Dict with ``work_dir``, ``num_atoms``, ``data_file``, ``params_file``,
        ``success``, and ``error``.
    """
    result = {
        "success": False,
        "work_dir": "",
        "num_atoms": 0,
        "data_file": "",
        "params_file": "",
        "error": None,
    }

    try:
        # Auto-convert standard SMILES to polymerizable form if needed
        acid_smiles, epoxide_smiles = _ensure_polymerizable(acid_smiles, epoxide_smiles)

        work_dir = tempfile.mkdtemp(prefix="vitrimer_tg_")
        box = _build_box(
            acid_smiles=acid_smiles,
            epoxide_smiles=epoxide_smiles,
            density=density,
            ntotal=ntotal,
            temperature=300.0,
            seed=seed,
            work_dir=work_dir,
        )

        # Write files to work_dir for downstream tools
        data_path = os.path.join(work_dir, "polymer.data")
        params_path = os.path.join(work_dir, "polymer.params")
        if not os.path.exists(data_path):
            with open(data_path, "w") as f:
                f.write(box.lammps_data)
        if not os.path.exists(params_path):
            with open(params_path, "w") as f:
                f.write(box.lammps_params)

        result["success"] = True
        result["work_dir"] = work_dir
        result["num_atoms"] = box.num_atoms
        result["data_file"] = data_path
        result["params_file"] = params_path

    except Exception as e:
        result["error"] = str(e)

    return result
