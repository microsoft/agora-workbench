"""Authorized local managed-write and promotion example."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from agora_workbench.data_lake import (
    AuthorizedManagedCatalogWriter,
    CatalogOperation,
    LocalManagedStorage,
    ManagedCatalogWriter,
    PromoteOutputRequest,
    RequestContext,
)


class ExampleAuthorizer:
    """Permit writes only for the explicit quickstart operator."""

    async def authorize(self, request, context) -> bool:
        return context.caller_id == "quickstart-operator" and request.operation in {
            CatalogOperation.PROMOTE,
        }


async def run(workspace: Path) -> None:
    scratch = workspace / "scratch" / "result.csv"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text("metric,value\nsynthetic,42\n", encoding="utf-8")
    writer = AuthorizedManagedCatalogWriter(
        ManagedCatalogWriter("approved-outputs", LocalManagedStorage(workspace / "lake")),
        ExampleAuthorizer(),
    )
    result = await writer.promote(
        PromoteOutputRequest(
            operation_id="quickstart-promotion",
            path="approved/result.csv",
            local_path=scratch,
            session_id="quickstart-session",
            output_name="result.csv",
        ),
        RequestContext(caller_id="quickstart-operator"),
    )
    print(json.dumps({"generation": result.generation, "artifact_id": result.artifact_id}, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    asyncio.run(run(parser.parse_args().workspace))


if __name__ == "__main__":
    main()
