"""Decision log chat middleware.

This module provides :class:`DecisionLogChatMiddleware`, an Agora
:class:`~middleware.protocols.ChatMiddleware` that observes every LLM
round-trip and accumulates events in a buffer, synthesising a
:class:`~middleware.decision_log.DecisionLogEntry` at meaningful boundaries.

The middleware is framework-agnostic.  To use it inside a MAF agent, wrap it
with :func:`~middleware.decision_log.adapters.maf_protocols.wrap_chat_middleware`
and supply a :class:`~middleware.decision_log.adapters.maf_protocols.MAFChatClientAdapter`
as the *chat_client*:

    from middleware.decision_log import DecisionLog
    from middleware.decision_log.adapters import DecisionLogChatMiddleware
    from middleware.decision_log.adapters.maf_protocols import (
        MAFChatClientAdapter,
        wrap_chat_middleware,
    )

    log = DecisionLog()
    agora_mw = DecisionLogChatMiddleware(
        log,
        agent_name="my_agent",
        chat_client=MAFChatClientAdapter(maf_client),
    )
    maf_mw = wrap_chat_middleware(agora_mw)
    agent = Agent(..., middleware=[maf_mw])

Synthesis is performed by a small LLM via a FIFO background queue so that
it never blocks the main agent's execution.  The resulting entries are
appended to a shared :class:`~middleware.decision_log.DecisionLog` instance
that can be read by a companion
:class:`~middleware.decision_log.DecisionLogContextProvider` for context
injection.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from middleware.protocols import ChatClient, ChatContext, ChatMiddleware, Message

from ..entry import DecisionLogEntry
from ..log import DecisionLog

LOGGER = logging.getLogger(__name__)

# Maximum characters per message when formatting events for the synthesis LLM.
_MAX_MSG_CHARS = 1000


# ------------------------------------------------------------------
# LLM synthesis schema & prompt
# ------------------------------------------------------------------


class _SynthesisOutput(BaseModel):
    """Structured output expected from the synthesis LLM call."""

    summary: str = Field(
        description=("A single concise sentence (max 200 characters) capturing what the agent decided or did.")
    )
    evidence: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Flat dictionary of supporting facts (e.g. tool name, error code, "
            "status value). Values must be strings. Omit if there is nothing "
            "noteworthy."
        ),
    )


DECISION_SYNTHESIS_PROMPT = """\
You are a decision log recorder for an AI agent system.

Given a sequence of observed LLM chat events from one or more round-trips,
produce a concise, structured decision log entry that captures the semantic
intent of the agent's actions.

The agent typically writes Python code and sends it to tool functions like
``execute_*_code``.  Focus on *what* the agent decided to do and *why*,
not on low-level code details.

Respond with a JSON object that has exactly these fields:
- "summary": a single sentence (max 200 characters) describing what the
  agent decided or did
- "evidence": a flat JSON object with string values containing key supporting
  facts (e.g. tool names, error codes, status values); may be empty {}

Be factual. Do not add information that is not present in the observed events.
"""


def _strip_markdown_fence(text: str) -> str:
    """Remove surrounding markdown code-fence from *text* if present."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*\n?", "", stripped)
        stripped = re.sub(r"\n?```\s*$", "", stripped)
        stripped = stripped.strip()
    return stripped


# ------------------------------------------------------------------
# Event capture helpers
# ------------------------------------------------------------------


def _capture_event(context: ChatContext) -> dict[str, Any]:
    """Extract a lightweight snapshot of a single LLM round-trip.

    Args:
        context: The :class:`~middleware.protocols.ChatContext` after
            ``call_next()`` has completed.

    Returns:
        A dict with ``input_summary``, ``output_summary``, and
        ``has_tool_calls`` keys.
    """
    # Summarise input messages (last few, to avoid repeating full history)
    input_parts: list[str] = []
    for msg in context.messages[-3:]:
        if msg.content:
            input_parts.append(f"[{msg.role}] {msg.content[:_MAX_MSG_CHARS]}")

    # Summarise output (result message)
    output_parts: list[str] = []
    result = context.result
    if result and result.content:
        output_parts.append(f"[{result.role}] {result.content[:_MAX_MSG_CHARS]}")

    return {
        "input_summary": "\n".join(input_parts) if input_parts else "(no input)",
        "output_summary": "\n".join(output_parts) if output_parts else "(no output)",
        "has_tool_calls": bool(context.tool_calls),
    }


def _format_buffer(buffer: list[dict[str, Any]]) -> str:
    """Format accumulated events into a string for the synthesis LLM."""
    parts: list[str] = []
    for i, event in enumerate(buffer, 1):
        tool_marker = " [TOOL CALLS]" if event.get("has_tool_calls") else ""
        parts.append(
            f"--- Round-trip {i}{tool_marker} ---\nInput:\n{event['input_summary']}\nOutput:\n{event['output_summary']}"
        )
    return "\n\n".join(parts)


# ------------------------------------------------------------------
# Chat middleware
# ------------------------------------------------------------------


