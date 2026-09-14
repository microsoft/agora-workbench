"""Tests for server-to-server object transfer functionality."""

import asyncio
import base64
import dill
import hashlib
import json
import pytest
import tracemalloc

from agora_workbench.data_lake import (
    RequestContext,
    TransferCancelledError,
    TransferChecksumError,
    TransferLimitError,
    TransferOptions,
    TransferTimeoutError,
)
from ..object_transfer import (
    MAX_TRANSFER_BODY_BYTES,
    ObjectSerializer,
    STREAMING_TRANSFER_INFO_HEADER,
    STREAMING_TRANSFER_VERSION,
    STREAMING_TRANSFER_VERSION_HEADER,
    decode_streaming_transfer_info,
    encode_streaming_transfer_info,
    parse_object_transfer_version,
    parse_transfer_correlation_metadata,
    receive_legacy_streaming_transfer,
    receive_streaming_transfer,
)
from ..sessions.objects import ObjectStore


def _streaming_envelope(data: bytes) -> bytes:
    return b'{"variable_name":"value","data":"' + base64.b64encode(data) + b'","metadata":{}}'


async def _body_chunks(body: bytes, chunk_size: int = 64 * 1024):
    for offset in range(0, len(body), chunk_size):
        yield body[offset : offset + chunk_size]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_server", "https://user:password@example.com/source"),
        ("transfer_id", "https://example.com/object?sig=secret"),
        ("source_server", "source\nforged-log-entry"),
        ("transfer_id", "transfer\rforged-header"),
        ("source_server", "x" * 129),
        ("transfer_id", "x" * 129),
    ],
)
def test_transfer_correlation_metadata_rejects_unsafe_identifiers(field, value):
    metadata = {"source_server": "source-1", "transfer_id": "transfer_1:retry.2"}
    metadata[field] = value

    with pytest.raises(ValueError, match=r"\AInvalid object transfer correlation metadata\.\Z"):
        parse_transfer_correlation_metadata(metadata)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({}, (None, None)),
        ({"source_server": "", "transfer_id": ""}, (None, None)),
        ({"source_server": "source-1", "transfer_id": "transfer_1:retry.2"}, ("source-1", "transfer_1:retry.2")),
    ],
)
def test_transfer_correlation_metadata_preserves_valid_compatibility(metadata, expected):
    assert parse_transfer_correlation_metadata(metadata) == expected


@pytest.mark.unit
@pytest.mark.parametrize("version", ["", "3", "20", "v2", "2 ", " 2", "2.0", "2,3"])
def test_object_transfer_version_rejects_every_present_non_v2_token(version):
    with pytest.raises(ValueError, match=r"\AUnsupported object transfer version\.\Z"):
        parse_object_transfer_version(version)


