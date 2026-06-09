# Options for making a CodeExecutionServer

`CodeExecutionServer` is the base class for all MCP code execution servers in Agora Workbench. This guide covers the different ways to configure and instantiate one.

#### New to Agora Workbench?
The [Your First Server](../tutorials/first_server/README.md) tutorial walks you through building a server with a real tool step-by-step before diving into the full reference below.

## Minimal server

The simplest server needs only a `ServerConfig` with a name, description, environment type, and dependency specification:

```python
import asyncio
from agora_workbench.code_execution import CodeExecutionServer, ServerConfig
from agora_workbench.code_execution.auth import create_noop_auth_config

config = ServerConfig(
    name="myserver",
    description="Execute Python code with numpy and pandas.",
    type="uv",
    dependency_file="numpy\npandas\n",
)

server = CodeExecutionServer(server_config=config, auth_config=create_noop_auth_config())

if __name__ == "__main__":
    asyncio.run(server.run_http(host="0.0.0.0", port=8000))
```

This gives you an MCP server with an `execute_myserver_code` tool that runs Python in an isolated environment with numpy and pandas available.

## Environment types

The `type` field on `ServerConfig` controls how the Python environment is managed:

| Type | Dependency format | Best for |
|------|-------------------|----------|
| `"uv"` | `requirements.txt` content | Fast installs, pure-Python packages |
| `"conda"` | `environment.yml` content | Scientific packages with native deps (RDKit, GDAL, etc.) |
| `"pip"` | `requirements.txt` content | Legacy environments or Docker-baked deps |

### Environment model

`CodeExecutionServer` always runs code in an isolated kernel environment built from `ServerConfig.dependency_file`, regardless of `type` (`uv`, `conda`, or `pip`).

- For `type="uv"` and `type="pip"`, `dependency_file` contains `requirements.txt` content
- For `type="conda"`, `dependency_file` contains `environment.yml` content
- For native/compiled dependencies in conda environments (for example `ngspice`, `gdal`, `netcdf4`), use **`conda-forge`** rather than system package managers like `apt-get`

### Example: conda environment

```python
config = ServerConfig(
    name="chemistry",
    description="RDKit cheminformatics environment.",
    type="conda",
    dependency_file="""\
name: chemistry
channels:
  - conda-forge
dependencies:
  - python=3.11
  - rdkit
  - numpy
  - pandas
""",
)
```

Minimal native-dependency example:

```python
config = ServerConfig(
    name="circuits",
    description="Conda environment with compiled/native dependencies.",
    type="conda",
    dependency_file="""\
name: circuits
channels:
  - conda-forge
dependencies:
  - python=3.11
  - ngspice
  - gdal
""",
)
```

## Adding domain tools

Pass a `ToolRegistry` to expose typed domain tools that are discoverable via `search_{name}_tools` and callable through code execution:

```python
from agora_workbench.code_execution import CodeExecutionServer, ServerConfig, ToolRegistry
from agora_workbench.code_execution.auth import create_noop_auth_config
from my_domain.tools import MY_TOOLS  # list of ToolDefinition objects

registry = ToolRegistry()
for tool in MY_TOOLS:
    registry.register_tool(tool)

server = CodeExecutionServer(
    server_config=config,
    tool_registry=registry,
    auth_config=create_noop_auth_config(),
)
```

See [Tool pattern](tool-pattern.md) for how to define `ToolDefinition` objects.

## Subclassing for custom behavior

Subclass `CodeExecutionServer` to add preprocessing, custom hooks, or environment setup:

```python
class ChemistryServer(CodeExecutionServer):
    """Inject common imports before every code execution."""

    def preprocess_code(self, code: str) -> str:
        return "from rdkit import Chem\nimport numpy as np\n" + code
```

## Configuration reference

`ServerConfig` has four groups of settings:

### Identity

| Field | Description |
|-------|-------------|
| `name` | Server/environment name (becomes part of the MCP tool name) |
| `description` | Capabilities description (shown to the agent in the tool description) |
| `server_description` | Optional server-level description (FastMCP `instructions`) |
| `entra_client_id` | Entra ID app registration client ID |
| `entra_tenant_id` | Azure AD tenant ID |

### Environment

| Field | Description |
|-------|-------------|
| `type` | `"uv"`, `"conda"`, or `"pip"` |
| `dependency_file` | Content of the dependency file |
| `auto_build` | Build environment on startup if missing (default: `True`) |
| `build_dir` | Custom build directory (default: `~/.cache/mcp-envs/{name}`) |
| `additional_commands` | Extra shell commands after env setup |

### Execution

| Field | Description |
|-------|-------------|
| `max_timeout` | Maximum allowed timeout per execution (default: 600s) |
| `default_timeout` | Default timeout (default: 300s) |
| `output_truncation_threshold` | Max chars in stdout/stderr before truncation |
| `parallel_max_concurrency` | Max parallel executions (0 = unlimited) |

### Features

| Field | Description |
|-------|-------------|
| `tool_search_backend` | `"bm25"` (default) or `"azure_ai_search"` |

## Authentication

Pass an `AuthConfig` to control how requests are authenticated:

```python
from agora_workbench.code_execution.auth import create_noop_auth_config
from agora_workbench.code_execution.auth.entra import create_entra_auth_config

# For local development (no auth):
server = CodeExecutionServer(server_config=config, auth_config=create_noop_auth_config())

# For production with Entra ID:
server = CodeExecutionServer(server_config=config, auth_config=create_entra_auth_config())
```

See [Authentication options](authentication.md) for the full auth guide.

## Publishers (artifact output)

Configure publishers to allow the agent to publish artifacts (files, plots) from code execution:

```python
from agora_workbench.code_execution.auth import create_noop_auth_config
from agora_workbench.code_execution.data_access import LocalFilePublisher, BlobPublisher

publishers = [
    LocalFilePublisher(base_dir="/tmp/artifacts"),
    BlobPublisher(account_url="https://myaccount.blob.core.windows.net", container="outputs"),
]

server = CodeExecutionServer(
    server_config=config,
    auth_config=create_noop_auth_config(),
    publishers=publishers,
)
```

## Running the server

```python
import asyncio

# HTTP mode (standard deployment)
asyncio.run(server.run_http(host="0.0.0.0", port=8000))

# Warm up environment without serving (useful in Docker builds)
asyncio.run(server.warm())
```

The server exposes:

- `/mcp` — MCP endpoint (SSE/streamable HTTP)
- `/health` — health check
- `/.well-known/oauth-protected-resource` — RFC 9728 OAuth protected resource metadata (when auth is enabled)
