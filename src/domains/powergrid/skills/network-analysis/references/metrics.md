# Network Metrics Reference

## Structural Metrics

| Metric | Description | How to Compute |
|--------|-------------|----------------|
| Bus count | Total number of buses/nodes | `len(network.buses)` |
| Line count | Total number of transmission lines | `len(network.lines)` |
| Transformer count | Total number of transformers | `len(network.transformers)` |
| Connected components | Number of isolated sub-networks | `nx.number_connected_components(G)` |
| Diameter | Longest shortest path in the network | `nx.diameter(G)` (only if connected) |
| Average degree | Mean number of connections per bus | `2 * G.number_of_edges() / G.number_of_nodes()` |
| Bridges | Lines whose removal disconnects the network | `list(nx.bridges(G))` |
| Articulation points | Buses whose removal disconnects the network | `list(nx.articulation_points(G))` |

## Capacity Metrics

| Metric | Description | How to Compute |
|--------|-------------|----------------|
| Total generation capacity | Sum of all generator nominal power | `network.generators.p_nom.sum()` |
| Total load | Sum of all load active power | `network.loads.p_set.sum()` |
| Reserve margin | (Gen capacity - Load) / Load | `(gen_cap - total_load) / total_load` |
| Line utilization | Fraction of thermal capacity used | `lines_t.p0.abs() / lines.s_nom` |

## Voltage Level Summary

```python
voltage_levels = network.buses.v_nom.unique()
for v in sorted(voltage_levels):
    count = (network.buses.v_nom == v).sum()
    print(f"  {v} kV: {count} buses")
```

## Generation Mix

```python
gen_by_carrier = network.generators.groupby("carrier").p_nom.sum()
print(gen_by_carrier.sort_values(ascending=False))
```
