"""
In-memory registry for :class:`~code_execution.tool_registry.ToolDefinition` objects.

Tools are assigned a sequential integer ID upon registration and indexed by
both their integer ID and their string name for O(1) lookups.
"""

import logging
from copy import copy

from .tool_schema import ToolDefinition

LOGGER = logging.getLogger(__name__)


class ToolRegistry:
    """Registry that stores and indexes :class:`~code_execution.tool_registry.ToolDefinition` objects.

    Each registered tool receives a unique integer ID (assigned sequentially
    starting from ``0``).  Tools can be retrieved by ID or by name in O(1) time
    via internal dictionaries.

    Args:
        package: Default Python package for tool imports. When set, the kernel
            proxy generates ``from {package}.{tool_name} import {tool_name}``
            for each registered tool that does not set ``module_override`` or
            an explicit ``module``.
    """

    def __init__(self, package: str | None = None):
        self.package = package
        self.tools: list[ToolDefinition] = []
        self.next_id = 0
        self._name_index: dict[str, ToolDefinition] = {}
        self._id_index: dict[int, ToolDefinition] = {}

    def register_tool(self, tool: ToolDefinition) -> None:
        """Register a tool in the registry.

        A copy of *tool* is stored so that the registry's internal state
        cannot be mutated through the caller's reference.  The copy is
        assigned a unique integer ``id`` before being stored.

        Module resolution order:
            1. ``tool.module_override`` (explicit full module path on the tool)
            2. ``tool.module`` (already resolved, e.g. from catalog deserialization)
            3. ``{registry.package}.{tool.name}`` (default convention)

        If none of these yield a module path, a :class:`ValueError` is raised.

        Args:
            tool: The :class:`~code_execution.tool_registry.ToolDefinition` to register.

        Raises:
            ValueError: If the tool's module cannot be resolved.
        """
        _tool = copy(tool)
        _tool.id = self.next_id

        # Resolve the kernel import module path
        if _tool.module_override:
            _tool.module = _tool.module_override
        elif not _tool.module:
            if self.package:
                _tool.module = f"{self.package}.{_tool.name}"
            else:
                raise ValueError(
                    f"Tool '{_tool.name}' has no module_override and no explicit module set, "
                    f"and the registry has no default package. Either set "
                    f"ToolRegistry(package='my_package'), or set module_override on the tool."
                )

        self.tools.append(_tool)
        self._name_index[_tool.name] = _tool
        self._id_index[self.next_id] = _tool
        self.next_id += 1

    def get_tool_by_name(self, name: str) -> ToolDefinition:
        """Return the tool registered under *name*.

        Args:
            name: The tool's string name as supplied when it was registered.

        Returns:
            The :class:`~code_execution.tool_registry.ToolDefinition` registered under *name*.

        Raises:
            ValueError: If no tool with that name is registered.
        """
        if tool := self._name_index.get(name):
            return tool
        else:
            raise ValueError(f"The tool {name} is not registered.")

    def get_tool_by_server_and_name(self, server_name: str, name: str) -> ToolDefinition:
        """Return the tool registered under *server_name* and *name*.

        Args:
            server_name: The MCP server name the tool belongs to.
            name: The tool's string name.

        Returns:
            The matching :class:`~code_execution.tool_registry.ToolDefinition`.

        Raises:
            ValueError: If no matching tool can be found.
        """
        for tool in self.tools:
            if tool.name == name and (tool.server_name or "") == (server_name or ""):
                return tool
        raise ValueError(f"The tool '{name}' on server '{server_name}' is not registered.")

    def get_tool_by_id(self, tool_id: int) -> ToolDefinition:
        """Return the tool with the given integer registry ID.

        Args:
            tool_id: The integer ID assigned to the tool at registration time.

        Returns:
            The :class:`~code_execution.tool_registry.ToolDefinition` with that ID.

        Raises:
            ValueError: If no tool with that ID is registered.
        """
        if tool := self._id_index.get(tool_id):
            return tool
        else:
            raise ValueError(f"The tool with id {tool_id} is not registered.")

    def get_id_by_name(self, name: str) -> int:
        """Return the integer registry ID for the tool registered under *name*.

        Args:
            name: The tool's string name.

        Returns:
            The integer ID assigned to the tool at registration time.

        Raises:
            ValueError: If no tool with that name is registered.
        """
        if tool := self._name_index.get(name):
            assert tool.id is not None
            return tool.id
        else:
            raise ValueError(f"The tool {name} is not registered.")

    def get_name_by_id(self, tool_id: int) -> str:
        """Return the name of the tool with the given integer registry ID.

        Args:
            tool_id: The integer ID assigned to the tool at registration time.

        Returns:
            The string name of the registered tool.

        Raises:
            ValueError: If no tool with that ID is registered.
        """
        if tool := self._id_index.get(tool_id):
            return tool.name
        else:
            raise ValueError(f"The tool with id {tool_id} is not registered.")

    def remove_tool_by_id(self, tool_id: int) -> bool:
        """Remove the tool with the given ID and update indexes.

        Args:
            tool_id: The integer ID of the tool to remove.

        Returns:
            ``True`` if the tool was successfully removed.

        Raises:
            ValueError: If no tool with that ID is registered.
        """
        tool = self.get_tool_by_id(tool_id)
        self.tools = [t for t in self.tools if t.id != tool_id]
        del self._id_index[tool_id]
        if tool.name in self._name_index:
            del self._name_index[tool.name]
        return True

    def remove_tool_by_name(self, name: str) -> bool:
        """Remove the tool with the given name and update indexes.

        Args:
            name: The string name of the tool to remove.

        Returns:
            ``True`` if the tool was successfully removed.

        Raises:
            ValueError: If no tool with that name is registered.
        """
        tool = self.get_tool_by_name(name)
        self.tools = [t for t in self.tools if t.name != name]
        del self._name_index[name]
        if tool.id in self._id_index:
            del self._id_index[tool.id]
        return True
