"""Prompt rendering utilities for the ToolMaker agent."""

from pathlib import Path

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PackageLoader, select_autoescape

# AgoraAgentMAF root
BASE_DIR = Path(__file__).parent.parent.parent.parent.resolve()

jinja_env = Environment(
    loader=ChoiceLoader(
        [
            PackageLoader("agent_bot.toolmaker.prompts", ""),
            FileSystemLoader(BASE_DIR),
        ]
    ),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_exploration_prompt() -> str:
    """Render the system prompt for the exploration stage."""
    template = jinja_env.get_template("exploration_prompt.jinja")
    return template.render()


def render_implementation_prompt() -> str:
    """Render the system prompt for the implementation stage."""
    template = jinja_env.get_template("implementation_prompt.jinja")
    return template.render()


def render_registration_prompt() -> str:
    """Render the system prompt for the registration stage."""
    template = jinja_env.get_template("registration_prompt.jinja")
    return template.render()
