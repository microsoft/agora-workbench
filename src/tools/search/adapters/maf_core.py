"""
MAF adapter for the search-tools catalog.

Wraps :func:`tools.search.core.create_search_tools_descriptor` in a
``FunctionTool``.  This is the **only** file in the search package that
imports ``agent_framework``.

Requires the ``maf`` extra: ``pip install agora-workbench[maf]``
"""

try:
    from agent_framework import FunctionTool
except ImportError as e:
    raise ImportError(
        "agent-framework is required for MAF adapters. Install with: pip install agora-workbench[maf]"
    ) from e

from tools.search.core import SearchToolsInput, create_search_tools_descriptor
from tools.tool_search import ToolSearchBackend


# ============================================================================
# Re-export the input model so existing imports keep working
# ============================================================================

__all__ = [
    "SearchToolsInput",
    "create_search_tools_function",
]


# ============================================================================
# MAF factory
# ============================================================================


def create_search_tools_function(backend: ToolSearchBackend) -> FunctionTool:
    """Create a ``search_tools`` ``FunctionTool`` backed by *backend*.

    Delegates to :func:`~tools.search.core.create_search_tools_descriptor`
    and wraps the result in a ``FunctionTool``.

    Args:
        backend: A search backend implementing
                 :class:`~tools.tool_search.ToolSearchBackend`.

    Returns:
        ``FunctionTool`` named ``search_tools``.
    """
    descriptor = create_search_tools_descriptor(backend)
    return FunctionTool(
        name=descriptor.name,
        description=descriptor.description,
        approval_mode="never_require",
        func=descriptor.func,
        input_model=descriptor.input_model,
    )
