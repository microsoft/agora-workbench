# Artifact Registry Sync

Synchronizes artifact metadata from the blob-details index and Purview catalog to the artifact registry index.

## How It Works

For each artifact in the blob-details index, the sync script:

1. Retrieves the blob entity from Purview using `metadata_storage_path` (blob URL)
2. Extracts artifact description from Purview `userDescription` field
3. Walks up the blob path hierarchy to find the nearest parent with a description (semantic parent)
4. Generates vector embeddings via Azure OpenAI (3072-dim, text-embedding-3-large)
5. Uploads the enriched document to the artifact registry

## Prerequisites

- **Azure AI Search** with `blob-details` and `artifact-registry` indexes deployed (see `../search/`)
- **Microsoft Purview** with storage scanned and `userDescription` annotations added (see `../semantic/`)
- **Azure OpenAI** with an embedding model deployed
- **Azure CLI** logged in (`az login`)
- Roles: Search Index Data Contributor, Purview Data Curator, Cognitive Services OpenAI User

## Usage

```bash
# Sync all artifacts
uv run sync.py \
  --search-service agora-ai-search \
  --purview-account agora-purview \
  --azure-openai-endpoint "https://agora-research-resource.cognitiveservices.azure.com" \
  --azure-openai-embedding-deployment text-embedding-3-large

# Sync a single artifact by ID
uv run sync.py ... --artifact-id <base64-encoded-blob-url>

# Dry run (preview without uploading)
uv run sync.py ... --dry-run

# Filter by OData expression
uv run sync.py ... --filter "artifact_type eq 'blob' and created_at ge 2026-01-01T00:00:00Z"

# Clean up stale artifact-registry entries (Purview entity no longer exists)
uv run sync.py ... --cleanup

# Clean up with custom safeguard limits
uv run sync.py ... --cleanup --max-cleanup 100 --cleanup-threshold 0.3

# Verify blobs still exist in storage before enrichment
uv run sync.py ... --cleanup --verify-blobs
```

## Cleanup & Safeguards

When `--cleanup` is enabled, artifacts whose Purview entity no longer exists are deleted from the **artifact-registry** index only. The blob-details index is never modified (it is the source of truth populated by the indexer).

Three safeguards prevent accidental mass deletion:

1. **Not-found vs transient error distinction** — Only confirmed `ResourceNotFoundError` from Purview triggers cleanup. Transient errors (timeouts, 500s, auth failures) increment the `failed` counter instead.
2. **Max cleanup cap** (`--max-cleanup`, default: 50) — Limits the number of deletions per run. Once reached, remaining stale artifacts are skipped.
3. **Circuit breaker** (`--cleanup-threshold`, default: 0.2) — If the ratio of cleaned-to-processed artifacts exceeds this threshold (after 10+ processed), further deletions are halted. Protects against Purview outages causing false positives.

## CLI Options

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `--search-service` | Yes | — | Azure AI Search service name |
| `--purview-account` | Yes | — | Microsoft Purview account name |
| `--azure-openai-endpoint` | Yes | — | Azure OpenAI resource endpoint URL |
| `--azure-openai-embedding-deployment` | Yes | — | Embedding model deployment name |
| `--blob-details-index` | No | `blob-details` | Source index name |
| `--artifact-registry-index` | No | `artifact-registry` | Target index name |
| `--filter` | No | — | OData filter expression |
| `--artifact-id` | No | — | Specific artifact ID to sync |
| `--batch-size` | No | `100` | Documents per upload batch |
| `--dry-run` | No | `False` | Preview without uploading |
| `--cleanup` | No | `False` | Delete stale artifact-registry entries when Purview entity is missing |
| `--max-cleanup` | No | `50` | Max stale entries to delete per run |
| `--cleanup-threshold` | No | `0.2` | Max cleaned/processed ratio before halting (circuit breaker) |
| `--verify-blobs` | No | `False` | HEAD-check each blob URL before enrichment (adds latency) |
| `--verbose` | No | `False` | Detailed logging |
