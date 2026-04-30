# Tool-Learning Vignette Index Deployment

Azure AI Search index, data source, skillset, and indexer definitions for the tool-learning vignette system. These resources populate the `tool-vignettes` search index from an Azure Table Storage table written to by the `VignetteMiddleware`.

## Schema

Defined in `index.jinja`. The index supports hybrid keyword + vector search with integrated vectorization.

| Field | Type | Filterable | Searchable | Purpose |
|-------|------|:---:|:---:|---------|
| `vignette_id` | String (key) | ✓ | | Base64-encoded RowKey from Table Storage |
| `kind` | String | ✓ | | `anti_pattern` or `repair_template` |
| `scope` | String | ✓ | | `global`, `org`, or `user` |
| `tenant_id` | String | ✓ | | Tenant ID for scope filtering |
| `user_id` | String | ✓ | | User ID for scope filtering |
| `tool_name` | String | ✓ | | Domain tool name (mandatory filter) |
| `error_class` | String | ✓ | | Error class (e.g. `ValueError`) |
| `confidence` | Double | ✓ | | Ranking score (0.0–1.0) |
| `title` | String | | ✓ | BM25 keyword search |
| `summary` | String | | ✓ | BM25 keyword search |
| `content_vector` | Vector (3072d) | | ✓ | HNSW cosine similarity via integrated vectorization |
| `payload_json` | String | | | Full serialized `Vignette` for round-trip deserialization |

## Deployment

### Prerequisites

- Azure CLI authenticated (`az login`)
- An Azure AI Search service
- An Azure Storage account with the `ToolVignettes` table
- An Azure OpenAI resource with a `text-embedding-3-large` deployment

### 1. Deploy the Index (once)

```bash
uv run deploy.py \
  --search-endpoint <search-endpoint-url> \
  --azure-openai-endpoint <openai-endpoint> \
  index
```

Creates a versioned index (`tool-vignettes-v1`) with an alias `tool-vignettes`.

### 2. Deploy a Source (data source + skillset + indexer)

```bash
uv run deploy.py \
  --search-endpoint <search-endpoint-url> \
  --azure-openai-endpoint <openai-endpoint> \
  source \
  --source-id <unique-source-id> \
  --storage-resource-id <storage-account-resource-id> \
  --managed-identity-id <managed-identity-resource-id>
```

Optional flags:
- `--table-name <name>` — override table name (default: `ToolVignettes`)
- `--azure-openai-embedding-deployment <name>` — override embedding model (default: `text-embedding-3-large`)
- `--deploy-only` — skip immediate indexer run

## How It Works

1. **Data source** (`datasource.jinja`) — connects to the Azure Table Storage table using a managed identity
2. **Skillset** (`skillset.jinja`) — uses Azure OpenAI to generate embeddings from the `summary` field
3. **Indexer** (`indexer.jinja`) — maps table entity fields to index fields, runs the skillset, and populates the index
4. **Index** (`index.jinja`) — stores the fields with vector search configured for hybrid retrieval

The `VignetteMiddleware` writes vignettes to Table Storage. The indexer periodically picks them up, generates embeddings, and makes them searchable. The `SearchVignetteRepo` then queries this index using hybrid search (BM25 + vector similarity) with OData filters for scope and tool name.
