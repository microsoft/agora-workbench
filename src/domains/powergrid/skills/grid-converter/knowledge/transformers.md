# Transformer Detection

## Algorithm

Transformers connect buses at different voltage levels within the same substation.

### Step 1: Group buses by substation name

Clean bus names to extract a base substation name:

```python
import re

def clean_sub_name(name):
    if not name:
        return ""
    n = re.sub(r"^\d+", "", str(name)).strip()  # strip leading digits
    for sfx in [" 230", " 115", " 500", " 138", " 69", " 345", " 765", " TAP", " T"]:
        if n.endswith(sfx):
            n = n[:-len(sfx)].strip()
    return n
```

Examples:
- `"345 CLOVERDALE 345"` → `"CLOVERDALE"`
- `"115 CLOVERDALE 115"` → `"CLOVERDALE"`
- `"BREMO 230"` → `"BREMO"`

Group all buses by their cleaned substation name.

### Step 2: Create transformers between voltage pairs

For each substation group with 2+ buses at different voltages:
1. Sort buses by kV descending
2. Create a transformer between each adjacent voltage pair

Example: Substation "CLOVERDALE" has buses at 345 kV, 230 kV, 69 kV:
- Transformer 1: 345 kV bus → 230 kV bus
- Transformer 2: 230 kV bus → 69 kV bus

Skip pairs where both buses have the same kV.

### Step 3: Infer s_nom (MVA rating)

The transformer MVA rating is inferred from the highest-rated line connected to either bus:

```python
# Pre-compute max line rating at each bus
max_rating = {}
for line in lines:
    for bid in (line["fr"], line["to"]):
        if line["rate_a"] > 0:
            max_rating[bid] = max(max_rating.get(bid, 0), line["rate_a"])

# For each transformer
s_nom = max(max_rating.get(hv_bus_id, 0), max_rating.get(lv_bus_id, 0))
if s_nom <= 0:
    s_nom = 750.0  # default fallback
```

### Step 4: Set impedance based on voltage ratio

| Type | Condition | R (p.u.) | X (p.u.) | Example |
|------|-----------|----------|----------|---------|
| Autotransformer | HV/LV < 2.5 | 0.003 | 0.10 | 500/230, 230/138 |
| Two-winding | HV/LV ≥ 2.5 | 0.003 | 0.15 | 230/69 |

Autotransformers (lower voltage ratio) have lower leakage reactance because the windings share a common core section.

## Adding Transformers to PyPSA

```python
ratio = hv_kv / lv_kv if lv_kv > 0 else 99
x_pu = 0.10 if ratio < 2.5 else 0.15

n.add("Transformer", xfmr_name,
    bus0=f"bus_{hv_bus_id}",
    bus1=f"bus_{lv_bus_id}",
    r=0.003,
    x=x_pu,
    s_nom=s_nom,
    tap_ratio=1.0,
)
```

## Notes

- The substation name cleaning is heuristic and may need adjustment for different xlsx naming conventions — always verify by inspecting actual bus names in the data
- Some studies use explicit transformer tables — prefer those when available
- If the xlsx has a "Transformer" sheet, use that directly instead of detecting from bus names
