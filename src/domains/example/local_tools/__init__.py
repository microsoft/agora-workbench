"""Local tools package for example domain."""

from .echo_tool import echo_with_magic_word
from .tool_registry import create_local_tool_registry

__all__ = ["echo_with_magic_word", "create_local_tool_registry"]
