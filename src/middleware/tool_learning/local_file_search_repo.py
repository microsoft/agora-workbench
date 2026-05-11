"""Local BM25-backed vignette search.

Provides :class:`LocalFileSearchVignetteRepo`, a dependency-free retrieval
backend that reads vignettes from the same on-disk layout used by
:class:`~.local_file_repo.LocalFileVignetteRepo` and ranks them with the
shared :class:`~tools.search._bm25.BM25Index`.

This is the read-side complement to the local write backend and lets
callers run the tool-learning middleware end-to-end without Azure AI
Search.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional, Tuple

from pydantic import ValidationError

from tools.search._bm25 import BM25Index

from .config import ToolLearningConfig
from .local_file_repo import LocalFileVignetteRepo
from .models import Vignette

LOGGER = logging.getLogger(__name__)


def _vignette_text(v: Vignette) -> str:
    """Build the indexable text for a vignette.

    Concatenates the human-readable fields most likely to match a query:
    title, summary, tags, the anti-pattern rule/rationale, and the repair
    steps. Empty fields are skipped so they don't introduce noise.
    """
    parts: list[str] = [v.title, v.summary]
    parts.extend(v.tags)
    if v.anti_pattern is not None:
        parts.append(v.anti_pattern.rule)
        if v.anti_pattern.rationale:
            parts.append(v.anti_pattern.rationale)
    if v.repair is not None:
        parts.extend(v.repair.steps)
    return " ".join(p for p in parts if p)


def _applicable_scopes(tenant_id: Optional[str], user_id: Optional[str]) -> set[str]:
    """Compute which scope levels are visible to this caller."""
    scopes = {"global"}
    if tenant_id:
        scopes.add("org")
        if user_id:
            scopes.add("user")
    return scopes


class LocalFileSearchVignetteRepo:
    """BM25 search over locally persisted vignette JSON files.

    Reads from the same directory used by
    :class:`~.local_file_repo.LocalFileVignetteRepo` so a single
    ``local_storage_dir`` setting drives both write and retrieval.

    Indexes are cached per ``(tenant_id, tool_name)`` and lazily rebuilt
    whenever the underlying file's mtime changes. Each query scans the
    caller's tenant file plus the ``global`` tenant file so global
    vignettes are always reachable.

    Implements the :class:`~.read_repo.VignetteSearchRepo` protocol.
    """

    def __init__(self, config: ToolLearningConfig) -> None:
        self._config = config
        # Reuse the write-repo's path computation and JSON parsing so the
        # two backends stay in lockstep on disk layout.
        self._files = LocalFileVignetteRepo(config=config)
        # Cache: (tenant_key, tool_name) -> (mtime_ns, BM25Index[Vignette])
        self._cache: dict[Tuple[str, str], Tuple[int, BM25Index[Vignette]]] = {}

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def _load_index(self, tenant_id: Optional[str], tool_name: str) -> BM25Index[Vignette]:
        """Return an up-to-date BM25 index for ``(tenant, tool)``.

        Uses the file mtime as a freshness token; rebuilds only when the
        underlying JSON file has changed since the last query.
        """
        path = self._files._tool_file_path(tenant_id, tool_name)
        cache_key = (tenant_id or "global", tool_name)
        mtime_ns = path.stat().st_mtime_ns if path.exists() else 0

        cached = self._cache.get(cache_key)
        if cached is not None and cached[0] == mtime_ns:
            return cached[1]

        index: BM25Index[Vignette] = BM25Index()
        for vignette in self._read_vignettes(path):
            index.add(vignette, _vignette_text(vignette))

        self._cache[cache_key] = (mtime_ns, index)
        return index

    def _read_vignettes(self, path: Path) -> List[Vignette]:
        """Parse vignettes from a single JSON file (best-effort)."""
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            LOGGER.warning("Failed to parse local vignette store %s: %s", path, exc)
            return []
        if not isinstance(payload, list):
            LOGGER.warning("Skipping malformed local vignette store %s: expected a list payload.", path)
            return []
        out: List[Vignette] = []
        for item in payload:
            try:
                out.append(Vignette.model_validate(item))
            except ValidationError as exc:
                LOGGER.warning("Skipping malformed local vignette entry in %s: %s", path, exc)
        return out

    # ------------------------------------------------------------------
    # VignetteSearchRepo protocol
    # ------------------------------------------------------------------

    def search_vignettes(
        self,
        query_text: str,
        tool_name: str,
        kind: Optional[str] = None,
        error_class: Optional[str] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        top_k: Optional[int] = None,
    ) -> List[Vignette]:
        """BM25 search over locally persisted vignettes for ``tool_name``.

        Combines hits from the caller's tenant file and the ``global``
        tenant file, applies scope/kind/error-class/min-confidence
        filters, and returns up to ``top_k`` results sorted by BM25 score.
        """
        k = top_k if top_k is not None else self._config.top_k
        applicable = _applicable_scopes(tenant_id, user_id)

        # Always consult the caller's tenant file plus the global file.
        # Use a key set to avoid double-loading when tenant_id is None.
        tenant_keys: list[Optional[str]] = [None]
        if tenant_id:
            tenant_keys.append(tenant_id)

        scored: list[tuple[Vignette, float]] = []
        seen_ids: set[str] = set()

        for t in tenant_keys:
            index = self._load_index(t, tool_name)
            # Pull a generous candidate pool so post-filters don't starve
            # the result set; cap at index size.
            candidate_k = max(k * 4, k)
            for vignette, score in index.search(query_text, top_k=candidate_k):
                if vignette.vignette_id in seen_ids:
                    continue
                if vignette.scope not in applicable:
                    continue
                if tenant_id is not None and vignette.scope in {"org", "user"} and vignette.tenant_id != tenant_id:
                    continue
                if user_id is not None and vignette.scope == "user" and vignette.user_id != user_id:
                    continue
                if vignette.scope == "user" and user_id is None:
                    continue
                if kind is not None and vignette.kind != kind:
                    continue
                if error_class is not None and vignette.match.error_class != error_class:
                    continue
                if vignette.confidence < self._config.min_confidence:
                    continue
                if score <= 0.0:
                    continue
                scored.append((vignette, score))
                seen_ids.add(vignette.vignette_id)

        scored.sort(key=lambda x: x[1], reverse=True)
        return [v for v, _ in scored[:k]]
