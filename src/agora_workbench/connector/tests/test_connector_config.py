"""Tests for connector configuration models."""

import pytest
from pydantic import ValidationError

from agora_workbench.connector import GatewayConfig, GatewayPolicy, RouterConfig, UpstreamConfig


class TestUpstreamConfig:
    """Tests for UpstreamConfig model."""

    def test_minimal_config(self):
        config = UpstreamConfig(name="chemistry", url="http://chemistry:8000")
        assert config.name == "chemistry"
        assert config.url == "http://chemistry:8000"
        assert config.expose_tools is None
        assert config.tool_aliases == {}

    def test_full_config(self):
        config = UpstreamConfig(
            name="gis",
            url="https://gis-server.internal.example.com",
            expose_tools=["reproject", "buffer_*"],
            tool_aliases={"reproject": "geo_reproject"},
        )
        assert config.expose_tools == ["reproject", "buffer_*"]
        assert config.tool_aliases == {"reproject": "geo_reproject"}


class TestGatewayPolicy:
    """Tests for GatewayPolicy model."""

    def test_defaults(self):
        policy = GatewayPolicy()
        assert policy.allowed_tools is None
        assert policy.blocked_tools == []
        assert policy.max_calls_per_minute is None

    def test_rate_limit(self):
        policy = GatewayPolicy(max_calls_per_minute=30)
        assert policy.max_calls_per_minute == 30

    def test_rate_limit_minimum(self):
        with pytest.raises(ValidationError):
            GatewayPolicy(max_calls_per_minute=0)


class TestRouterConfig:
    """Tests for RouterConfig model."""

    def test_basic(self):
        config = RouterConfig(
            name="science-hub",
            upstreams=[
                UpstreamConfig(name="chemistry", url="http://chemistry:8000"),
                UpstreamConfig(name="gis", url="http://gis:8000"),
            ],
        )
        assert config.name == "science-hub"
        assert len(config.upstreams) == 2

    def test_requires_at_least_one_upstream(self):
        with pytest.raises(ValidationError):
            RouterConfig(name="bad", upstreams=[])


class TestGatewayConfig:
    """Tests for GatewayConfig model."""

    def test_basic(self):
        config = GatewayConfig(
            name="chem-gateway",
            upstream=UpstreamConfig(name="chemistry", url="http://chemistry:8000"),
            policy=GatewayPolicy(
                max_calls_per_minute=60,
                blocked_tools=["parallel_execute"],
            ),
        )
        assert config.name == "chem-gateway"
        assert config.upstream.name == "chemistry"
        assert config.policy.max_calls_per_minute == 60
        assert config.policy.blocked_tools == ["parallel_execute"]

    def test_default_policy(self):
        config = GatewayConfig(
            name="gw",
            upstream=UpstreamConfig(name="x", url="http://x:8000"),
        )
        assert config.policy.allowed_tools is None
        assert config.policy.blocked_tools == []
