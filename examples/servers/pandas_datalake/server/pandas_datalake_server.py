"""Minimal pandas environment with a SQLite-backed object data lake."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from agora_workbench.code_execution import CatalogIntegration, CodeExecutionServer, ServerConfig
from agora_workbench.code_execution.auth import create_noop_auth_config
from agora_workbench.data_lake import (
    DevelopmentAllowAllCatalogAuthorizer,
    ResourceLease,
    ResourceOwnership,
)
from examples.servers.pandas_datalake.server.sqlite_data_lake import (
    SQLiteCatalogProvider,
    SQLiteObjectLake,
    SQLiteObjectFetcher,
)

PANDAS_REQUIREMENTS = """\
pandas>=2.3.3
pyarrow>=22.0.0
"""

PANDAS_PRELUDE = """\
import pandas as pd
import pyarrow as pa
"""


class PandasDataLakeServer(CodeExecutionServer):
    """Code-execution server with pandas and pyarrow imported in every request."""

    def preprocess_code(self, code: str) -> str:
        return PANDAS_PRELUDE + code


def create_server(
    database_path: str | Path,
    *,
    auto_build: bool = True,
    seed_demo_data: bool = True,
) -> PandasDataLakeServer:
    """Create a pandas server whose catalog metadata and object bytes live in SQLite."""
    lake = SQLiteObjectLake(database_path)

    catalog = CatalogIntegration(
        ResourceLease(
            SQLiteCatalogProvider(lake, seed_demo_data=seed_demo_data),
            ResourceOwnership.OWNED,
        ),
        authorizer=DevelopmentAllowAllCatalogAuthorizer(),
        fetcher_factory=lambda _context: SQLiteObjectFetcher(lake),
    )
    config = ServerConfig(
        name="pandas",
        description=(
            "Execute Python with pandas and pyarrow. Use search_data to discover tabular objects "
            "stored as BLOBs in the local SQLite data lake, then pass a result's load_path to "
            "pd.read_csv or another pandas reader."
        ),
        type="uv",
        dependency_file=PANDAS_REQUIREMENTS,
        auto_build=auto_build,
    )
    return PandasDataLakeServer(
        server_config=config,
        auth_config=create_noop_auth_config(),
        catalog=catalog,
    )


DATABASE_PATH = Path(os.getenv("PANDAS_DATALAKE_DB", "/tmp/agora-pandas-datalake.sqlite3"))
server = create_server(DATABASE_PATH)


if __name__ == "__main__":
    if "--warm" in sys.argv:
        asyncio.run(server.warm())
    else:
        host = os.getenv("HOST", "127.0.0.1")
        port = int(os.getenv("PORT", "8022"))
        asyncio.run(server.run_http(host=host, port=port))
