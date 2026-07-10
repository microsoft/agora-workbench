"""
Data models for code execution configuration and results.

This module contains Pydantic models used by the code execution server:
- ToolCallRecord: Structured record of a tool call
- CodeExecutionResult: Output from code execution
- ServerConfig: Configuration for a CodeExecutionServer instance
"""

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class AssetSpec(BaseModel):
    """Specification for a large artifact (model weights, data files) to provision.

    Assets are fetched into the environment cache directory at server startup
    (when ``auto_provision=True`` on the parent ``ServerConfig``) and
    skipped if already present with a matching checksum.

    Supported source URI schemes:
    - ``https://`` — streaming HTTP download with retry
    - ``abfss://`` or ``https://*.blob.core.windows.net/`` — Azure Blob Storage
    - ``file:///path`` or bare path — local copy (useful with Docker bind mounts)

    Accessing assets from tool implementations:
        The ``MCP_ASSET_CACHE_DIR`` environment variable is set on every kernel
        process, pointing to the cache directory.  Tool code can locate assets via::

            import os
            from pathlib import Path

            cache = Path(os.environ["MCP_ASSET_CACHE_DIR"])
            weights = cache / "models/weights.safetensors"
    """

    name: str = Field(description="Logical name for the asset (e.g., 'diffusion-weights')")
    source: str = Field(
        description=(
            "URI to fetch from. Supported schemes: https://, az://<container>/<blob>, "
            "file:///local/path, or a bare filesystem path."
        )
    )
    destination: str = Field(
        description=(
            "Relative path under the environment cache directory where the asset "
            "should be placed (e.g., 'models/weights.safetensors')"
        )
    )
    size_hint_mb: Optional[int] = Field(
        default=None,
        description="Expected size in MB. Used for timeout calculation and progress reporting.",
    )
    checksum: Optional[str] = Field(
        default=None,
        description="SHA-256 hex digest. When provided, skip download if destination matches.",
    )


