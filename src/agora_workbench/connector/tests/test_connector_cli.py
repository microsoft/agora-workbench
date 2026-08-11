"""Tests for connector.cli environment-variable config parsing."""

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agora_workbench.code_execution.auth import create_noop_auth_config

from agora_workbench.connector import cli
from agora_workbench.connector.cli import (
    ConfigError,
    build_auth_config,
    build_config,
    parse_upstreams_from_env,
    parse_workers_from_env,
    resolve_auth_factory,
    validate_upstream_names,
)

_SENTINEL_AUTH_CONFIG = create_noop_auth_config()
"""A real AuthConfig instance used to assert which backend build_auth_config picked."""

_NOT_CALLABLE = "just a string"
"""A non-callable module attribute resolved by name via resolve_auth_factory, not by reference."""


def _make_sentinel_auth_config():
    return _SENTINEL_AUTH_CONFIG


class _FactoryHolder:
    @staticmethod
    def create():
        return _SENTINEL_AUTH_CONFIG


class TestParseUpstreamsFromEnv:
    def test_discovers_upstream_urls(self):
        env = {
            "UPSTREAM_CHEMISTRY_URL": "http://chemistry:8000/mcp",
            "UPSTREAM_GIS_URL": "http://gis:8000/mcp",
            "OTHER_VAR": "ignored",
        }
        with patch.dict(os.environ, env, clear=True):
            result = parse_upstreams_from_env()
        assert result == [
            ("chemistry", "http://chemistry:8000/mcp"),
            ("gis", "http://gis:8000/mcp"),
        ]

    def test_returns_sorted_by_lowercased_name(self):
        env = {
            "UPSTREAM_bETA_URL": "http://b:8000",
            "UPSTREAM_ALPHA_URL": "http://a:8000",
        }
        with patch.dict(os.environ, env, clear=True):
            result = parse_upstreams_from_env()
        assert [name for name, _ in result] == ["alpha", "beta"]

    def test_lowercases_name(self):
        env = {"UPSTREAM_MyService_URL": "http://svc:8000"}
        with patch.dict(os.environ, env, clear=True):
            result = parse_upstreams_from_env()
        assert result[0][0] == "myservice"

    def test_empty_value_raises(self):
        env = {"UPSTREAM_CHEMISTRY_URL": "  "}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigError, match="empty"):
                parse_upstreams_from_env()

    def test_no_upstreams_returns_empty(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            result = parse_upstreams_from_env()
        assert result == []


class TestValidateUpstreamNames:
    def test_valid_names_pass(self):
        validate_upstream_names([("chemistry", "url"), ("gis2", "url")])

    def test_duplicate_names_raise(self):
        with pytest.raises(ConfigError, match="Duplicate"):
            validate_upstream_names([("chemistry", "url1"), ("chemistry", "url2")])

    def test_invalid_name_raises(self):
        with pytest.raises(ConfigError, match="not a valid identifier"):
            validate_upstream_names([("123bad", "url")])

    def test_name_starting_with_underscore_raises(self):
        with pytest.raises(ConfigError, match="not a valid identifier"):
            validate_upstream_names([("_private", "url")])


class TestBuildConfig:
    def test_router_mode_with_multiple_upstreams(self):
        env = {
            "CONNECTOR_MODE": "router",
            "CONNECTOR_NAME": "science-hub",
            "UPSTREAM_CHEMISTRY_URL": "http://chemistry:8000/mcp",
            "UPSTREAM_GIS_URL": "http://gis:8000/mcp",
        }
        with patch.dict(os.environ, env, clear=True):
            router_config, gateway_config = build_config()

        assert gateway_config is None
        assert router_config is not None
        assert router_config.name == "science-hub"
        assert len(router_config.upstreams) == 2
        assert router_config.upstreams[0].name == "chemistry"
        assert router_config.upstreams[1].name == "gis"

    def test_router_mode_is_default(self):
        env = {
            "UPSTREAM_CHEMISTRY_URL": "http://chemistry:8000/mcp",
        }
        with patch.dict(os.environ, env, clear=True):
            router_config, gateway_config = build_config()

        assert router_config is not None
        assert gateway_config is None

    def test_gateway_mode_with_one_upstream(self):
        env = {
            "CONNECTOR_MODE": "gateway",
            "UPSTREAM_CHEMISTRY_URL": "http://chemistry:8000/mcp",
            "GATEWAY_BLOCKED_TOOLS": "parallel_execute,dangerous_tool",
            "GATEWAY_MAX_CALLS_PER_MINUTE": "60",
        }
        with patch.dict(os.environ, env, clear=True):
            router_config, gateway_config = build_config()

        assert router_config is None
        assert gateway_config is not None
        assert gateway_config.upstream.name == "chemistry"
        assert gateway_config.policy.blocked_tools == ["parallel_execute", "dangerous_tool"]
        assert gateway_config.policy.max_calls_per_minute == 60

    def test_gateway_mode_with_multiple_upstreams_raises(self):
        env = {
            "CONNECTOR_MODE": "gateway",
            "UPSTREAM_CHEMISTRY_URL": "http://chemistry:8000/mcp",
            "UPSTREAM_GIS_URL": "http://gis:8000/mcp",
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigError, match="exactly one upstream"):
                build_config()

    def test_invalid_gateway_max_calls_raises(self):
        env = {
            "CONNECTOR_MODE": "gateway",
            "UPSTREAM_CHEMISTRY_URL": "http://chemistry:8000/mcp",
            "GATEWAY_MAX_CALLS_PER_MINUTE": "abc",
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigError, match="GATEWAY_MAX_CALLS_PER_MINUTE"):
                build_config()

    def test_no_upstreams_raises(self):
        env = {"CONNECTOR_MODE": "router"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigError, match="No upstream servers"):
                build_config()

    def test_invalid_mode_raises(self):
        env = {
            "CONNECTOR_MODE": "invalid",
            "UPSTREAM_X_URL": "http://x:8000",
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigError, match="Invalid CONNECTOR_MODE"):
                build_config()

    def test_entra_ids_passed_through(self):
        env = {
            "UPSTREAM_CHEMISTRY_URL": "http://chemistry:8000/mcp",
            "ENTRA_CLIENT_ID": "my-client-id",
            "ENTRA_TENANT_ID": "my-tenant-id",
        }
        with patch.dict(os.environ, env, clear=True):
            router_config, _ = build_config()

        assert router_config.entra_client_id == "my-client-id"
        assert router_config.entra_tenant_id == "my-tenant-id"

    def test_mode_is_case_insensitive(self):
        env = {
            "CONNECTOR_MODE": "ROUTER",
            "UPSTREAM_X_URL": "http://x:8000",
        }
        with patch.dict(os.environ, env, clear=True):
            router_config, _ = build_config()
        assert router_config is not None


class TestResolveAuthFactory:
    def test_resolves_module_attribute(self):
        resolved = resolve_auth_factory("agora_workbench.code_execution.auth:create_noop_auth_config")
        from agora_workbench.code_execution.auth import create_noop_auth_config

        assert resolved is create_noop_auth_config

    def test_resolves_dotted_attribute_path(self):
        resolved = resolve_auth_factory(f"{__name__}:_FactoryHolder.create")
        assert resolved() is _SENTINEL_AUTH_CONFIG

    def test_strips_surrounding_whitespace(self):
        resolved = resolve_auth_factory("  agora_workbench.code_execution.auth:create_noop_auth_config  ")
        assert callable(resolved)

    @pytest.mark.parametrize("spec", ["no_colon_here", ":create", "module:", "a:b:c", "   ", ""])
    def test_rejects_malformed_spec(self, spec):
        with pytest.raises(ConfigError, match="Invalid auth factory spec"):
            resolve_auth_factory(spec)

    def test_rejects_unimportable_module(self):
        with pytest.raises(ConfigError, match="Could not import module 'definitely_not_a_real_pkg'"):
            resolve_auth_factory("definitely_not_a_real_pkg:create")

    def test_rejects_missing_attribute(self):
        with pytest.raises(ConfigError, match="has no attribute 'not_there'"):
            resolve_auth_factory(f"{__name__}:not_there")

    def test_rejects_non_callable_attribute(self):
        assert not callable(_NOT_CALLABLE)
        with pytest.raises(ConfigError, match="not callable"):
            resolve_auth_factory(f"{__name__}:_NOT_CALLABLE")


class TestBuildAuthConfig:
    def test_explicit_factory_takes_precedence_over_env(self, caplog):
        env = {
            "CONNECTOR_AUTH_FACTORY": "agora_workbench.code_execution.auth:create_noop_auth_config",
            "ENTRA_CLIENT_ID": "cid",
            "ENTRA_TENANT_ID": "tid",
        }
        with patch.dict(os.environ, env, clear=True):
            result = build_auth_config(lambda: _SENTINEL_AUTH_CONFIG)

        assert result is _SENTINEL_AUTH_CONFIG
        assert "Ignoring CONNECTOR_AUTH_FACTORY" in caplog.text

    def test_env_factory_takes_precedence_over_entra(self):
        env = {
            "CONNECTOR_AUTH_FACTORY": f"{__name__}:_make_sentinel_auth_config",
            "ENTRA_CLIENT_ID": "cid",
            "ENTRA_TENANT_ID": "tid",
        }
        with patch.dict(os.environ, env, clear=True):
            result = build_auth_config()

        assert result is _SENTINEL_AUTH_CONFIG

    def test_env_factory_resolution_failure_is_config_error(self):
        with patch.dict(os.environ, {"CONNECTOR_AUTH_FACTORY": "nope_not_real:create"}, clear=True):
            with pytest.raises(ConfigError, match="Could not import module"):
                build_auth_config()

    def test_factory_exception_is_wrapped_as_config_error(self):
        def boom():
            raise RuntimeError("backend unavailable")

        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ConfigError, match="raised RuntimeError: backend unavailable"):
                build_auth_config(boom)

    def test_factory_returning_wrong_type_is_config_error(self):
        def bad_factory():
            return "not-an-auth-config"

        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ConfigError, match="returned a str, expected an AuthConfig"):
                build_auth_config(bad_factory)  # pyright: ignore[reportArgumentType]

    def test_falls_back_to_entra_when_both_vars_set(self):
        env = {"ENTRA_CLIENT_ID": "cid", "ENTRA_TENANT_ID": "tid"}
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "agora_workbench.code_execution.auth.create_entra_auth_config",
                return_value=_SENTINEL_AUTH_CONFIG,
            ) as create_entra:
                result = build_auth_config()

        assert result is _SENTINEL_AUTH_CONFIG
        create_entra.assert_called_once_with(client_id="cid", tenant_id="tid")

    @pytest.mark.parametrize(
        ("env", "missing"),
        [
            ({"ENTRA_CLIENT_ID": "cid"}, "ENTRA_TENANT_ID"),
            ({"ENTRA_TENANT_ID": "tid"}, "ENTRA_CLIENT_ID"),
        ],
    )
    def test_partial_entra_config_is_config_error(self, env, missing):
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigError, match=f"{missing} is not"):
                build_auth_config()

    def test_unconfigured_auth_raises_instead_of_silently_using_noop(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ConfigError, match="No authentication backend is configured"):
                build_auth_config()

    @pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "on"])
    def test_noop_requires_explicit_opt_in(self, flag, caplog):
        from agora_workbench.code_execution.auth import AuthConfig

        with patch.dict(os.environ, {"CONNECTOR_ALLOW_NOOP_AUTH": flag}, clear=True):
            result = build_auth_config()

        assert isinstance(result, AuthConfig)
        assert "no-op auth" in caplog.text

    @pytest.mark.parametrize("flag", ["0", "false", "no", "off", ""])
    def test_falsey_opt_in_still_raises(self, flag):
        with patch.dict(os.environ, {"CONNECTOR_ALLOW_NOOP_AUTH": flag}, clear=True):
            with pytest.raises(ConfigError, match="No authentication backend is configured"):
                build_auth_config()

    def test_ambiguous_opt_in_value_is_rejected(self):
        with patch.dict(os.environ, {"CONNECTOR_ALLOW_NOOP_AUTH": "maybe"}, clear=True):
            with pytest.raises(ConfigError, match="Invalid CONNECTOR_ALLOW_NOOP_AUTH='maybe'"):
                build_auth_config()


