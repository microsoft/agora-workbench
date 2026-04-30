"""
Pydantic models for the tool-learning memory vignette schema.

Vignettes are small, structured records of two types:
  - anti_pattern: guardrails describing known failure patterns and safer alternatives.
  - repair_template: bounded, tool-specific recovery recipes keyed to error classes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

VignetteKind = Literal["anti_pattern", "repair_template"]
Scope = Literal["user", "org", "global"]


class ToolSignature(BaseModel):
    """Identifies a specific tool."""

    tool_name: str
    tool_version: Optional[str] = None
    provider: Optional[str] = None  # e.g. "mcp", "native", "rest"


class MatchSpec(BaseModel):
    """Match conditions used by retrieval and/or post-filtering."""

    error_class: Optional[str] = None
    arg_keys: Optional[List[str]] = None
    # JSON-serializable constraints; keep it simple in Phase 1
    arg_constraints: Optional[Dict[str, Any]] = None


class AntiPattern(BaseModel):
    """Describes a known failure pattern and a safer alternative."""

    rule: str  # concise: "Avoid X; prefer Y"
    rationale: Optional[str] = None
    bad_example: Optional[Dict[str, Any]] = None
    good_example: Optional[Dict[str, Any]] = None
    severity: Literal["soft", "hard"] = "soft"  # "hard" may block call


class RepairStrategy(BaseModel):
    """A bounded, tool-specific recovery recipe keyed to an error class."""

    steps: List[str]  # ordered recipe
    patched_args_example: Optional[Dict[str, Any]] = None
    max_retries: int = 1
    stop_if: Optional[List[str]] = None


class Vignette(BaseModel):
    """
    A vignette is a structured record of a tool-call failure and its fix.

    Two kinds:
      - anti_pattern: guardrail describing a known failure pattern.
      - repair_template: bounded recovery recipe keyed to an error class.
    """

    # Identity
    vignette_id: str = Field(..., description="Stable ID (uuid or deterministic hash)")
    kind: VignetteKind

    # Scope and ownership
    scope: Scope = "user"
    tenant_id: Optional[str] = None
    user_id: Optional[str] = None

    # Matching
    tool: ToolSignature
    match: MatchSpec

    # Content
    title: str
    summary: str
    tags: List[str] = Field(default_factory=list)
    anti_pattern: Optional[AntiPattern] = None
    repair: Optional[RepairStrategy] = None

    # Ranking + lifecycle
    confidence: float = 0.70
    promoted: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_trace_id: Optional[str] = None

    @model_validator(mode="after")
    def _validate_kind_payload(self) -> "Vignette":
        if self.kind == "anti_pattern" and self.anti_pattern is None:
            raise ValueError("anti_pattern vignette requires anti_pattern payload")
        if self.kind == "repair_template" and self.repair is None:
            raise ValueError("repair_template vignette requires repair payload")
        if self.scope == "user" and (not self.user_id or not self.tenant_id):
            raise ValueError("user scope requires both user_id and tenant_id")
        if self.scope == "org" and not self.tenant_id:
            raise ValueError("org scope requires tenant_id")
        return self


def compute_vignette_id(
    tool_name: str,
    kind: VignetteKind,
    error_class: Optional[str],
    rule_or_steps: str,
    arg_keys: Optional[List[str]],
) -> str:
    """
    Compute a deterministic vignette ID from its key fields.

    ID = sha256(tool_name | kind | error_class | rule_or_steps | sorted_arg_keys)

    Args:
        tool_name: Name of the tool.
        kind: Vignette kind ("anti_pattern" or "repair_template").
        error_class: Optional error class.
        rule_or_steps: Rule text (for anti_pattern) or JSON-serialized steps (for repair_template).
        arg_keys: Optional sorted list of argument keys.

    Returns:
        Hex-encoded SHA-256 digest (64 chars).
    """
    normalized_arg_keys = json.dumps(sorted(arg_keys or []))
    raw = "|".join(
        [
            tool_name,
            kind,
            error_class or "none",
            rule_or_steps,
            normalized_arg_keys,
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()