class SidecarConfig(BaseModel):
    """Specification for a long-lived helper process co-located with the server.

    A *sidecar* is a background process the ``CodeExecutionServer`` launches at
    startup and shuts down on exit. It exists to host expensive, process-global
    state — most commonly a heavy model that would otherwise be reloaded (and
    its memory multiplied) inside every isolated kernel session. The model is
    loaded **once** in the sidecar; kernel-side tool code reaches it over
    loopback HTTP, so each ``execute_{name}_code`` session stays cheap.

    By default the sidecar runs with the server's own **kernel environment**
    Python interpreter (``ServerConfig.get_python_path()``), so it can import
    the same heavy dependencies the tools use without a second environment. Set
    ``use_env_python=False`` to launch an arbitrary executable instead (e.g. a
    sidecar shipped as a standalone binary or served from another interpreter).

    Discovery contract:
        The resolved base URL (``http://{host}:{port}``) is exported into the
        environment under ``url_env_var`` on the server process *before* any
        kernel is spawned, so every kernel inherits it. Tool code reads it::

            import os, httpx
            base = os.environ["MYMODEL_SERVICE_URL"]
            resp = httpx.post(f"{base}/predict", json={...}, timeout=600)

        The sidecar process is told where to bind via the ``SIDECAR_HOST`` and
        ``SIDECAR_PORT`` environment variables (in addition to any ``env``
        overrides), so a single entrypoint can honor the configured address.
    """

    name: str = Field(description="Logical name for the sidecar (e.g., 'retrochimera-model').")
    command: list[str] = Field(
        description=(
            "Argument vector for the sidecar. When ``use_env_python`` is True (default) "
            "these args are appended to the kernel environment's Python interpreter, so "
            "``['-m', 'mypkg.model_service']`` runs that module in the tools' environment. "
            "When False, ``command`` is executed verbatim as its own program."
        )
    )
    use_env_python: bool = Field(
        default=True,
        description=(
            "Prepend the server's kernel-environment Python (ServerConfig.get_python_path()) "
            "to ``command``. Disable to run an arbitrary executable that manages its own runtime."
        ),
    )
    url_env_var: str = Field(
        description=(
            "Environment variable name under which the sidecar's base URL is exported to "
            "the server process (and therefore inherited by every kernel). Tool code reads "
            "this to locate the sidecar."
        )
    )
    host: str = Field(
        default="127.0.0.1",
        description="Loopback bind address for the sidecar. Kept on 127.0.0.1 by default; the sidecar is an internal implementation detail and must not be exposed off-box.",
    )
    port: int = Field(
        gt=0,
        lt=65536,
        description="TCP port the sidecar listens on (passed to it via SIDECAR_PORT and used to build url_env_var).",
    )
    health_path: str = Field(
        default="/health",
        description="Path polled (HTTP GET, expecting 2xx) to determine readiness after launch.",
    )
    readiness_timeout_s: float = Field(
        default=120.0,
        gt=0,
        description="Maximum seconds to wait for the sidecar's health endpoint to return success before failing startup.",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Extra environment variables set on the sidecar process (merged over the inherited environment).",
    )

    @field_validator("host")
    @classmethod
    def _validate_loopback_host(cls, value: str) -> str:
        """Reject non-loopback bind addresses.

        A sidecar is an internal implementation detail reached only by co-located
        kernels; binding it to a routable address (e.g. ``0.0.0.0``) would expose
        it off-box. Only loopback addresses and ``localhost`` are permitted. For a
        genuinely remote/shared service, don't use a sidecar — point tool code at
        the remote URL directly (see the sidecars guide).
        """
        import ipaddress

        if value == "localhost":
            return value
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError(
                f"Sidecar host must be a loopback IP address or 'localhost', got {value!r}."
            ) from exc
        if not address.is_loopback:
            raise ValueError(
                f"Sidecar host must be loopback (got {value!r}); sidecars are internal and "
                f"must not be exposed off-box. Use a separate service for remote access."
            )
        return value

    def base_url(self) -> str:
        """The base URL kernels use to reach the sidecar."""
        return f"http://{self.host}:{self.port}"

    def health_url(self) -> str:
        """The URL polled to determine readiness."""
        return f"{self.base_url()}{self.health_path if self.health_path.startswith('/') else '/' + self.health_path}"


class ToolCallRecord(BaseModel):
    """Structured record of a tool call made during code execution.

    Captured by instrumented proxy wrappers injected into the kernel.
    Each record represents one tool invocation with its arguments,
    result, timing, and success/failure status.
    """

    tool_name: str = Field(description="Name of the tool that was called")
    args: dict = Field(default_factory=dict, description="Arguments passed to the tool (JSON-safe snapshot)")
    result: dict = Field(default_factory=dict, description="Return value from the tool (JSON-safe snapshot)")
    duration_ms: float = Field(default=0.0, ge=0, description="Execution time in milliseconds")
    success: bool = Field(default=True, description="Whether the tool call succeeded")
    error: Optional[str] = Field(default=None, description="Error message if tool call failed")
    timestamp: float = Field(default=0.0, description="Unix timestamp when the call was made")


class CodeExecutionResult(BaseModel):
    """Result of code execution with stdout, stderr, and metadata."""

    description: str = Field(
        default="",
        description="One-line agent-supplied summary of what this code does (surfaced in the activity UI)",
    )
    stdout: str = Field(default="", description="Standard output from code execution")
    stderr: str = Field(default="", description="Standard error from code execution")
    execution_time: float = Field(default=0.0, ge=0, description="Execution time in seconds")
    success: bool = Field(default=True, description="Whether execution completed successfully")
    error: Optional[str] = Field(default=None, description="Error message if execution failed")
    tool_calls: list[ToolCallRecord] = Field(
        default_factory=list,
        description="Structured tool-call records captured during execution",
    )
    displays: list[dict] = Field(
        default_factory=list,
        description=(
            "Rich kernel outputs (matplotlib figures, images, SVGs, HTML) captured "
            "from Jupyter display_data and execute_result messages. Each entry has "
            "the shape ``{'mime_type': str, 'data': str, 'metadata': dict}``. "
            "Streamed to the activity UI for rendering; excluded from the JSON "
            "returned to the agent to keep its context window small."
        ),
    )
    artifacts: list[dict] = Field(
        default_factory=list,
        description=(
            "Files newly created or modified under the session's outputs directory "
            "during this execute. Each entry is a metadata dict — never the bytes — "
            "of shape {name, size_bytes, mime_type, modified_at, download_token}. "
            "Lightweight artifact names are returned to the agent so it can "
            "publish them via <gui>name</gui>; download URLs are not composed "
            "until the agent explicitly publishes."
        ),
    )


