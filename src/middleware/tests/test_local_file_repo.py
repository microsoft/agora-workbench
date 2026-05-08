from __future__ import annotations

from pathlib import Path

import pytest

from middleware.tool_learning.adapters import maf_function
from middleware.tool_learning.config import ToolLearningConfig
from middleware.tool_learning.local_file_repo import LocalFileVignetteRepo
from middleware.tool_learning.models import AntiPattern, MatchSpec, ToolSignature, Vignette


def _make_vignette(tool_name: str = "calendar.create_event", tags: list[str] | None = None) -> Vignette:
    return Vignette(
        vignette_id="v-001",
        kind="anti_pattern",
        scope="global",
        tool=ToolSignature(tool_name=tool_name),
        match=MatchSpec(arg_keys=["timezone"]),
        title="Timezone required",
        summary="Must include timezone.",
        anti_pattern=AntiPattern(rule="Do not omit timezone.", severity="hard"),
        tags=tags or [],
    )


@pytest.mark.unit
def test_tool_learning_config_reads_local_dir_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TOOL_LEARNING_LOCAL_DIR", "/tmp/agora-vignettes")
    config = ToolLearningConfig.from_env()
    assert config.local_storage_dir == "/tmp/agora-vignettes"


@pytest.mark.unit
def test_local_file_repo_upsert_and_get_for_tool(tmp_path: Path):
    repo = LocalFileVignetteRepo(ToolLearningConfig(local_storage_dir=str(tmp_path)))

    repo.upsert_vignette(_make_vignette(tags=["alpha"]))
    repo.upsert_vignette(_make_vignette(tags=["beta"]))

    vignettes = repo.get_vignettes_for_tool("calendar.create_event")
    assert len(vignettes) == 1
    assert vignettes[0].confidence == pytest.approx(0.75)
    assert vignettes[0].tags == ["alpha", "beta"]


@pytest.mark.unit
def test_vignette_function_middleware_prefers_table_when_configured(monkeypatch: pytest.MonkeyPatch):
    class FakeSearchRepo:
        def __init__(self, config, credential):
            del config, credential

    class FakeTableRepo:
        def __init__(self, config, credential):
            del config, credential

    class FakeLocalRepo:
        def __init__(self, config):
            del config

    monkeypatch.setattr(maf_function, "SearchVignetteRepo", FakeSearchRepo)
    monkeypatch.setattr(maf_function, "TableVignetteRepo", FakeTableRepo)
    monkeypatch.setattr(maf_function, "LocalFileVignetteRepo", FakeLocalRepo)

    config = ToolLearningConfig(table_storage_endpoint="https://example.table.core.windows.net", local_storage_dir="/tmp/v")
    middleware = maf_function.VignetteFunctionMiddleware(config=config, credential=object(), write_vignettes=True)

    assert isinstance(middleware._write_repo, FakeTableRepo)


@pytest.mark.unit
def test_vignette_function_middleware_uses_local_when_table_unset(monkeypatch: pytest.MonkeyPatch):
    class FakeSearchRepo:
        def __init__(self, config, credential):
            del config, credential

    class FakeTableRepo:
        def __init__(self, config, credential):
            del config, credential

    class FakeLocalRepo:
        def __init__(self, config):
            del config

    monkeypatch.setattr(maf_function, "SearchVignetteRepo", FakeSearchRepo)
    monkeypatch.setattr(maf_function, "TableVignetteRepo", FakeTableRepo)
    monkeypatch.setattr(maf_function, "LocalFileVignetteRepo", FakeLocalRepo)

    config = ToolLearningConfig(local_storage_dir="/tmp/v")
    middleware = maf_function.VignetteFunctionMiddleware(config=config, credential=object(), write_vignettes=True)

    assert isinstance(middleware._write_repo, FakeLocalRepo)


@pytest.mark.unit
def test_vignette_function_middleware_respects_storage_override(monkeypatch: pytest.MonkeyPatch):
    class FakeSearchRepo:
        def __init__(self, config, credential):
            del config, credential

    class FakeTableRepo:
        def __init__(self, config, credential):
            del config, credential

    class FakeLocalRepo:
        def __init__(self, config):
            del config

    monkeypatch.setattr(maf_function, "SearchVignetteRepo", FakeSearchRepo)
    monkeypatch.setattr(maf_function, "TableVignetteRepo", FakeTableRepo)
    monkeypatch.setattr(maf_function, "LocalFileVignetteRepo", FakeLocalRepo)

    config = ToolLearningConfig(table_storage_endpoint="https://example.table.core.windows.net")
    middleware = maf_function.VignetteFunctionMiddleware(
        config=config,
        credential=object(),
        write_vignettes=True,
        storage="local",
    )

    assert isinstance(middleware._write_repo, FakeLocalRepo)