@pytest.mark.unit
def test_object_transfer_version_preserves_absent_v1_and_exact_v2():
    assert parse_object_transfer_version(None) is False
    assert parse_object_transfer_version(STREAMING_TRANSFER_VERSION) is True


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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streaming_receiver_peak_memory_is_independent_of_payload_size(tmp_path):
    data = b"x" * (16 * 1024 * 1024)
    body = _streaming_envelope(data)
    destination = tmp_path / "received.pkl"

    tracemalloc.start()
    try:
        result = await receive_streaming_transfer(
            _body_chunks(body),
            destination,
            expected_size=len(data),
            expected_sha256=hashlib.sha256(data).hexdigest(),
            options=TransferOptions(
                max_bytes=len(data),
                chunk_size=64 * 1024,
                expected_sha256=hashlib.sha256(data).hexdigest(),
            ),
            context=RequestContext(),
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.bytes_transferred == len(data)
    assert destination.stat().st_size == len(data)
    assert peak < 2 * 1024 * 1024


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_receiver_peak_memory_is_independent_of_payload_size(tmp_path):
    data = b"x" * (16 * 1024 * 1024)
    body = _streaming_envelope(data)
    destination = tmp_path / "received.pkl"

    tracemalloc.start()
    try:
        envelope = await receive_legacy_streaming_transfer(
            _body_chunks(body),
            destination,
            options=TransferOptions(max_bytes=len(data), chunk_size=64 * 1024),
            context=RequestContext(),
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert envelope.variable_name == "value"
    assert envelope.result.bytes_transferred == len(data)
    assert destination.stat().st_size == len(data)
    assert peak < 2 * 1024 * 1024


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b'{"variable_name":"value","data":"!!!!","metadata":{}}',
        b'{"variable_name":"value","data":"',
        b'{"variable_name":"value","data":"YQ==","metadata":{},"extra":true}',
        b'{"variable_name":"value","data":"YQ==","metadata":{},"metadata":{}}',
    ],
)
async def test_legacy_receiver_rejects_malformed_envelopes_and_cleans_partial(tmp_path, body):
    destination = tmp_path / "received.pkl"

    with pytest.raises(ValueError):
        await receive_legacy_streaming_transfer(
            _body_chunks(body, 2),
            destination,
            options=TransferOptions(),
            context=RequestContext(),
        )

    assert not destination.exists()
    assert list(tmp_path.glob(".*.part")) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_receiver_rejects_chunked_encoded_overflow_and_cleans_partial(tmp_path, monkeypatch):
    from .. import object_transfer as object_transfer_module

    monkeypatch.setattr(object_transfer_module, "MAX_TRANSFER_BODY_BYTES", 32)
    destination = tmp_path / "received.pkl"

    with pytest.raises(TransferLimitError):
        await receive_legacy_streaming_transfer(
            _body_chunks(_streaming_envelope(b"x" * 64), 5),
            destination,
            options=TransferOptions(max_bytes=64),
            context=RequestContext(),
        )

    assert not destination.exists()
    assert list(tmp_path.glob(".*.part")) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_receiver_accepts_exact_encoded_body_boundary(tmp_path, monkeypatch):
    from .. import object_transfer as object_transfer_module

    body = _streaming_envelope(b"content")
    monkeypatch.setattr(object_transfer_module, "MAX_TRANSFER_BODY_BYTES", len(body))
    destination = tmp_path / "received.pkl"

    envelope = await receive_legacy_streaming_transfer(
        _body_chunks(body, 3),
        destination,
        options=TransferOptions(),
        context=RequestContext(),
    )

    assert envelope.result.bytes_transferred == len(b"content")
    assert destination.read_bytes() == b"content"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_receiver_accepts_json_whitespace_compatibility(tmp_path):
    data = b"content"
    body = json.dumps(
        {
            "variable_name": "value",
            "data": base64.b64encode(data).decode(),
            "metadata": {},
        }
    ).encode()
    destination = tmp_path / "received.pkl"

    envelope = await receive_legacy_streaming_transfer(
        _body_chunks(body, 3),
        destination,
        options=TransferOptions(),
        context=RequestContext(),
    )

    assert envelope.variable_name == "value"
    assert destination.read_bytes() == data


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streaming_receiver_rejects_malformed_base64_and_cleans_partial(tmp_path):
    destination = tmp_path / "received.pkl"
    body = b'{"variable_name":"value","data":"AAAA!!!!","metadata":{}}'

    with pytest.raises(ValueError, match="base64"):
        await receive_streaming_transfer(
            _body_chunks(body, 3),
            destination,
            expected_size=6,
            expected_sha256="0" * 64,
            options=TransferOptions(expected_sha256="0" * 64),
            context=RequestContext(),
        )

    assert not destination.exists()
    assert list(tmp_path.glob(".*.part")) == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("missing-closing-quote", lambda body: body.replace(b'","metadata"', b',"metadata"', 1)),
        ("missing-closing-brace", lambda body: body[:-1]),
        ("trailing-garbage", lambda body: body + b"unexpected"),
        ("extra-json-field", lambda body: body[:-1] + b',"unexpected":true}'),
    ],
)
async def test_streaming_receiver_rejects_invalid_final_framing_without_commit(tmp_path, case, mutate):
    data = b"content"
    destination = tmp_path / f"{case}.pkl"

    with pytest.raises(ValueError):
        await receive_streaming_transfer(
            _body_chunks(mutate(_streaming_envelope(data)), 3),
            destination,
            expected_size=len(data),
            expected_sha256=hashlib.sha256(data).hexdigest(),
            options=TransferOptions(),
            context=RequestContext(),
        )

    assert not destination.exists()
    assert list(tmp_path.glob(".*.part")) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streaming_receiver_accepts_split_terminator_and_json_trailing_whitespace(tmp_path):
    data = b"content"
    body = _streaming_envelope(data) + b" \t\r\n"
    destination = tmp_path / "received.pkl"

    async def split_terminator():
        yield body[:-5]
        yield body[-5:-4]
        yield body[-4:-2]
        yield body[-2:]

    result = await receive_streaming_transfer(
        split_terminator(),
        destination,
        expected_size=len(data),
        expected_sha256=hashlib.sha256(data).hexdigest(),
        options=TransferOptions(),
        context=RequestContext(),
    )

    assert result.bytes_transferred == len(data)
    assert destination.read_bytes() == data


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streaming_receiver_accepts_uppercase_declared_checksum(tmp_path):
    destination = tmp_path / "received.pkl"
    data = b"content"
    checksum = hashlib.sha256(data).hexdigest()

    result = await receive_streaming_transfer(
        _body_chunks(_streaming_envelope(data), 3),
        destination,
        expected_size=len(data),
        expected_sha256=checksum.upper(),
        options=TransferOptions(expected_sha256=checksum),
        context=RequestContext(),
    )

    assert result.checksum_sha256 == checksum
    assert destination.read_bytes() == data


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streaming_receiver_rejects_mismatched_normalized_checksum(tmp_path):
    data = b"content"
    checksum = hashlib.sha256(data).hexdigest()

    with pytest.raises(ValueError, match="does not match"):
        await receive_streaming_transfer(
            _body_chunks(_streaming_envelope(data)),
            tmp_path / "received.pkl",
            expected_size=len(data),
            expected_sha256=checksum.upper(),
            options=TransferOptions(expected_sha256="0" * 64),
            context=RequestContext(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streaming_receiver_enforces_quota_and_cleans_partial(tmp_path):
    destination = tmp_path / "received.pkl"
    data = b"oversized"

    with pytest.raises(TransferLimitError):
        await receive_streaming_transfer(
            _body_chunks(_streaming_envelope(data), 5),
            destination,
            expected_size=len(data),
            expected_sha256=hashlib.sha256(data).hexdigest(),
            options=TransferOptions(max_bytes=None, quota_bytes=3),
            context=RequestContext(),
        )

    assert not destination.exists()
    assert list(tmp_path.glob(".*.part")) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streaming_receiver_rejects_checksum_and_cleans_partial(tmp_path):
    destination = tmp_path / "received.pkl"
    data = b"content"

    with pytest.raises(TransferChecksumError):
        await receive_streaming_transfer(
            _body_chunks(_streaming_envelope(data), 4),
            destination,
            expected_size=len(data),
            expected_sha256="0" * 64,
            options=TransferOptions(expected_sha256="0" * 64),
            context=RequestContext(),
        )

    assert not destination.exists()
    assert list(tmp_path.glob(".*.part")) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streaming_receiver_cancellation_and_timeout_clean_partials(tmp_path):
    cancellation = asyncio.Event()
    cancellation.set()
    cancelled_destination = tmp_path / "cancelled.pkl"

    with pytest.raises(TransferCancelledError):
        await receive_streaming_transfer(
            _body_chunks(_streaming_envelope(b"content")),
            cancelled_destination,
            expected_size=7,
            expected_sha256=hashlib.sha256(b"content").hexdigest(),
            options=TransferOptions(cancellation_event=cancellation),
            context=RequestContext(),
        )

    async def stalled_body():
        yield b'{"variable_name":"value","data":"'
        await asyncio.Event().wait()

    timeout_destination = tmp_path / "timeout.pkl"
    with pytest.raises(TransferTimeoutError):
        await receive_streaming_transfer(
            stalled_body(),
            timeout_destination,
            expected_size=7,
            expected_sha256=hashlib.sha256(b"content").hexdigest(),
            options=TransferOptions(timeout_seconds=0.05),
            context=RequestContext(),
        )

    assert not cancelled_destination.exists()
    assert not timeout_destination.exists()
    assert list(tmp_path.glob(".*.part")) == []


@pytest.mark.unit
def test_streaming_receive_endpoint_uses_versioned_incremental_path(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from unittest.mock import AsyncMock, MagicMock

    from ..auth import create_noop_auth_config
    from ..code_execution_models import ServerConfig
    from ..server import CodeExecutionServer
    from .. import server as server_module

    server = CodeExecutionServer(
        server_config=ServerConfig(name="test", type="uv", description="Test", dependency_file="# Test"),
        auth_config=create_noop_auth_config(),
        working_dir=tmp_path,
    )
    session = MagicMock(session_id="session-1", user_identity="user@example.com")
    server.session_manager = MagicMock()
    server.session_manager.get_session.return_value = session
    server.session_manager.execute_code_for_session = AsyncMock(return_value=("", "", True, [], []))
    server.activity_publisher = MagicMock()
    monkeypatch.setattr(server_module, "get_current_user_identity", lambda: "user@example.com")

    data = dill.dumps({"value": 1})
    checksum = hashlib.sha256(data).hexdigest()
    info = encode_streaming_transfer_info(
        variable_name="received",
        session_id="session-1",
        metadata={"source_server": "source", "transfer_id": "transfer-1"},
        size_bytes=len(data),
        checksum_sha256=checksum,
    )
    app = server.mcp.http_app(transport="streamable-http")
    server._add_custom_endpoints(app)

    with TestClient(app) as client:
        response = client.post(
            "/object-transfer/receive",
            content=_streaming_envelope(data).replace(b'"value"', b'"received"', 1),
            headers={
                STREAMING_TRANSFER_VERSION_HEADER: STREAMING_TRANSFER_VERSION,
                STREAMING_TRANSFER_INFO_HEADER: info,
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 200
    assert response.json()["size_bytes"] == len(data)
    server.session_manager.execute_code_for_session.assert_awaited_once()
    activity = server.activity_publisher.publish_nowait.call_args.args[0]
    assert activity["source_server"] == "source"
    assert activity["transfer_id"] == "transfer-1"


@pytest.mark.unit
@pytest.mark.parametrize("streaming_transfer", [True, False])
def test_receive_endpoint_rejects_unsafe_correlation_metadata_without_disclosure(
    tmp_path, monkeypatch, caplog, streaming_transfer
):
    from starlette.testclient import TestClient
    from unittest.mock import MagicMock

    from ..auth import create_noop_auth_config
    from ..code_execution_models import ServerConfig
    from ..server import CodeExecutionServer
    from .. import server as server_module

    server = CodeExecutionServer(
        server_config=ServerConfig(name="test", type="uv", description="Test", dependency_file="# Test"),
        auth_config=create_noop_auth_config(),
        working_dir=tmp_path,
    )
    server.session_manager = MagicMock()
    server.activity_publisher = MagicMock()
    monkeypatch.setattr(server_module, "get_current_user_identity", lambda: "user@example.com")

    data = dill.dumps({"value": 1})
    query_secret = "top-" + "secret"
    unsafe_source = f"https://user:password@example.com/source?sig={query_secret}"
    metadata = {"source_server": unsafe_source, "transfer_id": "transfer-1"}
    headers = {"Content-Type": "application/json"}
    if streaming_transfer:
        headers.update(
            {
                STREAMING_TRANSFER_VERSION_HEADER: STREAMING_TRANSFER_VERSION,
                STREAMING_TRANSFER_INFO_HEADER: encode_streaming_transfer_info(
                    variable_name="received",
                    session_id="session-1",
                    metadata=metadata,
                    size_bytes=len(data),
                    checksum_sha256=hashlib.sha256(data).hexdigest(),
                ),
            }
        )
        content = _streaming_envelope(data).replace(b'"value"', b'"received"', 1)
    else:
        content = json.dumps(
            {
                "variable_name": "received",
                "session_id": "session-1",
                "metadata": metadata,
                "data": base64.b64encode(data).decode(),
            }
        ).encode()

    app = server.mcp.http_app(transport="streamable-http")
    server._add_custom_endpoints(app)
    with caplog.at_level("DEBUG"), TestClient(app) as client:
        response = client.post("/object-transfer/receive", content=content, headers=headers)

    assert response.status_code == 400
    assert response.json() == {"success": False, "error": "Invalid object transfer correlation metadata."}
    assert query_secret not in response.text
    assert query_secret not in caplog.text
    server.activity_publisher.publish_nowait.assert_not_called()
    server.session_manager.get_session.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("version", ["3", "future", "2 "])
def test_receive_endpoint_rejects_unknown_explicit_protocol_version_before_legacy_parse(tmp_path, monkeypatch, version):
    from starlette.testclient import TestClient
    from starlette.requests import Request

    from ..auth import create_noop_auth_config
    from ..code_execution_models import ServerConfig
    from ..server import CodeExecutionServer

    json_called = False

    async def fail_if_json_called(_request):
        nonlocal json_called
        json_called = True
        raise AssertionError("unknown versions must not materialize the request body")

    monkeypatch.setattr(Request, "json", fail_if_json_called)
    server = CodeExecutionServer(
        server_config=ServerConfig(name="test", type="uv", description="Test", dependency_file="# Test"),
        auth_config=create_noop_auth_config(),
        working_dir=tmp_path,
    )
    app = server.mcp.http_app(transport="streamable-http")
    server._add_custom_endpoints(app)

    with TestClient(app) as client:
        response = client.post(
            "/object-transfer/receive",
            content=b"not a legacy JSON body",
            headers={STREAMING_TRANSFER_VERSION_HEADER: version},
        )

    assert response.status_code == 400
    assert response.json() == {"success": False, "error": "Unsupported object transfer version."}
    assert not json_called


@pytest.mark.unit
def test_receive_endpoint_without_version_header_streams_legacy_without_request_json(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from starlette.requests import Request
    from unittest.mock import AsyncMock, MagicMock

    from ..auth import create_noop_auth_config
    from ..code_execution_models import ServerConfig
    from ..server import CodeExecutionServer
    from .. import server as server_module

    async def fail_if_json_called(_request):
        raise AssertionError("legacy v1 must not materialize request.json")

    monkeypatch.setattr(Request, "json", fail_if_json_called)
    server = CodeExecutionServer(
        server_config=ServerConfig(name="test", type="uv", description="Test", dependency_file="# Test"),
        auth_config=create_noop_auth_config(),
        working_dir=tmp_path,
    )
    session = MagicMock(session_id="session-1", user_identity="user@example.com")
    server.session_manager = MagicMock()
    server.session_manager.get_session.return_value = session
    server.session_manager.execute_code_for_session = AsyncMock(return_value=("", "", True, [], []))
    server.activity_publisher = MagicMock()
    monkeypatch.setattr(server_module, "get_current_user_identity", lambda: "user@example.com")
    app = server.mcp.http_app(transport="streamable-http")
    server._add_custom_endpoints(app)
    data = dill.dumps({"value": 1})
    body = json.dumps(
        {
            "variable_name": "received",
            "data": base64.b64encode(data).decode(),
            "metadata": {"source_server": "source", "transfer_id": "transfer-1"},
            "session_id": "session-1",
        },
        separators=(",", ":"),
    ).encode()

    with TestClient(app) as client:
        response = client.post("/object-transfer/receive", content=body)

    assert response.status_code == 200
    assert response.json()["size_bytes"] == len(data)
    server.session_manager.execute_code_for_session.assert_awaited_once()


@pytest.mark.unit
def test_legacy_endpoint_rejects_declared_oversized_body_before_streaming(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from starlette.requests import Request

    from ..auth import create_noop_auth_config
    from ..code_execution_models import ServerConfig
    from ..server import CodeExecutionServer

    stream_called = False

    async def fail_if_streamed(_request):
        nonlocal stream_called
        stream_called = True
        raise AssertionError("declared oversized body must not be consumed")
        yield b""

    monkeypatch.setattr(Request, "stream", fail_if_streamed)
    server = CodeExecutionServer(
        server_config=ServerConfig(name="test", type="uv", description="Test", dependency_file="# Test"),
        auth_config=create_noop_auth_config(),
        working_dir=tmp_path,
    )
    app = server.mcp.http_app(transport="streamable-http")
    server._add_custom_endpoints(app)

    with TestClient(app) as client:
        response = client.post(
            "/object-transfer/receive",
            content=b"x",
            headers={"Content-Length": str(MAX_TRANSFER_BODY_BYTES + 1)},
        )

    assert response.status_code == 413
    assert not stream_called


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_receiver_disconnect_cleans_partial(tmp_path):
    from starlette.requests import ClientDisconnect

    destination = tmp_path / "received.pkl"

    async def disconnected_body():
        yield b'{"variable_name":"value","data":"YQ'
        raise ClientDisconnect()

    with pytest.raises(ClientDisconnect):
        await receive_legacy_streaming_transfer(
            disconnected_body(),
            destination,
            options=TransferOptions(),
            context=RequestContext(),
        )

    assert not destination.exists()
    assert list(tmp_path.glob(".*.part")) == []


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
            captured_payload = {}

            async def post(*args, **kwargs):
                body = b"".join([chunk async for chunk in kwargs["content"]])
                captured_payload.update(json.loads(body))
                return mock_response

            mock_client.post = AsyncMock(side_effect=post)
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

            payload = captured_payload
            assert payload["variable_name"] == "target_var"
            assert payload["data"] == expected_b64
            assert payload["metadata"]["source_server"] == "chemistry"
            assert payload["metadata"]["transfer_id"] == "abc123"

            headers = call_args[1]["headers"]
            assert headers[STREAMING_TRANSFER_VERSION_HEADER] == STREAMING_TRANSFER_VERSION
            transfer_info = decode_streaming_transfer_info(headers[STREAMING_TRANSFER_INFO_HEADER])
            assert transfer_info["variable_name"] == "target_var"
            assert transfer_info["size_bytes"] == len(serialized)
            assert transfer_info["checksum_sha256"] == hashlib.sha256(serialized).hexdigest()
            assert headers["Authorization"] == "Bearer test-token"

            assert "Injected 'target_var' into gis kernel" in result

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_publish_preserves_structured_error_response(self, tmp_path):
        """Structured peer errors remain actionable to the send tool."""
        from unittest.mock import AsyncMock, patch

        import httpx

        from ..data_access.publishers import ObjectTransferError, ServerPublisher

        publisher = ServerPublisher(server_name="gis", target_url="http://localhost:8001")
        publisher._user_token = "test-token"
        publisher._source_server = "powergrid"
        publisher._transfer_id = "abc123"

        pkl_file = tmp_path / "data.pkl"
        pkl_file.write_bytes(b"data")

        request = httpx.Request("POST", "http://localhost:8001/object-transfer/receive")
        response = httpx.Response(
            404,
            request=request,
            json={
                "success": False,
                "error": "No active session found to receive the object",
                "hint": "Initialize the destination server, then retry.",
            },
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(ObjectTransferError) as exc_info:
                await publisher.publish(local_path=pkl_file, name="result", session_id="")

        assert exc_info.value.to_payload() == {
            "success": False,
            "error": "Object transfer to 'gis' failed: No active session found to receive the object",
            "hint": "Initialize the destination server, then retry.",
            "status_code": 404,
        }

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
            captured_payload = {}

            async def post(*args, **kwargs):
                body = b"".join([chunk async for chunk in kwargs["content"]])
                captured_payload.update(json.loads(body))
                return mock_response

            mock_client.post = AsyncMock(side_effect=post)
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
            captured_payload = {}

            async def post(*args, **kwargs):
                body = b"".join([chunk async for chunk in kwargs["content"]])
                captured_payload.update(json.loads(body))
                return mock_response

            mock_client.post = AsyncMock(side_effect=post)
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
            captured_payload = {}

            async def post(*args, **kwargs):
                body = b"".join([chunk async for chunk in kwargs["content"]])
                captured_payload.update(json.loads(body))
                return mock_response

            mock_client.post = AsyncMock(side_effect=post)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await publisher.publish(local_path=pkl_file, name="var", session_id="session-123")

            payload = captured_payload
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

    @pytest.mark.unit
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "target_url",
        [
            "https://user:password@allowed.example",
            "https://user%40tenant:password@allowed.example",
            "https://@allowed.example",
        ],
    )
    async def test_publish_rejects_userinfo_before_client_or_token_forwarding(self, tmp_path, monkeypatch, target_url):
        from unittest.mock import patch

        from ..data_access.publishers import ServerPublisher

        monkeypatch.setenv("OBJECT_TRANSFER_ALLOWED_HOSTS", "allowed.example")
        publisher = ServerPublisher(server_name="gis", target_url=target_url)
        publisher._user_token = "must-not-be-forwarded"
        publisher._source_server = "src"
        publisher._transfer_id = "transfer-1"
        pkl_file = tmp_path / "data.pkl"
        pkl_file.write_bytes(b"data")

        with patch("httpx.AsyncClient") as mock_client_cls, pytest.raises(ValueError, match="user information"):
            await publisher.publish(local_path=pkl_file, name="var", session_id="")

        mock_client_cls.assert_not_called()


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
        _validate_target_url("https://example.azure.com:8443/mcp")
        _validate_target_url("https://[2001:db8::1]:8443/mcp")

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
    @pytest.mark.parametrize(
        "url",
        [
            "https://user@allowed.example",
            "https://user:password@allowed.example",
            "https://@allowed.example",
            "https://:password@allowed.example",
            "https://user:@allowed.example",
            "https://user%40tenant@allowed.example",
            "https://user%3Apassword@allowed.example",
            "https://user@[::1]:8000",
        ],
    )
    def test_userinfo_authorities_are_rejected(self, monkeypatch, url):
        from ..object_transfer import _validate_target_url

        monkeypatch.setenv("OBJECT_TRANSFER_ALLOWED_HOSTS", "allowed.example ::1")
        with pytest.raises(ValueError, match="user information"):
            _validate_target_url(url)

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("url", "message"),
        [
            ("https://allowed.example%40evil.example", "percent-encoding"),
            ("https://allowed.example%5cevil.example", "percent-encoding"),
            ("https://allowed.example\\@evil.example", "ambiguous"),
            ("https://allowed.example\\evil/path", "ambiguous"),
            ("https://allowed.example/path\nforged", "ambiguous"),
            ("https://allowed.example/path?sig=secret", "query string or fragment"),
            ("https://allowed.example/path#secret", "query string or fragment"),
            ("https://allowed.example:99999/path", "invalid authority"),
            ("https://allowed.example:not-a-port/path", "invalid authority"),
        ],
    )
    def test_ambiguous_or_credential_bearing_target_components_are_rejected(self, url, message):
        from ..object_transfer import _validate_target_url

        with pytest.raises(ValueError, match=message):
            _validate_target_url(url)

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
