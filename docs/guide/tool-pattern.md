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
from agora_workbench.code_execution import ToolDefinition, ToolParameter, ReturnSpec, StateTransition

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

Tool implementations live in a **pip-installable package** that is installed into the kernel environment at build time. This is the recommended pattern because:

1. The kernel and server are **separate Python processes** — the kernel cannot import server-side packages like `code_execution`, and the server shouldn't need heavy domain dependencies like RDKit or PyPSA.
2. A pip-installable package can be tested independently of the server framework.
3. The `additional_commands` in `ServerConfig` installs it into the kernel automatically.

### Implementation package structure

```
my_domain_tools/
├── pyproject.toml
└── src/
    └── my_domain_tools/
        ├── __init__.py           ← empty
        ├── parse_molecule.py     ← one module per tool (matches tool name)
        └── compute_descriptors.py
```

**`pyproject.toml`** (minimal):

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "my-domain-tools"
version = "0.1.0"
requires-python = ">=3.6"
# No dependencies — domain libraries come from the kernel environment (conda/uv)
```

### Writing a tool function

Each tool is a regular Python function that returns a dict:

```python
# my_domain_tools/parse_molecule.py

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

!!! warning "Kernel import boundary"
    Implementation modules must **not** import from `code_execution`, `base`, `connector`, or any server-side package. These are only available in the server process. The kernel environment has only the packages listed in your `ServerConfig.dependency_file` plus whatever you install via `additional_commands`.

!!! info "Kernel Python version"
    The execution kernel can run any Python ≥ 3.6. The server's injected infrastructure is compatible with Python 3.6+, so tool packages may target older runtimes (e.g., legacy ML stacks with Python 3.6–3.9). The server process itself requires Python ≥ 3.11.

### Reaching a sidecar from a tool

Tool code runs in a **fresh kernel process per session**, so loading an
expensive resource (a large model) inside a tool pays that cost *every session*
— on a modest host this exhausts RAM. The fix is a
**[sidecar](sidecars.md)**: the server loads the resource once per container and
exports its URL to every kernel via an environment variable. A tool reaches it
over loopback HTTP — no server-side imports required (which the boundary above
forbids anyway):

```python
# my_domain_tools/predict.py
import os
import httpx

def predict(smiles: str) -> dict:
    """Run inference against the shared model sidecar."""
    base = os.environ.get("MYMODEL_SERVICE_URL")  # SidecarConfig.url_env_var
    if base:
        resp = httpx.post(f"{base}/predict", json={"smiles": smiles}, timeout=600)
        resp.raise_for_status()
        return resp.json()
    # Dev/test fallback: load and run the model in-process.
    from .model import load_model
    return load_model().predict(smiles)
```

