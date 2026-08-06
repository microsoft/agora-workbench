"""CLI entrypoint for connector servers.

Reads configuration from environment variables and starts the appropriate
connector mode (router, gateway, or dispatcher).

Environment variables:
    CONNECTOR_MODE: "router" (default), "gateway", or "dispatcher"
    UPSTREAM_<NAME>_URL: Base URL for each upstream (e.g., UPSTREAM_CHEMISTRY_URL)
    WORKER_<NAME>_URL: Base URL for each worker in dispatcher mode (e.g., WORKER_CHEM1_URL)
    WORKER_<NAME>_WEIGHT: (Optional) Routing weight for a worker (default: 1)
    ENTRA_CLIENT_ID: (Optional) Entra ID client ID for auth
    ENTRA_TENANT_ID: (Optional) Entra tenant ID for auth
    CONNECTOR_AUTH_FACTORY: (Optional) "module.path:factory" returning a custom AuthConfig.
        Takes precedence over ENTRA_CLIENT_ID/ENTRA_TENANT_ID.
    CONNECTOR_ALLOW_NOOP_AUTH: (Optional) Set to 1 to start with authentication disabled when no
        other backend is configured. Local development only; without it an unconfigured
        connector refuses to start rather than running unprotected.
    CONNECTOR_NAME: (Optional) Server name (defaults to "connector")
    CONNECTOR_PORT: (Optional) HTTP port (defaults to 8000)
    GATEWAY_BLOCKED_TOOLS: (Optional) Comma-separated blocked tool names
    GATEWAY_MAX_CALLS_PER_MINUTE: (Optional) Rate limit for gateway mode
    DISPATCHER_STRATEGY: (Optional) Routing strategy: "round_robin" (default), "least_loaded", "sticky_session"
    DISPATCHER_HEALTH_CHECK_INTERVAL: (Optional) Seconds between health polls (default: 10)
    DISPATCHER_FAILURE_POLICY: (Optional) "error" (default) or "reroute"
"""

import importlib
import logging
import os
import re
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from agora_workbench.code_execution.auth import AuthConfig

LOGGER = logging.getLogger(__name__)

_UPSTREAM_URL_PATTERN = re.compile(r"^UPSTREAM_([A-Za-z][A-Za-z0-9_]*)_URL$")
_WORKER_URL_PATTERN = re.compile(r"^WORKER_([A-Za-z][A-Za-z0-9_]*)_URL$")
_WORKER_WEIGHT_PATTERN = re.compile(r"^WORKER_([A-Za-z][A-Za-z0-9_]*)_WEIGHT$")
_SAFE_NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")

AUTH_FACTORY_ENV_VAR = "CONNECTOR_AUTH_FACTORY"
"""Environment variable naming a ``"module.path:factory"`` that returns an ``AuthConfig``."""

ALLOW_NOOP_AUTH_ENV_VAR = "CONNECTOR_ALLOW_NOOP_AUTH"
"""Environment variable that must be truthy to start the connector with authentication disabled."""

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSEY_VALUES = frozenset({"", "0", "false", "no", "off"})


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


def parse_workers_from_env() -> list[tuple[str, str, int]]:
    """Discover WORKER_<NAME>_URL env vars and return (name, url, weight) tuples.

    Also checks for optional WORKER_<NAME>_WEIGHT env vars.
    Names are lowercased for consistency. Results are sorted by name.
    """
    workers: dict[str, tuple[str, int]] = {}
    for key, value in sorted(os.environ.items()):
        match = _WORKER_URL_PATTERN.match(key)
        if match:
            name = match.group(1).lower()
            if not value.strip():
                raise ConfigError(f"Environment variable {key} is set but empty")
            workers[name] = (value.strip(), 1)

    # Apply weights
    for key, value in os.environ.items():
        match = _WORKER_WEIGHT_PATTERN.match(key)
        if match:
            name = match.group(1).lower()
            if name not in workers:
                raise ConfigError(
                    f"WORKER_{name.upper()}_WEIGHT is set but no corresponding WORKER_{name.upper()}_URL found"
                )
            try:
                weight = int(value.strip())
                if weight < 1:
                    raise ValueError("must be >= 1")
            except ValueError as exc:
                raise ConfigError(
                    f"Invalid WORKER_{name.upper()}_WEIGHT='{value}'. Must be a positive integer."
                ) from exc
            url = workers[name][0]
            workers[name] = (url, weight)

    return sorted([(name, url, weight) for name, (url, weight) in workers.items()])


