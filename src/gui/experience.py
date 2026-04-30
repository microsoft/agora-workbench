"""Experience file management — persistent per-user preferences and lessons.

Stores experience files under ``experiences/{user_id}.md``.  Each file is
plain Markdown that is later injected into the agent's user-message context
block so the agent can adapt to the user's preferences across sessions.
"""

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

LOGGER = logging.getLogger(__name__)

router = APIRouter()

# Per-user async lock to prevent concurrent read-modify-write races.
# The dict is bounded in practice because the current API only ever uses
# DEFAULT_USER; for future multi-user support the dict is capped and old
# entries are evicted when it grows beyond _MAX_LOCK_ENTRIES.
_user_locks: dict[str, asyncio.Lock] = {}
_locks_mutex = asyncio.Lock()
_MAX_LOCK_ENTRIES = 256

EXPERIENCES_DIR = Path(__file__).resolve().parents[1] / "gui" / "experiences"
EXPERIENCES_DIR.mkdir(exist_ok=True)

DEFAULT_USER = "default"


def _experience_path(user_id: str = DEFAULT_USER) -> Path:
    """Return the path to the experience file for a given user."""
    # Sanitize user_id to prevent path traversal
    safe_id = "".join(c for c in user_id if c.isalnum() or c in ("-", "_"))
    if not safe_id:
        safe_id = DEFAULT_USER
    return EXPERIENCES_DIR / f"{safe_id}.md"


async def _get_user_lock(user_id: str) -> asyncio.Lock:
    """Return (creating if needed) the per-user async lock.

    Evicts the oldest entry when the dict exceeds ``_MAX_LOCK_ENTRIES`` so
    the mapping stays bounded in long-running processes.
    """
    async with _locks_mutex:
        if user_id not in _user_locks:
            if len(_user_locks) >= _MAX_LOCK_ENTRIES:
                # Evict the first (oldest) entry — only safe while holding the
                # mutex and while the evicted lock is not held by another task.
                evict_key = next(iter(_user_locks))
                _user_locks.pop(evict_key, None)
            _user_locks[user_id] = asyncio.Lock()
        return _user_locks[user_id]


def read_experience(user_id: str = DEFAULT_USER) -> str:
    """Read the experience file for a user.  Returns empty string if none."""
    path = _experience_path(user_id)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


def write_experience(content: str, user_id: str = DEFAULT_USER) -> None:
    """Write the experience file atomically for a user.

    Uses a temp-file + replace pattern so a concurrent reader never sees a
    partial write.
    """
    path = _experience_path(user_id)
    dir_ = path.parent
    fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    LOGGER.info("Saved experience file: %s (%d chars)", path.name, len(content))


# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------


class ExperienceResponse(BaseModel):
    content: str


class ExperienceUpdateRequest(BaseModel):
    content: str


class SummarizeRequest(BaseModel):
    messages: list[dict] = Field(
        description="Conversation messages [{role, content}, ...]"
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/experience")
async def get_experience() -> ExperienceResponse:
    """Get the current experience file content."""
    lock = await _get_user_lock(DEFAULT_USER)
    async with lock:
        return ExperienceResponse(content=read_experience())


@router.put("/api/experience")
async def update_experience(req: ExperienceUpdateRequest) -> ExperienceResponse:
    """Update the experience file with user-provided content."""
    lock = await _get_user_lock(DEFAULT_USER)
    async with lock:
        write_experience(req.content)
    return ExperienceResponse(content=req.content)


@router.post("/api/experience/summarize")
async def summarize_experience(req: SummarizeRequest) -> ExperienceResponse:
    """Summarize a conversation into experience entries and merge them."""
    from .agent_lifecycle import get_agent

    if not req.messages:
        raise HTTPException(status_code=400, detail="No messages to summarize")

    # Format the conversation for the summarizer
    conversation = "\n".join(
        f"{'User' if m.get('role') == 'user' else 'Agent'}: {m.get('content', '')}"
        for m in req.messages
        if m.get("content", "").strip()
    )

    lock = await _get_user_lock(DEFAULT_USER)
    async with lock:
        existing = read_experience()

        prompt = f"""Analyze the following conversation between a user and a GIS map agent.
Extract reusable preferences, patterns, lessons, and instructions that should be
remembered for future sessions. Focus on:

- Map styling preferences (colors, point sizes, basemap choices, label preferences)
- Data handling patterns (preferred file formats, column mappings, data sources)
- Analysis workflows the user frequently requests
- Communication style preferences
- Domain-specific knowledge or terminology the user expects
- Lessons learned from mistakes or corrections during the conversation

{f'''Here is the user's existing experience file. Apply these rules:
- PRESERVE existing entries that are not contradicted by the conversation.
- UPDATE entries that the conversation contradicts or refines (e.g., if the
  user used to prefer blue points but now asks for red, update the preference).
- ADD new insights from the conversation that are not already covered.
- REMOVE entries only if the conversation explicitly says to stop doing something.

--- EXISTING EXPERIENCE ---
{existing}
--- END EXISTING EXPERIENCE ---''' if existing else "There is no existing experience file yet."}

--- CONVERSATION ---
{conversation}
--- END CONVERSATION ---

Output ONLY the updated experience file in Markdown format. Use clear headings and
bullet points. Keep it concise — this will be injected into every future session's
user-message context block, so brevity matters. If there is nothing new worth
remembering from this conversation, return the existing content unchanged (or empty
if none existed)."""

        try:
            agent = await get_agent()
            # Use the agent's underlying LLM client directly
            from agent_framework import Message as AFMessage

            response = await agent._client.get_response(
                [
                    AFMessage(role="system", text="You are a helpful assistant that extracts reusable preferences and lessons from conversations."),
                    AFMessage(role="user", text=prompt),
                ],
                stream=False,
            )
            new_content = response.text.strip() if response.text else existing
        except Exception:
            LOGGER.exception("Failed to summarize experience")
            raise HTTPException(
                status_code=500,
                detail="Failed to summarize experience.",
            )

        write_experience(new_content)
    return ExperienceResponse(content=new_content)
