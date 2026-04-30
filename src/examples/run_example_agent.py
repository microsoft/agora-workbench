"""
==================================================
Example: General Agent with MCP Code Execution
==================================================

This script demonstrates how to run an AgoraAgent configured
with the example domain. The agent auto-discovers MCP servers
from server_registry.yaml and gets execute_code + session
management tools for each registered server.
"""

import asyncio
from agent_bot.agora import AgoraAgent
from dotenv import load_dotenv
from log_config import setup_logging

setup_logging(__file__)
load_dotenv(verbose=True, override=True)


agent = AgoraAgent(
    domain_prompt_path="domains/example/domain_prompt/example.jinja",
    llm="gpt-5.2_2025-12-11",
)


async def main():
    prompt = """Test the counter tools:

1. Create a counter with initial value 10
2. Increment the counter by 5
3. Get the current counter value and report it

Also calculate the first 10 Fibonacci numbers."""

    async with agent:
        result = await agent.go(prompt)
        print(f"\n✅ Final answer: {result.text}")


if __name__ == "__main__":
    asyncio.run(main())
