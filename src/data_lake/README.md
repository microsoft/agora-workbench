# Data Lake Module

A unified data discovery and governance system for blob storage artifacts, combining Azure AI Search, Microsoft Purview, and Azure OpenAI to enable semantic search with RBAC-aware artifact retrieval.

## Local Development (No Azure Credentials)

For local-only development, you can use a file-backed catalog instead of Azure services:

1. Set `DATA_LAKE_LOCAL_CATALOG` to a YAML file path (example catalog: `tools/adapters/catalog.example.yaml`).
2. Leave `DATA_LAKE_SEARCH_ENDPOINT` unset.
3. Use data lake tools as usual; search runs locally using BM25 keyword ranking over artifact metadata.

Catalog format:

```yaml
artifacts:
  - artifact_id: "sample-weather-csv"
    name: "Daily Weather Observations"
    description: "NOAA daily weather station data for Pacific Northwest"
    artifact_type: "blob"
    domain: "earthscience"
    source: "local"
    storage_url: "./data/weather/daily_obs.csv"
    tags: ["weather", "noaa", "temperature"]
```

### Where to Store Data Files

The local catalog is **discovery only** — it tells the agent what data exists but doesn't serve files. The `storage_url` field is metadata the agent uses to generate code that reads the file. Where you place the actual data depends on your setup:

| Scenario | Data location | Notes |
|----------|--------------|-------|
| **Local filesystem** | `./data/` or `~/datasets/` | Simplest; use absolute or project-relative paths in `storage_url` |
| **Docker (MCP server)** | Mounted volume (e.g., `/data/`) | Mount via `docker-compose.yml` so the execution container can access files |
| **Azurite (blob emulator)** | `http://127.0.0.1:10000/devstoreaccount1/` | Use well-known connection string; `storage_url` is the local blob URL |

Example with a mounted volume for an MCP code execution server:

```yaml
# docker-compose.yml
services:
  chemistry:
    volumes:
      - ./data:/data:ro
```

The agent discovers the artifact via the catalog, sees `storage_url: "./data/weather/daily_obs.csv"`, and generates code like `pd.read_csv("/data/weather/daily_obs.csv")` to run in the execution environment.

## Architecture

The system has three primary components:

1. **Blob-Details Index** ([search/blob_details/](search/blob_details/)) — automated indexing of blob storage metadata (paths, sizes, content types)
2. **Microsoft Purview** ([semantic/](semantic/)) — data catalog with manual semantic annotations via `userDescription` fields
3. **Artifact Registry** ([search/registry/](search/registry/)) — discovery-optimized search index with vector embeddings (3072-dim, text-embedding-3-large) and mandatory RBAC filtering

The **sync script** ([sync/](sync/)) enriches the artifact registry by combining blob-details metadata with Purview descriptions and Azure OpenAI embeddings.

Additional modules:

- **Ingestion pipeline** ([ingest/](ingest/)) — manifest-driven orchestrator that automates steps 3–6
- **Manifest schemas** ([manifest/](manifest/)) — consolidated Pydantic YAML manifest definitions for both ingestion and utility workflows, plus a CLI runner for utility manifests
- **Utilities** ([utilities/](utilities/)) — standalone helpers (`update_purview_entity`, `list_artifact_registry`, `move_entities`):

```
Blob Storage → Blob-Details Index → Sync Script → Artifact Registry
                                      ↑
                                 Purview Catalog
                                 Azure OpenAI
```

## Initial Setup (Once per Account)

1. Deploy the blob-details index schema:

```bash
AZURE_AI_SEARCH_NAME="agora-ai-search"

uv run search/blob_details/deploy.py --search-service ${AZURE_AI_SEARCH_NAME} index
```

The Azure AI Search resource name may also be set with the environmental variable `DATA_LAKE_SEARCH_NAME`. Please see `AgoraAgentMAF/.env.example`.

2. Deploy the artifact-registry schema:

```bash
# Azure OpenAI endpoint/deployment to be used for vectorization
AZURE_OPENAI_ENDPOINT="https://agora-research-resource.cognitiveservices.azure.com/openai/deployments/text-embedding-3-large/embeddings?api-version=2023-05-15"
AZURE_OPENAI_DEPLOYMENT="text-embedding-3-large"

uv run search/registry/deploy.py --search-service agora-ai-search \
  --azure-openai-endpoint ${AZURE_OPENAI_ENDPOINT} \
  --azure-openai-embedding-deployment ${AZURE_OPENAI_DEPLOYMENT}
```

