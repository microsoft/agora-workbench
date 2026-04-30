"""Tests for tool proxy generation."""

import json
import sys
import types
from io import StringIO

import pytest

from ..code_execution.tool_proxy import (
    FLUSH_SNIPPET,
    generate_list_tools_code,
    generate_tool_proxies,
    generate_tracing_infrastructure_code,
)
from ..code_execution.tool_registry import (
    ReturnSpec,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_simple_tool(name="my_tool", module="mock_tools", description="A simple tool."):
    """Create a minimal ToolDefinition for testing."""
    return ToolDefinition(
        name=name,
        description=description,
        required_parameters=[
            ToolParameter(name="x", type=str, description="First arg"),
            ToolParameter(name="y", type=int, description="Second arg"),
        ],
        optional_parameters=[
            ToolParameter(name="z", type=float, description="Optional arg", default=1.5),
        ],
        return_spec=[
            ReturnSpec(name="result", type=dict, description="The result"),
        ],
        module=module,
    )


def _make_handle_tool():
    """Create a ToolDefinition with non-builtin type parameters and returns."""

    class FakeFlowsheet:
        pass

    return ToolDefinition(
        name="solve_flowsheet",
        description="Solve the flowsheet.",
        required_parameters=[
            ToolParameter(name="flowsheet", type=FakeFlowsheet, description="The flowsheet object"),
        ],
        optional_parameters=[],
        return_spec=[
            ReturnSpec(name="solved", type=FakeFlowsheet, description="Solved flowsheet"),
            ReturnSpec(name="summary", type=dict, description="Summary"),
        ],
        module="mock_tools",
    )


def _exec_infrastructure(namespace=None):
    """Execute the tracing infrastructure code and return the namespace."""
    if namespace is None:
        namespace = {"__builtins__": __builtins__}
    exec(generate_tracing_infrastructure_code(), namespace)
    return namespace


def _registry_with(*tools):
    """Build a ToolRegistry with the given tool definitions."""
    reg = ToolRegistry()
    for t in tools:
        reg.register_tool(t)
    return reg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tracing_infrastructure_code_is_valid_python():
    """Generate the code, exec() it, verify _tool_call_log exists and has correct methods."""
    code = generate_tracing_infrastructure_code()
    ns = {"__builtins__": __builtins__}
    exec(code, ns)

    log = ns["_tool_call_log"]
    assert log is not None
    assert hasattr(log, "record")
    assert callable(log.record)
    assert hasattr(log, "flush")
    assert callable(log.flush)


@pytest.mark.unit
def test_tracing_infrastructure_idempotent():
    """Execute the code twice, verify only one ToolCallLog instance exists."""
    code = generate_tracing_infrastructure_code()
    ns = {"__builtins__": __builtins__}
    exec(code, ns)
    first_log = ns["_tool_call_log"]

    # Execute again in the same namespace
    exec(code, ns)
    second_log = ns["_tool_call_log"]

    assert first_log is second_log


@pytest.mark.unit
def test_tool_call_log_record_and_flush():
    """Record two calls, flush, verify structure, second flush returns empty."""
    ns = _exec_infrastructure()
    log = ns["_tool_call_log"]

    log.record("tool_a", {"x": 1}, {"out": 2}, 12.34, success=True)
    log.record("tool_b", {"y": "hello"}, {}, 56.78, success=False, error="ValueError: bad")

    calls = log.flush()
    assert len(calls) == 2

    assert calls[0]["tool_name"] == "tool_a"
    assert calls[0]["args"] == {"x": 1}
    assert calls[0]["result"] == {"out": 2}
    assert calls[0]["duration_ms"] == 12.34
    assert calls[0]["success"] is True
    assert calls[0]["error"] is None
    assert "timestamp" in calls[0]

    assert calls[1]["tool_name"] == "tool_b"
    assert calls[1]["success"] is False
    assert calls[1]["error"] == "ValueError: bad"

    # Second flush should be empty
    assert log.flush() == []


@pytest.mark.unit
def test_safe_serialize_handles_non_serializable():
    """Non-serializable objects get stable identity ref format."""
    ns = _exec_infrastructure()
    log = ns["_tool_call_log"]

    class Custom:
        def __repr__(self):
            return "Custom(special)"

    obj = Custom()
    log.record("tool_x", {"obj": obj}, {"nested": {"inner": obj}}, 1.0, success=True)
    calls = log.flush()

    # Same object should get the same identity ref everywhere
    assert calls[0]["args"]["obj"] == "<Custom@1>"
    assert calls[0]["result"]["nested"]["inner"] == "<Custom@1>"


@pytest.mark.unit
def test_generate_tool_proxies_valid_python():
    """Generated proxy code must be valid Python (compilable)."""
    reg = _registry_with(_make_simple_tool("tool_a"), _make_simple_tool("tool_b", module="other_mod"))
    code = generate_tool_proxies(reg)
    # Should not raise
    compile(code, "<generated>", "exec")


@pytest.mark.unit
def test_generate_tool_proxies_function_names():
    """Generated proxy code defines the correct function names in namespace."""
    tool_a = _make_simple_tool("tool_alpha", module="mock_tools")
    tool_b = _make_simple_tool("tool_beta", module="mock_tools")
    reg = _registry_with(tool_a, tool_b)

    infra_code = generate_tracing_infrastructure_code()
    proxy_code = generate_tool_proxies(reg)

    # Inject mock module
    mock_mod = types.ModuleType("mock_tools")
    mock_mod.tool_alpha = lambda x, y, z=1.5: {"success": True}
    mock_mod.tool_beta = lambda x, y, z=1.5: {"success": True}
    sys.modules["mock_tools"] = mock_mod

    try:
        ns = {"__builtins__": __builtins__}
        exec(infra_code, ns)
        exec(proxy_code, ns)

        assert "tool_alpha" in ns
        assert callable(ns["tool_alpha"])
        assert "tool_beta" in ns
        assert callable(ns["tool_beta"])

        # Actually call one to verify it works end-to-end
        result = ns["tool_alpha"](x="hello", y=42)
        assert result == {"success": True}

        calls = ns["_tool_call_log"].flush()
        assert len(calls) == 1
        assert calls[0]["tool_name"] == "tool_alpha"
        assert calls[0]["success"] is True
    finally:
        del sys.modules["mock_tools"]


@pytest.mark.unit
def test_generate_tool_proxies_with_handle_params():
    """Handle parameters should use the actual Python type name as forward ref."""
    tool = _make_handle_tool()
    reg = _registry_with(tool)
    code = generate_tool_proxies(reg)

    # The annotation should be a string forward ref (not bare class name)
    assert "flowsheet: 'FakeFlowsheet'" in code


@pytest.mark.unit
def test_generate_tool_proxies_returns_raw_objects():
    """Tools with non-builtin return types should NOT include ObjectRegistry storage logic.

    The proxy returns the raw result from _impl() without handle wrapping.
    """
    tool = _make_handle_tool()
    reg = _registry_with(tool)
    code = generate_tool_proxies(reg)

    assert "_object_registry" not in code
    assert "_handles" not in code
    assert "uuid" not in code


@pytest.mark.unit
def test_tracing_identity_refs_for_non_serializable():
    """Non-serializable objects get stable <Type@N> identity refs in traces."""
    ns = _exec_infrastructure()
    log = ns["_tool_call_log"]

    class Flowsheet:
        pass

    fs = Flowsheet()

    # Record two calls using the same object
    log.record("create_flowsheet", {}, {"flowsheet": fs, "name": "test"}, 10.0, success=True)
    log.record("solve_flowsheet", {"flowsheet": fs}, {"converged": True}, 20.0, success=True)

    calls = log.flush()

    # The flowsheet should get a stable identity ref
    ref = calls[0]["result"]["flowsheet"]
    assert ref == "<Flowsheet@1>"

    # Same object in args of second call should get the same ref
    assert calls[1]["args"]["flowsheet"] == "<Flowsheet@1>"


@pytest.mark.unit
def test_tracing_identity_refs_different_objects():
    """Different non-serializable objects get different identity refs."""
    ns = _exec_infrastructure()
    log = ns["_tool_call_log"]

    class Widget:
        pass

    w1 = Widget()
    w2 = Widget()

    log.record("make", {}, {"a": w1, "b": w2}, 5.0, success=True)
    calls = log.flush()

    assert calls[0]["result"]["a"] == "<Widget@1>"
    assert calls[0]["result"]["b"] == "<Widget@2>"


@pytest.mark.unit
def test_tracing_identity_refs_reset_on_flush():
    """Identity refs reset after flush(), so a new execution starts from @1."""
    ns = _exec_infrastructure()
    log = ns["_tool_call_log"]

    class Thing:
        pass

    log.record("t1", {}, {"obj": Thing()}, 1.0, success=True)
    calls1 = log.flush()
    assert calls1[0]["result"]["obj"] == "<Thing@1>"

    # After flush, new objects start from @1 again
    log.record("t2", {}, {"obj": Thing()}, 1.0, success=True)
    calls2 = log.flush()
    assert calls2[0]["result"]["obj"] == "<Thing@1>"


@pytest.mark.unit
def test_generate_list_tools_code():
    """list_tools() should print tool names, signatures and descriptions."""
    tool_a = _make_simple_tool("create_flowsheet", description="Create a new flowsheet.")
    tool_b = _make_simple_tool("solve_flowsheet", description="Solve the flowsheet.")
    reg = _registry_with(tool_a, tool_b)

    code = generate_list_tools_code(reg)
    ns = {"__builtins__": __builtins__}
    exec(code, ns)

    assert "list_tools" in ns

    # Capture stdout
    old_stdout = sys.stdout
    sys.stdout = captured = StringIO()
    try:
        ns["list_tools"]()
    finally:
        sys.stdout = old_stdout

    output = captured.getvalue()
    assert "Available tools:" in output
    assert "create_flowsheet" in output
    assert "solve_flowsheet" in output
    assert "x: str" in output
    assert "Create a new flowsheet." in output


@pytest.mark.unit
def test_flush_snippet_safe_without_log():
    """FLUSH_SNIPPET must safely return [] when no _tool_call_log exists."""
    ns = {"__builtins__": __builtins__}

    old_stdout = sys.stdout
    sys.stdout = captured = StringIO()
    try:
        exec(FLUSH_SNIPPET, ns)
    finally:
        sys.stdout = old_stdout

    result = json.loads(captured.getvalue().strip())
    assert result == []


@pytest.mark.unit
def test_flush_snippet_extracts_log():
    """FLUSH_SNIPPET must extract recorded tool calls as JSON on stdout."""
    ns = _exec_infrastructure()
    log = ns["_tool_call_log"]
    log.record("test_tool", {"a": 1}, {"b": 2}, 5.0, success=True)

    old_stdout = sys.stdout
    sys.stdout = captured = StringIO()
    try:
        exec(FLUSH_SNIPPET, ns)
    finally:
        sys.stdout = old_stdout

    records = json.loads(captured.getvalue().strip())
    assert len(records) == 1
    assert records[0]["tool_name"] == "test_tool"
    assert records[0]["args"] == {"a": 1}
    assert records[0]["success"] is True
