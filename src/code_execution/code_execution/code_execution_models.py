"""
Data models for code execution configuration and results.

This module contains Pydantic models used by the code execution server:
- ToolCallRecord: Structured record of a tool call
- CodeExecutionResult: Output from code execution
- EnvironmentConfig: Configuration for Python environments
"""

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field


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

    stdout: str = Field(default="", description="Standard output from code execution")
    stderr: str = Field(default="", description="Standard error from code execution")
    execution_time: float = Field(default=0.0, ge=0, description="Execution time in seconds")
    success: bool = Field(default=True, description="Whether execution completed successfully")
    error: Optional[str] = Field(default=None, description="Error message if execution failed")
    tool_calls: list[ToolCallRecord] = Field(
        default_factory=list,
        description="Structured tool-call records captured during execution",
    )


class EnvironmentConfig(BaseModel):
    """
    Configuration for building/locating a Python execution environment.

    Supports multiple dependency management systems:
    - uv: Fast Python package installer (requirements.txt)
    - conda: Anaconda/Miniconda (environment.yml)
    - pip: Standard Python (requirements.txt)
    """

    name: str = Field(description="Environment name (e.g., 'powergrid', 'chemistry')")
    description: str = Field(
        description="Description of the environment's capabilities and packages (appears in MCP tool description)"
    )
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

    def get_build_dir(self) -> Path:
        """Get the directory where environment will be built."""
        if self.build_dir:
            return self.build_dir
        return Path.home() / ".cache" / "mcp-envs" / self.name / self.type

    def get_python_path(self) -> Path:
        """Get the path to the Python executable."""
        build_dir = self.get_build_dir()

        if self.type in ["conda", "uv", "pip"]:
            return build_dir / "bin" / "python"
        else:
            raise ValueError(f"Unknown environment type: {self.type}")