class DecisionLogChatMiddleware(ChatMiddleware):
    """Agora ChatMiddleware that records agent decisions at each LLM round-trip.

    Implements :class:`~middleware.protocols.ChatMiddleware` — wrap it with
    :func:`~middleware.decision_log.adapters.maf_protocols.wrap_chat_middleware`
    to use it inside a MAF agent.

    Events are accumulated in a buffer and synthesised into
    :class:`~middleware.decision_log.DecisionLogEntry` objects at meaningful
    boundaries:

    * When the LLM response contains **no tool calls** (end of a reasoning
      chain — the agent has reached a conclusion or is returning a result).
    * When the buffer reaches *max_buffer_size* (prevents unbounded
      accumulation during long tool-call sequences).

    Synthesis is delegated to a small LLM via a FIFO background queue, so
    it never blocks the main agent's execution.

    Args:
        decision_log: Shared :class:`~middleware.decision_log.DecisionLog`
            instance to append entries to.
        agent_name: Name of the agent being tracked.
        chat_client: :class:`~middleware.protocols.ChatClient` for the
            synthesis LLM.  Use
            :class:`~middleware.decision_log.adapters.maf_protocols.MAFChatClientAdapter`
            to wrap a MAF client.
        max_buffer_size: Maximum accumulated events before forcing a
            synthesis (default ``5``).
    """

    def __init__(
        self,
        decision_log: DecisionLog,
        agent_name: str,
        chat_client: ChatClient,
        max_buffer_size: int = 5,
    ) -> None:
        self._log = decision_log
        self._agent_name = agent_name
        self._chat_client = chat_client
        self._max_buffer_size = max_buffer_size

        self._buffer: list[dict[str, Any]] = []
        self._synthesis_queue: asyncio.Queue[tuple[str, str]] | None = None
        self._worker_task: asyncio.Task[None] | None = None

    async def process(
        self,
        context: ChatContext,
        call_next,
    ) -> None:
        """Intercept each LLM round-trip, accumulate, and synthesise at boundaries."""
        await call_next()

        self._buffer.append(_capture_event(context))
        has_tool_calls = self._buffer[-1]["has_tool_calls"]

        should_synthesize = not has_tool_calls or len(self._buffer) >= self._max_buffer_size

        if should_synthesize and self._buffer:
            events_text = _format_buffer(self._buffer)
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._buffer = []

            self._ensure_worker()
            await self._synthesis_queue.put((events_text, timestamp))

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    async def flush(self) -> None:
        """Wait for all pending background synthesis tasks to complete.

        Also forces synthesis of any buffered events that haven't yet
        reached a natural boundary.
        """
        # Flush any remaining buffered events
        if self._buffer:
            events_text = _format_buffer(self._buffer)
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._buffer = []

            self._ensure_worker()
            await self._synthesis_queue.put((events_text, timestamp))

        if self._synthesis_queue is not None:
            await self._synthesis_queue.join()

    async def aclose(self) -> None:
        """Flush pending work and shut down the background synthesis worker.

        After calling this method the middleware instance must not be
        reused.  Safe to call multiple times or when no worker has been
        started.
        """
        await self.flush()

        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
            self._synthesis_queue = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        """Lazily start the FIFO background synthesis worker."""
        if self._synthesis_queue is None:
            self._synthesis_queue = asyncio.Queue()
            self._worker_task = asyncio.create_task(self._synthesis_worker())

    async def _synthesis_worker(self) -> None:
        """Process synthesis requests sequentially in FIFO order."""
        try:
            while True:
                events_text, timestamp = await self._synthesis_queue.get()
                try:
                    entry = await self._synthesise_entry(events_text, timestamp)
                    if entry is not None:
                        self._log._append(entry)
                        LOGGER.debug(
                            "Decision log: recorded entry agent=%s summary=%r evidence=%s",
                            self._agent_name,
                            entry.summary,
                            dict(entry.evidence),
                        )
                except Exception:
                    LOGGER.exception("Decision log: synthesis worker failed for entry")
                finally:
                    self._synthesis_queue.task_done()
        except asyncio.CancelledError:
            LOGGER.debug("Decision log: synthesis worker cancelled")
            raise

    async def _synthesise_entry(self, events_text: str, timestamp: str) -> Optional[DecisionLogEntry]:
        """Use a small LLM to synthesise a decision log entry.

        Args:
            events_text: Pre-formatted string of observed events.
            timestamp: ISO 8601 UTC timestamp string.

        Returns:
            A :class:`DecisionLogEntry`, or ``None`` if synthesis fails.
        """
        messages = [
            Message(role="system", content=DECISION_SYNTHESIS_PROMPT),
            Message(
                role="user",
                content=(
                    "Given these observed events, produce a concise "
                    "decision log entry.\n\nObserved events:\n" + events_text
                ),
            ),
        ]

        try:
            raw_text = await self._chat_client.complete(messages)
        except Exception:
            LOGGER.exception("Decision log: LLM synthesis call failed; skipping entry")
            return None

        try:
            cleaned = _strip_markdown_fence(raw_text)
            output = _SynthesisOutput.model_validate_json(cleaned)
        except Exception:
            LOGGER.warning(
                "Decision log: could not parse LLM synthesis output; skipping entry. Raw text: %r",
                raw_text[:200],
            )
            return None

        return DecisionLogEntry(
            timestamp=timestamp,
            agent=self._agent_name,
            summary=output.summary,
            evidence=dict(output.evidence),
        )
