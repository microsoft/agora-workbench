---
name: build-box
description: Construct an initial vitrimer simulation box from acid and epoxide SMILES using EMC with the PCFF force field.
parent_skill: vitrimer-tg-estimation
---

# Build Vitrimer Box

Use this skill when the user has acid and epoxide monomer SMILES and needs to create an initial LAMMPS simulation box for Tg estimation.

## What It Does

The `build_vitrimer_box` tool uses **EMC (Enhanced Monte Carlo)** to:

1. Parse acid and epoxide SMILES with connection points
2. Construct an alternating copolymer chain topology
3. Place ~4 chains in a cubic periodic box using Monte Carlo placement
4. Parameterize the system with the **PCFF** (Polymer Consistent Force Field)
5. Output LAMMPS-compatible `polymer.data` and `polymer.params` files

## SMILES Requirements

Monomer SMILES must include connection points marked with `*` to indicate where polymerization occurs:

```
Acid example:     *C(=O)CCCCC(=O)*           (adipic acid)
Epoxide example:  *C(O)COc1ccc(C(C)(C)c2ccc(OCC(*)O)cc2)cc1  (BADGE, opened)
```

The `*` atoms tell EMC where to form bonds between alternating acid and epoxide units.

> **Implementation note:** Internally, the tool merges the acid and epoxide
> SMILES into a single repeat-unit SMILES (bonding one `*` from each monomer)
> so that EMC treats the chain as a homopolymer.  This avoids a limitation in
> EMC's polymer builder where separate alternating groups with two connection
> points each fail with "Groups … do not connect" errors.  Both input SMILES
> must have exactly **2** connection points (`*`).

## Calling the Tool

```python
result = build_vitrimer_box(
    acid_smiles="*C(=O)CCCCC(=O)*",
    epoxide_smiles="*C(O)COc1ccc(C(C)(C)c2ccc(OCC(*)O)cc2)cc1",
    density=0.5,     # g/cm³ — deliberately low; annealing densifies
    ntotal=4000,     # ~4 chains of ~1000 atoms
    seed=42,
)
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `acid_smiles` | str | *required* | Acid monomer SMILES with `*` connection points |
| `epoxide_smiles` | str | *required* | Epoxide monomer SMILES with `*` connection points |
| `density` | float | 0.5 | Initial box density in g/cm³ |
| `ntotal` | int | 4000 | Target total atom count |
| `seed` | int | 42 | Random seed for EMC placement |

### Return Values

| Field | Type | Description |
|-------|------|-------------|
| `success` | bool | Whether construction succeeded |
| `work_dir` | str | Path to the working directory — pass this to subsequent tools |
| `num_atoms` | int | Actual number of atoms placed |
| `data_file` | str | Path to `polymer.data` |
| `params_file` | str | Path to `polymer.params` |
| `error` | str | Error message if failed |

## Common Issues

### "PCFF parameterization failed"
The monomer contains atom types not covered by PCFF. Try:
- Simplifying the structure (remove exotic heteroatoms)
- Checking that SMILES are valid with RDKit before calling

### "EMC failed after N attempts"
EMC's Monte Carlo placement couldn't converge. Try:
- Reducing `ntotal` (fewer atoms = easier placement)
- Using a different `seed`
- Ensuring connection points are correctly placed in the SMILES

### Low atom count
If `num_atoms` is much less than `ntotal`, EMC couldn't place enough monomers. This usually means the monomers are too large for the requested `ntotal`. Increase `ntotal` proportionally.

## What Happens Next

The `work_dir` returned by this tool is passed directly to `run_equilibration`:

```python
eq_result = run_equilibration(work_dir=result["work_dir"])
```
