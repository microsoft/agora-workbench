"""Default code-execution behavior for ``CodeExecutionServer``."""

import ast
import json
import logging
from typing import Callable, Optional, TYPE_CHECKING

from fastapi import HTTPException
from fastmcp import Context

from .code_execution_models import CodeExecutionResult
from .code_extraction import (
    ASSET_PATHLIB_IMPORT,
    _VAR_PREFIX_ASSET,
    build_asset_preamble,
    extract_references,
    generate_safe_varname,
    replace_literals_in_source,
)
from .sessions import get_current_user_identity, set_current_session

if TYPE_CHECKING:
    from . import CodeExecutionServer

LOGGER = logging.getLogger(__name__)


# block file system calls that agent might use to cross session isolation boundary
_OS_BLOCKED_FS_CALLS: set[str] = {
    "listdir",
    "scandir",
    "walk",
    "fwalk",
    "getcwd",
    "chdir",
    "fchdir",
    "remove",
    "unlink",
    "rmdir",
    "removedirs",
    "rename",
    "renames",
    "replace",
    "link",
    "symlink",
    "readlink",
    "system",
}

_GLOB_BLOCKED_FS_CALLS: set[str] = {"glob", "iglob"}
_PATHLIB_BLOCKED_FS_CALLS: set[str] = {"iterdir", "rglob"}

_BLOCKED_FS_CALLS: set[str] = _OS_BLOCKED_FS_CALLS | _GLOB_BLOCKED_FS_CALLS | _PATHLIB_BLOCKED_FS_CALLS

# Constructors whose return values belong to a dangerous module family.
# Used by _build_taint_set to trace e.g. ``p = Path(...)`` back to "pathlib".
_DANGEROUS_CONSTRUCTORS: dict[str, str] = {
    "Path": "pathlib",
    "PurePath": "pathlib",
    "PosixPath": "pathlib",
    "WindowsPath": "pathlib",
    "PurePosixPath": "pathlib",
    "PureWindowsPath": "pathlib",
}

# block calls and modules that can be used to directly circumvent the other blocks
_BLOCKED_DANGEROUS_CALLS: set[str] = {
    "__import__",
    "eval",
    "exec",
    "compile",
    "getattr",
}

_BLOCKED_MODULES: set[str] = {
    "subprocess",
    "shutil",
    "ftplib",
    "telnetlib",
    "smtplib",
    "http.server",
    "socketserver",
}

# Allowed absolute-path prefixes.  Paths outside these are rejected.
# /tmp covers the data-lake temp directories
_ALLOWED_PATH_PREFIXES: tuple[str, ...] = ("/tmp",)


