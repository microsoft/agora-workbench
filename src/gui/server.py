"""
FastAPI backend for the GIS Agent GUI.

Provides:
- POST /api/chat       — SSE endpoint: streams map state after chat
- GET  /api/map-state  — current map state (layers, view)
- GET  /api/layers/{f} — serve a GeoJSON layer file
- POST /api/reset      — reset session
- GET  /api/export-map  — export map as standalone HTML

The React frontend renders map layers with react-leaflet.

Launch:
    cd AgoraAgentMAF
    uv run uvicorn gui.server:app --host 0.0.0.0 --port 8000 --reload
"""

import logging
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi import Request as FastAPIRequest
from rio_tiler.errors import TileOutsideBounds
from dotenv import load_dotenv

from .map_state import invalidate_map_state
from .map_capture import fulfill_capture_request
from .chat import router as chat_router
from .data_catalog import router as data_catalog_router
from .layers import router as layers_router, TRANSPARENT_PNG
from .export_map import router as export_map_router
from .experience import router as experience_router
from .capabilities import router as capabilities_router

load_dotenv(verbose=True, override=True)

# ---------------------------------------------------------------------------
# Logging — write all logs to gui/logs/ with one file per chat session
# ---------------------------------------------------------------------------

_LOGS_DIR = Path(__file__).resolve().parent / "logs"
_LOGS_DIR.mkdir(exist_ok=True)
_current_file_handler: logging.Handler | None = None
_log_file_lock = threading.Lock()


def _ensure_log_file() -> None:
    """Create a log file if one isn't active. Called on each chat message."""
    global _current_file_handler
    with _log_file_lock:
        if _current_file_handler is not None:
            return
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        log_path = _LOGS_DIR / f"gui_{timestamp}.log"

        handler = logging.FileHandler(log_path, mode="w")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(name)-30s | %(levelname)-7s | %(message)s",
                datefmt="%H:%M:%S",
            )
        )

        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        _current_file_handler = handler

        # Quiet noisy HTTP loggers
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

        logging.getLogger(__name__).info("New session log: %s", log_path)


def _close_log_file() -> None:
    """Close the current log file. Next chat message will create a new one."""
    global _current_file_handler
    with _log_file_lock:
        if _current_file_handler is not None:
            logging.getLogger(__name__).info("Session ended — closing log file")
            logging.getLogger().removeHandler(_current_file_handler)
            _current_file_handler.close()
            _current_file_handler = None

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Agora GIS Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://localhost:\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include route modules
app.include_router(chat_router)
app.include_router(data_catalog_router)
app.include_router(layers_router)
app.include_router(export_map_router)
app.include_router(experience_router)
app.include_router(capabilities_router)


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------

@app.exception_handler(TileOutsideBounds)
async def tile_outside_bounds_handler(request, exc):
    return Response(content=TRANSPARENT_PNG, media_type="image/png")


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

@app.post("/api/reset")
async def reset():
    invalidate_map_state()
    _close_log_file()
    _ensure_log_file()
    return {"status": "ok", "map_state": None}


# ---------------------------------------------------------------------------
# Map capture — receives screenshots from the frontend
# ---------------------------------------------------------------------------

MAX_MAP_CAPTURE_BYTES = 5 * 1024 * 1024  # 5 MB limit for PNG uploads
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@app.post("/api/map-capture/{request_id}")
async def map_capture(request_id: str, request: FastAPIRequest):
    """Receive a PNG screenshot from the frontend for a pending capture request."""
    from fastapi import HTTPException

    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "image/png":
        raise HTTPException(status_code=415, detail="Content-Type must be image/png")

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_MAP_CAPTURE_BYTES:
                raise HTTPException(status_code=413, detail="PNG upload too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")

    body = bytearray()
    async for chunk in request.stream():
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) > MAX_MAP_CAPTURE_BYTES:
            raise HTTPException(status_code=413, detail="PNG upload too large")

    if not body:
        raise HTTPException(status_code=400, detail="Empty body")
    if not bytes(body).startswith(PNG_SIGNATURE):
        raise HTTPException(status_code=415, detail="Body is not a valid PNG upload")
    if not fulfill_capture_request(request_id, bytes(body)):
        raise HTTPException(status_code=404, detail="No pending capture for this request_id")
    return {"status": "ok"}
