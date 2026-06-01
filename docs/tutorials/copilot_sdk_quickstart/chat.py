"""Multi-turn chat REPL on top of the Copilot SDK quickstart.

Same setup as ``agent.py`` (model + energysystems MCP + system message),
but loops on stdin. The ``CopilotClient`` session is created once and
reused across turns — history is carried by the session itself.

Run from the repo root:

    uv run python docs/tutorials/copilot_sdk_quickstart/chat.py
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent import (
    build_system_message,
    energy_mcp_config,
    resolve_llm,
)

LOGGER = logging.getLogger("copilot_sdk_quickstart.chat")


async def repl() -> int:
    from copilot import CopilotClient
    from copilot.session import AssistantMessageData, PermissionHandler

    model, provider = resolve_llm()
    energy = await energy_mcp_config()
    if energy is None:
        LOGGER.error(
            "Energysystems MCP server unreachable; exiting with status 1. "
            "Start it with: cd examples/domain_examples/energysystems && docker compose up -d"
        )
        return 1

    session_kwargs: dict[str, Any] = {
        "model": model,
        "mcp_servers": {"energysystems": energy},
        "system_message": build_system_message(),
    }
    if provider is not None:
        session_kwargs["provider"] = provider

    async with CopilotClient() as client:
        async with await client.create_session(
            # Tutorial-only: auto-approve every tool call. Safe here because the
            # only exposed surface is the sandboxed energysystems MCP kernel.
            # In production, replace with a custom handler that allowlists tools.
            on_permission_request=PermissionHandler.approve_all,
            **session_kwargs,
        ) as session:
            print("\nEnergy systems quickstart chat. Type a question, blank line to quit.\n")
            while True:
                try:
                    user_text = input("you> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not user_text:
                    break

                reply = await session.send_and_wait(user_text, timeout=300.0)
                content = "(no assistant.message returned)"
                if reply and isinstance(reply.data, AssistantMessageData):
                    content = reply.data.content
                print(f"\nagent> {content}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(repl()))
