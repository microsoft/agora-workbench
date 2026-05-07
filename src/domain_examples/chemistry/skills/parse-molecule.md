---
name: parse-molecule
description: Parse a SMILES string to obtain canonical SMILES, molecular formula, weight, and atom/bond counts using the parse_molecule tool.
---

# Parse Molecule

Use the `parse_molecule` tool when the user needs to:
- Validate a SMILES string
- Get the canonical form of a molecule
- Look up basic identity info (formula, weight, atom count)

## Tool Signature

```python
result = parse_molecule(smiles="CCO")
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `smiles`  | str  | Yes      | SMILES string (e.g. `"CCO"`, `"c1ccccc1"`) |

### Returns

| Field | Type | Description |
|-------|------|-------------|
| `canonical_smiles` | str | Canonical SMILES |
| `molecular_formula` | str | e.g. `"C2H6O"` |
| `molecular_weight` | float | Molecular weight in Daltons |
| `num_atoms` | int | Heavy (non-hydrogen) atom count |
| `num_bonds` | int | Bond count |

## Example

```python
result = parse_molecule(smiles="CC(=O)Oc1ccccc1C(=O)O")
print(result)
# {
#   "canonical_smiles": "CC(=O)Oc1ccccc1C(=O)O",
#   "molecular_formula": "C9H8O4",
#   "molecular_weight": 180.0423,
#   "num_atoms": 13,
#   "num_bonds": 14
# }
```

## Error Handling

Invalid SMILES raise `ValueError`. Always handle this when accepting
user-supplied input:

```python
try:
    result = parse_molecule(smiles=user_input)
except ValueError as e:
    print(f"Could not parse: {e}")
```
