"""
==================================================
Example: Agent Without Domain Prompt
==================================================

This script demonstrates an AgoraAgent with no domain prompt.
The agent auto-discovers MCP servers and their tools from server_registry.yaml.
It uses search_tools to find domain tools, then invokes them via
execute_code in the appropriate server's Python environment.

Domain skills are discovered on demand via ``query_state_graph`` and
loaded with ``load_skill``.

Tool-learning middleware is included:
  - VignetteRunMiddleware (agent-level): injects anti-pattern guardrails
    before each LLM call; tool names are discovered automatically.
  - VignetteFunctionMiddleware (function-level): validates tool-call args
    against hard constraints and applies repair templates on failure.
"""

import asyncio
from agora import AgoraAgent
from auth.auth import create_azure_credential
from dotenv import load_dotenv
from log_config import setup_logging
from middleware.tool_learning import (
    ToolLearningConfig,
    VignetteFunctionMiddleware,
    VignetteRunMiddleware,
)

setup_logging(__file__)


load_dotenv(verbose=True, override=True)

# Shared config + credential for both middleware
tl_config = ToolLearningConfig.from_env()
credential = create_azure_credential()

# Agent-level: injects guardrails before each LLM call
vignette_run_mw = VignetteRunMiddleware(config=tl_config, credential=credential)

# Function-level: validates args pre-call, repairs + learns on failure
vignette_fn_mw = VignetteFunctionMiddleware(config=tl_config, credential=credential)

# Create agent with no domain prompt.
# The agent auto-discovers MCP servers from server_registry.yaml.
agent = AgoraAgent(
    llm="gpt-5.1_2025-11-13",
    middleware=[vignette_run_mw, vignette_fn_mw],
)


async def main():
    prompt = """Analyze the topology of the network in texas_elec_no_flex_s100_c50_ec_lv1.0_1H_E.nc. 
    How many buses, lines, and generators does it have? Is the network fully connected? 
    Are there any critical lines (bridges) whose failure would disconnect the network?
"""

    async with agent:
        result = await agent.go(prompt)
        print(f"\n✅ Final answer: {result.text}")


if __name__ == "__main__":
    asyncio.run(main())