def validate_code(server: "CodeExecutionServer", code: str) -> tuple[bool, Optional[str]]:
    """
    Validate code before execution.

        This implementation performs three layers of screening:

        1. **Blocked modules** – imports of modules listed in
           ``_BLOCKED_MODULES`` (e.g. ``subprocess``, ``shutil``) are
           rejected outright.
        2. **Blocked function calls** – calls to filesystem-exploration
           or destructive functions listed in ``_BLOCKED_FS_CALLS``
           (e.g. ``os.listdir``, ``Path.iterdir``) are rejected.
        3. **Path restriction** – string literals that look like absolute
           paths (starting with ``/``) are checked against
           ``_ALLOWED_PATH_PREFIXES``.  References to paths outside
           those prefixes are rejected.

        Override in a subclass to relax, tighten, or replace these
        checks entirely.

        Returns:
            ``(True, None)`` when the code is acceptable, or
            ``(False, error_message)`` describing the violation.
    """
    if not code or not code.strip():
        return False, "Code cannot be empty"

    # --- AST-based analysis ---
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # Let the kernel report the syntax error with a proper traceback;
        # do not block execution here.
        return True, None

    blocked_calls = server._BLOCKED_FS_CALLS
    blocked_dangerous_calls = getattr(server, "_BLOCKED_DANGEROUS_CALLS", set())
    blocked_modules = server._BLOCKED_MODULES
    allowed_prefixes = server._ALLOWED_PATH_PREFIXES

    module_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name.split(".")[0]
                module_aliases[local_name] = alias.name

    # Lightweight taint pass: trace variables back to dangerous sources
    tainted_vars: dict[str, str] = _build_taint_set(tree, module_aliases)

    # Pre-collect "/" constant node IDs that are safely used as string-method
    # separators (e.g. s.split("/", 1) or "/".join(parts)).  These are not
    # filesystem paths and must not be rejected by the absolute-path check.
    str_delim_methods = frozenset(
        {"split", "rsplit", "partition", "rpartition", "join", "strip", "lstrip", "rstrip", "replace"}
    )
    safe_slash_ids: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            attr = n.func.attr
            if attr in str_delim_methods:
                # e.g. s.split("/", 1) — "/" is in args
                for arg in n.args:
                    if isinstance(arg, ast.Constant) and arg.value == "/":
                        safe_slash_ids.add(id(arg))
                # e.g. "/".join(parts) — "/" is the object the method is called on
                if isinstance(n.func.value, ast.Constant) and n.func.value.value == "/":
                    safe_slash_ids.add(id(n.func.value))

    for node in ast.walk(tree):
        # --- 1. Blocked imports ---
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                module_prefixes = [".".join(parts[:i]) for i in range(1, len(parts) + 1)]
                if any(prefix in blocked_modules for prefix in module_prefixes):
                    return False, (
                        f"Importing '{alias.name}' is not allowed in the code execution environment. "
                        f"Blocked modules: {', '.join(sorted(blocked_modules))}"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                # Check both the root package and all dotted prefixes
                # so that e.g. "http.server" matches "from http.server import ...".
                parts = node.module.split(".")
                module_prefixes = [".".join(parts[:i]) for i in range(1, len(parts) + 1)]
                if any(prefix in blocked_modules for prefix in module_prefixes):
                    return False, (
                        f"Importing from '{node.module}' is not allowed in the code execution environment. "
                        f"Blocked modules: {', '.join(sorted(blocked_modules))}"
                    )

        # --- 2. Blocked function calls ---
        elif isinstance(node, ast.Call):
            func_name = _resolve_call_name(node)
            if _is_safe_imported_module_call(node, func_name, module_aliases, tainted_vars):
                continue

            if func_name and (func_name in blocked_calls or func_name in blocked_dangerous_calls):
                return False, (
                    f"Call to '{func_name}' is not allowed — filesystem exploration/manipulation "
                    f"and dangerous metaprogramming functions are blocked in the code execution environment."
                )

        # --- 3. Absolute-path restriction ---
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            val = node.value.strip()
            if _contains_parent_traversal(val):
                return False, "Relative path traversal using '..' is not allowed in the code execution environment."

            # Skip '/' only when it is a safe string-method delimiter
            # (e.g. s.split("/", 1)).  All other absolute paths are checked.
            if val.startswith("/") and not val.startswith("//") and id(node) not in safe_slash_ids:
                # Looks like an absolute POSIX path; check prefixes.
                if not any(val == prefix or val.startswith(prefix + "/") for prefix in allowed_prefixes):
                    return False, (
                        f"Absolute path '{val}' is outside the allowed directories. "
                        f"Permitted prefixes: {', '.join(allowed_prefixes)}"
                    )

        elif isinstance(node, (ast.BinOp, ast.JoinedStr, ast.Call)):
            for candidate in _extract_dynamic_path_candidates(node, module_aliases):
                val = candidate.strip()
                if _contains_parent_traversal(val):
                    return False, "Relative path traversal using '..' is not allowed in the code execution environment."

                if val.startswith("/") and not val.startswith("//"):
                    if not any(val == prefix or val.startswith(prefix + "/") for prefix in allowed_prefixes):
                        return False, (
                            f"Absolute path '{val}' is outside the allowed directories. "
                            f"Permitted prefixes: {', '.join(allowed_prefixes)}"
                        )

    return True, None


def _resolve_call_name(node: ast.Call) -> Optional[str]:
    """
    Extract the leaf function name from a Call node.

        Returns the rightmost name in a dotted call chain, e.g.
        ``os.path.join(...)`` → ``"join"``, ``listdir(...)`` → ``"listdir"``.
        Returns ``None`` for calls that cannot be statically resolved
        (e.g. computed attribute access, subscript calls).
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _contains_parent_traversal(path_literal: str) -> bool:
    """Return True when a string literal contains parent-directory traversal segments."""
    if not path_literal:
        return False
    # Ignore URL-like strings; traversal rules are for filesystem paths.
    if "://" in path_literal or path_literal.startswith("//"):
        return False

    normalized = path_literal.replace("\\", "/")
    return ".." in normalized.split("/")


def _is_safe_imported_module_call(
    node: ast.Call,
    func_name: Optional[str],
    module_aliases: dict[str, str],
    tainted_vars: dict[str, str],
) -> bool:
    """Return True when an attribute call is safe (receiver does not trace back to a dangerous module).

    Only returns False when the receiver can be traced back to os, glob, or
    pathlib — either via import aliases or via variable assignments
    (e.g. ``p = Path(...)``).  Calls on unknown variables (e.g.
    ``df.rename()``, ``network.remove()``) are assumed safe.
    """
    if not func_name or not isinstance(node.func, ast.Attribute):
        return False
    if not isinstance(node.func.value, ast.Name):
        return True

    base_name = node.func.value.id

    # Check taint: did this variable trace back to a dangerous library?
    taint_family = tainted_vars.get(base_name)
    if taint_family is not None:
        if taint_family == "os" and func_name in _OS_BLOCKED_FS_CALLS:
            return False
        if taint_family == "glob" and func_name in _GLOB_BLOCKED_FS_CALLS:
            return False
        if taint_family == "pathlib" and func_name in _PATHLIB_BLOCKED_FS_CALLS:
            return False
        return True

    # Check import aliases
    canonical_module = module_aliases.get(base_name)
    if not canonical_module:
        # Even without an explicit import, recognise well-known dangerous
        # module names (the import may have happened in a previous cell).
        if base_name in ("os", "glob", "pathlib"):
            canonical_module = base_name
        else:
            return True

    # Only block if the import traces to a dangerous stdlib family.
    if func_name in _OS_BLOCKED_FS_CALLS:
        return not canonical_module.startswith("os")
    if func_name in _GLOB_BLOCKED_FS_CALLS:
        return not canonical_module.startswith("glob")
    if func_name in _PATHLIB_BLOCKED_FS_CALLS:
        return not canonical_module.startswith("pathlib")
    return False


def _build_taint_set(tree: ast.AST, module_aliases: dict[str, str]) -> dict[str, str]:
    """Trace variables back to their original library.

    Returns a dict mapping variable names to their "family" (e.g. "os",
    "pathlib").  A variable is tainted when assigned from:
      - A dangerous constructor:  ``p = Path(...)``  → "pathlib"
      - Another tainted variable: ``q = p``           → inherits p's family
      - A tainted attribute:      ``pp = os.path``    → "os"
    """
    _DANGEROUS_FAMILIES = {"os", "glob", "pathlib"}
    tainted: dict[str, str] = {}

    # Seed from imports
    for local_name, canonical in module_aliases.items():
        root = canonical.split(".")[0]
        if root in _DANGEROUS_FAMILIES:
            tainted[local_name] = root

    # Propagate through assignments (fixed-point, max 3 passes)
    for _ in range(3):
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            target = node.targets[0].id
            family = _resolve_rhs_family(node.value, tainted)
            if family and tainted.get(target) != family:
                tainted[target] = family
                changed = True
        if not changed:
            break

    return tainted


def _resolve_rhs_family(node: ast.AST, tainted: dict[str, str]) -> Optional[str]:
    """Determine which dangerous library family an assignment RHS traces back to."""
    # ``p = Path(...)`` or ``p = pathlib.Path(...)``
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in _DANGEROUS_CONSTRUCTORS:
            return _DANGEROUS_CONSTRUCTORS[func.id]
        if isinstance(func, ast.Attribute) and func.attr in _DANGEROUS_CONSTRUCTORS:
            root = func.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in tainted:
                return tainted[root.id]

    # ``q = p``
    if isinstance(node, ast.Name) and node.id in tainted:
        return tainted[node.id]

    # ``pp = os.path``
    if isinstance(node, ast.Attribute):
        root = node.value
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name) and root.id in tainted:
            return tainted[root.id]

    return None


def _resolve_static_string(node: ast.AST) -> Optional[str]:
    """Best-effort static resolution for common string-construction patterns."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value

    if isinstance(node, ast.JoinedStr):
        # Keep structure while marking unknown runtime fragments.
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append("{expr}")
            else:
                return None
        return "".join(parts)

    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Add):
            # Handle literal concatenation like '/etc' + '/passwd'.
            left = _resolve_static_string(node.left)
            right = _resolve_static_string(node.right)
            if left is not None and right is not None:
                return left + right
        if isinstance(node.op, ast.Mod):
            # Treat "'/etc/%s' % name" as a path rooted at '/etc/'.
            left = _resolve_static_string(node.left)
            if left is not None:
                return left

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        base = _resolve_static_string(node.func.value)
        if base is not None:
            return base

    return None


