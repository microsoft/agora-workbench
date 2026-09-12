"""End-to-end safety guarantees for local and Azure transfer primitives."""

from __future__ import annotations

import asyncio
import gc
import hashlib
import logging
import os
import tracemalloc
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agora_workbench.data_lake import (
    AzureBlobScope,
    InvalidRequestError,
    PermissionDeniedError,
    RequestContext,
    RESERVED_MANIFEST_PATH,
    RESERVED_OPERATIONS_PREFIX,
    RESERVED_RECEIPTS_PREFIX,
    RESERVED_REVISIONS_PREFIX,
    TransferCancelledError,
    TransferChecksumError,
    TransferLimitError,
    TransferOptions,
    TransferTimeoutError,
    UnsafePathError,
    UnsupportedOperationError,
    canonicalize_azure_uri,
    validate_managed_revision_path,
)
from agora_workbench.data_lake.transfer import await_transfer, safe_artifact_reference, stream_chunks_to_file

from ...data_access.fetchers import AssetFetcher, BlobFetcher, LocalFileFetcher
from ...data_access.manager import DataLakeDataManager
from ...data_access import publishers as publishers_module
from ...data_access.publishers import BlobPublisher, LocalFilePublisher, ServerPublisher, publish_compat


def _part_files(parent: Path) -> list[Path]:
    return list(parent.glob(".*.part"))


@pytest.mark.parametrize("chunk_size", [True, 1.5])
def test_transfer_options_reject_non_integer_chunk_sizes(chunk_size):
    with pytest.raises(ValueError, match="chunk_size"):
        TransferOptions(chunk_size=chunk_size)


@pytest.mark.parametrize("field", ["max_bytes", "quota_bytes"])
@pytest.mark.parametrize("value", [True, 1.5, float("nan")])
def test_transfer_options_reject_non_integer_size_bounds(field, value):
    with pytest.raises(ValueError, match=field):
        TransferOptions(**{field: value})


@pytest.mark.parametrize("timeout", [True, "1", float("nan"), float("inf"), 0, -1])
def test_transfer_options_reject_invalid_timeouts(timeout):
    with pytest.raises(ValueError, match="timeout_seconds"):
        TransferOptions(timeout_seconds=timeout)


def test_safe_artifact_reference_sanitizes_raw_and_tagged_uris():
    raw = "https://user:password@example.com/data?sig=secret#fragment"

    assert safe_artifact_reference(raw) == "https://example.com/data"
    assert safe_artifact_reference(f"<blob>{raw}</blob>") == "<blob>https://example.com/data</blob>"


