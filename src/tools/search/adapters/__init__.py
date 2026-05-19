"""MAF adapters for tool search.

The client-side MAF adapters (``create_search_tools_function``,
``create_query_state_graph_function``, ``create_load_skill_function``) have
been removed.  Tool search is now handled server-side by each MCP server's
``search_{name}_tools`` MCP tool, discoverable via standard ``list_tools``.
"""

__all__: list[str] = []
