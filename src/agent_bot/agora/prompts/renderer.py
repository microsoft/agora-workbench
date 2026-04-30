"""Prompt rendering utilities using Jinja2 templates."""

from pathlib import Path
from typing import Optional

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PackageLoader, select_autoescape


# Get the base directory (AgoraAgentMAF root)
BASE_DIR = Path(__file__).parent.parent.parent.parent.resolve()

# Create Jinja2 environment that can load from both package and file system
jinja_env = Environment(
    loader=ChoiceLoader(
        [
            PackageLoader("agent_bot.agora.prompts", ""),  # Load agent templates
            FileSystemLoader(BASE_DIR),  # Load from package root for domain templates
        ]
    ),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_system_prompt(
    domain_prompt_path: Optional[str] = None,
    domain_prompt_paths: Optional[list[str]] = None,
    enable_toolmaker: bool = False,
) -> str:
    """
    Render the system prompt using Jinja2 template.

    Args:
        domain_prompt_path: Path to a single domain-specific prompt template (relative to package root,
                          e.g., "domains/powergrid/domain_prompt/powergrid.jinja").
                          Kept for backward compatibility.
        domain_prompt_paths: List of paths to domain-specific prompt templates. If both
                           domain_prompt_path and domain_prompt_paths are provided,
                           the single path is prepended to the list (deduped).
        enable_toolmaker: If True, include toolmaker-specific agent instructions.

    Returns:
        Rendered system prompt
    """
    # Build combined list of domain prompt paths
    all_paths: list[str] = []
    if domain_prompt_path:
        all_paths.append(domain_prompt_path)
    if domain_prompt_paths:
        for p in domain_prompt_paths:
            if p not in all_paths:
                all_paths.append(p)

    template = jinja_env.get_template("base_system_prompt.jinja")

    return template.render(
        domain_prompt_path=all_paths[0] if len(all_paths) == 1 else None,
        domain_prompt_paths=all_paths if all_paths else None,
        enable_toolmaker=enable_toolmaker,
    )
