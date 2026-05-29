"""Tests for the /catalog endpoint on CodeExecutionServer."""

from starlette.testclient import TestClient

from code_execution import CodeExecutionServer, ServerConfig, ToolDefinition, ToolParameter, ToolRegistry
from code_execution.auth import create_noop_auth_config


def _make_server(tool_registry=None) -> CodeExecutionServer:
    """Create a minimal CodeExecutionServer for testing."""
    config = ServerConfig(
        name="test-server",
        description="Test server",
        type="uv",
        dependency_file="",
        auto_build=False,
    )
    return CodeExecutionServer(
        server_config=config,
        tool_registry=tool_registry,
        auth_config=create_noop_auth_config(),
    )


def _make_tool_registry() -> ToolRegistry:
    """Create a ToolRegistry with sample tools."""
    registry = ToolRegistry()
    registry.register_tool(
        ToolDefinition(
            name="compute_descriptors",
            description="Compute molecular descriptors for a SMILES string.",
            module="chemistry.tools",
            required_parameters=[
                ToolParameter(name="smiles", type=str, description="SMILES input"),
            ],
            affordances=["compute descriptors", "molecular properties"],
        )
    )
    registry.register_tool(
        ToolDefinition(
            name="cluster_molecules",
            description="Cluster molecules by fingerprint similarity.",
            module="chemistry.tools",
            required_parameters=[
                ToolParameter(name="smiles_list", type=list, description="List of SMILES"),
            ],
            optional_parameters=[
                ToolParameter(name="cutoff", type=float, description="Distance cutoff", default=0.5),
            ],
        )
    )
    return registry


class TestCatalogEndpoint:
    """Tests for GET /catalog on CodeExecutionServer."""

    def test_catalog_returns_empty_when_no_registry(self):
        """Server with no tool registry returns empty catalog."""
        server = _make_server(tool_registry=None)
        app = server.mcp.http_app(transport="streamable-http")
        server._add_custom_endpoints(app)

        client = TestClient(app)
        response = client.get("/catalog")
        assert response.status_code == 200
        data = response.json()
        assert data["server_name"] == "test-server"
        assert data["tools"] == []

    def test_catalog_returns_tools(self):
        """Server with tool registry returns serialized tools."""
        registry = _make_tool_registry()
        server = _make_server(tool_registry=registry)
        app = server.mcp.http_app(transport="streamable-http")
        server._add_custom_endpoints(app)

        client = TestClient(app)
        response = client.get("/catalog")
        assert response.status_code == 200
        data = response.json()
        assert data["server_name"] == "test-server"
        assert len(data["tools"]) == 2

        # Verify tool structure
        tool_names = {t["name"] for t in data["tools"]}
        assert tool_names == {"compute_descriptors", "cluster_molecules"}

        # Verify a tool has expected fields
        descriptors_tool = next(t for t in data["tools"] if t["name"] == "compute_descriptors")
        assert descriptors_tool["description"] == "Compute molecular descriptors for a SMILES string."
        assert descriptors_tool["module"] == "chemistry.tools"
        assert len(descriptors_tool["required_parameters"]) == 1
        assert descriptors_tool["required_parameters"][0]["name"] == "smiles"
        assert descriptors_tool["affordances"] == ["compute descriptors", "molecular properties"]

    def test_catalog_tools_are_deserializable(self):
        """Catalog output can be round-tripped back to ToolDefinition."""
        registry = _make_tool_registry()
        server = _make_server(tool_registry=registry)
        app = server.mcp.http_app(transport="streamable-http")
        server._add_custom_endpoints(app)

        client = TestClient(app)
        response = client.get("/catalog")
        data = response.json()

        # Round-trip: deserialize back to ToolDefinition
        for tool_data in data["tools"]:
            tool_def = ToolDefinition(**tool_data)
            assert tool_def.name in {"compute_descriptors", "cluster_molecules"}
            assert tool_def.description
            assert tool_def.module == "chemistry.tools"
