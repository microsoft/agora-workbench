"""
Explicit-input MCP server connection helper.

The platform's job is to do the boring per-server plumbing — probe
``/health``, pick the right auth headers, hand back a configured
``httpx.AsyncClient``. It deliberately has *no opinion* about where the
list of servers comes from: the caller passes
``list[McpServerConfig]`` directly. They can hardcode it, read from a
yaml file (see :func:`load_server_registry`), parse env vars, ingest an
existing app config — any source they like.

This module has **no agent-framework imports** (neither MAF nor Semantic
Kernel nor LangGraph). Wrapping the returned :class:`McpServer` entries
into a framework-specific tool type is a one-line list comprehension the
user writes in their own code.

Examples
--------

Hardcoded inline (simplest case)::

    from utilities.mcp_clients import McpServerConfig, connect_mcp_servers

    servers = await connect_mcp_servers([
        McpServerConfig(name="earthscience", url="http://localhost:8021/mcp"),
    ])

From a yaml file you wrote::

    from utilities.mcp_clients import connect_mcp_servers, load_server_registry

    configs = load_server_registry("my_servers.yaml")
    servers = await connect_mcp_servers(configs)

Wrapping for MAF::

    from agent_framework import MCPStreamableHTTPTool

    tools = [
        MCPStreamableHTTPTool(
            name=s.name, url=s.url, http_client=s.http_client,
            approval_mode="never_require",
        )
        for s in servers
    ]

Why the read timeout matters
----------------------------

MCP's streamable-HTTP transport keeps long-lived SSE streams open and
sits idle between events. httpx's default 5-second read timeout will
kill those streams mid-tool-call. ``MCPStreamableHTTPTool`` only applies
its own SSE-friendly defaults when it constructs the client itself —
when callers pass in an ``http_client`` (as our wrapper does), it is
used verbatim. So the runtime timeout has to be set here.

The default is ``httpx.Timeout(30.0, read=1200.0)`` — 30s connect, 20min
read — which is comfortable for typical notebook-style tools. Override
via the ``client_timeout`` argument to :func:`connect_mcp_servers` when
you need longer reads (slow data fetches, model warmup) or tighter
bounds (production deployments where a hung tool should fail fast).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import httpx
import yaml

LOGGER = logging.getLogger(__name__)

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_DEV_BEARER = "dev-token"


@dataclass
class McpServerConfig:
    """User-supplied description of one MCP server to connect to.

    Attributes
    ----------
    name : str
        Human-friendly identifier used in logs and (typically) as the
        tool name when wrapping into a framework-specific MCP tool.
    url : str
        Streamable-HTTP MCP endpoint, e.g.
        ``"http://localhost:8021/mcp"``. The ``/health`` probe is derived
        by stripping a trailing ``/mcp``.
    scope : str | None
        OAuth scope. Required for non-localhost URLs (used with
        :func:`utilities.auth.get_token_provider`). Ignored for
        localhost URLs, which use a dev bearer token.

    Example::

        McpServerConfig(
            name="earthscience",
            url="http://localhost:8021/mcp",
        )

        McpServerConfig(
            name="chemistry-prod",
            url="https://chemistry.example.com/mcp",
            scope="api://12c90937-013c-468e-93cf-eb8083d69ca7/.default",
        )
    """

    name: str
    url: str
    scope: Optional[str] = None


@dataclass
class McpServer:
    """Framework-neutral metadata for one *live* MCP server connection.

    Returned by :func:`connect_mcp_servers` after a successful health
    probe. Wrap into your agent framework's MCP tool type with a
    one-line list comprehension (see module docstring).

    Attributes
    ----------
    name : str
        Carried through from the input :class:`McpServerConfig`.
    url : str
        Carried through from the input :class:`McpServerConfig`.
    http_client : httpx.AsyncClient
        Pre-configured client with the right ``Authorization`` header
        (dev bearer for localhost, real token provider otherwise). The
        caller is responsible for closing the client when the agent
        shuts down (typically via ``AsyncExitStack`` or the framework's
        own MCP tool lifecycle).
    """

    name: str
    url: str
    http_client: httpx.AsyncClient


def load_server_registry(path: Union[str, Path]) -> list[McpServerConfig]:
    """Parse a yaml file in the documented registry format.

    Convenience for callers who prefer to keep their server list in
    yaml. The platform does not auto-load any file — you pass the path
    explicitly.

    Expected file format::

        # default scope used when an entry omits its own (non-localhost only)
        scope: api://12c90937-013c-468e-93cf-eb8083d69ca7/.default

        servers:
          - name: earthscience
            url: http://localhost:8021/mcp
          - name: chemistry-prod
            url: https://chemistry.example.com/mcp
            scope: api://chemistry/.default       # overrides top-level

    Parameters
    ----------
    path : str | Path
        Path to a yaml file.

    Returns
    -------
    list[McpServerConfig]
        Ready to pass to :func:`connect_mcp_servers`.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist.
    ValueError
        If the file is malformed (root not a mapping, ``servers`` not a
        list, or an entry is missing ``name``/``url``).

    Example::

        configs = load_server_registry("examples/local_servers.yaml")
        servers = await connect_mcp_servers(configs)
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Registry yaml not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Registry root must be a mapping: {p}")
    raw_servers = data.get("servers", [])
    if not isinstance(raw_servers, list):
        raise ValueError(f"Registry 'servers' must be a list: {p}")

    default_scope = data.get("scope")
    configs: list[McpServerConfig] = []
    for entry in raw_servers:
        if not isinstance(entry, dict):
            raise ValueError(f"Registry entry must be a mapping: {entry!r}")
        if "name" not in entry or "url" not in entry:
            raise ValueError(f"Registry entry missing 'name' or 'url': {entry!r}")
        configs.append(
            McpServerConfig(
                name=entry["name"],
                url=entry["url"],
                scope=entry.get("scope") or default_scope,
            )
        )
    return configs


