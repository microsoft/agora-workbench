# Blob Details Index

Azure AI Search index that stores storage metadata for blob artifacts (paths, sizes, content types). This is a **lookup** index — discovery queries should target the artifact registry instead.

## Schema

Defined in `index.json`. All fields are filterable and sortable but not searchable.

| Field | Type | Purpose |
|-------|------|---------|
| `artifact_id` | String (key) | Base64-encoded storage path |
| `metadata_storage_path` | String | Full blob URI |
| `metadata_storage_name` | String | Filename |
| `metadata_storage_size` | Int64 | Blob size in bytes |
| `metadata_storage_content_type` | String | MIME type |
| `metadata_storage_last_modified` | DateTimeOffset | Last modification timestamp |

## Deployment

### 1. Deploy the Index (once)

```bash
uv run deploy.py index --search-service <search-service-name>
```

Creates a versioned index (`blob-details-v1`) with an alias `blob-details`.

### 2. Deploy a Source (data source + indexer)

```bash
uv run deploy.py source \
  --search-service <search-service-name> \
  --source-id <unique-source-id> \
  --storage-resource-id <storage-account-resource-id> \
  --managed-identity-id <managed-identity-resource-id> \
  --container-name <blob-container-name>
```

Optional flags: `--container-query <subfolder>`, `--included-extensions json txt`, `--excluded-extensions zip tar`, `--deploy-only` (skip immediate indexer run).

Multiple sources can target different containers or storage accounts while writing to the same index.

## Indexer Behavior

- Extracts **storage metadata only** (`dataToExtract: "storageMetadata"`) — no blob content is indexed
- `metadata_storage_path` → `artifact_id` via `base64Encode` (primary key)
- `metadata_storage_path` is also stored as-is, which the sync script needs to query Purview by qualified name

## Integration

After blobs are indexed here, run the sync script to enrich and populate the artifact registry:

```bash
cd ../../sync
uv run sync.py --search-service agora-ai-search \
  --purview-account agora-purview \
  --azure-openai-endpoint "..." \
  --azure-openai-embedding-deployment text-embedding-3-large
```
