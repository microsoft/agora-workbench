"""Tests for ConnectorConfig model."""

import pytest
from pydantic import ValidationError

from code_execution.connector import ConnectorConfig, GatewayPolicy, UpstreamConfig


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


class TestConnectorConfig:
    """Tests for ConnectorConfig model."""

    def test_router_mode(self):
        config = ConnectorConfig(
            name="science-hub",
            mode="router",
            upstreams=[
                UpstreamConfig(name="chemistry", url="http://chemistry:8000"),
                UpstreamConfig(name="gis", url="http://gis:8000"),
            ],
        )
        assert config.mode == "router"
        assert len(config.upstreams) == 2
        assert config.gateway_policy is None

    def test_gateway_mode(self):
        config = ConnectorConfig(
            name="chem-gateway",
            mode="gateway",
            upstreams=[
                UpstreamConfig(name="chemistry", url="http://chemistry:8000"),
            ],
            gateway_policy=GatewayPolicy(
                max_calls_per_minute=60,
                blocked_tools=["parallel_execute"],
            ),
        )
        assert config.mode == "gateway"
        assert config.gateway_policy.max_calls_per_minute == 60
        assert config.gateway_policy.blocked_tools == ["parallel_execute"]

    def test_invalid_mode(self):
        with pytest.raises(ValidationError):
            ConnectorConfig(
                name="bad",
                mode="invalid",
                upstreams=[UpstreamConfig(name="x", url="http://x:8000")],
            )

    def test_requires_upstreams(self):
        with pytest.raises(ValidationError):
            ConnectorConfig(name="bad", mode="router")
