# Chemistry MCP Server (RDKit)

A domain-specific MCP code execution server for cheminformatics, powered by [RDKit](https://www.rdkit.org/) (BSD-3-Clause license).

Exposes an `execute_chemistry_code` MCP tool that runs Python code in an isolated environment with RDKit and common scientific packages pre-installed.

## Pre-installed Packages

| Package | Purpose |
|---------|---------|
| **rdkit** | Cheminformatics toolkit (SMILES parsing, fingerprints, descriptors, reactions) |
| **numpy** | Numerical computing |
| **pandas** | Data manipulation |
| **scipy** | Scientific computing |
| **matplotlib** | Plotting |
| **scikit-learn** | Machine learning |

## Quick Start

### 1. Build the base image (one-time)

```bash
cd src
docker build -f deployment/mcp_server/base.Dockerfile -t mcp-server-base:local .
```

### 2. Build and run the chemistry server

```bash
cd src/domain_examples/chemistry
docker compose up --build
```

The server will be available at `http://localhost:8020`. The first startup takes a few minutes while the conda environment is built (subsequent starts are cached).

### 3. Verify

```bash
curl http://localhost:8020/health
```

## Usage Examples

The `execute_chemistry_code` tool accepts Python code. Common RDKit modules are auto-imported (`Chem`, `Draw`, `Descriptors`, `AllChem`, `rdMolDescriptors`, `PandasTools`, `np`, `pd`).

### Parse SMILES and compute molecular weight

```python
mol = Chem.MolFromSmiles("CCO")  # ethanol
mw = Descriptors.MolWt(mol)
print(f"Molecular weight of ethanol: {mw:.2f}")
```

### Compute molecular descriptors

```python
smiles_list = ["CCO", "CC(=O)O", "c1ccccc1", "CC(=O)Oc1ccccc1C(=O)O"]
data = []
for smi in smiles_list:
    mol = Chem.MolFromSmiles(smi)
    data.append({
        "SMILES": smi,
        "MW": Descriptors.MolWt(mol),
        "LogP": Descriptors.MolLogP(mol),
        "HBD": Descriptors.NumHDonors(mol),
        "HBA": Descriptors.NumHAcceptors(mol),
        "TPSA": Descriptors.TPSA(mol),
    })
df = pd.DataFrame(data)
print(df.to_string(index=False))
```

### Substructure search

```python
molecules = [Chem.MolFromSmiles(s) for s in ["CCO", "CCCO", "c1ccccc1", "CC(=O)O"]]
pattern = Chem.MolFromSmarts("[OX2H]")  # hydroxyl group

matches = [Chem.MolToSmiles(m) for m in molecules if m.HasSubstructMatch(pattern)]
print(f"Molecules with -OH group: {matches}")
```

### Morgan fingerprints for similarity

```python
from rdkit import DataStructs

mol1 = Chem.MolFromSmiles("c1ccccc1")  # benzene
mol2 = Chem.MolFromSmiles("c1ccc(O)cc1")  # phenol
mol3 = Chem.MolFromSmiles("CCCCCC")  # hexane

fp1 = AllChem.GetMorganFingerprintAsBitVect(mol1, 2, nBits=2048)
fp2 = AllChem.GetMorganFingerprintAsBitVect(mol2, 2, nBits=2048)
fp3 = AllChem.GetMorganFingerprintAsBitVect(mol3, 2, nBits=2048)

print(f"Benzene vs Phenol: {DataStructs.TanimotoSimilarity(fp1, fp2):.3f}")
print(f"Benzene vs Hexane: {DataStructs.TanimotoSimilarity(fp1, fp3):.3f}")
```

## Authentication

This example uses `create_noop_auth_config()` (no authentication required). For production deployments with Entra ID, see the [deployment README](../../deployment/mcp_server/README.md).

## Domain Tools

In addition to the general `execute_chemistry_code` tool, the server registers three domain-specific tools that are injected into the execution kernel as callable functions. These demonstrate the `ToolRegistry` / `ToolDefinition` pattern at increasing levels of complexity:

| Tool | Complexity | Description |
|------|-----------|-------------|
| `parse_molecule` | Low | Parse SMILES → canonical form, formula, weight, atom/bond counts |
| `compute_descriptors` | Medium | Physicochemical descriptors with optional subset selection + Lipinski evaluation |
| `find_similar_molecules` | High | Fingerprint-based Tanimoto similarity search with algorithm selection |

These tools are defined in `tools/` and registered in `chemistry_server.py` via `ToolRegistry`. Each tool has a corresponding skill file in `skills/` describing usage patterns.

### Using domain tools in code

Domain tools are available as regular Python functions inside `execute_chemistry_code`:

```python
# Low: parse a molecule
info = parse_molecule(smiles="CCO")
print(info["molecular_formula"])  # C2H6O

# Medium: compute descriptors
result = compute_descriptors(smiles="c1ccccc1", descriptors=["logp", "tpsa"])
print(result["lipinski_pass"])  # True

# High: similarity search
matches = find_similar_molecules(
    query_smiles="c1ccccc1",
    candidate_smiles_list=["c1ccc(O)cc1", "CCCCCC"],
    threshold=0.3,
)
for m in matches["matches"]:
    print(f"{m['canonical_smiles']}: {m['similarity']:.3f}")
```

## License

RDKit is distributed under the [BSD 3-Clause License](https://github.com/rdkit/rdkit/blob/master/license.txt).
