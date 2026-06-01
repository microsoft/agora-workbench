"""Tests for connector.cli environment-variable config parsing."""

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from connector import cli
from connector.cli import (
    ConfigError,
    build_config,
    parse_upstreams_from_env,
    validate_upstream_names,
)


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


class TestMain:
    def test_invalid_port_exits_with_config_error(self, caplog):
        with (
            patch.dict(os.environ, {"CONNECTOR_PORT": "abc"}, clear=True),
            patch("connector.cli.build_config", return_value=(SimpleNamespace(name="connector", upstreams=[]), None)),
            patch("connector.cli.build_auth_config", return_value=None),
            pytest.raises(SystemExit) as exc_info,
        ):
            cli.main()

        assert exc_info.value.code == 1
        assert "CONNECTOR_PORT/MCP_SERVER_PORT" in caplog.text
