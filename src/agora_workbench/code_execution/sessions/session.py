"""Generic session container with common functionality."""

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, TypeVar, Generic

from .objects import ObjectStore

T = TypeVar("T")


class Session(Generic[T]):
    """
    Generic session container managing stateful data across MCP server interactions.

    Sessions provide lifecycle management, ownership tracking, and metadata storage for
    persistent resources like code execution environments, database connections, or
    computation state. Each session is owned by a specific user (identified via JWT token
    claims) and tracks access patterns, status transitions, and cleanup requirements.

    Attributes:
        session_id (str): Unique identifier for the session.
        data (T): The session's payload data, type-parameterized for type safety.
        session_type (str): Categorizes session type (e.g., "python", "database").
        user_identity (str): Owner's composite identifier from JWT token (``oid@tid``).
        user_token (str): User's bearer token for authentication.
        metadata (Dict): Optional key-value metadata for session configuration.
        token_claims (Dict): Optional cached JWT token claims for session authorization.
            These claims are used to restore authentication context without re-validating
            the JWT token. Intentionally excluded from get_info() for security.
        created_at (datetime): Timestamp when session was created.
        last_accessed (datetime): Timestamp of most recent session access.
        status (str): Current session state (e.g., "created", "active", "error").
        data_manager (DataLakeDataManager): Manager for DataLake asset access.

    Example:
        >>> from .session import Session
        >>> session = Session(
        ...     session_id="sess_123",
        ...     data={"counter": 0},
        ...     session_type="demo",
        ...     user_identity="user-oid-xyz",
        ...     user_token="eyJ...",
        ...     metadata={"version": "1.0"},
        ...     token_claims={"oid": "user-oid-xyz", "exp": 1234567890},
        ... )
        >>> session.touch()  # Update last accessed time
        >>> session.update_status("active")
        >>> info = session.get_info()
    """

    def __init__(
        self,
        session_id: str,
        data: T,
        session_type: str,
        user_identity: str,
        user_token: str,
        token_claims: Dict,
        metadata: Optional[Dict] = None,
    ):
        self.session_id = session_id
        self.data = data
        self.created_at = datetime.now()
        self.last_accessed = datetime.now()
        self.metadata = metadata or {}
        self.user_identity = user_identity
        self.user_token = user_token
        self.token_claims = token_claims
        self.status = "created"
        self.session_type = session_type
        self._asset_counter: int = 0
        self._status_history = [("created", datetime.now())]

        # Initialize data manager for DataLake asset access
        from ..data_access.manager import DataLakeDataManager

        self.data_manager = DataLakeDataManager()

        # Initialize object store for asset objects
        self.object_store = ObjectStore()

    def touch(self):
        """Update last accessed timestamp."""
        self.last_accessed = datetime.now()

    def update_status(self, new_status: str):
        """Update session status with history tracking."""
        self.status = new_status
        self._status_history.append((new_status, datetime.now()))
        self.touch()

    def get_info(self) -> Dict[str, Any]:
        """Return session information."""
        age_seconds = (datetime.now() - self.created_at).total_seconds()
        idle_seconds = (datetime.now() - self.last_accessed).total_seconds()

        return {
            "session_id": self.session_id,
            "session_type": self.session_type,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "last_accessed": self.last_accessed.isoformat(),
            "age_seconds": age_seconds,
            "idle_seconds": idle_seconds,
            "metadata": self.metadata,
            "user_identity": self.user_identity,
            "status_history": [{"status": s, "timestamp": t.isoformat()} for s, t in self._status_history],
        }

    def cleanup(self):
        """
        Cleanup session resources including session files.

        Raises:
            Exception: If cleanup fails, to allow calling code to handle the failure
        """
        # Clean up data manager cache directory
        self.data_manager.cleanup()

        # Call cleanup on data if it has a cleanup method
        if hasattr(self.data, "cleanup"):
            self.data.cleanup()  # pyright: ignore reportAttributeAccessIssue

        # Clean up session file if it exists
        if isinstance(self.data, dict) and "session_file" in self.data:
            session_file = Path(self.data["session_file"])
            if session_file.exists():
                # Remove the session file
                session_file.unlink()

                # Try to remove the parent directory if it's a temp directory for this session
                session_dir = session_file.parent
                if f"session_{self.session_id}" in str(session_dir):
                    try:
                        shutil.rmtree(session_dir)
                    except OSError:
                        # Directory might not be empty or already deleted, that's ok
                        pass
