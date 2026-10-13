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
    asyncio.run(server.run_http(host="127.0.0.1", port=8000))
```

This gives you an MCP server with an `execute_myserver_code` tool that runs Python in an isolated environment with numpy and pandas available.

!!! warning "No-op authentication is local-only"
    `create_noop_auth_config()` accepts requests without validating the caller.
    Keep code-execution servers on loopback. Use production authentication for
    remote access; a non-loopback bind in no-op mode is rejected unless an
    explicit external-network-boundary acknowledgement is provided.

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

#### UV environment completion marker

A `uv` environment is ready only when both of these exist in `build_dir`:

- `bin/python`
- `.env_build_complete`

Agora Workbench writes `.env_build_complete` only after dependency installation
and every `additional_commands` entry succeeds. Its content is not an API; its
presence records that the complete configured build finished.

If `bin/python` exists without the marker, the directory is treated as a stale
or interrupted environment. With `auto_build=True`, Agora Workbench removes the
directory and rebuilds it. With `auto_build=False`, startup reports that the
environment is missing or incomplete.

For a prebuilt container or an externally prepared `build_dir`, run
`CodeExecutionServer.warm()` during the image build whenever possible. If the
environment must be assembled outside Workbench, create
`.env_build_complete` only after installing all dependencies and successfully
running the configured additional commands. Do not store unrelated persistent
files in `build_dir`, because an incomplete directory may be removed during
automatic recovery.

!!! warning "Heavy models load *per session*"
    Each `execute_{name}_code` session runs in its **own kernel process**, so
    anything a tool loads — including a multi-gigabyte model — is loaded once
    *per session*. N concurrent sessions means N copies in RAM, which readily
    exhausts a modest host. If your environment includes a large model or other
    expensive process-global state, load it once in a **[sidecar](sidecars.md)**
    and share it across sessions instead.

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

`ServerConfig` has six groups of settings:

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
| `asset_cache_dir` | Writable directory for provisioned assets and transfer staging (default: parent of `build_dir`) |
| `additional_commands` | Extra shell commands after env setup |

### Assets

| Field | Description |
|-------|-------------|
| `assets` | List of `AssetSpec` objects provisioned into the env cache at startup (see [Working with data](working-with-data.md#asset-provisioning)) |
| `auto_provision` | Fetch the declared `assets` on startup (default: `True`) |

### Sidecars

| Field | Description |
|-------|-------------|
| `sidecars` | List of `SidecarConfig` objects — long-lived helper processes launched at startup and stopped on shutdown, used to load an expensive resource (e.g. a model) once and share it across all kernel sessions over loopback HTTP (see [Sidecars](sidecars.md)) |

### Execution

| Field | Description |
|-------|-------------|
| `execution_mode` | `"sync"` (default), `"async_only"`, or `"adaptive"` — controls whether `execute_{name}_code` blocks, always backgrounds, or auto-promotes long-running calls |
| `promotion_threshold_s` | Seconds before adaptive mode promotes to background (default: 60) |
| `max_timeout` | Maximum allowed timeout per execution (default: 600s) |
| `default_timeout` | Default timeout (default: 300s) |
| `output_truncation_threshold` | Max chars in stdout/stderr before truncation |
| `parallel_max_concurrency` | Max parallel executions (0 = unlimited) |
| `kernel_network_mode` | `"inherit"` (effective default), `"isolated"` (network namespace), or `"seccomp"` (post-start syscall filter). Restricted kernels cannot open IP network connections. When omitted with a custom `SessionManager`, its `SessionConfig` setting is preserved. |

### Isolating kernels from IP networks

Workbench provides two fail-closed Linux backends. Prefer
`kernel_network_mode="isolated"` when the runtime permits unprivileged user and
network namespaces:

```python
config = ServerConfig(
    name="offline-analysis",
    description="Analyze operator-provided local data without network access.",
    type="uv",
    dependency_file="numpy\npandas\n",
    kernel_network_mode="isolated",
)
```

Workbench switches the Jupyter control channels from TCP to Unix IPC sockets, so
the server can still execute code while the kernel and its subprocesses have no
route to the server network or external IP networks. The launcher also creates a
subordinate user namespace, maps the server identity to root only inside that
namespace while configuring loopback, then drops the capability bounding set
and enables `no_new_privs` before executing the kernel. Kernel code does not
receive namespace capabilities in the server's user namespace.

This option is Linux-only and requires the `unshare` and `setpriv` commands from
util-linux, the `ip` command from iproute2, and runtime permission to create
unprivileged user and network namespaces. Workbench fails the kernel launch
rather than falling back to the inherited network when the boundary is
unavailable.

Managed container runtimes sometimes prohibit namespace creation. On a 64-bit
Linux image with `libseccomp.so.2`, use `kernel_network_mode="seccomp"` instead:

```python
config = ServerConfig(
    name="offline-analysis",
    description="Analyze operator-provided local data without network access.",
    type="uv",
    dependency_file="numpy\npandas\n",
    kernel_network_mode="seccomp",
)
```

This backend starts Jupyter over Unix IPC first because ZeroMQ creates temporary
IP sockets during initialization. Workbench starts the kernel from a fresh
private directory with user-site imports disabled and fresh IPython/Jupyter
configuration directories. It replaces mutable kernelspec arguments and
environment entries with an isolated Python startup that disables automatic
site processing, IPython/Jupyter configuration loading, and profile startup
files before initialization. Only validated package roots are added before
loading the kernel application. After Jupyter is ready, Workbench installs an
irreversible libseccomp filter before registering the kernel for execution. The
filter is synchronized across existing kernel threads, inherited by
subprocesses, rejects all new non-Unix sockets plus new `connect()`, `bind()`,
and `listen()` calls, blocks io_uring and cross-process memory syscalls that
could provide alternative bypasses, and verifies that no non-Unix socket
descriptors remain open. Workbench also disables process dumpability for the
server and seccomp kernels, preventing same-UID `ptrace` and `/proc/<pid>/mem`
injection independently of host Yama settings. This disables core dumps for the
Workbench server. The kernel is shut down if any installation or verification
step fails.

Install the libseccomp runtime library in the container image, for example
`libseccomp2` on Debian/Ubuntu. Unlike namespace isolation, seccomp isolation
also blocks private TCP loopback created by the kernel. Existing applications
that use `127.0.0.1` for local worker coordination should continue using the
namespace backend.

The Python environment used by a seccomp kernel must be immutable to executed
code. Install the interpreter and site-packages under a parent hierarchy owned
by a different user, then run Workbench as a non-root user without write access
to any path in that hierarchy. Workbench recursively validates standard-library
and package roots, including symlink targets, before starting the kernel. For
images that build the kernel environment
with `--warm`, configure its build directory under a location such as
`/opt/kernel-envs`, recursively change that hierarchy to `root:root` ownership
in a later root build step, and switch back to the runtime user. Changing only
an environment below a writable home or cache directory is insufficient because
the runtime user could replace the entire environment. Workbench validates the
interpreter, virtual-environment configuration, standard-library and
site-packages paths, resolved symlink targets, and every controlling parent
directory. Startup fails if any path is owned or writable by the kernel
identity.

Restricted kernels cannot use:

- public or private HTTP services;
- host TCP loopback services, including Workbench `SidecarConfig` sidecars;
- server-to-server transfers initiated inside the kernel;
- domain tools that open IP sockets.

Private loopback remains available only with the namespace backend, so a kernel
and its own subprocesses can communicate over `127.0.0.1` without reaching
services in the Workbench server namespace. The seccomp backend permits
Unix-domain socket creation and socket pairs, but rejects new stream
connections/listeners as well as new IPv4 and IPv6 sockets, including loopback.
Unix datagrams can still send to an existing filesystem endpoint, so do not
expose trusted Unix sockets inside writable or kernel-accessible paths.

Seccomp startup does not process `.pth` files because they can redirect imports
to paths outside the validated immutable environment. Editable installs and
legacy namespace packages that depend on `.pth` processing must be converted to
regular installed packages. Jupyter debugging, `multiprocessing.Manager`,
`forkserver`, and non-native-architecture helper binaries are also incompatible
with this mode; use the namespace backend when those features are required.

Environment construction, asset provisioning, catalog materialization, and
artifact publishing run in the server process and are not affected. Files
materialized before execution remain available to the kernel.

!!! warning "Network boundary, not a complete process sandbox"
    These settings isolate IP networking only. They do not assign a different
    operating-system user to the kernel. Namespace isolation drops supplementary
    group memberships, so paths accessible only through a supplementary group
    may become unreadable. Both backends enable `no_new_privs`, which prevents
    setuid helpers from gaining privileges.

    All kernels still run with the same host UID by default. Network isolation
    therefore does not prevent one kernel from accessing another kernel's files
    or Unix IPC endpoints when host filesystem permissions allow it. Do not
    expose host-control or network-proxy Unix sockets (for example, a container
    runtime socket) to kernel code. Hostile multi-user workloads additionally
    require per-kernel OS identities or separate worker isolation.

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
from agora_workbench.data_lake.execution import BlobPublisher, LocalFilePublisher

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
asyncio.run(server.run_http(host="127.0.0.1", port=8000))

# Warm up environment without serving (useful in Docker builds)
asyncio.run(server.warm())
```

The server exposes:

- `/mcp` — MCP endpoint (SSE/streamable HTTP)
- `/health` — health check
- `/.well-known/oauth-protected-resource` — RFC 9728 OAuth protected resource metadata (when auth is enabled)
