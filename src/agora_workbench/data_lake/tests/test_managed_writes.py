"""Managed catalog write, recovery, and concurrency tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agora_workbench.data_lake import (
    RESERVED_MANIFEST_PATH,
    ArtifactMetadata,
    ArtifactNotFoundError,
    ArtifactReference,
    AuthorizedManagedCatalogWriter,
    BlobManagedStorage,
    CatalogManifest,
    CatalogOperation,
    ConflictError,
    DeleteOutcome,
    InvalidRequestError,
    LocalManagedStorage,
    ListRequest,
    ManagedCatalogWriter,
    PermissionDeniedError,
    PreconditionFailedError,
    PromoteOutputRequest,
    RegisterArtifactRequest,
    ReconciliationError,
    RemoveArtifactRequest,
    RequestContext,
    RetryExhaustedError,
    TransferCancelledError,
    TransferOptions,
    TransferTimeoutError,
    UploadArtifactRequest,
    UnsafePathError,
    logical_artifact_id,
)
from agora_workbench.data_lake.catalog import CatalogConfig, DiscoveryMode, ManifestCatalogProvider, SourceConfig


def _writer(root: Path, **kwargs) -> ManagedCatalogWriter:
    return ManagedCatalogWriter(
        "managed",
        LocalManagedStorage(root),
        **kwargs,
    )


async def test_local_upload_commits_immutable_revision_and_is_idempotent(tmp_path):
    source = tmp_path / "source.csv"
    source.write_text("one")
    writer = _writer(tmp_path / "lake")
    request = UploadArtifactRequest(
        "upload-1",
        "reports/current.csv",
        source,
        metadata=ArtifactMetadata(description="A durable report"),
    )

    first = await writer.upload(request, RequestContext(caller_id="alice"))
    repeated = await writer.upload(request, RequestContext(caller_id="alice"))

    assert repeated == first
    assert first.generation == 1
    manifest = await writer.read_manifest(minimum_generation=first.generation)
    artifact = manifest.artifacts[0]
    assert artifact.path == "reports/current.csv"
    assert artifact.storage_path == ".agora/revisions/" + first.artifact_id + "/upload-1.data"
    assert artifact.provenance is not None
    assert artifact.provenance.caller_id == "alice"
    assert artifact.provenance.source_uri is None
    assert artifact.revisions[0].operation_id == "upload-1"
    assert (tmp_path / "lake" / artifact.storage_path).read_text() == "one"

    with pytest.raises(ConflictError):
        await writer.upload(
            UploadArtifactRequest("upload-1", "reports/different.csv", source),
        )

    source.unlink()
    assert await writer.upload(request, RequestContext(caller_id="alice")) == first
    with pytest.raises(ConflictError, match="reused"):
        await writer.upload(
            UploadArtifactRequest(
                "upload-1",
                "reports/current.csv",
                tmp_path / "different-vanished-input.csv",
                metadata=ArtifactMetadata(description="A durable report"),
            )
        )


async def test_managed_manifest_round_trips_through_read_provider(tmp_path):
    source = tmp_path / "source.csv"
    source.write_text("one")
    lake = tmp_path / "lake"
    writer = _writer(lake, revision_retention=0)
    committed = await writer.upload(UploadArtifactRequest("round-trip", "logical/report.csv", source))
    provider = ManifestCatalogProvider(
        CatalogConfig(
            sources=[
                SourceConfig(
                    source_id="managed",
                    path=str(lake),
                    discovery=DiscoveryMode.MANIFEST,
                    manifest=RESERVED_MANIFEST_PATH,
                )
            ]
        )
    )
    try:
        await provider.load()
        artifact = (await provider.list(ListRequest(), RequestContext())).items[0]
        assert artifact.reference.artifact_id == committed.artifact_id
        assert artifact.metadata["logical_path"] == "logical/report.csv"
        assert artifact.locator is not None
        assert artifact.locator.uri.endswith(str(committed.storage_path))

        await writer.remove(
            RemoveArtifactRequest(
                "round-trip-remove",
                ArtifactReference(committed.artifact_id, "managed"),
                expected_revision_id=committed.revision_id,
            )
        )
        await provider.load()
        assert (await provider.list(ListRequest(), RequestContext())).items == ()
    finally:
        await provider.aclose()


@pytest.mark.parametrize("step", ["after_intent", "after_object", "after_manifest", "after_receipt"])
async def test_local_upload_recovers_from_every_interruption_step(tmp_path, step):
    source = tmp_path / f"{step}.csv"
    source.write_text(step)
    raised = False

    def interrupt(current, _operation_id):
        nonlocal raised
        if current == step and not raised:
            raised = True
            raise RuntimeError("simulated process interruption")

    writer = _writer(tmp_path / step, interruption_hook=interrupt)
    request = UploadArtifactRequest(f"op-{step}", "artifact.csv", source)
    with pytest.raises(RuntimeError, match="interruption"):
        await writer.upload(request)

    recovered = await writer.upload(request)
    manifest = await writer.read_manifest(recovered.generation)
    assert len(manifest.artifacts) == 1
    assert len(manifest.artifacts[0].revisions) == 1
    assert manifest.artifacts[0].revision_id == f"op-{step}"


async def test_local_reconciliation_removes_only_owned_uncommitted_object(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"owned")

    def interrupt(step, _operation_id):
        if step == "after_object":
            raise RuntimeError("stop")

    writer = _writer(tmp_path / "lake", interruption_hook=interrupt)
    request = UploadArtifactRequest("orphan-op", "artifact.bin", source)
    with pytest.raises(RuntimeError):
        await writer.upload(request)
    object_path = tmp_path / "lake" / ".agora/revisions" / request.path.replace("/", "_")
    assert not object_path.exists()  # actual path is identity based, not caller controlled

    report = await writer.reconcile(grace_seconds=0)
    assert report.removed_orphans == ("orphan-op",)
    assert not list((tmp_path / "lake" / ".agora/revisions").rglob("*.bin"))


async def test_local_concurrent_writers_merge_without_lost_registration(tmp_path):
    lake = tmp_path / "lake"
    sources = []
    for index in range(8):
        path = tmp_path / f"{index}.txt"
        path.write_text(str(index))
        sources.append(path)

    async def upload(index):
        writer = _writer(lake)
        return await writer.upload(UploadArtifactRequest(f"race-{index}", f"{index}.txt", sources[index]))

    results = await asyncio.gather(*(upload(index) for index in range(len(sources))))
    manifest = await _writer(lake).read_manifest()
    assert manifest.generation == len(sources)
    assert {item.path for item in manifest.artifacts} == {f"{index}.txt" for index in range(len(sources))}
    assert {result.generation for result in results} == set(range(1, len(sources) + 1))


async def test_preconditions_prevent_stale_overwrite(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_text("first")
    second.write_text("second")
    writer = _writer(tmp_path / "lake")
    committed = await writer.upload(UploadArtifactRequest("first-op", "same.txt", first))

    stale_request = UploadArtifactRequest(
        "stale-op",
        "same.txt",
        second,
        expected_generation=committed.generation - 1,
    )
    with pytest.raises(PreconditionFailedError):
        await writer.upload(stale_request)
    with pytest.raises(PreconditionFailedError):
        await writer.upload(stale_request)

    report = await writer.reconcile(grace_seconds=0)

    assert report.removed_orphans == ("stale-op",)
    with pytest.raises(PreconditionFailedError):
        await writer.upload(stale_request)
    manifest = await writer.read_manifest()
    assert manifest.artifacts[0].revision_id == "first-op"


async def test_missing_remove_is_retryable_instead_of_wedged_commit_ready(tmp_path):
    lake = tmp_path / "lake"
    writer = _writer(lake)
    request = RemoveArtifactRequest("missing-remove", ArtifactReference("missing", "managed"))

    with pytest.raises(ArtifactNotFoundError):
        await writer.remove(request)
    with pytest.raises(ArtifactNotFoundError):
        await writer.remove(request)

    intent = json.loads((lake / ".agora/operations/missing-remove.json").read_text())
    assert intent["state"] == "abandoned"


async def test_remove_recovery_survives_later_reupload(tmp_path):
    source = tmp_path / "source"
    replacement = tmp_path / "replacement"
    source.write_text("first")
    replacement.write_text("second")
    lake = tmp_path / "lake"
    base_writer = _writer(lake, revision_retention=0)
    uploaded = await base_writer.upload(UploadArtifactRequest("initial", "artifact.txt", source))
    raised = False

    def interrupt(step, operation_id):
        nonlocal raised
        if step == "after_manifest" and operation_id == "remove-before-reupload" and not raised:
            raised = True
            raise RuntimeError("remove interrupted")

    interrupted_writer = _writer(lake, interruption_hook=interrupt, revision_retention=0)
    remove_request = RemoveArtifactRequest(
        "remove-before-reupload",
        ArtifactReference(uploaded.artifact_id, "managed"),
        garbage_collect=True,
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        await interrupted_writer.remove(remove_request)

    await base_writer.upload(UploadArtifactRequest("replacement", "artifact.txt", replacement))
    report = await base_writer.reconcile(grace_seconds=0)
    recovered = await base_writer.remove(remove_request)

    assert report.recovered == ("remove-before-reupload",)
    assert recovered.deleted
    assert not recovered.cleanup_pending
    assert recovered.generation == uploaded.generation + 1
    current = (await base_writer.read_manifest()).artifacts[0]
    assert current.revision_id == "replacement"
    assert (lake / str(current.storage_path)).read_text() == "second"
    assert not (lake / str(uploaded.storage_path)).exists()


async def test_upload_retry_recovers_its_own_revision_after_later_update(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_text("first")
    second.write_text("second")
    lake = tmp_path / "lake"
    raised = False

    def interrupt(step, operation_id):
        nonlocal raised
        if step == "after_manifest" and operation_id == "first-operation" and not raised:
            raised = True
            raise RuntimeError("receipt interruption")

    interrupted = _writer(lake, interruption_hook=interrupt)
    first_request = UploadArtifactRequest("first-operation", "artifact.txt", first)
    with pytest.raises(RuntimeError, match="receipt interruption"):
        await interrupted.upload(first_request)

    writer = _writer(lake)
    second_result = await writer.upload(UploadArtifactRequest("second-operation", "artifact.txt", second))
    recovered = await writer.upload(first_request)

    assert second_result.generation == 2
    assert recovered.generation == 1
    assert recovered.revision_id == "first-operation"
    assert recovered.storage_path is not None
    assert recovered.storage_path.endswith("/first-operation.data")


async def test_logical_paths_reject_reserved_namespace_before_side_effects(tmp_path):
    source = tmp_path / "source"
    source.write_text("content")
    lake = tmp_path / "lake"
    writer = _writer(lake)

    with pytest.raises(UnsafePathError, match="reserved Agora namespace"):
        await writer.upload(UploadArtifactRequest("reserved", ".agora/revisions/forbidden", source))

    assert not (lake / RESERVED_MANIFEST_PATH).exists()
    assert not (lake / ".agora/operations").exists()

    with pytest.raises(InvalidRequestError, match="reserved for provider metadata"):
        CatalogManifest.from_mapping(
            {
                "version": 1,
                "generation": 1,
                "artifacts": [{"path": ".agora/revisions/forbidden"}],
            }
        )


async def test_create_collision_never_adopts_another_writers_bytes(tmp_path):
    source = tmp_path / "source"
    source.write_text("ours")
    lake = tmp_path / "lake"
    writer = _writer(lake)
    artifact_id = logical_artifact_id("managed", "same.txt")
    revision = lake / ".agora" / "revisions" / artifact_id / "collision.data"
    revision.parent.mkdir(parents=True)
    revision.write_text("theirs")

    with pytest.raises(ConflictError, match="not safely owned"):
        await writer.upload(UploadArtifactRequest("collision", "same.txt", source))
    report = await writer.reconcile(grace_seconds=0)

    assert revision.read_text() == "theirs"
    assert report.deferred == ("collision",)
    assert not (lake / RESERVED_MANIFEST_PATH).exists()


async def test_retry_recovers_object_created_before_ownership_checkpoint(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("owned before checkpoint")
    lake = tmp_path / "lake"
    writer = _writer(lake)
    original_update = writer._update_operation
    crashed = False

    async def crash_ownership_checkpoint(operation_id, lease_id, **updates):
        nonlocal crashed
        if updates.get("ownership_verified") is True and not crashed:
            crashed = True
            raise RuntimeError("ownership checkpoint crash")
        await original_update(operation_id, lease_id, **updates)

    monkeypatch.setattr(writer, "_update_operation", crash_ownership_checkpoint)
    request = UploadArtifactRequest("ownership-gap", "artifact.txt", source)
    with pytest.raises(RuntimeError, match="ownership checkpoint"):
        await writer.upload(request)

    recovered = await _writer(lake).upload(request)

    assert recovered.revision_id == "ownership-gap"
    assert (lake / str(recovered.storage_path)).read_text() == "owned before checkpoint"


async def test_reconciliation_recovers_ownership_created_before_checkpoint(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("reconcile owned object")
    lake = tmp_path / "lake"
    writer = _writer(lake)
    original_update = writer._update_operation
    crashed = False

    async def crash_ownership_checkpoint(operation_id, lease_id, **updates):
        nonlocal crashed
        if updates.get("ownership_verified") is True and not crashed:
            crashed = True
            raise RuntimeError("ownership checkpoint crash")
        await original_update(operation_id, lease_id, **updates)

    monkeypatch.setattr(writer, "_update_operation", crash_ownership_checkpoint)
    with pytest.raises(RuntimeError, match="ownership checkpoint"):
        await writer.upload(UploadArtifactRequest("ownership-reconcile", "artifact.txt", source))

    report = await _writer(lake).reconcile(grace_seconds=0)

    assert report.removed_orphans == ("ownership-reconcile",)
    assert not list((lake / ".agora/revisions").rglob("ownership-reconcile.data"))


async def test_retry_cannot_race_abandoned_orphan_deletion(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("abandoned race")
    lake = tmp_path / "lake"
    crashed = False

    def interrupt(step, operation_id):
        nonlocal crashed
        if step == "after_object" and operation_id == "abandoned-race" and not crashed:
            crashed = True
            raise RuntimeError("after object crash")

    awaitable_writer = _writer(lake, interruption_hook=interrupt)
    request = UploadArtifactRequest("abandoned-race", "artifact.txt", source)
    with pytest.raises(RuntimeError, match="after object"):
        await awaitable_writer.upload(request)

    backend = LocalManagedStorage(lake)
    deleting = asyncio.Event()
    release_delete = asyncio.Event()
    original_delete = backend.delete_owned

    async def paused_delete(*args):
        deleting.set()
        await release_delete.wait()
        return await original_delete(*args)

    monkeypatch.setattr(backend, "delete_owned", paused_delete)
    reconciling = asyncio.create_task(ManagedCatalogWriter("managed", backend).reconcile(grace_seconds=0))
    await deleting.wait()
    with pytest.raises(ConflictError, match="recovered"):
        await _writer(lake).upload(request)

    release_delete.set()
    assert (await reconciling).removed_orphans == ("abandoned-race",)
    assert (await _writer(lake).upload(request)).revision_id == "abandoned-race"


async def test_local_transfer_hashes_the_transferred_snapshot_in_one_pass(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("before")
    lake = tmp_path / "lake"
    backend = LocalManagedStorage(lake)
    original = backend._copy_stage

    def mutate_before_copy(local_path, stage, options):
        local_path.write_text("after")
        return original(local_path, stage, options)

    monkeypatch.setattr(backend, "_copy_stage", mutate_before_copy)
    writer = ManagedCatalogWriter("managed", backend)
    result = await writer.upload(UploadArtifactRequest("changed-source", "artifact.txt", source))
    artifact = (await writer.read_manifest(result.generation)).artifacts[0]
    assert (lake / str(result.storage_path)).read_text() == "after"
    assert artifact.checksum_sha256 == hashlib.sha256(b"after").hexdigest()


async def test_local_transfer_honors_cancellation_before_copy_and_publish(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"")
    lake = tmp_path / "lake"
    backend = LocalManagedStorage(lake)
    writer = ManagedCatalogWriter("managed", backend)
    cancelled = asyncio.Event()
    cancelled.set()

    with pytest.raises(TransferCancelledError):
        await writer.upload(
            UploadArtifactRequest(
                "cancelled-empty",
                "empty.bin",
                source,
                transfer_options=TransferOptions(cancellation_event=cancelled),
            )
        )

    cancelled.clear()
    original = backend._copy_stage

    def cancel_after_copy(local_path, stage, options):
        checksum = original(local_path, stage, options)
        cancelled.set()
        return checksum

    monkeypatch.setattr(backend, "_copy_stage", cancel_after_copy)
    with pytest.raises(TransferCancelledError):
        await writer.upload(
            UploadArtifactRequest(
                "cancelled-before-publish",
                "empty.bin",
                source,
                transfer_options=TransferOptions(cancellation_event=cancelled),
            )
        )

    assert not any((lake / ".agora/revisions").rglob("*.data"))
    assert (await writer.read_manifest()).artifacts == ()


async def test_external_registration_is_read_only_and_remove_never_deletes_bytes(tmp_path):
    lake = tmp_path / "lake"
    lake.mkdir()
    external = lake / "caller-owned.csv"
    external.write_text("keep me")
    writer = _writer(lake, revision_retention=0)
    registered = await writer.register(
        RegisterArtifactRequest(
            "register-1",
            "logical.csv",
            "caller-owned.csv",
            checksum_sha256=hashlib.sha256(b"keep me").hexdigest(),
        ),
    )
    removed = await writer.remove(
        RemoveArtifactRequest(
            "remove-1",
            ArtifactReference(registered.artifact_id, "managed"),
            expected_revision_id=registered.revision_id,
        )
    )

    assert removed.deleted
    assert external.read_text() == "keep me"
    assert not (lake / str(registered.storage_path)).exists()
    assert (await writer.read_manifest()).artifacts[0].deleted_at is not None


async def test_external_registration_resolves_immutable_snapshot(tmp_path):
    lake = tmp_path / "lake"
    lake.mkdir()
    external = lake / "caller-owned.csv"
    external.write_text("version one")
    writer = _writer(lake)
    result = await writer.register(
        RegisterArtifactRequest(
            "external-snapshot",
            "logical.csv",
            "caller-owned.csv",
            checksum_sha256=hashlib.sha256(b"version one").hexdigest(),
        )
    )
    external.write_text("version two")
    artifact = (await writer.read_manifest(result.generation)).artifacts[0]
    assert (lake / str(artifact.storage_path)).read_text() == "version one"
    assert external.read_text() == "version two"


async def test_external_registration_validates_and_records_verified_snapshot_size(tmp_path):
    lake = tmp_path / "lake"
    lake.mkdir()
    external = lake / "caller-owned.csv"
    external.write_bytes(b"ten-bytes!")
    checksum = hashlib.sha256(b"ten-bytes!").hexdigest()
    writer = _writer(lake)

    with pytest.raises(PreconditionFailedError, match="size"):
        await writer.register(
            RegisterArtifactRequest(
                "wrong-size",
                "logical.csv",
                "caller-owned.csv",
                checksum_sha256=checksum,
                size_bytes=1,
            )
        )

    result = await writer.register(
        RegisterArtifactRequest(
            "verified-size",
            "logical.csv",
            "caller-owned.csv",
            checksum_sha256=checksum,
            size_bytes=10,
        )
    )
    artifact = (await writer.read_manifest(result.generation)).artifacts[0]
    assert artifact.size_bytes == 10
    assert artifact.revisions[0].size_bytes == 10
    assert not (lake / ".agora/revisions" / result.artifact_id / "wrong-size.data").exists()


async def test_registration_collision_cannot_claim_identical_foreign_bytes(tmp_path):
    lake = tmp_path / "lake"
    lake.mkdir()
    external = lake / "caller-owned.csv"
    external.write_text("same bytes")
    operation_id = "foreign-register"
    artifact_id = logical_artifact_id("managed", "logical.csv")
    revision = lake / ".agora/revisions" / artifact_id / f"{operation_id}.data"
    revision.parent.mkdir(parents=True)
    revision.write_text("same bytes")
    writer = _writer(lake)

    with pytest.raises(ConflictError, match="not owned"):
        await writer.register(
            RegisterArtifactRequest(
                operation_id,
                "logical.csv",
                "caller-owned.csv",
                checksum_sha256=hashlib.sha256(b"same bytes").hexdigest(),
            )
        )
    report = await writer.reconcile(grace_seconds=0)

    assert revision.read_text() == "same bytes"
    assert report.deferred == (operation_id,)


async def test_tombstone_is_committed_before_owned_gc(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("managed")
    lake = tmp_path / "lake"
    backend = LocalManagedStorage(lake)
    writer = ManagedCatalogWriter("managed", backend, revision_retention=0)
    uploaded = await writer.upload(UploadArtifactRequest("upload", "managed.txt", source))
    original_delete = backend.delete_owned

    async def assert_tombstone(path, operation_id, version_token):
        manifest = await writer.read_manifest()
        assert manifest.artifacts[0].deleted_at is not None
        return await original_delete(path, operation_id, version_token)

    monkeypatch.setattr(backend, "delete_owned", assert_tombstone)
    result = await writer.remove(
        RemoveArtifactRequest(
            "remove",
            ArtifactReference(uploaded.artifact_id, "managed"),
            expected_revision_id=uploaded.revision_id,
        )
    )
    assert not result.cleanup_pending
    assert not (lake / str(uploaded.storage_path)).exists()


@pytest.mark.parametrize("step", ["after_manifest", "after_receipt"])
async def test_interrupted_remove_retry_resumes_requested_cleanup(tmp_path, step):
    source = tmp_path / "source"
    source.write_text("managed")
    lake = tmp_path / "lake"
    raised = False

    def interrupt(current, operation_id):
        nonlocal raised
        if operation_id == "remove" and current == step and not raised:
            raised = True
            raise RuntimeError("remove interruption")

    writer = _writer(lake, revision_retention=0, interruption_hook=interrupt)
    uploaded = await writer.upload(UploadArtifactRequest("upload", "artifact.txt", source))
    request = RemoveArtifactRequest("remove", ArtifactReference(uploaded.artifact_id, "managed"))
    with pytest.raises(RuntimeError, match="interruption"):
        await writer.remove(request)

    recovered = await writer.remove(request)
    assert recovered.deleted and not recovered.cleanup_pending
    assert not (lake / str(uploaded.storage_path)).exists()


async def test_reconciliation_resumes_interrupted_remove_cleanup(tmp_path):
    source = tmp_path / "source"
    source.write_text("managed")
    lake = tmp_path / "lake"

    def interrupt(step, operation_id):
        if operation_id == "remove" and step == "after_manifest":
            raise RuntimeError("remove interruption")

    writer = _writer(lake, revision_retention=0, interruption_hook=interrupt)
    uploaded = await writer.upload(UploadArtifactRequest("upload", "artifact.txt", source))
    with pytest.raises(RuntimeError):
        await writer.remove(RemoveArtifactRequest("remove", ArtifactReference(uploaded.artifact_id, "managed")))

    report = await writer.reconcile(grace_seconds=0)
    assert report.recovered == ("remove",)
    assert not (lake / str(uploaded.storage_path)).exists()
    receipt = json.loads((lake / ".agora/receipts/remove.json").read_text())
    assert receipt["cleanup_pending"] is False


async def test_cleanup_retry_treats_confirmed_absence_as_success(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("managed")
    lake = tmp_path / "lake"
    backend = LocalManagedStorage(lake)
    writer = ManagedCatalogWriter("managed", backend, revision_retention=0)
    uploaded = await writer.upload(UploadArtifactRequest("upload", "artifact.txt", source))
    original_delete = backend.delete_owned
    interrupted = False

    async def delete_then_interrupt(path, operation_id, version_token):
        nonlocal interrupted
        outcome = await original_delete(path, operation_id, version_token)
        if not interrupted:
            interrupted = True
            assert outcome is DeleteOutcome.DELETED
            raise RuntimeError("cleanup response lost")
        return outcome

    monkeypatch.setattr(backend, "delete_owned", delete_then_interrupt)
    request = RemoveArtifactRequest("remove", ArtifactReference(uploaded.artifact_id, "managed"))
    with pytest.raises(RuntimeError, match="response lost"):
        await writer.remove(request)

    recovered = await writer.remove(request)
    assert recovered.deleted and not recovered.cleanup_pending


async def test_promotion_is_explicit_and_preserves_scratch_output(tmp_path):
    output = tmp_path / "scratch" / "result.csv"
    output.parent.mkdir()
    output.write_text("result")
    writer = _writer(tmp_path / "lake")

    assert (await writer.read_manifest()).artifacts == ()
    result = await writer.promote(
        PromoteOutputRequest(
            "promote-1",
            "approved/result.csv",
            output,
            session_id="session-1",
            output_name="result.csv",
        )
    )
    artifact = (await writer.read_manifest(result.generation)).artifacts[0]
    assert output.read_text() == "result"
    assert artifact.provenance is not None
    assert artifact.provenance.kind == CatalogOperation.PROMOTE
    assert artifact.provenance.session_id == "session-1"


class _DenyWrites:
    async def authorize(self, request, context):
        return request.operation not in {
            CatalogOperation.REGISTER,
            CatalogOperation.UPLOAD,
            CatalogOperation.REMOVE,
            CatalogOperation.PROMOTE,
        }


async def test_unauthorized_writer_cannot_mutate_bytes_or_metadata(tmp_path):
    source = tmp_path / "source"
    source.write_text("secret")
    lake = tmp_path / "lake"
    writer = AuthorizedManagedCatalogWriter(_writer(lake), _DenyWrites())

    with pytest.raises(PermissionDeniedError):
        await writer.upload(UploadArtifactRequest("denied", "artifact", source), RequestContext(caller_id="mallory"))
    assert not (lake / RESERVED_MANIFEST_PATH).exists()
    assert not (lake / ".agora/operations").exists()


class _ArtifactDenyPolicy:
    def __init__(self):
        self.requests = []

    async def authorize(self, request, context):
        self.requests.append(request)
        return request.reference is None


@pytest.mark.parametrize("kind", ["register", "upload", "promote"])
async def test_create_mutations_require_effective_artifact_authorization(tmp_path, kind):
    source = tmp_path / "source"
    source.write_text("content")
    lake = tmp_path / "lake"
    if kind == "register":
        lake.mkdir()
        external = lake / "external"
        external.write_text("content")
        request = RegisterArtifactRequest(
            "denied-register",
            "logical.txt",
            "external",
            checksum_sha256=hashlib.sha256(b"content").hexdigest(),
        )
    elif kind == "promote":
        request = PromoteOutputRequest(
            "denied-promote",
            "logical.txt",
            source,
            session_id="session",
            output_name="source",
        )
    else:
        request = UploadArtifactRequest("denied-upload", "logical.txt", source)
    policy = _ArtifactDenyPolicy()
    authorized = AuthorizedManagedCatalogWriter(_writer(lake), policy)

    with pytest.raises(PermissionDeniedError):
        await getattr(authorized, kind)(request)

    assert len(policy.requests) == 2
    assert policy.requests[0].reference is None
    assert policy.requests[1].reference == ArtifactReference(logical_artifact_id("managed", "logical.txt"), "managed")
    assert not (lake / RESERVED_MANIFEST_PATH).exists()
    assert not (lake / ".agora/operations").exists()


async def test_reconciliation_defers_heartbeating_slow_writer(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("slow")
    lake = tmp_path / "lake"
    backend = LocalManagedStorage(lake)
    created = asyncio.Event()
    original = backend.create_from_file

    async def slow_create(*args):
        version = await original(*args)
        created.set()
        await asyncio.sleep(0.15)
        return version

    monkeypatch.setattr(backend, "create_from_file", slow_create)
    writer = ManagedCatalogWriter("managed", backend, operation_lease_seconds=0.03)
    upload = asyncio.create_task(writer.upload(UploadArtifactRequest("slow-op", "artifact.txt", source)))
    await created.wait()
    await asyncio.sleep(0.06)

    report = await writer.reconcile(grace_seconds=0)
    assert report.deferred == ("slow-op",)
    result = await upload
    assert (lake / str(result.storage_path)).read_text() == "slow"


async def test_local_reconciliation_removes_reserved_staging_but_preserves_linked_revision(tmp_path):
    lake = tmp_path / "lake"
    writer = _writer(lake)
    target = lake / ".agora/revisions/artifact/stage-crash.data"
    target.parent.mkdir(parents=True)
    target.write_text("complete")
    stage_dir = lake / ".agora/staging/stage-crash"
    stage_dir.mkdir(parents=True)
    stage = stage_dir / "leftover.stage"
    stage.hardlink_to(target)

    report = await writer.reconcile(grace_seconds=0)

    assert report.removed_staging == ("stage-crash",)
    assert not stage.exists()
    assert target.read_text() == "complete"


async def test_local_publication_durably_creates_all_managed_directories(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("durable directories")
    lake = tmp_path / "lake"
    backend = LocalManagedStorage(lake)
    fsynced: list[Path] = []
    original_fsync = backend._fsync_directory

    def record_fsync(path):
        fsynced.append(path)
        original_fsync(path)

    monkeypatch.setattr(backend, "_fsync_directory", record_fsync)

    await backend.create_from_file(
        ".agora/revisions/nested/artifact/durable-dirs.data",
        source,
        "durable-dirs",
        None,
        TransferOptions(),
    )

    assert lake in fsynced
    assert lake / ".agora" in fsynced
    assert lake / ".agora/staging" in fsynced
    assert lake / ".agora/revisions" in fsynced
    assert lake / ".agora/revisions/nested" in fsynced
    assert lake / ".agora/revisions/nested/artifact" in fsynced


async def test_local_staging_revalidates_writer_created_after_operation_scan(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("concurrent staging")
    lake = tmp_path / "lake"
    backend = LocalManagedStorage(lake)
    writer = ManagedCatalogWriter("managed", backend)
    scan_done = asyncio.Event()
    stage_started = asyncio.Event()
    release_copy = threading.Event()
    loop = asyncio.get_running_loop()
    original_list = backend.list_json
    original_copy = backend._copy_stage

    async def scanned_list(prefix):
        values = await original_list(prefix)
        scan_done.set()
        await stage_started.wait()
        return values

    def blocked_copy(local_path, stage, options):
        loop.call_soon_threadsafe(stage_started.set)
        assert release_copy.wait(timeout=5)
        return original_copy(local_path, stage, options)

    monkeypatch.setattr(backend, "list_json", scanned_list)
    monkeypatch.setattr(backend, "_copy_stage", blocked_copy)
    reconciling = asyncio.create_task(writer.reconcile(grace_seconds=0))
    await scan_done.wait()
    uploading = asyncio.create_task(writer.upload(UploadArtifactRequest("scan-race", "artifact.txt", source)))
    report = await reconciling

    assert report.deferred_staging == ("scan-race",)
    release_copy.set()
    result = await uploading
    assert (lake / str(result.storage_path)).read_text() == "concurrent staging"


class _BlobError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code


class _BlobDownload:
    def __init__(self, value, etag):
        self._value = value
        self.properties = SimpleNamespace(etag=etag)

    async def readall(self):
        return self._value

    async def chunks(self):
        yield self._value


class _Blob:
    def __init__(self, service, name):
        self.service = service
        self.name = name

    async def upload_blob(self, data, *, overwrite, metadata=None, etag=None, match_condition=None):
        del match_condition
        await asyncio.sleep(0)
        if hasattr(data, "read"):
            data = data.read()
        elif hasattr(data, "__aiter__"):
            data = b"".join([chunk async for chunk in data])
        if self.name in self.service.values:
            if not overwrite:
                raise _BlobError(409)
            if etag != self.service.values[self.name]["etag"]:
                raise _BlobError(412)
        elif overwrite and etag is not None:
            raise _BlobError(412)
        if self.name.endswith("manifest.json") and self.service.conflicts:
            self.service.conflicts -= 1
            raise _BlobError(412)
        self.service.counter += 1
        new_etag = f'"etag-{self.service.counter}"'
        self.service.values[self.name] = {
            "data": bytes(data),
            "etag": new_etag,
            "metadata": dict(metadata or {}),
        }
        return {"etag": new_etag}

    async def download_blob(self):
        await asyncio.sleep(0)
        if self.name not in self.service.values:
            raise _BlobError(404)
        item = self.service.values[self.name]
        return _BlobDownload(item["data"], item["etag"])

    async def get_blob_properties(self):
        await asyncio.sleep(0)
        if self.name not in self.service.values:
            raise _BlobError(404)
        item = self.service.values[self.name]
        return SimpleNamespace(etag=item["etag"], size=len(item["data"]), metadata=item["metadata"])

    async def delete_blob(self, *, etag, match_condition):
        del match_condition
        await asyncio.sleep(0)
        if self.name not in self.service.values:
            raise _BlobError(404)
        if self.service.values[self.name]["etag"] != etag:
            raise _BlobError(412)
        del self.service.values[self.name]

    async def set_blob_metadata(self, metadata, *, etag, match_condition):
        del match_condition
        if self.name not in self.service.values:
            raise _BlobError(404)
        if self.service.values[self.name]["etag"] != etag:
            raise _BlobError(412)
        self.service.counter += 1
        self.service.values[self.name]["etag"] = f'"etag-{self.service.counter}"'
        self.service.values[self.name]["metadata"] = dict(metadata)


class _Container:
    def __init__(self, service):
        self.service = service

    def get_blob_client(self, blob):
        return _Blob(self.service, blob)

    async def list_blobs(self, *, name_starts_with):
        for name in sorted(self.service.values):
            if name.startswith(name_starts_with):
                yield SimpleNamespace(name=name)


class _BlobService:
    def __init__(self):
        self.values = {}
        self.counter = 0
        self.conflicts = 0

    def get_blob_client(self, *, container, blob):
        assert container == "container"
        return _Blob(self, blob)

    def get_container_client(self, container):
        assert container == "container"
        return _Container(self)


async def test_blob_writer_retries_etag_conflict_and_commits_generation(tmp_path):
    source = tmp_path / "source"
    source.write_text("blob")
    service = _BlobService()
    service.conflicts = 2
    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    writer = ManagedCatalogWriter("managed", backend, max_conflict_retries=2)

    result = await writer.upload(UploadArtifactRequest("blob-op", "artifact.txt", source))

    assert result.generation == 1
    manifest = json.loads(service.values[f"catalog/{RESERVED_MANIFEST_PATH}"]["data"])
    assert manifest["artifacts"][0]["revision_id"] == "blob-op"
    assert not any(name.startswith("catalog/catalog/") for name in service.values)


async def test_blob_retry_recovers_object_created_before_ownership_checkpoint(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("blob ownership")
    service = _BlobService()
    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    writer = ManagedCatalogWriter("managed", backend)
    original_update = writer._update_operation
    crashed = False

    async def crash_ownership_checkpoint(operation_id, lease_id, **updates):
        nonlocal crashed
        if updates.get("ownership_verified") is True and not crashed:
            crashed = True
            raise RuntimeError("ownership checkpoint crash")
        await original_update(operation_id, lease_id, **updates)

    monkeypatch.setattr(writer, "_update_operation", crash_ownership_checkpoint)
    request = UploadArtifactRequest("blob-ownership-gap", "artifact.txt", source)
    with pytest.raises(RuntimeError, match="ownership checkpoint"):
        await writer.upload(request)

    object_value = next(value for name, value in service.values.items() if name.endswith("/blob-ownership-gap.data"))
    assert object_value["metadata"]["agora_operation_id"] == "blob-ownership-gap"
    recovered = await ManagedCatalogWriter("managed", backend).upload(request)
    assert recovered.revision_id == "blob-ownership-gap"


async def test_blob_checksum_failure_cannot_be_adopted_on_retry(tmp_path):
    source = tmp_path / "source"
    source.write_text("actual")
    service = _BlobService()
    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    writer = ManagedCatalogWriter("managed", backend)
    request = UploadArtifactRequest(
        "bad-checksum",
        "artifact.txt",
        source,
        transfer_options=TransferOptions(expected_sha256=hashlib.sha256(b"other").hexdigest()),
    )

    with pytest.raises(PreconditionFailedError):
        await writer.upload(request)
    with pytest.raises(PreconditionFailedError):
        await writer.upload(request)

    assert not any(name.endswith("/bad-checksum.data") for name in service.values)
    assert not (await writer.read_manifest()).artifacts


async def test_blob_checksum_cleanup_never_deletes_replacement(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("actual")
    service = _BlobService()
    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    original_blob = backend._blob
    replaced = False

    def replacing_blob(path):
        nonlocal replaced
        blob = original_blob(path)
        original_delete = blob.delete_blob

        async def replace_before_delete(*, etag, match_condition):
            nonlocal replaced
            if not replaced:
                replaced = True
                name = blob.name
                service.counter += 1
                service.values[name] = {
                    "data": b"replacement",
                    "etag": f'"etag-{service.counter}"',
                    "metadata": {"agora_operation_id": "other"},
                }
            return await original_delete(etag=etag, match_condition=match_condition)

        blob.delete_blob = replace_before_delete
        return blob

    monkeypatch.setattr(backend, "_blob", replacing_blob)
    request = UploadArtifactRequest(
        "checksum-replacement",
        "artifact.txt",
        source,
        transfer_options=TransferOptions(expected_sha256=hashlib.sha256(b"other").hexdigest()),
    )

    with pytest.raises(_BlobError):
        await ManagedCatalogWriter("managed", backend).upload(request)

    replacement = next(value for name, value in service.values.items() if name.endswith("/checksum-replacement.data"))
    assert replacement["data"] == b"replacement"


async def test_blob_caller_metadata_cannot_override_managed_ownership(tmp_path):
    service = _BlobService()
    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    source = tmp_path / "source"
    source.write_text("metadata")
    writer = ManagedCatalogWriter("managed", backend)
    result = await writer.upload(
        UploadArtifactRequest(
            "metadata-owner",
            "artifact.txt",
            source,
            transfer_options=TransferOptions(object_metadata={"agora_operation_id": "spoofed"}),
        )
    )

    object_value = service.values[f"catalog/{result.storage_path}"]
    assert object_value["metadata"]["agora_operation_id"] == "metadata-owner"


async def test_blob_writer_exposes_retry_exhaustion_without_overwriting_object(tmp_path):
    source = tmp_path / "source"
    source.write_text("blob")
    service = _BlobService()
    service.conflicts = 2
    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    writer = ManagedCatalogWriter("managed", backend, max_conflict_retries=1)

    with pytest.raises(RetryExhaustedError):
        await writer.upload(UploadArtifactRequest("blob-fail", "artifact.txt", source))

    revisions = [name for name in service.values if "/revisions/" in name and not name.endswith(".json")]
    assert len(revisions) == 1
    assert f"catalog/{RESERVED_MANIFEST_PATH}" not in service.values

    service.conflicts = 0
    result = await writer.upload(UploadArtifactRequest("blob-fail", "artifact.txt", source))

    assert result.revision_id == "blob-fail"


@pytest.mark.parametrize("step", ["after_intent", "after_object", "after_manifest", "after_receipt"])
async def test_blob_writer_recovers_from_every_interruption_step(tmp_path, step):
    source = tmp_path / step
    source.write_text(step)
    service = _BlobService()
    raised = False

    def interrupt(current, _operation_id):
        nonlocal raised
        if current == step and not raised:
            raised = True
            raise RuntimeError("blob interruption")

    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    writer = ManagedCatalogWriter("managed", backend, interruption_hook=interrupt)
    request = UploadArtifactRequest(f"blob-{step}", "artifact.txt", source)
    with pytest.raises(RuntimeError, match="interruption"):
        await writer.upload(request)

    result = await writer.upload(request)
    assert result.generation == 1
    manifest = await writer.read_manifest(1)
    assert manifest.artifacts[0].revision_id == f"blob-{step}"


async def test_blob_concurrent_writers_retry_and_merge(tmp_path):
    service = _BlobService()
    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    sources = []
    for index in range(6):
        source = tmp_path / f"blob-{index}"
        source.write_text(str(index))
        sources.append(source)

    async def upload(index):
        writer = ManagedCatalogWriter("managed", backend, max_conflict_retries=10)
        return await writer.upload(UploadArtifactRequest(f"blob-race-{index}", f"{index}.txt", sources[index]))

    results = await asyncio.gather(*(upload(index) for index in range(len(sources))))
    manifest = await ManagedCatalogWriter("managed", backend).read_manifest()
    assert manifest.generation == len(sources)
    assert len(manifest.artifacts) == len(sources)
    assert {result.generation for result in results} == set(range(1, len(sources) + 1))


async def test_blob_cleanup_requires_managed_ownership_and_exact_version(tmp_path):
    source = tmp_path / "source"
    source.write_text("managed")
    service = _BlobService()
    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    writer = ManagedCatalogWriter("managed", backend, revision_retention=0)
    uploaded = await writer.upload(UploadArtifactRequest("blob-upload", "artifact.txt", source))
    object_name = "catalog/" + str(uploaded.storage_path)
    service.values[object_name]["etag"] = '"changed-by-another-writer"'

    removed = await writer.remove(
        RemoveArtifactRequest(
            "blob-remove",
            ArtifactReference(uploaded.artifact_id, "managed"),
            expected_revision_id=uploaded.revision_id,
        )
    )

    assert removed.deleted and removed.cleanup_pending
    assert object_name in service.values


async def test_blob_external_registration_snapshots_source_bytes(tmp_path):
    service = _BlobService()
    service.counter += 1
    service.values["catalog/external.txt"] = {
        "data": b"external one",
        "etag": '"external-1"',
        "metadata": {},
    }
    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    writer = ManagedCatalogWriter("managed", backend)
    result = await writer.register(
        RegisterArtifactRequest(
            "blob-register",
            "logical.txt",
            "external.txt",
            checksum_sha256=hashlib.sha256(b"external one").hexdigest(),
        )
    )
    service.values["catalog/external.txt"]["data"] = b"external two"

    snapshot = service.values["catalog/" + str(result.storage_path)]
    assert snapshot["data"] == b"external one"
    assert service.values["catalog/external.txt"]["data"] == b"external two"


async def test_blob_registration_timeout_covers_source_download_initialization(tmp_path, monkeypatch):
    service = _BlobService()
    service.values["catalog/external.bin"] = {
        "data": b"external",
        "etag": '"source-etag"',
        "metadata": {},
    }
    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    original_download = _Blob.download_blob

    async def delayed_download(self):
        if self.name == "catalog/external.bin":
            await asyncio.sleep(0.1)
        return await original_download(self)

    monkeypatch.setattr(_Blob, "download_blob", delayed_download)
    request = RegisterArtifactRequest(
        "registration-timeout",
        "logical.bin",
        "external.bin",
        checksum_sha256=hashlib.sha256(b"external").hexdigest(),
        transfer_options=TransferOptions(timeout_seconds=0.01),
    )

    with pytest.raises(TransferTimeoutError):
        await ManagedCatalogWriter("managed", backend).register(request)

    assert not any(name.endswith("/registration-timeout.data") for name in service.values)


async def test_blob_registration_timeout_covers_final_properties(tmp_path, monkeypatch):
    service = _BlobService()
    service.values["catalog/external.bin"] = {
        "data": b"external",
        "etag": '"source-etag"',
        "metadata": {},
    }
    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    original_properties = _Blob.get_blob_properties

    async def delayed_properties(self):
        if self.name.endswith("/properties-timeout.data"):
            await asyncio.sleep(0.1)
        return await original_properties(self)

    monkeypatch.setattr(_Blob, "get_blob_properties", delayed_properties)
    request = RegisterArtifactRequest(
        "properties-timeout",
        "logical.bin",
        "external.bin",
        checksum_sha256=hashlib.sha256(b"external").hexdigest(),
        transfer_options=TransferOptions(timeout_seconds=0.01),
    )

    with pytest.raises(TransferTimeoutError):
        await ManagedCatalogWriter("managed", backend).register(request)


async def test_managed_transfer_diagnostics_preserve_request_context(tmp_path):
    source = tmp_path / "source"
    source.write_text("diagnostics")
    events = []
    context = RequestContext(request_id="request", caller_id="caller")
    writer = _writer(tmp_path / "lake")

    await writer.upload(
        UploadArtifactRequest(
            "diagnostics",
            "artifact.txt",
            source,
            transfer_options=TransferOptions(diagnostic_hook=events.append),
        ),
        context,
    )

    assert [event.state for event in events] == ["started", "completed"]
    assert all(event.context is context for event in events)
    assert events[-1].checksum_sha256 == hashlib.sha256(b"diagnostics").hexdigest()


async def test_blob_reconciliation_defers_heartbeating_slow_writer(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("slow blob")
    service = _BlobService()
    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    created = asyncio.Event()
    original = backend.create_from_file

    async def slow_create(*args):
        version = await original(*args)
        created.set()
        await asyncio.sleep(0.15)
        return version

    monkeypatch.setattr(backend, "create_from_file", slow_create)
    writer = ManagedCatalogWriter("managed", backend, operation_lease_seconds=0.03)
    upload = asyncio.create_task(writer.upload(UploadArtifactRequest("slow-blob", "artifact.txt", source)))
    await created.wait()
    await asyncio.sleep(0.06)

    report = await writer.reconcile(grace_seconds=0)
    assert report.deferred == ("slow-blob",)
    result = await upload
    assert "catalog/" + str(result.storage_path) in service.values


async def test_blob_heartbeat_covers_slow_manifest_cas(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("slow manifest")
    service = _BlobService()
    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    manifest_started = asyncio.Event()
    release_manifest = asyncio.Event()
    original_replace = backend.replace_json

    async def slow_manifest(path, value, expected_token):
        if path == RESERVED_MANIFEST_PATH:
            manifest_started.set()
            await release_manifest.wait()
        return await original_replace(path, value, expected_token)

    monkeypatch.setattr(backend, "replace_json", slow_manifest)
    writer = ManagedCatalogWriter("managed", backend, operation_lease_seconds=0.03)
    upload = asyncio.create_task(writer.upload(UploadArtifactRequest("slow-cas", "artifact.txt", source)))
    await manifest_started.wait()
    await asyncio.sleep(0.08)

    report = await writer.reconcile(grace_seconds=0)
    assert report.deferred == ("slow-cas",)
    object_name = next(name for name in service.values if name.endswith("/slow-cas.data"))
    assert object_name in service.values

    release_manifest.set()
    result = await upload
    assert (await writer.read_manifest(result.generation)).artifacts[0].revision_id == "slow-cas"


async def test_blob_remove_heartbeat_prevents_retry_lease_reclamation(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("remove slowly")
    service = _BlobService()
    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    writer = ManagedCatalogWriter("managed", backend, operation_lease_seconds=0.03)
    uploaded = await writer.upload(UploadArtifactRequest("upload-remove", "artifact.txt", source))
    manifest_started = asyncio.Event()
    release_manifest = asyncio.Event()
    original_replace = backend.replace_json

    async def slow_manifest(path, value, expected_token):
        if path == RESERVED_MANIFEST_PATH:
            manifest_started.set()
            await release_manifest.wait()
        return await original_replace(path, value, expected_token)

    monkeypatch.setattr(backend, "replace_json", slow_manifest)
    request = RemoveArtifactRequest("slow-remove", ArtifactReference(uploaded.artifact_id, "managed"))
    removing = asyncio.create_task(writer.remove(request))
    await manifest_started.wait()
    await asyncio.sleep(0.08)

    retry_writer = ManagedCatalogWriter("managed", backend, operation_lease_seconds=0.03)
    with pytest.raises(ConflictError, match="commit_ready"):
        await retry_writer.remove(request)

    release_manifest.set()
    removed = await removing
    assert removed.deleted


async def test_expired_commit_ready_operation_is_fenced_collected_and_retryable(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("commit ready")
    service = _BlobService()
    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    writer = ManagedCatalogWriter("managed", backend, operation_lease_seconds=0.01)

    async def crash_before_manifest(*args, **kwargs):
        raise RuntimeError("hard crash boundary")

    monkeypatch.setattr(writer, "_commit_revision", crash_before_manifest)
    with pytest.raises(RuntimeError, match="hard crash"):
        await writer.upload(UploadArtifactRequest("commit-ready", "artifact.txt", source))
    await asyncio.sleep(0.03)

    report = await writer.reconcile(grace_seconds=0)
    assert report.removed_orphans == ("commit-ready",)
    assert not any(name.endswith("/commit-ready.data") for name in service.values)

    retry = ManagedCatalogWriter("managed", backend)
    result = await retry.upload(UploadArtifactRequest("commit-ready", "artifact.txt", source))
    assert result.revision_id == "commit-ready"


async def test_fence_recovery_preserves_upload_generation_precondition(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_text("first")
    second.write_text("second")
    lake = tmp_path / "lake"
    base = _writer(lake)
    committed = await base.upload(UploadArtifactRequest("first", "first.txt", first))
    crashing = _writer(lake, operation_lease_seconds=0.01)

    async def crash_before_manifest(*args, **kwargs):
        raise RuntimeError("hard crash boundary")

    monkeypatch.setattr(crashing, "_commit_revision", crash_before_manifest)
    request = UploadArtifactRequest(
        "guarded-upload",
        "second.txt",
        second,
        expected_generation=committed.generation,
    )
    with pytest.raises(RuntimeError, match="hard crash"):
        await crashing.upload(request)
    await asyncio.sleep(0.03)

    report = await base.reconcile(grace_seconds=0)
    assert report.removed_orphans == ("guarded-upload",)
    assert (await base.read_manifest()).generation == committed.generation + 2

    result = await base.upload(request)
    assert result.generation == committed.generation + 3


async def test_fence_recovery_does_not_validate_originally_invalid_generation(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("invalid generation")
    lake = tmp_path / "lake"
    crashing = _writer(lake, operation_lease_seconds=0.01)

    async def crash_before_manifest(*args, **kwargs):
        raise RuntimeError("hard crash boundary")

    monkeypatch.setattr(crashing, "_commit_revision", crash_before_manifest)
    request = UploadArtifactRequest(
        "invalid-guard",
        "artifact.txt",
        source,
        expected_generation=999,
    )
    with pytest.raises(RuntimeError, match="hard crash"):
        await crashing.upload(request)
    await asyncio.sleep(0.03)

    writer = _writer(lake)
    report = await writer.reconcile(grace_seconds=0)
    assert report.removed_orphans == ("invalid-guard",)

    with pytest.raises(PreconditionFailedError):
        await writer.upload(request)
    assert not (await writer.read_manifest()).artifacts


async def test_fence_recovery_does_not_mask_unrelated_generation_change(tmp_path, monkeypatch):
    first = tmp_path / "first"
    stalled = tmp_path / "stalled"
    unrelated = tmp_path / "unrelated"
    first.write_text("first")
    stalled.write_text("stalled")
    unrelated.write_text("unrelated")
    lake = tmp_path / "lake"
    base = _writer(lake)
    committed = await base.upload(UploadArtifactRequest("first", "first.txt", first))
    crashing = _writer(lake, operation_lease_seconds=0.01)

    async def crash_before_manifest(*args, **kwargs):
        raise RuntimeError("hard crash boundary")

    monkeypatch.setattr(crashing, "_commit_revision", crash_before_manifest)
    request = UploadArtifactRequest(
        "guarded-stalled",
        "stalled.txt",
        stalled,
        expected_generation=committed.generation,
    )
    with pytest.raises(RuntimeError, match="hard crash"):
        await crashing.upload(request)
    await asyncio.sleep(0.03)

    backend = base._backend
    deleting = asyncio.Event()
    release_delete = asyncio.Event()
    original_delete = backend.delete_owned

    async def paused_delete(*args):
        deleting.set()
        await release_delete.wait()
        return await original_delete(*args)

    monkeypatch.setattr(backend, "delete_owned", paused_delete)
    reconciling = asyncio.create_task(base.reconcile(grace_seconds=0))
    await deleting.wait()
    await _writer(lake).upload(UploadArtifactRequest("unrelated", "unrelated.txt", unrelated))
    release_delete.set()
    await reconciling

    with pytest.raises(PreconditionFailedError):
        await base.upload(request)


async def test_crash_after_fence_clear_preserves_unrelated_generation_detection(tmp_path, monkeypatch):
    first = tmp_path / "first"
    stalled = tmp_path / "stalled"
    unrelated = tmp_path / "unrelated"
    first.write_text("first")
    stalled.write_text("stalled")
    unrelated.write_text("unrelated")
    lake = tmp_path / "lake"
    base = _writer(lake)
    committed = await base.upload(UploadArtifactRequest("first-clear", "first.txt", first))
    crashing = _writer(lake, operation_lease_seconds=0.01)

    async def crash_before_manifest(*args, **kwargs):
        raise RuntimeError("hard crash boundary")

    monkeypatch.setattr(crashing, "_commit_revision", crash_before_manifest)
    request = UploadArtifactRequest(
        "clear-crash",
        "stalled.txt",
        stalled,
        expected_generation=committed.generation,
    )
    with pytest.raises(RuntimeError, match="hard crash"):
        await crashing.upload(request)
    await asyncio.sleep(0.03)

    recovering = _writer(lake)
    original_clear = recovering._clear_commit_fence
    clear_crashed = False

    async def clear_then_crash(*args):
        nonlocal clear_crashed
        result = await original_clear(*args)
        if not clear_crashed:
            clear_crashed = True
            raise RuntimeError("after clear crash")
        return result

    monkeypatch.setattr(recovering, "_clear_commit_fence", clear_then_crash)
    with pytest.raises(ReconciliationError):
        await recovering.reconcile(grace_seconds=0)

    await base.upload(UploadArtifactRequest("after-clear", "unrelated.txt", unrelated))
    await base.reconcile(grace_seconds=0)
    with pytest.raises(PreconditionFailedError):
        await base.upload(request)


async def test_fence_recovery_preserves_remove_generation_precondition(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("remove me")
    lake = tmp_path / "lake"
    base = _writer(lake)
    uploaded = await base.upload(UploadArtifactRequest("upload", "artifact.txt", source))
    crashing = _writer(lake, operation_lease_seconds=0.01)

    async def crash_before_manifest(*args, **kwargs):
        raise RuntimeError("hard crash boundary")

    monkeypatch.setattr(crashing, "_remove_under_lease", crash_before_manifest)
    request = RemoveArtifactRequest(
        "guarded-remove",
        ArtifactReference(uploaded.artifact_id, "managed"),
        expected_generation=uploaded.generation,
    )
    with pytest.raises(RuntimeError, match="hard crash"):
        await crashing.remove(request)
    await asyncio.sleep(0.03)

    report = await base.reconcile(grace_seconds=0)
    assert report.deferred == ("guarded-remove",)
    assert (await base.read_manifest()).generation == uploaded.generation + 2

    result = await base.remove(request)
    assert result.generation == uploaded.generation + 3


async def test_retry_cannot_race_fenced_orphan_deletion(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("delete race")
    service = _BlobService()
    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    writer = ManagedCatalogWriter("managed", backend, operation_lease_seconds=0.01)

    async def crash_before_manifest(*args, **kwargs):
        raise RuntimeError("hard crash boundary")

    monkeypatch.setattr(writer, "_commit_revision", crash_before_manifest)
    request = UploadArtifactRequest("retry-delete-race", "artifact.txt", source)
    with pytest.raises(RuntimeError, match="hard crash"):
        await writer.upload(request)
    await asyncio.sleep(0.03)

    deleting = asyncio.Event()
    release_delete = asyncio.Event()
    original_delete = backend.delete_owned

    async def paused_delete(*args):
        deleting.set()
        await release_delete.wait()
        return await original_delete(*args)

    monkeypatch.setattr(backend, "delete_owned", paused_delete)
    reconciling = asyncio.create_task(
        ManagedCatalogWriter("managed", backend, operation_lease_seconds=0.01).reconcile(grace_seconds=0)
    )
    await deleting.wait()

    with pytest.raises(ConflictError, match="commit_ready"):
        await ManagedCatalogWriter("managed", backend).upload(request)

    release_delete.set()
    report = await reconciling
    assert report.removed_orphans == ("retry-delete-race",)
    result = await ManagedCatalogWriter("managed", backend).upload(request)
    assert result.revision_id == "retry-delete-race"
    assert any(name.endswith("/retry-delete-race.data") for name in service.values)


async def test_expired_active_blob_commit_is_fenced_without_losing_winner(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("racing commit")
    service = _BlobService()
    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    writer = ManagedCatalogWriter("managed", backend, operation_lease_seconds=0.01)
    manifest_started = asyncio.Event()
    release_manifest = asyncio.Event()
    original_replace = backend.replace_json

    @asynccontextmanager
    async def no_heartbeat(*args):
        yield

    async def delay_original_commit(path, value, expected_token):
        if path == RESERVED_MANIFEST_PATH and not value.get("commit_fences") and not manifest_started.is_set():
            manifest_started.set()
            await release_manifest.wait()
        return await original_replace(path, value, expected_token)

    monkeypatch.setattr(writer, "_heartbeat", no_heartbeat)
    monkeypatch.setattr(backend, "replace_json", delay_original_commit)
    uploading = asyncio.create_task(writer.upload(UploadArtifactRequest("fenced-race", "artifact.txt", source)))
    await manifest_started.wait()
    await asyncio.sleep(0.03)

    report = await ManagedCatalogWriter(
        "managed",
        backend,
        operation_lease_seconds=0.01,
    ).reconcile(grace_seconds=0)
    assert report.removed_orphans == ("fenced-race",)

    release_manifest.set()
    with pytest.raises(ConflictError, match="abandoned"):
        await uploading

    retry = ManagedCatalogWriter("managed", backend)
    result = await retry.upload(UploadArtifactRequest("fenced-race", "artifact.txt", source))
    assert result.revision_id == "fenced-race"


async def test_fence_between_writer_assert_and_manifest_load_prevents_late_commit(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("assert load race")
    service = _BlobService()
    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    writer = ManagedCatalogWriter("managed", backend, operation_lease_seconds=0.01)
    writer_checked = asyncio.Event()
    release_writer = asyncio.Event()
    original_assert = writer._assert_active_operation
    checks = 0

    @asynccontextmanager
    async def no_heartbeat(*args):
        yield

    async def pause_after_loop_assert(operation_id, lease_id):
        nonlocal checks
        await original_assert(operation_id, lease_id)
        checks += 1
        if checks == 2:
            writer_checked.set()
            await release_writer.wait()

    monkeypatch.setattr(writer, "_heartbeat", no_heartbeat)
    monkeypatch.setattr(writer, "_assert_active_operation", pause_after_loop_assert)
    uploading = asyncio.create_task(writer.upload(UploadArtifactRequest("assert-load-race", "artifact.txt", source)))
    await writer_checked.wait()
    await asyncio.sleep(0.03)

    report = await ManagedCatalogWriter(
        "managed",
        backend,
        operation_lease_seconds=0.01,
    ).reconcile(grace_seconds=0)
    assert report.removed_orphans == ("assert-load-race",)

    release_writer.set()
    with pytest.raises(ConflictError, match="abandoned"):
        await uploading
    manifest = await ManagedCatalogWriter("managed", backend).read_manifest()
    assert not manifest.artifacts


async def test_ambiguous_blob_manifest_success_preserves_revision_for_reconciliation(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("ambiguous success")
    service = _BlobService()
    backend = BlobManagedStorage(_Container(service), prefix="catalog")
    writer = ManagedCatalogWriter("managed", backend)
    original_replace = backend.replace_json

    async def commit_then_timeout(path, value, expected_token):
        result = await original_replace(path, value, expected_token)
        if path == RESERVED_MANIFEST_PATH:
            raise TimeoutError("response lost after server commit")
        return result

    monkeypatch.setattr(backend, "replace_json", commit_then_timeout)
    with pytest.raises(TimeoutError, match="response lost"):
        await writer.upload(UploadArtifactRequest("ambiguous", "artifact.txt", source))

    intent = service.values["catalog/.agora/operations/ambiguous.json"]
    assert json.loads(intent["data"])["state"] == "commit_ready"
    monkeypatch.setattr(backend, "replace_json", original_replace)
    report = await writer.reconcile(grace_seconds=0)
    assert report.recovered == ("ambiguous",)
    assert any(name.endswith("/ambiguous.data") for name in service.values)
