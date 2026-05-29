"""FastAPI app that ingests events from MCP servers and streams them to a browser.

Endpoints:
    POST /events  — ingest one ActivityEvent (called by CodeExecutionServer.ActivityPublisher)
    GET  /stream  — Server-Sent Events stream for the browser
    GET  /events/recent — JSON snapshot of the in-memory event buffer
    GET  /        — static frontend (placeholder HTML for now; React later)

Run:
    uv run python -m activity_ui.server
    # or in docker-compose, see src/activity_ui/Dockerfile
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from .models import ActivityEvent

LOGGER = logging.getLogger(__name__)

# Ring buffer of recent events so a newly-connected browser sees history.
BUFFER_SIZE = int(os.getenv("ACTIVITY_UI_BUFFER", "200"))

# Per-subscriber bounded queue so a slow browser can't OOM the server.
SUBSCRIBER_QUEUE_SIZE = 100

STATIC_DIR = Path(__file__).parent / "static"


class EventBus:
    """In-process pub/sub for activity events."""

    def __init__(self, buffer_size: int = BUFFER_SIZE) -> None:
        self._buffer: collections.deque[ActivityEvent] = collections.deque(maxlen=buffer_size)
        self._subscribers: set[asyncio.Queue[ActivityEvent]] = set()
        self._lock = asyncio.Lock()

    async def publish(self, event: ActivityEvent) -> None:
        async with self._lock:
            self._buffer.append(event)
            dead: list[asyncio.Queue[ActivityEvent]] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    # Slow consumer — drop them rather than block everyone.
                    dead.append(q)
            for q in dead:
                self._subscribers.discard(q)

    async def subscribe(self) -> "asyncio.Queue[ActivityEvent]":
        q: asyncio.Queue[ActivityEvent] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        async with self._lock:
            # Replay history so the browser sees recent context on connect.
            for event in self._buffer:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    break
            self._subscribers.add(q)
        return q

    async def unsubscribe(self, q: "asyncio.Queue[ActivityEvent]") -> None:
        async with self._lock:
            self._subscribers.discard(q)

    def snapshot(self) -> list[ActivityEvent]:
        return list(self._buffer)


def create_app() -> FastAPI:
    app = FastAPI(title="Agora Activity UI", version="0.1.0")
    bus = EventBus()
    app.state.bus = bus

    @app.post("/events")
    async def ingest_event(event: ActivityEvent) -> dict[str, str]:
        await bus.publish(event)
        return {"status": "ok"}

    @app.get("/events/recent")
    async def recent_events() -> JSONResponse:
        return JSONResponse([e.model_dump() for e in bus.snapshot()])

    @app.get("/stream")
    async def stream(request: Request) -> EventSourceResponse:
        q = await bus.subscribe()

        async def event_generator():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=15.0)
                        yield {
                            "event": "activity",
                            "data": json.dumps(event.model_dump()),
                        }
                    except asyncio.TimeoutError:
                        # Heartbeat to keep proxies / browsers from closing the connection.
                        yield {"event": "ping", "data": ""}
            finally:
                await bus.unsubscribe(q)

        return EventSourceResponse(event_generator())

    @app.get("/health")
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # Static frontend (placeholder HTML for now; replaced by React build later).
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = int(os.getenv("ACTIVITY_UI_PORT", "8030"))
    host = os.getenv("ACTIVITY_UI_HOST", "0.0.0.0")
    LOGGER.info("Starting Activity UI on %s:%d", host, port)
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


app = create_app()


if __name__ == "__main__":
    main()
