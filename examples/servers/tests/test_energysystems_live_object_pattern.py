from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
ENERGYSYSTEMS_ROOT = REPO_ROOT / "examples" / "servers" / "energysystems"
TOOLS_SRC = ENERGYSYSTEMS_ROOT / "energysystems_tools" / "src" / "energysystems_tools"
DEFINITIONS_PATH = ENERGYSYSTEMS_ROOT / "tools" / "definitions.py"


def _load_definitions_module():
    spec = importlib.util.spec_from_file_location("energysystems_definitions", DEFINITIONS_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_energysystems_tools_remove_builtin_registry_pattern():
    tool_files = sorted(TOOLS_SRC.glob("*.py"))
    assert tool_files, "Expected energy systems tool source files"

    for tool_file in tool_files:
        source = tool_file.read_text(encoding="utf-8")
        assert "import builtins" not in source
        assert "_pypsa_" not in source


def test_energysystems_tool_definitions_accept_pypsa_network_objects():
    definitions = _load_definitions_module()

    tools_with_network_input = [
        definitions.add_components,
        definitions.add_time_series,
        definitions.run_power_flow,
        definitions.run_optimal_power_flow,
        definitions.run_capacity_expansion,
        definitions.analyze_costs,
        definitions.analyze_topology,
    ]

    for tool_def in tools_with_network_input:
        parameter = tool_def.required_parameters[0]
        assert parameter.name == "network"
        assert parameter.type.__module__ == "pypsa"
        assert parameter.type.__name__ == "Network"

    network_return = definitions.define_network.return_spec[0]
    assert network_return.name == "network"
    assert network_return.type.__module__ == "pypsa"
    assert network_return.type.__name__ == "Network"
