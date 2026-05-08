"""
VignetteFunctionMiddleware: intercepts tool calls for validation and repair.

This module provides :class:`VignetteFunctionMiddleware`, an Agora
:class:`~middleware.protocols.FunctionMiddleware` that validates tool calls
against anti-patterns and applies repair templates when tool calls fail.

The middleware is framework-agnostic.  To use it inside a MAF agent, wrap it
with :func:`~middleware.decision_log.adapters.maf_protocols.wrap_function_middleware`:

    from middleware.tool_learning.adapters import VignetteFunctionMiddleware
    from middleware.decision_log.adapters.maf_protocols import wrap_function_middleware

    agora_mw = VignetteFunctionMiddleware(
        config=ToolLearningConfig.from_env(),
        credential=credential,
    )
    maf_mw = wrap_function_middleware(agora_mw)
    agent = Agent(..., middleware=[maf_mw])

Before a tool call:
  - Retrieves anti-pattern vignettes and checks for "hard" constraint violations.
  - Hard violations block execution and surface the guardrail to the caller.

After a tool call failure:
  - Retrieves repair-template vignettes for (tool_name, error_class).
  - Executes a bounded repair loop (patching arguments via repair steps).
  - On success, emits a vignette candidate for the write pipeline.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from middleware.protocols import (
    FunctionInvocationContext,
    FunctionMiddleware,
    MiddlewareTermination,
)

from ..compile import compile_vignettes
from ..config import ToolLearningConfig
from ..local_file_repo import LocalFileVignetteRepo
from ..render import render_repair_block
from ..search_repo import SearchVignetteRepo
from ..table_repo import TableVignetteRepo
from ..write_repo import VignetteWriteRepo

LOGGER = logging.getLogger(__name__)


def _extract_error_class(exc: Exception) -> str:
    """Extract a stable error class name from an exception."""
    return type(exc).__name__


def _check_hard_violations(vignettes, args: Dict[str, Any]) -> List[str]:
    """
    Check whether any hard anti-pattern constraints are violated.

    Treats match.arg_keys as *required* keys: a violation is raised when any
    listed key is absent from the current tool-call arguments.

    Returns a list of violation messages (empty if no violations).
    """
    violations = []
    for v in vignettes:
        if v.anti_pattern and v.anti_pattern.severity == "hard":
            # Treat match.arg_keys as required keys: flag if any are missing
            if v.match.arg_keys:
                required_keys = set(v.match.arg_keys)
                missing_keys = required_keys - set(args.keys())
                if missing_keys:
                    violations.append(f"HARD constraint violated for {v.tool.tool_name!r}: {v.anti_pattern.rule}")
    return violations


class VignetteFunctionMiddleware(FunctionMiddleware):
    """
    Agora FunctionMiddleware that validates tool calls against anti-patterns and
    applies repair templates when tool calls fail.

    Implements :class:`~middleware.protocols.FunctionMiddleware` — wrap it with
    :func:`~middleware.decision_log.adapters.maf_protocols.wrap_function_middleware`
    to use it inside a MAF agent.

    Args:
        config: Tool-learning configuration.
        credential: Azure TokenCredential. Used for Search and table write backend.
        tenant_id: Optional tenant ID for scope filtering.
        user_id: Optional user ID for scope filtering.
        write_vignettes: If True, compile and upsert vignettes on successful repair.
        storage: Optional write backend override ("table" or "local").
    """

    def __init__(
        self,
        config: ToolLearningConfig,
        credential=None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        write_vignettes: bool = True,
        storage: Optional[str] = None,
    ) -> None:
        self._config = config
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._write_vignettes = write_vignettes
        self._search_repo: Optional[SearchVignetteRepo] = None
        self._write_repo: Optional[VignetteWriteRepo] = None

        if storage is not None and storage not in {"table", "local"}:
            raise ValueError("storage must be one of: 'table', 'local', or None")

        try:
            self._search_repo = SearchVignetteRepo(config=config, credential=credential)
        except Exception as e:
            LOGGER.warning("VignetteFunctionMiddleware: search repo unavailable: %s", e)

        if write_vignettes:
            try:
                if storage == "local":
                    self._write_repo = LocalFileVignetteRepo(config=config)
                elif storage == "table":
                    self._write_repo = TableVignetteRepo(config=config, credential=credential)
                elif config.table_storage_endpoint:
                    self._write_repo = TableVignetteRepo(config=config, credential=credential)
                elif config.local_storage_dir:
                    self._write_repo = LocalFileVignetteRepo(config=config)
                else:
                    LOGGER.debug("VignetteFunctionMiddleware: no write backend configured, skipping writes.")
            except Exception as e:
                LOGGER.warning("VignetteFunctionMiddleware: write repo unavailable: %s", e)

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        """
        Intercept a function/tool invocation.

        Pre-call: validate hard anti-pattern constraints.
        Post-call: apply repair templates on failure; write vignettes on success.
        """
        tool_name = context.function.name
        args: Dict[str, Any] = dict(context.arguments)

        # --- Pre-call: check hard constraints ---
        if self._search_repo:
            try:
                guardrail_vignettes = await asyncio.to_thread(
                    self._search_repo.search_vignettes,
                    f"anti-pattern constraints for {tool_name}",
                    tool_name,
                    "anti_pattern",
                    None,
                    self._tenant_id,
                    self._user_id,
                )
                violations = _check_hard_violations(guardrail_vignettes, args)
                if violations:
                    violation_text = "\n".join(violations)
                    LOGGER.warning("Hard constraint violation(s) for %s:\n%s", tool_name, violation_text)
                    # Block execution — raise MiddlewareTermination with guardrail message
                    result_text = (
                        f"Tool call blocked by hard guardrail constraints:\n{violation_text}\n"
                        "Please revise the tool arguments to comply with the listed constraints."
                    )
                    raise MiddlewareTermination(reason="Hard constraint violated", result=result_text)
            except MiddlewareTermination:
                raise
            except Exception as e:
                LOGGER.warning("Pre-call guardrail check failed for %s: %s", tool_name, e)

        # --- Execute the tool call ---
        original_args = dict(args)
        call_error: Optional[Exception] = None
        try:
            await call_next()
        except Exception as exc:
            call_error = exc

        if call_error is None:
            # Success — nothing to repair
            return

        # --- Post-call: apply repair templates ---
        error_class = _extract_error_class(call_error)
        error_message = str(call_error)
        LOGGER.info("Tool %s failed with %s; looking up repair templates.", tool_name, error_class)

        if not self._search_repo:
            raise call_error

        repair_vignettes = []
        try:
            repair_vignettes = await asyncio.to_thread(
                self._search_repo.search_vignettes,
                f"repair {tool_name} after {error_class}",
                tool_name,
                "repair_template",
                error_class,
                self._tenant_id,
                self._user_id,
            )
        except Exception as e:
            LOGGER.warning("Failed to fetch repair templates for %s: %s", tool_name, e)

        if not repair_vignettes:
            raise call_error

        # Try each repair template (bounded by max_retries)
        repair_block = render_repair_block(repair_vignettes)
        LOGGER.info("Applying repair playbook for %s:\n%s", tool_name, repair_block)

        last_error = call_error
        repair_succeeded = False
        applied_steps: List[str] = []
        actual_patched_args: Dict[str, Any] = dict(original_args)

        for vignette in repair_vignettes:
            if vignette.repair is None:
                continue
            for attempt in range(vignette.repair.max_retries):
                try:
                    # Apply patched args example as the new arguments if available
                    if vignette.repair.patched_args_example:
                        patched = dict(vignette.repair.patched_args_example)
                        context.arguments = patched
                        actual_patched_args = patched

                    await call_next()
                    repair_succeeded = True
                    applied_steps = vignette.repair.steps
                    # Capture the args actually in effect after a successful repair
                    actual_patched_args = dict(context.arguments)
                    LOGGER.info(
                        "Repair succeeded for %s on attempt %d/%d",
                        tool_name,
                        attempt + 1,
                        vignette.repair.max_retries,
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    should_stop = any(stop_kw in str(exc) for stop_kw in (vignette.repair.stop_if or []))
                    if should_stop:
                        LOGGER.info("Stopping repair loop due to stop_if condition: %s", exc)
                        break

            if repair_succeeded:
                break

        if not repair_succeeded:
            raise last_error

        # --- Write vignette on successful repair ---
        if self._write_vignettes and self._write_repo and applied_steps:
            try:
                new_vignettes = compile_vignettes(
                    tool_name=tool_name,
                    original_args=original_args,
                    patched_args=actual_patched_args,
                    error_class=error_class,
                    error_message=error_message,
                    repair_steps=applied_steps,
                    scope="user" if self._user_id else ("org" if self._tenant_id else "global"),
                    tenant_id=self._tenant_id,
                    user_id=self._user_id,
                )
                for v in new_vignettes:
                    try:
                        await asyncio.to_thread(self._write_repo.upsert_vignette, v)
                    except Exception as e:
                        LOGGER.warning("Failed to write vignette %s: %s", v.vignette_id, e)
            except Exception as e:
                LOGGER.warning("Vignette compilation failed after repair: %s", e)