class TestMain:
    def test_invalid_port_exits_with_config_error(self, caplog):
        with (
            patch.dict(os.environ, {"CONNECTOR_PORT": "abc"}, clear=True),
            patch(
                "agora_workbench.connector.cli.build_config",
                return_value=(SimpleNamespace(name="connector", upstreams=[]), None),
            ),
            patch("agora_workbench.connector.cli.build_auth_config", return_value=None),
            pytest.raises(SystemExit) as exc_info,
        ):
            cli.main()

        assert exc_info.value.code == 1
        assert "CONNECTOR_PORT/MCP_SERVER_PORT" in caplog.text

    def test_unconfigured_auth_exits_with_config_error(self, caplog):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "agora_workbench.connector.cli.build_config",
                return_value=(SimpleNamespace(name="connector", upstreams=[]), None),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            cli.main()

        assert exc_info.value.code == 1
        assert "No authentication backend is configured" in caplog.text

    def test_passes_caller_supplied_factory_through_to_server(self):
        from agora_workbench.connector.models import RouterConfig, UpstreamConfig

        router_config = RouterConfig(name="connector", upstreams=[UpstreamConfig(name="x", url="http://x:8000")])

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("agora_workbench.connector.cli.build_config", return_value=(router_config, None)),
            patch("agora_workbench.connector.router.RouterServer") as router_server,
            patch("asyncio.run"),
        ):
            cli.main(auth_config_factory=_make_sentinel_auth_config)

        _, kwargs = router_server.call_args
        assert kwargs["auth_config"] is _SENTINEL_AUTH_CONFIG


