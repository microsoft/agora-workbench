"""Tests for the Azure AI Search tool search backend."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import code_execution.tools.search.azure_ai_tool_search as azure_tool_search
from code_execution.tools.search.azure_ai_tool_search import AzureAIToolSearchBackend
from utilities.tool_search import ToolInfo, ToolSearchResult


class FakeEmbeddingResponse:
    def __init__(self, embedding: list[float]):
        self._embedding = embedding

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"data": [{"embedding": self._embedding}]}


class FakeAsyncClient:
    def __init__(self, embeddings: list[list[float]]):
        self._embeddings = list(embeddings)
        self.requests: list[tuple[str, dict]] = []
        self.closed = False

    async def post(self, url: str, *, json: dict) -> FakeEmbeddingResponse:
        self.requests.append((url, json))
        return FakeEmbeddingResponse(self._embeddings.pop(0))

    async def close(self) -> None:
        self.closed = True


class FakeSearchResults:
    def __init__(self, documents: list[dict]):
        self._documents = list(documents)
        self._index = 0

    def __aiter__(self) -> "FakeSearchResults":
        self._index = 0
        return self

    async def __anext__(self) -> dict:
        if self._index >= len(self._documents):
            raise StopAsyncIteration
        document = self._documents[self._index]
        self._index += 1
        return document


class FakeSearchClient:
    def __init__(self, responses: list[FakeSearchResults | Exception] | None = None):
        self.responses = list(responses or [])
        self.search_calls: list[dict] = []
        self.uploaded_documents: list[dict] = []
        self.closed = False

    async def merge_or_upload_documents(self, *, documents: list[dict]) -> list[dict]:
        self.uploaded_documents.extend(documents)
        return [{"key": doc["id"], "status": True} for doc in documents]

    async def search(self, **kwargs) -> FakeSearchResults:
        self.search_calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def close(self) -> None:
        self.closed = True


class FakeIndexClient:
    def __init__(self):
        self.indexes = []
        self.deleted_indexes: list[str] = []
        self.closed = False

    async def create_or_update_index(self, index) -> object:
        self.indexes.append(index)
        return index

    async def delete_index(self, index_name: str) -> None:
        self.deleted_indexes.append(index_name)

    async def close(self) -> None:
        self.closed = True


class FakeClosable:
    def __init__(self):
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def sample_tools() -> list[ToolInfo]:
    return [
        ToolInfo(
            name="run_opf",
            description="Run optimal power flow",
            server_name="powergrid",
            affordances=("optimal dispatch",),
            state_requires=("grid.loaded",),
            state_produces=("grid.solved",),
        ),
        ToolInfo(
            name="build_network",
            description="Build a network model",
            server_name="powergrid",
            affordances=("topology",),
        ),
    ]


@pytest.fixture
def azure_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOL_SEARCH_ENDPOINT", "https://tool-search.test.windows.net")
    monkeypatch.setenv("TOOL_SEARCH_VECTORIZER_ENDPOINT", "https://aoai.test.azure.com")
    monkeypatch.setenv("TOOL_SEARCH_VECTORIZER_DEPLOYMENT", "text-embedding-3-large")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")


class TestAzureAIToolSearchBackend:
    @pytest.mark.unit
    def test_constructs_with_tools(self, sample_tools, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(azure_tool_search, "get_search_credential_async", lambda: object())
        backend = AzureAIToolSearchBackend(sample_tools, server_name="Power Grid")
        assert backend.server_name == "Power Grid"
        assert backend.index_name.startswith("tool-search-power-grid-")
        assert len(backend.index_name.rsplit("-", 1)[1]) == 8

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_initialize_creates_index_and_uploads_documents(
        self,
        sample_tools,
        azure_env,
        monkeypatch: pytest.MonkeyPatch,
    ):
        fake_search_client = FakeSearchClient()
        fake_index_client = FakeIndexClient()
        fake_http_client = FakeAsyncClient([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])
        registered_callbacks: list = []

        monkeypatch.setattr(azure_tool_search, "get_search_credential_async", lambda: object())
        monkeypatch.setattr(azure_tool_search, "SearchClient", lambda **kwargs: fake_search_client)
        monkeypatch.setattr(azure_tool_search, "SearchIndexClient", lambda **kwargs: fake_index_client)
        monkeypatch.setattr(azure_tool_search.httpx, "AsyncClient", lambda *args, **kwargs: fake_http_client)
        monkeypatch.setattr(
            azure_tool_search.atexit, "register", lambda callback: registered_callbacks.append(callback)
        )

        backend = AzureAIToolSearchBackend(sample_tools, server_name="powergrid")
        await backend.initialize()

        assert backend.index_name.startswith("tool-search-powergrid-")
        assert len(fake_index_client.indexes) == 1
        assert fake_index_client.indexes[0].name == backend.index_name
        assert len(fake_search_client.uploaded_documents) == 2
        assert fake_search_client.uploaded_documents[0]["name"] == "run_opf"
        assert fake_search_client.uploaded_documents[0]["state_requires"] == ["grid.loaded"]
        assert fake_search_client.uploaded_documents[0]["description_vector"] == [0.1, 0.2, 0.3]
        assert fake_http_client.requests[0][0].endswith("api-version=2024-06-01")
        assert registered_callbacks == [backend._atexit_cleanup]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_search_returns_tool_search_results(self, sample_tools, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(azure_tool_search, "get_search_credential_async", lambda: object())
        backend = AzureAIToolSearchBackend(sample_tools, server_name="powergrid")
        backend._initialized = True
        backend._search_client = FakeSearchClient(
            responses=[
                FakeSearchResults(
                    [
                        {
                            "name": "run_opf",
                            "server_name": "powergrid",
                            "description": "Run optimal power flow",
                            "state_requires": ["grid.loaded"],
                            "state_produces": ["grid.solved"],
                            "@search.score": 1.5,
                        }
                    ]
                )
            ]
        )
        monkeypatch.setattr(backend, "_embed_text", AsyncMock(return_value=[0.1, 0.2, 0.3]))

        results = await backend.search("optimal power flow", top=1)

        assert len(results) == 1
        assert isinstance(results[0], ToolSearchResult)
        assert results[0].name == "run_opf"
        assert results[0].score == 1.5
        assert backend._search_client.search_calls[0]["query_type"] == "semantic"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_search_falls_back_to_keyword_only(self, sample_tools, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(azure_tool_search, "get_search_credential_async", lambda: object())
        backend = AzureAIToolSearchBackend(sample_tools, server_name="powergrid")
        fake_search_client = FakeSearchClient(
            responses=[
                RuntimeError("semantic unavailable"),
                FakeSearchResults(
                    [
                        {
                            "name": "build_network",
                            "server_name": "powergrid",
                            "description": "Build a network model",
                            "state_requires": [],
                            "state_produces": [],
                            "@search.score": 0.8,
                        }
                    ]
                ),
            ]
        )
        backend._initialized = True
        backend._search_client = fake_search_client
        monkeypatch.setattr(backend, "_embed_text", AsyncMock(return_value=[0.1, 0.2, 0.3]))

        results = await backend.search("network topology", top=1)

        assert [result.name for result in results] == ["build_network"]
        assert len(fake_search_client.search_calls) == 2
        assert fake_search_client.search_calls[0]["query_type"] == "semantic"
        assert "query_type" not in fake_search_client.search_calls[1]
        assert "vector_queries" not in fake_search_client.search_calls[1]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_close_deletes_index_and_cleans_up_resources(self, sample_tools, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(azure_tool_search, "get_search_credential_async", lambda: object())
        unregistered_callbacks: list = []
        monkeypatch.setattr(
            azure_tool_search.atexit, "unregister", lambda callback: unregistered_callbacks.append(callback)
        )

        backend = AzureAIToolSearchBackend(sample_tools, server_name="powergrid")
        fake_index_client = FakeIndexClient()
        backend._search_client = FakeClosable()
        backend._index_client = fake_index_client
        backend._http_client = FakeClosable()
        backend._credential = FakeClosable()
        backend._initialized = True
        backend._index_created = True
        backend._atexit_cleanup = lambda: None
        cleanup_callback = backend._atexit_cleanup

        await backend.close()

        assert fake_index_client.deleted_indexes == [backend.index_name]
        assert unregistered_callbacks == [cleanup_callback]
        assert backend._initialized is False
        assert backend._search_client is None
        assert backend._index_client is None
        assert backend._http_client is None
        assert backend._index_created is False
        assert backend._atexit_cleanup is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_initialize_registers_atexit_index_cleanup(
        self, sample_tools, azure_env, monkeypatch: pytest.MonkeyPatch
    ):
        fake_search_client = FakeSearchClient()
        fake_index_client = FakeIndexClient()
        fake_http_client = FakeAsyncClient([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])
        registered_callbacks: list = []
        deleted_indexes: list[tuple[str, str]] = []

        monkeypatch.setattr(azure_tool_search, "get_search_credential_async", lambda: object())
        monkeypatch.setattr(azure_tool_search, "SearchClient", lambda **kwargs: fake_search_client)
        monkeypatch.setattr(azure_tool_search, "SearchIndexClient", lambda **kwargs: fake_index_client)
        monkeypatch.setattr(azure_tool_search.httpx, "AsyncClient", lambda *args, **kwargs: fake_http_client)
        monkeypatch.setattr(
            azure_tool_search.atexit, "register", lambda callback: registered_callbacks.append(callback)
        )
        monkeypatch.setattr(
            azure_tool_search,
            "_delete_index_at_exit",
            lambda *, endpoint, index_name: deleted_indexes.append((endpoint, index_name)),
        )

        backend = AzureAIToolSearchBackend(sample_tools, server_name="powergrid")
        await backend.initialize()

        assert len(registered_callbacks) == 1
        assert registered_callbacks[0] is backend._atexit_cleanup
        registered_callbacks[0]()
        assert deleted_indexes == [(backend.endpoint, backend.index_name)]
