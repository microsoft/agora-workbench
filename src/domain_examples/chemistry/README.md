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

The server will be available at `http://localhost:8020`. The chemistry conda
environment is built into the image during `docker compose up --build`, so
container startup does not need to build it on first run.

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

In addition to the general `execute_chemistry_code` tool, the server registers domain-specific tools that are injected into the execution kernel as callable functions. These tools form a **state graph** demonstrating how tool transitions model workflows at increasing levels of complexity.

### Architecture: Installed Tools Package

Tool **implementations** live in `chemistry_tools/`, a standalone pip-installable Python package. This package is installed into the conda execution environment at build time via `additional_commands`. The server holds only the **metadata** (`ToolDefinition` objects in `tools/definitions.py`) — schemas, state transitions, and affordances.

```
chemistry/
├── chemistry_tools/          # Pip package installed in the kernel environment
│   ├── pyproject.toml
│   └── src/chemistry_tools/  # Pure implementation functions (no server deps)
│       ├── parse_molecule.py
│       ├── compute_descriptors.py
│       └── ...
├── tools/                    # Server-side metadata only (ToolDefinition objects)
│   ├── __init__.py           # Exports CHEMISTRY_TOOLS list
│   └── definitions.py       # All ToolDefinition objects (module="chemistry_tools.xxx")
├── states.py                 # State vocabulary enum + affordances
├── skills/                   # Workflow-oriented skill guides
└── server/
    └── chemistry_server.py   # Registers tools, installs package via additional_commands
```

This separation means:
- The kernel can `from chemistry_tools.parse_molecule import parse_molecule` directly
- Implementation code has zero dependencies on the server framework
- The package can be tested independently with just RDKit installed

### State Graph

```
parse_molecule ──► molecule_parsed ──┬── enumerate_functional_groups ──► groups_identified
                                     ├── compute_descriptors ──► descriptors_computed
                                     │       └── filter_drug_candidates ──► candidates_filtered
                                     └── compute_fingerprints ──► fingerprints_computed
                                             ├── find_similar_molecules ──► similarity_computed
                                             └── cluster_molecules ──► molecules_clustered
```

### Tool Summary

| Tool | Chain | Requires | Produces |
|------|-------|----------|----------|
| `parse_molecule` | Entry point | — | `molecule_parsed` |
| `enumerate_functional_groups` | Molecular analysis | `molecule_parsed` | `groups_identified` |
| `compute_descriptors` | Drug screening | `molecule_parsed` | `descriptors_computed` |
| `filter_drug_candidates` | Drug screening | `descriptors_computed` | `candidates_filtered` |
| `compute_fingerprints` | Similarity/Clustering | `molecule_parsed` | `fingerprints_computed` |
| `find_similar_molecules` | Similarity | `fingerprints_computed` | `similarity_computed` |
| `cluster_molecules` | Clustering | `fingerprints_computed` | `molecules_clustered` |

Tools are defined in `tools/` and registered in `chemistry_server.py` via `ToolRegistry`. Each workflow has a corresponding skill file in `skills/` describing the full chain. The state vocabulary is defined in `states.py`.

### Using domain tools in code

Domain tools are available as regular Python functions inside `execute_chemistry_code`:

```python
# Chain 1: Molecular analysis
info = parse_molecule(smiles="CC(=O)Oc1ccccc1C(=O)O")
groups = enumerate_functional_groups(smiles="CC(=O)Oc1ccccc1C(=O)O")

# Chain 2: Drug screening
descriptors = compute_descriptors(smiles="c1ccccc1")
screening = filter_drug_candidates(
    smiles_list=["CCO", "c1ccccc1", "CC(=O)Oc1ccccc1C(=O)O"],
    rules="both",
)

# Chain 3: Similarity & clustering (branching graph)
fps = compute_fingerprints(smiles_list=library)
hits = find_similar_molecules(query_smiles="c1ccccc1", candidate_smiles_list=library)
clusters = cluster_molecules(smiles_list=library, cutoff=0.4)
```

## License

RDKit is distributed under the [BSD 3-Clause License](https://github.com/rdkit/rdkit/blob/master/license.txt).