def _extract_dynamic_path_candidates(node: ast.AST, module_aliases: dict[str, str]) -> list[str]:
    """Extract path-like string candidates from dynamic expressions."""
    candidates: list[str] = []

    resolved = _resolve_static_string(node)
    if resolved is not None:
        candidates.append(resolved)

    # Handle os.path.join(...) when we can statically resolve enough pieces.
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join":
        if isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "path":
            base_obj = node.func.value.value
            if isinstance(base_obj, ast.Name):
                canonical = module_aliases.get(base_obj.id, base_obj.id)
                if canonical == "os":
                    part_values = [_resolve_static_string(arg) for arg in node.args]
                    if part_values and part_values[0] is not None:
                        joined = part_values[0]
                        for part in part_values[1:]:
                            if part is None:
                                # Stop when the join becomes runtime-dependent.
                                break
                            if joined.endswith("/"):
                                joined = joined.rstrip("/")
                            joined = joined + "/" + part.lstrip("/")
                        candidates.append(joined)

    return candidates


def _build_disallowed_actions_description(server: "CodeExecutionServer") -> str:
    """Build a dynamic description of disallowed actions for MCP tool metadata."""
    blocked_modules = sorted(str(module) for module in server._BLOCKED_MODULES)
    blocked_calls = sorted(str(call) for call in server._BLOCKED_FS_CALLS)
    blocked_dangerous_calls = sorted(str(call) for call in getattr(server, "_BLOCKED_DANGEROUS_CALLS", set()))
    allowed_prefixes = sorted(str(prefix) for prefix in server._ALLOWED_PATH_PREFIXES)

    modules_text = ", ".join(blocked_modules) if blocked_modules else "(none)"
    calls_text = ", ".join(blocked_calls) if blocked_calls else "(none)"
    dangerous_calls_text = ", ".join(blocked_dangerous_calls) if blocked_dangerous_calls else "(none)"
    allowed_paths_text = ", ".join(allowed_prefixes) if allowed_prefixes else "(none)"

    return (
        "Disallowed actions:\n"
        f"- Blocked imports: {modules_text}\n"
        f"- Blocked calls: {calls_text}\n"
        f"- Blocked dangerous calls: {dangerous_calls_text}\n"
        "- Relative path traversal using '..' segments is blocked.\n"
        "- Absolute paths are restricted to allowed prefixes only. "
        f"Allowed prefixes: {allowed_paths_text}\n\n"
        "Auto-resolution of handles and assets:\n"
        "Handle IDs (h_xxxxxxxxxxxx) and asset tags (<type>id</type>) embedded as "
        "string literals in code are automatically detected and resolved. "
        "References must appear as complete string literals in assignments, "
        "function arguments, return values, or within list/dict/tuple/set literals. "
        "They are replaced with variables that hold the resolved object (for handles) "
        "or a Path to the cached file (for assets)."
    )


