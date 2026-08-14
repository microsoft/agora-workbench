"""Kernel bootstrap state must follow kernel lifetime, not session lifetime.

One-time setup that lives *inside* the kernel process — the AGORA_OUTPUT_DIR
preamble, the tool proxy functions — is invalidated the moment that process is
replaced.  Because a session id outlives its kernel (idle cleanup, timeout
recovery, explicit close followed by reuse of the same logical id), tracking
that setup per session leaves a rebuilt kernel silently un-bootstrapped: the
agent's next call hits ``NameError`` on helpers it was told exist.

These tests pin the generation-keyed scheme that makes every teardown path
correct without the teardown path having to know about the bootstrap state.
"""

import pytest

from ..code_execution_models import CodeExecutionResult
from ..server import CodeExecutionServer
from ..sessions.manager import (
    KERNEL_BOOTSTRAP_TOOL_PROXIES,
    SessionManager,
)
from ..tool_registry import ToolDefinition, ToolParameter, ToolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry() -> ToolRegistry:
    """A registry with one fully-resolved tool, enough to generate proxies."""
    registry = ToolRegistry(package="aurora_tools")
    registry.register_tool(
        ToolDefinition(
            name="prepare_aurora_batch",
            description="Build a forecast batch.",
            required_parameters=[ToolParameter(name="steps", type=int, description="Forecast steps")],
        )
    )
    return registry


class _RecordingServer:
    """Drives the real ``_inject_tool_proxies`` against a stubbed kernel.

    Only the kernel round-trip is faked; proxy code generation, the bootstrap
    check and the bookkeeping under test all run for real.
    """

    def __init__(self, session_manager: SessionManager):
        self.server = object.__new__(CodeExecutionServer)
        self.server.session_manager = session_manager
        self.server.tool_registry = _make_registry()
        self.executed: list[str] = []

        async def _fake_execute_code(code: str, timeout: int = 30) -> CodeExecutionResult:
            self.executed.append(code)
            return CodeExecutionResult(success=True)

        self.server._execute_code = _fake_execute_code

    def pop_executed(self) -> list[str]:
        batch, self.executed = list(self.executed), []
        return batch


@pytest.fixture
def manager(tmp_path, monkeypatch) -> SessionManager:
    """A SessionManager whose outputs dir is redirected away from ``~``."""
    from .. import sessions as sessions_pkg

    monkeypatch.setattr(sessions_pkg.manager, "_OUTPUTS_BASE_DIR", tmp_path)
    return sessions_pkg.SessionManager()


# ---------------------------------------------------------------------------
# Generation bookkeeping
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKernelGenerations:
    def test_generation_is_none_without_a_kernel(self, manager):
        assert manager.get_kernel_generation("nope") is None

    def test_generations_are_unique_and_monotonic(self, manager):
        first = manager._assign_kernel_generation("s1")
        second = manager._assign_kernel_generation("s1")
        other = manager._assign_kernel_generation("s2")

        assert second > first, "a restarted kernel must get a strictly greater generation"
        assert other > second, "generations are global, not per-session"
        assert manager.get_kernel_generation("s1") == second

    def test_generation_is_not_reused_after_discard(self, manager):
        """The counter must not rewind on teardown, or a rebuilt kernel could
        inherit a stale generation and be mistaken for a bootstrapped one."""
        first = manager._assign_kernel_generation("s1")
        manager._discard_kernel_generation("s1")
        assert manager.get_kernel_generation("s1") is None

        assert manager._assign_kernel_generation("s1") > first


@pytest.mark.unit
class TestBootstrapMarking:
    def test_mark_then_query(self, manager):
        manager._assign_kernel_generation("s1")
        assert manager.mark_kernel_bootstrapped("s1", "widget") is True
        assert manager.is_kernel_bootstrapped("s1", "widget") is True

    def test_unrelated_key_is_not_bootstrapped(self, manager):
        manager._assign_kernel_generation("s1")
        manager.mark_kernel_bootstrapped("s1", "widget")
        assert manager.is_kernel_bootstrapped("s1", "other") is False

    def test_marking_without_a_kernel_is_a_no_op(self, manager):
        """Nothing to bootstrap against, so the caller must retry later."""
        assert manager.mark_kernel_bootstrapped("s1", "widget") is False
        assert manager.is_kernel_bootstrapped("s1", "widget") is False

    def test_restart_invalidates_bootstrap(self, manager):
        manager._assign_kernel_generation("s1")
        manager.mark_kernel_bootstrapped("s1", "widget")

        manager._assign_kernel_generation("s1")  # same session id, new kernel
        assert manager.is_kernel_bootstrapped("s1", "widget") is False

    def test_teardown_invalidates_bootstrap(self, manager):
        manager._assign_kernel_generation("s1")
        manager.mark_kernel_bootstrapped("s1", "widget")

        manager._discard_kernel_generation("s1")
        assert manager.is_kernel_bootstrapped("s1", "widget") is False

    def test_restart_releases_prior_bootstrap_state(self, manager):
        """Bookkeeping must not accumulate one entry per kernel ever started."""
        first = manager._assign_kernel_generation("s1")
        manager.mark_kernel_bootstrapped("s1", "widget")
        manager._assign_kernel_generation("s1")

        assert first not in manager._kernel_bootstrap_state

    def test_discard_releases_bootstrap_state(self, manager):
        generation = manager._assign_kernel_generation("s1")
        manager.mark_kernel_bootstrapped("s1", "widget")
        manager._discard_kernel_generation("s1")

        assert generation not in manager._kernel_bootstrap_state

    async def test_shutdown_kernel_discards_generation(self, manager):
        """Covers the wiring, not just the helper: real kernel teardown must
        clear the generation so the replacement kernel is re-bootstrapped."""

        class _StubKernelManager:
            async def shutdown_kernel(self, now=False):
                pass

            async def cleanup_resources(self):
                pass

        class _StubKernelClient:
            def stop_channels(self):
                pass

        manager._kernels["s1"] = (_StubKernelManager(), _StubKernelClient())
        manager._kernel_last_used["s1"] = 0.0
        manager._assign_kernel_generation("s1")
        manager.mark_kernel_bootstrapped("s1", KERNEL_BOOTSTRAP_TOOL_PROXIES)

        await manager._shutdown_kernel("s1")

        assert manager.get_kernel_generation("s1") is None
        assert manager.is_kernel_bootstrapped("s1", KERNEL_BOOTSTRAP_TOOL_PROXIES) is False


