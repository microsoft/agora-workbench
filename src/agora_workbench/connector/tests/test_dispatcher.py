"""Integration tests for DispatcherServer."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agora_workbench.connector import DispatcherConfig, WorkerConfig
from agora_workbench.connector.dispatcher import DispatcherServer, _get_session_id_from_context


# Sample catalog that all identical workers return
WORKER_CATALOG = {
    "server_name": "chemistry",
    "execution": {
        "mode": "adaptive",
        "default_timeout": 21600,
        "max_timeout": 86400,
        "promotion_threshold_s": 45,
    },
    "tools": [
        {
            "name": "execute_code",
            "description": "Execute Python code in the chemistry environment.",
            "module": "chemistry.server",
            "required_parameters": [
                {"name": "code", "type": "builtins.str", "description": "Python code"},
            ],
            "optional_parameters": [
                {"name": "timeout", "type": "builtins.int", "description": "Timeout", "default": 300},
            ],
            "return_spec": [],
            "state_transition": {"requires": [], "produces": []},
            "affordances": ["code execution", "chemistry"],
        },
        {
            "name": "compute_descriptors",
            "description": "Compute molecular descriptors.",
            "module": "chemistry.tools",
            "required_parameters": [
                {"name": "smiles", "type": "builtins.str", "description": "SMILES input"},
            ],
            "optional_parameters": [],
            "return_spec": [],
            "state_transition": {"requires": [], "produces": []},
            "affordances": ["molecular properties", "descriptors"],
        },
    ],
}


def _mock_catalog_response(catalog: dict | None = None) -> httpx.Response:
    """Create a mock catalog response."""
    request = httpx.Request("GET", "http://mock/catalog")
    return httpx.Response(200, json=WORKER_CATALOG if catalog is None else catalog, request=request)


def _mock_health_response(healthy: bool = True) -> httpx.Response:
    """Create a mock health response."""
    request = httpx.Request("GET", "http://mock/health")
    status = 200 if healthy else 503
    return httpx.Response(status, json={"status": "healthy" if healthy else "unhealthy"}, request=request)


def _mock_mcp_response(text: str = "result") -> httpx.Response:
    """Create a mock MCP tool call response."""
    request = httpx.Request("POST", "http://mock/mcp")
    data = {
        "jsonrpc": "2.0",
        "id": "test-id",
        "result": {
            "content": [{"type": "text", "text": text}],
        },
    }
    return httpx.Response(200, json=data, request=request)


def _mock_mcp_init_response(session_id: str = "session-123") -> httpx.Response:
    """Create a mock MCP initialize response."""
    request = httpx.Request("POST", "http://mock/mcp")
    data = {
        "jsonrpc": "2.0",
        "id": "init-id",
        "result": {"protocolVersion": "2025-03-26", "capabilities": {}},
    }
    return httpx.Response(200, json=data, headers={"mcp-session-id": session_id}, request=request)


@pytest.fixture
def two_worker_config():
    return DispatcherConfig(
        name="chem-dispatcher",
        description="Chemistry worker pool",
        workers=[
            WorkerConfig(name="worker-1", url="http://worker-1:8000"),
            WorkerConfig(name="worker-2", url="http://worker-2:8000"),
        ],
        strategy="round_robin",
        session_affinity=True,
        health_check_interval=10.0,
    )


@pytest.fixture
def two_worker_weighted_config():
    return DispatcherConfig(
        name="chem-dispatcher",
        workers=[
            WorkerConfig(name="worker-1", url="http://worker-1:8000", weight=2),
            WorkerConfig(name="worker-2", url="http://worker-2:8000", weight=1),
        ],
        strategy="round_robin",
    )


class TestDispatcherConfig:
    """Tests for DispatcherConfig model."""

    def test_basic_config(self, two_worker_config):
        assert two_worker_config.name == "chem-dispatcher"
        assert len(two_worker_config.workers) == 2
        assert two_worker_config.strategy == "round_robin"
        assert two_worker_config.session_affinity is True

    def test_defaults(self):
        config = DispatcherConfig(
            name="test",
            workers=[WorkerConfig(name="w1", url="http://w1:8000")],
        )
        assert config.strategy == "round_robin"
        assert config.session_affinity is True
        assert config.health_check_interval == 10.0
        assert config.worker_failure_policy == "error"

    def test_worker_weight_default(self):
        w = WorkerConfig(name="w1", url="http://w1:8000")
        assert w.weight == 1

    def test_sticky_session_requires_affinity(self):
        """sticky_session strategy with session_affinity=False raises ValueError."""
        with pytest.raises(ValueError, match="requires session_affinity=True"):
            DispatcherConfig(
                name="test",
                workers=[WorkerConfig(name="w1", url="http://w1:8000")],
                strategy="sticky_session",
                session_affinity=False,
            )


class TestDispatcherCatalogSync:
    """Tests for catalog fetching from workers."""

    @pytest.mark.asyncio
    async def test_fetches_catalog_from_first_worker(self, two_worker_config):
        """Dispatcher fetches catalog from first reachable worker."""
        server = DispatcherServer(two_worker_config)

        async def mock_get(url, **kwargs):
            return _mock_catalog_response()

        with patch("agora_workbench.connector.dispatcher.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await server._sync_dispatcher_catalog()

        # Catalog stored for all workers
        assert "worker-1" in server._upstream_catalogs
        assert "worker-2" in server._upstream_catalogs
        assert len(server._upstream_catalogs["worker-1"]) == 2
        assert server._upstream_execution_settings["worker-2"]["default_timeout"] == 21600

        server._setup_tools()
        tools = await server.mcp.list_tools()
        execute_code = next(t for t in tools if t.name == "execute_code")
        timeout_schema = execute_code.parameters["properties"]["timeout"]
        assert "configured default of 21600 seconds" in timeout_schema["description"]
        assert "45-second promotion threshold" in timeout_schema["description"]
        assert not (
            isinstance(execute_code.output_schema, dict) and execute_code.output_schema.get("x-fastmcp-wrap-result")
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_second_worker(self, two_worker_config):
        """If first worker is unreachable, fetches catalog from second."""
        server = DispatcherServer(two_worker_config)
        call_count = {"n": 0}

        async def mock_get(url, **kwargs):
            call_count["n"] += 1
            if "worker-1" in url:
                raise httpx.RequestError("connection refused")
            return _mock_catalog_response()

        with patch("agora_workbench.connector.dispatcher.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await server._sync_dispatcher_catalog()

        assert "worker-2" in server._upstream_catalogs
        # worker-1 marked unhealthy
        assert "worker-1" not in server._healthy_workers

    @pytest.mark.asyncio
    async def test_missing_execution_settings_clear_all_worker_aliases(self, two_worker_config):
        """A catalog without execution metadata must not retain stale worker guidance."""
        server = DispatcherServer(two_worker_config)
        server._upstream_execution_settings = {
            "worker-1": {"default_timeout": 300},
            "worker-2": {"default_timeout": 300},
        }
        catalog_without_execution = {key: value for key, value in WORKER_CATALOG.items() if key != "execution"}

        async def mock_get(url, **kwargs):
            return _mock_catalog_response(catalog_without_execution)

        with patch("agora_workbench.connector.dispatcher.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await server._sync_dispatcher_catalog()

        assert server._upstream_execution_settings == {}

    @pytest.mark.asyncio
    async def test_discovers_execute_tool_name(self, two_worker_config):
        """Dispatcher discovers the execute_code tool name from catalog."""
        server = DispatcherServer(two_worker_config)

        async def mock_get(url, **kwargs):
            return _mock_catalog_response()

        with patch("agora_workbench.connector.dispatcher.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await server._sync_dispatcher_catalog()

        assert server._upstream_execute_tool_name == "execute_code"


class TestDispatcherRouting:
    """Tests for routing strategies."""

    @pytest.mark.asyncio
    async def test_round_robin_distributes_evenly(self, two_worker_config):
        """Round robin distributes calls across workers."""
        two_worker_config.session_affinity = False
        server = DispatcherServer(two_worker_config)

        selections = []
        for _ in range(6):
            # Each call gets a unique session to avoid affinity
            import uuid

            session_id = str(uuid.uuid4())
            worker = await server._select_worker(session_id)
            selections.append(worker)

        assert selections.count("worker-1") == 3
        assert selections.count("worker-2") == 3

    @pytest.mark.asyncio
    async def test_weighted_round_robin(self, two_worker_weighted_config):
        """Weighted round robin respects worker weights."""
        two_worker_weighted_config.session_affinity = False
        server = DispatcherServer(two_worker_weighted_config)

        selections = []
        for _ in range(6):
            import uuid

            session_id = str(uuid.uuid4())
            worker = await server._select_worker(session_id)
            selections.append(worker)

        # weight 2:1, so in 6 calls: worker-1 gets 4, worker-2 gets 2
        assert selections.count("worker-1") == 4
        assert selections.count("worker-2") == 2

    @pytest.mark.asyncio
    async def test_session_affinity(self, two_worker_config):
        """Once a session is assigned, subsequent calls go to same worker."""
        server = DispatcherServer(two_worker_config)

        session_id = "test-session-123"
        first_worker = await server._select_worker(session_id)

        # All subsequent calls should go to the same worker
        for _ in range(5):
            worker = await server._select_worker(session_id)
            assert worker == first_worker

    @pytest.mark.asyncio
    async def test_least_loaded_picks_least_busy(self):
        """Least loaded picks worker with fewest active calls."""
        config = DispatcherConfig(
            name="test",
            workers=[
                WorkerConfig(name="worker-1", url="http://w1:8000"),
                WorkerConfig(name="worker-2", url="http://w2:8000"),
            ],
            strategy="least_loaded",
            session_affinity=False,
        )
        server = DispatcherServer(config)

        # Simulate worker-1 being busy
        server._active_calls["worker-1"] = 5
        server._active_calls["worker-2"] = 1

        import uuid

        worker = await server._select_worker(str(uuid.uuid4()))
        assert worker == "worker-2"

    @pytest.mark.asyncio
    async def test_no_healthy_workers_returns_none(self, two_worker_config):
        """Returns None when no workers are healthy."""
        server = DispatcherServer(two_worker_config)
        server._healthy_workers = set()

        worker = await server._select_worker("session-1")
        assert worker is None


class TestDispatcherSessionAffinity:
    """Tests for session affinity and worker failure."""

    @pytest.mark.asyncio
    async def test_worker_failure_error_policy(self, two_worker_config):
        """Error policy returns None when assigned worker is unhealthy."""
        two_worker_config.worker_failure_policy = "error"
        server = DispatcherServer(two_worker_config)

        session_id = "test-session"
        # Assign to worker-1
        server._session_affinity_map[session_id] = "worker-1"
        # Mark worker-1 unhealthy
        server._healthy_workers.discard("worker-1")

        result = await server._select_worker(session_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_worker_failure_reroute_policy(self, two_worker_config):
        """Reroute policy assigns a new worker when assigned one is unhealthy."""
        two_worker_config.worker_failure_policy = "reroute"
        server = DispatcherServer(two_worker_config)

        session_id = "test-session"
        # Assign to worker-1
        server._session_affinity_map[session_id] = "worker-1"
        # Mark worker-1 unhealthy
        server._healthy_workers.discard("worker-1")

        result = await server._select_worker(session_id)
        assert result == "worker-2"
        # Affinity updated
        assert server._session_affinity_map[session_id] == "worker-2"


class TestDispatcherHealthCheck:
    """Tests for health checking."""

    @pytest.mark.asyncio
    async def test_unhealthy_worker_removed(self, two_worker_config):
        """Health check removes unhealthy workers from the pool."""
        server = DispatcherServer(two_worker_config)

        async def mock_get(url, **kwargs):
            if "worker-1" in url:
                return _mock_health_response(healthy=True)
            else:
                return _mock_health_response(healthy=False)

        with patch("agora_workbench.connector.dispatcher.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await server._poll_worker_health()

        assert "worker-1" in server._healthy_workers
        assert "worker-2" not in server._healthy_workers

    @pytest.mark.asyncio
    async def test_recovered_worker_readded(self, two_worker_config):
        """Health check restores workers that recover."""
        server = DispatcherServer(two_worker_config)
        # Start with worker-2 unhealthy
        server._healthy_workers.discard("worker-2")

        async def mock_get(url, **kwargs):
            return _mock_health_response(healthy=True)

        with patch("agora_workbench.connector.dispatcher.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await server._poll_worker_health()

        assert "worker-1" in server._healthy_workers
        assert "worker-2" in server._healthy_workers

    @pytest.mark.asyncio
    async def test_connection_error_marks_unhealthy(self, two_worker_config):
        """Workers that raise connection errors are marked unhealthy."""
        server = DispatcherServer(two_worker_config)

        async def mock_get(url, **kwargs):
            raise httpx.ConnectError("refused")

        with patch("agora_workbench.connector.dispatcher.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await server._poll_worker_health()

        assert len(server._healthy_workers) == 0


class TestDispatcherToolRegistration:
    """Tests for tool registration."""

    @pytest.mark.asyncio
    async def test_registers_execute_code_tool(self, two_worker_config):
        """Dispatcher registers a single execute_code tool."""
        server = DispatcherServer(two_worker_config)

        async def mock_get(url, **kwargs):
            return _mock_catalog_response()

        with patch("agora_workbench.connector.dispatcher.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await server._sync_dispatcher_catalog()

        server._setup_tools()

        # Verify the tool was registered on the MCP instance
        tools = await server.mcp.list_tools()
        tool_names = [tool.name for tool in tools]
        assert "execute_code" in tool_names


class TestDispatcherProxyCall:
    """Tests for proxied tool calls."""

    @pytest.mark.asyncio
    async def test_proxy_call_success(self, two_worker_config):
        """Successful proxy call returns result text."""
        server = DispatcherServer(two_worker_config)
        server._upstream_execute_tool_name = "execute_code"
        upstream = server._get_upstream_for_worker("worker-1")

        async def mock_post(url, **kwargs):
            if "initialize" in str(kwargs.get("json", {}).get("method", "")):
                return _mock_mcp_init_response("sess-abc")
            return _mock_mcp_response("Hello from worker-1!")

        with patch("agora_workbench.connector.dispatcher.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("agora_workbench.connector.dispatcher.get_current_request_token", return_value="token-123"):
                result = await server._proxy_dispatcher_call(
                    upstream=upstream,
                    tool_name="execute_code",
                    arguments={"code": "print('hi')", "description": ""},
                    ctx=None,
                    connector_session_id="conn-session-1",
                )

        assert "Hello from worker-1!" in result

    @pytest.mark.asyncio
    async def test_proxy_call_marks_unhealthy_on_failure(self, two_worker_config):
        """Proxy call marks worker unhealthy on connection error."""
        server = DispatcherServer(two_worker_config)
        server._upstream_execute_tool_name = "execute_code"
        upstream = server._get_upstream_for_worker("worker-1")

        # Pre-establish a session so the failure happens at the tool call level
        session_key = ("conn-session-1", "worker-1")
        server._dispatcher_sessions[session_key] = "existing-session-id"

        async def mock_post(url, **kwargs):
            raise httpx.ConnectError("refused")

        with patch("agora_workbench.connector.dispatcher.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("agora_workbench.connector.dispatcher.get_current_request_token", return_value=None):
                result = await server._proxy_dispatcher_call(
                    upstream=upstream,
                    tool_name="execute_code",
                    arguments={"code": "x", "description": ""},
                    ctx=None,
                    connector_session_id="conn-session-1",
                )

        assert "worker-1" not in server._healthy_workers
        assert "Cannot reach worker" in result


class TestGetSessionIdFromContext:
    """Tests for session ID extraction."""

    def test_extracts_from_context(self):
        ctx = MagicMock()
        ctx.session_id = "my-session-id"
        assert _get_session_id_from_context(ctx) == "my-session-id"

    def test_fallback_on_none_context(self):
        result = _get_session_id_from_context(None)
        # Should return a UUID string
        assert len(result) == 36  # UUID format

    def test_fallback_on_missing_attr(self):
        ctx = MagicMock(spec=[])  # No attributes
        result = _get_session_id_from_context(ctx)
        assert len(result) == 36
