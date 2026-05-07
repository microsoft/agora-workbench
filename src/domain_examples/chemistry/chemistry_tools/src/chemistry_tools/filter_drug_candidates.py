"""Screen molecules against drug-likeness rules (Lipinski, Veber)."""

from rdkit import Chem
from rdkit.Chem import Descriptors

SUPPORTED_RULES = ("lipinski", "veber", "both")


def filter_drug_candidates(
    smiles_list: list,
    rules: str = "lipinski",
) -> dict:
    """Screen molecules against drug-likeness filters.

    Args:
        smiles_list: List of SMILES strings to screen.
        rules: Rule set to apply — ``"lipinski"`` (Ro5), ``"veber"``
            (rotatable bonds ≤ 10, TPSA ≤ 140), or ``"both"``.

    Returns:
        Dictionary with ``rules``, ``num_input``, ``num_passed``,
        ``num_failed``, ``passed`` (list of dicts), and ``failed``
        (list of dicts with failure reasons).

    Raises:
        ValueError: If smiles_list is empty or rules is unsupported.
    """
    if not smiles_list or not isinstance(smiles_list, list):
        raise ValueError("smiles_list must be a non-empty list of SMILES strings.")

    rules_lower = rules.lower()
    if rules_lower not in SUPPORTED_RULES:
        raise ValueError(f"Unsupported rules: {rules!r}. Choose from: {', '.join(SUPPORTED_RULES)}")

    passed = []
    failed = []

    for smi in smiles_list:
        if not isinstance(smi, str):
            continue
        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            failed.append(
                {
                    "input_smiles": smi,
                    "canonical_smiles": None,
                    "reasons": ["invalid SMILES"],
                }
            )
            continue

        canonical = Chem.MolToSmiles(mol, isomericSmiles=True)
        mw = Descriptors.MolWt(mol)
        logp = Descriptors.MolLogP(mol)
        hbd = Descriptors.NumHDonors(mol)
        hba = Descriptors.NumHAcceptors(mol)
        tpsa = Descriptors.TPSA(mol)
        rot = Descriptors.NumRotatableBonds(mol)

        reasons = []

        if rules_lower in ("lipinski", "both"):
            if mw >= 500:
                reasons.append(f"MW={mw:.1f} ≥ 500")
            if logp >= 5:
                reasons.append(f"LogP={logp:.2f} ≥ 5")
            if hbd > 5:
                reasons.append(f"HBD={hbd} > 5")
            if hba > 10:
                reasons.append(f"HBA={hba} > 10")

        if rules_lower in ("veber", "both"):
            if rot > 10:
                reasons.append(f"RotBonds={rot} > 10")
            if tpsa > 140:
                reasons.append(f"TPSA={tpsa:.1f} > 140")

        entry = {
            "input_smiles": smi,
            "canonical_smiles": canonical,
            "properties": {
                "MW": round(mw, 2),
                "LogP": round(logp, 2),
                "HBD": hbd,
                "HBA": hba,
                "TPSA": round(tpsa, 2),
                "RotBonds": rot,
            },
        }

        if reasons:
            entry["reasons"] = reasons
            failed.append(entry)
        else:
            passed.append(entry)

    return {
        "rules": rules_lower,
        "num_input": len(smiles_list),
        "num_passed": len(passed),
        "num_failed": len(failed),
        "passed": passed,
        "failed": failed,
    }
