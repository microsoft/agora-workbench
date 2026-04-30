"""
FunctionTools for code generation and task specification management.

These tools allow the ToolMaker agent to:
  - Write/read/delete files in domain server directories
  - Read example domain implementations for reference
  - Build up a TaskSpec incrementally via conversation
"""

import json
import logging
from pathlib import Path
from typing import Optional

from agent_framework import FunctionTool
from pydantic import BaseModel, Field

from ..models import (
    TaskSpec,
    ArgumentSpec,
    ReturnFieldSpec,
    ExampleInvocation,
)

LOGGER = logging.getLogger(__name__)

# AgoraAgentMAF root (for domain file operations)
_AGORA_MAF_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def _safe_domain_path(domain_name: str, relative_path: str) -> Path:
    """
    Resolve a path safely within a domain's server directory.

    Prevents path traversal attacks by ensuring the resolved path
    stays within the domain directory.
    """
    domain_dir = (_AGORA_MAF_ROOT / "domains" / domain_name / "server").resolve()
    target = (domain_dir / relative_path).resolve()
    try:
        target.relative_to(domain_dir)
    except ValueError:
        raise ValueError(f"Path traversal detected: {relative_path}")
    return target


# ── Pydantic input models ────────────────────────────────────────────────


class WriteDomainFileInput(BaseModel):
    domain_name: str = Field(description="Domain name (e.g. 'stamp')")
    relative_path: str = Field(description="File path relative to domains/{domain}/server/")
    content: str = Field(description="File content to write")


class ReadDomainFileInput(BaseModel):
    domain_name: str = Field(description="Domain name (e.g. 'stamp')")
    relative_path: str = Field(description="File path relative to domains/{domain}/server/")


class ListDomainFilesInput(BaseModel):
    domain_name: str = Field(description="Domain name (e.g. 'stamp')")


class DeleteDomainFileInput(BaseModel):
    domain_name: str = Field(description="Domain name (e.g. 'stamp')")
    relative_path: str = Field(description="File path relative to domains/{domain}/server/")


class ReadExampleDomainInput(BaseModel):
    file_path: str = Field(description="File path relative to domains/example/server/")


class ViewTaskSpecInput(BaseModel):
    """No parameters needed."""

    pass


class UpdateTaskSpecInput(BaseModel):
    repo_url: Optional[str] = Field(default=None, description="GitHub repository URL")
    repo_branch: Optional[str] = Field(default=None, description="Branch or commit to use")
    repo_name: Optional[str] = Field(default=None, description="Short repository name")
    task_description: Optional[str] = Field(default=None, description="Description of the tool's purpose")
    tool_name: Optional[str] = Field(default=None, description="Name of the tool function")
    domain_name: Optional[str] = Field(default=None, description="Domain name for the MCP server")
    implementation_plan: Optional[str] = Field(default=None, description="High-level implementation plan")
    install_commands: Optional[str] = Field(default=None, description="Commands to install dependencies")


class AddArgumentInput(BaseModel):
    name: str = Field(description="Parameter name")
    type: str = Field(description="Python type annotation (e.g. 'int', 'str', 'list[float]')")
    description: str = Field(description="What this argument represents")
    default: Optional[str] = Field(default=None, description="Default value as Python literal")


class AddReturnFieldInput(BaseModel):
    name: str = Field(description="Key name in the returned dict")
    type: str = Field(description="Python type of the value")
    description: str = Field(description="What this return field represents")


class AddExampleInput(BaseModel):
    arguments: str = Field(description='JSON string mapping argument names to example values (e.g. \'{"n": "10"}\')')
    expected_output: str = Field(
        default="",
        description='Optional JSON string of expected output to validate against (e.g. \'{"result": "1.2 million"}\')',
    )
    expected_description: str = Field(
        default="", description="Natural-language description of expected output (used if expected_output is empty)"
    )


class FinalizeTaskSpecInput(BaseModel):
    """No parameters needed."""

    pass


