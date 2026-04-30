"""CLI runner for standalone data-lake utility manifests."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict

from .manifest import UtilityManifest
from ..utilities.utilities import list_artifact_registry, update_purview_entity

logger = logging.getLogger(__name__)


def run_manifest(
    manifest: UtilityManifest,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Execute the operations described by a utility manifest."""
    effective_dry_run = dry_run
    results: Dict[str, Any] = {
        "dry_run": effective_dry_run,
        "artifact_registry_results": [],
        "updated_entities": 0,
    }

    if manifest.registry_query is not None:
        query = manifest.registry_query
        artifacts = list_artifact_registry(
            search_service=query.search_service,
            index_name=query.index_name,
            filter_expression=query.filter_expression,
            top=query.top,
            select_fields=query.select_fields,
        )
        results["artifact_registry_results"] = artifacts

        if query.output_path:
            output_path = Path(query.output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(artifacts, indent=2))
            logger.info(
                "Wrote %d artifact-registry results to %s",
                len(artifacts),
                output_path,
            )

    if manifest.entity_updates:
        assert manifest.purview_account is not None
        for entity_update in manifest.entity_updates:
            update_purview_entity(
                manifest.purview_account,
                entity_update.qualified_name,
                new_name=entity_update.new_name,
                new_description=entity_update.new_description,
                dry_run=effective_dry_run,
            )
        if effective_dry_run:
            results["would_update_entities"] = len(manifest.entity_updates)
        else:
            results["updated_entities"] = len(manifest.entity_updates)

    return results


def parse_args() -> argparse.Namespace:
    """Parse CLI args for the utility manifest runner."""
    parser = argparse.ArgumentParser(description="Run standalone data-lake utility operations from a YAML manifest.")
    parser.add_argument("manifest", help="Path to the utility manifest YAML file.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview Purview updates without applying them.",
    )
    return parser.parse_args()


def main() -> None:
    """CLI entry point."""
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    manifest = UtilityManifest.from_yaml(args.manifest)
    run_manifest(manifest, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
