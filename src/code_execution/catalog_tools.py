"""MCP tools for server-side catalog search."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

from .data_access.catalog import CatalogConfig, CatalogDB
from .data_access.catalog.embeddings import EmbeddingProvider

LOGGER = logging.getLogger(__name__)


@dataclass
class CatalogToolsContext:
    """Holds the initialized catalog state for tool handlers."""

    db: CatalogDB
    embedding_provider: Optional[EmbeddingProvider]
    config: CatalogConfig


def register_catalog_tools(mcp: "FastMCP", ctx: CatalogToolsContext, activity_publisher: Any = None) -> None:
    """Register catalog search tools on a FastMCP server instance.

    When *activity_publisher* is provided, ``search_data`` emits a
    ``data_searched`` activity event so the search step shows up in the GUI.
    """

    async def search_data(
        query: str,
        domain: Optional[str] = None,
        source_type: Optional[str] = None,
        top: int = 10,
    ) -> list[dict]:
        """Search the data catalog for artifacts matching a query.

        Uses hybrid keyword + vector search to find relevant data files.

        Args:
            query: Natural language search query.
            domain: Optional domain filter (e.g., 'earthscience', 'powergrid').
            source_type: Optional storage type filter ('local' or 'blob').
            top: Maximum number of results to return (default 10).

        Returns:
            List of matching artifacts with metadata and relevance scores.
        """
        # Compute query embedding (skipped for keyword-only / BM25 catalogs)
        query_embedding: Optional[list[float]] = None
        if query.strip() and ctx.embedding_provider is not None:
            embeddings = await ctx.embedding_provider.embed([query])
            query_embedding = embeddings[0]

        results = ctx.db.search(
            query=query,
            query_embedding=query_embedding,
            domain=domain,
            source_type=source_type,
            top=top,
            hybrid_alpha=ctx.config.search.hybrid_alpha,
        )
        hits = [r.to_dict() for r in results]
        for hit in hits:
            uri = hit.get("storage_uri")
            if uri:
                # Ready-to-use reference: paste this into execute_*_code to load the
                # file. The platform fetches it into the sandbox and substitutes a
                # local Path. The raw storage_uri is server-side and not readable
                # from the sandbox (only /tmp is).
                hit["load_path"] = f"<local>{uri}</local>"
        if activity_publisher is not None:
            query_label = repr(query) if query else "'' (all)"
            activity_publisher.publish_nowait(
                {
                    "type": "data_searched",
                    "description": f"search data {query_label} → {len(hits)} dataset(s)",
                    "query": query,
                    "matched_artifacts": [h.get("name", "") for h in hits],
                    "success": True,
                }
            )
        return hits

    async def get_artifact(artifact_id: str) -> dict:
        """Get detailed metadata for a specific artifact by ID.

        Args:
            artifact_id: The unique identifier of the artifact.

        Returns:
            Full artifact metadata including storage URI, domain, and description.
        """
        record = ctx.db.get_artifact(artifact_id)
        if record is None:
            return {"error": f"Artifact not found: {artifact_id}"}
        data = record.to_dict()
        uri = data.get("storage_uri")
        if uri:
            data["load_path"] = f"<local>{uri}</local>"
        return data

    async def list_domains() -> list[str]:
        """List all available data domains in the catalog.

        Returns:
            Sorted list of unique domain names.
        """
        return ctx.db.list_domains()

    async def query_catalog(sql: str, max_rows: int = 100) -> list[dict] | dict:
        """Run a read-only SQL query against the data catalog database.

        Use this for structured filtering, aggregation, or exploration that
        goes beyond what search_data provides.

        Available tables:
          - artifacts (id, name, storage_uri, description, domain,
                       source_type, content_type, size_bytes, indexed_at)
          - artifacts_fts (FTS5 virtual table: name, description, domain)
            Usage: SELECT * FROM artifacts_fts WHERE artifacts_fts MATCH 'query'

        Args:
            sql: A SELECT query to execute. Write operations are rejected.
            max_rows: Maximum number of rows to return (default 100).

        Returns:
            List of result row dictionaries, or an error dict.
        """
        try:
            return ctx.db.execute_readonly(sql, max_rows=max_rows)
        except (ValueError, Exception) as e:
            return {"error": str(e)}

    mcp.tool(
        name="search_data",
        description=(
            "Search the data catalog for files and datasets matching a natural-language query. "
            "Each result includes a `load_path` — paste that exact string into your "
            "execute_*_code (e.g. `pypsa.Network(load_path)` or `pd.read_csv(load_path)`) to load "
            "the file: the platform fetches it into the sandbox and substitutes a local Path. "
            "Do NOT pass the raw `storage_uri` or the artifact `id` to open the file — they are "
            "server-side and not readable from the sandbox. Supports filtering by domain and storage type."
        ),
    )(search_data)

    mcp.tool(
        name="query_catalog",
        description=(
            "Run a read-only SQL query against the data catalog. "
            "Use for structured filtering (e.g., by content_type, size_bytes), "
            "aggregations, or exploration beyond natural language search. "
            "Table: artifacts (id, name, storage_uri, description, domain, "
            "source_type, content_type, size_bytes, indexed_at). "
            "FTS5 table: artifacts_fts (MATCH queries on name, description, domain)."
        ),
    )(query_catalog)

    mcp.tool(
        name="get_artifact",
        description=(
            "Get detailed metadata for a specific data artifact by its ID, including a "
            "`load_path` you paste into execute_*_code to load the file."
        ),
    )(get_artifact)

    mcp.tool(
        name="list_domains",
        description="List all available data domains in the catalog (e.g., 'earthscience', 'powergrid').",
    )(list_domains)

    LOGGER.info("Registered catalog tools: search_data, query_catalog, get_artifact, list_domains")
