"""Event schema shared by MCP-server publishers and the activity UI."""

from __future__ import annotations

import time
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

EventType = Literal[
    "code_executed",
    "code_failed",
    "session_created",
    "session_closed",
    "job_started",
    "job_finished",
    "push_object_sent",
    "push_object_received",
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

    # session lifecycle
    session_id: Optional[str] = None

    # background jobs
    job_id: Optional[str] = None
    job_status: Optional[str] = None

    # push_object correlation
    transfer_id: Optional[str] = None
    variable_name: Optional[str] = None
    target_server: Optional[str] = None  # set on push_object_sent
    source_server: Optional[str] = None  # set on push_object_received
