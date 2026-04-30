"""Tests for ObjectStore class - in-session object storage."""

from ...code_execution.sessions.objects import ObjectStore


class TestObjectStoreBasicOperations:
    """Test basic storage and retrieval operations."""

    def test_store_and_get_object(self):
        """Test storing and retrieving a basic object."""
        store = ObjectStore()
        store.store("key1", "value1")

        assert store.get("key1") == "value1"

    def test_store_overwrites_existing_key(self):
        """Test that storing with existing key overwrites the value."""
        store = ObjectStore()

        store.store("key", "original")
        assert store.get("key") == "original"

        store.store("key", "updated")
        assert store.get("key") == "updated"

    def test_get_nonexistent_key_returns_default(self):
        """Test getting a key that doesn't exist returns default value."""
        store = ObjectStore()

        assert store.get("nonexistent") is None
        assert store.get("nonexistent", "default") == "default"
        assert store.get("nonexistent", 42) == 42


class TestObjectStoreMetadata:
    """Test metadata handling."""

    def test_store_with_metadata(self):
        """Test storing objects with metadata."""
        store = ObjectStore()
        metadata = {"type": "dataframe", "shape": (100, 5)}

        store.store("data", {"some": "data"}, metadata=metadata)

        # Verify object is stored
        assert store.get("data") == {"some": "data"}

        # Verify metadata is stored
        assert store._metadata["data"] == metadata

    def test_store_without_metadata_creates_empty_dict(self):
        """Test that storing without metadata creates empty metadata dict."""
        store = ObjectStore()

        store.store("key", "value")

        assert "key" in store._metadata
        assert store._metadata["key"] == {}

    def test_metadata_overwrites_on_store(self):
        """Test that metadata is overwritten when key is reused."""
        store = ObjectStore()

        store.store("key", "value1", metadata={"version": 1})
        assert store._metadata["key"] == {"version": 1}

        store.store("key", "value2", metadata={"version": 2})
        assert store._metadata["key"] == {"version": 2}

    def test_none_metadata_creates_empty_dict(self):
        """Test that None metadata is converted to empty dict."""
        store = ObjectStore()

        store.store("key", "value", metadata=None)

        assert store._metadata["key"] == {}


class TestObjectStoreContains:
    """Test key existence checking."""

    def test_contains_existing_key(self):
        """Test that __contains__ returns True for existing key."""
        store = ObjectStore()
        store.store("key", "value")

        assert "key" in store
        assert ("key" in store) is True

    def test_contains_nonexistent_key(self):
        """Test that __contains__ returns False for nonexistent key."""
        store = ObjectStore()

        assert "nonexistent" not in store
        assert ("nonexistent" in store) is False

    def test_contains_after_delete(self):
        """Test that __contains__ returns False after deletion."""
        store = ObjectStore()
        store.store("key", "value")

        assert "key" in store

        store.delete("key")

        assert "key" not in store


class TestObjectStoreDeletion:
    """Test key deletion operations."""

    def test_delete_existing_key(self):
        """Test deleting an existing key."""
        store = ObjectStore()
        store.store("key", "value", metadata={"info": "data"})

        assert "key" in store

        store.delete("key")

        assert "key" not in store
        assert store.get("key") is None
        assert "key" not in store._metadata

    def test_delete_nonexistent_key_no_error(self):
        """Test that deleting nonexistent key doesn't raise error."""
        store = ObjectStore()

        # Should not raise
        store.delete("nonexistent")

    def test_delete_removes_both_object_and_metadata(self):
        """Test that delete removes both object and metadata."""
        store = ObjectStore()
        store.store("key", "value", metadata={"meta": "data"})

        assert "key" in store._objects
        assert "key" in store._metadata

        store.delete("key")

        assert "key" not in store._objects
        assert "key" not in store._metadata

    def test_delete_multiple_keys(self):
        """Test deleting multiple keys."""
        store = ObjectStore()
        store.store("key1", "value1")
        store.store("key2", "value2")
        store.store("key3", "value3")

        store.delete("key1")
        store.delete("key3")

        assert "key1" not in store
        assert "key2" in store
        assert "key3" not in store
        assert store.get("key2") == "value2"


