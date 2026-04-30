"""
Configuration for the tool-learning memory module.

Reads Azure Table Storage and Azure AI Search settings from environment variables.
"""

import os
from dataclasses import dataclass, field


@dataclass
class ToolLearningConfig:
    """Configuration for the tool-learning module."""

    # Azure Table Storage
    table_storage_endpoint: str = field(default_factory=lambda: os.getenv("TOOL_LEARNING_TABLE_ENDPOINT", ""))
    table_name: str = field(default_factory=lambda: os.getenv("TOOL_LEARNING_TABLE_NAME", "ToolVignettes"))

    # Azure AI Search
    search_endpoint: str = field(default_factory=lambda: os.getenv("TOOL_LEARNING_SEARCH_ENDPOINT", ""))
    search_index_name: str = field(default_factory=lambda: os.getenv("TOOL_LEARNING_SEARCH_INDEX", "tool-vignettes"))

    # Retrieval parameters
    top_k: int = field(default_factory=lambda: int(os.getenv("TOOL_LEARNING_TOP_K", "5")))
    min_confidence: float = field(default_factory=lambda: float(os.getenv("TOOL_LEARNING_MIN_CONFIDENCE", "0.0")))

    @classmethod
    def from_env(cls) -> "ToolLearningConfig":
        """Create configuration from environment variables."""
        return cls()
