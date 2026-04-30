"""Tests for ToolMaker repo exploration tools."""

from unittest.mock import patch

import pytest

from agent_bot.toolmaker.tools.repo_tools import (
    create_repo_tools,
    _safe_path,
)


@pytest.fixture
def mock_repo(tmp_path):
    """Create a mock repository structure for testing."""
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()

    # Create some files
    (repo_dir / "README.md").write_text("# Test Repo\nThis is a test repository.")
    (repo_dir / "setup.py").write_text("from setuptools import setup\nsetup(name='test')")

    src_dir = repo_dir / "src"
    src_dir.mkdir()
    (src_dir / "__init__.py").write_text("")
    (src_dir / "main.py").write_text("def main():\n    print('hello')\n")
    (src_dir / "utils.py").write_text("def helper(x: int) -> int:\n    return x + 1\n")

    tests_dir = repo_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_main.py").write_text("def test_main(): pass\n")

    return repo_dir


class TestSafePath:
    def test_normal_path(self, tmp_path):
        with patch("agent_bot.toolmaker.tools.repo_tools._TOOLMAKER_WORKSPACE", tmp_path):
            repo_dir = tmp_path / "myrepo"
            repo_dir.mkdir()
            result = _safe_path("myrepo", "src/main.py")
            assert str(result).startswith(str(repo_dir))

    def test_traversal_rejected(self, tmp_path):
        with patch("agent_bot.toolmaker.tools.repo_tools._TOOLMAKER_WORKSPACE", tmp_path):
            repo_dir = tmp_path / "myrepo"
            repo_dir.mkdir()
            with pytest.raises(ValueError, match="Path traversal"):
                _safe_path("myrepo", "../../etc/passwd")


class TestRepoTools:
    """Test the repo tool functions (excluding clone_repo which needs git)."""

    @pytest.fixture
    def tools(self):
        return {t.name: t for t in create_repo_tools()}

    def test_all_tools_created(self, tools):
        expected = {"clone_repo", "read_repo_file", "list_repo_dir", "search_repo", "browse_url", "run_bash_in_repo"}
        assert set(tools.keys()) == expected

    def test_tool_names_and_descriptions(self, tools):
        for name, tool in tools.items():
            assert tool.name == name
            assert tool.description  # should have a description

    @pytest.mark.asyncio
    async def test_read_repo_file(self, mock_repo):
        with patch("agent_bot.toolmaker.tools.repo_tools._TOOLMAKER_WORKSPACE", mock_repo.parent):
            tools = {t.name: t for t in create_repo_tools()}
            result = await tools["read_repo_file"].func(
                repo_name=mock_repo.name,
                file_path="README.md",
            )
            assert "Test Repo" in result

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self, mock_repo):
        with patch("agent_bot.toolmaker.tools.repo_tools._TOOLMAKER_WORKSPACE", mock_repo.parent):
            tools = {t.name: t for t in create_repo_tools()}
            result = await tools["read_repo_file"].func(
                repo_name=mock_repo.name,
                file_path="nonexistent.py",
            )
            assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_list_repo_dir(self, mock_repo):
        with patch("agent_bot.toolmaker.tools.repo_tools._TOOLMAKER_WORKSPACE", mock_repo.parent):
            tools = {t.name: t for t in create_repo_tools()}
            result = await tools["list_repo_dir"].func(
                repo_name=mock_repo.name,
                dir_path=".",
                recursive=False,
            )
            assert "README.md" in result
            assert "src/" in result

    @pytest.mark.asyncio
    async def test_list_repo_dir_recursive(self, mock_repo):
        with patch("agent_bot.toolmaker.tools.repo_tools._TOOLMAKER_WORKSPACE", mock_repo.parent):
            tools = {t.name: t for t in create_repo_tools()}
            result = await tools["list_repo_dir"].func(
                repo_name=mock_repo.name,
                dir_path=".",
                recursive=True,
            )
            assert "main.py" in result

    @pytest.mark.asyncio
    async def test_search_repo(self, mock_repo):
        with patch("agent_bot.toolmaker.tools.repo_tools._TOOLMAKER_WORKSPACE", mock_repo.parent):
            tools = {t.name: t for t in create_repo_tools()}
            result = await tools["search_repo"].func(
                repo_name=mock_repo.name,
                pattern="def helper",
                file_glob="*.py",
            )
            assert "helper" in result

    @pytest.mark.asyncio
    async def test_search_repo_no_match(self, mock_repo):
        with patch("agent_bot.toolmaker.tools.repo_tools._TOOLMAKER_WORKSPACE", mock_repo.parent):
            tools = {t.name: t for t in create_repo_tools()}
            result = await tools["search_repo"].func(
                repo_name=mock_repo.name,
                pattern="nonexistent_function_xyz",
                file_glob="*.py",
            )
            assert "no matches" in result.lower()