The vectorizer endpoint and deployment may also be set with the environmental variables `DATA_LAKE_VECTORIZER_ENDPOINT` and `DATA_LAKE_VECTORIZER_DEPLOYMENT`, respectively. Please see `AgoraAgentMAF/.env.example`.

3. Configure Purview to avoid grouping blobs in resource sets:

```bash
uv run semantic/deploy.py --account agora-purview configure-resource-sets
```

---

## Standalone Utility Helpers

The helpers in [utilities/](utilities/) can be imported directly when you need to inspect the artifact registry or make a targeted Purview metadata change without running the full ingestion pipeline.

These examples use `AzureCliCredential`, so authenticate first:

```bash
az login
```

If you prefer a YAML-driven workflow for repeatable catalog edits, copy and fill out [manifest/example_manifest.yaml](manifest/example_manifest.yaml), then run:

```bash
uv run -m data_lake.manifest.run_manifest data_lake/manifest/example_manifest.yaml
# Preview Purview changes only:
uv run -m data_lake.manifest.run_manifest data_lake/manifest/example_manifest.yaml --dry-run
```

That manifest supports two optional operation blocks:
- `registry_query` to audit the artifact registry and optionally write the results to JSON.
- `entity_updates` to batch `update_purview_entity()` calls under one `purview_account`.

### Update a Purview Entity Name or Description

Use `update_purview_entity()` to update the display name and/or `userDescription` of a single Purview entity identified by its qualified name (the blob URL).

```python
from data_lake.utilities.utilities import update_purview_entity

update_purview_entity(
  purview_account="agora-purview",
  qualified_name="https://coienergydata.blob.core.windows.net/energydata/US_Elec_Transmission_Line/lines.geojson",
  new_name="US transmission lines",
  new_description="Geospatial transmission line dataset for U.S. grid analysis.",
)
```

Notes:
- Pass only `new_name` or only `new_description` if you want to change one field and preserve the other.
- For directory-like entities, pass the qualified name with a trailing `/`; the helper will try the Purview container type first.
- Use `dry_run=True` to log the intended update without writing changes.

```python
update_purview_entity(
  purview_account="agora-purview",
  qualified_name="https://coienergydata.blob.core.windows.net/energydata/US_Elec_Transmission_Line/",
  new_description="Folder-level semantic description for transmission line artifacts.",
  dry_run=True,
)
```

### List Documents in the Artifact Registry

Use `list_artifact_registry()` to query the Azure AI Search artifact-registry index and return matching documents as Python dictionaries.

```python
from data_lake.utilities.utilities import list_artifact_registry

artifacts = list_artifact_registry(
  search_service="agora-ai-search",
  filter_expression="domain eq 'powergrid'",
  top=5,
  select_fields=["artifact_id", "name", "domain", "artifact_type"],
)

for artifact in artifacts:
  print(artifact)
```

Common uses:
- Audit which artifacts are currently indexed for a domain.
- Compare registry contents against blob storage or Purview after a sync.
- Limit the returned payload with `select_fields` when you only need a few columns.

If your deployment uses a non-default index alias/name, override it with `index_name`:

```python
artifacts = list_artifact_registry(
  search_service="agora-ai-search",
  index_name="artifact-registry-v1",
  top=20,
)
```

### Adding a New Data Source (Automated)

The fastest way to add a new data source is via the **ingestion manifest**. You fill out a single YAML file describing the source and its semantic metadata, then the orchestrator runs steps 3–6 automatically.

1. Copy the example manifest and edit it:

```bash
cp manifest/example_manifest.yaml my_source.yaml
# Edit my_source.yaml – fill in source, governance, and semantic sections
```

2. Validate the manifest:

```bash
uv run -m data_lake.ingest.orchestrator validate my_source.yaml
```

3. Run the full pipeline (or preview with `--dry-run`):

```bash
uv run -m data_lake.ingest.orchestrator ingest my_source.yaml --verbose
# Preview: uv run -m data_lake.ingest.orchestrator ingest my_source.yaml --dry-run
```

The orchestrator will:
- **Step 3** – Register the storage account in Purview & create a container scan
- **Step 4** – Trigger the Purview scan + deploy & run the blob-details indexer (in parallel)
- **Step 5** – Push the semantic descriptions from your manifest into Purview entities
- **Step 6** – Run the sync script to populate the artifact registry

