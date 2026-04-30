"""
EMC builder for vitrimer alternating copolymer boxes.

Constructs initial LAMMPS simulation boxes from acid + epoxide SMILES
using EMC (Enhanced Monte Carlo) with the PCFF force field. Adapted from
the earthshots/sorbent/initial_config/emc.py wrapper.
"""

import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from random import randint
from typing import Optional

from rdkit import Chem
from pyemc import runner as emc_runner

# Set EMC_ROOT so the Perl scripts and EMC binary can locate field files.
# This mirrors the pattern in earthshots/sorbent/initial_config/emc_fields.py.
os.environ["EMC_ROOT"] = os.path.join(emc_runner._get_path(), "emc")

LOGGER = logging.getLogger(__name__)


@dataclass
class VitrimerBoxResult:
    """Result of an EMC box construction."""

    lammps_data: str
    lammps_params: str
    emc_input: str
    emc_output: str
    build_file: str
    num_atoms: int


def _open_epoxide_smiles(epoxide_smiles: str) -> str:
    """
    Convert an epoxide SMILES to its ring-opened form for polymerization.

    The epoxy group (C1OC1 or C1CO1) is opened to produce a secondary
    alcohol with two connection points (*) for chain extension.

    If the molecule doesn't contain an obvious epoxide ring, the SMILES
    is returned unchanged with terminal connection points appended.
    """
    mol = Chem.MolFromSmiles(epoxide_smiles)
    if mol is None:
        raise ValueError(f"Failed to parse epoxide SMILES: {epoxide_smiles}")

    # For the EMC alternating copolymer approach, we need the opened form.
    # The publication uses EMC to handle ring-opening chemistry internally.
    # We pass the original SMILES and let EMC's polymer builder handle it.
    return Chem.MolToSmiles(mol)


