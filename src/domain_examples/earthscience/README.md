# Earth Science MCP Server (Planetary Computer)

A domain-specific MCP code execution server for earth science and remote sensing, powered by [Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/) (free, public API — no account required).

Exposes an `execute_earthscience_code` MCP tool that runs Python code in an isolated environment with satellite imagery discovery and geospatial analysis packages pre-installed.

## Pre-installed Packages

| Package | Purpose |
|---------|---------|
| **pystac-client** | Search satellite imagery catalogs via STAC API |
| **planetary-computer** | Sign requests for Planetary Computer data access |
| **rasterio** | Read/write raster data (GeoTIFF, COG) |
| **xarray** | N-dimensional labeled array analysis |
| **rioxarray** | xarray + rasterio integration for geospatial rasters |
| **geopandas** | Vector geometry and spatial joins |
| **shapely** | Geometric operations (buffer, intersect, union) |
| **numpy** | Numerical computing |
| **pandas** | Data manipulation |
| **scipy** | Scientific computing |
| **matplotlib** | Visualization |

## Quick Start

### 1. Build the base image (one-time)

```bash
cd src
docker build -f deployment/mcp_server/base.Dockerfile -t mcp-server-base:local .
```

### 2. Build and run the earth science server

```bash
cd src/domain_examples/earthscience
docker compose up --build
```

The server will be available at `http://localhost:8021`. The first startup takes a few minutes while the conda environment is built (subsequent starts are cached).

### 3. Verify

```bash
curl http://localhost:8021/health
```

## Usage Examples

The `execute_earthscience_code` tool accepts Python code. Common geospatial modules are auto-imported (`planetary_computer`, `pystac_client`, `rasterio`, `xarray`, `rioxarray`, `geopandas`, `numpy`, `pandas`, `shapely.geometry`).

### Search for Sentinel-2 imagery

```python
catalog = pystac_client.Client.open(
    "https://planetarycomputer.microsoft.com/api/stac/v1",
    modifier=planetary_computer.sign_inplace,
)

# Search for imagery over San Francisco
search = catalog.search(
    collections=["sentinel-2-l2a"],
    bbox=[-122.5, 37.7, -122.3, 37.9],
    datetime="2024-06-01/2024-06-30",
    query={"eo:cloud_cover": {"lt": 20}},
)

items = search.item_collection()
print(f"Found {len(items)} scenes")
for item in items[:3]:
    print(f"  {item.id} — {item.datetime} — cloud: {item.properties['eo:cloud_cover']}%")
```

### Compute NDVI from a Sentinel-2 scene

```python
catalog = pystac_client.Client.open(
    "https://planetarycomputer.microsoft.com/api/stac/v1",
    modifier=planetary_computer.sign_inplace,
)

search = catalog.search(
    collections=["sentinel-2-l2a"],
    bbox=[-122.5, 37.7, -122.3, 37.9],
    datetime="2024-06-15",
    query={"eo:cloud_cover": {"lt": 10}},
    max_items=1,
)
item = next(search.items())

# Read red (B04) and NIR (B08) bands
red_href = item.assets["B04"].href
nir_href = item.assets["B08"].href

with rasterio.open(red_href) as src:
    red = src.read(1, window=rasterio.windows.Window(0, 0, 512, 512)).astype(float)
with rasterio.open(nir_href) as src:
    nir = src.read(1, window=rasterio.windows.Window(0, 0, 512, 512)).astype(float)

# Compute NDVI
ndvi = (nir - red) / (nir + red + 1e-10)
print(f"NDVI range: [{ndvi.min():.3f}, {ndvi.max():.3f}]")
print(f"Mean NDVI: {ndvi.mean():.3f}")
```

### Load raster data with xarray

```python
catalog = pystac_client.Client.open(
    "https://planetarycomputer.microsoft.com/api/stac/v1",
    modifier=planetary_computer.sign_inplace,
)

search = catalog.search(
    collections=["landsat-c2-l2"],
    bbox=[-105.3, 39.9, -105.1, 40.1],
    datetime="2024-07-01/2024-07-31",
    max_items=1,
)
item = next(search.items())

# Open surface reflectance band as xarray
ds = xr.open_dataarray(item.assets["green"].href, engine="rasterio")
print(ds)
print(f"Shape: {ds.shape}, CRS: {ds.rio.crs}")
```

### List available datasets

```python
catalog = pystac_client.Client.open(
    "https://planetarycomputer.microsoft.com/api/stac/v1",
)

collections = catalog.get_collections()
for c in list(collections)[:10]:
    print(f"{c.id}: {c.title}")
```

## Data Access

The Planetary Computer STAC API is **free and publicly accessible**. No API key or account is needed for:
- Searching the catalog (STAC queries)
- Downloading data (throttled for anonymous access)

For higher throughput, the `planetary_computer.sign_inplace` modifier automatically handles SAS token signing — also free.

## Authentication

This example uses `create_noop_auth_config()` (no authentication required for the MCP server). For production deployments with Entra ID, see the [deployment README](../../deployment/mcp_server/README.md).
