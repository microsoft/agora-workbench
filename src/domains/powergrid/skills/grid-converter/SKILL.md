---
name: grid-converter
description: "Build a solvable PyPSA power grid model from a PJM N-1 study Excel file (.xlsx). Teaches parsing buses/lines, computing impedance, matching generators to EIA/OSM plant data, allocating demand from EIA-930, detecting transformers, and cleaning up the network."
---

# Grid Converter Skill

## When to Use

When asked to convert a PJM N-1 study Excel file into a PyPSA network (.nc) and/or GeoJSON map.

## Process Overview

The xlsx provides **topology** (buses + lines). To make it solvable, you must:

0. **Acquire external data** — EIA demand, EIA plants, optionally OSM features
1. **Parse buses and lines** from the xlsx sheets
2. **Extract line lengths** from RAW sheets (if available)
3. **Detect transformers** between voltage levels at the same substation
4. **Compute line impedance** (R, X, B) from voltage class + line length + SIL bundling
5. **Identify and match generators** to EIA/OSM plant data for real capacity and fuel costs
6. **Allocate demand** from EIA-930 hourly data, distributed by Load Serving Capacity
7. **Clean up the network** — remove isolated buses, keep largest connected component
8. **Export** to PyPSA NetCDF (.nc) and optionally GeoJSON

## Knowledge Pages

For each step, read the corresponding knowledge page for full details:

| Step | What to do | Knowledge page |
|------|-----------|----------------|
| 0 | Download EIA demand, EIA plants, OSM data | [knowledge/data_fetching.md](knowledge/data_fetching.md) |
| 1-2 | Parse buses, lines, RAW sheets from xlsx | [knowledge/excel_format.md](knowledge/excel_format.md) |
| 3 | Detect transformers between voltage levels | [knowledge/transformers.md](knowledge/transformers.md) |
| 4 | Compute line impedance with SIL bundling | [knowledge/impedance.md](knowledge/impedance.md) |
| 5 | Identify generator buses, match to EIA/OSM | [knowledge/generators.md](knowledge/generators.md) |
| 6 | Allocate demand to buses | [knowledge/demand.md](knowledge/demand.md) |
| 7 | Remove isolated buses, extract LCC | [knowledge/network_cleanup.md](knowledge/network_cleanup.md) |

State code lookup tables (ISO, FIPS, abbreviations) for all 50 US states are available in [knowledge/state_codes.json](knowledge/state_codes.json).

## Key Principle

The xlsx format varies across studies (different sheet names, column layouts, zone semantics). **Always inspect the actual xlsx first** (list sheet names, read header rows), then write parsing code adapted to it. The physics (impedance, SIL, fuel costs) and algorithms (LCC extraction, demand allocation) are universal and described in the knowledge pages.

## Typical Pipeline Structure

```python
import openpyxl
import pypsa

# 0. Download external data (see data_fetching.md)
#    - EIA-930 demand for the target BA and date
#    - EIA-860 plant data for the target state
#    - Optionally OSM power features for plant matching

# 1. Parse xlsx
wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
# Parse buses from "Summary - Bus N-1" (adapt columns to actual layout)
# Parse lines from "Summary - Line N-1"
# Extract RAW line lengths from "RAW result - BUS N-1" / "RAW result - LineTap N-1"
wb.close()

# 2. Detect transformers (same-substation buses at different kV)
# 3. Build PyPSA network: add buses, lines (with impedance), transformers
# 4. Match generator buses to EIA/OSM plants, add generators
# 5. Allocate demand to load buses
# 6. Cleanup: remove isolated buses, keep LCC
# 7. Export: n.export_to_netcdf("grid.nc")
```

## Required Packages

- `pypsa` — network construction and export
- `openpyxl` — Excel file parsing
- `requests` — downloading EIA/OSM data from APIs

All are available in the powergrid execution environment.

## Output Files

- **`.nc`** (NetCDF) — solvable PyPSA network, can be passed to `run_opf` tool or loaded with `pypsa.Network("file.nc")`
- **`.geojson`** (optional) — buses as Points + lines as LineStrings for map visualization
