"""Local file-backed DataLake search backend."""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import yaml

from data_lake.search.registry import ArtifactRegistryDocument
from tools.search.bm25_tool_search import _tokenize

from .maf import DataLakeSearchBackend, DataLakeSearchParams

LOGGER = logging.getLogger(__name__)


def _asset_tag(asset: dict[str, Any]) -> str | None:
    artifact_type = asset.get("artifact_type")
    artifact_id = asset.get("artifact_id")
    if artifact_type and artifact_id:
        return f"<{artifact_type}>{artifact_id}</{artifact_type}>"
    return None


class _CatalogBM25Index:
    """Small BM25 index for local catalog artifacts."""

    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = docs
        self._tokens_by_doc: list[list[str]] = []
        self._df: dict[str, int] = {}
        self._avgdl = 0.0

        for doc in docs:
            tags = doc.get("tags") or []
            if not isinstance(tags, list):
                tags = []
            text = (
                f"{doc.get('name', '')} {doc.get('description', '')} "
                f"{doc.get('domain', '')} {' '.join(str(tag) for tag in tags)}"
            )
            tokens = _tokenize(text)
            self._tokens_by_doc.append(tokens)
            seen: set[str] = set()
            for token in tokens:
                if token not in seen:
                    self._df[token] = self._df.get(token, 0) + 1
                    seen.add(token)

        if self._tokens_by_doc:
            self._avgdl = sum(len(tokens) for tokens in self._tokens_by_doc) / len(self._tokens_by_doc)

    def search(self, query: str) -> list[tuple[dict[str, Any], float]]:
        if not self._docs:
            return []
        query_tokens = _tokenize(query)

        n = len(self._docs)
        scored: list[tuple[dict[str, Any], float]] = []
        for doc, doc_tokens in zip(self._docs, self._tokens_by_doc):
            if not query_tokens:
                scored.append((doc, 0.0))
                continue

            tf_map: dict[str, int] = {}
            for token in doc_tokens:
                tf_map[token] = tf_map.get(token, 0) + 1

            dl = len(doc_tokens)
            score = 0.0
            for query_token in query_tokens:
                df = self._df.get(query_token)
                if not df:
                    continue
                tf = tf_map.get(query_token, 0)
                if tf == 0:
                    continue

                idf = math.log((n - df + 0.5) / (df + 0.5) + 1.0)
                if self._avgdl == 0:
                    tf_norm = (tf * 2.5) / (tf + 1.5)
                else:
                    tf_norm = (tf * 2.5) / (tf + 1.5 * (1 - 0.75 + 0.75 * dl / self._avgdl))
                score += idf * tf_norm

            scored.append((doc, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored


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
            if "tags" in artifact:
                catalog_doc["tags"] = artifact.get("tags")
            self._catalog_docs.append(catalog_doc)

        self._index = _CatalogBM25Index(self._catalog_docs)

    @property
    def available_domains(self) -> list[str]:
        domains = {str(doc.get("domain")) for doc in self._catalog_docs if doc.get("domain")}
        return sorted(domains)

    async def search(self, params: DataLakeSearchParams) -> list[dict]:
        scored = self._index.search(params.query)
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
        asset_tag = _asset_tag(asset)
        if not select_fields:
            projected = {k: v for k, v in asset.items() if k != "tags"}
        else:
            projected = {k: asset.get(k) for k in select_fields if k != "asset_tag"}
        if asset_tag:
            projected["asset_tag"] = asset_tag
        return projected

    @staticmethod
    def _apply_order_by(assets: list[dict[str, Any]], order_by: list[str]) -> list[dict[str, Any]]:
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
                key=lambda asset: (asset.get(field) is None, str(asset.get(field, "")).lower()),
                reverse=descending,
            )
        return sorted_assets


def discover_local_catalog_domains(catalog_path: str) -> list[str]:
    """Read unique domains from a local YAML catalog."""
    backend = LocalDataLakeSearchBackend(catalog_path=catalog_path)
    return backend.available_domains

