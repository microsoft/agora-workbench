"""
ToolLearningMiddleware: server-side middleware that learns from domain tool failures.

Intercepts ``execute_*_code`` tool results on the MCP server, inspects the
embedded ``ToolCallRecord`` objects, and:

  Post-call (on domain-tool failure):
    Fetches repair-template vignettes from Azure AI Search for the failing
    inner tool and appends guidance to the response so the agent can self-correct.

  Post-call (on domain-tool success after prior failure):
    Compiles the fail/fix pair into vignette(s) and upserts them to Azure
    Table Storage for future retrieval.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from .code_execution_models import CodeExecutionResult, ToolCallRecord
from .sessions import get_current_user_identity, get_current_token_claims

if TYPE_CHECKING:
    from .server import CodeExecutionServer

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inline config so the server package has no dependency on middleware.*
# ---------------------------------------------------------------------------


@dataclass
class ToolLearningConfig:
    """Configuration for the tool-learning module (server-side copy)."""

    table_storage_endpoint: str = field(default_factory=lambda: os.getenv("TOOL_LEARNING_TABLE_ENDPOINT", ""))
    table_name: str = field(default_factory=lambda: os.getenv("TOOL_LEARNING_TABLE_NAME", "ToolVignettes"))
    search_endpoint: str = field(default_factory=lambda: os.getenv("TOOL_LEARNING_SEARCH_ENDPOINT", ""))
    search_index_name: str = field(default_factory=lambda: os.getenv("TOOL_LEARNING_SEARCH_INDEX", "tool-vignettes"))
    top_k: int = field(default_factory=lambda: int(os.getenv("TOOL_LEARNING_TOP_K", "5")))
    min_confidence: float = field(default_factory=lambda: float(os.getenv("TOOL_LEARNING_MIN_CONFIDENCE", "0.0")))

    @classmethod
    def from_env(cls) -> "ToolLearningConfig":
        return cls()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_code_execution_tool(tool_name: str) -> bool:
    """Check if a tool name matches the execute_*_code pattern."""
    return tool_name.startswith("execute_") and tool_name.endswith("_code")


def _parse_execution_result(result: ToolResult) -> Optional[CodeExecutionResult]:
    """Extract CodeExecutionResult from a ToolResult's text content."""
    for block in result.content:
        if isinstance(block, TextContent) and block.text:
            try:
                data = json.loads(block.text)
                return CodeExecutionResult.model_validate(data)
            except Exception:
                continue
    return None


def _extract_error_class(error: str) -> str:
    """Extract a stable error class from an error message string."""
    for line in reversed(error.strip().splitlines()):
        line = line.strip()
        if ":" in line and not line.startswith(" "):
            candidate = line.split(":")[0].strip()
            if candidate and candidate[0].isupper() and " " not in candidate:
                return candidate
    return "UnknownError"


# ---------------------------------------------------------------------------
# Lazy import helpers for middleware.tool_learning (optional dependency)
# ---------------------------------------------------------------------------


def _import_search_repo():
    from middleware.tool_learning.search_repo import SearchVignetteRepo

    return SearchVignetteRepo


def _import_table_repo():
    from middleware.tool_learning.table_repo import TableVignetteRepo

    return TableVignetteRepo


def _import_render():
    from middleware.tool_learning.render import render_repair_block

    return render_repair_block


