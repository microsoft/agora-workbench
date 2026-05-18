"""Data catalog API — browse and search data lake assets from the GUI.

Exposes ``GET /api/data-catalog`` which performs a hybrid search against
the same Azure AI Search artifact registry used by the agent's
``search_data_lake_catalog`` tool.  The endpoint does **not** require
user authentication — it uses ``get_search_credential_async()``, which
provides either an API key credential or a chained Azure credential for
local-dev (Azure CLI login) and deployed environments (managed identity).
"""

import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

LOGGER = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class CatalogAsset(BaseModel):
    """Minimal representation of a data lake asset for the GUI catalog."""

    name: str
    description: str
    asset_tag: str
    domain: str
    artifact_type: str


class CatalogResponse(BaseModel):
    assets: list[CatalogAsset]
    configured: bool


class CatalogDomainsResponse(BaseModel):
    domains: list[str]
    configured: bool


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/api/data-catalog", response_model=CatalogResponse)
async def search_data_catalog(
    q: str = Query(default="", description="Search query (empty = browse all)"),
    domain: Optional[str] = Query(default=None, description="Filter by domain"),
    top: int = Query(default=30, ge=1, le=100, description="Max results"),
    skip: int = Query(default=0, ge=0, description="Number of results to skip (for pagination)"),
):
    """Search the data lake catalog for available datasets.

    Returns a lightweight list suitable for display in the GUI sidebar.
    Falls back gracefully when the data lake is not configured.
    """
    endpoint = os.getenv("DATA_LAKE_SEARCH_ENDPOINT")
    index_name = os.getenv("DATA_LAKE_CATALOG_INDEX_NAME", "artifact-registry")

    if not endpoint:
        LOGGER.debug("Data lake not configured — returning empty catalog")
        return CatalogResponse(assets=[], configured=False)

    try:
        from azure.search.documents.aio import SearchClient

        from utilities.auth import get_search_credential_async

        credential = get_search_credential_async()
        client = SearchClient(
            endpoint=endpoint,
            index_name=index_name,
            credential=credential,
        )

        try:
            # Build optional OData filter
            filter_expr = None
            if domain:
                safe_domain = domain.replace("'", "''")
                filter_expr = f"domain eq '{safe_domain}'"

            search_text = q if q.strip() else "*"

            try:
                # Try semantic search first (best relevance)
                results = await client.search(
                    search_text=search_text,
                    filter=filter_expr,
                    select=[
                        "artifact_id",
                        "artifact_type",
                        "name",
                        "description",
                        "semantic_dataset_description",
                        "domain",
                    ],
                    top=top,
                    skip=skip,
                    query_type="semantic",
                    semantic_configuration_name="default-semantic-config",
                )
            except Exception:
                # Fall back to plain text search
                LOGGER.debug("Semantic search unavailable, falling back to text search")
                results = await client.search(
                    search_text=search_text,
                    filter=filter_expr,
                    select=[
                        "artifact_id",
                        "artifact_type",
                        "name",
                        "description",
                        "semantic_dataset_description",
                        "domain",
                    ],
                    top=top,
                    skip=skip,
                )

            assets: list[CatalogAsset] = []
            async for doc in results:
                artifact_type = doc.get("artifact_type", "")
                artifact_id = doc.get("artifact_id", "")
                name = doc.get("name", artifact_id)
                description = doc.get("semantic_dataset_description") or doc.get("description") or ""
                asset_tag = (
                    f"<{artifact_type}>{artifact_id}</{artifact_type}>"
                    if artifact_type and artifact_id
                    else artifact_id
                )
                assets.append(
                    CatalogAsset(
                        name=name,
                        description=description,
                        asset_tag=asset_tag,
                        domain=doc.get("domain", ""),
                        artifact_type=artifact_type,
                    )
                )
        finally:
            await client.close()
            await credential.close()

        LOGGER.info("Data catalog: returned %d assets for query=%r domain=%r skip=%d", len(assets), q, domain, skip)
        return CatalogResponse(assets=assets, configured=True)

    except Exception as e:
        LOGGER.error("Data catalog search failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Data catalog search failed")


@router.get("/api/data-catalog/domains", response_model=CatalogDomainsResponse)
async def list_catalog_domains():
    """Return all distinct domain values in the data lake catalog."""
    endpoint = os.getenv("DATA_LAKE_SEARCH_ENDPOINT")
    index_name = os.getenv("DATA_LAKE_CATALOG_INDEX_NAME", "artifact-registry")

    if not endpoint:
        return CatalogDomainsResponse(domains=[], configured=False)

    try:
        from azure.search.documents.aio import SearchClient

        from utilities.auth import get_search_credential_async

        credential = get_search_credential_async()
        client = SearchClient(
            endpoint=endpoint,
            index_name=index_name,
            credential=credential,
        )

        try:
            # Use facets to get distinct domain values without loading documents
            results = await client.search(
                search_text="*",
                facets=["domain,count:100"],
                top=0,
            )
            # Consume results iterator (needed to populate facets)
            async for _ in results:
                pass
            facets = results.get_facets() or {}
            domain_facets = facets.get("domain", [])
            domains = sorted(f["value"] for f in domain_facets if f.get("value"))
        finally:
            await client.close()
            await credential.close()

        return CatalogDomainsResponse(domains=domains, configured=True)

    except Exception as e:
        LOGGER.error("Data catalog domain listing failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Data catalog domain listing failed")
