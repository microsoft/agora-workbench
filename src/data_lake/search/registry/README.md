# Artifact Registry Index

Azure AI Search index that serves as the discovery surface for the data lake. It stores enriched artifact metadata with vector embeddings for semantic search and mandatory RBAC filtering.

## Schema

The index schema is defined in `index.jinja` and auto-generated Pydantic models live in `models.py`.

### Key Fields

| Field | Type | Source | Purpose |
|-------|------|--------|---------|
| `artifact_id` | String (key) | blob-details | Stable ID derived from blob path (base64 encoded URL) |
| `name` | String | blob-details | Human-readable filename; title field for semantic search |
| `description` | String | Purview `userDescription` | Artifact-specific description |
| `description_vector` | Collection(Single) | Azure OpenAI | 3072-dim embedding of `description` |
| `semantic_dataset_id` | String | Purview hierarchy | ID of nearest parent with a description |
| `semantic_dataset_name` | String | Purview hierarchy | Parent entity name |
| `semantic_dataset_description` | String | Purview hierarchy | Parent description |
| `semantic_dataset_description_vector` | Collection(Single) | Azure OpenAI | 3072-dim embedding of parent description |
| `domain` | String | Purview collection | Business domain for faceting |
| `rbacScope` | String | Sync script | Azure Resource ID for access control filtering |
| `detail_index` / `detail_key` | String | Sync script | Routing to the blob-details lookup index |

## Pydantic Models

After modifying `index.jinja`, regenerate and validate:

```bash
uv run python generate_models.py           # regenerate models.py
uv run python generate_models.py --validate # check models match schema
```

## Deployment

```bash
uv run deploy.py \
  --search-service <search-service-name> \
  --azure-openai-endpoint "<endpoint-url>" \
  --azure-openai-embedding-deployment <deployment-name>
```

This creates a versioned index (e.g., `artifact-registry-v1`) with an alias `artifact-registry` pointing to it.

## Populating

The registry is populated exclusively by the sync script (`../sync/sync.py`). See `../sync/README.md`.
