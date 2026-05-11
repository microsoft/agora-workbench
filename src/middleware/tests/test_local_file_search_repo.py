"""Tests for the local BM25 vignette search backend."""

from __future__ import annotations

from pathlib import Path

import pytest

from middleware.tool_learning.adapters import maf_function
from middleware.tool_learning.config import ToolLearningConfig
from middleware.tool_learning.local_file_repo import LocalFileVignetteRepo
from middleware.tool_learning.local_file_search_repo import LocalFileSearchVignetteRepo
from middleware.tool_learning.models import (
    AntiPattern,
    MatchSpec,
    RepairStrategy,
    ToolSignature,
    Vignette,
)


def _anti_pattern(
    *,
    vignette_id: str,
    tool_name: str = "calendar.create_event",
    title: str = "Title",
    summary: str = "Summary",
    rule: str = "Some rule",
    tags: list[str] | None = None,
    scope: str = "global",
    tenant_id: str | None = None,
    user_id: str | None = None,
    confidence: float = 0.7,
) -> Vignette:
    return Vignette(
        vignette_id=vignette_id,
        kind="anti_pattern",
        scope=scope,  # type: ignore[arg-type]
        tenant_id=tenant_id,
        user_id=user_id,
        tool=ToolSignature(tool_name=tool_name),
        match=MatchSpec(arg_keys=["timezone"]),
        title=title,
        summary=summary,
        anti_pattern=AntiPattern(rule=rule, severity="hard"),
        tags=tags or [],
        confidence=confidence,
    )


def _repair(
    *,
    vignette_id: str,
    tool_name: str = "calendar.create_event",
    error_class: str = "ValueError",
    title: str = "Repair title",
    summary: str = "Repair summary",
    steps: list[str] | None = None,
) -> Vignette:
    return Vignette(
        vignette_id=vignette_id,
        kind="repair_template",
        scope="global",
        tool=ToolSignature(tool_name=tool_name),
        match=MatchSpec(error_class=error_class),
        title=title,
        summary=summary,
        repair=RepairStrategy(steps=steps or ["retry once"], max_retries=1),
    )


@pytest.mark.unit
def test_local_search_returns_empty_when_no_files(tmp_path: Path):
    repo = LocalFileSearchVignetteRepo(ToolLearningConfig(local_storage_dir=str(tmp_path)))
    assert repo.search_vignettes("anything", "calendar.create_event") == []


@pytest.mark.unit
def test_local_search_ranks_keyword_matches_first(tmp_path: Path):
    config = ToolLearningConfig(local_storage_dir=str(tmp_path))
    writer = LocalFileVignetteRepo(config)
    writer.upsert_vignette(
        _anti_pattern(vignette_id="v-tz", title="Timezone", summary="Always include timezone for events.")
    )
    writer.upsert_vignette(
        _anti_pattern(vignette_id="v-attendees", title="Attendees", summary="Validate attendee email addresses.")
    )

    repo = LocalFileSearchVignetteRepo(config)
    results = repo.search_vignettes("timezone missing", "calendar.create_event")

    assert [v.vignette_id for v in results][0] == "v-tz"


@pytest.mark.unit
def test_local_search_filters_by_kind_and_error_class(tmp_path: Path):
    config = ToolLearningConfig(local_storage_dir=str(tmp_path))
    writer = LocalFileVignetteRepo(config)
    writer.upsert_vignette(_anti_pattern(vignette_id="v-ap", summary="timezone guidance"))
    writer.upsert_vignette(_repair(vignette_id="v-rep", summary="timezone repair", error_class="TimezoneError"))

    repo = LocalFileSearchVignetteRepo(config)

    only_repair = repo.search_vignettes("timezone", "calendar.create_event", kind="repair_template")
    assert [v.vignette_id for v in only_repair] == ["v-rep"]

    by_error = repo.search_vignettes(
        "timezone",
        "calendar.create_event",
        kind="repair_template",
        error_class="TimezoneError",
    )
    assert [v.vignette_id for v in by_error] == ["v-rep"]

    no_match = repo.search_vignettes(
        "timezone",
        "calendar.create_event",
        kind="repair_template",
        error_class="OtherError",
    )
    assert no_match == []


@pytest.mark.unit
def test_local_search_scope_filtering(tmp_path: Path):
    config = ToolLearningConfig(local_storage_dir=str(tmp_path))
    writer = LocalFileVignetteRepo(config)

    writer.upsert_vignette(_anti_pattern(vignette_id="v-global", summary="global timezone advice"))
    writer.upsert_vignette(
        _anti_pattern(
            vignette_id="v-org",
            scope="org",
            tenant_id="tenant-A",
            summary="org timezone advice",
        )
    )
    writer.upsert_vignette(
        _anti_pattern(
            vignette_id="v-user",
            scope="user",
            tenant_id="tenant-A",
            user_id="user-1",
            summary="user timezone advice",
        )
    )

    repo = LocalFileSearchVignetteRepo(config)

    # Anonymous caller: only global is visible.
    anon = repo.search_vignettes("timezone", "calendar.create_event")
    assert {v.vignette_id for v in anon} == {"v-global"}

    # Tenant-only caller: global + org.
    tenant = repo.search_vignettes("timezone", "calendar.create_event", tenant_id="tenant-A")
    assert {v.vignette_id for v in tenant} == {"v-global", "v-org"}

    # Full identity: global + org + user.
    full = repo.search_vignettes(
        "timezone",
        "calendar.create_event",
        tenant_id="tenant-A",
        user_id="user-1",
    )
    assert {v.vignette_id for v in full} == {"v-global", "v-org", "v-user"}

    # Different tenant: only global (other tenant's org/user filtered out).
    other = repo.search_vignettes("timezone", "calendar.create_event", tenant_id="tenant-B")
    assert {v.vignette_id for v in other} == {"v-global"}


