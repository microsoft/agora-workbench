---
name: similarity-and-clustering
description: Compute fingerprints, search for similar molecules, and cluster chemical libraries — a branching workflow using compute_fingerprints, find_similar_molecules, and cluster_molecules.
states:
  - chemistry.molecule_parsed
  - chemistry.fingerprints_computed
  - chemistry.similarity_computed
  - chemistry.molecules_clustered
---

# Similarity Search & Clustering

Use this skill when the user wants to compare molecules, find structural
analogs, or group a compound library into chemical series.

## State Graph

This workflow demonstrates a **branching** state graph: `compute_fingerprints`
is a shared prerequisite for both similarity search and clustering.

```
parse_molecule(smiles)
    → chemistry.molecule_parsed

compute_fingerprints(smiles_list)
    requires: chemistry.molecule_parsed
    → chemistry.fingerprints_computed
           │
     ┌─────┴──────┐
     ▼             ▼
find_similar    cluster_
  _molecules      molecules
     │             │
     ▼             ▼
chemistry.     chemistry.
similarity_    molecules_
computed       clustered
```

## Tools

### compute_fingerprints

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `smiles_list` | list | Yes | — | SMILES strings to fingerprint |
| `fingerprint_type` | str | No | `"morgan"` | `"morgan"`, `"rdkit"`, or `"maccs"` |
| `radius` | int | No | 2 | Morgan radius |
| `n_bits` | int | No | 2048 | Bit-vector length |

**Returns:** `fingerprint_type`, `num_molecules`, `num_valid`,
`num_invalid`, `fingerprints` (per-molecule bit data)

### find_similar_molecules

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query_smiles` | str | Yes | — | Query molecule SMILES |
| `candidate_smiles_list` | list | Yes | — | Candidates to compare |
| `threshold` | float | No | 0.7 | Min Tanimoto similarity |
| `fingerprint_type` | str | No | `"morgan"` | Algorithm |
| `radius` | int | No | 2 | Morgan radius |
| `n_bits` | int | No | 2048 | Bit-vector length |

**Returns:** `query_smiles`, `fingerprint_type`, `threshold`,
`num_candidates`, `num_matches`, `matches` (ranked list with
`index`, `input_smiles`, `canonical_smiles`, `similarity`)

### cluster_molecules

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `smiles_list` | list | Yes | — | SMILES strings to cluster |
| `cutoff` | float | No | 0.5 | Tanimoto distance cutoff |
| `fingerprint_type` | str | No | `"morgan"` | Algorithm |
| `radius` | int | No | 2 | Morgan radius |
| `n_bits` | int | No | 2048 | Bit-vector length |

**Returns:** `num_molecules`, `num_clusters`, `cutoff`,
`fingerprint_type`, `clusters` (list with `cluster_id`, `size`,
`centroid`, `members`)

## Workflow Examples

### Similarity search

```python
# Step 1: Compute fingerprints for your library
library = ["c1ccc(O)cc1", "CCCCCC", "c1ccc(N)cc1", "C1CCCCC1", "c1ccc(F)cc1"]
fps = compute_fingerprints(smiles_list=library, fingerprint_type="morgan")
print(f"Fingerprinted {fps['num_valid']} molecules")

# Step 2: Find molecules similar to benzene
results = find_similar_molecules(
    query_smiles="c1ccccc1",
    candidate_smiles_list=library,
    threshold=0.3,
)
for m in results["matches"]:
    print(f"  {m['canonical_smiles']}: {m['similarity']:.3f}")
```

### Clustering

```python
# Cluster a diverse set of molecules
library = [
    "c1ccccc1", "c1ccc(O)cc1", "c1ccc(N)cc1",  # aromatics
    "CCCCCC", "CCCCCCC", "CCCCCCCC",              # alkanes
    "CC(=O)O", "CCC(=O)O",                        # acids
]
result = cluster_molecules(smiles_list=library, cutoff=0.4)
for cluster in result["clusters"]:
    centroid = cluster["centroid"]["canonical_smiles"]
    members = [m["canonical_smiles"] for m in cluster["members"]]
    print(f"Cluster {cluster['cluster_id']} (centroid: {centroid}): {members}")
```

### Combined workflow

```python
# Cluster, then find the most similar pair within the largest cluster
clusters = cluster_molecules(smiles_list=big_library, cutoff=0.5)
largest = clusters["clusters"][0]
member_smiles = [m["canonical_smiles"] for m in largest["members"]]

# Search within the cluster for close analogs of the centroid
hits = find_similar_molecules(
    query_smiles=largest["centroid"]["canonical_smiles"],
    candidate_smiles_list=member_smiles,
    threshold=0.8,
)
print(f"Closest analogs to centroid: {[h['canonical_smiles'] for h in hits['matches']]}")
```

## Fingerprint Type Guide

| Type | Best For | Notes |
|------|----------|-------|
| Morgan (ECFP) | General similarity, SAR | radius=2 ≈ ECFP4, radius=3 ≈ ECFP6 |
| RDKit | Topological similarity | Good for scaffold-level comparison |
| MACCS | Quick screening | 166 predefined keys, fast but less specific |
