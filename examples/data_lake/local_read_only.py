"""Zero-cloud catalog quickstart using only public Agora Workbench APIs."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import yaml

from agora_workbench.data_lake import ListRequest, PageRequest, RequestContext, SearchRequest
from agora_workbench.data_lake.catalog import CatalogConfig, CatalogDB, CatalogIndexer, SQLiteCatalogProvider


async def run(workspace: Path, discovery: str) -> None:
    data = workspace / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "weather.csv").write_text("day,temperature_c\n2026-01-01,7\n", encoding="utf-8")
    source: dict[str, object] = {
        "source_id": "synthetic-weather",
        "path": str(data),
        "discovery": discovery,
        "domain": "weather",
    }
    if discovery == "manifest":
        source["manifest"] = "approved.manifest.json"
        (data / "approved.manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "generation": 1,
                    "artifacts": [
                        {
                            "path": "weather.csv",
                            "artifact_id": "approved-weather",
                            "description": "Synthetic approved weather observations",
                            "domain": "weather",
                            "media_type": "text/csv",
                        }
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    config_path = workspace / "catalog.yaml"
    config_path.write_text(yaml.safe_dump({"version": 1, "sources": [source]}, sort_keys=False), encoding="utf-8")

    config = CatalogConfig.from_yaml(config_path)
    database = CatalogDB(workspace / "catalog.db")
    database.open()
    try:
        await CatalogIndexer(config, database).index()
        provider = SQLiteCatalogProvider(database, ("synthetic-weather",), hybrid_alpha=1.0)
        listed = await provider.list(ListRequest(page=PageRequest(limit=10)), RequestContext(caller_id="quickstart"))
        found = await provider.search(
            SearchRequest("weather", page=PageRequest(limit=10)),
            RequestContext(caller_id="quickstart"),
        )
        print(
            json.dumps(
                {
                    "discovery": discovery,
                    "listed": [artifact.presentation.name for artifact in listed.items],
                    "search_matches": [artifact.reference.artifact_id for artifact in found.items],
                },
                sort_keys=True,
            )
        )
    finally:
        database.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--discovery", choices=("scan", "manifest"), default="scan")
    args = parser.parse_args()
    asyncio.run(run(args.workspace, args.discovery))


if __name__ == "__main__":
    main()
