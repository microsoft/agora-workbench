"""Local file repository for tool-learning vignettes."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .config import ToolLearningConfig
from .models import Vignette
from .write_repo import VignetteWriteRepo

LOGGER = logging.getLogger(__name__)

_DEFAULT_LOCAL_DIR = "~/.agora/vignettes"


def _safe_path_component(raw: str) -> str:
    """Create a filesystem-safe path component from a free-form identifier."""
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    return normalized or "default"


def _build_row_key(scope: str, kind: str, error_class: Optional[str], vignette_id: str) -> str:
    """Build a deterministic row-key equivalent for local dedupe."""
    return f"{scope}|{kind}|{error_class or 'none'}|{vignette_id}"


class LocalFileVignetteRepo(VignetteWriteRepo):
    """Persists vignette payloads in JSON files under a local directory."""

    def __init__(self, config: ToolLearningConfig) -> None:
        self._config = config
        configured_dir = (config.local_storage_dir or "").strip()
        self._base_dir = Path(configured_dir or _DEFAULT_LOCAL_DIR).expanduser()
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _tool_file_path(self, tenant_id: Optional[str], tool_name: str) -> Path:
        tenant = _safe_path_component(tenant_id or "global")
        tool_safe = _safe_path_component(tool_name)
        tool_hash = hashlib.sha256(tool_name.encode("utf-8")).hexdigest()[:12]
        return self._base_dir / tenant / f"{tool_safe}-{tool_hash}.json"

    def _read_vignettes(self, path: Path) -> List[Vignette]:
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.warning("Failed to parse local vignette store %s: %s", path, exc)
            return []

        if not isinstance(payload, list):
            LOGGER.warning("Skipping malformed local vignette store %s: expected a list payload.", path)
            return []

        vignettes: List[Vignette] = []
        for item in payload:
            try:
                vignettes.append(Vignette.model_validate(item))
            except Exception as exc:
                LOGGER.warning("Skipping malformed local vignette entry in %s: %s", path, exc)
        return vignettes

    def _write_vignettes(self, path: Path, vignettes: List[Vignette]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [v.model_dump(mode="json") for v in vignettes]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def upsert_vignette(self, vignette: Vignette) -> None:
        path = self._tool_file_path(vignette.tenant_id, vignette.tool.tool_name)
        existing = self._read_vignettes(path)
        row_key = _build_row_key(vignette.scope, vignette.kind, vignette.match.error_class, vignette.vignette_id)

        merged = False
        updated: List[Vignette] = []
        for current in existing:
            current_row_key = _build_row_key(current.scope, current.kind, current.match.error_class, current.vignette_id)
            if current_row_key != row_key:
                updated.append(current)
                continue

            merged_tags = sorted(set(current.tags) | set(vignette.tags))
            updated.append(
                vignette.model_copy(
                    update={
                        "confidence": min(current.confidence + 0.05, 1.0),
                        "tags": merged_tags,
                        "created_at": current.created_at,
                        "updated_at": datetime.now(timezone.utc),
                    }
                )
            )
            merged = True

        if not merged:
            updated.append(vignette)

        updated.sort(key=lambda v: v.confidence, reverse=True)
        self._write_vignettes(path, updated)

    def get_vignettes_for_tool(
        self,
        tool_name: str,
        tenant_id: Optional[str] = None,
        kind: Optional[str] = None,
        error_class: Optional[str] = None,
        max_results: int = 20,
    ) -> List[Vignette]:
        path = self._tool_file_path(tenant_id, tool_name)
        vignettes = self._read_vignettes(path)
        filtered = [
            vignette
            for vignette in vignettes
            if (kind is None or vignette.kind == kind)
            and (error_class is None or vignette.match.error_class == error_class)
        ]
        filtered.sort(key=lambda v: v.confidence, reverse=True)
        return filtered[:max_results]
