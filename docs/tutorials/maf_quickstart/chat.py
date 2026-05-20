"""Multi-turn conversation REPL for the chemistry MCP server.

Same setup as ``agent.py`` (chat client + data lake tool + chemistry MCP tool),
but instead of running a single hardcoded prompt, this opens an interactive
loop. One ``AgentSession`` is created and reused, so the agent remembers
earlier messages in the same shell session.

Run from the repo root:

    uv run python docs/tutorials/maf_quickstart/chat.py

Usage in the prompt:
    you> list the chemistry tools
    agent> ...

    you> parse aspirin's SMILES and tell me its molecular weight
    agent> ...

    you> /quit          # or press Ctrl-D
"""

from __future__ import annotations

import asyncio
import logging
import sys

# Pull the setup steps from agent.py — same imports/path setup applies because
# importing agent.py executes its module-level sys.path and load_dotenv calls.
from agent import (  # type: ignore[import-not-found]
    step_a_chat_client,
    step_b_data_lake_tool,
    step_c_chemistry_tool,
    step_d_build_agent,
    _nullcontext,
)

LOGGER = logging.getLogger("maf_quickstart.chat")


async def conversation_loop(agent) -> None:
    """Multi-turn REPL with one AgentSession across all turns."""
    session = agent.create_session()

    print("\n" + "=" * 70)
    print("Chat with the chemistry agent.")
    print("Type a message and press Enter.  /quit or Ctrl-D to exit.")
    print("=" * 70 + "\n")

    while True:
        try:
            prompt = (await asyncio.to_thread(input, "you> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not prompt:
            continue
        if prompt.lower() in {"/quit", "/exit", "quit", "exit"}:
            break

        try:
            response = await agent.run(prompt, session=session)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive on transient errors
            print(f"\n[error] {exc}\n")
            continue

        text = response.text if hasattr(response, "text") else str(response)
        print(f"\nagent> {text}\n")


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    chat_client = step_a_chat_client()
    data_lake_tool = await step_b_data_lake_tool()
    chemistry_tool = await step_c_chemistry_tool()

    tools = [t for t in (data_lake_tool, chemistry_tool) if t is not None]
    if not tools:
        LOGGER.error(
            "No tools available. Configure the data lake and/or start the "
            "chemistry MCP server, then re-run."
        )
        return 1

    agent = step_d_build_agent(chat_client, tools)

    # Open the chemistry MCP tool's persistent connection for the whole session.
    async with chemistry_tool if chemistry_tool is not None else _nullcontext():
        await conversation_loop(agent)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
