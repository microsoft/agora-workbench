"""FunctionTool for capturing the map view as a screenshot via the frontend.

The tool emits a ``capture_request`` event through the SSE event callback,
then waits for the frontend to screenshot the Leaflet map and POST the
PNG bytes to ``/api/map-capture``.

Because the OpenAI API does not support images in tool-result messages,
the captured image bytes are stored in `_image_holder` so that the agent
can inject them into the next user message as multimodal content.
"""

import asyncio
import json
import logging
import uuid
from typing import Any, Callable, Optional

try:
    from agent_framework import FunctionTool
except ImportError as e:
    raise ImportError(
        "agent-framework is required for MAF adapters. "
        "Install with: pip install agora-workbench[maf]"
    ) from e
from pydantic import BaseModel, Field

from ..map_capture import (
    CAPTURE_TIMEOUT,
    cancel_capture_request,
    create_capture_request,
)

LOGGER = logging.getLogger(__name__)


class CaptureMapViewInput(BaseModel):
    """Input model for the ``capture_map_view`` FunctionTool."""

    latitude: float = Field(description="Center latitude (WGS84) to zoom to.")
    longitude: float = Field(description="Center longitude (WGS84) to zoom to.")
    zoom: int = Field(
        default=14,
        ge=1,
        le=20,
        description=(
            "Zoom level. 1 = world, 8 = state/region, 13 = city, "
            "16 = neighborhood, 18 = building. Default 14 for site-level."
        ),
    )
    purpose: str = Field(
        default="",
        description=(
            "Brief description of what you want to observe in the screenshot "
            "(e.g. 'check land use around candidate site', 'verify road access'). "
            "This helps interpret the image."
        ),
    )


def create_capture_map_view_function(
    event_callback_holder: dict[str, Optional[Callable[[dict], Any]]],
    image_holder: dict[str, Any],
) -> FunctionTool:
    """Create a ``capture_map_view`` :class:`FunctionTool`.

    Parameters
    ----------
    event_callback_holder : dict
        Mutable dict with key ``"callback"`` holding the SSE event callback.
    image_holder : dict
        Mutable dict with key ``"pending_image"`` where the tool stores
        captured PNG bytes.  The agent's ``run()`` method reads this after
        the tool call completes and injects the image into the conversation.

    Returns
    -------
    FunctionTool
        Named ``capture_map_view``.
    """

    async def capture_map_view(
        latitude: float,
        longitude: float,
        zoom: int = 14,
        purpose: str = "",
    ) -> str:
        """Capture a screenshot of the map at the specified location.

        Moves the map to the given center and zoom level, then captures
        a screenshot including the basemap and all visible data layers.
        The image will be shown to you for visual analysis.

        Use this tool when you need to visually inspect a location —
        for example to verify land use, check proximity to infrastructure,
        or assess terrain features that aren't captured in vector data.

        Args:
            latitude: Center latitude (WGS84).
            longitude: Center longitude (WGS84).
            zoom: Zoom level (1-20). Default 14 for site inspection.
            purpose: What you want to observe (helps interpretation).
        """
        callback = event_callback_holder.get("callback")
        if callback is None:
            return json.dumps({
                "error": "Map capture is not available (no SSE connection).",
                "hint": "This tool only works in the GUI agent with an active frontend.",
            })

        request_id = str(uuid.uuid4())
        LOGGER.info(
            "capture_map_view: requesting screenshot zoom=%d purpose=%r",
            zoom, purpose,
        )

        # Register the async rendezvous point
        future = create_capture_request(request_id)

        # Emit the capture request via SSE to the frontend
        callback({
            "event": "capture_request",
            "request_id": request_id,
            "center": [latitude, longitude],
            "zoom": zoom,
            "purpose": purpose,
        })

        # Wait for the frontend to POST the screenshot
        try:
            image_bytes = await asyncio.wait_for(future, timeout=CAPTURE_TIMEOUT)
        except asyncio.TimeoutError:
            cancel_capture_request(request_id)
            LOGGER.warning("capture_map_view timed out for request_id=%s", request_id)
            return json.dumps({
                "error": "Map capture timed out — the frontend did not respond.",
                "hint": "Ensure the GUI frontend is open and connected.",
            })
        except asyncio.CancelledError:
            cancel_capture_request(request_id)
            return json.dumps({"error": "Map capture was cancelled."})

        LOGGER.info(
            "capture_map_view: received %d bytes for request_id=%s",
            len(image_bytes), request_id,
        )

        # Store the image for injection by the agent's run() method
        if image_holder.get("pending_images") is None:
            image_holder["pending_images"] = []
        image_holder["pending_images"].append({
            "image": image_bytes,
            "purpose": purpose or "general inspection",
            "center": [latitude, longitude],
            "zoom": zoom,
        })

        return json.dumps({
            "status": "success",
            "message": (
                f"Screenshot captured at ({latitude}, {longitude}) zoom {zoom}. "
                f"The image is now available for your visual analysis."
            ),
            "center": [latitude, longitude],
            "zoom": zoom,
            "purpose": purpose or "general inspection",
            "size_bytes": len(image_bytes),
        })

    return FunctionTool(
        name="capture_map_view",
        description=(
            "Capture a screenshot of the interactive map at a specified location "
            "and zoom level.  The screenshot includes the basemap (streets, "
            "satellite, or topographic tiles) and all visible data layers.  "
            "Use this to visually inspect locations — for example, to verify "
            "land use around a candidate site, check proximity to roads or "
            "water features, or assess terrain.  The image will be shown to "
            "you for visual analysis.  Only use this when visual context "
            "would add value beyond what the vector/tabular data provides.  "
            "Set 'purpose' to describe specifically what you are looking for "
            "so your analysis stays focused.  If the zoom level is too high "
            "or too low to see what you need, capture again at a different "
            "zoom — you can call this tool multiple times."
        ),
        approval_mode="never_require",
        func=capture_map_view,
        input_model=CaptureMapViewInput,
    )
