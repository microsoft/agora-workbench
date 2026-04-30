"""
FunctionTools for building Docker images, running tool functions directly,
and iterating on domain implementations.

These power the automated compile → test → edit loop in Stage 2.

Architecture: Tools are tested in a lightweight Docker container that installs
only the domain's own requirements. The tool function is called directly via
``docker exec`` — no MCP server, no auth, no network exposure. This avoids
the need for the private-feed ``mise`` package and Entra ID credentials that
the full CodeExecutionServer requires.
"""

import json
import logging
import subprocess
import textwrap
import time
from pathlib import Path

from agent_framework import FunctionTool
from pydantic import BaseModel, Field

from ..models import ImplementationState, BuildStatus, TestResult

LOGGER = logging.getLogger(__name__)

# AgoraAgentMAF root
_AGORA_MAF_ROOT = Path(__file__).resolve().parent.parent.parent.parent


# ── Pydantic input models ────────────────────────────────────────────────


class BuildDomainImageInput(BaseModel):
    domain_name: str = Field(description="Domain name (e.g. 'stamp')")


class StartDomainContainerInput(BaseModel):
    domain_name: str = Field(description="Domain name (e.g. 'stamp')")


class StopDomainContainerInput(BaseModel):
    domain_name: str = Field(description="Domain name (e.g. 'stamp')")


class TestDomainToolInput(BaseModel):
    domain_name: str = Field(description="Domain name (e.g. 'stamp')")
    tool_name: str = Field(description="Name of the tool to test")
    arguments: str = Field(description="JSON string of tool arguments (e.g. '{\"n\": 10}')")
    expected_output: str = Field(
        default="",
        description='Optional JSON string of expected output to validate against (e.g. \'{"result": "1.2 million"}\')',
    )


class GetContainerLogsInput(BaseModel):
    domain_name: str = Field(description="Domain name (e.g. 'stamp')")
    tail: int = Field(default=100, description="Number of log lines to retrieve")


class RunBuildCommandInput(BaseModel):
    command: str = Field(description="Shell command to run in the AgoraAgentMAF root directory")
    timeout: int = Field(default=300, description="Timeout in seconds")


# ── Tool factory ─────────────────────────────────────────────────────────


