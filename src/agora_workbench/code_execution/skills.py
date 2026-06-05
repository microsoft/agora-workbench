"""
Skill discovery and representation.

Skills are multi-step workflow guides (markdown files) that teach an agent
how to compose domain tools for a particular task.  They are discovered
from the filesystem and passed to :class:`~code_execution.CodeExecutionServer`
at construction time.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Skill:
    """A workflow skill that guides an agent through a multi-step tool chain.

    Skills are authored as markdown files with YAML frontmatter declaring
    metadata (name, description, associated states).  The full markdown
    content is served to the agent via the ``load_{name}_skill`` MCP tool.

    Attributes:
        name: Unique skill identifier (from frontmatter ``name:`` field).
        description: Brief description of what this skill covers.
        domain: Domain this skill belongs to (directory name or explicit).
        states: State tokens this skill's workflow covers.
        content: Full markdown content (including frontmatter) for agent consumption.
        path: Filesystem path the skill was loaded from (for debugging).
    """

    name: str
    description: str = ""
    domain: str = ""
    states: list[str] = field(default_factory=list)
    content: str = ""
    path: str = ""


def _parse_skill_frontmatter(path: Path) -> dict[str, Any]:
    """Extract YAML frontmatter from a skill markdown file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}
    try:
        return yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}


def discover_skills(skills_dir: Path, domain: str = "") -> list[Skill]:
    """Discover skill markdown files in a directory and return Skill objects.

    Scans ``skills_dir`` (and subdirectories) for ``*.md`` files with a
    ``name:`` field in their YAML frontmatter.  Files without valid
    frontmatter are skipped.

    Parameters
    ----------
    skills_dir : Path
        Directory to scan for skill markdown files.
    domain : str
        Domain name to assign to discovered skills.  If empty, defaults
        to the parent directory name of ``skills_dir``.

    Returns
    -------
    list[Skill]
        Discovered skills with their full content loaded.

    Example
    -------
    ::

        from agora_workbench.code_execution.skills import discover_skills

        skills = discover_skills(Path(__file__).parent / "skills")
    """
    if not skills_dir.is_dir():
        return []

    domain_name = domain or skills_dir.parent.name
    skills: list[Skill] = []

    for skill_md in sorted(skills_dir.rglob("*.md")):
        fm = _parse_skill_frontmatter(skill_md)
        if not fm.get("name"):
            continue
        try:
            content = skill_md.read_text(encoding="utf-8")
        except OSError:
            LOGGER.warning("Failed to read skill file: %s", skill_md)
            continue

        # Normalize states to always be a list (YAML may produce a scalar string)
        raw_states = fm.get("states", [])
        if isinstance(raw_states, str):
            raw_states = [raw_states]
        elif not isinstance(raw_states, list):
            raw_states = list(raw_states) if raw_states else []

        skills.append(
            Skill(
                name=fm["name"],
                description=fm.get("description", ""),
                domain=domain_name,
                states=raw_states,
                content=content,
                path=str(skill_md),
            )
        )

    LOGGER.debug("Discovered %d skills in %s", len(skills), skills_dir)
    return skills
