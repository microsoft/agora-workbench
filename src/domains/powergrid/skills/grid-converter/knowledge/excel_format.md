# Excel Format: PJM N-1 Study Files

## Expected Sheets

PJM N-1 study xlsx files typically contain these sheets:

| Sheet | Content | Required |
|-------|---------|----------|
| `Summary - Bus N-1` | All buses with ID, name, kV, coordinates, LSC | Yes |
| `Summary - Line N-1` | All transmission lines with from/to, kV, ratings | Yes |
| `RAW result - BUS N-1` | PSLF model data: line lengths, base flows, ratings | Recommended |
| `RAW result - LineTap N-1` | Additional PSLF data | Optional |

**Important**: Sheet names and column layouts vary across studies. Always inspect the actual xlsx before writing parsing code. Use `wb.sheetnames` to list sheets and read the first few rows to identify columns.

## Bus Sheet Columns (typical DVP layout)

| Col Index | Content | Notes |
|-----------|---------|-------|
| 0 | Bus ID | Numeric or alphanumeric |
| 2 | Bus Name | May contain `_GEN` suffix for generators |
| 3 | Voltage (kV) | Base voltage |
| 5 | County | |
| 6 | State | |
| 9 | Zone | Sub-region within the study area |
| 10 | Latitude | May be missing for some buses |
| 11 | Longitude | May be missing for some buses |
| 13 | Load Serving Capacity (MW) | Used for demand allocation; may be negative (treat as 0) |

## Parsing Buses

```python
buses = {}
for row in ws.iter_rows(min_row=2, values_only=True):
    if row[0] is None:
        continue
    bid = str(row[0])
    lsc = safe_float(row[13], 0.0)
    if lsc < 0:
        lsc = 0.0
    buses[bid] = {
        "name": str(row[2]).strip() if row[2] else bid,
        "kv": safe_float(row[3], 230.0),
        "lat": safe_float(row[10]),
        "lon": safe_float(row[11]),
        "load_serving_capacity_mw": lsc,
        "zone": str(row[9]).strip() if row[9] else "",
        "is_gen": False,
    }
```

## Line Sheet Columns (typical DVP layout)

| Col Index | Content | Notes |
|-----------|---------|-------|
| 0 | From Bus ID | |
| 1 | To Bus ID | |
| 2 | Circuit number | e.g., "1", "2" for parallel circuits |
| 4 | From Bus Name | |
| 5 | To Bus Name | |
| 6 | Voltage (kV) | |
| 7 | Rating | Format: "4295/4357" (base/continuous MVA) |
| 14 | Latitude | Of one endpoint |
| 15 | Longitude | Of one endpoint |

**Rating parsing:** Split `"4295/4357"` on `/` to get (base_mva, continuous_mva).

## External Buses

Lines may reference bus IDs not present in the bus table. These are **external buses** — tie points to adjacent utilities or ISOs. Create them on the fly using the kV and coordinates from the line entry, with LSC = 0.

Check if the bus name contains `_GEN`, `GEN_`, or ends with `GEN` to flag it as a generator bus. External buses that are NOT generators become **external tie-point generators** (large equivalent generators representing the rest of the grid — see generators.md).

## RAW Sheet Columns

The RAW sheets contain per-contingency results. Each monitored facility has a line length and base-case power flow. Extract unique values keyed by `(min(fr, to), max(fr, to), ckt)`.

| Col Index | Content |
|-----------|---------|
| 4 | From Bus ID (int) |
| 6 | To Bus ID (int) |
| 8 | Circuit ID |
| 16 | Base MVA rating |
| 17 | Continuous MVA rating |
| 20 | Base-case power flow (MW) |
| 24 | Line length (miles) |

**Length conversion:** `km = miles × 1.609344`

**Deduplication:** RAW sheets have many rows (one per contingency × monitored facility). Use first-seen value for each unique branch key:

```python
raw_lengths = {}
for row in ws.iter_rows(min_row=2, values_only=True):
    fr, to = safe_int(row[4]), safe_int(row[6])
    if fr is None or to is None:
        continue
    ckt = str(row[8]).strip() if row[8] else "1"
    key = (min(fr, to), max(fr, to), ckt)
    length = safe_float(row[24])
    if length and length > 0 and key not in raw_lengths:
        raw_lengths[key] = length * 1.609344
```

## Multi-ISO Considerations

Some xlsx files span multiple ISOs. In that case:
- The Zone column identifies which ISO/utility each bus belongs to
- Demand allocation should be done per-zone, not globally
- External tie generators should reflect the adjacent ISO's average LMP
- Each zone may need its own EIA-930 demand data source
