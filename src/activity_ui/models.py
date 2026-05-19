"""Event schema shared by MCP-server publishers and the activity UI."""

from __future__ import annotations

import time
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

EventType = Literal[
    "code_executed",
    "code_failed",
    "job_started",
    "tools_listed",
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

    # set on most events so the UI can group by session
    session_id: Optional[str] = None

    # background jobs
    job_id: Optional[str] = None

    # parallel_execute correlation (set on per-child code_executed/code_failed)
    batch_id: Optional[str] = None

    # tools_listed
    tool_names: Optional[list[str]] = None

    # push_object correlation
    transfer_id: Optional[str] = None
    variable_name: Optional[str] = None
    target_server: Optional[str] = None  # set on push_object_sent
    source_server: Optional[str] = None  # set on push_object_received
