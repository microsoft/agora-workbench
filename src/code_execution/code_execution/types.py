"""
Type definitions for code execution components.

Defines common types used across the code_execution module for assets
and execution contexts.
"""

import re
from typing import TypeAlias

# Variable name to be injected into execution namespace
# Example: "network", "data_file", "config"
VarName: TypeAlias = str

# Asset identifier in type-tagged format: <type>base64_id</type>
# Examples: "<blob>aHR0cHM6Ly8...</blob>", "<sql>query_id_123</sql>"
AssetId: TypeAlias = str

# Regex matching type-tagged asset references: <type>id</type>
ASSET_TAG_RE = re.compile(r"^<(\w+)>([^<>]+)</\1>$")
# Fallback for unclosed tags: <type>id  (no closing tag)
ASSET_TAG_UNCLOSED_RE = re.compile(r"^<(\w+)>([^<>]+)$")
