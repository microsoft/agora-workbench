"""Storage backends for session management."""

from abc import ABC, abstractmethod
from typing import Dict, Optional, TYPE_CHECKING
import threading
import logging

if TYPE_CHECKING:
    from .session import Session

LOGGER = logging.getLogger(__name__)


class SessionStorageBackend(ABC):
    """Abstract interface for session storage."""

    @abstractmethod
    def store(self, session_id: str, session: "Session"):
        """Store a session."""
        pass

    @abstractmethod
    def retrieve(self, session_id: str) -> Optional["Session"]:
        """Retrieve a session by ID."""
        pass

    @abstractmethod
    def delete(self, session_id: str):
        """Delete a session by ID."""
        pass

    @abstractmethod
    def list_all(self) -> Dict[str, "Session"]:
        """List all sessions."""
        pass

    @abstractmethod
    def count(self) -> int:
        """Count total sessions."""
        pass


class InMemoryStorage(SessionStorageBackend):
    """Thread-safe in-memory storage."""

    def __init__(self):
        self._sessions: Dict[str, "Session"] = {}
        self._lock = threading.Lock()

    def store(self, session_id: str, session: "Session"):
        with self._lock:
            self._sessions[session_id] = session

    def retrieve(self, session_id: str) -> Optional["Session"]:
        with self._lock:
            return self._sessions.get(session_id)

    def delete(self, session_id: str):
        with self._lock:
            self._sessions.pop(session_id, None)

    def list_all(self) -> Dict[str, "Session"]:
        with self._lock:
            return dict(self._sessions)

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)
