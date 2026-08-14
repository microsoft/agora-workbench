"""
Session management for code execution servers.

This module provides infrastructure for managing stateful sessions across
multiple MCP tool calls, enabling complex multi-step workflows while keeping
the MCP interface stateless from the agent's perspective.

Key Features:
- Thread-safe session management with automatic cleanup
- Context-based session injection (no session_id in tool signatures)
- Pluggable storage backends (in-memory, Redis)
- Session lifecycle tracking and validation

Usage:
    # In server initialization
    from agora_workbench.code_execution.sessions import SessionManager, SessionConfig

    manager = SessionManager(SessionConfig(
        max_sessions=50,
        timeout_minutes=30
    ))

    # In tool implementation
    from agora_workbench.code_execution.sessions import get_current_session

    def my_tool():
        session = get_current_session()
        data = session.data
        # ... use data
"""

# Session implementation
from .session import Session, SessionContext

# Storage backends
from .storage import InMemoryStorage

# Session manager
from .manager import (
    SessionManager,
    SessionConfig,
    MaxSessionsReachedError,
    KERNEL_BOOTSTRAP_OUTPUTS,
    KERNEL_BOOTSTRAP_TOOL_PROXIES,
)

# Context management
from .context import (
    get_current_session,
    set_current_session,
    get_current_session_id,
    get_current_request_token,
    set_current_request_token,
    get_current_user_identity,
    set_current_user_identity,
    get_current_token_claims,
    set_current_token_claims,
    SessionNotFound,
)

# MCP integration
from .mcp_integration import with_session_injection, create_session_metadata_extractor

# Meta tools
from .meta_tools import (
    create_list_sessions_tool,
    create_get_session_info_tool,
    create_close_session_tool,
    create_inspect_session_tool,
    register_session_meta_tools,
)


__all__ = [
    # Session
    "Session",
    "SessionContext",
    # Storage
    "InMemoryStorage",
    # Manager
    "SessionManager",
    "SessionConfig",
    "MaxSessionsReachedError",
    "KERNEL_BOOTSTRAP_OUTPUTS",
    "KERNEL_BOOTSTRAP_TOOL_PROXIES",
    # Context
    "get_current_session",
    "set_current_session",
    "get_current_session_id",
    "get_current_request_token",
    "set_current_request_token",
    "get_current_user_identity",
    "set_current_user_identity",
    "get_current_token_claims",
    "set_current_token_claims",
    "SessionNotFound",
    # MCP integration
    "with_session_injection",
    "create_session_metadata_extractor",
    # Meta tools
    "create_list_sessions_tool",
    "create_get_session_info_tool",
    "create_close_session_tool",
    "create_inspect_session_tool",
    "register_session_meta_tools",
]
