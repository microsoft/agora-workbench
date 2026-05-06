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
cd src/domains/chemistry
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

## License

RDKit is distributed under the [BSD 3-Clause License](https://github.com/rdkit/rdkit/blob/master/license.txt).
