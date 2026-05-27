"""Meta tools for session management.

These tools allow agents to list, inspect, and close sessions.
They can be registered with any CodeExecutionServer that uses sessions.
"""

import json
import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, Any

if TYPE_CHECKING:
    from .manager import SessionManager

LOGGER = logging.getLogger(__name__)


def create_list_sessions_tool(session_manager: "SessionManager"):
    """
    Create a tool to list active sessions.

    Args:
        session_manager: SessionManager instance

    Returns:
        Tool function that can be registered with MCP
    """

    async def list_sessions(summary_only: bool = True) -> str:
        """
        List active sessions.

        Args:
            summary_only: If True, return only session_id, status, and age.
                         If False, return full session details.

        Returns:
            JSON string with list of sessions
        """
        try:
            sessions = session_manager.list_sessions()

            if summary_only:
                # Return minimal info
                summary = [
                    {
                        "session_id": s["session_id"],
                        "session_type": s["session_type"],
                        "status": s["status"],
                        "age_seconds": s["age_seconds"],
                        "idle_seconds": s["idle_seconds"],
                    }
                    for s in sessions
                ]
                return json.dumps({"success": True, "count": len(summary), "sessions": summary}, indent=2)
            else:
                # Return full details
                return json.dumps({"success": True, "count": len(sessions), "sessions": sessions}, indent=2)

        except Exception as e:
            LOGGER.error(f"Failed to list sessions: {e}", exc_info=True)
            return json.dumps({"success": False, "error": str(e)}, indent=2)

    return list_sessions


def create_get_session_info_tool(session_manager: "SessionManager"):
    """
    Create a tool to get detailed information about a specific session.

    Args:
        session_manager: SessionManager instance

    Returns:
        Tool function that can be registered with MCP
    """

    async def get_session_info(session_id: str) -> str:
        """
        Get detailed information about a specific session.

        Args:
            session_id: Session ID to inspect

        Returns:
            JSON string with session details
        """
        try:
            session = session_manager.get_session(session_id)
            info = session.get_info()

            return json.dumps({"success": True, "session": info}, indent=2)

        except ValueError as e:
            return json.dumps({"success": False, "error": f"Session not found: {e}"}, indent=2)
        except Exception as e:
            LOGGER.error(f"Failed to get session info: {e}", exc_info=True)
            return json.dumps({"success": False, "error": str(e)}, indent=2)

    return get_session_info


def create_close_session_tool(session_manager: "SessionManager"):
    """
    Create a tool to explicitly close a session.

    Args:
        session_manager: SessionManager instance

    Returns:
        Tool function that can be registered with MCP
    """

    async def close_session(session_id: str) -> str:
        """
        Close a session and free its resources.

        Args:
            session_id: Session ID to close

        Returns:
            JSON string with success status
        """
        try:
            session_manager.close_session(session_id)

            return json.dumps(
                {
                    "success": True,
                    "message": f"Session {session_id} closed successfully",
                    "remaining_sessions": session_manager.storage.count(),
                },
                indent=2,
            )

        except Exception as e:
            LOGGER.error(f"Failed to close session: {e}", exc_info=True)
            return json.dumps({"success": False, "error": str(e)}, indent=2)

    return close_session


def create_inspect_session_tool(
    session_manager: "SessionManager",
    inspector: Optional[Callable[[str], Awaitable[dict[str, Any]]]] = None,
):
    """Create a tool to inspect a session namespace and status."""

    async def inspect_session(session_id: str) -> str:
        try:
            # Validate session existence first (keeps behavior aligned with other meta tools).
            session_manager.get_session(session_id)

            if inspector is None:
                result = {
                    "session_id": session_id,
                    "status": "idle",
                    "job_id": None,
                    "job_status": None,
                    "namespace": {},
                }
            else:
                result = await inspector(session_id)

            return json.dumps({"success": True, **result}, indent=2)
        except ValueError as e:
            return json.dumps({"success": False, "error": f"Session not found: {e}"}, indent=2)
        except Exception as e:
            LOGGER.error(f"Failed to inspect session: {e}", exc_info=True)
            return json.dumps({"success": False, "error": str(e)}, indent=2)

    return inspect_session


def register_session_meta_tools(
    mcp,
    session_manager: "SessionManager",
    name_prefix: str,
    inspector: Optional[Callable[[str], Awaitable[dict[str, Any]]]] = None,
):
    """
    Register all session management meta tools with an MCP server.

    Args:
        mcp: FastMCP instance
        session_manager: SessionManager instance
        name_prefix: Prefix for tool names (e.g. "powergrid" -> "powergrid_list_sessions").
            Required to avoid name collisions when multiple servers register session tools.
    """
    # Create tools
    list_sessions = create_list_sessions_tool(session_manager)
    get_session_info = create_get_session_info_tool(session_manager)
    close_session = create_close_session_tool(session_manager)
    inspect_session = create_inspect_session_tool(session_manager, inspector=inspector)

    if not name_prefix:
        raise ValueError("name_prefix is required to avoid tool name collisions")

    # Build prefixed names
    prefix = f"{name_prefix}_"

    # Register with FastMCP
    mcp.tool(name=f"{prefix}list_sessions", description="List all active sessions")(list_sessions)

    mcp.tool(name=f"{prefix}get_session_info", description="Get detailed information about a specific session")(
        get_session_info
    )

    mcp.tool(name=f"{prefix}close_session", description="Close a session")(close_session)
    mcp.tool(
        name=f"{prefix}inspect_session",
        description="Inspect a session namespace, variable summaries, and background job status",
    )(inspect_session)

    LOGGER.info(f"Registered session meta tools with prefix '{name_prefix}'")
