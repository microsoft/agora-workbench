"""
Tests for environment builders.
"""

import shutil
from pathlib import Path

import pytest

from ..code_execution.code_execution_models import EnvironmentConfig
from ..code_execution.environment_builders import (
    build_conda_environment,
    build_pip_environment,
    build_uv_environment,
)


@pytest.fixture
def uv_config(tmp_path: Path) -> EnvironmentConfig:
    """Create a test UV environment config."""
    return EnvironmentConfig(
        name="test_uv",
        description="Test UV environment",
        type="uv",
        dependency_file="requests>=2.31.0\n",
        auto_build=True,
        build_dir=tmp_path / "test_uv" / "uv",
    )


@pytest.fixture
def conda_config(tmp_path: Path) -> EnvironmentConfig:
    """Create a test Conda environment config."""
    env_yaml = """name: test_conda
channels:
  - conda-forge
  - defaults
dependencies:
  - python=3.11
  - numpy
"""
    return EnvironmentConfig(
        name="test_conda",
        description="Test Conda environment",
        type="conda",
        dependency_file=env_yaml,
        auto_build=True,
        build_dir=tmp_path / "test_conda" / "conda",
    )


@pytest.fixture
def pip_config(tmp_path: Path) -> EnvironmentConfig:
    """Create a test pip environment config."""
    return EnvironmentConfig(
        name="test_pip",
        description="Test pip environment",
        type="pip",
        dependency_file="requests>=2.31.0\n",
        auto_build=True,
        build_dir=tmp_path / "test_pip" / "pip",
    )


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("uv"), reason="uv not installed")
async def test_build_uv_environment(uv_config):
    """Test building a UV environment."""
    # Write dependency file to parent directory
    parent_dir = uv_config.build_dir.parent
    parent_dir.mkdir(parents=True, exist_ok=True)
    (parent_dir / "requirements.txt").write_text(uv_config.dependency_file)

    await build_uv_environment(uv_config)

    # Check that environment was created
    python_path = uv_config.get_python_path()
    assert python_path.exists()
    assert (uv_config.build_dir / "bin").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(not (shutil.which("conda") or shutil.which("mamba")), reason="conda/mamba not installed")
async def test_build_conda_environment(conda_config):
    """Test building a Conda environment."""
    # Write dependency file to parent directory
    parent_dir = conda_config.build_dir.parent
    parent_dir.mkdir(parents=True, exist_ok=True)
    (parent_dir / "environment.yml").write_text(conda_config.dependency_file)

    await build_conda_environment(conda_config)

    # Check that environment was created
    python_path = conda_config.get_python_path()
    assert python_path.exists()
    assert (conda_config.build_dir / "bin").exists()


@pytest.mark.asyncio
async def test_build_pip_environment(pip_config):
    """Test building a pip environment."""
    # Write dependency file to parent directory
    parent_dir = pip_config.build_dir.parent
    parent_dir.mkdir(parents=True, exist_ok=True)
    (parent_dir / "requirements.txt").write_text(pip_config.dependency_file)

    await build_pip_environment(pip_config)

    # Check that environment was created
    python_path = pip_config.get_python_path()
    assert python_path.exists()
    assert (pip_config.build_dir / "bin").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("uv"), reason="uv not installed")
async def test_uv_environment_reuse(uv_config):
    """Test that UV environment is reused if it exists."""
    # Build once
    parent_dir = uv_config.build_dir.parent
    parent_dir.mkdir(parents=True, exist_ok=True)
    (parent_dir / "requirements.txt").write_text(uv_config.dependency_file)

    await build_uv_environment(uv_config)
    python_path = uv_config.get_python_path()

    # Build again - should reuse
    await build_uv_environment(uv_config)

    # Python binary should not have been recreated
    assert python_path.exists()


def test_environment_config_get_build_dir():
    """Test that build_dir is constructed correctly."""
    config = EnvironmentConfig(
        name="myenv",
        description="Test",
        type="uv",
        dependency_file="numpy\n",
    )

    build_dir = config.get_build_dir()
    assert build_dir.name == "uv"
    assert build_dir.parent.name == "myenv"
    assert "mcp-envs" in str(build_dir)


def test_environment_config_get_python_path():
    """Test that Python path is constructed correctly."""
    config = EnvironmentConfig(
        name="myenv",
        description="Test",
        type="uv",
        dependency_file="numpy\n",
    )

    python_path = config.get_python_path()
    assert python_path.name == "python"
    assert "bin" in str(python_path)


def test_environment_config_custom_build_dir(tmp_path):
    """Test using a custom build directory."""
    custom_dir = tmp_path / "custom" / "location"

    config = EnvironmentConfig(
        name="myenv",
        description="Test",
        type="pip",
        dependency_file="numpy\n",
        build_dir=custom_dir,
    )

    assert config.get_build_dir() == custom_dir


# ---------------------------------------------------------------------------
# additional_commands behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("uv"), reason="uv not installed")
async def test_additional_command_runs_inside_env(tmp_path: Path) -> None:
    """``additional_commands`` must execute against the env's interpreter.

    Regression test for the bug where host shell init (e.g. a miniforge
    install in the base image that activates an unrelated base env on
    interactive bash startup) would shadow the env's PATH, causing
    ``python -m pip install ...`` to be intercepted by a mamba wrapper
    and fail. We pass ``--noprofile --norc`` to bash to keep this honest.
    """
    marker = tmp_path / "which_python.txt"
    config = EnvironmentConfig(
        name="test_addcmd_ok",
        description="additional_commands sanity",
        type="uv",
        dependency_file="\n",
        auto_build=True,
        build_dir=tmp_path / "test_addcmd_ok" / "uv",
        additional_commands=[
            # Resolve the actual ``python`` on PATH inside the activated
            # venv and write it to a marker file. We then assert the
            # resolved interpreter lives under the venv's build_dir.
            f"command -v python > {marker}",
        ],
    )

    parent_dir = config.build_dir.parent
    parent_dir.mkdir(parents=True, exist_ok=True)
    (parent_dir / "requirements.txt").write_text(config.dependency_file)

    await build_uv_environment(config)

    written = marker.read_text().strip()
    assert str(config.build_dir) in written, (
        f"additional_command ran outside the env: python={written!r} "
        f"build_dir={config.build_dir}"
    )


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("uv"), reason="uv not installed")
async def test_additional_command_failure_raises(tmp_path: Path) -> None:
    """A failing ``additional_commands`` step must surface as a build error.

    Regression test: previously failures were swallowed with
    ``LOGGER.warning("...continuing anyway")``, which let servers come up
    with a half-built environment (e.g. a domain-tools pip package that
    silently failed to install). The build now raises ``RuntimeError``.
    """
    config = EnvironmentConfig(
        name="test_addcmd_fail",
        description="additional_commands failure",
        type="uv",
        dependency_file="\n",
        auto_build=True,
        build_dir=tmp_path / "test_addcmd_fail" / "uv",
        additional_commands=["false"],  # exits 1
    )

    parent_dir = config.build_dir.parent
    parent_dir.mkdir(parents=True, exist_ok=True)
    (parent_dir / "requirements.txt").write_text(config.dependency_file)

    with pytest.raises(RuntimeError, match="Additional command 1/1 failed"):
        await build_uv_environment(config)

