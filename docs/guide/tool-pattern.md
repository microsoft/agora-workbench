# CodeExecutionServer tool pattern

Domain tools are typed Python functions that run inside the server's execution environment. They are registered via `ToolDefinition` metadata objects and invoked by the agent through the `execute_{name}_code` MCP tool.

## How tools work

1. You define a `ToolDefinition` — schema, parameters, return spec, and module path
2. The server registers it in a `ToolRegistry`
3. At runtime, the agent discovers tools via `search_{name}_tools` and calls them from within `execute_{name}_code` blocks
4. The server injects proxy wrappers so tool calls are traced and validated

Tools are **not** exposed as individual MCP tools. Instead, the agent writes Python code that imports and calls them. This gives the agent full programmatic flexibility — it can compose tools, loop over inputs, and handle errors in code.

## Defining a tool

```python
from code_execution import ToolDefinition, ToolParameter, ReturnSpec, StateTransition

parse_molecule = ToolDefinition(
    name="parse_molecule",
    description=(
        "Parse a SMILES string and return canonical SMILES, molecular formula, "
        "molecular weight, heavy-atom count, and bond count."
    ),
    required_parameters=[
        ToolParameter(name="smiles", type=str, description="SMILES string to parse"),
    ],
    return_spec=[
        ReturnSpec(name="canonical_smiles", type=str, description="Canonical SMILES"),
        ReturnSpec(name="molecular_formula", type=str, description="Molecular formula"),
        ReturnSpec(name="molecular_weight", type=float, description="Molecular weight in Da"),
    ],
    state_transition=StateTransition(
        produces=frozenset({"chemistry.molecule_parsed"}),
    ),
    affordances=[
        "parse a SMILES string",
        "get the molecular weight of a compound",
        "canonicalize SMILES",
    ],
)
```

Note that `module` is not set here — it will be resolved automatically when the tool is registered with a `ToolRegistry` that has a `package` configured (see [Registering tools](#registering-tools) below).

## ToolDefinition fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | ✓ | Function name (must match the actual function name in the module) |
| `description` | ✓ | What the tool does (shown to the agent during search) |
| `required_parameters` | ✓ | List of `ToolParameter` objects |
| `optional_parameters` | | Optional parameters with defaults |
| `return_spec` | | List of `ReturnSpec` objects describing the return dict |
| `module` | | Resolved import path (usually set automatically by `ToolRegistry`) |
| `module_override` | | Full module path override when the tool doesn't follow `{package}.{name}` convention |
| `state_transition` | | States this tool requires and produces (for skill workflows) |
| `affordances` | | Natural-language phrases describing what this tool can do (improves search) |

## Implementing the tool function

The actual implementation lives in a separate package that is installed into the execution environment:

```python
# chemistry_tools/parse_molecule.py

def parse_molecule(smiles: str) -> dict:
    """Parse a SMILES string and return molecular properties."""
    from rdkit import Chem
    from rdkit.Chem import Descriptors

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"Invalid SMILES: {smiles}")

    return {
        "canonical_smiles": Chem.MolToSmiles(mol),
        "molecular_formula": Chem.rdMolDescriptors.CalcMolFormula(mol),
        "molecular_weight": Descriptors.ExactMolWt(mol),
    }
```

!!! note "Separation of definition and implementation"
    The `ToolDefinition` (metadata) lives in your server code. The implementation lives in a pip-installable package that is installed into the execution environment via `additional_commands` in `ServerConfig`. This separation ensures the server process doesn't need domain-heavy dependencies.

## Registering tools

```python
from code_execution import ToolRegistry

from my_domain.tools import parse_molecule, compute_descriptors

registry = ToolRegistry(package="my_domain_tools")
registry.register_tool(parse_molecule)
registry.register_tool(compute_descriptors)
```

The `package` parameter tells the registry how to resolve kernel imports. For each tool, the proxy will generate `from {package}.{tool_name} import {tool_name}` inside the execution kernel. This means your implementation package should have one module per tool (e.g. `my_domain_tools/parse_molecule.py` containing a `parse_molecule()` function).

If a tool lives in a non-standard location (e.g. multiple tools in a shared module), use `module_override`:

```python
# Tool implementation lives in my_domain_tools/utils.py, not my_domain_tools/helper_func.py
helper_func = ToolDefinition(
    name="helper_func",
    description="...",
    module_override="my_domain_tools.utils",  # from my_domain_tools.utils import helper_func
)
```

Then pass the registry to your server:

```python
from code_execution.auth import create_noop_auth_config

server = CodeExecutionServer(
    server_config=config,
    tool_registry=registry,
    auth_config=create_noop_auth_config(),
)
```

## State transitions

Tools can declare state they produce and require, enabling workflow planning:

```python
compute_descriptors = ToolDefinition(
    name="compute_descriptors",
    description="Compute physicochemical descriptors for a parsed molecule.",
    required_parameters=[
        ToolParameter(name="smiles", type=str, description="Canonical SMILES"),
    ],
    return_spec=[...],
    module="chemistry_tools.compute_descriptors",
    state_transition=StateTransition(
        requires=frozenset({"chemistry.molecule_parsed"}),
        produces=frozenset({"chemistry.descriptors_computed"}),
    ),
)
```

This creates a directed graph of tool workflows. See [Skill pattern](skill-pattern.md) for how skills compose tools using these state annotations.

## How the agent uses tools

When an agent calls `execute_chemistry_code`, it writes Python that imports and calls the tool:

```python
# Agent-generated code sent to execute_chemistry_code
result = parse_molecule(smiles="CCO")
print(f"Formula: {result['molecular_formula']}")
print(f"Weight: {result['molecular_weight']:.2f} Da")
```

The server injects tracing proxies so that:

- Tool calls are recorded with timing, arguments, and results
- Errors are captured with full tracebacks
- The agent sees structured output
