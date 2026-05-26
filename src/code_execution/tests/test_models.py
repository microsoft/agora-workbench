"""
Tests for data models.
"""

import json

import pytest
from pydantic import ValidationError

from ..code_execution_models import CodeExecutionResult, EnvironmentConfig


def test_code_execution_result_defaults():
    """Test CodeExecutionResult with default values."""
    result = CodeExecutionResult()

    assert result.stdout == ""
    assert result.stderr == ""
    assert result.execution_time == 0.0
    assert result.success is True
    assert result.error is None


def test_code_execution_result_with_values():
    """Test CodeExecutionResult with explicit values."""
    result = CodeExecutionResult(
        stdout="output",
        stderr="error output",
        execution_time=1.5,
        success=False,
        error="Something went wrong",
    )

    assert result.stdout == "output"
    assert result.stderr == "error output"
    assert result.execution_time == 1.5
    assert result.success is False
    assert result.error == "Something went wrong"


def test_code_execution_result_serialization():
    """Test that CodeExecutionResult can be serialized to JSON."""
    result = CodeExecutionResult(
        stdout="test output",
        stderr="test error",
        execution_time=2.5,
        success=True,
    )

    json_str = result.model_dump_json()
    data = json.loads(json_str)

    assert data["stdout"] == "test output"
    assert data["stderr"] == "test error"
    assert data["execution_time"] == 2.5
    assert data["success"] is True


def test_environment_config_required_fields():
    """Test that EnvironmentConfig requires essential fields."""
    with pytest.raises(ValidationError):
        EnvironmentConfig()  # type: ignore[call-arg]


def test_environment_config_serialization():
    """Test that EnvironmentConfig can be serialized."""
    config = EnvironmentConfig(
        name="serialize_test",
        description="Serialization test",
        type="uv",
        dependency_file="numpy\n",
    )

    json_str = config.model_dump_json()
    data = json.loads(json_str)

    assert data["name"] == "serialize_test"
    assert data["type"] == "uv"
    assert data["auto_build"] is True
