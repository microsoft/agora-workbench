"""MCP tools for server-side catalog search."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

from .data_access.catalog import CatalogConfig, CatalogDB
from .data_access.catalog.embeddings import EmbeddingProvider

LOGGER = logging.getLogger(__name__)


@dataclass
class CatalogToolsContext:
    """Holds the initialized catalog state for tool handlers."""

    db: CatalogDB
    embedding_provider: EmbeddingProvider
    config: CatalogConfig


def register_catalog_tools(mcp: "FastMCP", ctx: CatalogToolsContext) -> None:
    """Register catalog search tools on a FastMCP server instance."""

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
        # Compute query embedding
        query_embedding: Optional[list[float]] = None
        if query.strip():
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
        return [r.to_dict() for r in results]

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
        return record.to_dict()

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
            "Search the data catalog for files and datasets matching a natural language query. "
            "Supports filtering by domain and storage type. Returns ranked results with metadata."
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
        description="Get detailed metadata for a specific data artifact by its ID.",
    )(get_artifact)

    mcp.tool(
        name="list_domains",
        description="List all available data domains in the catalog (e.g., 'earthscience', 'powergrid').",
    )(list_domains)

    LOGGER.info("Registered catalog tools: search_data, query_catalog, get_artifact, list_domains")
