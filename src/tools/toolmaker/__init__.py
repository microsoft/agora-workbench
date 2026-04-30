"""
ToolMaker integration tools for AgoraAgent.

Provides the `create_tool_from_repo` FunctionTool that allows AgoraAgent to
dynamically invoke ToolMaker to create new MCP domain tools from GitHub repositories.
"""

from .toolmaker_tool import create_toolmaker_function

__all__ = ["create_toolmaker_function"]
