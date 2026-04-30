# Semantic Metadata — Microsoft Purview

Purview integration for cataloging blob storage and enriching artifacts with semantic metadata through `userDescription` annotations.

## Prerequisites

Grant yourself the necessary Purview RBAC roles:

```bash
# Data Curator (create/edit semantic datasets)
az role assignment create \
  --role "Purview Data Curator" \
  --assignee-object-id $(az ad signed-in-user show --query id -o tsv) \
  --scope /subscriptions/<sub-id>/resourceGroups/<rg>/providers/Microsoft.Purview/accounts/<purview-account>

# Data Source Administrator (register data sources, create scans)
az role assignment create \
  --role "Purview Data Source Administrator" \
  --assignee-object-id $(az ad signed-in-user show --query id -o tsv) \
  --scope /subscriptions/<sub-id>/resourceGroups/<rg>/providers/Microsoft.Purview/accounts/<purview-account>
```

Grant Purview's managed identity `Storage Blob Data Reader` on each storage account it needs to scan:

```bash
PURVIEW_PRINCIPAL_ID=$(az purview account show \
  --name <purview-account> \
  --resource-group <resource-group> \
  --query identity.principalId -o tsv)

az role assignment create \
  --role "Storage Blob Data Reader" \
  --assignee-object-id $PURVIEW_PRINCIPAL_ID \
  --scope /subscriptions/<sub-id>/resourceGroups/<rg>/providers/Microsoft.Storage/storageAccounts/<storage-account>
```

## CLI Commands

All commands use `deploy.py`:

```bash
# One-time: disable resource set grouping (catalogs each blob individually)
uv run deploy.py --account agora-purview configure-resource-sets

# Register a storage account as a data source
uv run deploy.py --account agora-purview register-storage \
  --storage-account grid0eastus2 \
  --resource-group agora-rg \
  --subscription-id <sub-id> \
  --collection power-grid

# Create a scan targeting a specific container
uv run deploy.py --account agora-purview create-storage-scan \
  --storage-account grid0eastus2 \
  --container demo \
  --collection power-grid \
  --scan-name grid0eastus2_demo_scan

# Trigger the scan (--wait blocks until completion)
uv run deploy.py --account agora-purview scan \
  --storage-account grid0eastus2 \
  --scan-name grid0eastus2_demo_scan \
  --wait
```

## Annotation Guidelines

After scanning, add `userDescription` annotations via the [Purview web UI](https://ms.web.purview.azure.com):

- **Files**: describe the specific artifact (e.g., "Texas electricity pypsa network with 1874 buses")
- **Directories**: describe the dataset/collection (e.g., "Synthetic grids files that mimic the ERCOT power grid"). The nearest parent directory with a description becomes the artifact's semantic parent during sync.

## Integration with Sync

The sync script (`../sync/sync.py`) queries Purview entities by `qualified_name` (blob URL), extracts `userDescription`, and walks up the path hierarchy to find the semantic parent. See `../sync/README.md`.
