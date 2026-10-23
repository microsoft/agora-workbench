"""
Tests for data models.
"""

import json

import pytest
from pydantic import ValidationError

from ..code_execution_models import CodeExecutionResult, ServerConfig


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


def test_code_execution_result_displays_default_empty():
    """displays defaults to an empty list and round-trips through model_dump."""
    result = CodeExecutionResult(stdout="ok")
    assert result.displays == []
    dumped = result.model_dump()
    assert dumped["displays"] == []


def test_code_execution_result_displays_excluded():
    """Agent-facing JSON serialization can drop displays to keep context small.

    The activity-publish path keeps displays; the agent return path uses
    ``model_dump(exclude={'displays'})`` because matplotlib PNG payloads
    are several hundred KB and would blow the agent's token budget.
    """
    big_png = "x" * 50000
    result = CodeExecutionResult(
        stdout="<Figure>",
        displays=[{"mime_type": "image/png", "data": big_png, "metadata": {}}],
    )
    full = result.model_dump()
    assert len(full["displays"]) == 1
    assert full["displays"][0]["data"] == big_png

    agent_view = result.model_dump(exclude={"displays"})
    assert "displays" not in agent_view
    assert agent_view["stdout"] == "<Figure>"


def test_server_config_required_fields():
    """Test that ServerConfig requires essential fields."""
    with pytest.raises(ValidationError):
        ServerConfig()  # type: ignore[call-arg]


def test_server_config_serialization():
    """Test that ServerConfig can be serialized."""
    config = ServerConfig(
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


def _server_config(**overrides) -> ServerConfig:
    return ServerConfig(name="env_test", description="Env test", type="uv", dependency_file="", **overrides)


def test_server_config_accepts_domain_kernel_environment():
    config = _server_config(kernel_environment={"LD_LIBRARY_PATH": "/opt/ipopt/lib", "DWSIM_PATH": "/opt/dwsim/"})

    assert config.kernel_environment["LD_LIBRARY_PATH"] == "/opt/ipopt/lib"


@pytest.mark.parametrize(
    "name",
    ["PATH", "MCP_ASSET_CACHE_DIR", "USER_IDENTITY", "PYTHONPATH", "PYTHONSTARTUP", "LD_PRELOAD", "path"],
)
def test_server_config_rejects_reserved_kernel_environment_names(name):
    with pytest.raises(ValidationError, match="may not set"):
        _server_config(kernel_environment={name: "/x"})


@pytest.mark.parametrize("name", ["", "1BAD", "BAD-NAME", "A=B"])
def test_server_config_rejects_invalid_kernel_environment_names(name):
    with pytest.raises(ValidationError, match="not a valid environment variable name"):
        _server_config(kernel_environment={name: "x"})


def test_server_config_rejects_sidecar_url_kernel_environment_name():
    from ..code_execution_models import SidecarConfig

    with pytest.raises(ValidationError, match="MODEL_URL"):
        _server_config(
            sidecars=[SidecarConfig(name="model", command=["serve"], url_env_var="MODEL_URL", port=9100)],
            kernel_environment={"MODEL_URL": "http://elsewhere"},
        )