def _is_local(url: str) -> bool:
    host = httpx.URL(url).host or ""
    return host in _LOCAL_HOSTS


# Long read timeout for SSE: the MCP streamable-http transport keeps long-lived
# GET/POST streams that may sit idle between events. httpx's default 5s read
# timeout kills these streams mid-tool-call, and MCPStreamableHTTPTool uses any
# http_client we pass in verbatim without applying its own streaming defaults.
_SSE_TIMEOUT = httpx.Timeout(30.0, read=1200.0)


def _build_http_client(
    url: str,
    scope: Optional[str],
    client_timeout: Optional[httpx.Timeout] = None,
) -> httpx.AsyncClient:
    """Pick auth: dev bearer for localhost, token provider otherwise."""
    timeout = client_timeout if client_timeout is not None else _SSE_TIMEOUT
    if _is_local(url):
        return httpx.AsyncClient(
            headers={"Authorization": f"Bearer {_DEV_BEARER}"},
            timeout=timeout,
        )

    if not scope:
        raise ValueError(
            f"Non-localhost MCP server {url!r} requires a 'scope' on its "
            "McpServerConfig (or in the registry yaml's top-level fallback)."
        )

    from utilities.auth import BearerTokenAuth, get_token_provider

    token_provider = get_token_provider(scope)
    return httpx.AsyncClient(auth=BearerTokenAuth(token_provider), timeout=timeout)


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
    configs: list[McpServerConfig],
    timeout: float = 2.0,
    client_timeout: Optional[httpx.Timeout] = None,
) -> list[McpServer]:
    """Probe a list of MCP servers and return live connections.

    The platform takes the list, probes each ``/health``, picks auth
    headers, and hands back ready-to-use connections. It has no opinion
    about where ``configs`` came from — see :func:`load_server_registry`
    for a yaml loader, or build the list inline / from env vars / from
    your existing app config.

    Parameters
    ----------
    configs : list[McpServerConfig]
        Servers to probe.
    timeout : float
        Per-server health-probe timeout in seconds. Only used for the
        ``/health`` GET that happens during connection setup.
    client_timeout : httpx.Timeout | None
        Timeout configuration applied to the returned ``httpx.AsyncClient``
        used by the MCP streamable-HTTP transport. The ``read`` value caps
        how long the SSE response stream may stay idle between events;
        anything shorter than the longest plausible tool call will hang
        the agent when a tool runs longer than that window (see "Why the
        read timeout matters" in the module docstring).

        Defaults to ``httpx.Timeout(30.0, read=1200.0)`` — 30s connect,
        20min read — which suits typical long-running notebook-style
        tool calls. Override when your tools may run longer, or when you
        want tighter bounds for production deployments.

    Returns
    -------
    list[McpServer]
        One entry per server that responded to ``/health`` with 2xx.
        Unreachable servers are skipped with a WARNING log line; this
        function never raises for transport errors.

    Example::

        from utilities.mcp_clients import McpServerConfig, connect_mcp_servers

        servers = await connect_mcp_servers([
            McpServerConfig(name="earthscience", url="http://localhost:8021/mcp"),
            McpServerConfig(name="chemistry", url="http://localhost:8020/mcp"),
        ])
        for s in servers:
            print(f"connected: {s.name} @ {s.url}")

    Custom timeout::

        import httpx

        servers = await connect_mcp_servers(
            [McpServerConfig(name="earthscience", url="http://localhost:8021/mcp")],
            client_timeout=httpx.Timeout(30.0, read=3600.0),  # 1h read for slow tools
        )
    """
    if not configs:
        LOGGER.info("connect_mcp_servers called with empty config list.")
        return []

    live: list[McpServer] = []
    for cfg in configs:
        http_client = _build_http_client(cfg.url, cfg.scope, client_timeout=client_timeout)
        if not await _is_healthy(cfg.url, http_client, timeout):
            await http_client.aclose()
            continue
        LOGGER.info("Connected to MCP server %r at %s", cfg.name, cfg.url)
        live.append(McpServer(name=cfg.name, url=cfg.url, http_client=http_client))

    return live