async def test_local_streaming_peak_memory_is_independent_of_file_size(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"x" * (16 * 1024 * 1024))
    destination = tmp_path / "destination.bin"
    fetcher = LocalFileFetcher(allowed_roots=[str(tmp_path)])

    tracemalloc.start()
    try:
        result = await fetcher.fetch_to_file_result(
            str(source),
            destination,
            options=TransferOptions(chunk_size=64 * 1024, max_bytes=32 * 1024 * 1024),
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.bytes_transferred == source.stat().st_size
    assert destination.stat().st_size == source.stat().st_size
    assert peak < 2 * 1024 * 1024


@pytest.mark.parametrize(
    ("options", "error"),
    [
        (TransferOptions(max_bytes=3), TransferLimitError),
        (TransferOptions(expected_sha256="0" * 64), TransferChecksumError),
    ],
)
async def test_local_download_failure_preserves_destination_and_cleans_partial(tmp_path, options, error):
    source = tmp_path / "source.bin"
    source.write_bytes(b"content")
    destination = tmp_path / "destination.bin"
    destination.write_bytes(b"previous")

    with pytest.raises(error):
        await LocalFileFetcher([str(tmp_path)]).fetch_to_file(
            str(source),
            destination,
            options=options,
        )

    assert destination.read_bytes() == b"previous"
    assert _part_files(tmp_path) == []


async def test_local_download_cancellation_preserves_destination_and_context(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"content")
    destination = tmp_path / "destination.bin"
    destination.write_bytes(b"previous")
    cancellation = asyncio.Event()
    cancellation.set()
    context = RequestContext(request_id="request-1", caller_id="caller-1")
    diagnostics = []

    with pytest.raises(TransferCancelledError):
        await LocalFileFetcher([str(tmp_path)]).fetch_to_file_result(
            str(source),
            destination,
            options=TransferOptions(cancellation_event=cancellation, diagnostic_hook=diagnostics.append),
            context=context,
        )

    assert destination.read_bytes() == b"previous"
    assert _part_files(tmp_path) == []
    assert diagnostics[0].context is context
    assert diagnostics[-1].context is context
    assert diagnostics[-1].state == "failed"


async def test_timeout_cleans_partial_and_preserves_existing_destination(tmp_path):
    destination = tmp_path / "destination.bin"
    destination.write_bytes(b"previous")

    async def slow_chunks():
        yield b"first"
        await asyncio.sleep(0.05)
        yield b"second"

    with pytest.raises(TransferTimeoutError):
        await stream_chunks_to_file(
            slow_chunks(),
            destination,
            options=TransferOptions(timeout_seconds=0.01),
            context=RequestContext(),
        )

    assert destination.read_bytes() == b"previous"
    assert _part_files(tmp_path) == []


async def test_provider_timeout_without_configured_deadline_is_not_masked(tmp_path):
    async def provider_request():
        raise TimeoutError("provider deadline")

    with pytest.raises(TransferTimeoutError, match="Provider transfer timed out"):
        await await_transfer(
            provider_request(),
            TransferOptions(timeout_seconds=None),
            operation="download",
        )

    async def timed_out_chunks():
        raise TimeoutError("provider deadline")
        yield b""  # pragma: no cover

    with pytest.raises(TransferTimeoutError, match="Provider transfer timed out"):
        await stream_chunks_to_file(
            timed_out_chunks(),
            tmp_path / "destination.bin",
            options=TransferOptions(timeout_seconds=None),
            context=RequestContext(),
        )


async def test_await_transfer_cancels_provider_on_timeout_and_external_cancellation():
    async def run_case(*, timeout_seconds, cancel_outer):
        provider_started = asyncio.Event()
        provider_finished = asyncio.Event()

        async def provider():
            provider_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                provider_finished.set()

        operation = asyncio.create_task(
            await_transfer(
                provider(),
                TransferOptions(timeout_seconds=timeout_seconds),
                operation="upload",
            )
        )
        await provider_started.wait()
        if cancel_outer:
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                _ = await operation
        else:
            with pytest.raises(TransferTimeoutError):
                _ = await operation
        assert provider_finished.is_set()

    await run_case(timeout_seconds=0.01, cancel_outer=False)
    await run_case(timeout_seconds=None, cancel_outer=True)


async def test_transfer_cancellation_after_final_chunk_prevents_commit(tmp_path):
    destination = tmp_path / "destination.bin"
    destination.write_bytes(b"previous")
    cancellation = asyncio.Event()

    async def chunks():
        yield b"complete"
        cancellation.set()

    with pytest.raises(TransferCancelledError):
        await stream_chunks_to_file(
            chunks(),
            destination,
            options=TransferOptions(cancellation_event=cancellation),
            context=RequestContext(),
        )

    assert destination.read_bytes() == b"previous"
    assert _part_files(tmp_path) == []


async def test_transfer_rejects_symlinked_destination_parent(tmp_path):
    safe = tmp_path / "safe"
    outside = tmp_path / "outside"
    safe.mkdir()
    outside.mkdir()
    (safe / "link").symlink_to(outside, target_is_directory=True)

    async def chunks():
        yield b"content"

    with pytest.raises(OSError):
        await stream_chunks_to_file(
            chunks(),
            safe / "link" / "destination.bin",
            options=TransferOptions(),
            context=RequestContext(),
        )

    assert list(outside.iterdir()) == []


async def test_local_fetcher_rejects_traversal_and_symlink_escape(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (allowed / "link.txt").symlink_to(outside)
    fetcher = LocalFileFetcher([str(allowed)])

    with pytest.raises(PermissionError, match="outside allowed roots"):
        await fetcher.fetch_to_file(str(allowed / ".." / "outside.txt"), tmp_path / "copy-a")
    with pytest.raises(PermissionError, match="outside allowed roots"):
        await fetcher.fetch_to_file(str(allowed / "link.txt"), tmp_path / "copy-b")

    assert not (tmp_path / "copy-a").exists()
    assert not (tmp_path / "copy-b").exists()


async def test_local_fetcher_rejects_reserved_component_and_allowed_root_directory(tmp_path):
    reserved = tmp_path / ".agora"
    reserved.mkdir()
    (reserved / "manifest.json").write_text("{}")
    fetcher = LocalFileFetcher([str(tmp_path)])

    with pytest.raises(PermissionError, match="reserved"):
        await fetcher.fetch_to_file(str(reserved / "manifest.json"), tmp_path / "copy")
    with pytest.raises(PermissionError, match="regular file"):
        await fetcher.fetch_to_file(str(tmp_path), tmp_path / "directory-copy")

    await fetcher.close()


async def test_local_fetcher_root_swap_after_containment_uses_retained_verified_root(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "data.bin"
    source.write_bytes(b"trusted")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.bin").write_bytes(b"attacker")
    fetcher = LocalFileFetcher([str(allowed)])
    original_check = fetcher._resolve_and_check

    def swap_after_check(qualified_name):
        checked = original_check(qualified_name)
        allowed.rename(tmp_path / "allowed-original")
        allowed.symlink_to(outside, target_is_directory=True)
        return checked

    monkeypatch.setattr(fetcher, "_resolve_and_check", swap_after_check)
    destination = tmp_path / "destination.bin"

    await fetcher.fetch_to_file(str(source), destination)

    assert destination.read_bytes() == b"trusted"
    await fetcher.close()


async def test_local_fetcher_closes_source_when_destination_setup_fails(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    source.write_bytes(b"content")
    safe = tmp_path / "safe"
    outside = tmp_path / "outside"
    safe.mkdir()
    outside.mkdir()
    (safe / "link").symlink_to(outside, target_is_directory=True)
    fetcher = LocalFileFetcher([str(tmp_path)])
    checked_path, descriptor = fetcher._open_checked(str(source))
    monkeypatch.setattr(fetcher, "_open_checked", lambda _qualified_name: (checked_path, descriptor))

    with pytest.raises(OSError):
        await fetcher.fetch_to_file(str(source), safe / "link" / "destination.bin")

    with pytest.raises(OSError):
        os.fstat(descriptor)
    await fetcher.close()


def test_local_fetcher_finalizer_closes_retained_root_descriptors(tmp_path):
    fetcher = LocalFileFetcher([str(tmp_path)])
    descriptor = fetcher._allowed_root_fds[0]

    del fetcher
    gc.collect()

    with pytest.raises(OSError):
        os.fstat(descriptor)


async def test_local_publisher_rejects_symlink_parent_before_writing(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"content")
    output_root = tmp_path / "outputs"
    outside = tmp_path / "outside"
    output_root.mkdir()
    outside.mkdir()
    (output_root / "session").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        await LocalFilePublisher(output_root).publish(source, "result.bin", "session")

    assert list(outside.iterdir()) == []
    assert _part_files(outside) == []


async def test_local_publisher_rejects_root_replaced_by_symlink_after_construction(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"content")
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    publisher = LocalFilePublisher(output_root)
    original_root = tmp_path / "outputs-original"
    output_root.rename(original_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    output_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises((OSError, UnsafePathError)):
        await publisher.publish(source, "result.bin", "session")

    assert list(outside.iterdir()) == []
    await publisher.close()


async def test_local_publisher_rejects_existing_root_replaced_before_first_publish(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"content")
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    publisher = LocalFilePublisher(output_root)
    original_root = tmp_path / "outputs-original"
    output_root.rename(original_root)
    output_root.mkdir()

    with pytest.raises(UnsafePathError, match="replaced"):
        await publisher.publish(source, "result.bin", "session")

    assert list(output_root.iterdir()) == []
    assert list(original_root.iterdir()) == []
    await publisher.close()


@pytest.mark.parametrize("publisher_kind", ["local", "blob"])
async def test_publishers_reject_source_paths_with_symlinked_parent(tmp_path, publisher_kind):
    actual = tmp_path / "actual"
    actual.mkdir()
    source = actual / "source.bin"
    source.write_bytes(b"secret")
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    source_through_link = linked / "source.bin"

    if publisher_kind == "local":
        publisher = LocalFilePublisher(tmp_path / "outputs")
        with pytest.raises(OSError):
            await publisher.publish(source_through_link, "result.bin", "session")
        assert not (tmp_path / "outputs" / "session" / "result.bin").exists()
        await publisher.close()
    else:
        blob_client = MagicMock()
        blob_client.upload_blob = AsyncMock()
        service_client = MagicMock()
        service_client.get_blob_client.return_value = blob_client
        service_client.close = AsyncMock()
        publisher = BlobPublisher(
            "https://account123.blob.core.windows.net",
            "container",
            staging_dir=tmp_path / "staging",
        )
        publisher._client = service_client
        with pytest.raises(OSError):
            await publisher.publish(source_through_link, "result.bin", "session")
        blob_client.upload_blob.assert_not_awaited()


async def test_publishers_reject_provider_reserved_names_before_side_effects(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"content")
    local_root = tmp_path / "local"
    blob = BlobPublisher("https://account123.blob.core.windows.net", "container")
    blob._client = MagicMock(side_effect=AssertionError("network must not be touched"))

    with pytest.raises(ValueError, match="reserved"):
        await LocalFilePublisher(local_root).publish(source, ".agora/manifest.json", "session")
    with pytest.raises(ValueError, match="reserved"):
        await blob.publish(source, ".agora/operations/op-1", "session")

    assert not local_root.exists()


@pytest.mark.parametrize(
    ("options", "error"),
    [
        (TransferOptions(max_bytes=3), TransferLimitError),
        (TransferOptions(expected_sha256="0" * 64), TransferChecksumError),
    ],
)
async def test_local_publisher_failure_has_no_visible_or_partial_output(tmp_path, options, error):
    source = tmp_path / "source.bin"
    source.write_bytes(b"content")
    output_root = tmp_path / "outputs"

    with pytest.raises(error):
        await LocalFilePublisher(output_root).publish(
            source,
            "result.bin",
            "session",
            options=options,
        )

    assert not (output_root / "session" / "result.bin").exists()
    assert _part_files(output_root / "session") == []


def test_blob_scope_validates_account_container_prefix_and_reserved_names():
    scope = AzureBlobScope.from_uri("https://account123.blob.core.windows.net/container/data/")
    assert scope.contains("account123", "container", "data/file.csv")
    assert not scope.contains("account123", "container", "database/file.csv")
    assert not scope.contains("otheraccount", "container", "data/file.csv")

    with pytest.raises(InvalidRequestError, match="account name is malformed"):
        AzureBlobScope.from_uri("az://x/container/data")
    with pytest.raises(InvalidRequestError, match="container name is malformed"):
        AzureBlobScope.from_uri("az://account123/a/data")
    for malformed in ("container/path", "container?query", "container#fragment"):
        with pytest.raises(InvalidRequestError, match="container name is malformed"):
            AzureBlobScope("account123", malformed)
    with pytest.raises(InvalidRequestError, match="account name is malformed"):
        AzureBlobScope("account123/path", "container")
    assert RESERVED_MANIFEST_PATH == ".agora/manifest.json"
    assert RESERVED_OPERATIONS_PREFIX == ".agora/operations/"
    assert RESERVED_REVISIONS_PREFIX == ".agora/revisions/"
    assert RESERVED_RECEIPTS_PREFIX == ".agora/receipts/"
    assert validate_managed_revision_path(".agora/revisions/op-1/data.bin") == (".agora/revisions/op-1/data.bin")
    with pytest.raises(InvalidRequestError, match="revisions prefix"):
        validate_managed_revision_path(".agora/operations/op-1")


def test_transfer_object_metadata_is_copied_immutable_and_rejects_credential_keys():
    metadata = {"agora-operation-id": "operation-1"}
    options = TransferOptions(object_metadata=metadata)
    metadata["agora-operation-id"] = "changed"

    assert options.object_metadata == {"agora-operation-id": "operation-1"}
    with pytest.raises(TypeError):
        options.object_metadata["other"] = "value"  # type: ignore[index]
    with pytest.raises(ValueError, match="credential-bearing"):
        TransferOptions(object_metadata={"authorization-token": "secret"})
    with pytest.raises(ValueError, match="URI values"):
        TransferOptions(
            object_metadata={"source": "https://account123.blob.core.windows.net/container/data?sig=secret"}
        )
    with pytest.raises(ValueError, match="URI values"):
        TransferOptions(object_metadata={"source": "https://user:secret@example.com/data"})
    assert TransferOptions(
        object_metadata={"source": "abfss://container@account123.dfs.core.windows.net/data"}
    ).object_metadata


@pytest.mark.parametrize(
    "uri",
    [
        "az://account123/container/data/file.csv",
        "https://account123.blob.core.windows.net/container/data/file.csv",
        "https://account123.dfs.core.windows.net/container/data/file.csv",
        "abfss://container@account123.dfs.core.windows.net/data/file.csv",
    ],
)
def test_supported_blob_uri_forms_remain_compatible(uri):
    assert canonicalize_azure_uri(uri) == "az://account123/container/data/file.csv"


def test_blob_fetcher_accepts_valid_percent_encoded_object_names():
    fetcher = BlobFetcher(credential=MagicMock())

    assert fetcher._parse_blob_url("az://account123/container/folder/file%20name.csv") == (
        "account123",
        "container",
        "folder/file name.csv",
    )


@pytest.mark.parametrize(
    "uri",
    [
        "az://account123/container/data%2Fescape.csv",
        "az://account123/container/data/%2e%2e/escape.csv",
        "az://account123/container/data/%",
    ],
)
def test_blob_uri_rejects_ambiguous_encoded_paths(uri):
    with pytest.raises(InvalidRequestError):
        canonicalize_azure_uri(uri)


async def test_blob_fetcher_rejects_prefix_and_reserved_paths_before_network(tmp_path):
    fetcher = BlobFetcher(
        credential=MagicMock(),
        allowed_locations=["az://account123/container/data"],
    )
    fetcher._get_client = MagicMock(side_effect=AssertionError("network must not be touched"))

    with pytest.raises(PermissionDeniedError):
        await fetcher.fetch_to_file(
            "az://account123/container/database/file.csv",
            tmp_path / "outside.bin",
        )
    with pytest.raises(InvalidRequestError, match="reserved"):
        await fetcher.fetch_to_file(
            "az://account123/container/.agora/manifest.json",
            tmp_path / "reserved.bin",
            options=TransferOptions(allow_reserved=True),
        )


async def test_blob_fetcher_diagnostics_cover_stream_acquisition_failure(tmp_path):
    diagnostics = []
    blob_client = MagicMock()
    blob_client.download_blob = AsyncMock(side_effect=RuntimeError("authentication failed"))
    service_client = MagicMock()
    service_client.get_blob_client.return_value = blob_client
    fetcher = BlobFetcher(credential=MagicMock())
    fetcher._clients["https://account123.blob.core.windows.net"] = service_client

    with pytest.raises(RuntimeError, match="authentication failed"):
        await fetcher.fetch_to_file(
            "az://account123/container/data.bin",
            tmp_path / "blob.bin",
            options=TransferOptions(diagnostic_hook=diagnostics.append),
        )

    assert [diagnostic.state for diagnostic in diagnostics] == ["started", "failed"]


async def test_blob_streaming_checksum_cleanup_and_credential_safe_diagnostics(tmp_path, caplog):
    class Stream:
        async def chunks(self):
            yield b"abc"
            yield b"def"

    blob_client = MagicMock()
    blob_client.download_blob = AsyncMock(return_value=Stream())
    service_client = MagicMock()
    service_client.get_blob_client.return_value = blob_client
    fetcher = BlobFetcher(credential=MagicMock())
    fetcher._clients["https://account123.blob.core.windows.net"] = service_client
    destination = tmp_path / "blob.bin"

    with caplog.at_level(logging.INFO), pytest.raises(TransferChecksumError):
        await fetcher.fetch_to_file(
            "https://account123.blob.core.windows.net/container/data.bin?sig=DO_NOT_LOG",
            destination,
            options=TransferOptions(expected_sha256="0" * 64),
        )

    assert not destination.exists()
    assert _part_files(tmp_path) == []
    assert "DO_NOT_LOG" not in caplog.text


@pytest.mark.parametrize(
    ("options", "error"),
    [
        (TransferOptions(max_bytes=3), TransferLimitError),
        (TransferOptions(timeout_seconds=0.01), TransferTimeoutError),
    ],
)
async def test_blob_streaming_limit_and_timeout_clean_partial(tmp_path, options, error):
    class Stream:
        async def chunks(self):
            yield b"abcd"
            await asyncio.sleep(0.05)
            yield b"efgh"

    blob_client = MagicMock()
    blob_client.download_blob = AsyncMock(return_value=Stream())
    service_client = MagicMock()
    service_client.get_blob_client.return_value = blob_client
    fetcher = BlobFetcher(credential=MagicMock())
    fetcher._clients["https://account123.blob.core.windows.net"] = service_client
    destination = tmp_path / "blob.bin"
    destination.write_bytes(b"previous")

    with pytest.raises(error):
        await fetcher.fetch_to_file(
            "az://account123/container/data.bin",
            destination,
            options=options,
        )

    assert destination.read_bytes() == b"previous"
    assert _part_files(tmp_path) == []


async def test_blob_streaming_cancellation_cleans_partial(tmp_path):
    cancellation = asyncio.Event()

    class Stream:
        async def chunks(self):
            yield b"abcd"
            cancellation.set()
            yield b"efgh"

    blob_client = MagicMock()
    blob_client.download_blob = AsyncMock(return_value=Stream())
    service_client = MagicMock()
    service_client.get_blob_client.return_value = blob_client
    fetcher = BlobFetcher(credential=MagicMock())
    fetcher._clients["https://account123.blob.core.windows.net"] = service_client
    destination = tmp_path / "blob.bin"

    with pytest.raises(TransferCancelledError):
        await fetcher.fetch_to_file(
            "az://account123/container/data.bin",
            destination,
            options=TransferOptions(cancellation_event=cancellation),
        )

    assert not destination.exists()
    assert _part_files(tmp_path) == []


async def test_blob_publisher_streams_file_and_returns_auditable_result(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    uploaded = bytearray()

    async def upload(stream, **kwargs):
        assert kwargs == {"overwrite": True}
        while chunk := stream.read(2):
            uploaded.extend(chunk)
            await asyncio.sleep(0)

    blob_client = MagicMock()
    blob_client.upload_blob = upload
    service_client = MagicMock()
    service_client.get_blob_client.return_value = blob_client
    publisher = BlobPublisher(
        "https://account123.blob.core.windows.net",
        "container",
        prefix="published",
    )
    publisher._client = service_client
    context = RequestContext(request_id="operation-1", caller_id="caller-1")

    uri, result = await publisher.publish_with_result(
        source,
        "result.bin",
        "session",
        options=TransferOptions(expected_sha256=hashlib.sha256(b"payload").hexdigest(), chunk_size=2),
        context=context,
    )

    assert bytes(uploaded) == b"payload"
    assert uri == "https://account123.blob.core.windows.net/container/published/session/result.bin"
    assert result.context is context
    assert result.resource == "az://account123/container/published/session/result.bin"


async def test_blob_publisher_returns_encoded_https_locator(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    blob_client = MagicMock()
    blob_client.upload_blob = AsyncMock()
    service_client = MagicMock()
    service_client.get_blob_client.return_value = blob_client
    publisher = BlobPublisher("https://account123.blob.core.windows.net", "container")
    publisher._client = service_client

    uri = await publisher.publish(source, "file name?.csv", "session")

    assert uri == "https://account123.blob.core.windows.net/container/session/file%20name%3F.csv"


async def test_blob_publisher_uploads_immutable_snapshot_and_reports_uploaded_checksum(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"original")
    uploaded = bytearray()

    async def upload(stream, **kwargs):
        source.write_bytes(b"changed-and-larger")
        while chunk := stream.read(2):
            uploaded.extend(chunk)

    blob_client = MagicMock()
    blob_client.upload_blob = upload
    service_client = MagicMock()
    service_client.get_blob_client.return_value = blob_client
    publisher = BlobPublisher("https://account123.blob.core.windows.net", "container")
    publisher._client = service_client

    _, result = await publisher.publish_with_result(
        source,
        "result.bin",
        "session",
        options=TransferOptions(max_bytes=len(b"original"), quota_bytes=len(b"original"), chunk_size=2),
    )

    assert bytes(uploaded) == b"original"
    assert result.bytes_transferred == len(uploaded)
    assert result.checksum_sha256 == hashlib.sha256(uploaded).hexdigest()
    assert list(tmp_path.glob(".*.upload")) == []


async def test_blob_publisher_stages_outside_read_only_source_directory(tmp_path):
    source_dir = tmp_path / "read-only-source"
    source_dir.mkdir()
    source = source_dir / "source.bin"
    source.write_bytes(b"payload")
    staging = tmp_path / "publisher-staging"
    uploaded = bytearray()

    async def upload(stream, **kwargs):
        uploaded.extend(stream.read())

    blob_client = MagicMock()
    blob_client.upload_blob = upload
    service_client = MagicMock()
    service_client.get_blob_client.return_value = blob_client
    publisher = BlobPublisher(
        "https://account123.blob.core.windows.net",
        "container",
        staging_dir=staging,
    )
    publisher._client = service_client
    source_dir.chmod(0o555)
    try:
        _, result = await publisher.publish_with_result(source, "result.bin", "session")
    finally:
        source_dir.chmod(0o755)

    assert bytes(uploaded) == b"payload"
    assert result.checksum_sha256 == hashlib.sha256(b"payload").hexdigest()
    assert list(source_dir.glob(".*.upload")) == []
    assert list(staging.glob("*.upload")) == []


async def test_blob_publisher_cancels_upload_before_closing_snapshot(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    upload_started = asyncio.Event()
    upload_cancelled_with_open_stream = asyncio.Event()

    async def upload(stream, **kwargs):
        upload_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert stream.read(1) == b"p"
            upload_cancelled_with_open_stream.set()

    blob_client = MagicMock()
    blob_client.upload_blob = upload
    service_client = MagicMock()
    service_client.get_blob_client.return_value = blob_client
    service_client.close = AsyncMock()
    publisher = BlobPublisher(
        "https://account123.blob.core.windows.net",
        "container",
        staging_dir=tmp_path / "staging",
    )
    publisher._client = service_client

    publish_task = asyncio.create_task(publisher.publish(source, "result.bin", "session"))
    await upload_started.wait()
    publish_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await publish_task

    assert upload_cancelled_with_open_stream.is_set()
    assert list((tmp_path / "staging").glob("*.upload")) == []
    await publisher.close()


async def test_blob_publisher_rejects_replaced_staging_root(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    staging = tmp_path / "staging"
    publisher = BlobPublisher(
        "https://account123.blob.core.windows.net",
        "container",
        staging_dir=staging,
    )
    original_staging = tmp_path / "staging-original"
    staging.rename(original_staging)
    outside = tmp_path / "outside"
    outside.mkdir()
    staging.symlink_to(outside, target_is_directory=True)

    with pytest.raises((OSError, UnsafePathError)):
        await publisher.publish(source, "result.bin", "session")

    assert list(outside.iterdir()) == []
    assert list(original_staging.iterdir()) == []
    await publisher.close()


async def test_blob_conditional_create_seam_uses_create_only_precondition(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    blob_client = MagicMock()
    blob_client.upload_blob = AsyncMock()
    service_client = MagicMock()
    service_client.get_blob_client.return_value = blob_client
    publisher = BlobPublisher("https://account123.blob.core.windows.net", "container")
    publisher._client = service_client
    context = RequestContext(request_id="operation-1", caller_id="caller-1")

    _, result = await publisher.publish_with_result(
        source,
        "result.bin",
        "session",
        options=TransferOptions(
            create_exclusive=True,
            object_metadata={"agora-operation-id": "operation-1"},
        ),
        context=context,
    )

    assert blob_client.upload_blob.await_args.kwargs == {
        "overwrite": False,
        "if_none_match": "*",
        "metadata": {"agora-operation-id": "operation-1"},
    }
    assert result.created is True
    assert result.context is context
    assert result.object_metadata == {"agora-operation-id": "operation-1"}


async def test_reserved_blob_write_requires_explicit_trusted_option(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    blob_client = MagicMock()
    blob_client.upload_blob = AsyncMock()
    service_client = MagicMock()
    service_client.get_blob_client.return_value = blob_client
    publisher = BlobPublisher(
        "https://account123.blob.core.windows.net",
        "container",
        prefix=RESERVED_REVISIONS_PREFIX.rstrip("/"),
    )
    publisher._client = service_client

    with pytest.raises(InvalidRequestError, match="reserved"):
        await publisher.publish_with_result(source, "data.bin", "operation-1")

    uri, result = await publisher.publish_with_result(
        source,
        "data.bin",
        "operation-1",
        options=TransferOptions(
            create_exclusive=True,
            allow_reserved=True,
            object_metadata={"agora-operation-id": "operation-1"},
        ),
    )

    assert uri.endswith("/.agora/revisions/operation-1/data.bin")
    assert result.created is True


@pytest.mark.parametrize(
    "reserved_path",
    [
        RESERVED_MANIFEST_PATH,
        f"{RESERVED_OPERATIONS_PREFIX}operation-1",
        f"{RESERVED_RECEIPTS_PREFIX}operation-1",
    ],
)
async def test_trusted_reserved_write_option_only_allows_revision_paths(tmp_path, reserved_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    blob_client = MagicMock()
    blob_client.upload_blob = AsyncMock()
    service_client = MagicMock()
    service_client.get_blob_client.return_value = blob_client
    service_client.close = AsyncMock()
    blob = BlobPublisher(
        "https://account123.blob.core.windows.net",
        "container",
        staging_dir=tmp_path / "staging",
    )
    blob._client = service_client
    local = LocalFilePublisher(tmp_path / "outputs")
    options = TransferOptions(allow_reserved=True)

    with pytest.raises(InvalidRequestError, match="revisions prefix"):
        await blob.publish(source, reserved_path, "", options=options)
    with pytest.raises(InvalidRequestError, match="revisions prefix"):
        await local.publish(source, reserved_path, "", options=options)

    blob_client.upload_blob.assert_not_awaited()
    assert not (tmp_path / "outputs" / reserved_path).exists()
    await blob.close()
    await local.close()


async def test_local_conditional_create_does_not_replace_existing_object(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"new")
    output = tmp_path / "outputs" / "session" / "result.bin"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"existing")
    publisher = LocalFilePublisher(tmp_path / "outputs")

    with pytest.raises(FileExistsError):
        await publisher.publish_with_result(
            source,
            "result.bin",
            "session",
            options=TransferOptions(create_exclusive=True),
        )

    assert output.read_bytes() == b"existing"
    assert _part_files(output.parent) == []
    await publisher.close()


async def test_local_publisher_cancellation_after_copy_prevents_commit(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    destination = tmp_path / "outputs" / "session" / "result.bin"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"previous")
    cancellation = asyncio.Event()
    original_copy = publishers_module._copy_local_descriptors

    async def copy_then_cancel(*args, **kwargs):
        result = await original_copy(*args, **kwargs)
        cancellation.set()
        return result

    monkeypatch.setattr(publishers_module, "_copy_local_descriptors", copy_then_cancel)
    publisher = LocalFilePublisher(tmp_path / "outputs")

    with pytest.raises(TransferCancelledError):
        await publisher.publish(
            source,
            "result.bin",
            "session",
            options=TransferOptions(cancellation_event=cancellation),
        )

    assert destination.read_bytes() == b"previous"
    assert _part_files(destination.parent) == []
    await publisher.close()


async def test_local_publisher_rejects_unsupported_object_metadata_before_write(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"new")
    publisher = LocalFilePublisher(tmp_path / "outputs")

    with pytest.raises(UnsupportedOperationError, match="object metadata"):
        await publisher.publish_with_result(
            source,
            "result.bin",
            "session",
            options=TransferOptions(object_metadata={"agora-operation-id": "operation-1"}),
        )

    assert not (tmp_path / "outputs").exists()
    await publisher.close()


async def test_blob_publisher_rejects_size_and_checksum_before_remote_side_effect(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    blob_client = MagicMock()
    blob_client.upload_blob = AsyncMock()
    service_client = MagicMock()
    service_client.get_blob_client.return_value = blob_client
    publisher = BlobPublisher("https://account123.blob.core.windows.net", "container")
    publisher._client = service_client

    with pytest.raises(TransferLimitError):
        await publisher.publish(source, "result.bin", "session", options=TransferOptions(max_bytes=3))
    with pytest.raises(TransferChecksumError):
        await publisher.publish(
            source,
            "result.bin",
            "session",
            options=TransferOptions(expected_sha256="0" * 64),
        )

    blob_client.upload_blob.assert_not_awaited()


async def test_unsupported_streaming_capability_is_explicit(tmp_path):
    publisher = ServerPublisher("peer", target_url="https://peer.example")
    with pytest.raises(UnsupportedOperationError, match="bounded file publishing"):
        await publisher.publish_with_result(tmp_path / "unused", "value", "")

    class LegacyFetcher(AssetFetcher):
        async def fetch(self, qualified_name: str):
            return b""

        async def fetch_to_file(self, qualified_name: str, dest_path, **kwargs):
            return 0

        def can_handle(self, qualified_name: str) -> bool:
            return True

    with pytest.raises(UnsupportedOperationError, match="bounded streaming"):
        await LegacyFetcher().fetch_to_file_result("custom://object", tmp_path / "object")


async def test_default_manager_rejects_arbitrary_remote_urls_without_disclosure(tmp_path):
    manager = DataLakeDataManager(credential=None)
    try:
        with pytest.raises(UnsupportedOperationError) as exc:
            await manager._fetch_asset_to_file(
                "https://example.com/file.csv?token=DO_NOT_DISCLOSE",
                tmp_path / "file.csv",
            )
        assert "DO_NOT_DISCLOSE" not in str(exc.value)
        assert exc.value.resource_id == "https://example.com/file.csv"
    finally:
        await manager.aclose()


async def test_manager_uses_detailed_builtin_fetcher_and_enforces_transfer_options(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"content")
    manager = DataLakeDataManager(
        credential=None,
        allowed_local_roots=[str(tmp_path)],
    )
    try:
        with pytest.raises(TransferLimitError):
            await manager._fetch_asset_to_file(
                str(source),
                tmp_path / "destination.bin",
                transfer_options=TransferOptions(max_bytes=1),
            )
    finally:
        await manager.aclose()


async def test_manager_legacy_fetcher_failure_preserves_existing_destination(tmp_path):
    class FailingLegacyFetcher(AssetFetcher):
        async def fetch(self, qualified_name: str):
            return b""

        async def fetch_to_file(self, qualified_name: str, dest_path, **kwargs):
            Path(dest_path).write_bytes(b"partial")
            raise RuntimeError("failed")

        def can_handle(self, qualified_name: str) -> bool:
            return qualified_name.startswith("legacy://")

    manager = DataLakeDataManager(extra_fetchers=[FailingLegacyFetcher()])
    destination = tmp_path / "cached.bin"
    destination.write_bytes(b"committed")
    try:
        with pytest.raises(RuntimeError, match="failed"):
            await manager._fetch_asset_to_file("legacy://object", destination)
        assert destination.read_bytes() == b"committed"
        assert list(tmp_path.glob(".*.legacy-part")) == []
    finally:
        await manager.aclose()


async def test_diagnostic_hook_failures_never_change_transfer_semantics(tmp_path, caplog):
    def failing_hook(diagnostic):
        raise RuntimeError(f"hook failed at {diagnostic.state}")

    source = tmp_path / "source.bin"
    source.write_bytes(b"content")
    destination = tmp_path / "destination.bin"
    options = TransferOptions(diagnostic_hook=failing_hook)

    with caplog.at_level(logging.WARNING):
        result = await LocalFileFetcher([str(tmp_path)]).fetch_to_file_result(
            str(source),
            destination,
            options=options,
        )
    assert result.bytes_transferred == len(b"content")
    assert destination.read_bytes() == b"content"
    assert "Transfer diagnostic hook failed" in caplog.text

    with pytest.raises(TransferLimitError):
        await LocalFileFetcher([str(tmp_path)]).fetch_to_file_result(
            str(source),
            tmp_path / "failed.bin",
            options=TransferOptions(max_bytes=1, diagnostic_hook=failing_hook),
        )

    async def self_cancelling_hook(diagnostic):
        raise asyncio.CancelledError

    cancelled_hook_destination = tmp_path / "cancelled-hook.bin"
    await LocalFileFetcher([str(tmp_path)]).fetch_to_file_result(
        str(source),
        cancelled_hook_destination,
        options=TransferOptions(diagnostic_hook=self_cancelling_hook),
    )
    assert cancelled_hook_destination.read_bytes() == b"content"


async def test_publish_compat_supports_legacy_three_argument_publisher(tmp_path):
    class LegacyPublisher:
        async def publish(self, local_path: Path, name: str, session_id: str) -> str:
            return f"{local_path}:{name}:{session_id}"

    source = tmp_path / "source.bin"
    context = RequestContext(request_id="request-1")

    result = await publish_compat(
        LegacyPublisher(),
        local_path=source,
        name="value",
        session_id="session",
        context=context,
    )

    assert result == f"{source}:value:session"