class TestParseWorkersFromEnv:
    def test_discovers_worker_urls(self):
        env = {
            "WORKER_CHEM1_URL": "http://chem-1:8000",
            "WORKER_CHEM2_URL": "http://chem-2:8000",
            "OTHER_VAR": "ignored",
        }
        with patch.dict(os.environ, env, clear=True):
            result = parse_workers_from_env()
        assert result == [
            ("chem1", "http://chem-1:8000", 1),
            ("chem2", "http://chem-2:8000", 1),
        ]

    def test_applies_weight(self):
        env = {
            "WORKER_CHEM1_URL": "http://chem-1:8000",
            "WORKER_CHEM1_WEIGHT": "3",
            "WORKER_CHEM2_URL": "http://chem-2:8000",
        }
        with patch.dict(os.environ, env, clear=True):
            result = parse_workers_from_env()
        assert result == [
            ("chem1", "http://chem-1:8000", 3),
            ("chem2", "http://chem-2:8000", 1),
        ]

    def test_empty_url_raises(self):
        env = {"WORKER_CHEM1_URL": "  "}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigError, match="empty"):
                parse_workers_from_env()

    def test_weight_without_url_raises(self):
        env = {"WORKER_CHEM1_WEIGHT": "2"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigError, match="no corresponding"):
                parse_workers_from_env()

    def test_invalid_weight_raises(self):
        env = {
            "WORKER_CHEM1_URL": "http://chem-1:8000",
            "WORKER_CHEM1_WEIGHT": "abc",
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigError, match="positive integer"):
                parse_workers_from_env()

    def test_zero_weight_raises(self):
        env = {
            "WORKER_CHEM1_URL": "http://chem-1:8000",
            "WORKER_CHEM1_WEIGHT": "0",
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigError, match="positive integer"):
                parse_workers_from_env()

    def test_no_workers_returns_empty(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            result = parse_workers_from_env()
        assert result == []


class TestBuildConfigDispatcher:
    def test_dispatcher_mode_basic(self):
        env = {
            "CONNECTOR_MODE": "dispatcher",
            "CONNECTOR_NAME": "chem-pool",
            "WORKER_CHEM1_URL": "http://chem-1:8000",
            "WORKER_CHEM2_URL": "http://chem-2:8000",
        }
        with patch.dict(os.environ, env, clear=True):
            config, _ = build_config()

        from agora_workbench.connector.models import DispatcherConfig

        assert isinstance(config, DispatcherConfig)
        assert config.name == "chem-pool"
        assert len(config.workers) == 2
        assert config.workers[0].name == "chem1"
        assert config.workers[1].name == "chem2"
        assert config.strategy == "round_robin"

    def test_dispatcher_mode_with_options(self):
        env = {
            "CONNECTOR_MODE": "dispatcher",
            "WORKER_CHEM1_URL": "http://chem-1:8000",
            "WORKER_CHEM1_WEIGHT": "2",
            "DISPATCHER_STRATEGY": "least_loaded",
            "DISPATCHER_HEALTH_CHECK_INTERVAL": "5",
            "DISPATCHER_FAILURE_POLICY": "reroute",
        }
        with patch.dict(os.environ, env, clear=True):
            config, _ = build_config()

        from agora_workbench.connector.models import DispatcherConfig

        assert isinstance(config, DispatcherConfig)
        assert config.strategy == "least_loaded"
        assert config.health_check_interval == 5.0
        assert config.worker_failure_policy == "reroute"
        assert config.workers[0].weight == 2

    def test_dispatcher_no_workers_raises(self):
        env = {"CONNECTOR_MODE": "dispatcher"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigError, match="No workers configured"):
                build_config()

    def test_dispatcher_invalid_strategy_raises(self):
        env = {
            "CONNECTOR_MODE": "dispatcher",
            "WORKER_CHEM1_URL": "http://chem-1:8000",
            "DISPATCHER_STRATEGY": "random",
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigError, match="DISPATCHER_STRATEGY"):
                build_config()

    def test_dispatcher_invalid_health_interval_raises(self):
        env = {
            "CONNECTOR_MODE": "dispatcher",
            "WORKER_CHEM1_URL": "http://chem-1:8000",
            "DISPATCHER_HEALTH_CHECK_INTERVAL": "-5",
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigError, match="DISPATCHER_HEALTH_CHECK_INTERVAL"):
                build_config()

    def test_dispatcher_invalid_failure_policy_raises(self):
        env = {
            "CONNECTOR_MODE": "dispatcher",
            "WORKER_CHEM1_URL": "http://chem-1:8000",
            "DISPATCHER_FAILURE_POLICY": "retry",
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigError, match="DISPATCHER_FAILURE_POLICY"):
                build_config()

    def test_dispatcher_entra_ids_passed_through(self):
        env = {
            "CONNECTOR_MODE": "dispatcher",
            "WORKER_CHEM1_URL": "http://chem-1:8000",
            "ENTRA_CLIENT_ID": "my-client",
            "ENTRA_TENANT_ID": "my-tenant",
        }
        with patch.dict(os.environ, env, clear=True):
            config, _ = build_config()

        assert config.entra_client_id == "my-client"
        assert config.entra_tenant_id == "my-tenant"

    def test_invalid_mode_mentions_dispatcher(self):
        env = {
            "CONNECTOR_MODE": "invalid",
            "UPSTREAM_X_URL": "http://x:8000",
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigError, match="dispatcher"):
                build_config()