def _import_compile():
    from middleware.tool_learning.compile import compile_vignettes

    return compile_vignettes


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class ToolLearningMiddleware(Middleware):
    """
    FastMCP middleware that learns from domain tool call failures.

    Inspects ToolCallRecord objects embedded in execute_*_code results to
    detect inner domain tool failures, fetch repair guidance, and compile
    new vignettes from successful repairs.

    Tracks per-session failure history so that when a previously-failed
    tool call succeeds, the fail/fix pair can be compiled into a vignette.
    """

    MAX_SESSIONS = 100  # Cap on tracked sessions to prevent unbounded growth

    def __init__(self, server: "CodeExecutionServer", config, credential=None):
        self.server = server
        self._config = config
        self._search_repo = None
        self._table_repo = None
        self._failure_history: dict[str, dict[str, ToolCallRecord]] = {}

        try:
            SearchVignetteRepo = _import_search_repo()
            self._search_repo = SearchVignetteRepo(config=config, credential=credential)
        except Exception as e:
            LOGGER.warning("ToolLearningMiddleware: search repo unavailable: %s", e)

        try:
            TableVignetteRepo = _import_table_repo()
            self._table_repo = TableVignetteRepo(config=config, credential=credential)
        except Exception as e:
            LOGGER.warning("ToolLearningMiddleware: table repo unavailable: %s", e)

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        tool_name = context.message.name

        if not _is_code_execution_tool(tool_name):
            return await call_next(context)

        result = await call_next(context)

        exec_result = _parse_execution_result(result)
        if not exec_result or not exec_result.tool_calls:
            return result

        session_id = None
        if context.fastmcp_context:
            try:
                session_id = context.fastmcp_context.session_id
            except (RuntimeError, AttributeError):
                pass
        session_key = session_id or "default"

        guidance_lines: list[str] = []
        for record in exec_result.tool_calls:
            if not record.success and record.error:
                guidance = await self._handle_failure(session_key, record)
                if guidance:
                    guidance_lines.append(guidance)
            elif record.success:
                await self._handle_success(session_key, record)

        if guidance_lines:
            return self._append_guidance(result, guidance_lines)

        return result

    async def _handle_failure(self, session_key: str, record: ToolCallRecord) -> Optional[str]:
        """Handle a failed inner tool call: store in history and fetch repair advice."""
        error_class = _extract_error_class(record.error or "")
        LOGGER.info(
            "Domain tool '%s' failed with %s; looking up repair templates.",
            record.tool_name,
            error_class,
        )

        if session_key not in self._failure_history:
            # Evict oldest session if at capacity
            if len(self._failure_history) >= self.MAX_SESSIONS:
                oldest = next(iter(self._failure_history))
                del self._failure_history[oldest]
            self._failure_history[session_key] = {}
        self._failure_history[session_key][record.tool_name] = record

        if not self._search_repo:
            return None

        try:
            render_repair_block = _import_render()
            claims = get_current_token_claims()
            user_id = get_current_user_identity()
            tenant_id = claims.get("tid") if claims else None
            repair_vignettes = await asyncio.to_thread(
                self._search_repo.search_vignettes,
                f"repair {record.tool_name} after {error_class}",
                record.tool_name,
                "repair_template",
                error_class,
                tenant_id,
                user_id,
            )
            if repair_vignettes:
                return render_repair_block(repair_vignettes)
        except Exception as e:
            LOGGER.warning(
                "Failed to fetch repair templates for %s: %s",
                record.tool_name,
                e,
            )

        return None

    async def _handle_success(self, session_key: str, record: ToolCallRecord) -> None:
        """Handle a successful tool call: compile vignette if it previously failed."""
        prior_failures = self._failure_history.get(session_key, {})
        failed_record = prior_failures.pop(record.tool_name, None)
        if failed_record is None or not self._table_repo:
            return

        error_class = _extract_error_class(failed_record.error or "")
        LOGGER.info(
            "Domain tool '%s' succeeded after prior %s failure; compiling vignette.",
            record.tool_name,
            error_class,
        )

        claims = get_current_token_claims()
        user_id = get_current_user_identity()
        tenant_id = claims.get("tid") if claims else None

        try:
            compile_vignettes = _import_compile()
            vignettes = compile_vignettes(
                tool_name=record.tool_name,
                original_args=failed_record.args,
                patched_args=record.args,
                error_class=error_class,
                error_message=failed_record.error or "",
                repair_steps=[f"Changed args for keys: {sorted(set(failed_record.args) | set(record.args))}"],
                scope="user" if user_id else ("org" if tenant_id else "global"),
                tenant_id=tenant_id,
                user_id=user_id,
            )
            for v in vignettes:
                try:
                    await asyncio.to_thread(self._table_repo.upsert_vignette, v)
                    LOGGER.info(
                        "Upserted vignette %s for tool %s",
                        v.vignette_id,
                        record.tool_name,
                    )
                except Exception as e:
                    LOGGER.warning("Failed to upsert vignette %s: %s", v.vignette_id, e)
        except Exception as e:
            LOGGER.warning("Vignette compilation failed: %s", e)

    @staticmethod
    def _append_guidance(result: ToolResult, guidance_lines: list[str]) -> ToolResult:
        """Append repair guidance text to the tool result."""
        guidance_text = "\n\n--- Tool Learning: Repair Guidance ---\n" + "\n".join(guidance_lines)
        new_content = list(result.content) + [TextContent(type="text", text=guidance_text)]
        return ToolResult(
            content=new_content,
            structured_content=result.structured_content,
            meta=result.meta,
        )
