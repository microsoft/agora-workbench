"""
==================================================
Example: Create Aurora Weather Prediction Tool
==================================================

This script demonstrates using AgoraAgent with ToolMaker to create
a weather prediction tool from Microsoft's Aurora foundation model.

Aurora (https://github.com/microsoft/aurora) is a large-scale foundation
model for Earth system forecasting. This example asks the agent to wrap
Aurora's prediction capabilities as a reusable MCP tool.

Usage:
  cd AgoraAgentMAF
  uv run python -m examples.run_aurora_toolmaker
"""

import asyncio
import logging

from agora import AgoraAgent
from dotenv import load_dotenv


load_dotenv(verbose=True, override=True)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("tools.toolmaker").setLevel(logging.INFO)
logging.getLogger("toolmaker").setLevel(logging.INFO)

agent = AgoraAgent(
    llm="gpt-5.1_2025-11-13",
    enable_toolmaker=True,
    toolmaker_llm="gpt-5.1_2025-11-13",
    max_iterations=500,
)


async def cli_input_handler(question: str, context: str = "") -> str:
    """Prompt the user for input when the agent asks a question."""
    if context:
        print(f"\n📋 Context: {context}")
    return input(f"\n❓ {question}\n> ")


async def main():
    prompt = (
        "I need a tool that can run weather predictions using "
        "Microsoft's Aurora foundation model. Create one from "
        "https://github.com/microsoft/aurora that can produce "
        "a short-range weather forecast given initial atmospheric conditions."
    )

    print(f"🚀 Sending prompt: {prompt}\n")

    async with agent:
        result = await agent.go(prompt, input_handler=cli_input_handler)
        print(f"\n✅ Final answer: {result.text}")


if __name__ == "__main__":
    asyncio.run(main())