def build_tool(server: "CodeExecutionServer") -> Callable:
    """Setup the general code execution tool."""

    async def execute_code_tool(
        ctx: Context,
        code: str,
        description: str = "",
        timeout: int = server.default_timeout,
        background: bool = False,
    ) -> str:
        """
        Execute Python code in the isolated environment with persistent state.

        Before executing, set ``description`` to a one-sentence summary of what
        this code does and why — for example,
        ``"Compute molecular descriptors for the candidate library"``. The
        description is surfaced to the end-user watching the server's activity
        feed so they can follow along with the agent's reasoning. Keep it short
        and human-readable; leave it empty only for trivial follow-ups (e.g. a
        ``print(result)`` after a previous call).

        Handle IDs (``h_xxxxxxxxxxxx``) and asset tags (``<type>id</type>``)
        embedded as string literals in the code are automatically detected and
        resolved.  Each matched literal is replaced with a synthetic variable
        that holds the resolved object (for handles) or a ``Path`` to the
        cached file (for assets).  References must appear as complete string
        literals in assignments, function arguments, return statements, or
        container literals (list/dict/tuple/set).

        Args:
            code: Python code to execute
            description: One-sentence summary of what the code does (shown in
                the activity UI). Optional but strongly recommended.
            timeout: Execution timeout in seconds (max: {max_timeout})
            background: When True, submit code to run in the same session kernel
                and return immediately with a job handle.

        Returns:
            Execution result with stdout, stderr, status, and session_id
        """
        # Extract session_id from fastmcp Context
        session_id = None
        if ctx:
            try:
                session_id = ctx.session_id
            except (RuntimeError, AttributeError):
                pass

        session = None
        try:
            # Restore auth ContextVars using the MCP transport session id
            server._restore_auth_context_for_mcp_session(session_id)

            session = await server._get_or_create_session(server.get_tool_name(), session_id=session_id)
            set_current_session(session)

            # Inject tool proxies on first use of this session
            await server._inject_tool_proxies(session.session_id)

            # Initialize namespace if new session
            if "namespace" not in session.data:
                session.data["namespace"] = {}
                LOGGER.info(f"Initialized new namespace for session {session.session_id}")

            # --- Auto-extract assets from code ---
            # Single-pass extraction: refs is the deduplicated list,
            # all_occurrences maps each value to every matching AST node,
            # code_names is the set of all identifiers in user code.
            refs, all_occurrences, code_names = extract_references(code)

            preamble_lines: list[str] = []
            all_replacements: list[tuple[ast.Constant, str]] = []
            asset_counter = 0
            has_assets = False

            # Build the set of names to avoid: names in user code + session namespace
            occupied_names = code_names
            namespace = session.data.get("namespace")
            if isinstance(namespace, dict):
                occupied_names.update(namespace.keys())

            for _, kind, value in refs:
                var_name, asset_counter = generate_safe_varname(
                    _VAR_PREFIX_ASSET,
                    asset_counter,
                    occupied_names,
                )
                has_assets = True

                # Resolve asset to cache path (fail fast)
                cache_path = await session.data_manager.get_cache_path(value)
                preamble_lines.extend(build_asset_preamble(var_name, str(cache_path)))
                LOGGER.debug(f"Auto-extracted asset '{value}' -> {var_name}")

                # Use pre-collected occurrences (no re-parse needed)
                for occ_node, _ in all_occurrences.get(value, []):
                    all_replacements.append((occ_node, var_name))

            if all_replacements:
                source_lines = code.splitlines(keepends=True)
                source_lines = replace_literals_in_source(source_lines, all_replacements)
                code = "".join(source_lines)

            if preamble_lines:
                # Emit the pathlib import once if any assets were extracted
                if has_assets:
                    preamble_lines.insert(0, ASSET_PATHLIB_IMPORT)
                preamble = "\n".join(preamble_lines) + "\n\n"
                code = preamble + code

            LOGGER.info(f"Auto-extraction: {asset_counter} asset(s)")

            # Clamp timeout to max
            if timeout <= 0:
                raise ValueError("Timeout must be greater than 0 seconds.")
            timeout = min(timeout, server.max_timeout)

            LOGGER.info(
                f"Executing code in {server.environment_config.name} environment "
                f"(session={session.session_id[:8]}, timeout={timeout}s)"
            )

            if background:
                job_result = await server._execute_code_background(code, timeout)
                if description:
                    job_result["description"] = description
                server.session_manager.update_session(session.session_id, session)
                server.activity_publisher.publish_nowait(
                    {
                        "type": "job_started",
                        "description": description,
                        "code": code,
                        "session_id": session.session_id,
                        "job_id": job_result.get("job_id"),
                    }
                )
                return json.dumps(job_result, indent=2)

            # Execute code with persistent namespace from session
            result = await server._execute_code_with_tracing(code, timeout)
            result.description = description

            # Save the session to persist the updated namespace
            server.session_manager.update_session(session.session_id, session)

            # Publish activity event (best-effort; no-op when ACTIVITY_UI_URL is unset).
            server.activity_publisher.publish_nowait(
                {
                    "type": "code_executed" if result.success else "code_failed",
                    "description": description,
                    "code": code,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "success": result.success,
                    "duration_ms": result.execution_time * 1000.0,
                    "tool_calls": [tc.model_dump() for tc in result.tool_calls],
                    "error": result.error,
                    "session_id": session.session_id,
                }
            )

            # Return result with session_id
            result_dict = result.model_dump()
            result_dict["session_id"] = session.session_id
            return json.dumps(result_dict, indent=2)
        except HTTPException as e:
            if e.status_code == 429 and isinstance(e.detail, dict):
                return json.dumps(e.detail, indent=2)
            LOGGER.error(f"Execution failed: {e}", exc_info=True)
            error_result = CodeExecutionResult(success=False, error=str(e), description=description)
            error_dict = error_result.model_dump()
            if session:
                error_dict["session_id"] = session.session_id
            server.activity_publisher.publish_nowait(
                {
                    "type": "code_failed",
                    "description": description,
                    "code": code,
                    "success": False,
                    "error": str(e),
                    "session_id": session.session_id if session else None,
                }
            )
            return json.dumps(error_dict, indent=2)
        except Exception as e:
            LOGGER.error(f"Execution failed: {e}", exc_info=True)
            error_result = CodeExecutionResult(success=False, error=str(e), description=description)
            error_dict = error_result.model_dump()
            if session:
                error_dict["session_id"] = session.session_id
            server.activity_publisher.publish_nowait(
                {
                    "type": "code_failed",
                    "description": description,
                    "code": code,
                    "success": False,
                    "error": str(e),
                    "session_id": session.session_id if session else None,
                }
            )
            return json.dumps(error_dict, indent=2)
        finally:
            if session:
                set_current_session(None)
            server._clear_auth_context()

    return execute_code_tool


def build_check_job_tool(server: "CodeExecutionServer") -> Callable:
    """Build a tool that checks status/output for a background code-execution job."""

    async def check_job_tool(ctx: Context, job_id: str) -> str:
        """Check the status of a background `execute_code(background=True)` job."""
        mcp_session_id = None
        if ctx:
            try:
                mcp_session_id = ctx.session_id
            except (RuntimeError, AttributeError):
                pass

        try:
            server._restore_auth_context_for_mcp_session(mcp_session_id)
            caller_identity = get_current_user_identity()
            # Pass caller_identity so that missing-job and unauthorized-access both
            # raise ValueError("Job … not found"), preventing job-id existence probing.
            status = server.session_manager.check_background_job(job_id, caller_identity=caller_identity)
            return json.dumps(status, indent=2)
        except Exception as e:
            LOGGER.error(f"check_job failed: {e}", exc_info=True)
            return json.dumps({"success": False, "error": str(e)}, indent=2)
        finally:
            server._clear_auth_context()

    return check_job_tool
