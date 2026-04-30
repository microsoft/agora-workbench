"""Tests for ToolSearchIndexManager lifecycle management."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tools.search.build_tool_list import ToolInfo
from tools.search.manager import (
    INDEX_NAME_PREFIX,
    TOOL_SEARCH_ENDPOINT_ENV,
    TOOL_SEARCH_VECTORIZER_ENDPOINT_ENV,
    TOOL_SEARCH_VECTORIZER_DEPLOYMENT_ENV,
    ToolSearchIndexManager,
)

_SEARCH_ENDPOINT = "https://s.search.windows.net"
_OPENAI_ENDPOINT = "https://oai.openai.azure.com"
_EMBEDDING_DEPLOYMENT = "text-embedding-3-large"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_info(
    name: str = "run_opf",
    description: str = "Run optimal power flow",
    server_name: str = "powergrid",
):
    return ToolInfo(name=name, description=description, server_name=server_name)


# ---------------------------------------------------------------------------
# ToolSearchIndexManager.from_env
# ---------------------------------------------------------------------------


class TestFromEnv:
    @pytest.fixture(autouse=True)
    def _set_required_env(self, monkeypatch):
        """Set the required env vars for all from_env tests."""
        monkeypatch.setenv(TOOL_SEARCH_ENDPOINT_ENV, _SEARCH_ENDPOINT)
        monkeypatch.setenv(TOOL_SEARCH_VECTORIZER_ENDPOINT_ENV, _OPENAI_ENDPOINT)
        monkeypatch.setenv(TOOL_SEARCH_VECTORIZER_DEPLOYMENT_ENV, _EMBEDDING_DEPLOYMENT)

    @pytest.mark.unit
    def test_raises_when_endpoint_missing(self, monkeypatch):
        monkeypatch.delenv(TOOL_SEARCH_ENDPOINT_ENV, raising=False)
        with pytest.raises(ValueError, match=TOOL_SEARCH_ENDPOINT_ENV):
            ToolSearchIndexManager.from_env()

    @pytest.mark.unit
    def test_raises_when_openai_endpoint_missing(self, monkeypatch):
        monkeypatch.delenv(TOOL_SEARCH_VECTORIZER_ENDPOINT_ENV, raising=False)
        with pytest.raises(ValueError, match=TOOL_SEARCH_VECTORIZER_ENDPOINT_ENV):
            ToolSearchIndexManager.from_env()

    @pytest.mark.unit
    def test_raises_when_embedding_deployment_missing(self, monkeypatch):
        monkeypatch.delenv(TOOL_SEARCH_VECTORIZER_DEPLOYMENT_ENV, raising=False)
        with pytest.raises(ValueError, match=TOOL_SEARCH_VECTORIZER_DEPLOYMENT_ENV):
            ToolSearchIndexManager.from_env()

    @pytest.mark.unit
    def test_index_name_auto_generated(self):
        mgr = ToolSearchIndexManager.from_env()
        assert mgr.index_name.startswith(INDEX_NAME_PREFIX + "-")
        assert len(mgr.index_name) == len(INDEX_NAME_PREFIX) + 1 + 8  # prefix-hex8

    @pytest.mark.unit
    def test_each_instance_gets_unique_index_name(self):
        mgr1 = ToolSearchIndexManager.from_env()
        mgr2 = ToolSearchIndexManager.from_env()
        assert mgr1.index_name != mgr2.index_name

    @pytest.mark.unit
    def test_trailing_slash_stripped(self):
        mgr = ToolSearchIndexManager.from_env()
        assert not mgr.search_endpoint.endswith("/")
        assert not mgr.azure_openai_endpoint.endswith("/")


# ---------------------------------------------------------------------------
# _render_index_definition
# ---------------------------------------------------------------------------


class TestRenderIndexDefinition:
    @pytest.mark.unit
    def test_renders_with_generated_index_name(self):
        mgr = ToolSearchIndexManager(
            search_endpoint=_SEARCH_ENDPOINT,
            azure_openai_endpoint=_OPENAI_ENDPOINT,
            azure_openai_embedding_deployment=_EMBEDDING_DEPLOYMENT,
        )
        definition = mgr._render_index_definition()
        assert definition["name"] == mgr.index_name
        assert definition["name"].startswith(INDEX_NAME_PREFIX + "-")
        assert "fields" in definition
        field_names = {f["name"] for f in definition["fields"]}
        assert "tool_id" in field_names
        assert "name" in field_names
        assert "description" in field_names
        assert "description_vector" in field_names
        assert "server_name" in field_names

    @pytest.mark.unit
    def test_renders_vector_search_config(self):
        mgr = ToolSearchIndexManager(
            search_endpoint=_SEARCH_ENDPOINT,
            azure_openai_endpoint=_OPENAI_ENDPOINT,
            azure_openai_embedding_deployment=_EMBEDDING_DEPLOYMENT,
        )
        definition = mgr._render_index_definition()
        assert "vectorSearch" in definition
        vectorizers = definition["vectorSearch"]["vectorizers"]
        assert len(vectorizers) == 1
        assert vectorizers[0]["azureOpenAIParameters"]["resourceUri"] == _OPENAI_ENDPOINT
        assert vectorizers[0]["azureOpenAIParameters"]["deploymentId"] == _EMBEDDING_DEPLOYMENT


# ---------------------------------------------------------------------------
# _build_documents
# ---------------------------------------------------------------------------


class TestBuildDocuments:
    @pytest.mark.unit
    def test_tool_document_structure(self):
        mgr = ToolSearchIndexManager(_SEARCH_ENDPOINT, _OPENAI_ENDPOINT, _EMBEDDING_DEPLOYMENT)
        tools = [_make_tool_info("run_opf", "Run OPF", "powergrid")]

        docs = mgr._build_documents(tools)

        assert len(docs) == 1
        doc = docs[0]
        assert doc["tool_id"] == "powergrid--run_opf"
        assert doc["name"] == "run_opf"
        assert doc["description"] == "Run OPF"
        assert doc["server_name"] == "powergrid"

    @pytest.mark.unit
    def test_tool_without_server_name(self):
        mgr = ToolSearchIndexManager(_SEARCH_ENDPOINT, _OPENAI_ENDPOINT, _EMBEDDING_DEPLOYMENT)
        tools = [_make_tool_info("run_opf", "Run OPF", "")]

        docs = mgr._build_documents(tools)

        assert docs[0]["tool_id"] == "run_opf"
        assert docs[0]["server_name"] == ""

    @pytest.mark.unit
    def test_empty_list_returns_no_documents(self):
        mgr = ToolSearchIndexManager(_SEARCH_ENDPOINT, _OPENAI_ENDPOINT, _EMBEDDING_DEPLOYMENT)
        docs = mgr._build_documents([])
        assert docs == []


# ---------------------------------------------------------------------------
# deploy_index
# ---------------------------------------------------------------------------


class TestDeployIndex:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_deploy_index_calls_put(self):
        mgr = ToolSearchIndexManager(_SEARCH_ENDPOINT, _OPENAI_ENDPOINT, _EMBEDDING_DEPLOYMENT)

        mock_response = MagicMock()
        mock_response.status_code = 201

        mock_get_token = AsyncMock(return_value="fake-token")
        mock_put = AsyncMock(return_value=mock_response)

        with patch.object(mgr, "_get_token", mock_get_token):
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_http = AsyncMock()
                mock_http.__aenter__ = AsyncMock(return_value=mock_http)
                mock_http.__aexit__ = AsyncMock(return_value=False)
                mock_http.put = mock_put
                mock_client_cls.return_value = mock_http

                await mgr.deploy_index()

        assert mgr._index_deployed is True
        put_call = mock_put.call_args
        assert f"{_SEARCH_ENDPOINT}/indexes/{mgr.index_name}" in str(put_call)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_deploy_index_raises_on_error(self):
        mgr = ToolSearchIndexManager(_SEARCH_ENDPOINT, _OPENAI_ENDPOINT, _EMBEDDING_DEPLOYMENT)

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"
        mock_response.raise_for_status = MagicMock(side_effect=Exception("403 Forbidden"))

        with patch.object(mgr, "_get_token", AsyncMock(return_value="tok")):
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_http = AsyncMock()
                mock_http.__aenter__ = AsyncMock(return_value=mock_http)
                mock_http.__aexit__ = AsyncMock(return_value=False)
                mock_http.put = AsyncMock(return_value=mock_response)
                mock_client_cls.return_value = mock_http

                with pytest.raises(Exception, match="403"):
                    await mgr.deploy_index()

        assert mgr._index_deployed is False


# ---------------------------------------------------------------------------
# populate_index
# ---------------------------------------------------------------------------


class TestPopulateIndex:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_skips_if_not_deployed(self):
        mgr = ToolSearchIndexManager(_SEARCH_ENDPOINT, _OPENAI_ENDPOINT, _EMBEDDING_DEPLOYMENT)
        tools = [_make_tool_info()]

        with patch.object(mgr, "_get_token", AsyncMock()):
            with patch("httpx.AsyncClient") as mock_client_cls:
                await mgr.populate_index(tools)
                mock_client_cls.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_uploads_documents(self):
        mgr = ToolSearchIndexManager(_SEARCH_ENDPOINT, _OPENAI_ENDPOINT, _EMBEDDING_DEPLOYMENT)
        mgr._index_deployed = True

        tools = [_make_tool_info("run_opf", "OPF")]

        result_body = {"value": [{"key": "run_opf", "status": True}]}
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = result_body

        with patch.object(mgr, "_get_token", AsyncMock(return_value="tok")):
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_http = AsyncMock()
                mock_http.__aenter__ = AsyncMock(return_value=mock_http)
                mock_http.__aexit__ = AsyncMock(return_value=False)
                mock_http.post = AsyncMock(return_value=mock_response)
                mock_client_cls.return_value = mock_http
                with patch.object(mgr, "_add_embeddings", new=AsyncMock()) as mock_embed:
                    await mgr.populate_index(tools)

        mock_embed.assert_called_once()

        mock_http.post.assert_called_once()
        post_call = mock_http.post.call_args
        batch = post_call.kwargs["json"]
        assert len(batch["value"]) == 1
        assert batch["value"][0]["@search.action"] == "upload"
        assert batch["value"][0]["name"] == "run_opf"


# ---------------------------------------------------------------------------
# delete_index
# ---------------------------------------------------------------------------


class TestDeleteIndex:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_no_op_when_not_deployed(self):
        mgr = ToolSearchIndexManager(_SEARCH_ENDPOINT, _OPENAI_ENDPOINT, _EMBEDDING_DEPLOYMENT)
        # Should not raise or call anything
        with patch.object(mgr, "_get_token", AsyncMock()) as mock_token:
            await mgr.delete_index()
            mock_token.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_deletes_index_via_http(self):
        mgr = ToolSearchIndexManager(_SEARCH_ENDPOINT, _OPENAI_ENDPOINT, _EMBEDDING_DEPLOYMENT)
        mgr._index_deployed = True

        mock_response = MagicMock()
        mock_response.status_code = 204

        with patch.object(mgr, "_get_token", AsyncMock(return_value="tok")):
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_http = AsyncMock()
                mock_http.__aenter__ = AsyncMock(return_value=mock_http)
                mock_http.__aexit__ = AsyncMock(return_value=False)
                mock_http.delete = AsyncMock(return_value=mock_response)
                mock_client_cls.return_value = mock_http

                await mgr.delete_index()

        assert mgr._index_deployed is False
        mock_http.delete.assert_called_once()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_swallows_errors_during_delete(self):
        mgr = ToolSearchIndexManager(_SEARCH_ENDPOINT, _OPENAI_ENDPOINT, _EMBEDDING_DEPLOYMENT)
        mgr._index_deployed = True

        with patch.object(mgr, "_get_token", AsyncMock(side_effect=Exception("auth failed"))):
            # Should not raise
            await mgr.delete_index()


# ---------------------------------------------------------------------------
# setup + cleanup registration
# ---------------------------------------------------------------------------


class TestSetup:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_setup_deploys_populates_registers(self):
        mgr = ToolSearchIndexManager(_SEARCH_ENDPOINT, _OPENAI_ENDPOINT, _EMBEDDING_DEPLOYMENT)
        tools = [_make_tool_info()]

        with patch.object(mgr, "deploy_index", new=AsyncMock()) as mock_deploy:
            with patch.object(mgr, "populate_index", new=AsyncMock()) as mock_populate:
                with patch.object(mgr, "_register_cleanup") as mock_register:
                    await mgr.setup(tools)

        mock_deploy.assert_called_once()
        mock_populate.assert_called_once_with(tools)
        mock_register.assert_called_once()

    @pytest.mark.unit
    def test_register_cleanup_idempotent(self):
        mgr = ToolSearchIndexManager(_SEARCH_ENDPOINT, _OPENAI_ENDPOINT, _EMBEDDING_DEPLOYMENT)
        import signal as _signal

        original_sigterm = _signal.getsignal(_signal.SIGTERM)
        original_sigint = _signal.getsignal(_signal.SIGINT)
        try:
            mgr._register_cleanup()
            mgr._register_cleanup()  # second call should be a no-op
            assert mgr._cleanup_registered is True
        finally:
            # Restore both signal handlers to avoid test interference
            _signal.signal(_signal.SIGTERM, original_sigterm)
            _signal.signal(_signal.SIGINT, original_sigint)


# ---------------------------------------------------------------------------
# Async context manager
# ---------------------------------------------------------------------------


class TestContextManager:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_aenter_returns_self(self):
        mgr = ToolSearchIndexManager(_SEARCH_ENDPOINT, _OPENAI_ENDPOINT, _EMBEDDING_DEPLOYMENT)
        result = await mgr.__aenter__()
        assert result is mgr

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_aexit_calls_delete(self):
        mgr = ToolSearchIndexManager(_SEARCH_ENDPOINT, _OPENAI_ENDPOINT, _EMBEDDING_DEPLOYMENT)
        with patch.object(mgr, "delete_index", new=AsyncMock()) as mock_delete:
            await mgr.__aexit__(None, None, None)
        mock_delete.assert_called_once()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_context_manager_cleans_up_on_exception(self):
        mgr = ToolSearchIndexManager(_SEARCH_ENDPOINT, _OPENAI_ENDPOINT, _EMBEDDING_DEPLOYMENT)
        mgr._index_deployed = True
        deleted = []

        with patch.object(mgr, "delete_index", new=AsyncMock(side_effect=lambda: deleted.append(True))):
            try:
                async with mgr:
                    raise RuntimeError("simulated error")
            except RuntimeError:
                pass

        assert deleted == [True]
