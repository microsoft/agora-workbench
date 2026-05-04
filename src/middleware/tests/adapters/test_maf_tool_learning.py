"""Tests for MAF tool-learning middleware adapters."""

from __future__ import annotations

import pytest

from middleware.tool_learning.models import (
    AntiPattern,
    MatchSpec,
    ToolSignature,
    Vignette,
)
from middleware.tool_learning.adapters.maf_function import _check_hard_violations
from middleware.protocols import FunctionInfo


# ---------------------------------------------------------------------------
# Hard-constraint check tests
# ---------------------------------------------------------------------------


class TestCheckHardViolations:
    """Test the _check_hard_violations helper in middleware_function."""

    def _make_hard_vignette(self, required_keys: list[str]) -> Vignette:
        return Vignette(
            vignette_id="hv-001",
            kind="anti_pattern",
            scope="user",
            tenant_id="t1",
            user_id="u1",
            tool=ToolSignature(tool_name="calendar.create_event"),
            match=MatchSpec(arg_keys=required_keys),
            title="Timezone required",
            summary="Must supply timezone.",
            anti_pattern=AntiPattern(
                rule="Do not omit 'timezone' for calendar.create_event.",
                severity="hard",
            ),
        )

    @pytest.mark.unit
    def test_no_violation_when_required_key_present(self):
        """No violation when the required key is present in args."""
        v = self._make_hard_vignette(["timezone"])
        violations = _check_hard_violations([v], {"timezone": "UTC", "title": "Meeting"})
        assert violations == []

    @pytest.mark.unit
    def test_violation_when_required_key_missing(self):
        """Violation is raised when a required key is absent from args."""
        v = self._make_hard_vignette(["timezone"])
        violations = _check_hard_violations([v], {"title": "Meeting"})
        assert len(violations) == 1
        assert "calendar.create_event" in violations[0]

    @pytest.mark.unit
    def test_no_violation_for_soft_anti_pattern(self):
        """Soft-severity anti-patterns never produce hard violations."""
        v = Vignette(
            vignette_id="sv-001",
            kind="anti_pattern",
            scope="user",
            tenant_id="t1",
            user_id="u1",
            tool=ToolSignature(tool_name="my_tool"),
            match=MatchSpec(arg_keys=["required_key"]),
            title="Soft rule",
            summary="Soft advisory only.",
            anti_pattern=AntiPattern(
                rule="Prefer X over Y.",
                severity="soft",
            ),
        )
        violations = _check_hard_violations([v], {})
        assert violations == []

    @pytest.mark.unit
    def test_no_violation_when_no_arg_keys(self):
        """Hard anti-patterns with no arg_keys do not produce violations."""
        v = Vignette(
            vignette_id="hv-002",
            kind="anti_pattern",
            scope="user",
            tenant_id="t1",
            user_id="u1",
            tool=ToolSignature(tool_name="my_tool"),
            match=MatchSpec(),
            title="Hard rule",
            summary="No arg_keys specified.",
            anti_pattern=AntiPattern(
                rule="Always use correct args.",
                severity="hard",
            ),
        )
        violations = _check_hard_violations([v], {})
        assert violations == []


# ---------------------------------------------------------------------------
# VignetteRunMiddleware tests (now a ContextProvider)
# ---------------------------------------------------------------------------


