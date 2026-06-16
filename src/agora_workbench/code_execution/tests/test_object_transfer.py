"""Tests for server-to-server object transfer functionality."""

import dill
import pytest

from ..object_transfer import (
    ObjectSerializer,
)
from ..sessions.objects import ObjectStore


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
# ServerPublisher tests
# ---------------------------------------------------------------------------


class TestServerPublisher:
    """Tests for the ServerPublisher HTTP transfer logic."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_publish_constructs_correct_request(self, tmp_path):
        """Test that publish sends the correct payload to the target server."""
        from unittest.mock import AsyncMock, patch, MagicMock

        from ..data_access.publishers import ServerPublisher

        publisher = ServerPublisher(server_name="gis", target_url="http://localhost:8001")
        publisher._user_token = "test-token"
        publisher._source_server = "chemistry"
        publisher._transfer_id = "abc123"

        # Create a temp file with serialized data
        serialized = ObjectSerializer.serialize({"test": "data"})
        pkl_file = tmp_path / "data.pkl"
        pkl_file.write_bytes(serialized)

        import base64

        expected_b64 = base64.b64encode(serialized).decode("ascii")

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

            result = await publisher.publish(
                local_path=pkl_file,
                name="target_var",
                session_id="",
            )

            # Verify the HTTP call was made correctly
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert call_args[0][0] == "http://localhost:8001/object-transfer/receive"

            payload = call_args[1]["json"]
            assert payload["variable_name"] == "target_var"
            assert payload["data"] == expected_b64
            assert payload["metadata"]["source_server"] == "chemistry"
            assert payload["metadata"]["transfer_id"] == "abc123"

            headers = call_args[1]["headers"]
            assert headers["Authorization"] == "Bearer test-token"

            assert "Injected 'target_var' into gis kernel" in result

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_publish_strips_mcp_suffix(self, tmp_path):
        """Test that /mcp suffix is stripped before appending /object-transfer/receive."""
        from unittest.mock import AsyncMock, patch, MagicMock

        from ..data_access.publishers import ServerPublisher

        publisher = ServerPublisher(server_name="gis", target_url="http://localhost:8001/mcp")
        publisher._user_token = "token"
        publisher._source_server = "src"
        publisher._transfer_id = ""

        pkl_file = tmp_path / "data.pkl"
        pkl_file.write_bytes(b"data")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"success": True}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await publisher.publish(local_path=pkl_file, name="k", session_id="")
            url = mock_client.post.call_args[0][0]
            assert url == "http://localhost:8001/object-transfer/receive"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_publish_strips_mcp_trailing_slash(self, tmp_path):
        """Test that /mcp/ suffix (with trailing slash) is stripped correctly."""
        from unittest.mock import AsyncMock, patch, MagicMock

        from ..data_access.publishers import ServerPublisher

        publisher = ServerPublisher(server_name="gis", target_url="http://localhost:8001/mcp/")
        publisher._user_token = "token"
        publisher._source_server = "src"
        publisher._transfer_id = ""

        pkl_file = tmp_path / "data.pkl"
        pkl_file.write_bytes(b"data")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"success": True}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await publisher.publish(local_path=pkl_file, name="k", session_id="")
            url = mock_client.post.call_args[0][0]
            assert url == "http://localhost:8001/object-transfer/receive"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_publish_rejects_non_loopback_http(self, tmp_path):
        """Test that publish raises ValueError for plain-HTTP non-loopback targets."""
        from ..data_access.publishers import ServerPublisher

        publisher = ServerPublisher(server_name="gis", target_url="http://gis-server:8000")
        publisher._user_token = "secret-token"
        publisher._source_server = "src"
        publisher._transfer_id = ""

        pkl_file = tmp_path / "data.pkl"
        pkl_file.write_bytes(b"data")

        with pytest.raises(ValueError, match="Plain HTTP"):
            await publisher.publish(local_path=pkl_file, name="my_var", session_id="")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_publish_includes_auth_header_for_loopback_http(self, tmp_path):
        """Test that Authorization header is included for loopback HTTP targets."""
        from unittest.mock import AsyncMock, patch, MagicMock

        from ..data_access.publishers import ServerPublisher

        publisher = ServerPublisher(server_name="gis", target_url="http://127.0.0.1:8001")
        publisher._user_token = "test-token"
        publisher._source_server = "src"
        publisher._transfer_id = ""

        pkl_file = tmp_path / "data.pkl"
        pkl_file.write_bytes(b"data")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"success": True}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await publisher.publish(local_path=pkl_file, name="my_var", session_id="")

            headers = mock_client.post.call_args[1]["headers"]
            assert headers.get("Authorization") == "Bearer test-token", (
                "Bearer token should be forwarded to loopback HTTP targets"
            )

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_publish_includes_session_id_when_provided(self, tmp_path):
        """Test that session_id is included in payload when not empty."""
        from unittest.mock import AsyncMock, patch, MagicMock

        from ..data_access.publishers import ServerPublisher

        publisher = ServerPublisher(server_name="gis", target_url="http://localhost:8001")
        publisher._user_token = "token"
        publisher._source_server = "src"
        publisher._transfer_id = ""

        pkl_file = tmp_path / "data.pkl"
        pkl_file.write_bytes(b"data")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"success": True}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await publisher.publish(local_path=pkl_file, name="var", session_id="session-123")

            payload = mock_client.post.call_args[1]["json"]
            assert payload["session_id"] == "session-123"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_publish_raises_without_token(self, tmp_path):
        """Test that publish raises RuntimeError when no user token is set."""
        from ..data_access.publishers import ServerPublisher

        publisher = ServerPublisher(server_name="gis", target_url="http://localhost:8001")
        # No _user_token set

        pkl_file = tmp_path / "data.pkl"
        pkl_file.write_bytes(b"data")

        with pytest.raises(RuntimeError, match="_user_token"):
            await publisher.publish(local_path=pkl_file, name="var", session_id="")


# ---------------------------------------------------------------------------
# URL validation tests
# ---------------------------------------------------------------------------


class TestValidateTargetUrl:
    """Tests for the _validate_target_url helper."""

    @pytest.mark.unit
    def test_https_url_accepted(self):
        """HTTPS URLs with arbitrary hostnames should be accepted."""
        from ..object_transfer import _validate_target_url

        # Should not raise
        _validate_target_url("https://example.azurecontainerapps.io")
        _validate_target_url("https://example.azure.com/path")

    @pytest.mark.unit
    def test_http_loopback_accepted(self):
        """Plain HTTP is permitted for loopback addresses."""
        from ..object_transfer import _validate_target_url

        _validate_target_url("http://localhost:8001")
        _validate_target_url("http://127.0.0.1:8001")
        _validate_target_url("http://[::1]:8001")

    @pytest.mark.unit
    def test_http_non_loopback_rejected(self):
        """Plain HTTP to a non-loopback host must be rejected."""
        from ..object_transfer import _validate_target_url

        with pytest.raises(ValueError, match="Plain HTTP"):
            _validate_target_url("http://example.com")

    @pytest.mark.unit
    def test_allow_list_respected(self, monkeypatch):
        """Hostname allow-list blocks unlisted HTTPS hosts."""
        from ..object_transfer import _validate_target_url

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
        from ..object_transfer import _validate_target_url

        monkeypatch.setenv("OBJECT_TRANSFER_ALLOWED_HOSTS", "*.azurecontainerapps.io")

        # Loopback is always allowed regardless of allow-list
        _validate_target_url("http://localhost:9000")

    @pytest.mark.unit
    def test_allow_list_comma_separated(self, monkeypatch):
        """Allow-list patterns may be comma-separated."""
        from ..object_transfer import _validate_target_url

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
        from ..object_transfer import _validate_target_url

        # Pattern with mixed case and trailing dot should still match
        monkeypatch.setenv("OBJECT_TRANSFER_ALLOWED_HOSTS", "*.AzureContainerApps.IO.")
        _validate_target_url("https://myserver.azurecontainerapps.io")

    @pytest.mark.unit
    def test_missing_hostname_rejected(self):
        """URLs without a hostname must be rejected."""
        from ..object_transfer import _validate_target_url

        with pytest.raises(ValueError, match="hostname"):
            _validate_target_url("https:///path")

    @pytest.mark.unit
    def test_non_http_scheme_rejected(self):
        """Non-HTTP/HTTPS schemes must be rejected even for loopback."""
        from ..object_transfer import _validate_target_url

        with pytest.raises(ValueError, match="HTTP or HTTPS"):
            _validate_target_url("ftp://localhost:21")

        with pytest.raises(ValueError, match="HTTP or HTTPS"):
            _validate_target_url("ftp://example.com")

    @pytest.mark.unit
    def test_http_error_message_is_clear(self):
        """Error message for plain-HTTP rejection should clearly direct to HTTPS."""
        from ..object_transfer import _validate_target_url

        with pytest.raises(ValueError, match="HTTPS"):
            _validate_target_url("http://example.com")

    @pytest.mark.unit
    def test_trusted_http_host_accepted(self, monkeypatch):
        """Plain HTTP is allowed when the host is listed in OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS."""
        from ..object_transfer import _validate_target_url

        monkeypatch.setenv(
            "OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS",
            "chemistry-server,earthscience-server,energysystems-server",
        )
        _validate_target_url("http://chemistry-server:8000")
        _validate_target_url("http://earthscience-server:8000/mcp")
        _validate_target_url("http://energysystems-server:8000")

    @pytest.mark.unit
    def test_trusted_http_host_wildcard(self, monkeypatch):
        """Trusted-host patterns support the leading '*.' wildcard."""
        from ..object_transfer import _validate_target_url

        monkeypatch.setenv("OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS", "*.svc.cluster.local")
        _validate_target_url("http://chem.svc.cluster.local")

        # Unlisted host is still rejected.
        with pytest.raises(ValueError, match="Plain HTTP"):
            _validate_target_url("http://chem.example.com")

    @pytest.mark.unit
    def test_trusted_http_does_not_bypass_allow_list(self, monkeypatch):
        """A trusted HTTP host must still satisfy OBJECT_TRANSFER_ALLOWED_HOSTS when set."""
        from ..object_transfer import _validate_target_url

        monkeypatch.setenv("OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS", "chemistry-server")
        monkeypatch.setenv("OBJECT_TRANSFER_ALLOWED_HOSTS", "earthscience-server")

        # HTTP scheme is unblocked (trusted-host hit) but the allow-list rejects
        # this hostname, so validation still fails.
        with pytest.raises(ValueError, match="allowed-host"):
            _validate_target_url("http://chemistry-server:8000")

    @pytest.mark.unit
    def test_trusted_http_unset_rejects_non_loopback_http(self, monkeypatch):
        """With the trusted-host env var unset, plain HTTP to non-loopback is rejected."""
        from ..object_transfer import _validate_target_url

        monkeypatch.delenv("OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS", raising=False)
        with pytest.raises(ValueError, match="Plain HTTP"):
            _validate_target_url("http://chemistry-server:8000")

    @pytest.mark.unit
    def test_trust_http_allows_plain_http_without_env(self, monkeypatch):
        """trust_http=True permits plain HTTP to a non-loopback host with no env var.

        Models a peer whose http:// URL came from AGORA_PEER_REGISTRY: the
        operator already chose the scheme, so re-listing it in
        OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS is unnecessary.
        """
        from ..object_transfer import _validate_target_url

        monkeypatch.delenv("OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS", raising=False)
        # Without trust_http this would raise; with it, validation passes.
        _validate_target_url("http://earthscience-server:8000", trust_http=True)

    @pytest.mark.unit
    def test_trust_http_still_enforces_allow_list(self, monkeypatch):
        """trust_http=True only unblocks the scheme — the SSRF allow-list still applies."""
        from ..object_transfer import _validate_target_url

        monkeypatch.delenv("OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS", raising=False)
        monkeypatch.setenv("OBJECT_TRANSFER_ALLOWED_HOSTS", "earthscience-server")

        # Scheme is unblocked by trust_http, but the allow-list rejects this host.
        with pytest.raises(ValueError, match="allowed-host"):
            _validate_target_url("http://chemistry-server:8000", trust_http=True)

        # A host that satisfies the allow-list passes.
        _validate_target_url("http://earthscience-server:8000", trust_http=True)

