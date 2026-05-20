"""
Registry-driven MCP server connection helper.

Reads ``server_registry.yaml``, probes each entry's ``/health`` endpoint,
and returns framework-neutral connection metadata. Users wrap the
returned :class:`McpServer` entries in their agent framework's MCP tool
type with a one-line list comprehension.

This module deliberately has **no agent-framework imports** — neither
MAF nor Semantic Kernel nor LangGraph. The platform provides the boring
parts (registry, health probe, auth headers); each framework's wrapper
is one constructor call the user writes themselves.

Example
-------

MAF (``agent_framework.MCPStreamableHTTPTool``)::

    from agent_framework import MCPStreamableHTTPTool
    from utilities.mcp_clients import connect_mcp_servers

    servers = await connect_mcp_servers(servers=["earthscience"])
    tools = [
        MCPStreamableHTTPTool(
            name=s.name, url=s.url, http_client=s.http_client,
            approval_mode="never_require",
        )
        for s in servers
    ]

Raw MCP::

    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    servers = await connect_mcp_servers(...)
    # open a ClientSession per server with s.http_client
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
import yaml

LOGGER = logging.getLogger(__name__)

_DEFAULT_REGISTRY = Path(__file__).resolve().parent.parent / "server_registry.yaml"
_ENV_OVERRIDE = "WORKBENCH_SERVERS"
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_DEV_BEARER = "dev-token"


@dataclass
class McpServer:
    """Framework-neutral metadata for one live MCP server connection.

    Attributes
    ----------
    name : str
        The server's registry name (e.g. ``"earthscience"``).
    url : str
        Streamable-HTTP MCP endpoint, e.g. ``http://localhost:8021/mcp``.
    http_client : httpx.AsyncClient
        Pre-configured client with the right ``Authorization`` header.
        Localhost entries use a dev bearer token; non-localhost entries
        use :func:`utilities.auth.get_token_provider` against the entry's
        scope. **The caller is responsible for closing the client** when
        the agent shuts down.
    """

    name: str
    url: str
    http_client: httpx.AsyncClient


def _load_registry(registry_path: Path) -> dict:
    """Load and validate the registry yaml. Raises if malformed."""
    if not registry_path.is_file():
        raise FileNotFoundError(f"Server registry not found: {registry_path}")
    with registry_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Registry root must be a mapping: {registry_path}")
    servers = data.get("servers", [])
    if not isinstance(servers, list):
        raise ValueError(f"Registry 'servers' must be a list: {registry_path}")
    return data


def _select(
    servers_arg: Optional[list[str]],
    available: list[dict],
) -> list[dict]:
    """Filter registry entries by name. Env var overrides the argument."""
    env_value = os.environ.get(_ENV_OVERRIDE, "").strip()
    if env_value:
        wanted = [s.strip() for s in env_value.split(",") if s.strip()]
    elif servers_arg is not None:
        wanted = list(servers_arg)
    else:
        return available

    by_name = {entry["name"]: entry for entry in available if "name" in entry}
    selected: list[dict] = []
    for name in wanted:
        if name in by_name:
            selected.append(by_name[name])
        else:
            LOGGER.warning("Requested MCP server %r not in registry; skipping.", name)
    return selected


def _is_local(url: str) -> bool:
    host = httpx.URL(url).host or ""
    return host in _LOCAL_HOSTS


def _build_http_client(url: str, scope: Optional[str]) -> httpx.AsyncClient:
    """Pick auth: dev bearer for localhost, token provider otherwise."""
    if _is_local(url):
        return httpx.AsyncClient(headers={"Authorization": f"Bearer {_DEV_BEARER}"})

    if not scope:
        raise ValueError(
            f"Non-localhost MCP server {url!r} requires a 'scope' in the registry "
            "(per-server or top-level fallback)."
        )

    from utilities.auth import BearerTokenAuth, get_token_provider

    token_provider = get_token_provider(scope)
    return httpx.AsyncClient(auth=BearerTokenAuth(token_provider))


async def _is_healthy(url: str, http_client: httpx.AsyncClient, timeout: float) -> bool:
    """Probe ``{base}/health`` (derived by stripping a trailing ``/mcp``)."""
    health_url = url.rsplit("/mcp", 1)[0] + "/health"
    try:
        resp = await http_client.get(health_url, timeout=timeout)
        resp.raise_for_status()
        return True
    except Exception as exc:
        LOGGER.warning("MCP server unreachable at %s (%s) — skipping.", health_url, exc)
        return False


async def connect_mcp_servers(
    servers: Optional[list[str]] = None,
    registry_path: Optional[Path] = None,
    timeout: float = 2.0,
) -> list[McpServer]:
    """Probe registry-listed MCP servers and return live connections.

    Parameters
    ----------
    servers : list[str] | None
        Filter by registry name. ``None`` returns every entry.
        Overridden by the ``WORKBENCH_SERVERS`` env var (comma-separated
        names) when set.
    registry_path : Path | None
        Path to the registry yaml. Defaults to ``src/server_registry.yaml``
        relative to this file.
    timeout : float
        Per-server health-probe timeout in seconds.

    Returns
    -------
    list[McpServer]
        One entry per server that responded to ``/health`` with 2xx.
        Unreachable servers are skipped with a WARNING log line; this
        function never raises for transport errors.
    """
    path = registry_path or _DEFAULT_REGISTRY
    registry = _load_registry(path)
    default_scope = registry.get("scope")
    available: list[dict] = registry.get("servers", [])

    selected = _select(servers, available)
    if not selected:
        LOGGER.info("No MCP servers selected from registry %s.", path)
        return []

    live: list[McpServer] = []
    for entry in selected:
        name = entry.get("name")
        url = entry.get("url")
        if not name or not url:
            LOGGER.warning("Registry entry missing 'name' or 'url'; skipping: %r", entry)
            continue
        scope = entry.get("scope") or default_scope
        http_client = _build_http_client(url, scope)

        if not await _is_healthy(url, http_client, timeout):
            await http_client.aclose()
            continue

        LOGGER.info("Connected to MCP server %r at %s", name, url)
        live.append(McpServer(name=name, url=url, http_client=http_client))

    return live
