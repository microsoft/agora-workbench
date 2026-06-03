"""Context-based session management using ContextVars.

This module provides thread-safe access to the current session without
passing session_id as a parameter to every tool function.
"""

import logging
from contextvars import ContextVar
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .session import Session

LOGGER = logging.getLogger(__name__)


class SessionNotFound(Exception):
    """Raised when no session is active in the current context."""

    pass


# Thread-safe context variable for current session
_current_session: ContextVar[Optional["Session"]] = ContextVar("current_session", default=None)

# Thread-safe context variable for current request token (for session ownership validation)
_current_request_token: ContextVar[Optional[str]] = ContextVar("current_request_token", default=None)

# Thread-safe context variable for current user identity (extracted from validated token)
_current_user_identity: ContextVar[Optional[str]] = ContextVar("current_user_identity", default=None)

# Thread-safe context variable for validated token claims (to avoid re-validation)
_current_token_claims: ContextVar[Optional[dict]] = ContextVar("current_token_claims", default=None)


def get_current_session() -> "Session":
    """
    Get the session for the current execution context.

    This function retrieves the session that was injected by the MCP
    request handler. Tools should call this instead of receiving
    session_id as a parameter.

    Returns:
        Session: The active session

    Raises:
        SessionNotFound: If no session is active in current context

    Example:
        def my_tool() -> dict:
            session = get_current_session()
            builder = session.data
            # ... use builder

    Note:
        The session type is not validated - tools naturally fail fast
        if they try to use incompatible session data (duck typing).
    """
    session = _current_session.get()
    if session is None:
        raise SessionNotFound(
            "No active session in current context. Ensure session_id was provided in the MCP request."
        )
    return session


def set_current_session(session: Optional["Session"]):
    """
    Set the session for the current execution context.

    This is called by the MCP request handler to inject the session
    before executing a tool. Should not be called directly by tools.

    Args:
        session: Session to set, or None to clear
    """
    _current_session.set(session)
    if session:
        LOGGER.debug(f"Set current session: {session.session_id}")
    else:
        LOGGER.debug("Cleared current session")


def get_current_session_id() -> Optional[str]:
    """
    Get the session ID for the current context, if any.

    Returns:
        Session ID string, or None if no active session
    """
    session = _current_session.get()
    return session.session_id if session else None


def get_current_request_token() -> Optional[str]:
    """
    Get the current request authentication token from context.

    This is used for session ownership validation and data access operations

    Returns:
        Request token string or None if not available (e.g., local mode)
    """
    return _current_request_token.get()


def set_current_request_token(token: Optional[str]):
    """
    Set the current request authentication token in context.

    Should be called by middleware/auth layer when processing requests.

    Args:
        token: Request authentication token (typically Bearer token)
    """
    _current_request_token.set(token)
    if token:
        LOGGER.debug(f"Set request token in context (length: {len(token)})")
    else:
        LOGGER.debug("Cleared request token from context")


def get_current_user_identity() -> Optional[str]:
    """
    Get the current user identity from context (extracted from validated token).

    Returns:
        User identity (typically Entra ID OID) if available, None otherwise
    """
    return _current_user_identity.get()


def set_current_user_identity(user_identity: Optional[str]):
    """
    Set the current user identity in context.

    Should be called by authentication middleware after validating the token.

    Args:
        user_identity: Composite user identity from token claims (e.g., oid@tid)
    """
    _current_user_identity.set(user_identity)
    if user_identity:
        LOGGER.debug("Set user identity in context")
    else:
        LOGGER.debug("Cleared user identity from context")


def get_current_token_claims() -> Optional[dict]:
    """
    Get the validated token claims from context.

    Returns decoded JWT claims if available. This avoids redundant token validation.

    Returns:
        Dict of token claims if available, None otherwise
    """
    return _current_token_claims.get()


def set_current_token_claims(claims: Optional[dict]):
    """
    Set the validated token claims in context.

    Should be called by authentication middleware after successfully validating the token.
    This allows other parts of the code to use the validated claims without re-validation.

    Args:
        claims: Decoded JWT token claims dict
    """
    _current_token_claims.set(claims)
    if claims:
        LOGGER.debug("Set token claims in context")
    else:
        LOGGER.debug("Cleared token claims from context")
