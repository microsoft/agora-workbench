# Extension points (abstract interfaces)

Agora Workbench is designed around abstract interfaces that you can implement to customize behavior without modifying the core. This guide documents the key extension points.

## Authentication interfaces

Defined in `code_execution.auth.base`:

### TokenValidator

Validates bearer tokens from incoming requests:

```python
class TokenValidator(ABC):
    @abstractmethod
    async def validate(self, token: str, *, request_path: str = "/mcp", request_method: str = "POST") -> dict:
        """Validate a bearer token and return decoded claims."""
        ...
```

Built-in implementations: `EntraTokenValidator`, `NoOpTokenValidator`

### IdentityExtractor

Derives a unique user identity string from token claims:

```python
class IdentityExtractor(ABC):
    @abstractmethod
    def extract(self, claims: dict) -> str | None:
        """Return a unique user identifier from decoded claims."""
        ...
```

Built-in implementations: `EntraIdentityExtractor`, `NoOpIdentityExtractor`

### CredentialProvider

Provides credentials for accessing downstream Azure resources:

```python
class CredentialProvider(ABC):
    @abstractmethod
    async def get_token(self, scope: str) -> AccessToken:
        """Obtain an access token for the given resource scope."""
        ...

    async def close(self) -> None:
        """Release any held resources."""
```

Built-in implementations: `EntraCredentialProvider`, `NoOpCredentialProvider`

## Tool search backends

Defined in `code_execution.tools.tool_search`:

### ToolSearchBackend

Pluggable backend for the `search_{name}_tools` MCP tool:

```python
class ToolSearchBackend(ABC):
    def index(self, tools: list[ToolInfo], skills: list[dict] | None = None, server_name: str = "") -> None:
        """Receive the tool catalog and skill metadata to index."""
        ...

    @abstractmethod
    async def search(self, query: str, top: int = 5, category: SearchCategory = "all") -> list[ToolSearchResult]:
        """Search tools by natural-language query."""
        ...
```

Built-in implementations:

| Backend | Class | Description |
|---------|-------|-------------|
| BM25 | `BM25ToolSearchBackend` | Local keyword search, zero dependencies |
| Azure AI Search | `AzureAIToolSearchBackend` | Cloud-hosted semantic/vector search |

### Custom search backend example

```python
from code_execution.tools.tool_search import ToolSearchBackend, ToolInfo, ToolSearchResult, SearchCategory

class ElasticsearchToolSearch(ToolSearchBackend):
    def __init__(self, es_client, index_name: str):
        self._client = es_client
        self._index = index_name

    def index(self, tools: list[ToolInfo], skills: list[dict] | None = None, server_name: str = "") -> None:
        for tool in tools:
            self._client.index(index=self._index, body={
                "name": tool.name,
                "description": tool.description,
                "server_name": tool.server_name,
            })

    async def search(self, query: str, top: int = 5, category: SearchCategory = "all") -> list[ToolSearchResult]:
        results = self._client.search(
            index=self._index,
            body={"query": {"match": {"description": query}}},
            size=top,
        )
        return [
            ToolSearchResult(
                name=hit["_source"]["name"],
                server_name=hit["_source"]["server_name"],
                description=hit["_source"]["description"],
                execution_type="mcp",
                score=hit["_score"],
            )
            for hit in results["hits"]["hits"]
        ]
```

Pass to the server:

```python
from code_execution.auth import create_noop_auth_config

server = CodeExecutionServer(
    server_config=config,
    auth_config=create_noop_auth_config(),
    tool_search_backend=ElasticsearchToolSearch(es, "tools"),
)
```

## Data access: AssetFetcher

Defined in `code_execution.data_access.fetchers`:

### AssetFetcher

Fetch assets from custom sources:

```python
class AssetFetcher(ABC):
    @abstractmethod
    def can_handle(self, uri: str) -> bool:
        """Return True if this fetcher handles the given URI scheme."""
        ...

    @abstractmethod
    async def fetch(self, uri: str, destination: Path) -> None:
        """Download the asset to the local destination path."""
        ...
```

Built-in implementations: `BlobFetcher` (Azure Blob), `LocalFileFetcher`

### Custom fetcher example

```python
from code_execution.data_access.fetchers import AssetFetcher

class S3Fetcher(AssetFetcher):
    def can_handle(self, uri: str) -> bool:
        return uri.startswith("s3://")

    async def fetch(self, uri: str, destination: Path) -> None:
        # Download from S3 to destination
        ...
```

## Data access: AssetPublisher

Defined in `code_execution.data_access.publishers`:

### AssetPublisher

Publish artifacts from code execution to external storage:

```python
class AssetPublisher(ABC):
    @abstractmethod
    def can_handle(self, destination: str) -> bool:
        """Return True if this publisher handles the given destination."""
        ...

    @abstractmethod
    async def publish(self, source: Path, destination: str, metadata: dict) -> str:
        """Publish the file and return a URL or reference."""
        ...
```

Built-in implementations: `GuiPublisher`, `LocalFilePublisher`, `BlobPublisher`

## ConnectorServer

Defined in `connector.base`:

### Abstract methods for custom connectors

```python
class ConnectorServer(BaseMCPServer):
    @abstractmethod
    def _setup_tools(self) -> None:
        """Register mode-specific proxy tools."""
        ...

    @abstractmethod
    def _get_upstreams(self) -> list[UpstreamConfig]:
        """Declare the upstream server list."""
        ...
```

Built-in implementations: `RouterServer`, `GatewayServer`, `DispatcherServer`

## CodeExecutionServer hooks

### preprocess_code

Override to inject imports, setup code, or transform user code before execution:

```python
class MyServer(CodeExecutionServer):
    def preprocess_code(self, code: str) -> str:
        return "import numpy as np\n" + code
```

## Summary of extension points

| Interface | Module | Purpose |
|-----------|--------|---------|
| `TokenValidator` | `code_execution.auth.base` | Custom token validation |
| `IdentityExtractor` | `code_execution.auth.base` | Custom identity derivation |
| `CredentialProvider` | `code_execution.auth.base` | Custom downstream credentials |
| `ToolSearchBackend` | `code_execution.tools.tool_search` | Custom tool search |
| `AssetFetcher` | `code_execution.data_access.fetchers` | Custom data sources |
| `AssetPublisher` | `code_execution.data_access.publishers` | Custom artifact output |
| `ConnectorServer` | `connector.base` | Custom server composition |
| `CodeExecutionServer.preprocess_code` | `code_execution.server` | Code preprocessing |
