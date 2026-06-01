"""CLI entrypoint for connector servers.

Reads configuration from environment variables and starts the appropriate
connector mode (router or gateway).

Environment variables:
    CONNECTOR_MODE: "router" (default) or "gateway"
    UPSTREAM_<NAME>_URL: Base URL for each upstream (e.g., UPSTREAM_CHEMISTRY_URL)
    ENTRA_CLIENT_ID: (Optional) Entra ID client ID for auth
    ENTRA_TENANT_ID: (Optional) Entra tenant ID for auth
    CONNECTOR_NAME: (Optional) Server name (defaults to "connector")
    CONNECTOR_PORT: (Optional) HTTP port (defaults to 8000)
    GATEWAY_BLOCKED_TOOLS: (Optional) Comma-separated blocked tool names
    GATEWAY_MAX_CALLS_PER_MINUTE: (Optional) Rate limit for gateway mode
"""

import logging
import os
import re
import sys

LOGGER = logging.getLogger(__name__)

_UPSTREAM_URL_PATTERN = re.compile(r"^UPSTREAM_([A-Za-z][A-Za-z0-9_]*)_URL$")
_SAFE_NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


class ConfigError(Exception):
    """Raised when environment configuration is invalid."""


def parse_upstreams_from_env() -> list[tuple[str, str]]:
    """Discover UPSTREAM_<NAME>_URL env vars and return (name, url) pairs.

    Names are lowercased for consistency with the UpstreamConfig model.
    Results are sorted by name for deterministic ordering.
    """
    upstreams: list[tuple[str, str]] = []
    for key, value in sorted(os.environ.items()):
        match = _UPSTREAM_URL_PATTERN.match(key)
        if match:
            name = match.group(1).lower()
            if not value.strip():
                raise ConfigError(f"Environment variable {key} is set but empty")
            upstreams.append((name, value.strip()))
    return sorted(upstreams, key=lambda upstream: upstream[0])


def validate_upstream_names(upstreams: list[tuple[str, str]]) -> None:
    """Validate that upstream names are safe identifiers with no duplicates."""
    seen: set[str] = set()
    for name, _ in upstreams:
        if not _SAFE_NAME_PATTERN.match(name):
            raise ConfigError(f"Upstream name '{name}' is not a valid identifier. Must match [a-zA-Z][a-zA-Z0-9_]*")
        if name in seen:
            raise ConfigError(f"Duplicate upstream name '{name}' (env vars are case-insensitive)")
        seen.add(name)


def build_config():
    """Build the appropriate connector config from environment variables.

    Returns a (RouterConfig, None) or (None, GatewayConfig) tuple depending
    on CONNECTOR_MODE.
    """
    from .models import GatewayConfig, GatewayPolicy, RouterConfig, UpstreamConfig

    mode = os.getenv("CONNECTOR_MODE", "router").strip().lower()
    server_name = os.getenv("CONNECTOR_NAME", "connector").strip()

    upstreams = parse_upstreams_from_env()
    validate_upstream_names(upstreams)

    if not upstreams:
        raise ConfigError(
            "No upstream servers configured. Set at least one UPSTREAM_<NAME>_URL "
            "environment variable (e.g., UPSTREAM_CHEMISTRY_URL=http://chemistry:8000/mcp)"
        )

    upstream_configs = [UpstreamConfig(name=name, url=url) for name, url in upstreams]

    entra_client_id = os.getenv("ENTRA_CLIENT_ID")
    entra_tenant_id = os.getenv("ENTRA_TENANT_ID")

    if mode == "router":
        return RouterConfig(
            name=server_name,
            upstreams=upstream_configs,
            entra_client_id=entra_client_id,
            entra_tenant_id=entra_tenant_id,
        ), None

    elif mode == "gateway":
        if len(upstream_configs) != 1:
            raise ConfigError(
                f"Gateway mode requires exactly one upstream, but found {len(upstream_configs)}: "
                f"{[u.name for u in upstream_configs]}. "
                f"Use CONNECTOR_MODE=router for multiple upstreams."
            )

        blocked_tools_raw = os.getenv("GATEWAY_BLOCKED_TOOLS", "")
        blocked_tools = [t.strip() for t in blocked_tools_raw.split(",") if t.strip()]

        max_calls_raw = os.getenv("GATEWAY_MAX_CALLS_PER_MINUTE")
        if max_calls_raw:
            try:
                max_calls_per_minute = int(max_calls_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"Invalid GATEWAY_MAX_CALLS_PER_MINUTE='{max_calls_raw}'. Must be an integer."
                ) from exc
        else:
            max_calls_per_minute = None

        policy = GatewayPolicy(
            blocked_tools=blocked_tools,
            max_calls_per_minute=max_calls_per_minute,
        )

        return None, GatewayConfig(
            name=server_name,
            upstream=upstream_configs[0],
            policy=policy,
            entra_client_id=entra_client_id,
            entra_tenant_id=entra_tenant_id,
        )

    else:
        raise ConfigError(f"Invalid CONNECTOR_MODE='{mode}'. Must be 'router' or 'gateway'.")


def build_auth_config():
    """Build auth config from environment, defaulting to Entra if credentials present."""
    from code_execution.auth import create_entra_auth_config, create_noop_auth_config

    entra_client_id = os.getenv("ENTRA_CLIENT_ID")
    entra_tenant_id = os.getenv("ENTRA_TENANT_ID")

    if entra_client_id and entra_tenant_id:
        LOGGER.info("Configuring Entra ID auth (client_id=%s)", entra_client_id)
        return create_entra_auth_config(
            client_id=entra_client_id,
            tenant_id=entra_tenant_id,
        )

    LOGGER.warning(
        "No ENTRA_CLIENT_ID/ENTRA_TENANT_ID set — using no-op auth. This is only appropriate for local development."
    )
    return create_noop_auth_config()


def main() -> None:
    """Main entrypoint for the connector server CLI."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        router_config, gateway_config = build_config()
        port_raw = os.getenv("CONNECTOR_PORT", os.getenv("MCP_SERVER_PORT", "8000"))
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ConfigError(f"Invalid CONNECTOR_PORT/MCP_SERVER_PORT='{port_raw}'. Must be an integer.") from exc
    except ConfigError as exc:
        LOGGER.error("Configuration error: %s", exc)
        sys.exit(1)

    auth_config = build_auth_config()

    import asyncio

    if router_config:
        from .router import RouterServer

        LOGGER.info(
            "Starting RouterServer '%s' with %d upstream(s) on port %d",
            router_config.name,
            len(router_config.upstreams),
            port,
        )
        server = RouterServer(router_config, auth_config=auth_config)
        asyncio.run(server.run_http(port=port))
    else:
        from .gateway import GatewayServer

        assert gateway_config is not None
        LOGGER.info(
            "Starting GatewayServer '%s' proxying '%s' on port %d",
            gateway_config.name,
            gateway_config.upstream.name,
            port,
        )
        server = GatewayServer(gateway_config, auth_config=auth_config)
        asyncio.run(server.run_http(port=port))
