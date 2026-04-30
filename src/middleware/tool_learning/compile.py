"""
Vignette compiler: converts a successful tool-call repair event into vignette(s).

When a tool call fails and a subsequent patched call succeeds, this module
compiles the failure/fix pair into anti-pattern and/or repair-template vignettes
that can be upserted into the vignette store.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .models import (
    AntiPattern,
    MatchSpec,
    RepairStrategy,
    ToolSignature,
    Vignette,
    compute_vignette_id,
)

LOGGER = logging.getLogger(__name__)

# Keys whose values are likely to contain secrets/credentials
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "secret",
        "password",
        "credential",
        "connection_string",
        "private_key",
        "bearer",
    }
)

_REDACTED = "***REDACTED***"


def _redact_sensitive(args: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of *args* with values for known sensitive keys redacted."""
    return {k: (_REDACTED if k.lower() in _SENSITIVE_KEYS else v) for k, v in args.items()}


def _extract_arg_keys(args: Optional[Dict[str, Any]]) -> List[str]:
    """Return sorted list of argument keys from a tool-call args dict."""
    if not args:
        return []
    return sorted(args.keys())


def compile_vignettes(
    tool_name: str,
    original_args: Dict[str, Any],
    patched_args: Dict[str, Any],
    error_class: str,
    error_message: str,
    repair_steps: List[str],
    scope: str = "user",
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    tool_version: Optional[str] = None,
    provider: Optional[str] = None,
    source_trace_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> List[Vignette]:
    """
    Compile vignettes from a successful tool-call repair event.

    Creates:
      1. An anti-pattern vignette if the failure was caused by a recognizable
         argument pattern (i.e. args changed between original and patched call).
      2. A repair-template vignette capturing the ordered repair steps.

    Args:
        tool_name: Name of the tool that failed.
        original_args: The original (failing) tool-call arguments.
        patched_args: The corrected (succeeding) tool-call arguments.
        error_class: Stable error class identifier (e.g. "AuthenticationError").
        error_message: Human-readable error message from the failure.
        repair_steps: Ordered list of steps that led to the successful repair.
        scope: Vignette scope ("user", "org", "global"). Default "user".
        tenant_id: Tenant ID (required for org and user scopes).
        user_id: User ID (required for user scope).
        tool_version: Optional tool version string.
        provider: Optional provider string (e.g. "mcp", "native").
        source_trace_id: Optional trace ID linking back to the original failure.
        tags: Optional extra tags.

    Returns:
        List of compiled Vignette objects (0-2 items).
    """
    vignettes: List[Vignette] = []
    now = datetime.now(timezone.utc)
    tool_sig = ToolSignature(
        tool_name=tool_name,
        tool_version=tool_version,
        provider=provider,
    )
    arg_keys = _extract_arg_keys(original_args)
    base_tags = list(tags or [])

    # 1. Anti-pattern vignette (only if args changed)
    all_keys = set(original_args.keys()) | set(patched_args.keys())
    changed_keys = [k for k in all_keys if original_args.get(k) != patched_args.get(k)]
    if changed_keys:
        rule = f"Avoid args that trigger {error_class}; ensure correct values for: {', '.join(sorted(changed_keys))}."
        ap_id = compute_vignette_id(
            tool_name=tool_name,
            kind="anti_pattern",
            error_class=error_class,
            rule_or_steps=rule,
            arg_keys=arg_keys,
        )
        try:
            ap_vignette = Vignette(
                vignette_id=ap_id,
                kind="anti_pattern",
                scope=scope,  # type: ignore[arg-type]
                tenant_id=tenant_id,
                user_id=user_id,
                tool=tool_sig,
                match=MatchSpec(
                    error_class=error_class,
                    arg_keys=arg_keys,
                ),
                title=f"Anti-pattern: {tool_name} / {error_class}",
                summary=f"Tool {tool_name!r} failed with {error_class}: {error_message[:200]}",
                tags=base_tags + ["auto_compiled"],
                anti_pattern=AntiPattern(
                    rule=rule,
                    rationale=error_message[:500],
                    bad_example=_redact_sensitive(original_args),
                    good_example=_redact_sensitive(patched_args),
                    severity="soft",
                ),
                confidence=0.60,
                created_at=now,
                updated_at=now,
                source_trace_id=source_trace_id,
            )
            vignettes.append(ap_vignette)
        except Exception as e:
            LOGGER.warning("Failed to compile anti_pattern vignette: %s", e)

    # 2. Repair-template vignette (always, when steps provided)
    if repair_steps:
        steps_json = json.dumps(repair_steps)
        rt_id = compute_vignette_id(
            tool_name=tool_name,
            kind="repair_template",
            error_class=error_class,
            rule_or_steps=steps_json,
            arg_keys=arg_keys,
        )
        try:
            rt_vignette = Vignette(
                vignette_id=rt_id,
                kind="repair_template",
                scope=scope,  # type: ignore[arg-type]
                tenant_id=tenant_id,
                user_id=user_id,
                tool=tool_sig,
                match=MatchSpec(
                    error_class=error_class,
                    arg_keys=arg_keys,
                ),
                title=f"Repair: {tool_name} / {error_class}",
                summary=f"Repair playbook for {tool_name!r} after {error_class}.",
                tags=base_tags + ["auto_compiled"],
                repair=RepairStrategy(
                    steps=repair_steps,
                    patched_args_example=_redact_sensitive(patched_args),
                    max_retries=1,
                ),
                confidence=0.65,
                created_at=now,
                updated_at=now,
                source_trace_id=source_trace_id,
            )
            vignettes.append(rt_vignette)
        except Exception as e:
            LOGGER.warning("Failed to compile repair_template vignette: %s", e)

    return vignettes
