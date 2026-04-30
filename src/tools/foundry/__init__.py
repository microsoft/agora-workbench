"""Azure AI Foundry integration module."""

from .foundry_client import (
    FoundryClientManager,
    get_foundry_client,
    reset_foundry_client,
)
from .foundry_adapter import (
    FoundryToolAdapter,
    get_foundry_adapter,
)
from .foundry_models import (
    FoundryAgentConfig,
    FoundryBuiltinTool,
    FoundryToolParameters,
    FoundryToolResult,
)

__all__ = [
    # Client
    "FoundryClientManager",
    "get_foundry_client",
    "reset_foundry_client",
    # Adapter
    "FoundryToolAdapter",
    "get_foundry_adapter",
    # Models
    "FoundryAgentConfig",
    "FoundryBuiltinTool",
    "FoundryToolParameters",
    "FoundryToolResult",
]
