"""MAF adapters for GUI tools.

Requires the ``maf`` extra: ``pip install agora-workbench[maf]``
"""

from .maf_capture_tool import create_capture_map_view_function
from .maf_story_map_tool import create_story_map_function

__all__ = ["create_capture_map_view_function", "create_story_map_function"]
