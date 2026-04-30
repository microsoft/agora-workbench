---
name: network-analysis
description: Analyze power network topology, connectivity, and structural properties using PyPSA and NetworkX, including finding isolated components, critical lines, and network statistics.
---

# Network Topology Analysis

Use this skill when the user asks about network structure, connectivity,
topology metrics, critical infrastructure identification, or network statistics.

## Loading and Inspecting a Network

```python
import pypsa

network = pypsa.Network("path/to/network.nc")

# Quick overview
print(f"Buses: {len(network.buses)}")
print(f"Lines: {len(network.lines)}")
print(f"Generators: {len(network.generators)}")
print(f"Loads: {len(network.loads)}")
print(f"Transformers: {len(network.transformers)}")

# Full statistics
network.statistics()
```

## Converting to NetworkX for Graph Analysis

```python
import networkx as nx

# Build graph from PyPSA network
G = nx.Graph()
for idx, line in network.lines.iterrows():
    G.add_edge(line.bus0, line.bus1, name=idx, s_nom=line.s_nom, length=line.length)
for idx, trafo in network.transformers.iterrows():
    G.add_edge(trafo.bus0, trafo.bus1, name=idx, s_nom=trafo.s_nom)
```

## Common Analysis Tasks

### Connectivity Check

```python
components = list(nx.connected_components(G))
print(f"Connected components: {len(components)}")
if len(components) > 1:
    for i, comp in enumerate(components):
        print(f"  Component {i}: {len(comp)} buses")
```

### Critical Line Identification (Bridges)

```python
bridges = list(nx.bridges(G))
print(f"Critical lines (bridges): {len(bridges)}")
# These are single points of failure — removing any bridge disconnects the network
```

### Degree Analysis

```python
degrees = dict(G.degree())
# High-degree buses are major substations/hubs
hubs = sorted(degrees.items(), key=lambda x: x[1], reverse=True)[:10]
```

### Shortest Path Between Buses

```python
path = nx.shortest_path(G, source="bus_A", target="bus_B")
print(f"Shortest path: {' -> '.join(path)} ({len(path)-1} hops)")
```

## Identifying Overloaded Lines

After running power flow or OPF:

```python
loading = network.lines_t.p0.abs() / network.lines.s_nom
overloaded = loading.max()[loading.max() > 0.9]
print(f"Lines loaded > 90%: {len(overloaded)}")
```

## Network Summary Template

When reporting network statistics, include:

1. **Size**: bus count, line count, generator count, load count
2. **Connectivity**: number of connected components, any isolated buses
3. **Capacity**: total generation capacity vs total load
4. **Voltage levels**: unique voltage levels present
5. **Critical infrastructure**: bridges, high-degree hubs

For the full list of recommended metrics, see [references/metrics.md](references/metrics.md).
