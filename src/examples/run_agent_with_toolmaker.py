"""
==================================================
Example: AgoraAgent with ToolMaker Enabled
==================================================

This script demonstrates how to run an AgoraAgent with enable_toolmaker=True.

When the agent encounters a request that no existing tool can handle, it will:
  1. Ask the user whether to create a reusable tool or just solve directly
  2. If creating a tool: invoke ToolMaker to build, test, and load a new MCP tool
  3. Use the new tool to answer the original question
  4. Ask the user whether to save the tool for future sessions

Usage:
  cd AgoraAgentMAF
  uv run python -m examples.run_agent_with_toolmaker
"""

import asyncio
import logging

from agora import AgoraAgent
from dotenv import load_dotenv


load_dotenv(verbose=True, override=True)

# Show ToolMaker progress (build, test, load steps)
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("tools.toolmaker").setLevel(logging.INFO)
logging.getLogger("toolmaker").setLevel(logging.INFO)

agent = AgoraAgent(
    llm="gpt-5.1_2025-11-13",
    enable_toolmaker=True,
    toolmaker_llm="gpt-5.1_2025-11-13",  # model used by the ToolMaker sub-agent
    max_iterations=500,
)


async def cli_input_handler(question: str, context: str = "") -> str:
    """Prompt the user for input when the agent asks a question."""
    if context:
        print(f"\n📋 Context: {context}")
    return input(f"\n❓ {question}\n> ")


async def main():
    prompt = (
        "I need a tool that can convert between integers and Roman numerals. "
        "Create one for me so I can use it in future tasks and then use it to give the roman value for 421."
    )

    print(f"🚀 Sending prompt: {prompt}\n")

    async with agent:
        result = await agent.go(prompt, input_handler=cli_input_handler)
        print(f"\n✅ Final answer: {result.text}")


if __name__ == "__main__":
    asyncio.run(main())
