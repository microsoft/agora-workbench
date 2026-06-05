"""Tests for ToolRegistry class."""

import pytest

from code_execution import ToolRegistry, ToolDefinition, ToolParameter


class TestToolRegistry:
    """Test cases for ToolRegistry."""

    def test_initialize_empty_registry(self):
        """Test creating an empty registry."""
        registry = ToolRegistry()
        assert registry.tools == []
        assert registry.next_id == 0
        assert registry._name_index == {}
        assert registry._id_index == {}

    def test_register_valid_tool(self, empty_registry, sample_tool):
        """Test registering a valid tool."""
        empty_registry.register_tool(sample_tool)

        assert len(empty_registry.tools) == 1
        assert empty_registry.next_id == 1
        # Check the registered tool's id, not the original fixture
        registered_tool = empty_registry.get_tool_by_name("solve_network")
        assert registered_tool.id == 0
        assert "solve_network" in empty_registry._name_index
        assert 0 in empty_registry._id_index

    def test_register_multiple_tools(self, empty_registry, sample_tool, another_tool):
        """Test registering multiple tools."""
        empty_registry.register_tool(sample_tool)
        empty_registry.register_tool(another_tool)

        assert len(empty_registry.tools) == 2
        assert empty_registry.next_id == 2
        # Check registered tools' ids
        assert empty_registry.get_tool_by_name("solve_network").id == 0
        assert empty_registry.get_tool_by_name("build_network").id == 1

    def test_register_invalid_tool_missing_name(self, empty_registry):
        """Test that registering a tool without a name raises ValidationError."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            # ToolDefinition requires name
            ToolDefinition(  # type: ignore[call-arg]
                description="Missing name",
                required_parameters=[],
                module="test.module",
            )

    def test_register_invalid_tool_missing_description(self, empty_registry):
        """Test that registering a tool without a description raises ValidationError."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            # ToolDefinition requires description
            ToolDefinition(  # type: ignore[call-arg]
                name="test_tool",
                required_parameters=[],
                module="test.module",
            )

    def test_get_tool_by_name(self, empty_registry, sample_tool):
        """Test retrieving a tool by name."""
        empty_registry.register_tool(sample_tool)
        retrieved = empty_registry.get_tool_by_name("solve_network")

        assert retrieved is not None
        assert retrieved.name == "solve_network"
        assert retrieved.description == "Solve power network optimization"

    def test_get_tool_by_name_not_found(self, empty_registry):
        """Test retrieving a non-existent tool by name raises ValueError."""
        with pytest.raises(ValueError, match="not registered"):
            empty_registry.get_tool_by_name("nonexistent")

    def test_get_tool_by_id(self, empty_registry, sample_tool):
        """Test retrieving a tool by id."""
        empty_registry.register_tool(sample_tool)
        retrieved = empty_registry.get_tool_by_id(0)

        assert retrieved is not None
        assert retrieved.name == "solve_network"
        assert retrieved.id == 0

    def test_get_tool_by_id_not_found(self, empty_registry):
        """Test retrieving a non-existent tool by id raises ValueError."""
        with pytest.raises(ValueError, match="not registered"):
            empty_registry.get_tool_by_id(999)

    def test_get_id_by_name(self, empty_registry, sample_tool):
        """Test getting tool id by name."""
        empty_registry.register_tool(sample_tool)
        tool_id = empty_registry.get_id_by_name("solve_network")

        assert tool_id == 0

    def test_get_id_by_name_not_found(self, empty_registry):
        """Test getting id for non-existent tool name raises ValueError."""
        with pytest.raises(ValueError, match="not registered"):
            empty_registry.get_id_by_name("nonexistent")

    def test_get_name_by_id(self, empty_registry, sample_tool):
        """Test getting tool name by id."""
        empty_registry.register_tool(sample_tool)
        tool_name = empty_registry.get_name_by_id(0)

        assert tool_name == "solve_network"

    def test_get_name_by_id_not_found(self, empty_registry):
        """Test getting name for non-existent tool id raises ValueError."""
        with pytest.raises(ValueError, match="not registered"):
            empty_registry.get_name_by_id(999)

    def test_tools_collection(self, empty_registry, sample_tool, another_tool):
        """Test accessing tools collection."""
        empty_registry.register_tool(sample_tool)
        empty_registry.register_tool(another_tool)

        tools = empty_registry.tools

        assert len(tools) == 2
        assert tools[0].name == "solve_network"
        assert tools[0].id == 0
        assert tools[1].name == "build_network"
        assert tools[1].id == 1

    def test_tools_collection_empty(self, empty_registry):
        """Test accessing tools from empty registry."""
        tools = empty_registry.tools
        assert tools == []

    def test_remove_tool_by_id(self, empty_registry, sample_tool, another_tool):
        """Test removing a tool by id."""
        empty_registry.register_tool(sample_tool)
        empty_registry.register_tool(another_tool)

        result = empty_registry.remove_tool_by_id(0)

        assert result is True
        assert len(empty_registry.tools) == 1
        assert 0 not in empty_registry._id_index
        assert "solve_network" not in empty_registry._name_index
        assert empty_registry.get_tool_by_name("build_network") is not None

    def test_remove_tool_by_id_not_found(self, empty_registry, sample_tool):
        """Test removing a non-existent tool by id raises ValueError."""
        empty_registry.register_tool(sample_tool)
        with pytest.raises(ValueError, match="not registered"):
            empty_registry.remove_tool_by_id(999)
        assert len(empty_registry.tools) == 1

    def test_remove_tool_by_name(self, empty_registry, sample_tool, another_tool):
        """Test removing a tool by name."""
        empty_registry.register_tool(sample_tool)
        empty_registry.register_tool(another_tool)

        result = empty_registry.remove_tool_by_name("solve_network")

        assert result is True
        assert len(empty_registry.tools) == 1
        assert "solve_network" not in empty_registry._name_index
        assert 0 not in empty_registry._id_index
        assert empty_registry.get_tool_by_name("build_network") is not None

    def test_remove_tool_by_name_not_found(self, empty_registry, sample_tool):
        """Test removing a non-existent tool by name raises ValueError."""
        empty_registry.register_tool(sample_tool)
        with pytest.raises(ValueError, match="not registered"):
            empty_registry.remove_tool_by_name("nonexistent")
        assert len(empty_registry.tools) == 1

    def test_registry_indexes_consistency_after_operations(self, empty_registry, sample_tool, another_tool):
        """Test that indexes remain consistent after multiple operations."""
        # Add tools
        empty_registry.register_tool(sample_tool)
        empty_registry.register_tool(another_tool)

        # Verify initial state
        assert len(empty_registry._name_index) == 2
        assert len(empty_registry._id_index) == 2

        # Remove one tool
        empty_registry.remove_tool_by_name("solve_network")

        # Verify indexes updated correctly
        assert len(empty_registry._name_index) == 1
        assert len(empty_registry._id_index) == 1
        assert "solve_network" not in empty_registry._name_index
        assert 0 not in empty_registry._id_index

        # Add another tool
        new_tool = ToolDefinition(
            name="analyze_results",
            description="Analyze optimization results",
            required_parameters=[ToolParameter(name="result_id", type=str, description="Result ID")],
            module="test.tools.analysis",
        )
        empty_registry.register_tool(new_tool)

        # Verify indexes updated correctly
        assert len(empty_registry._name_index) == 2
        assert len(empty_registry._id_index) == 2
        assert "analyze_results" in empty_registry._name_index