You can also run individual steps with `--step N` (e.g. `--step 5` to re-push descriptions).

See [manifest/example_manifest.yaml](manifest/example_manifest.yaml) for the full manifest schema with documentation.

---

### Adding a New Data Source (Manual Steps)

Currently, only blob storage data sources are supported.

1. Register your source with Azure AI Search. This will create a data source for the blob storage container if not already present. An indexer specific to the blob container will be created and run.

```bash
# Azure ID for the Azure Storage account containing the data source
STORAGE_RESOURCE_ID="/subscriptions/7be6291d-d314-4fb5-8377-b89b8b116529/resourceGroups/agora-rg/providers/Microsoft.Storage/storageAccounts/grid0eastus2"

# Azure ID for the managed identity used to provide access to the Azure Storage account
MANAGED_IDENTITY_ID="/subscriptions/7be6291d-d314-4fb5-8377-b89b8b116529/resourceGroups/agora-rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/agora-identity"

uv run search/blob_details/deploy.py --search-service agora-ai-search source \
  --source-id grid0eastus2-demo \
  --storage-resource-id ${STORAGE_RESOURCE_ID} \
  --managed-identity-id ${MANAGED_IDENTITY_ID} \
  --container-name demo
```

The managed identity may also be set with the environmental variable `DEFAULT_IDENTITY_RESOURCE_ID`. Please see `AgoraAgentMAF/.env.example`.

The status of the indexer is, for the moment, only visible in the [portal](https://ms.portal.azure.com/?l=en.en-us#@microsoft.onmicrosoft.com/resource/subscriptions/7be6291d-d314-4fb5-8377-b89b8b116529/resourceGroups/agora-rg/providers/Microsoft.Search/searchServices/agora-ai-search/indexers).

2. Register your source with Purview:

```bash
# A Purview collection (existing or novel) with which the Azure Storage account should be associated.
PURVIEW_COLLECTION_NAME="power-grid"

uv run semantic/deploy.py --account agora-purview register-storage \
  --storage-account grid0eastus2 \
  --resource-group agora-rg \
  --subscription-id 7be6291d-d314-4fb5-8377-b89b8b116529 \
  --collection ${PURVIEW_COLLECTION_NAME}
```

3. Create a Purview scan, which will identify resources in a given blob container and catalog them.

```bash
uv run semantic/deploy.py --account agora-purview create-storage-scan \
  --storage-account grid0eastus2 \
  --container demo \
  --collection ${PURVIEW_COLLECTION_NAME} \
  --scan-name grid0eastus2_demo_scan

uv run semantic/deploy.py --account agora-purview scan \
  --storage-account grid0eastus2 \
  --scan-name grid0eastus2_demo_scan
```

The status of the scan is, for the moment, only visible in the [Purview GUI](https://ms.web.purview.azure.com/resource/agora-purview/main/catalog/home).

4. Wait for both the blob-details indexer and Purview scan to complete.

5. Add semantic metadata to your new artifacts using the [Purview GUI](https://ms.web.purview.azure.com/resource/agora-purview/main/catalog/home):

   - To each blob object, create an asset description describing the particulars of that artifact.
   - To each blob folder, create an asset description describing what the artifacts underneath that folder have in common. The closest folder with a defined description to a given artifact will be associated to that artifact.
   - At the moment, annotations at the container-level or higher are not utilized.

6. Sync the blob-details index and Purview catalog to update the artifact-registry:

```bash
# Azure OpenAI endpoint/deployment to be used for vectorization
# This must match those set during insertion of the artifact-registry schema
AZURE_OPENAI_ENDPOINT="https://agora-research-resource.cognitiveservices.azure.com/openai/deployments/text-embedding-3-large/embeddings?api-version=2023-05-15"
AZURE_OPENAI_DEPLOYMENT="text-embedding-3-large"

uv run sync/sync.py --search-service agora-ai-search \
  --purview-account agora-purview \
  --azure-openai-endpoint ${AZURE_OPENAI_ENDPOINT}
```

The vectorizer endpoint and deployment may also be set with the environmental variables `DATA_LAKE_VECTORIZER_ENDPOINT` and `DATA_LAKE_VECTORIZER_DEPLOYMENT`, respectively. Please see `AgoraAgentMAF/.env.example`.
