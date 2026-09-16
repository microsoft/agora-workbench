from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from agora_workbench.code_execution.catalog_integration import register_catalog_discovery_tools
from agora_workbench.code_execution.sessions import SessionContext
from agora_workbench.data_lake.execution import DataLakeDataManager


REPO_ROOT = Path(__file__).resolve().parents[3]
ENERGYSYSTEMS_ROOT = REPO_ROOT / "examples" / "servers" / "energysystems"
CUSTOM_STORAGE_PATH = ENERGYSYSTEMS_ROOT / "server" / "custom_storage.py"


def _load_custom_storage_module():
    spec = importlib.util.spec_from_file_location("energysystems_custom_storage", CUSTOM_STORAGE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_energysystems_catalog_search_load_path_fetches_packaged_dataset():
    custom_storage = _load_custom_storage_module()
    integration = custom_storage.create_catalog_integration(ENERGYSYSTEMS_ROOT)
    context = SessionContext(
        session_id="energysystems-demo-session",
        user_identity="local-developer",
        user_token="",
    )
    binding = None
    manager = None

    await integration.startup()
    try:
        binding = integration.bind_session(context, execution_references=True)
        fetchers = integration.create_fetchers(context)
        manager = DataLakeDataManager(credential=MagicMock())
        manager.bind_catalog_resolver(binding.resolver, fetchers=fetchers)
        session = SimpleNamespace(
            session_id=context.session_id,
            data_manager=manager,
            extensions={"catalog": binding},
        )
        captured: dict[str, object] = {}
        fake_server = SimpleNamespace(
            mcp=SimpleNamespace(
                tool=lambda name, description: lambda function: captured.setdefault(name, function),
            ),
            _get_or_create_session=AsyncMock(return_value=session),
        )
        register_catalog_discovery_tools(fake_server, integration)

        search_data = cast(Any, captured["search_data"])
        results = await search_data("50 bus")

        assert isinstance(results, list)
        assert len(results) == 1
        assert results[0]["id"] == "grid-50bus"
        assert results[0]["source_id"] == "energysystems-demo"
        assert results[0]["load_path"].startswith("<blob>catalog-v1:")
        assert "energysystems://" not in repr(results)

        cached_path = await manager.get_cache_path(results[0]["load_path"])
        source_path = ENERGYSYSTEMS_ROOT / "data" / "grid_50bus.nc"
        assert cached_path != source_path
        assert cached_path.read_bytes() == source_path.read_bytes()
    finally:
        if manager is not None:
            await manager.aclose()
        if binding is not None:
            await binding.aclose()
        await integration.shutdown()


@pytest.mark.asyncio
async def test_energysystems_fetcher_rejects_locator_traversal():
    custom_storage = _load_custom_storage_module()
    fetcher = custom_storage.EnergySystemsDatasetFetcher(ENERGYSYSTEMS_ROOT / "data")
    try:
        with pytest.raises(ValueError, match="dataset path"):
            await fetcher.fetch("energysystems://datasets/../catalog.yaml")
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_energysystems_catalog_creates_fresh_session_fetchers():
    custom_storage = _load_custom_storage_module()
    integration = custom_storage.create_catalog_integration(ENERGYSYSTEMS_ROOT)
    context = SessionContext(
        session_id="energysystems-demo-session",
        user_identity="local-developer",
        user_token="",
    )

    first = integration.create_fetchers(context)
    second = integration.create_fetchers(context)
    try:
        assert len(first) == len(second) == 1
        assert first[0] is not second[0]
    finally:
        for fetcher in (*first, *second):
            await getattr(fetcher, "close")()
        await integration.shutdown()
