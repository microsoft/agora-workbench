"""
Environment builders for different Python package managers.

This module provides builder functions for creating Python environments
using various tools: uv, conda, and pip.
"""

import asyncio
import logging
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .code_execution_models import EnvironmentConfig

LOGGER = logging.getLogger(__name__)


IPYKERNEL_REQUIREMENT = os.getenv("MCP_IPYKERNEL_REQUIREMENT", "ipykernel>=6.29.0")

# Packages that the CodeExecutionServer infrastructure assumes are available
# in every kernel environment.  These are injected automatically by the
# environment builders regardless of the domain's dependency file.
#
# Add entries here when server-side code generates kernel snippets that
# import a package (e.g. ``import dill`` in the object-transfer path).
SERVER_REQUIRED_KERNEL_PACKAGES: list[str] = [
    IPYKERNEL_REQUIREMENT,
    "dill>=0.3.8",
]


def _get_wheelhouse_dir() -> Path | None:
    """Return a local wheelhouse directory if available.

    This is an opt-in performance optimization for containerized execution:
    if the image pre-downloads wheels (e.g., ipykernel/ipython), we can
    install from local files instead of hitting the network.
    """

    # Explicit override
    env_value = os.getenv("MCP_WHEELHOUSE")
    if env_value:
        wheelhouse = Path(env_value)
        return wheelhouse if wheelhouse.exists() else None

    # Common defaults
    for candidate in (Path("/opt/wheelhouse"), Path("/opt/wheels")):
        if candidate.exists():
            return candidate
    return None


def _with_wheelhouse_args(cmd: list[str]) -> list[str]:
    """Add installer flags to prefer local wheels when available."""
    wheelhouse = _get_wheelhouse_dir()
    if not wheelhouse:
        return cmd

    # Support both pip and uv pip: both accept --find-links.
    # Do NOT use --no-index: we only want to prefer the wheelhouse, not
    # forbid downloading other deps that aren't pre-cached.
    return cmd + ["--find-links", str(wheelhouse)]


def _package_name(requirement: str) -> str:
    """Extract the bare package name from a requirement specifier."""
    for sep in (">=", "<=", "==", "!=", "~=", ">", "<", "["):
        if sep in requirement:
            return requirement[: requirement.index(sep)].strip()
    return requirement.strip()


def _ensure_packages_in_requirements_file(req_file: Path, packages: list[str]) -> None:
    """Ensure a requirements file includes all *packages*.

    Appends any missing entries.  Each element of *packages* is a PEP 508
    requirement string (e.g. ``"dill>=0.3.8"``).
    """
    try:
        content = req_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return

    content_lower = content.lower()
    missing = [pkg for pkg in packages if _package_name(pkg).lower() not in content_lower]

    if not missing:
        return

    suffix = "" if content.endswith("\n") else "\n"
    req_file.write_text(content + suffix + "\n".join(missing) + "\n", encoding="utf-8")


def _ensure_packages_in_environment_yml(dep_file: Path, packages: list[str]) -> None:
    """Ensure a conda environment.yml includes all *packages*.

    Injects missing dependency lines under the top-level ``dependencies:``
    list.  If no such section exists one is appended.
    """
    try:
        content = dep_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return

    content_lower = content.lower()
    missing = [pkg for pkg in packages if _package_name(pkg).lower() not in content_lower]

    if not missing:
        return

    lines = content.splitlines(keepends=True)
    injected = False

    for idx, line in enumerate(lines):
        if line.strip() == "dependencies:" and (len(line) - len(line.lstrip(" ")) == 0):
            insert_at = idx + 1
            for pkg in missing:
                lines.insert(insert_at, f"  - {pkg}\n")
                insert_at += 1
            injected = True
            break

    if not injected:
        suffix = "\n" if content.endswith("\n") else "\n\n"
        lines.append(suffix)
        lines.append("dependencies:\n")
        for pkg in missing:
            lines.append(f"  - {pkg}\n")

    dep_file.write_text("".join(lines), encoding="utf-8")


