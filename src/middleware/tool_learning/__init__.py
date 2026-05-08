"""
Tool-learning memory module.

Provides anti-pattern guardrails and repair-template retrieval to help agents
avoid repeating tool-call mistakes and recover quickly from failures.

Components:
  - models: Pydantic vignette schemas
  - config: Environment/config wiring
  - render: Deterministic renderer for prompt injection
  - table_repo: Azure Table Storage CRUD (source of truth)
  - local_file_repo: local JSON persistence for vignette writes
  - write_repo: write backend protocol
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
from .local_file_repo import LocalFileVignetteRepo
from .table_repo import TableVignetteRepo
from .search_repo import SearchVignetteRepo
from .write_repo import VignetteWriteRepo

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
    "LocalFileVignetteRepo",
    "TableVignetteRepo",
    "SearchVignetteRepo",
    "VignetteWriteRepo",
]
