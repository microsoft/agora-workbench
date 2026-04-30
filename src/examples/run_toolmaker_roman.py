"""
==================================================
Example: ToolMaker Agent - Roman Numerals (Blind Test)
==================================================

This script tests the ToolMaker agent with the 'roman' library.
The agent does NOT see the expected outputs - those are in blind_test_fixtures.py.

After the agent creates the tool, we run BLIND VALIDATION against
hidden test cases to verify correctness.

Usage:
  cd AgoraAgentMAF
  uv run python -m examples.run_toolmaker_roman
"""

import asyncio
import shutil
import subprocess
from pathlib import Path

from agent_bot.toolmaker import ToolMakerAgent
from agent_bot.toolmaker.tests.blind_validator import run_blind_tests
from dotenv import load_dotenv


load_dotenv(verbose=True, override=True)

# Domain name used in this example
DOMAIN_NAME = "roman"

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
    # Simple prompt - agent discovers everything by exploring the repo
    # Ground truth is in blind_test_fixtures.py (agent never sees it)
    prompt = (
        "I want to create a tool from the GitHub repository "
        "https://github.com/zopefoundation/roman — it should expose a "
        "'to_roman' function that takes an integer and returns "
        "the Roman numeral string representation."
    )

    blind_test_results = None

    try:
        async with agent:
            result = await agent.go(prompt)
            print(f"\n✅ Agent finished: {result.text}")

            # Run blind validation against hidden test fixtures
            # The agent never saw these test cases!
            print("\n" + "="*60)
            print("RUNNING BLIND VALIDATION (agent never saw these tests)")
            print("="*60)
            blind_test_results = await run_blind_tests(DOMAIN_NAME, port=8010)

            if blind_test_results["total"] > 0:
                pct = 100 * blind_test_results["passed"] / blind_test_results["total"]
                print(f"\n📊 BLIND TEST SCORE: {blind_test_results['passed']}/{blind_test_results['total']} ({pct:.0f}%)")
            else:
                print(f"\n⚠️  No blind tests ran: {blind_test_results.get('error', 'unknown error')}")

    finally:
        # Clean up test artifacts after run (success or failure)
        cleanup_test_artifacts()

    return blind_test_results


if __name__ == "__main__":
    asyncio.run(main())
