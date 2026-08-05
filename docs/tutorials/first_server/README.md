# Your First Server

This tutorial walks you through building a working Agora Workbench MCP server from scratch — one that has a real domain tool, a correct file layout, and a Docker-ready entry point.

By the end you will have:

- A `ServerConfig` (uv environment — no conda needed)
- One `ToolDefinition` with its implementation in a separate kernel-safe package
- A `ToolRegistry` with `package=` that automatically resolves kernel import paths
- A separate implementation file that is safe to run in the kernel
- A `--warm` flag for Docker pre-build

To see a version of this pattern that leverages the more advanced feature of Agora Workbench, jump to the
[chemistry example](https://github.com/microsoft/agora-workbench/tree/main/examples/servers/chemistry).
For advanced tool features (state transitions, skills, affordances) see the
[Tool pattern](../../guide/tool-pattern.md) and [Skill pattern](../../guide/skill-pattern.md) guides.

## Prerequisites

Create a virtual environment and install the released Agora Workbench package:

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
python -m pip install "agora-workbench==0.1.2"
```

---

## Step 1 — The minimal server (no tools)

Start with the snippet from [Options for making a CodeExecutionServer](../../guide/server-options.md):

```python
# server.py
import asyncio
from agora_workbench.code_execution import CodeExecutionServer, ServerConfig
from agora_workbench.code_execution.auth import create_noop_auth_config

config = ServerConfig(
    name="myserver",
    description="Execute Python code.",
    type="uv",
    dependency_file="# third-party packages, one per line (e.g. numpy)\n",
)

server = CodeExecutionServer(server_config=config, auth_config=create_noop_auth_config())

if __name__ == "__main__":
    asyncio.run(server.run_http(host="0.0.0.0", port=8000))
```

This already gives the agent an `execute_myserver_code` MCP tool. The agent can
run Python in a fresh `uv` environment and use standard-library modules without
listing them as dependencies.

**File layout so far:**

```
myserver/
└── server.py
```

---

## Step 2 — Understand the kernel/server boundary

Before adding a tool, it is important to understand the two separate Python processes involved:

```
┌──────────────────────────────────────────┐
│  Server process  (your machine / Docker) │
│  • imports Agora Workbench               │
│  • holds ToolDefinition metadata         │
│  • manages the MCP endpoint              │
└────────────────┬─────────────────────────┘
                 │ spawns / communicates with
┌────────────────▼─────────────────────────┐
│  Kernel process  (isolated environment)  │
│  • runs agent-provided Python code       │
│  • imports your tool implementation      │
│  • CANNOT import agora_workbench.code_execution          │
└──────────────────────────────────────────┘
```

The `ToolRegistry(package=...)` setting tells the server how to resolve kernel imports. When you register a tool named `"summarize_numbers"` with `package="myserver_tools"`, the kernel will execute:

```python
from myserver_tools.summarize_numbers import summarize_numbers
```

This means the implementation module (`myserver_tools/summarize_numbers.py`) must:

- Be installable into the kernel environment (as a pip package via `additional_commands`)
- **Not** import anything from `code_execution` — those packages are only in the server environment

The `ToolDefinition` (the metadata object) lives in your server code. The function implementation lives in a pip-installable package that is installed into the kernel environment.

---

## Step 3 — Add a tool implementation package

Create a minimal pip-installable package for the tool implementation:

```
myserver/
├── server.py
└── myserver_tools/               ← new
    ├── pyproject.toml
    └── src/
        └── myserver_tools/
            ├── __init__.py
            └── summarize_numbers.py
```

**`myserver_tools/pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "myserver-tools"
version = "0.1.0"
requires-python = ">=3.11"
```

**`myserver_tools/src/myserver_tools/__init__.py`**

```python
# intentionally empty
```

**`myserver_tools/src/myserver_tools/summarize_numbers.py`**

```python
def summarize_numbers(numbers: list[float]) -> dict:
    """Return basic summary statistics for a list of numbers."""
    import numpy as np

    if not numbers:
        raise ValueError("numbers must not be empty")

    arr = np.array(numbers, dtype=float)
    return {
        "count": int(len(arr)),
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "stdev": float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0,
        "min": float(np.min(arr)),
        "max": float(np.max(arr)),
    }
```

Note: The implementation imports `numpy` lazily (inside the function body).
This keeps the module importable even if `numpy` is not installed at import
time and is a pattern you will see throughout the example servers.
`numpy` is a third-party library, so it must be listed in `dependency_file`
(added in Step 5).

---

## Step 4 — Add the ToolDefinition and ToolRegistry to the server

Create a `tools/` directory alongside `server.py` to hold the server-side metadata:

```
myserver/
├── server.py
├── tools/
│   ├── __init__.py
│   └── definitions.py            ← new
└── myserver_tools/
    └── ...
```

**`tools/__init__.py`**

```python
from .definitions import MYSERVER_TOOLS

__all__ = ["MYSERVER_TOOLS"]
```

**`tools/definitions.py`**

```python
from agora_workbench.code_execution import ReturnSpec, ToolDefinition, ToolParameter

summarize_numbers = ToolDefinition(
    name="summarize_numbers",
    description=(
        "Compute summary statistics (count, mean, median, stdev, min, max) "
        "for a list of numbers."
    ),
    required_parameters=[
        ToolParameter(
            name="numbers",
            type=list,
            description="List of numeric values to summarize.",
        ),
    ],
    return_spec=[
        ReturnSpec(name="count", type=int, description="Number of values"),
        ReturnSpec(name="mean", type=float, description="Arithmetic mean"),
        ReturnSpec(name="median", type=float, description="Median value"),
        ReturnSpec(name="stdev", type=float, description="Sample standard deviation"),
        ReturnSpec(name="min", type=float, description="Minimum value"),
        ReturnSpec(name="max", type=float, description="Maximum value"),
    ],
)

MYSERVER_TOOLS = [summarize_numbers]
```

Notice that there is no `module` field here — the kernel import path will be resolved automatically when the tool is registered with a `ToolRegistry` that has a `package` configured (next step).

---

## Step 5 — Wire everything together

Update `server.py` to register the tool:

```python
# server.py
import asyncio
import sys
from pathlib import Path

from agora_workbench.code_execution import CodeExecutionServer, ServerConfig, ToolRegistry
from agora_workbench.code_execution.auth import create_noop_auth_config

from tools import MYSERVER_TOOLS

# Path to the implementation package so it can be pip-installed into the kernel
_TOOLS_PKG = str(Path(__file__).resolve().parent / "myserver_tools")

config = ServerConfig(
    name="myserver",
    description="Execute Python code with numpy helpers.",
    type="uv",
    dependency_file="numpy\n",
    # Install the implementation package into the kernel environment
    additional_commands=[
        f"pip install --no-deps {_TOOLS_PKG}",
    ],
)

registry = ToolRegistry(package="myserver_tools")
for tool_def in MYSERVER_TOOLS:
    registry.register_tool(tool_def)

server = CodeExecutionServer(
    server_config=config,
    tool_registry=registry,
    auth_config=create_noop_auth_config(),
)

if __name__ == "__main__":
    if "--warm" in sys.argv:
        # Pre-build the kernel environment (useful in Docker)
        asyncio.run(server.warm())
    else:
        asyncio.run(server.run_http(host="0.0.0.0", port=8000))
```

**Final file layout:**

```
myserver/
├── server.py                     ← entry point
├── tools/
│   ├── __init__.py               ← exports MYSERVER_TOOLS
│   └── definitions.py            ← ToolDefinition metadata
└── myserver_tools/               ← pip package for the kernel
    ├── pyproject.toml
    └── src/
        └── myserver_tools/
            ├── __init__.py
            └── summarize_numbers.py  ← pure implementation, no server deps
```

---

## Step 6 — Run and verify

```bash
# From the myserver/ directory
python server.py
```

The server starts on `http://localhost:8000`. You can verify it is healthy:

```bash
curl http://localhost:8000/health
```

When an agent calls `execute_myserver_code`, it can use the tool like this:

```python
result = summarize_numbers(numbers=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
print(f"Mean: {result['mean']}, Stdev: {result['stdev']:.2f}")
```

The agent also has a `search_myserver_tools` MCP tool to discover registered tools by description.

---

## The `--warm` flag for Docker

Passing `--warm` runs `server.warm()`, which builds the kernel environment and exits without serving. This is the recommended pattern for Docker images:

```dockerfile
# Dockerfile (excerpt)
RUN python server.py --warm
CMD ["python", "server.py"]
```

Pre-building the environment in the image layer means the container is ready to serve immediately on startup.

---

## What you built

| Component | File | Purpose |
|-----------|------|---------|
| `ServerConfig` | `server.py` | uv environment, dependency spec, install commands |
| `ToolDefinition` | `tools/definitions.py` | Schema metadata (params, returns, affordances) |
| `ToolRegistry` | `server.py` | Registers tools, resolves kernel import paths via `package=` |
| Implementation | `myserver_tools/summarize_numbers.py` | Pure function — no server deps |
| Entry point | `server.py` | `--warm` vs HTTP serve |

---

## Where to go next

- **Production reference**: the [chemistry example](https://github.com/microsoft/agora-workbench/tree/main/examples/servers/chemistry) applies all of these patterns at scale — conda environment, multiple tools with state transitions, skills, blob storage publishers, and prelude injection.
- **More tool features**: [Tool pattern](../../guide/tool-pattern.md) covers `StateTransition`, `affordances`, optional parameters, and `ReturnSpec` in depth.
- **Multi-step workflows**: [Skill pattern](../../guide/skill-pattern.md) shows how to compose tools into agent-executable skill guides.
- **All server options**: [Options for making a CodeExecutionServer](../../guide/server-options.md) is the reference for `ServerConfig`, auth, publishers, and more.
