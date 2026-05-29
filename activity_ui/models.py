"""Event schema shared by MCP-server publishers and the activity UI."""

from __future__ import annotations

import time
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

EventType = Literal[
    "code_executed",
    "code_failed",
    "job_started",
    "job_finished",
    "push_object_sent",
    "push_object_received",
    "tool_search",
    "skill_loaded",
    "workflow_planned",
    "batch_cancelled",
    "artifact_published",
]


class ActivityEvent(BaseModel):
    """A single event published by an MCP server to the activity UI.

    Flat schema with optional fields keyed by ``type``. The activity UI's
    frontend decides how to render each event using ``type`` + ``description``
    + the populated optional fields.
    """

    type: EventType
    server: str = Field(description="MCP server name (e.g. 'chemistry', 'gis')")
    timestamp: float = Field(default_factory=time.time, description="Unix timestamp")

    description: str = Field(default="", description="Human-readable summary shown to the user")

    # code_executed / code_failed
    code: Optional[str] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    success: Optional[bool] = None
    duration_ms: Optional[float] = None
    tool_calls: Optional[list[dict[str, Any]]] = None
    error: Optional[str] = None
    # Rich kernel outputs (matplotlib figures, images, SVGs, HTML).  Each
    # entry: ``{"mime_type": str, "data": str, "metadata": dict}``.
    # Rendered inline in the activity card; not sent to the agent.
    displays: Optional[list[dict[str, Any]]] = None
    # Files written to the session's outputs dir during this execute.  Each
    # entry: {name, size_bytes, mime_type, modified_at, download_url}.  The
    # UI renders a collapsed "artifacts (N)" disclosure with a download link
    # per entry; payload never contains the file bytes themselves.
    artifacts: Optional[list[dict[str, Any]]] = None

    # set on most events so the UI can group by session
    session_id: Optional[str] = None

    # background jobs
    job_id: Optional[str] = None

    # parallel_execute correlation (set on per-child code_executed/code_failed)
    batch_id: Optional[str] = None

    # push_object correlation
    transfer_id: Optional[str] = None
    variable_name: Optional[str] = None
    target_server: Optional[str] = None  # set on push_object_sent
    source_server: Optional[str] = None  # set on push_object_received

    # tool_search
    query: Optional[str] = None
    category: Optional[str] = None
    matched_tools: Optional[list[str]] = None
    matched_skills: Optional[list[str]] = None

    # skill_loaded
    skill_name: Optional[str] = None

    # workflow_planned
    domain: Optional[str] = None
    mode: Optional[str] = None
    current_state: Optional[str] = None
    target_state: Optional[str] = None
    tool_name: Optional[str] = None  # also reused if a workflow step targets a specific tool