class TestToolDefinitionSerialization:
    """Test cases for ToolDefinition Pydantic model serialization/deserialization."""

    def test_tool_definition_serialization(self):
        """Test that ToolDefinition serializes Type objects to strings."""
        tool_def = ToolDefinition(
            name="test_tool",
            description="A test tool",
            required_parameters=[
                ToolParameter(name="param1", type=str, description="String param"),
                ToolParameter(name="param2", type=int, description="Int param"),
            ],
            optional_parameters=[
                ToolParameter(name="opt_param", type=float, description="Float param", default=3.14),
            ],
            module="test.module",
        )

        # Serialize to dict
        serialized = tool_def.model_dump()

        # Verify Type objects are converted to strings
        assert isinstance(serialized["required_parameters"][0]["type"], str)
        assert isinstance(serialized["required_parameters"][1]["type"], str)
        assert isinstance(serialized["optional_parameters"][0]["type"], str)

        # Verify the string format (module.qualname)
        assert serialized["required_parameters"][0]["type"] == "builtins.str"
        assert serialized["required_parameters"][1]["type"] == "builtins.int"
        assert serialized["optional_parameters"][0]["type"] == "builtins.float"

    def test_tool_definition_deserialization(self):
        """Test that ToolDefinition deserializes string type names back to Type objects."""
        tool_data = {
            "name": "test_tool",
            "description": "A test tool",
            "required_parameters": [
                {"name": "param1", "type": "builtins.str", "description": "String param"},
                {"name": "param2", "type": "builtins.int", "description": "Int param"},
            ],
            "optional_parameters": [
                {"name": "opt_param", "type": "builtins.float", "description": "Float param", "default": 3.14},
            ],
            "module": "test.module",
        }

        # Deserialize from dict
        tool_def = ToolDefinition(**tool_data)

        # Verify strings are converted back to Type objects
        assert tool_def.required_parameters[0].type is str
        assert tool_def.required_parameters[1].type is int
        assert tool_def.optional_parameters[0].type is float

    def test_tool_definition_round_trip(self):
        """Test that ToolDefinition can serialize and deserialize without data loss."""
        original = ToolDefinition(
            name="complex_tool",
            description="A complex test tool",
            required_parameters=[
                ToolParameter(name="text", type=str, description="Text input"),
                ToolParameter(name="count", type=int, description="Count value"),
                ToolParameter(name="flag", type=bool, description="Boolean flag"),
            ],
            optional_parameters=[
                ToolParameter(name="data", type=dict, description="Data dict", default={}),
                ToolParameter(name="items", type=list, description="Item list", default=[]),
            ],
            module="complex.module.path",
            id=42,
        )

        # Serialize
        serialized = original.model_dump()

        # Deserialize
        restored = ToolDefinition(**serialized)

        # Verify all fields match
        assert restored.name == original.name
        assert restored.description == original.description
        assert restored.module == original.module
        assert restored.id == original.id

        # Verify parameters are correctly restored
        assert len(restored.required_parameters) == len(original.required_parameters)
        assert len(restored.optional_parameters) == len(original.optional_parameters)

        for orig_param, rest_param in zip(original.required_parameters, restored.required_parameters):
            assert rest_param.name == orig_param.name
            assert rest_param.type is orig_param.type
            assert rest_param.description == orig_param.description

        for orig_param, rest_param in zip(original.optional_parameters, restored.optional_parameters):
            assert rest_param.name == orig_param.name
            assert rest_param.type is orig_param.type
            assert rest_param.description == orig_param.description


