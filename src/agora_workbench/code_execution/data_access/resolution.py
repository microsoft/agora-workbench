"""
Utilities for detecting and resolving DataLake-cataloged asset references.
"""

import asyncio
import contextvars
import logging
import re
from typing import Any, TYPE_CHECKING

from fastmcp.server.middleware import Middleware, MiddlewareContext

from ..types import AssetId, VarName

if TYPE_CHECKING:
    from ..server import CodeExecutionServer

LOGGER = logging.getLogger(__name__)

# ContextVar for passing asset resolution metadata from middleware to tool callback.
# The middleware populates this before FastMCP/Pydantic validation; the tool
# callback reads it to know which parameters were resolved to cache paths.
_resolved_assets: contextvars.ContextVar[list] = contextvars.ContextVar("_resolved_assets", default=[])

# Parameters that should be excluded from asset resolution even when they
# match the ``<type>id</type>`` tag format.  Keyed by (tool_name_suffix, param_name).
# Tool name suffix is used because tool names are prefixed with the server name.
_RESOLUTION_EXEMPT_PARAMS: set[tuple[str, str]] = {
    ("_publish_artifact", "destination"),
}


def looks_like_qualified_name(value: str) -> bool:
    """
    Check if a string looks like a DataLake qualified name.

    Recognizes type-tagged artifact format: <type>id</type>
    Examples: <blob>abc123</blob>, <sql>xyz789</sql>

    Args:
        value: String to check

    Returns:
        True if value matches the type-tagged artifact pattern
    """
    if not isinstance(value, str):
        return False

    stripped = value.strip()
    if not stripped:
        return False

    # Accept both <type>id</type> and unclosed <type>id (LLM sometimes omits closing tag)
    return bool(re.match(r"^<(\w+)>([^<>]+)</\1>$", stripped)) or bool(re.match(r"^<(\w+)>([^<>]+)$", stripped))


def should_resolve_as_asset(value: Any) -> bool:
    """
    Determine if a parameter value should be resolved as a DataLake asset.

    Detection is purely value-based: any string matching the type-tagged
    format ``<type>id</type>`` will be resolved.

    Args:
        value: Parameter value to check

    Returns:
        True if value is a type-tagged asset reference
    """
    if not isinstance(value, str):
        return False

    return looks_like_qualified_name(value)


class AssetResolutionMiddleware(Middleware):
    """
    FastMCP middleware that resolves DataLake asset references before Pydantic validation.

    When the agent sends a tool call with tagged asset references like
    ``<blob>base64_id</blob>``, this middleware intercepts the arguments,
    resolves each reference to a local cache path via the session's data
    manager, and replaces the argument value with the path string.  This
    happens *before* FastMCP/Pydantic coerces arguments against the function
    signature, so parameters with their natural types (``Path``, ``bool``,
    ``int``, etc.) receive properly typed values instead of being mangled by
    premature coercion.

    Resolution metadata (qualified name, cache path, parameter name) is stored
    in a ``ContextVar`` so that the tool callback can inject the asset into the
    kernel for the generated execution code.
    """

    def __init__(self, server: "CodeExecutionServer"):
        self.server = server

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        from ..sessions import set_current_session

        # Create a copy to avoid mutating the shared context
        original_arguments = context.message.arguments or {}
        arguments = original_arguments.copy()

        # Quick scan: do any arguments contain asset tags?
        # Skip parameters that are exempt from resolution (e.g. publish_artifact's
        # destination param uses the same tag syntax for routing, not asset lookup).
        tool_name = context.message.name
        assets_to_resolve = {
            param_name: param_value
            for param_name, param_value in arguments.items()
            if isinstance(param_value, str)
            and should_resolve_as_asset(param_value)
            and not any(
                tool_name.endswith(suffix) and param_name == pname for suffix, pname in _RESOLUTION_EXEMPT_PARAMS
            )
        }

        if not assets_to_resolve:
            _resolved_assets.set([])
            return await call_next(context)

        # --- Session and auth setup (needed for data_manager access) ---
        fastmcp_ctx = context.fastmcp_context
        session_id = None
        if fastmcp_ctx:
            try:
                session_id = fastmcp_ctx.session_id
            except RuntimeError:
                pass

        self.server._restore_auth_context_for_mcp_session(session_id)
        tool_name = context.message.name
        session = await self.server._get_or_create_session(tool_name, session_id=session_id)
        set_current_session(session)

        # --- Resolve all tagged assets concurrently ---
        try:
            LOGGER.info(f"Middleware: resolving {len(assets_to_resolve)} asset(s) for tool '{tool_name}'")

            async def _fetch(param_name: VarName, qualified_name: AssetId):
                try:
                    cache_path = await session.data_manager.get_cache_path(qualified_name)
                    return (param_name, qualified_name, str(cache_path), None)
                except Exception as e:
                    return (param_name, qualified_name, None, e)

            results = await asyncio.gather(*[_fetch(pn, pv) for pn, pv in assets_to_resolve.items()])

            # Check for errors
            for param_name, qualified_name, cache_path, error in results:
                if error:
                    raise RuntimeError(
                        f"Failed to resolve DataLake asset '{qualified_name}' for parameter '{param_name}': {error}"
                    ) from error

            # Replace argument values in-place and build injection metadata
            resolved = []
            for param_name, qualified_name, cache_path, _ in results:
                session._asset_counter += 1
                asset_key = f"_asset_{param_name}_{session._asset_counter}"

                # Store metadata in session object store
                session.object_store.store(
                    asset_key,
                    {
                        "qualified_name": qualified_name,
                        "cache_path": cache_path,
                    },
                )

                # Replace the tagged reference with the resolved cache path so
                # that Pydantic can coerce it naturally (e.g. str -> Path).
                arguments[param_name] = cache_path
                resolved.append((param_name, asset_key, cache_path))

                LOGGER.debug(f"Middleware: resolved '{param_name}': {qualified_name} -> {cache_path}")

            # Store resolution metadata for the tool callback
            _resolved_assets.set(resolved)

            # Update context with modified arguments for downstream processing
            context.message.arguments = arguments

        except Exception:
            # Clean up on failure
            set_current_session(None)
            self.server._clear_auth_context()
            raise

        try:
            return await call_next(context)
        finally:
            # always clean up session and auth context after tool execution
            set_current_session(None)
            self.server._clear_auth_context()
