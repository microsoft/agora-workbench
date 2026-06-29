"""
Tool proxy generation for programmatic tool use.

This module generates instrumented Python proxy functions that get injected
into the Jupyter kernel, enabling agents to call tools directly in their
code while maintaining full observability through structured tool-call traces.
"""

import textwrap
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tool_registry import ToolDefinition, ToolRegistry

# ---------------------------------------------------------------------------
# Type-annotation mapping for generated code
# ---------------------------------------------------------------------------
_BUILTIN_TYPE_NAMES = {
    "str": "str",
    "int": "int",
    "float": "float",
    "bool": "bool",
    "dict": "dict",
    "list": "list",
}

# Flush snippet for server-side trace extraction
FLUSH_SNIPPET = (
    "import json as _fj; "
    "print(_fj.dumps(globals().get("
    "'_tool_call_log', "
    "type('_',(),{'flush':lambda s:[]})())"
    ".flush()))"
)


def _type_annotation(param_type: type) -> str:
    """Return the source-code annotation string for a parameter or return type."""
    name = param_type.__name__
    # For non-builtin types, emit a string literal (forward ref)
    # so the annotation does not require the type to be present at definition time.
    if name not in _BUILTIN_TYPE_NAMES:
        return repr(name)
    return _BUILTIN_TYPE_NAMES[name]


def _single_object_return_type(return_spec: list) -> type | None:
    """Return concrete return type when return_spec describes a single non-dict value."""
    if len(return_spec) != 1:
        return None
    rs = return_spec[0]
    try:
        if issubclass(rs.type, dict):
            return None
    except TypeError:
        if rs.type is dict:
            return None
    return rs.type


# ---------------------------------------------------------------------------
# 1. Tracing infrastructure
# ---------------------------------------------------------------------------


def generate_tracing_infrastructure_code() -> str:
    """Return Python source for ToolCallLog class + initialization.

    IMPORTANT: This code is exec'd inside the kernel, which may run Python 3.6+.
    Do not use syntax requiring a newer version (e.g., PEP 604 unions, walrus operator,
    match statements, PEP 585 built-in generics like list[int]).
    """
    return textwrap.dedent("""\
        import time, json, traceback

        class ToolCallLog:
            \"\"\"Accumulates structured tool-call records during a single execute_code invocation.\"\"\"

            def __init__(self):
                self._calls = []
                self._object_refs = {}
                self._next_ref = 1

            def record(self, tool_name, args, result, duration_ms, success, error=None):
                self._calls.append({
                    "tool_name": tool_name,
                    "args": self._safe_serialize(args),
                    "result": self._safe_serialize(result) if isinstance(result, dict) else self._safe_serialize_value(result),
                    "duration_ms": round(duration_ms, 2),
                    "success": success,
                    "error": error,
                    "timestamp": time.time(),
                })

            def flush(self):
                \"\"\"Return all accumulated records and reset.\"\"\"
                calls = self._calls
                self._calls = []
                self._object_refs = {}
                self._next_ref = 1
                return calls

            def _safe_serialize(self, obj):
                \"\"\"Make a JSON-safe copy. For dicts, process each value recursively.\"\"\"
                if isinstance(obj, dict):
                    return {k: self._safe_serialize(v) for k, v in obj.items()}
                if isinstance(obj, (list, tuple)):
                    return [self._safe_serialize(item) for item in obj]
                return self._safe_serialize_value(obj)

            def _safe_serialize_value(self, val):
                \"\"\"Serialize a single value safely.

                Non-serializable objects get stable identity refs (e.g. '<Flowsheet@1>')
                that persist across tool calls within the same execution, enabling
                trace correlation without polluting the tool interface with handle IDs.
                \"\"\"
                try:
                    json.dumps(val)
                    return val
                except (TypeError, ValueError):
                    obj_id = id(val)
                    if obj_id not in self._object_refs:
                        type_name = type(val).__name__
                        ref_num = self._next_ref
                        self._next_ref += 1
                        self._object_refs[obj_id] = f"<{type_name}@{ref_num}>"
                    return self._object_refs[obj_id]

        if '_tool_call_log' not in globals():
            globals()['_tool_call_log'] = ToolCallLog()
    """)


# ---------------------------------------------------------------------------
# 2. Tool proxies
# ---------------------------------------------------------------------------


