"""
==================================================
Example: ToolMaker Agent
==================================================

This script demonstrates how to run the ToolMaker agent to create
a new MCP domain server from a GitHub repository.

The agent will:
  1. Explore the repository and ask what tool you want to create
  2. Generate all the domain server code (server, tool registry, tool impl)
  3. Build the Docker image, test the tool, and iterate until it works
  4. Register the domain in AgoraAgentMAF's config files

Usage:
  cd AgoraAgentMAF
  uv run python -m examples.run_toolmaker_humanize
"""

import asyncio
import shutil
import subprocess
from pathlib import Path

from toolmaker import ToolMakerAgent
from dotenv import load_dotenv


load_dotenv(verbose=True, override=True)

# Domain name used in this example
DOMAIN_NAME = "humanize"

# Directories created by the agent
AGORA_MAF_ROOT = Path(__file__).resolve().parent.parent
DOMAIN_DIR = AGORA_MAF_ROOT / "domains" / DOMAIN_NAME
WORKSPACE_DIR = Path("/tmp/toolmaker_workspace") / DOMAIN_NAME


def cleanup_test_artifacts():
    """Remove all files and folders created during the test run."""
    print("\n🧹 Cleaning up test artifacts...")

    # Stop and remove Docker container
    container_name = f"toolmaker-{DOMAIN_NAME}-server"
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True,
        timeout=30,
    )
    print(f"  - Removed container: {container_name}")

    # Remove domain directory
    if DOMAIN_DIR.exists():
        shutil.rmtree(DOMAIN_DIR)
        print(f"  - Removed: {DOMAIN_DIR}")

    # Remove workspace directory
    if WORKSPACE_DIR.exists():
        shutil.rmtree(WORKSPACE_DIR)
        print(f"  - Removed: {WORKSPACE_DIR}")

    print("✅ Cleanup complete")


agent = ToolMakerAgent(
    llm="gpt-5.1_2025-11-13",
    max_iterations=500,
)


async def main():
    # The ToolMaker agent is fully conversational — just describe what you want.
    # It will explore the repo and ask clarifying questions.
    #
    # Using python-humanize: a simple library that converts numbers/dates to
    # human-readable strings. Easy to test and verify.
    # Examples:
    #   humanize.naturalsize(1000000) → "1.0 MB"
    #   humanize.intword(123456789)   → "123.5 million"
    #   humanize.ordinal(3)           → "3rd"
    prompt = (
        "I want to create a tool from the GitHub repository "
        "https://github.com/python-humanize/humanize — it should expose a "
        "'humanize_number' function that takes an integer and a format type "
        "(one of 'intword', 'intcomma', 'ordinal', or 'apnumber') and returns "
        "the human-readable string representation."
    )

    try:
        async with agent:
            result = await agent.go(prompt)
            print(f"\n✅ Final answer: {result.text}")
    finally:
        # Clean up test artifacts after run (success or failure)
        cleanup_test_artifacts()


if __name__ == "__main__":
    asyncio.run(main())
