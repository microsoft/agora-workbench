"""Subprocess tests for optional catalog dependency boundaries."""

from __future__ import annotations

import subprocess
import sys
import textwrap


def _run_isolated(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_keyword_catalog_blocks_optional_imports_and_reopens():
    result = _run_isolated(
        """
        import asyncio
        import importlib.abc
        import sys
        import tempfile
        from pathlib import Path

        blocked = (
            "azure",
            "openai",
            "sqlite_vec",
        )

        class BlockOptionalImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if any(fullname == name or fullname.startswith(f"{name}.") for name in blocked):
                    raise ModuleNotFoundError(f"blocked optional import: {fullname}", name=fullname)
                return None

        sys.meta_path.insert(0, BlockOptionalImports())

        from agora_workbench.code_execution.data_access.catalog import (
            CatalogConfig,
            CatalogDB,
            CatalogIndexer,
            SourceConfig,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            source.mkdir()
            (source / "weather.csv").write_text("date,temp\\n2026-01-01,5.2", encoding="utf-8")
            db_path = root / "catalog.db"
            config = CatalogConfig(sources=[SourceConfig(path=str(source), domain="weather")])

            db = CatalogDB(db_path)
            db.open()
            assert db.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'artifacts_vec'"
            ).fetchone() is None
            assert asyncio.run(CatalogIndexer(config, db).index()) == 1
            assert asyncio.run(CatalogIndexer(config, db).index()) == 0
            assert db.search("weather")[0].name == "weather.csv"
            db.close()

            reopened = CatalogDB(db_path)
            reopened.open()
            assert reopened.search("weather")[0].name == "weather.csv"
            reopened.close()

        for module_name in sys.modules:
            assert not any(
                module_name == name or module_name.startswith(f"{name}.")
                for name in blocked
            ), module_name
        """
    )

    assert result.returncode == 0, result.stderr


def test_vector_selection_reports_missing_sqlite_vec():
    result = _run_isolated(
        """
        import importlib.abc
        import sys

        class BlockSqliteVec(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "sqlite_vec":
                    raise ModuleNotFoundError("blocked sqlite_vec", name=fullname)
                return None

        sys.meta_path.insert(0, BlockSqliteVec())

        from agora_workbench.code_execution.data_access.catalog import CatalogDB

        db = CatalogDB(":memory:", vec_dimensions=2)
        db.open()
        try:
            try:
                db.upsert_artifact(
                    artifact_id="a",
                    name="a.csv",
                    storage_uri="/a.csv",
                    indexed_at="2026-01-01T00:00:00Z",
                    embedding=[1.0, 0.0],
                )
            except RuntimeError as exc:
                assert "agora-workbench[catalog-vector]" in str(exc)
            else:
                raise AssertionError("vector operation unexpectedly succeeded")
            assert db.get_artifact("a") is None
        finally:
            db.close()
        """
    )

    assert result.returncode == 0, result.stderr


def test_noop_auth_import_does_not_require_azure_sdk():
    result = _run_isolated(
        """
        import importlib.abc
        import sys

        class BlockAzure(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "azure" or fullname.startswith("azure."):
                    raise ModuleNotFoundError(f"blocked Azure SDK import: {fullname}", name=fullname)
                return None

        sys.meta_path.insert(0, BlockAzure())

        import agora_workbench.code_execution.auth as auth
        import agora_workbench.code_execution.tools.search as search
        from agora_workbench.code_execution.auth import create_noop_auth_config
        from agora_workbench.code_execution.tools.search import create_tool_search_backend

        assert create_noop_auth_config() is not None
        assert type(create_tool_search_backend("bm25")).__name__ == "BM25ToolSearchBackend"
        assert auth.noop.__name__.endswith(".auth.noop")
        assert auth.base.__name__.endswith(".auth.base")
        assert search.bm25_tool_search.__name__.endswith(".search.bm25_tool_search")
        assert search.state_graph.__name__.endswith(".search.state_graph")
        try:
            create_tool_search_backend("azure_ai_search")
        except RuntimeError as exc:
            assert "agora-workbench[azure]" in str(exc)
        else:
            raise AssertionError("Azure tool search unexpectedly initialized")

        try:
            from agora_workbench.code_execution.auth import EntraCredentialProvider
        except ImportError as exc:
            assert "agora-workbench[azure]" in str(exc)
        else:
            raise AssertionError(f"Azure auth unexpectedly imported: {EntraCredentialProvider}")

        for module_owner, attribute in (
            (auth, "entra"),
            (auth, "azure_credentials"),
            (search, "azure_ai_tool_search"),
        ):
            try:
                getattr(module_owner, attribute)
            except ImportError as exc:
                assert "agora-workbench[azure]" in str(exc)
            else:
                raise AssertionError(f"Optional submodule unexpectedly imported: {attribute}")

        assert not any(name == "azure" or name.startswith("azure.") for name in sys.modules)
        """
    )

    assert result.returncode == 0, result.stderr


def test_v021_eager_import_surface_does_not_require_optional_sdks():
    result = _run_isolated(
        """
        import importlib.abc
        import sys

        blocked = ("azure", "openai", "sqlite_vec")

        class BlockOptionalImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if any(fullname == name or fullname.startswith(f"{name}.") for name in blocked):
                    raise ModuleNotFoundError(f"blocked optional import: {fullname}", name=fullname)
                return None

        sys.meta_path.insert(0, BlockOptionalImports())

        # Explicitly load the modules exported by the v0.2.1 eager package
        # initializers. PR #345 restores these imports, so #335 must remain safe
        # without depending on the current parent-package __getattr__ behavior.
        from agora_workbench.base import BaseMCPServer
        from agora_workbench.code_execution.code_execution_models import (
            AssetSpec,
            CodeExecutionResult,
            ServerConfig,
            SidecarConfig,
        )
        from agora_workbench.code_execution.data_access.artifact_resolvers import (
            ArtifactResolver,
            SearchIndexArtifactResolver,
        )
        from agora_workbench.code_execution.data_access.credentials import (
            MsalCacheCredential,
            create_storage_credential,
        )
        from agora_workbench.code_execution.data_access.publishers import (
            AssetPublisher,
            BlobPublisher,
            GuiPublisher,
            LocalFilePublisher,
            ServerPublisher,
            parse_destination_tag,
        )
        from agora_workbench.code_execution.data_access.resolution import (
            AssetResolutionMiddleware,
            looks_like_qualified_name,
            should_resolve_as_asset,
        )
        from agora_workbench.code_execution.server import CodeExecutionServer
        from agora_workbench.code_execution.skills import Skill, discover_skills
        from agora_workbench.code_execution.tool_registry import (
            ReturnSpec,
            State,
            StateTransition,
            ToolDefinition,
            ToolParameter,
            ToolRegistry,
        )
        from agora_workbench.connector import (
            ConnectorServer,
            DispatcherConfig,
            DispatcherServer,
            GatewayConfig,
            GatewayPolicy,
            GatewayServer,
            RouterConfig,
            RouterServer,
            UpstreamConfig,
            WorkerConfig,
        )

        assert all(
            value is not None
            for value in (
                AssetResolutionMiddleware,
                ArtifactResolver,
                AssetPublisher,
                AssetSpec,
                BaseMCPServer,
                BlobPublisher,
                CodeExecutionResult,
                CodeExecutionServer,
                ConnectorServer,
                DispatcherConfig,
                DispatcherServer,
                GatewayConfig,
                GatewayPolicy,
                GatewayServer,
                GuiPublisher,
                LocalFilePublisher,
                MsalCacheCredential,
                ReturnSpec,
                RouterConfig,
                RouterServer,
                SearchIndexArtifactResolver,
                ServerConfig,
                ServerPublisher,
                SidecarConfig,
                Skill,
                State,
                StateTransition,
                ToolDefinition,
                ToolParameter,
                ToolRegistry,
                UpstreamConfig,
                WorkerConfig,
                create_storage_credential,
                discover_skills,
                looks_like_qualified_name,
                parse_destination_tag,
                should_resolve_as_asset,
            )
        )
        assert not any(
            any(name == blocked_name or name.startswith(f"{blocked_name}.") for blocked_name in blocked)
            for name in sys.modules
        )
        """
    )

    assert result.returncode == 0, result.stderr