@pytest.mark.unit
def test_local_search_min_confidence_filter(tmp_path: Path):
    config = ToolLearningConfig(local_storage_dir=str(tmp_path), min_confidence=0.8)
    writer = LocalFileVignetteRepo(config)
    writer.upsert_vignette(_anti_pattern(vignette_id="v-low", summary="timezone weak", confidence=0.5))
    writer.upsert_vignette(_anti_pattern(vignette_id="v-high", summary="timezone strong", confidence=0.9))

    repo = LocalFileSearchVignetteRepo(config)
    results = repo.search_vignettes("timezone", "calendar.create_event")
    assert [v.vignette_id for v in results] == ["v-high"]


@pytest.mark.unit
def test_local_search_rebuilds_on_file_change(tmp_path: Path):
    config = ToolLearningConfig(local_storage_dir=str(tmp_path))
    writer = LocalFileVignetteRepo(config)
    writer.upsert_vignette(_anti_pattern(vignette_id="v-1", summary="timezone first"))

    repo = LocalFileSearchVignetteRepo(config)
    first = repo.search_vignettes("timezone", "calendar.create_event")
    assert {v.vignette_id for v in first} == {"v-1"}

    # Add another vignette with a distinct id; bump mtime by writing again.
    writer.upsert_vignette(_anti_pattern(vignette_id="v-2", summary="timezone second"))
    # Force mtime change (some filesystems have second-resolution mtimes).
    path = writer._tool_file_path(None, "calendar.create_event")
    import os
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    second = repo.search_vignettes("timezone", "calendar.create_event")
    assert {v.vignette_id for v in second} == {"v-1", "v-2"}


@pytest.mark.unit
def test_local_search_top_k_limits_results(tmp_path: Path):
    config = ToolLearningConfig(local_storage_dir=str(tmp_path))
    writer = LocalFileVignetteRepo(config)
    for i in range(5):
        writer.upsert_vignette(
            _anti_pattern(vignette_id=f"v-{i}", summary=f"timezone variation {i}")
        )

    repo = LocalFileSearchVignetteRepo(config)
    assert len(repo.search_vignettes("timezone", "calendar.create_event", top_k=2)) == 2


# ----------------------------------------------------------------------
# Middleware backend selection
# ----------------------------------------------------------------------


@pytest.mark.unit
def test_middleware_selects_local_search_when_only_local_dir_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config = ToolLearningConfig(local_storage_dir=str(tmp_path))
    middleware = maf_function.VignetteFunctionMiddleware(
        config=config,
        write_vignettes=False,
    )
    assert isinstance(middleware._search_repo, LocalFileSearchVignetteRepo)


@pytest.mark.unit
def test_middleware_search_override_local_forces_local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config = ToolLearningConfig(
        local_storage_dir=str(tmp_path),
        search_endpoint="https://example.search.windows.net",
    )
    middleware = maf_function.VignetteFunctionMiddleware(
        config=config,
        write_vignettes=False,
        search="local",
    )
    assert isinstance(middleware._search_repo, LocalFileSearchVignetteRepo)


@pytest.mark.unit
def test_middleware_search_invalid_value_raises(tmp_path: Path):
    config = ToolLearningConfig(local_storage_dir=str(tmp_path))
    with pytest.raises(ValueError):
        maf_function.VignetteFunctionMiddleware(config=config, search="bogus")


@pytest.mark.unit
def test_middleware_no_search_backend_when_nothing_configured():
    middleware = maf_function.VignetteFunctionMiddleware(
        config=ToolLearningConfig(),
        write_vignettes=False,
    )
    assert middleware._search_repo is None


@pytest.mark.unit
def test_middleware_warns_when_read_and_write_backends_disagree(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
):
    # Stub the Azure write repo so we don't need real credentials.
    class FakeTableRepo:
        def __init__(self, config, credential):
            del config, credential

    monkeypatch.setattr(maf_function, "TableVignetteRepo", FakeTableRepo)

    config = ToolLearningConfig(
        local_storage_dir=str(tmp_path),
        table_storage_endpoint="https://example.table.core.windows.net",
    )
    with caplog.at_level("WARNING", logger=maf_function.LOGGER.name):
        maf_function.VignetteFunctionMiddleware(
            config=config,
            credential=object(),
            write_vignettes=True,
            search="local",
            storage="table",
        )

    assert any("target different stores" in rec.message for rec in caplog.records)


@pytest.mark.unit
def test_middleware_no_warning_when_backends_agree(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
):
    config = ToolLearningConfig(local_storage_dir=str(tmp_path))
    with caplog.at_level("WARNING", logger=maf_function.LOGGER.name):
        maf_function.VignetteFunctionMiddleware(
            config=config,
            write_vignettes=True,
        )
    assert not any("target different stores" in rec.message for rec in caplog.records)
