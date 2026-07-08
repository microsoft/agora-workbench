"""Lifecycle management for co-located sidecar processes.

A *sidecar* is a long-lived helper process launched alongside a
:class:`~agora_workbench.code_execution.server.CodeExecutionServer`. Its purpose
is to hold expensive, process-global state — typically a heavy model — so it is
loaded **once** per container and shared across all kernel sessions over
loopback HTTP, rather than being reloaded (and its memory multiplied) inside
each isolated kernel.

:class:`SidecarManager` owns the launch/readiness/teardown of the sidecars
declared on a :class:`ServerConfig`. It:

* resolves each sidecar's argv (prepending the kernel environment's Python when
  ``use_env_python`` is set, so the sidecar shares the tools' dependencies),
* launches the process bound to loopback,
* waits for the sidecar's health endpoint to report ready,
* exports the sidecar base URL into ``os.environ`` under the configured
  ``url_env_var`` so every subsequently-spawned kernel inherits it, and
* terminates the processes on shutdown (graceful SIGTERM, then SIGKILL).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Optional

import httpx

if TYPE_CHECKING:
    from .code_execution_models import ServerConfig, SidecarConfig

LOGGER = logging.getLogger(__name__)

# Grace period between SIGTERM and SIGKILL when stopping a sidecar.
_TERMINATION_GRACE_S = 10.0
# Interval between health-endpoint polls while waiting for readiness.
_HEALTH_POLL_INTERVAL_S = 1.0


class SidecarManager:
    """Launches, health-checks, and stops the sidecars declared on a ServerConfig."""

    def __init__(self, config: "ServerConfig"):
        self._config = config
        self._processes: dict[str, subprocess.Popen] = {}

    @property
    def running(self) -> bool:
        """True if any sidecar process is currently tracked."""
        return bool(self._processes)

    # ------------------------------------------------------------------ #
    # Startup
    # ------------------------------------------------------------------ #
    async def start_all(self) -> None:
        """Launch every declared sidecar and block until each reports ready.

        On any failure the already-started sidecars are torn down so the caller
        is never left with a partially-initialized set of processes.
        """
        if not self._config.sidecars:
            return

        LOGGER.info("Starting %d sidecar(s) for server '%s'.", len(self._config.sidecars), self._config.name)
        try:
            for spec in self._config.sidecars:
                await self._start_one(spec)
        except Exception:
            LOGGER.error("Sidecar startup failed; stopping any already-started sidecars.", exc_info=True)
            await self.stop_all()
            raise

    async def _start_one(self, spec: "SidecarConfig") -> None:
        if spec.name in self._processes:
            raise ValueError(f"Duplicate sidecar name '{spec.name}'.")

        argv = self._resolve_argv(spec)
        env = self._build_env(spec)

        LOGGER.info("Launching sidecar '%s': %s", spec.name, " ".join(argv))
        try:
            # start_new_session detaches the child into its own process group so
            # we can signal the whole group and it is not disturbed by signals
            # delivered to the server's controlling terminal.
            proc = subprocess.Popen(argv, env=env, start_new_session=True)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Sidecar '{spec.name}' executable not found: {argv[0]!r}. "
                f"When use_env_python is True the kernel environment must be built first."
            ) from exc
        self._processes[spec.name] = proc

        await self._await_ready(spec, proc)

        # Publish the discovery URL so kernels (which copy os.environ at spawn)
        # can reach the sidecar. Mirrors how MCP_ASSET_CACHE_DIR is exposed.
        os.environ[spec.url_env_var] = spec.base_url()
        LOGGER.info(
            "Sidecar '%s' ready at %s (exported as %s).",
            spec.name,
            spec.base_url(),
            spec.url_env_var,
        )

    def _resolve_argv(self, spec: "SidecarConfig") -> list[str]:
        if not spec.use_env_python:
            return list(spec.command)
        python = self._config.get_python_path()
        if not python.exists():
            raise RuntimeError(
                f"Sidecar '{spec.name}' requires the kernel environment Python at {python}, "
                f"but it does not exist. Ensure the environment is built before startup."
            )
        return [str(python), *spec.command]

    def _build_env(self, spec: "SidecarConfig") -> dict[str, str]:
        env = os.environ.copy()
        # Tell the sidecar where to bind. A single entrypoint can honor these
        # without hardcoding the address chosen by the operator.
        env["SIDECAR_HOST"] = spec.host
        env["SIDECAR_PORT"] = str(spec.port)
        env.update(spec.env)
        return env

    async def _await_ready(self, spec: "SidecarConfig", proc: subprocess.Popen) -> None:
        deadline = time.monotonic() + spec.readiness_timeout_s
        url = spec.health_url()
        last_error: Optional[str] = None

        async with httpx.AsyncClient(timeout=5.0) as client:
            while time.monotonic() < deadline:
                exit_code = proc.poll()
                if exit_code is not None:
                    raise RuntimeError(
                        f"Sidecar '{spec.name}' exited during startup with code {exit_code} "
                        f"before its health endpoint became ready."
                    )
                try:
                    resp = await client.get(url)
                    if resp.is_success:
                        return
                    last_error = f"HTTP {resp.status_code}"
                except httpx.HTTPError as exc:
                    last_error = str(exc) or exc.__class__.__name__
                await asyncio.sleep(_HEALTH_POLL_INTERVAL_S)

        raise TimeoutError(
            f"Sidecar '{spec.name}' did not become ready at {url} within "
            f"{spec.readiness_timeout_s:.0f}s (last error: {last_error})."
        )

    # ------------------------------------------------------------------ #
    # Shutdown
    # ------------------------------------------------------------------ #
    async def stop_all(self) -> None:
        """Stop every tracked sidecar (SIGTERM, then SIGKILL after a grace period)."""
        if not self._processes:
            return
        LOGGER.info("Stopping %d sidecar(s) for server '%s'.", len(self._processes), self._config.name)
        for name in list(self._processes):
            await self._stop_one(name)

    async def _stop_one(self, name: str) -> None:
        proc = self._processes.pop(name, None)
        if proc is None:
            return
        if proc.poll() is not None:
            return

        LOGGER.info("Terminating sidecar '%s' (pid %d).", name, proc.pid)
        self._signal_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=_TERMINATION_GRACE_S)
            return
        except asyncio.TimeoutError:
            LOGGER.warning("Sidecar '%s' did not exit after SIGTERM; sending SIGKILL.", name)
        self._signal_group(proc, signal.SIGKILL)
        try:
            await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=_TERMINATION_GRACE_S)
        except asyncio.TimeoutError:
            LOGGER.error("Sidecar '%s' (pid %d) did not exit after SIGKILL.", name, proc.pid)

    @staticmethod
    def _signal_group(proc: subprocess.Popen, sig: int) -> None:
        """Signal the sidecar's whole process group, falling back to the process."""
        try:
            if sys.platform != "win32":
                os.killpg(os.getpgid(proc.pid), sig)
            else:  # pragma: no cover - POSIX is the supported deployment target
                proc.send_signal(sig)
        except ProcessLookupError:
            pass  # Already gone.
        except Exception:
            LOGGER.debug("Failed to signal sidecar process group; signaling process directly.", exc_info=True)
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass
