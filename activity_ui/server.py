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
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from .auth import (
    NoOpTokenValidator,
    _get_validator,
    mint_stream_token,
    require_event_writer,
    require_stream_reader,
    set_stream_token_cookie,
    stream_token_expiry,
)
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

    @app.post("/events", dependencies=[Depends(require_event_writer)])
    async def ingest_event(event: ActivityEvent) -> dict[str, str]:
        await bus.publish(event)
        return {"status": "ok"}

    @app.post("/stream-token")
    async def issue_stream_token(request: Request) -> Response:
        """Mint a short-lived stream token for browser SSE access.

        This endpoint is NOT in EasyAuth excludedPaths, so the browser must
        be logged in via EasyAuth. The token is set as an HttpOnly cookie.
        """
        # Extract identity from EasyAuth-injected header (trusted on protected paths)
        subject = request.headers.get("X-MS-CLIENT-PRINCIPAL-ID")
        if not subject:
            # In local dev (NoOp mode), allow anonymous stream tokens
            if isinstance(_get_validator(), NoOpTokenValidator):
                subject = "anonymous"
            else:
                raise HTTPException(status_code=401, detail="Missing identity header")
        token = mint_stream_token(subject)
        response = Response(content='{"status":"ok"}', media_type="application/json")
        response.headers["Cache-Control"] = "no-store"
        set_stream_token_cookie(response, token)
        return response

    @app.get("/events/recent", dependencies=[Depends(require_stream_reader)])
    async def recent_events() -> JSONResponse:
        return JSONResponse([e.model_dump() for e in bus.snapshot()])

    @app.get("/stream", dependencies=[Depends(require_stream_reader)])
    async def stream(request: Request) -> EventSourceResponse:
        q = await bus.subscribe()

        # Determine when the stream token expires so we can close the connection
        token = request.cookies.get("activity_stream_token") or request.query_params.get("token")
        expiry = stream_token_expiry(token) if token else None

        async def event_generator():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    # Close stream when token expires (browser will reconnect with fresh token)
                    if expiry and datetime.now(timezone.utc) >= expiry:
                        yield {"event": "token_expired", "data": ""}
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
            except asyncio.CancelledError:
                # Expected when the client disconnects or the server shuts down; cleanup runs in finally.
                pass
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
