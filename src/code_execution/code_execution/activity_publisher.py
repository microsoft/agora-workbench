"""Fire-and-forget HTTP publisher that ships activity events to the UI service.

Behaviour:

- If ``ACTIVITY_UI_URL`` is unset, every ``publish()`` is a silent no-op.
- Otherwise events are buffered in an in-memory queue and drained by a
  background task that POSTs them to ``{ACTIVITY_UI_URL}/events``.
- HTTP failures are logged at DEBUG level and dropped — observability must
  never affect the hot path of code execution.
- The queue is bounded; overflow drops the *oldest* events (a backed-up UI
  shouldn't be allowed to OOM the server).

Wire it into ``CodeExecutionServer``:

.. code-block:: python

    self.activity_publisher = ActivityPublisher(server_name=config.name)
    # in your async startup:
    await self.activity_publisher.start()
    # later, when something happens:
    self.activity_publisher.publish_nowait({"type": "code_executed", ...})
    # on shutdown:
    await self.activity_publisher.stop()
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import time
from typing import Any, Optional

import httpx

LOGGER = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 500
DEFAULT_TIMEOUT_SECONDS = 2.0


class ActivityPublisher:
    """Posts activity events to the UI sidecar; silently no-ops if not configured."""

    def __init__(
        self,
        server_name: str,
        ui_url: Optional[str] = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.server_name = server_name
        self.ui_url = (ui_url if ui_url is not None else os.getenv("ACTIVITY_UI_URL", "")).rstrip("/")
        self._queue: collections.deque[dict[str, Any]] = collections.deque(maxlen=queue_size)
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._timeout = timeout_seconds
        self._stopped = False

    @property
    def enabled(self) -> bool:
        return bool(self.ui_url)

    async def start(self) -> None:
        if not self.enabled:
            LOGGER.debug("ActivityPublisher disabled (ACTIVITY_UI_URL not set)")
            return
        if self._task is not None:
            return
        self._client = httpx.AsyncClient(timeout=self._timeout)
        self._task = asyncio.create_task(self._drain_loop(), name="activity-publisher-drain")
        LOGGER.info("ActivityPublisher → %s", self.ui_url)

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self._timeout + 0.5)
            except asyncio.TimeoutError:
                self._task.cancel()
        if self._client is not None:
            await self._client.aclose()

    def publish_nowait(self, event: dict[str, Any]) -> None:
        """Enqueue an event for delivery. Always safe; never raises."""
        if not self.enabled:
            return
        event.setdefault("server", self.server_name)
        event.setdefault("timestamp", time.time())
        # deque with maxlen drops the oldest automatically — that's what we want.
        self._queue.append(event)
        self._wake.set()

    async def _drain_loop(self) -> None:
        assert self._client is not None
        url = f"{self.ui_url}/events"
        while not self._stopped:
            if not self._queue:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                self._wake.clear()
                continue

            event = self._queue.popleft()
            try:
                await self._client.post(url, json=event)
            except Exception as exc:  # noqa: BLE001 - intentional broad: observability is best-effort
                LOGGER.debug("ActivityPublisher POST failed: %s", exc)