class TestToolRegistryPackageResolution:
    """Test cases for ToolRegistry package-based module resolution."""

    def test_package_resolves_module_from_name(self):
        """Registry with package set should resolve module as {package}.{name}."""
        registry = ToolRegistry(package="my_tools")
        tool = ToolDefinition(
            name="parse_data",
            description="Parse input data",
        )
        registry.register_tool(tool)

        registered = registry.get_tool_by_name("parse_data")
        assert registered.module == "my_tools.parse_data"

    def test_module_override_takes_precedence(self):
        """module_override should take precedence over registry package."""
        registry = ToolRegistry(package="my_tools")
        tool = ToolDefinition(
            name="parse_data",
            description="Parse input data",
            module_override="my_tools.shared_module",
        )
        registry.register_tool(tool)

        registered = registry.get_tool_by_name("parse_data")
        assert registered.module == "my_tools.shared_module"

    def test_explicit_module_accepted_as_is(self):
        """Explicit module set on ToolDefinition should be used without modification."""
        registry = ToolRegistry(package="my_tools")
        tool = ToolDefinition(
            name="parse_data",
            description="Parse input data",
            module="legacy.package.parse_data",
        )
        registry.register_tool(tool)

        registered = registry.get_tool_by_name("parse_data")
        assert registered.module == "legacy.package.parse_data"

    def test_no_package_no_module_raises(self):
        """Registry without package should raise when tool has no module."""
        registry = ToolRegistry()
        tool = ToolDefinition(
            name="parse_data",
            description="Parse input data",
        )

        with pytest.raises(ValueError, match="no module_override and no explicit module"):
            registry.register_tool(tool)

    def test_no_package_with_explicit_module_works(self):
        """Registry without package should accept tools that have explicit module."""
        registry = ToolRegistry()
        tool = ToolDefinition(
            name="parse_data",
            description="Parse input data",
            module="some.module",
        )
        registry.register_tool(tool)

        registered = registry.get_tool_by_name("parse_data")
        assert registered.module == "some.module"

    def test_module_override_excluded_from_serialization(self):
        """module_override should not appear in serialized output (catalog JSON)."""
        tool = ToolDefinition(
            name="parse_data",
            description="Parse input data",
            module="resolved.path",
            module_override="my_tools.shared",
        )
        serialized = tool.model_dump()
        assert "module_override" not in serialized
        assert serialized["module"] == "resolved.path"

    def test_catalog_roundtrip_with_module(self):
        """ToolDefinition with module should round-trip through JSON correctly."""
        registry = ToolRegistry(package="chemistry_tools")
        tool = ToolDefinition(
            name="parse_molecule",
            description="Parse a SMILES string",
        )
        registry.register_tool(tool)

        registered = registry.get_tool_by_name("parse_molecule")
        serialized = registered.model_dump()
        restored = ToolDefinition(**serialized)

        assert restored.module == "chemistry_tools.parse_molecule"
        assert restored.name == "parse_molecule"


class TestToolRegistrySerialization:
    """Test cases for ToolRegistry serialization/deserialization with ToolDefinition."""

    def test_registry_can_register_and_retrieve(self):
        """Minimal smoke test for ToolRegistry wiring."""
        registry = ToolRegistry()
        tool = ToolDefinition(
            name="tool_one",
            description="First tool",
            required_parameters=[ToolParameter(name="x", type=str, description="x")],
            optional_parameters=[],
            module="module.one",
        )

        registry.register_tool(tool)
        assert registry.get_tool_by_name("tool_one").id == 0