class TestVignetteRunMiddlewareProvide:
    """Test VignetteRunMiddleware.provide() with dynamic discovery and caching."""

    def _make_context(self, tool_names=None):
        """Build a minimal mock Agora AgentContext."""
        from unittest.mock import MagicMock

        ctx = MagicMock()
        ctx.tools = [FunctionInfo(name=n) for n in (tool_names or [])]
        ctx.extend_messages = MagicMock()
        return ctx

    def _make_middleware(self, search_repo=None, **kwargs):
        """Create middleware with a mocked search repo (bypass config validation)."""
        from middleware.tool_learning.adapters.maf_run import VignetteRunMiddleware
        from middleware.tool_learning.config import ToolLearningConfig

        config = ToolLearningConfig(
            search_endpoint="https://fake.search.windows.net",
            search_index_name="test-index",
        )
        mw = VignetteRunMiddleware.__new__(VignetteRunMiddleware)
        mw._config = config
        mw._explicit_tool_names = kwargs.get("tool_names")
        mw._tenant_id = kwargs.get("tenant_id")
        mw._user_id = kwargs.get("user_id")
        mw._search_repo = search_repo
        mw._cache = {}
        return mw

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_discovers_tool_names_from_context_tools(self):
        """When no explicit tool_names, middleware discovers from context.tools."""
        from unittest.mock import MagicMock, AsyncMock

        search_repo = MagicMock()
        search_repo.search_vignettes.return_value = []

        mw = self._make_middleware(search_repo=search_repo)
        ctx = self._make_context(tool_names=["my_tool"])

        await mw.provide(ctx)

        search_repo.search_vignettes.assert_called_once()
        args = search_repo.search_vignettes.call_args
        assert args[0][1] == "my_tool"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_uses_explicit_tool_names_over_discovery(self):
        """When explicit tool_names provided, middleware ignores agent tools."""
        from unittest.mock import MagicMock, AsyncMock

        search_repo = MagicMock()
        search_repo.search_vignettes.return_value = []

        mw = self._make_middleware(search_repo=search_repo, tool_names=["override_tool"])
        ctx = self._make_context(tool_names=["agent_tool"])

        await mw.provide(ctx)

        args = search_repo.search_vignettes.call_args
        assert args[0][1] == "override_tool"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_caches_vignette_results(self):
        """Repeated calls with same tool set use cached vignettes."""
        from unittest.mock import MagicMock, AsyncMock

        search_repo = MagicMock()
        search_repo.search_vignettes.return_value = []

        mw = self._make_middleware(search_repo=search_repo)
        ctx = self._make_context(tool_names=["my_tool"])

        await mw.provide(ctx)
        await mw.provide(ctx)

        # search_vignettes called only once — second call hits cache
        assert search_repo.search_vignettes.call_count == 1

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_cache_miss_on_different_tool_set(self):
        """Different tool sets produce separate cache entries."""
        from unittest.mock import MagicMock, AsyncMock

        search_repo = MagicMock()
        search_repo.search_vignettes.return_value = []

        mw = self._make_middleware(search_repo=search_repo)

        ctx1 = self._make_context(tool_names=["tool_a"])
        await mw.provide(ctx1)

        ctx2 = self._make_context(tool_names=["tool_b"])
        await mw.provide(ctx2)

        assert search_repo.search_vignettes.call_count == 2

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_no_search_when_no_repo(self):
        """When search repo is unavailable, middleware is a no-op."""
        from unittest.mock import AsyncMock

        mw = self._make_middleware(search_repo=None)
        ctx = self._make_context(tool_names=["my_tool"])

        # Should not raise
        await mw.provide(ctx)
        ctx.extend_messages.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_no_search_when_no_tools(self):
        """When no tools are registered, middleware is a no-op."""
        from unittest.mock import MagicMock, AsyncMock

        search_repo = MagicMock()
        mw = self._make_middleware(search_repo=search_repo)
        ctx = self._make_context(tool_names=[])

        await mw.provide(ctx)

        search_repo.search_vignettes.assert_not_called()
        ctx.extend_messages.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_resolve_tool_names_uses_context_tools(self):
        """_resolve_tool_names uses context.tools (Agora protocol), not MAF internals."""
        from middleware.tool_learning.adapters.maf_run import VignetteRunMiddleware
        from middleware.tool_learning.config import ToolLearningConfig

        config = ToolLearningConfig(
            search_endpoint="https://fake.search.windows.net",
            search_index_name="test-index",
        )
        mw = VignetteRunMiddleware.__new__(VignetteRunMiddleware)
        mw._config = config
        mw._explicit_tool_names = None
        mw._tenant_id = None
        mw._user_id = None
        mw._search_repo = None
        mw._cache = {}

        ctx = self._make_context(tool_names=["search_tools", "execute_grid_code"])
        names = mw._resolve_tool_names(ctx)
        assert "search_tools" in names
        assert "execute_grid_code" in names
        assert len(names) == 2
