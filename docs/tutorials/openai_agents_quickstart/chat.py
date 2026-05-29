"""Multi-turn chat REPL on top of the openai-agents quickstart.

Same setup as ``agent.py`` (chat client + chemistry MCP server + Agent), but
loops on stdin so you can have a back-and-forth conversation. History is
threaded through using ``Runner.run(input=..., previous_response_id=...)``
so the agent remembers prior turns across the session.

Run from the repo root:

    uv run python docs/tutorials/openai_agents_quickstart/chat.py
"""

from __future__ import annotations

import asyncio
import logging

from agent import (
    step_a_chat_client,
    step_b_data_lake_tool,
    step_c_chemistry_tool,
    step_d_build_agent,
)

LOGGER = logging.getLogger("openai_agents_quickstart.chat")


async def repl() -> int:
    from agents import Runner

    model = step_a_chat_client()
    await step_b_data_lake_tool()
    chemistry_server = await step_c_chemistry_tool()
    if chemistry_server is None:
        LOGGER.error(
            "Chemistry MCP server unreachable. Start it with: "
            "cd examples/domain_examples/chemistry && docker compose up -d"
        )
        return 1

    async with chemistry_server:
        agent = step_d_build_agent(model, [chemistry_server])
        print("\nChemistry quickstart chat. Type a question, blank line to quit.\n")

        previous_response_id: str | None = None
        while True:
            try:
                user_text = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user_text:
                break

            result = await Runner.run(
                agent,
                user_text,
                previous_response_id=previous_response_id,
            )
            print(f"\nagent> {result.final_output}\n")
            # `last_response_id` is the canonical OAI Responses thread anchor.
            # Falling back to None starts a fresh thread on the next turn.
            previous_response_id = getattr(result, "last_response_id", None)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(repl()))
