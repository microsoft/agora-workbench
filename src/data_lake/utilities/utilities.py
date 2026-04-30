"""
Standalone helpers for managing Purview catalog entities.

These utilities do not require an ingestion manifest and can be used
independently via the CLI or imported directly.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from azure.identity import AzureCliCredential
from azure.purview.catalog import PurviewCatalogClient
from azure.search.documents import SearchClient

logger = logging.getLogger(__name__)


def move_entities(
    purview_account: str,
    storage_account: str,
    container: str,
    path_prefixes: List[str],
    target_collection: str,
    *,
    dry_run: bool = False,
) -> None:
    """Discover Purview entities by path prefix and move them to *target_collection*.

    Args:
        purview_account: Purview account name (e.g. ``"agora-purview"``).
        storage_account: Azure Storage account name.
        container: Blob container name.
        path_prefixes: Subfolder prefixes relative to the container root.
        target_collection: Target Purview collection to move entities into.
        dry_run: If True, only log what would happen.
    """
    from data_lake.semantic import PurviewDataSourceManager

    credential = AzureCliCredential()
    endpoint = f"https://{purview_account}.purview.azure.com"
    client = PurviewCatalogClient(endpoint=endpoint, credential=credential)

    # Ensure target collection exists (auto-create if needed)
    mgr = PurviewDataSourceManager(purview_account)
    mgr.ensure_collection_exists(target_collection)

    base = f"https://{storage_account}.blob.core.windows.net/{container}"

    all_guids: List[str] = []

    for prefix in path_prefixes:
        prefix = prefix.strip("/")
        search_prefix = f"{base}/{prefix}"
        logger.info(f"Searching for entities under: {search_prefix}")

        # Purview discovery query — search with wildcard, filter by qualifiedName prefix
        guids_for_prefix: List[str] = []
        offset = 0
        limit = 100

        while True:
            body = {
                "keywords": "*",
                "filter": {
                    "and": [
                        {
                            "attributeName": "qualifiedName",
                            "operator": "startswith",
                            "attributeValue": search_prefix,
                        }
                    ]
                },
                "limit": limit,
                "offset": offset,
            }
            result = client.discovery.query(search_request=body)
            values = result.get("value", [])

            if not values:
                break

            for entity in values:
                guid = entity.get("id")
                qn = entity.get("qualifiedName", "")
                name = entity.get("name", "")
                if guid:
                    guids_for_prefix.append(guid)
                    logger.debug(f"  Found: {name} ({qn})")

            # Check if there are more pages
            if len(values) < limit:
                break
            offset += limit

        logger.info(f"  Found {len(guids_for_prefix)} entities under '{prefix}'")
        all_guids.extend(guids_for_prefix)

    if not all_guids:
        logger.warning("No entities found matching the given prefixes – nothing to move.")
        return

    # Deduplicate (an entity could theoretically match multiple prefixes)
    all_guids = list(dict.fromkeys(all_guids))

    logger.info(f"Moving {len(all_guids)} entities to collection '{target_collection}'")

    if dry_run:
        logger.info(f"[DRY RUN] Would move {len(all_guids)} entities")
        for g in all_guids[:10]:
            logger.info(f"  [DRY RUN] guid={g}")
        if len(all_guids) > 10:
            logger.info(f"  [DRY RUN] … and {len(all_guids) - 10} more")
        return

    # Move in batches of 100 (API safety)
    batch_size = 100
    for i in range(0, len(all_guids), batch_size):
        batch = all_guids[i : i + batch_size]
        body = {"entityGuids": batch}
        client.collection.move_entities_to_collection(
            collection=target_collection,
            move_entities_request=body,
        )
        logger.info(f"  ✓ Moved batch {i // batch_size + 1} ({len(batch)} entities)")

    logger.info(f"✓ Move complete – {len(all_guids)} entities now in '{target_collection}'")


def update_purview_entity(
    purview_account: str,
    qualified_name: str,
    *,
    new_name: Optional[str] = None,
    new_description: Optional[str] = None,
    dry_run: bool = False,
) -> None:
    """Update the name and/or description of a Purview entity by qualified name.

    Fetches the entity from Purview by its qualified name (blob URL), then
    updates the ``name`` and/or ``userDescription`` attributes in place.
    At least one of *new_name* or *new_description* must be provided.

    Supports both ``azure_blob_path`` and ``azure_blob_container`` entity
    types — directory paths (ending with ``/``) are tried as
    ``azure_blob_container`` first, then ``azure_blob_path``.

    Args:
        purview_account: Purview account name (e.g. ``"agora-purview"``).
        qualified_name: Full qualified name of the entity (blob URL).
        new_name: New display name to set.  Pass ``None`` to leave unchanged.
        new_description: New user description to set.  Pass ``None`` to leave
            unchanged.
        dry_run: If True, only log what would happen without making changes.

    Raises:
        ValueError: If neither *new_name* nor *new_description* is provided,
            or if the entity cannot be found in Purview.
    """
    if new_name is None and new_description is None:
        raise ValueError("At least one of new_name or new_description must be provided")

    from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

    credential = AzureCliCredential()
    endpoint = f"https://{purview_account}.purview.azure.com"
    client = PurviewCatalogClient(endpoint=endpoint, credential=credential)

    # Determine which entity types to try
    if qualified_name.endswith("/"):
        type_names = ["azure_blob_container", "azure_blob_path"]
    else:
        type_names = ["azure_blob_path"]

    entity_result = None
    matched_type = None
    for type_name in type_names:
        effective_qn = qualified_name.rstrip("/") if type_name == "azure_blob_container" else qualified_name
        try:
            entity_result = client.entity.get_by_unique_attributes(
                type_name=type_name,
                attr_qualified_name=effective_qn,
            )
            matched_type = type_name
            break
        except (ResourceNotFoundError, HttpResponseError):
            continue

    if not entity_result:
        raise ValueError(
            f"Entity not found in Purview for qualified name: {qualified_name}\n"
            "Ensure the Purview scan has cataloged this path."
        )

    entity_data = entity_result.get("entity", {})
    guid = entity_data.get("guid")
    attributes = entity_data.get("attributes", {})
    current_name = attributes.get("name", "")

    if not guid:
        raise ValueError(f"Entity found but missing GUID: {qualified_name}")

    effective_qn = qualified_name.rstrip("/") if matched_type == "azure_blob_container" else qualified_name
    updated_name = new_name if new_name is not None else current_name
    updated_description = new_description if new_description is not None else attributes.get("userDescription")

    logger.info(f"Updating entity: {qualified_name} (guid={guid}, type={matched_type})")
    if new_name is not None:
        logger.info(f"  name: {current_name!r} → {new_name!r}")
    if new_description is not None:
        current_description = attributes.get("userDescription")
        logger.info(f"  userDescription: {current_description!r} → {new_description!r}")

    if dry_run:
        logger.info("[DRY RUN] No changes made")
        return

    body: Dict[str, Any] = {
        "entity": {
            "guid": guid,
            "typeName": matched_type,
            "attributes": {
                "qualifiedName": effective_qn,
                "name": updated_name,
                "userDescription": updated_description,
            },
        }
    }
    client.entity.create_or_update(entity=body)
    logger.info(f"✓ Entity updated: {qualified_name}")


def list_artifact_registry(
    search_service: str,
    *,
    index_name: str = "artifact-registry",
    filter_expression: Optional[str] = None,
    top: Optional[int] = None,
    select_fields: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """List artifacts in the artifact registry Azure AI Search index.

    Queries the artifact registry index and returns matching documents.
    Useful for auditing what is currently indexed and comparing against
    blob storage or Purview.

    Args:
        search_service: Azure AI Search service name (e.g. ``"agora-search"``).
        index_name: Name of the artifact registry index
            (default: ``"artifact-registry"``).
        filter_expression: Optional OData filter expression to restrict results
            (e.g. ``"domain eq 'energy'"``, ``"artifact_type eq 'blob'"``).
        top: Maximum number of results to return.  ``None`` (the default)
            fetches all matching documents.
        select_fields: Optional list of fields to return.  When ``None`` all
            available fields are returned.

    Returns:
        List of artifact registry document dicts.

    Raises:
        azure.core.exceptions.HttpResponseError: If the search service returns
            an error (e.g. index does not exist, invalid filter expression).
        azure.core.exceptions.ServiceRequestError: If the search service
            cannot be reached (DNS/network failure).
    """
    credential = AzureCliCredential()
    endpoint = f"https://{search_service}.search.windows.net"

    client = SearchClient(
        endpoint=endpoint,
        index_name=index_name,
        credential=credential,
    )

    search_kwargs: Dict[str, Any] = {
        "search_text": "*",
    }
    if top is not None:
        search_kwargs["top"] = top
    if filter_expression:
        search_kwargs["filter"] = filter_expression
    if select_fields:
        search_kwargs["select"] = select_fields

    results = client.search(**search_kwargs)
    artifacts = [dict(r) for r in results]
    logger.info(f"Retrieved {len(artifacts)} artifacts from '{index_name}' in {search_service}")
    return artifacts
