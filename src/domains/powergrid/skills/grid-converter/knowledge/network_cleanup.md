# Network Cleanup

## Why Cleanup Is Needed

After parsing and assembling all components, the network may have:
1. **Isolated buses** — buses with no connected lines or transformers (orphaned during parsing)
2. **Small disconnected islands** — clusters of buses not connected to the main grid

Both cause OPF solvers to fail or produce meaningless results.

## Algorithm

### Step 1: Remove isolated buses

Find all buses referenced by at least one line or transformer. Remove any bus not in that set, along with its attached loads and generators.

```python
connected = set()
for _, row in n.lines.iterrows():
    connected.add(row.bus0)
    connected.add(row.bus1)
for _, row in n.transformers.iterrows():
    connected.add(row.bus0)
    connected.add(row.bus1)

isolated = set(n.buses.index) - connected
# Remove isolated buses and their dependents
```

### Step 2: Extract largest connected component (LCC)

Build an adjacency graph from lines and transformers. Run BFS from each unvisited bus to discover connected components. Keep only the largest one.

```python
# Build adjacency list
adj = {}
for _, row in n.lines.iterrows():
    adj.setdefault(row.bus0, set()).add(row.bus1)
    adj.setdefault(row.bus1, set()).add(row.bus0)
for _, row in n.transformers.iterrows():
    adj.setdefault(row.bus0, set()).add(row.bus1)
    adj.setdefault(row.bus1, set()).add(row.bus0)

# BFS to find components
visited = set()
components = []
for bus in n.buses.index:
    if bus in visited:
        continue
    comp = set()
    queue = [bus]
    while queue:
        b = queue.pop()
        if b in visited:
            continue
        visited.add(b)
        comp.add(b)
        for nb in adj.get(b, []):
            if nb not in visited:
                queue.append(nb)
    components.append(comp)

# Keep only the largest
if len(components) > 1:
    lcc = max(components, key=len)
    drop_buses = set(n.buses.index) - lcc
    # Remove dropped buses and their dependents
```

### Removing buses and dependents

When removing a set of buses, also remove all attached components:

```python
def remove_buses_and_dependents(n, bus_set):
    # Remove loads
    orphan_loads = n.loads[n.loads.bus.isin(bus_set)].index
    if len(orphan_loads):
        n.remove("Load", list(orphan_loads))

    # Remove generators
    orphan_gens = n.generators[n.generators.bus.isin(bus_set)].index
    if len(orphan_gens):
        n.remove("Generator", list(orphan_gens))

    # Remove lines
    drop_lines = n.lines[
        n.lines.bus0.isin(bus_set) | n.lines.bus1.isin(bus_set)
    ].index
    if len(drop_lines):
        n.remove("Line", list(drop_lines))

    # Remove transformers
    drop_xfmrs = n.transformers[
        n.transformers.bus0.isin(bus_set) | n.transformers.bus1.isin(bus_set)
    ].index
    if len(drop_xfmrs):
        n.remove("Transformer", list(drop_xfmrs))

    # Remove the buses themselves
    n.remove("Bus", list(bus_set))
```

## Typical Results

For a Virginia DVP grid: ~1,435 raw buses → ~1,400 after cleanup. Typically drops 5-6 small fragments of 2-5 buses each.

## When to Run

Run cleanup **after** all components (buses, lines, transformers, generators, loads) have been added, but **before** exporting or solving.
