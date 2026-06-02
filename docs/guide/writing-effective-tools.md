# Writing effective tools and skills

This guide covers best practices for defining `ToolDefinition` objects and writing skill workflows that work well with LLM agents.

## Tool design principles

### 1. Write clear, action-oriented descriptions

The `description` field is what the agent sees during tool search. Make it concrete:

```python
# ✗ Vague
description="Process molecule data"

# ✓ Specific
description=(
    "Parse a SMILES string and return canonical SMILES, molecular formula, "
    "molecular weight, heavy-atom count, and bond count."
)
```

### 2. Use meaningful affordances

Affordances are natural-language phrases that improve search recall. Think about how a user might ask for this tool:

```python
affordances=[
    "parse a SMILES string",
    "get the molecular weight of a compound",
    "canonicalize SMILES",
    "what is the formula for this molecule",
]
```

### 3. Return structured dictionaries

Tools should return dictionaries with well-named keys. The `return_spec` documents these for the agent:

```python
return_spec=[
    ReturnSpec(name="canonical_smiles", type=str, description="Canonical SMILES"),
    ReturnSpec(name="molecular_weight", type=float, description="Molecular weight in Daltons"),
]
```

### 4. Fail fast with clear errors

Validate inputs early and raise `ValueError` with a message the agent can act on:

```python
def parse_molecule(smiles: str) -> dict:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(
            f"Invalid SMILES string: {smiles!r}. "
            "Check for unbalanced parentheses or invalid atom symbols."
        )
    ...
```

### 5. Keep tools focused

Each tool should do one thing well. If a function does multiple steps, split it:

```python
# ✗ Too broad
compute_everything = ToolDefinition(name="compute_everything", ...)

# ✓ Focused, composable
parse_molecule = ToolDefinition(name="parse_molecule", ...)
compute_descriptors = ToolDefinition(name="compute_descriptors", ...)
compute_fingerprints = ToolDefinition(name="compute_fingerprints", ...)
```

### 6. Use state transitions for ordering

Declare what each tool requires and produces so the agent knows the correct call order:

```python
state_transition=StateTransition(
    requires=frozenset({"chemistry.molecule_parsed"}),
    produces=frozenset({"chemistry.descriptors_computed"}),
)
```

## Parameter best practices

### Use descriptive parameter names

```python
# ✗ Ambiguous
ToolParameter(name="s", type=str, description="Input string")

# ✓ Clear
ToolParameter(name="smiles", type=str, description="SMILES string for the molecule")
```

### Provide optional parameters with sensible defaults

```python
optional_parameters=[
    ToolParameter(
        name="n_clusters",
        type=int,
        description="Number of clusters for k-means (default: 5)",
        default=5,
    ),
]
```

### Use type annotations consistently

Supported types for `ToolParameter.type`: `str`, `int`, `float`, `bool`, `list`, `dict`, `Path`.

## Skill writing best practices

### 1. Start with "when to use"

The agent needs to know when this skill applies:

```markdown
## When to use
When the user asks about molecular similarity, compound clustering,
or finding structurally related molecules.
```

### 2. Show the state graph visually

ASCII art helps the agent understand flow:

```markdown
## State Graph
```
parse_molecule → compute_fingerprints → compute_similarity
                                      → cluster_molecules
```
```

### 3. Give concrete instructions per step

```markdown
## Steps
1. **Parse all molecules** — call `parse_molecule` for each SMILES in the input set
2. **Compute fingerprints** — call `compute_fingerprints(smiles_list=[...], fp_type="morgan")`
3. **Compute pairwise similarity** — call `compute_similarity(fingerprints=...)`
```

### 4. Include error handling guidance

```markdown
## Error handling
- If `parse_molecule` fails, report the invalid SMILES to the user and skip it
- If fewer than 2 valid molecules remain, inform the user that clustering requires at least 2
```

### 5. Document output formatting expectations

```markdown
## Output format
Present results as a table with columns: Molecule, Cluster, Similarity Score.
If the user asked for a plot, generate a dendrogram using matplotlib.
```

## Common pitfalls

| Pitfall | Fix |
|---------|-----|
| Tool description says "internal helper" | Write user-facing descriptions — the agent is your user |
| No `return_spec` defined | Always document return fields so the agent knows what to expect |
| Tool does I/O the agent can't see | Return paths or summaries; don't just write to disk silently |
| Skill has no error guidance | The agent will retry endlessly — tell it when to stop |
| State names are generic (`"done"`, `"ready"`) | Use domain-qualified names (`"chemistry.molecule_parsed"`) |
| Tool implementation imports heavy libs at module level | Import inside the function — the kernel environment may differ |
