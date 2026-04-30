"""MCP request handler utilities with session injection support."""

import asyncio
import logging
from typing import Any, Dict, Optional, Callable, TYPE_CHECKING
from functools import wraps

from .context import set_current_session

if TYPE_CHECKING:
    from .manager import SessionManager

LOGGER = logging.getLogger(__name__)


def with_session_injection(session_manager: "SessionManager", requires_session: bool = False):
    """
    Decorator to inject session from MCP request metadata into execution context.

    This decorator should wrap tool functions that need session access. It:
    1. Extracts session_id from request metadata (if present)
    2. Retrieves the session from the session manager
    3. Sets it in the execution context using ContextVar
    4. Executes the tool function
    5. Clears the context

    Args:
        session_manager: SessionManager instance to retrieve sessions from
        requires_session: If True, raises error if session_id not provided

    Example:
        @with_session_injection(session_manager, requires_session=True)
        async def solve_flowsheet(solver: str = "ipopt"):
            session = get_current_session()
            builder = session.data
            # ... use builder
    """
    # Import here to avoid circular import

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            # Extract session_id from kwargs (FastMCP may inject it)
            session_id = kwargs.pop("session_id", None)

            # Retrieve and inject session if provided
            session = None
            if session_id:
                try:
                    session = session_manager.get_session(session_id)
                    set_current_session(session)
                    LOGGER.debug(f"Injected session {session_id} into context for {func.__name__}")
                except ValueError as e:
                    LOGGER.error(f"Failed to retrieve session {session_id}: {e}")
                    if requires_session:
                        raise
            elif requires_session:
                raise ValueError(f"Tool {func.__name__} requires session_id in metadata, but none provided")

            try:
                # Execute the tool
                if asyncio.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)
                return result
            finally:
                # Clear session from context
                set_current_session(None)

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            # Same logic for sync functions
            session_id = kwargs.pop("session_id", None)

            session = None
            if session_id:
                try:
                    session = session_manager.get_session(session_id)
                    set_current_session(session)
                    LOGGER.debug(f"Injected session {session_id} into context for {func.__name__}")
                except ValueError as e:
                    LOGGER.error(f"Failed to retrieve session {session_id}: {e}")
                    if requires_session:
                        raise
            elif requires_session:
                raise ValueError(f"Tool {func.__name__} requires session_id in metadata, but none provided")

            try:
                result = func(*args, **kwargs)
                return result
            finally:
                set_current_session(None)

        # Return appropriate wrapper based on function type
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


def create_session_metadata_extractor():
    """
    Create a middleware-style function to extract session_id from MCP request.

    This can be integrated into FastMCP's request handling pipeline to
    automatically extract session_id from request metadata and inject it
    as a parameter.

    Note: The exact integration depends on FastMCP's middleware/hook support.
    """

    def extract_session_id(request: Dict[str, Any]) -> Optional[str]:
        """Extract session_id from MCP request metadata."""
        # MCP requests may include metadata at various levels
        # Check both top-level and params-level metadata

        # Check params.metadata
        params = request.get("params", {})
        metadata = params.get("metadata", {})
        session_id = metadata.get("session_id")

        if session_id:
            LOGGER.debug(f"Extracted session_id from request: {session_id}")
            return session_id

        # Check top-level metadata (fallback)
        metadata = request.get("metadata", {})
        session_id = metadata.get("session_id")

        if session_id:
            LOGGER.debug(f"Extracted session_id from top-level metadata: {session_id}")
            return session_id

        return None

    return extract_session_id
