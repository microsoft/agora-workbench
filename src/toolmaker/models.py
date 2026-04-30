"""
Data models for the ToolMaker agent.

These models track the state of tool creation from a GitHub repository:
  - TaskSpec: user-provided task specification (repo + desired tool interface)
  - GeneratedFile: a single file in the generated domain folder
  - ImplementationState: tracks the full lifecycle of code generation, building, and testing
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Task specification (built conversationally in Stage 1) ───────────────


class ArgumentSpec(BaseModel):
    """Specification for a single function argument."""

    name: str = Field(description="Parameter name")
    type: str = Field(description="Python type annotation (e.g. 'str', 'int', 'list[float]')")
    description: str = Field(description="What this argument represents")
    default: Optional[str] = Field(default=None, description="Default value as a Python literal, or None if required")

    def __repr__(self) -> str:
        default_str = f" = {self.default}" if self.default is not None else ""
        return f"{self.name}: {self.type}{default_str}  # {self.description}"


class ReturnFieldSpec(BaseModel):
    """Specification for a single return field (tools return dicts)."""

    name: str = Field(description="Key name in the returned dict")
    type: str = Field(description="Python type of the value")
    description: str = Field(description="What this return field represents")


class ExampleInvocation(BaseModel):
    """An example call to the tool for testing."""

    arguments: dict[str, str] = Field(description="Mapping of argument names to example values (as Python literals)")
    expected_output: Optional[dict] = Field(
        default=None, description="Expected output dict to validate against (e.g. {'result': '1.2 million'})"
    )
    expected_description: str = Field(
        default="", description="Natural-language description of expected output (used if expected_output is None)"
    )


class TaskSpec(BaseModel):
    """
    Complete specification of a tool to create from a GitHub repository.

    Built up incrementally during Stage 1 (Exploration) via conversation with the user.
    """

    # Repository info
    repo_url: str = Field(default="", description="GitHub repository URL")
    repo_branch: str = Field(default="main", description="Branch or commit to use")
    repo_name: str = Field(default="", description="Short repository name (e.g. 'STAMP')")

    # Task description
    task_description: str = Field(default="", description="Natural language description of the tool's purpose")

    # Function interface
    tool_name: str = Field(default="", description="Name of the tool function (e.g. 'extract_features')")
    domain_name: str = Field(default="", description="Domain name for the MCP server (e.g. 'stamp')")
    arguments: list[ArgumentSpec] = Field(default_factory=list, description="Function arguments")
    returns: list[ReturnFieldSpec] = Field(default_factory=list, description="Return value fields")

    # Examples
    examples: list[ExampleInvocation] = Field(default_factory=list, description="Example invocations for testing")

    # Exploration context
    repo_summary: str = Field(default="", description="Summary of repo exploration findings")
    implementation_plan: str = Field(default="", description="High-level plan for implementing the tool")
    install_commands: str = Field(default="", description="Commands needed to install the repo and its dependencies")

    @property
    def is_complete(self) -> bool:
        """Whether the spec has enough info to proceed to implementation."""
        return bool(
            self.repo_url
            and self.task_description
            and self.tool_name
            and self.domain_name
            and self.arguments
            and self.returns
        )

    @property
    def missing_fields(self) -> list[str]:
        """List of fields that still need to be filled in."""
        missing = []
        if not self.repo_url:
            missing.append("repo_url")
        if not self.task_description:
            missing.append("task_description")
        if not self.tool_name:
            missing.append("tool_name")
        if not self.domain_name:
            missing.append("domain_name")
        if not self.arguments:
            missing.append("arguments")
        if not self.returns:
            missing.append("returns")
        return missing

    def python_signature(self) -> str:
        """Generate the Python function signature string."""
        params = []
        for arg in self.arguments:
            p = f"{arg.name}: {arg.type}"
            if arg.default is not None:
                p += f" = {arg.default}"
            params.append(p)
        return f"def {self.tool_name}({', '.join(params)}) -> dict:"

    def view(self) -> str:
        """Return a human-readable view of the task specification."""
        lines = ["═══ Task Specification ═══"]
        lines.append(f"  Repository:  {self.repo_url or '(not set)'}")
        if self.repo_branch != "main":
            lines.append(f"  Branch:      {self.repo_branch}")
        lines.append(f"  Repo name:   {self.repo_name or '(not set)'}")
        lines.append(f"  Domain name: {self.domain_name or '(not set)'}")
        lines.append(f"  Tool name:   {self.tool_name or '(not set)'}")
        lines.append(f"  Description: {self.task_description or '(not set)'}")
        lines.append("")

        if self.arguments:
            lines.append("  Arguments:")
            for arg in self.arguments:
                lines.append(f"    - {arg!r}")
        else:
            lines.append("  Arguments:   (none defined)")

        if self.returns:
            lines.append("  Returns:")
            for ret in self.returns:
                lines.append(f"    - {ret.name}: {ret.type}  # {ret.description}")
        else:
            lines.append("  Returns:     (none defined)")

        if self.examples:
            lines.append("")
            lines.append("  Examples:")
            for i, ex in enumerate(self.examples, 1):
                lines.append(f"    Example {i}: {ex.arguments}")
                if ex.expected_output:
                    lines.append(f"      Expected output: {ex.expected_output}")
                elif ex.expected_description:
                    lines.append(f"      Expected (desc): {ex.expected_description}")

        if self.implementation_plan:
            lines.append("")
            lines.append("  Implementation plan:")
            for plan_line in self.implementation_plan.split("\n"):
                lines.append(f"    {plan_line}")

        completeness = "COMPLETE" if self.is_complete else f"INCOMPLETE (missing: {', '.join(self.missing_fields)})"
        lines.append(f"\n  Status: {completeness}")
        return "\n".join(lines)


# ── Implementation state (tracks Stage 2 lifecycle) ──────────────────────


class BuildStatus(str, Enum):
    """Status of the Docker build / test cycle."""

    NOT_STARTED = "not_started"
    BUILDING = "building"
    BUILD_FAILED = "build_failed"
    TESTING = "testing"
    TEST_FAILED = "test_failed"
    PASSED = "passed"


class ToolPersistence(str, Enum):
    """How long the created tool should persist."""

    UNDECIDED = "undecided"
    SESSION_ONLY = "session_only"
    REUSABLE = "reusable"


class GeneratedFile(BaseModel):
    """A file generated by the agent for the domain folder."""

    relative_path: str = Field(description="Path relative to domains/{domain_name}/server/")
    content: str = Field(description="File content")


class TestResult(BaseModel):
    """Result of testing a tool via the MCP server."""

    tool_name: str
    arguments: dict
    success: bool
    output: Optional[str] = None
    expected_output: Optional[dict] = None
    validation_passed: Optional[bool] = None  # None = no expected value to compare
    error: Optional[str] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None


class ImplementationState(BaseModel):
    """
    Tracks the full state of code generation, building, and testing.

    Updated throughout Stage 2 as the agent iterates on the implementation.
    """

    iteration: int = Field(default=0, description="Current iteration number")
    max_iterations: int = Field(default=30, description="Maximum iterations before giving up")
    build_status: BuildStatus = Field(default=BuildStatus.NOT_STARTED)
    persistence: ToolPersistence = Field(default=ToolPersistence.UNDECIDED, description="User's persistence choice")
    generated_files: list[GeneratedFile] = Field(default_factory=list)
    test_results: list[TestResult] = Field(default_factory=list)
    problem_summaries: list[str] = Field(default_factory=list, description="Summaries of past failures for context")
    container_id: Optional[str] = Field(default=None, description="Running Docker container ID")
    image_tag: Optional[str] = Field(default=None, description="Built Docker image tag")
    server_port: int = Field(default=8010, description="Port the MCP server is running on")

    @property
    def tests_passed(self) -> bool:
        """Whether the implementation passed all tests.

        Uses ``build_status`` as the authoritative signal set by
        ``test_domain_tool``.  The status is set to PASSED only when the
        *last* test execution succeeded.  Earlier failures from the
        build-test-edit loop are expected and do not count against the
        final result — the test_results list is append-only and may
        contain superseded failures from earlier iterations.
        """
        return self.build_status == BuildStatus.PASSED and bool(self.test_results)

    @property
    def image_built(self) -> bool:
        """Whether the Docker image was successfully built."""
        return self.image_tag is not None

    @property
    def server_url(self) -> Optional[str]:
        """URL of the running MCP server, or None if no container is running."""
        if self.container_id is not None:
            return f"http://localhost:{self.server_port}/mcp"
        return None

    @property
    def build_error(self) -> Optional[str]:
        """Most recent build or test error message."""
        if self.test_results:
            last = self.test_results[-1]
            if last.error:
                return last.error
        if self.problem_summaries:
            return self.problem_summaries[-1]
        return None

    def view(self) -> str:
        """Return a human-readable view of the implementation state."""
        lines = ["═══ Implementation State ═══"]
        lines.append(f"  Iteration:    {self.iteration}/{self.max_iterations}")
        lines.append(f"  Build status: {self.build_status.value}")
        lines.append(f"  Files generated: {len(self.generated_files)}")
        for f in self.generated_files:
            lines.append(f"    - {f.relative_path}")
        if self.test_results:
            lines.append("  Test results:")
            for i, result in enumerate(self.test_results, 1):
                # Determine status based on success AND validation
                if not result.success:
                    status = "FAIL ✗"
                elif result.validation_passed is False:
                    status = "MISMATCH ⚠"
                elif result.validation_passed is True:
                    status = "PASS ✓ (validated)"
                else:
                    status = "PASS ✓ (no expected value)"
                lines.append(f"    [{i}] {result.tool_name}: {status}")
                lines.append(f"        Args: {result.arguments}")
                if result.output:
                    # Truncate long outputs
                    output_preview = result.output[:300] + "..." if len(result.output) > 300 else result.output
                    lines.append(f"        Output: {output_preview}")
                if result.expected_output:
                    lines.append(f"        Expected: {result.expected_output}")
                if result.error:
                    lines.append(f"        Error: {result.error[:200]}")
        if self.problem_summaries:
            lines.append(f"  Past problems: {len(self.problem_summaries)}")
        return "\n".join(lines)