The `url_env_var` name is whatever you set on the `SidecarConfig`; guarding on
its presence keeps the same tool working in unit tests and local development
without the sidecar running. See [Sidecars](sidecars.md) for how to declare and
serve one. (This mirrors how static [assets](working-with-data.md#asset-provisioning)
are reached via `MCP_ASSET_CACHE_DIR`.)

### Installing the package into the kernel

Use `additional_commands` in your `ServerConfig` to pip-install the tools package into the kernel environment:

```python
from pathlib import Path

_TOOLS_PKG = str(Path(__file__).resolve().parent / "my_domain_tools")

config = ServerConfig(
    name="myserver",
    description="...",
    type="uv",
    dependency_file="numpy\npandas\n",
    additional_commands=[
        f"pip install --no-deps {_TOOLS_PKG}",
    ],
)
```

The `--no-deps` flag is intentional — domain dependencies (RDKit, numpy, etc.) are already declared in `dependency_file` and managed by the environment. The tools package only provides the function code.

## Registering tools

```python
from agora_workbench.code_execution import ToolRegistry

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
from agora_workbench.code_execution.auth import create_noop_auth_config

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

## Stateful domains — sharing objects across tool calls

Each session has a **persistent Jupyter kernel**. Variables assigned in one `execute_*_code` call remain available in subsequent calls within the same session. This is the intended mechanism for sharing state between tool invocations.

### The pattern: tools return live objects, the agent holds them in variables

For domains where tools operate on complex objects (simulation models, networks, circuits, dataframes), tools should **return the live object** and accept it as a parameter in downstream calls:

```python
# energysystems_tools/define_network.py

import pypsa

def define_network(name: str, snapshots: int = 24) -> pypsa.Network:
    """Create a PyPSA network and return it."""
    import pandas as pd

    network = pypsa.Network()
    network.name = name
    network.set_snapshots(pd.date_range("2025-01-01", periods=snapshots, freq="h"))
    return network
```

```python
# energysystems_tools/add_bus.py

import pypsa

def add_bus(network: pypsa.Network, name: str, v_nom: float = 110.0) -> dict:
    """Add a bus to an existing network."""
    network.add("Bus", name, v_nom=v_nom)
    return {"bus_name": name, "v_nom": v_nom, "total_buses": len(network.buses)}
```

The agent composes these naturally:

```python
# Agent-generated code — variables persist in the kernel across calls
network = define_network("grid1", snapshots=48)
add_bus(network, "high_voltage", v_nom=220)
add_bus(network, "low_voltage", v_nom=20)
```

In a subsequent `execute_*_code` call, `network` is still available:

```python
# Later call in the same session — kernel state persists
result = optimize(network, solver="highs")
print(result["objective_value"])
```

### Resuming a session from another agent

Every execution response includes a `session_id`. If a different agent or a new
MCP connection must process the retained state, pass that value explicitly:

```text
execute_energysystems_code(
    execution_session_id="<session_id from the earlier execution>",
    description="Optimize the retained network.",
    code='result = optimize(network, solver="highs")\nprint(result["objective_value"])',
)
```

`execution_session_id` is an execution-kernel identifier, not the MCP transport
session ID. The server verifies that the caller owns the session; unknown,
expired, or unauthorized IDs fail without creating a new kernel.

### Why this works

- The kernel process is **one-per-session** and lives for the session's lifetime
- Tool proxy functions run in the kernel's `__main__` namespace
- Variables assigned in `__main__` persist across all `execute_*_code` invocations
- Tools receive and return live Python objects — no serialization boundary

!!! note "This sharing is per *session*, not per *container*"
    Kernel variables are shared across tool calls **within one session**. They
    are **not** shared between sessions — each session is a separate kernel
    process with its own copy. To share one expensive instance (e.g. a model)
    across *all* sessions, don't hold it in a kernel variable; serve it from a
    **[sidecar](sidecars.md)**.

### What to avoid

**Do not** use `builtins._*` as a hidden registry for sharing state between tools:

```python
# ✗ Anti-pattern — hidden shared state via builtins
def define_network(name: str) -> dict:
    network = pypsa.Network()
    import builtins
    if not hasattr(builtins, "_networks"):
        builtins._networks = {}
    builtins._networks[name] = network  # hidden side-effect
    return {"name": name}  # returns a summary, not the object

def optimize(name: str) -> dict:
    import builtins
    network = builtins._networks[name]  # magic lookup
    ...
```

This pattern obscures data flow from the agent and makes tools harder to compose. Instead, let the agent manage object references explicitly through kernel variables.

### Return type guidance for stateful tools

| Scenario | Return type | Example |
|----------|-------------|---------|
| Object will be passed to other tools | The live object | `-> pypsa.Network` |
| Object + summary for the agent | Tuple or the object (agent can `print()` it) | `-> pypsa.Network` |
| Pure query / leaf operation | Dict with results | `-> dict` |
| Mutation of existing object | Dict summarizing what changed | `-> dict` |

!!! tip "ToolDefinition return_spec for non-serializable returns"
    `return_spec` documents the JSON-serializable summary that appears in traces. When a tool returns a live object, the tracing layer records it as a type reference (e.g. `<Network@1>`). You can still provide `return_spec` to document the *semantics* of what's returned, even if the actual value isn't a dict.
