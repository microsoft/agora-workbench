"""
==================================================
Example: Optimization With DWSim (Plan-Then-Execute, Autopilot)
==================================================

Uses :class:`PlanThenExecuteAgent` in autopilot mode for a DWSIM
simulation task.  Planning skills are auto-advertised via
``SkillsProvider``; domain skills (DWSIM sub-skills) are discovered
on demand via ``query_state_graph`` and loaded with ``load_skill``.
"""

import asyncio
from agent_bot.plan_then_execute import PlanThenExecuteAgent
from dotenv import load_dotenv
from log_config import setup_logging

setup_logging(__file__)
load_dotenv(verbose=True, override=True)

agent = PlanThenExecuteAgent(
    llm="gpt-5.4_2026-03-05",
    autopilot=True,
)


async def main():
    prompt = """
    - Create a DWSim model for production of ethyl acetate from acetic acid and ethanol at 100 kg/h product stream scale.
    - Determine the sensitivity of the process yield to the reactor temperature across a 100 K range from room temperature.

    - Build and execute the flowsheet using the dedicated tools. Use the DWSim code execution tool only as a last resort.
    - Make reasonable assumptions about unstated or ambiguous requirements. Make a reasonable assumption for Keq(T).
    """

    async with agent:
        result = await agent.go(prompt)
        print(f"\n✅ Final answer: {result.text}")


if __name__ == "__main__":
    asyncio.run(main())
