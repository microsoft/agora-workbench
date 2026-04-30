# Data Lake Manifests

Consolidated Pydantic YAML manifest schemas and CLI runners for both the **ingestion pipeline** and **standalone utility operations**.

## Manifest Types

### 1. Ingestion Manifest (`IngestionManifest`)

Drives the full ingestion pipeline via the orchestrator. A single YAML file describes your data source, datasets (subfolders), and semantic metadata — the orchestrator chains the ingestion steps together automatically.

```bash
# Copy and fill out a manifest
cp data_lake/manifest/example_manifest.yaml my_manifest.yaml

# Validate it
uv run -m data_lake.ingest.orchestrator validate my_manifest.yaml

# Dry run (no changes made)
uv run -m data_lake.ingest.orchestrator ingest my_manifest.yaml --dry-run

# Run for real
uv run -m data_lake.ingest.orchestrator ingest my_manifest.yaml
```

#### Pipeline Steps

| Step | Name | What it does |
|------|------|-------------|
| **3** | Register & Scan | Registers the blob storage account in Microsoft Purview, creates a scan scoped to the subfolder, and applies a scan filter. |
| **4a** | Trigger Scan | Triggers the Purview scan and waits for it to complete. |
| **4b** | Create Indexer | Creates an Azure AI Search datasource and indexer for the `blob-details` index, scoped to the subfolder. |
| **5** | Push Descriptions | Writes the semantic descriptions from the manifest (subfolder-level + per-artifact) into Purview entity `userDescription` fields. |
| **6** | Sync Registry | Runs the sync module to merge blob-details + Purview metadata into the `artifact-registry` search index. |

Run a single step with `--step N`:

```bash
uv run -m data_lake.ingest.orchestrator ingest my_manifest.yaml --step 5
```

#### Ingestion Manifest Fields

| Section | Field | Required | Description |
|---------|-------|----------|-------------|
| `source` | `storage_account` | Yes | Azure Storage account name |
| | `resource_group` | Yes | Resource group containing the storage account |
| | `subscription_id` | Yes | Azure subscription ID |
| | `container` | Yes | Blob container name |
| | `managed_identity_id` | No | Falls back to `DEFAULT_IDENTITY_RESOURCE_ID` env var |
| `governance` | `purview_account` | Yes | Microsoft Purview account name |
| | `collection` | No | Default Purview collection (can be overridden per-dataset) |
| `datasets[]` | `subfolder` | No | Subfolder scope within the container |
| | `description` | Yes | High-level dataset description (applied to subfolder entity) |
| | `collection` | No | Per-dataset Purview collection (overrides governance default) |
| | `artifacts[]` | No | Per-file descriptions |
| `search` | `search_service` | No | Falls back to `DATA_LAKE_SEARCH_NAME` env var |
| `embedding` | `azure_openai_endpoint` | No | Falls back to `DATA_LAKE_VECTORIZER_ENDPOINT` env var |

#### Moving Entities Between Collections

The orchestrator CLI also supports moving Purview entities between collections:

```bash
# Dry run – see which entities would move
uv run -m data_lake.ingest.orchestrator move \
  --purview agora-purview \
  --storage grid0eastus2 \
  --container demo \
  --prefixes texas_grid \
  --to powergrid \
  --dry-run

# Execute the move
uv run -m data_lake.ingest.orchestrator move \
  --purview agora-purview \
  --storage grid0eastus2 \
  --container demo \
  --prefixes texas_grid \
  --to powergrid
```

### 2. Utility Manifest (`UtilityManifest`)

For standalone catalog audits and Purview metadata updates without running the full pipeline.

```bash
uv run -m data_lake.manifest.run_manifest my_utility.yaml
uv run -m data_lake.manifest.run_manifest my_utility.yaml --dry-run
```

Supports two optional operation blocks:
- `registry_query` — audit the artifact registry and optionally write results to JSON.
- `entity_updates` — batch `update_purview_entity()` calls under one `purview_account`.

#### Utility Manifest Fields

| Field | Required | Description |
|-------|----------|-------------|
| `version` | No | Manifest schema version (default: `"1"`) |
| `purview_account` | When `entity_updates` present | Purview account name |
| `registry_query.search_service` | Yes (if querying) | Azure AI Search service name |
| `registry_query.index_name` | No | Index name (default: `artifact-registry`) |
| `registry_query.filter_expression` | No | OData filter |
| `registry_query.top` | No | Max results (default: all) |
| `registry_query.select_fields` | No | Limit returned fields |
| `registry_query.output_path` | No | Write results to a JSON file |
| `entity_updates[].qualified_name` | Yes | Blob URL identifying the Purview entity |
| `entity_updates[].new_name` | No | New display name |
| `entity_updates[].new_description` | No | New `userDescription` value |

## Prerequisites

- **Azure CLI** authenticated (`az login`)
- **Environment variables** (or set equivalents in the manifest):
  - `DEFAULT_IDENTITY_RESOURCE_ID` – managed identity resource ID for Purview scans
  - `DATA_LAKE_SEARCH_NAME` – Azure AI Search service name
  - `DATA_LAKE_VECTORIZER_ENDPOINT` – Azure OpenAI embedding endpoint

## Files

| File | Description |
|------|-------------|
| `manifest.py` | Pydantic models for both `IngestionManifest` and `UtilityManifest` schemas |
| `run_manifest.py` | CLI runner for utility manifests |
| `example_manifest.yaml` | Combined reference template (both manifest types, fully commented) |
| `demo_manifest.yaml` | Runnable utility manifest for testing |