def build_config():
    """Build the appropriate connector config from environment variables.

    Returns a (config, mode) tuple where config is a RouterConfig,
    GatewayConfig, or DispatcherConfig.
    """
    from .models import (
        DispatcherConfig,
        GatewayConfig,
        GatewayPolicy,
        RouterConfig,
        UpstreamConfig,
        WorkerConfig,
    )

    mode = os.getenv("CONNECTOR_MODE", "router").strip().lower()
    server_name = os.getenv("CONNECTOR_NAME", "connector").strip()
    entra_client_id = os.getenv("ENTRA_CLIENT_ID")
    entra_tenant_id = os.getenv("ENTRA_TENANT_ID")

    if mode == "dispatcher":
        workers = parse_workers_from_env()
        if not workers:
            raise ConfigError(
                "No workers configured for dispatcher mode. Set at least one WORKER_<NAME>_URL "
                "environment variable (e.g., WORKER_CHEM1_URL=http://chem-worker-1:8000)"
            )

        worker_configs = [WorkerConfig(name=name, url=url, weight=weight) for name, url, weight in workers]

        strategy = os.getenv("DISPATCHER_STRATEGY", "round_robin").strip().lower()
        valid_strategies = ("round_robin", "least_loaded", "sticky_session")
        if strategy not in valid_strategies:
            raise ConfigError(
                f"Invalid DISPATCHER_STRATEGY='{strategy}'. Must be one of: {', '.join(valid_strategies)}"
            )

        health_interval_raw = os.getenv("DISPATCHER_HEALTH_CHECK_INTERVAL", "10")
        try:
            health_interval = float(health_interval_raw)
            if health_interval <= 0:
                raise ValueError("must be > 0")
        except ValueError as exc:
            raise ConfigError(
                f"Invalid DISPATCHER_HEALTH_CHECK_INTERVAL='{health_interval_raw}'. Must be a positive number."
            ) from exc

        failure_policy = os.getenv("DISPATCHER_FAILURE_POLICY", "error").strip().lower()
        if failure_policy not in ("error", "reroute"):
            raise ConfigError(f"Invalid DISPATCHER_FAILURE_POLICY='{failure_policy}'. Must be 'error' or 'reroute'.")

        return DispatcherConfig(
            name=server_name,
            workers=worker_configs,
            strategy=strategy,
            health_check_interval=health_interval,
            worker_failure_policy=failure_policy,
            entra_client_id=entra_client_id,
            entra_tenant_id=entra_tenant_id,
        ), None

    # Router/Gateway modes use UPSTREAM_<NAME>_URL vars
    upstreams = parse_upstreams_from_env()
    validate_upstream_names(upstreams)

    if not upstreams:
        raise ConfigError(
            "No upstream servers configured. Set at least one UPSTREAM_<NAME>_URL "
            "environment variable (e.g., UPSTREAM_CHEMISTRY_URL=http://chemistry:8000/mcp)"
        )

    upstream_configs = [UpstreamConfig(name=name, url=url) for name, url in upstreams]

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
        raise ConfigError(f"Invalid CONNECTOR_MODE='{mode}'. Must be 'router', 'gateway', or 'dispatcher'.")


def _env_flag(name: str) -> bool:
    """Read a boolean environment variable, rejecting values that aren't clearly true or false."""
    raw = os.getenv(name)
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in _TRUTHY_VALUES:
        return True
    if value in _FALSEY_VALUES:
        return False
    raise ConfigError(f"Invalid {name}='{raw}'. Must be one of: 1/0, true/false, yes/no, on/off.")


def resolve_auth_factory(spec: str) -> Callable[[], "AuthConfig"]:
    """Resolve a ``"module.path:attribute"`` spec to a zero-argument callable.

    The attribute may be dotted (e.g. ``"my_pkg.auth:Backend.create"``) to reach a
    classmethod or other nested attribute.

    Raises:
        ConfigError: If the spec is malformed, the module can't be imported, the
            attribute doesn't exist, or the resolved object isn't callable.
    """
    target = spec.strip()
    module_path, separator, attribute_path = target.partition(":")
    module_path = module_path.strip()
    attribute_path = attribute_path.strip()

    if not separator or ":" in attribute_path or not module_path or not attribute_path:
        raise ConfigError(
            f"Invalid auth factory spec '{spec}'. Expected 'module.path:factory_name', "
            "with exactly one ':' separating the module from the attribute."
        )

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise ConfigError(
            f"Could not import module '{module_path}' for auth factory '{spec}': {exc}. "
            "Ensure the package providing it is installed in the connector's environment."
        ) from exc

    resolved: object = module
    traversed = module_path
    for part in attribute_path.split("."):
        try:
            resolved = getattr(resolved, part)
        except AttributeError as exc:
            raise ConfigError(f"Auth factory '{spec}' not found: '{traversed}' has no attribute '{part}'.") from exc
        traversed = f"{traversed}.{part}"

    if not callable(resolved):
        raise ConfigError(f"Auth factory '{spec}' resolved to a {type(resolved).__name__}, which is not callable.")

    return cast("Callable[[], AuthConfig]", resolved)


