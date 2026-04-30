"""Agent lifecycle — singleton GUIAgent management."""

import asyncio
import logging

from gui.agent import GUIAgent

LOGGER = logging.getLogger(__name__)

_agent: GUIAgent | None = None
_agent_lock = asyncio.Lock()
_init_task: asyncio.Task | None = None


def _create_agent() -> GUIAgent:
    return GUIAgent(
        llm="gpt-5.4",
        domain_prompt_path="domains/gis/domain_prompt/gis.jinja",
    )


async def _init_agent() -> None:
    """Initialize a new agent under the lock."""
    global _agent
    async with _agent_lock:
        if _agent is not None:
            return  # already initialized (race with get_agent)
        _agent = _create_agent()
        await _agent.__aenter__()
        await _agent.warm_up()
        LOGGER.info("Agent pre-initialized and ready")


async def get_agent() -> GUIAgent:
    global _agent, _init_task
    # If a background init is running, wait for it
    if _init_task is not None and not _init_task.done():
        await _init_task
        _init_task = None
    async with _agent_lock:
        if _agent is None:
            _agent = _create_agent()
            await _agent.__aenter__()
        return _agent


async def reset_agent():
    """Tear down the current agent and kick off background init of a new one."""
    global _agent, _init_task
    async with _agent_lock:
        if _agent is not None:
            await _agent.__aexit__(None, None, None)
            _agent = None
    # Start background init so the next chat message is fast
    _init_task = asyncio.create_task(_init_agent())
