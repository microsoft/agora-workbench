"""
Data models for code execution configuration and results.

This module contains Pydantic models used by the code execution server:
- ToolCallRecord: Structured record of a tool call
- CodeExecutionResult: Output from code execution
- ServerConfig: Configuration for a CodeExecutionServer instance
"""

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field


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


class ServerConfig(BaseModel):
    """Configuration for a CodeExecutionServer instance.

    Fields are organized into logical groups:

    **Identity** — server and tool naming/description:
        name, description, server_description

    **Environment** — Python environment build settings:
        type, dependency_file, auto_build, build_dir, additional_commands

    **Assets** — large artifact provisioning:
        assets, auto_provision

    **Execution** — code execution policy:
        max_timeout, default_timeout, output_truncation_threshold, parallel_max_concurrency

    **Features** — optional server capabilities:
        domains_dir, tool_search_backend
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

    max_timeout: int = Field(
        default=600,
        description="Maximum allowed execution timeout in seconds for any single code run.",
    )
    default_timeout: int = Field(
        default=300,
        description="Default execution timeout in seconds when not specified by the caller.",
    )
    output_truncation_threshold: int = Field(
        default=50_000,
        description=(
            "Maximum characters allowed in stdout/stderr before truncation. "
            "Large outputs are trimmed and a guidance message is appended. "
            "Set to 0 to disable truncation. Can be overridden via the "
            "CODE_OUTPUT_TRUNCATION_THRESHOLD environment variable."
        ),
    )
    parallel_max_concurrency: int = Field(
        default=0,
        description=(
            "Maximum number of parallel code executions allowed. "
            "0 means unlimited. Can be overridden via the "
            "PARALLEL_EXECUTE_MAX_CONCURRENCY environment variable."
        ),
    )

    # --- Features ---

    domains_dir: Optional[Path] = Field(
        default=None,
        description=(
            "Path to the domains/ directory containing domain state definitions and skills. "
            "Used by workflow planning and skill loading tools. "
            "When None, these features are disabled unless tools carry state annotations directly."
        ),
    )
    tool_search_backend: Literal["bm25", "azure_ai_search"] = Field(
        default="bm25",
        description=(
            "Tool search backend for the server-side search_tools MCP tool. "
            "Supported values: 'bm25' (default) and 'azure_ai_search'."
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