class TestObjectStoreListKeys:
    """Test listing stored keys."""

    def test_list_keys_empty_store(self):
        """Test listing keys in empty store."""
        store = ObjectStore()

        keys = store.list_keys()

        assert keys == []
        assert isinstance(keys, list)

    def test_list_keys_single_key(self):
        """Test listing keys with single stored object."""
        store = ObjectStore()
        store.store("key1", "value1")

        keys = store.list_keys()

        assert keys == ["key1"]

    def test_list_keys_multiple_keys(self):
        """Test listing multiple keys."""
        store = ObjectStore()
        store.store("key1", "value1")
        store.store("key2", "value2")
        store.store("key3", "value3")

        keys = store.list_keys()

        assert len(keys) == 3
        assert set(keys) == {"key1", "key2", "key3"}

    def test_list_keys_after_delete(self):
        """Test listing keys after deletion."""
        store = ObjectStore()
        store.store("key1", "value1")
        store.store("key2", "value2")
        store.store("key3", "value3")

        store.delete("key2")

        keys = store.list_keys()

        assert len(keys) == 2
        assert set(keys) == {"key1", "key3"}

    def test_list_keys_returns_copy(self):
        """Test that list_keys returns a new list each time."""
        store = ObjectStore()
        store.store("key1", "value1")

        keys1 = store.list_keys()
        keys2 = store.list_keys()

        # Should be equal but not the same object
        assert keys1 == keys2
        assert keys1 is not keys2


class TestObjectStoreClear:
    """Test clearing all stored objects."""

    def test_clear_empty_store(self):
        """Test clearing an already empty store."""
        store = ObjectStore()

        # Should not raise
        store.clear()

        assert store.list_keys() == []

    def test_clear_removes_all_objects(self):
        """Test that clear removes all stored objects."""
        store = ObjectStore()
        store.store("key1", "value1")
        store.store("key2", "value2")
        store.store("key3", "value3")

        assert len(store.list_keys()) == 3

        store.clear()

        assert store.list_keys() == []
        assert store.get("key1") is None
        assert store.get("key2") is None
        assert store.get("key3") is None

    def test_clear_removes_all_metadata(self):
        """Test that clear removes all metadata."""
        store = ObjectStore()
        store.store("key1", "value1", metadata={"meta1": "data1"})
        store.store("key2", "value2", metadata={"meta2": "data2"})

        assert len(store._metadata) == 2

        store.clear()

        assert len(store._metadata) == 0

    def test_store_after_clear(self):
        """Test that store works normally after clear."""
        store = ObjectStore()
        store.store("key1", "value1")

        store.clear()

        store.store("key2", "value2")

        assert store.list_keys() == ["key2"]
        assert store.get("key2") == "value2"


class TestObjectStoreEdgeCases:
    """Test edge cases and special scenarios."""

    def test_empty_string_as_key(self):
        """Test using empty string as key."""
        store = ObjectStore()

        store.store("", "empty_key_value")

        assert "" in store
        assert store.get("") == "empty_key_value"

    def test_special_characters_in_keys(self):
        """Test keys with special characters."""
        store = ObjectStore()

        special_keys = [
            "key-with-dash",
            "key_with_underscore",
            "key.with.dots",
            "key/with/slashes",
            "key with spaces",
            "key@with@special",
            "key#123",
        ]

        for key in special_keys:
            store.store(key, f"value_for_{key}")

        for key in special_keys:
            assert key in store
            assert store.get(key) == f"value_for_{key}"

    def test_numeric_string_keys(self):
        """Test using numeric strings as keys."""
        store = ObjectStore()

        store.store("123", "numeric_string")
        store.store("3.14", "float_string")

        assert store.get("123") == "numeric_string"
        assert store.get("3.14") == "float_string"

    def test_object_mutability(self):
        """Test that stored objects maintain their mutability."""
        store = ObjectStore()

        # Store a mutable object
        original_list = [1, 2, 3]
        store.store("list", original_list)

        # Modify the original
        original_list.append(4)

        # The stored object should also be modified (same reference)
        assert store.get("list") == [1, 2, 3, 4]
        assert store.get("list") is original_list
