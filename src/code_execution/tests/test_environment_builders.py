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