# ---------------------------------------------------------------------------
# The reported bug: proxies not re-injected into a rebuilt kernel
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestToolProxyReinjection:
    async def test_injects_on_first_use(self, manager):
        harness = _RecordingServer(manager)
        manager._assign_kernel_generation("s1")

        await harness.server._inject_tool_proxies("s1")

        executed = harness.pop_executed()
        assert len(executed) == 3, "tracing infrastructure + proxies + list_tools"
        assert any("prepare_aurora_batch" in block for block in executed)

    async def test_is_idempotent_for_a_live_kernel(self, manager):
        harness = _RecordingServer(manager)
        manager._assign_kernel_generation("s1")

        await harness.server._inject_tool_proxies("s1")
        harness.pop_executed()
        await harness.server._inject_tool_proxies("s1")

        assert harness.pop_executed() == []

    async def test_reinjects_after_kernel_restart(self, manager):
        """Regression: a rebuilt kernel used to keep the session-keyed latch,
        so its namespace never received the proxies and every call to a tool
        helper raised NameError."""
        harness = _RecordingServer(manager)
        manager._assign_kernel_generation("s1")
        await harness.server._inject_tool_proxies("s1")
        harness.pop_executed()

        manager._assign_kernel_generation("s1")  # same session id, new kernel
        await harness.server._inject_tool_proxies("s1")

        executed = harness.pop_executed()
        assert len(executed) == 3
        assert any("prepare_aurora_batch" in block for block in executed)

    async def test_reinjects_after_kernel_teardown(self, manager):
        harness = _RecordingServer(manager)
        manager._assign_kernel_generation("s1")
        await harness.server._inject_tool_proxies("s1")
        harness.pop_executed()

        manager._discard_kernel_generation("s1")
        manager._assign_kernel_generation("s1")
        await harness.server._inject_tool_proxies("s1")

        assert len(harness.pop_executed()) == 3

    async def test_injection_before_kernel_exists_is_retried(self, manager):
        """``_execute_code`` creates the kernel lazily, so a stub that never
        registers one must not leave the server believing it succeeded."""
        harness = _RecordingServer(manager)

        await harness.server._inject_tool_proxies("s1")
        assert len(harness.pop_executed()) == 3

        await harness.server._inject_tool_proxies("s1")
        assert len(harness.pop_executed()) == 3, "no kernel to record against, so injection repeats"

    async def test_no_tools_means_no_injection(self, manager):
        harness = _RecordingServer(manager)
        harness.server.tool_registry = ToolRegistry()
        manager._assign_kernel_generation("s1")

        await harness.server._inject_tool_proxies("s1")

        assert harness.pop_executed() == []
        assert manager.is_kernel_bootstrapped("s1", KERNEL_BOOTSTRAP_TOOL_PROXIES) is False


# ---------------------------------------------------------------------------
# Trace flushing is gated on the same state
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestToolTracingActive:
    async def test_inactive_before_injection(self, manager):
        harness = _RecordingServer(manager)
        manager._assign_kernel_generation("s1")

        assert harness.server._tool_tracing_active("s1") is False

    async def test_active_after_injection(self, manager):
        harness = _RecordingServer(manager)
        manager._assign_kernel_generation("s1")
        await harness.server._inject_tool_proxies("s1")

        assert harness.server._tool_tracing_active("s1") is True

    async def test_inactive_after_kernel_restart(self, manager):
        """The kernel-side ToolCallLog died with the old process; flushing it
        would fail, so tracing must report inactive until re-injection."""
        harness = _RecordingServer(manager)
        manager._assign_kernel_generation("s1")
        await harness.server._inject_tool_proxies("s1")

        manager._assign_kernel_generation("s1")
        assert harness.server._tool_tracing_active("s1") is False

    async def test_inactive_without_session_id(self, manager):
        harness = _RecordingServer(manager)
        assert harness.server._tool_tracing_active(None) is False

    async def test_inactive_without_tools(self, manager):
        harness = _RecordingServer(manager)
        manager._assign_kernel_generation("s1")
        await harness.server._inject_tool_proxies("s1")

        harness.server.tool_registry = ToolRegistry()
        assert harness.server._tool_tracing_active("s1") is False