async def build_uv_environment(config: "EnvironmentConfig"):
    """Build environment using uv.

    Args:
        config: EnvironmentConfig with type="uv" and uv.lock dependency_file
    """
    build_dir = config.get_build_dir()

    # Check if uv is installed
    uv_bin = shutil.which("uv")
    if not uv_bin:
        raise RuntimeError(
            "uv not found. Install from https://github.com/astral-sh/uv\n"
            "Quick install: curl -LsSf https://astral.sh/uv/install.sh | sh"
        )

    LOGGER.info(f"Using uv to create environment at {build_dir}")

    # Check if environment already exists and is complete.
    # A partial environment (venv exists but pip install failed) must be rebuilt.
    python_path = build_dir / "bin" / "python"
    marker_file = build_dir / ".env_build_complete"
    if python_path.exists() and not marker_file.exists():
        LOGGER.warning(f"Stale/incomplete environment detected at {build_dir} — removing and rebuilding")
        import shutil as _shutil

        _shutil.rmtree(build_dir, ignore_errors=True)

    if not python_path.exists():
        # Create virtual environment with uv
        cmd = [uv_bin, "venv", str(build_dir)]
        LOGGER.info(f"Running: {' '.join(cmd)}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_msg = stderr.decode() if stderr else stdout.decode()
            raise RuntimeError(f"Failed to create uv venv: {error_msg}")

        LOGGER.info("uv venv created successfully")
    else:
        LOGGER.info(f"Environment already exists at {build_dir}, reusing it")

    # Install dependencies from requirements.txt
    req_file_path = build_dir.parent / "requirements.txt"

    if not req_file_path.exists():
        raise RuntimeError(
            f"requirements.txt file not found at {req_file_path}. UV environments require a requirements.txt file."
        )

    _ensure_packages_in_requirements_file(req_file_path, SERVER_REQUIRED_KERNEL_PACKAGES)

    # Install packages using uv pip, targeting the specific Python in the venv
    python_path = build_dir / "bin" / "python"
    cmd = [uv_bin, "pip", "install", "--python", str(python_path), "-r", str(req_file_path)]
    cmd = _with_wheelhouse_args(cmd)
    LOGGER.info("Installing dependencies from requirements.txt")
    LOGGER.info(f"Running: {' '.join(cmd)}")

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        error_msg = stderr.decode() if stderr else stdout.decode()
        raise RuntimeError(f"Failed to install dependencies from requirements.txt: {error_msg}")

    LOGGER.info("Dependencies installed successfully from requirements.txt")

    # Run additional commands if specified
    if config.additional_commands:
        LOGGER.info(f"Running {len(config.additional_commands)} additional command(s)")

        for i, command in enumerate(config.additional_commands, 1):
            LOGGER.info(f"Running additional command {i}/{len(config.additional_commands)}: {command}")

            # Run command using the venv's Python
            activate_script = shlex.quote(str(build_dir / "bin" / "activate"))
            cmd = ["bash", "-c", f"source {activate_script} && {command}"]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                error_msg = stderr.decode() if stderr else stdout.decode()
                LOGGER.warning(f"Additional command failed (continuing anyway): {error_msg}")
            else:
                LOGGER.info(f"Additional command {i} completed successfully")

    # Mark environment as successfully built
    marker_file = build_dir / ".env_build_complete"
    marker_file.write_text("ok")


async def build_conda_environment(config: "EnvironmentConfig"):
    """Build environment using conda.

    Args:
        config: EnvironmentConfig with type="conda" and environment.yaml dependency_file
    """
    build_dir = config.get_build_dir()

    # Find conda or mamba
    conda_bin = shutil.which("mamba") or shutil.which("conda")
    if not conda_bin:
        raise RuntimeError(
            "conda/mamba not found. Install Miniconda from https://docs.conda.io/en/latest/miniconda.html "
            "or Mamba from https://mamba.readthedocs.io/"
        )

    LOGGER.info(f"Using {conda_bin} to create environment")

    # Check if environment already exists
    python_path = build_dir / "bin" / "python"
    dep_file_path = build_dir.parent / "environment.yml"

    # Ensure server-required packages are present for kernel registration.
    _ensure_packages_in_environment_yml(dep_file_path, SERVER_REQUIRED_KERNEL_PACKAGES)

    if not python_path.exists():
        # Create base conda environment
        if not dep_file_path.exists():
            raise RuntimeError(f"environment.yml file not found at {dep_file_path}")

        # Create from environment.yml file
        cmd = [conda_bin, "env", "create", "--prefix", str(build_dir), "--file", str(dep_file_path), "--yes"]
        LOGGER.info(f"Running: {' '.join(cmd)}")

        # Create the conda environment
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_msg = stderr.decode() if stderr else stdout.decode()
            raise RuntimeError(f"Failed to create conda environment: {error_msg}")

        LOGGER.info(f"Conda environment created successfully at {build_dir}")
    else:
        LOGGER.info(f"Environment already exists at {build_dir}, reusing it")

    # Run additional commands if specified
    if config.additional_commands:
        LOGGER.info(f"Running {len(config.additional_commands)} additional command(s)")

        for i, command in enumerate(config.additional_commands, 1):
            LOGGER.info(f"Running additional command {i}/{len(config.additional_commands)}: {command}")

            # Run command with the conda environment's bin/ on PATH.
            # Avoid ``conda run`` / ``mamba run`` which generate wrapper
            # scripts using ``exec --`` — a syntax not supported by all
            # bash versions bundled in conda environments.
            env = os.environ.copy()
            env["PATH"] = f"{build_dir / 'bin'}:{env.get('PATH', '')}"
            env["CONDA_PREFIX"] = str(build_dir)
            env["CONDA_DEFAULT_ENV"] = build_dir.name
            cmd = ["bash", "-c", command]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                error_msg = stderr.decode() if stderr else stdout.decode()
                LOGGER.warning(f"Additional command failed (continuing anyway): {error_msg}")
            else:
                LOGGER.info(f"Additional command {i} completed successfully")


async def build_pip_environment(config: "EnvironmentConfig"):
    """Build environment using Python venv + pip.

    Args:
        config: EnvironmentConfig with type="pip" and requirements.txt dependency_file
    """
    build_dir = config.get_build_dir()

    LOGGER.info(f"Creating Python venv at {build_dir}")

    # Check if environment already exists
    python_path = build_dir / "bin" / "python"
    if not python_path.exists():
        # Create virtual environment using Python's venv module
        cmd = [sys.executable, "-m", "venv", str(build_dir)]
        LOGGER.info(f"Running: {' '.join(cmd)}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_msg = stderr.decode() if stderr else stdout.decode()
            raise RuntimeError(f"Failed to create venv: {error_msg}")

        LOGGER.info("Virtual environment created successfully")
    else:
        LOGGER.info(f"Environment already exists at {build_dir}, reusing it")

    # Install dependencies from requirements.txt in parent directory
    req_file = build_dir.parent / "requirements.txt"

    if req_file.exists():
        _ensure_packages_in_requirements_file(req_file, SERVER_REQUIRED_KERNEL_PACKAGES)
        pip_bin = build_dir / "bin" / "pip"
        cmd = [str(pip_bin), "install", "-r", str(req_file)]
        cmd = _with_wheelhouse_args(cmd)
        LOGGER.info("Installing dependencies from requirements.txt")
        LOGGER.info(f"Running: {' '.join(cmd)}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_msg = stderr.decode() if stderr else stdout.decode()
            raise RuntimeError(f"Failed to install dependencies: {error_msg}")

        LOGGER.info("Dependencies installed successfully")
    else:
        raise RuntimeError(f"requirements.txt file not found at {req_file}")

    # Run additional commands if specified
    if config.additional_commands:
        LOGGER.info(f"Running {len(config.additional_commands)} additional command(s)")

        for i, command in enumerate(config.additional_commands, 1):
            LOGGER.info(f"Running additional command {i}/{len(config.additional_commands)}: {command}")

            # Run command using the venv's Python
            activate_script = shlex.quote(str(build_dir / "bin" / "activate"))
            cmd = ["bash", "-c", f"source {activate_script} && {command}"]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                error_msg = stderr.decode() if stderr else stdout.decode()
                LOGGER.warning(f"Additional command failed (continuing anyway): {error_msg}")
            else:
                LOGGER.info(f"Additional command {i} completed successfully")
