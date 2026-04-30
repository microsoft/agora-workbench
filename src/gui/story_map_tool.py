"""FunctionTool for emitting a story map — a guided spatial walkthrough.

The agent calls ``present_story_map`` after completing a complex spatial
analysis (e.g. site selection, corridor comparison).  The tool emits a
``story_map`` SSE event to the frontend, which renders a step-through
viewer overlaid on the map.

Each step defines a map view (center + zoom), optional layer highlights,
and a narrative text that appears alongside the map.
"""

import json
import logging
import uuid
from typing import Any, Callable, Optional

from agent_framework import FunctionTool
from pydantic import BaseModel, Field

LOGGER = logging.getLogger(__name__)


class StoryMapStep(BaseModel):
    """A single step in a story map walkthrough."""

    title: str = Field(description="Short heading for this step (e.g. 'Candidate A: Bus 315857').")
    narrative: str = Field(
        description="Markdown text explaining what the user should observe at this view. Keep it concise — 2-4 sentences."
    )
    latitude: float = Field(description="Center latitude (WGS84) the map should fly to.")
    longitude: float = Field(description="Center longitude (WGS84) the map should fly to.")
    zoom: int = Field(
        default=14,
        ge=1,
        le=20,
        description="Zoom level. 8 = regional, 13 = city, 16 = neighborhood, 18 = building.",
    )
    highlight_layers: list[str] = Field(
        default_factory=list,
        description=(
            "Layer IDs to visually emphasise at this step. "
            "These must match existing layer IDs from the current map state. "
            "Non-highlighted layers will be dimmed (not hidden)."
        ),
    )


class StoryMapInput(BaseModel):
    """Input model for the ``present_story_map`` FunctionTool."""

    title: str = Field(description="Overall title for the story map (e.g. 'Data Center Siting Analysis').")
    steps: list[StoryMapStep] = Field(
        description="Ordered list of story map steps. Each step is one map view with narrative.",
        min_length=2,
    )


def create_story_map_function(
    event_callback_holder: dict[str, Optional[Callable[[dict], Any]]],
) -> FunctionTool:
    """Create a ``present_story_map`` :class:`FunctionTool`.

    Parameters
    ----------
    event_callback_holder : dict
        Mutable dict with key ``"callback"`` holding the SSE event callback.

    Returns
    -------
    FunctionTool
        Named ``present_story_map``.
    """

    async def present_story_map(title: str, steps: list[dict]) -> str:
        """Present an interactive story map — a guided spatial walkthrough.

        After completing a complex spatial analysis, use this tool to walk
        the user through your findings step by step on the map.  Each step
        flies the map to a location, optionally highlights specific layers,
        and shows narrative text explaining what to observe.

        The user can step forward/backward at their own pace.

        Args:
            title: Overall title (e.g. "Data Center Siting Comparison").
            steps: List of step objects, each with:
                - title: Short heading for the step.
                - narrative: Markdown explanation (2-4 sentences).
                - latitude, longitude: Center coordinates (WGS84).
                - zoom: Map zoom level (1-20).
                - highlight_layers: Optional list of layer IDs to emphasise.
        """
        callback = event_callback_holder.get("callback")
        if callback is None:
            return json.dumps({
                "error": "Story map presentation is not available (no SSE connection).",
                "hint": "This tool only works in the GUI agent with an active frontend.",
            })

        # Validate steps
        validated_steps = []
        for i, step in enumerate(steps):
            try:
                s = StoryMapStep(**step) if isinstance(step, dict) else step
                validated_steps.append(s.model_dump() if hasattr(s, "model_dump") else step)
            except Exception as e:
                return json.dumps({
                    "error": f"Invalid step {i}: {e}",
                    "hint": "Each step needs at least title, narrative, latitude, longitude.",
                })

        if len(validated_steps) < 2:
            return json.dumps({
                "error": "A story map needs at least 2 steps.",
                "hint": "Add more steps to create a meaningful walkthrough.",
            })

        story_id = str(uuid.uuid4())

        LOGGER.info(
            "present_story_map: emitting story '%s' with %d steps",
            title, len(validated_steps),
        )

        callback({
            "event": "story_map",
            "story_id": story_id,
            "title": title,
            "steps": validated_steps,
        })

        return json.dumps({
            "status": "success",
            "message": f"Story map '{title}' presented to the user with {len(validated_steps)} steps.",
            "story_id": story_id,
            "step_count": len(validated_steps),
        })

    return FunctionTool(
        name="present_story_map",
        description=(
            "Present an interactive story map — a guided spatial walkthrough. "
            "Use after completing a complex spatial analysis to walk the user "
            "through findings step by step on the map. Each step flies to a "
            "location, highlights relevant layers, and shows narrative text. "
            "The user can step forward and backward at their own pace."
        ),
        func=present_story_map,
        input_model=StoryMapInput,
        approval_mode="never_require",
    )
