import logging
from copy import copy

from .tool_schema import ToolDefinition

LOGGER = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self):
        self.tools: list[ToolDefinition] = []
        self.next_id = 0
        self._name_index: dict[str, ToolDefinition] = {}
        self._id_index: dict[int, ToolDefinition] = {}

    def register_tool(self, tool: ToolDefinition) -> None:
        """
        Register a tool in the registry.

        Args:
            tool: a ToolDefinition to register
        """
        _tool = copy(tool)
        _tool.id = self.next_id
        self.tools.append(_tool)
        self._name_index[_tool.name] = _tool
        self._id_index[self.next_id] = _tool
        self.next_id += 1

    def get_tool_by_name(self, name) -> ToolDefinition:
        if tool := self._name_index.get(name):
            return tool
        else:
            raise ValueError(f"The tool {name} is not registered.")

    def get_tool_by_server_and_name(self, server_name: str, name: str) -> ToolDefinition:
        """Return the tool registered under *server_name* and *name*.

        Raises:
            ValueError: If no matching tool can be found.
        """
        for tool in self.tools:
            if tool.name == name and (tool.server_name or "") == (server_name or ""):
                return tool
        raise ValueError(f"The tool '{name}' on server '{server_name}' is not registered.")

    def get_tool_by_id(self, tool_id) -> ToolDefinition:
        if tool := self._id_index.get(tool_id):
            return tool
        else:
            raise ValueError(f"The tool with id {tool_id} is not registered.")

    def get_id_by_name(self, name) -> int:
        if tool := self._name_index.get(name):
            assert tool.id is not None
            return tool.id
        else:
            raise ValueError(f"The tool {name} is not registered.")

    def get_name_by_id(self, tool_id) -> str:
        if tool := self._id_index.get(tool_id):
            return tool.name
        else:
            raise ValueError(f"The tool with id {tool_id} is not registered.")

    def remove_tool_by_id(self, tool_id) -> bool:
        """Remove the tool with the given id and update indexes."""
        tool = self.get_tool_by_id(tool_id)
        self.tools = [t for t in self.tools if t.id != tool_id]
        del self._id_index[tool_id]
        if tool.name in self._name_index:
            del self._name_index[tool.name]
        return True

    def remove_tool_by_name(self, name) -> bool:
        """Remove the tool with the given name and update indexes."""
        tool = self.get_tool_by_name(name)
        self.tools = [t for t in self.tools if t.name != name]
        del self._name_index[name]
        if tool.id in self._id_index:
            del self._id_index[tool.id]
        return True
