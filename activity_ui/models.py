"""Event schema shared by MCP-server publishers and the activity UI."""

from __future__ import annotations

import time
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

EventType = Literal[
    "code_executed",
    "code_failed",
    "job_started",
    "job_finished",
    "object_sent",
    "object_received",
    "tool_search",
    "data_searched",
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

    # Stable per-event id, assigned on ingest. The UI dedupes on this so buffer
    # replays / SSE reconnects don't re-append the same events to the feed.
    id: str = Field(default_factory=lambda: uuid4().hex)
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
    # Files written to the outputs dir during this execute, surfaced as a
    # reminder + direct download button in the feed.  Each entry:
    # {name, mime_type, download_url}.  download_url is a token capability link
    # to the server's /artifacts endpoint (no auth on that route), so the user
    # can download a saved file directly without the agent publishing it.
    saved_files: Optional[list[dict[str, Any]]] = None

    # set on most events so the UI can group by session
    session_id: Optional[str] = None

    # background jobs
    job_id: Optional[str] = None

    # parallel_execute correlation (set on per-child code_executed/code_failed)
    batch_id: Optional[str] = None

    # object-transfer correlation
    transfer_id: Optional[str] = None
    variable_name: Optional[str] = None
    target_server: Optional[str] = None  # set on object_sent
    source_server: Optional[str] = None  # set on object_received

    # tool_search
    query: Optional[str] = None
    category: Optional[str] = None
    matched_tools: Optional[list[str]] = None
    matched_skills: Optional[list[str]] = None

    # data_searched: catalog data-search hits (dataset / file names)
    matched_artifacts: Optional[list[str]] = None

    # skill_loaded
    skill_name: Optional[str] = None

    # artifact_published: a file pushed to a publisher (GUI download, Blob, local).
    # remote_uri is the destination URI — an http(s) download URL for <gui>/<blob>
    # destinations (rendered as a link) or a filesystem path for <local>.
    artifact_name: Optional[str] = None
    destination: Optional[str] = None  # the raw tag, e.g. "<gui>results.csv</gui>"
    remote_uri: Optional[str] = None
    size_bytes: Optional[int] = None
    mime_type: Optional[str] = None

    # workflow_planned
    domain: Optional[str] = None
    mode: Optional[str] = None
    current_state: Optional[str] = None
    target_state: Optional[str] = None
    tool_name: Optional[str] = None  # also reused if a workflow step targets a specific tool
