"""
Tool-learning memory module.

Provides anti-pattern guardrails and repair-template retrieval to help agents
avoid repeating tool-call mistakes and recover quickly from failures.

Components:
  - models: Pydantic vignette schemas
  - config: Environment/config wiring
  - render: Deterministic renderer for prompt injection
  - table_repo: Azure Table Storage CRUD (source of truth)
  - search_repo: Azure AI Search hybrid retrieval
  - compile: Log → vignette compiler
"""

from .config import ToolLearningConfig
from .models import (
    Vignette,
    VignetteKind,
    Scope,
    ToolSignature,
    MatchSpec,
    AntiPattern,
    RepairStrategy,
    compute_vignette_id,
)
from .render import render_guardrails_block, render_repair_block
from .compile import compile_vignettes
from .table_repo import TableVignetteRepo
from .search_repo import SearchVignetteRepo

__all__ = [
    # Config
    "ToolLearningConfig",
    # Models
    "Vignette",
    "VignetteKind",
    "Scope",
    "ToolSignature",
    "MatchSpec",
    "AntiPattern",
    "RepairStrategy",
    "compute_vignette_id",
    # Rendering
    "render_guardrails_block",
    "render_repair_block",
    # Compilation
    "compile_vignettes",
    # Repositories
    "TableVignetteRepo",
    "SearchVignetteRepo",
]
