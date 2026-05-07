---
name: find-similar-molecules
description: Search a candidate list for molecules similar to a query using fingerprint-based Tanimoto similarity (Morgan, RDKit, or MACCS) via the find_similar_molecules tool.
---

# Find Similar Molecules

Use `find_similar_molecules` when the user needs to:
- Screen a library for structurally similar compounds
- Rank molecules by Tanimoto similarity
- Perform virtual screening or SAR exploration

## Tool Signature

```python
result = find_similar_molecules(
    query_smiles="c1ccccc1",
    candidate_smiles_list=["c1ccc(O)cc1", "CCCCCC", "c1ccc(N)cc1"],
)
```

### Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query_smiles` | str | Yes | — | SMILES of the query molecule |
| `candidate_smiles_list` | list | Yes | — | List of SMILES strings to compare |
| `threshold` | float | No | 0.7 | Minimum Tanimoto similarity (0.0–1.0) |
| `fingerprint_type` | str | No | `"morgan"` | Algorithm: `"morgan"`, `"rdkit"`, or `"maccs"` |
| `radius` | int | No | 2 | Morgan fingerprint radius |
| `n_bits` | int | No | 2048 | Bit-vector length (Morgan/RDKit) |

### Returns

| Field | Type | Description |
|-------|------|-------------|
| `query_smiles` | str | Canonical query SMILES |
| `fingerprint_type` | str | Algorithm used |
| `threshold` | float | Threshold applied |
| `num_candidates` | int | Total candidates evaluated |
| `num_matches` | int | Matches above threshold |
| `matches` | list[dict] | Ranked results (see below) |

Each match dict:

| Field | Type | Description |
|-------|------|-------------|
| `index` | int | Position in input list |
| `input_smiles` | str | Original SMILES from input |
| `canonical_smiles` | str | Canonical SMILES |
| `similarity` | float | Tanimoto similarity score |

## Examples

### Basic similarity search

```python
result = find_similar_molecules(
    query_smiles="c1ccccc1",
    candidate_smiles_list=["c1ccc(O)cc1", "CCCCCC", "c1ccc(N)cc1", "C1CCCCC1"],
    threshold=0.3,
)
for m in result["matches"]:
    print(f"  {m['canonical_smiles']}: {m['similarity']:.3f}")
```

### Using MACCS fingerprints

```python
result = find_similar_molecules(
    query_smiles="CC(=O)Oc1ccccc1C(=O)O",  # aspirin
    candidate_smiles_list=library_smiles,
    fingerprint_type="maccs",
    threshold=0.6,
)
print(f"Found {result['num_matches']} similar compounds")
```

### Adjusting Morgan radius for specificity

A larger radius captures more structural context, producing higher
specificity (fewer, more closely related matches):

```python
result = find_similar_molecules(
    query_smiles="c1ccccc1",
    candidate_smiles_list=candidates,
    radius=3,
    threshold=0.5,
)
```

## Notes

- Invalid candidate SMILES are silently skipped (not counted as matches).
- Results are sorted by descending similarity.
- For large candidate lists (>10 000), consider batching to manage memory.
