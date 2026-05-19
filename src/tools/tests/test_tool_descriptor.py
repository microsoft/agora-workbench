"""Tests for ToolDescriptor."""

import pytest

from pydantic import BaseModel, Field

from tools.tool_descriptor import ToolDescriptor


# ---------------------------------------------------------------------------
# ToolDescriptor
# ---------------------------------------------------------------------------


class _DummyInput(BaseModel):
    x: str = Field(description="A test field")


class TestToolDescriptor:
    @pytest.mark.unit
    def test_fields(self):
        async def my_func(x: str) -> str:
            return x

        td = ToolDescriptor(
            name="my_tool",
            description="A test tool",
            input_model=_DummyInput,
            func=my_func,
        )
        assert td.name == "my_tool"
        assert td.description == "A test tool"
        assert td.input_schema["type"] == "object"
        assert td.func is my_func
        assert td.input_model is _DummyInput

    @pytest.mark.unit
    def test_input_schema_derived_from_model(self):
        """input_schema is auto-derived from input_model when not provided."""

        async def noop(x: str) -> str:
            return ""

        td = ToolDescriptor(
            name="noop",
            description="",
            input_model=_DummyInput,
            func=noop,
        )
        assert td.input_schema == _DummyInput.model_json_schema()

    @pytest.mark.unit
    def test_explicit_input_schema_not_overridden(self):
        """Explicit input_schema is preserved even when input_model is given."""

        async def noop(x: str) -> str:
            return ""

        custom_schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        td = ToolDescriptor(
            name="custom",
            description="",
            input_model=_DummyInput,
            input_schema=custom_schema,
            func=noop,
        )
        assert td.input_schema is custom_schema

    @pytest.mark.unit
    def test_validation_empty_name_raises(self):
        async def noop() -> str:
            return ""

        with pytest.raises(ValueError, match="non-empty"):
            ToolDescriptor(name="", description="d", input_model=_DummyInput, func=noop)

    @pytest.mark.unit
    def test_validation_non_async_func_raises(self):
        def sync_func() -> str:
            return ""

        with pytest.raises(TypeError, match="async callable"):
            # Deliberately passing a non-async callable to test runtime validation
            kwargs: dict = {"name": "t", "description": "d", "input_model": _DummyInput, "func": sync_func}
            ToolDescriptor(**kwargs)

    @pytest.mark.unit
    def test_validation_non_dict_schema_raises(self):
        async def noop() -> str:
            return ""

        with pytest.raises(TypeError, match="dict"):
            # Deliberately passing a non-dict to test runtime validation
            kwargs: dict = {
                "name": "t",
                "description": "d",
                "input_model": _DummyInput,
                "input_schema": "not a dict",
                "func": noop,
            }
            ToolDescriptor(**kwargs)
