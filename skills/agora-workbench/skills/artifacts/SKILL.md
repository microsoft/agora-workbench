---
name: artifacts
description: >
  Fetch data assets into the kernel, transfer Python objects between servers,
  and publish output files to the user. Activate when working with data files,
  cross-server data movement, or when the user asks for downloads or exports.
---

# Artifacts: Fetch, Transfer, and Publish

## Discovering Data Assets

Servers with a data catalog expose tools for finding available datasets:

```
search_data(query="temperature observations", domain="earthscience", top=5)
list_domains()                    # See all available data domains
get_artifact(artifact_id="...")   # Get full metadata for a specific artifact
query_catalog(sql="SELECT name, domain, description FROM artifacts WHERE domain = 'powergrid'")
```

**Not all servers have a data catalog.** These tools (`search_data`,
`list_domains`, `get_artifact`, `query_catalog`) are registered only when the
server is configured with one — and unlike most tools they are **not** prefixed
with the server name. If they are not in the server's tool list, the server has
no catalog: skip them and use the data references the user provides directly.
Configuring a catalog is a server-author/deployment task, not something you can
do from the client side.

When present, results include the artifact's `storage_uri`, `domain`,
`source_type` (local or blob), `description`, and `content_type`. Use these
tools to find the correct asset reference before using it in code.

## Fetching Data Assets

Asset references use a **type-tagged format** where the tag indicates the storage
backend and the inner value is the artifact identifier to resolve:

| Tag format | Storage location | Example |
|------------|------------------|---------|
| `<blob>{artifact_id}</blob>` | Azure Blob Storage (resolved server-side from the artifact ID) | `<blob>abc123</blob>` |
| `<local>/data/grid/lines.geojson</local>` | Server local filesystem | `<local>/data/grid/lines.geojson</local>` |

For blob assets, use the artifact `id` returned by `search_data` / `get_artifact` inside the `<blob>…</blob>` tag (not a blob path).
Embed these tagged references as string literals in your code — the server
automatically detects them, downloads the file to a local cache, and replaces
the literal with a `Path` variable:

```python
# The tag tells the server which asset to resolve; it is replaced with a local Path at runtime
df = pd.read_csv("<blob>abc123</blob>")
network = load_network("<local>/data/grid/texas_grid.nc</local>")
```

You do not need to handle authentication or downloads — the server's managed
identity fetches the data on your behalf.

## Cross-Server Object Transfer

Move Python objects between servers without serializing through agent context:

```
{server}_send(data_ref="molecule_data", to="gis", name="input_data")
```

### Addressing the target server

Use the **logical server name** (e.g., `"gis"`, `"chemistry"`) as the `to`
parameter. The server resolves this to the correct internal address based on
its deployment configuration.

If the transfer fails with a connection or trust error, the error message will
indicate the expanded URL and what went wrong. In that case, ask the user for
guidance on the correct destination name.

Do not attempt to guess deployment URLs, ports, or hostnames — the logical name
is the correct default in all environments.

### Parameters

| Parameter | Usage |
|-----------|-------|
| `data_ref` | Kernel variable name or filename in `AGORA_OUTPUT_DIR` to transfer |
| `to` | Logical destination name (e.g., `"gis"`, `"blob"`, `"user"`, `"local"`) |
| `name` | Variable name at destination (defaults to `data_ref` if empty) |
| `session_id` | Target session ID (if empty, uses the caller's first active session on target) |

### When to use object transfer

- Passing DataFrames, arrays, or complex objects between domain servers.
- Avoiding token-heavy serialization of large data in chat context.
- Multi-domain workflows where one server produces input for another.

## Publishing Artifacts

To produce downloadable files for the user:

1. Write files to the session output directory inside `execute_{server}_code`.
   The kernel pre-injects a helper and a variable for this — never resolve the
   path to an absolute string yourself:

   ```python
   # ✓ Preferred — agora_output() builds the path for you, no f-string needed
   df.to_csv(agora_output("results.csv"), index=False)

   # ✓ Also correct — use the pre-injected variable name directly
   df.to_csv(f"{AGORA_OUTPUT_DIR}/results.csv", index=False)

   # ✓ Also correct — read AGORA_OUTPUT_DIR from the environment
   import os
   df.to_csv(f"{os.environ['AGORA_OUTPUT_DIR']}/results.csv", index=False)

   # ✗ WRONG — do not hardcode the resolved path (triggers path guardrails)
   df.to_csv("/tmp/agora_output_abc123/results.csv", index=False)
   ```

   Both `agora_output("name")` and the bare `AGORA_OUTPUT_DIR` symbol are
   injected automatically — no `import` is required to use them.

2. Publish only when the user explicitly requests a download or export:
   ```
   {server}_send(data_ref="results.csv", to="user")
   ```

### Destinations

| Destination | Publisher | Use case |
|-------------|-----------|----------|
| `"user"` | GuiPublisher | Browser download for the user |
| `"blob"` | BlobPublisher | Azure Blob Storage |
| `"local"` | LocalFilePublisher | Local filesystem |

**Not all destinations are available on every server.** Available destinations
depend on the server's configured publishers. If you use an unsupported
destination, the tool returns an error listing the available options. When
unsure, try `"user"` first — the GuiPublisher is always available.

### Rules

- Only write user-facing files to `AGORA_OUTPUT_DIR` — files elsewhere stay inside the container.
- Build output paths with `agora_output("name")` or the `AGORA_OUTPUT_DIR` variable — never hardcode the resolved absolute path.
- Do not publish unless the user asks for a download or export.
- The `data_ref` must match a file already written to `AGORA_OUTPUT_DIR` (for file-based sends).
