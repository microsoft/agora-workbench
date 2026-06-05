# CodeExecutionServer skill pattern

Skills are multi-step workflows described in Markdown that guide the agent through a sequence of tool calls. They build on top of the [tool pattern](tool-pattern.md) by composing tools into directed graphs with state tracking.

## What is a skill?

A skill is a `SKILL.md` file that:

1. Declares the domain it covers and what states the workflow passes through
2. Provides natural-language instructions the agent follows
3. Describes the state graph — which tools to call in what order

Skills are passed explicitly to `CodeExecutionServer` via the `skills=` parameter.

## File structure

```
domains/
└── chemistry/
    └── skills/
        ├── SKILL.md                    # Main skill descriptor
        ├── molecular-analysis.md       # Workflow: molecular analysis chain
        ├── drug-screening.md           # Workflow: drug-likeness screening
        └── similarity-and-clustering.md # Workflow: fingerprint similarity
```

## SKILL.md format

The `SKILL.md` file uses YAML front matter for metadata and Markdown body for agent instructions:

```markdown
---
name: chemistry-rdkit
description: >
  Molecular analysis and cheminformatics using RDKit — SMILES handling,
  descriptor calculation, fingerprints, substructure search, similarity,
  clustering, and drug-likeness screening.
states:
  - chemistry.molecule_parsed
  - chemistry.groups_identified
  - chemistry.descriptors_computed
  - chemistry.candidates_filtered
  - chemistry.fingerprints_computed
  - chemistry.similarity_computed
  - chemistry.molecules_clustered
---

# Chemistry / RDKit

Use this skill when the user asks about molecules, chemical structures, SMILES,
molecular properties, similarity, substructure matching, or any cheminformatics
task.

## State Graph Overview

The domain tools form a directed graph of workflows. `parse_molecule` is the
entry point; downstream tools have prerequisite states.

```
parse_molecule
  → chemistry.molecule_parsed
        ├── enumerate_functional_groups → chemistry.groups_identified
        ├── compute_descriptors → chemistry.descriptors_computed
        │       └── filter_drug_candidates → chemistry.candidates_filtered
        └── compute_fingerprints → chemistry.fingerprints_computed
                ├── compute_similarity → chemistry.similarity_computed
                └── cluster_molecules → chemistry.molecules_clustered
```
```

## How skills connect to tools

Each tool's `StateTransition` annotation declares:

- **`requires`** — states that must exist before this tool can run
- **`produces`** — states this tool generates after successful execution

The skill's `states` list in the front matter enumerates all states in the workflow. The agent uses this to plan which tools to call and in what order.

## Registering skills with the server

Pass `Skill` objects to `CodeExecutionServer` at construction time. There are two patterns:

### Option A: Explicit definition (recommended for small skill sets)

Define `Skill` objects directly in your server module. This gives you full control over metadata and makes the server self-contained:

```python
from pathlib import Path
from code_execution import CodeExecutionServer, Skill

_SKILL_PATH = Path(__file__).parent / "skills" / "SKILL.md"

SKILLS = [
    Skill(
        name="chemistry-rdkit",
        description="Molecular analysis and cheminformatics using RDKit...",
        domain="chemistry",
        states=[
            "chemistry.molecule_parsed",
            "chemistry.descriptors_computed",
            "chemistry.fingerprints_computed",
        ],
        content=_SKILL_PATH.read_text(encoding="utf-8"),
        path=str(_SKILL_PATH),
    ),
]

server = CodeExecutionServer(
    server_config=config,
    tool_registry=tool_registry,
    skills=SKILLS,
)
```

### Option B: Filesystem discovery (convenient for many skills)

Use `discover_skills()` to scan a directory for all `*.md` files with valid YAML frontmatter:

```python
from pathlib import Path
from code_execution import CodeExecutionServer, discover_skills

skills = discover_skills(Path(__file__).parent / "skills", domain="chemistry")

server = CodeExecutionServer(
    server_config=config,
    tool_registry=tool_registry,
    skills=skills,
)
```

The function recursively scans for `.md` files with a `name:` field in their frontmatter and returns `Skill` objects with their full content loaded.

## Writing workflow documents

Each workflow file (e.g., `molecular-analysis.md`) provides step-by-step instructions for one path through the state graph:

```markdown
# Molecular Analysis

## When to use
When the user wants to analyze a molecule's structure, properties, or functional groups.

## Steps

1. **Parse the molecule** — call `parse_molecule(smiles=...)` to validate and canonicalize
2. **Identify functional groups** — call `enumerate_functional_groups(smiles=...)` 
3. **Compute descriptors** — call `compute_descriptors(smiles=...)` for physicochemical properties

## Notes
- Always start with `parse_molecule` to validate the input SMILES
- Present results in a structured table when multiple descriptors are computed
```

## Benefits of the skill pattern

- **Guided workflows** — the agent follows prescribed paths instead of guessing tool order
- **State validation** — prerequisite states prevent the agent from calling tools out of order
- **Discoverability** — skills are loaded at startup and matched to user requests
- **Composability** — multiple workflows can share the same underlying tools
- **Documentation as code** — the Markdown files serve as both agent instructions and human documentation

## When to use skills vs. bare tools

| Scenario | Use |
|----------|-----|
| Single-purpose tools (parse, convert, validate) | Tools only |
| Multi-step analysis with a clear order | Skill + tools |
| Branching workflows (analysis → screening OR clustering) | Skill with multiple workflow docs |
| Ad-hoc code execution with no domain tools | Neither — just `execute_{name}_code` |
