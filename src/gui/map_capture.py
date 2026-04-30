"""Map capture coordination — async rendezvous between agent tool and frontend.

The ``capture_map_view`` FunctionTool emits a capture request via the SSE
event callback and then *awaits* the frontend's response (a PNG screenshot
posted to ``/api/map-capture``).  This module provides the shared state that
connects the two halves of that handshake.
"""

import asyncio
import logging

LOGGER = logging.getLogger(__name__)

# Pending capture requests: {request_id: asyncio.Future[bytes]}
# NOTE: This dict is process-local. The GUI backend must be run with a single
# Uvicorn worker (the default) to guarantee that the POST to /api/map-capture
# lands in the same process that created the Future.
_pending_captures: dict[str, asyncio.Future[bytes]] = {}

# Timeout for waiting on the frontend to respond (seconds).
# Must be generous enough to handle queued captures — each capture takes
# ~3.5s (fly animation + tile loading + screenshot), so a queue of 8
# needs ~28s for the last one.
CAPTURE_TIMEOUT = 60


def create_capture_request(request_id: str) -> asyncio.Future[bytes]:
    """Register a pending capture and return a Future the tool can await."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[bytes] = loop.create_future()
    _pending_captures[request_id] = future
    return future


def fulfill_capture_request(request_id: str, image_bytes: bytes) -> bool:
    """Called by the /api/map-capture endpoint to deliver the screenshot.

    Returns True if the request_id was found and fulfilled, False otherwise.
    """
    future = _pending_captures.pop(request_id, None)
    if future is None or future.done():
        LOGGER.warning("No pending capture for request_id=%s", request_id)
        return False
    future.set_result(image_bytes)
    LOGGER.info("Capture fulfilled for request_id=%s (%d bytes)", request_id, len(image_bytes))
    return True


def cancel_capture_request(request_id: str) -> None:
    """Clean up a pending capture (e.g. on timeout)."""
    future = _pending_captures.pop(request_id, None)
    if future is not None and not future.done():
        future.cancel()
