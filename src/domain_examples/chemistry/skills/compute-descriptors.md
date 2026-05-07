---
name: compute-descriptors
description: Compute physicochemical descriptors (MW, LogP, HBD, HBA, TPSA, etc.) and evaluate Lipinski's Rule of Five using the compute_descriptors tool.
---

# Compute Descriptors

Use `compute_descriptors` when the user needs to:
- Calculate physicochemical properties of a molecule
- Evaluate drug-likeness (Lipinski's Rule of Five)
- Compare descriptor profiles across molecules

## Tool Signature

```python
result = compute_descriptors(smiles="CCO")
result = compute_descriptors(smiles="CCO", descriptors=["logp", "tpsa"])
```

### Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `smiles` | str | Yes | — | SMILES string |
| `descriptors` | list | No | all | Subset of descriptor names to compute |

### Available Descriptors

| Name | Description |
|------|-------------|
| `molecular_weight` | Molecular weight (Da) |
| `logp` | Wildman-Crippen LogP |
| `hbd` | Hydrogen-bond donors |
| `hba` | Hydrogen-bond acceptors |
| `tpsa` | Topological polar surface area (Å²) |
| `rotatable_bonds` | Rotatable bond count |
| `ring_count` | Total ring count |
| `aromatic_rings` | Aromatic ring count |
| `fraction_csp3` | Fraction of sp3 carbons |
| `heavy_atom_count` | Heavy atom count |

### Returns

| Field | Type | Description |
|-------|------|-------------|
| `smiles` | str | Canonical SMILES |
| `descriptors` | dict | Mapping of descriptor name → value |
| `lipinski_pass` | bool | Passes Rule of Five |

## Examples

### All descriptors

```python
result = compute_descriptors(smiles="c1ccc(O)cc1")
print(result["lipinski_pass"])  # True
for name, value in result["descriptors"].items():
    print(f"  {name}: {value}")
```

### Selected descriptors only

```python
result = compute_descriptors(
    smiles="CC(=O)Oc1ccccc1C(=O)O",
    descriptors=["molecular_weight", "logp", "hbd", "hba"]
)
```

### Batch comparison

```python
smiles_list = ["CCO", "c1ccccc1", "CC(=O)Oc1ccccc1C(=O)O"]
rows = []
for smi in smiles_list:
    r = compute_descriptors(smiles=smi)
    row = {"SMILES": r["smiles"], "Lipinski": r["lipinski_pass"]}
    row.update(r["descriptors"])
    rows.append(row)
df = pd.DataFrame(rows)
print(df.to_string(index=False))
```