def create_docker_tools(impl_state: ImplementationState) -> list[FunctionTool]:
    """Create FunctionTools for Docker build/test operations.

    Tools test domain code in a lightweight container that only installs the
    domain's own requirements.  No MCP server, no auth, no network exposure.
    """

    async def build_domain_image(domain_name: str) -> str:
        """Build a lightweight Docker image for testing domain tool functions.

        Generates a minimal Dockerfile that installs only the domain's own
        requirements — no CodeExecutionServer, no MISE auth, no private feeds.
        The domain server code must already exist under domains/{domain_name}/server/.
        """
        impl_state.build_status = BuildStatus.BUILDING
        impl_state.iteration += 1

        domain_dir = _AGORA_MAF_ROOT / "domains" / domain_name / "server"
        if not domain_dir.exists():
            impl_state.build_status = BuildStatus.BUILD_FAILED
            return f"Error: domain directory not found: domains/{domain_name}/server/"

        image_tag = f"agoramaf-toolmaker-{domain_name}:latest"
        impl_state.image_tag = image_tag

        # Generate a lightweight Dockerfile for the new domain
        dockerfile_content = _generate_domain_dockerfile(domain_name)
        temp_dockerfile = domain_dir / "Dockerfile.toolmaker"
        temp_dockerfile.write_text(dockerfile_content)

        try:
            result = subprocess.run(
                [
                    "docker",
                    "build",
                    "-f",
                    str(temp_dockerfile),
                    "-t",
                    image_tag,
                    str(_AGORA_MAF_ROOT),
                ],
                capture_output=True,
                text=True,
                timeout=600,
                cwd=str(_AGORA_MAF_ROOT),
            )

            if result.returncode != 0:
                impl_state.build_status = BuildStatus.BUILD_FAILED
                error = result.stderr[-3000:] if len(result.stderr) > 3000 else result.stderr
                return f"Docker build FAILED:\n{error}"

            impl_state.build_status = BuildStatus.TESTING
            return f"Docker build SUCCEEDED. Image: {image_tag}"

        except subprocess.TimeoutExpired:
            impl_state.build_status = BuildStatus.BUILD_FAILED
            return "Error: Docker build timed out after 600 seconds"
        except FileNotFoundError:
            impl_state.build_status = BuildStatus.BUILD_FAILED
            return "Error: 'docker' command not found. Is Docker installed?"
        except Exception as e:
            impl_state.build_status = BuildStatus.BUILD_FAILED
            return f"Error during build: {e}"
        finally:
            if temp_dockerfile.exists():
                temp_dockerfile.unlink()

    async def start_domain_container(domain_name: str) -> str:
        """Start a lightweight container for testing domain tools.

        The container stays alive via ``sleep infinity`` — no server process,
        no ports exposed, no network access needed.  Use ``test_domain_tool``
        to execute tool functions inside it via ``docker exec``.
        """
        image_tag = impl_state.image_tag or f"agoramaf-toolmaker-{domain_name}:latest"
        container_name = f"toolmaker-{domain_name}-server"

        # Stop any existing container with the same name
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            timeout=30,
        )

        try:
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    container_name,
                    "--network",
                    "none",
                    image_tag,
                    "sleep",
                    "infinity",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                return f"Error starting container: {result.stderr}"

            impl_state.container_id = result.stdout.strip()[:12]

            # Quick sanity check — container should be running
            time.sleep(1)
            inspect = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Status}}", container_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            status = inspect.stdout.strip()
            if status != "running":
                logs = subprocess.run(
                    ["docker", "logs", "--tail", "20", container_name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                return (
                    f"Container exited unexpectedly (status: {status}).\n"
                    f"Logs:\n{logs.stdout[-2000:]}\n{logs.stderr[-2000:]}"
                )

            return f"Container started (name: {container_name}, id: {impl_state.container_id}). Ready for testing."

        except Exception as e:
            return f"Error starting container: {e}"

    async def stop_domain_container(domain_name: str) -> str:
        """Stop and remove the domain test container."""
        container_name = f"toolmaker-{domain_name}-server"
        try:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            impl_state.container_id = None
            return f"Stopped and removed container: {container_name}"
        except Exception as e:
            return f"Error stopping container: {e}"

    async def test_domain_tool(
        domain_name: str,
        tool_name: str,
        arguments: str,
        expected_output: str = "",
    ) -> str:
        """Test a domain tool by calling the function directly inside the container.

        Runs ``docker exec`` to import the tool module and invoke the function
        with the provided arguments.  No MCP server or auth required.

        Args:
            domain_name: Name of the domain being tested
            tool_name: Name of the tool function to call
            arguments: JSON string of arguments to pass to the tool
            expected_output: Optional JSON string of expected output to validate against.
                             If provided, the test will compare actual vs expected.
        """
        impl_state.build_status = BuildStatus.TESTING
        container_name = f"toolmaker-{domain_name}-server"

        try:
            args_dict = json.loads(arguments)
        except json.JSONDecodeError as e:
            return f"Error: invalid JSON arguments: {e}"

        # Parse expected output if provided
        expected_dict = None
        if expected_output:
            try:
                expected_dict = json.loads(expected_output)
            except json.JSONDecodeError as e:
                return f"Error: invalid JSON expected_output: {e}"

        # Build a Python snippet that discovers the tool module from the
        # domain's tool_registry and calls the function directly.
        args_json = json.dumps(args_dict)
        test_script = textwrap.dedent(f"""\
            import importlib, json, sys, traceback
            try:
                # Import the domain's tool_registry to find the module for this tool
                registry_mod = importlib.import_module("domains.{domain_name}.server.tool_registry")
                # Look for the factory function (create_*_tool_registry)
                factory = None
                for attr_name in dir(registry_mod):
                    attr = getattr(registry_mod, attr_name)
                    if callable(attr) and attr_name.startswith("create_") and attr_name.endswith("_tool_registry"):
                        factory = attr
                        break
                if factory is None:
                    print(json.dumps({{"error": "No create_*_tool_registry factory found in tool_registry.py"}}))
                    sys.exit(1)
                registry = factory()
                tool_def = registry.get_tool_by_name("{tool_name}")
                if tool_def is None:
                    names = [t.name for t in registry.tools]
                    print(json.dumps({{"error": f"Tool '{tool_name}' not found. Available: {{names}}"}}))
                    sys.exit(1)
                # Import the tool module and call the function
                tool_module = importlib.import_module(tool_def.module)
                tool_func = getattr(tool_module, "{tool_name}")
                args = json.loads('{args_json}')
                result = tool_func(**args)
                print(json.dumps({{"success": True, "result": result}}))
            except Exception:
                traceback.print_exc()
                print(json.dumps({{"success": False, "error": traceback.format_exc()}}))
        """)

        try:
            result = subprocess.run(
                ["docker", "exec", container_name, "python", "-c", test_script],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            test_result = TestResult(
                tool_name=tool_name,
                arguments=args_dict,
                success=False,
                error="Tool execution timed out after 120 seconds",
            )
            impl_state.test_results.append(test_result)
            impl_state.build_status = BuildStatus.TEST_FAILED
            return "Error: tool execution timed out after 120 seconds"
        except Exception as e:
            test_result = TestResult(
                tool_name=tool_name,
                arguments=args_dict,
                success=False,
                error=str(e),
            )
            impl_state.test_results.append(test_result)
            impl_state.build_status = BuildStatus.TEST_FAILED
            return f"Error running docker exec: {e}"

        # Combine stdout/stderr for diagnostics
        full_output = result.stdout
        if result.stderr:
            full_output += f"\nstderr:\n{result.stderr}"

        if result.returncode != 0:
            test_result = TestResult(
                tool_name=tool_name,
                arguments=args_dict,
                success=False,
                error=full_output[-3000:],
                stdout=result.stdout[-2000:] if result.stdout else None,
                stderr=result.stderr[-2000:] if result.stderr else None,
            )
            impl_state.test_results.append(test_result)
            impl_state.build_status = BuildStatus.TEST_FAILED
            return f"Tool execution FAILED (exit code {result.returncode}):\n{full_output[-3000:]}"

        # Parse the JSON result from the last line of stdout
        stdout_lines = result.stdout.strip().splitlines()
        try:
            exec_result = json.loads(stdout_lines[-1])
        except (json.JSONDecodeError, IndexError):
            test_result = TestResult(
                tool_name=tool_name,
                arguments=args_dict,
                success=False,
                error=f"Could not parse tool output as JSON.\nFull output:\n{full_output[-3000:]}",
                stdout=result.stdout[-2000:] if result.stdout else None,
                stderr=result.stderr[-2000:] if result.stderr else None,
            )
            impl_state.test_results.append(test_result)
            impl_state.build_status = BuildStatus.TEST_FAILED
            return f"Tool execution produced unparseable output:\n{full_output[-3000:]}"

        if not exec_result.get("success"):
            error_msg = exec_result.get("error", "Unknown error")
            test_result = TestResult(
                tool_name=tool_name,
                arguments=args_dict,
                success=False,
                error=error_msg[:3000],
                stdout=result.stdout[-2000:] if result.stdout else None,
                stderr=result.stderr[-2000:] if result.stderr else None,
            )
            impl_state.test_results.append(test_result)
            impl_state.build_status = BuildStatus.TEST_FAILED
            return f"Tool call FAILED:\n{error_msg[:3000]}"

        # Success — extract tool's return value
        tool_output = exec_result.get("result", {})
        tool_output_str = json.dumps(tool_output, default=str)

        # Validate against expected output if provided
        validation_passed = None
        validation_message = ""
        if expected_dict is not None:
            if "result" in expected_dict and isinstance(tool_output, dict) and "result" in tool_output:
                validation_passed = tool_output["result"] == expected_dict["result"]
            else:
                validation_passed = tool_output == expected_dict

            if validation_passed:
                validation_message = "Output matches expected value."
            else:
                validation_message = f"Output MISMATCH!\n  Expected: {expected_dict}\n  Actual: {tool_output}"

        # Any print() output from the tool (everything except the last JSON line)
        tool_logs = "\n".join(stdout_lines[:-1]) if len(stdout_lines) > 1 else ""

        test_result = TestResult(
            tool_name=tool_name,
            arguments=args_dict,
            success=True,
            output=tool_output_str[:5000],
            expected_output=expected_dict,
            validation_passed=validation_passed,
            stdout=tool_logs[:2000] if tool_logs else None,
            stderr=result.stderr[-2000:] if result.stderr else None,
        )
        impl_state.test_results.append(test_result)
        impl_state.build_status = BuildStatus.PASSED

        status_msg = f"Tool call SUCCEEDED:\n{tool_output_str[:5000]}"
        if tool_logs:
            status_msg += f"\n\nTool logs:\n{tool_logs[:2000]}"
        if validation_message:
            status_msg += f"\n\nValidation: {validation_message}"
        return status_msg

    async def get_container_logs(domain_name: str, tail: int = 100) -> str:
        """Get recent logs from the domain test container."""
        container_name = f"toolmaker-{domain_name}-server"
        try:
            result = subprocess.run(
                ["docker", "logs", "--tail", str(tail), container_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = ""
            if result.stdout:
                output += f"stdout:\n{result.stdout[-5000:]}"
            if result.stderr:
                output += f"\nstderr:\n{result.stderr[-5000:]}"
            return output if output.strip() else "(no logs)"
        except Exception as e:
            return f"Error getting logs: {e}"

    async def run_build_command(command: str, timeout: int = 300) -> str:
        """Run a shell command in the AgoraAgentMAF root directory.

        Use for debugging: checking Docker status, inspecting files, running
        pip freeze inside a container, etc.
        """
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=str(_AGORA_MAF_ROOT),
                timeout=timeout,
            )
            output = ""
            if result.stdout:
                output += result.stdout[:8000]
            if result.stderr:
                output += f"\nstderr:\n{result.stderr[:4000]}"
            if result.returncode != 0:
                output += f"\n(exit code: {result.returncode})"
            return output if output.strip() else "(no output)"
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {timeout} seconds"
        except Exception as e:
            return f"Error: {e}"

    return [
        FunctionTool(
            name="build_domain_image",
            description=(
                "Build a lightweight Docker image for testing domain tools. "
                "The domain server code must exist under domains/{domain_name}/server/. "
                "Only installs the domain's own requirements — no auth, no private feeds."
            ),
            func=build_domain_image,
            input_model=BuildDomainImageInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="start_domain_container",
            description=(
                "Start a test container for the domain (no server, no ports). "
                "Must be called after build_domain_image succeeds."
            ),
            func=start_domain_container,
            input_model=StartDomainContainerInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="stop_domain_container",
            description="Stop and remove the domain test container.",
            func=stop_domain_container,
            input_model=StopDomainContainerInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="test_domain_tool",
            description=(
                "Test a domain tool by calling the function directly inside the container. "
                "Arguments should be a JSON string mapping parameter names to values. "
                "Optionally provide expected_output as JSON to validate the result."
            ),
            func=test_domain_tool,
            input_model=TestDomainToolInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="get_container_logs",
            description="Get recent logs from the domain test container (for debugging).",
            func=get_container_logs,
            input_model=GetContainerLogsInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="run_build_command",
            description=(
                "Run a shell command in the AgoraAgentMAF root directory. "
                "Use for debugging: checking Docker status, inspecting files, "
                "running commands inside containers, etc."
            ),
            func=run_build_command,
            input_model=RunBuildCommandInput,
            approval_mode="never_require",
        ),
    ]


# ── Helpers ──────────────────────────────────────────────────────────────


def _generate_domain_dockerfile(domain_name: str) -> str:
    """Generate a lightweight Dockerfile for testing domain tool functions.

    This image installs only the domain's own requirements and tools package.
    It does NOT include the CodeExecutionServer, MISE auth, or any private-feed
    packages.  The container is used exclusively for local ``docker exec``
    testing and exposes no network ports.
    """
    return f"""# Auto-generated lightweight Dockerfile for testing {domain_name} tools
# Built by the ToolMaker agent — NO auth, NO server, NO network exposure

FROM mcr.microsoft.com/devcontainers/python:3.11

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \\
    curl git build-essential && \\
    rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Provide a lightweight ``code_execution`` shim so domain tool_registry files
# can ``from code_execution import ToolRegistry, ToolDefinition, ...`` without
# pulling in the full server stack (which requires MISE, Azure SDKs, etc.).
# We copy only the self-contained tool_registry subpackage and re-export its
# public symbols from a thin __init__.py.
COPY code_execution/code_execution/tool_registry/ /app/code_execution/tool_registry/
RUN printf '%s\\n' \\
    'from .tool_registry import ToolRegistry, ToolDefinition, ToolParameter, ReturnSpec' \\
    '__all__ = ["ToolRegistry", "ToolDefinition", "ToolParameter", "ReturnSpec"]' \\
    > /app/code_execution/__init__.py

# Copy the domain server
COPY domains/{domain_name}/server/ /app/domains/{domain_name}/server/
RUN touch /app/domains/__init__.py /app/domains/{domain_name}/__init__.py /app/domains/{domain_name}/server/__init__.py 2>/dev/null || true

# Install domain-specific requirements if present
RUN if [ -f /app/domains/{domain_name}/server/requirements.txt ]; then \\
        uv pip install --system -r /app/domains/{domain_name}/server/requirements.txt; \\
    fi

# Install domain tools package if present
RUN if [ -f /app/domains/{domain_name}/server/tools/pyproject.toml ]; then \\
        uv pip install --system -e /app/domains/{domain_name}/server/tools/; \\
    fi

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Keep container alive for docker exec testing
CMD ["sleep", "infinity"]
"""