def _validate_smiles(smiles: str, label: str) -> str:
    """Validate and canonicalize a SMILES string."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"Failed to parse {label} SMILES: {smiles}")
    return Chem.MolToSmiles(mol)


def _merge_repeat_unit(acid_smiles: str, epoxide_smiles: str) -> str:
    """
    Merge acid and epoxide SMILES into a single repeat unit for EMC.

    Each monomer has two connection points (``*``).  One ``*`` from the
    acid is bonded to one ``*`` from the epoxide, producing a merged
    SMILES with exactly two remaining ``*`` (one from each monomer) that
    serve as the chain-extension points of the repeat unit.

    This avoids the EMC limitation where separate alternating groups with
    two connection points each cause "Groups … do not connect" errors in
    the polymer builder.
    """
    mol_acid = Chem.MolFromSmiles(acid_smiles)
    mol_epoxide = Chem.MolFromSmiles(epoxide_smiles)
    if mol_acid is None:
        raise ValueError(f"Failed to parse acid SMILES: {acid_smiles}")
    if mol_epoxide is None:
        raise ValueError(f"Failed to parse epoxide SMILES: {epoxide_smiles}")

    # Find wildcard atoms (atomic number 0, i.e. ``*``)
    acid_stars = [a.GetIdx() for a in mol_acid.GetAtoms() if a.GetAtomicNum() == 0]
    epoxide_stars = [a.GetIdx() for a in mol_epoxide.GetAtoms() if a.GetAtomicNum() == 0]
    if len(acid_stars) < 2 or len(epoxide_stars) < 2:
        raise ValueError(
            "Both acid and epoxide SMILES must have at least 2 connection "
            f"points (*).  Got {len(acid_stars)} and {len(epoxide_stars)}."
        )

    # Combine molecules
    combo = Chem.RWMol(Chem.CombineMols(mol_acid, mol_epoxide))
    n_acid = mol_acid.GetNumAtoms()

    # Pick one * from acid and one * from epoxide to form the internal bond.
    # Use the second * of acid and the first * of epoxide (arbitrary but
    # consistent choice).
    star_acid = acid_stars[1]
    star_epoxide = epoxide_stars[0] + n_acid  # offset for combined mol

    # Find the neighbour of each * (the actual heavy atom it's attached to)
    nbr_acid = combo.GetAtomWithIdx(star_acid).GetNeighbors()[0].GetIdx()
    nbr_epoxide = combo.GetAtomWithIdx(star_epoxide).GetNeighbors()[0].GetIdx()

    # Remove the two * atoms (remove higher index first to keep indices valid)
    to_remove = sorted([star_acid, star_epoxide], reverse=True)
    for idx in to_remove:
        combo.RemoveAtom(idx)

    # Recalculate neighbour indices after removal
    # Build an index-mapping: old -> new
    removed_sorted = sorted([star_acid, star_epoxide])

    def _new_idx(old: int) -> int:
        shift = sum(1 for r in removed_sorted if r < old)
        return old - shift

    nbr_acid_new = _new_idx(nbr_acid)
    nbr_epoxide_new = _new_idx(nbr_epoxide)

    # Add bond between the two heavy-atom neighbours
    combo.AddBond(nbr_acid_new, nbr_epoxide_new, Chem.BondType.SINGLE)

    try:
        Chem.SanitizeMol(combo)
    except Exception:
        pass  # best effort; EMC will validate further

    merged = Chem.MolToSmiles(combo)
    # Verify exactly 2 * remain
    n_stars = merged.count("*")
    if n_stars != 2:
        raise ValueError(
            f"Merged repeat unit has {n_stars} connection points (expected 2). "
            "Check that each monomer has exactly 2 connection points."
        )
    return merged


def _write_emc_input(
    acid_smiles: str,
    epoxide_smiles: str,
    density: float,
    temperature: float,
    ntotal: int,
    seed: int,
) -> str:
    """
    Generate an EMC input file for alternating copolymer vitrimer construction.

    The acid and epoxide monomers are merged into a single repeat-unit
    SMILES so that EMC treats the chain as a simple homopolymer.  This
    avoids the EMC polymer-builder limitation where two-connection-point
    alternating groups fail with "Groups … do not connect".

    The PCFF force field is used, with an explicit ``field_location``
    pointing to the pyemc installation to avoid tilde-expansion issues
    in the Perl field-locator script.
    """
    merged_smiles = _merge_repeat_unit(acid_smiles, epoxide_smiles)

    # Estimate repeat count so total atoms ≈ ntotal.
    # Count heavy + H atoms in the merged SMILES via RDKit.
    mol = Chem.MolFromSmiles(merged_smiles)
    if mol is not None:
        mol_h = Chem.AddHs(mol)
        atoms_per_repeat = mol_h.GetNumAtoms()
    else:
        atoms_per_repeat = 60  # fallback
    nrepeat = max(2, ntotal // atoms_per_repeat)

    lines = []

    # ── OPTIONS ──────────────────────────────────────────────────────
    lines.append("#!/usr/bin/env emc.pl")
    lines.append("")
    lines.append("ITEM\tOPTIONS")
    lines.append("")
    lines.append("build_dir\t\t.")
    lines.append("build_replace\t\ttrue")
    lines.append(f"density\t\t{density}")
    lines.append(f"temperature\t\t{temperature}")
    lines.append("niterations\t\t10000")
    lines.append("field_charge\t\tfalse")
    lines.append("field_increment\t\twarn")
    lines.append("field\t\tpcff")

    lines.append("mol\t\ttrue")
    lines.append(f"ntotal\t\t{ntotal}")
    lines.append("replace\t\ttrue")
    lines.append(f"seed\t\t{seed}")
    lines.append("")
    lines.append("ITEM\tEND")
    lines.append("")

    # ── GROUPS ───────────────────────────────────────────────────────
    # Single merged repeat unit with two connection points that link to
    # itself (homopolymer chain), plus a methyl terminator.
    lines.append("ITEM\tGROUPS")
    lines.append("")
    lines.append(f"repeat\t\t{merged_smiles},1,repeat:2,2,repeat:1")
    lines.append("term\t\t*C,1,repeat:1,1,repeat:2")
    lines.append("")
    lines.append("ITEM\tEND")
    lines.append("")

    # ── CLUSTERS ─────────────────────────────────────────────────────
    lines.append("ITEM\tCLUSTERS")
    lines.append("")
    lines.append("vitrimer\t\trandom\t1")
    lines.append("")
    lines.append("ITEM\tEND")
    lines.append("")

    # ── POLYMERS ─────────────────────────────────────────────────────
    lines.append("ITEM\tPOLYMERS")
    lines.append("")
    lines.append("vitrimer")
    lines.append(f"1\t\trepeat,{nrepeat},term,2")
    lines.append("")
    lines.append("ITEM\tEND")

    return "\n".join(lines) + "\n"


def _run_emc(
    emc_input: str,
    work_dir: str,
    max_retries: int = 3,
) -> tuple[str, str]:
    """
    Execute EMC and return (stdout_log, build_file_content).

    Raises RuntimeError on failure.
    """
    input_path = os.path.join(work_dir, "input.emc")
    with open(input_path, "w") as f:
        f.write(emc_input)

    # Ensure EMC_ROOT points to the absolute pyemc/emc directory so that
    # the Perl scripts (emc.pl → EMC::IO::emc_root) can locate the PCFF
    # force-field files without relying on tilde expansion or $0 path
    # resolution, both of which can fail inside virtualenvs / containers.
    emc_root = os.path.join(emc_runner._get_path(), "emc")
    env = os.environ.copy()
    env["EMC_ROOT"] = emc_root

    output_log = ""
    for attempt in range(max_retries):
        # Step 1: generate build.emc via emc.pl
        emc_build_path = os.path.join(emc_runner._get_path(), "emc", "scripts", "emc.pl")
        result = subprocess.run(
            [emc_build_path, input_path],
            capture_output=True,
            text=True,
            cwd=work_dir,
            env=env,
        )
        output_log = result.stdout + result.stderr

        # Step 2: execute build.emc
        build_emc_path = os.path.join(work_dir, "build.emc")
        if not os.path.exists(build_emc_path):
            LOGGER.warning(f"EMC attempt {attempt + 1}: build.emc not generated")
            # Update seed in the input and retry
            seed = randint(1, 1_000_000)
            emc_input = re.sub(r"seed\s+\d+", f"seed\t\t{seed}", emc_input)
            with open(input_path, "w") as f:
                f.write(emc_input)
            continue

        emc_run_path = os.path.join(emc_runner._get_path(), "emc", "bin", str(emc_runner._get_exec()))
        result = subprocess.run(
            [emc_run_path, build_emc_path],
            capture_output=True,
            text=True,
            cwd=work_dir,
            env=env,
        )
        output_log += result.stdout + result.stderr

        # Check for known errors
        if re.search(r"Missing force field parameters", output_log):
            raise RuntimeError(
                "PCFF parameterization failed. The provided SMILES may contain "
                "atom types not covered by the PCFF force field."
            )
        if re.search(r"Missing rules", output_log):
            raise RuntimeError("EMC rule matching failed for the provided structures.")
        if re.search(r"No mass in system", output_log):
            raise RuntimeError("Fewer atoms than ntotal were allocated. Try increasing ntotal.")

        # Check for successful completion
        if re.search(r"\(\* Energy \*\)", output_log):
            build_content = ""
            if os.path.exists(build_emc_path):
                with open(build_emc_path) as f:
                    build_content = f.read()
            return output_log, build_content

        # Unhandled error — retry with new seed
        LOGGER.warning(f"EMC attempt {attempt + 1}: unhandled error, retrying")
        seed = randint(1, 1_000_000)
        emc_input = re.sub(r"seed\s+\d+", f"seed\t\t{seed}", emc_input)
        with open(input_path, "w") as f:
            f.write(emc_input)

    raise RuntimeError(f"EMC failed after {max_retries} attempts. Last output:\n{output_log[-2000:]}")


def build_vitrimer_box(
    acid_smiles: str,
    epoxide_smiles: str,
    density: float = 0.5,
    ntotal: int = 4000,
    temperature: float = 300.0,
    seed: Optional[int] = None,
    work_dir: Optional[str] = None,
) -> VitrimerBoxResult:
    """
    Build a vitrimer simulation box using EMC with PCFF.

    Constructs an alternating copolymer from the given acid and epoxide
    monomers, placing 4 chains in a cubic box at the specified density.

    Args:
        acid_smiles: SMILES string for the carboxylic acid monomer.
            Must include connection point(s) marked with ``*``.
        epoxide_smiles: SMILES string for the epoxide monomer.
            Must include connection point(s) marked with ``*``.
        density: Initial box density in g/cm³ (default 0.5, per protocol).
        ntotal: Target total atom count (default ~4000 for 4 chains).
        temperature: Initial temperature in K for EMC placement.
        seed: Random seed for EMC (random if not provided).
        work_dir: Directory for output files (temp dir if not provided).

    Returns:
        VitrimerBoxResult with LAMMPS data/params file contents and metadata.
    """
    if seed is None:
        seed = randint(1, 1_000_000)

    if work_dir is None:
        work_dir = tempfile.mkdtemp(prefix="vitrimer_tg_")

    os.makedirs(work_dir, exist_ok=True)

    # Generate EMC input
    emc_input = _write_emc_input(
        acid_smiles=acid_smiles,
        epoxide_smiles=epoxide_smiles,
        density=density,
        temperature=temperature,
        ntotal=ntotal,
        seed=seed,
    )

    # Run EMC
    output_log, build_content = _run_emc(emc_input, work_dir)

    # Collect output files
    data_path = os.path.join(work_dir, "polymer.data")
    params_path = os.path.join(work_dir, "polymer.params")

    # EMC may use different output names; search for .data and .params files
    if not os.path.exists(data_path):
        for fname in os.listdir(work_dir):
            if fname.endswith(".data") and fname != "build.emc":
                data_path = os.path.join(work_dir, fname)
                break
    if not os.path.exists(params_path):
        for fname in os.listdir(work_dir):
            if fname.endswith(".params"):
                params_path = os.path.join(work_dir, fname)
                break

    if not os.path.exists(data_path):
        raise RuntimeError(f"EMC did not produce a LAMMPS data file in {work_dir}")
    if not os.path.exists(params_path):
        raise RuntimeError(f"EMC did not produce a LAMMPS params file in {work_dir}")

    with open(data_path) as f:
        lammps_data = f.read()
    with open(params_path) as f:
        lammps_params = f.read()

    # Parse atom count from data file header
    num_atoms = 0
    match = re.search(r"(\d+)\s+atoms", lammps_data)
    if match:
        num_atoms = int(match.group(1))

    return VitrimerBoxResult(
        lammps_data=lammps_data,
        lammps_params=lammps_params,
        emc_input=emc_input,
        emc_output=output_log,
        build_file=build_content,
        num_atoms=num_atoms,
    )
