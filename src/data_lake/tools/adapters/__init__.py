"""MAF adapters for data lake tools.

Requires the ``maf`` extra: ``pip install agora-workbench[maf]``
"""

from .maf import create_data_lake_search_tool, is_data_lake_configured

__all__ = ["create_data_lake_search_tool", "is_data_lake_configured"]
