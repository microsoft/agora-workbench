"""
Abstract base classes for session management.

Defines interfaces that all session types and storage backends must implement.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, Optional


class BaseSession(ABC):
    """Abstract base class for all session types."""

    def __init__(self, session_id: str, data: Any, metadata: Optional[Dict] = None):
        self.session_id = session_id
        self.data = data
        self.created_at = datetime.now()
        self.last_accessed = datetime.now()
        self.metadata = metadata or {}
        self.status = "created"

    def touch(self):
        """Update last accessed timestamp."""
        self.last_accessed = datetime.now()

    def update_status(self, new_status: str):
        """Update session status."""
        self.status = new_status
        self.touch()

    @abstractmethod
    def get_info(self) -> Dict[str, Any]:
        """Return session information for listing/debugging."""
        pass

    @abstractmethod
    def cleanup(self):
        """Perform any necessary cleanup before session deletion."""
        pass


class SessionStorageBackend(ABC):
    """Abstract interface for session storage."""

    @abstractmethod
    def store(self, session_id: str, session: BaseSession):
        """Store a session."""
        pass

    @abstractmethod
    def retrieve(self, session_id: str) -> Optional[BaseSession]:
        """Retrieve a session by ID."""
        pass

    @abstractmethod
    def delete(self, session_id: str):
        """Delete a session by ID."""
        pass

    @abstractmethod
    def list_all(self) -> Dict[str, BaseSession]:
        """List all sessions."""
        pass

    @abstractmethod
    def count(self) -> int:
        """Count total sessions."""
        pass
