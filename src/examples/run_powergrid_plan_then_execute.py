"""
==================================================
Example: Power Grid Agent with MCP Code Execution
==================================================

This script demonstrates how to run a domain-specific PlanThenExecuteAgent
configured for power grid analysis.  Planning skills are auto-advertised
via ``SkillsProvider``; domain skills are discovered on demand via
``query_state_graph`` and loaded with ``load_skill``.
"""

import asyncio
from plan_then_execute import PlanThenExecuteAgent
from dotenv import load_dotenv
from log_config import setup_logging

setup_logging(__file__)
load_dotenv(verbose=True, override=True)


agent = PlanThenExecuteAgent(
    llm="gpt-5.1_2025-11-13",
)


async def main():
    prompt = """Run optimal power flow on a Texas-like grid using synthetic data.

Steps:
1. Use execute_powergrid_code to create a synthetic PyPSA network (4-5 buses, generators with costs, loads, lines) and save it to a .pkl file
2. Then call run_opf with the path to that saved file
3. Report the OPF results"""

    async with agent:
        result = await agent.go(prompt)
        print(f"\n✅ Final answer: {result.text}")


if __name__ == "__main__":
    asyncio.run(main())
