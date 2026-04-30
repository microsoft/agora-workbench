"""Prompt rendering utilities for the plan-then-execute agent."""

from pathlib import Path

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PackageLoader, select_autoescape

# AgoraAgentMAF root
BASE_DIR = Path(__file__).parent.parent.parent.parent.resolve()

jinja_env = Environment(
    loader=ChoiceLoader(
        [
            PackageLoader("plan_then_execute.prompts", ""),
            FileSystemLoader(BASE_DIR),
        ]
    ),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_planning_prompt(*, autopilot: bool = False) -> str:
    """Render the system prompt for the planning stage."""
    template = jinja_env.get_template("planning_prompt.jinja")
    return template.render(autopilot=autopilot)


def render_execution_prompt(*, autopilot: bool = False) -> str:
    """Render the system prompt for the execution stage."""
    template = jinja_env.get_template("execution_prompt.jinja")
    return template.render(autopilot=autopilot)


def render_presentation_prompt(*, autopilot: bool = False) -> str:
    """Render the system prompt for the presentation stage."""
    template = jinja_env.get_template("presentation_prompt.jinja")
    return template.render(autopilot=autopilot)
