"""Tests for ToolMaker codegen and task spec tools."""

from unittest.mock import patch

import pytest

from toolmaker.models import TaskSpec
from toolmaker.tools.codegen_tools import (
    create_codegen_tools,
    create_task_spec_tools,
    _safe_domain_path,
)


class TestSafeDomainPath:
    def test_traversal_rejected(self, tmp_path):
        with patch("toolmaker.tools.codegen_tools._AGORA_MAF_ROOT", tmp_path):
            domain_dir = tmp_path / "domains" / "test" / "server"
            domain_dir.mkdir(parents=True)
            with pytest.raises(ValueError, match="Path traversal"):
                _safe_domain_path("test", "../../../etc/passwd")


class TestCodegenTools:
    @pytest.fixture
    def domain_root(self, tmp_path):
        """Set up a temporary root for domain file operations."""
        with patch("toolmaker.tools.codegen_tools._AGORA_MAF_ROOT", tmp_path):
            # Create example domain for read_example_domain
            example_dir = tmp_path / "domains" / "example" / "server"
            example_dir.mkdir(parents=True)
            (example_dir / "example_server.py").write_text("# Example server\n")
            yield tmp_path

    @pytest.fixture
    def tools(self, domain_root):
        task_spec = TaskSpec()
        return {t.name: t for t in create_codegen_tools(task_spec)}

    def test_all_tools_created(self, tools):
        expected = {"write_domain_file", "read_domain_file", "list_domain_files", "delete_domain_file",
                     "read_example_domain"}
        assert set(tools.keys()) == expected

    @pytest.mark.asyncio
    async def test_write_and_read(self, domain_root):
        task_spec = TaskSpec()
        with patch("toolmaker.tools.codegen_tools._AGORA_MAF_ROOT", domain_root):
            tools = {t.name: t for t in create_codegen_tools(task_spec)}
            # Write
            result = await tools["write_domain_file"].func(
                domain_name="test",
                relative_path="server.py",
                content="print('hello')",
            )
            assert "Successfully wrote" in result

            # Read back
            result = await tools["read_domain_file"].func(
                domain_name="test",
                relative_path="server.py",
            )
            assert "print('hello')" in result

    @pytest.mark.asyncio
    async def test_list_domain_files(self, domain_root):
        task_spec = TaskSpec()
        with patch("toolmaker.tools.codegen_tools._AGORA_MAF_ROOT", domain_root):
            tools = {t.name: t for t in create_codegen_tools(task_spec)}
            # Write a file first
            await tools["write_domain_file"].func(
                domain_name="test",
                relative_path="server.py",
                content="x = 1",
            )
            result = await tools["list_domain_files"].func(domain_name="test")
            assert "server.py" in result

    @pytest.mark.asyncio
    async def test_delete_domain_file(self, domain_root):
        task_spec = TaskSpec()
        with patch("toolmaker.tools.codegen_tools._AGORA_MAF_ROOT", domain_root):
            tools = {t.name: t for t in create_codegen_tools(task_spec)}
            await tools["write_domain_file"].func(
                domain_name="test", relative_path="tmp.py", content="x"
            )
            result = await tools["delete_domain_file"].func(
                domain_name="test", relative_path="tmp.py"
            )
            assert "Deleted" in result

    @pytest.mark.asyncio
    async def test_read_example_domain(self, domain_root):
        with patch("toolmaker.tools.codegen_tools._AGORA_MAF_ROOT", domain_root):
            task_spec = TaskSpec()
            tools = {t.name: t for t in create_codegen_tools(task_spec)}
            result = await tools["read_example_domain"].func(file_path="example_server.py")
            assert "Example server" in result


class TestTaskSpecTools:
    @pytest.fixture
    def spec_and_tools(self):
        spec = TaskSpec()
        tools = {t.name: t for t in create_task_spec_tools(spec)}
        return spec, tools

    def test_all_tools_created(self, spec_and_tools):
        _, tools = spec_and_tools
        expected = {"view_task_spec", "update_task_spec", "add_argument", "add_return_field",
                     "add_example", "finalize_task_spec"}
        assert set(tools.keys()) == expected

    @pytest.mark.asyncio
    async def test_view_task_spec(self, spec_and_tools):
        spec, tools = spec_and_tools
        result = await tools["view_task_spec"].func()
        assert "Task Specification" in result

    @pytest.mark.asyncio
    async def test_update_task_spec(self, spec_and_tools):
        spec, tools = spec_and_tools
        result = await tools["update_task_spec"].func(
            repo_url="https://github.com/test/repo",
            tool_name="my_tool",
        )
        assert "Updated" in result
        assert spec.repo_url == "https://github.com/test/repo"
        assert spec.tool_name == "my_tool"

    @pytest.mark.asyncio
    async def test_add_argument(self, spec_and_tools):
        spec, tools = spec_and_tools
        result = await tools["add_argument"].func(
            name="x", type="int", description="a number"
        )
        assert "Added argument" in result
        assert len(spec.arguments) == 1
        assert spec.arguments[0].name == "x"

    @pytest.mark.asyncio
    async def test_add_return_field(self, spec_and_tools):
        spec, tools = spec_and_tools
        result = await tools["add_return_field"].func(
            name="result", type="int", description="output"
        )
        assert "Added return field" in result
        assert len(spec.returns) == 1

    @pytest.mark.asyncio
    async def test_add_example(self, spec_and_tools):
        spec, tools = spec_and_tools
        result = await tools["add_example"].func(
            arguments='{"x": "10"}',
            expected_description="should return 10",
        )
        assert "Added example" in result
        assert len(spec.examples) == 1

    @pytest.mark.asyncio
    async def test_add_example_invalid_json(self, spec_and_tools):
        spec, tools = spec_and_tools
        result = await tools["add_example"].func(arguments="not json")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_finalize_incomplete(self, spec_and_tools):
        spec, tools = spec_and_tools
        result = await tools["finalize_task_spec"].func()
        assert "Cannot finalize" in result

    @pytest.mark.asyncio
    async def test_finalize_complete(self, spec_and_tools):
        spec, tools = spec_and_tools
        spec.repo_url = "https://github.com/test/repo"
        spec.task_description = "Does something"
        spec.tool_name = "my_tool"
        spec.domain_name = "myrepo"
        await tools["add_argument"].func(name="x", type="int", description="val")
        await tools["add_return_field"].func(name="result", type="int", description="out")
        result = await tools["finalize_task_spec"].func()
        assert "finalized" in result.lower()
