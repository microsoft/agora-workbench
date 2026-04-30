"""Tests for server-to-server object transfer functionality."""

import dill
import pytest

from ..code_execution.object_transfer import (
    ObjectSerializer,
    ObjectTransferClient,
)
from ..code_execution.sessions.objects import ObjectStore


# ---------------------------------------------------------------------------
# ObjectSerializer tests
# ---------------------------------------------------------------------------


class TestObjectSerializer:
    """Tests for serialization and deserialization of Python objects."""

    @pytest.mark.unit
    def test_serialize_simple_types(self):
        """Test serialization of basic Python types."""
        for obj in [42, 3.14, "hello", True, None, [1, 2, 3], {"a": 1}]:
            data = ObjectSerializer.serialize(obj)
            assert isinstance(data, bytes)
            assert len(data) > 0
            # Deserialization happens inside the kernel in production; use dill
            # directly here to verify the serialized format is correct.
            assert dill.loads(data) == obj

    @pytest.mark.unit
    def test_serialize_complex_object(self):
        """Test serialization of a more complex nested structure."""
        obj = {
            "name": "test_network",
            "nodes": [{"id": i, "value": float(i)} for i in range(100)],
            "metadata": {"version": 2, "nested": {"deep": True}},
        }
        data = ObjectSerializer.serialize(obj)
        result = dill.loads(data)
        assert result == obj

    @pytest.mark.unit
    def test_serialize_lambda(self):
        """Test serialization of a lambda (dill supports this, unlike pickle)."""
        fn = lambda x: x * 2  # noqa: E731
        data = ObjectSerializer.serialize(fn)
        restored = dill.loads(data)
        assert restored(5) == 10

    @pytest.mark.unit
    def test_serialize_class_instance(self):
        """Test serialization of a custom class instance."""

        class Counter:
            def __init__(self, value):
                self.value = value

            def increment(self):
                self.value += 1
                return self.value

        counter = Counter(10)
        counter.increment()

        data = ObjectSerializer.serialize(counter)
        restored = dill.loads(data)
        assert restored.value == 11
        assert restored.increment() == 12

    @pytest.mark.unit
    def test_serialize_non_serializable_raises(self):
        """Test that truly non-serializable objects raise TypeError."""
        # Active generators cannot be serialized even by dill
        gen = (x for x in range(10))
        next(gen)  # advance to make it a running generator
        with pytest.raises(TypeError, match="cannot be serialized"):
            ObjectSerializer.serialize(gen)

    @pytest.mark.unit
    def test_base64_roundtrip(self):
        """Test base64 encoding/decoding roundtrip."""
        original = b"\x80\x04\x95\x05\x00\x00\x00\x00\x00\x00\x00\x8c\x01a\x94."
        encoded = ObjectSerializer.to_base64(original)
        assert isinstance(encoded, str)
        decoded = ObjectSerializer.from_base64(encoded)
        assert decoded == original

    @pytest.mark.unit
    def test_full_roundtrip_with_base64(self):
        """Test full serialize → base64 → decode → deserialize roundtrip."""
        obj = {"key": "value", "numbers": [1, 2, 3]}
        serialized = ObjectSerializer.serialize(obj)
        encoded = ObjectSerializer.to_base64(serialized)
        decoded = ObjectSerializer.from_base64(encoded)
        # Deserialization in production occurs inside the kernel; use dill
        # directly here to confirm the payload round-trips correctly.
        result = dill.loads(decoded)
        assert result == obj


# ---------------------------------------------------------------------------
# ObjectStore get_metadata tests
# ---------------------------------------------------------------------------


class TestObjectStoreGetMetadata:
    """Tests for the get_metadata method."""

    @pytest.mark.unit
    def test_get_metadata_existing_key(self):
        """Test getting metadata for an existing key."""
        store = ObjectStore()
        store.store("key", "value", metadata={"type": "string", "source": "test"})

        meta = store.get_metadata("key")
        assert meta == {"type": "string", "source": "test"}

    @pytest.mark.unit
    def test_get_metadata_no_metadata(self):
        """Test getting metadata when none was provided."""
        store = ObjectStore()
        store.store("key", "value")

        meta = store.get_metadata("key")
        assert meta == {}

    @pytest.mark.unit
    def test_get_metadata_nonexistent_key(self):
        """Test getting metadata for a non-existent key returns empty dict."""
        store = ObjectStore()
        meta = store.get_metadata("nonexistent")
        assert meta == {}


