"""
==================================================
Example: Optimization With DWSim — AgoraAgent + Planning Module
==================================================

Variant of ``run_dwsim_opt.py`` that uses the standalone ``planning``
package to give a regular :class:`AgoraAgent` structured plan management
tools (add/insert/remove steps, dependencies, status tracking, etc.)
instead of relying on the three-stage :class:`PlanThenExecuteAgent`
workflow.

Planning skills (SKILL.md files under ``planning/skills/``) are
attached alongside the planning tools and auto-advertised via
``SkillsProvider``, giving the agent contextual guidance on *how* to
use the plan tools.  Domain-specific skills (e.g. DWSIM sub-skills)
are discovered on demand via ``query_state_graph`` and loaded with
``load_skill``.
"""

import asyncio
from pathlib import Path

import planning as _planning_pkg
from agora import AgoraAgent
from dotenv import load_dotenv
from log_config import setup_logging
from planning import PlanStore, create_plan_tools

setup_logging(__file__)
load_dotenv(verbose=True, override=True)

_PLANNING_SKILLS_DIR = Path(_planning_pkg.__file__).resolve().parent / "skills"


class PlanningAgoraAgent(AgoraAgent):
    """AgoraAgent extended with structured planning tools from the ``planning`` package."""

    def __init__(self, plan_store: PlanStore | None = None, **kwargs):
        # Advertise planning skills alongside the planning tools
        kwargs.setdefault(
            "skill_paths",
            [str(_PLANNING_SKILLS_DIR)] if _PLANNING_SKILLS_DIR.is_dir() else [],
        )
        super().__init__(**kwargs)
        self.plan_store = plan_store or PlanStore()

    def _build_tools(self) -> tuple[list, list[str]]:
        tools, errors = super()._build_tools()
        tools.extend(create_plan_tools(self.plan_store))
        return tools, errors


agent = PlanningAgoraAgent(
    llm="gpt-5.4_2026-03-05",
)


async def main():
    prompt = """
    - Create a DWSim model for production of ethyl acetate from acetic acid and ethanol at 100 kg/h product stream scale.
    - Determine the sensitivity of the process yield to the reactor temperature across a 100 K range from room temperature.

    - Build and execute the flowsheet using the dedicated tools. Use the DWSim code execution tool only as a last resort.
    - Make reasonable assumptions about unstated or ambiguous requirements. Make a reasonable assumption for Keq(T).

    Use the plan management tools to build a structured plan before execution,
    then track your progress through each step.
    """

    async with agent:
        result = await agent.go(prompt)
        print(f"\n✅ Final answer: {result.text}")
        print(f"\n📋 Final plan state:\n{agent.plan_store.view()}")


if __name__ == "__main__":
    asyncio.run(main())
