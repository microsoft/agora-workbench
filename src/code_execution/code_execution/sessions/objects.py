"""In-session object storage for materialized assets and intermediate results."""

import logging
from typing import Any, Dict, Optional

LOGGER = logging.getLogger(__name__)


class ObjectStore:
    """
    In-session object storage for materialized assets and intermediate results.

    Provides a simple key-value store within a session for storing Python objects.
    """

    def __init__(self):
        """Initialize empty storage."""
        self._objects: Dict[str, Any] = {}
        self._metadata: Dict[str, Dict] = {}

    def store(self, key: str, obj: Any, metadata: Optional[Dict] = None):
        """
        Store an object in session storage.

        Args:
            key: Unique key for the object
            obj: Object to store (any Python object)
            metadata: Optional metadata about the object
        """
        self._objects[key] = obj
        self._metadata[key] = metadata or {}
        LOGGER.debug(f"Stored object '{key}' in session storage")

    def get(self, key: str, default: Any = None) -> Any:
        """
        Retrieve an object from storage.

        Args:
            key: Key to retrieve
            default: Value to return if key not found

        Returns:
            Stored object or default
        """
        return self._objects.get(key, default)

    def get_metadata(self, key: str) -> Dict[str, Any]:
        """
        Retrieve metadata for a stored object.

        Args:
            key: Key to retrieve metadata for

        Returns:
            Metadata dict, or empty dict if key not found
        """
        return self._metadata.get(key, {})

    def __contains__(self, key: str) -> bool:
        """Check if key exists in storage."""
        return key in self._objects

    def delete(self, key: str):
        """Delete an object from storage."""
        self._objects.pop(key, None)
        self._metadata.pop(key, None)

    def list_keys(self) -> list[str]:
        """List all keys in storage."""
        return list(self._objects.keys())

    def clear(self):
        """Clear all stored objects."""
        self._objects.clear()
        self._metadata.clear()
