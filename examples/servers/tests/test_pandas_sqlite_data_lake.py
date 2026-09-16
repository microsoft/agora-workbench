from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest

from agora_workbench.code_execution.catalog_integration import _encode_reference
from agora_workbench.data_lake import (
    ArtifactReference,
    InvalidRequestError,
    ListRequest,
    PageRequest,
    RequestContext,
    SearchRequest,
    TransferOptions,
)
from examples.servers.pandas_datalake.server.pandas_datalake_server import create_server
from examples.servers.pandas_datalake.server.sqlite_data_lake import (
    SQLiteCatalogProvider,
    SQLiteObjectFetcher,
    SQLiteObjectLake,
)


async def test_sqlite_catalog_keeps_revisions_and_pages_results(tmp_path: Path):
    lake = SQLiteObjectLake(tmp_path / "lake.sqlite3")
    lake.initialize()
    revision_one = lake.put_object(
        "sales.csv",
        b"region,revenue\nwest,10\n",
        name="sales.csv",
        description="Sales data",
        media_type="text/csv",
        metadata={"domain": "tabular", "source_type": "sqlite"},
    )
    lake.put_object(
        "customers.csv",
        b"id,active\n1,true\n",
        name="customers.csv",
        media_type="text/csv",
        metadata={"domain": "tabular", "source_type": "sqlite"},
    )
    revision_two = lake.put_object(
        "sales.csv",
        b"region,revenue\nwest,12\n",
        name="sales.csv",
        description="Sales data",
        media_type="text/csv",
        metadata={"domain": "tabular", "source_type": "sqlite"},
    )
    provider = SQLiteCatalogProvider(lake)

    page_one = await provider.list(
        ListRequest(page=PageRequest(limit=1)),
        RequestContext(caller_id="developer"),
    )
    assert len(page_one.items) == 1
    assert page_one.next_cursor is not None
    page_two = await provider.list(
        ListRequest(page=PageRequest(limit=1, cursor=page_one.next_cursor)),
        RequestContext(caller_id="developer"),
    )
    assert len(page_two.items) == 1
    assert page_two.next_cursor is None

    found = await provider.search(
        SearchRequest(
            "sales",
            filters={"domain": "tabular", "source_type": "sqlite"},
        ),
        RequestContext(caller_id="developer"),
    )
    assert [artifact.reference.artifact_id for artifact in found.items] == ["sales.csv"]
    assert found.items[0].revision == revision_two.revision

    old = await provider.get(revision_one, RequestContext(caller_id="developer"))
    current = await provider.get(
        ArtifactReference("sales.csv", lake.source_id),
        RequestContext(caller_id="developer"),
    )
    assert old.revision == 1
    assert old.reference.revision == 1
    assert current.revision == 2
    assert current.reference.revision is None

    with pytest.raises(InvalidRequestError, match="Invalid SQLite catalog cursor"):
        await provider.list(
            ListRequest(page=PageRequest(cursor="!not-base64!")),
            RequestContext(caller_id="developer"),
        )


async def test_sqlite_fetcher_streams_blob_with_integrity_checks(tmp_path: Path):
    lake = SQLiteObjectLake(tmp_path / "lake.sqlite3")
    lake.initialize()
    content = b"region,revenue\nwest,10\n"
    reference = lake.put_object("sales.csv", content, media_type="text/csv")
    fetcher = SQLiteObjectFetcher(lake)
    destination = tmp_path / "cached.csv"

    result = await fetcher.fetch_to_file_result(
        lake.locator_for(reference),
        destination,
        options=TransferOptions(expected_sha256=hashlib.sha256(content).hexdigest()),
        context=RequestContext(caller_id="developer"),
    )

    assert result.bytes_transferred == len(content)
    assert destination.read_bytes() == content


async def test_server_catalog_load_path_reaches_pandas(tmp_path: Path):
    server = create_server(
        tmp_path / "lake.sqlite3",
        auto_build=False,
        seed_demo_data=True,
    )
    assert server.catalog is not None
    await server.catalog.startup()
    try:
        session_id = server.session_manager.create_session({}, "developer", "token", {})
        session = server.session_manager.get_session(session_id)
        binding = session.extensions["catalog"]
        page = await binding.catalog.search(
            SearchRequest(
                "sales",
                filters={"domain": "tabular", "source_type": "sqlite"},
            ),
            binding.context,
        )
        artifact = page.items[0]
        reference = ArtifactReference(
            artifact.reference.artifact_id,
            artifact.reference.source_id,
            artifact.revision,
        )
        cached_path = await session.data_manager.get_cache_path(f"<blob>{_encode_reference(reference)}</blob>")

        frame = pd.read_csv(cached_path)
        assert frame.groupby("region")["revenue"].sum().to_dict() == {"east": 208, "west": 255}
    finally:
        await server.session_manager.aclose_all_sessions()
        await server.catalog.shutdown()
