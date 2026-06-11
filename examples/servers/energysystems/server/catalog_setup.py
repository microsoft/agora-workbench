"""Local data-catalog wiring for the energysystems server.

Indexes the synthetic grid datasets under ``data/`` into a SQLite catalog and
registers the catalog search tools (``search_data`` / ``query_catalog`` /
``get_artifact`` / ``list_domains``) on the MCP server.

The catalog runs keyword-only (SQLite FTS5 / BM25) — no embedding model, no
Azure, no extra setup. ``catalog.yaml`` sets ``embedding_model: none``; switch
it to ``azure-openai`` (with an endpoint) there if semantic search is wanted.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Optional

from agora_workbench.code_execution.catalog_tools import CatalogToolsContext, register_catalog_tools
from agora_workbench.code_execution.data_access.catalog import CatalogConfig, CatalogDB
from agora_workbench.code_execution.data_access.catalog.indexer import CatalogIndexer

LOGGER = logging.getLogger(__name__)

# Keep references to opened catalog DBs so their SQLite connections are not
# garbage-collected for the lifetime of the process.
_OPEN_CATALOGS: list = []


def _resolve_local_sources(config: CatalogConfig, base_dir: Path) -> None:
    """Rewrite relative local source paths to absolute, based on *base_dir*."""
    for source in config.sources:
        path = Path(source.path)
        if source.source_type == "local" and not path.is_absolute():
            source.path = str((base_dir / path).resolve())


def setup_catalog(server, energysystems_dir: Path) -> Optional[CatalogDB]:
    """Build the local catalog and register catalog tools on ``server``.

    Returns the open :class:`CatalogDB` (kept alive in a module-level list), or
    ``None`` when no ``catalog.yaml`` is present. The index is keyword-only
    (BM25) unless ``catalog.yaml`` configures an embedding model.
    """
    catalog_yaml = energysystems_dir / "catalog.yaml"
    if not catalog_yaml.exists():
        LOGGER.warning("No catalog.yaml at %s — skipping catalog setup.", catalog_yaml)
        return None

    config = CatalogConfig.from_yaml(catalog_yaml)
    _resolve_local_sources(config, energysystems_dir)

    # File-backed DB so the read-only query_catalog connection sees indexed rows.
    db_path = Path(tempfile.mkdtemp(prefix="energysystems_catalog_")) / "catalog.db"
    db = CatalogDB(db_path=str(db_path))
    db.open()

    # No embedding_provider passed → the indexer resolves it from catalog.yaml
    # (embedding_model: none → keyword-only / BM25).
    count = asyncio.run(CatalogIndexer(config=config, db=db).index())
    LOGGER.info("energysystems catalog: indexed %d artifact(s) at %s", count, db_path)

    ctx = CatalogToolsContext(db=db, embedding_provider=None, config=config)
    register_catalog_tools(server.mcp, ctx, activity_publisher=getattr(server, "activity_publisher", None))
    _OPEN_CATALOGS.append(db)
    return db
