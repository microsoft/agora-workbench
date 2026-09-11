"""Authoritative local and mocked-Blob manifest catalog tests."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agora_workbench.code_execution.data_access.catalog.indexer import _EnumerationResult
from agora_workbench.data_lake import (
    ArtifactReference,
    ArtifactNotFoundError,
    AuthorizedCatalogProvider,
    BackendUnavailableError,
    CatalogManifest,
    CatalogPolicyMode,
    DevelopmentAllowAllCatalogAuthorizer,
    InvalidRequestError,
    ListRequest,
    MAX_MANIFEST_BYTES,
    PageRequest,
    RequestContext,
    SearchRequest,
)
from agora_workbench.data_lake.catalog import (
    CatalogConfig,
    CatalogDB,
    CatalogIndexer,
    DiscoveryMode,
    SourceConfig,
    convert_catalog_config,
)
from agora_workbench.data_lake.providers import (
    CatalogArtifactResolver,
    ManifestCatalogProvider,
    SQLiteCatalogProvider,
    _cursor_offset,
    _next_cursor,
)


def _manifest(generation: int = 1, *, description: str = "Approved data") -> dict[str, object]:
    return {
        "version": 1,
        "generation": generation,
        "artifacts": [
            {
                "path": "approved/data.csv",
                "artifact_id": "approved-data",
                "description": description,
                "domain": "science",
                "media_type": "text/csv",
                "size_bytes": 4,
                "content_revision": f"content-{generation}",
                "metadata_revision": f"metadata-{generation}",
                "checksum_sha256": "a" * 64,
                "aliases": ["external:approved"],
            }
        ],
    }


def _local_config(root, manifest="manifest.json") -> CatalogConfig:
    return CatalogConfig(
        sources=[
            SourceConfig(
                source_id="approved",
                path=str(root),
                discovery=DiscoveryMode.MANIFEST,
                manifest=manifest,
                files={"not-registered.csv": {"description": "This remains only an override"}},
            )
        ]
    )


async def test_local_manifest_is_authoritative_and_resolves_registered_revision(tmp_path):
    (tmp_path / "approved").mkdir()
    (tmp_path / "approved" / "data.csv").write_text("data")
    (tmp_path / "not-registered.csv").write_text("hidden")
    (tmp_path / "manifest.json").write_text(json.dumps(_manifest()))
    provider = ManifestCatalogProvider(_local_config(tmp_path))
    try:
        assert await provider.load() == 1
        page = await provider.list(ListRequest(), RequestContext())
        assert len(page.items) == 1
        artifact = page.items[0]
        assert artifact.reference == ArtifactReference("approved-data", source_id="approved")
        assert artifact.presentation.description == "Approved data"
        assert artifact.presentation.media_type == "text/csv"
        assert artifact.metadata["domain"] == "science"
        assert artifact.checksum_sha256 == "a" * 64
        assert "not-registered.csv" not in artifact.locator.uri
        resolver = CatalogArtifactResolver(provider, "approved")
        assert await resolver.resolve("external:approved") == str(tmp_path / "approved" / "data.csv")
        authorized = AuthorizedCatalogProvider(
            provider,
            DevelopmentAllowAllCatalogAuthorizer(),
            mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE,
        )
        assert len((await authorized.list(ListRequest(), RequestContext(caller_id="reader"))).items) == 1
        alias_reference = ArtifactReference("external:approved", source_id="approved")
        assert (await authorized.get(alias_reference, RequestContext(caller_id="reader"))).reference == alias_reference
        assert (
            await authorized.resolve(alias_reference, RequestContext(caller_id="reader"))
        ).reference == alias_reference
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "{",
        json.dumps({"version": 99, "generation": 1, "artifacts": []}),
        json.dumps({"version": True, "generation": 1, "artifacts": []}),
        json.dumps({"version": 1.0, "generation": 1, "artifacts": []}),
        json.dumps({"version": 1, "generation": 1}),
        json.dumps({"version": 1, "generation": 1, "artifacts": [{"path": "data.csv", "artifact_id": ""}]}),
        json.dumps({"version": 1, "generation": 1, "artifacts": [{"path": "data.csv", "aliases": [" alias"]}]}),
    ],
)
async def test_invalid_manifest_fails_without_scan_fallback(tmp_path, payload):
    (tmp_path / "unregistered.csv").write_text("must remain absent")
    (tmp_path / "manifest.json").write_text(payload)
    provider = ManifestCatalogProvider(_local_config(tmp_path))
    try:
        with pytest.raises(BackendUnavailableError):
            await provider.load()
        assert not provider.readiness().ready
        with pytest.raises(BackendUnavailableError):
            await provider.list(ListRequest(), RequestContext())
    finally:
        await provider.aclose()


async def test_missing_manifest_fails_explicitly(tmp_path):
    provider = ManifestCatalogProvider(_local_config(tmp_path, "missing.json"))
    try:
        with pytest.raises(BackendUnavailableError, match="no valid generation.*approved") as exc_info:
            await provider.load()
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__context__ is None
        assert str(tmp_path) not in str(exc_info.value)
        assert str(tmp_path) not in (provider.readiness().reason or "")
        assert str(tmp_path) not in (provider.readiness().sources[0].error or "")
    finally:
        await provider.aclose()


async def test_manifest_provider_rejects_scan_cache_without_manifest_generation(tmp_path):
    (tmp_path / "scanned.csv").write_text("scan-only")
    db_path = tmp_path / "catalog.db"
    scan_db = CatalogDB(db_path, vec_dimensions=None)
    scan_db.open()
    try:
        scan_config = CatalogConfig(
            sources=[
                SourceConfig(
                    source_id="approved",
                    path=str(tmp_path),
                    discovery=DiscoveryMode.SCAN,
                )
            ]
        )
        await CatalogIndexer(scan_config, scan_db).index()
        state = scan_db.get_source_refresh_state("approved")
        assert state is not None
        assert state.successful_generation == 1
        assert state.manifest_generation is None
    finally:
        scan_db.close()

    provider = ManifestCatalogProvider(_local_config(tmp_path, "missing.json"), db_path=db_path)
    try:
        with pytest.raises(BackendUnavailableError, match="no valid generation"):
            await provider.load()
        assert not provider.readiness().ready
        with pytest.raises(BackendUnavailableError):
            await provider.list(ListRequest(), RequestContext())
    finally:
        await provider.aclose()


async def test_empty_manifest_is_valid_and_authoritatively_empty(tmp_path):
    (tmp_path / "unregistered.csv").write_text("hidden")
    (tmp_path / "manifest.json").write_text(json.dumps({"version": 1, "generation": 1, "artifacts": []}))
    provider = ManifestCatalogProvider(_local_config(tmp_path))
    try:
        assert await provider.load() == 0
        assert provider.readiness().ready
        assert (await provider.list(ListRequest(), RequestContext())).items == ()
    finally:
        await provider.aclose()


async def test_failed_refresh_preserves_generation_until_stale_bound(tmp_path):
    (tmp_path / "approved").mkdir()
    (tmp_path / "approved" / "data.csv").write_text("data")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest()))
    provider = ManifestCatalogProvider(_local_config(tmp_path), max_stale_seconds=60)
    try:
        await provider.load()
        manifest_path.write_text("{")
        with pytest.raises(BackendUnavailableError):
            await provider.load()
        readiness = provider.readiness()
        assert readiness.ready and readiness.stale
        assert len((await provider.list(ListRequest(), RequestContext())).items) == 1

        provider._source_stale_limits["approved"] = 0
        old = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        provider._db_owned.conn.execute(
            "UPDATE catalog_source_refreshes SET last_success_at=? WHERE source_id='approved'",
            (old,),
        )
        provider._db_owned.conn.commit()
        assert not provider.readiness().ready
        with pytest.raises(BackendUnavailableError):
            await provider.list(ListRequest(), RequestContext())
    finally:
        await provider.aclose()


@pytest.mark.parametrize("failure_stage", ["duplicate", "validation", "write"])
async def test_post_enumeration_failures_expire_by_last_success_age(
    tmp_path,
    monkeypatch,
    failure_stage,
):
    (tmp_path / "approved").mkdir()
    (tmp_path / "approved" / "data.csv").write_text("data")
    (tmp_path / "approved" / "other.csv").write_text("other")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest()))
    provider = ManifestCatalogProvider(_local_config(tmp_path), max_stale_seconds=1)
    try:
        await provider.load()
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        provider._db_owned.conn.execute(
            "UPDATE catalog_source_refreshes SET last_success_at=? WHERE source_id='approved'",
            (old,),
        )
        provider._db_owned.conn.commit()

        if failure_stage == "duplicate":
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "generation": 2,
                        "artifacts": [
                            {"path": "approved/data.csv", "artifact_id": "duplicate"},
                            {"path": "approved/other.csv", "artifact_id": "duplicate"},
                        ],
                    }
                )
            )
        elif failure_stage == "validation":
            monkeypatch.setattr(
                provider._indexer,
                "_compute_rows",
                AsyncMock(side_effect=ValueError("embedding validation failed")),
            )
        else:
            monkeypatch.setattr(
                provider._db_owned,
                "apply_refresh_batch",
                MagicMock(side_effect=sqlite3.OperationalError("write failed")),
            )

        with pytest.raises(BackendUnavailableError):
            await provider.load()
        readiness = provider.readiness()
        assert readiness.stale
        assert not readiness.ready
        with pytest.raises(BackendUnavailableError):
            await provider.list(ListRequest(), RequestContext())
        if failure_stage in {"validation", "write"}:
            assert readiness.sources[0].status == "success"
    finally:
        await provider.aclose()


async def test_post_enumeration_failure_is_readable_within_stale_bound(tmp_path, monkeypatch):
    (tmp_path / "approved").mkdir()
    (tmp_path / "approved" / "data.csv").write_text("data")
    (tmp_path / "manifest.json").write_text(json.dumps(_manifest()))
    provider = ManifestCatalogProvider(_local_config(tmp_path), max_stale_seconds=60)
    try:
        await provider.load()
        monkeypatch.setattr(
            provider._db_owned,
            "apply_refresh_batch",
            MagicMock(side_effect=sqlite3.OperationalError("write failed")),
        )
        with pytest.raises(BackendUnavailableError):
            await provider.load()
        readiness = provider.readiness()
        assert readiness.ready and readiness.stale
        assert readiness.sources[0].status == "success"
        assert len((await provider.list(ListRequest(), RequestContext())).items) == 1
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    ("timestamp_kind", "expected_ready"),
    [
        ("naive-recent", True),
        ("naive-old", False),
        ("aware-offset", True),
        ("future", True),
        ("malformed", False),
    ],
)
async def test_readiness_normalizes_legacy_success_timestamps(
    tmp_path,
    timestamp_kind,
    expected_ready,
):
    (tmp_path / "manifest.json").write_text(json.dumps(_manifest()))
    provider = ManifestCatalogProvider(_local_config(tmp_path), max_stale_seconds=60)
    try:
        await provider.load()
        now = datetime.now(timezone.utc)
        timestamps = {
            "naive-recent": now.replace(tzinfo=None).isoformat(),
            "naive-old": (now - timedelta(days=1)).replace(tzinfo=None).isoformat(),
            "aware-offset": now.astimezone(timezone(timedelta(hours=5))).isoformat(),
            "future": (now + timedelta(days=1)).isoformat(),
            "malformed": "not-a-timestamp/secret",
        }
        provider._db_owned.conn.execute(
            "UPDATE catalog_source_refreshes SET last_success_at=? WHERE source_id='approved'",
            (timestamps[timestamp_kind],),
        )
        provider._db_owned.conn.commit()
        (tmp_path / "manifest.json").write_text("{")
        with pytest.raises(BackendUnavailableError):
            await provider.load()
        readiness = provider.readiness()
        assert readiness.ready is expected_ready
        assert readiness.stale
        assert "not-a-timestamp" not in (readiness.reason or "")
    finally:
        await provider.aclose()


async def test_restart_post_enumeration_write_failure_uses_persisted_last_success_age(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "root"
    root.mkdir()
    (root / "approved").mkdir()
    (root / "approved" / "data.csv").write_text("data")
    (root / "approved" / "other.csv").write_text("other")
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest()))
    db_path = tmp_path / "reader.db"
    provider = ManifestCatalogProvider(_local_config(root), db_path=db_path)
    await provider.load()
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    provider._db_owned.conn.execute(
        "UPDATE catalog_source_refreshes SET last_success_at=? WHERE source_id='approved'",
        (old,),
    )
    provider._db_owned.conn.commit()
    await provider.aclose()

    manifest_path.write_text(json.dumps(_manifest(2)))
    restarted = ManifestCatalogProvider(
        _local_config(root),
        db_path=db_path,
        max_stale_seconds=1,
    )
    try:
        monkeypatch.setattr(
            restarted._db_owned,
            "apply_refresh_batch",
            MagicMock(side_effect=sqlite3.OperationalError("write failed")),
        )
        with pytest.raises(BackendUnavailableError):
            await restarted.load()
        assert restarted.readiness().stale
        assert not restarted.readiness().ready
        assert restarted.readiness().sources[0].status == "success"
    finally:
        await restarted.aclose()


async def test_generation_change_retains_exact_revision_and_restart_rebuilds(tmp_path):
    (tmp_path / "approved").mkdir()
    (tmp_path / "approved" / "data.csv").write_text("data")
    manifest_path = tmp_path / "manifest.json"
    db_path = tmp_path / "reader.db"
    manifest_path.write_text(json.dumps(_manifest()))
    provider = ManifestCatalogProvider(_local_config(tmp_path), db_path=db_path)
    await provider.load()
    current = (await provider.list(ListRequest(), RequestContext())).items[0]
    manifest_path.write_text(json.dumps(_manifest(2, description="Updated")))
    await provider.load()
    updated = await provider.get(current.reference, RequestContext())
    pinned = await provider.get(
        ArtifactReference("approved-data", source_id="approved", revision=1),
        RequestContext(),
    )
    assert updated.presentation.description == "Updated"
    assert updated.revision == 2
    assert pinned.presentation.description == "Approved data"
    assert pinned.revision == 1
    await provider.aclose()

    restarted = ManifestCatalogProvider(_local_config(tmp_path), db_path=db_path)
    try:
        assert not restarted.readiness().ready
        await restarted.load()
        assert restarted.readiness().ready
        assert (await restarted.list(ListRequest(), RequestContext())).items[0].revision == 2
    finally:
        await restarted.aclose()


async def test_manifest_stable_id_moves_and_preserves_pinned_revisions_across_restart(tmp_path):
    old_path = tmp_path / "old.csv"
    new_path = tmp_path / "moved" / "new.csv"
    new_path.parent.mkdir()
    old_path.write_text("old")
    new_path.write_text("new")
    manifest_path = tmp_path / "manifest.json"
    db_path = tmp_path / "reader.db"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "generation": 1,
                "artifacts": [{"path": "old.csv", "artifact_id": "stable-id"}],
            }
        )
    )
    provider = ManifestCatalogProvider(_local_config(tmp_path), db_path=db_path)
    await provider.load()
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "generation": 2,
                "artifacts": [{"path": "moved/new.csv", "artifact_id": "stable-id"}],
            }
        )
    )
    await provider.load()
    current = await provider.get(ArtifactReference("stable-id", source_id="approved"), RequestContext())
    revision_one = await provider.get(
        ArtifactReference("stable-id", source_id="approved", revision=1),
        RequestContext(),
    )
    assert current.locator.uri == str(new_path)
    assert current.revision == 3
    assert revision_one.locator.uri == str(old_path)
    with pytest.raises(ArtifactNotFoundError):
        await provider.get(
            ArtifactReference("stable-id", source_id="approved", revision=2),
            RequestContext(),
        )
    await provider.aclose()

    restarted = ManifestCatalogProvider(_local_config(tmp_path), db_path=db_path)
    try:
        await restarted.load()
        current = await restarted.get(
            ArtifactReference("stable-id", source_id="approved"),
            RequestContext(),
        )
        revision_one = await restarted.get(
            ArtifactReference("stable-id", source_id="approved", revision=1),
            RequestContext(),
        )
        assert current.locator.uri == str(new_path)
        assert current.revision == 3
        assert revision_one.locator.uri == str(old_path)
    finally:
        await restarted.aclose()


async def test_tombstoned_manifest_id_can_be_reregistered_at_new_path(tmp_path):
    (tmp_path / "old.csv").write_text("old")
    (tmp_path / "new.csv").write_text("new")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "generation": 1,
                "artifacts": [{"path": "old.csv", "artifact_id": "stable-id"}],
            }
        )
    )
    provider = ManifestCatalogProvider(_local_config(tmp_path))
    try:
        await provider.load()
        manifest_path.write_text(json.dumps({"version": 1, "generation": 2, "artifacts": []}))
        await provider.load()
        assert (await provider.list(ListRequest(), RequestContext())).items == ()
        manifest_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "generation": 3,
                    "artifacts": [{"path": "new.csv", "artifact_id": "stable-id"}],
                }
            )
        )
        await provider.load()
        current = await provider.get(
            ArtifactReference("stable-id", source_id="approved"),
            RequestContext(),
        )
        revision_one = await provider.get(
            ArtifactReference("stable-id", source_id="approved", revision=1),
            RequestContext(),
        )
        assert current.revision == 3
        assert current.locator.uri == str(tmp_path / "new.csv")
        assert revision_one.locator.uri == str(tmp_path / "old.csv")
    finally:
        await provider.aclose()


async def test_persistent_cache_ignores_historical_removed_sources(tmp_path):
    db_path = tmp_path / "reader.db"
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    for root, source_id in ((first_root, "first"), (second_root, "second")):
        root.mkdir()
        (root / "data.csv").write_text(source_id)
        (root / "manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "generation": 1,
                    "artifacts": [{"path": "data.csv"}],
                }
            )
        )

    first = ManifestCatalogProvider(
        CatalogConfig(
            sources=[
                SourceConfig(
                    source_id="first",
                    path=str(first_root),
                    discovery="manifest",
                    manifest="manifest.json",
                )
            ]
        ),
        db_path=db_path,
    )
    await first.load()
    await first.aclose()

    second = ManifestCatalogProvider(
        CatalogConfig(
            sources=[
                SourceConfig(
                    source_id="second",
                    path=str(second_root),
                    discovery="manifest",
                    manifest="manifest.json",
                )
            ]
        ),
        db_path=db_path,
    )
    try:
        await second.load()
        readiness = second.readiness()
        assert readiness.ready
        assert {state.source_id for state in readiness.sources} == {"second"}
        artifacts = (await second.list(ListRequest(), RequestContext())).items
        assert len(artifacts) == 1
        assert artifacts[0].reference.source_id == "second"
    finally:
        await second.aclose()


@pytest.mark.parametrize(
    "replacement",
    [
        _manifest(2, description="Changed without generation"),
        _manifest(1),
    ],
)
async def test_manifest_generation_etag_rules_preserve_last_valid_state(tmp_path, replacement):
    (tmp_path / "approved").mkdir()
    (tmp_path / "approved" / "data.csv").write_text("data")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(2)))
    provider = ManifestCatalogProvider(_local_config(tmp_path))
    try:
        await provider.load()
        manifest_path.write_text(json.dumps(replacement))
        with pytest.raises(BackendUnavailableError):
            await provider.load()
        artifact = (await provider.list(ListRequest(), RequestContext())).items[0]
        assert artifact.presentation.description == "Approved data"
        state = provider.readiness().sources[0]
        assert state.manifest_generation == 2
        assert state.status == "error"
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "manifest",
    [
        {
            "version": 1,
            "generation": 1,
            "artifacts": [
                {"path": "a.csv", "artifact_id": "duplicate"},
                {"path": "b.csv", "artifact_id": "duplicate"},
            ],
        },
        {
            "version": 1,
            "generation": 1,
            "artifacts": [
                {"path": "a.csv", "aliases": ["external:duplicate"]},
                {"path": "b.csv", "aliases": ["external:duplicate"]},
            ],
        },
        {
            "version": 1,
            "generation": 1,
            "artifacts": [
                {"path": "a.csv", "aliases": ["duplicate", "artifact-id:duplicate"]},
                {"path": "b.csv", "artifact_id": "duplicate"},
            ],
        },
    ],
)
def test_manifest_identity_conflicts_are_rejected_before_indexing(manifest):
    db = CatalogDB(":memory:", vec_dimensions=None)
    db.open()
    try:
        with pytest.raises(Exception, match="unique|multiple artifacts|alias for another"):
            parsed = CatalogManifest.from_mapping(manifest)
            artifacts = [
                {
                    "artifact_id": artifact.artifact_id or f"generated-{index}",
                    "logical_path": artifact.path,
                    "storage_uri": f"/root/{artifact.path}",
                    "aliases": list(artifact.aliases),
                }
                for index, artifact in enumerate(parsed.artifacts)
            ]
            CatalogIndexer(CatalogConfig(), db)._validate_manifest_artifacts("source", artifacts)
    finally:
        db.close()


def test_manifest_parser_is_bounded_strict_json():
    with pytest.raises(ValueError, match="size limit"):
        CatalogIndexer._parse_manifest(b" " * (MAX_MANIFEST_BYTES + 1), "manifest.json")
    alias_bomb = b"a: &a [x, x, x]\nb: [*a, *a, *a]\n"
    with pytest.raises(ValueError, match="Malformed manifest"):
        CatalogIndexer._parse_manifest(alias_bomb, "manifest.json")
    duplicate_keys = b'{"version":1,"version":1,"generation":1,"artifacts":[]}'
    with pytest.raises(ValueError, match="Duplicate JSON object key"):
        CatalogIndexer._parse_manifest(duplicate_keys, "manifest.json")


async def test_local_manifest_rejects_multiple_paths_to_same_locator(tmp_path):
    (tmp_path / "real.csv").write_text("data")
    (tmp_path / "alias.csv").symlink_to(tmp_path / "real.csv")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "generation": 1,
                "artifacts": [{"path": "real.csv"}, {"path": "alias.csv"}],
            }
        )
    )
    provider = ManifestCatalogProvider(_local_config(tmp_path))
    try:
        with pytest.raises(BackendUnavailableError):
            await provider.load()
        assert not provider.readiness().ready
        assert not provider._closed
    finally:
        await provider.aclose()
    await provider.aclose()


async def test_aliases_are_source_scoped_across_manifest_sources(tmp_path):
    sources = []
    for source_id in ("one", "two"):
        root = tmp_path / source_id
        root.mkdir()
        (root / "data.csv").write_text(source_id)
        (root / "manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "generation": 1,
                    "artifacts": [{"path": "data.csv", "aliases": ["external:shared"]}],
                }
            )
        )
        sources.append(
            SourceConfig(
                source_id=source_id,
                path=str(root),
                discovery="manifest",
                manifest="manifest.json",
            )
        )
    provider = ManifestCatalogProvider(CatalogConfig(sources=sources))
    try:
        await provider.load()
        one = await provider.get(
            ArtifactReference("external:shared", source_id="one"),
            RequestContext(),
        )
        two = await provider.get(
            ArtifactReference("external:shared", source_id="two"),
            RequestContext(),
        )
        assert one.locator.uri.endswith("/one/data.csv")
        assert two.locator.uri.endswith("/two/data.csv")
        assert one.reference.artifact_id == two.reference.artifact_id == "external:shared"
        assert one.reference != two.reference
    finally:
        await provider.aclose()


async def test_bad_manifest_source_preserves_itself_without_blocking_healthy_source(tmp_path):
    sources = []
    roots = {}
    for source_id in ("healthy", "bad"):
        root = tmp_path / source_id
        roots[source_id] = root
        root.mkdir()
        (root / "data.csv").write_text(source_id)
        (root / "other.csv").write_text(f"{source_id}-other")
        (root / "manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "generation": 1,
                    "artifacts": [
                        {
                            "path": "data.csv",
                            "description": f"{source_id}-one",
                        }
                    ],
                }
            )
        )
        sources.append(
            SourceConfig(
                source_id=source_id,
                path=str(root),
                discovery="manifest",
                manifest="manifest.json",
            )
        )
    provider = ManifestCatalogProvider(
        CatalogConfig(sources=sources),
        max_stale_seconds=60,
    )
    try:
        await provider.load()
        roots["healthy"].joinpath("manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "generation": 2,
                    "artifacts": [{"path": "data.csv", "description": "healthy-two"}],
                }
            )
        )
        roots["bad"].joinpath("manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "generation": 2,
                    "artifacts": [
                        {"path": "data.csv", "artifact_id": "duplicate"},
                        {"path": "other.csv", "artifact_id": "duplicate"},
                    ],
                }
            )
        )
        with pytest.raises(BackendUnavailableError):
            await provider.load()
        artifacts = {
            artifact.reference.source_id: artifact
            for artifact in (await provider.list(ListRequest(), RequestContext())).items
        }
        assert provider.readiness().ready and provider.readiness().stale
        assert artifacts["healthy"].presentation.description == "healthy-two"
        assert artifacts["bad"].presentation.description == "bad-one"
    finally:
        await provider.aclose()


async def test_duplicate_ids_across_new_manifest_sources_are_isolated(tmp_path):
    sources = []
    for source_id, artifact_id in (
        ("healthy", "healthy-id"),
        ("conflict-one", "shared-id"),
        ("conflict-two", "shared-id"),
    ):
        root = tmp_path / source_id
        root.mkdir()
        (root / "data.csv").write_text(source_id)
        (root / "manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "generation": 1,
                    "artifacts": [{"path": "data.csv", "artifact_id": artifact_id}],
                }
            )
        )
        sources.append(
            SourceConfig(
                source_id=source_id,
                path=str(root),
                discovery="manifest",
                manifest="manifest.json",
            )
        )

    provider = ManifestCatalogProvider(CatalogConfig(sources=sources))
    try:
        with pytest.raises(BackendUnavailableError, match="manifest_invalid") as exc_info:
            await provider.load()

        states = {state.source_id: state for state in provider.readiness().sources}
        assert states["healthy"].status == "success"
        assert states["conflict-one"].error == "manifest_invalid: source refresh failed"
        assert states["conflict-two"].error == "manifest_invalid: source refresh failed"
        assert provider._db_owned.get_artifact("healthy-id", source_id="healthy") is not None
        assert provider._db_owned.get_artifact("shared-id", include_deleted=True) is None
        assert "conflict-one: manifest_invalid" in str(exc_info.value)
        assert "conflict-two: manifest_invalid" in (provider.readiness().reason or "")
    finally:
        await provider.aclose()


async def test_global_canonical_id_alias_conflict_isolated_to_bad_source(tmp_path):
    roots = {source_id: tmp_path / source_id for source_id in ("canonical", "alias")}
    for root in roots.values():
        root.mkdir()
        (root / "data.csv").write_text(root.name)
    roots["canonical"].joinpath("manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "generation": 1,
                "artifacts": [{"path": "data.csv", "artifact_id": "other-id", "description": "one"}],
            }
        )
    )
    roots["alias"].joinpath("manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "generation": 1,
                "artifacts": [{"path": "data.csv", "artifact_id": "alias-id"}],
            }
        )
    )
    provider = ManifestCatalogProvider(
        CatalogConfig(
            sources=[
                SourceConfig(
                    source_id=source_id,
                    path=str(root),
                    discovery="manifest",
                    manifest="manifest.json",
                )
                for source_id, root in roots.items()
            ]
        )
    )
    try:
        await provider.load()
        roots["canonical"].joinpath("manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "generation": 2,
                    "artifacts": [{"path": "data.csv", "artifact_id": "other-id", "description": "two"}],
                }
            )
        )
        roots["alias"].joinpath("manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "generation": 2,
                    "artifacts": [
                        {
                            "path": "data.csv",
                            "artifact_id": "alias-id",
                            "aliases": ["artifact-id:other-id"],
                        }
                    ],
                }
            )
        )
        with pytest.raises(BackendUnavailableError, match="alias: manifest_invalid"):
            await provider.load()

        states = {state.source_id: state for state in provider.readiness().sources}
        assert states["canonical"].successful_generation == 2
        assert states["canonical"].status == "success"
        assert states["alias"].successful_generation == 1
        assert states["alias"].status == "error"
        canonical = await provider.get(
            ArtifactReference("other-id", source_id="canonical"),
            RequestContext(),
        )
        assert canonical.presentation.description == "two"
    finally:
        await provider.aclose()


async def test_retained_path_conflict_isolated_to_bad_source(tmp_path):
    roots = {source_id: tmp_path / source_id for source_id in ("healthy", "bad")}
    for source_id, root in roots.items():
        root.mkdir()
        (root / "old.csv").write_text("old")
        (root / "occupied.csv").write_text("occupied")
        artifacts = [{"path": "old.csv", "artifact_id": f"{source_id}-moving"}]
        if source_id == "bad":
            artifacts.append({"path": "occupied.csv", "artifact_id": "bad-occupant"})
        (root / "manifest.json").write_text(json.dumps({"version": 1, "generation": 1, "artifacts": artifacts}))
    provider = ManifestCatalogProvider(
        CatalogConfig(
            sources=[
                SourceConfig(
                    source_id=source_id,
                    path=str(root),
                    discovery="manifest",
                    manifest="manifest.json",
                )
                for source_id, root in roots.items()
            ]
        ),
        max_stale_seconds=60,
    )
    try:
        await provider.load()
        roots["healthy"].joinpath("manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "generation": 2,
                    "artifacts": [
                        {
                            "path": "old.csv",
                            "artifact_id": "healthy-moving",
                            "description": "healthy-two",
                        }
                    ],
                }
            )
        )
        roots["bad"].joinpath("manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "generation": 2,
                    "artifacts": [{"path": "occupied.csv", "artifact_id": "bad-moving"}],
                }
            )
        )
        with pytest.raises(BackendUnavailableError):
            await provider.load()
        states = {state.source_id: state for state in provider.readiness().sources}
        assert states["healthy"].successful_generation == 2
        assert states["healthy"].status == "success"
        assert states["bad"].successful_generation == 1
        assert states["bad"].status == "error"
        healthy = await provider.get(
            ArtifactReference("healthy-moving", source_id="healthy"),
            RequestContext(),
        )
        bad = await provider.get(
            ArtifactReference("bad-moving", source_id="bad"),
            RequestContext(),
        )
        assert healthy.presentation.description == "healthy-two"
        assert bad.locator.uri == str(roots["bad"] / "old.csv")
    finally:
        await provider.aclose()


async def test_manifest_rejects_new_artifact_id_for_retained_path(tmp_path):
    (tmp_path / "occupied.csv").write_text("occupied")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "generation": 1,
                "artifacts": [{"path": "occupied.csv", "artifact_id": "original-id"}],
            }
        )
    )
    provider = ManifestCatalogProvider(_local_config(tmp_path))
    try:
        await provider.load()
        manifest_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "generation": 2,
                    "artifacts": [{"path": "occupied.csv", "artifact_id": "replacement-id"}],
                }
            )
        )
        with pytest.raises(BackendUnavailableError):
            await provider.load()

        original = await provider.get(
            ArtifactReference("original-id", source_id="approved"),
            RequestContext(),
        )
        assert original.locator.uri == str(tmp_path / "occupied.csv")
        with pytest.raises(ArtifactNotFoundError):
            await provider.get(
                ArtifactReference("replacement-id", source_id="approved"),
                RequestContext(),
            )
    finally:
        await provider.aclose()


class _Download:
    def __init__(self, payload, etag: str | None = '"download-etag"', size: int | None = None):
        self._payload = payload
        if etag is not None:
            self.properties = SimpleNamespace(etag=etag, size=len(payload) if size is None else size)

    async def chunks(self):
        yield self._payload


class _BlobClient:
    def __init__(self, payload, etag: str | None = '"etag-one"'):
        self._payload = payload
        self._etag = etag

    async def download_blob(self, *, offset, length):
        assert offset == 0
        assert length == MAX_MANIFEST_BYTES + 1
        return _Download(self._payload, self._etag)

    async def get_blob_properties(self):
        raise AssertionError("Manifest ETag must come from the download response")


class _BlobService:
    def __init__(self, payload, etag: str | None = '"etag-one"'):
        self._payload = payload
        self._etag = etag

    def get_blob_client(self, *, container, blob):
        assert container == "container"
        assert blob == "prefix/manifest.json"
        return _BlobClient(self._payload, self._etag)


async def test_mocked_blob_manifest_matches_local_registered_metadata(tmp_path):
    (tmp_path / "approved").mkdir()
    (tmp_path / "approved" / "data.csv").write_text("data")
    payload = json.dumps(_manifest()).encode()
    (tmp_path / "manifest.json").write_bytes(payload)
    local = ManifestCatalogProvider(_local_config(tmp_path))
    await local.load()
    local_artifact = (await local.list(ListRequest(), RequestContext())).items[0]

    blob_source = SourceConfig(
        source_id="approved",
        path="az://account123/container/prefix",
        discovery="manifest",
        manifest="manifest.json",
    )
    blob = ManifestCatalogProvider(CatalogConfig(sources=[blob_source]))
    clients = {"https://account123.blob.core.windows.net": _BlobService(payload)}
    try:
        artifacts = await blob._indexer._enumerate_blob_manifest(blob_source, MagicMock(), clients)
        assert blob._indexer._manifest_revisions["approved"] == (1, '"etag-one"')

        async def enumerate_blob_sources(_sources):
            blob._indexer._manifest_revisions["approved"] = (1, '"etag-one"')
            return _EnumerationResult(artifacts, {"approved"})

        blob._indexer._enumerate_blob_sources_concurrent = enumerate_blob_sources
        await blob.load()
        blob_artifact = (await blob.list(ListRequest(), RequestContext())).items[0]
        assert blob_artifact.reference == local_artifact.reference
        assert blob_artifact.presentation == local_artifact.presentation
        assert blob_artifact.checksum_sha256 == local_artifact.checksum_sha256
        assert blob_artifact.content_revision == local_artifact.content_revision
        assert blob_artifact.metadata_revision == local_artifact.metadata_revision
        assert blob_artifact.locator.uri == "az://account123/container/prefix/approved/data.csv"
    finally:
        await local.aclose()
        await blob.aclose()


async def test_blob_manifest_falls_back_to_downloaded_content_digest_without_response_etag():
    payload = json.dumps(_manifest()).encode()
    source = SourceConfig(
        source_id="approved",
        path="az://account123/container/prefix",
        discovery="manifest",
        manifest="manifest.json",
    )
    provider = ManifestCatalogProvider(CatalogConfig(sources=[source]))
    clients = {"https://account123.blob.core.windows.net": _BlobService(payload, etag=None)}
    try:
        await provider._indexer._enumerate_blob_manifest(source, MagicMock(), clients)
        generation, revision = provider._indexer._manifest_revisions["approved"]
        assert generation == 1
        assert revision.startswith("sha256:")
    finally:
        await provider.aclose()


def test_local_manifest_read_is_bounded_when_declared_size_lies():
    payload = b"x" * (MAX_MANIFEST_BYTES + 2)

    class BoundedBytesIO(io.BytesIO):
        def read(self, size=-1):
            assert size == MAX_MANIFEST_BYTES + 1
            return super().read(size)

    class ManifestPath:
        def stat(self):
            return SimpleNamespace(st_size=1)

        def open(self, mode):
            assert mode == "rb"
            return BoundedBytesIO(payload)

    with pytest.raises(ValueError, match="size limit"):
        CatalogIndexer._read_local_manifest(cast(Path, ManifestPath()))


async def test_blob_manifest_read_is_bounded_when_declared_size_lies():
    download = _Download(b"x" * (MAX_MANIFEST_BYTES + 2), size=1)
    with pytest.raises(ValueError, match="size limit"):
        await CatalogIndexer._read_blob_manifest(download)


async def test_blob_credential_failure_records_error_and_never_scans():
    source = SourceConfig(
        source_id="approved",
        path="az://account123/container/prefix",
        discovery="manifest",
        manifest="manifest.json",
    )
    provider = ManifestCatalogProvider(CatalogConfig(sources=[source]))
    provider._indexer._enumerate_blob_sources_concurrent = AsyncMock(
        return_value=_EnumerationResult([], set(), {"approved": "ClientAuthenticationError: denied"})
    )
    try:
        with pytest.raises(BackendUnavailableError):
            await provider.load()
        state = provider.readiness().sources[0]
        assert state.status == "error"
        assert state.error is not None
        assert "ClientAuthenticationError" in state.error
    finally:
        await provider.aclose()


async def test_blob_credential_failure_runs_real_manifest_path_without_listing(monkeypatch):
    from azure.identity import aio as identity_aio
    from azure.storage.blob import aio as blob_aio

    class Credential:
        async def close(self):
            return None

    class FailingBlob:
        async def download_blob(self, *, offset, length):
            del offset, length
            raise PermissionError("credential denied?sig=do-not-expose")

    class Service:
        def __init__(self, service_url, credential):
            assert service_url == "https://account123.blob.core.windows.net"
            assert isinstance(credential, Credential)

        def get_blob_client(self, *, container, blob):
            assert container == "container"
            assert blob == "prefix/manifest.json"
            return FailingBlob()

        def get_container_client(self, container):
            raise AssertionError("Manifest mode must not list the container")

        async def close(self):
            return None

    monkeypatch.setattr(identity_aio, "DefaultAzureCredential", Credential)
    monkeypatch.setattr(blob_aio, "BlobServiceClient", Service)
    source = SourceConfig(
        source_id="approved",
        path="az://account123/container/prefix",
        discovery="manifest",
        manifest="manifest.json",
    )
    provider = ManifestCatalogProvider(CatalogConfig(sources=[source]))
    with pytest.raises(BackendUnavailableError):
        await provider.load()
    state = provider.readiness().sources[0]
    assert state.status == "error"
    assert state.error == "credential_or_access_failure: source refresh failed"
    assert "do-not-expose" not in (provider.readiness().reason or "")
    assert not provider._closed
    await provider.aclose()


async def test_blob_setup_failure_preserves_healthy_local_manifest(tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    (local_root / "data.csv").write_text("local")
    (local_root / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "generation": 1,
                "artifacts": [{"path": "data.csv", "artifact_id": "local-data"}],
            }
        )
    )
    provider = ManifestCatalogProvider(
        CatalogConfig(
            sources=[
                SourceConfig(
                    source_id="local",
                    path=str(local_root),
                    discovery="manifest",
                    manifest="manifest.json",
                ),
                SourceConfig(
                    source_id="blob",
                    path="az://account123/container/prefix",
                    discovery="manifest",
                    manifest="manifest.json",
                ),
            ]
        )
    )
    dependency_error = RuntimeError("Azure Blob catalog sources require the azure extra")
    dependency_error.__cause__ = ModuleNotFoundError("No module named 'azure'")
    provider._indexer._enumerate_blob_sources_concurrent = AsyncMock(side_effect=dependency_error)
    try:
        with pytest.raises(BackendUnavailableError, match="blob"):
            await provider.load()

        states = {state.source_id: state for state in provider.readiness().sources}
        assert states["local"].status == "success"
        assert states["local"].manifest_generation == 1
        assert states["blob"].status == "error"
        assert states["blob"].error == "optional_dependency_missing: source refresh failed"
        assert provider._db_owned.get_artifact("local-data", source_id="local") is not None
        assert not provider.readiness().ready
        assert "blob: optional_dependency_missing" in (provider.readiness().reason or "")
    finally:
        await provider.aclose()


def test_constructor_rejects_empty_sources_before_opening_sqlite():
    with patch.object(CatalogDB, "open") as open_db:
        with pytest.raises(ValueError, match="at least one source"):
            ManifestCatalogProvider(CatalogConfig())
    open_db.assert_not_called()


def test_constructor_rejects_infinite_default_stale_limit(tmp_path):
    with pytest.raises(ValueError, match="finite"):
        ManifestCatalogProvider(_local_config(tmp_path), max_stale_seconds=float("inf"))


async def test_first_load_failure_is_retryable_and_cancellation_closes_owned_sqlite(tmp_path, monkeypatch):
    missing = ManifestCatalogProvider(_local_config(tmp_path, "missing.json"))
    with pytest.raises(BackendUnavailableError, match="no valid generation"):
        await missing.load()
    assert not missing._closed
    assert missing._db_owned._conn is not None
    assert "preserved" not in (missing.readiness().reason or "")
    (tmp_path / "missing.json").write_text(json.dumps({"version": 1, "generation": 1, "artifacts": []}))
    assert await missing.load() == 0
    assert missing.readiness().ready
    await missing.aclose()
    assert missing._db_owned._conn is None
    await missing.aclose()

    (tmp_path / "manifest.json").write_text(json.dumps({"version": 1, "generation": 1, "artifacts": []}))
    cancelled = ManifestCatalogProvider(_local_config(tmp_path))
    monkeypatch.setattr(
        cancelled._indexer,
        "index",
        AsyncMock(side_effect=asyncio.CancelledError),
    )
    with pytest.raises(asyncio.CancelledError):
        await cancelled.load()
    assert cancelled._closed
    assert cancelled._db_owned._conn is None


async def test_manifest_provider_async_context_closes_owned_sqlite(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"version": 1, "generation": 1, "artifacts": []}))
    provider = ManifestCatalogProvider(_local_config(tmp_path))
    async with provider as opened:
        assert opened is provider
        await opened.load()
        assert opened.readiness().ready
    assert provider._closed
    assert provider._db_owned._conn is None


async def test_manifest_loads_are_serialized_and_close_waits_for_refresh(tmp_path, monkeypatch):
    (tmp_path / "manifest.json").write_text(json.dumps({"version": 1, "generation": 1, "artifacts": []}))
    provider = ManifestCatalogProvider(_local_config(tmp_path))
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    max_active = 0

    async def blocked_index():
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        entered.set()
        await release.wait()
        active -= 1
        return 0

    monkeypatch.setattr(provider._indexer, "index", blocked_index)
    first = asyncio.create_task(provider.load())
    await entered.wait()
    second = asyncio.create_task(provider.load())
    close = asyncio.create_task(provider.aclose())
    await asyncio.sleep(0)
    assert not second.done()
    assert not close.done()
    assert provider._db_owned._conn is not None

    release.set()
    assert await first == 0
    assert await second == 0
    close_result = await close
    assert close_result is None
    assert max_active == 1
    assert provider._closed
    assert provider._db_owned._conn is None


async def test_search_pagination_returns_more_than_one_hundred_matches(tmp_path):
    db = CatalogDB(tmp_path / "catalog.db", vec_dimensions=None)
    db.open()
    try:
        for index in range(120):
            db.upsert_artifact(
                artifact_id=f"artifact-{index:03d}",
                source_id="approved",
                logical_path=f"artifact-{index:03d}.csv",
                name=f"weather-{index:03d}.csv",
                storage_uri=f"/data/artifact-{index:03d}.csv",
                description="hourly weather observations",
            )
        provider = SQLiteCatalogProvider(db, ("approved",))
        cursor = None
        artifact_ids = []
        while True:
            page = await provider.search(
                SearchRequest("weather", page=PageRequest(limit=50, cursor=cursor)),
                RequestContext(),
            )
            artifact_ids.extend(artifact.reference.artifact_id for artifact in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break

        assert len(artifact_ids) == 120
        assert len(set(artifact_ids)) == 120
    finally:
        db.close()


async def test_catalog_cursor_rejects_unbounded_offset(tmp_path):
    db = CatalogDB(tmp_path / "catalog.db", vec_dimensions=None)
    db.open()
    try:
        provider = SQLiteCatalogProvider(db, ("approved",))
        request = {"operation": "list", "source_ids": [], "filters": {}}
        cursor = base64.urlsafe_b64encode(json.dumps({"offset": 10_001, "request": request}).encode()).decode()
        with pytest.raises(InvalidRequestError, match="offset"):
            await provider.list(
                ListRequest(page=PageRequest(cursor=cursor)),
                RequestContext(),
            )
    finally:
        db.close()


def test_catalog_does_not_emit_cursor_beyond_offset_limit():
    request = {"operation": "list", "source_ids": [], "filters": {}}

    assert _next_cursor(9_990, 100, 100, request) is None
    boundary_cursor = _next_cursor(9_990, 10, 10, request)
    assert boundary_cursor is not None
    assert _cursor_offset(boundary_cursor, request) == 10_000


@pytest.mark.parametrize(
    "manifest",
    [
        "../outside.json",
        "%2e%2e/outside.json",
        "az://account123/container/prefix/../outside.json",
    ],
)
def test_blob_manifest_rejects_dot_segment_prefix_escape(manifest):
    source = SourceConfig(
        source_id="approved",
        path="az://account123/container/prefix",
        discovery="manifest",
        manifest=manifest,
    )
    with pytest.raises(ValueError, match="dot segments"):
        CatalogIndexer._blob_manifest_name(source)


async def test_refresh_error_preserves_source_id_with_colon(tmp_path):
    provider = ManifestCatalogProvider(
        CatalogConfig(
            sources=[
                SourceConfig(
                    source_id="team:approved",
                    path=str(tmp_path),
                    discovery="manifest",
                    manifest="missing.json",
                )
            ]
        )
    )
    try:
        with pytest.raises(BackendUnavailableError, match="team:approved"):
            await provider.load()
        assert "team:approved" in (provider.readiness().reason or "")
    finally:
        await provider.aclose()


def test_versioned_config_conversion_and_boundaries(tmp_path):
    config, report = CatalogConfig.convert(
        {
            "sources": [
                {"path": str(tmp_path)},
                {
                    "source_id": "approved",
                    "path": "az://account123/container/prefix",
                    "discovery": "manifest",
                    "manifest": "manifest.json",
                },
            ]
        }
    )
    assert config.version == 1
    assert report["storage_accessed"] is False
    assert report["configuration_valid"] is True
    assert report["manifest_checked"] is False
    assert report["manifest_content_valid"] is None
    sources = cast(list[dict[str, object]], report["sources"])
    assert sources[0]["discovery"] == "scan"
    assert sources[1]["source_id"] == "approved"
    with pytest.raises(ValueError, match="Unsupported catalog configuration version"):
        CatalogConfig(version=2)
    with pytest.raises(ValueError, match="source path must be non-empty"):
        SourceConfig(path="")
    with pytest.raises(ValueError, match="source_id must be non-empty"):
        SourceConfig(path=str(tmp_path), source_id=" ")
    with pytest.raises(ValueError):
        SourceConfig(path=str(tmp_path), discovery="unsupported")
    with pytest.raises(ValueError, match="explicit stable source_id"):
        SourceConfig(path=str(tmp_path), discovery="manifest", manifest="manifest.json")
    with pytest.raises(ValueError, match="within the configured source prefix"):
        CatalogIndexer._blob_manifest_name(
            SourceConfig(
                source_id="approved",
                path="az://account123/container/prefix",
                discovery="manifest",
                manifest="az://account123/container/other/manifest.json",
            )
        )
    secret = "DO_NOT_PERSIST"
    source = SourceConfig(
        source_id="approved",
        path="az://account123/container/prefix",
        discovery="manifest",
        manifest=f"https://account123.blob.core.windows.net/container/prefix/manifest.json?sig={secret}",
    )
    assert source.manifest == "az://account123/container/prefix/manifest.json"
    assert secret not in repr(source)


async def test_config_conversion_and_storage_dry_run_do_not_mutate(tmp_path):
    (tmp_path / "data.csv").write_text("data")
    (tmp_path / "manifest.json").write_text(json.dumps(_manifest()))
    source_path = tmp_path / "catalog.yaml"
    source_path.write_text(
        f"sources:\n  - source_id: approved\n    path: {tmp_path}\n"
        "    discovery: manifest\n    manifest: manifest.json\n"
    )
    conversion = convert_catalog_config(source_path)
    assert conversion.changed
    assert not conversion.written
    assert "version: 1" in conversion.rendered_yaml
    assert conversion.summary["configuration_valid"] is True
    assert conversion.summary["manifest_checked"] is False
    assert conversion.summary["manifest_content_valid"] is None

    explicit_path = tmp_path / "explicit.yaml"
    explicit_path.write_text(conversion.rendered_yaml)
    explicit_conversion = convert_catalog_config(explicit_path)
    assert not explicit_conversion.changed
    with pytest.raises(ValueError, match="destination must differ"):
        convert_catalog_config(explicit_path, explicit_path, dry_run=False)

    db = CatalogDB(":memory:", vec_dimensions=None)
    db.open()
    try:
        config = CatalogConfig.from_yaml(source_path)
        dry_run = await CatalogIndexer(config, db).dry_run()
        assert not dry_run.has_errors
        assert dry_run.sources[0].added == 1
        assert dry_run.sources[0].manifest_generation == 1
        assert dry_run.configuration_valid
        assert dry_run.sources[0].manifest_checked
        assert dry_run.sources[0].manifest_content_valid is True
        assert db.list_artifacts() == []
        assert db.list_source_refresh_states() == []

        (tmp_path / "manifest.json").write_text("{")
        invalid_dry_run = await CatalogIndexer(config, db).dry_run()
        assert invalid_dry_run.has_errors
        assert invalid_dry_run.sources[0].manifest_checked
        assert invalid_dry_run.sources[0].manifest_content_valid is False
        assert db.list_artifacts() == []
        assert db.list_source_refresh_states() == []
    finally:
        db.close()


def test_schema_v2_cache_is_upgraded_for_manifest_refresh_state(tmp_path):
    db_path = tmp_path / "v2.db"
    db = CatalogDB(db_path, vec_dimensions=2)
    db.open()
    artifact_id = db.upsert_artifact(
        artifact_id="artifact",
        source_id="source",
        logical_path="data.csv",
        source_root="/data",
        name="data.csv",
        storage_uri="/data/data.csv",
        description="first",
        source_type="local",
        indexed_at="2026-01-01T00:00:00+00:00",
        content_revision="content-1",
        metadata_revision="metadata-1",
        aliases=["external:stable"],
        embedding=[1.0, 0.0],
    )
    db.upsert_artifact(
        artifact_id=artifact_id,
        source_id="source",
        logical_path="data.csv",
        source_root="/data",
        name="data.csv",
        storage_uri="/data/data.csv",
        description="second",
        source_type="local",
        indexed_at="2026-01-02T00:00:00+00:00",
        content_revision="content-2",
        metadata_revision="metadata-2",
        aliases=["external:stable"],
        embedding=[0.0, 1.0],
        _replace_embedding=True,
    )
    db.apply_refresh_batch(
        [],
        [],
        [
            {
                "source_id": "source",
                "source_type": "local",
                "root_uri": "/data",
                "attempted_at": "2026-01-02T00:00:00+00:00",
                "succeeded": True,
                "artifact_count": 1,
                "error": None,
            }
        ],
    )
    db.close()

    connection = sqlite3.connect(db_path)
    connection.execute("ALTER TABLE catalog_source_refreshes DROP COLUMN manifest_generation")
    connection.execute("ALTER TABLE catalog_source_refreshes DROP COLUMN manifest_etag")
    connection.execute("PRAGMA user_version = 2")
    connection.commit()
    connection.close()

    migrated = CatalogDB(db_path, vec_dimensions=2)
    migrated.open()
    try:
        columns = {row["name"] for row in migrated.conn.execute("PRAGMA table_info(catalog_source_refreshes)")}
        assert {"manifest_generation", "manifest_etag"} <= columns
        assert migrated.conn.execute("PRAGMA user_version").fetchone()[0] == 3
        record = migrated.get_artifact("external:stable", source_id="source")
        assert record is not None
        assert record.description == "second"
        assert len(migrated.list_revisions("artifact", source_id="source")) == 2
        assert migrated.resolve_artifact_id("external:stable", "source") == artifact_id
        state = migrated.get_source_refresh_state("source")
        assert state is not None
        assert state.successful_generation == 1
        assert state.artifact_count == 1
        assert state.manifest_generation is None
        assert state.manifest_etag is None
        assert migrated.has_vector(artifact_id)
        assert migrated.search("", query_embedding=[0.0, 1.0])[0].id == artifact_id
    finally:
        migrated.close()