# ---------------------------------------------------------------------------
# ObjectTransferClient tests
# ---------------------------------------------------------------------------


class TestObjectTransferClient:
    """Tests for the HTTP transfer client."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_push_constructs_correct_request(self):
        """Test that push sends the correct payload to the target server."""
        from unittest.mock import AsyncMock, patch, MagicMock

        client = ObjectTransferClient(user_token="test-token")

        serialized = ObjectSerializer.serialize({"test": "data"})
        expected_b64 = ObjectSerializer.to_base64(serialized)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"success": True, "variable_name": "target_var"}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await client.push(
                target_url="http://localhost:8001",
                variable_name="target_var",
                serialized_data=serialized,
                metadata={"source": "test"},
            )

            # Verify the HTTP call was made correctly
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert call_args[0][0] == "http://localhost:8001/object-transfer/receive"

            payload = call_args[1]["json"]
            assert payload["variable_name"] == "target_var"
            assert payload["data"] == expected_b64
            assert payload["metadata"]["source"] == "test"

            headers = call_args[1]["headers"]
            assert headers["Authorization"] == "Bearer test-token"

            assert result == {"success": True, "variable_name": "target_var"}

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_push_strips_trailing_slash(self):
        """Test that trailing slashes are handled in target URL."""
        from unittest.mock import AsyncMock, patch, MagicMock

        client = ObjectTransferClient(user_token="token")
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"success": True}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await client.push(
                target_url="http://localhost:8001/",
                variable_name="k",
                serialized_data=b"data",
            )
            url = mock_client.post.call_args[0][0]
            assert url == "http://localhost:8001/object-transfer/receive"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_push_strips_mcp_suffix(self):
        """Test that /mcp suffix is stripped before appending /object-transfer/receive."""
        from unittest.mock import AsyncMock, patch, MagicMock

        client = ObjectTransferClient(user_token="token")
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"success": True}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await client.push(
                target_url="http://localhost:8001/mcp",
                variable_name="k",
                serialized_data=b"data",
            )
            url = mock_client.post.call_args[0][0]
            assert url == "http://localhost:8001/object-transfer/receive"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_push_strips_mcp_trailing_slash_suffix(self):
        """Test that /mcp/ suffix (with trailing slash) is stripped correctly."""
        from unittest.mock import AsyncMock, patch, MagicMock

        client = ObjectTransferClient(user_token="token")
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"success": True}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await client.push(
                target_url="http://localhost:8001/mcp/",
                variable_name="k",
                serialized_data=b"data",
            )
            url = mock_client.post.call_args[0][0]
            assert url == "http://localhost:8001/object-transfer/receive"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_push_rejects_non_loopback_http(self):
        """Test that push raises ValueError for plain-HTTP non-loopback targets."""
        client = ObjectTransferClient(user_token="secret-token")

        with pytest.raises(ValueError, match="Plain HTTP"):
            await client.push(
                target_url="http://gis-server:8000",
                variable_name="my_var",
                serialized_data=b"data",
            )

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_push_includes_auth_header_for_loopback_http(self):
        """Test that Authorization header is included for loopback HTTP targets."""
        from unittest.mock import AsyncMock, patch, MagicMock

        client = ObjectTransferClient(user_token="test-token")
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"success": True}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await client.push(
                target_url="http://127.0.0.1:8001",
                variable_name="my_var",
                serialized_data=b"data",
            )

            headers = mock_client.post.call_args[1]["headers"]
            assert headers.get("Authorization") == "Bearer test-token", (
                "Bearer token should be forwarded to loopback HTTP targets"
            )


# ---------------------------------------------------------------------------
# URL validation tests
# ---------------------------------------------------------------------------


class TestValidateTargetUrl:
    """Tests for the _validate_target_url helper."""

    @pytest.mark.unit
    def test_https_url_accepted(self):
        """HTTPS URLs with arbitrary hostnames should be accepted."""
        from ..code_execution.object_transfer import _validate_target_url

        # Should not raise
        _validate_target_url("https://example.azurecontainerapps.io")
        _validate_target_url("https://example.azure.com/path")

    @pytest.mark.unit
    def test_http_loopback_accepted(self):
        """Plain HTTP is permitted for loopback addresses."""
        from ..code_execution.object_transfer import _validate_target_url

        _validate_target_url("http://localhost:8001")
        _validate_target_url("http://127.0.0.1:8001")
        _validate_target_url("http://[::1]:8001")

    @pytest.mark.unit
    def test_http_non_loopback_rejected(self):
        """Plain HTTP to a non-loopback host must be rejected."""
        from ..code_execution.object_transfer import _validate_target_url

        with pytest.raises(ValueError, match="Plain HTTP"):
            _validate_target_url("http://example.com")

    @pytest.mark.unit
    def test_allow_list_respected(self, monkeypatch):
        """Hostname allow-list blocks unlisted HTTPS hosts."""
        from ..code_execution.object_transfer import _validate_target_url

        monkeypatch.setenv("OBJECT_TRANSFER_ALLOWED_HOSTS", "*.azurecontainerapps.io")

        # Listed host passes
        _validate_target_url("https://myserver.azurecontainerapps.io")

        # Unlisted host raises
        with pytest.raises(ValueError, match="allowed-host"):
            _validate_target_url("https://attacker.example.com")

        # Base domain itself is not matched by wildcard
        with pytest.raises(ValueError, match="allowed-host"):
            _validate_target_url("https://azurecontainerapps.io")

    @pytest.mark.unit
    def test_allow_list_loopback_bypass(self, monkeypatch):
        """Loopback addresses bypass the allow-list."""
        from ..code_execution.object_transfer import _validate_target_url

        monkeypatch.setenv("OBJECT_TRANSFER_ALLOWED_HOSTS", "*.azurecontainerapps.io")

        # Loopback is always allowed regardless of allow-list
        _validate_target_url("http://localhost:9000")

    @pytest.mark.unit
    def test_allow_list_comma_separated(self, monkeypatch):
        """Allow-list patterns may be comma-separated."""
        from ..code_execution.object_transfer import _validate_target_url

        monkeypatch.setenv(
            "OBJECT_TRANSFER_ALLOWED_HOSTS",
            "*.azurecontainerapps.io,*.azure.com",
        )
        _validate_target_url("https://myserver.azurecontainerapps.io")
        _validate_target_url("https://myservice.azure.com")

        with pytest.raises(ValueError, match="allowed-host"):
            _validate_target_url("https://attacker.example.com")

    @pytest.mark.unit
    def test_allow_list_case_insensitive(self, monkeypatch):
        """Allow-list patterns with uppercase or trailing dots are normalized."""
        from ..code_execution.object_transfer import _validate_target_url

        # Pattern with mixed case and trailing dot should still match
        monkeypatch.setenv("OBJECT_TRANSFER_ALLOWED_HOSTS", "*.AzureContainerApps.IO.")
        _validate_target_url("https://myserver.azurecontainerapps.io")

    @pytest.mark.unit
    def test_missing_hostname_rejected(self):
        """URLs without a hostname must be rejected."""
        from ..code_execution.object_transfer import _validate_target_url

        with pytest.raises(ValueError, match="hostname"):
            _validate_target_url("https:///path")

    @pytest.mark.unit
    def test_non_http_scheme_rejected(self):
        """Non-HTTP/HTTPS schemes must be rejected even for loopback."""
        from ..code_execution.object_transfer import _validate_target_url

        with pytest.raises(ValueError, match="HTTP or HTTPS"):
            _validate_target_url("ftp://localhost:21")

        with pytest.raises(ValueError, match="HTTP or HTTPS"):
            _validate_target_url("ftp://example.com")

    @pytest.mark.unit
    def test_http_error_message_is_clear(self):
        """Error message for plain-HTTP rejection should clearly direct to HTTPS."""
        from ..code_execution.object_transfer import _validate_target_url

        with pytest.raises(ValueError, match="HTTPS"):
            _validate_target_url("http://example.com")

