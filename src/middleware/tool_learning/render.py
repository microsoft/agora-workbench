"""
Deterministic renderer for tool-learning memory vignettes.

Converts vignettes into compact prompt snippets for injection into agent context.
"""

from __future__ import annotations

from typing import List

from .models import Vignette


def render_anti_pattern(vignette: Vignette) -> str:
    """
    Render a single anti-pattern vignette as a bullet line.

    Format: "- [HARD] Avoid: <bad>. Prefer: <good>." or "- <rule>"
    """
    if vignette.anti_pattern is None:
        raise ValueError(f"Vignette {vignette.vignette_id!r} has no anti_pattern payload")
    ap = vignette.anti_pattern
    prefix = "- HARD: " if ap.severity == "hard" else "- "
    return f"{prefix}{ap.rule}"


def render_repair_template(vignette: Vignette) -> str:
    """
    Render a single repair-template vignette as an ordered step list.

    Format:
        [Repair Playbook: <tool> | <error_class>]
        - Step 1: ...
        - Step 2: ...
    """
    if vignette.repair is None:
        raise ValueError(f"Vignette {vignette.vignette_id!r} has no repair payload")
    repair = vignette.repair
    error_class = vignette.match.error_class or "unknown error"
    header = f"[Repair Playbook: {vignette.tool.tool_name} | {error_class}]"
    steps = "\n".join(f"- Step {i + 1}: {step}" for i, step in enumerate(repair.steps))
    return f"{header}\n{steps}"


def render_guardrails_block(vignettes: List[Vignette]) -> str:
    """
    Render a collection of anti-pattern vignettes as a guardrails block.

    Example output:
        [Tool Guardrails: excel.add_column]
        - Avoid: SUM(A:B) (column labels). Prefer explicit ranges like A1:B10.
        - HARD: Do not omit 'timezone' for calendar.create_event.

    Args:
        vignettes: Anti-pattern vignettes for the same tool (mixed tools are supported).

    Returns:
        Compact multi-line string, or empty string if no vignettes.
    """
    if not vignettes:
        return ""

    anti_patterns = [v for v in vignettes if v.kind == "anti_pattern" and v.anti_pattern is not None]
    if not anti_patterns:
        return ""

    # Group by tool name for cleaner rendering
    by_tool: dict[str, list[Vignette]] = {}
    for v in anti_patterns:
        tool_key = v.tool.tool_name
        by_tool.setdefault(tool_key, []).append(v)

    blocks: list[str] = []
    for tool_name, tool_vignettes in by_tool.items():
        header = f"[Tool Guardrails: {tool_name}]"
        lines = [render_anti_pattern(v) for v in tool_vignettes]
        blocks.append(header + "\n" + "\n".join(lines))

    return "\n\n".join(blocks)


def render_repair_block(vignettes: List[Vignette]) -> str:
    """
    Render a collection of repair-template vignettes as repair playbooks.

    Args:
        vignettes: Repair-template vignettes.

    Returns:
        Compact multi-line string, or empty string if no vignettes.
    """
    if not vignettes:
        return ""

    repair_templates = [v for v in vignettes if v.kind == "repair_template" and v.repair is not None]
    if not repair_templates:
        return ""

    blocks = [render_repair_template(v) for v in repair_templates]
    return "\n\n".join(blocks)