# ── Tool factory ─────────────────────────────────────────────────────────


def create_codegen_tools(task_spec) -> list[FunctionTool]:
    """Create FunctionTools for code generation, bound to the given TaskSpec."""

    async def write_domain_file(domain_name: str, relative_path: str, content: str) -> str:
        """Write a file in the domain server directory."""
        try:
            target = _safe_domain_path(domain_name, relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            return f"Successfully wrote {len(content)} bytes to {relative_path}"
        except Exception as e:
            return f"Error writing file: {e}"

    async def read_domain_file(domain_name: str, relative_path: str) -> str:
        """Read a file from the domain server directory."""
        try:
            target = _safe_domain_path(domain_name, relative_path)
            if not target.exists():
                return f"Error: file not found: {relative_path}"
            content = target.read_text()
            return f"=== {relative_path} ===\n{content}"
        except Exception as e:
            return f"Error reading file: {e}"

    async def list_domain_files(domain_name: str) -> str:
        """List all files in the domain server directory."""
        try:
            domain_dir = _AGORA_MAF_ROOT / "domains" / domain_name / "server"
            if not domain_dir.exists():
                return f"Domain directory does not exist: {domain_dir}"
            files = []
            for p in domain_dir.rglob("*"):
                if p.is_file():
                    files.append(str(p.relative_to(domain_dir)))
            if not files:
                return "(no files)"
            return "\n".join(sorted(files))
        except Exception as e:
            return f"Error listing files: {e}"

    async def delete_domain_file(domain_name: str, relative_path: str) -> str:
        """Delete a file from the domain server directory."""
        try:
            target = _safe_domain_path(domain_name, relative_path)
            if not target.exists():
                return f"File does not exist: {relative_path}"
            target.unlink()
            return f"Deleted {relative_path}"
        except Exception as e:
            return f"Error deleting file: {e}"

    async def read_example_domain(file_path: str) -> str:
        """Read a file from the example domain for reference.

        Use this to understand the expected structure and patterns for domain servers.
        """
        try:
            example_dir = _AGORA_MAF_ROOT / "domains" / "example" / "server"
            target = (example_dir / file_path).resolve()
            if not str(target).startswith(str(example_dir.resolve())):
                return "Error: path traversal detected"
            if not target.exists():
                # List available files
                files = [str(p.relative_to(example_dir)) for p in example_dir.rglob("*") if p.is_file()]
                return f"File not found: {file_path}\n\nAvailable files:\n" + "\n".join(sorted(files))
            return f"=== example/{file_path} ===\n{target.read_text()}"
        except Exception as e:
            return f"Error reading example: {e}"

    return [
        FunctionTool(
            name="write_domain_file",
            description="Write a file in the domain server directory (domains/{domain}/server/).",
            func=write_domain_file,
            input_model=WriteDomainFileInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="read_domain_file",
            description="Read a file from the domain server directory.",
            func=read_domain_file,
            input_model=ReadDomainFileInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="list_domain_files",
            description="List all files in the domain server directory.",
            func=list_domain_files,
            input_model=ListDomainFilesInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="delete_domain_file",
            description="Delete a file from the domain server directory.",
            func=delete_domain_file,
            input_model=DeleteDomainFileInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="read_example_domain",
            description=(
                "Read a file from the example domain (domains/example/server/) for reference. "
                "Use this to understand the expected patterns for domain servers."
            ),
            func=read_example_domain,
            input_model=ReadExampleDomainInput,
            approval_mode="never_require",
        ),
    ]


def create_task_spec_tools(task_spec: TaskSpec) -> list[FunctionTool]:
    """Create FunctionTools for building up the TaskSpec."""

    async def view_task_spec() -> str:
        """View the current state of the task specification."""
        return task_spec.view()

    async def update_task_spec(
        repo_url: Optional[str] = None,
        repo_branch: Optional[str] = None,
        repo_name: Optional[str] = None,
        task_description: Optional[str] = None,
        tool_name: Optional[str] = None,
        domain_name: Optional[str] = None,
        implementation_plan: Optional[str] = None,
        install_commands: Optional[str] = None,
    ) -> str:
        """Update fields in the task specification."""
        updated = []
        if repo_url is not None:
            task_spec.repo_url = repo_url
            updated.append("repo_url")
        if repo_branch is not None:
            task_spec.repo_branch = repo_branch
            updated.append("repo_branch")
        if repo_name is not None:
            task_spec.repo_name = repo_name
            updated.append("repo_name")
        if task_description is not None:
            task_spec.task_description = task_description
            updated.append("task_description")
        if tool_name is not None:
            task_spec.tool_name = tool_name
            updated.append("tool_name")
        if domain_name is not None:
            task_spec.domain_name = domain_name
            updated.append("domain_name")
        if implementation_plan is not None:
            task_spec.implementation_plan = implementation_plan
            updated.append("implementation_plan")
        if install_commands is not None:
            task_spec.install_commands = install_commands
            updated.append("install_commands")

        if updated:
            return f"Updated: {', '.join(updated)}\n\n{task_spec.view()}"
        return "No fields were updated."

    async def add_argument(name: str, type: str, description: str, default: Optional[str] = None) -> str:
        """Add an argument to the tool specification."""
        arg = ArgumentSpec(name=name, type=type, description=description, default=default)
        task_spec.arguments.append(arg)
        return f"Added argument: {arg!r}"

    async def add_return_field(name: str, type: str, description: str) -> str:
        """Add a return field to the tool specification."""
        ret = ReturnFieldSpec(name=name, type=type, description=description)
        task_spec.returns.append(ret)
        return f"Added return field: {ret.name}: {ret.type}"

    async def add_example(arguments: str, expected_output: str = "", expected_description: str = "") -> str:
        """Add an example invocation for testing. Arguments should be a JSON string."""
        try:
            args_dict = json.loads(arguments)
        except json.JSONDecodeError as e:
            return f"Error: invalid JSON for arguments: {e}"

        # Parse expected_output if provided
        expected_dict = None
        if expected_output:
            try:
                expected_dict = json.loads(expected_output)
            except json.JSONDecodeError as e:
                return f"Error: invalid JSON for expected_output: {e}"

        example = ExampleInvocation(
            arguments=args_dict, expected_output=expected_dict, expected_description=expected_description
        )
        task_spec.examples.append(example)
        return f"Added example #{len(task_spec.examples)}: {args_dict}" + (
            f" (expected: {expected_dict})" if expected_dict else ""
        )

    async def finalize_task_spec() -> str:
        """Finalize the task specification and proceed to implementation.

        Returns an error if required fields are missing.
        """
        if not task_spec.is_complete:
            return f"Cannot finalize: missing fields: {', '.join(task_spec.missing_fields)}"
        return f"Task specification finalized.\n\n{task_spec.view()}"

    return [
        FunctionTool(
            name="view_task_spec",
            description="View the current state of the task specification.",
            func=view_task_spec,
            input_model=ViewTaskSpecInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="update_task_spec",
            description="Update one or more fields in the task specification.",
            func=update_task_spec,
            input_model=UpdateTaskSpecInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="add_argument",
            description="Add an argument to the tool specification.",
            func=add_argument,
            input_model=AddArgumentInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="add_return_field",
            description="Add a return field to the tool specification.",
            func=add_return_field,
            input_model=AddReturnFieldInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="add_example",
            description=(
                "Add an example invocation for testing. "
                "Provide arguments as JSON, and optionally expected_output as JSON for validation."
            ),
            func=add_example,
            input_model=AddExampleInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="finalize_task_spec",
            description="Finalize the task specification and proceed to implementation.",
            func=finalize_task_spec,
            input_model=FinalizeTaskSpecInput,
            approval_mode="never_require",
        ),
    ]
