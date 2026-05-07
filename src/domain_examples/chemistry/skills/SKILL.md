---
name: chemistry-rdkit
description: Molecular analysis and cheminformatics using RDKit — SMILES handling, descriptor calculation, fingerprints, substructure search, similarity, clustering, and drug-likeness screening via domain tools and the execute_chemistry_code tool.
states:
  - chemistry.molecule_parsed
  - chemistry.groups_identified
  - chemistry.descriptors_computed
  - chemistry.candidates_filtered
  - chemistry.fingerprints_computed
  - chemistry.similarity_computed
  - chemistry.molecules_clustered
---

# Chemistry / RDKit

Use this skill when the user asks about molecules, chemical structures, SMILES,
molecular properties, similarity, substructure matching, or any cheminformatics
task. Code runs in the `execute_chemistry_code` tool with RDKit auto-imported.

## State Graph Overview

The domain tools form a directed graph of workflows. `parse_molecule` is the
entry point; downstream tools have prerequisite states that guide workflow
planning.

```
parse_molecule ─────► chemistry.molecule_parsed
                              │
              ┌───────────────┼───────────────────┐
              ▼               ▼                   ▼
   enumerate_functional  compute_descriptors  compute_fingerprints
      _groups               │                    │
              │             ▼                    ├──────────────┐
              ▼     chemistry.descriptors_       ▼              ▼
   chemistry.groups_    computed         chemistry.fingerprints_ │
    identified          │                  computed              │
                        ▼                    │                   │
               filter_drug_candidates        ▼                  ▼
                        │           find_similar_molecules  cluster_molecules
                        ▼                    │                   │
               chemistry.candidates_         ▼                   ▼
                 filtered           chemistry.similarity_  chemistry.molecules_
                                     computed               clustered
```

## Workflow Skills

| Skill | Tools | Description |
|-------|-------|-------------|
| [molecular-analysis](molecular-analysis.md) | `parse_molecule` → `enumerate_functional_groups` | Structural characterization |
| [drug-screening](drug-screening.md) | `compute_descriptors` → `filter_drug_candidates` | Drug-likeness evaluation |
| [similarity-and-clustering](similarity-and-clustering.md) | `compute_fingerprints` → `find_similar_molecules` / `cluster_molecules` | Library search and grouping |

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
the molecule. Invalid SMILES silently return `None`, causing an exception on
the next operation.

```python
# CORRECT
mol = Chem.MolFromSmiles(user_smiles)
if mol is None:
    print(f"Invalid SMILES: {user_smiles}")
else:
    mw = Descriptors.MolWt(mol)

# WRONG — will crash on invalid input
mol = Chem.MolFromSmiles(user_smiles)
mw = Descriptors.MolWt(mol)  # Exception if mol is None
```

## SMILES vs SMARTS

- Use `Chem.MolFromSmiles()` for **specific molecules** (e.g., `"CCO"` for ethanol)
- Use `Chem.MolFromSmarts()` for **substructure patterns** (e.g., `"[OX2H]"` for hydroxyl)
- Mixing them up silently gives wrong or empty results

## Fingerprints and Similarity

- Prefer `GetMorganFingerprintAsBitVect` over `GetMorganFingerprint` when
  computing Tanimoto similarity for consistent behavior and easier downstream
  handling
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
