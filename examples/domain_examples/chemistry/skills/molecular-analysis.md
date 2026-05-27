---
name: molecular-analysis
description: Parse molecules and identify functional groups — a two-step workflow for structural characterization using parse_molecule and enumerate_functional_groups.
states:
  - chemistry.molecule_parsed
  - chemistry.groups_identified
---

# Molecular Analysis

Use this skill when the user wants to understand the structure of a molecule:
parsing SMILES, validating input, obtaining basic properties, or identifying
functional groups.

## State Graph

```
parse_molecule(smiles)
    → chemistry.molecule_parsed

enumerate_functional_groups(smiles)
    requires: chemistry.molecule_parsed
    → chemistry.groups_identified
```

## Tools

### parse_molecule

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `smiles` | str | Yes | SMILES string to parse |

**Returns:** `canonical_smiles`, `molecular_formula`, `molecular_weight`,
`num_atoms`, `num_bonds`

### enumerate_functional_groups

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `smiles` | str | Yes | SMILES string to analyze |

**Returns:** `smiles` (canonical), `groups_found` (list of group dicts with
`name`, `smarts`, `count`, `atom_indices`), `num_groups_found`

Recognized groups: hydroxyl, carboxyl, primary/secondary/tertiary amine,
amide, aldehyde, ketone, ester, ether, nitro, sulfhydryl, phenol, halide,
nitrile, phosphate.

## Workflow Example

```python
# Step 1: Parse and validate the molecule
info = parse_molecule(smiles="CC(=O)Oc1ccccc1C(=O)O")  # aspirin
print(f"{info['molecular_formula']}, MW={info['molecular_weight']:.2f}")

# Step 2: Identify functional groups
groups = enumerate_functional_groups(smiles="CC(=O)Oc1ccccc1C(=O)O")
for g in groups["groups_found"]:
    print(f"  {g['name']}: {g['count']} occurrence(s)")
```

## Error Handling

Both tools raise `ValueError` on invalid SMILES. Always handle this for
user-supplied input:

```python
try:
    info = parse_molecule(smiles=user_input)
except ValueError as e:
    print(f"Could not parse: {e}")
```
