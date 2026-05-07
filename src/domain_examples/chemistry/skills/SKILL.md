---
name: chemistry-rdkit
description: Molecular analysis and cheminformatics using RDKit — SMILES handling, descriptor calculation, fingerprints, substructure search, and reaction enumeration via the execute_chemistry_code tool.
---

# Chemistry / RDKit

Use this skill when the user asks about molecules, chemical structures, SMILES,
molecular properties, similarity, substructure matching, or any cheminformatics
task. Code runs in the `execute_chemistry_code` tool with RDKit auto-imported.

## Auto-Imported Modules

These are available without explicit imports:

```python
from rdkit import Chem
from rdkit.Chem import Draw, Descriptors, AllChem, rdMolDescriptors, PandasTools
import numpy as np
import pandas as pd
```

## Critical: SMILES Validation

**Always** check that `Chem.MolFromSmiles()` did not return `None` before using
the molecule. Invalid SMILES silently return `None`, causing `AttributeError`
on the next operation.

```python
# CORRECT
mol = Chem.MolFromSmiles(user_smiles)
if mol is None:
    print(f"Invalid SMILES: {user_smiles}")
else:
    mw = Descriptors.MolWt(mol)

# WRONG — will crash on invalid input
mol = Chem.MolFromSmiles(user_smiles)
mw = Descriptors.MolWt(mol)  # AttributeError if mol is None
```

## SMILES vs SMARTS

- Use `Chem.MolFromSmiles()` for **specific molecules** (e.g., `"CCO"` for ethanol)
- Use `Chem.MolFromSmarts()` for **substructure patterns** (e.g., `"[OX2H]"` for hydroxyl)
- Mixing them up silently gives wrong or empty results

## Fingerprints and Similarity

- Use `GetMorganFingerprintAsBitVect` (not `GetMorganFingerprint`) when computing
  Tanimoto similarity — the count-based version is incompatible with
  `DataStructs.TanimotoSimilarity()`
- Standard defaults: **radius=2, nBits=2048**. Use radius=3 for higher specificity.

```python
from rdkit import DataStructs

fp1 = AllChem.GetMorganFingerprintAsBitVect(mol1, 2, nBits=2048)
fp2 = AllChem.GetMorganFingerprintAsBitVect(mol2, 2, nBits=2048)
similarity = DataStructs.TanimotoSimilarity(fp1, fp2)
```

## Molecular Descriptors

For drug-likeness screening, use Lipinski's Rule of Five:

| Property | Threshold | RDKit Function |
|----------|-----------|----------------|
| Molecular weight | < 500 | `Descriptors.MolWt(mol)` |
| LogP | < 5 | `Descriptors.MolLogP(mol)` |
| H-bond donors | ≤ 5 | `Descriptors.NumHDonors(mol)` |
| H-bond acceptors | ≤ 10 | `Descriptors.NumHAcceptors(mol)` |
| TPSA | < 140 Å² | `Descriptors.TPSA(mol)` |

## Stereochemistry

Preserve stereochemistry when generating canonical SMILES:

```python
canonical = Chem.MolToSmiles(mol, isomericSmiles=True)
```

## Sanitization

- `Chem.SanitizeMol(mol)` is called automatically by `MolFromSmiles`. Call it
  explicitly only after manual atom/bond edits (e.g., `RWMol` operations).
- For substructure query molecules, do **not** sanitize — use
  `Chem.MolFromSmarts()` which skips sanitization by design.

## Batch Processing Pattern

When processing multiple molecules, collect results into a DataFrame:

```python
smiles_list = ["CCO", "CC(=O)O", "c1ccccc1"]
data = []
for smi in smiles_list:
    mol = Chem.MolFromSmiles(smi)
    if mol is None:
        continue
    data.append({
        "SMILES": smi,
        "MW": Descriptors.MolWt(mol),
        "LogP": Descriptors.MolLogP(mol),
        "HBD": Descriptors.NumHDonors(mol),
        "HBA": Descriptors.NumHAcceptors(mol),
    })
df = pd.DataFrame(data)
print(df.to_string(index=False))
```
