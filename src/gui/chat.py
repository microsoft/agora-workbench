"""SSE chat endpoint."""

import json
import logging

from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .map_state import read_map_state

LOGGER = logging.getLogger(__name__)

router = APIRouter()


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


async def _sse_generator(message: str, annotations: list[AnnotationPayload], viewport: ViewportPayload | None):
    """Yield SSE events — agent integration removed."""
    map_state = read_map_state()
    yield f"event: map_state\ndata: {json.dumps(map_state)}\n\n"
    yield f"event: done\ndata: {json.dumps({})}\n\n"


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
