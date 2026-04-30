"""SSE chat endpoint — streams tool-call events from GUIAgent."""

import asyncio
import json
import logging
import uuid

from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .agent_lifecycle import get_agent
from .map_state import read_map_state

LOGGER = logging.getLogger(__name__)

router = APIRouter()

# Async lock to serialize access to the singleton GUIAgent instance.
_AGENT_RUN_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class AnnotationPayload(BaseModel):
    label: str
    type: Literal["pin", "polygon"]
    geometry: dict


class ViewportPayload(BaseModel):
    center: tuple[float, float]
    zoom: float


class ChatRequest(BaseModel):
    message: str
    annotations: list[AnnotationPayload] = Field(default_factory=list)
    viewport: ViewportPayload | None = None


# ---------------------------------------------------------------------------
# SSE endpoint
# ---------------------------------------------------------------------------


def _format_annotations(annotations: list[AnnotationPayload]) -> str:
    """Format annotations into a human-readable context block for the agent."""
    lines = ["The user has placed the following annotations on the map:"]
    for a in annotations:
        if a.type == "pin" and a.geometry.get("type") == "Point":
            coords = a.geometry.get("coordinates", [])
            if len(coords) >= 2:
                lng, lat = coords[0], coords[1]
                lines.append(f"- {a.label}: Point at latitude {lat:.6f}, longitude {lng:.6f}")
        elif a.type == "polygon" and a.geometry.get("type") == "Polygon":
            rings = a.geometry.get("coordinates", [[]])
            if rings and rings[0]:
                vertices = rings[0]
                lats = [v[1] for v in vertices if isinstance(v, (list, tuple)) and len(v) >= 2]
                lngs = [v[0] for v in vertices if isinstance(v, (list, tuple)) and len(v) >= 2]
                if not lats:
                    continue
                n_vertices = len(vertices)
                # Closed ring: last == first, so subtract 1 for display
                if n_vertices > 1 and vertices[0] == vertices[-1]:
                    n_vertices -= 1
                vertex_str = ", ".join(
                    f"({v[1]:.6f}, {v[0]:.6f})" for v in vertices[:20]
                    if isinstance(v, (list, tuple)) and len(v) >= 2
                )
                bbox = (
                    f"Bounding box: {min(lats):.4f} to {max(lats):.4f} lat, "
                    f"{min(lngs):.4f} to {max(lngs):.4f} lon"
                )
                lines.append(
                    f"- {a.label}: Polygon with {n_vertices} vertices at [{vertex_str}]. {bbox}"
                )
    lines.append("When the user refers to these labels, use the corresponding coordinates.")
    return "\n".join(lines)


async def _sse_generator(message: str, annotations: list[AnnotationPayload], viewport: ViewportPayload | None):
    """Run GUIAgent and yield SSE events for tool calls, results, and final response."""
    event_queue: asyncio.Queue[dict | None] = asyncio.Queue()

    def _emit(event: dict) -> None:
        event_queue.put_nowait(event)

    async def _run_agent():
        try:
            agent = await get_agent()
            # Prepend annotation context when the user has placed pins/polygons
            augmented_message = message
            if annotations:
                annotation_context = _format_annotations(annotations)
                augmented_message = f"{annotation_context}\n\n{message}"
            # Serialize access to the singleton GUIAgent to avoid interleaving
            # conversation state and SSE events across concurrent requests.
            async with _AGENT_RUN_LOCK:
                response = await agent.run(
                    augmented_message,
                    event_callback=_emit,
                    viewport=(viewport.center, viewport.zoom) if viewport else None,
                )
            event_queue.put_nowait({"event": "response", "text": response})
        except Exception:
            error_id = str(uuid.uuid4())
            LOGGER.exception("Agent error [error_id=%s]", error_id)
            event_queue.put_nowait(
                {
                    "event": "error",
                    "message": "An internal error occurred.",
                    "error_id": error_id,
                }
            )
        finally:
            event_queue.put_nowait(None)  # sentinel

    task = asyncio.create_task(_run_agent())

    try:
        while True:
            evt = await event_queue.get()
            if evt is None:
                break
            event_type = evt.get("event", "message")
            yield f"event: {event_type}\ndata: {json.dumps(evt)}\n\n"

        map_state = read_map_state()
        yield f"event: map_state\ndata: {json.dumps(map_state)}\n\n"
        yield f"event: done\ndata: {json.dumps({})}\n\n"
    except Exception:
        LOGGER.exception("SSE generator error")
        yield f"event: error\ndata: {json.dumps({'message': 'An internal error occurred.'})}\n\n"
        yield f"event: done\ndata: {json.dumps({})}\n\n"
    finally:
        if not task.done():
            task.cancel()


@router.post("/api/chat")
async def chat(req: ChatRequest):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    # Lazily create a log file on first message of each session
    from .server import _ensure_log_file
    _ensure_log_file()

    return StreamingResponse(
        _sse_generator(req.message, req.annotations, req.viewport),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
