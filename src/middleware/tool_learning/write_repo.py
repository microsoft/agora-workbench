"""Write-repository protocol for vignette persistence backends."""

from __future__ import annotations

from typing import List, Optional, Protocol

from .models import Vignette


class VignetteWriteRepo(Protocol):
    """Persistence interface for vignette write backends."""

    def upsert_vignette(self, vignette: Vignette) -> None:
        """Insert or update a vignette."""
        ...

    def get_vignettes_for_tool(
        self,
        tool_name: str,
        tenant_id: Optional[str] = None,
        kind: Optional[str] = None,
        error_class: Optional[str] = None,
        max_results: int = 20,
    ) -> List[Vignette]:
        """Retrieve vignettes for a tool."""
        ...
