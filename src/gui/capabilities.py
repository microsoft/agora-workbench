"""Skills discovery API — surfaces agent capabilities to the GUI.

Scans domains/*/skills/ and planning/skills/ directories for SKILL.md
files, extracts YAML frontmatter (name + description), and groups them
by domain.  Only skills whose MCP server is currently registered (healthy
and authenticated) are returned — so the panel never advertises
capabilities the agent can't actually use.

The frontend uses this to power the Skills Panel and the ``/skills``
chat command.
"""

import logging
import re
from pathlib import Path

import yaml
from fastapi import APIRouter
from pydantic import BaseModel

LOGGER = logging.getLogger(__name__)

router = APIRouter()

# Match the agent framework's discovery depth.
_MAX_SEARCH_DEPTH = 2


# ---------------------------------------------------------------------------
# Skill discovery
# ---------------------------------------------------------------------------


def _extract_frontmatter(text: str) -> tuple[str, str] | None:
    """Extract name and description from YAML frontmatter.

    Returns (name, description) or None if frontmatter is missing/invalid.
    """
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return None
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    name = meta.get("name", "")
    description = meta.get("description", "")
    if not name:
        return None
    return str(name), str(description)


def _get_active_server_names() -> set[str]:
    """Return the set of MCP server names that are currently registered.

    Only servers that passed health-check and auth validation during
    auto-discovery are present in the registry.

    Note: ``list_servers()`` may trigger ``_auto_discover()`` which
    performs blocking I/O (health checks).  This function is designed
    to be called via ``asyncio.to_thread`` from async endpoints so it
    never blocks the event loop.
    """
    from tools.mcp import get_mcp_registry

    registry = get_mcp_registry()
    return set(registry.list_servers().keys())


def _discover_skills() -> list[dict]:
    """Walk skill directories and return skills for active MCP servers only.

    Each returned dict has keys: name, description, domain.
    Uses the same directory layout and depth limit as the agent framework's
    SkillsProvider to ensure the panel shows exactly what the agent can load.
    """
    from gui.agent import _discover_skill_paths

    active_servers = _get_active_server_names()
    skill_paths = _discover_skill_paths()
    results: list[dict] = []
    seen_names: set[str] = set()

    for root_dir_str in skill_paths:
        root_dir = Path(root_dir_str)
        if not root_dir.is_dir():
            continue

        # Derive domain name from path: .../domains/{domain}/skills -> domain
        # For planning skills: .../planning/skills -> "planning"
        parts = root_dir.parts
        if "domains" in parts:
            idx = parts.index("domains")
            domain = parts[idx + 1] if idx + 1 < len(parts) else "other"
        elif "planning" in parts:
            domain = "planning"
        else:
            domain = "other"

        # Skip domains whose MCP server is not active.
        # Planning skills are always shown (no dedicated MCP server).
        if domain not in ("planning", "other") and domain not in active_servers:
            LOGGER.debug("Skipping skills for inactive domain '%s'", domain)
            continue

        # Recursive search matching framework's MAX_SEARCH_DEPTH=2
        _search_dir(root_dir, 0, domain, results, seen_names)

    return results


def _search_dir(
    directory: Path,
    depth: int,
    domain: str,
    results: list[dict],
    seen_names: set[str],
) -> None:
    """Recursively search for SKILL.md files up to _MAX_SEARCH_DEPTH."""
    skill_file = directory / "SKILL.md"
    if skill_file.is_file():
        try:
            text = skill_file.read_text(encoding="utf-8")
        except OSError:
            LOGGER.warning("Failed to read %s", skill_file)
        else:
            parsed = _extract_frontmatter(text)
            if parsed and parsed[0] not in seen_names:
                name, description = parsed
                seen_names.add(name)
                results.append({
                    "name": name,
                    "description": description,
                    "domain": domain,
                })

    if depth >= _MAX_SEARCH_DEPTH:
        return

    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return

    for entry in entries:
        if entry.is_dir():
            _search_dir(entry, depth + 1, domain, results, seen_names)


# ---------------------------------------------------------------------------
# API models and endpoint
# ---------------------------------------------------------------------------


class SkillInfo(BaseModel):
    name: str
    description: str
    domain: str


class SkillsResponse(BaseModel):
    skills: list[SkillInfo]


class ToolInfo(BaseModel):
    name: str
    description: str
    server: str


class ToolsResponse(BaseModel):
    tools: list[ToolInfo]


@router.get("/api/skills")
async def list_skills() -> SkillsResponse:
    """List skills for domains whose MCP server is currently active.

    Runs discovery in a worker thread so that first-call auto-discovery
    (which does blocking health-checks) never stalls the event loop.
    """
    import asyncio

    raw = await asyncio.to_thread(_discover_skills)
    skills = [SkillInfo(**s) for s in raw]
    return SkillsResponse(skills=skills)


@router.get("/api/tools")
async def list_tools() -> ToolsResponse:
    """List domain-level tools from all active MCP servers.

    Loads tool definitions from each domain's ToolRegistry (defined in
    domain_registry.yaml).  Only domains with an active MCP server and
    a registered ToolRegistry are included.
    """
    import asyncio
    import importlib
    from domains.domain_registry import get_domain_registry

    def _collect() -> list[dict]:
        active_servers = _get_active_server_names()
        domain_registry = get_domain_registry()
        results: list[dict] = []

        for name, config in domain_registry.list_domains().items():
            if name not in active_servers:
                continue
            mod_path = config.tool_registry_module
            func_name = config.tool_registry_function
            if not mod_path or not func_name:
                continue
            try:
                mod = importlib.import_module(mod_path)
                factory = getattr(mod, func_name)
                tool_reg = factory()
                for t in tool_reg.tools:
                    results.append({
                        "name": t.name,
                        "description": t.description or "",
                        "server": name,
                    })
            except Exception:
                LOGGER.warning("Failed to load tool registry for '%s'", name, exc_info=True)

        return results

    raw = await asyncio.to_thread(_collect)
    tools = [ToolInfo(**t) for t in raw]
    return ToolsResponse(tools=tools)
