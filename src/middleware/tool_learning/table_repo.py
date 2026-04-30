"""
Azure Table Storage repository for tool-learning memory vignettes.

Provides upsert and retrieval operations for Vignette entities.
The table schema stores flattened filter fields plus a full JSON payload for round-trips.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableServiceClient, TableClient

from .config import ToolLearningConfig
from .models import Vignette

LOGGER = logging.getLogger(__name__)


def _escape_odata(value: str) -> str:
    """Escape a string value for safe inclusion in an OData filter literal."""
    return value.replace("'", "''")


def _build_partition_key(tenant_id: Optional[str], tool_name: str) -> str:
    """Build the PartitionKey for a vignette entity."""
    return f"{tenant_id or 'global'}|{tool_name}"


def _build_row_key(scope: str, kind: str, error_class: Optional[str], vignette_id: str) -> str:
    """Build the RowKey for a vignette entity."""
    return f"{scope}|{kind}|{error_class or 'none'}|{vignette_id}"


def _vignette_to_entity(vignette: Vignette) -> dict:
    """Serialize a Vignette to an Azure Table entity dict."""
    return {
        "PartitionKey": _build_partition_key(vignette.tenant_id, vignette.tool.tool_name),
        "RowKey": _build_row_key(vignette.scope, vignette.kind, vignette.match.error_class, vignette.vignette_id),
        "vignette_id": vignette.vignette_id,
        "kind": vignette.kind,
        "scope": vignette.scope,
        "tenant_id": vignette.tenant_id or "",
        "user_id": vignette.user_id or "",
        "tool_name": vignette.tool.tool_name,
        "tool_version": vignette.tool.tool_version or "",
        "error_class": vignette.match.error_class or "",
        "confidence": vignette.confidence,
        "title": vignette.title,
        "summary": vignette.summary,
        "tags_json": json.dumps(vignette.tags),
        "updated_at": vignette.updated_at.isoformat(),
        "payload_json": vignette.model_dump_json(),
    }


def _entity_to_vignette(entity: dict) -> Vignette:
    """Deserialize an Azure Table entity back to a Vignette."""
    return Vignette.model_validate_json(entity["payload_json"])


class TableVignetteRepo:
    """
    Azure Table Storage repository for Vignette entities.

    Supports upsert (create or update) and retrieval by tool name.
    Uses the caller-supplied TokenCredential for authentication.
    """

    def __init__(
        self,
        config: ToolLearningConfig,
        credential: Optional[TokenCredential] = None,
    ) -> None:
        """
        Initialize the repository.

        Args:
            config: Tool-learning configuration.
            credential: Azure TokenCredential (e.g. DefaultAzureCredential).
        """
        self._config = config
        self._client = self._create_client(credential)

    def _create_client(self, credential: Optional[TokenCredential]) -> TableClient:
        if not self._config.table_storage_endpoint:
            raise ValueError("TOOL_LEARNING_TABLE_ENDPOINT must be set.")
        if credential is None:
            raise ValueError("A TokenCredential must be provided for Table Storage access.")
        service = TableServiceClient(
            endpoint=self._config.table_storage_endpoint,
            credential=credential,
        )
        return service.get_table_client(self._config.table_name)

    def upsert_vignette(self, vignette: Vignette) -> None:
        """
        Upsert a vignette into the table.

        If the vignette_id already exists, the confidence is bumped (capped at 1.0)
        and tags are merged before upserting.

        Args:
            vignette: The vignette to upsert.
        """
        row_key = _build_row_key(vignette.scope, vignette.kind, vignette.match.error_class, vignette.vignette_id)
        partition_key = _build_partition_key(vignette.tenant_id, vignette.tool.tool_name)

        # Check if it already exists and merge confidence + tags
        try:
            existing_entity = self._client.get_entity(partition_key=partition_key, row_key=row_key)
            existing = _entity_to_vignette(existing_entity)
            merged_tags = sorted(set(existing.tags) | set(vignette.tags))
            new_confidence = min(existing.confidence + 0.05, 1.0)
            vignette = vignette.model_copy(
                update={
                    "confidence": new_confidence,
                    "tags": merged_tags,
                    "created_at": existing.created_at,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
        except ResourceNotFoundError:
            # Entity does not exist — proceed with upsert as-is
            pass

        entity = _vignette_to_entity(vignette)
        self._client.upsert_entity(entity=entity)
        LOGGER.info("Upserted vignette %s (%s)", vignette.vignette_id, vignette.kind)

    def get_vignettes_for_tool(
        self,
        tool_name: str,
        tenant_id: Optional[str] = None,
        kind: Optional[str] = None,
        error_class: Optional[str] = None,
        max_results: int = 20,
    ) -> List[Vignette]:
        """
        Retrieve vignettes for a specific tool from the table.

        Args:
            tool_name: Name of the tool.
            tenant_id: Optional tenant ID to scope the query.
            kind: Optional vignette kind filter ("anti_pattern" or "repair_template").
            error_class: Optional error class filter.
            max_results: Maximum number of results to return.

        Returns:
            List of Vignette objects sorted by confidence descending.
        """
        partition_key = _build_partition_key(tenant_id, tool_name)
        escaped_pk = _escape_odata(partition_key)
        filters = [f"PartitionKey eq '{escaped_pk}'"]
        if kind:
            filters.append(f"kind eq '{_escape_odata(kind)}'")
        if error_class:
            filters.append(f"error_class eq '{_escape_odata(error_class)}'")

        query_filter = " and ".join(filters)

        vignettes: List[Vignette] = []
        try:
            entities = self._client.query_entities(
                query_filter=query_filter,
                results_per_page=max_results,
            )
            for entity in entities:
                try:
                    vignettes.append(_entity_to_vignette(entity))
                except Exception as e:
                    LOGGER.warning("Skipping malformed vignette entity: %s", e)
                if len(vignettes) >= max_results:
                    break
        except Exception as e:
            LOGGER.error("Failed to query vignettes from table: %s", e)

        vignettes.sort(key=lambda v: v.confidence, reverse=True)
        return vignettes