def build_auth_config(factory: Callable[[], "AuthConfig"] | None = None) -> "AuthConfig":
    """Build the auth config the connector server runs with.

    Backends are selected in this order:

    1. ``factory``, when the caller passes one (see :func:`main`).
    2. The ``CONNECTOR_AUTH_FACTORY`` environment variable.
    3. Entra ID, when ``ENTRA_CLIENT_ID`` and ``ENTRA_TENANT_ID`` are both set.
    4. No-op auth, but only when ``CONNECTOR_ALLOW_NOOP_AUTH`` is truthy.

    Raises:
        ConfigError: If no backend is configured, or the configured one is unusable.
            Running unauthenticated is never the silent default -- it has to be
            requested explicitly so a missing or misspelled variable fails startup
            instead of starting the connector unprotected.
    """
    from agora_workbench.code_execution.auth import AuthConfig, create_entra_auth_config, create_noop_auth_config

    spec_raw = os.getenv(AUTH_FACTORY_ENV_VAR)
    spec = spec_raw.strip() if spec_raw else ""
    source = ""

    if factory is not None:
        if spec:
            LOGGER.warning(
                "Ignoring %s='%s' because an auth config factory was supplied in-process by the caller.",
                AUTH_FACTORY_ENV_VAR,
                spec,
            )
        source = "the factory supplied by the caller"
    elif spec:
        factory = resolve_auth_factory(spec)
        source = f"{AUTH_FACTORY_ENV_VAR}='{spec}'"

    if factory is not None:
        LOGGER.info("Configuring auth from %s", source)
        try:
            auth_config = factory()
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f"Auth factory from {source} raised {type(exc).__name__}: {exc}") from exc

        if not isinstance(auth_config, AuthConfig):
            raise ConfigError(
                f"Auth factory from {source} returned a {type(auth_config).__name__}, expected an AuthConfig."
            )
        return auth_config

    entra_client_id = os.getenv("ENTRA_CLIENT_ID")
    entra_tenant_id = os.getenv("ENTRA_TENANT_ID")

    if entra_client_id and entra_tenant_id:
        LOGGER.info("Configuring Entra ID auth (client_id=%s)", entra_client_id)
        return create_entra_auth_config(
            client_id=entra_client_id,
            tenant_id=entra_tenant_id,
        )

    if entra_client_id or entra_tenant_id:
        missing = "ENTRA_TENANT_ID" if entra_client_id else "ENTRA_CLIENT_ID"
        present = "ENTRA_CLIENT_ID" if entra_client_id else "ENTRA_TENANT_ID"
        raise ConfigError(
            f"{present} is set but {missing} is not. Entra ID auth requires both. "
            "Set the missing variable, or configure a different backend."
        )

    if _env_flag(ALLOW_NOOP_AUTH_ENV_VAR):
        LOGGER.warning(
            "%s is set — starting with no-op auth. Every request is accepted without token validation. "
            "This is only appropriate for local development.",
            ALLOW_NOOP_AUTH_ENV_VAR,
        )
        return create_noop_auth_config()

    raise ConfigError(
        "No authentication backend is configured. Set ENTRA_CLIENT_ID and ENTRA_TENANT_ID to use Entra ID, "
        f"or {AUTH_FACTORY_ENV_VAR}='module.path:factory' to supply a custom AuthConfig. "
        f"To run with authentication disabled (local development only), set {ALLOW_NOOP_AUTH_ENV_VAR}=1."
    )


def main(auth_config_factory: Callable[[], "AuthConfig"] | None = None) -> None:
    """Main entrypoint for the connector server CLI.

    Args:
        auth_config_factory: Optional zero-argument callable returning the ``AuthConfig``
            to run with. Lets a downstream package ship a thin console script that reuses
            this CLI's environment parsing and server selection instead of forking it.
            When omitted, the backend is selected from the environment -- see
            :func:`build_auth_config`.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        config_a, config_b = build_config()
        port_raw = os.getenv("CONNECTOR_PORT", os.getenv("MCP_SERVER_PORT", "8000"))
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ConfigError(f"Invalid CONNECTOR_PORT/MCP_SERVER_PORT='{port_raw}'. Must be an integer.") from exc
        auth_config = build_auth_config(auth_config_factory)
    except ConfigError as exc:
        LOGGER.error("Configuration error: %s", exc)
        sys.exit(1)

    import asyncio

    from .models import DispatcherConfig, RouterConfig

    if isinstance(config_a, DispatcherConfig):
        from .dispatcher import DispatcherServer

        LOGGER.info(
            "Starting DispatcherServer '%s' with %d worker(s), strategy=%s on port %d",
            config_a.name,
            len(config_a.workers),
            config_a.strategy,
            port,
        )
        server = DispatcherServer(config_a, auth_config=auth_config)
        asyncio.run(server.run_http(port=port))
    elif isinstance(config_a, RouterConfig):
        from .router import RouterServer

        LOGGER.info(
            "Starting RouterServer '%s' with %d upstream(s) on port %d",
            config_a.name,
            len(config_a.upstreams),
            port,
        )
        server = RouterServer(config_a, auth_config=auth_config)
        asyncio.run(server.run_http(port=port))
    else:
        from .gateway import GatewayServer

        assert config_b is not None
        LOGGER.info(
            "Starting GatewayServer '%s' proxying '%s' on port %d",
            config_b.name,
            config_b.upstream.name,
            port,
        )
        server = GatewayServer(config_b, auth_config=auth_config)
        asyncio.run(server.run_http(port=port))
