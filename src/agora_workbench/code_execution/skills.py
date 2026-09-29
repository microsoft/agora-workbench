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
from pathlib import PurePosixPath
from typing import Any

import yaml

LOGGER = logging.getLogger(__name__)
_REFERENCE_LINK_PATTERN = re.compile(
    r"(?P<prefix>(?<!\!)\[[^\]\n]+\]\()"
    r"(?P<target>references/[^)\s]+\.md)"
    r"(?P<suffix>(?:\s+(?:\"[^\"]*\"|'[^']*'))?\))"
)


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
        content: Full markdown content, including frontmatter and linked local
            Markdown references, for agent consumption.
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


def _reference_anchor(reference_path: PurePosixPath) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", reference_path.with_suffix("").as_posix().lower()).strip("-")
    return f"skill-reference-{slug}"


def load_skill_content(skill_path: Path) -> str:
    """Load a skill and append Markdown references linked from it.

    Only relative ``references/*.md`` links below the skill directory are
    expanded. External links, missing files, and unsafe paths remain unchanged.
    """
    content = skill_path.read_text(encoding="utf-8")
    skill_dir = skill_path.parent.resolve()
    references: dict[Path, tuple[str, str, str]] = {}
    reference_anchors: set[str] = set()

    def replace_reference_link(match: re.Match[str]) -> str:
        target = match.group("target")
        relative_path = PurePosixPath(target)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            LOGGER.warning("Ignoring unsafe skill reference %s in %s", target, skill_path)
            return match.group(0)

        reference_path = (skill_dir / Path(*relative_path.parts)).resolve()
        try:
            reference_path.relative_to(skill_dir)
        except ValueError:
            LOGGER.warning("Ignoring skill reference outside %s: %s", skill_dir, target)
            return match.group(0)

        existing = references.get(reference_path)
        if existing is not None:
            anchor = existing[1]
        else:
            try:
                reference_content = reference_path.read_text(encoding="utf-8")
            except OSError as exc:
                LOGGER.warning("Failed to read skill reference %s from %s: %s", target, skill_path, exc)
                return match.group(0)
            anchor = _reference_anchor(relative_path)
            anchor_suffix = 2
            while anchor in reference_anchors:
                anchor = f"{_reference_anchor(relative_path)}-{anchor_suffix}"
                anchor_suffix += 1
            reference_anchors.add(anchor)
            references[reference_path] = (target, anchor, reference_content)

        return f"{match.group('prefix')}#{anchor}{match.group('suffix')}"

    expanded_content = _REFERENCE_LINK_PATTERN.sub(replace_reference_link, content)
    if not references:
        return content

    appendix = ["", "", "## Included references"]
    for target, anchor, reference_content in references.values():
        appendix.extend(
            [
                "",
                f'<a id="{anchor}"></a>',
                "",
                f"### `{target}`",
                "",
                reference_content.strip(),
            ]
        )
    return expanded_content.rstrip() + "\n".join(appendix) + "\n"


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
            content = load_skill_content(skill_md)
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
