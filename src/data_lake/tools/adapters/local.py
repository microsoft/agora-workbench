"""Local file-backed DataLake search backend."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from data_lake.search.registry import ArtifactRegistryDocument
from utilities.bm25 import BM25Index

from .maf import DataLakeSearchBackend, DataLakeSearchParams, format_asset_tag

LOGGER = logging.getLogger(__name__)


def _catalog_doc_text(doc: dict[str, Any]) -> str:
    """Derive the indexable text for a catalog artifact document."""
    tags = doc.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    return (
        f"{doc.get('name', '')} {doc.get('description', '')} "
        f"{doc.get('domain', '')} {' '.join(str(tag) for tag in tags)}"
    )


def _build_catalog_index(docs: list[dict[str, Any]]) -> BM25Index[dict[str, Any]]:
    """Build a BM25 index over local catalog artifact documents."""
    index: BM25Index[dict[str, Any]] = BM25Index()
    for doc in docs:
        index.add(doc, _catalog_doc_text(doc))
    return index



class LocalDataLakeSearchBackend(DataLakeSearchBackend):
    """Local YAML-catalog implementation of :class:`DataLakeSearchBackend`."""

    def __init__(self, catalog_path: str):
        self._catalog_path = Path(catalog_path)
        if not self._catalog_path.exists():
            raise FileNotFoundError(f"Local DataLake catalog file not found: {self._catalog_path}")

        raw = yaml.safe_load(self._catalog_path.read_text(encoding="utf-8")) or {}
        artifacts = raw.get("artifacts") or []
        if not isinstance(artifacts, list):
            raise ValueError("Local DataLake catalog must define a top-level 'artifacts' list.")

        self._catalog_docs: list[dict[str, Any]] = []
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                LOGGER.warning("Skipping non-object artifact entry in local catalog: %r", artifact)
                continue

            validated = ArtifactRegistryDocument.model_validate(artifact).model_dump(mode="json", by_alias=True)
            catalog_doc = dict(validated)
            # Keep optional free-form tags for BM25 indexing only (not part of registry schema/output).
            if "tags" in artifact:
                catalog_doc["tags"] = artifact.get("tags")
            self._catalog_docs.append(catalog_doc)

        self._index = _build_catalog_index(self._catalog_docs)

    def get_catalog_docs(self) -> list[dict[str, Any]]:
        """Return a copy of loaded catalog documents."""
        return [dict(doc) for doc in self._catalog_docs]

    @property
    def available_domains(self) -> list[str]:
        domains = {str(doc.get("domain")) for doc in self._catalog_docs if doc.get("domain")}
        return sorted(domains)

    async def search(self, params: DataLakeSearchParams) -> list[dict]:
        scored = self._index.search(params.query, top_k=len(self._index))
        filtered = [dict(doc) for doc, _ in scored if self._matches_filters(doc, params)]

        if params.order_by:
            filtered = self._apply_order_by(filtered, params.order_by)

        selected = [self._project_fields(asset, params.select_fields) for asset in filtered]
        return selected[: params.top]

    @staticmethod
    def _matches_filters(asset: dict[str, Any], params: DataLakeSearchParams) -> bool:
        if params.artifact_types and asset.get("artifact_type") not in set(params.artifact_types):
            return False
        if params.domains and asset.get("domain") not in set(params.domains):
            return False
        if params.sources and asset.get("source") not in set(params.sources):
            return False
        return True

    @staticmethod
    def _project_fields(asset: dict[str, Any], select_fields: list[str] | None) -> dict[str, Any]:
        asset_tag = format_asset_tag(asset)
        if not select_fields:
            projected = {k: v for k, v in asset.items() if k != "tags"}
        else:
            projected = {k: asset.get(k) for k in select_fields if k != "asset_tag"}
        if asset_tag:
            projected["asset_tag"] = asset_tag
        return projected

    @staticmethod
    def _apply_order_by(assets: list[dict[str, Any]], order_by: list[str]) -> list[dict[str, Any]]:
        def sort_value(asset: dict[str, Any], field: str) -> tuple[bool, str]:
            value = asset.get(field)
            return (value is None, str(value or "").lower())

        sorted_assets = list(assets)
        criteria: list[tuple[str, bool]] = []
        for clause in order_by:
            parts = clause.strip().split()
            if not parts:
                continue
            field = parts[0]
            descending = len(parts) > 1 and parts[1].lower() == "desc"
            criteria.append((field, descending))

        for field, descending in reversed(criteria):
            sorted_assets.sort(
                key=lambda asset, order_field=field: sort_value(asset, order_field),
                reverse=descending,
            )
        return sorted_assets


def discover_local_catalog_domains(catalog_path: str) -> list[str]:
    """Read unique domains from a local YAML catalog."""
    catalog = Path(catalog_path)
    if not catalog.exists():
        raise FileNotFoundError(f"Local DataLake catalog file not found: {catalog}")

    raw = yaml.safe_load(catalog.read_text(encoding="utf-8")) or {}
    artifacts = raw.get("artifacts") or []
    if not isinstance(artifacts, list):
        raise ValueError("Local DataLake catalog must define a top-level 'artifacts' list.")

    domains = {str(artifact.get("domain")) for artifact in artifacts if isinstance(artifact, dict) and artifact.get("domain")}
    return sorted(domains)
