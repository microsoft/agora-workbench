---
name: drug-screening
description: Compute molecular descriptors and screen compounds for drug-likeness using Lipinski and Veber rules — a two-step workflow using compute_descriptors and filter_drug_candidates.
states:
  - chemistry.molecule_parsed
  - chemistry.descriptors_computed
  - chemistry.candidates_filtered
---

# Drug-Likeness Screening

Use this skill when the user wants to evaluate whether molecules are
drug-like, compute physicochemical descriptors, or filter a compound
library for lead candidates.

## State Graph

```
parse_molecule(smiles)
    → chemistry.molecule_parsed

compute_descriptors(smiles)
    requires: chemistry.molecule_parsed
    → chemistry.descriptors_computed

filter_drug_candidates(smiles_list)
    requires: chemistry.descriptors_computed
    → chemistry.candidates_filtered
```

## Tools

### compute_descriptors

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `smiles` | str | Yes | — | SMILES string |
| `descriptors` | list | No | all | Subset of descriptor names |

Available descriptors: `molecular_weight`, `logp`, `hbd`, `hba`, `tpsa`,
`rotatable_bonds`, `ring_count`, `aromatic_rings`, `fraction_csp3`,
`heavy_atom_count`.

**Returns:** `smiles` (canonical), `descriptors` (name→value dict),
`lipinski_pass` (bool)

### filter_drug_candidates

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `smiles_list` | list | Yes | — | SMILES strings to screen |
| `rules` | str | No | `"lipinski"` | `"lipinski"`, `"veber"`, or `"both"` |

**Returns:** `rules`, `num_input`, `num_passed`, `num_failed`,
`passed` (list with properties), `failed` (list with failure reasons)

**Rule thresholds:**

| Rule Set | Property | Threshold |
|----------|----------|-----------|
| Lipinski | MW | < 500 |
| Lipinski | LogP | < 5 |
| Lipinski | HBD | ≤ 5 |
| Lipinski | HBA | ≤ 10 |
| Veber | Rotatable bonds | ≤ 10 |
| Veber | TPSA | ≤ 140 Å² |

## Workflow Example

```python
# Step 1: Characterize a single molecule
result = compute_descriptors(smiles="c1ccc(O)cc1")
print(f"LogP: {result['descriptors']['logp']}")
print(f"Drug-like: {result['lipinski_pass']}")

# Step 2: Screen a library
library = ["CCO", "c1ccccc1", "CC(=O)Oc1ccccc1C(=O)O",
           "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O"]  # ibuprofen
screening = filter_drug_candidates(smiles_list=library, rules="both")
print(f"{screening['num_passed']}/{screening['num_input']} passed")

for mol in screening["failed"]:
    print(f"  FAIL: {mol['canonical_smiles']} — {', '.join(mol['reasons'])}")
```

## Selective Descriptor Computation

When you only need specific descriptors, pass the names to avoid
unnecessary computation:

```python
result = compute_descriptors(
    smiles="CC(=O)Oc1ccccc1C(=O)O",
    descriptors=["molecular_weight", "logp", "tpsa"]
)
```