class ServerConfig(BaseModel):
    """Configuration for a CodeExecutionServer instance.

    Fields are organized into logical groups:

    **Identity** — server and tool naming/description:
        name, description, server_description, entra_client_id, entra_tenant_id

    **Environment** — Python environment build settings:
        type, dependency_file, auto_build, build_dir, additional_commands

    **Assets** — large artifact provisioning:
        assets, auto_provision

    **Execution** — code execution policy:
        max_timeout, default_timeout, output_truncation_threshold, parallel_max_concurrency

    **Features** — optional server capabilities:
        tool_search_backend, peer_registry

    For fields that also have an environment variable counterpart (e.g.,
    output_truncation_threshold / CODE_OUTPUT_TRUNCATION_THRESHOLD), the
    ServerConfig value takes precedence when set. The env var serves as
    a deployment-wide default.
    """

    # --- Identity ---

    name: str = Field(description="Server/environment name (e.g., 'powergrid', 'chemistry')")
    description: str = Field(
        description="Description of the environment's capabilities and packages (appears in MCP tool description)"
    )
    server_description: Optional[str] = Field(
        default=None,
        description=(
            "Server-level description (used as FastMCP ``instructions``). Falls back to "
            "``description`` when unset. Override when the server-as-a-whole pitch should "
            "differ from the per-tool ``execute_{name}_code`` description."
        ),
    )
    entra_client_id: Optional[str] = Field(
        default=None,
        description=(
            "Entra ID application (client) ID for this server's app registration. "
            "When set, overrides the ENTRA_CLIENT_ID environment variable. Use this "
            "when deploying multiple servers with distinct app registrations."
        ),
    )
    entra_tenant_id: Optional[str] = Field(
        default=None,
        description=(
            "Azure AD tenant ID for this server's app registration. "
            "When set, overrides the ENTRA_TENANT_ID environment variable. Use this "
            "when deploying multiple servers with distinct app registrations."
        ),
    )

    # --- Environment ---

    type: Literal["uv", "conda", "pip"] = Field(description="Type of environment/dependency manager")
    dependency_file: str = Field(
        description="Serialized content of dependency file (environment.yml or requirements.txt)"
    )
    auto_build: bool = Field(default=True, description="Automatically build environment if it doesn't exist")
    build_dir: Optional[Path] = Field(
        default=None, description="Directory where environment should be created (default: ~/.cache/mcp-envs/{name})"
    )
    additional_commands: list[str] = Field(
        default_factory=list,
        description="Additional shell commands to run after environment setup (e.g., 'pip install package', 'conda install -y tool')",
    )
    sidecars: list[SidecarConfig] = Field(
        default_factory=list,
        description=(
            "Long-lived helper processes launched at server startup and stopped on shutdown. "
            "Use a sidecar to load an expensive, process-global resource (e.g. a large model) "
            "exactly once and share it across all kernel sessions over loopback HTTP, instead "
            "of paying its memory cost inside every isolated kernel. Each sidecar's base URL is "
            "exported to kernels via its ``url_env_var``. Not started during ``warm`` (build-time)."
        ),
    )

    # --- Assets ---

    assets: list[AssetSpec] = Field(
        default_factory=list,
        description=(
            "Large artifacts (model weights, data files) to provision into the cache "
            "directory before first tool execution. Fetched at server startup when "
            "auto_provision is True."
        ),
    )
    auto_provision: bool = Field(
        default=True,
        description=(
            "Automatically fetch assets at server startup if they are not already cached. "
            "Set to False when assets are pre-provisioned (e.g., baked into Docker image "
            "or available on a mounted volume)."
        ),
    )

    # --- Execution ---

    execution_mode: Literal["sync", "async_only", "adaptive"] = Field(
        default="sync",
        description=(
            "Controls how the ``execute_{name}_code`` tool returns results.\n\n"
            "- ``sync`` (default): blocks until completion or timeout, returning "
            "the full result inline.\n"
            "- ``async_only``: every invocation is submitted as a background job "
            "and the tool returns a job handle immediately.  The agent must poll "
            "with ``{name}_check_job`` to retrieve results.\n"
            "- ``adaptive``: starts executing synchronously; if execution is still "
            "running after ``promotion_threshold_s`` seconds, it is automatically "
            "promoted to a background job and a job handle is returned."
        ),
    )
    promotion_threshold_s: float = Field(
        default=30.0,
        gt=0,
        description=(
            "Seconds to wait before promoting a synchronous execution to a "
            "background job.  Only used when ``execution_mode='adaptive'``."
        ),
    )
    max_timeout: int = Field(
        default=600,
        description="Maximum allowed execution timeout in seconds for any single code run.",
    )
    default_timeout: int = Field(
        default=300,
        description="Default execution timeout in seconds when not specified by the caller.",
    )
    output_truncation_threshold: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Maximum characters allowed in stdout/stderr before truncation. "
            "Large outputs are trimmed and a guidance message is appended. "
            "Set to 0 to disable truncation. When None, falls back to the "
            "CODE_OUTPUT_TRUNCATION_THRESHOLD env var (default: 50000)."
        ),
    )
    parallel_max_concurrency: Optional[int] = Field(
        default=None,
        description=(
            "Maximum number of parallel code executions allowed. "
            "0 means unlimited. When None, falls back to the "
            "PARALLEL_EXECUTE_MAX_CONCURRENCY env var (default: 0)."
        ),
    )

    # --- Features ---

    tool_search_backend: Literal["bm25", "azure_ai_search"] = Field(
        default="bm25",
        description=(
            "Tool search backend for the server-side search_tools MCP tool. "
            "Supported values: 'bm25' (default) and 'azure_ai_search'."
        ),
    )
    peer_registry: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Logical name → base URL map of peer CodeExecutionServers this server may push "
            "objects to via ``{name}_send(to=<peer>)``. Lets the send tool resolve peer "
            "destinations dynamically — operators maintain one shared registry instead of "
            "pre-registering a ServerPublisher per peer (O(N) config, not O(N²)). Merged with "
            "the ``AGORA_PEER_REGISTRY`` env var, which is either inline JSON or a path to a "
            "JSON file and takes precedence. The server's own ``name`` is ignored if present. "
            "Only names in this allow-list are reachable — the agent cannot send to arbitrary URLs. "
            "A plain-HTTP registry peer is trusted as configured (the operator chose the scheme "
            "in the URL), so it need not also appear in ``OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS``."
        ),
    )

    def get_build_dir(self) -> Path:
        """Get the directory where environment will be built."""
        if self.build_dir:
            return self.build_dir
        return Path.home() / ".cache" / "mcp-envs" / self.name / self.type

    def get_cache_dir(self) -> Path:
        """Get the root cache directory for this environment (parent of build_dir).

        Asset destinations are resolved relative to this directory.
        """
        if self.build_dir:
            return self.build_dir.parent
        return Path.home() / ".cache" / "mcp-envs" / self.name

    def get_python_path(self) -> Path:
        """Get the path to the Python executable."""
        build_dir = self.get_build_dir()

        if self.type in ["conda", "uv", "pip"]:
            return build_dir / "bin" / "python"
        else:
            raise ValueError(f"Unknown environment type: {self.type}")
