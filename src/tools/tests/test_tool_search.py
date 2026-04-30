"""Tests for core.tool_search — protocol and model definitions."""

import pytest

from tools.tool_search import ToolKey, ToolSearchBackend, ToolSearchResult


class TestToolSearchResult:
    """Tests for the ToolSearchResult Pydantic model."""

    @pytest.mark.unit
    def test_required_fields(self):
        result = ToolSearchResult(
            name="run_opf",
            server_name="powergrid",
            description="Run optimal power flow",
            execution_type="mcp",
        )
        assert result.name == "run_opf"
        assert result.server_name == "powergrid"
        assert result.description == "Run optimal power flow"
        assert result.execution_type == "mcp"
        assert result.score is None

    @pytest.mark.unit
    def test_optional_score(self):
        result = ToolSearchResult(
            name="run_opf",
            server_name="",
            description="Run OPF",
            execution_type="mcp",
            score=0.85,
        )
        assert result.score == 0.85

    @pytest.mark.unit
    def test_model_dump(self):
        result = ToolSearchResult(
            name="run_opf",
            server_name="powergrid",
            description="OPF analysis",
            execution_type="mcp",
            score=1.5,
        )
        d = result.model_dump()
        assert d == {
            "name": "run_opf",
            "server_name": "powergrid",
            "description": "OPF analysis",
            "execution_type": "mcp",
            "score": 1.5,
            "state_requires": [],
            "state_produces": [],
        }


class TestToolKey:
    @pytest.mark.unit
    def test_tool_key_is_tuple_alias(self):
        key: ToolKey = ("server", "tool")
        assert key == ("server", "tool")
        assert key[0] == "server"
        assert key[1] == "tool"


class TestToolSearchBackendABC:
    """Tests for the ToolSearchBackend abstract base class."""

    @pytest.mark.unit
    def test_abc_cannot_be_instantiated_directly(self):
        """ToolSearchBackend is abstract and cannot be instantiated."""
        with pytest.raises(TypeError):
            ToolSearchBackend()  # type: ignore[reportAbstractUsage]

    @pytest.mark.unit
    def test_subclass_with_search_is_valid(self):
        """A concrete subclass implementing search() is a valid backend."""

        class _FakeBackend(ToolSearchBackend):
            async def search(self, query: str, top: int = 5) -> list[ToolSearchResult]:
                return []

        backend = _FakeBackend()
        assert isinstance(backend, ToolSearchBackend)
        assert backend.user_token == ""

    @pytest.mark.unit
    def test_subclass_stores_user_token(self):
        """user_token passed at construction is stored as an attribute."""

        class _FakeBackend(ToolSearchBackend):
            async def search(self, query: str, top: int = 5) -> list[ToolSearchResult]:
                return []

        backend = _FakeBackend(user_token="my-token")
        assert backend.user_token == "my-token"

    @pytest.mark.unit
    def test_non_subclass_is_not_instance(self):
        """A class that doesn't inherit ToolSearchBackend is not an instance."""

        class _NoInherit:
            async def search(self, query: str, top: int = 5) -> list[ToolSearchResult]:
                return []

        assert not isinstance(_NoInherit(), ToolSearchBackend)

    @pytest.mark.unit
    def test_bm25_backend_is_subclass(self):
        """BM25ToolSearchBackend inherits from ToolSearchBackend."""
        from tools.search.bm25_tool_search import BM25ToolSearchBackend

        backend = BM25ToolSearchBackend(tools=[])
        assert isinstance(backend, ToolSearchBackend)

    @pytest.mark.unit
    def test_azure_backend_is_subclass(self):
        """AzureAIToolSearchBackend inherits from ToolSearchBackend."""
        from unittest.mock import patch, MagicMock

        with patch(
            "tools.search.azure_ai_tool_search.create_async_obo_credential",
            return_value=MagicMock(),
        ):
            from tools.search.azure_ai_tool_search import AzureAIToolSearchBackend

            backend = AzureAIToolSearchBackend("test-index", user_token="tok")
            assert isinstance(backend, ToolSearchBackend)
            assert backend.user_token == "tok"
