# Energy Systems MCP Server (PyPSA)

A domain-specific MCP code execution server for power system modeling and analysis, powered by [PyPSA](https://pypsa.org/) (Python for Power System Analysis) with the free [HiGHS](https://highs.dev/) LP/MIP solver.

Exposes an `execute_energysystems_code` MCP tool that runs Python code in an isolated environment with power system modeling, optimization, and geospatial packages pre-installed.

## Pre-installed Packages

| Package | Purpose |
| --- | --- |
| **pypsa** | Power system modeling: networks, generators, loads, optimal power flow |
| **linopy** | Linear optimization modeling (PyPSA's optimization backend) |
| **highspy** | HiGHS solver — free LP/MIP/QP solver, no license required |
| **numpy** | Numerical computing |
| **pandas** | Time series and tabular data |
| **scipy** | Scientific computing, sparse matrices |
| **matplotlib** | Visualization |
| **networkx** | Graph/network topology analysis |
| **geopandas** | Geospatial vector data (bus locations, transmission corridors) |
| **shapely** | Geometric operations |
| **xarray** | N-dimensional labeled arrays (weather/climate data) |
| **netcdf4** | Read/write NetCDF files (PyPSA network export format) |
| **plotly** | Interactive plotting |
| **seaborn** | Statistical visualization |

## Quick Start

### 1. Build the base image (one-time)

```bash
# From the repository root:
docker build -f src/agora_workbench/deployment/templates/docker/base.Dockerfile -t mcp-server-base:local .
```

### 2. Build and run the energy systems server

```bash
cd examples/domain_examples/energysystems
docker compose up --build
```

The server will be available at `http://localhost:8022`. The first startup takes a few minutes while the conda environment is built (subsequent starts are cached).

### 3. Verify

```bash
curl http://localhost:8022/health
```

## Usage Examples

The `execute_energysystems_code` tool accepts Python code. Common modules are auto-imported (`pypsa`, `numpy as np`, `pandas as pd`, `networkx as nx`, `matplotlib.pyplot as plt`).

The energy-systems domain tools follow a live-object pattern: `define_network`
returns a live `pypsa.Network`, and subsequent tools accept that same object.

```python
network = define_network(name="grid1", snapshots=48)
add_components(
    network=network,
    buses=[{"name": "North"}, {"name": "South"}],
    lines=[{"name": "N-S", "bus0": "North", "bus1": "South", "s_nom": 500, "x": 0.01}],
)
opf = run_optimal_power_flow(network=network)
```

### Create a simple 3-bus network

```python
# Create a network with 3 buses and 3 lines
n = pypsa.Network()
n.set_snapshots(pd.date_range("2024-01-01", periods=24, freq="h"))

# Add buses
n.add("Bus", "Bus 0", v_nom=380)
n.add("Bus", "Bus 1", v_nom=380)
n.add("Bus", "Bus 2", v_nom=380)

# Add lines
n.add("Line", "Line 0-1", bus0="Bus 0", bus1="Bus 1", s_nom=1000, x=0.01)
n.add("Line", "Line 1-2", bus0="Bus 1", bus1="Bus 2", s_nom=500, x=0.02)
n.add("Line", "Line 0-2", bus0="Bus 0", bus1="Bus 2", s_nom=800, x=0.015)

# Add a generator and load
n.add("Generator", "Coal", bus="Bus 0", p_nom=500, marginal_cost=30)
n.add("Generator", "Wind", bus="Bus 1", p_nom=300, marginal_cost=0,
      p_max_pu=np.random.uniform(0.2, 0.9, 24))
n.add("Load", "Load", bus="Bus 2", p_set=400)

print(f"Network: {len(n.buses)} buses, {len(n.lines)} lines")
print(f"Generators: {list(n.generators.index)}")
print(f"Total generation capacity: {n.generators.p_nom.sum()} MW")
```

### Run optimal power flow (linear OPF)

```python
n = pypsa.Network()
n.set_snapshots(pd.date_range("2024-01-01", periods=24, freq="h"))

n.add("Bus", "North")
n.add("Bus", "South")
n.add("Line", "North-South", bus0="North", bus1="South", s_nom=200, x=0.01)

# Generators with different marginal costs
n.add("Generator", "Gas", bus="North", p_nom=500, marginal_cost=50)
n.add("Generator", "Solar", bus="South", p_nom=400, marginal_cost=0,
      p_max_pu=[0]*6 + [0.3, 0.6, 0.8, 0.9, 0.95, 1.0,
                0.95, 0.9, 0.8, 0.6, 0.3, 0] + [0]*6)

n.add("Load", "City", bus="South", p_set=300)

# Solve linear optimal power flow
status = n.optimize(solver_name="highs")
print(f"Solver status: {status[0]}")
print(f"Total system cost: ${n.objective:.2f}")
print(f"\nGenerator dispatch (first 6 hours):")
print(n.generators_t.p.head(6))
```

### Capacity expansion planning

```python
n = pypsa.Network()
n.set_snapshots(pd.date_range("2024-01-01", periods=8760, freq="h"))

n.add("Bus", "System")

# Existing coal plant
n.add("Generator", "Coal", bus="System", p_nom=1000, marginal_cost=40,
      carrier="coal")

# Expandable renewables (capital cost drives investment decisions)
n.add("Generator", "Wind", bus="System", p_nom_extendable=True,
      capital_cost=1200000, marginal_cost=0, carrier="wind",
      p_max_pu=np.random.beta(2, 5, 8760))  # Simulated wind capacity factors

n.add("Generator", "Solar", bus="System", p_nom_extendable=True,
      capital_cost=800000, marginal_cost=0, carrier="solar",
      p_max_pu=np.clip(np.sin(np.linspace(0, 2*np.pi*365, 8760)) * 0.5 + 0.1, 0, 1))

# Battery storage
n.add("StorageUnit", "Battery", bus="System", p_nom_extendable=True,
      capital_cost=300000, marginal_cost=1, max_hours=4, carrier="battery")

# Demand (seasonal pattern)
hourly_demand = 2000 + 500 * np.sin(np.linspace(0, 2*np.pi*365, 8760))
n.add("Load", "Demand", bus="System", p_set=hourly_demand)

# Optimize with capacity expansion
status = n.optimize(solver_name="highs")
print(f"Status: {status[0]}, Cost: ${n.objective:,.0f}")
print(f"\nOptimal capacities:")
print(f"  Wind:    {n.generators.loc['Wind', 'p_nom_opt']:.0f} MW")
print(f"  Solar:   {n.generators.loc['Solar', 'p_nom_opt']:.0f} MW")
print(f"  Battery: {n.storage_units.loc['Battery', 'p_nom_opt']:.0f} MW")
```

### Analyze network topology

```python
n = pypsa.Network()

# Create a meshed network
buses = [f"Bus_{i}" for i in range(6)]
for bus in buses:
    n.add("Bus", bus)

lines = [("Bus_0", "Bus_1"), ("Bus_0", "Bus_2"), ("Bus_1", "Bus_3"),
         ("Bus_2", "Bus_3"), ("Bus_2", "Bus_4"), ("Bus_3", "Bus_5"),
         ("Bus_4", "Bus_5")]

for i, (b0, b1) in enumerate(lines):
    n.add("Line", f"Line_{i}", bus0=b0, bus1=b1, s_nom=100, x=0.01)

# Analyze with networkx
G = n.graph()
print(f"Nodes: {G.number_of_nodes()}, Edges: {G.number_of_edges()}")
print(f"Connected: {nx.is_connected(G)}")
print(f"Diameter: {nx.diameter(G)}")
print(f"Average path length: {nx.average_shortest_path_length(G):.2f}")
print(f"\nDegree centrality:")
for node, centrality in sorted(nx.degree_centrality(G).items(),
                                 key=lambda x: -x[1]):
    print(f"  {node}: {centrality:.3f}")
```

## Solver

This server uses [HiGHS](https://highs.dev/) — a free, open-source, high-performance LP/MIP/QP solver. No commercial license is needed. HiGHS is the default solver for PyPSA when available.

For larger problems, you can also use:
- `glpk` (free, slower for large MIPs)
- `gurobi` (commercial, requires license — not included by default)

## Authentication

This example uses `create_noop_auth_config()` (no authentication required for the MCP server). For production deployments with Entra ID, see the [deployment guide](../../../docs/guide/deploying.md).