def _build_proxy_source(tool_def: "ToolDefinition") -> str:  # noqa: C901 – generator is inherently branchy
    """Build the source code for a single tool's instrumented proxy function."""

    name = tool_def.name
    all_params = list(tool_def.required_parameters) + list(tool_def.optional_parameters)

    # --- signature parts ---
    sig_parts: list[str] = []
    for p in tool_def.required_parameters:
        ann = _type_annotation(p.type)
        sig_parts.append(f"{p.name}: {ann}")
    for p in tool_def.optional_parameters:
        ann = _type_annotation(p.type)
        if p.default is not None:
            sig_parts.append(f"{p.name}: {ann} = {p.default!r}")
        else:
            sig_parts.append(f"{p.name}: {ann} = None")

    signature = ", ".join(sig_parts)

    # --- docstring ---
    doc_lines = [tool_def.description, ""]
    if all_params:
        doc_lines.append("Args:")
        for p in all_params:
            ann = _type_annotation(p.type)
            desc = p.description or "No description."
            doc_lines.append(f"    {p.name} ({ann}): {desc}")
    single_return_type = _single_object_return_type(tool_def.return_spec)
    if tool_def.return_spec:
        doc_lines.append("")
        doc_lines.append("Returns:")
        if single_return_type is not None:
            rs = tool_def.return_spec[0]
            ann = _type_annotation(rs.type)
            desc = rs.description or "No description."
            doc_lines.append(f"    {ann}: {desc}")
        else:
            doc_lines.append("    dict with keys:")
            for rs in tool_def.return_spec:
                ann = _type_annotation(rs.type)
                desc = rs.description or "No description."
                doc_lines.append(f"        {rs.name} ({ann}): {desc}")
    docstring = "\n    ".join(doc_lines)

    # --- build body ---
    body_lines: list[str] = []

    # lazy import of the real implementation
    module_path = tool_def.module
    if not module_path:
        raise ValueError(
            f"Tool '{name}' has no resolved module path. "
            "Register it with ToolRegistry(package='...') or set module/module_override."
        )
    module_file = f"{module_path.replace('.', '/')}.py"
    import_error_message = (
        f"Could not import tool '{name}' from '{module_path}'. "
        f"Expected: function '{name}' in module '{module_file}'. "
        "Hint: set module or module_override on the ToolDefinition to use a custom path."
    )
    body_lines.append("try:")
    body_lines.append(f"    from {module_path} import {name} as _impl")
    body_lines.append("except ImportError as _import_error:")
    body_lines.append(f"    raise ImportError({import_error_message!r}) from _import_error")
    body_lines.append("_log = globals().get('_tool_call_log')")

    # snapshot args
    arg_dict_entries = ", ".join(f"'{p.name}': {p.name}" for p in all_params)
    body_lines.append(f"_args = {{{arg_dict_entries}}}")

    # kwargs for the call (same names)
    kwargs_entries = ", ".join(f"{p.name}={p.name}" for p in all_params)

    # timing + call
    body_lines.append("import time as _time")
    body_lines.append("_t0 = _time.perf_counter()")
    body_lines.append("try:")
    body_lines.append(f"    _result = _impl({kwargs_entries})")
    body_lines.append("    _duration = (_time.perf_counter() - _t0) * 1000")
    body_lines.append("    if _log is not None:")
    body_lines.append("        _log.record({name!r}, _args, _result, _duration, success=True)".format(name=name))
    body_lines.append("    return _result")

    # exception handling
    body_lines.append("except Exception as _e:")
    body_lines.append("    _duration = (_time.perf_counter() - _t0) * 1000")
    body_lines.append("    if _log is not None:")
    body_lines.append(
        "        _log.record({name!r}, _args, {{}}, _duration, success=False, "
        'error=f"{{type(_e).__name__}}: {{_e}}")'.format(name=name)
    )
    body_lines.append("    raise")

    indented_body = "\n    ".join(body_lines)

    return f'def {name}({signature}):\n    """{docstring}\n    """\n    {indented_body}\n'


def generate_tool_proxies(tool_registry: "ToolRegistry") -> str:
    """Return Python source defining instrumented wrapper functions for all registered tools."""
    blocks: list[str] = []
    for tool_def in tool_registry.tools:
        blocks.append(_build_proxy_source(tool_def))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# 3. list_tools() helper
# ---------------------------------------------------------------------------


def generate_list_tools_code(tool_registry: "ToolRegistry") -> str:
    """Return Python source for a ``list_tools()`` kernel function."""
    tool_lines: list[str] = []

    for td in tool_registry.tools:
        # Build signature string
        sig_parts: list[str] = []
        for p in td.required_parameters:
            ann = _type_annotation(p.type)
            sig_parts.append(f"{p.name}: {ann}")
        for p in td.optional_parameters:
            ann = _type_annotation(p.type)
            if p.default is not None:
                sig_parts.append(f"{p.name}: {ann} = {p.default!r}")
            else:
                sig_parts.append(f"{p.name}: {ann} = None")
        sig = ", ".join(sig_parts)

        # Return type hint
        single_return_type = _single_object_return_type(td.return_spec)
        if single_return_type is not None:
            ret = _type_annotation(single_return_type)
        elif td.return_spec:
            ret = "dict"
        else:
            ret = "None"

        header = f"{td.name}({sig}) -> {ret}"
        desc = td.description.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
        tool_lines.append(f'    print("  {header}")')
        tool_lines.append(f'    print("    {desc}")')
        tool_lines.append("    print()")

    body = "\n".join(tool_lines)
    return (
        f'def list_tools():\n    """Print a catalog of all available tools."""\n    print("Available tools:")\n{body}\n'
    )
